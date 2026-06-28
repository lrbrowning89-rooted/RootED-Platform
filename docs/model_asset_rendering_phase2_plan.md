# Model Asset Rendering Phase 2 Plan

## Current Findings

- `question_metadata.model_id` is populated by production migration scripts for newer content packs.
- `question_metadata` is not part of `app_core/schema.sql` or the `ensure_schema()` bootstrap in `dashboard_v5.py`.
- No `model_assets` table definition is present in the live schema, migrations, or docs reviewed.
- The student question path selects a `current_question` in `student_view()` after objective and level selection, then renders the inline `student_html` template.
- The exact future resolution point is after `current_question = level_qrows[question_index]` and before `render_template_string(student_html, ...)`.

## Implementation Plan

Backend:
- Keep adaptive selection unchanged.
- After `current_question` is selected, resolve `question_metadata.model_id` for `current_question["question_id"]`.
- If `model_id` is present, look up the matching row in `model_assets`.
- Pass a separate `current_model_asset` payload into the student template.
- Do not include asset status in scoring, attempts, responses, Rolling-7, graph routing, or objective sequencing.

UI/template:
- Render assets between the question stem and answer choices.
- Use asset type to choose the renderer:
  - image: `<img>` with constrained width and required alt text.
  - diagram: initially render as image or static HTML/SVG only after an approved asset format exists.
  - table: render semantic `<table>` from structured data.
  - chart: render an accessible static image first, then optional interactive chart later.
- Keep the answer form and hidden fields unchanged.

Missing asset behavior:
- If there is no `model_id`, show only the question as it appears today.
- If `model_id` exists but `model_assets` is absent or has no matching row, show only the question as it appears today.
- Optionally log missing assets for teachers/admins later, but do not show student-facing warnings during delivery.

Accessibility:
- Require `alt_text` for image-like assets.
- If an asset is decorative or redundant, store an explicit empty alt text and mark it as such in metadata.
- For charts and tables, provide a concise text summary and preserve readable data in semantic markup.
- Ensure asset content does not replace the question stem; it should supplement the prompt.

Future support:
- Define `model_assets` with stable fields before rendering:
  - `model_id` primary key
  - `asset_type`
  - `src` or structured content field
  - `alt_text`
  - optional `title`, `caption`, `mime_type`, `data_json`, `created_at`, `updated_at`
- Add validation so imported questions with `model_id` can be audited against available assets.
- Add a teacher/admin report for missing or inaccessible model assets.
- Consider moving inline templates into real Jinja templates before expanding asset rendering complexity.

## Phase 2 Foundation Added

- Added non-rendering helper functions in `dashboard_v5.py`:
  - `table_exists()`
  - `table_columns()`
  - `get_question_model_id()`
  - `get_model_asset()`
  - `resolve_model_asset_for_question()`
- These helpers return missing-state payloads instead of raising when metadata or asset tables are absent.
- No route currently calls the resolver, so the student UI remains unchanged.
