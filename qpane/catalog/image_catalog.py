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

"""In-memory catalog that tracks images, paths, and mask/pyramid state."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Set

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from ..concurrency import TaskExecutorProtocol
from ..core import Config
from ..rendering import PyramidManager
from .image_map import ImageMap
from ..types import CatalogEntry
from .image_utils import images_differ

if TYPE_CHECKING:
    from ..masks.mask import MaskManager
logger = logging.getLogger(__name__)


class ImageCatalog(QObject):
    """Qt-backed data model tracking catalog images, paths, masks, and pyramids."""

    pyramidReady = Signal(uuid.UUID)

    def __init__(
        self,
        config: Config,
        executor: TaskExecutorProtocol,
        parent=None,
        mask_manager: "MaskManager" | None = None,
    ):
        """Initialize the Qt-backed catalog and its managers.

        Args:
            config: Active configuration snapshot for cache/pyramid policies.
            executor: Shared task executor powering pyramid generation.
            parent: Optional QObject parent used for Qt ownership.
            mask_manager: Mask manager wired to catalog lifecycle events.
        """
        super().__init__(parent)
        self._config = config
        self.image_ids: List[uuid.UUID] = []
        self.images_by_id: Dict[uuid.UUID, QImage] = {}
        self.paths_by_id: Dict[uuid.UUID, Path | None] = {}
        self.current_id: uuid.UUID | None = None
        self.mask_manager: "MaskManager" | None = mask_manager
        self.pyramid_manager = PyramidManager(
            config=config, parent=self, executor=executor
        )
        self.pyramid_manager.pyramidReady.connect(self.pyramidReady)

    def apply_config(self, config: Config) -> None:
        """Propagate configuration updates to dependent managers.

        Args:
            config: Updated configuration snapshot.
        """
        self._config = config
        self.pyramid_manager.apply_config(config)
        current_id = self.current_id
        if current_id is None:
            return
        image = self.images_by_id.get(current_id)
        path = self.paths_by_id.get(current_id)
        if image is not None and not image.isNull():
            self.pyramid_manager.generate_pyramid_for_image(current_id, image, path)

    def set_mask_manager(self, mask_manager: "MaskManager" | None) -> None:
        """Assign or replace the mask manager backend.

        Args:
            mask_manager: Manager responsible for responding to catalog events.
        """
        self.mask_manager = mask_manager

    def setImagesByID(
        self,
        image_map: ImageMap,
        current_id: uuid.UUID,
    ) -> tuple[Set[uuid.UUID], Set[uuid.UUID]]:
        """Replace the entire catalog while keeping pyramids and masks in sync.

        Args:
            image_map: Ordered mapping of IDs to ``CatalogEntry`` records.
            current_id: Identifier that should become the current selection.

        Returns:
            Image IDs whose pyramids were removed and IDs whose content changed
            but kept the same identifier.

        Raises:
            ValueError: If ``image_map`` is empty.
            KeyError: If ``current_id`` is not contained within ``image_map``.
            TypeError: If entries are not ``CatalogEntry`` instances with QImage payloads.

        Side effects:
            Clears pyramids for removed/changed paths and notifies the mask manager
            about removed images before regenerating new pyramids.
        """
        if not image_map:
            raise ValueError("image_map must not be empty")
        if current_id not in image_map:
            raise KeyError("current_id must be a key in image_map")
        formatted: Dict[uuid.UUID, QImage] = {}
        for iid, entry in image_map.items():
            if not isinstance(entry, CatalogEntry):
                raise TypeError("image_map values must be CatalogEntry instances")
            if not isinstance(entry.image, QImage):
                raise TypeError("CatalogEntry.image must be a QImage instance")
            formatted[iid] = self._ensureArgb32(entry.image)
        old_ids = set(self.image_ids)
        new_ids = set(image_map.keys())
        ids_to_remove = old_ids - new_ids
        mask_manager = self.mask_manager
        for iid in ids_to_remove:
            if mask_manager:
                mask_manager.handle_image_removal(iid)
        ids_with_changed_content: Set[uuid.UUID] = set()
        for iid, entry in image_map.items():
            existing_image = self.images_by_id.get(iid)
            if images_differ(existing_image, formatted[iid]):
                ids_with_changed_content.add(iid)
        for iid in ids_to_remove:
            self.pyramid_manager.remove_pyramid(iid)
        for iid in ids_with_changed_content:
            self.pyramid_manager.remove_pyramid(iid)
        for iid in image_map.keys():
            image = formatted[iid]
            if image.isNull():
                continue
            self.pyramid_manager.generate_pyramid_for_image(
                iid, image, image_map[iid].path
            )
        self.image_ids = list(image_map.keys())
        self.images_by_id = {iid: formatted[iid] for iid in self.image_ids}
        self.paths_by_id = {iid: image_map[iid].path for iid in self.image_ids}
        self.current_id = current_id
        return ids_to_remove, ids_with_changed_content

    def addImage(
        self,
        image_id: uuid.UUID,
        image: QImage,
        path: Path | None,
    ):
        """Add or replace a single image entry without touching the rest of the catalog.

        Args:
            image_id: Identifier to create or overwrite.
            image: Image data to store; must not be null.
            path: Optional filesystem path that triggers pyramid generation when new.

        Raises:
            ValueError: If ``image`` is null.

        Side effects:
            Removes pyramids for displaced paths, regenerates pyramids for new or
            updated content, and leaves mask state intact unless IDs are removed.
        """
        if image.isNull():
            logger.error("addImage called with null QImage for %s", image_id)
            raise ValueError("image must not be null")
        formatted_image = self._ensureArgb32(image)
        existing_image = self.images_by_id.get(image_id)
        if images_differ(existing_image, formatted_image):
            self.pyramid_manager.remove_pyramid(image_id)
        self.pyramid_manager.generate_pyramid_for_image(image_id, formatted_image, path)
        if image_id not in self.image_ids:
            self.image_ids.append(image_id)
        self.images_by_id[image_id] = formatted_image
        self.paths_by_id[image_id] = path

    def updateCurrentEntry(
        self,
        *,
        image: QImage | None = None,
        path: Path | None = None,
    ) -> bool:
        """Refresh the stored image/path for the active selection, updating caches.

        Args:
            image: Replacement pixels for the current ID.
            path: Replacement filesystem path for the current ID.

        Side effects:
            Rebuilds pyramids for replaced paths and regenerates the pyramid when
            image content changes.

        Returns:
            True when the underlying content or path changed.
        """
        current_id = self.current_id
        if current_id is None:
            if self.image_ids and (image is not None or path is not None):
                logger.warning(
                    "Ignoring updateCurrentEntry because no current image is selected"
                )
            return False
        previous_image = self.images_by_id.get(current_id)
        formatted_image: QImage | None = None
        if image is not None and not image.isNull():
            formatted_image = self._ensureArgb32(image)
            self.images_by_id[current_id] = formatted_image
        old_path = self.paths_by_id.get(current_id)
        reference_image = formatted_image or self.images_by_id.get(current_id)
        content_changed = False
        if old_path != path:
            content_changed = True
            self.pyramid_manager.remove_pyramid(current_id)
            if (
                path is not None
                and reference_image is not None
                and not reference_image.isNull()
            ):
                self.pyramid_manager.generate_pyramid_for_image(
                    current_id, reference_image, path
                )
        elif (
            path is not None
            and reference_image is not None
            and not reference_image.isNull()
        ):
            existing_image = previous_image
            if images_differ(existing_image, reference_image):
                self.pyramid_manager.remove_pyramid(current_id)
                self.pyramid_manager.generate_pyramid_for_image(
                    current_id, reference_image, path
                )
                content_changed = True
        if current_id in self.paths_by_id or path is not None:
            self.paths_by_id[current_id] = path
        return content_changed

    def removeImageByID(self, image_id: uuid.UUID):
        """Remove the image and its metadata for ``image_id``.

        Args:
            image_id: Identifier to remove.

        Raises:
            KeyError: If ``image_id`` is not known to the catalog.

        Side effects:
            Removes pyramids and mask state for the image and updates the current
            selection when the removed image was active.
        """
        if image_id not in self.images_by_id:
            raise KeyError("image_id not found")
        # Pyramid and mask cleanup
        self.pyramid_manager.remove_pyramid(image_id)
        if self.mask_manager:
            self.mask_manager.handle_image_removal(image_id)
        # Remove from stores
        self.images_by_id.pop(image_id, None)
        self.paths_by_id.pop(image_id, None)
        if image_id in self.image_ids:
            self.image_ids.remove(image_id)
        # Update current selection
        if not self.image_ids:
            self.current_id = None
        elif self.current_id == image_id:
            self.current_id = self.image_ids[0]

    def clearImages(self):
        """Reset the catalog, pyramids, and masks to an empty state.

        Side effects:
            Clears pyramids, mask state, catalog ordering, and the active selection.
        """
        self.pyramid_manager.clear()
        if self.mask_manager:
            self.mask_manager.clear_all()
        self.image_ids = []
        self.images_by_id = {}
        self.paths_by_id = {}
        self.current_id = None

    def setCurrentImageID(self, image_id: uuid.UUID | None):
        """Update the current image selection by UUID.

        Args:
            image_id: Identifier that should become current, or None to deselect.

        Raises:
            KeyError: If ``image_id`` is not found and is not None.
        """
        if image_id is not None and image_id not in self.images_by_id:
            raise KeyError("image_id not found")
        self.current_id = image_id

    def getImage(self, image_id: uuid.UUID) -> QImage | None:
        """Return the QImage for a specific image ID."""
        return self.images_by_id.get(image_id)

    def getPath(self, image_id: uuid.UUID) -> Path | None:
        """Return the filesystem Path for a specific image ID."""
        return self.paths_by_id.get(image_id)

    def getCurrentImage(self) -> QImage | None:
        """Return the QImage for the currently selected image, if any."""
        return self.images_by_id.get(self.current_id) if self.current_id else None

    def getCurrentPath(self) -> Path | None:
        """Return the filesystem Path for the current image, if any."""
        return self.paths_by_id.get(self.current_id) if self.current_id else None

    def getCurrentId(self) -> uuid.UUID | None:
        """Return the UUID for the currently selected image, if any."""
        return self.current_id

    def get_mask_manager(self) -> "MaskManager" | None:
        """Expose the mask manager currently associated with this catalog."""
        return self.mask_manager

    def containsImage(self, image_id: uuid.UUID) -> bool:
        """Return True when the catalog stores an image for ``image_id``."""
        return image_id in self.images_by_id

    def getImageIds(self) -> List[uuid.UUID]:
        """Return a copy of the catalog's UUID ordering."""
        return list(self.image_ids)

    def hasImages(self) -> bool:
        """Return ``True`` when at least one image is stored."""
        return bool(self.image_ids)

    def getAllImages(self) -> List[QImage]:
        """Return each stored QImage preserving insertion order."""
        return [
            self.images_by_id[iid] for iid in self.image_ids if iid in self.images_by_id
        ]

    def getAllPaths(self) -> List[Path | None]:
        """Return filesystem paths aligned with :meth:`getAllImages`."""
        return [self.paths_by_id.get(iid) for iid in self.image_ids]

    def getBestFitImage(
        self, image_id: uuid.UUID | None, target_width: float
    ) -> QImage | None:
        """Retrieve the best-fit pyramid image for the requested width.

        Args:
            image_id: Image ID that identifies the pyramid to query.
            target_width: Desired width in device pixels.

        Returns:
            Approximated image level or ``None`` when unavailable.
        """
        if image_id is None:
            return None
        return self.pyramid_manager.get_best_fit_image(image_id, target_width)

    def _ensureArgb32(self, image: QImage) -> QImage:
        """Return a QImage in ARGB32_Premultiplied format; gracefully handle null images."""
        if image.isNull() or image.format() == QImage.Format_ARGB32_Premultiplied:
            return image
        return image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
