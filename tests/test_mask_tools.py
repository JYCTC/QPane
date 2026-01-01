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

"""Tests for mask tool interactions and signals."""

import math
import uuid
import numpy as np
import pytest
from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from qpane.catalog.image_utils import qimage_to_numpy_view_grayscale8
from qpane.masks.strokes import _DecimatedStrokeState
from qpane.masks.stroke_render import render_stroke_segments
from qpane.masks.tools import BrushTool
from qpane.rendering.coordinates import PanelHitTest
from qpane.masks.mask_controller import MaskStrokeSegmentPayload
from qpane.tools import ToolDependencies


class _WheelEventStub:
    def __init__(self, pixel: QPoint | None = None, angle: QPoint | None = None):
        self._pixel = pixel or QPoint(0, 0)
        self._angle = angle or QPoint(0, 0)
        self.accepted = False

    def pixelDelta(self) -> QPoint:
        return self._pixel

    def angleDelta(self) -> QPoint:
        return self._angle

    def accept(self) -> None:
        self.accepted = True


class _PositionStub:
    def __init__(self, point: QPoint):
        self._point = point

    def toPoint(self) -> QPoint:
        return self._point


class _MouseEventStub:
    def __init__(
        self, point: QPoint, button: Qt.MouseButton = Qt.MouseButton.LeftButton
    ):
        self._point = point
        self._button = button
        self.accepted = False

    def button(self) -> Qt.MouseButton:
        return self._button

    def position(self):
        return _PositionStub(self._point)

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.accepted = False


class _MonotonicStub:
    def __init__(self):
        self._value = 0.0

    def advance(self, delta: float) -> None:
        self._value += delta

    def __call__(self) -> float:
        return self._value


def test_decimated_stroke_state_tracks_stride_and_dirty_rect():
    state = _DecimatedStrokeState(mask_id=uuid.uuid4(), stride=2)
    mask_view = np.zeros((8, 8), dtype=np.uint8)
    dirty_rect = QRect(QPoint(0, 0), QPoint(3, 3))
    start = QPoint(0, 0)
    end = QPoint(3, 3)
    preview = state.preview_segment(
        dirty_rect=dirty_rect,
        start_point=start,
        end_point=end,
        erase=False,
        brush_size=4,
        mask_view=mask_view,
    )
    assert state._dirty_rect == dirty_rect
    assert preview.text("qpane_preview_stride") == "2"
    assert preview.text("qpane_preview_provisional") == "1"
    preview_view, _ = qimage_to_numpy_view_grayscale8(preview)
    height = dirty_rect.bottom() - dirty_rect.top() + 1
    width = dirty_rect.right() - dirty_rect.left() + 1
    expected_height = math.ceil(height / state.stride)
    expected_width = math.ceil(width / state.stride)
    assert preview_view.shape == (expected_height, expected_width)
    assert np.any(preview_view > 0)
    second_rect = QRect(QPoint(2, 2), QPoint(4, 4))
    state.preview_segment(
        dirty_rect=second_rect,
        start_point=QPoint(2, 2),
        end_point=QPoint(4, 4),
        erase=True,
        brush_size=4,
        mask_view=mask_view,
    )
    assert state._dirty_rect == dirty_rect.united(second_rect)
    assert len(state._segments) == 2


@pytest.mark.parametrize("brush_size", [3, 5, 6, 11])
def test_preview_matches_worker_single_point(brush_size):
    state = _DecimatedStrokeState(mask_id=uuid.uuid4(), stride=1)
    mask_view = np.zeros((64, 64), dtype=np.uint8)
    start = QPoint(10, 12)
    stroke_rect = QRect(start, start).normalized()
    margin = int(brush_size / 2) + 2
    dirty_rect = stroke_rect.adjusted(-margin, -margin, margin, margin)
    preview = state.preview_segment(
        dirty_rect=dirty_rect,
        start_point=start,
        end_point=start,
        erase=False,
        brush_size=brush_size,
        mask_view=mask_view,
    )
    preview_view, _ = qimage_to_numpy_view_grayscale8(preview)
    y0, x0 = dirty_rect.top(), dirty_rect.left()
    y1, x1 = dirty_rect.bottom() + 1, dirty_rect.right() + 1
    before_slice = mask_view[y0:y1, x0:x1]
    segment = MaskStrokeSegmentPayload(
        start=(int(start.x()), int(start.y())),
        end=(int(start.x()), int(start.y())),
        brush_size=brush_size,
        erase=False,
    )
    after_slice, _ = render_stroke_segments(
        before=before_slice,
        dirty_rect=dirty_rect,
        segments=(segment,),
    )
    assert preview_view.shape == after_slice.shape
    np.testing.assert_array_equal(preview_view, after_slice)


def test_brush_tool_wheel_clamps_and_grows(qapp):
    tool = BrushTool()
    brush_size = 2
    emitted_sizes: list[int] = []

    def get_brush_size() -> int:
        return brush_size

    def on_size_changed(value: int) -> None:
        nonlocal brush_size
        brush_size = value
        emitted_sizes.append(value)

    tool.activate(
        ToolDependencies(get_brush_size=get_brush_size, get_brush_increment=lambda: 5)
    )
    tool.signals.brush_size_changed.connect(on_size_changed)
    negative_event = _WheelEventStub(pixel=QPoint(0, -1))
    tool.wheelEvent(negative_event)
    assert negative_event.accepted
    positive_event = _WheelEventStub(pixel=QPoint(0, 1))
    tool.wheelEvent(positive_event)
    assert positive_event.accepted
    assert emitted_sizes == [1, 6]


def test_brush_tool_straight_line_shift_mode(qapp):
    tool = BrushTool()
    shift_state = False

    def is_shift_held() -> bool:
        return shift_state

    tool.activate(
        ToolDependencies(
            is_shift_held=is_shift_held,
            panel_to_content_point=lambda point: point,
            image_to_panel_point=lambda point: point,
        )
    )
    strokes: list[tuple[QPoint, QPoint, bool]] = []
    undo_events: list[None] = []
    tool.signals.stroke_applied.connect(
        lambda start, end, erase: strokes.append((start, end, erase))
    )
    tool.signals.undo_state_push_requested.connect(lambda: undo_events.append(None))
    first_point = QPoint(10, 10)
    first_press = _MouseEventStub(first_point)
    tool.mousePressEvent(first_press)
    assert first_press.accepted
    release_event = _MouseEventStub(first_point)
    tool.mouseReleaseEvent(release_event)
    shift_state = True
    second_point = QPoint(20, 20)
    second_press = _MouseEventStub(second_point)
    tool.mousePressEvent(second_press)
    assert second_press.accepted
    assert strokes == [
        (first_point, first_point, False),
        (first_point, second_point, False),
    ]
    assert len(undo_events) == 2
    assert tool.last_paint_anchor_point is not None
    assert tool.last_paint_anchor_point.raw == second_point
    assert tool.is_drawing is False
    assert tool.current_preview_point is None


def test_brush_tool_continuous_stroke_emits_segments(qapp):
    tool = BrushTool()
    alt_state = True

    def is_alt_held() -> bool:
        return alt_state

    tool.activate(
        ToolDependencies(
            is_alt_held=is_alt_held,
            panel_to_content_point=lambda point: point,
        )
    )
    strokes: list[tuple[QPoint, QPoint, bool]] = []
    undo_events: list[None] = []
    completed_events: list[None] = []
    tool.signals.stroke_applied.connect(
        lambda start, end, erase: strokes.append((start, end, erase))
    )
    tool.signals.undo_state_push_requested.connect(lambda: undo_events.append(None))
    tool.signals.stroke_completed.connect(lambda: completed_events.append(None))
    start_point = QPoint(5, 5)
    press_event = _MouseEventStub(start_point)
    tool.mousePressEvent(press_event)
    assert press_event.accepted
    move_point = QPoint(8, 8)
    move_event = _MouseEventStub(move_point)
    tool.mouseMoveEvent(move_event)
    assert move_event.accepted
    release_event = _MouseEventStub(move_point)
    tool.mouseReleaseEvent(release_event)
    assert release_event.accepted
    assert strokes == [
        (start_point, start_point, True),
        (start_point, move_point, True),
    ]
    assert len(undo_events) == 1
    assert len(completed_events) == 1
    assert tool.last_paint_anchor_point is not None
    assert tool.last_paint_anchor_point.raw == move_point
    assert tool.is_drawing is False


def test_brush_tool_merges_back_to_back_taps(qapp):
    tool = BrushTool()
    clock = _MonotonicStub()
    tool.activate(
        ToolDependencies(
            panel_to_content_point=lambda point: point,
            monotonic_time=clock,
            stroke_merge_window_s=0.25,
            stroke_merge_distance_px=5,
        )
    )
    strokes: list[tuple[QPoint, QPoint, bool]] = []
    undo_events: list[None] = []
    completed_events: list[None] = []
    tool.signals.stroke_applied.connect(
        lambda start, end, erase: strokes.append((start, end, erase))
    )
    tool.signals.undo_state_push_requested.connect(lambda: undo_events.append(None))
    tool.signals.stroke_completed.connect(lambda: completed_events.append(None))
    point = QPoint(6, 6)
    first_press = _MouseEventStub(point)
    tool.mousePressEvent(first_press)
    assert first_press.accepted
    clock.advance(0.01)
    first_release = _MouseEventStub(point)
    tool.mouseReleaseEvent(first_release)
    assert first_release.accepted
    assert len(strokes) == 1
    assert len(undo_events) == 1
    assert len(completed_events) == 1
    clock.advance(0.05)
    second_press = _MouseEventStub(point)
    tool.mousePressEvent(second_press)
    assert second_press.accepted
    second_release = _MouseEventStub(point)
    tool.mouseReleaseEvent(second_release)
    assert not second_release.accepted
    assert len(strokes) == 1
    assert len(undo_events) == 1
    assert len(completed_events) == 1
    clock.advance(0.4)
    third_press = _MouseEventStub(point)
    tool.mousePressEvent(third_press)
    assert third_press.accepted
    third_release = _MouseEventStub(point)
    tool.mouseReleaseEvent(third_release)
    assert third_release.accepted
    assert len(strokes) == 2
    assert len(undo_events) == 2
    assert len(completed_events) == 2


def test_brush_tool_accepts_partial_edge_stroke(qapp):
    tool = BrushTool()
    strokes: list[tuple[QPoint, QPoint, bool]] = []
    tool.signals.stroke_applied.connect(
        lambda start, end, erase: strokes.append((start, end, erase))
    )

    def panel_hit(point: QPoint) -> PanelHitTest:
        return PanelHitTest(
            panel_point=point,
            raw_point=QPointF(-1.0, 5.0),
            clamped_point=QPoint(0, 5),
            inside_image=False,
        )

    tool.activate(
        ToolDependencies(
            panel_hit_test=panel_hit,
            is_point_in_widget=lambda _: True,
            get_image_rect=lambda: QRect(QPoint(0, 0), QPoint(9, 9)),
            get_brush_size=lambda: 6,
        )
    )
    press_event = _MouseEventStub(QPoint(0, 5))
    tool.mousePressEvent(press_event)
    assert press_event.accepted
    assert strokes == [(QPoint(-1, 5), QPoint(-1, 5), False)]


def test_brush_tool_ignores_events_outside_widget(qapp):
    tool = BrushTool()
    hit_calls = 0

    def panel_hit(point: QPoint) -> PanelHitTest:
        nonlocal hit_calls
        hit_calls += 1
        return PanelHitTest(
            panel_point=point,
            raw_point=QPointF(point.x(), point.y()),
            clamped_point=point,
            inside_image=True,
        )

    tool.activate(
        ToolDependencies(
            panel_hit_test=panel_hit,
            is_point_in_widget=lambda _: False,
            get_image_rect=lambda: QRect(QPoint(0, 0), QPoint(9, 9)),
        )
    )
    press_event = _MouseEventStub(QPoint(2, 2))
    tool.mousePressEvent(press_event)
    assert not press_event.accepted
    assert hit_calls == 0
