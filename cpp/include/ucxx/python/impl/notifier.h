/**
 * Copyright (c) 2022, NVIDIA CORPORATION. All rights reserved.
 *
 * See file LICENSE for terms.
 */
#pragma once

#if UCXX_ENABLE_PYTHON
#include <memory>
#include <mutex>

#include <ucxx/python/python_future.h>
#endif

namespace ucxx {

namespace python {

void Notifier::scheduleFutureNotify(std::shared_ptr<Future> future, ucs_status_t status)
{
  ucxx_trace_req(
    "Notifier::scheduleFutureNotify(): future: %p, handle: %p", future.get(), future->getHandle());
  auto p = std::make_pair(future, status);
  {
    std::lock_guard lock(_notifierThreadMutex);
    _notifierThreadFutureStatus.push_back(p);
    _notifierThreadFutureStatusReady = true;
  }
  _notifierThreadConditionVariable.notify_one();
  ucxx_trace_req("Notifier::scheduleFutureNotify() notified: future: %p, handle: %p",
                 future.get(),
                 future->getHandle());
}

void Notifier::runRequestNotifier()
{
  ucxx_trace_req("Notifier::runRequestNotifier()");
  decltype(_notifierThreadFutureStatus) notifierThreadFutureStatus;
  {
    std::unique_lock lock(_notifierThreadMutex);
    ucxx_trace_req("Notifier::runRequestNotifier()1: %lu, %lu",
                   _notifierThreadFutureStatus.size(),
                   notifierThreadFutureStatus.size());
    std::swap(_notifierThreadFutureStatus, notifierThreadFutureStatus);
    ucxx_trace_req("Notifier::runRequestNotifier()2: %lu, %lu",
                   _notifierThreadFutureStatus.size(),
                   notifierThreadFutureStatus.size());
  }

  ucxx_trace_req("Notifier::runRequestNotifier() notifying %lu", notifierThreadFutureStatus.size());
  for (auto& p : notifierThreadFutureStatus) {
    // r->future_set_result;
    p.first->set(p.second);
    ucxx_trace_req("Notifier::runRequestNotifier() notified future: %p, handle: %p",
                   p.first.get(),
                   p.first->getHandle());
  }
}

}  // namespace python

}  // namespace ucxx
