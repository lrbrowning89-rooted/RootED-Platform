# RootED SVG Interaction Overlay Specification v1.0

## Purpose

An interaction overlay is transparent SVG geometry aligned to an approved raster visual.
It enables selection, highlighting, labels, callouts, accessibility, and event handling without redrawing the illustration.

## Required rules

1. The SVG `viewBox` must match the pixel dimensions of the associated reference image.
2. Every instructional structure must have a stable lowercase `snake_case` ID.
3. Repeated structures use zero-padded suffixes such as `mitochondrion_01`.
4. Released IDs are permanent API contracts. Never rename or reuse them.
5. Geometry is invisible by default and must not alter the approved artwork.
6. The overlay must include an accessible `<title>` and `<desc>`.
7. Each hit region must use `class="hit-region"` and `data-label`.
8. Label and callout anchors use `anchor_<structure_id>`.
9. Artwork and overlay versions are tracked independently.
10. Overlay geometry requires visual-alignment QA at intended responsive sizes.

## Runtime behavior

The frontend may toggle:

- `.is-highlighted`
- `.is-selected`
- `.debug`

The overlay should be positioned absolutely over the raster image with identical width, height, and aspect ratio.
