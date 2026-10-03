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
conversions or full semantic correctness. Conflicts remain separate, existing
values remain unchanged, and partial source failure can coexist with proposals.
Real-pilot proposals additionally require verbatim supporting quotations and
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