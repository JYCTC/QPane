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

"""Rendering helpers for mask stroke previews and worker slices."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QBrush, QImage, QPainter, QPen

from ..catalog.image_utils import (
    numpy_to_qimage_grayscale8,
    qimage_to_numpy_grayscale8,
)
from .mask_controller import MaskStrokeSegmentPayload


def stroke_pen_width(brush_size: int, stride: int = 1) -> int:
    """Return the pen width for a brush diameter respecting preview stride."""
    stride_value = max(1, int(stride))
    brush_value = max(1, int(brush_size))
    width = int(round(float(brush_value) / stride_value))
    return max(1, width)


def stroke_radius(brush_size: int, stride: int = 1) -> float:
    """Return the ellipse radius for a brush diameter respecting preview stride."""
    stride_value = max(1, int(stride))
    brush_value = max(1, int(brush_size))
    radius = (float(brush_value) / 2.0) / stride_value
    return max(0.5, radius)


def render_stroke_segments(
    *,
    before: np.ndarray,
    dirty_rect: QRect,
    segments: tuple[MaskStrokeSegmentPayload, ...],
) -> tuple[np.ndarray, QImage]:
    """Replay `segments` against `before` and return the updated slice and preview."""
    working_array = np.array(before, copy=True)
    image = numpy_to_qimage_grayscale8(working_array)
    if segments:
        painter = QPainter(image)
        try:
            origin = dirty_rect.topLeft()
            for segment in segments:
                _paint_segment(painter, origin, segment)
        finally:
            painter.end()
    after_slice = qimage_to_numpy_grayscale8(image)
    return after_slice, image.copy()


def _paint_segment(
    painter: QPainter,
    origin: QPoint,
    segment: MaskStrokeSegmentPayload,
) -> None:
    """Paint `segment` relative to `origin` onto `painter`."""
    stride = 1
    draw_color = Qt.GlobalColor.black if segment.erase else Qt.GlobalColor.white
    pen = QPen()
    pen.setWidth(stroke_pen_width(segment.brush_size, stride=stride))
    pen.setColor(draw_color)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    start = QPoint(
        int(segment.start[0] - origin.x()),
        int(segment.start[1] - origin.y()),
    )
    end = QPoint(
        int(segment.end[0] - origin.x()),
        int(segment.end[1] - origin.y()),
    )
    painter.drawLine(start, end)
    if segment.start == segment.end:
        painter.setBrush(QBrush(draw_color))
        painter.setPen(Qt.PenStyle.NoPen)
        radius = stroke_radius(segment.brush_size, stride=stride)
        center = QPointF(float(start.x()), float(start.y()))
        painter.drawEllipse(center, radius, radius)
