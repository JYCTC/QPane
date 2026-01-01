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

"""Drag-and-drop utilities consumed by QPane and catalog controllers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QSizeF
from PySide6.QtGui import QImage, QMouseEvent

from ..rendering import ViewportZoomMode
from .dragout import maybeStartDrag

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ..qpane import QPane


def drag_out_image(qpane: "QPane", event: QMouseEvent | None) -> None:
    """Forward Qt drag requests to the drag-out helper while preserving the signature.

    Args:
        qpane: QPane whose current image should be offered to the OS drag target.
        event: Mouse event forwarded from Qt; accepted only to match the slot signature.
    """
    if not getattr(qpane.settings, "drag_out_enabled", True):
        return
    maybeStartDrag(qpane, event)


def is_drag_out_allowed(
    *,
    image: QImage,
    zoom: float,
    zoom_mode: ViewportZoomMode,
    viewport_size: QSizeF,
) -> bool:
    """Return True when drag-out gestures keep the image within the current viewport.

    Args:
        image: Latest rendered image that may be dragged out.
        zoom: Current zoom factor applied to `image`.
        zoom_mode: Active zoom policy controlling fit vs. manual zoom.
        viewport_size: Visible viewport dimensions expressed in device pixels.

    Returns:
        True when the scaled image fits inside the viewport or zoom-fit mode is active.
    """
    if image.isNull():
        return False
    scaled_size = image.size() * zoom
    return (
        scaled_size.width() <= viewport_size.width()
        and scaled_size.height() <= viewport_size.height()
    )
