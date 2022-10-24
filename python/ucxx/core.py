# Copyright (c) 2019-2021, NVIDIA CORPORATION. All rights reserved.
# Copyright (c) 2020       UT-Battelle, LLC. All rights reserved.
# See file LICENSE for terms.

import array
import asyncio
import gc
import logging
import os
import struct
import threading
import weakref
from functools import partial
from os import close as close_fd
from queue import Queue

from ._lib import libucxx as ucx_api
from ._lib.arr import Array
from ._lib.libucxx import (
    UCXBaseException,
    UCXCanceled,
    UCXCloseError,
    UCXConnectionResetError,
    UCXError,
)
from .continuous_ucx_progress import PollingMode, ThreadMode
from .utils import hash64bits

logger = logging.getLogger("ucx")

# The module should only instantiate one instance of the application context
# However, the init of CUDA must happen after all process forks thus we delay
# the instantiation of the application context to the first use of the API.
_ctx = None


def _get_ctx():
    global _ctx
    if _ctx is None:
        _ctx = ApplicationContext()
    return _ctx


async def exchange_peer_info(endpoint, msg_tag, ctrl_tag, listener):
    """Help function that exchange endpoint information"""

    # Pack peer information incl. a checksum
    fmt = "QQQ"
    my_info = struct.pack(fmt, msg_tag, ctrl_tag, hash64bits(msg_tag, ctrl_tag))
    peer_info = bytearray(len(my_info))
    my_info_arr = Array(my_info)
    peer_info_arr = Array(peer_info)

    # Send/recv peer information. Notice, we force an `await` between the two
    # streaming calls (see <https://github.com/rapidsai/ucx-py/pull/509>)
    if listener is True:
        req = endpoint.stream_send(my_info_arr)
        await req.wait()
        req = endpoint.stream_recv(peer_info_arr)
        await req.wait()
    else:
        req = endpoint.stream_recv(peer_info_arr)
        await req.wait()
        req = endpoint.stream_send(my_info_arr)
        await req.wait()

    # Unpacking and sanity check of the peer information
    ret = {}
    (ret["msg_tag"], ret["ctrl_tag"], ret["checksum"]) = struct.unpack(fmt, peer_info)

    expected_checksum = hash64bits(ret["msg_tag"], ret["ctrl_tag"])

    if expected_checksum != ret["checksum"]:
        raise RuntimeError(
            f'Checksum invalid! {hex(expected_checksum)} != {hex(ret["checksum"])}'
        )

    return ret


async def _listener_handler_coroutine(conn_request, ctx, func, endpoint_error_handling):
    # def _listener_handler_coroutine(conn_request, ctx, func, endpoint_error_handling):
    # We create the Endpoint in five steps:
    #  1) Create endpoint from conn_request
    #  2) Generate unique IDs to use as tags
    #  3) Exchange endpoint info such as tags
    #  4) Setup control receive callback
    #  5) Execute the listener's callback function
    endpoint = conn_request

    seed = os.urandom(16)
    msg_tag = hash64bits("msg_tag", seed, endpoint.handle)
    ctrl_tag = hash64bits("ctrl_tag", seed, endpoint.handle)

    peer_info = await exchange_peer_info(
        endpoint=endpoint,
        msg_tag=msg_tag,
        ctrl_tag=ctrl_tag,
        listener=True,
    )
    tags = {
        "msg_send": peer_info["msg_tag"],
        "msg_recv": msg_tag,
        "ctrl_send": peer_info["ctrl_tag"],
        "ctrl_recv": ctrl_tag,
    }
    ep = Endpoint(endpoint=endpoint, ctx=ctx, tags=tags)

    logger.debug(
        "_listener_handler() server: %s, error handling: %s, msg-tag-send: %s, "
        "msg-tag-recv: %s, ctrl-tag-send: %s, ctrl-tag-recv: %s"
        % (
            hex(endpoint.handle),
            endpoint_error_handling,
            hex(ep._tags["msg_send"]),
            hex(ep._tags["msg_recv"]),
            hex(ep._tags["ctrl_send"]),
            hex(ep._tags["ctrl_recv"]),
        )
    )

    # Removing references here to avoid delayed clean up
    del ctx

    # Finally, we call `func`
    if asyncio.iscoroutinefunction(func):
        try:
            await func(ep)
        except Exception as e:
            logger.error(f"Uncatched listener callback error {type(e)}: {e}")
    else:
        func(ep)


def _listener_handler(
    conn_request, event_loop, callback_func, ctx, endpoint_error_handling
):
    asyncio.run_coroutine_threadsafe(
        _listener_handler_coroutine(
            conn_request,
            ctx,
            callback_func,
            endpoint_error_handling,
        ),
        event_loop,
    )


async def _run_request_notifier(worker):
    worker.run_request_notifier()


async def _notifier_coroutine(worker):
    worker.populate_python_futures_pool()
    finished = worker.wait_request_notifier()
    if finished:
        return True

    # Notify all enqueued waiting futures
    await _run_request_notifier(worker)

    return False


def _notifierThread(event_loop, worker, q):
    logger.debug("Starting Notifier Thread")
    asyncio.set_event_loop(event_loop)
    shutdown = False

    while True:
        worker.populate_python_futures_pool()
        state = worker.wait_request_notifier(period_ns=int(1e9))  # 1 second timeout

        if not q.empty():
            q_val = q.get()
            if q_val == "shutdown":
                logger.warning("_notifierThread shutting down")
                shutdown = True
            else:
                logger.warning(
                    f"_notifierThread got unknown message from IPC queue: {q_val}"
                )

        if state == ucx_api.PythonRequestNotifierWaitState.Shutdown or shutdown is True:
            return
        elif state == ucx_api.PythonRequestNotifierWaitState.Timeout:
            continue

        # Notify all enqueued waiting futures
        task = asyncio.run_coroutine_threadsafe(
            _run_request_notifier(worker), event_loop
        )
        task.result()


class ApplicationContext:
    """
    The context of the Asyncio interface of UCX.
    """

    def __init__(
        self,
        config_dict={},
        progress_mode=None,
        enable_delayed_submission=None,
        enable_python_future=None,
    ):
        self.progress_tasks = []
        loop = asyncio.get_event_loop()
        self.notifier_thread_q = None
        self.notifier_thread = None

        self.progress_mode = ApplicationContext._check_progress_mode(progress_mode)

        enable_delayed_submission = ApplicationContext._check_enable_delayed_submission(
            enable_delayed_submission
        )
        enable_python_future = ApplicationContext._check_enable_python_future(
            enable_python_future, self.progress_mode
        )

        # For now, a application context only has one worker
        self.context = ucx_api.UCXContext(config_dict)
        self.worker = ucx_api.UCXWorker(
            self.context,
            enable_delayed_submission=enable_delayed_submission,
            enable_python_future=enable_python_future,
        )

        self.start_notifier_thread()

        weakref.finalize(self, self.progress_tasks.clear)

        # Ensure progress even before Endpoints get created, for example to
        # receive messages directly on a worker after a remote endpoint
        # connected with `create_endpoint_from_worker_address`.
        self.continuous_ucx_progress()

    @staticmethod
    def _check_progress_mode(progress_mode):
        if progress_mode is None:
            if "UCXPY_PROGRESS_MODE" in os.environ:
                progress_mode = os.environ["UCXPY_PROGRESS_MODE"]
            else:
                progress_mode = "thread"

        valid_progress_modes = ["polling", "thread", "thread-polling"]
        if not isinstance(progress_mode, str) or not any(
            progress_mode == m for m in valid_progress_modes
        ):
            raise ValueError(
                f"Unknown progress mode {progress_mode}, "
                "valid modes are: 'blocking', 'polling', 'thread' or 'thread-polling'"
            )

        return progress_mode

    @staticmethod
    def _check_enable_delayed_submission(enable_delayed_submission):
        if enable_delayed_submission is None:
            if "UCXPY_ENABLE_DELAYED_SUBMISSION" in os.environ:
                enable_delayed_submission = (
                    False
                    if os.environ["UCXPY_ENABLE_DELAYED_SUBMISSION"] == "0"
                    else True
                )
            else:
                enable_delayed_submission = True

    @staticmethod
    def _check_enable_python_future(enable_python_future, progress_mode):
        if enable_python_future is None:
            if "UCXPY_ENABLE_PYTHON_FUTURE" in os.environ:
                explicit_enable_python_future = (
                    os.environ["UCXPY_ENABLE_PYTHON_FUTURE"] != "0"
                )
            else:
                explicit_enable_python_future = False
        else:
            explicit_enable_python_future = enable_python_future

        if not progress_mode.startswith("thread") and explicit_enable_python_future:
            logger.warning(
                f"Notifier thread requested, but {progress_mode} does not "
                "support it, using Python wait_yield()."
            )
            enable_python_future = False
        return explicit_enable_python_future

    def start_notifier_thread(self):
        if self.worker.is_python_future_enabled():
            logger.debug("UCXX_ENABLE_PYTHON available, enabling notifier thread")
            self.notifier_thread_q = Queue()
            self.notifier_thread = threading.Thread(
                target=_notifierThread,
                args=(loop, self.worker, self.notifier_thread_q),
                name="UCX-Py Async Notifier Thread",
            )
            self.notifier_thread.start()
        else:
            logger.debug(
                "UCXX not compiled with UCXX_ENABLE_PYTHON, disabling notifier thread"
            )

    def stop_notifier_thread(self):
        """
        Stop Python future notifier thread

        Stop the notifier thread if context is running with Python future
        notification enabled via `UCXPY_ENABLE_PYTHON_FUTURE=1` or
        `ucp.init(..., enable_python_future=True)`.
        """
        if self.notifier_thread_q and self.notifier_thread:
            self.notifier_thread_q.put("shutdown")
            self.notifier_thread.join()
            logger.debug("Notifier thread stopped")
        else:
            logger.debug("Notifier thread not running")

    def create_listener(
        self,
        callback_func,
        port=0,
        endpoint_error_handling=True,
    ):
        """Create and start a listener to accept incoming connections

        callback_func is the function or coroutine that takes one
        argument -- the Endpoint connected to the client.

        Notice, the listening is closed when the returned Listener
        goes out of scope thus remember to keep a reference to the object.

        Parameters
        ----------
        callback_func: function or coroutine
            A callback function that gets invoked when an incoming
            connection is accepted
        port: int, optional
            An unused port number for listening, or `0` to let UCX assign
            an unused port.
        endpoint_error_handling: boolean, optional
            If `True` (default) enable endpoint error handling raising
            exceptions when an error occurs, may incur in performance penalties
            but prevents a process from terminating unexpectedly that may
            happen when disabled. If `False` endpoint endpoint error handling
            is disabled.

        Returns
        -------
        Listener
            The new listener. When this object is deleted, the listening stops
        """
        self.continuous_ucx_progress()
        if port is None:
            port = 0

        loop = asyncio.get_event_loop()

        logger.info("create_listener() - Start listening on port %d" % port)
        ret = Listener(
            ucx_api.UCXListener.create(
                worker=self.worker,
                port=port,
                cb_func=_listener_handler,
                cb_args=(loop, callback_func, self, endpoint_error_handling),
                deliver_endpoint=True,
            )
        )
        return ret

    async def create_endpoint(self, ip_address, port, endpoint_error_handling=True):
        """Create a new endpoint to a server

        Parameters
        ----------
        ip_address: str
            IP address of the server the endpoint should connect to
        port: int
            IP address of the server the endpoint should connect to
        endpoint_error_handling: boolean, optional
            If `True` (default) enable endpoint error handling raising
            exceptions when an error occurs, may incur in performance penalties
            but prevents a process from terminating unexpectedly that may
            happen when disabled. If `False` endpoint endpoint error handling
            is disabled.

        Returns
        -------
        Endpoint
            The new endpoint
        """
        self.continuous_ucx_progress()

        ucx_ep = ucx_api.UCXEndpoint.create(
            self.worker, ip_address, port, endpoint_error_handling
        )
        self.worker.progress()

        # We create the Endpoint in three steps:
        #  1) Generate unique IDs to use as tags
        #  2) Exchange endpoint info such as tags
        #  3) Use the info to create an endpoint
        seed = os.urandom(16)
        msg_tag = hash64bits("msg_tag", seed, ucx_ep.handle)
        ctrl_tag = hash64bits("ctrl_tag", seed, ucx_ep.handle)
        peer_info = await exchange_peer_info(
            endpoint=ucx_ep,
            msg_tag=msg_tag,
            ctrl_tag=ctrl_tag,
            listener=False,
        )
        tags = {
            "msg_send": peer_info["msg_tag"],
            "msg_recv": msg_tag,
            "ctrl_send": peer_info["ctrl_tag"],
            "ctrl_recv": ctrl_tag,
        }
        ep = Endpoint(endpoint=ucx_ep, ctx=self, tags=tags)

        logger.debug(
            "create_endpoint() client: %s, error handling: %s, msg-tag-send: %s, "
            "msg-tag-recv: %s, ctrl-tag-send: %s, ctrl-tag-recv: %s"
            % (
                hex(ep._ep.handle),
                endpoint_error_handling,
                hex(ep._tags["msg_send"]),
                hex(ep._tags["msg_recv"]),
                hex(ep._tags["ctrl_send"]),
                hex(ep._tags["ctrl_recv"]),
            )
        )

        return ep

    async def create_endpoint_from_worker_address(
        self,
        address,
        endpoint_error_handling=True,
    ):
        """Create a new endpoint to a server

        Parameters
        ----------
        address: UCXAddress
        endpoint_error_handling: boolean, optional
            If `True` (default) enable endpoint error handling raising
            exceptions when an error occurs, may incur in performance penalties
            but prevents a process from terminating unexpectedly that may
            happen when disabled. If `False` endpoint endpoint error handling
            is disabled.

        Returns
        -------
        Endpoint
            The new endpoint
        """
        self.continuous_ucx_progress()

        ucx_ep = ucx_api.UCXEndpoint.create_from_worker_address(
            self.worker,
            address,
            endpoint_error_handling,
        )
        self.worker.progress()

        ep = Endpoint(endpoint=ucx_ep, ctx=self, tags=None)

        logger.debug(
            "create_endpoint() client: %s, error handling: %s"
            % (hex(ep._ep.handle), endpoint_error_handling)
        )

        return ep

    def continuous_ucx_progress(self, event_loop=None):
        """Guarantees continuous UCX progress

        Use this function to associate UCX progress with an event loop.
        Notice, multiple event loops can be associate with UCX progress.

        This function is automatically called when calling
        `create_listener()` or `create_endpoint()`.

        Parameters
        ----------
        event_loop: asyncio.event_loop, optional
            The event loop to evoke UCX progress. If None,
            `asyncio.get_event_loop()` is used.
        """
        loop = event_loop if event_loop is not None else asyncio.get_event_loop()
        if loop in self.progress_tasks:
            return  # Progress has already been guaranteed for the current event loop

        if self.progress_mode == "thread":
            task = ThreadMode(self.worker, loop, polling_mode=False)
        elif self.progress_mode == "thread-polling":
            task = ThreadMode(self.worker, loop, polling_mode=True)
        elif self.progress_mode == "polling":
            task = PollingMode(self.worker, loop)

        self.progress_tasks.append(task)

    def get_ucp_worker(self):
        """Returns the underlying UCP worker handle (ucp_worker_h)
        as a Python integer.
        """
        return self.worker.handle

    def get_config(self):
        """Returns all UCX configuration options as a dict.

        Returns
        -------
        dict
            The current UCX configuration options
        """
        return self.context.get_config()


class Listener:
    """A handle to the listening service started by `create_listener()`

    The listening continues as long as this object exist or `.close()` is called.
    Please use `create_listener()` to create an Listener.
    """

    def __init__(self, listener):
        if not isinstance(listener, ucx_api.UCXListener):
            raise ValueError("listener must be an instance of UCXListener")

        self._listener = listener

    def closed(self):
        """Is the listener closed?"""
        return self._listener is None

    @property
    def ip(self):
        """The listening network IP address"""
        return self._listener.ip

    @property
    def port(self):
        """The listening network port"""
        return self._listener.port

    def close(self):
        """Closing the listener"""
        self._listener = None


class Endpoint:
    """An endpoint represents a connection to a peer

    Please use `create_listener()` and `create_endpoint()`
    to create an Endpoint.
    """

    def __init__(self, endpoint, ctx, tags=None):
        if not isinstance(endpoint, ucx_api.UCXEndpoint):
            raise ValueError("endpoint must be an instance of UCXEndpoint")
        if not isinstance(ctx, ApplicationContext):
            raise ValueError("ctx must be an instance of ApplicationContext")

        self._ep = endpoint
        self._ctx = ctx
        self._send_count = 0  # Number of calls to self.send()
        self._recv_count = 0  # Number of calls to self.recv()
        self._finished_recv_count = 0  # Number of returned (finished) self.recv() calls
        self._shutting_down_peer = False  # Told peer to shutdown
        self._close_after_n_recv = None
        self._tags = tags

    @property
    def uid(self):
        """The unique ID of the underlying UCX endpoint"""
        return self._ep.handle

    def closed(self):
        """Is this endpoint closed?"""
        return self._ep is None or not self._ep.is_alive()

    def abort(self):
        """Close the communication immediately and abruptly.
        Useful in destructors or generators' ``finally`` blocks.

        Notice, this functions doesn't signal the connected peer to close.
        To do that, use `Endpoint.close()`
        """
        if self._ep is not None:
            logger.debug("Endpoint.abort(): %s" % hex(self.uid))
        self._ep = None
        self._ctx = None

    async def close(self):
        """Close the endpoint cleanly.
        This will attempt to flush outgoing buffers before actually
        closing the underlying UCX endpoint.
        """
        if self.closed():
            self.abort()
            return
        try:
            # Making sure we only tell peer to shutdown once
            if self._shutting_down_peer:
                return
            self._shutting_down_peer = True

        finally:
            if not self.closed():
                # Give all current outstanding send() calls a chance to return
                self._ctx.worker.progress()
                await asyncio.sleep(0)
                self.abort()

    # @ucx_api.nvtx_annotate("UCXPY_SEND", color="green", domain="ucxpy")
    async def send(self, buffer, tag=None, force_tag=False):
        """Send `buffer` to connected peer.

        Parameters
        ----------
        buffer: exposing the buffer protocol or array/cuda interface
            The buffer to send. Raise ValueError if buffer is smaller
            than nbytes.
        tag: hashable, optional
        tag: hashable, optional
            Set a tag that the receiver must match. Currently the tag
            is hashed together with the internal Endpoint tag that is
            agreed with the remote end at connection time. To enforce
            using the user tag, make sure to specify `force_tag=True`.
        force_tag: bool
            If true, force using `tag` as is, otherwise the value
            specified with `tag` (if any) will be hashed with the
            internal Endpoint tag.
        """
        self._ep.raise_on_error()
        if self.closed():
            raise UCXCloseError("Endpoint closed")
        if not isinstance(buffer, Array):
            buffer = Array(buffer)
        if tag is None:
            tag = self._tags["msg_send"]
        elif not force_tag:
            tag = hash64bits(self._tags["msg_send"], hash(tag))

        # Optimization to eliminate producing logger string overhead
        if logger.isEnabledFor(logging.DEBUG):
            nbytes = buffer.nbytes
            log = "[Send #%03d] ep: %s, tag: %s, nbytes: %d, type: %s" % (
                self._send_count,
                hex(self.uid),
                hex(tag),
                nbytes,
                type(buffer.obj),
            )
            logger.debug(log)

        self._send_count += 1

        try:
            request = self._ep.tag_send(buffer, tag)
            return await request.wait()
        except UCXCanceled as e:
            # If self._ep has already been closed and destroyed, we reraise the
            # UCXCanceled exception.
            if self._ep is None:
                raise e

    async def send_multi(self, buffers, tag=None, force_tag=False):
        """Send `buffer` to connected peer.

        Parameters
        ----------
        buffer: exposing the buffer protocol or array/cuda interface
            The buffer to send. Raise ValueError if buffer is smaller
            than nbytes.
        tag: hashable, optional
        tag: hashable, optional
            Set a tag that the receiver must match. Currently the tag
            is hashed together with the internal Endpoint tag that is
            agreed with the remote end at connection time. To enforce
            using the user tag, make sure to specify `force_tag=True`.
        force_tag: bool
            If true, force using `tag` as is, otherwise the value
            specified with `tag` (if any) will be hashed with the
            internal Endpoint tag.
        """
        self._ep.raise_on_error()
        if self.closed():
            raise UCXCloseError("Endpoint closed")
        if not (isinstance(buffers, list) or isinstance(buffers, tuple)):
            raise ValueError("The `buffers` argument must be a `list` or `tuple`")
        buffers = tuple([Array(b) if not isinstance(b, Array) else b for b in buffers])
        if tag is None:
            tag = self._tags["msg_send"]
        elif not force_tag:
            tag = hash64bits(self._tags["msg_send"], hash(tag))

        # Optimization to eliminate producing logger string overhead
        if logger.isEnabledFor(logging.DEBUG):
            log = "[Send Multi #%03d] ep: %s, tag: %s, nbytes: %s, type: %s" % (
                self._send_count,
                hex(self.uid),
                hex(tag),
                tuple([b.nbytes for b in buffers]),  # nbytes,
                tuple([type(b.obj) for b in buffers]),
            )
            logger.debug(log)

        self._send_count += 1

        try:
            return await self._ep.tag_send_multi(buffers, tag).wait()
        except UCXCanceled as e:
            # If self._ep has already been closed and destroyed, we reraise the
            # UCXCanceled exception.
            if self._ep is None:
                raise e

    async def send_obj(self, obj, tag=None):
        """Send `obj` to connected peer that calls `recv_obj()`.

        The transfer includes an extra message containing the size of `obj`,
        which increases the overhead slightly.

        Parameters
        ----------
        obj: exposing the buffer protocol or array/cuda interface
            The object to send.
        tag: hashable, optional
            Set a tag that the receiver must match.

        Example
        -------
        >>> await ep.send_obj(pickle.dumps([1,2,3]))
        """
        if not isinstance(obj, Array):
            obj = Array(obj)
        nbytes = Array(array.array("Q", [obj.nbytes]))
        await self.send(nbytes, tag=tag)
        await self.send(obj, tag=tag)

    # @ucx_api.nvtx_annotate("UCXPY_RECV", color="red", domain="ucxpy")
    async def recv(self, buffer, tag=None, force_tag=False):
        """Receive from connected peer into `buffer`.

        Parameters
        ----------
        buffer: exposing the buffer protocol or array/cuda interface
            The buffer to receive into. Raise ValueError if buffer
            is smaller than nbytes or read-only.
        tag: hashable, optional
            Set a tag that must match the received message. Currently
            the tag is hashed together with the internal Endpoint tag
            that is agreed with the remote end at connection time.
            To enforce using the user tag, make sure to specify
            `force_tag=True`.
        force_tag: bool
            If true, force using `tag` as is, otherwise the value
            specified with `tag` (if any) will be hashed with the
            internal Endpoint tag.
        """
        if tag is None:
            tag = self._tags["msg_recv"]
        elif not force_tag:
            tag = hash64bits(self._tags["msg_recv"], hash(tag))

        if not self._ctx.worker.tag_probe(tag):
            self._ep.raise_on_error()
            if self.closed():
                raise UCXCloseError("Endpoint closed")

        if not isinstance(buffer, Array):
            buffer = Array(buffer)

        # Optimization to eliminate producing logger string overhead
        if logger.isEnabledFor(logging.DEBUG):
            nbytes = buffer.nbytes
            log = "[Recv #%03d] ep: %s, tag: %s, nbytes: %d, type: %s" % (
                self._recv_count,
                hex(self.uid),
                hex(tag),
                nbytes,
                type(buffer.obj),
            )
            logger.debug(log)

        self._recv_count += 1

        req = self._ep.tag_recv(buffer, tag)
        ret = await req.wait()

        self._finished_recv_count += 1
        if (
            self._close_after_n_recv is not None
            and self._finished_recv_count >= self._close_after_n_recv
        ):
            self.abort()
        return ret

    async def recv_multi(self, tag=None, force_tag=False):
        """Receive from connected peer into `buffer`.

        Parameters
        ----------
        tag: hashable, optional
            Set a tag that must match the received message. Currently
            the tag is hashed together with the internal Endpoint tag
            that is agreed with the remote end at connection time.
            To enforce using the user tag, make sure to specify
            `force_tag=True`.
        force_tag: bool
            If true, force using `tag` as is, otherwise the value
            specified with `tag` (if any) will be hashed with the
            internal Endpoint tag.
        """
        if tag is None:
            tag = self._tags["msg_recv"]
        elif not force_tag:
            tag = hash64bits(self._tags["msg_recv"], hash(tag))

        if not self._ctx.worker.tag_probe(tag):
            self._ep.raise_on_error()
            if self.closed():
                raise UCXCloseError("Endpoint closed")

        # Optimization to eliminate producing logger string overhead
        if logger.isEnabledFor(logging.DEBUG):
            log = "[Recv Multi #%03d] ep: %s, tag: %s" % (
                self._recv_count,
                hex(self.uid),
                hex(tag),
            )
            logger.debug(log)

        self._recv_count += 1

        buffer_requests = self._ep.tag_recv_multi(tag)
        await buffer_requests.wait()
        for r in buffer_requests.get_requests():
            r.check_error()
        ret = buffer_requests.get_py_buffers()

        self._finished_recv_count += 1
        if (
            self._close_after_n_recv is not None
            and self._finished_recv_count >= self._close_after_n_recv
        ):
            self.abort()
        return ret

    async def recv_obj(self, tag=None, allocator=bytearray):
        """Receive from connected peer that calls `send_obj()`.

        As opposed to `recv()`, this function returns the received object.
        Data is received into a buffer allocated by `allocator`.

        The transfer includes an extra message containing the size of `obj`,
        which increses the overhead slightly.

        Parameters
        ----------
        tag: hashable, optional
            Set a tag that must match the received message. Notice, currently
            UCX-Py doesn't support a "any tag" thus `tag=None` only matches a
            send that also sets `tag=None`.
        allocator: callabale, optional
            Function to allocate the received object. The function should
            take the number of bytes to allocate as input and return a new
            buffer of that size as output.

        Example
        -------
        >>> await pickle.loads(ep.recv_obj())
        """
        nbytes = array.array("Q", [0])
        await self.recv(nbytes, tag=tag)
        nbytes = nbytes[0]
        ret = allocator(nbytes)
        await self.recv(ret, tag=tag)
        return ret

    def get_ucp_worker(self):
        """Returns the underlying UCP worker handle (ucp_worker_h)
        as a Python integer.
        """
        return self._ctx.worker.handle

    def get_ucp_endpoint(self):
        """Returns the underlying UCP endpoint handle (ucp_ep_h)
        as a Python integer.
        """
        return self._ep.handle

    def close_after_n_recv(self, n, count_from_ep_creation=False):
        """Close the endpoint after `n` received messages.

        Parameters
        ----------
        n: int
            Number of messages to received before closing the endpoint.
        count_from_ep_creation: bool, optional
            Whether to count `n` from this function call (default) or
            from the creation of the endpoint.
        """
        if not count_from_ep_creation:
            n += self._finished_recv_count  # Make `n` absolute
        if self._close_after_n_recv is not None:
            raise UCXError(
                "close_after_n_recv has already been set to: %d (abs)"
                % self._close_after_n_recv
            )
        if n == self._finished_recv_count:
            self.abort()
        elif n > self._finished_recv_count:
            self._close_after_n_recv = n
        else:
            raise UCXError(
                "`n` cannot be less than current recv_count: %d (abs) < %d (abs)"
                % (n, self._finished_recv_count)
            )

    def set_close_callback(self, callback_func, cb_args=None, cb_kwargs=None):
        """Register a user callback function to be called on Endpoint's closing.

        Allows the user to register a callback function to be called when the
        Endpoint's error callback is called, or during its finalizer if the error
        callback is never called.

        Once the callback is called, it's not possible to send any more messages.
        However, receiving messages may still be possible, as UCP may still have
        incoming messages in transit.

        Parameters
        ----------
        callback_func: callable
            The callback function to be called when the Endpoint's error callback
            is called, otherwise called on its finalizer.
        cb_args: tuple or None
            The arguments to be passed to the callback function as a `tuple`, or
            `None` (default).
        cb_kwargs: dict or None
            The keyword arguments to be passed to the callback function as a
            `dict`, or `None` (default).

        Example
        >>> ep.set_close_callback(lambda: print("Executing close callback"))
        """
        self._ep.set_close_callback(callback_func, cb_args, cb_kwargs)

    def is_alive(self):
        return self._ep.is_alive()


# The following functions initialize and use a single ApplicationContext instance


def init(options={}, env_takes_precedence=False, progress_mode=None):
    """Initiate UCX.

    Usually this is done automatically at the first API call
    but this function makes it possible to set UCX options programmable.
    Alternatively, UCX options can be specified through environment variables.

    Parameters
    ----------
    options: dict, optional
        UCX options send to the underlying UCX library
    env_takes_precedence: bool, optional
        Whether environment variables takes precedence over the `options`
        specified here.
    progress_mode: string, optional
        If None, thread UCX progress mode is used unless the environment variable
        `UCXPY_PROGRESS_MODE` is defined. Otherwise the options are 'blocking',
        'polling', 'thread'.
    """
    global _ctx
    if _ctx is not None:
        raise RuntimeError(
            "UCX is already initiated. Call reset() and init() "
            "in order to re-initate UCX with new options."
        )
    if env_takes_precedence:
        for k in os.environ.keys():
            if k in options:
                del options[k]

    _ctx = ApplicationContext(options, progress_mode=progress_mode)


def reset():
    """Resets the UCX library by shutting down all of UCX.

    The library is initiated at next API call.
    """
    stop_notifier_thread()
    global _ctx
    if _ctx is not None:
        weakref_ctx = weakref.ref(_ctx)
        _ctx = None
        gc.collect()
        if weakref_ctx() is not None:
            msg = (
                "Trying to reset UCX but not all Endpoints and/or Listeners "
                "are closed(). The following objects are still referencing "
                "ApplicationContext: "
            )
            for o in gc.get_referrers(weakref_ctx()):
                msg += "\n  %s" % str(o)
            raise UCXError(msg)


def stop_notifier_thread():
    global _ctx
    if _ctx:
        _ctx.stop_notifier_thread()
    else:
        logger.debug("UCX is not initialized.")


def get_ucx_version():
    """Return the version of the underlying UCX installation

    Notice, this function doesn't initialize UCX.

    Returns
    -------
    tuple
        The version as a tuple e.g. (1, 7, 0)
    """
    return ucx_api.get_ucx_version()


def progress():
    """Try to progress the communication layer

    Warning, it is illegal to call this from a call-back function such as
    the call-back function given to create_listener.
    """
    return _get_ctx().worker.progress()


def get_config():
    """Returns all UCX configuration options as a dict.

    If UCX is uninitialized, the options returned are the
    options used if UCX were to be initialized now.
    Notice, this function doesn't initialize UCX.

    Returns
    -------
    dict
        The current UCX configuration options
    """

    if _ctx is None:
        return ucx_api.get_current_options()
    else:
        return _get_ctx().get_config()


def create_listener(callback_func, port=None, endpoint_error_handling=True):
    return _get_ctx().create_listener(
        callback_func,
        port,
        endpoint_error_handling=endpoint_error_handling,
    )


async def create_endpoint(ip_address, port, endpoint_error_handling=True):
    return await _get_ctx().create_endpoint(
        ip_address,
        port,
        endpoint_error_handling=endpoint_error_handling,
    )


async def create_endpoint_from_worker_address(
    address,
    endpoint_error_handling=True,
):
    return await _get_ctx().create_endpoint_from_worker_address(
        address,
        endpoint_error_handling=endpoint_error_handling,
    )


def continuous_ucx_progress(event_loop=None):
    _get_ctx().continuous_ucx_progress(event_loop=event_loop)


def get_ucp_worker():
    return _get_ctx().get_ucp_worker()


# Setting the __doc__
create_listener.__doc__ = ApplicationContext.create_listener.__doc__
create_endpoint.__doc__ = ApplicationContext.create_endpoint.__doc__
continuous_ucx_progress.__doc__ = ApplicationContext.continuous_ucx_progress.__doc__
get_ucp_worker.__doc__ = ApplicationContext.get_ucp_worker.__doc__
stop_notifier_thread.__doc__ = ApplicationContext.stop_notifier_thread.__doc__
