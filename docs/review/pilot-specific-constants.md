# Pilot-specific constants inventory (2026-10-08)

Status: inventory only. Nothing below has been moved. Each entry is hard-coded for the four-item Angle Valves pilot (Mueller 213030/245747, Ford 225830/221315) or the gap-closing work that followed it, and will not generalize to about 1,400 vendors or other categories.

The proposed replacement direction is the same throughout. Keep rules as versioned data, keyed by category/hierarchy node and by vendor or vendor file. The code should hold only generic mechanisms: matchers, derivation executors and grounding checks. Line numbers refer to `main` after PR A and PR B.

## Worker and run harness

| Location | Constant | What it does | Proposed data-driven replacement |
|---|---|---|---|
| `backend/quality_worker.py:39-40` | `FORD_PDF_SHA256`, `FORD_PDF_BLOB` | Identifies the one Ford catalog PDF that gets a rendered page image. | A per-source binding flag `render_page_image: [pages]` set at batch import. The image is chosen per source, not by a global hash. |
| `backend/quality_worker.py:241-280` | `render_ford_page`, Ford image messages | Renders page 1 of that PDF only. | A generic `render_source_pages(binding)` driven by the same binding flag. |
| `backend/quality_worker.py:498-499` | `has_ford` gate | Image input only when the Ford hash is in the batch. | Use the per-binding flag above. |
| `backend/quality_worker.py:41` | `SMOKE_ATTRIBUTE = "Operating Head Style"` | Live canary target. | A run config `canary: {item_key, attribute_id, source_cell, expected}` stored with the batch. |
| `backend/quality_worker.py:284-360` | Mueller 213030 smoke: row `1096`, cell `T1096`, expected `Lockwing` | Model canary and its expected answer. | Same canary config. It is optional and not limited to Mueller. |
| `backend/quality_worker.py:474`, `:481` | Mueller textless-PDF sha `51bd50b0…` and `PIMITEM-213030` | OCR verification smoke. | A run config `ocr_verification: {binding_id}`. |
| `backend/quality_worker.py:97` | `"METER CONNX SIZE"` dimension-schedule marker | Suppresses an unkeyed family dimension schedule in drawings. | A per-vendor/document-template `variant_schedule_headers` list. |
| `backend/quality_worker.py:107` | `DRFTR\|CHKR\|ENGR\|THIRD ANGLE PROJECTION…` | Detects a Mueller-style drawing title block. | A per-template `title_block_markers` list with a generic default. |
| `backend/quality_web.py:35-38` | `MANUFACTURERS = {"ford": …, "mueller": …}` | Approved manufacturer hosts for web/tool search. | A vendor master table `vendor → approved_hosts` (about 1,400 rows), loaded per batch. |
| `backend/pdf_presentation.py:11-17` | `_HEADERS` (includes `meter connx size`, `approx wt lbs`, `selected submitted items`) | Recognizes pure table headers in drawings/submittals. | A per-category header lexicon with the generic headers as default. |

## Prompt and derivation rules (category-specific wording in `EVIDENCE_RULES` / tasks)

| Location | Rule | Proposed replacement |
|---|---|---|
| `backend/quality_pipeline.py:38-42` `NONFLANGED_OUTLET_RULE` and `_OUTLET_MECHANISM` / `_nonflanged_outlet` (`:290-300`, `:358`, `:390`, `:443`) | `nonflanged_outlet_mechanism_v1`: Flanged Outlet=False from a stated saddle-meter-swivel-nut / FIP / MIP outlet. **This is the only prompt text that still names a reference-comparison attribute** (allowed and asserted by `tests/test_gap_pass_second_look.py`). | A per-category derivation pack entry `{attribute, rule_id, premise_patterns, value}`. The prompt is rendered from the pack, and grounding enforces the same entry. |
| `backend/quality_pipeline.py:54-56`, `:243-245` `_LEAD` | Lead-Free inference from LLB, NL/-NL, NSF/ANSI 372, AB1953. | Category pack `inference_markers` for Lead-Free, kept with its negation patterns. |
| `backend/quality_pipeline.py:56-57`, `:361-362` | Locking Feature from `LOCKWING`/"for locking"; Padlock Wing from "padlock wing for locking". | Category pack `inference_markers` per Boolean attribute. |
| `backend/quality_pipeline.py:62-64`, `:314-316` | `connection_material_v1` (FIP/MIP to Iron pipe; copper service/flare/compression to Copper). | Category pack `derivation` entries. |
| `backend/quality_pipeline.py:65-69`, `:317-323` | `brass_plus_nl_identification_v1` (AWWA C800 potable-water brass paragraph + NL main-body paragraph to No-lead brass). | Category/vendor pack `derivation` entry with its paired-premise patterns. |
| `backend/quality_pipeline.py:274-280` | Abbreviation map (`llb`, `epdm`, …). | A category glossary table. |
| `backend/quality_pipeline.py:101-102` `REFINE_TASK` | Names Manufacturer title blocks and Primary Material brass/NL paragraphs as re-ask targets. | Render the re-ask hints from the category pack, or drop them once the pack exists. |
| `backend/quality_pipeline.py:116` `JUDGE_TASK` | Refers to the two named derivations. | Render from the same pack. |
| `backend/quality_tool_loop.py:376-378` (removed in PR B) | Priority list "Port Type; Material Standard; Compatible Meter Size; Flanged Outlet". | Removed. Order now comes only from the pending definitions. |

## Not pilot-specific (kept)

- **Untrusted-evidence, citation, normalization and definition rules** in `SHARED_SYSTEM` / `DEFINITION_RULES`.
- **Applicability statuses** (`exact`, `family-confirmed`, `family-unconfirmed`) and their reviewer questions.
