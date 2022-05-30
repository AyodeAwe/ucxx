/**
 * Copyright (c) 2022, NVIDIA CORPORATION. All rights reserved.
 *
 * See file LICENSE for terms.
 */
#pragma once

#include <functional>
#include <mutex>
#include <queue>
#include <thread>

#include <ucp/api/ucp.h>

#include <ucxx/component.h>
#include <ucxx/constructors.h>
#include <ucxx/context.h>
#include <ucxx/notification_request.h>
#include <ucxx/worker_progress_thread.h>

#if UCXX_ENABLE_PYTHON
#include <ucxx/python/notifier.h>
#include <ucxx/python/python_future.h>
#endif

namespace ucxx {

class Address;
class Endpoint;
class Listener;

class Worker : public Component {
 private:
  ucp_worker_h _handle{nullptr};
  int _epollFileDescriptor{-1};
  int _workerFileDescriptor{-1};
  int _wakeFileDescriptor{-1};
  std::shared_ptr<WorkerProgressThread> _progressThread{nullptr};
  InflightRequests _inflightRequestsToCancel{std::make_shared<InflightRequestMap>()};
  std::mutex _inflightMutex{};
  std::function<void(void*)> _progressThreadStartCallback{nullptr};
  void* _progressThreadStartCallbackArg{nullptr};
  std::shared_ptr<DelayedNotificationRequestCollection> _delayedNotificationRequestCollection{
    nullptr};
  std::mutex _pythonFuturesPoolMutex{};
#if UCXX_ENABLE_PYTHON
  std::queue<std::shared_ptr<ucxx::python::Future>> _pythonFuturesPool{};
  std::shared_ptr<ucxx::python::Notifier> _notifier{ucxx::python::createNotifier()};
#endif

  Worker(std::shared_ptr<Context> context, const bool enableDelayedNotification = false);

  void drainWorkerTagRecv();

 public:
  Worker()              = delete;
  Worker(const Worker&) = delete;
  Worker& operator=(Worker const&) = delete;
  Worker(Worker&& o)               = delete;
  Worker& operator=(Worker&& o) = delete;

  friend std::shared_ptr<Worker> createWorker(std::shared_ptr<Context> context,
                                              const bool enableDelayedNotification);

  ~Worker();

  ucp_worker_h getHandle();

  void initBlockingProgressMode();

  bool arm();

  bool progressWorkerEvent();

  void wakeProgressEvent();

  bool waitProgress();

  bool progressOnce();

  void progress();

  void registerNotificationRequest(NotificationRequestCallbackType callback);

  void populatePythonFuturesPool();

  std::shared_ptr<ucxx::python::Future> getPythonFuture();

  bool waitRequestNotifier();

  void runRequestNotifier();

  void stopRequestNotifierThread();

  void setProgressThreadStartCallback(std::function<void(void*)> callback, void* callbackArg);

  void startProgressThread(const bool pollingMode = true);

  void stopProgressThread();

  inline size_t cancelInflightRequests();

  void scheduleRequestCancel(InflightRequests inflightRequests);

  bool tagProbe(ucp_tag_t tag);

  std::shared_ptr<Address> getAddress();

  std::shared_ptr<Endpoint> createEndpointFromHostname(std::string ipAddress,
                                                       uint16_t port              = 0,
                                                       bool endpointErrorHandling = true);

  std::shared_ptr<Endpoint> createEndpointFromWorkerAddress(std::shared_ptr<Address> address,
                                                            bool endpointErrorHandling = true);

  std::shared_ptr<Listener> createListener(uint16_t port,
                                           ucp_listener_conn_callback_t callback,
                                           void* callbackArgs);
};

}  // namespace ucxx
