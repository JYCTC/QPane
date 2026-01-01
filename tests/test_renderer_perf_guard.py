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

from PySide6.QtCore import QPointF, QRect, QRectF, QSize
from PySide6.QtGui import QImage, QTransform
from qpane import QPane
from qpane.rendering import RenderState, RenderStrategy


def _cleanup_qpane(qpane, qapp):
    qpane.deleteLater()
    qapp.processEvents()


def test_renderer_paint_duration_updates(qapp):
    qpane = QPane(features=())
    qpane.resize(32, 32)
    try:
        renderer = qpane.view().renderer
        renderer.allocate_buffers(QSize(16, 16), 1.0)
        calls = []

        def fake_redraw(region, state):
            calls.append((region, state))

        renderer._redraw_base_image_buffer = fake_redraw  # type: ignore[attr-defined]
        renderer.markDirty()
        source_image = QImage(16, 16, QImage.Format_ARGB32)
        source_image.fill(0)
        state = RenderState(
            source_image=source_image,
            pyramid_scale=1.0,
            transform=QTransform(),
            zoom=1.0,
            strategy=RenderStrategy.DIRECT,
            render_hint_enabled=False,
            debug_draw_tile_grid=False,
            tiles_to_draw=[],
            tile_size=64,
            tile_overlap=0,
            max_tile_cols=1,
            max_tile_rows=1,
            qpane_rect=QRect(0, 0, 16, 16),
            current_pan=QPointF(0.0, 0.0),
            physical_viewport_rect=QRectF(0.0, 0.0, 16.0, 16.0),
            visible_tile_range=None,
        )
        renderer.paint(state)
        first_duration = renderer.get_last_paint_duration_ms()
        assert first_duration >= 0.0
        renderer.paint(state)
        second_duration = renderer.get_last_paint_duration_ms()
        assert second_duration >= 0.0
        assert calls, "renderer should invoke buffer redraw when dirty"
    finally:
        _cleanup_qpane(qpane, qapp)
