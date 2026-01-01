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
import uuid

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QImage, Qt

from qpane import QPane


@pytest.fixture()
def qpane_with_image(qapp):
    qpane = QPane(features=())
    qpane.resize(128, 128)
    image = QImage(128, 128, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.black)
    image_id = uuid.uuid4()
    image_map = QPane.imageMapFromLists([image], [None], [image_id])
    qpane.setImagesByID(image_map, image_id)
    yield qpane
    qpane.deleteLater()
    qapp.processEvents()


def test_try_scroll_buffers_uses_qpane_render_state(qpane_with_image, monkeypatch):
    qpane = qpane_with_image
    view = qpane.view()
    renderer = view.renderer
    renderer._buffer_pan = QPointF(0.0, 0.0)
    renderer._subpixel_pan_offset = QPointF(0.0, 0.0)
    repair_calls = {}

    def fake_repair(rects, state):
        repair_calls["rects"] = rects
        repair_calls["state"] = state

    monkeypatch.setattr(renderer, "_repair_base_buffer_strips", fake_repair)
    captured = {}
    original_calculate = view.calculateRenderState

    def fake_calculate(use_pan=None, **kwargs):
        captured["use_pan"] = use_pan
        return original_calculate(use_pan=use_pan, **kwargs)

    monkeypatch.setattr(view, "calculateRenderState", fake_calculate)
    new_pan = QPointF(4.0, 3.0)
    view.viewport.pan = QPointF(new_pan)
    result = renderer.tryScrollBuffers(new_pan)
    assert result is True
    assert captured["use_pan"] == renderer._buffer_pan
    assert "rects" in repair_calls and repair_calls["rects"]
    assert repair_calls["state"] is not None


def test_try_scroll_buffers_tracks_fractional_offsets(qpane_with_image, monkeypatch):
    qpane = qpane_with_image
    view = qpane.view()
    renderer = view.renderer
    renderer._buffer_pan = QPointF(0.0, 0.0)
    monkeypatch.setattr(
        renderer, "_repair_base_buffer_strips", lambda rects, state: None
    )
    fractional_pan = QPointF(7.75, 2.5)
    view.viewport.pan = QPointF(fractional_pan)
    result = renderer.tryScrollBuffers(fractional_pan)
    assert result is True
    offset = renderer.get_subpixel_pan_offset()
    expected_offset = view.viewport.pan - renderer._buffer_pan
    assert math.isclose(offset.x(), expected_offset.x(), abs_tol=1e-6)
    assert math.isclose(offset.y(), expected_offset.y(), abs_tol=1e-6)


def test_try_scroll_buffers_rejects_large_scroll(qpane_with_image):
    qpane = qpane_with_image
    renderer = qpane.view().renderer
    renderer._buffer_pan = QPointF(0.0, 0.0)
    large_pan = QPointF(renderer._base_image_buffer.width() * 2, 0.0)
    result = renderer.tryScrollBuffers(large_pan)
    assert result is False
    assert renderer._buffer_pan == QPointF(0.0, 0.0)


def test_try_scroll_buffers_requires_buffer(qapp):
    qpane = QPane(features=())
    try:
        renderer = qpane.view().renderer
        renderer._base_image_buffer = None
        result = renderer.tryScrollBuffers(QPointF(1.0, 1.0))
    finally:
        qpane.deleteLater()
        qapp.processEvents()
    assert result is False
