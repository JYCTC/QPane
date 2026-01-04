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

"""Tests for SAM predictor cache consumer wiring."""

from __future__ import annotations
from typing import Callable, List
import uuid
import pytest
from qpane.cache.coordinator import CacheCoordinator
from qpane.cache.consumers import SamPredictorCacheConsumer


class _Signal:
    """Lightweight signal stub to mimic Qt connect/emit semantics."""

    def __init__(self) -> None:
        self._callbacks: List[Callable[..., None]] = []

    def connect(self, callback: Callable[..., None]) -> None:
        self._callbacks.append(callback)

    def emit(self, *args, **kwargs) -> None:
        for callback in list(self._callbacks):
            callback(*args, **kwargs)


class _ManagerStub:
    """Minimal manager facade exposing predictor hooks used by the consumer."""

    def __init__(self) -> None:
        self.request_calls: list[tuple[object, uuid.UUID, object]] = []
        self.cache_bytes = 0
        self.pending_bytes = 0
        self._sam_predictors: dict[uuid.UUID, object] = {}
        self.predictorReady = _Signal()
        self.predictorCacheCleared = _Signal()
        self.predictorRemoved = _Signal()

    def requestPredictor(self, image, image_id: uuid.UUID, *, source_path=None) -> None:
        self.request_calls.append((image, image_id, source_path))

    def cache_usage_bytes(self) -> int:
        return self.cache_bytes

    def pendingUsageBytes(self) -> int:
        return self.pending_bytes

    def predictorImageIds(self) -> list[uuid.UUID]:
        return list(self._sam_predictors.keys())

    def cancelPendingPredictor(self, image_id: uuid.UUID) -> bool:
        return False

    def removeFromCache(self, image_id: uuid.UUID) -> bool:
        self._sam_predictors.pop(image_id, None)
        self.cache_bytes = 0
        return True


class _ManagerMissingHook(_ManagerStub):
    """Manager stub that omits a required hook to simulate miswiring."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelPendingPredictor = None


def test_predictor_consumer_tracks_pending_and_ready_usage():
    manager = _ManagerStub()
    coordinator = CacheCoordinator(512 * 1024 * 1024)
    consumer = SamPredictorCacheConsumer(manager, coordinator)
    image_id = uuid.uuid4()
    manager.pending_bytes = 4096
    manager.cache_bytes = 0
    manager.requestPredictor(None, image_id, source_path=None)  # wrapped by consumer
    snapshot = coordinator.snapshot()
    assert snapshot["consumers"]["predictors"]["usage_bytes"] == 4096
    manager.pending_bytes = 0
    manager.cache_bytes = 2048
    manager._sam_predictors[image_id] = object()
    manager.predictorReady.emit(object(), image_id)
    snapshot = coordinator.snapshot()
    assert snapshot["consumers"]["predictors"]["usage_bytes"] == 2048
    consumer._trim_to(0)
    snapshot = coordinator.snapshot()
    assert snapshot["consumers"]["predictors"]["usage_bytes"] == 0


def test_predictor_consumer_errors_when_required_hook_missing(caplog):
    manager = _ManagerMissingHook()
    coordinator = CacheCoordinator(512 * 1024 * 1024)
    with (
        caplog.at_level("ERROR"),
        pytest.raises(RuntimeError, match="cancelPendingPredictor"),
    ):
        SamPredictorCacheConsumer(manager, coordinator)
    assert (
        "Cannot wrap missing manager hook _ManagerMissingHook.cancelPendingPredictor"
        in caplog.text
    )
