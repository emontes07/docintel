# Quality model adapter

`backend.core.quality_model.ResponsesCompletion` sends one synchronous Azure
Responses API request per `complete_structured` call. It uses worker managed
identity (`ManagedIdentityCredential`, optionally selected by `AZURE_CLIENT_ID`)
and the Cognitive Services scope; an injected bearer-token provider is also
supported. Construction does not request a token. There is no deployment probe,
custom preflight, reservation, ledger, fallback model, or application retry.
SDK retries are disabled, and Responses storage is disabled.
`AZURE_OPENAI_AD_TOKEN` must be unset: the SDK would otherwise prioritize that
ambient static token over the worker's token provider.

## Configuration

| Setting | Default / behavior |
| --- | --- |
| `QUALITY_MODEL_DEPLOYMENT` | Falls back to existing `LLM_DEPLOYMENT`; otherwise required. |
| `LLM_ENDPOINT` | Azure resource root or `/openai/v1/` endpoint; falls back to `AI_FOUNDRY_ENDPOINT`. |
| `QUALITY_MODEL_EFFORT` | `medium` for extraction. |
| `QUALITY_MODEL_MAX_OUTPUT_TOKENS` | `16000`, including reasoning tokens. |
| `QUALITY_MODEL_INPUT_USD_PER_MILLION` | Uncached input price; unset means unknown. |
| `QUALITY_MODEL_CACHED_INPUT_USD_PER_MILLION` | Cached input price; unset means unknown. |
| `QUALITY_MODEL_CACHE_WRITE_USD_PER_MILLION` | Cache-write input price; needed when the service reports positive cache-write tokens. |
| `QUALITY_MODEL_OUTPUT_USD_PER_MILLION` | Output price, including reasoning; unset means unknown. |
| `QUALITY_MODEL_PRICE_BASIS` | Record label; defaults to `OpenAI public pricing estimate; not final Azure billing`. |
| `QUALITY_MAX_OUTPUT_TOKENS_EXTRACT` / `_REFINE` / `_SECOND_LOOK` / `_JUDGE` | Per-phase output limits (default `8000` / `8000` / `8000` / `2000`), never above `QUALITY_MODEL_MAX_OUTPUT_TOKENS`. Observed Phase 3 maxima: extract 4,711, refine 1,592, judge 1,009. |
| `QUALITY_MAX_OUTPUT_TOKENS_TOOL_STEP` / `_CLOSEOUT` | Optional tool-loop limits (default `4000` / `6000`); observed maxima 109 / 3,784. |
| `QUALITY_RUN_CAP_USD` / `QUALITY_SESSION_CAP_USD` | Monetary admission caps (default `10` / `40`). The session cap spans runs via `QUALITY_OVERNIGHT_PRIOR_COST_USD`; `QUALITY_OVERNIGHT_CAP_USD` is accepted as an alias. |

Constructor arguments override environment defaults. `reasoning_effort` overrides
effort for an individual call (for example a judge's configured `low`) without
changing subsequent extraction calls. A deployment's supported effort values are
determined by Azure, not guessed from its name. Existing `gpt-5` deployments remain
supported; no newer deployment or pricing is assumed to exist. Select a
Responses-compatible deployment/region and disclose the configured price basis.
Unknown cost remains explicit in ordinary extraction. The optional bounded tool
pass stops further actions when usage cannot be priced.
For the requested GPT-6 Sol run, the disclosed basis is
[OpenAI public pricing](https://developers.openai.com/api/docs/pricing), not final
Azure billing: short-context input/cached/cache-write/output prices are
`2 / 0.20 / 2.50 / 10` USD per million; long-context prices are
`4 / 0.40 / 5 / 15`. These are explicitly configured estimates, **not code
defaults**, and the appropriate context-tier rates must be selected by the caller.

## Model-facing packets

Every quality request uses one shared instruction block (`SHARED_SYSTEM`: untrusted-data,
citation, evidence and definition rules) and one cached prefix holding only the compact
structured definitions. The prefix and its `prompt_cache_key` are identical across
extract, refine, judge and second-look requests and across products with the same
definitions; the phase task follows the cache breakpoint in the request payload.
Evidence is projected to `citation_id`, a short source alias, text and presentation
fields (kind, page, table, row, column, document_role, header_labels). Qualifications
and applicability are listed once per source; an entry repeats them only when they
differ (for example a manufacturer title block, which also carries `limited_to`).
Attribute-ID lists, hashes, versions and timestamps are never sent; citations expand
back to the full stored `Evidence`. Each tier sees only its own evidence (a vendor
pass receives just its row), refinement echoes compact candidates, and judges see
only the cited entries. Raw `definition_context` is not sent.

## Usage

Fallback extraction receives only unresolved attribute requests, with that tier's
evidence packet. Prior-tier verdicts remain in results but are not fed
back as model instructions: the bounded live experiment did not improve accepted
coverage. A later tier can retain a separately cited value even if an earlier
candidate for the same attribute was disputed.
Reviewer wording distinguishes a retained pressure candidate from a pressure
question with no verified value; both still require definition/unit clarification.

Gap-closing extraction keeps decoded vendor inch marks (including an accidentally
repeated JSON escape) and dimension separators equivalent while preserving every
number, unit and `or` alternative. Suppressed drawing title blocks are restored
only for Manufacturer; their configuration dimensions cannot supply other values.
The second pass explicitly revisits already-cited passages for unresolved fields.

The additional closed rules are `nonflanged_outlet_mechanism_v1` (review-only
Flanged Outlet=False from a stated different outlet mechanism, never silence),
`connection_material_v1` (iron-pipe thread -> Iron pipe; copper service or
flare/compression connections -> Copper), and `brass_plus_nl_identification_v1`
(the potable-water brass paragraph plus the product's NL main-body paragraph ->
No-lead brass). Derived rules record their justification; quotes and applicability
still have to ground. Optional, negated, contradictory and wrong-role assertions
do not satisfy these rules.

Reviewer Proposed value cells show Yes/No, sentence-case all-caps vendor prose
while retaining technical abbreviations, and display `NSF61` as `NSF 61`.
Machine values and supporting quotations are unchanged.
The reviewer Confidence column is separate from model probability: High for
literal exact-product evidence, Medium for derived or confirmed-family evidence,
and Low for inference or unconfirmed-family evidence. The row uses its weakest
candidate confidence; Evidence retains each candidate's label. The pure
applicability classifier supplies these labels; it does not grant human approval.

### Local second look (default)

After the internal PDF and vendor tiers, `QUALITY_SECOND_LOOK_ENABLED` (worker
default `true`) sends one compact single-turn request per product over all of that
product's local evidence for the still unresolved or disputed attributes. Existing
candidates are echoed so they are not repeated. Each proposal must cite one local
tier and goes through the same grounding, applicability and judge; a
`second_look_summary` diagnostic records proposals, grounded, added and accepted.
Per-product `manufacturer_web`/`approved_web` tiers are no longer fetched; their
retrieval outcome is `not_attempted`. The run summary carries `web_yield`:
accepted web-cited values per web dollar for each attribute, which stays empty
while web is off.

### Bounded unresolved-attribute tools (optional)

`QUALITY_TOOL_LOOP_ENABLED=true` (default `false`) replaces the second look with
one Responses function-calling pass per product after the ordinary tiers, capped
at `QUALITY_TOOL_LOOP_MAX_STEPS` (default 6) counted actions. Only unresolved/disputed attributes participate.
The five tools are `search_vendor_rows`, `read_pdf_page`, `web_search`, `browse`
and `fetch_pdf`. Local tools read the already approved product-scoped evidence;
search terms contain only the MPN and requested attribute, never a reference
answer. Manufacturer discovery/retrieval precedes other public web. Only exact
discovered public URLs may be retrieved; search/Browse text alone is not evidence.
PDF/OCR retrieval retains the Mueller/Ford domain and five-page OCR limits.

Each product gets at most the configured actions (hard ceiling 15) and $1
additional tool-pass usage, inside the configurable run/session meter. Cost estimates include the complete effective
request, schema, tool definitions, source prefix, history, opaque reasoning replay
and maximum output; they never assume a cache hit. Actual usage is charged once.
Missing prices/unknown usage stop further requests, not a success-shaped result.
Every action and an explicit completion/budget/error summary appear in Diagnostics.
Already grounded first-pass candidates survive a stopped or failed tool pass.
Ordinary, recognized unavailable-source errors return explicit tool errors while
retaining failed diagnostics and actual charges; the same failed URL is not
retried. Unsafe inputs, unknown programming errors and accounting failures remain
fatal. With four steps left, the model must conclude without tools, leaving room
for majority judging. Search/Browse share the first pass's 12/6 per-product limits.
Tool-produced candidates are labeled in technical grounding metadata; rechecking
an identical candidate does not duplicate it.
Tool evidence sent to the model omits redundant machine-only metadata, while the
full original Evidence objects remain in grounding, judging, results and caches.
The cost estimate counts UTF-8 model text plus framing/schema/reasoning allowance,
not additional HTTP JSON escaping. Invalid search-result URLs are recorded as
rejected discovery entries and are never fetched; a bad lead does not invalidate
the other safe results. Invalid model-supplied tool URLs remain errors.
Both extraction and tools share the same type/origin/derivation rules. The tool
prompt carries no attribute priority list; pending attributes are investigated in
definition order.
If growing continuation history would exceed the remaining product budget, one
fresh no-tools terminal request can use all compact delivered evidence instead.
It retains quotes and provenance and must still fit the same step/product/global
limits; an unaffordable terminal request is not sent.

Tool proposals use the same structured grounder, applicability map, persistent
judge cache and low-effort majority judging. Disputes and partial submissions stay
visible. The scoring-only workbook is never an available tool or input source.

### Stable judging and prompt reuse

Grounded proposals are judged once per unique definition, typed value/unit,
canonical quote, evidence context (vendor column header, or PDF document role/kind),
interpretation rule and applicability status. Source location and version are not
part of the key, so the same quoted cell phrase is judged once across rows and
products. The key also versions judge instructions and deployment.
Persistent caches are owner-scoped and shared across products and runs.
Fresh proposals are batched. An initial accepted verdict is final; a disputed
first vote gets a second vote, and only a 1–1 split gets a third (majority of three).
Missing/invalid votes remain visibly disputed and are not cached. Cache hit/miss
and votes are recorded in technical diagnostics; acceptance is not human approval.
Extraction/refinement remain bounded to two calls per product/tier; judging can
add up to three calls for previously unseen identities (usually one or two).

The cached prefix is the compact definitions block only (see "Model-facing
packets"); its `prompt_cache_key` is derived from the shared instructions plus that
block, so every phase, tier and product with the same definitions reuses one entry.
On the configured GPT-6 Sol deployment, the Responses request marks the
prefix with an explicit `prompt_cache_breakpoint`, using
`prompt_cache_options={"mode":"explicit","ttl":"30m"}`. The locked SDK transmits
these v1 fields through `extra_body`; a native serializer test checks the wire
shape. Actual cached-input tokens, not expected cache eligibility, measure reuse.
See [Azure prompt caching](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching).

The Cowork draft is scoring-only. It is not a source binding, packet input,
instruction or citation. The offline scorer separately supports `agree`,
`format-only difference`, `differ`, `DocIntel-only`, `Cowork-only` and
`both-not-found`; tests capture actual extraction/refinement/judge arguments
(including images and serialized payloads) to detect reference leakage.

### Applicability and structured definitions

Each product has a source and citation-level map: `exact`, `family-confirmed`,
or `family-unconfirmed`, with printed identity/linkage quotations and locations.
An exact source summary cannot upgrade every generic paragraph. Family confirmation
requires an exact product row naming the matching family/drawing; H14250 and
H14255N are not aliases. Unconfirmed values remain visible with Low confidence
and a plain-language Questions row. They do not stop attribute-level fallback.
The judge checks quote support and the declared derivation, not whether to
override this map. Complete maps are retained in results and technical Diagnostics;
compact classifications accompany model citations without repeating full documents.

Approved definition metadata supplies Boolean, Enumerated, Multi-Select, Text,
and Number/unit instructions. Original definition rows are matched by node and
attribute. Example strings are preserved as examples, never promoted to a
whitelist or evidence. Explicit invalid types/options/units are rejected. Missing
guidance is different: unlisted enum values remain under `Other:`, and found
numeric values/units remain visible for clarification. Multi-Select uses `; `
separators; each member must independently ground in the cited quotation.
Component labels and qualifiers are retained where declared; unresolved component
permissions preserve source-groundable text with a review question, rather than
claiming whole-product applicability or silently dropping it. Reviewer Confidence,
Applicability and Questions remain distinct from model probability and approval.
Six-class scoring recognizes validated `Other:` display metadata as formatting;
it does not remove arbitrary source text or reconcile substantive synonyms.

Manufacturer PDF retrieval uses pinned public HTTPS on Mueller/Ford domains,
`pdftotext` first, then cached DI only for a textless PDF of at most five pages.
Only the worker injects the DI client. Successful byte-hash caches are reused;
failures remain explicit and retryable. `QUALITY_DI_USD_PER_PAGE` defaults to
the disclosed $0.01/page prebuilt-layout estimate. DI pages, cache hits and
unknown usage join the same run meter; no separate reservation or admission
system is introduced. Existing WebIQ search/browse caps remain unchanged.
`QUALITY_OCR_SMOKE=true` explicitly verifies the approved textless Mueller PDF
once in the worker before a run, using its actual page count (at most five).
It uses the same meter and successful byte cache, and never gives the API DI
permission. Leave it false after the requested verification. Missing local DI
configuration is reported as a pre-provider failure, not an unpriced analysis.

```python
from pydantic import BaseModel
from backend.core.quality_model import ResponsesCompletion

class Extraction(BaseModel):
    material: str | None

class Judgment(BaseModel):
    supported: bool

usage = []
model = ResponsesCompletion(usage_callback=usage.append)
answer = model.complete_structured(
    "Extract only attributes supported by the evidence.",
    "Exact product evidence: body material is brass.",
    Extraction,
)
judgment = model.complete_structured(
    "Judge whether the extracted value is supported by the evidence.",
    f"Evidence: body material is brass. Extracted: {answer.material}",
    Judgment,
    reasoning_effort="low",
)
# Optional page images: images=["data:image/png;base64,..."]
```

Text uses Responses `input_text` blocks. Image data URLs use `input_image` with
`image_url` as a string, not the Chat Completions nested image shape. The adapter
uses `responses.create(text={"format": ...})` and the OpenAI SDK's strict
Pydantic-schema conversion, verified against locked OpenAI **1.91.0**. Requests
target `/openai/v1/responses`, with no legacy `api-version` query parameter.
`AzureOpenAI` retains native refreshing bearer-token support in this SDK version;
the SDK-required constructor version is omitted from each request using its public
`Omit` marker. `AOAI_API_VERSION` does not change this v1 adapter.
Offline native-SDK tests cover both `gpt-5` and `gpt-6-sol` deployment names and
resource-root/full-v1 endpoint configuration; these tests do not make live calls.
JSON is
validated into the requested Pydantic model only after the response is available,
so a validation failure cannot hide its actual token usage. There is no JSON
repair, Markdown stripping, partial-output acceptance, or Chat Completions fallback.

## Usage records and errors

The quality verifier retains explicit `LOCKWING` and "padlock wing for locking"
descriptions as review-only feature-True inferences, not literal yes/no answers.
The closed `quoted_feature_presence_v1` rule applies only to Locking Feature
and Padlock Wing; `LOCKWING` alone does not prove Padlock Wing. Negation,
alternatives, optional/accessory descriptions and inference from absence remain
unsupported. A judge decision is recorded separately from human approval.

The optional canary records every grounded head-style interpretation and every
rejected answer. Zero or multiple grounded candidates are diagnostic outcomes,
not readiness failures. All grounded candidates and the spent call are carried
into the product's vendor pass; it consumes one of its two extraction slots.

`last_usage` starts as `{}` and describes the latest attempted provider request.
`call_records` retains independent copies of every record. A synchronous optional
callback receives another JSON-serializable copy. Records contain actual model
and deployment, response ID/status, schema name as `call_purpose`, effort, input,
cached-input, cache-write, reasoning, output and total tokens, prices, price basis, estimated cost, and
failure type. They do not contain prompts, output text, images, or credentials.
The instance is intended for sequential calls, not concurrent sharing.

Cost is `(ordinary_input * input_price + cached_input * cached_price +
cache_write * cache_write_price + output * output_price) / 1_000_000`.
Ordinary input excludes both cache-read and reported cache-write input. Reasoning
tokens are a subset of output tokens and are **not charged twice**. The service's
`usage.input_tokens_details.cache_write_tokens` is recorded if present, including
when the older SDK receives this newer field. If absent, `cache_write_tokens`
remains `None` and the estimate does not invent cache-write usage or surcharges.
Positive reported writes without a configured write price produce an unknown
estimate; zero or absent writes need no write price. Missing input/cached/output
usage or prices also produces an unknown (`None`) estimate, never a misleading
zero. This does not block execution. Partial usage fields remain unknown rather
than borrowing usage from a previous call. Estimates are not Azure invoices.

Refusal, truncation, failed status, empty output, and invalid schema output raise
`QualityModelResponseError`, retaining usage and invoking the callback. SDK
failures raise `QualityModelError` with the original cause; when Azure supplies no
usage, counts remain unknown. Invalid local configuration raises
`QualityModelConfigurationError` before sending. A callback failure is surfaced
after records are retained; if the response already failed, that original error
remains primary and receives a callback-failure note.

For offline tests, replace the public `model.client` with an object implementing
`responses.create(**kwargs)`, or patch the module's `AzureOpenAI` constructor.
Fake responses should expose the usual `status`, `output`, and `usage` fields
(objects or dictionaries). The focused tests additionally use the real SDK with
`httpx.MockTransport`, while denying sockets and credential acquisition.

```sh
.venv/bin/python -m pytest tests/test_quality_model.py -q
```

## Product quality extraction

`backend.quality_worker` loads the existing owner-scoped batch and cached PDF and
vendor evidence; it does not request new Document Intelligence analyses. Its
default first step checks Mueller Operating Head Style against the vendor row,
then processes all products in the same execution. `QUALITY_SMOKE_ONLY=true`
selects just that check; `QUALITY_SMOKE_FIRST=false` omits it on a subsequent run.
The original Ford PDF page is rendered in memory and supplied alongside its text.
The canary records its expected Lockwing/T1096 observation separately from the
model's interpretation. A different but grounded answer is retained with a review
qualification and continues to full extraction/judgment, rather than becoming an
additional readiness gate. Ungrounded responses and provider errors remain explicit.

Each product/tier packet contains the manifest, all attribute definitions and
product-scoped evidence. Catalog rows are selected by the model/part-number column,
including removal of variants not present in the batch and their duplicated
paragraphs. Shared component tables and family prose remain qualified evidence.
Explicit empty source attribute scopes retain their existing no-eligibility
meaning. Evidence references and source locations are preserved.

The tier order is PDF, vendor, manufacturer web, then other web, with unresolved
attributes alone advancing. Each tier permits extraction, one targeted second
pass, and a joint low-effort judge, at most three model calls. The first Mueller
vendor smoke occupies an extraction slot rather than adding a fourth call.
Proposals retain literal/derived/inferred origins, normalization or inference
justification, quotes, judge disagreements, conflicts and actionable reviewer
notes. Unit aliases use the same equivalences for normalization and grounding
(inch notation, psi/pounds per square inch, degree notation, and mm spellings);
alias-only matches record `unit_alias_v1`. A temperature degree sign does not
establish an angle unit. Pressure candidates remain visible without resolving the
definition question, and descriptive Lead-Free inferences require human review.
Documented derived abbreviation mappings include FIP/FNPT, MIP/MNPT, EPDM and
LLB (low lead brass). LLB also supports the base material brass, but plain brass
does not establish low-lead content. An arbitrary normalization explanation
cannot authorize an invented value.
Labeled Boolean yes/no/true/false answers retain literal support. Descriptive
Lead-Free candidates are always relabeled inferred with a review justification,
even if the model calls them literal or derived; an explicit negative answer
cannot be reinterpreted as a positive descriptive inference.

The worker connects all provider usage to one `QualityCostMeter`. Set
`QUALITY_RUN_BASE_COST_USD` to this logical run's build/base spending,
`QUALITY_OVERNIGHT_PRIOR_COST_USD` to earlier logical runs' spending, and
`QUALITY_WORKER_USD_PER_SECOND` to the disclosed active compute rate.
`QUALITY_COST_RUN_ID` defaults to the execution's `QUALITY_RUN_ID`; reuse a cost
run ID when manually restarting the same logical run, while giving each execution
a distinct run ID. Existing model/web charges then reload rather than reset.
Reusing the same `QUALITY_RUN_ID` also creates a new unique execution ID with
immutable execution snapshots and continued usage numbering; its root summary
is cumulative, not a second cost to add to individual execution snapshots.
`QUALITY_WEB_SEARCH_USD_PER_CALL` and `QUALITY_WEB_BROWSE_USD_PER_CALL` select
the disclosed WebIQ prices. Do not include earlier executions' usage again in
the base amount. Each execution summary includes the final meter snapshot;
unknown provider usage/cost remains unknown, never reported as free.

Web gap-fill uses `/search/web`, paid `/browse`, then independent retrieval of
the original page. Search and Browse default to USD 0.0125 per attempt; direct
HTTP retrieval has no WebIQ fee. Pending/unavailable Browse responses are recorded
without automatic polling or substitution of discovery snippets as evidence.
Per product/execution, at most 12 searches and six paid Browse attempts are made.
The worker prints the same persisted meter as `{"quality_cost": ...}` on exit,
including failures. Customer review packages contain five sheets
(Review/Evidence/Instructions/Summary/Questions); call Diagnostics are technical-only.
