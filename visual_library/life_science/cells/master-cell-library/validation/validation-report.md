# RootED Master Cell Library validation report

Run: 2026-07-29T15:21:30-05:00

Passed: **28**  
Failed: **0**

| Asset | Check | Result | Detail |
|---|---|---:|---|
| plant | valid_xml | PASS | SVG parses as XML. |
| plant | unique_ids | PASS | All IDs are unique. |
| plant | group_instructional | PASS | Required group 'instructional' exists. |
| plant | group_annotations | PASS | Required group 'annotations' exists. |
| plant | group_presentation | PASS | Required group 'presentation' exists. |
| plant | metadata_ids_resolve | PASS | Every metadata structure ID resolves in the SVG. |
| plant | metadata_complete | PASS | Required structure metadata fields are present. |
| plant | independent_highlight_targets | PASS | Every declared highlight target has an independent ID and the scoped structure class. |
| plant | no_external_links | PASS | No external image or resource links detected. |
| plant | version_alignment | PASS | SVG and metadata versions align. |
| plant | accessibility | PASS | SVG has img role, title, and description. |
| animal | valid_xml | PASS | SVG parses as XML. |
| animal | unique_ids | PASS | All IDs are unique. |
| animal | group_instructional | PASS | Required group 'instructional' exists. |
| animal | group_annotations | PASS | Required group 'annotations' exists. |
| animal | group_presentation | PASS | Required group 'presentation' exists. |
| animal | metadata_ids_resolve | PASS | Every metadata structure ID resolves in the SVG. |
| animal | metadata_complete | PASS | Required structure metadata fields are present. |
| animal | independent_highlight_targets | PASS | Every declared highlight target has an independent ID and the scoped structure class. |
| animal | no_external_links | PASS | No external image or resource links detected. |
| animal | version_alignment | PASS | SVG and metadata versions align. |
| animal | accessibility | PASS | SVG has img role, title, and description. |
| plant | png_exports | PASS | Plain and labeled PNG previews rendered. |
| plant | desaturation_preview | PASS | Desaturated accessibility-review preview rendered; boundaries use value and outline separation. |
| plant | chromium_pixel_consistency | PASS | Sampled-pixel hashes: Edge=FBBD6C31A1A65A81882388D27537878107C47A3342A2460515C4CE8FE2F11BD6 Chrome=FBBD6C31A1A65A81882388D27537878107C47A3342A2460515C4CE8FE2F11BD6 |
| animal | png_exports | PASS | Plain and labeled PNG previews rendered. |
| animal | desaturation_preview | PASS | Desaturated accessibility-review preview rendered; boundaries use value and outline separation. |
| animal | chromium_pixel_consistency | PASS | Sampled-pixel hashes: Edge=73A5262FA2D9CE63B329A98DF476D121E101C05E40F1FA96024CDF1FB5E082BA Chrome=73A5262FA2D9CE63B329A98DF476D121E101C05E40F1FA96024CDF1FB5E082BA |
