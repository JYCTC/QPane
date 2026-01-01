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

"""Placeholder interaction and drag-out behaviour driven by config."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QImage
from qpane import Config, QPane


def _placeholder_path(tmp_path: Path) -> Path:
    image = QImage(8, 8, QImage.Format_ARGB32)
    image.fill(0)
    path = tmp_path / "placeholder.png"
    assert image.save(str(path))
    return path


def test_placeholder_panzoom_allows_navigation_and_drag(qapp, tmp_path: Path) -> None:
    """Pan/zoom placeholder policy should unlock viewport and honor drag-out."""
    path = _placeholder_path(tmp_path)
    config = Config(
        placeholder={
            "source": str(path),
            "panzoom_enabled": True,
            "drag_out_enabled": True,
        }
    )
    qpane = QPane(config=config, features=())
    try:
        catalog = qpane.catalog()
        assert catalog.placeholderActive()
        assert not qpane.view().viewport.is_locked()
        assert qpane.getControlMode() == qpane.CONTROL_MODE_PANZOOM
        policy = catalog.placeholderPolicy()
        assert policy is not None and policy.drag_out_enabled
        assert qpane.isDragOutAllowed()
    finally:
        qpane.deleteLater()
        qapp.processEvents()


def test_placeholder_all_tools_retains_control_mode(qapp, tmp_path: Path) -> None:
    """Placeholder keeps pan/zoom unlocked when configured and preserves current tool."""
    path = _placeholder_path(tmp_path)
    config = Config(
        placeholder={
            "source": str(path),
            "panzoom_enabled": True,
            "drag_out_enabled": True,
        }
    )
    qpane = QPane(config=config, features=())
    try:
        default_mode = qpane.getControlMode()
        assert qpane.catalog().placeholderActive()
        assert not qpane.view().viewport.is_locked()
        assert qpane.getControlMode() == default_mode
        assert qpane.currentImagePath == path
    finally:
        qpane.deleteLater()
        qapp.processEvents()
