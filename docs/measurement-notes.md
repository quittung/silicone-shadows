# Compare page: measurement prototype

A first local prototype now replaces Fit all with Measure. Entering creates a
single world-space line immediately; there is no first-point/second-point flow.
Two 44px handles sit below and outward from the endpoints, connected by dashed
stems. Dragging adjusts the endpoints; arrow keys move by one screen pixel
(Shift: ten). A live readout in the top Measure panel uses the chosen metric/imperial units.

The sidebar is inert and outlines stay fixed until Done or Escape. Canvas
panning and wheel/pinch zoom remain available; the ruler stays in world space.
Mobile controls temporarily collapse. Exiting discards the ruler and restores
normal interaction. Nothing is persisted. Evaluate offset comfort on a real
phone; the initial layout has only been checked in a mobile-sized browser.

The earlier discussion below is retained as design context, not a to-do list.

## Purpose

Measure miscellaneous dimensions with a straight line between two points on
the scaled outline. Use the current metric/imperial units. This measures the
silhouette, not circumference or depth; source perspective limits accuracy.

## Possible integration

- Replace **Fit all** with **Measure** to avoid expanding the permanent UI.
  Consider the loss of Fit all as a way to recover an offscreen comparison.
- Enter an explicit, dedicated measurement mode. Hide orientation handles and
  freeze outline movement, panning, and zooming; frame the view before entering.
- Show the mode and a **Done** exit directly on the canvas, including when the
  mobile controls panel is collapsed. Keep outlines fully visible.
- Start with one measurement: place two endpoints, then adjust them by dragging.
  On exit, clear the measurement and restore normal interaction and selection.

## Unresolved interaction requirements

**The current step must be unmistakable.** Users must never have to guess
whether they have placed the first point, are choosing the second, or are
starting over. Clearly distinguish these states, show placed points, and make
correction and restart explicit. Candidate prompts are “Place the first point,”
“Place the second point,” and “Drag either endpoint to adjust.” The exact flow
and restart control still need design work; do not rely on prompts alone.

**Touch placement needs a precision strategy.** A finger both obscures the
target and places it imprecisely. Generous endpoint hit targets help adjustment
but do not solve precise positioning. Explore an offset crosshair or magnified
view only if needed, and validate placement and correction on a phone before
settling the interaction.

The main concern is adding clutter and ambiguous interaction state to a lean
comparison tool. Revisit measurement separately from mirroring, with these
questions resolved before implementation.
