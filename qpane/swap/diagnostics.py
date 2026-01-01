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

"""Swap diagnostics provider for the QPane status overlay."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable

from ..types import DiagnosticRecord

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ..qpane import QPane
    from .coordinator import SwapCoordinatorMetrics
logger = logging.getLogger(__name__)
_failure_logged: set[str] = set()


def swap_summary_provider(qpane: "QPane") -> Iterable[DiagnosticRecord]:
    """Return the navigation latency row for the core diagnostics tier."""
    metrics = _resolve_swap_metrics(qpane)
    if metrics is None:
        return tuple()
    summary = _format_swap_summary(metrics)
    if not summary:
        return tuple()
    return (DiagnosticRecord("Swap|Summary", summary),)


def swap_progress_provider(qpane: "QPane") -> Iterable[DiagnosticRecord]:
    """Return swap-related diagnostics rows sourced from the swap delegate."""
    metrics = _resolve_swap_metrics(qpane)
    if metrics is None:
        return tuple()
    rows: list[DiagnosticRecord] = []
    prefetch = _format_prefetch_line(metrics)
    if prefetch:
        rows.append(DiagnosticRecord("Swap|Prefetch", prefetch))
    renderer_line = _format_renderer_metrics(qpane)
    if renderer_line:
        rows.append(DiagnosticRecord("Swap|Renderer", renderer_line))
    mask_line = _format_mask_metrics(qpane)
    if mask_line:
        rows.append(DiagnosticRecord("Swap|Masks", mask_line))
    tile_line = _format_tile_metrics(qpane)
    if tile_line:
        rows.append(DiagnosticRecord("Swap|Tiles", tile_line))
    pyramid_line = _format_pyramid_metrics(qpane)
    if pyramid_line:
        rows.append(DiagnosticRecord("Swap|Pyramids", pyramid_line))
    sam_line = _format_sam_metrics(qpane)
    if sam_line:
        rows.append(DiagnosticRecord("Swap|SAM", sam_line))
    return tuple(rows)


def _resolve_swap_metrics(qpane: "QPane") -> "SwapCoordinatorMetrics" | None:
    """Return the swap metrics snapshot from the delegate when present."""
    delegate = getattr(qpane, "swapDelegate", None)
    snapshot_fn = getattr(delegate, "snapshot_metrics", None)
    if not callable(snapshot_fn):
        return None
    try:
        metrics: SwapCoordinatorMetrics = snapshot_fn()
    except Exception as exc:  # pragma: no cover - defensive guard
        _log_snapshot_failure("swap_metrics", "Swap metrics snapshot failed", exc)
        return None
    _clear_snapshot_failure("swap_metrics")
    return metrics


def _format_swap_summary(metrics: "SwapCoordinatorMetrics") -> str:
    """Format the swap summary row emphasising last navigation latency."""
    last_nav = getattr(metrics, "last_navigation_ms", None)
    if isinstance(last_nav, (int, float)) and last_nav >= 0:
        return f"nav={last_nav:.0f}ms"
    return "nav=-"


def _format_prefetch_line(metrics: "SwapCoordinatorMetrics") -> str:
    """Format pending prefetch counters for the diagnostics overlay."""
    pending_masks = getattr(metrics, "pending_mask_prefetch", None)
    pending_predictors = getattr(metrics, "pending_predictors", None)
    pending_pyramids = getattr(metrics, "pending_pyramid_prefetch", None)
    pending_tiles = getattr(metrics, "pending_tile_prefetch", None)
    parts: list[str] = []
    if isinstance(pending_masks, int) and pending_masks > 0:
        parts.append(f"mask_prefetch={pending_masks}")
    if isinstance(pending_predictors, int) and pending_predictors > 0:
        parts.append(f"predictors={pending_predictors}")
    if isinstance(pending_pyramids, int) and pending_pyramids > 0:
        parts.append(f"pyramids={pending_pyramids}")
    if isinstance(pending_tiles, int) and pending_tiles > 0:
        parts.append(f"tiles={pending_tiles}")
    return " | ".join(parts)


def _format_renderer_metrics(qpane: "QPane") -> str:
    """Build the renderer metrics row for the diagnostics overlay."""
    view = _view(qpane)
    renderer = None if view is None else view.renderer
    snapshot_fn = getattr(renderer, "snapshot_metrics", None)
    if not callable(snapshot_fn):
        return ""
    try:
        snapshot = snapshot_fn()
    except Exception as exc:  # pragma: no cover - defensive guard
        _log_snapshot_failure("renderer", "Renderer metrics snapshot failed", exc)
        return ""
    _clear_snapshot_failure("renderer")
    parts: list[str] = []
    allocations = getattr(snapshot, "base_buffer_allocations", None)
    if isinstance(allocations, int) and allocations > 0:
        parts.append(f"alloc={allocations}")
    attempts = getattr(snapshot, "scroll_attempts", None)
    hits = getattr(snapshot, "scroll_hits", None)
    misses = getattr(snapshot, "scroll_misses", None)
    if isinstance(attempts, int) and attempts >= 0:
        if isinstance(hits, int) and hits >= 0:
            parts.append(f"scroll={hits}/{attempts}")
        else:
            parts.append(f"scroll_attempts={attempts}")
    if isinstance(misses, int) and misses:
        parts.append(f"miss={misses}")
    full = getattr(snapshot, "full_redraws", None)
    partial = getattr(snapshot, "partial_redraws", None)
    if isinstance(full, int) and isinstance(partial, int) and (full or partial):
        parts.append(f"redraws={full}F/{partial}P")
    paint_ms = getattr(snapshot, "last_paint_ms", None)
    if isinstance(paint_ms, (int, float)) and paint_ms >= 0.0:
        parts.append(f"paint={paint_ms:.0f}ms")
    return " | ".join(parts)


def _format_mask_metrics(qpane: "QPane") -> str:
    """Build the diagnostics row describing mask cache usage."""
    controller = getattr(qpane, "mask_controller", None)
    snapshot_fn = getattr(controller, "snapshot_metrics", None)
    if not callable(snapshot_fn):
        return ""
    try:
        snapshot = snapshot_fn()
    except Exception as exc:  # pragma: no cover - defensive guard
        _log_snapshot_failure("mask", "Mask metrics snapshot failed", exc)
        return ""
    _clear_snapshot_failure("mask")
    usage_mb = _to_mb(getattr(snapshot, "cache_bytes", None))
    hits = getattr(snapshot, "hits", None)
    misses = getattr(snapshot, "misses", None)
    colorize_ms = getattr(snapshot, "colorize_last_ms", None)
    parts = [f"usage={usage_mb:.1f}MB"]
    if isinstance(hits, int) and hits > 0:
        parts.append(f"hit={hits}")
    if isinstance(misses, int) and misses > 0:
        parts.append(f"miss={misses}")
    if isinstance(colorize_ms, (int, float)) and colorize_ms > 0:
        parts.append(f"colorize={colorize_ms:.0f}ms")
    return " | ".join(parts)


def _format_tile_metrics(qpane: "QPane") -> str:
    """Summarize tile cache usage and retry counts for diagnostics."""
    view = _view(qpane)
    manager = None if view is None else view.tile_manager
    snapshot_fn = getattr(manager, "snapshot_metrics", None)
    if not callable(snapshot_fn):
        return ""
    try:
        snapshot = snapshot_fn()
    except Exception as exc:  # pragma: no cover - defensive guard
        _log_snapshot_failure("tiles", "Tile metrics snapshot failed", exc)
        return ""
    _clear_snapshot_failure("tiles")
    usage_mb = _to_mb(getattr(snapshot, "cache_bytes", None))
    limit_mb = _to_mb(getattr(snapshot, "cache_limit", None))
    hits = getattr(snapshot, "hits", None)
    misses = getattr(snapshot, "misses", None)
    pending = getattr(snapshot, "pending_retries", None)
    parts = [f"usage={usage_mb:.1f}/{limit_mb:.1f}MB"]
    if isinstance(hits, int) and hits > 0:
        parts.append(f"hit={hits}")
    if isinstance(misses, int) and misses > 0:
        parts.append(f"miss={misses}")
    if isinstance(pending, int) and pending:
        parts.append(f"retry={pending}")
    return " | ".join(parts)


def _format_pyramid_metrics(qpane: "QPane") -> str:
    """Summarize pyramid cache status for the diagnostics overlay."""
    catalog = _catalog(qpane)
    manager = getattr(catalog, "pyramid_manager", None)
    snapshot_fn = getattr(manager, "snapshot_metrics", None)
    if not callable(snapshot_fn):
        return ""
    try:
        snapshot = snapshot_fn()
    except Exception as exc:  # pragma: no cover - defensive guard
        _log_snapshot_failure("pyramid", "Pyramid metrics snapshot failed", exc)
        return ""
    _clear_snapshot_failure("pyramid")
    usage_mb = _to_mb(getattr(snapshot, "cache_bytes", None))
    limit_mb = _to_mb(getattr(snapshot, "cache_limit", None))
    active = getattr(snapshot, "active_jobs", None)
    parts = [f"usage={usage_mb:.1f}/{limit_mb:.1f}MB"]
    if isinstance(active, int) and active:
        parts.append(f"active={active}")
    return " | ".join(parts)


def _format_sam_metrics(qpane: "QPane") -> str:
    """Format cache stats for the SAM predictor workflow."""
    accessor = getattr(qpane, "samManager", None)
    manager = accessor() if callable(accessor) else None
    snapshot_fn = getattr(manager, "snapshot_metrics", None)
    if not callable(snapshot_fn):
        return ""
    try:
        snapshot = snapshot_fn()
    except Exception as exc:  # pragma: no cover - defensive guard
        _log_snapshot_failure("sam", "SAM metrics snapshot failed", exc)
        return ""
    _clear_snapshot_failure("sam")
    usage_mb = _to_mb(getattr(snapshot, "cache_bytes", None))
    count = getattr(snapshot, "cache_count", None)
    pending = getattr(snapshot, "pending_retries", None)
    parts = [f"usage={usage_mb:.1f}MB"]
    if isinstance(count, int):
        parts.append(f"cached={count}")
    if isinstance(pending, int) and pending:
        parts.append(f"retry={pending}")
    return " | ".join(parts)


_MB = 1024 * 1024


def _to_mb(value) -> float:
    """Convert byte counts to megabytes for diagnostics display."""
    if isinstance(value, (int, float)):
        return float(value) / _MB
    return 0.0


def _view(qpane: "QPane"):
    """Return the QPane view collaborator or ``None`` when unavailable."""
    try:
        return qpane.view()
    except AttributeError:
        return None


def _catalog(qpane: "QPane"):
    """Return the QPane catalog collaborator or ``None`` when unavailable."""
    try:
        return qpane.catalog()
    except AttributeError:
        return None


def _log_snapshot_failure(label: str, message: str, exc: BaseException) -> None:
    """Emit a once-per-failure diagnostic when a snapshot hook raises."""
    if label in _failure_logged:
        return
    _failure_logged.add(label)
    logger.exception("%s", message, exc_info=exc)


def _clear_snapshot_failure(label: str) -> None:
    """Reset failure tracking once a snapshot hook succeeds again."""
    _failure_logged.discard(label)
