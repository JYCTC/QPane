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

"""Lightweight tests for MaskService prefetch and activation decisions."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QImage

from qpane import Config
from qpane.core.config_features import MaskConfigSlice
from qpane.masks.mask import MaskManager
from qpane.masks.mask_controller import MaskController
from qpane.masks.mask_service import MaskService, PrefetchedOverlay
from qpane.masks import mask_service as mask_service_module
from qpane.types import DiagnosticRecord
from tests.helpers.executor_stubs import StubExecutor


def _build_service(qpane):
    manager = MaskManager()
    controller = MaskController(
        manager,
        lambda point: point,
        Config(),
        mask_config=MaskConfigSlice(),
    )
    service = MaskService(
        qpane=qpane,
        mask_manager=manager,
        mask_controller=controller,
        config=Config(),
        mask_config=MaskConfigSlice(),
        executor=StubExecutor(),
    )
    return service, manager, controller


@pytest.mark.usefixtures("qapp")
def test_resolve_prefetch_scales_filters_invalid(qpane_core):
    """Invalid or duplicate scales should be filtered out."""
    service, _, _ = _build_service(qpane_core)
    scales = service._resolve_prefetch_scales([1.0, 0.5, 0.5, 0, "bad", 0.25])
    assert scales == (0.5, 0.25)


@pytest.mark.usefixtures("qapp")
def test_prefetch_skips_when_disabled_or_no_executor(qpane_core):
    """Prefetch should return False when disabled or executor is unavailable."""
    service, _, _ = _build_service(qpane_core)
    image_id = uuid.uuid4()
    service.setPrefetchEnabled(False)
    assert service.prefetchColorizedMasks(image_id) is False
    service.setPrefetchEnabled(True)
    service._executor = None
    assert service.prefetchColorizedMasks(image_id) is False


@pytest.mark.usefixtures("qapp")
def test_prefetch_skips_when_no_masks(qpane_core):
    """Empty mask lists should short-circuit prefetch."""
    service, _, _ = _build_service(qpane_core)
    image_id = uuid.uuid4()
    assert service.prefetchColorizedMasks(image_id) is False
    assert service._prefetch_stats.skipped == 1


@pytest.mark.usefixtures("qapp")
def test_should_defer_activation_signals_for_small_ratio(qpane_core):
    """Large size drops should defer activation signals."""
    service, manager, _ = _build_service(qpane_core)
    prev_id = manager.create_mask(QImage(100, 100, QImage.Format_Grayscale8))
    next_id = manager.create_mask(QImage(10, 10, QImage.Format_Grayscale8))
    assert service._should_defer_activation_signals(prev_id, next_id) is True


@pytest.mark.usefixtures("qapp")
def test_should_defer_activation_signals_skips_when_sizes_grow(qpane_core):
    """Growing or equal masks should not defer activation signals."""
    service, manager, _ = _build_service(qpane_core)
    prev_id = manager.create_mask(QImage(10, 10, QImage.Format_Grayscale8))
    next_id = manager.create_mask(QImage(100, 100, QImage.Format_Grayscale8))
    assert service._should_defer_activation_signals(prev_id, next_id) is False


@pytest.mark.usefixtures("qapp")
def test_consume_prefetch_results_stashes_when_busy(qpane_core):
    """Busy masks should stash overlays and apply them once idle."""
    service, manager, controller = _build_service(qpane_core)
    mask_id = manager.create_mask(QImage(32, 32, QImage.Format_Grayscale8))
    overlay = PrefetchedOverlay(
        mask_id=mask_id,
        image=QImage(32, 32, QImage.Format_ARGB32),
        scaled=tuple(),
    )
    image_id = uuid.uuid4()
    service._prefetch_handles[image_id] = SimpleNamespace(handle=None, mask_count=1)
    service._stroke_pipeline.is_mask_busy = lambda _mid: True
    service._consume_prefetch_results(
        image_id=image_id,
        warmed=(overlay,),
        failures={},
        duration_ms=12.5,
        error=None,
        task_id=None,
    )
    assert mask_id in service._pending_prefetched_overlays
    service._stroke_pipeline.is_mask_busy = lambda _mid: False
    applied = service._maybe_apply_pending_prefetch(mask_id)
    assert applied is True


@pytest.mark.usefixtures("qapp")
def test_schedule_activation_signals_warms_and_resumes(monkeypatch, qpane_core):
    """Activation scheduling should warm caches and resume overlays for pending ids."""
    service, manager, controller = _build_service(qpane_core)
    mask_id = manager.create_mask(QImage(12, 12, QImage.Format_Grayscale8))
    image_id = uuid.uuid4()
    service._pending_activation_images.add(image_id)
    warm_calls: list[uuid.UUID | None] = []
    emit_calls: list[uuid.UUID | None] = []
    pending_calls: list[uuid.UUID | None] = []
    resume_calls: list[uuid.UUID | None] = []
    resume_update_calls: list[uuid.UUID | None] = []

    monkeypatch.setattr(controller, "warmMaskCache", lambda mid: warm_calls.append(mid))
    monkeypatch.setattr(
        controller,
        "emit_activation_signals",
        lambda mid: emit_calls.append(mid),
    )
    service.set_activation_resume_hooks(
        lambda image_id=None: resume_calls.append(image_id),
        lambda image_id=None: resume_update_calls.append(image_id),
        lambda image_id=None: pending_calls.append(image_id),
    )
    monkeypatch.setattr(
        mask_service_module.QTimer,
        "singleShot",
        lambda _ms, callback: callback(),
    )

    service._schedule_activation_signals(
        mask_id,
        warm_cache=True,
        image_id=image_id,
    )

    assert pending_calls == [image_id]
    assert warm_calls == [mask_id]
    assert emit_calls == [mask_id]
    assert resume_update_calls == [image_id]
    assert resume_calls == []
    assert image_id not in service._pending_activation_images


@pytest.mark.usefixtures("qapp")
def test_mask_service_diagnostics_provider_aggregates_recent_messages(qpane_core):
    """Diagnostics should summarize recent status entries and prefetch stats."""
    service, _, _ = _build_service(qpane_core)
    service._status_messages.clear()
    service._status_messages.append(("Mask", "Hidden"))
    service._status_messages.append(("Mask Prefetch", "Prefetch warmed 1 mask(s)"))
    service._status_messages.append(("Mask Error", "First issue"))
    service._status_messages.append(("Mask Error", "Second issue"))
    service._prefetch_stats.scheduled = 2
    service._prefetch_stats.completed = 1
    service._prefetch_stats.skipped = 0
    service._prefetch_stats.failed = 1
    service._prefetch_stats.last_message = "Prefetch warmed 1 mask(s)"
    service._prefetch_stats.last_duration_ms = 10.0

    records = service._diagnostics_provider(qpane_core)
    assert all(isinstance(record, DiagnosticRecord) for record in records)
    labels = [record.label for record in records]
    assert "Mask" not in labels
    prefetch_record = records[-1]
    assert prefetch_record.label == "Mask|Prefetch"
    assert "scheduled=2 completed=1 skipped=0 failed=1" in prefetch_record.value
    error_record = next(record for record in records if record.label == "Mask Error")
    assert "(+1 earlier)" in error_record.value


@pytest.mark.usefixtures("qapp")
def test_request_async_colorize_falls_back_to_snippet(qpane_core):
    """Async colorize should schedule snippet work when prefetch misses."""
    service, manager, controller = _build_service(qpane_core)
    mask_id = manager.create_mask(QImage(8, 8, QImage.Format_Grayscale8))
    layer = manager.get_layer(mask_id)
    assert layer is not None
    calls: list[uuid.UUID] = []
    controller.notify_async_colorize_complete = lambda mid: calls.append(mid)
    service.prefetchColorizedMasks = lambda *_args, **_kwargs: False
    service._schedule_snippet_colorize = lambda *_args, **_kwargs: False
    scheduled = service._request_async_colorize(mask_id, layer)
    assert scheduled is False
    assert calls == [mask_id]


@pytest.mark.usefixtures("qapp")
def test_invalidate_mask_cache_helpers_forward_to_controller(qpane_core):
    """Invalidate helpers should proxy to controller cache APIs."""
    service, _, controller = _build_service(qpane_core)
    mask_id = uuid.uuid4()
    image_id = uuid.uuid4()
    calls: list[tuple[str, object]] = []
    controller.invalidate_mask_cache = lambda mid: calls.append(("mask", mid))
    controller.invalidate_image_cache = lambda iid: calls.append(("image", iid))
    service.invalidateMaskCache(mask_id)
    service.invalidateMaskCachesForImage(image_id)
    service.invalidateMaskCache(None)
    service.invalidateMaskCachesForImage(None)
    assert ("mask", mask_id) in calls
    assert ("image", image_id) in calls
