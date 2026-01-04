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

from __future__ import annotations

import math
from pathlib import Path
import uuid

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QImage, Qt
from PySide6.QtWidgets import QWidget

from qpane.core import CacheSettings
from qpane.rendering import RenderStrategy, RenderingPresenter, ViewportZoomMode


def _cleanup_qpane(widget: QWidget, qapp) -> None:
    widget.deleteLater()
    qapp.processEvents()


def _make_image(
    width: int,
    height: int,
    color: Qt.GlobalColor = Qt.white,
) -> QImage:
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.fill(color)
    return image


class StubSettings:
    """Lightweight Config replacement covering presenter dependencies."""

    def __init__(
        self,
        *,
        tile_size: int = 256,
        tile_overlap: int = 0,
        draw_tile_grid: bool = False,
        min_view_size_px: int = 4,
        canvas_expansion_factor: float = 1.0,
        safe_min_zoom: float = 1e-3,
    ) -> None:
        self.tile_size = tile_size
        self.tile_overlap = tile_overlap
        self.draw_tile_grid = draw_tile_grid
        self.min_view_size_px = min_view_size_px
        self.canvas_expansion_factor = canvas_expansion_factor
        self.safe_min_zoom = safe_min_zoom
        self.cache = CacheSettings(mode="hard", budget_mb=8)
        self.cache.set_override_mb("tiles", 8)


class StubView:
    """Expose the viewport reference expected by CoordinateContext."""

    def __init__(self) -> None:
        self.viewport = None


class StubQPane(QWidget):
    """Minimal QWidget exposing the attributes accessed by RenderingPresenter."""

    def __init__(
        self,
        *,
        settings: StubSettings,
        size: tuple[int, int],
        dpr: float,
    ) -> None:
        super().__init__()
        self.setAttribute(Qt.WA_DontShowOnScreen, True)
        self.settings = settings
        self._dpr = dpr
        self._view = StubView()
        self.original_image = QImage()
        self.currentImagePath: Path | None = None
        self._current_image_id = uuid.uuid4()
        self._is_blank = True
        self.resize(*size)

    def view(self):
        """Return the stub view the presenter expects."""
        return self._view

    def attach_presenter(self, presenter) -> None:
        """Backfill the viewport reference once the presenter is built."""
        self._view.presenter = presenter
        self._view.viewport = presenter.viewport

    def devicePixelRatioF(self) -> float:  # pragma: no cover - Qt override
        return self._dpr

    def set_device_pixel_ratio(self, dpr: float) -> None:
        """Update the DPR backing physical viewport calculations."""
        self._dpr = dpr

    def physicalViewportRect(self) -> QRectF:  # pragma: no cover - Qt override
        rect = QRectF(self.rect())
        rect.setWidth(rect.width() * self._dpr)
        rect.setHeight(rect.height() * self._dpr)
        return rect

    def currentImageID(self):  # pragma: no cover - stub for presenter lookup
        return self._current_image_id


class StubCatalog:
    """Simplified catalog returning a preloaded QImage."""

    def __init__(self, image: QImage) -> None:
        self._base_image = image
        self._resolver = None

    @property
    def base_image(self) -> QImage:
        """Return the image used when best-fit logic is not overridden."""
        return self._base_image

    def set_base_image(self, image: QImage) -> None:
        """Replace the stored image backing presenter lookups."""
        self._base_image = image

    def set_best_fit_resolver(self, resolver) -> None:
        """Inject a callable that mirrors ImageCatalog.getBestFitImage."""
        self._resolver = resolver

    def getBestFitImage(self, image_id, width):  # pragma: no cover - simple passthrough
        if self._resolver is not None:
            return self._resolver(image_id, width)
        return self._base_image


class _NullHandle:
    """Provide the cancel API expected by TileManager."""

    def cancel(self) -> None:  # pragma: no cover - noop helper
        return None


class NoopExecutor:
    """Executor shim satisfying TileManager without spinning threads."""

    def submit(self, *_args, **_kwargs):  # pragma: no cover - noop helper
        return _NullHandle()


class PresenterHarness:
    """Bundle a stub qpane, catalog, and presenter for fast tests."""

    def __init__(
        self,
        *,
        qpane_size: tuple[int, int] = (256, 256),
        image_size: tuple[int, int] = (128, 128),
        color: Qt.GlobalColor = Qt.white,
        dpr: float = 1.0,
    ) -> None:
        self.settings = StubSettings()
        self.qpane = StubQPane(settings=self.settings, size=qpane_size, dpr=dpr)
        base_image = _make_image(image_size[0], image_size[1], color)
        self.catalog = StubCatalog(base_image)
        self.executor = NoopExecutor()
        self.qpane.original_image = base_image
        self.qpane.currentImagePath = Path("stub.png")
        self.presenter = RenderingPresenter(
            qpane=self.qpane,
            catalog=self.catalog,
            cache_registry=None,
            executor=self.executor,
        )
        self.qpane.attach_presenter(self.presenter)
        self.viewport = self.presenter.viewport
        self.viewport.setContentSize(base_image.size())

    def set_image(
        self,
        image: QImage,
        *,
        path: Path | None = None,
        image_id: uuid.UUID | None = None,
    ) -> None:
        """Update the original image and catalog backing data."""
        self.catalog.set_base_image(image)
        self.qpane.original_image = image
        if path is not None:
            self.qpane.currentImagePath = path
        if image_id is not None:
            self.qpane._current_image_id = image_id
        self.viewport.setContentSize(image.size())

    def set_catalog_resolver(self, resolver) -> None:
        """Proxy helper for custom best-fit lookups."""
        self.catalog.set_best_fit_resolver(resolver)

    def resize_qpane(self, width: int, height: int) -> None:
        """Resize the qpane widget and trigger viewport updates."""
        self.qpane.resize(width, height)

    def set_device_pixel_ratio(self, dpr: float) -> None:
        """Update the qpane DPR used by CoordinateContext."""
        self.qpane.set_device_pixel_ratio(dpr)


class StubTileManager:
    def __init__(self, tile_size: int = 128, tile_overlap: int = 0) -> None:
        self.tile_size = tile_size
        self.tile_overlap = tile_overlap
        self.requested: list = []
        self.cancelled = None

    def calculate_grid_dimensions(self, width: int, height: int) -> tuple[int, int]:
        cols = max(1, math.ceil(width / self.tile_size))
        rows = max(1, math.ceil(height / self.tile_size))
        return cols, rows

    def get_tile(self, identifier, source_image) -> QImage:
        tile = _make_image(self.tile_size, self.tile_size, Qt.black)
        self.requested.append(identifier)
        return tile

    def cancel_invisible_workers(self, visible_ids) -> None:
        self.cancelled = frozenset(visible_ids)


def test_presenter_calculateRenderState_blank_returns_none(qapp):
    harness = PresenterHarness(qpane_size=(64, 64), image_size=(32, 32))
    try:
        state = harness.presenter.calculateRenderState(is_blank=True)
        assert state is None
    finally:
        _cleanup_qpane(harness.qpane, qapp)


def test_presenter_calculateRenderState_enters_tile_mode_when_zoomed(qapp):
    harness = PresenterHarness(
        qpane_size=(256, 256),
        image_size=(2048, 2048),
        color=Qt.blue,
    )
    try:
        stub_manager = StubTileManager(tile_size=256, tile_overlap=0)
        presenter = harness.presenter
        presenter.tile_manager = stub_manager
        harness.viewport.zoom = 4.0
        harness.qpane.currentImagePath = Path("tile.png")
        state = presenter.calculateRenderState(is_blank=False)
        assert state is not None
        assert state.strategy == RenderStrategy.TILE
        assert stub_manager.requested
        assert len(state.tiles_to_draw) == len(stub_manager.requested)
        assert stub_manager.cancelled is not None
    finally:
        _cleanup_qpane(harness.qpane, qapp)


def test_presenter_handle_resize_invokes_expected_branch(monkeypatch, qapp):
    harness = PresenterHarness()
    try:
        presenter = harness.presenter
        fit_calls: list[str] = []
        pan_calls: list[QPointF] = []
        alloc_calls: list[str] = []
        monkeypatch.setattr(
            presenter.viewport, "setZoomFit", lambda: fit_calls.append("fit")
        )

        def fake_set_pan(value: QPointF) -> None:
            pan_calls.append(value)

        monkeypatch.setattr(presenter.viewport, "setPan", fake_set_pan)
        monkeypatch.setattr(
            presenter,
            "allocate_buffers",
            lambda: alloc_calls.append("alloc"),
        )
        presenter.viewport.zoom_mode = ViewportZoomMode.FIT
        presenter.handle_resize()
        assert fit_calls == ["fit"]
        assert alloc_calls == ["alloc"]
        fit_calls.clear()
        alloc_calls.clear()
        pan_calls.clear()
        presenter.viewport.zoom_mode = ViewportZoomMode.CUSTOM
        presenter.handle_resize()
        assert pan_calls and pan_calls[-1] == presenter.viewport.pan
        assert alloc_calls == ["alloc"]
    finally:
        _cleanup_qpane(harness.qpane, qapp)


def test_presenter_paint_reallocates_when_buffer_size_stale(monkeypatch, qapp):
    harness = PresenterHarness(
        qpane_size=(160, 120),
        image_size=(64, 64),
        color=Qt.yellow,
    )
    try:
        harness.qpane.currentImagePath = Path("stale.png")
        presenter = harness.presenter

        class BufferGuardRenderer:
            def __init__(self) -> None:
                self._buffer = QImage(
                    32,
                    32,
                    QImage.Format_ARGB32_Premultiplied,
                )
                self._buffer.fill(Qt.black)
                self.allocations = 0

            def paint(self, state) -> None:
                self.state = state

            def get_base_buffer(self) -> QImage:
                return self._buffer

            def get_subpixel_pan_offset(self) -> QPointF:
                return QPointF(0, 0)

            def allocate_buffers(self, size, dpr):
                self.allocations += 1
                self._buffer = QImage(
                    size.width(),
                    size.height(),
                    QImage.Format_ARGB32_Premultiplied,
                )
                self._buffer.fill(Qt.black)

            def markDirty(self, dirty_rect=None):
                pass

        guard_renderer = BufferGuardRenderer()
        monkeypatch.setattr(presenter, "renderer", guard_renderer)
        presenter.paint(
            is_blank=False,
            content_overlays={},
            overlays_suspended=True,
            draw_tool_overlay=None,
        )
        assert guard_renderer.allocations == 1
        expected_size = presenter._qpane_physical_size()
        assert guard_renderer.get_base_buffer().size() == expected_size
    finally:
        _cleanup_qpane(harness.qpane, qapp)


def test_presenter_paint_skips_allocation_when_buffer_size_matches(monkeypatch, qapp):
    harness = PresenterHarness(
        qpane_size=(128, 96),
        image_size=(64, 64),
        color=Qt.cyan,
    )
    try:
        harness.qpane.currentImagePath = Path("fresh.png")
        presenter = harness.presenter
        target_size = presenter._qpane_physical_size()

        class BufferGuardRenderer:
            def __init__(self) -> None:
                self._buffer = QImage(
                    target_size.width(),
                    target_size.height(),
                    QImage.Format_ARGB32_Premultiplied,
                )
                self._buffer.fill(Qt.black)
                self.allocations = 0

            def paint(self, state) -> None:
                self.state = state

            def get_base_buffer(self) -> QImage:
                return self._buffer

            def get_subpixel_pan_offset(self) -> QPointF:
                return QPointF(0, 0)

            def allocate_buffers(self, size, dpr):
                self.allocations += 1

            def markDirty(self, dirty_rect=None):
                pass

        guard_renderer = BufferGuardRenderer()
        monkeypatch.setattr(presenter, "renderer", guard_renderer)
        presenter.paint(
            is_blank=False,
            content_overlays={},
            overlays_suspended=True,
            draw_tool_overlay=None,
        )
        assert guard_renderer.allocations == 0
    finally:
        _cleanup_qpane(harness.qpane, qapp)


def test_presenter_paint_restores_transform_before_tool_overlay(monkeypatch, qapp):
    harness = PresenterHarness()
    try:
        presenter = harness.presenter

        class StubRenderer:
            def __init__(self) -> None:
                self._buffer = _make_image(16, 16, Qt.green)

            def paint(self, state) -> None:
                self.state = state

            def get_base_buffer(self) -> QImage:
                return self._buffer

            def get_subpixel_pan_offset(self) -> QPointF:
                return QPointF(0.5, 0.25)

            def allocate_buffers(self, size, dpr):
                self._buffer = _make_image(
                    size.width(),
                    size.height(),
                    Qt.green,
                )

        stub_renderer = StubRenderer()
        monkeypatch.setattr(presenter, "renderer", stub_renderer)
        monkeypatch.setattr(
            presenter,
            "calculateRenderState",
            lambda **_: object(),
        )
        observed_transforms = []

        def capture_tool_overlay(painter):
            observed_transforms.append(painter.transform())

        presenter.paint(
            is_blank=False,
            content_overlays={},
            overlays_suspended=True,
            draw_tool_overlay=capture_tool_overlay,
        )
        assert observed_transforms
        assert observed_transforms[-1].isIdentity()
    finally:
        _cleanup_qpane(harness.qpane, qapp)


def test_presenter_strategy_threshold_uses_physical_viewport(monkeypatch, qapp):
    harness = PresenterHarness(
        qpane_size=(200, 120),
        image_size=(1024, 1024),
        color=Qt.darkGray,
    )
    try:
        stub_manager = StubTileManager(tile_size=256, tile_overlap=0)
        presenter = harness.presenter
        presenter.tile_manager = stub_manager
        image_width = harness.qpane.original_image.width()
        image_height = harness.qpane.original_image.height()
        for dpr in (1.0, 2.0):
            harness.set_device_pixel_ratio(dpr)
            physical_width = harness.qpane.width() * dpr
            physical_height = harness.qpane.height() * dpr
            threshold_zoom = min(
                physical_width / image_width, physical_height / image_height
            )
            harness.viewport.zoom = threshold_zoom * 0.95
            state = presenter.calculateRenderState(is_blank=False)
            assert state is not None
            assert state.strategy == RenderStrategy.DIRECT
            harness.viewport.zoom = threshold_zoom * 1.05
            state = presenter.calculateRenderState(is_blank=False)
            assert state is not None
            assert state.strategy == RenderStrategy.TILE
    finally:
        _cleanup_qpane(harness.qpane, qapp)
