# RootED Model Asset Style Guide

## 1. Canvas Sizes

Use a consistent SVG viewBox so assets scale cleanly.

Primary:

```text
viewBox="0 0 900 520"
```

Use for most question diagrams.

Compact:

```text
viewBox="0 0 720 420"
```

Use for simple single-cell or single-system diagrams.

Wide process:

```text
viewBox="0 0 1000 520"
```

Use only when showing left-to-right flows such as oxygen or nutrient movement.

## 2. Font Family

Use system-safe sans serif fonts:

```text
Arial, Helvetica, sans-serif
```

Avoid decorative, handwritten, or condensed fonts.

## 3. Font Sizes

Recommended sizes:

```text
Title: 28-32px
Major labels: 20-22px
Secondary labels: 16-18px
Small annotations: 14-16px
```

Minimum readable text size: `14px`.

## 4. Stroke Widths

Use strong but not heavy outlines:

```text
Main structure outlines: 3px
Secondary outlines: 2px
Internal details: 1.5px
Arrows: 3px
Callout lines: 2px
```

Avoid hairline strokes below `1.5px`.

## 5. Color Palette

Use high-contrast, printer-friendly colors.

Recommended palette:

```text
Text: #1F2937
Main outline: #374151
Background: #FFFFFF
Panel fill: #F8FAFC
Cell/organ fill: #EAF4EA
Highlight green: #4F8A5B
Highlight blue: #3B82A6
Highlight gold: #D9A441
Highlight purple: #7C6AA6
Warning/contrast red: #B85C5C
Muted gray: #6B7280
```

Do not rely on color alone. Pair color with labels, arrows, patterns, or shape differences.

## 6. Arrow Styles

Use simple, consistent arrows.

```text
Arrow stroke: #374151
Arrow width: 3px
Arrowhead: filled triangle
Dashed arrows: only for "signal," "control," or "indirect" relationships
Solid arrows: movement of matter, energy, or direct sequence
```

Avoid crowded arrow networks. If more than 5 arrows are needed, simplify or split into another asset.

## 7. Label Placement

Place labels outside structures when possible, connected by short callout lines.

Rules:

- Avoid labels crossing arrows.
- Avoid labels inside small structures unless there is enough space.
- Keep related labels aligned in columns.
- Put the most important labels closest to the structure.
- Do not cover diagram evidence with labels.
- Use sentence case, not all caps.

## 8. Accessibility

Each SVG should support:

- A clear `<title>` matching the asset title.
- A concise `<desc>` explaining the model for screen readers.
- Meaningful `alt_text` in `model_assets`.
- High contrast between text and background.
- No color-only meaning.
- Readable labels at small screen sizes.
- Simple visual hierarchy for students with processing or attention needs.

Avoid:

- Tiny labels
- Dense legends
- Decorative textures
- Ambiguous color coding
- Unlabeled symbols

## 9. Middle School NGSS Complexity

Diagrams should show the minimum model needed to answer the question.

For grades 6-8:

- Prefer 3-6 labeled parts.
- Prefer one core relationship per diagram.
- Use familiar shapes before realistic detail.
- Show evidence clearly: arrows, grouping, labels, sequence, comparison.
- Avoid microscope-level realism unless the question requires it.
- Avoid advanced vocabulary not present in the question or objective.

A good RootED model should let a student say: "I know what I'm supposed to notice."

## 10. SVG Conventions

Each SVG should include:

```text
<title>...</title>
<desc>...</desc>
```

Use organized groups:

```text
g id="background"
g id="main-model"
g id="labels"
g id="arrows"
g id="callouts"
```

Use descriptive ids:

```text
id="cell-membrane"
id="nucleus"
id="oxygen-arrow"
id="digestive-system-label"
```

Avoid:

- Embedded raster images
- Inline scripts
- External fonts
- Animation
- CSS that depends on app styles
- Unnamed groups from design tools when possible

## 11. File Naming

Use lowercase kebab-case.

Format:

```text
<objective-id-lowercase>-<concept-slug>-<nn>.svg
```

Examples:

```text
ms-ls1-2a-cell-membrane-boundary-01.svg
ms-ls1-3b-circulatory-system-heart-vessels-01.svg
ms-ls1-3c-nervous-muscular-skeletal-interaction-01.svg
```

Store under:

```text
app_core/static/model_assets/
```

Database `src` should be:

```text
model_assets/<filename>.svg
```

## 12. Consistency Rules

Across all future assets:

- Use the same canvas proportions unless a diagram truly needs a different layout.
- Use the same palette and font scale.
- Use consistent arrow meanings.
- Keep labels direct and student-friendly.
- Prefer reusable visual metaphors: arrows for flow, grouping boxes for levels, highlighted outlines for focus.
- Keep asset titles aligned with `model_assets.title`.
- Ensure `alt_text`, SVG `<title>`, and SVG `<desc>` agree.
- Do not add decorative elements that do not support the science idea.

## Example Diagram Descriptions

### Example 1: Cell Membrane Boundary

A simple cell sits centered on a white canvas. The cell membrane is highlighted with a green outline. A callout label points to the outer boundary: "Cell membrane." The nucleus and cytoplasm are shown in muted colors but not emphasized. The model focuses attention on the membrane as the cell's boundary.

### Example 2: Digestive System Organs

A simplified torso outline shows the stomach, small intestine, and large intestine highlighted in blue. A title reads "Digestive System." Short labels point to each organ. One solid arrow shows food moving through the system. The diagram avoids unrelated organs so students focus on digestion and nutrient absorption.

### Example 3: Oxygen Moving From Lungs to Cells

A left-to-right process model shows lungs, blood vessels, and body cells. A solid blue arrow moves from lungs to blood, then from blood to body cells. Labels identify "lungs," "blood," and "body cells." A short caption states that oxygen moves through interacting body systems.
