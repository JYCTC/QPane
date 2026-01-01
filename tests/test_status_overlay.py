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

"""Tests for status overlay rendering and staleness."""

from __future__ import annotations
import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QWidget
from qpane.core import DiagnosticsSnapshot
from qpane.types import DiagnosticRecord
from qpane.ui.status_overlay import (
    FALLBACK_MESSAGE,
    OVERLAY_MARGIN_PX,
    PADDING_HORIZONTAL_PX,
    PADDING_VERTICAL_PX,
    STROKE_OFFSETS,
    STALE_THRESHOLD_SEC,
    QPaneStatusOverlay,
)


class DummyDiagnostics(QObject):
    diagnosticsUpdated = Signal(object)

    def __init__(self, snapshot: DiagnosticsSnapshot) -> None:
        super().__init__()
        self._snapshot = snapshot
        self.fail = False
        self.started = False

    def cached_snapshot(self, *, force: bool = False):
        if self.fail:
            raise RuntimeError("boom")
        self.diagnosticsUpdated.emit(self._snapshot)
        return self._snapshot

    def update_snapshot(self, snapshot: DiagnosticsSnapshot) -> None:
        self._snapshot = snapshot


class DummyQPane(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot = DiagnosticsSnapshot(
            records=(
                DiagnosticRecord("Paint", "12.3 ms"),
                DiagnosticRecord("Zoom", "125.0%"),
                DiagnosticRecord("", "Standalone entry"),
            )
        )
        self.diag = DummyDiagnostics(self.snapshot)

    def diagnostics(self) -> DummyDiagnostics:
        return self.diag


@pytest.mark.usefixtures("qapp")
def test_overlay_formats_stacked_rows() -> None:
    qpane = DummyQPane()
    overlay = QPaneStatusOverlay(qpane=qpane)
    try:
        overlay.set_active(True)
        expected = """Paint  12.3 ms
Zoom   125.0%
Standalone entry"""
        assert overlay._last_rendered_text == expected
    finally:
        overlay.deleteLater()
        qpane.deleteLater()


@pytest.mark.usefixtures("qapp")
def test_overlay_respects_viewport_and_scrolls_hidpi() -> None:
    """Constrain overlay to the viewport and initialize scroll on HiDPI panes."""
    qpane = DummyQPane()
    qpane.resize(320, 240)
    # Build a tall snapshot to force overflow.
    records = tuple(DiagnosticRecord(f"Row{i}", f"value {i}") for i in range(30))
    snapshot = DiagnosticsSnapshot(records=records)
    qpane.diag.update_snapshot(snapshot)
    overlay = QPaneStatusOverlay(qpane=qpane)
    try:
        overlay.devicePixelRatioF = lambda: 2.0  # type: ignore[assignment]
        overlay.set_active(True)
        available_height = qpane.height() - (OVERLAY_MARGIN_PX * 2)
        assert overlay.height() <= available_height
        assert overlay._pixmap_display_size.height() >= overlay.height()
        assert overlay._scroll_offset == max(
            overlay._pixmap_display_size.height() - overlay.height(), 0
        )
        # Bottom-left anchoring.
        assert overlay.x() == OVERLAY_MARGIN_PX or overlay.x() == max(
            qpane.width() - overlay.width(), 0
        )
        assert (
            overlay.y() == qpane.height() - overlay.height() - OVERLAY_MARGIN_PX
            or overlay.y() == max(qpane.height() - overlay.height(), 0)
        )
    finally:
        overlay.deleteLater()
        qpane.deleteLater()


@pytest.mark.usefixtures("qapp")
def test_overlay_marks_rows_stale_on_timeout() -> None:
    qpane = DummyQPane()
    overlay = QPaneStatusOverlay(qpane=qpane)
    try:
        overlay.set_active(True)
        # Simulate time passing by forcing the stale threshold and checking.
        overlay._last_snapshot_monotonic -= STALE_THRESHOLD_SEC
        overlay._check_stale()
        expected = """Paint  12.3 ms (stale)
Zoom   125.0%
Standalone entry"""
        assert overlay._last_rendered_text == expected
    finally:
        overlay.deleteLater()
        qpane.deleteLater()


@pytest.mark.usefixtures("qapp")
def test_overlay_falls_back_on_initial_errors() -> None:
    qpane = DummyQPane()
    qpane.diag.fail = True
    overlay = QPaneStatusOverlay(qpane=qpane)
    try:
        overlay.set_active(True)
        assert overlay._last_rendered_text == FALLBACK_MESSAGE
    finally:
        overlay.deleteLater()
        qpane.deleteLater()


@pytest.mark.usefixtures("qapp")
def test_overlay_allocates_stroke_and_shadow_slack() -> None:
    """Allocate extra overlay space for stroke and shadow rendering."""
    qpane = DummyQPane()
    qpane.resize(320, 240)
    overlay = QPaneStatusOverlay(qpane=qpane)
    try:
        overlay.set_active(True)
        text = overlay._last_rendered_text
        width_limit = max(qpane.width() - (OVERLAY_MARGIN_PX * 2), 1)
        metrics = overlay.fontMetrics()
        bounding = metrics.boundingRect(
            0, 0, width_limit, 10_000, Qt.TextWordWrap, text
        )
        stroke_left = max(
            (-dx for dx, _ in STROKE_OFFSETS if dx < 0),
            default=0,
        )
        stroke_right = max(
            (dx for dx, _ in STROKE_OFFSETS if dx > 0),
            default=0,
        )
        stroke_up = max(
            (-dy for _, dy in STROKE_OFFSETS if dy < 0),
            default=0,
        )
        stroke_down = max(
            (dy for _, dy in STROKE_OFFSETS if dy > 0),
            default=0,
        )
        expected_width = (
            bounding.width() + stroke_left + stroke_right + PADDING_HORIZONTAL_PX * 2
        )
        expected_height = (
            bounding.height() + stroke_up + stroke_down + PADDING_VERTICAL_PX * 2
        )
        assert overlay._pixmap_display_size.width() >= expected_width
        assert overlay._pixmap_display_size.height() >= expected_height
    finally:
        overlay.deleteLater()
        qpane.deleteLater()
