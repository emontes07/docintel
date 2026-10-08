# Second opinion: DocIntel cost and speed review (2026-10-08)

Independent, read-only review. No code was changed, no runs were started, and no Azure or model endpoints were called. The packet measurements below come from re-serializing the saved Phase 3 Final A inputs offline with the repository's own deterministic packet builders.

Scope reviewed:

- **Backend:** `backend/quality_pipeline.py`, `quality_judge.py`, `quality_tool_loop.py`, `quality_tool_model.py`, `quality_cost.py`, `quality_web.py`, `quality_worker.py`, `quality_definitions.py`, `pdf_presentation.py`, `core/quality_model.py` and `core/vendor_tables.py`.
- **Operations:** `scripts/operations.sh`, `run.sh` and `infra/batch.bicep`.
- **Phase 3 evidence:** the report `output/phase3-quality-stability-report-20261008.md`, the run data in `output/private/phase3/final-a/*`, and the technical export `output/Angle-Valves-Phase3-technical-20261008.xlsx`, including the Diagnostics sheet.

---

## Bottom line

1. **Under 1% of the 586K input tokens per product is source text.** About 62% is the same definitions and per-evidence metadata, re-sent on every call:
   - the 24 attribute names repeated on every evidence entry;
   - the same qualification sentence repeated 81 times;
   - sha256 versions, timestamps, and five rule paragraphs repeated for each of the 24 definitions.

   Another 31% is a per-product, 15-step agentic tool loop that replays its own history.
2. **59% of Final A's $3.65 bought no web value.** The tool pass cost $1.70 and the per-product web pass inside the tier loop cost $0.46: $2.16 in total. The only gap-fill gains came from **re-reading local PDFs**: 3 populated slots, 2 of them judge-disputed.
3. **The design is per product, per tier and per phase, with no amortization.** At 25K–250K items the money is in families (50–2,000 items share documents) and in vendor files (about 1,400). Neither is exploited: each Mueller product carried its family PDF in 7–8 calls (both tiers' extract, refine and judge votes), and each vendor workbook is re-parsed for every product.
4. **Recommended end state: about $40–60 per 1,000 items (central case) and under about $150 per 1,000 (conservative).** This compares with about $720–790 per 1,000 for the current design at scale. Quality parity is kept by retaining deterministic grounding, applicability and the judge.
5. **There are hard scaling blockers today, independent of model cost:**
   - hard-coded $10 run and $40 overnight caps (about 11 products at the current cost);
   - a 600 s replica timeout (about 6 products per execution);
   - job parallelism of 1 and no resume of completed items;
   - all items' evidence loaded into memory at start;
   - one shared cost blob written twice per call;
   - a review workbook that cannot hold 6M review rows.

---

## 1. Measured token breakdown (Final A, `phase3-corrected-a-20261007`)

### Method

- **Calls:** the 59 billed model calls come from `output/private/phase3/final-a/runtime-records.json` (`usage/0001…0128.json`, 59 with `operation=model`).
  - They reconcile exactly with `metrics.json` (2,344,638 input tokens, $3.0831 model cost).
  - The technical export's Diagnostics sheet has 62 `Quality model call` rows for this run: the 59 billed calls plus 3 `stopped` budget closeouts that sent no request.
  - `usage-log.json` (the container-log capture) holds only 47 of the 59 calls, so it was **not** used. See section 4, scaling blocker 4.
- **Packet sections:** rebuilt offline with `product_packet` and `cached_packet_parts` from the saved `results.json`, the way `output/private/phase3/replay_tool_packets.py` does it. No model calls were made.
- **Character-to-token calibration** used provider-reported cached/cache-write prefix sizes:
  - definitions and system JSON: about 4.8 characters per token (tool prefix 14,895 and tool-judge prefix 13,776);
  - evidence JSON: about 3.3 characters per token (Mueller extract prefix 51,703, Mueller judge prefix 51,061, Ford extract prefix 26,159, Ford judge prefix 25,517).

  All six prefixes fit within about 1%.
- **Accuracy:** call, phase, tier and product totals are exact. Splits *inside* a prefix are calibrated estimates.

### 1.1 What the money bought

Prices are the logged basis: $2/M input, $0.20/M cached, $2.50/M cache write, $10/M output.

| Component | Tokens | Cost | Share of model cost |
|---|---:|---:|---:|
| Uncached input | 767,155 | $1.534 | 49.8% |
| Cache **writes** (2.5× input price) | 392,982 | $0.982 | 31.9% |
| Cached reads | 1,184,501 | $0.237 | 7.7% |
| Output, including 14,107 reasoning tokens | 32,944 | $0.329 | 10.7% |
| **Model total** | 2,344,638 input | **$3.083** | 100% |

Web search/browse added $0.538, worker time $0.012 and the run base $0.021, for **$3.653** in total.

Caching is still net-positive: cached reads saved about $2.13 against about $0.20 of write premium. But **$0.453 (15% of model cost) went on cache writes that were never read again**:

- 4 rewrites of already-written prefixes, 95,269 tokens (calls 35, 37, 65 and 94);
- 4 terminal-closeout writes, 85,931 tokens.

### 1.2 By phase and tier (exact)

| Phase | Tier | Calls | Input tokens | % input | Cached | Cache write | Output | Cost | % cost | Avg input per call |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Tool loop (steps) | – | 20 | 737,466 | 31.5% | 238,320 | 59,580 | 946 | $1.085 | 35.2% | 36,873 |
| Judge (pass 1) | vendor_table | 6 | 321,249 | 13.7% | 306,366 | 0 | 832 | $0.099 | 3.2% | 53,541 |
| Refine | internal_pdf | 4 | 309,318 | 13.2% | 104,021 | 51,703 | 2,023 | $0.478 | 15.5% | 77,329 |
| Refine | vendor_table | 4 | 203,001 | 8.7% | 155,724 | 0 | 2,877 | $0.155 | 5.0% | 50,750 |
| Judge (pass 1) | internal_pdf | 5 | 191,465 | 8.2% | 76,551 | 76,578 | 733 | $0.291 | 9.4% | 38,293 |
| Extract | internal_pdf | 4 | 176,230 | 7.5% | 77,862 | 77,862 | 7,524 | $0.327 | 10.6% | 44,057 |
| Extract | vendor_table | 4 | 165,662 | 7.1% | 155,724 | 0 | 6,195 | $0.113 | 3.7% | 41,415 |
| Judge (tool pass) | internal_pdf | 7 | 120,278 | 5.1% | 55,104 | 41,328 | 1,961 | $0.182 | 5.9% | 17,182 |
| Tool closeout | – | 4 | 104,524 | 4.5% | 1,053 | 85,931 | 9,663 | $0.347 | 11.2% | 26,131 |
| Judge (tool pass) | vendor_table | 1 | 15,445 | 0.7% | 13,776 | 0 | 190 | $0.008 | 0.3% | 15,445 |
| **Total** | | **59** | **2,344,638** | | 1,184,501 | 392,982 | 32,944 | **$3.083** | | 39,740 |

Roll-ups:

- **First pass (extract, refine, judge):** 27 calls, 1,366,925 tokens (58%), $1.462 (47%).
  - internal_pdf tier: 13 calls, 677,013 tokens.
  - **vendor_table tier: 14 calls, 689,912 tokens, to read one spreadsheet row per product** (section 1.4).
- **Tool pass:** 32 calls, 977,713 tokens (42%), $1.622 of model cost, plus $0.075 of tool web, which gives the reported $1.70.
- **Per-product web tier** (`search`/`browse`/`verify_page` phases, run from the `TIERS` loop): 16 searches and 21 paid browses for $0.4625. 15 of 21 page verifications failed, and **no web-tier model call followed** because no web evidence survived.

### 1.3 By product (exact)

| Product | Input tokens | First pass (calls / tokens / $) | Tool pass (calls / tokens / $) | Model $ | First → last model call |
|---|---:|---|---|---:|---:|
| PIMITEM-213030 (Mueller) | 701,551 | 8 / 450,251 / $0.450 | 8 / 251,300 / $0.468 | $0.918 | 85 s |
| PIMITEM-245747 (Mueller) | 670,473 | 7 / 395,825 / $0.296 | 9 / 274,648 / $0.440 | $0.735 | 85 s |
| PIMITEM-225830 (Ford) | 499,157 | 7 / 292,613 / $0.439 | 7 / 206,544 / $0.348 | $0.787 | 60 s |
| PIMITEM-221315 (Ford) | 473,457 | 5 / 228,236 / $0.277 | 8 / 245,221 / $0.366 | $0.643 | 64 s |

The whole execution ran 394 s for 4 sequential products, about 98.5 s per product including web.

### 1.4 By packet section (calibrated)

| Section | Input tokens | % input | Per product | Attributed cost* | % model cost |
|---|---:|---:|---:|---:|---:|
| Structured definitions (`structured_definitions`) | 622,564 | 26.6% | 155,641 | $0.457 | 14.8% |
| Shared PDF evidence: **per-entry metadata** | 682,887 | 29.1% | 170,722 | $0.440 | 14.3% |
| Shared PDF evidence: **source text** | 18,959 | **0.8%** | 4,740 | $0.012 | 0.4% |
| Raw definitions (`definitions`) | 123,398 | 5.3% | 30,850 | $0.091 | 2.9% |
| Instructions and output schema | 57,586 | 2.5% | 14,396 | $0.041 | 1.3% |
| Product-specific payload (vendor row, applicability maps, manifest) | 64,594 | 2.8% | 16,148 | $0.129 | 4.2% |
| Ford catalog image (internal_pdf extract and refine) | 21,200 | 0.9% | 5,300 | $0.042 | 1.4% |
| Refine: echoed first-pass candidates and cited passages | 171,271 | 7.3% | 42,818 | $0.343 | 11.1% |
| Judge: candidate list | 52,984 | 2.3% | 13,246 | $0.106 | 3.4% |
| Tool-loop history (pending packet, tool outputs, encrypted reasoning) | 456,717 | 19.5% | 114,179 | $0.913 | 29.6% |
| Tool fresh-context closeouts (3 of 4 products) | 72,478 | 3.1% | 18,120 | $0.179 | 5.8% |
| Output (all calls) | 32,944 out | – | – | $0.329 | 10.7% |

\* Cost was attributed by position within each call: prefix sections take that call's actual cached and cache-write tokens, and the remainder is priced uncached. The rows sum to $3.08.

**Where the metadata comes from** (Mueller shared PDF block: 81 entries, 122,185 characters per packet):

- `attribute_ids` is 43,713 characters (36%). Every entry repeats all 24 attribute names.
- `qualification` is 21,422 characters (18%): the same sentence 81 times.
- `evidence_id`, `evidence_ids`, `source_version` and `source_locator` are 33,304 characters (27%), mostly sha256 hashes.
- Timestamps, tier, kind and discovery fields come to about 13.5K characters.
- The actual `text` is **2,782 characters (2.3%)**.

The cause is `PdfItem.prompt_entry` dumping the whole `Evidence` model ([pdf_presentation.py:58-63](../../backend/pdf_presentation.py#L58)), and `product_packet` adding unprojected entries with `entry.model_dump` ([quality_pipeline.py:495-497](../../backend/quality_pipeline.py#L495)).

**Structured definitions** are 53,403 characters per packet. `model_instruction` ([quality_definitions.py:321-365](../../backend/quality_definitions.py#L321)) emits `display_rule`, `normalization_rule`, `component_rule`, `grounding_rule` and `unresolved_rule`: about 1,600 of the roughly 2,200 characters per attribute. These have only 3 distinct variants across the 24 attributes. Raw `definitions` are sent **as well**.

**Every tier sees every tier's evidence.** `product_packet(manifest, evidence, tier, …)` is built from *all* evidence, not `active` ([quality_pipeline.py:691](../../backend/quality_pipeline.py#L691) and [743](../../backend/quality_pipeline.py#L743)). The Mueller vendor-table extract (53,958 tokens) is therefore byte-identical in size to the internal_pdf extract, even though the vendor evidence is one row of about 500 tokens. The same holds for the vendor judge: 6 calls of about 53.5K tokens each, three-vote disputes, to judge one row.

**Refine echoes full candidate objects back to the model** ([quality_pipeline.py:701-706](../../backend/quality_pipeline.py#L701)), including `grounding.applicability.proofs`. Ford has 21 internal_pdf first-pass candidates at up to about 7.8K characters each (final size, including judge metadata added later), so a Ford refine call is 98K tokens against 34K for extract.

**The tool loop** puts definitions and structured definitions in its prefix (14,895 tokens). It then re-sends the pending definitions in the first user message ([quality_tool_loop.py:836-843](../../backend/quality_tool_loop.py#L836)) and replays the full stateless history at every step ([893](../../backend/quality_tool_loop.py#L893)). Inputs grow from 28.7K to 48.7K by step 8.

**Measured compaction.** Re-serializing the same packets with only `{citation_id, source_id, text, presentation(kind/page/table/row/col/role)}` per entry, and with definitions minus the hoisted boilerplate:

| | Current | Compact | Reduction |
|---|---:|---:|---:|
| Definitions (all calls) | 13,329 | ~3,200 (rules once in the system prompt) | −76% |
| Mueller PDF evidence block | 36,977 | 1,962 | −95% |
| Ford PDF evidence block | 13,012 | 1,139 | −91% |
| Vendor row | 932 | 494 | −47% |
| **Mueller extract call** | 53,958 | **~7,000** | −87% |

---

## 2. Ten highest-impact changes, ranked by impact on $/1,000 and wall clock at 25K–250K items

Effects are measured on Final A where possible; otherwise the derivation is shown. "Pilot $/1k" is all-in, including web.

### #1 Compact, tier-scoped model packets (the "minimal packets" item) — Effort **S/M**

- **Change:**
  - (a) Give `PdfItem.prompt_entry` and the unprojected path in `product_packet` ([pdf_presentation.py:58-63](../../backend/pdf_presentation.py#L58), [quality_pipeline.py:481-530](../../backend/quality_pipeline.py#L481)) a model-facing projection: citation_id, short source id, text, and presentation (kind, page, table, row, column, document role). Move `qualification` and `attribute_ids` to a per-source table, sent once, and only when they differ from "all attributes". Never send hashes or timestamps; `expand_citations` ([533-541](../../backend/quality_pipeline.py#L533)) already maps citation ids back to full `Evidence`.
  - (b) Hoist the five rule fields out of `model_instruction` into `SYSTEM`, once per distinct variant. Send either the raw or the structured definition, not both.
  - (c) Build each tier's packet from `active` evidence plus a short identity header (manifest, MPN, applicability statuses). The vendor tier needs only its row and headers.
  - (d) Refine should echo `{attribute_id, value, unit, citation_ids, quote}` and the ids of already-cited passages, not full `Candidate` dumps.
  - (e) The pass-1 judge should send only cited entries, as the tool-pass judge already does ([quality_pipeline.py:797-809](../../backend/quality_pipeline.py#L797)).
- **Expected effect:**
  - Pass-1 calls drop from 34–98K tokens to about 6–12K.
  - The tool prefix drops from 14.9K to about 4K.
  - Projected on Final A: about 586K → about 180–200K input tokens per product; model cost about $3.08 → about $1.6; **about $908 → about $540 per 1k**.
  - Prefill latency falls with token count.
- **Quality risk: low–medium.** Grounding (`ground_structured_candidate`) still runs against full `Evidence`, so citations stay exact. The real risk is losing cues the model was using: the title-block role and the table headers. Keep `presentation.document_role` and `header_labels`. There may be upside: the 3 slots that only the tool pass recovered came from re-reading local PDF fragments in a *small* context.

### #2 Gate the gap pass, replace its default with a local "second look", and remove the per-product web tier — Effort **M**

- **Change:**
  - (a) Drop `manufacturer_web` and `approved_web` from the per-product `TIERS` loop ([quality_pipeline.py:670-680](../../backend/quality_pipeline.py#L670) → `QualityWeb.load`, [quality_web.py:256-311](../../backend/quality_web.py#L256)). The query quotes the MPN verbatim (`"014255    215N"`, with four internal spaces), pays for a browse, and then needs an exact-MPN direct fetch ([294](../../backend/quality_web.py#L294)). 15 of 21 fetches failed.
  - (b) By default, replace the 15-step agentic loop ([quality_tool_loop.py:802-900](../../backend/quality_tool_loop.py#L802)) with one compact, single-turn "second look" over local evidence for unresolved or disputed attributes.
  - (c) Run web discovery only when all three hold:
    - the attribute is on a per-category "web-findable" list;
    - there is no exact or family-confirmed local evidence;
    - the item or family is worth it.

    Run it **once per family** (series or drawing number), and use vendor-supplied URLs first: the Ford rows already carry `Submittal ID Path` URLs. Cache outcomes, including negatives, per family and attribute.
  - (d) If the loop is kept, cap it at about 6 steps. Fix the admission bound, which budgets `reasoning × 16,000` per replayed reasoning item ([quality_cost.py:176-177](../../backend/quality_cost.py#L176)) and forced fresh-context closeouts in 3 of 4 products. Stop dropping the cache prefix on closeout ([quality_tool_loop.py:870-886](../../backend/quality_tool_loop.py#L870)).
- **Expected effect:**
  - Removes about $0.54 per product of Final A spend: $2.16 / 4.
  - A second look costs about $0.02–0.04 per product.
  - Combined with #1: **pilot about $150–250 per 1k**.
  - At scale the gated family web pass is the largest remaining line (about 50% of the recommended central cost), so its gate is the main cost dial.
  - Wall clock: about 20–45 s less per product (the tool pass ran 23–50 s per product, from model-call timestamps).
- **Quality risk: medium.**
  - Across pr3-initial, Final A and Final B (12 product-runs), **no accepted value relied on web evidence**.
  - The tool pass did add local-PDF slots. In Final A:
    - PIMITEM-213030 Seal/Softgoods (2 values, disputed);
    - PIMITEM-245747 Seal/Softgoods (disputed);
    - PIMITEM-245747 Primary Material (accepted).
  - The rest were duplicates (221315 Material Standard with leading punctuation; 245747 Nominal Size).
  - Acceptance must show the second look, or the compact pass 1, reproduces those 3 slots.

### #3 Extract once per source document or family and map to many products — Effort **L**

- **Change:** add a family stage ahead of per-product work.
  - Group items by `source_family` / `shared_source_ids` ([quality_pipeline.py:461-478](../../backend/quality_pipeline.py#L461)); the shared evidence set is already computed in [quality_worker.py:465](../../backend/quality_worker.py#L465).
  - Run compact extract, refine and judge **once per document**.
  - Emit candidates as either family-wide (no variant qualifier) or variant-keyed. Model-keyed tables become row evidence; `scope_document` already detects these at [quality_worker.py:54-112](../../backend/quality_worker.py#L54).
  - Per product: deterministic `build_applicability_map` plus a compact residual call only where variant mapping is ambiguous.
  - Persist family results keyed by (document sha, definitions hash, policy version). New products or re-runs in the family then cost nothing.
- **Expected effect:**
  - The PDF cost per item becomes about $0.20 × 1.5 documents / F. That is about $0.006 at F = 50 and below $0.001 at F = 500, against about $0.37 per product today for PDF-tier calls.
  - The pilot cannot show this: F = 2. Final A sent the identical 37K-token Mueller evidence block to 2 products × (extract, refine, judge, tool judge).
- **Quality risk: medium–high.** This is exactly where variant transfer bites: the H14250 vs H14255N question, and the "do not transfer variant-specific dimensions" qualification.
  - Mitigation: only values stated without variant qualifiers become family-wide.
  - Keep family-unconfirmed values at Low confidence.
  - Regression test: Ford 333W vs 444W must keep 3/4" vs 1" sizes.

### #4 Vendor files: parse once, profile columns once, memoize per distinct cell phrase (vs "one column→attribute mapping then deterministic fill") — Effort **M**

- **Change:**
  - (a) Parse each workbook once per batch and index rows by MPN. `read_vendor_table` re-parses every sheet, up to 50,001 rows, **per product** ([vendor_tables.py:42](../../backend/core/vendor_tables.py#L42)); the loader caches only bytes ([quality_worker.py:188-197](../../backend/quality_worker.py#L188)).
  - (b) Per file, make one model call on headers plus about 30 sampled rows. It classifies columns as identity/logistics to ignore (price, weight, carton, photo path), single-attribute (deterministic map), or free-text description (phrase extraction).
  - (c) Extract once per unique (column header, normalized cell text), batching 30–50 phrases per call. Fill rows deterministically with row and cell citations. Judge each phrase identity once (#5).
- **Skepticism about pure column mapping:**
  - All 13 accepted Mueller vendor values came from free-text columns (`Descr 1`, `Descr 2`, `DescrGen1–5`) whose meaning varies by row.
  - Single cells feed 2–4 attributes. For example, `5/8 X 3/4 OR 3/4IN SADDLE METER SWIVEL NUT OUTLET` feeds Compatible Meter Size, Outlet Size, Outlet Connection Type and Flanged Outlet.
  - The Ford file has no attribute columns at all: its only value is the `-NL` suffix giving Lead-Free.
  - A column map alone would fill almost none of these. Phrase memoization keeps the same quote semantics and amortizes across the 50–2,000 rows of a family.
- **Expected effect:** the vendor tier was 14 calls, about 690K tokens and $0.367 for 4 rows. After the change it is about $0.0015–0.004 per row, assuming 25% unique phrases, plus about $0.06 per file.
- **Quality risk: low–medium.** Phrase meaning can depend on column or neighboring cells; key on the column header and allow multi-cell phrases. Row applicability stays exact by MPN.

### #5 Judge: location-independent verdict reuse, scoped packets, cheaper disputes (includes "skip judge for exact-cell literals") — Effort **S/M**

- **Change:**
  - `JudgeCache.identity` includes `source_locator` and `source_version` ([quality_judge.py:37-55](../../backend/quality_judge.py#L37)), so the same quote on another row or product never hits. Key it on definition, value, canonical quote, column header or document role, interpretation and applicability status.
  - Pass-1 votes send the full product packet ([quality_judge.py:112-117](../../backend/quality_judge.py#L112)); send only cited entries.
  - A first-vote dispute triggers 2 more full calls ([135-138](../../backend/quality_judge.py#L135)). Make the third vote conditional on a 1–1 split, and route low-value disputes straight to review.
- **On "skip the judge for exact-cell literal values", measured:**
  - Only **2 of 81** Final A candidates have `origin=literal` (Pressure Rating 100 PSI ×2). 52 are `derived` `Other: <verbatim>`.
  - Every judge dispute on exact or family-confirmed sources was a *real mapping question*:
    - Nominal Size `5/8X3/4X3/4` (213030);
    - Nominal Size `5/8X3/4"` (245747);
    - Stem Rotation `90° motion`;
    - AWWA C800 scope.
  - So the literal-only skip saves almost nothing, and widening it to verbatim `Other:` values would drop real catches. **Get the savings from verdict reuse instead.**
- **Expected effect:**
  - Judge spend was $0.58 (19% of model cost): 11 pass-1 calls at $0.39 plus 8 tool-pass calls at $0.19.
  - Scoped packets cut judge tokens by about 80%.
  - With reuse at family or vendor scale, judge calls approach about 0.05 per item.
- **Quality risk: low for scoping.** Low–medium for the cache key: include column or role so context-dependent phrases are not conflated.

### #6 Parallelism, sharding and resumability — Effort **M**

- **Change:**
  - Shard by family across replicas. Today `operations.sh` deploys `--parallelism 1 --replica-retry-limit 0` ([scripts/operations.sh:38-40](../../scripts/operations.sh#L38)), and `infra/batch.bicep` sets `replicaTimeout: 600` ([infra/batch.bicep:63](../../infra/batch.bicep#L63)), which is **about 6 products** at 98 s each.
  - Within a shard, keep 8–16 requests in flight behind a TPM token bucket. Today each product is strictly sequential: tiers → extract → refine → judge → up to 15 tool steps.
  - Skip completed items on restart; [quality_worker.py:497](../../backend/quality_worker.py#L497) re-runs every item.
  - Stream evidence per item instead of loading all items up front ([464](../../backend/quality_worker.py#L464)).
  - Use per-shard cost meters (section 4, scaling blocker 4).
  - Route a family's items to the same shard to keep prompt-cache locality.
- **Expected effect, wall clock:**
  - Current design, sequential: 1K = 27 h; 25K = 28 days; 250K = 285 days.
  - Current design, perfectly parallel at an assumed 2M TPM (586K tokens per item): 4.9 h, 5.1 days and 51 days.
  - Recommended design, about 11–18K tokens per item at the same TPM: about 0.1 h, 2.5 h and 22 h.
  - The deployment's TPM is not in IaC; plug in the real quota.
- **Quality risk: none** if per-item logic is unchanged.

### #7 Azure OpenAI Batch API for overnight runs — Effort **M**

- **Change:** submit the single-turn stages as Global Batch jobs: family-document extraction, vendor phrase batches, residual per-product calls and judge votes. Use `custom_id` = family, phrase or item key, with idempotent ingestion. The multi-turn tool loop stays real-time, if it is kept at all.
- **Expected effect:** about 50% off the eligible spend. Central 250K: $11.5K → $8.9K. A separate enqueued-token quota also stops the bulk run from competing with interactive TPM.
- **Quality risk: none.**
- **Operational risk:** verify that Responses requests with strict `json_schema`, `reasoning`, and the explicit `prompt_cache_options` / `prompt_cache_breakpoint` fields sent via `extra_body` ([core/quality_model.py:186-196](../../backend/core/quality_model.py#L186)) are accepted on Batch for the gpt-6-sol deployment. Assume cache discounts do not stack with the batch discount, and plan for up to 24 h turnaround.

### #8 Model tiering (gpt-6-luna for vendor phrases, first judge vote and second look; gpt-6-sol for PDF families, disputes and inferred values) — Effort **S** code, **M** evaluation

- **Change:**
  - Add a second `ResponsesCompletion` deployment and select by phase and tier in `run_product` `call()` ([quality_pipeline.py:656-668](../../backend/quality_pipeline.py#L656)) and in the vendor phrase stage.
  - Shadow-evaluate luna against sol on identical compact packets.
- **Expected effect:** luna pricing is not in the repository; I assume 1/4 of sol. Tiering is large *if you do not do #3–#5*. After them, the luna-eligible spend is small: −$530 of $12K at 250K central (about 4%). That is why it ranks here.
- **Quality risk: medium.** Enumerated normalization, `Other:` retention, and the inferred-boolean rules in `EVIDENCE_RULES` are where weaker models slip. Gate on at least 97% agreement with sol on the pilot plus a 200-item slice.

### #9 Fix prompt-cache mechanics — Effort **S**

- **Change:**
  - Instructions sit before the cache breakpoint (`instructions=system`, [core/quality_model.py:201](../../backend/core/quality_model.py#L201)) and differ per phase. Each family therefore writes 4 separate prefixes at 2.5× price: extract 51,703, judge 51,061, tool 14,895 and tool judge 13,776 tokens for Mueller.
  - Use one shared instruction block and put phase-specific instructions after the breakpoint.
  - Reuse the prefix on closeout, and order family members consecutively within the 30-minute TTL ([core/quality_model.py:191](../../backend/core/quality_model.py#L191)).
  - After #1, reconsider explicit mode for prefixes under about 4K tokens, where the write premium can exceed the read savings.
- **Expected effect:** −$0.45 on Final A (unused writes, section 1.1). After #1 it is much smaller, hence the low rank.
- **Quality risk: none.**

### #10 Output and reasoning budgets — Effort **S**

- **Change:**
  - `max_output_tokens` is 16,000 for every phase, yet judge outputs are 98–369 tokens. Set limits per phase: judge about 1,500, extract about 6,000.
  - The 16,000 limit also inflates the tool admission bound (#2d).
  - Use low reasoning effort for vendor phrases.
  - Template `reviewer_explanation` for memoized deterministic mappings instead of generating it per row.
- **Expected effect:** output was $0.33 (11%) today, but after #1 it becomes the *largest* pass-1 cost line: pass-1 output alone is $0.22. This avoids premature closeouts and also cuts latency.
- **Quality risk: low**, provided limits keep headroom above the observed maxima of 7.5K (extract) and 9.7K (closeout).

### Considered and not ranked

- **"DI sections relevant to the attribute" (per-attribute section routing):** after #1, the whole Mueller drawing is about 2K tokens and the Ford submittal about 1.1K. Per-attribute routing would multiply calls and risk recall: Manufacturer comes from the title block, and Primary Material needs two adjacent paragraphs. Use it only for long catalogs over about 20K tokens of text, selecting pages by MPN or series anchor; `scope_document` already removes neighboring rows.
- **Ford catalog image (5.3K tokens per internal_pdf call):** only 0.9% of input. Keep it for now, but send it once per family document, not with every extract and refine call.

---

## 3. Cost model: 1,000 / 25,000 / 250,000 items

### Assumptions (explicit; change them and re-run the arithmetic)

| Assumption | Value | Basis |
|---|---|---|
| Items with ≥1 internal PDF shared by a family | 60% | Assumption; the pilot is 100% |
| Items with an exact vendor row | 85% | Assumption; the pilot is 100% |
| Items with neither (web-only) | 6% | Derived (0.4 × 0.15) |
| Items per document family, F | 10 at 1K; 40 at 25K; 100 at 250K | Kept below the customer's 50–2,000 because small runs truncate families |
| Documents per family; DI pages per new document | 1.5; 5 at $0.01 per page | DI is charged identically in both designs (parse cache by sha) |
| Vendor files touched | 80 / 700 / 1,400 | Customer: about 1,400 vendors |
| Current design, PDF+vendor item | $0.787 (range $0.666–$0.908) | **Measured**: Final A/B mean, including web |
| Current design, vendor-only item | $0.60 | Derived: about $0.08 vendor-only pass 1 + $0.42 tool pass + $0.116 web tier |
| Current design, web-only item | $0.65 | Derived |
| Family document extraction (compact; extract + refine + judge on sol) | $0.20 per document | About 50K input and 10K output+reasoning tokens |
| Vendor profile; vendor row (25% unique phrases) | $0.06 per file; $0.004 per row on sol / $0.0015 on luna | Derived from measured row size (#4) |
| Residual compact per-product call | $0.03 for 40% of items | About 6K input, 1.5K output |
| Judge extras (disputes) | $0.005 per item | |
| Gated gap pass | $0.35 per family for 50% of families; $0.20 for 30% of non-PDF items | The main uncertainty: web yield was 0 in the pilot |
| luna price | 1/4 of sol | **Not in the repository; replace with your price sheet** |
| Batch API | 50% off single-turn stages; no stacking with caching | Azure Global Batch |
| Excluded | Image builds, hosting, human review time | |

### Results

| Items | Current design | Recommended, sol only, real-time | + luna (vendor phrases) | + luna + Batch API | Conservative† (luna + Batch) |
|---:|---:|---:|---:|---:|---:|
| 1,000 | **$720** ($720/1k) | $82 ($82/1k) | $80 | **$60** ($60/1k) | $185 ($185/1k) |
| 25,000 | **$17,900** ($716/1k) | $1,360 ($54/1k) | $1,305 | **$1,000** ($40/1k) | $2,805 ($112/1k) |
| 250,000 | **$178,900** ($716/1k) | $12,000 ($48/1k) | $11,480 | **$8,930** ($36/1k) | $24,230 ($97/1k) |

† Conservative case:

- every recommended unit cost doubled;
- residual call on 70% of items;
- F = 5 / 20 / 50;
- gap-pass gates at 80% of families and 50% of non-PDF items.

With the selected Final A ($0.908) as the PDF-item anchor instead of the A/B mean, the current design comes to about $788/1k.

**Where the recommended central 250K cost goes (sol only, real-time):**

| Line | Cost |
|---|---:|
| Gated gap pass | $6,260 (52%) |
| Residual per-product calls | $3,000 |
| Judge extras | $1,250 |
| Vendor files and rows | $930 |
| Family documents (2,250 documents) | $450 |
| DI | $110 |

**Sensitivity:**

- F = 5 with gates at 0.8 / 0.6 gives $147/1k.
- F = 200 with gates at 0.3 / 0.2 gives $38/1k.
- With the gap pass disabled, the result is $25 / $13 / $11 per 1k at the three scales.

**The single biggest unknown is web yield.** Measure it per attribute on a larger slice before paying for it.

### Throughput (input tokens per item and the TPM bound)

| | Current | Recommended |
|---|---:|---:|
| Input tokens per item | 586K | about 11K (250K) to 18K (1K) |
| Hours of tokens at an assumed 2M TPM: 1K / 25K / 250K | 4.9 / 122 / 1,221 | 0.1 / 2.5 / 22 |
| Sequential today (98.5 s per item) | 27 h / 28 days / 285 days | – |

---

## 4. What is wrong or over-engineered, and what blocks scaling

### Design

1. **The per-product × per-tier × per-phase pipeline re-sends everything to everyone.** The vendor tier carries the full PDF, the judge carries the full packet for each of 3 votes, and refine re-sends full candidates with applicability proofs.
2. **The provenance model leaks into the prompt.** `attribute_ids` is all 24 names per entry, and `qualification` and the hashes repeat per entry, together 29% of tokens with no extraction value. Provenance belongs in storage; the model needs citation ids.
3. **Definition boilerplate is repeated per attribute,** with only 3 variants across 24 attributes, plus duplicate raw and structured definitions: 32% of tokens.
4. **A 15-step agentic tool loop per product** with stateless full-history replay, a pessimistic admission bound (reasoning × 16,000 per item), and fresh-context closeouts that write never-reused caches. It is a lot of machinery for a pass that produced 0 web values in 12 product-runs. A deterministic "second look" plus family-level, URL-first retrieval covers the observed value.
5. **Pilot-specific logic sits in core paths and will not generalize to 1,400 vendors or other categories:**
   - `FORD_PDF_SHA256` and the Ford catalog-image path ([quality_worker.py:38](../../backend/quality_worker.py#L38), [495-496](../../backend/quality_worker.py#L495));
   - the hard-coded Mueller OCR sha ([471](../../backend/quality_worker.py#L471));
   - the smoke target row 1096 / T1096 "Lockwing" ([40](../../backend/quality_worker.py#L40), [309](../../backend/quality_worker.py#L309));
   - the two-manufacturer `MANUFACTURERS` map ([quality_web.py:35-38](../../backend/quality_web.py#L35));
   - product-specific derivations inside `EVIDENCE_RULES` (brass + NL, AWWA paragraphs, LOCKWING) ([quality_pipeline.py:33-66](../../backend/quality_pipeline.py#L33), [277](../../backend/quality_pipeline.py#L277)).

   Move these into per-category and per-vendor rule packs held as data.
6. **Benchmark-overfitting risk.** The tool system prompt prioritizes exactly the Cowork-only gaps: "Port Type; Material Standard; Compatible Meter Size; Flanged Outlet" ([quality_tool_loop.py:376-378](../../backend/quality_tool_loop.py#L376)). No reference values leak, but tuning prompts to the 5 Cowork-only slots of a 4-item benchmark will not transfer. `definition_context` also carries value-like hints, for example "Aqualine … (150 PSI)" and "UNS C89833 ≤0.25% lead", which deserve a separate isolation check.
7. **The quality signal is too small to steer cost work.** 4 items, and identical Final A/B runs differ on 10 of 96 slots. Any "no quality loss" claim needs a larger replayable evaluation set and a human-reviewed answer key (`backend/answer_key.py` exists).

### Scaling blockers (hard)

1. **Hard-coded spend caps:** $10 per run and $40 overnight ([quality_cost.py:65](../../backend/quality_cost.py#L65), [74-75](../../backend/quality_cost.py#L74)). That stops after about 11 products at the current cost.
2. **Replica timeout of 600 s** ([infra/batch.bicep:63](../../infra/batch.bicep#L63)) at about 98 s per product. **Parallelism 1 and replica retry 0** ([scripts/operations.sh:38-40](../../scripts/operations.sh#L38)).
3. **No resume:** completed items are re-run on restart ([quality_worker.py:497](../../backend/quality_worker.py#L497)). All items' evidence is loaded into memory up front ([464](../../backend/quality_worker.py#L464)), at roughly 400–700 KB per item of `results.json`.
4. **A single cost blob** is read-modify-written (ETag) on `before_call` and on `record` ([quality_cost.py:54-68](../../backend/quality_cost.py#L54), [116](../../backend/quality_cost.py#L116)), which contends under sharding. `summary.json` embeds the full usage history and grows with every call. `run.sh cost` tails 100 log lines ([operations.sh:135-136](../../scripts/operations.sh#L135)), and the container-log capture already lost 12 of 59 calls.
5. **`run.sh start` mutates the shared job's environment** ([operations.sh:58-59](../../scripts/operations.sh#L58)), so concurrent runs race.
6. **Vendor workbooks are re-parsed for every product** ([vendor_tables.py:42](../../backend/core/vendor_tables.py#L42)). At 2,000 items per file that is 2,000 full parses.
7. **Review volume:** 250K × 24 = 6M review rows, against Excel's 1,048,576 rows per sheet. Reviewer hours are the real bottleneck, not tokens. Review by exception (Low, disputed and inferred only) and approve at family level, where an approval of a family-wide candidate applies to every variant.

---

## 5. Proposed sequence of 3 PRs

All three PRs use the same quality gate on the 4-item pilot, over 2 runs:

- at least 69 populated slots and at least 65 judge-accepted (Final A);
- Cowork `differ` at most 31 and `Cowork-only` at most 5;
- 0 new conflicting facts;
- Final A/B-style stability of at least 86/96.

Scoring keeps the existing reference isolation.

### PR A — Compact, tier-scoped packets plus cache and output hygiene (#1, #9, #10, judge scoping from #5)

- **Files:**
  - `backend/pdf_presentation.py` (`prompt_entry`)
  - `backend/quality_pipeline.py` (`product_packet`, `cached_packet_parts`, refine block at 695-722, pass-1 judge packet at 743, `SYSTEM`)
  - `backend/quality_definitions.py` (`model_instruction`)
  - `backend/quality_judge.py` (vote packet)
  - `backend/core/quality_model.py` (per-phase `max_output_tokens`, shared instructions before the breakpoint)
  - `backend/quality_tool_loop.py` (no duplicate pending definitions; keep prefix on closeout)
- **Targets:**

  | Measure | Target | Final A |
  |---|---|---|
  | Input tokens per product | ≤ 200K | 586K |
  | All-in cost | ≤ $550 per 1,000 | $908 |
  | Unused cache writes | ≤ 5% of model cost | 15% |
  | Mean wall clock per product | ≤ 70 s | 98.5 s |

  Add a unit test asserting a character budget on the retained Mueller and Ford packets: pass-1 packet ≤ 40K characters.

### PR B — Gap-pass gating, local second look, and removal of the per-product web tier (#2, judge verdict reuse from #5)

- **Files:**
  - `backend/quality_pipeline.py` (`TIERS` loop, tool-loop entry at 762-860)
  - `backend/quality_tool_loop.py` (step cap, family scope, URL-first)
  - `backend/quality_cost.py` (`maximum_tool_cost` reasoning bound)
  - `backend/quality_web.py` (family-level query, negative cache)
  - `backend/quality_judge.py` (`identity` without locations, conditional third vote)
- **Targets:**

  | Measure | Target | Final A |
  |---|---|---|
  | All-in cost | ≤ $250 per 1,000 | $908 |
  | Web spend per pilot product | ≤ $0.02 | $0.134 |
  | Mean wall clock per product | ≤ 40 s | 98.5 s |

  The quality gate must also show that Final A's three tool-only slots (213030 and 245747 Seal/Softgoods, 245747 Primary Material) are still populated.

  Report web yield per attribute (accepted values per $) as a standing metric; that number decides the gate.

### PR C — Family and vendor amortization plus sharded, resumable orchestration (#3, #4, #6; feature flags for #7 Batch API and #8 luna)

- **Files:**
  - `backend/quality_worker.py` (family stage, streaming load, skip-completed, sharding)
  - `backend/quality_pipeline.py` (family extraction and per-product mapping)
  - `backend/core/vendor_tables.py` (parse once and index; phrase memoization)
  - `backend/quality_cost.py` (per-shard meters, configurable budgets replacing $10/$40)
  - `scripts/operations.sh` and `infra/batch.bicep` (parallelism, timeout, per-execution environment instead of mutating the job)
- **Acceptance set:** a ≥ 200-item slice built only from already-approved files, for example the Mueller rows in `av-source-2` and the Ford AV11 rows in `av-source-5`.
- **Targets:**

  | Measure | Target |
  |---|---|
  | Real-time cost on sol | ≤ $100 per 1,000 |
  | Cost with the Batch flag | ≤ $60 per 1,000 |
  | Throughput at 4 shards | ≥ 30 items per minute |
  | Re-processing after kill-and-restart | 0 completed items |
  | Workbook parses | 1 per file |
  | Peak worker memory | Independent of item count |

  Quality: the pilot gate is unchanged from PR B. On a 30-item human-reviewed sample from the slice there must be 0 cross-variant transfers (Ford 3/4" vs 1" sizes, Mueller drawing-variant questions preserved), with approved precision at least the pilot's.

---

### Caveats

- **Splits inside prefixes are estimates.** Section splits within a cached prefix use calibrated character-to-token ratios (±~3%); call, phase, tier and product totals are exact.
- **Some numbers are assumptions.** The luna price, Batch API support for explicit cache fields, and the deployment's TPM quota are assumptions to verify. Recommended unit costs are derived from measured packet sizes, not from live runs.
- **"No quality loss" is only provable on a larger replayable set.** The 4-item pilot cannot show family amortization (F = 2), and it is too noisy to prove "no quality loss" on its own. Hence each PR's gate plus the 200-item slice in PR C.
