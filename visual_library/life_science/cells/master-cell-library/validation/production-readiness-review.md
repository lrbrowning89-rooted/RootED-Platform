# RootED Production Readiness Review

**Review date:** 2026-07-29  
**Assets:** `rooted_master_plant_cell` 1.0.0 candidate; `rooted_master_animal_cell` 1.0.0 candidate  
**Recommendation:** Approved with minor revisions

## Executive finding

The masters are scientifically appropriate for middle-school instruction, visually coherent, technically functional, and capable of supporting all ten planned derivatives. No redesign or major reconstruction is recommended.

Four bounded revisions should be completed before production lock:

1. Isolate membrane geometry from interior cytoplasm-colored fill so `cell_membrane` represents only the membrane.
2. Declare repeated child IDs in metadata and explicitly classify each as permanent or internal.
3. Complete the labeled exports or rename them as partial-label examples.
4. Add an explicit schematic/not-to-scale statement and a non-color distinction for lysosomes versus vesicles.

## 1. Scientific accuracy

### Strengths

- Both cells contain the expected middle-school structures.
- Plant-specific wall, chloroplasts, and large central vacuole are clear.
- Animal-specific lysosomes and centrioles are present without dominating the model.
- The central vacuole is appropriately prominent.
- Nuclei are emphasized at an instructionally useful scale.
- Chloroplast and mitochondrion interiors are simplified without introducing biochemical pathways.
- Relative placement is plausible and does not imply fixed anatomical coordinates.

### Required minor corrections

- Add a metadata/README statement that the cells are schematic and not to scale and that real cell shapes and organelle counts vary.
- Strengthen the non-color distinction between lysosomes and vesicles. Their circular silhouettes are currently similar; a subtle internal mark or boundary convention would reduce ambiguity in desaturated use.

### Accepted simplifications

- Organelle scale is enlarged for recognition.
- The animal cell is round and the plant cell box-like as representative teaching forms, not universal morphology.
- The ER-to-nuclear-envelope relationship is simplified.
- Ribosomes and vesicles are illustrative rather than count-accurate.

## 2. Instructional design

### Strengths

- Strong figure-ground separation and restrained backgrounds.
- Large silhouettes and reinforced boundaries project clearly.
- Plant wall, central vacuole, nuclei, chloroplasts, mitochondria, and Golgi stacks are immediately recognizable.
- Muting and highlighting produce a clear attention hierarchy.
- Optional annotations do not alter canonical instructional geometry.
- The palette is muted enough to support overlays without becoming dull.

### Required minor correction

- Current labeled exports identify only a subset of major structures. Either complete the label set expected of a canonical labeled master or rename these exports to indicate that they are partial-label demonstrations.

### Enhancement

- Define minimum label size, leader-line clearance, safe-area, and maximum simultaneous-label density as reusable RootED annotation tokens.

## 3. Visual consistency

### Strengths

- Shared top-left lighting direction.
- Consistent pale blue cytoplasm, purple nuclei, orange mitochondria, blue vesicles, and pink Golgi apparatus.
- Compatible texture density, rounded rendering language, shadow softness, and projection-oriented outlines.
- Cell-specific shapes differ appropriately without appearing to come from separate illustration systems.

### Standardization opportunity

- Publish reusable stroke-width, shadow, glow, texture-opacity, and annotation-style tokens. Current values are visually compatible but embedded independently in each SVG.

## 4. Derivative readiness

| Model | Status | Notes |
|---|---|---|
| CELL_MODEL_01 | Ready after membrane isolation | Shared membrane ID exists; geometry should represent only the membrane. |
| CELL_MODEL_02 | Ready | Nucleus and nucleolus are independently addressable. |
| CELL_MODEL_03 | Ready | Cytoplasm is an independent fill target. |
| CELL_MODEL_04 | Ready | Nucleus, rough ER/ribosomes, and Golgi are independently targetable; arrows belong in annotations. |
| CELL_MODEL_05 | Ready | Membrane can be emphasized and absence callouts added without ghost plant structures. |
| CELL_MODEL_06 | Ready | Wall, chloroplasts, and vacuole have separate stable groups. |
| CELL_MODEL_07 | Ready | Multiple functional groups can be highlighted concurrently without a forced sequence. |
| CELL_MODEL_08 | Ready | Nucleus target plus annotation arrows supports middle-school coordination language. |
| CELL_MODEL_09 | Ready after membrane isolation | Wall and membrane can already be manipulated separately; semantic membrane geometry should be tightened. |
| CELL_MODEL_10 | Ready | Chloroplast and mitochondria parent groups support the approved conceptual relationship. |

No derivative requires redrawing or new organelle reconstruction.

## 5. Long-term platform fitness

### Ready capabilities

- Instructional highlighting and muting
- Teacher presentation states
- Student structure selection
- Optional annotations
- Desaturated and high-contrast variants
- Group and individual organelle targeting
- Opacity, visibility, transform, and path-based animation
- Future derivative generation

### Future enhancements

- Localization-ready annotation strings stored outside SVG markup
- Keyboard/focus state conventions for interactive exploration
- Optional bounding boxes or anchor points for automatic callout placement
- Reduced-motion animation rules
- Structure aliases and schema migrations for future breaking changes
- Shared design-token file rather than duplicated style constants

## 6. Architecture review

The separation of instructional structures, annotations, presentation effects, metadata, validation, and stable IDs is a strong reference architecture.

Before declaring the architecture universal:

- Document whether `presentation_foreground` is an approved second presentation stage or standardize explicit `presentation_back` and `presentation_front` groups.
- Add repeated-instance records such as `chloroplast_01` and `mitochondrion_01` to metadata.
- State whether each instance ID is permanent. Frontend code must not depend on IDs classified as internal.
- Isolate the full-area cytoplasm-colored fill currently carried by `cell_membrane`; membrane geometry should not duplicate cytoplasm semantics.

## 7. Behavior demonstrations

Generated demonstrations are stored in `validation/demonstrations/`.

- `01_nucleus_highlight_others_faded.png`
- `02_plant_wall_hidden_membrane_visible.png`
- `03_all_chloroplasts_highlighted.png`
- `04_chloroplast_01_highlighted.png`
- `05_labeled_instructional_export.png`

These prove:

- nucleus-only highlighting;
- independent fading of other instructional structures;
- wall removal without membrane removal;
- parent-group chloroplast highlighting;
- child-ID chloroplast highlighting;
- independent annotation activation and labeled export.

## 8. Final recommendation

**Approved with minor revisions.**

The revisions are bounded production-lock requirements, not grounds for redesign. Once completed and revalidated, promote both assets from `candidate` to `approved`, set `production_lock` to `true`, lock the documented permanent IDs, and begin the Cell Derivative Library.

