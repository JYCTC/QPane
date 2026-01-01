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

"""QPane widget facade coordinating rendering, catalog, mask, and tool APIs."""

import logging
import uuid
from math import isclose
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

import numpy as np
from PySide6.QtCore import (
    QEvent,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QImage,
    QScreen,
    QWheelEvent,
    QWindow,
)
from PySide6.QtWidgets import QWidget

from . import ui
from .cache import CacheCoordinator, cache_detail_provider
from .cache.registry import CacheRegistry
from .catalog import Catalog, CatalogMutationEvent, ImageCatalog, ImageMap, LinkManager
from .concurrency import TaskExecutorProtocol, ThreadPolicy
from .core import (
    Config,
    CursorProvider,
    DiagnosticsSnapshot,
    FeatureFailure,
    FeatureFallbacks,
    OverlayDrawFn,
    QPaneHooks,
    QPaneState,
    ToolFactory,
    ToolSignalBinder,
)
from .core.diagnostics_broker import Diagnostics
from .masks.workflow import MaskActivationSyncResult, MaskInfo, Masks
from .rendering import (
    RenderingPresenter,
    RenderState,
    View,
    ViewportZoomMode,
)
from .rendering.coordinates import PanelHitTest
from .types import CatalogEntry, CatalogSnapshot, DiagnosticsDomain, LinkedGroup
from .swap import SwapDelegate
from .tools import Tools
from .tools.base import ExtensionTool, ExtensionToolSignals
from .tools.delegate import ToolInteractionDelegate
from .ui import (
    CursorBuilder,
)
from .ui.diagnostics_controller import DiagnosticsOverlayController

if TYPE_CHECKING:
    from .autosave import AutosaveManager
    from .masks.mask import MaskLayer
    from .masks.mask_service import MaskService
    from .masks.mask_undo import MaskUndoState
    from .rendering import Renderer
    from .sam.manager import SamManager
logger = logging.getLogger(__name__)

__all__ = ["ExtensionTool", "ExtensionToolSignals", "QPane"]


class QPane(QWidget):
    """QWidget facade that routes rendering, catalog, mask, and tool orchestration."""

    # ========================================================================
    # Public API
    # ========================================================================
    CONTROL_MODE_PANZOOM = Tools.CONTROL_MODE_PANZOOM
    CONTROL_MODE_CURSOR = Tools.CONTROL_MODE_CURSOR
    CONTROL_MODE_DRAW_BRUSH = Tools.CONTROL_MODE_DRAW_BRUSH
    CONTROL_MODE_SMART_SELECT = Tools.CONTROL_MODE_SMART_SELECT
    imageLoaded: Signal = Signal(Path)
    """Emit the current image path after a swap applies; empty when unknown."""
    zoomChanged: Signal = Signal(float)
    """Emit the viewport zoom factor when view state changes."""
    viewportRectChanged: Signal = Signal(QRectF)
    """Emit the physical viewport rectangle whenever its size changes."""
    maskSaved: Signal = Signal(str, str)
    """Emit ``mask_id`` and file path after a mask autosave completes."""
    maskUndoStackChanged: Signal = Signal(uuid.UUID)
    """Emit the mask UUID when its undo stack mutates."""
    currentImageChanged: Signal = Signal(uuid.UUID)
    """Emit the active image UUID after navigation completes."""
    catalogChanged: Signal = Signal(CatalogMutationEvent)
    """Emit catalog mutation events describing the latest change."""
    catalogSelectionChanged: Signal = Signal(object)
    """Emit the active image UUID or ``None`` when selection changes."""
    linkGroupsChanged: Signal = Signal()
    """Emit when linked-group definitions change."""
    diagnosticsOverlayToggled: Signal = Signal(bool)
    """Emit overlay visibility state when the diagnostics HUD toggles."""
    diagnosticsDomainToggled: Signal = Signal(str, bool)
    """Emit diagnostics domain ID and enabled state after detail toggles."""
    samCheckpointStatusChanged: Signal = Signal(str, object)
    """Emit checkpoint status and path updates for SAM readiness tracking.
    The payload is ``(status, path)``, where ``path`` is a ``Path`` and status
    values include ``downloading``, ``ready``, ``failed``, and ``missing``.
    """
    samCheckpointProgress: Signal = Signal(int, object)
    """Emit checkpoint download progress updates for SAM readiness tracking.
    The payload is ``(downloaded, total)``, where ``total`` may be ``None`` if
    the size is unknown.
    """

    def __init__(
        self,
        *,
        config: Config | None = None,
        features: Iterable[str] | None = None,
        task_executor: TaskExecutorProtocol | None = None,
        thread_policy: ThreadPolicy | Mapping[str, Any] | None = None,
        config_strict: bool = False,
        **kwargs,
    ):
        """Build the QPane widget and wire core collaborators.

        Args:
            config: Initial configuration snapshot to apply.
            features: Optional feature names to install (mask, sam, etc.).
            task_executor: Existing executor instance to reuse.
            thread_policy: Policy or mapping forwarded to the executor builder.
            config_strict: When ``True``, reject overrides targeting inactive
                feature namespaces instead of logging warnings.
            **kwargs: Configuration overrides forwarded to ``QPaneState``.
        """
        super().__init__()
        self._state = QPaneState(
            qpane=self,
            initial_config=config,
            config_overrides=kwargs,
            features=features,
            task_executor=task_executor,
            thread_policy=thread_policy,
            config_strict=config_strict,
        )
        self._diagnostics_manager = self._state.diagnostics
        self.original_image = QImage()
        self.interaction = ToolInteractionDelegate(self)
        self._hooks = QPaneHooks(self)
        self._view: View | None = None
        self._catalog: Catalog | None = None
        self._masks: Masks | None = None
        self._tools: Tools | None = None
        self._is_blank = False
        self._diagnostics_overlay_controller: DiagnosticsOverlayController | None = None
        self._tracked_window: QWindow | None = None
        self._tracked_screen: QScreen | None = None
        self._tracked_screen_connections: set[str] = set()
        self._last_screen_dpr = float(self.devicePixelRatioF())
        self._last_link_groups: tuple[tuple[uuid.UUID, tuple[uuid.UUID, ...]], ...] = ()
        self._last_viewport_rect: QRectF | None = None
        self._initial_view_signals_scheduled = False
        self._init_core_components()
        view = self.view()
        catalog = self.catalog()
        self._masks = Masks(
            qpane=self,
            catalog=catalog,
            swap_delegate=view.swap_delegate,
            cache_registry=self._state.cache_registry,
        )
        self._masks.register_diagnostics(self._diagnostics_manager)
        self._state.install_features()
        self.applyCacheSettings()
        self.interaction.initialize_widget_properties()
        self.interaction.connect_signals()
        self._catalog.applyConfig(self.settings)
        self._apply_diagnostics_overlay_preferences()
        self._wire_facade_signals()
        self.destroyed.connect(self._state.on_destroyed)
        self._schedule_initial_view_signals()

    @staticmethod
    def imageMapFromLists(
        images: Iterable[QImage],
        paths: Iterable[Path | None] | None = None,
        ids: Iterable[uuid.UUID] | None = None,
    ) -> ImageMap:
        """Build an ImageMap of CatalogEntry values from aligned iterables via the shared helper."""
        return Catalog.imageMapFromLists(images, paths=paths, ids=ids)

    @property
    def settings(self) -> Config:
        """Expose the active configuration snapshot managed by QPaneState."""
        state = getattr(self, "_state", None)
        if state is None:
            raise AttributeError("QPane settings accessed before initialization")
        return state.settings

    @settings.setter
    def settings(self, new_settings: Config) -> None:
        """Prevent direct mutation; callers must use applySettings."""
        raise AttributeError(
            "QPane.settings is read-only; call QPane.applySettings to change configuration"
        )

    @property
    def installedFeatures(self) -> tuple[str, ...]:
        """Expose the set of features successfully installed on this QPane."""
        return self._state.installed_features

    def placeholderActive(self) -> bool:
        """Return True when the placeholder policy is active."""
        return self.catalog().placeholderActive()

    @property
    def currentImage(self) -> QImage:
        """Return the image currently displayed in this QPane."""
        catalog = self.catalog()
        return catalog.currentImage()

    @property
    def currentImagePath(self) -> Path | None:
        """Return the filesystem path for the current image, if any."""
        catalog = self.catalog()
        return catalog.currentImagePath()

    @property
    def allImages(self) -> list[QImage]:
        """Return a shallow copy of all original images currently held by this QPane."""
        catalog = self.catalog()
        return catalog.allImages()

    @property
    def allImagePaths(self) -> list[Path | None]:
        """Return a shallow copy of all file paths associated with images in this QPane."""
        catalog = self.catalog()
        return catalog.allImagePaths()

    def imagePath(self, image_id: uuid.UUID | None) -> Path | None:
        """Return the filesystem path for ``image_id`` when available."""
        catalog = self.catalog()
        return catalog.imagePath(image_id)

    def currentImageID(self) -> uuid.UUID | None:
        """Return the UUID of the currently selected image via the facade."""
        return self.catalog().currentImageID()

    def imageIDs(self) -> list[uuid.UUID]:
        """Return the ordered image IDs managed by the catalog via the facade."""
        return self.catalog().imageIDs()

    def hasImages(self) -> bool:
        """Return True when the catalog currently contains images."""
        return bool(self.catalog().imageIDs())

    def linkedGroups(self) -> tuple[LinkedGroup, ...]:
        """Return link groups paired with their stable identifiers via the facade."""
        return self.linkManager().getGroupRecords()

    def activeMaskID(self) -> uuid.UUID | None:
        """Return the active mask identifier when masking is available."""
        return self._masks_controller.getActiveMaskID()

    def maskIDsForImage(self, image_id: uuid.UUID | None = None) -> list[uuid.UUID]:
        """Return mask identifiers associated with ``image_id``."""
        return self._masks_controller.maskIDsForImage(image_id)

    def listMasksForImage(
        self, image_id: uuid.UUID | None = None
    ) -> tuple[MaskInfo, ...]:
        """Return mask metadata entries for ``image_id`` when masking is available."""
        return self._masks_controller.listMasksForImage(image_id)

    def getActiveMaskImage(self) -> QImage | None:
        """Return the QImage for the currently active mask layer."""
        return self._masks_controller.get_active_mask_image()

    def getMaskUndoState(self, mask_id: uuid.UUID) -> "MaskUndoState | None":
        """Expose the current undo/redo depth for ``mask_id`` when available."""
        return self._masks_controller.get_mask_undo_state(mask_id)

    def diagnosticsOverlayEnabled(self) -> bool:
        """Return True when the diagnostics overlay is currently visible."""
        return self.diagnosticsOverlayController().overlayEnabled()

    def diagnosticsDomains(self) -> tuple[str, ...]:
        """Return diagnostics domains that expose detail-tier providers."""
        return self.diagnosticsOverlayController().domains()

    def diagnosticsDomainEnabled(self, domain: str | DiagnosticsDomain) -> bool:
        """Return True when detail-tier diagnostics for ``domain`` are active.

        Raises:
            ValueError: When the requested diagnostics domain is unavailable.
        """
        canonical = self._normalize_diagnostics_domain(domain)
        return self.diagnosticsOverlayController().domainEnabled(canonical)

    def maskFeatureAvailable(self) -> bool:
        """Return True when mask tooling is currently available."""
        return self._masks_controller.mask_feature_available()

    def samFeatureAvailable(self) -> bool:
        """Return True when SAM tooling is currently available."""
        return self._masks_controller.sam_feature_available()

    def samCheckpointReady(self) -> bool:
        """Return True when the SAM checkpoint is available on disk."""
        manager = self._sam_manager
        if manager is None:
            return False
        return manager.checkpointReady()

    def samCheckpointPath(self) -> Path | None:
        """Return the resolved SAM checkpoint path when SAM is available."""
        manager = self._sam_manager
        return None if manager is None else manager.checkpointPath()

    def refreshSamFeature(self) -> tuple[bool, str]:
        """Reinstall SAM tooling using the current configuration snapshot.

        Returns:
            Tuple of (success, message) describing the refresh result.

        Side effects:
            Detaches the active SAM manager and reinstalls the SAM feature.
        """
        if "sam" not in self.installedFeatures:
            return False, "SAM tools disabled in this mode."
        try:
            from .features import FeatureInstallError
            from .masks.sam_feature import install_sam_feature

            self._masks_controller.detachSamManager()
            install_sam_feature(self)
        except FeatureInstallError as exc:
            hint = f" {exc.hint}" if exc.hint else ""
            return False, f"SAM refresh failed: {exc}.{hint}".strip()
        except Exception as exc:
            return False, f"SAM refresh failed: {exc}."
        return True, "SAM refreshed."

    def availableControlModes(self) -> tuple[str, ...]:
        """Return registered control mode identifiers in activation order."""
        return self._tools_manager.available_modes()

    def getControlMode(self) -> str:
        """Return the name of the currently active control mode."""
        return self._tools_manager.get_control_mode()

    def currentZoom(self) -> float:
        """Return the current viewport zoom factor without accessing view internals elsewhere."""
        return float(self.view().viewport.zoom)

    def currentViewportRect(self) -> QRectF:
        """Return the cached physical viewport rectangle reported via ``viewportRectChanged``."""
        rect = self._last_viewport_rect
        return QRectF(rect) if rect is not None else self.physicalViewportRect()

    def setZoomFit(self) -> None:
        """Fit the current content to the viewport and recenter pan."""
        self.view().viewport.setZoomFit()

    def setZoom1To1(self, anchor: QPoint | QPointF | None = None) -> None:
        """Snap zoom to native scale while keeping ``anchor`` steady when provided."""
        self.view().viewport.setZoom1To1(anchor=anchor)

    def applyZoom(
        self,
        requested_zoom: float,
        anchor: QPoint | QPointF | None = None,
    ):
        """Clamp zoom requests and remap unity to the device-native scale.

        Args:
            requested_zoom: Desired zoom multiple in image-space units. Values above 10 are capped,
                and a request of 1.0 is converted to ``viewport.nativeZoom()`` so HiDPI displays
                render one image pixel per physical device pixel.
            anchor: Optional widget-space point to keep stationary while zooming.

        Side effects:
            Logs a warning and returns when no image is loaded or the viewport is locked; otherwise
            forwards the bounded zoom to ``viewport.applyZoom()``.
        """
        new_zoom = self._normalize_zoom_request(requested_zoom)
        if new_zoom is None:
            return
        self.view().viewport.applyZoom(new_zoom, anchor=anchor)

    def panelHitTest(self, panel_pos: QPoint) -> PanelHitTest | None:
        """Return panel hit-test metadata matching ``panel_pos`` when content is available."""
        return self.view().panel_hit_test(panel_pos)

    def applySettings(self, *, config: Config | None = None, **overrides) -> None:
        """Replace the active configuration snapshot and reconfigure services.

        Args:
            config: Optional configuration snapshot to apply.
            overrides: Configuration overrides forwarded to ``QPaneState``.

        Side effects:
            Refreshes mask autosave wiring, marks the view dirty, and schedules a repaint.

        Raises:
            ValueError: When strict config mode is enabled and overrides target
                inactive feature namespaces.
        """
        self._state.apply_settings(config=config, **overrides)
        self.refreshMaskAutosavePolicy()
        self._apply_diagnostics_overlay_preferences()
        self._refresh_screen_tracking()
        self.markDirty()
        self.update()

    def setDiagnosticsOverlayEnabled(self, enabled: bool) -> None:
        """Show or hide the diagnostics overlay via its controller."""
        self.diagnosticsOverlayController().setOverlayEnabled(enabled)

    def setDiagnosticsDomainEnabled(
        self, domain: str | DiagnosticsDomain, enabled: bool
    ) -> None:
        """Enable or disable detail-tier diagnostics providers for ``domain``.

        Raises:
            ValueError: When the requested diagnostics domain is unavailable.
        """
        canonical = self._normalize_diagnostics_domain(domain)
        self.diagnosticsOverlayController().setDomainEnabled(canonical, enabled)

    def registerOverlay(
        self,
        name: str,
        draw_fn: OverlayDrawFn,
    ) -> None:
        """Register a content-space overlay to be painted after the base image.

        Raises:
            ValueError: If `name` is already present.
        """
        self.interaction.registerOverlay(name, draw_fn)

    def unregisterOverlay(self, name: str) -> None:
        """Remove a previously registered overlay.

        Missing entries are ignored so callers can always unregister during teardown.
        """
        self.interaction.unregisterOverlay(name)

    def contentOverlays(self) -> Mapping[str, OverlayDrawFn]:
        """Return the current overlay registry maintained by the interaction layer."""
        return self.interaction.content_overlays

    def overlaysSuspended(self) -> bool:
        """Return True when interaction-managed overlays are currently suppressed."""
        return self.interaction.overlays_suspended

    def overlaysResumePending(self) -> bool:
        """Indicate overlays should resume once pending activation work finishes."""
        return self.interaction.overlays_resume_pending

    def resumeOverlays(self) -> None:
        """Allow overlay drawing to resume on the next paint."""
        self.interaction.resume_overlays()

    def resumeOverlaysAndUpdate(self) -> None:
        """Resume overlays and trigger a repaint."""
        self.interaction.resume_overlays_and_update()

    def maybeResumeOverlays(self) -> None:
        """Resume overlays when activation has completed for the active image."""
        self.interaction.maybe_resume_overlays()

    def registerCursorProvider(self, mode: str, provider: CursorProvider) -> None:
        """Attach a cursor provider via the supported facade helper.

        If the mode is active when this is called, the cursor updates immediately.
        """
        self.interaction.registerCursorProvider(mode, provider)

    def unregisterCursorProvider(self, mode: str) -> None:
        """Detach a previously registered cursor provider."""
        self.interaction.unregisterCursorProvider(mode)

    def registerTool(
        self,
        mode: str,
        factory: ToolFactory,
        *,
        on_connect: ToolSignalBinder | None = None,
        on_disconnect: ToolSignalBinder | None = None,
    ) -> None:
        """Register a custom control mode through the supported facade API.

        Args:
            mode: Unique identifier for the tool mode.
            factory: Callable that creates a tool instance when the mode activates.
            on_connect: Optional binder for wiring tool-specific signals.
            on_disconnect: Optional binder invoked during teardown to unwire signals.
        """
        self.hooks.registerTool(
            mode,
            factory,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
        )

    def unregisterTool(self, mode: str) -> None:
        """Remove a previously registered tool mode via the supported facade."""
        self.hooks.unregisterTool(mode)

    def setImagesByID(
        self,
        image_map: ImageMap,
        current_id: uuid.UUID,
    ):
        """Replace the catalog contents and navigate to ``current_id`` via the facade."""
        catalog = self.catalog()
        catalog.setImagesByID(image_map, current_id)

    def clearImages(self):
        """Reset the catalog, linked views, and caches before showing the configured placeholder."""
        catalog = self.catalog()
        catalog.clearImages()

    def removeImageByID(self, image_id: uuid.UUID):
        """Remove ``image_id`` when present; callers remain responsible for navigation."""
        catalog = self.catalog()
        catalog.removeImageByID(image_id)

    def removeImagesByID(self, image_ids: list[uuid.UUID]):
        """Remove the provided image IDs when present without selecting a fallback."""
        catalog = self.catalog()
        catalog.removeImagesByID(image_ids)

    def setCurrentImageID(self, image_id: uuid.UUID | None):
        """Navigate to ``image_id`` while overlays are suspended for navigation.

        If ``image_id`` is None, the current image is deselected and the qpane
        reverts to its configured fallback state (placeholder or blank).
        """
        self.interaction.suspend_overlays_for_navigation()
        catalog = self.catalog()
        catalog.setCurrentImageID(image_id)
        if image_id is None:
            self._emit_catalog_selection_changed(None)

    def setAllImagesLinked(self, enabled: bool):
        """Toggle pan/zoom synchronization across all images."""
        image_ids = self.catalog().imageIDs()
        if enabled and len(image_ids) >= 2:
            members = tuple(image_ids)
            existing = self.linkedGroups()
            reuse_id = None
            for group in existing:
                if set(group.members) == set(members):
                    reuse_id = group.group_id
                    break
            group_id = reuse_id if reuse_id is not None else uuid.uuid4()
            self.setLinkedGroups((LinkedGroup(group_id=group_id, members=members),))
        else:
            self.setLinkedGroups(tuple())

    def setLinkedGroups(self, groups: Iterable[LinkedGroup]) -> None:
        """Define linked pan/zoom groups and emit link change signals.

        Args:
            groups: LinkedGroup definitions to persist.

        Side effects:
            Emits ``linkGroupsChanged`` when the group definition changes.
        """
        self.linkManager().setGroups(tuple(groups))
        self._maybe_emit_link_groups_changed()

    def getCatalogSnapshot(self) -> CatalogSnapshot:
        """Return a structured catalog snapshot for host consumption.

        Returns:
            CatalogSnapshot: Ordered catalog entries, linked groups, and active IDs.
        """
        image_ids = tuple(self.imageIDs())
        all_images = self.allImages
        all_paths = self.allImagePaths
        catalog_entries: dict[uuid.UUID, CatalogEntry] = {}
        for image_id, image, path in zip(image_ids, all_images, all_paths):
            catalog_entries[image_id] = CatalogEntry(image=image, path=path)
        return CatalogSnapshot(
            catalog=catalog_entries,
            linked_groups=tuple(self.linkedGroups()),
            order=image_ids,
            current_image_id=self.currentImageID(),
            active_mask_id=self.activeMaskID(),
            mask_capable=self.maskFeatureAvailable(),
        )

    def createBlankMask(self, size: QSize) -> "uuid.UUID | None":
        """Create an empty mask layer for the active image and return its ID.

        Args:
            size: Dimensions of the new mask in image pixels.

        Returns:
            The new mask UUID, or None when mask tooling is unavailable.

        Side effects:
            Emits ``catalogChanged`` with ``maskCreated`` when a mask is created.
        """
        mask_id = self._masks_controller.create_blank_mask(size)
        if mask_id is not None:
            self._emit_catalog_mutation("maskCreated", affected_ids=(mask_id,))
        return mask_id

    def loadMaskFromFile(self, path: str) -> "uuid.UUID | None":
        """Load a mask layer from disk and return its ID when available.

        Side effects:
            Emits ``catalogChanged`` with ``maskImported`` when a mask is loaded.
        """
        mask_id = self._masks_controller.load_mask_from_file(path)
        if mask_id is not None:
            self._emit_catalog_mutation("maskImported", affected_ids=(mask_id,))
        return mask_id

    def removeMaskFromImage(self, image_id: uuid.UUID, mask_id: uuid.UUID) -> bool:
        """Remove `mask_id` from `image_id` through the active mask service.

        Side effects:
            Emits ``catalogChanged`` with ``maskDeleted`` when removal succeeds.
            Emits ``catalogSelectionChanged`` for the active image when removal succeeds.
        """
        removed = self._masks_controller.remove_mask_from_image(image_id, mask_id)
        if removed:
            self._emit_catalog_mutation("maskDeleted", affected_ids=(mask_id,))
            self._emit_catalog_selection_changed(image_id)
        return removed

    def setActiveMaskID(self, mask_id):
        """Set the active mask for editing while letting the service manage ordering."""
        changed = self._masks_controller.set_active_mask_id(mask_id)
        if changed:
            current_id = None
            try:
                current_id = self.catalog().currentImageID()
            except Exception:
                current_id = None
            self._emit_catalog_selection_changed(current_id)
        return changed

    def setMaskProperties(
        self, mask_id, color: QColor | None = None, opacity: float | None = None
    ):
        """Update display properties for ``mask_id``.

        Args:
            mask_id: Identifier of the mask to update.
            color: New color when provided; leave unchanged when None.
            opacity: New opacity when provided; leave unchanged when None.
        """
        changed = self._masks_controller.set_mask_properties(
            mask_id, color=color, opacity=opacity
        )
        if changed:
            self._emit_catalog_mutation(
                "maskPropertiesChanged", affected_ids=(mask_id,)
            )
        return changed

    def prefetchMaskOverlays(
        self, image_id: uuid.UUID | None, *, reason: str = "navigation"
    ) -> bool:
        """Request asynchronous warming of mask overlays for `image_id` when masking is available."""
        return self._masks_controller.prefetch_mask_overlays(image_id, reason=reason)

    def cycleMasksForward(self):
        """Cycle the mask layer stack forward, moving the bottom layer to the top."""
        return self._masks_controller.cycle_masks_forward()

    def cycleMasksBackward(self):
        """Cycle the mask layer stack backward, moving the top layer to the bottom."""
        return self._masks_controller.cycle_masks_backward()

    def undoMaskEdit(self) -> bool:
        """Undo the last mask edit through the mask workflow."""
        return self._masks_controller.undo_mask_edit()

    def redoMaskEdit(self) -> bool:
        """Redo the last reverted mask edit through the mask workflow."""
        return self._masks_controller.redo_mask_edit()

    def setControlMode(
        self,
        mode: str,
    ):
        """Delegate control-mode changes to the interaction layer."""
        if self.catalog().placeholderActive():
            mask_modes = {
                Tools.CONTROL_MODE_DRAW_BRUSH,
                Tools.CONTROL_MODE_SMART_SELECT,
            }
            if mode in mask_modes:
                logger.info(
                    "Ignoring mask control mode while placeholder is active: %s", mode
                )
                return
        self.interaction.set_control_mode(mode)

    # ========================================================================
    # Internal Implementation
    # ========================================================================

    def catalog(self) -> Catalog:
        """Expose the catalog facade managing catalog state and navigation hooks."""
        if self._catalog is None:
            raise AttributeError("Catalog accessed before initialization")
        return self._catalog

    def view(self) -> View:
        """Expose the view collaborator that owns viewport, tile, and swap services."""
        if self._view is None:
            raise AttributeError("View accessed before initialization")
        return self._view

    def presenter(self) -> RenderingPresenter:
        """Expose the RenderingPresenter managed by the rendering stack."""
        return self.view().presenter

    def linkManager(self) -> LinkManager:
        """Expose the link manager coordinating linked-view groups."""
        return self.view().link_manager

    def diagnostics(self) -> Diagnostics:
        """Expose the diagnostics coordinator for this QPane."""
        return self._diagnostics_manager

    def diagnosticsOverlayController(self) -> DiagnosticsOverlayController:
        """Return the diagnostics overlay controller owned by this QPane."""
        controller = self._diagnostics_overlay_controller
        if controller is None:
            controller = DiagnosticsOverlayController(self)
            self._diagnostics_overlay_controller = controller
        return controller

    @property
    def executor(self) -> TaskExecutorProtocol:
        """Return the task executor shared across QPane subsystems."""
        return self._state.executor

    @property
    def cacheCoordinator(self) -> CacheCoordinator | None:
        """Return the cache coordinator when coordination is enabled."""
        return self._state.cache_coordinator

    @property
    def swapDelegate(self) -> SwapDelegate:
        """Expose the swap delegate orchestrating catalog navigation."""
        return self.view().swap_delegate

    @property
    def _masks_controller(self) -> Masks:
        """Return the masks workflow controller."""
        if self._masks is None:
            raise AttributeError("Masks accessed before initialization")
        return self._masks

    @property
    def _tools_manager(self) -> Tools:
        """Return the tools manager orchestrating input modes."""
        if self._tools is None:
            raise AttributeError("Tools accessed before initialization")
        return self._tools

    @property
    def hooks(self) -> QPaneHooks:
        """Expose internal hook helpers reserved for QPane feature installers.

        Hosts must use the QPane.register* facade methods instead of calling this property directly.
        """
        return self._hooks

    def _init_core_components(self):
        """Initialize core viewer components that do not require optional features."""
        self._state.cache_coordinator = self._state.build_cache_coordinator()
        self._state.cache_registry = CacheRegistry(self._state.cache_coordinator)
        self._image_catalog = ImageCatalog(
            config=self.settings,
            executor=self.executor,
            parent=self,
        )
        view = View(
            qpane=self,
            state=self._state,
            catalog=self._image_catalog,
            executor=self.executor,
        )
        self._view = view
        self._catalog = Catalog(
            catalog=self._image_catalog,
            controller=view.catalog_controller,
            link_manager=view.link_manager,
            swap_delegate=view.swap_delegate,
            qpane=self,
        )
        self._tools = Tools(parent=self)
        self.cursor_builder = CursorBuilder()
        # Placeholders for optional subsystems; installed by feature hooks.
        self.mask_service = None
        self.mask_controller = None
        self._sam_manager = None
        self._autosave_manager = None
        view.register_diagnostics(self._diagnostics_manager)
        self._diagnostics_manager.register_cache_providers(
            cache_detail_provider,
            tier="detail",
        )

    def _wire_facade_signals(self) -> None:
        """Connect facade-level signals for catalog, link, and diagnostics events."""
        catalog = self.catalog()
        catalog.setMutationListener(self._handle_catalog_mutation)
        self.currentImageChanged.connect(self._handle_current_image_changed_signal)
        controller = self.diagnosticsOverlayController()
        controller.setOverlayChangedCallback(self._handle_diagnostics_overlay_toggled)
        controller.setDetailChangedCallback(self._handle_diagnostics_detail_toggled)
        self._last_link_groups = self._normalized_link_groups()
        self._emit_catalog_selection_changed(self.catalog().currentImageID())

    def _schedule_initial_view_signals(self) -> None:
        """Ensure the first zoom/viewport signals emit once Qt shows the widget."""
        if self._initial_view_signals_scheduled:
            return
        self._initial_view_signals_scheduled = True
        QTimer.singleShot(0, self._emit_initial_view_signals)

    def _emit_initial_view_signals(self) -> None:
        """Emit initial zoom and viewport snapshots after the widget initializes."""
        self._initial_view_signals_scheduled = False
        self._emit_zoom_snapshot()
        self._emit_viewport_rect_if_changed(force=True)

    def featureFallbacks(self) -> FeatureFallbacks:
        """Expose the fallback tracker used to log optional feature availability."""
        return self._state.fallbacks

    def failedFeatures(self) -> Mapping[str, FeatureFailure]:
        """Return recorded feature installation failures keyed by feature name."""
        return self._state.failed_features

    def gatherDiagnostics(self) -> DiagnosticsSnapshot:
        """Collect a diagnostic snapshot for this QPane instance."""
        return self.diagnostics().gather()

    def createStatusOverlay(self, *, parent: QWidget | None = None):
        """Create a status overlay widget bound to this QPane."""
        return ui.create_status_overlay(self, parent=parent)

    def applyCacheSettings(self) -> None:
        """Propagate cache configuration to view-managed controllers."""
        self._state.apply_cache_settings()

    def _apply_diagnostics_overlay_preferences(self) -> None:
        """Synchronize overlay visibility and detail toggles with settings.

        Raises:
            ValueError: When configured diagnostics domains are not available.
        """
        controller = self.diagnosticsOverlayController()
        settings = self.settings
        enabled_domains = tuple(
            getattr(settings, "diagnostics_domains_enabled", ()) or ()
        )
        available_domains = set(controller.domains())
        unknown = tuple(
            domain for domain in enabled_domains if domain not in available_domains
        )
        if unknown:
            raise ValueError(
                f"Diagnostics domains not available for this qpane: {', '.join(unknown)}"
            )
        for domain in available_domains:
            controller.setDomainEnabled(domain, domain in enabled_domains)
        overlay_enabled = bool(getattr(settings, "diagnostics_overlay_enabled", False))
        controller.setOverlayEnabled(overlay_enabled)

    def _normalize_diagnostics_domain(self, domain: str | DiagnosticsDomain) -> str:
        """Return a canonical diagnostics domain or raise when unavailable."""
        controller = self.diagnosticsOverlayController()
        available = set(controller.domains())
        candidate = (
            domain.value if isinstance(domain, DiagnosticsDomain) else str(domain)
        )
        canonical = candidate.strip().lower()
        if canonical not in available:
            raise ValueError(
                f"Diagnostics domain '{candidate}' is not available for this qpane"
            )
        return canonical

    def attachAutosaveManager(self, manager: "AutosaveManager") -> None:
        """Install the autosave manager used by optional features.

        Replaces any existing manager; masking hooks detach it automatically when autosave is disabled.
        """
        self.hooks.attachAutosaveManager(manager)

    def detachAutosaveManager(self) -> None:
        """Remove the currently attached autosave manager, if any.

        Missing managers are ignored so callers can always invoke this during teardown.
        """
        self.hooks.detachAutosaveManager()

    def autosaveManager(self) -> "AutosaveManager | None":
        """Return the currently attached autosave manager, if any."""
        return self._autosave_manager

    def _set_autosave_manager(self, manager: "AutosaveManager | None") -> None:
        """Internal helper used by hooks to manage autosave state."""
        self._autosave_manager = manager

    def attachMaskService(self, service: "MaskService") -> None:
        """Attach the mask service facade and refresh autosave hooks.

        Side effects:
            Emits ``catalogChanged`` with ``maskServiceAttached``.
        """
        self._masks_controller.attachMaskService(service)
        self._emit_catalog_mutation("maskServiceAttached", affected_ids=())

    def detachMaskService(self) -> None:
        """Detach the mask service and tear down autosave wiring.

        Side effects:
            Emits ``catalogChanged`` with ``maskServiceDetached``.
        """
        self._masks_controller.detachMaskService()
        self._emit_catalog_mutation("maskServiceDetached", affected_ids=())

    def attachSamManager(self, sam_manager: "SamManager") -> None:
        """Attach a SamManager instance and wire its signals."""
        self._masks_controller.attachSamManager(sam_manager)

    def detachSamManager(self) -> None:
        """Detach the SAM manager and cancel outstanding predictor work."""
        self._masks_controller.detachSamManager()

    def samManager(self) -> "SamManager | None":
        """Return the active SAM manager when installed."""
        return self._sam_manager

    def _set_sam_manager(self, manager: "SamManager | None") -> None:
        """Internal helper for workflow/hooks to track SAM managers."""
        self._sam_manager = manager

    def addImage(self, image_id: uuid.UUID, image: QImage, path: Path | None):
        """Add or replace a single catalog entry without changing the selection."""
        catalog = self.catalog()
        catalog.addImage(image_id, image, path)

    def _display_current_catalog_image(self, *, fit_view: bool = True) -> None:
        """Render the catalog's current image if present; otherwise blank the qpane."""
        catalog = self.catalog()
        catalog.displayCurrentCatalogImage(fit_view=fit_view)

    @property
    def imageCount(self) -> int:
        """Return the total number of images managed by this QPane."""
        catalog = self.catalog()
        return catalog.imageCount()

    def linkedViewGroupID(self, image_id: uuid.UUID) -> uuid.UUID | None:
        """Return the linked-view group identifier that contains ``image_id`` when linked."""
        return self.catalog().linkedViewGroupID(image_id)

    def updateMaskFromFile(self, mask_id: "uuid.UUID", file_path: str) -> bool:
        """Replace a mask layer's pixels from ``file_path`` while preserving metadata.

        Args:
            mask_id: Identifier of the mask layer to update.
            file_path: Filesystem path to the replacement mask image.

        Returns:
            True when the layer was updated successfully.
        """
        return self._masks_controller.update_mask_from_file(mask_id, file_path)

    def setBrushSize(self, size: int):
        """Set the brush diameter in pixels."""
        self._masks_controller.set_brush_size(size)

    def invalidateActiveMaskCache(self):
        """Invalidate the colorized pixmap cache for the currently active mask.

        External tools that mutate mask images directly should call this to keep previews in sync.
        """
        return self._masks_controller.invalidate_active_mask_cache()

    def updateMaskRegion(
        self,
        dirty_image_rect: QRect,
        active_mask_layer: "MaskLayer",
        *,
        sub_mask_image: QImage | None = None,
        force_async_colorize: bool = False,
    ) -> bool:
        """Forward mask-region updates to refresh cached overlays.

        Args:
            dirty_image_rect: Image-space rectangle that was modified.
            active_mask_layer: Layer owning the updated pixels.
            sub_mask_image: Optional pre-updated snippet to reuse instead of copying from the layer.
            force_async_colorize: Queue high-resolution colorization even when previews are decimated.

        Returns:
            True when the region update is dispatched successfully.
        """
        return self._masks_controller.update_mask_region(
            dirty_image_rect,
            active_mask_layer,
            sub_mask_image=sub_mask_image,
            force_async_colorize=force_async_colorize,
        )

    def generateAndApplyMask(self, bbox: np.ndarray, erase_mode: bool = False):
        """Generate a mask from ``bbox`` and apply it through the mask workflow."""
        return self._masks_controller.generate_and_apply_mask(
            bbox, erase_mode=erase_mode
        )

    def _sync_mask_activation_for_image(
        self, image_id: uuid.UUID | None
    ) -> MaskActivationSyncResult:
        """Synchronize mask activation for `image_id` and surface workflow status."""
        return self._masks_controller.sync_mask_activation_for_image(image_id)

    def isMaskActivationPending(self, image_id: uuid.UUID | None = None) -> bool:
        """Return True while deferred mask activation remains outstanding."""
        return self._masks_controller.is_activation_pending(image_id)

    def refreshMaskAutosavePolicy(self) -> None:
        """Re-evaluate mask autosave wiring after feature state changes."""
        self._masks_controller.refreshMaskAutosavePolicy()

    def resetActiveSamPredictor(self) -> None:
        """Clear any cached predictor so SAM requests start fresh."""
        self._masks_controller.resetActiveSamPredictor()

    def refreshCursor(self) -> None:
        """Refresh the QWidget cursor via the interaction delegate."""
        self.interaction.update_cursor()

    def updateBrushCursor(self, erase_indicator: bool = False) -> None:
        """Delegate brush cursor updates to the mask bridge via the interaction layer."""
        self.interaction.update_brush_cursor(erase_indicator=erase_indicator)

    def updateModifierKeyCursor(self) -> None:
        """Update modifier-sensitive cursors via the interaction delegate."""
        self.interaction.update_modifier_key_cursor()

    def setPanZoomLocked(self, locked: bool):
        """Delegate pan/zoom lock state to the viewport."""
        self.view().viewport.set_locked(bool(locked))

    def blank(self):
        """Blank the qpane without clearing caches."""
        self.interaction.blank()

    def getPan(self) -> QPointF:
        """Return the current pan offset."""
        return self.view().viewport.pan

    def setPan(self, pan: QPointF):
        """Delegate pan updates to the viewport."""
        self.view().viewport.setPan(pan)

    def getZoomMode(self) -> ViewportZoomMode:
        """Expose the active zoom mode reported by the viewport."""
        return self.view().viewport.get_zoom_mode()

    def markDirty(self, dirty_rect: QRect | QRectF | None = None):
        """Mark a region of the qpane as dirty by delegating to the renderer.

        Passing ``None`` marks the entire qpane dirty.
        """
        self.view().mark_dirty(dirty_rect)

    def _save_zoom_pan_for_current_image(self):
        """Persist the current viewport transform through the swap delegate."""
        self.view().swap_delegate.save_zoom_pan_for_current_image()

    def _restore_zoom_pan_for_new_image(self, image_id):
        """Restore the saved viewport transform for ``image_id`` when present."""
        self.view().swap_delegate.restore_zoom_pan_for_new_image(image_id)

    def _apply_zoom_interpolated(
        self,
        requested_zoom: float,
        anchor: QPoint | QPointF | None = None,
    ) -> None:
        """Apply a clamped zoom request using the viewport interpolation path."""
        new_zoom = self._normalize_zoom_request(requested_zoom)
        if new_zoom is None:
            return
        self.view().viewport.applyZoomInterpolated(new_zoom, anchor=anchor)

    def _apply_zoom_interpolated_with_mode(
        self,
        requested_zoom: float,
        anchor: QPoint | QPointF | None,
        target_mode: ViewportZoomMode,
    ) -> None:
        """Apply an interpolated zoom request while setting the target mode."""
        if target_mode == ViewportZoomMode.FIT:
            if not self._can_apply_zoom():
                return
            new_zoom = requested_zoom
            if new_zoom <= 0:
                return
        else:
            reinterpret_one = target_mode != ViewportZoomMode.FIT
            new_zoom = self._normalize_zoom_request(
                requested_zoom, reinterpret_one_as_native=reinterpret_one
            )
            if new_zoom is None:
                return
        target_pan = None
        fit_zoom = None
        if target_mode == ViewportZoomMode.FIT:
            target_pan = QPointF(0, 0)
            fit_zoom = new_zoom
        elif target_mode == ViewportZoomMode.ONE_TO_ONE:
            target_pan = None if anchor is not None else QPointF(0, 0)
        self.view().viewport.applyZoomInterpolatedWithMode(
            new_zoom,
            anchor=anchor,
            target_mode=target_mode,
            target_pan=target_pan,
            fit_zoom=fit_zoom,
        )

    def _apply_zoom_fit_interpolated(self) -> None:
        """Fit the viewport using an interpolated transition."""
        if not self._can_apply_zoom():
            return
        self.view().viewport.setZoomFitInterpolated()

    def _apply_zoom_one_to_one_interpolated(
        self, anchor: QPoint | QPointF | None = None
    ) -> None:
        """Snap to 1:1 zoom using an interpolated transition."""
        if not self._can_apply_zoom():
            return
        self.view().viewport.setZoom1To1Interpolated(anchor=anchor)

    def saveCurrentViewState(self) -> None:
        """Persist the current pan/zoom state for the active image."""
        self._save_zoom_pan_for_current_image()

    def restoreViewStateForImage(self, image_id: uuid.UUID) -> None:
        """Reapply a saved pan/zoom state for ``image_id`` when available."""
        self._restore_zoom_pan_for_new_image(image_id)

    def nativeZoom(self) -> float:
        """Return the zoom level where one image pixel equals one device pixel."""
        return self.view().viewport.nativeZoom()

    def calculateRenderState(
        self, *, use_pan: QPointF | None = None
    ) -> RenderState | None:
        """Expose the presenter's render-state calculation to collaborators."""
        return self.view().calculateRenderState(
            use_pan=use_pan, is_blank=self._is_blank
        )

    def isDragOutAllowed(self) -> bool:
        """Return True when drag-out is enabled and the image fits the viewport."""
        catalog = self.catalog()
        if catalog.placeholderActive():
            policy = catalog.placeholderPolicy()
            if policy is None or not getattr(policy, "drag_out_enabled", False):
                return False
            if self.original_image.isNull():
                return False
        if not getattr(self.settings, "drag_out_enabled", True):
            return False
        return ui.is_drag_out_allowed(
            image=self.original_image,
            zoom=self.view().viewport.zoom,
            zoom_mode=self.view().viewport.get_zoom_mode(),
            viewport_size=self.physicalViewportRect().size(),
        )

    def replaceRenderer(self, renderer: "Renderer") -> None:
        """Swap the active renderer while keeping presenter/view state aligned."""
        self.view().replace_renderer(renderer)

    def onViewChanged(self):
        """Slot connected to the viewport's viewChanged signal."""
        self.markDirty()
        self.update()
        self.refreshCursor()
        self._emit_zoom_snapshot()
        self._emit_viewport_rect_if_changed()

    def _allocate_buffers(self):
        """Calculate buffer properties and tell the renderer to allocate them."""
        self.view().allocate_buffers()

    def physicalViewportRect(self) -> QRectF:
        """Return the current viewport rectangle in physical (device) pixels.

        Useful for tile visibility and rendering alignment.
        """
        return self.view().physical_viewport_rect()

    def panelToImagePoint(self, panel_pos: QPoint) -> QPoint | None:
        """Delegates coordinate conversion to the viewport."""
        return self.view().panel_to_image_point(panel_pos)

    def imageToPanelPoint(self, image_point: QPoint) -> QPointF | None:
        """Delegates coordinate conversion to the viewport."""
        return self.view().image_to_panel_point(image_point)

    def _screen_tracking_enabled(self) -> bool:
        """Return True when zoom normalization across screens is enabled."""
        return bool(getattr(self.settings, "normalize_zoom_on_screen_change", False))

    def _refresh_rate_tracking_enabled(self) -> bool:
        """Return True when smooth zoom should target the display refresh rate."""
        return bool(getattr(self.settings, "smooth_zoom_use_display_fps", True))

    def _screen_tracking_required(self) -> bool:
        """Return True when the window should listen for screen change events."""
        return self._screen_tracking_enabled() or self._refresh_rate_tracking_enabled()

    def _normalize_one_to_one_enabled(self) -> bool:
        """Return True when 1:1 zoom normalization is allowed."""
        return bool(getattr(self.settings, "normalize_zoom_for_one_to_one", False))

    def _viewport_in_one_to_one(self, viewport) -> bool:
        """Return True when ``viewport`` currently represents a 1:1 zoom."""
        zoom_mode = viewport.get_zoom_mode()
        if zoom_mode == ViewportZoomMode.ONE_TO_ONE:
            return True
        native_zoom = float(viewport.nativeZoom())
        if native_zoom <= 0:
            return False
        return isclose(viewport.zoom, native_zoom, rel_tol=1e-6, abs_tol=1e-6)

    def _refresh_screen_tracking(self) -> None:
        """Attach or detach screen-change listeners based on the current setting."""
        if not self._screen_tracking_required():
            self._disconnect_screen_signals()
            return
        self._connect_screen_signals()
        if self._tracked_screen is not None:
            self._set_tracked_screen(self._tracked_screen, force=True)

    def _screen_device_pixel_ratio(self, screen: QScreen | None) -> float:
        """Return the DPR for ``screen`` or this qpane when unavailable."""
        if screen is not None:
            ratio = float(screen.devicePixelRatio())
        else:
            ratio = float(self.devicePixelRatioF())
        return ratio if ratio > 0 else 1.0

    def _safe_disconnect(self, signal: object, handler: object) -> None:
        """Best-effort disconnect for Qt signals during teardown."""
        try:
            signal.disconnect(handler)
        except (TypeError, RuntimeError, SystemError):
            pass

    def _rebase_zoom_for_screen_change(self, old_dpr: float, new_dpr: float) -> None:
        """Scale zoom/pan so viewport coverage stays stable across DPR changes.

        Args:
            old_dpr: Device pixel ratio before the change.
            new_dpr: Device pixel ratio reported by the new screen.
        """
        if not self._screen_tracking_enabled():
            return
        if old_dpr <= 0 or new_dpr <= 0:
            return
        if isclose(old_dpr, new_dpr, rel_tol=1e-6, abs_tol=1e-6):
            return
        view = self.view()
        viewport = view.viewport
        if not self._normalize_one_to_one_enabled() and self._viewport_in_one_to_one(
            viewport
        ):
            self._last_screen_dpr = new_dpr
            return
        scale = new_dpr / old_dpr
        new_zoom = viewport.zoom * scale
        pan = viewport.pan
        scaled_pan = QPointF(pan.x() * scale, pan.y() * scale)
        viewport.setZoomAndPan(new_zoom, scaled_pan)
        view.presenter.ensure_view_alignment(force=True)
        self._last_screen_dpr = new_dpr

    def _connect_screen_signals(self) -> None:
        """Ensure the window and active screen notify us about DPR changes."""
        window = self._resolve_window_handle()
        if window is None:
            return
        if self._tracked_window is not window:
            self._disconnect_window_signals()
            window.screenChanged.connect(self._handle_screen_changed)
            window.destroyed.connect(self._handle_tracked_window_destroyed)
            self._tracked_window = window
        self._set_tracked_screen(window.screen())

    def _resolve_window_handle(self) -> QWindow | None:
        """Return the top-level window handle hosting this widget."""
        handle = self.windowHandle()
        if handle is not None:
            return handle
        window = self.window()
        if window is None:
            return None
        return window.windowHandle()

    def _disconnect_screen_signals(self) -> None:
        """Detach all screen tracking hooks."""
        self._disconnect_window_signals()
        self._set_tracked_screen(None)

    def _disconnect_window_signals(self) -> None:
        """Safely disconnect tracked window change hooks and clear the reference."""
        window = self._tracked_window
        if window is None:
            return
        self._safe_disconnect(window.screenChanged, self._handle_screen_changed)
        self._safe_disconnect(window.destroyed, self._handle_tracked_window_destroyed)
        self._tracked_window = None

    def _set_tracked_screen(
        self, screen: QScreen | None, *, force: bool = False
    ) -> None:
        """Swap the screen DPI listener to ``screen`` when provided."""
        if not force and self._tracked_screen is screen:
            return
        if self._tracked_screen is not None:
            if "dpi" in self._tracked_screen_connections:
                self._safe_disconnect(
                    self._tracked_screen.logicalDotsPerInchChanged,
                    self._handle_screen_dpi_changed,
                )
            if "refresh" in self._tracked_screen_connections:
                self._safe_disconnect(
                    self._tracked_screen.refreshRateChanged,
                    self._handle_screen_refresh_rate_changed,
                )
        self._tracked_screen = None
        self._tracked_screen_connections.clear()
        if screen is None:
            return
        if self._screen_tracking_enabled():
            screen.logicalDotsPerInchChanged.connect(self._handle_screen_dpi_changed)
            self._tracked_screen_connections.add("dpi")
        if self._refresh_rate_tracking_enabled():
            screen.refreshRateChanged.connect(self._handle_screen_refresh_rate_changed)
            self._tracked_screen_connections.add("refresh")
        self._tracked_screen = screen
        self._last_screen_dpr = self._screen_device_pixel_ratio(screen)
        self.view().viewport.update_detected_refresh_rate(screen.refreshRate())

    def _handle_tracked_window_destroyed(self, destroyed: object | None = None) -> None:
        """Clear tracked window references when the host window is destroyed."""
        if destroyed is not None and destroyed is not self._tracked_window:
            return
        self._tracked_window = None
        self._set_tracked_screen(None)

    def _handle_screen_changed(self, screen: QScreen | None) -> None:
        """Normalize zoom when the widget moves to a different screen."""
        if self._screen_tracking_enabled():
            old_dpr = self._last_screen_dpr
            new_dpr = self._screen_device_pixel_ratio(screen)
            self._rebase_zoom_for_screen_change(old_dpr, new_dpr)
        self._set_tracked_screen(screen)
        self._emit_viewport_rect_if_changed(force=True)

    def _handle_screen_dpi_changed(self, *_args: object) -> None:
        """Normalize zoom when the current screen updates its DPI."""
        screen = self._tracked_screen
        if not self._screen_tracking_enabled():
            return
        screen = self._tracked_screen
        if screen is None:
            return
        old_dpr = self._last_screen_dpr
        new_dpr = self._screen_device_pixel_ratio(screen)
        self._rebase_zoom_for_screen_change(old_dpr, new_dpr)
        self._last_screen_dpr = new_dpr
        self._emit_viewport_rect_if_changed(force=True)

    def _handle_screen_refresh_rate_changed(self, *_args: object) -> None:
        """Record the latest refresh rate when the screen reports a change."""
        screen = self._tracked_screen
        if screen is None:
            return
        self.view().viewport.update_detected_refresh_rate(screen.refreshRate())

    def _emit_zoom_snapshot(self) -> None:
        """Emit the current zoom factor without reaching into demo code."""
        try:
            zoom = float(self.view().viewport.zoom)
        except Exception:  # pragma: no cover - defensive path for shutdown
            return
        self.zoomChanged.emit(zoom)

    def _normalize_zoom_request(
        self, requested_zoom: float, *, reinterpret_one_as_native: bool = True
    ) -> float | None:
        """Validate and clamp a zoom request for viewport application."""
        viewport = self.view().viewport
        if not self._can_apply_zoom():
            return None
        # Limit maximum zoom, and reinterpret '1.0' as nativeZoom() for DPI-accuracy
        requested_zoom = min(requested_zoom, 10.0)
        if reinterpret_one_as_native and abs(requested_zoom - 1.0) < 1e-6:
            requested_zoom = self.nativeZoom()
        min_zoom = viewport.min_zoom()
        return max(requested_zoom, min_zoom)

    def _can_apply_zoom(self) -> bool:
        """Return True when zoom updates are allowed for the current view."""
        viewport = self.view().viewport
        if self.original_image.isNull():
            logger.warning("applyZoom ignored because no image is loaded")
            return False
        if viewport.is_locked():
            logger.warning("applyZoom ignored because the viewport is locked")
            return False
        return True

    def _emit_viewport_rect_if_changed(self, *, force: bool = False) -> None:
        """Emit the physical viewport rectangle when it differs from the last snapshot."""
        try:
            rect = QRectF(self.physicalViewportRect())
        except Exception:  # pragma: no cover - defensive path during teardown
            return
        if not force and self._last_viewport_rect == rect:
            return
        self._last_viewport_rect = rect
        self.viewportRectChanged.emit(rect)

    def _handle_catalog_mutation(self, event: CatalogMutationEvent) -> None:
        """Relay catalog mutations through the QPane signal surface."""
        self.catalogChanged.emit(event)
        self._maybe_emit_link_groups_changed()

    def _emit_catalog_mutation(
        self, reason: str, *, affected_ids: Iterable[uuid.UUID] | None = None
    ) -> None:
        """Emit a catalog mutation event through the QPane surface."""
        current_id: uuid.UUID | None
        try:
            current_id = self.catalog().currentImageID()
        except Exception:
            current_id = None
        event = CatalogMutationEvent(
            reason=reason,
            affected_ids=tuple(affected_ids or ()),
            current_id=current_id,
        )
        self._handle_catalog_mutation(event)

    def _normalized_link_groups(
        self,
    ) -> tuple[tuple[uuid.UUID, tuple[uuid.UUID, ...]], ...]:
        """Return normalized link-group definitions for change detection."""
        normalized: list[tuple[uuid.UUID, tuple[uuid.UUID, ...]]] = []
        for group in self.linkedGroups():
            normalized.append((group.group_id, tuple(sorted(group.members))))
        normalized.sort(key=lambda item: item[0].hex)
        return tuple(normalized)

    def _maybe_emit_link_groups_changed(self) -> None:
        """Emit link-group changes when the current definition differs."""
        groups = self._normalized_link_groups()
        if groups == self._last_link_groups:
            return
        self._last_link_groups = groups
        self.linkGroupsChanged.emit()

    def _emit_catalog_selection_changed(self, image_id: uuid.UUID | None) -> None:
        """Emit catalog selection changes for the active image."""
        self.catalogSelectionChanged.emit(image_id)

    def _handle_current_image_changed_signal(self, image_id: uuid.UUID) -> None:
        """Emit selection updates when the active image changes."""
        self._emit_catalog_selection_changed(image_id)

    def _handle_diagnostics_overlay_toggled(self, enabled: bool) -> None:
        """Emit overlay toggle changes while avoiding duplicate signals."""
        self.diagnosticsOverlayToggled.emit(enabled)

    def _handle_diagnostics_detail_toggled(self, domain: str, enabled: bool) -> None:
        """Emit diagnostics domain detail toggle changes."""
        self.diagnosticsDomainToggled.emit(domain, enabled)

    def resizeEvent(self, event):
        """Handle qpane resizing by realigning the view and refreshing the cursor."""
        self.view().ensure_view_alignment(force=True)
        self.update()
        self.refreshCursor()
        self._emit_viewport_rect_if_changed(force=True)

    def minimumSizeHint(self) -> QSize:
        """Prevent resizing below the configured minimum view size."""
        return self.view().minimum_size_hint()

    def paintEvent(self, event):
        """Delegate painting to the presenter and overlays after ensuring alignment."""
        self.view().ensure_view_alignment()
        presenter = self.view().presenter
        presenter.paint(
            is_blank=self._is_blank,
            content_overlays=self.interaction.content_overlays,
            overlays_suspended=self.interaction.overlays_suspended,
            draw_tool_overlay=self._tools_manager.draw_overlay,
        )
        self.interaction.maybe_resume_overlays()

    def wheelEvent(self, event: QWheelEvent):
        """Route wheel events to the interaction layer for gesture handling."""
        self.interaction.handle_wheel_event(event)

    def mousePressEvent(self, event):
        """Forward mouse press events to the interaction delegate."""
        self.interaction.handle_mouse_press(event)

    def mouseMoveEvent(self, event):
        """Forward mouse move events to the interaction delegate."""
        self.interaction.handle_mouse_move(event)

    def mouseReleaseEvent(self, event):
        """Forward mouse release events to the interaction delegate."""
        self.interaction.handle_mouse_release(event)

    def mouseDoubleClickEvent(self, event):
        """Forward mouse double-click events to the interaction delegate."""
        self.interaction.handle_mouse_double_click(event)

    def enterEvent(self, event):
        """Forward enter events before invoking QWidget handling."""
        self.interaction.handle_enter_event(event)
        super().enterEvent(event)

    def leaveEvent(self, event):
        """Forward leave events before invoking QWidget handling."""
        self.interaction.handle_leave_event(event)
        super().leaveEvent(event)

    def event(self, event):
        """Refresh screen tracking when widget window ownership changes."""
        if not hasattr(self, "_state"):
            return super().event(event)
        event_type = event.type()
        if event_type in (
            QEvent.Type.WinIdChange,
            QEvent.Type.ParentChange,
            QEvent.Type.ShowToParent,
        ):
            self._refresh_screen_tracking()
        return super().event(event)

    def showEvent(self, event):
        """Handle initial show-time setup that depends on widget geometry."""
        super().showEvent(event)
        self.interaction.handle_show_event()
        self._refresh_screen_tracking()
        self._emit_viewport_rect_if_changed(force=True)

    def keyPressEvent(self, event):
        """Let the interaction layer handle key presses first, falling back to QWidget."""
        if self.interaction.handle_key_press(event):
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        """Let the interaction layer handle key releases first, falling back to QWidget."""
        if self.interaction.handle_key_release(event):
            return
        super().keyReleaseEvent(event)
