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

"""Compose rendering, catalog, link, and swap collaborators behind QPane.view()."""

from __future__ import annotations


import uuid

from typing import TYPE_CHECKING


from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize

from ..cache.registry import CacheRegistry
from ..catalog import CatalogController, ImageCatalog, LinkManager
from ..core import (
    CacheSettings,
    Config,
    QPaneState,
    PrefetchSettings,
)

from ..swap import SwapDelegate

from ..swap.diagnostics import swap_progress_provider, swap_summary_provider

from .diagnostics import rendering_retry_provider

from .coordinates import PanelHitTest

from .presenter import RenderingPresenter

from .tiles import TileIdentifier


if TYPE_CHECKING:  # pragma: no cover - import guard for typing only

    from ..concurrency import TaskExecutorProtocol
    from ..qpane import QPane
    from ..rendering import RenderState, Renderer
    from ..core.diagnostics_broker import Diagnostics


class View:
    """Own the rendering surface collaborators while QPane serves as a thin facade."""

    def __init__(
        self,
        *,
        qpane: "QPane",
        state: QPaneState,
        catalog: ImageCatalog,
        executor: "TaskExecutorProtocol",
    ) -> None:
        """Wire rendering/swap/catalog collaborators owned by QPane.view()."""
        self._qpane = qpane
        self._state = state
        self._catalog = catalog
        self._executor = executor
        self._cache_registry: CacheRegistry | None = state.cache_registry
        self._attach_pyramid_manager()
        self.presenter = RenderingPresenter(
            qpane=qpane,
            catalog=catalog,
            cache_registry=self._cache_registry,
            executor=executor,
        )
        self.viewport = self.presenter.viewport
        self.tile_manager = self.presenter.tile_manager
        self.renderer = self.presenter.renderer
        self.link_manager = LinkManager()
        self.swap_delegate = SwapDelegate(
            qpane=qpane,
            catalog=catalog,
            viewport=self.viewport,
            tile_manager=self.tile_manager,
            rendering=self.presenter,
            prefetch_settings=self._prefetch_settings_from_config(qpane.settings),
            mark_dirty=self.mark_dirty,
        )
        self.catalog_controller = CatalogController(
            qpane=qpane,
            catalog=catalog,
            viewport=self.viewport,
            tile_manager=self.tile_manager,
            link_manager=self.link_manager,
            swap_delegate=self.swap_delegate,
        )
        self.swap_delegate.attach_catalog_controller(self.catalog_controller)
        self._connect_rendering_signals()

    def replace_renderer(self, renderer: "Renderer") -> None:
        """Replace the renderer while keeping presenter and view references in sync."""
        self.presenter.renderer = renderer
        self.renderer = renderer

    def calculateRenderState(
        self,
        *,
        use_pan: QPointF | None = None,
        is_blank: bool = False,
    ) -> "RenderState | None":
        """Delegate RenderState calculation to the presenter."""
        return self.presenter.calculateRenderState(use_pan=use_pan, is_blank=is_blank)

    def mark_dirty(self, dirty_rect: QRect | QRectF | None = None) -> None:
        """Forward dirty-region tracking to the presenter."""
        self.presenter.mark_dirty(dirty_rect)

    def ensure_view_alignment(self, *, force: bool = False) -> None:
        """Keep the viewport aligned via the presenter helper."""
        self.presenter.ensure_view_alignment(force=force)

    def allocate_buffers(self) -> None:
        """Ask the presenter to allocate renderer buffers."""
        self.presenter.allocate_buffers()

    def physical_viewport_rect(self) -> QRectF:
        """Return the current viewport rectangle in physical pixels."""
        return self.presenter.physical_viewport_rect()

    def panel_to_image_point(self, panel_pos: QPoint) -> QPoint | None:
        """Convert panel coordinates to image coordinates via the presenter."""
        return self.presenter.panel_to_image_point(panel_pos)

    def panel_hit_test(self, panel_pos: QPoint) -> PanelHitTest | None:
        """Expose viewport hit testing for panel coordinates."""
        return self.presenter.panel_hit_test(panel_pos)

    def image_to_panel_point(self, image_point: QPoint) -> QPointF | None:
        """Convert image coordinates to panel coordinates via the presenter."""
        return self.presenter.image_to_panel_point(image_point)

    def minimum_size_hint(self) -> QSize:
        """Expose the presenter minimum size hint."""
        return self.presenter.minimum_size_hint()

    def register_diagnostics(self, broker: "Diagnostics") -> None:
        """Install rendering and swap diagnostics providers via the diagnostics manager."""
        broker.register_swap_providers(swap_summary_provider, tier="core")
        broker.register_swap_providers(swap_progress_provider)
        broker.register_provider(
            rendering_retry_provider,
            domain="retry",
            tier="detail",
        )

    def handle_tile_ready(self, identifier: TileIdentifier) -> None:
        """Forward tile-ready signals to the swap delegate."""
        self.swap_delegate.handle_tile_ready(identifier)

    def handle_pyramid_ready(self, image_id: uuid.UUID | None) -> None:
        """Bridge pyramid-ready notifications from the catalog to swap plumbing."""
        self.swap_delegate.handle_pyramid_ready(image_id)

    def _attach_pyramid_manager(self) -> None:
        """Wire the catalog's pyramid manager into the shared cache registry."""
        registry = self._cache_registry
        if registry is None:
            return
        registry.attach_pyramid_manager(self._catalog.pyramid_manager)

    def _connect_rendering_signals(self) -> None:
        """Connect tile/pyramid events directly to the rendering stack."""
        self.tile_manager.tileReady.connect(self.handle_tile_ready)
        self._catalog.pyramidReady.connect(self.handle_pyramid_ready)

    def _prefetch_settings_from_config(self, config: Config) -> PrefetchSettings:
        """Return a PrefetchSettings clone from config.cache, enforcing the expected shape."""
        cache_settings = getattr(config, "cache", None)
        if not isinstance(cache_settings, CacheSettings):
            raise TypeError("config.cache must be a CacheSettings instance")
        prefetch = cache_settings.prefetch
        if not isinstance(prefetch, PrefetchSettings):
            raise TypeError("config.cache.prefetch must be a PrefetchSettings instance")
        return prefetch.clone()
