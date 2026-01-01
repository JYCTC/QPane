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

"""Public type primitives and enums exposed through the qpane facade."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPointF, QRect, QRectF
from PySide6.QtGui import QImage, QTransform

if TYPE_CHECKING:
    from .masks.workflow import MaskInfo
__all__ = [
    "CacheMode",
    "PlaceholderScaleMode",
    "ZoomMode",
    "DiagnosticsDomain",
    "ControlMode",
    "CatalogEntry",
    "LinkedGroup",
    "DiagnosticRecord",
    "OverlayState",
    "MaskInfo",
    "MaskSavedPayload",
    "CatalogSnapshot",
]


class CacheMode(str, Enum):
    """Cache budgeting strategy."""

    AUTO = "auto"
    HARD = "hard"


class PlaceholderScaleMode(str, Enum):
    """Scaling rule applied to placeholder assets."""

    AUTO = "auto"
    LOGICAL_FIT = "logical_fit"
    PHYSICAL_FIT = "physical_fit"
    RELATIVE_FIT = "relative_fit"


class ZoomMode(str, Enum):
    """Zoom policy used by placeholder rendering."""

    FIT = "fit"
    LOCKED_ZOOM = "locked_zoom"
    LOCKED_SIZE = "locked_size"


class DiagnosticsDomain(str, Enum):
    """Diagnostics categories exposed through the facade."""

    CACHE = "cache"
    SWAP = "swap"
    MASK = "mask"
    EXECUTOR = "executor"
    RETRY = "retry"
    SAM = "sam"


class ControlMode(str, Enum):
    """Built-in control modes supported by the tool manager."""

    CURSOR = "cursor"
    PANZOOM = "panzoom"
    DRAW_BRUSH = "draw-brush"
    SMART_SELECT = "smart-select"


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """Structured catalog entry containing image data and an optional path."""

    image: QImage
    path: Path | None


@dataclass(frozen=True, slots=True)
class LinkedGroup:
    """Linked-view group descriptor with a stable identifier."""

    group_id: uuid.UUID
    members: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class DiagnosticRecord:
    """Single name/value diagnostic entry shown in overlays."""

    label: str
    value: str

    def formatted(self) -> str:
        """Return a human-friendly string for display."""
        if not self.label:
            return self.value
        return f"{self.label}: {self.value}"

    def __str__(self) -> str:  # pragma: no cover - formatting helper
        """Return the formatted representation for inline rendering."""
        return self.formatted()


MaskSavedPayload = tuple[str, str]


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """Structured catalog state returned by the facade snapshot helper."""

    catalog: dict[uuid.UUID, CatalogEntry]
    linked_groups: tuple[LinkedGroup, ...]
    order: tuple[uuid.UUID, ...]
    current_image_id: uuid.UUID | None
    active_mask_id: uuid.UUID | None
    mask_capable: bool


@dataclass(frozen=True, slots=True)
class OverlayState:
    """Stable overlay context describing the current view and render snapshot."""

    zoom: float
    qpane_rect: QRect
    source_image: QImage
    transform: QTransform
    current_pan: QPointF
    physical_viewport_rect: QRectF


def __getattr__(name: str) -> Any:
    """Lazily resolve MaskInfo and CatalogMutationEvent to avoid import cycles."""
    if name == "MaskInfo":
        from .masks.workflow import MaskInfo as _MaskInfo

        globals()["MaskInfo"] = _MaskInfo
        return _MaskInfo
    if name == "CatalogMutationEvent":
        from .catalog.catalog import CatalogMutationEvent as _CatalogMutationEvent

        globals()["CatalogMutationEvent"] = _CatalogMutationEvent
        return _CatalogMutationEvent
    raise AttributeError(f"module {__name__!s} has no attribute {name}")
