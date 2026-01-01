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

"""Hook interfaces for QPane feature installers.

Centralize the integration points exposed by the QPane facade so optional feature
installers share consistent, documented signatures.
"""

from __future__ import annotations


from dataclasses import dataclass

from typing import TYPE_CHECKING, Protocol


from PySide6.QtGui import QCursor, QPainter


from ..tools import ToolManagerSignals


if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ..tools.base import ExtensionTool
    from ..autosave import AutosaveManager
    from ..qpane import QPane
    from ..types import OverlayState
    from .diagnostics import DiagnosticsProvider


class ToolFactory(Protocol):
    """Factory callable used to create tool instances on demand."""

    def __call__(self) -> "ExtensionTool":
        """Instantiate and return a tool for the registered mode."""
        ...


class ToolSignalBinder(Protocol):
    """Callable that wires a tool's signals to the shared ToolManagerSignals object."""

    def __call__(self, signals: ToolManagerSignals, tool: "ExtensionTool") -> None:
        """Bind or unbind tool signals to the shared manager."""
        ...


class OverlayDrawFn(Protocol):
    """Callable that draws additional content overlays after the base image."""

    def __call__(self, painter: QPainter, state: "OverlayState") -> None:
        """Render overlay content after the base image is painted."""
        ...


class CursorProvider(Protocol):
    """Callable that returns a cursor for the current QPane state."""

    def __call__(self, qpane: "QPane") -> QCursor | None:
        """Return the cursor to display for the active control mode."""
        ...


@dataclass
class QPaneHooks:
    """Expose stable hook helpers for optional feature installers."""

    qpane: "QPane"

    def registerTool(
        self,
        mode: str,
        factory: ToolFactory,
        *,
        on_connect: ToolSignalBinder | None = None,
        on_disconnect: ToolSignalBinder | None = None,
    ) -> None:
        """Register a tool mode so installers can extend the interaction surface.

        Args:
            mode: Unique control-mode identifier.
            factory: Callable that creates a tool instance when the mode activates.
            on_connect: Optional callback that wires tool signals after creation.
            on_disconnect: Optional callback that unwires signals during teardown.
        """
        tools = self.qpane._tools_manager
        tools.registerTool(
            mode,
            factory,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
        )

    def unregisterTool(self, mode: str) -> None:
        """Remove a previously registered tool mode."""
        self.qpane._tools_manager.unregisterTool(mode)

    def registerOverlay(self, name: str, draw_fn: OverlayDrawFn) -> None:
        """Register a named overlay that renders after the base image.

        Args:
            name: Identifier used to manage the overlay lifecycle.
            draw_fn: Callable invoked after the base image finishes rendering.

        Raises:
            ValueError: If `name` is already registered.
        """
        self.qpane.interaction.registerOverlay(name, draw_fn)

    def unregisterOverlay(self, name: str) -> None:
        """Remove a previously registered overlay.

        Missing entries are ignored so callers can always unregister during teardown.
        """
        self.qpane.interaction.unregisterOverlay(name)

    def registerCursorProvider(self, mode: str, provider: CursorProvider) -> None:
        """Attach a provider that supplies cursors for the given control mode.

        Args:
            mode: Control-mode identifier associated with the cursor.
            provider: Callable that inspects the QPane and returns a ``QCursor`` or
                ``None``. When the target mode is active, the cursor updates
                immediately after registration.
        """
        self.qpane.interaction.registerCursorProvider(mode, provider)

    def unregisterCursorProvider(self, mode: str) -> None:
        """Detach a cursor provider previously associated with a control mode."""
        self.qpane.interaction.unregisterCursorProvider(mode)

    def register_diagnostics_provider(
        self,
        provider: "DiagnosticsProvider",
        *,
        domain: str = "custom",
        tier: str = "core",
    ) -> None:
        """Register an additional diagnostics provider for the QPane overlay.

        Args:
            provider: Callable that yields :class:`~qpane.types.DiagnosticRecord`
                entries for the owning QPane.
            domain: Logical diagnostics domain (cache, swap, mask, etc.).
            tier: ``"core"`` to keep the provider always-on, ``"detail"`` to allow
                callers to toggle it on demand.
        """
        diagnostics = self.qpane.diagnostics()
        diagnostics.register_provider(provider, domain=domain, tier=tier)

    def attachAutosaveManager(self, manager: "AutosaveManager") -> None:
        """Install the autosave manager used by mask-related features.

        Args:
            manager: Autosave manager that handles mask persistence. Replaces any
                existing manager; the mask autosave coordinator detaches it when
                autosave is disabled.
        """
        self.qpane._set_autosave_manager(manager)

    def detachAutosaveManager(self) -> None:
        """Remove the currently attached autosave manager, if any.

        Missing managers are ignored so callers can always invoke this during teardown.
        """
        if self.qpane.autosaveManager() is not None:
            self.qpane._set_autosave_manager(None)
