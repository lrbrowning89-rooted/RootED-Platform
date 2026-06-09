# Model Asset System Phase 1

## Purpose

Phase 1 introduces a reusable model asset registry without changing existing question behavior, adaptive progression, Rolling-7 logic, or student question rendering.

The current platform can already store a question-level `model_id` in `question_metadata`. Phase 1 adds the `model_assets` table so those identifiers can resolve to reusable diagrams, microscope images, investigation tables, and graphs in a future rendering phase.

## Data Model

`model_assets` is the source of truth for reusable visual and structured evidence assets.

Fields:

- `model_id`: stable primary key used by questions through `question_metadata.model_id`
- `asset_type`: asset category such as `diagram`, `microscope_image`, `investigation_table`, or `graph`
- `title`: short internal or display title
- `description`: author-facing description of what the asset represents
- `file_path`: static file path for image-backed assets
- `alt_text`: accessibility text for rendered visual assets
- `caption`: optional student-facing caption
- `source`: provenance or generation/source note
- `created_at`: integer timestamp

## Relationship To Questions

Questions remain text-first and continue to live in the existing `questions` table.

Question metadata may include:

```text
question_metadata.model_id -> model_assets.model_id
```

This keeps model assets reusable. Multiple questions may point to the same `model_id`, and a question may continue to have `model_id = NULL` when no asset is needed.

Phase 1 does not add a foreign key from `question_metadata` to `model_assets` because existing imports may reference model IDs before the corresponding reusable assets are loaded. Validation can enforce this later when the asset library is populated.

## Future Image And Diagram Support

For diagrams and microscope images, `model_assets.file_path` should point to a file under a dedicated static asset folder, such as:

```text
app_core/static/model_assets/
```

Recommended `asset_type` values:

- `diagram`
- `microscope_image`

Future rendering can load the question's `model_id`, resolve the matching `model_assets` row, and display the image with `alt_text` and optional `caption`.

## Future Investigation Table Support

Investigation tables should be stored as structured data rather than screenshots when possible.

Phase 1 does not add a table payload field yet. A future phase should add either:

- `structured_payload_json` to `model_assets`, or
- a companion table such as `model_asset_payloads`

The renderer can then turn rows and columns into accessible HTML tables.

## Future Graph Support

Graphs should also prefer structured data over static images when possible.

A future graph payload should include:

- graph type
- x-axis label
- y-axis label
- data series
- units
- caption and alt text

Static graph images can still use `file_path` for MVP cases, but structured graph payloads will be easier to validate, remediate, and render accessibly.

## Phase 1 Boundaries

Phase 1 intentionally does not:

- render assets in the student UI
- change question selection
- change adaptive progression
- change Rolling-7 behavior
- change graph routing
- modify existing question/content data
