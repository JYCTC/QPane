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

"""Tests verifying PyramidManager integration with the shared executor."""

from __future__ import annotations
import logging
import time
from pathlib import Path
import uuid
import pytest
from PySide6.QtGui import QImage, Qt
from qpane import Config
from qpane.rendering import ImagePyramid, PyramidManager, PyramidStatus
from tests.helpers.executor_stubs import RejectingStubExecutor, StubExecutor


class _DummyExecutor:
    def __init__(self) -> None:
        self.cancelled = []

    def submit(self, runnable, category: str, *, device: str | None = None):
        raise NotImplementedError

    def cancel(self, handle):
        self.cancelled.append(handle)
        return True

    def shutdown(self, wait: bool = True):
        return None


class _DummyWorker:
    def cancel(self) -> None:
        return None


@pytest.fixture
def sample_image() -> QImage:
    """Return a tiny ARGB image suitable for pyramid generation."""
    image = QImage(32, 32, QImage.Format_ARGB32)
    image.fill(Qt.white)
    return image


@pytest.mark.usefixtures("qapp")
class TestPyramidManager:
    def test_regeneration_after_cancellation(self, sample_image: QImage, qapp):
        """Regenerating a cancelled pyramid should succeed and update cache."""
        executor = StubExecutor()
        manager = PyramidManager(config=Config(), executor=executor)
        image_id = uuid.uuid4()
        source_path = Path("regen-cancel.png")
        # Start and cancel
        manager.generate_pyramid_for_image(image_id, sample_image, source_path)
        manager.shutdown(wait=False)  # Should cancel all
        qapp.processEvents()
        # Regenerate
        manager.generate_pyramid_for_image(image_id, sample_image, source_path)
        pending = list(executor.pending_tasks())
        assert pending
        executor.run_task(pending[0].handle.task_id)
        qapp.processEvents()
        pyramid = manager.pyramid_for_image_id(image_id)
        assert pyramid is not None
        assert pyramid.status == PyramidStatus.COMPLETE
        assert manager.cache_usage_bytes > 0

    def test_eviction_lru_ordering(self, sample_image: QImage, qapp):
        """Eviction should remove the least recently used pyramid first (at least one evicted)."""
        executor = StubExecutor()
        manager = PyramidManager(
            config=Config(cache={"pyramids": {"mb": 1}}), executor=executor
        )
        manager.cache_limit_bytes = 5000
        image_ids = [uuid.uuid4() for _ in range(3)]
        paths = [Path(f"img-{i}.png") for i in range(3)]
        for image_id, path in zip(image_ids, paths):
            manager.generate_pyramid_for_image(image_id, sample_image, path)
            pending = list(executor.pending_tasks())
            assert pending
            executor.run_task(pending[0].handle.task_id)
            qapp.processEvents()
        cached = list(manager.iter_cached_ids())
        # Only one should remain in cache due to aggressive eviction
        assert len(cached) == 1
        # It should be the most recently used before the last insertion (img-1.png)
        assert cached[0] == image_ids[1]

    def test_cache_budget_enforcement(self, sample_image: QImage, qapp):
        """Cache usage should not exceed the configured budget after eviction."""
        executor = StubExecutor()
        manager = PyramidManager(
            config=Config(cache={"pyramids": {"mb": 1}}), executor=executor
        )
        manager.cache_limit_bytes = 1
        for i in range(5):
            image_id = uuid.uuid4()
            path = Path(f"budget-{i}.png")
            manager.generate_pyramid_for_image(image_id, sample_image, path)
            pending = list(executor.pending_tasks())
            assert pending
            executor.run_task(pending[0].handle.task_id)
            qapp.processEvents()
        assert manager.cache_usage_bytes <= manager.cache_limit_bytes

    def test_cancellation_cleanup(self, sample_image: QImage, qapp):
        """Cancelled pyramids should not be cached and should not be COMPLETE."""
        executor = StubExecutor()
        manager = PyramidManager(config=Config(), executor=executor)
        image_id = uuid.uuid4()
        source_path = Path("cancel-me.png")
        manager.generate_pyramid_for_image(image_id, sample_image, source_path)
        # Simulate cancellation before worker starts
        manager.shutdown(wait=False)
        qapp.processEvents()
        pyramid = manager.pyramid_for_image_id(image_id)
        assert pyramid is not None
        assert pyramid.status != PyramidStatus.COMPLETE
        assert image_id not in manager.iter_cached_ids()

    """Tests for pyramid generation and eviction behaviour under the stub executor."""

    def test_generate_pyramid_records_completion(
        self, sample_image: QImage, qapp
    ) -> None:
        """Generate a pyramid and ensure the executor recorded a pyramid task."""
        executor = StubExecutor()
        manager = PyramidManager(config=Config(), executor=executor)
        image_id = uuid.uuid4()
        source_path = Path("image-a.png")
        manager.generate_pyramid_for_image(image_id, sample_image, source_path)
        pending = list(executor.pending_tasks())
        assert pending and pending[0].handle.category == "pyramid"
        executor.run_task(pending[0].handle.task_id)
        qapp.processEvents()
        assert executor.finished, "Pyramid worker should report completion"
        outcome = executor.finished[-1][1]
        assert outcome.success is True
        assert manager.cache_usage_bytes > 0

    def test_eviction_uses_main_thread_dispatch_when_available(
        self, sample_image: QImage, qapp
    ) -> None:
        """Eviction should prefer dispatch_to_main_thread when supported."""
        executor = StubExecutor()
        manager = PyramidManager(
            config=Config(cache={"pyramids": {"mb": 1}}), executor=executor
        )
        manager.cache_limit_bytes = 5000
        image_id = uuid.uuid4()
        source_path = Path("image-b.png")
        second_id = uuid.uuid4()
        second_path = Path("image-b2.png")
        manager.generate_pyramid_for_image(image_id, sample_image, source_path)
        first = next(executor.pending_tasks())
        executor.run_task(first.handle.task_id)
        manager.generate_pyramid_for_image(second_id, sample_image, second_path)
        second = next(
            task for task in executor.pending_tasks() if task.handle != first.handle
        )
        executor.run_task(second.handle.task_id)
        qapp.processEvents()
        maintenance = [
            record
            for record in executor.pending_tasks()
            if record.handle.category == "maintenance"
        ]
        assert maintenance, "Eviction callback should be queued"
        assert maintenance[0].callback is not None

    def test_eviction_falls_back_when_main_dispatch_missing(
        self, sample_image: QImage, qapp
    ) -> None:
        """Executors without dispatch support should receive a runnable submission."""
        executor = StubExecutor(supports_main_thread_dispatch=False)
        manager = PyramidManager(
            config=Config(cache={"pyramids": {"mb": 1}}), executor=executor
        )
        manager.cache_limit_bytes = 5000
        image_id = uuid.uuid4()
        source_path = Path("image-c.png")
        second_id = uuid.uuid4()
        second_path = Path("image-c2.png")
        manager.generate_pyramid_for_image(image_id, sample_image, source_path)
        first = next(executor.pending_tasks())
        executor.run_task(first.handle.task_id)
        manager.generate_pyramid_for_image(second_id, sample_image, second_path)
        second = next(
            task for task in executor.pending_tasks() if task.handle != first.handle
        )
        executor.run_task(second.handle.task_id)
        qapp.processEvents()
        maintenance = [
            record
            for record in executor.pending_tasks()
            if record.handle.category == "maintenance"
        ]
        assert maintenance, "Eviction runnable should be queued"
        assert maintenance[0].runnable is not None

    def test_generate_pyramid_retries_after_throttle(
        self, sample_image: QImage, qapp
    ) -> None:
        """PyramidManager should resubmit work after TaskRejected."""
        executor = RejectingStubExecutor(reject_counts={"pyramid": 1})
        manager = PyramidManager(config=Config(), executor=executor)
        image_id = uuid.uuid4()
        source_path = Path("throttled.png")
        throttled: list[tuple[uuid.UUID, int]] = []
        manager.pyramidThrottled.connect(
            lambda predictor_id, attempt: throttled.append((predictor_id, attempt))
        )
        manager.generate_pyramid_for_image(image_id, sample_image, source_path)
        assert throttled == [(image_id, 1)]
        assert executor.rejections

        def wait_for(predicate, *, timeout: float = 1.0) -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                qapp.processEvents()
                if predicate():
                    return
                time.sleep(0.01)
            raise AssertionError("condition was not met before timeout")

        wait_for(
            lambda: any(
                record.handle.category == "pyramid"
                for record in executor.pending_tasks()
            )
        )
        pending = list(executor.pending_tasks())
        assert pending
        executor.run_task(pending[0].handle.task_id)
        wait_for(
            lambda: manager.pyramid_for_image_id(image_id) is not None
            and manager.pyramid_for_image_id(image_id).status == PyramidStatus.COMPLETE
        )
        pyramid = manager.pyramid_for_image_id(image_id)
        assert pyramid is not None
        assert pyramid.status == PyramidStatus.COMPLETE

    def test_shutdown_waits_when_owning_executor(self, sample_image: QImage):
        """wait=True should stop the executor when the manager owns it."""
        executor = StubExecutor()
        manager = PyramidManager(
            config=Config(),
            executor=executor,
            owns_executor=True,
        )
        manager.shutdown(wait=True)
        assert executor.shutdown_called is True

    def test_shutdown_does_not_stop_shared_executor(self, sample_image: QImage):
        """Shared executors must not be shut down implicitly."""
        executor = StubExecutor()
        manager = PyramidManager(config=Config(), executor=executor)
        manager.shutdown(wait=True)
        assert executor.shutdown_called is False

    def test_prefetch_pyramid_skips_when_complete(self, sample_image: QImage, qapp):
        manager = PyramidManager(config=Config(), executor=StubExecutor())
        image_id = uuid.uuid4()
        source_path = Path("prefetch-complete.png")
        pyramid = ImagePyramid(
            image_id=image_id,
            source_path=source_path,
            full_resolution_image=sample_image,
        )
        pyramid.status = PyramidStatus.COMPLETE
        manager._pyramids[image_id] = pyramid
        scheduled = manager.prefetch_pyramid(image_id, sample_image, source_path)
        assert scheduled is False
        metrics = manager.snapshot_metrics()
        assert metrics.prefetch_completed >= 1

    def test_prefetch_pyramid_raises_on_missing_image_id(
        self, sample_image: QImage
    ) -> None:
        manager = PyramidManager(config=Config(), executor=StubExecutor())
        with pytest.raises(ValueError):
            manager.prefetch_pyramid(None, sample_image, None)  # type: ignore[arg-type]

    def test_generate_pyramid_raises_on_missing_image_id(
        self, sample_image: QImage
    ) -> None:
        manager = PyramidManager(config=Config(), executor=StubExecutor())
        with pytest.raises(ValueError):
            manager.generate_pyramid_for_image(None, sample_image, None)  # type: ignore[arg-type]

    def test_remove_pyramid_raises_on_missing_path(self):
        manager = PyramidManager(config=Config(), executor=StubExecutor())
        with pytest.raises(ValueError):
            manager.remove_pyramid(None)  # type: ignore[arg-type]

    def test_cancel_prefetch_updates_metrics(self, sample_image: QImage, qapp):
        manager = PyramidManager(config=Config(), executor=StubExecutor())
        manager._executor = _DummyExecutor()
        image_id = uuid.uuid4()
        manager._prefetch_begin(image_id)
        manager._active_handles[image_id] = object()
        manager._active_workers[image_id] = _DummyWorker()
        cancelled = manager.cancel_prefetch([image_id])
        assert cancelled == [image_id]
        metrics = manager.snapshot_metrics()
        assert metrics.prefetch_failed >= 1
        assert not manager._prefetch_pending(image_id)

    def test_pyramid_guard_rejects_oversized_item(
        self, caplog: pytest.LogCaptureFixture, qapp
    ) -> None:
        """Pyramids exceeding the hard budget should not be cached."""
        config = Config(
            cache={
                "mode": "hard",
                "budget_mb": 1,
                "weights": {"pyramids": 1, "tiles": 0, "masks": 0, "predictors": 0},
            }
        )
        executor = StubExecutor()
        manager = PyramidManager(config=config, executor=executor)
        large_image = QImage(2048, 2048, QImage.Format_ARGB32)
        large_image.fill(Qt.white)
        image_id = uuid.uuid4()
        source_path = Path("oversize-pyramid.png")
        with caplog.at_level(logging.WARNING):
            manager.generate_pyramid_for_image(image_id, large_image, source_path)
            pending = list(executor.pending_tasks())
            assert pending
            executor.run_task(pending[0].handle.task_id)
            qapp.processEvents()
        assert manager.cache_usage_bytes == 0
        assert list(manager.iter_cached_ids()) == []
        warnings = [
            record for record in caplog.records if "not cached" in record.message
        ]
        assert len(warnings) == 1
