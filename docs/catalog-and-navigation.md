**← Previous:** [Configuration Reference](configuration-reference.md)

# Catalog and Navigation

The catalog is the source of truth for your viewer. It maps UUIDs to images, handles ordering, and drives the navigation logic so you don't have to manage indices manually. Whether you're building a simple slideshow or a complex gallery, the catalog keeps your UI and the renderer in sync.

## Build an Image Catalog
To populate the viewer, you convert your images into an ordered map. QPane handles the ID generation and internal mapping, letting you focus on the content.

Use `QPane.imageMapFromLists` to create the catalog structure, then apply it with `QPane.setImagesByID`.

```python
from PySide6.QtGui import QImage
from qpane import QPane

# 1. Prepare your data (filter out nulls first!)
images = [QImage("one.png"), QImage("two.png")]
paths = ["one.png", "two.png"]

# 2. Build the map
# If you skip 'ids', QPane generates UUIDs for you.
image_map = QPane.imageMapFromLists(images, paths=paths)

# 3. Load it and pick the starting image
start_id = next(iter(image_map))
viewer.setImagesByID(image_map, current_id=start_id)
```

> **Pro Tip:** Always check `image.isNull()` before adding it to the list. `QPane.imageMapFromLists` raises a `ValueError` if the input lists (images/paths/IDs) have mismatched lengths.

### Understanding Entries
Under the hood, the catalog stores `CatalogEntry` objects. Each one wraps a `QImage` and an optional `Path`. You usually won't create these manually, but you'll see them if you inspect the catalog directly.

### Managing the List
*   **Snapshots:** Building a sidebar? `QPane.getCatalogSnapshot` returns a `CatalogSnapshot`—a thread-safe frozen view of the entire state (entries, order, and selection). Use this to render your gallery UI without racing against the main thread.
*   **Clearing:** `QPane.clearImages` wipes the catalog and shows the placeholder.
*   **Pruning:** Use `QPane.removeImageByID` or `QPane.removeImagesByID` to drop specific items. This happens in-place and won't force a full reload.

## Navigate Between Images
To move through the content, use `QPane.setCurrentImageID`. Pass a UUID to navigate to an image, or `None` to clear the view. This handles the heavy lifting: it suspends overlays, swaps the render buffers, and fires the selection signals.

You can inspect the current state with `QPane.currentImageID`, `QPane.currentImage`, and `QPane.currentImagePath`. For the full list, check `QPane.imageIDs` or `QPane.hasImages`.

```python
# Cycle to the next image
ids = viewer.imageIDs()
if len(ids) > 1:
    current = viewer.currentImageID()
    # Find current index, defaulting to 0 if not found
    idx = ids.index(current) if current in ids else 0
    next_id = ids[(idx + 1) % len(ids)]
    
    viewer.setCurrentImageID(next_id)
```

> **Heads-up:** `setCurrentImageID` temporarily suspends overlays (like cursors or masks) during the swap to prevent visual glitches. If you're writing a custom tool, expect a brief "reset" when navigation occurs.

### Looping and Looking Up
Need to find a specific file or process every loaded item?
*   `QPane.imagePath(id)`: Look up the filesystem path for a specific UUID.
*   `QPane.allImages`: Get a list of all `QImage` objects in catalog order.
*   `QPane.allImagePaths`: Get the corresponding list of paths (some might be `None`).

## Programmatic View Control
While users typically pan and zoom with the mouse, your host application often needs to drive the view directly—for example, to implement a "Reset" button or a "100%" zoom shortcut.

*   **Fitting & Resetting:** Use `QPane.setZoomFit()` to instantly re-frame the content within the viewport.
*   **Precision Zooming:** Use `QPane.applyZoom(zoom, anchor=None)`. Unlike a raw property setter, this helper clamps values to safe limits and handles High-DPR scaling so that `1.0` truly means "1 image pixel = 1 screen pixel."
*   **Snapping:** `QPane.setZoom1To1(anchor=None)` is a shortcut for that native pixel-perfect view.
*   **Inspection:** Read `QPane.currentZoom` to get the effective zoom level.

Both zoom methods accept an optional `anchor` (QPoint or QPointF) to keep a specific point stationary while the scale changes.

> **Demo Tip:** The demonstration app exposes these via a percent-only zoom input in the status bar and a nearby toggle button that switches between Fit and 1:1. Use that layout as a reference for tutorializing zoom controls in your own host.

> **Note:** The default Pan/Zoom tool snaps wheel zoom steps to the native 1:1 scale when crossing it, ensuring users hit 100% on the way in or out.

## Link Views (Synchronized Pan/Zoom)
To compare images side-by-side—like before/after shots or exposure brackets—you can link them. Linked images share their pan and zoom state; moving one moves them all.

*   **Quick Link:** `QPane.setAllImagesLinked(True)` locks every image in the catalog together.
*   **Custom Groups:** `QPane.setLinkedGroups` lets you define specific subsets using `LinkedGroup` objects.
*   **Inspection:** `QPane.linkedGroups` returns the active definitions.

> **Note:** Linking requires at least two images. For the best "lockstep" feel, ensure linked images share the same aspect ratio.

## Listen for Events
To keep your UI in sync, connect to QPane's signals. They tell you exactly when the viewer state changes so you can update labels, buttons, or sidebars.

### Navigation Events
*   `QPane.catalogSelectionChanged`: Fires when the active image ID changes (or becomes `None`). Use this to update your window title or "Next" button.
*   `QPane.currentImageChanged`: Similar to selection changed, but strictly emits the UUID.
*   `QPane.catalogChanged`: Fires on structural changes (add, remove, reorder). The signal carries a `CatalogMutationEvent` which tells you the `reason` (e.g., "maskCreated") and the `affected_ids`.

### Content Events
*   `QPane.imageLoaded`: Fires when the new image pixels are actually ready to render.
*   `QPane.linkGroupsChanged`: Fires when the link definitions are updated.

### View State Events
*   `QPane.zoomChanged`: Emits the zoom factor (`float`) whenever it changes.
*   `QPane.viewportRectChanged`: Emits the physical viewport rectangle (`QRectF`).
    *   *Why this matters:* This fires not just on resize, but when the window moves between screens with different DPIs. If you're drawing custom overlays, use this signal (or `QPane.currentViewportRect()`) to keep your coordinates aligned.

### Hit Testing
Need to know where the mouse is? `QPane.panelHitTest(pos)` converts widget coordinates into image coordinates, handling all the aspect-ratio and zoom math for you. The returned `PanelHitTest` exposes:

- `PanelHitTest.panel_point`: Panel-space position that was tested.
- `PanelHitTest.raw_point`: Unclamped image-space coordinate as float.
- `PanelHitTest.clamped_point`: Image-space coordinate clamped to image bounds.
- `PanelHitTest.inside_image`: True when the raw point lies inside the image.

**Continue →** [Interaction Modes](interaction-modes.md)
