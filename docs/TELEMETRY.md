# Offline telemetry and reviewer packages

This Track B work is code preparation and synthetic validation, **not** permission
to run services, deploy a build, retry work, change a pilot limit, or overwrite a
previous result or reviewer package. No monitoring value controls execution.
The existing approval, consumption guard, source policies and no-retry behavior
remain authoritative.

## Persistent timing and accounting

`EnrichmentResult.telemetry` is optional for historical compatibility. New
multi-source results contain `execution-telemetry-v1` with:

* Local monotonic item-processing elapsed milliseconds.
* One record per source tier, including non-attempted tiers, with elapsed time,
  retrieval time, extraction-plus-validation time, and outcome.
* New model requests, response-cache hits, model-call elapsed time, measured
  input/output tokens and unknown-usage call count.
* Explicit known token subtotals, separate from nullable complete measured totals.
* Estimated model cost using the approved per-token price inputs, **not billing**.
  The item receipt retains those two numeric model unit prices for reproduction.
* Reserved input/output bounds and cost, separate from measured usage; reserved
  search/direct-page attempts are distinguished from response success.

`RealBatchProcessor` joins existing inference/usage and reservation receipts.
There are no added provider calls, polls, SDK retries, model requests or guard
reservations. Reading a response cache contributes zero **new** model requests
and no newly measured provider usage or charge. Duplicate failure notifications
are not counted again. Missing usage remains `null`, not zero, and does not
become a reservation estimate. Missing price inputs leave estimated cost `null`.
`known_*` fields are explicitly partial subtotals when usage is incomplete.
Model cost excludes parsing, hosting, build and network costs; reserved costs may
cover other operations and are not summed into the measured-model estimate.

The result records pipeline time through extraction/accounting. The item-state
`elapsed_ms` additionally covers worker bookkeeping. Tier extraction time includes
validation and cache access; model request time covers the actual completion
wrapper invocation. These timings overlap and must not be added as independent
latencies. Concurrent item durations are **not** batch wall time or throughput.
There is no timing threshold, alarm-driven kill switch, automatic resubmission,
or modification of a budget window.

`BatchService.export()` retains the existing technical sheets and adds:

* **Telemetry**: per-item/per-tier timings, usage, missingness and reservations.
* **Telemetry Summary**: a small batch accounting/coverage summary, explicitly
  distinguishing missing results and uninstrumented historical results.

Technical exports remain private: their existing diagnostic, source, attempt and
hash columns are not the customer reviewer workbook.

### Python APIs

```python
from backend.telemetry import record_accounting, summary_report

record_accounting(telemetry, inference_receipts, reservation_receipts,
                  unit_prices=approved_prices)
report = summary_report(results, expected_items=20)
```

`record_accounting` joins already-observed receipts in memory. `summary_report`
returns JSON-ready `execution-summary-v1` data; it performs no I/O. It reports
literal candidates separately from `inferred_review_candidates`; neither is
measured accuracy. Missing results/usage make complete totals unknown rather than
silently reducing them. An empty, unprocessed batch must supply `expected_items`
to avoid being mistaken for a batch with zero expected items.

## Customer-facing reviewer workbook

```python
from backend.reviewer_workbook import build_reviewer_package, build_snapshot_reviewer_package

package = build_reviewer_package(
    selected_results,
    source_labels={"private-source-id": "Manufacturer data sheet"},
    attempt_metadata={"product-id": private_attempt_metadata},
)
# package.workbook: XLSX bytes
# package.private_binding: separate private JSON-ready binding
# package.summary: small JSON-ready presentation counts

# For a new answer-key template, include ALL immutable snapshot slots:
package = build_snapshot_reviewer_package(snapshot_record)
```

This API is offline and has no file writes. The caller must select exactly one
attempt per product, save a **new** workbook, retain the private binding separately,
and never replace earlier machine history or the first actual reviewer package.
Saved private files must be create-once with mode `0600`; new private directories
must use mode `0700`. Creating additional Track B artifacts does not change the
finite hash-bound baseline of existing historical receipts.
Duplicate Product ID + Attribute identities are rejected rather than silently
merged; MPN is validated context, not an alternative identity discriminator.
Formula-looking values remain literal text through the existing XLSX writer.
Long values use the existing lossless `Long text` sheet.

The **Review** sheet has stable columns, in this order:

```
Product ID | MPN | Attribute | Decision | Correction | Correction unit | Reason
Status | Next action | Proposed value | Unit | Evidence basis
Supporting quote | Evidence | Source URL | Retrieved at | Applicability
```

`Decision` is blank until a reviewer records `Approve`, `Correct`, or `Reject`.
Every decision requires a `Reason`.
No candidate index or other technical binding appears anywhere in the workbook,
including hidden columns or sheets. The separate private binding uses a zero-based
selection only when one candidate exists (or a prior explicit approval selected
one); conflicting proposals have no implicit selection. Correction and correction-unit
columns do not replace the original proposal. Existing persisted review decisions
may be displayed; package construction does not create any new review.

`build_snapshot_reviewer_package(snapshot: dict) -> ReviewerPackage` accepts the
complete immutable `AnswerKeyService` snapshot record (`id`, `body.owner`,
`body.batch_id`, and `body.slots`). It validates the snapshot content digest, then
renders every requested slot, including **No result available** rows. It always
blanks the four human-input fields (`Decision`, `Correction`, `Correction unit`,
`Reason`), regardless of older reviews. It never looks up the
current batch, substitutes a later attempt, or creates a synthetic machine result.
Private row bindings retain snapshot ID, item key, full product, attribute,
attempt key, original result SHA-256 and candidate indexes. These never appear
in the workbook; the scoring service persists them server-side.

The **Evidence** sheet shows product/attribute, proposed value/unit, sanitized
source label, permitted numeric page/table/cell coordinates, quote, original public URL, recorded retrieval time, applicability and
literal/inferred basis. URL credentials, queries and fragments are not exported.
Missing provider retrieval times say `Not recorded`; local observation time is
never substituted. Internal storage paths, hashes and attempt diagnostics are
omitted from the workbook. Known hash/URI patterns are redacted from display text;
source quotes still require operator review before external sharing. This is
structured minimization, not an arbitrary-secret detector.

`private_binding` retains the workbook hash, source versions, selected result
snapshot hash and caller-supplied attempt metadata. Its snapshot hash basis is
`validated_result_model_dump_json`; raw stored-object hashes, if required, belong
in supplied private attempt metadata. None of this binding is embedded in XLSX.
The snapshot adapter instead retains the exact original result hash from the
immutable snapshot; it does not replace that with a reserialized-model hash.
The scoring service can register either a newly generated or an already-sent
trusted sanitized workbook as its immutable baseline. Approval resolves its
private candidate selection; that selection is never added to customer columns.

### Actionable status mapping

| Internal condition | Reviewer presentation |
| --- | --- |
| Existing value | Existing value retained |
| Grounded proposal | Proposal ready for review |
| Descriptive-only Boolean | Descriptive inference — review required |
| Conflicting proposals | Conflicting evidence — review needed |
| Missing evidence | Supporting evidence needed |
| Source retrieval failure | Source unavailable |
| Invalid generated quote/value/citation | Evidence verification needs attention |
| Model/provider failure | Processing unavailable |
| Unresolved definition/unit | Definition needs clarification |

The customer workbook does not display the legacy technical failure label.
Missing evidence and failed verification remain distinct, with different next
actions. Descriptive inference is never presented as literal Boolean evidence,
certification, approval, or measured accuracy.

## Synthetic validation

`tests/test_scale_telemetry.py` blocks sockets/DNS and runs 10- and 20-item synthetic
batches, including concurrent execution, partial usage, cache accounting, exact
decimal estimates, persistent results/export, immutable rerun behavior, status
mapping, formula safety and private-binding separation. These tests do not change
the real pilot's product/request limits and are not a live capacity certification.

### Isolated repository-local regression command

Legacy `test_batch.py` / `test_batch_api.py` fixtures normally use pytest's
external scratch directory. For environments requiring all scratch files to stay
inside this repository, their normal `SQLiteStore` initializer correctly rejects
that location before tests can run. That produced two test failures and fifteen
setup errors, all with `Development batch storage must be outside the repository`;
it was not an extraction/export assertion regression.

After removing that setup blocker, the two hosted acceptance variants exposed
a Track B export-fixture regression: `scripts/release_fixture.py` required exactly
the eight historical sheets. Its verifier now accepts either those original
sheets or the complete additional `Telemetry` / `Telemetry Summary` pair,
validates the pair's two item rows and unknown usage/cost semantics, and retains
all original ownership, immutable-result, source and review assertions.
Negative tests reject a missing telemetry partner, a missing item row, or invented
zero measured usage. No production storage restriction was changed.

An explicitly loaded test-only placement adapter matches the existing real-worker
fixture technique. It allows only the exact `private`, `state`, and `private-upload`
homes below each test's own scratch directory, and only in the two legacy test
modules. It does not modify production code, other paths, storage methods, CAS,
leases, or guards. The plugin is not enabled by default in pytest or production.

```sh
mkdir -p .test-work
/Users/erikmontes/Desktop/repo/docintel/.venv/bin/python -m pytest \
  -p tests.local_sqlite_fixtures --basetemp=.test-work/track-b-closeout \
  tests/test_batch.py tests/test_batch_api.py tests/test_scale_telemetry.py \
  tests/test_multisource.py tests/test_real_batch_worker.py \
  tests/test_optional_web_gapfill.py tests/test_answer_key.py \
  tests/test_answer_key_api.py -q --tb=short
```

The scale test also verifies that this opt-in plugin leaves production
repository-storage rejection intact outside those exact legacy fixtures.
The 20-item test is bounded to 40 synthetic completion invocations and verifies
60 attribute result rows and 80 tier rows in the technical export. Five calls
deliberately lack usage: both result summary and export retain unknown complete
totals/cost, explicitly known subtotals of 420 input / 140 output tokens, and
separate reservations of 40,000 input / 81,920 output units.
## Vendor quotation locations

Reviewer evidence retains the cited row and, for structured vendor rows, displays
the cells containing the complete supporting quotation after Unicode, case and
whitespace normalization. Surrounding quotation marks are removed only for this
display match. These are labeled **quote matched cells**, not represented as
additional model-supplied citations. Multiple matching cells are all shown.
Without an exact cell match, the location remains explicitly row-level; unavailable
cell metadata is identified. Values, grounding decisions and original results are
unchanged.
