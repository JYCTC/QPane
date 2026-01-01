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

"""Brush tool implementation and ToolManager wiring helpers for mask workflows."""

from __future__ import annotations

import logging
from dataclasses import dataclass
import math
import time
from typing import Callable, TYPE_CHECKING

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QCursor, QMouseEvent, QPainter, QPen

from qpane.rendering.coordinates import PanelHitTest
from qpane.tools.base import BaseTool
from qpane.tools import ToolDependencies

if TYPE_CHECKING:
    from qpane.tools.tools import ToolManagerSignals
logger = logging.getLogger(__name__)

__all__ = ("BrushTool", "connect_brush_signals", "disconnect_brush_signals")


@dataclass(slots=True)
class _StrokePoint:
    """Container for raw/clamped image coordinates used by brush strokes."""

    raw: QPoint
    clamped: QPoint


class BrushTool(BaseTool):
    """Tool for drawing on a mask layer with a brush.

    Emits the following signals via ``self.signals``:
    - ``stroke_applied(start: QPoint, end: QPoint, erase: bool)`` whenever the brush paints a segment
    - ``undo_state_push_requested()`` when a new stroke begins
    - ``brush_size_changed(size: int)`` when the user scrolls the wheel to resize the brush
    """

    def __init__(self):
        """Initialize brush state and default signal helpers."""
        super().__init__()
        self._reset_state()

    @staticmethod
    def _default_preview_pens() -> tuple[QPen, QPen]:
        """Return the default preview pens used for brush outlines."""
        return QPen(Qt.black), QPen(Qt.white)

    def _reset_state(self) -> None:
        """Reset drawing flags and lambdas to safe defaults."""
        self.is_drawing = False
        self.last_draw_point: _StrokePoint | None = None
        self.last_paint_anchor_point: _StrokePoint | None = None
        self.current_preview_point: _StrokePoint | None = None
        self._pending_undo_push = False
        self._stroke_has_content = False
        self._last_stroke_point: _StrokePoint | None = None
        self._last_stroke_finished_at: float | None = None
        self._is_alt_held: Callable[[], bool] = lambda: False
        self._is_shift_held: Callable[[], bool] = lambda: False
        self._get_brush_size: Callable[[], int] = lambda: 20
        self._get_preview_pens: Callable[[], tuple[QPen, QPen]] = (
            self._default_preview_pens
        )
        self._panel_hit_test: Callable[[QPoint], PanelHitTest | None] | None = None
        self._panel_to_content_point: Callable[[QPoint], QPoint | None] = (
            lambda point: None
        )
        self._image_to_panel_point: Callable[[QPoint], QPoint | None] = (
            lambda point: None
        )
        self._is_point_in_widget: Callable[[QPoint], bool] = lambda point: True
        self._get_image_rect: Callable[[], QRect] = lambda: QRect()
        self._get_brush_increment: Callable[[], int] = lambda: 5
        self._monotonic: Callable[[], float] = time.monotonic
        self._merge_click_window_s = 0.25
        self._merge_distance_px = 3

    def activate(self, dependencies: ToolDependencies) -> None:
        """Capture QPane-supplied helpers for brush size, preview pens, and modifiers.

        Expected callables:
        - `is_alt_held` / `is_shift_held`: modifier guards used for erase/line modes.
        - `get_brush_size`: returns the current brush diameter.
        - `get_preview_pens`: returns outline/inline pens for stroke previews.
        - `panel_hit_test` / `is_point_in_widget` / `get_image_rect`: map panel
          positions to raw/clamped coordinates for edge-aware strokes.
        - `panel_to_content_point` / `image_to_panel_point`: coordinate transforms.
        - `get_brush_increment`: scroll delta used when resizing the brush.
        Each dependency is optional; defaults ensure the tool remains inert yet safe.
        """
        self._is_alt_held = dependencies.get("is_alt_held", lambda: False)
        self._is_shift_held = dependencies.get("is_shift_held", lambda: False)
        self._get_brush_size = dependencies.get("get_brush_size", lambda: 20)
        self._get_preview_pens = dependencies.get(
            "get_preview_pens", self._default_preview_pens
        )
        self._panel_hit_test = dependencies.get("panel_hit_test")
        self._panel_to_content_point = dependencies.get(
            "panel_to_content_point", lambda point: None
        )
        self._image_to_panel_point = dependencies.get(
            "image_to_panel_point", lambda point: None
        )
        self._is_point_in_widget = dependencies.get(
            "is_point_in_widget", lambda point: True
        )
        self._get_image_rect = dependencies.get("get_image_rect", lambda: QRect())
        self._get_brush_increment = dependencies.get("get_brush_increment", lambda: 5)
        self._monotonic = dependencies.get("monotonic_time", time.monotonic)
        self._merge_click_window_s = max(
            0.0,
            float(dependencies.get("stroke_merge_window_s", 0.25)),
        )
        self._merge_distance_px = max(
            0,
            int(dependencies.get("stroke_merge_distance_px", 3)),
        )

    def deactivate(self):
        """Drop QPane-provided dependencies and reset internal state."""
        self._reset_state()

    def getCursor(self) -> QCursor | None:
        """Defer cursor rendering to the QPane so it can supply the brush preview."""
        return None

    def mousePressEvent(self, event: QMouseEvent):
        """Begin a brush stroke or execute a straight-line stroke when shift is held."""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        panel_point = event.position().toPoint()
        stroke_point = self._resolve_stroke_point(panel_point)
        if stroke_point is None:
            return
        if self._should_collapse_click(stroke_point):
            event.accept()
            return
        self._pending_undo_push = True
        self._stroke_has_content = False
        erase_mode = self._is_alt_held()
        if self._is_shift_held() and self.last_paint_anchor_point is not None:
            self._pending_undo_push = True
            self._emit_stroke(
                self.last_paint_anchor_point,
                stroke_point,
                erase_mode,
                push_undo=True,
            )
            self.last_paint_anchor_point = stroke_point
            self.current_preview_point = None
            self.signals.repaint_overlay_requested.emit()
            self._record_stroke_completion(stroke_point)
            self._stroke_has_content = False
        else:
            self.is_drawing = True
            self._emit_stroke(
                stroke_point,
                stroke_point,
                erase_mode,
                push_undo=True,
            )
            self.last_draw_point = stroke_point
            self.last_paint_anchor_point = stroke_point
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        """Update the brush stroke preview or continue a freehand stroke."""
        panel_point = event.position().toPoint()
        stroke_point = self._resolve_stroke_point(panel_point)
        if stroke_point is None:
            return
        if self._is_shift_held() and self.last_paint_anchor_point is not None:
            self.current_preview_point = stroke_point
            self.signals.repaint_overlay_requested.emit()
            event.accept()
            return
        if self.is_drawing and self.last_draw_point is not None:
            erase_mode = self._is_alt_held()
            self._emit_stroke(
                self.last_draw_point,
                stroke_point,
                erase_mode,
                push_undo=False,
            )
            self.last_draw_point = stroke_point
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):
        """Finish an active brush stroke and persist the anchor for shift lines."""
        if event.button() == Qt.MouseButton.LeftButton and self.is_drawing:
            if self._stroke_has_content:
                self.signals.stroke_completed.emit()
                self.last_paint_anchor_point = self.last_draw_point
                self._record_stroke_completion(self.last_paint_anchor_point)
            self._stroke_has_content = False
            self.is_drawing = False
            self.last_draw_point = None
            self.current_preview_point = None
            self.signals.repaint_overlay_requested.emit()
            event.accept()

    def leaveEvent(self, event):
        """Clear any preview when the cursor leaves the drawing area."""
        self.current_preview_point = None
        self.signals.repaint_overlay_requested.emit()
        event.ignore()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Treat double clicks as handled so they do not spawn redundant strokes."""
        event.accept()

    def wheelEvent(self, event):
        """Adjust the brush size based on wheel or trackpad delta."""
        angle = event.angleDelta()
        pixel = event.pixelDelta()
        if pixel.y() != 0:
            delta = pixel.y()
        elif pixel.x() != 0:
            delta = pixel.x()
        elif angle.y() != 0:
            delta = angle.y()
        else:
            delta = angle.x()
        if delta == 0:
            event.accept()
            return
        increment = self._get_brush_increment()
        new_size = self._get_brush_size() + (increment if delta > 0 else -increment)
        self.signals.brush_size_changed.emit(max(1, new_size))
        event.accept()

    def get_preview_line_points(self):
        """Return the preview endpoints when the user is building a straight line."""
        if not self._is_shift_held():
            return None, None
        if self.last_paint_anchor_point is None or self.current_preview_point is None:
            return None, None
        return (
            self.last_paint_anchor_point.clamped,
            self.current_preview_point.clamped,
        )

    def draw_overlay(self, painter: QPainter):
        """Render the shift-line preview on top of the image."""
        start_point, end_point = self.get_preview_line_points()
        if not start_point or not end_point:
            return
        p1 = self._image_to_panel_point(start_point)
        p2 = self._image_to_panel_point(end_point)
        if p1 is None or p2 is None:
            return
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            outline_pen, inline_pen = self._get_preview_pens()
            painter.setPen(outline_pen)
            painter.drawLine(p1, p2)
            painter.setPen(inline_pen)
            painter.drawLine(p1, p2)
        finally:
            painter.restore()

    def _emit_stroke(
        self,
        start_point: _StrokePoint,
        end_point: _StrokePoint,
        erase_mode: bool,
        *,
        push_undo: bool,
    ) -> None:
        """Emit stroke signals and track whether undo push is needed."""
        if push_undo:
            self._ensure_undo_push()
        self.signals.stroke_applied.emit(start_point.raw, end_point.raw, erase_mode)
        self._stroke_has_content = True
        if push_undo and not self.is_drawing:
            self.signals.stroke_completed.emit()
            self._record_stroke_completion(end_point)

    def _resolve_stroke_point(self, panel_point: QPoint) -> _StrokePoint | None:
        """Map a panel coordinate to raw/clamped image points, if possible."""
        if not self._is_point_in_widget(panel_point):
            return None
        if self._panel_hit_test is not None:
            hit = self._panel_hit_test(panel_point)
            if hit is None:
                return None
            return self._stroke_point_from_hit(hit)
        return self._fallback_stroke_point(panel_point)

    def _stroke_point_from_hit(self, hit: PanelHitTest) -> _StrokePoint | None:
        """Convert a hit-test result into stroke points inside the image bounds."""
        image_rect = self._get_image_rect()
        if image_rect.isNull() or image_rect.isEmpty():
            return None
        raw_x = float(hit.raw_point.x())
        raw_y = float(hit.raw_point.y())
        radius = max(0.5, float(self._get_brush_size()) / 2.0)
        raw_point = QPoint(int(round(raw_x)), int(round(raw_y)))
        if hit.inside_image:
            return _StrokePoint(raw=raw_point, clamped=hit.clamped_point)
        clamp_x = min(max(raw_x, float(image_rect.left())), float(image_rect.right()))
        clamp_y = min(max(raw_y, float(image_rect.top())), float(image_rect.bottom()))
        if math.hypot(raw_x - clamp_x, raw_y - clamp_y) > radius:
            return None
        return _StrokePoint(raw=raw_point, clamped=hit.clamped_point)

    def _fallback_stroke_point(self, panel_point: QPoint) -> _StrokePoint | None:
        """Map panel coordinates to image space when no hit-test is available."""
        image_point = self._panel_to_content_point(panel_point)
        if image_point is None:
            return None
        qpoint = QPoint(image_point)
        return _StrokePoint(raw=QPoint(qpoint), clamped=qpoint)

    def _ensure_undo_push(self) -> None:
        """Push the undo stack lazily so no-op taps stay silent."""
        if not self._pending_undo_push:
            return
        self.signals.undo_state_push_requested.emit()
        self._pending_undo_push = False

    def _record_stroke_completion(self, point: _StrokePoint | None) -> None:
        """Record when the last stroke ended so rapid taps can be merged."""
        if point is None:
            return
        self._last_stroke_point = point
        self._last_stroke_finished_at = self._monotonic()

    def _should_collapse_click(self, stroke_point: _StrokePoint) -> bool:
        """Return True when a tap repeats the last stroke within the merge window."""
        if self._merge_click_window_s <= 0:
            return False
        if self._last_stroke_point is None or self._last_stroke_finished_at is None:
            return False
        elapsed = self._monotonic() - self._last_stroke_finished_at
        if elapsed > self._merge_click_window_s:
            return False
        previous = self._last_stroke_point.clamped
        distance = (previous - stroke_point.clamped).manhattanLength()
        return distance <= self._merge_distance_px


_BRUSH_SIGNAL_MAPPINGS: tuple[tuple[str, str], ...] = (
    ("stroke_applied", "stroke_applied"),
    ("brush_size_changed", "brush_size_changed"),
    ("stroke_completed", "stroke_completed"),
    ("undo_state_push_requested", "undo_state_push_requested"),
)


def _wire_mask_tool_signals(
    tool: BaseTool,
    manager_signals: "ToolManagerSignals",
    mapping: tuple[tuple[str, str], ...],
    *,
    tool_name: str,
) -> None:
    """Connect a mask-aware tool's signals to the ToolManager bus."""
    for tool_attr, manager_attr in mapping:
        signal = getattr(tool.signals, tool_attr, None)
        if signal is None:
            raise AttributeError(
                f"{tool_name} is missing required signal '{tool_attr}'. Update its signal contract before wiring."
            )
        target = getattr(manager_signals, manager_attr, None)
        if target is None:
            raise AttributeError(
                f"ToolManagerSignals no longer expose '{manager_attr}'. Update the signal mapping to match."
            )
        signal.connect(target)


def _unwire_mask_tool_signals(
    tool: BaseTool,
    manager_signals: "ToolManagerSignals",
    mapping: tuple[tuple[str, str], ...],
    *,
    tool_name: str,
) -> None:
    """Disconnect mask-aware tool signals with diagnostics."""
    for tool_attr, manager_attr in mapping:
        signal = getattr(tool.signals, tool_attr, None)
        target = getattr(manager_signals, manager_attr, None)
        if signal is None or target is None:
            logger.warning(
                "Skipping disconnect for '%s' -> '%s'; signal contract drift detected.",
                tool_attr,
                manager_attr,
            )
            continue
        try:
            signal.disconnect(target)
        except (TypeError, RuntimeError) as exc:
            logger.warning(
                "Failed to disconnect brush signal '%s': %s",
                tool_attr,
                exc,
            )


def connect_brush_signals(
    manager_signals: "ToolManagerSignals", tool: BaseTool
) -> None:
    """Bridge BrushTool emissions into ToolManagerSignals using the shared contract."""
    _wire_mask_tool_signals(
        tool,
        manager_signals,
        _BRUSH_SIGNAL_MAPPINGS,
        tool_name=type(tool).__name__,
    )


def disconnect_brush_signals(
    manager_signals: "ToolManagerSignals", tool: BaseTool
) -> None:
    """Tear down BrushTool wiring using the shared contract mapping."""
    _unwire_mask_tool_signals(
        tool,
        manager_signals,
        _BRUSH_SIGNAL_MAPPINGS,
        tool_name=type(tool).__name__,
    )
