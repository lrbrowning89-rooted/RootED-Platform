# RootED Canonical Master Cell Library

## Production record

| Field | Value |
|---|---|
| Asset version | 1.0.0 |
| Schema version | 1.0.0 |
| Created | 2026-07-29 |
| Last modified | 2026-07-29 |
| Production status | Candidate — awaiting production approval |
| Canonical sources | `source/master_plant_cell.svg`, `source/master_animal_cell.svg` |
| Approval reference | `../masters/RootED_Master_Cell_Library_v1.png` |
| Derived assets | None approved. PNGs in `exports/` are canonical-source previews only. |

## Purpose

This package is the reference implementation for future RootED canonical visual assets. It establishes a reuse-first production model: one editable, machine-addressable canonical source supports many instructional derivatives without redrawing.

The flattened approval-board PNG is retained as a visual reference and approval record. It is not a production source.

## Reference architecture

Every canonical SVG must use three top-level content groups:

1. `instructional` — scientifically or instructionally meaningful structures.
2. `annotations` — optional labels, arrows, and callouts.
3. `presentation` and/or `presentation_foreground` — background, texture, shadows, highlights, and other non-instructional effects.

Presentation effects must never be the sole carrier of instructional meaning. Annotations must be independently removable. Instructional structures must remain selectable and highlightable without editing presentation layers.

## Permanent ID contract

IDs within `instructional` are public API identifiers. After production approval:

- IDs must not be renamed or repurposed.
- Removal or renaming is a breaking schema change.
- New IDs may be added in a backward-compatible minor version.
- Aliases and migrations must be documented if a future breaking change is unavoidable.
- Repeated structures use a stable plural parent and stable singular children, such as `mitochondria` / `mitochondrion_01`.

Generic editor-generated identifiers are prohibited.

## Highlighting contract

Every instructional structure carries the `structure` class. The platform may:

- add `rooted-highlight` to a target;
- add `rooted-muted` to non-target structures;
- hide a structure with `display:none`;
- change opacity without modifying neighboring structures.

Color is not the only available highlight channel. The included highlight style combines a yellow glow with a dark strengthened outline. Platform derivatives may substitute an outline, halo, pattern, callout, or motion treatment.

## Accessibility rules

- Structure boundaries use strengthened outlines suitable for classroom projection.
- Neighboring structures differ through value, outline, and shape—not hue alone.
- The artwork remains interpretable when desaturated.
- SVG titles, descriptions, roles, and accessible labels identify major structures.
- Annotation text uses a scalable sans-serif font, high contrast, and a white paint-order halo.
- Labels are optional and do not alter the canonical instructional geometry.
- Accessibility descriptions in metadata are the source for future screen-reader and alternate-format work.

## Versioning

Semantic versioning applies independently to:

- **Asset version:** changes to visual geometry, structure inventory, or approved appearance.
- **Schema version:** changes to metadata fields, ID contracts, or package architecture.

Production status values are `draft`, `candidate`, `approved`, `deprecated`, and `retired`.

## Derivative workflow

All `CELL_MODEL_*` assets must be generated exclusively from these SVG sources after the masters receive production approval. A flattened PNG export must never be used as a derivative source.

Recommended workflow:

1. Load the canonical SVG.
2. Keep permanent IDs unchanged.
3. Apply derivative state through classes, opacity, visibility, overlays, and annotation content.
4. Record the derivative ID in metadata.
5. Validate browser rendering, accessibility, and instructional integrity.
6. Export presentation formats only after SVG validation.

## Controlled reconstruction notes

The approved appearance was reconstructed from a flattened 1536×1024 reference. Fine cytoplasm texture, small ribosome/vesicle placement, hidden background geometry, gradient parameters, shadows, and tiny internal organelle markings are controlled approximations. No new scientific structures were introduced.

## Package contents

- `source/` — canonical SVG candidates.
- `exports/` — rendered PNG previews, including optional labeled views.
- `metadata/` — automation-ready asset and structure records.
- `validation/` — browser harness and validation results.

