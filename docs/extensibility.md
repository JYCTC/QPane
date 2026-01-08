**← Previous:** [Diagnostics](diagnostics.md)

# Extensibility

QPane isn't just a viewer; it's a canvas. Whether you need to draw a scale bar, change the cursor based on keyboard modifiers, or build a completely new interaction mode (like a "measurement tape"), QPane exposes the same hooks it uses internally. This means your extensions feel native, performant, and integrated.

## 1. Overlays: Draw on Top
Want to add a watermark, a grid, or a "Zoom: 100%" label? Overlays let you paint directly onto the canvas after the image is rendered but before the tool layer.

*   **Register:** `QPane.registerOverlay(name, draw_fn)`
*   **Remove:** `QPane.unregisterOverlay(name)`

Your `draw_fn` receives a `QPainter` and an `OverlayState` object. The painter operates in **Widget Space** (physical pixels), so `0,0` is the top-left corner of the viewer widget. `OverlayState` is a stable snapshot tuned for overlays:

*   `OverlayState.zoom`: Current zoom factor.
*   `OverlayState.qpane_rect`: Widget-space bounds of the viewer.
*   `OverlayState.physical_viewport_rect`: Device-pixel viewport bounds (useful for pixel-perfect guides).
*   `OverlayState.transform`: Image-to-widget transform for anchoring to image coordinates.
*   `OverlayState.current_pan`: Current pan offset in widget space.
*   `OverlayState.source_image`: The resolved image used for the current render pass.

```python
from PySide6.QtCore import Qt

def draw_hud(painter, state):
    # 'state.qpane_rect' is the viewport in widget pixels.
    # We use it to anchor text to the top-left corner.
    rect = state.qpane_rect.adjusted(10, 10, -10, -10)
    painter.setPen(Qt.yellow)
    painter.drawText(rect, Qt.AlignTop | Qt.AlignLeft, f"Zoom: {state.zoom:.2f}x")

# Add it to the stack
viewer.registerOverlay("zoom_hud", draw_hud)

# Trigger a repaint so it shows up immediately
viewer.update()
```

> **Pro Tip:** Registration order determines draw order. If you register "grid" then "labels", the labels will draw on top of the grid.

Overlay hooks may be suspended during navigation or activation workflows so visual layers stay consistent with the active image. Use `QPane.overlaysSuspended()` and `QPane.overlaysResumePending()` to inspect that state, and `QPane.resumeOverlays()`, `QPane.resumeOverlaysAndUpdate()`, or `QPane.maybeResumeOverlays()` when your host needs to coordinate overlay lifecycles with its own async work. If you need to introspect what is registered, `QPane.contentOverlays()` returns the current overlay map.

## 2. Cursors: Context-Aware Feedback
Static cursors are boring. QPane uses **Cursor Providers**-functions that decide what the cursor should look like based on the current state. This lets you show a "forbidden" sign when hovering over invalid areas or a "crosshair" only when a specific key is held.

*   **Register:** `QPane.registerCursorProvider(mode, provider)`
*   **Remove:** `QPane.unregisterCursorProvider(mode)`

The provider function is called whenever the mouse moves or state changes. If it returns `None`, QPane falls back to the default cursor for that mode.

```python
from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor

def smart_cursor(qpane):
    # Show a crosshair only if we are zoomed in past 100%
    if qpane.currentZoom() > 1.0:
        return QCursor(Qt.CrossCursor)
    return None  # Fallback to standard arrow

viewer.registerCursorProvider("inspect_mode", smart_cursor)
```

## 3. Custom Tools: Take Control
Tools define how the viewer responds to input. While QPane comes with `panzoom`, `cursor`, and `brush` modes, you can register your own to handle clicks, drags, and key presses exactly how you want.

*   **Register:** `QPane.registerTool(mode, factory, *, on_connect=None, on_disconnect=None)`
*   **Remove:** `QPane.unregisterTool(mode)`

A "Tool" is just a class that receives events (like `mousePressEvent`). You register a `factory` (a function that creates your tool) so QPane can spin it up when the mode activates.

> **Heads-up:** You cannot unregister the currently active tool. Always switch the viewer to a safe mode (like `QPane.CONTROL_MODE_PANZOOM`) before removing your custom tool.

### Tool Lifecycle & Events
Tool entry points live on `ExtensionTool`. Override these to handle input:

*   **Lifecycle:** `ExtensionTool.activate`, `ExtensionTool.deactivate`
*   **Mouse:** `ExtensionTool.mousePressEvent`, `ExtensionTool.mouseMoveEvent`, `ExtensionTool.mouseReleaseEvent`, `ExtensionTool.mouseDoubleClickEvent`
*   **Wheel:** `ExtensionTool.wheelEvent`
*   **Hover:** `ExtensionTool.enterEvent`, `ExtensionTool.leaveEvent`
*   **Keyboard:** `ExtensionTool.keyPressEvent`, `ExtensionTool.keyReleaseEvent`
*   **Rendering:** `ExtensionTool.draw_overlay`, `ExtensionTool.getCursor`

`ExtensionTool` ships with concrete no-op implementations for every handler, so you only need to override the events you care about. Overrides must follow the expected signatures exactly.

### Tool Signals
Emit requests through `ExtensionTool.signals` (an `ExtensionToolSignals` instance) to control the viewer:

*   **Navigation:** `ExtensionToolSignals.pan_requested`, `ExtensionToolSignals.zoom_requested`
*   **System:** `ExtensionToolSignals.repaint_overlay_requested`, `ExtensionToolSignals.cursor_update_requested`

## Putting it Together: The "Lens" Tool
Let's build a "Lens" feature: a custom mode where the mouse drives a magnifying glass overlay. This combines all three extension points.

```python
from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QCursor, QColor
from qpane import ExtensionTool

# 1. The Tool: Tracks mouse position
# Inherit from ExtensionTool and override only the events you need.
class LensTool(ExtensionTool):
    def __init__(self, qpane):
        super().__init__()  # Initialize signal bus
        self.qpane = qpane

    def mouseMoveEvent(self, event):
        self.qpane.update()  # Request redraw so the lens follows the mouse

    # Optional overrides (base class already provides no-op handlers)
    def mousePressEvent(self, event): pass
    def mouseReleaseEvent(self, event): pass
    def wheelEvent(self, event): pass

# 2. The Overlay: Draws the circle
def draw_lens(painter, state):
    # Only draw when our mode is active
    if viewer.getControlMode() != "lens":
        return

    # Get the cursor position in widget coordinates
    cursor_pos = viewer.mapFromGlobal(QCursor.pos())
    
    # Draw a yellow circle at the mouse position
    painter.setPen(Qt.yellow)
    painter.drawEllipse(cursor_pos, 50, 50)

# 3. The Cursor: Hides the default pointer so the lens is clear
def lens_cursor(qpane):
    return QCursor(Qt.BlankCursor)

# 4. Wire it up
# Note: The factory must be a callable that returns the tool instance.
viewer.registerTool("lens", lambda: LensTool(viewer))
viewer.registerOverlay("lens_visual", draw_lens)
viewer.registerCursorProvider("lens", lens_cursor)

# Activate!
viewer.setControlMode("lens")
```

Tool actions are requested by emitting signals on `self.signals`. For example, emit
`repaint_overlay_requested` to force an overlay redraw or `pan_requested`/`zoom_requested`
to ask QPane to move the viewport.
The signal hub is `ExtensionToolSignals`, exposed as `self.signals` on every `ExtensionTool`.

> **Try it Live:** The QPane demo includes a playground for these hooks. Run `python examples/demo.py` and check out the **Hooks** menu.
>
> *Note: The demo playground uses a simplified helper to let you edit just the drawing logic live. When building a permanent tool, use the full `ExtensionTool` structure shown above.*

## Rules of the Road
To keep your extensions playing nicely with the rest of QPane:

1.  **Unregister Safely:** `unregisterOverlay` and friends are idempotent—they won't crash if the item is already gone. It's safe to call them in your cleanup code even if you aren't sure they were registered.
2.  **Watch Your Coordinates:**
    *   **Painters** in overlays use **Widget Space** (physical pixels).
    *   **`state.qpane_rect`** is in **Widget Space**.
    *   Use `qpane.panelHitTest(pos)` if you need to convert mouse clicks to image pixels.
3.  **Performance Matters:** Overlays run on the main render loop. Keep your `draw_fn` fast—avoid loading files or heavy math inside the draw call.

## Related Docs
*   [Interaction Modes](interaction-modes.md): Learn about the built-in tools you can switch to.
*   [Catalog and Navigation](catalog-and-navigation.md): Understand how your tools interact with image loading.

**Continue →** [API Reference](api-reference.md)
