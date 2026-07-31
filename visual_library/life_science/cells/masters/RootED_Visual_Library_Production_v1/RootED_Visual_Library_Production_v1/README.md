# RootED Master Cell Library — Production Package v1.0

This package starts the production implementation of the RootED Visual Library.

## Included

- Approved original Master Cell Library reference
- Isolated plant-cell and animal-cell reference crops
- Transparent SVG interaction overlays
- Stable component ID index
- Accessibility metadata
- Asset manifest and checksums
- Overlay specification
- Validation script

## Important status

The PNG artwork is the approved MVP visual reference.
The SVG files are transparent interaction overlays, not replacement artwork.
The current overlay geometry is a production candidate and must be checked in the RootED frontend before production lock.

## Integration pattern

Render the PNG and SVG in the same positioned container:

```html
<div class="rooted-visual">
  <img src="plant_cell_reference_v1.png" alt="" />
  <object data="plant_cell_overlay_v1.svg" type="image/svg+xml"></object>
</div>
```

Both layers must use the same aspect ratio and occupy the same bounds.
