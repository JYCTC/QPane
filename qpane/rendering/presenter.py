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

"""Rendering presenter responsible for QPane's drawing pipeline."""

from __future__ import annotations


import logging

from math import isclose

from typing import TYPE_CHECKING, Callable, Mapping


from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, QSizeF

from PySide6.QtGui import QImage, QPainter, Qt

from PySide6.QtWidgets import QWidget


from .coordinates import CoordinateContext, PanelHitTest

from .render import Renderer, RenderState, RenderStrategy, TileRenderData

from .tiles import TileIdentifier, TileManager

from .viewport import Viewport, ViewportZoomMode
from ..types import OverlayState


if TYPE_CHECKING:
    from ..cache.registry import CacheRegistry
    from ..catalog import ImageCatalog
    from ..concurrency import TaskExecutorProtocol
    from ..core import OverlayDrawFn
    from ..qpane import QPane
logger = logging.getLogger(__name__)


class RenderingPresenter:
    """Encapsulate rendering-specific state and QWidget hooks for QPane."""

    def __init__(
        self,
        *,
        qpane: "QPane",
        catalog: "ImageCatalog",
        cache_registry: "CacheRegistry" | None,
        executor: "TaskExecutorProtocol",
    ) -> None:
        """Compose viewport/tile/renderer collaborators owned by the presenter."""
        self._qpane = qpane
        self._catalog = catalog
        self.viewport = Viewport(qpane, qpane.settings)
        self.tile_manager = TileManager(qpane.settings, parent=qpane, executor=executor)
        if cache_registry is not None:
            cache_registry.attach_tile_manager(self.tile_manager)
        self.renderer = Renderer(qpane)
        self._last_view_size = QSize()
        self._last_device_pixel_ratio = float(qpane.devicePixelRatioF())

    def calculateRenderState(
        self,
        *,
        use_pan: QPointF | None = None,
        is_blank: bool = False,
    ) -> RenderState | None:
        """Build a RenderState snapshot for the current viewport."""
        original_image = self._qpane.original_image
        if original_image.isNull() or is_blank:
            return None
        target_width = original_image.width() * self.viewport.zoom
        source_image = self._catalog.getBestFitImage(
            self._qpane.currentImageID(), target_width
        )
        if source_image is None or source_image.isNull():
            source_image = original_image
        pyramid_scale = (
            source_image.width() / original_image.width()
            if original_image.width() > 0
            else 1.0
        )
        canvas_size_physical = QSizeF(original_image.size()) * self.viewport.zoom
        physical_viewport_rect = self.physical_viewport_rect()
        viewport_size_physical = QSizeF(physical_viewport_rect.size())
        strategy = RenderStrategy.DIRECT
        if (
            canvas_size_physical.width() > viewport_size_physical.width()
            or canvas_size_physical.height() > viewport_size_physical.height()
        ):
            strategy = RenderStrategy.TILE
        transform = self.viewport.get_transform(
            source_image.size(), pyramid_scale, pan_override=use_pan
        )
        pan_value = use_pan if use_pan is not None else self.viewport.pan
        render_hint_enabled = self.viewport.zoom < self.viewport.nativeZoom() * 2.0
        debug_draw_tile_grid = self._qpane.settings.draw_tile_grid
        tile_size = self.tile_manager.tile_size
        tile_overlap = self.tile_manager.tile_overlap
        max_cols = 0
        max_rows = 0
        tiles_to_draw: list[TileRenderData] = []
        current_id = self._qpane.currentImageID()
        current_path = self._qpane.currentImagePath
        visible_range: tuple[int, int, int, int] | None = None
        if strategy == RenderStrategy.TILE:
            max_cols, max_rows = self.tile_manager.calculate_grid_dimensions(
                source_image.width(), source_image.height()
            )
            visible_range = self._calculate_visible_tile_range(
                source_image=source_image,
                zoom=self.viewport.zoom,
                pyramid_scale=pyramid_scale,
                current_pan=pan_value,
                physical_viewport_rect=physical_viewport_rect,
                tile_size=tile_size,
                tile_overlap=tile_overlap,
                max_cols=max_cols,
                max_rows=max_rows,
            )
            start_row, end_row, start_col, end_col = visible_range
            visible_ids: set[TileIdentifier] = set()
            for row in range(start_row, end_row + 1):
                for col in range(start_col, end_col + 1):
                    if current_id is None:
                        continue
                    ident = TileIdentifier(
                        image_id=current_id,
                        source_path=current_path,
                        pyramid_scale=pyramid_scale,
                        row=row,
                        col=col,
                    )
                    visible_ids.add(ident)
                    tile_image = self.tile_manager.get_tile(ident, source_image)
                    if tile_image:
                        draw_pos = self.get_tile_draw_position(ident)
                        tiles_to_draw.append(TileRenderData(tile_image, draw_pos))
            self.tile_manager.cancel_invisible_workers(visible_ids)
        return RenderState(
            source_image=source_image,
            pyramid_scale=pyramid_scale,
            transform=transform,
            zoom=self.viewport.zoom,
            strategy=strategy,
            render_hint_enabled=render_hint_enabled,
            debug_draw_tile_grid=debug_draw_tile_grid,
            tiles_to_draw=tiles_to_draw,
            tile_size=tile_size,
            tile_overlap=tile_overlap,
            max_tile_cols=max_cols,
            max_tile_rows=max_rows,
            qpane_rect=self._qpane.rect(),
            current_pan=pan_value,
            physical_viewport_rect=physical_viewport_rect,
            visible_tile_range=visible_range,
        )

    def paint(
        self,
        *,
        is_blank: bool,
        content_overlays: Mapping[str, "OverlayDrawFn"],
        overlays_suspended: bool,
        draw_tool_overlay: Callable[[QPainter], None] | None,
    ) -> RenderState | None:
        """Render the current frame and return the RenderState used."""
        if is_blank:
            render_state = (
                self.calculateRenderState(is_blank=is_blank)
                if content_overlays
                else None
            )
            painter = QPainter(self._qpane)
            try:
                painter.fillRect(self._qpane.rect(), Qt.transparent)
                if render_state and not overlays_suspended:
                    overlay_state = self._build_overlay_state(render_state)
                    for draw_overlay in content_overlays.values():
                        draw_overlay(painter, overlay_state)
            finally:
                painter.end()
            return render_state
        render_state = self.calculateRenderState(is_blank=is_blank)
        if render_state:
            self._ensure_buffer_matches_widget()
            self.renderer.paint(render_state)
        painter = QPainter(self._qpane)
        transform_applied = False
        try:
            base_buffer = self.renderer.get_base_buffer()
            if base_buffer:
                offset = self.renderer.get_subpixel_pan_offset()
                if offset != QPointF(0, 0):
                    context = CoordinateContext(self._qpane)
                    logical_offset = context.physical_to_logical(offset)
                    painter.translate(logical_offset)
                    transform_applied = True
                painter.drawImage(0, 0, base_buffer)
            if render_state and not overlays_suspended:
                overlay_state = self._build_overlay_state(render_state)
                for draw_overlay in content_overlays.values():
                    draw_overlay(painter, overlay_state)
            if transform_applied:
                painter.resetTransform()
            if draw_tool_overlay and not is_blank:
                draw_tool_overlay(painter)
        finally:
            painter.end()
        return render_state

    def _build_overlay_state(self, render_state: RenderState) -> OverlayState:
        """Project a RenderState snapshot onto the public OverlayState surface."""
        return OverlayState(
            zoom=render_state.zoom,
            qpane_rect=render_state.qpane_rect,
            source_image=render_state.source_image,
            transform=render_state.transform,
            current_pan=render_state.current_pan,
            physical_viewport_rect=render_state.physical_viewport_rect,
        )

    def mark_dirty(self, dirty_rect: QRect | QRectF | None = None) -> None:
        """Forward dirty-region notifications to the renderer."""
        self.renderer.markDirty(dirty_rect)

    def allocate_buffers(self) -> None:
        """Allocate the renderer buffers to match the current widget size."""
        self._refresh_backing_buffers()

    def ensure_view_alignment(self, *, force: bool = False) -> None:
        """Reapply FIT/custom zoom and buffers when the qpane geometry changes."""
        current_size = self._qpane.size()
        current_dpr = float(self._qpane.devicePixelRatioF())
        dpr_changed = not isclose(
            current_dpr, self._last_device_pixel_ratio, rel_tol=1e-9, abs_tol=1e-9
        )
        if not force and current_size == self._last_view_size and not dpr_changed:
            return
        zoom_mode = self.viewport.get_zoom_mode()
        if zoom_mode == ViewportZoomMode.FIT:
            self.viewport.setZoomFit()
        else:
            self.viewport.setPan(self.viewport.pan)
        self.allocate_buffers()
        self._last_view_size = QSize(current_size)
        self._last_device_pixel_ratio = current_dpr

    def physical_viewport_rect(self) -> QRectF:
        """Return the viewport rectangle expressed in device pixels."""
        context = CoordinateContext(self._qpane)
        return context.logical_to_physical(QRectF(self._qpane.rect()))

    def panel_to_image_point(self, panel_pos: QPoint) -> QPoint | None:
        """Convert a panel coordinate into image space using the viewport."""
        return self.viewport.panel_to_content_point(panel_pos)

    def panel_hit_test(self, panel_pos: QPoint) -> PanelHitTest | None:
        """Return hit-test metadata for panel coordinates via the viewport."""
        return self.viewport.panel_hit_test(panel_pos)

    def image_to_panel_point(self, image_point: QPoint) -> QPointF | None:
        """Project an image-space coordinate into the widget."""
        return self.viewport.content_to_panel_point(image_point)

    def handle_resize(self) -> None:
        """Respond to QWidget resize events."""
        if self.viewport.get_zoom_mode() == ViewportZoomMode.FIT:
            self._handle_resize_fit_mode()
        else:
            self._handle_resize_custom_mode()
        self.allocate_buffers()

    def minimum_size_hint(self) -> QSize:
        """Return the safe minimum widget size for the current image."""
        original_image = self._qpane.original_image
        if original_image.isNull():
            base_hint = QWidget.minimumSizeHint(self._qpane)
            if base_hint.isValid() and not base_hint.isNull():
                return base_hint
            return QSize(1, 1)
        safe_min_zoom = getattr(self._qpane.settings, "safe_min_zoom", 1e-3)
        min_zoom = max(self.viewport.min_zoom(), safe_min_zoom)
        min_width = max(1, int(round(original_image.width() * min_zoom)))
        min_height = max(1, int(round(original_image.height() * min_zoom)))
        return QSize(min_width, min_height)

    def _qpane_physical_size(self) -> QSize:
        """Return the qpane's current size expressed in device pixels."""
        context = CoordinateContext(self._qpane)
        logical_size = QSizeF(self._qpane.size())
        return context.logical_to_physical(logical_size).toSize()

    def _refresh_backing_buffers(self) -> None:
        """Rebuild renderer buffers based on the current widget DPR and size."""
        physical_size = self._qpane_physical_size()
        dpr = self._qpane.devicePixelRatioF()
        self.renderer.allocate_buffers(physical_size, dpr)

    def _ensure_buffer_matches_widget(self) -> None:
        """Reallocate renderer buffers when the widget size has changed."""
        base_buffer = self.renderer.get_base_buffer()
        if base_buffer is None:
            self.allocate_buffers()
            return
        expected_size = self._qpane_physical_size()
        if base_buffer.size() != expected_size:
            self.allocate_buffers()

    # Internal helpers

    def get_tile_draw_position(self, identifier: TileIdentifier) -> QPointF:
        """Return the upper-left draw position for ``identifier`` in source coords."""
        stride = self.tile_manager.tile_size - self.tile_manager.tile_overlap
        draw_x = identifier.col * stride
        draw_y = identifier.row * stride
        return QPointF(draw_x, draw_y)

    def _calculate_visible_tile_range(
        self,
        *,
        source_image: QImage,
        zoom: float,
        pyramid_scale: float,
        current_pan: QPointF,
        physical_viewport_rect: QRectF,
        tile_size: int,
        tile_overlap: int,
        max_cols: int,
        max_rows: int,
    ) -> tuple[int, int, int, int]:
        """Compute the inclusive tile index bounds that intersect the viewport."""
        qpane_center_phys = QPointF(physical_viewport_rect.center())
        source_center = QPointF(source_image.width() / 2.0, source_image.height() / 2.0)
        effective_zoom = zoom / pyramid_scale if pyramid_scale != 0 else zoom
        tl_rel_center = (
            physical_viewport_rect.topLeft() - qpane_center_phys - current_pan
        )
        tl_source_coords = (tl_rel_center / effective_zoom) + source_center
        br_rel_center = (
            physical_viewport_rect.bottomRight() - qpane_center_phys - current_pan
        )
        br_source_coords = (br_rel_center / effective_zoom) + source_center
        visible_rect = QRectF(tl_source_coords, br_source_coords).normalized()
        stride = tile_size - tile_overlap
        if stride <= 0:
            logger.error(
                "Tile stride is non-positive; size=%s overlap=%s max_cols=%s max_rows=%s",
                tile_size,
                tile_overlap,
                max_cols,
                max_rows,
            )
            return 0, -1, 0, -1
        start_col = max(0, int(visible_rect.left() / stride) - 1)
        start_row = max(0, int(visible_rect.top() / stride) - 1)
        end_col = min(max_cols - 1, int(visible_rect.right() / stride) + 1)
        end_row = min(max_rows - 1, int(visible_rect.bottom() / stride) + 1)
        return start_row, end_row, start_col, end_col

    def _handle_resize_fit_mode(self) -> None:
        """Keep the viewport zoom aligned with the available widget size in FIT mode."""
        self.viewport.setZoomFit()

    def _handle_resize_custom_mode(self) -> None:
        """Reapply the current pan so it is clamped after a custom-mode resize."""
        self.viewport.setPan(self.viewport.pan)
