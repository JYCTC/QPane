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

"""Rendering pipeline and metrics helpers for the QPane viewer."""

import time

from dataclasses import dataclass

from enum import Enum

from typing import TYPE_CHECKING


from PySide6.QtCore import QPointF, QRect, QRectF, QSize, QSizeF, Qt

from PySide6.QtGui import QColor, QImage, QPainter, QPen, QRegion, QTransform


from .coordinates import CoordinateContext


if TYPE_CHECKING:
    from ..qpane import QPane


@dataclass(frozen=True)
class TileRenderData:
    """An immutable container for a single tile to be rendered."""

    image: QImage
    draw_pos: QPointF


class RenderStrategy(str, Enum):
    """Supported rendering strategies for the base buffer pipeline."""

    DIRECT = "direct"
    TILE = "tile"


@dataclass(frozen=True)
class RenderState:
    """An immutable container for all parameters needed for a single render pass."""

    # Image and view properties
    source_image: QImage
    pyramid_scale: float
    transform: QTransform
    zoom: float
    strategy: RenderStrategy
    render_hint_enabled: bool
    # Overlay/debug data
    debug_draw_tile_grid: bool
    # Tiling properties
    tiles_to_draw: list[TileRenderData]
    tile_size: int
    tile_overlap: int
    max_tile_cols: int
    max_tile_rows: int
    # Context from the QPane
    qpane_rect: QRect
    current_pan: QPointF
    physical_viewport_rect: QRectF
    visible_tile_range: tuple[int, int, int, int] | None


@dataclass(frozen=True)
class RendererMetrics:
    """Runtime counters describing renderer buffer reuse behaviour."""

    base_buffer_allocations: int
    scroll_attempts: int
    scroll_hits: int
    scroll_misses: int
    full_redraws: int
    partial_redraws: int
    last_paint_ms: float


class Renderer:
    """Own the offscreen buffers plus reuse heuristics for the QPane widget."""

    def __init__(self, qpane: "QPane"):
        """Bind rendering to `qpane` while tracking buffer reuse health."""
        self._qpane = qpane
        self._current_render_state: RenderState | None = None
        self._base_image_buffer = None
        self._dirty_region = QRegion()
        self._buffer_pan = QPointF(0, 0)
        self._subpixel_pan_offset = QPointF(0, 0)
        self._scroll_temp: QImage | None = None
        self._last_paint_duration_ms = 0.0
        self._paint_duration_sum_ms = 0.0
        self._paint_duration_count = 0
        self._paint_duration_max_ms = 0.0
        self._base_buffer_allocations = 0
        self._scroll_attempts = 0
        self._scroll_hits = 0
        self._scroll_misses = 0
        self._full_redraws = 0
        self._partial_redraws = 0

    @property
    def qpane(self) -> "QPane":
        """Return the QPane associated with this renderer."""
        return self._qpane

    def paint(self, state: RenderState):
        """Prepare offscreen buffers for the requested state without drawing to the widget."""
        start_time = time.perf_counter()
        # Ensure buffers are allocated. The QPane is responsible for calling
        # _allocate_buffers on resize, but we need to handle the initial case.
        if self._base_image_buffer is None:
            self.markDirty(state.qpane_rect)  # Mark entire view dirty for first paint
        # Redraw dirty buffers if any region has been marked as dirty.
        if not self._dirty_region.isEmpty():
            # Pass the entire region object and state to the redraw methods.
            self._redraw_base_image_buffer(self._dirty_region, state)
        # Clear the dirty region now that buffer painting is complete for this frame.
        self._dirty_region = QRegion()
        end_time = time.perf_counter()
        self._last_paint_duration_ms = (end_time - start_time) * 1000
        if self._last_paint_duration_ms > 0.0:
            self._paint_duration_sum_ms += self._last_paint_duration_ms
            self._paint_duration_count += 1
            self._paint_duration_max_ms = max(
                self._paint_duration_max_ms, self._last_paint_duration_ms
            )
        self._mark_diagnostics_dirty()

    def allocate_buffers(self, physical_size: QSize, dpr: float):
        """Allocate and clear the base buffer sized to the physical viewport."""
        self._base_image_buffer = self._allocate_dpi_buffer(physical_size, dpr)
        self._base_image_buffer.fill(Qt.transparent)
        self._scroll_temp = None
        self._base_buffer_allocations += 1
        # Mark the entire view as dirty since the buffers are new.
        self.markDirty()

    def markDirty(self, dirty_rect: QRect | QRectF | QRegion | None = None):
        """Mark a region dirty for the next render pass; None targets the full viewport."""
        if dirty_rect is None:
            self._dirty_region += QRect(-100000, -100000, 200000, 200000)
            return
        if isinstance(dirty_rect, QRegion):
            if not dirty_rect.isEmpty():
                self._dirty_region += QRegion(dirty_rect)
            return
        if isinstance(dirty_rect, QRectF):
            dirty_rect = dirty_rect.toAlignedRect()
        if isinstance(dirty_rect, QRect):
            if dirty_rect.isEmpty():
                return
            self._dirty_region += dirty_rect
            return
        raise TypeError(f"Unsupported dirty input: {type(dirty_rect)!r}")

    def tryScrollBuffers(self, new_pan: QPointF) -> bool:
        """Attempts to reuse the existing buffer by scrolling and repairing edge strips."""
        if self._base_image_buffer is None:
            return False
        delta_pan = new_pan - self._buffer_pan
        self._scroll_attempts += 1
        dx = int(delta_pan.x())
        dy = int(delta_pan.y())
        if dx == 0 and dy == 0:
            self._scroll_hits += 1
            viewport = self.qpane.view().viewport
            self._subpixel_pan_offset = viewport.pan - self._buffer_pan
            self.qpane.update()
            return True
        if (
            abs(dx) >= self._base_image_buffer.width()
            or abs(dy) >= self._base_image_buffer.height()
        ):
            self._scroll_misses += 1
            return False
        context = CoordinateContext(self.qpane)
        logical_delta = context.physical_to_logical(QPointF(dx, dy))
        base_image = self._base_image_buffer
        if (
            self._scroll_temp is None
            or self._scroll_temp.size() != base_image.size()
            or self._scroll_temp.devicePixelRatio() != base_image.devicePixelRatio()
        ):
            self._scroll_temp = self._allocate_dpi_buffer(
                base_image.size(), base_image.devicePixelRatio()
            )
        self._scroll_temp.swap(self._base_image_buffer)
        self._base_image_buffer.fill(Qt.transparent)
        painter = QPainter(self._base_image_buffer)
        painter.drawImage(logical_delta, self._scroll_temp)
        painter.end()
        self._mark_diagnostics_dirty()
        self._buffer_pan += QPointF(dx, dy)
        w = self._base_image_buffer.width()
        h = self._base_image_buffer.height()
        repair_rects: list[QRect] = []
        if dy > 0:
            repair_rects.append(QRect(0, 0, w, dy))
        if dy < 0:
            repair_rects.append(QRect(0, h + dy, w, -dy))
        if dx > 0:
            repair_rects.append(QRect(0, 0, dx, h))
        if dx < 0:
            repair_rects.append(QRect(w + dx, 0, -dx, h))
        if repair_rects:
            qpane_calculate = getattr(self.qpane, "calculateRenderState", None)
            if not callable(qpane_calculate):
                raise AttributeError(
                    "QPane must provide calculateRenderState for buffer repair"
                )
            state = qpane_calculate(use_pan=self._buffer_pan)
            if state:
                self._repair_base_buffer_strips(repair_rects, state)
        self._scroll_hits += 1
        viewport = self.qpane.view().viewport
        self._subpixel_pan_offset = viewport.pan - self._buffer_pan
        self.qpane.update()
        self._mark_diagnostics_dirty()
        return True

    def get_last_paint_duration_ms(self) -> float:
        """Return the duration of the last paint call in milliseconds."""
        return self._last_paint_duration_ms

    def get_current_render_state(self) -> RenderState | None:
        """Return the most recent render state captured during painting."""
        return self._current_render_state

    def get_base_buffer(self) -> QImage | None:
        """Return the current base image buffer used for painting."""
        return self._base_image_buffer

    def get_subpixel_pan_offset(self) -> QPointF:
        """Return the subpixel offset applied when scrolling reused buffers."""
        return self._subpixel_pan_offset

    def snapshot_metrics(self) -> RendererMetrics:
        """Return current renderer reuse counters for diagnostics displays."""
        return RendererMetrics(
            base_buffer_allocations=self._base_buffer_allocations,
            scroll_attempts=self._scroll_attempts,
            scroll_hits=self._scroll_hits,
            scroll_misses=self._scroll_misses,
            full_redraws=self._full_redraws,
            partial_redraws=self._partial_redraws,
            last_paint_ms=self._last_paint_duration_ms,
        )

    def paint_stats(self) -> tuple[float, float, float]:
        """Return (last, average, max) paint durations in milliseconds."""
        average = (
            self._paint_duration_sum_ms / self._paint_duration_count
            if self._paint_duration_count > 0
            else 0.0
        )
        return (
            self._last_paint_duration_ms,
            average,
            self._paint_duration_max_ms,
        )

    def _buffer_rect_to_image_rect(
        self, buffer_rect_phys: QRectF, render_state: "RenderState"
    ) -> QRectF:
        """Map a physical buffer rectangle back into source-image coordinates using the inverse transform."""
        # The transform in the render state maps: Source Image -> Logical Buffer Coords.
        # We need the inverse to map from the buffer back to the source image.
        fwd_transform = render_state.transform
        inv_transform, is_invertible = fwd_transform.inverted()
        if not is_invertible:
            return QRectF()
        context = CoordinateContext(self.qpane)
        # Map PHYSICAL buffer coordinates -> LOGICAL qpane space before projecting
        # through the inverted transform into SOURCE image space.
        buffer_rect_log = context.physical_to_logical(buffer_rect_phys)
        # Map the entire logical buffer rect back to the source image space at once.
        # This is more numerically stable than mapping individual points.
        return inv_transform.mapRect(buffer_rect_log)

    def _repair_base_buffer_strips(self, repair_rects: list[QRect], state: RenderState):
        """Repairs the base image and any overlays in the given rectangular strips."""
        source_image_for_repair = state.source_image
        painter = QPainter(self._base_image_buffer)
        context = CoordinateContext(self.qpane)
        try:
            if state.render_hint_enabled:
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            for rect in repair_rects:
                dest_rect_phys = QRectF(rect)
                dest_rect_log = context.physical_to_logical(dest_rect_phys)
                source_rect = self._buffer_rect_to_image_rect(dest_rect_phys, state)
                if source_rect.isValid():
                    painter.drawImage(
                        dest_rect_log, source_image_for_repair, source_rect
                    )
            if state.strategy == RenderStrategy.TILE:
                base_clip_region_log = QRegion()
                for rect in repair_rects:
                    logical_rect = context.physical_to_logical(QRectF(rect)).toRect()
                    base_clip_region_log = base_clip_region_log.united(logical_rect)
                painter.setClipRegion(base_clip_region_log)
                painter.setTransform(state.transform)
                self._draw_tile_debug_overlay(painter, state)
        finally:
            painter.end()

    def _allocate_dpi_buffer(self, physical_size: QSize, dpr: float) -> QImage:
        """Create an ARGB buffer tagged with the given DPR for the physical viewport size."""
        buffer = QImage(physical_size, QImage.Format_ARGB32_Premultiplied)
        buffer.setDevicePixelRatio(dpr)
        return buffer

    def _redraw_base_image_buffer(self, dirty_region: QRegion, state: RenderState):
        """Repaint the base buffer for the dirty region, clearing outside-image areas before drawing."""
        if self._base_image_buffer is None:
            return
        qpane_rect = state.qpane_rect
        qpane_region = QRegion(qpane_rect)
        if dirty_region.intersected(qpane_region) == qpane_region:
            self._full_redraws += 1
        else:
            self._partial_redraws += 1
        self._current_render_state = state
        buffer_painter = QPainter(self._base_image_buffer)
        try:
            # Image bounds in buffer coords (no double-transform).
            img_src = QRectF(
                0, 0, state.source_image.width(), state.source_image.height()
            )
            img_log = state.transform.mapRect(img_src)
            # Use aligned bounds with a small expansion to avoid rounding gaps.
            img_region = QRegion(img_log.toAlignedRect().adjusted(-1, -1, 1, 1))
            # Split the incoming dirty region into outside/inside parts.
            outside_region = dirty_region.subtracted(img_region)
            inside_region = dirty_region.intersected(img_region)
            # Phase A: clear outside-of-image dirty area (no drawing there).
            if not outside_region.isEmpty():
                buffer_painter.setClipRegion(outside_region)
                buffer_painter.setCompositionMode(QPainter.CompositionMode_Source)
                for rect in outside_region:
                    buffer_painter.fillRect(rect, Qt.transparent)
                buffer_painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            # Phase B: clear inside-of-image dirty area, then draw.
            if not inside_region.isEmpty():
                buffer_painter.setClipRegion(inside_region)
                buffer_painter.setCompositionMode(QPainter.CompositionMode_Source)
                for rect in inside_region:
                    buffer_painter.fillRect(rect, Qt.transparent)
                buffer_painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
                if state.render_hint_enabled:
                    buffer_painter.setRenderHint(
                        QPainter.RenderHint.SmoothPixmapTransform, True
                    )
                buffer_painter.setTransform(state.transform)
                if state.strategy == RenderStrategy.DIRECT:
                    self._draw_direct_view(buffer_painter, state)
                elif state.strategy == RenderStrategy.TILE:
                    self._draw_tiled_view(buffer_painter, state)
        finally:
            buffer_painter.end()
            if state.qpane_rect in dirty_region:
                self._buffer_pan = QPointF(state.current_pan)
                self._subpixel_pan_offset = QPointF(0, 0)

    def _draw_tile_debug_overlay(self, painter: QPainter, state: RenderState):
        """Draw a debug grid over the visible tiles using the current transform."""
        if not state.debug_draw_tile_grid:
            return
        max_cols, max_rows = state.max_tile_cols, state.max_tile_rows
        if max_cols <= 0 or max_rows <= 0:
            return
        tile_size = state.tile_size
        stride = max(1, tile_size - state.tile_overlap)
        visible_range = state.visible_tile_range
        if visible_range is None:
            return
        start_row, end_row, start_col, end_col = visible_range
        if start_row > end_row or start_col > end_col:
            return
        effective_zoom = state.zoom / state.pyramid_scale
        pen = QPen(QColor(255, 0, 0, 100))
        pen.setWidthF(2.0 / effective_zoom)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        for r in range(start_row, end_row + 1):
            for c in range(start_col, end_col + 1):
                draw_pos = QPointF(c * stride, r * stride)
                debug_rect = QRectF(draw_pos, QSizeF(tile_size, tile_size))
                painter.drawRect(debug_rect)

    def _draw_tiled_view(self, painter: QPainter, state: RenderState):
        """Draw the tiled view clipped to the image bounds using the render state's transform."""
        img_rect_src = QRectF(
            0, 0, state.source_image.width(), state.source_image.height()
        )
        painter.save()
        # Slight padding guards against subpixel rounding at the edges.
        painter.setClipRect(img_rect_src.adjusted(-0.5, -0.5, 0.5, 0.5))
        painter.drawImage(0, 0, state.source_image)
        for tile_data in state.tiles_to_draw:
            painter.drawImage(tile_data.draw_pos, tile_data.image)
        if state.debug_draw_tile_grid:
            self._draw_tile_debug_overlay(painter, state)
        painter.restore()

    def _draw_direct_view(self, painter: QPainter, state: RenderState):
        """Draw the source image directly with no tiling helpers."""
        painter.drawImage(0, 0, state.source_image)

    def _mark_diagnostics_dirty(self) -> None:
        """Mark render diagnostics dirty on the QPane if available."""
        diagnostics = getattr(self.qpane, "diagnostics", None)
        if not callable(diagnostics):
            return
        try:
            manager = diagnostics()
        except Exception:  # pragma: no cover - defensive guard
            return
        if manager is not None:
            manager.set_dirty("render")
