# RootED Model Asset Library

This folder is the working library for images and models before they are published into the app.

The live app serves approved assets from:

```text
app_core/static/model_assets/
```

Keep that production folder stable. Do not move a file out of `app_core/static/model_assets/` after a `model_assets.src` row points to it unless the database row is updated at the same time.

## Folder Pattern

Each objective folder uses the same workflow:

```text
01_references/  Real photos, microscope images, source links, citation notes, and inspiration.
02_source/      Editable originals such as layered design files, raw exports, or source captures.
03_drafts/      In-progress visuals that are not ready for student use.
04_review/      Candidate images packaged for instructional review.
05_approved/    Teacher-approved masters ready to publish.
06_exports/     Final app-ready PNG/SVG files using the exact catalog filename.
```

The current objective folders are:

```text
ms-ls1-1b/
ms-ls1-2a/
ms-ls1-2b/
ms-ls1-3a/
ms-ls1-3b/
ms-ls1-3c/
```

## Publishing Rule

When an asset is approved:

1. Put the reviewed master in `05_approved/`.
2. Put the app-ready file in `06_exports/`.
3. Copy the app-ready file into `app_core/static/model_assets/`.
4. Confirm the filename matches the `model_assets.src` catalog value.
5. Run `tools/audit_model_assets.py`.

## Naming

Use the production filename from the catalog whenever possible.

Examples:

```text
ms-ls1-1b-leaf-cells-01.png
ms-ls1-2a-cell-membrane-boundary-01.svg
ms-ls1-3b-circulatory-system-heart-vessels-01.png
```

Drafts may add a suffix:

```text
ms-ls1-1b-leaf-cells-01-draft-a.png
ms-ls1-1b-leaf-cells-01-review-2026-07-31.png
```

Do not publish drafts or placeholders into the app.

## Asset Quality

Follow `docs/model_asset_style_guide.md`.

A RootED model asset must be something a teacher would feel comfortable projecting during instruction. Prefer real photos, microscope images, or high-quality scientific illustrations when they teach the concept better than a simplified diagram.

## Shared Materials

Use `_shared/` for references, source notes, palettes, and reusable materials that apply across multiple objectives. Keep objective-specific material inside the matching objective folder.
