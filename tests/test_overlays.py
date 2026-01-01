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

"""Tests for overlay resumption helpers."""

from __future__ import annotations

from types import SimpleNamespace

from qpane.ui.overlays import maybe_resume_overlays, resume_overlays


def test_maybe_resume_overlays_noop_when_flags_clear() -> None:
    """Resume helper should not toggle state when flags are inactive."""
    interaction = SimpleNamespace(
        overlays_suspended=False, overlays_resume_pending=False
    )
    qpane = SimpleNamespace(
        _masks_controller=SimpleNamespace(is_activation_pending=lambda _id: False)
    )
    maybe_resume_overlays(qpane, interaction)
    assert interaction.overlays_suspended is False
    assert interaction.overlays_resume_pending is False


def test_maybe_resume_overlays_resumes_on_workflow_error() -> None:
    """Workflow failures should force a defensive resume."""

    class _WorkflowStub:
        def is_activation_pending(self, _image_id):
            raise RuntimeError("boom")

    interaction = SimpleNamespace(overlays_suspended=True, overlays_resume_pending=True)
    qpane = SimpleNamespace(
        _masks_controller=_WorkflowStub(),
        catalog=lambda: SimpleNamespace(currentImageID=lambda: "image-1"),
    )
    maybe_resume_overlays(qpane, interaction)
    assert interaction.overlays_suspended is False
    assert interaction.overlays_resume_pending is False


def test_maybe_resume_overlays_keeps_suspended_when_pending() -> None:
    """Pending activations should keep overlays suspended."""
    interaction = SimpleNamespace(overlays_suspended=True, overlays_resume_pending=True)
    qpane = SimpleNamespace(
        _masks_controller=SimpleNamespace(is_activation_pending=lambda _id: True),
        catalog=lambda: SimpleNamespace(currentImageID=lambda: "image-1"),
    )
    maybe_resume_overlays(qpane, interaction)
    assert interaction.overlays_suspended is True
    assert interaction.overlays_resume_pending is True


def test_resume_overlays_clears_flags() -> None:
    """resume_overlays should clear suspension flags."""
    interaction = SimpleNamespace(overlays_suspended=True, overlays_resume_pending=True)
    resume_overlays(interaction)
    assert interaction.overlays_suspended is False
    assert interaction.overlays_resume_pending is False
