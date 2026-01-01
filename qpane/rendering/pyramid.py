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

"""Generate and cache image pyramids on executor-backed workers while keeping UI work responsive."""

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Sequence

from PySide6.QtCore import QObject, QRunnable, Qt, Signal
from PySide6.QtGui import QImage

from ..concurrency import (
    BaseWorker,
    RetryController,
    TaskExecutorProtocol,
    TaskHandle,
    TaskRejected,
    makeQtRetryController,
    qt_retry_dispatcher,
)
from ..core import CacheSettings, Config
from ..core.threading import assert_qt_main_thread
from .cache_utils import CacheEvictionCoordinator, ExecutorOwnerMixin
from .cache_metrics import CacheManagerMetrics, CacheMetricsMixin

logger = logging.getLogger(__name__)

_PYRAMID_EVICTION_BATCH = 3
_PYRAMID_RETRY_BASE_MS = 75
_PYRAMID_RETRY_MAX_MS = 1500


class PyramidStatus(str, Enum):
    """Enumerates lifecycle states for pyramid generation."""

    PENDING = "pending"
    GENERATING = "generating"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PyramidWorkerSignals(QObject):
    """Defines signals available from a running worker thread."""

    finished = Signal(Path)  # Emits the source_path when generation is done
    error = Signal(Path, str)  # Emits path and error message on failure


class PyramidGeneratorWorker(QRunnable, BaseWorker):
    """Background worker that builds a single image pyramid for a source image."""

    def __init__(self, pyramid: "ImagePyramid", config: Config):
        """Store the target ``pyramid`` and config snapshot for generation."""
        QRunnable.__init__(self)
        BaseWorker.__init__(self)
        self.pyramid = pyramid
        self._config = config
        self.signals = PyramidWorkerSignals()

    def run(self):
        """Generate pyramid levels and report completion or failure."""
        try:
            if self.is_cancelled:
                self._handle_cancellation()
                return
            self.pyramid.status = PyramidStatus.GENERATING
            self.logger.info(
                "Generating pyramid for %s",
                self.pyramid.source_path,
            )
            source_qimage = self.pyramid.full_resolution_image
            # Ensure image is in a 4-channel format to preserve transparency.
            if source_qimage.format() != QImage.Format_ARGB32_Premultiplied:
                source_qimage = source_qimage.convertToFormat(
                    QImage.Format_ARGB32_Premultiplied
                )
            width, height = source_qimage.width(), source_qimage.height()
            current_scale = 1.0
            loop_width, loop_height = width, height
            while max(loop_width, loop_height) > self._config.min_view_size_px:
                if self.is_cancelled:
                    self._handle_cancellation()
                    return
                current_scale /= 2.0
                new_width = int(width * current_scale)
                new_height = int(height * current_scale)
                if new_width <= 0 or new_height <= 0:
                    break
                loop_width, loop_height = new_width, new_height
                # Use Qt's high-quality smooth scaler.
                qt_image = source_qimage.scaled(
                    new_width,
                    new_height,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
                self.pyramid.levels[current_scale] = qt_image.copy()
            # Calculate the total size of the pyramid
            total_size = self.pyramid.full_resolution_image.sizeInBytes()
            for level_image in self.pyramid.levels.values():
                total_size += level_image.sizeInBytes()
            self.pyramid.size_bytes = total_size
            self.pyramid.status = PyramidStatus.COMPLETE
            self.emit_finished(True, payload=self.pyramid.source_path)
        except Exception as exc:
            self.pyramid.status = PyramidStatus.FAILED
            self.emit_finished(
                False,
                payload=(self.pyramid.source_path, str(exc)),
                error=exc,
            )

    def cancel(self):
        """Request cancellation for the running worker."""
        BaseWorker.cancel(self)

    def _handle_cancellation(self) -> None:
        """Mark the pyramid as cancelled and emit completion payload once."""
        if self.pyramid.status == PyramidStatus.CANCELLED:
            return
        self.pyramid.status = PyramidStatus.CANCELLED
        self.logger.info(
            "Cancelled pyramid generation for %s", self.pyramid.source_path
        )
        self.emit_finished(
            False,
            payload=(self.pyramid.source_path, "cancelled"),
        )


@dataclass
class ImagePyramid:
    """Container for the original image plus its downscaled pyramid levels.

    PyramidManager mutates status and levels on the main thread while workers populate levels in the background.
    """

    source_path: Path
    full_resolution_image: QImage
    levels: Dict[float, QImage] = field(default_factory=dict)
    status: PyramidStatus = PyramidStatus.PENDING
    size_bytes: int = 0


class PyramidManager(QObject, CacheMetricsMixin, ExecutorOwnerMixin):
    """Manage pyramid creation, caching, and retrieval for tiled rendering.

    Generates pyramids on the shared executor, enforces byte budgets with LRU eviction, and keeps mutations on the Qt main thread. Retry scheduling relies on the shared controller's main-thread dispatch. Callers treat returned ImagePyramids as read-only snapshots.
    """

    pyramidReady = Signal(Path)
    pyramidThrottled = Signal(Path, int)

    def __init__(
        self,
        config: Config,
        parent=None,
        *,
        executor: TaskExecutorProtocol,
        owns_executor: bool = False,
    ):
        """Initialise caches, workers, and retry controllers for pyramid generation."""
        super().__init__(parent)
        CacheMetricsMixin.__init__(self)
        ExecutorOwnerMixin.__init__(
            self,
            executor_logger=logger,
            owner_name="PyramidManager",
        )
        self._config = config
        self._executor: TaskExecutorProtocol | None = executor
        self._owns_executor = bool(owns_executor)
        self._pyramids: Dict[Path, ImagePyramid] = {}
        self._cache: OrderedDict[Path, ImagePyramid] = OrderedDict()
        self.cache_limit_bytes = self._resolve_cache_limit_bytes(config)
        self._cache_admission_guard = None
        self._managed_mode = False
        self._rejected_cache_keys: set[Path] = set()
        self._cache_size_bytes: int = 0
        self._active_workers: Dict[Path, PyramidGeneratorWorker] = {}
        self._active_handles: Dict[Path, TaskHandle] = {}
        dispatcher = qt_retry_dispatcher(self._executor, category="pyramid_main")
        self._pyramid_retry: RetryController[Path, ImagePyramid] = (
            makeQtRetryController(
                "pyramid",
                _PYRAMID_RETRY_BASE_MS,
                _PYRAMID_RETRY_MAX_MS,
                parent=self,
                dispatcher=dispatcher,
            )
        )
        self._eviction = CacheEvictionCoordinator(logger=logger, name="pyramid cache")

    def apply_config(self, config: Config) -> None:
        """Refresh derived values after a configuration update."""
        self._config = config
        if not self._managed_mode:
            self._enforce_cache_size()

    @property
    def cache_usage_bytes(self) -> int:
        """Return the current pyramid cache usage in bytes."""
        return self._cache_size_bytes

    def set_managed_mode(self, enabled: bool) -> None:
        """Enable or disable managed mode.

        In managed mode, the manager disables automatic self-eviction and relaxes
        admission checks, relying on an external coordinator to drive trims.
        """
        self._managed_mode = bool(enabled)

    def set_admission_guard(self, guard: Callable[[int], bool] | None) -> None:
        """Install an optional hard-cap guard consulted before caching pyramids."""
        self._cache_admission_guard = guard

    def mark_external_trim(self, reason: str) -> None:
        """Tag the next eviction batch with an external ``reason``."""
        self._next_eviction_reason = reason

    def pyramid_for_path(self, source_path: Path) -> "ImagePyramid | None":
        """Return the ImagePyramid for a given path, or None if not present."""
        self._assert_main_thread()
        return self._pyramids.get(source_path)

    def iter_cached_paths(self):
        """Yield cached image paths in LRU order (oldest first)."""
        self._assert_main_thread()
        return iter(self._cache.keys())

    def pending_paths(self):
        """Return paths that still have generation in progress."""
        self._assert_main_thread()
        return set(self._active_workers.keys())

    def prefetch_pyramid(
        self,
        image: QImage,
        source_path: Path,
        *,
        reason: str = "prefetch",
    ) -> bool:
        """Request background pyramid generation for `source_path` if needed."""
        self._assert_main_thread()
        if source_path is None:
            raise ValueError("source_path is required")
        if image.isNull():
            return False
        if self._prefetch_pending(source_path):
            logger.debug("Pyramid prefetch already pending for %s", source_path)
            return False
        pyramid = self._pyramids.get(source_path)
        if pyramid is not None and pyramid.status == PyramidStatus.COMPLETE:
            self._prefetch_skip_hit()
            return False
        if source_path in self._active_handles:
            logger.debug("Pyramid generation already active for %s", source_path)
            return False
        self._prefetch_begin(source_path, record_start=False)
        try:
            self.generate_pyramid_for_image(image, source_path)
        except Exception:
            self._prefetch_finish(source_path, success=False)
            logger.exception(
                "Pyramid prefetch submission failed (path=%s)", source_path
            )
            raise
        logger.info(
            "Scheduled pyramid prefetch for %s (reason=%s)", source_path, reason
        )
        return True

    def cancel_prefetch(
        self,
        paths: Sequence[Path],
        *,
        reason: str = "navigation",
    ) -> list[Path]:
        """Cancel outstanding pyramid prefetch requests."""
        if not paths:
            return []
        self._assert_main_thread()
        cancelled: list[Path] = []
        executor = self._executor
        if executor is None:
            raise RuntimeError("PyramidManager executor is missing")
        for path in paths:
            if not self._prefetch_pending(path):
                continue
            handle = self._active_handles.get(path)
            worker = self._active_workers.get(path)
            cancelled_flag = False
            if executor is not None and handle is not None:
                try:
                    cancelled_flag = executor.cancel(handle)
                except Exception:
                    cancelled_flag = False
            if not cancelled_flag and worker is not None:
                try:
                    worker.cancel()
                except Exception:  # pragma: no cover - defensive guard
                    logger.exception(
                        "Pyramid worker cancel threw (path=%s, reason=%s)",
                        path,
                        reason,
                    )
            self._detach_worker(path)
            self._cancel_pyramid_retry(path)
            self._prefetch_finish(path, success=False)
            cancelled.append(path)
            logger.info(
                "Cancelled pyramid prefetch %s (reason=%s, executor_cancelled=%s)",
                path,
                reason,
                cancelled_flag,
            )
        return cancelled

    def generate_pyramid_for_image(self, image: QImage, source_path: Path):
        """Start a worker to generate a pyramid for ``source_path``."""
        self._assert_main_thread()
        if source_path is None:
            raise ValueError("source_path is required")
        existing = self._pyramids.get(source_path)
        if existing is None:
            pyramid = ImagePyramid(
                source_path=source_path,
                full_resolution_image=image,
            )
            self._pyramids[source_path] = pyramid
        else:
            pyramid = existing
            pyramid.full_resolution_image = image

        def _submit(pyr: ImagePyramid, attempt: int):
            """Submit ``pyr`` to the executor unless it already has an active worker."""
            # Avoid duplicate submission when already active
            handle = self._active_handles.get(pyr.source_path)
            if handle is not None:
                return handle
            worker = PyramidGeneratorWorker(pyr, self._config)
            BaseWorker.connect_queued(
                worker.signals.finished,
                self._on_pyramid_generated,
            )
            BaseWorker.connect_queued(
                worker.signals.error,
                self._on_pyramid_error,
            )
            executor = self._executor
            if executor is None:
                raise RuntimeError("PyramidManager executor is missing")
            handle = executor.submit(worker, category="pyramid")
            self._active_workers[pyr.source_path] = worker
            self._active_handles[pyr.source_path] = handle
            self._prefetch_mark_started(pyr.source_path)
            logger.info("Queued pyramid generation for %s", pyr.source_path)
            return handle

        def _coalesce(old: ImagePyramid, new: ImagePyramid) -> ImagePyramid:
            """Update ``old`` pyramid with the latest full-resolution image."""
            old.full_resolution_image = new.full_resolution_image
            return old

        def _throttle(path: Path, next_attempt: int, rej: TaskRejected):
            """Record throttling metadata and emit the public signal."""
            logger.warning(
                "Pyramid generation for %s throttled: pending %s limit=%s "
                "(total=%s, category=%s)",
                path,
                rej.limit_type,
                rej.limit_value,
                rej.pending_total,
                rej.pending_category,
            )
            self.pyramidThrottled.emit(path, next_attempt)

        self._queue_pyramid_retry(
            source_path,
            pyramid,
            submit=_submit,
            throttle=_throttle,
            coalesce=_coalesce,
        )

    def _on_pyramid_generated(self, source_path: Path):
        """Slot for when a pyramid worker successfully finishes."""
        self._assert_main_thread()
        self._detach_worker(source_path)
        self._pyramid_retry.onSuccess(source_path)
        self._prefetch_finish(source_path, success=True)
        if source_path in self._pyramids:
            pyramid = self._pyramids[source_path]
            if pyramid.status == PyramidStatus.COMPLETE:
                if self._allow_cache_insert(pyramid.size_bytes, source_path):
                    self._cache[source_path] = pyramid
                    self._cache_size_bytes += pyramid.size_bytes
                    if not self._managed_mode:
                        self._enforce_cache_size()
                    logger.info("Pyramid generated for %s", source_path)
                self.pyramidReady.emit(source_path)
            elif pyramid.status == PyramidStatus.CANCELLED:
                logger.info(
                    "Skipped cache promotion for cancelled pyramid %s",
                    source_path,
                )
            else:
                logger.warning(
                    "Unexpected pyramid status %s for %s during completion",
                    pyramid.status,
                    source_path,
                )

    def _on_pyramid_error(self, source_path: Path, error_message: str):
        """Slot for when a pyramid worker encounters an error."""
        self._assert_main_thread()
        self._detach_worker(source_path)
        self._pyramid_retry.onFailure(source_path)
        self._prefetch_finish(source_path, success=False)
        pyramid = self._pyramids.get(source_path)
        if pyramid and pyramid.status != PyramidStatus.CANCELLED:
            pyramid.status = PyramidStatus.FAILED
        if error_message == "cancelled":
            logger.info("Pyramid generation cancelled for %s", source_path)
            return
        logger.error(
            "Pyramid generation failed for %s: %s",
            source_path,
            error_message,
        )

    def get_best_fit_image(
        self, source_path: Path, target_width: float
    ) -> QImage | None:
        """Return the pyramid level closest to the target width without upscaling.

        Falls back to the full-resolution image when no pyramid exists, generation failed or was cancelled, the target width is invalid, or the pyramid is incomplete or would upscale.
        """
        self._assert_main_thread()
        if source_path is None:
            return None
        pyramid = self.pyramid_for_path(source_path)
        if pyramid is None:
            self._cache_misses += 1
            return None
        if pyramid.status in (PyramidStatus.CANCELLED, PyramidStatus.FAILED):
            self._cache_misses += 1
            return pyramid.full_resolution_image
        original_image = pyramid.full_resolution_image
        original_width = original_image.width()
        if original_width <= 0 or target_width is None or target_width <= 0:
            self._cache_misses += 1
            return original_image
        if (
            pyramid.status != PyramidStatus.COMPLETE
            or not pyramid.levels
            or target_width >= original_width
        ):
            self._cache_misses += 1
            return original_image
        target_scale = target_width / original_width
        # Pick the smallest scale that still meets ``target_scale``
        available_scales = [
            scale for scale in pyramid.levels.keys() if scale >= target_scale
        ]
        best_scale = min(available_scales, default=None)
        if best_scale is not None:
            self._cache_hits += 1
            return pyramid.levels[best_scale]
        self._cache_misses += 1
        return original_image

    def remove_pyramid(self, source_path: Path) -> None:
        """Purge the pyramid, cache state, and worker bookkeeping for ``source_path``."""
        self._assert_main_thread()
        if source_path is None:
            raise ValueError("source_path is required")
        was_cached = source_path in self._cache
        had_worker = source_path in self._active_workers
        self._drop_cache_entry(source_path)
        self._cancel_pyramid_retry(source_path)
        self._pyramids.pop(source_path, None)
        self._active_handles.pop(source_path, None)
        self._active_workers.pop(source_path, None)
        self._prefetch_drop(source_path)
        logger.info(
            "Removed pyramid state for %s (cached=%s, worker=%s)",
            source_path,
            was_cached,
            had_worker,
        )

    def clear(self) -> None:
        """Cancel workers, reset counters, and empty every cache entry."""
        self.shutdown(wait=False)
        self._assert_main_thread()
        pyramid_count = len(self._pyramids)
        self._pyramids.clear()
        had_entries = bool(self._cache)
        self._cache.clear()
        self._cache_size_bytes = 0
        self._rejected_cache_keys.clear()
        self._prefetch_drop_all()
        self._reset_cache_metrics()
        assert self._cache_size_bytes == 0, "Cache size not zero after clear"
        if had_entries:
            self._record_eviction_metadata("clear")
        logger.info(
            "Cleared pyramid cache (pyramids=%d, cache_entries=%s)",
            pyramid_count,
            had_entries,
        )

    def snapshot_metrics(self) -> CacheManagerMetrics:
        """Return cache metrics for diagnostics and testing."""
        return self._snapshot_cache_metrics(
            cache_bytes=self._cache_size_bytes,
            cache_limit=self.cache_limit_bytes,
            active_jobs=len(self._active_handles),
            pending_retries=len(self.pending_retry_paths()),
        )

    def retry_snapshot(self):
        """Expose the retry controller snapshot for diagnostics consumers."""
        return self._pyramid_retry.snapshot()

    def pending_retry_paths(self) -> list[Path]:
        """Return source paths currently queued for retry."""
        return list(self._pyramid_retry.pendingKeys())

    def _drop_cache_entry(self, source_path: Path) -> None:
        """Remove a pyramid from the LRU cache and update size accounting."""
        self._assert_main_thread()
        if source_path in self._cache:
            self._cache_size_bytes -= self._cache[source_path].size_bytes
            del self._cache[source_path]
            assert self._cache_size_bytes >= 0, "Cache size went negative"

    def _allow_cache_insert(self, size_bytes: int, key: Path) -> bool:
        """Return True when ``size_bytes`` is within pyramid guardrails."""
        size = max(0, int(size_bytes))
        budget_limit = max(0, int(self.cache_limit_bytes))

        def _warn(limit_value: int) -> None:
            """Log a cache admission rejection once per key."""
            if key in self._rejected_cache_keys:
                return
            logger.warning(
                "requested item exceeds budget; not cached | consumer=pyramids | "
                "size=%d | budget=%d",
                size,
                limit_value,
            )
            self._rejected_cache_keys.add(key)

        if not self._managed_mode and size > budget_limit:
            _warn(budget_limit)
            return False
        guard = self._cache_admission_guard
        if guard is not None and not guard(size):
            _warn(budget_limit)
            return False
        return True

    def _queue_pyramid_retry(
        self,
        source_path: Path,
        pyramid: "ImagePyramid",
        *,
        submit: Callable[["ImagePyramid", int], TaskHandle],
        throttle: Callable[[Path, int, TaskRejected], None],
        coalesce: (
            Callable[["ImagePyramid", "ImagePyramid"], "ImagePyramid"] | None
        ) = None,
    ) -> None:
        """Queue pyramid generation work through the retry controller."""
        self._pyramid_retry.queueOrCoalesce(
            source_path,
            pyramid,
            submit=submit,
            throttle=throttle,
            coalesce=coalesce,
        )

    def _cancel_pyramid_retry(self, source_path: Path) -> None:
        """Cancel any pending retry for ``source_path``."""
        self._pyramid_retry.cancel(source_path)

    def _cancel_all_pyramid_retries(self) -> None:
        """Cancel every queued pyramid retry."""
        self._pyramid_retry.cancelAll()

    def _enforce_cache_size(self) -> None:
        """Request async eviction when the cache exceeds its budget."""
        if self._cache_size_bytes <= self.cache_limit_bytes or not self._cache:
            return
        if self._eviction.pending:
            return
        self._ensure_next_eviction_reason("limit")
        executor = self._executor
        if executor is None:
            raise RuntimeError("PyramidManager executor is missing")
        self._eviction.schedule(
            executor=executor,
            callback=self._run_eviction_batch,
            category="maintenance",
        )

    def _run_eviction_batch(self) -> None:
        """Evict a bounded batch of pyramids on the main thread."""
        reason = self._consume_next_eviction_reason("limit")
        evicted = 0
        evicted_paths = []
        bytes_freed = 0
        while (
            self._cache_size_bytes > self.cache_limit_bytes
            and self._cache
            and evicted < _PYRAMID_EVICTION_BATCH
        ):
            lru_path = next(iter(self._cache))
            removed_bytes = 0
            pyramid = self._cache.get(lru_path)
            if pyramid is not None:
                removed_bytes = pyramid.size_bytes
            self._drop_cache_entry(lru_path)
            if lru_path in self._pyramids:
                del self._pyramids[lru_path]
            if removed_bytes:
                bytes_freed += removed_bytes
                self._evicted_bytes += removed_bytes
            evicted_paths.append(str(lru_path))
            self._evictions_total += 1
            self._record_eviction_metadata(reason)
            evicted += 1
        if evicted_paths:
            logger.info(
                "Eviction batch: evicted=%d, paths=%s, bytes_freed=%d, "
                "total=%d, limit=%d",
                evicted,
                evicted_paths,
                bytes_freed,
                self._cache_size_bytes,
                self.cache_limit_bytes,
            )
        if (
            not self._managed_mode
            and self._cache_size_bytes > self.cache_limit_bytes
            and self._cache
        ):
            self._enforce_cache_size()

    def _cancel_eviction_task(self) -> None:
        """Cancel a pending eviction callback when one exists."""
        self._eviction.cancel(self._executor)

    def shutdown(self, *, wait: bool = True) -> None:
        """Cancel workers and pending eviction callbacks."""
        self._assert_main_thread()
        self._cancel_eviction_task()
        self._cancel_all_pyramid_retries()
        if not self._active_handles:
            self._maybe_wait_for_executor(wait)
            return
        for source_path, handle in list(self._active_handles.items()):
            executor = self._executor
            if executor is None:
                raise RuntimeError("PyramidManager executor is missing")
            cancelled = executor.cancel(handle)
            if not cancelled:
                worker = self._active_workers.get(source_path)
                if worker is not None:
                    worker.cancel()
            logger.info(
                "Requested cancellation for pyramid %s (cancelled=%s)",
                source_path,
                cancelled,
            )
        self._active_handles.clear()
        self._active_workers.clear()
        self._prefetch_drop_all()
        self._maybe_wait_for_executor(wait)

    def _detach_worker(self, source_path: Path) -> None:
        """Remove bookkeeping for a finished or failed worker."""
        self._active_workers.pop(source_path, None)
        self._active_handles.pop(source_path, None)

    def _assert_main_thread(self):
        """Raise AssertionError if not running on the Qt main thread."""
        assert_qt_main_thread(self)

    @staticmethod
    def _resolve_cache_limit_bytes(config: Config) -> int:
        """Return the pyramid cache budget derived from cache settings."""
        cache_settings = getattr(config, "cache", None)
        if not isinstance(cache_settings, CacheSettings):
            cache_settings = CacheSettings()
        budgets = cache_settings.resolved_consumer_budgets_bytes()
        return int(budgets.get("pyramids", 0))
