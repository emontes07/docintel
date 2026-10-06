# Supplied-Evidence Enrichment Contracts

[README](../README.md) | [User guide](USER_GUIDE.md) | [Local pilot](../PILOT.md)

This developer guide retains the README's earlier single-product CLI and diagnostic
scope. It is not the business batch submission workflow. The committed dependency
lock is reconciled; each release still requires a clean locked validation.

The separate `real_pilot` batch mode uses the same extraction, citation validation,
immutable result, review, and export contracts. It calls the attribute-level cascade:
internal PDF, vendor table, manufacturer web, then explicitly permitted web.
Later tiers receive only unresolved/conflicted attributes. Source failures remain
visible without discarding independent supported findings. This mode requires
server-side scope, service, deadline, attempt, token, and cost reservations; selecting
it in the portal is not operator approval.

### Optional WebIQ gapfill readiness (closed by default)

`OptionalWebPolicy` in `backend/core/websearch_policy.py` is an offline, explicit
server-side policy contract for a **next, separately authorized scope**.
The normal production `main()` → `run_batch()` path loads it only when the worker
environment has `DOCINTEL_OPTIONAL_WEB_GAPFILL_ENABLED=true` and valid
`DOCINTEL_OPTIONAL_WEB_GAPFILL_POLICY_JSON`. The exact lowercase value `true` is
required; missing, false or other values retain the existing worker behavior.
Policy JSON alone never enables the optional mode. HTTP request data cannot
select it. `RealBatchProcessor(..., optional_web_policy=policy)` remains available
for direct callers and offline measurement. Constructing it is not authorization.
It cannot extend the current internal-only approval or its recovery amendments.
No approval, allowance, execution limit, deployment setting, or live entitlement
is changed by this implementation. The existing real-pilot guard must independently
authorize the exact batch, worker, full source scope, active window and consumption.
Opted-in malformed or mismatched policy fails before a worker execution reservation.
There is no automatic activation or new approval framework.

The prospective two-product plan is four internal model requests (PDF then vendor
table per product), up to two optional web model requests, two paid WebIQ `/web`
searches, **zero `/browse` calls**, and at most four independent original-page GET
attempts. Optional per-product ceilings are one search, two direct page attempts,
and one model request, shared across both web tiers. Existing durable `search`,
`web_retrieval`, and `inference` counters remain authoritative; direct original
retrieval is not paid WebIQ browse. The policy adds a separately bounded optional
cost sub-budget, never another global allowance. Failed reserved work is not
refunded or retried. The normal legacy full-scope path remains unchanged.

Only unresolved/conflicted attributes reach web, after PDF and vendor processing.
An inferred-only descriptive Boolean candidate remains unresolved for this purpose;
it does not suppress a subsequent literal source check. Existing and literally
supported attributes are excluded. The policy binds each selected item to explicit
public manufacturer, public MPN, public attribute terms, approved source IDs, and
exact HTTPS hosts. Search construction reads **only** these public fields and the
pending-attribute selection—not internal identity terms, IDs, document excerpts,
customer values, definition descriptions, or prior candidate text.

Discovery titles/passages never become evidence. Supplied and discovered URLs
use the same independent original-page retriever: exact approved host, public DNS
and pinned IP, HTTPS, no redirects/proxies/credentials/retries, bounded text/HTML
only. At most two distinct URLs are attempted per product. Original URL,
retrieval time, response-byte SHA-256 and media type are persisted in source
provenance, even when retrieved content fails product applicability. Evidence
retains original text, URL/version, retrieval time, attribute scope and discovery
method. Both manufacturer and MPN must match independently retrieved text;
candidate quote, value, citation and applicability validation still apply.

Local optional operation, cost, or **complete input** capacity decisions record
`optional_*_capacity` skips before reservation or client invocation. Missing,
disabled, unavailable or failing providers record explicit optional outcomes and
preserve independent internal proposals and their export. Optional input is never
truncated to fit: the full source text remains available in the private result
when inference is skipped. Source and model failures have no fallback retry.
An expired or invalid authorization is still fatal, including during optional
preflight. A denial from the existing global reservation guard is **not** a local
optional skip: in opted-in mode it stops queued work even if it occurs on the
preceding internal inference or retrieval. Guard/binding/usage errors remain fatal.

#### Future operator configuration and production entry

After one backend build containing this implementation, a separately authorized
operator can supply these two **worker-only** environment settings for the next
approved run; no further code-only injection or frontend build is needed:

* `DOCINTEL_OPTIONAL_WEB_GAPFILL_ENABLED=true`
* `DOCINTEL_OPTIONAL_WEB_GAPFILL_POLICY_JSON`: one complete JSON object matching
  `OptionalWebPolicy` (maximum 65,536 UTF-8 bytes, rejected rather than truncated).

The JSON shape is below; placeholders must be replaced from the selected approved
batch and explicit public terms, never copied from document excerpts:

```json
{
  "batch_sha256": "<64-character binding_digest(record)>",
  "items": {
    "<approved item_key>": {
      "manufacturer": "<public manufacturer>",
      "mpn": "<public MPN matching the approved item>",
      "attribute_terms": {"<approved attribute_id>": "<public search term>"},
      "source_ids": ["<approved web source_id>"],
      "allowed_hosts": ["manufacturer.example"]
    }
  },
  "max_cost_microdollars": 0,
  "max_search_calls": 2,
  "max_direct_page_attempts": 4,
  "max_inference_calls": 2,
  "max_input_tokens": 26000,
  "max_output_tokens": 2048
}
```

`items` permits at most two products. `batch_sha256` is the existing
`backend.real_pilot.binding_digest(record)`, not merely the batch identifier.
The cost value of zero in this template intentionally skips optional work; set
it only to the separately selected optional sub-budget **within** the unchanged
global allowance. Bind the intended exact source IDs and public HTTPS hosts.
Internal item/source IDs in this configuration are lookup keys, not query terms.
The policy is validated and copied once when constructing the production processor.

Existing guarded settings and installed approval remain mandatory, including
`DOCINTEL_REAL_PILOT_ENABLED=true`, the approved worker identity/service settings,
and independently authorized `full` scope. WebIQ still requires its existing
`WEBSEARCH_PROVIDER=webiq`, approved `WEBIQ_ENDPOINT`, and secret-backed
`WEBIQ_API_KEY`; do not put credentials in policy JSON. These optional settings
neither replace those controls nor authorize the current internal-only pilot.
Use the unchanged finite worker entry:

```sh
python -m backend.batch_worker --real-pilot --batch-id "<approved batch ID>" \
  --concurrency 1 --item-limit 2 --max-batches 1
```

API signatures:

```python
configured_optional_web_policy(
    environ: Mapping[str, str] | None = None,
) -> OptionalWebPolicy | None

RealBatchProcessor(store, record, guard, *, optional_web_policy=None)
run_batch(store, batch_id, *, concurrency=2, item_limit=100, processor=None)
```

The policy loader is in `backend.core.websearch_policy` and performs no network,
authentication, storage mutation or reservation. A supplied `processor` retains
the existing explicit test/embedding override; normal production omits it.
No runtime environment or installed approval is changed by checking in this code.

#### Exact complete-prompt measurement API

Call the pure
`backend.batch_worker.prepare_inference_request(system, user, ExtractionResponse, deployment=...)`
with the exact extraction instructions and the complete JSON user payload that
`run_enrichment` supplies. The result provides the actual compact `system` and
`user` strings, citation references, request parameters, cache-version digest,
and `accounting`. Execution uses this same function, not a second estimator.
Its `input_bound` / `accounting["max_input_tokens"]` is:

```
UTF-8 bytes of complete system instructions (including compact instructions)
+ UTF-8 bytes of complete compact user JSON (definitions, product, metadata, evidence)
+ UTF-8 bytes of json.dumps(ExtractionResponse.model_json_schema())
+ 4096 framing allowance
```

This is the established conservative byte-based reservation, not a token count.
After complete-payload remeasurement superseded the earlier 20,000 planning
estimate, each prospective optional request must fit **26,000 input-bound units
plus 2,048 output**, without evidence truncation. That is 28,048 combined units
per request, below the existing 30,000 per-request recovery bound; it does not
authorize recovery web calls. The two-request prospective web input ceiling is
52,000. These are next-run admission limits, not modifications to any allowance,
global guard, current configuration, or runtime authorization.

`accounting` exposes each component and the fixed output bound; model/skip
provenance retains the receipt. Schema and instruction changes require recomputing
all six complete requests, with exact internal input measured separately.
Placeholder-only payload measurements do **not** establish that unknown future
original pages fit: the complete retrieved-page payload must still pass admission,
or inference is explicitly skipped with no truncation. Synthetic tests cover
six-call orchestration; they are not the private exact-source readiness gate and
do not establish live authority or finalized billing.

## Replay And Explicit Live Mode

From the repository root, a synthetic offline replay needs neither Azure login nor
a model call. The output must be a new private file outside Git:

```sh
.venv/bin/python -m backend.extract tests/fixtures/enrichment/synthetic.json --output "$REPLAY_OUTPUT"
```

Set `REPLAY_OUTPUT` to an approved unused path first. Replay consumes typed parsed
document fixtures and a supplied generated response. It does not parse a PDF, fetch
documents, populate Search, or establish integration readiness. Exit codes are `0`
for completed processing, `1` for a recorded extraction/retrieval failure, and `2`
for invalid configuration/input/review or output errors. Zero does not imply an
accurate candidate or a completed review.

Live CLI execution requires **both** `--live` and input
`execution_mode: "live_inference"`; replay input defaults to `offline_replay`. Replay requires a
`generated_response` object (an empty candidates array is valid). Live input forbids
that field, even null. Both consume supplied parsed evidence for one product;
neither retrieves documents, parses PDFs, nor uses web sources. A separately
authorized live diagnosis may use `--no-retries`, rejected in replay mode. That
flag does not authorize another invocation or a fallback.

Live text settings are `LLM_ENDPOINT` and `LLM_DEPLOYMENT`; use the approved
OpenAI-specific advertised endpoint, not a project endpoint or an invented hostname.
Endpoint precedence in `LLMClient` is explicit constructor value, then
`LLM_ENDPOINT`, then legacy `AI_FOUNDRY_ENDPOINT`. The standalone client uses
`DefaultAzureCredential` with Cognitive Services scope; this differs from the
hosted batch worker's explicit managed identity. The root dotenv is loaded
independently of working directory; process values win per setting. azd outputs
are not loaded automatically. No WebIQ key is needed for extraction.

## Input, Evidence, And Review

A bundle includes `manifest.product` (`item_id`, `vendor`, `mpn`, `hierarchy_node`),
typed `attributes`, optional `existing_values`, and explicit `source_ids`. Every
source must match all product fields and supply a parsed `document`, explicitly
located `excerpts`, or an `error_code`. A document has `source`, `cache_key`, `parsed_at`, and located
paragraphs/table cells. Unlocated `raw_text` is not cited evidence. Missing page,
provider retrieval time, or source publication time is not invented; application
observation time is distinct.

Attribute examples never become prompt evidence or reference answers. Candidate
validation checks cited IDs, type, unit, and literal support, not inferred
conversions or full semantic correctness. The narrowly labeled, review-only
Lead-Free descriptive rule below is the sole nonliteral Boolean exception.
Conflicts remain separate, existing
values remain unchanged, and partial source failure can coexist with proposals.
Real-pilot proposals additionally require grounded, normalized supporting quotations and
product/component applicability qualifications. Evidence has an explicit source
tier and approved attribute scope. Unknown numeric units are not assumed
dimensionless: unit guidance must be resolved before a numeric proposal is accepted.
The original cited unit remains in evidence. Definition context and
examples are not evidence. Optional model confidence is not measured accuracy.
WebIQ discovery passages are not substituted for separately retrieved original
source content. PDF pilot analysis is bounded to the first five pages, with that
limitation retained in provenance.
Only missing definitions, product identity, and selected source excerpts/provenance
enter the prompt. Model errors never fall back to replay or web.

### Quote grounding: bounded order tolerance, not semantic entailment

`match_quote` first uses the existing exact/normalized quotation matching.
Only if that fails does it try an order-tolerant **multiset** containment check
within a single logical source scope. After existing normalization, the only
discardable filler words are `a`, `an`, `the`, `is`, `are`, `of`, `for`, and `with`.
The fallback requires at least three remaining quote tokens. A **95% minimum
overlap screen is followed by a zero-unsupported-content-token check**; 95%
overlap alone never accepts a quotation. Repeated tokens must also be supported.
This admits limited syntactic reordering, not synonyms, semantic paraphrases,
unit conversions, or newly supplied facts.

Negations, alternatives, limits, inlet/outlet roles, and other protected
qualifiers are not filler. Their counts and relevant local bindings must remain
supported; number/unit bindings are retained. Values and numeric qualifiers are
not substituted or normalized to different meanings. Multi-role component or
rating scopes cannot be used as an unordered pool to swap associated values.
PDF component/material groups remain separate: a seal's material cannot support
a body's material, and different physical rows are not merged.

For vendor tables, each semicolon-delimited quote clause must match one original
cell within the same logical vendor evidence scope. No clause may fabricate a
fact by pooling words from unrelated cells. Verification records retain the
original evidence IDs, locators and matched cells, with normalization markers
including `order_tolerant_multiset`, `overlap_screen_0.95`,
`no_unsupported_content_tokens`, and, when applicable, `clause_to_cell`.
The strict `match_text` path for **candidate values and units is unchanged**.
Successful quote containment is neither proof of semantic entailment nor human
approval; product applicability and the remaining candidate checks still apply.

### Lead-Free descriptive Boolean proposals require review

Literal labeled Boolean evidence remains preferred. The only descriptive
inference rule, `lead_free_description_v1`, may propose unitless **Lead-Free
`True`** from grounded exact-product wording such as “lead-free brass valve,”
“low-lead valve,” or “no-lead product.” This is a narrow syntactic review rule,
not a claim that descriptive marketing language establishes literal Boolean
evidence, a regulatory threshold, compliance, or certification.
The validator deterministically classifies a submitted model candidate; it does
not scan an empty response to synthesize new candidates.

The complete cited fragments must pass the conservative checks, not merely a
cropped quotation. Negations, alternatives, conditional or variant language,
component-only claims, accessories/replacements, requirements/examples, and
certification claims do not authorize the inference. An eligible explicit
Lead-Free answer blocks descriptive inference, including an uncited literal
False. This rule never infers False, applies to no other Boolean attribute, and
does not use definition examples or another product's evidence.

Such candidates carry `evidence_basis: "inferred_from_description"` and
`inference_rule: "lead_free_description_v1"`. Their qualification explicitly
includes **`inferred_from_description — requires review`**; confidence is absent.
Verification retains the grounded quotation but has `value: null`: no literal
Boolean-value match is invented. Literal/historical candidates default to
`evidence_basis: "literal"` and no inference rule.
Exports retain dedicated `Evidence basis` and `Inference rule` columns alongside
the supporting quote and flagged qualification; no frontend change is required.

Inferred candidates remain separate review proposals, never automatic approvals.
They remain unresolved for subsequent vendor/web gapfill. Matching literal
evidence can resolve the gap; differing supported values remain a reviewable
conflict without erasing the earlier inferred candidate. Batch coverage lists
`inferred_review_required` separately and does not count an inferred candidate
as literal internal or external support.

### PDF row presentation and citation round-trip

The real-pilot compact prompt (`real-evidence-rows-v3`) projects cached PDF
table cells into physical rows. Recognized source headers label the values;
a changed header section or a gap between rows ends the previous mapping.
Unrecognized columns are explicitly numbered, not assigned invented headers.
Notes and title paragraphs remain separate. Duplicate paragraphs/cells, pure
headers, and sparse drawing callouts are excluded from the presentation or
marked as drawing context; dimensional values remain available. This changes
neither the cached parse contract nor stored evidence. Results and exports retain
every original evidence record and locator, including omitted presentation noise.

A compact row reference expands to every contributing original cell and header.
Paragraph duplicates retain their original IDs. Whitespace, reference ordering,
and lowercase `e` remain harmless; unknown references and partial citations that
cannot ground the quote remain invalid. The verifier uses the same row
reconstruction as the prompt, including normalized header separators. It never
joins different physical table rows. Row numbers, generated labels, header text,
and part-index numbers cannot supply candidate values. Parallel component groups
do not make a seal's material the body's material. Source scope, units, variant
applicability, conflicts and human review requirements still apply.

New server verification records use `grounded-normalization-v3`; row quotes identify
`reconstructed_row`, the normalization rules, and every original locator.
Stored v1/v2/v3 verification records remain readable; historical records are not
rewritten. Verification metadata is server-produced and remains absent from the
`ExtractionResponse` model response schema. Budget reservations still
include the complete system/user payload, response schema, 4,096 framing
allowance and 2,048 output allowance per request. Smaller evidence presentation
does not establish that the cumulative PDF and vendor-tier requests fit.

The opt-in `tests/test_private_pdf_rows.py` gate uses existing private, hash-bound
inputs via `DOCINTEL_TEST_PDF_ROWS_WORK` and `DOCINTEL_TEST_PDF_ROWS_DOCUMENTS`.
Set `DOCINTEL_TEST_PDF_ROWS_REQUIRE_COMMITTED=true` and a new
`DOCINTEL_TEST_PDF_ROWS_OUTPUT` filename prefix to retain exact-source private
receipts. It checks the real cascade payloads, supplied-response verification,
original export locators, isolated stale-state reconciliation, and the unchanged
production reservation guard. A passing test can deliberately report `NO_GO`.
Its verifier-only workbook is not a live enrichment result, budget approval,
hosted reconciliation, or evidence of model accuracy. Customer fixtures and
outputs must never be supplied to public Git or CI.

`--reviews` imports separate `ReviewDecision` records with attribute, decision,
reviewer, timestamp, and reason. Approval selects `candidate_index`; correction
supplies `corrected_value` and the definition's unit where applicable. Rejection
supplies neither. Human review is never automatic.

`review_annotations` are post-generation notes with origin `review_annotation`,
zero-based candidate index, text, author, and timezone-aware annotation time. They
must refer to an existing candidate and remain separate from approval and evidence.
Older results default to an empty annotation list. For historical annotation work,
validate and annotate a private copy, retaining original and derived hashes; never
overwrite the original result or mark it approved merely because notes were added.

## Execution Metadata And Observations

Exports distinguish `execution_mode`, `candidate_source` (`supplied_response` or
`llm`), `model_call_status`, `skip_reason`, `extraction_error`, and sanitized
`failure`. `not_attempted`, `skipped`, `failed`, and `succeeded` describe the client
boundary, not an HTTP request count. A successful completion can still fail
evidence/schema validation (`invalid_response`); other generation errors are
`model_failed`. Raw exception bodies, prompts, headers, and credentials are not
exported. Older exports cannot recover discarded diagnostic information.

The two synthetic cases under [tests/fixtures/enrichment](../tests/fixtures/enrichment)
have separate expected results, never model inputs. The 2026-09-30 bounded live
observations produced one supported candidate for the positive case and no
candidates for the unsupported control, with review pending and retries disabled.
That is two-case evidence, not customer accuracy or production readiness. Offline
regressions use mocks; no live calls were made for this documentation update.

## Retrieval Diagnostic Boundary

Vendor-table and both web tiers remain `not_attempted` in this enrichment path.
Explicit `--web-provider` selection fails before retrieval rather than switching
providers or returning an empty successful search.

WebIQ REST is documented and implemented separately by the
[standalone diagnostic](../scripts/WEBIQ-DIAGNOSTIC.md). The application adapter
remains deliberately unavailable pending integration work. The previously
user-reported Microsoft evaluation-profile lookup at
`2026-09-30T04:41:51.207059+00:00` returned HTTP 200,
`success_results`, and three results with grounding content and document URLs.
This establishes connectivity and response validation only, not original-source
verification, exact product matching, attribute support, customer-tenant enablement,
or production readiness. No lookup was repeated for this increment.
Foundry citation mapping never converts generated answers into snippets. No raw
provider payloads are persisted by that mapping; the scoped enrichment output
contains the typed manifest, selected internal excerpts, proposals, outcomes,
review annotations, and review records. The former README's diagnostic caveats
are retained here without repeating customer-specific document locations.