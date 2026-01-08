#    QPane - High-performance PySide6 image viewer
#    Copyright (C) 2025  Artificial Sweetener and contributors
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Concurrency infrastructure primitives used by QPane managers and hosts."""

from .base_worker import BaseWorker
from .executor import (
    ExecutorSnapshot,
    QThreadPoolExecutor,
    TaskExecutorProtocol,
    LiveTunableExecutorProtocol,
    TaskHandle,
    TaskOutcome,
    TaskRejected,
)
from .metrics import (
    executor_diagnostics_provider,
    executor_summary_provider,
    gather_executor_snapshot,
    retry_diagnostics_provider,
    retry_summary_provider,
)
from .retry_view import RetryEntriesView
from .retry import (
    BackoffPolicy,
    QtTimerScheduler,
    RetryContext,
    RetryController,
    RetrySchedulingError,
    RetryPolicy,
    TerminationPolicy,
    qt_retry_dispatcher,
    makeQtRetryController,
)
from .thread_policy import ThreadPolicy, build_thread_policy, update_thread_policy

__all__ = [
    "BaseWorker",
    "ExecutorSnapshot",
    "QThreadPoolExecutor",
    "TaskExecutorProtocol",
    "LiveTunableExecutorProtocol",
    "TaskHandle",
    "TaskOutcome",
    "TaskRejected",
    "ThreadPolicy",
    "build_thread_policy",
    "update_thread_policy",
    "executor_diagnostics_provider",
    "executor_summary_provider",
    "gather_executor_snapshot",
    "retry_diagnostics_provider",
    "retry_summary_provider",
    "BackoffPolicy",
    "RetryController",
    "QtTimerScheduler",
    "RetryContext",
    "RetrySchedulingError",
    "TerminationPolicy",
    "RetryPolicy",
    "qt_retry_dispatcher",
    "makeQtRetryController",
    "RetryEntriesView",
]
