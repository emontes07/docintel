# Four-product append-only continuation

This is a **new, single-use** continuation of the retained four-product batch,
not a retry of the failed gapfill deployment. Run the operator as a module:

```text
python -m scripts.four_product_continuation --help
```

No planning function loads `.env.local`, contacts Azure, starts readiness, stages
source, or creates an approval. Private data and receipts must remain in the
existing owner-only release root, never Git or CI. JSON comparisons use parsed,
canonical records; raw capture hashes only verify the captured bytes.

## Actual stopped PREP and authorized continuation

The v4 API claim/reservation succeeded, then credential construction failed
before DI authentication/submission. The retained stopped snapshot has 35
records, no SDK operation ID, no completed parse/cache, and reserved totals of
two analyses/ten pages. All earlier executions and reservations remain consumed.
Do not rerun the whole helper, refund, reset, or allocate another reservation.

`ford-continuation-owner-authorization-20261006T233051Z.json` separately permits
one continuation of that existing reservation after focused CI and exact native
SDK no-send validation. The guard additionally requires the fixed
`operations/ford-analysis-preparation/<source-sha>/continuation/` records:
`attempt.json`, `authorization.json`, `plan.json`, `failure-before.json`,
`program.json`, and `completed.json`. The completion must bind the original
completed PREP receipt, cache, ledger, operation ID and original reservation.
Its plan must retain the pre-submission failure and native DI no-send/CI proof.
The original local failure is historical evidence, not the remote
`operations/ford-analysis-preparation/<source-sha>/failure.json` rejection marker.
Claim-only state never satisfies readiness.

## Required order

1. Preserve the latest `gapfill-actual-outcome.json`, its 29 unchanged historical
   records, failed deployment, closure, publication and readiness receipts. The
   actual baseline has the newer API image, the older manual-worker image and
   unchanged frontend. Never assume API and worker already match.
   Also preserve the mode-0600
   `four-product-owner-authorization-20261006T201622Z.json` capture. Its
   `capacity_additions: null` / pending decision remains immutable: it is broad
   consent including the separate PREP exception, **not** capacity, evidence
   that PREP executed, or a readiness clock. Later measured capacity approval
   belongs in a new separate receipt, never an edit of that capture.
2. Complete the separately approved **one Ford pages-1–5 PREPARATION analysis**
   before readiness. Its immutable completed/analysis receipts and compatible
   cache must be captured alongside the actual post-PREP ledger. PREP must not
   charge a worker execution. No local/synthetic parse can replace that cache.
   The guard requires all ten immutable PREP records: `attempt.json`,
   `authorization.json`, `ledger-before.json`, `submitted.json`, `parsed.json`,
   `analysis-receipt.json`, `completed.json`, `reserved.json`, `packet.json`,
   and `storage-program.json`. The latter is a JSON envelope containing the
   storage-only program source and its UTF-8 source hash, not a raw `.py` record.
   Any `failure.json` blocks
   continuation even if a completed record exists. Canonical cache/ledger hashes,
   operation ID, requested/returned model/API, page range/count, SDK-result hash
   (not raw HTTP), authorization, source, parser, timestamps and the single
   no-execution reservation must agree. Raw capture hashes are retained only
   for capture integrity; JSON record checks remain canonical.
   The sole new cache is
   `parses/2d9239607c598350d1cdd3ca96ee84acc4e0eb5c2cf11ee5c312492b774c63f3.json`;
   the private gate calls the unchanged `BatchProcessor.cached()` for both Ford
   bindings. Synthetic PREP rehearsal output never satisfies this actual-cache
   prerequisite.

   **Identity/grant distinction:** retained
   `four-product-existing-role-evidence-20261006.json` establishes that the worker
   and approved operator already have Cognitive Services User at the exact DI
   resource. The API identity lacks that DI grant; the operator lacks Blob-data
   access. No new worker grant is needed and **no IAM changes are authorized for
   this run**. The separately approved split route uses the API managed identity
   for reservation/archive/cache storage and the existing operator's local
   `AzureCliCredential` for the single DI call, without transferring tokens.
   Its user approval is retained separately in
   `four-product-preparation-identity-authorization-20261006T221236Z.json`.
   The v4 narrow approval requires `existing_operator_di_access_verified=true`
   and `operator_analysis_api_storage_approved=true`, not an API DI grant.
   Its packet binds the approved operator/tenant/subscription and original API
   principal. The guard verifies actual SDK identity metadata, no token storage,
   the explicitly false local token-signature-verification claim, and the local
   PDF's equality to the API-verified Blob hash/ETag. It reconstructs the charged
   pre-analysis ledger from the preserved baseline and checks `reserved.json`,
   proving the single no-execution charge preceded local SDK analysis.
   Both new owner approval and existing-role evidence files are semantic,
   finite history pins even though their names start with `four-product-`.
   Never assert a nonexistent API DI grant to satisfy a legacy check. The later
   model worker uses both PDF caches and performs no fresh DI analysis.
3. Measure all **12 complete** payload reservations: PDF and vendor-table
   requests for each product, plus one optional manufacturer-web request each.
   `payload_entry(item_key, tier, prepared)` validates complete production
   `PreparedInferenceRequest` accounting, including instructions/schema/framing;
   its `payload_sha256` is the production request's `version` digest.
   `capacity_request(ledger, approval, row_amendment, plan,
   duration_assumptions=...)` computes a request,
   not authority. Every plan entry contains `item_key`, `tier`, `input_tokens`,
   `output_tokens` and the complete `payload_sha256`.
4. Obtain a separate explicit capacity receipt. `amendment_scope(...)` refuses
   absent/unapproved/mismatched receipts; input capacity is never guessed. The
   receipt has `approved`, `approved_by`, `approved_at`, `receipt_id`, and the
   exact fields returned by `capacity_request`. Consumed reservations are never
   reset or refunded.
   Omitting duration assumptions leaves them explicitly pending (`null`).
   `amendment_scope` and the guard reject that pending value: the owner's exact
   capacity decision must include the conditional duration forecast.
5. Pin the actual reviewed committed source, including authorized Track B,
   four-product, schema and pacing work. The source review supplies `approved`,
   `revision`, `base_revision`, exact `tree`, `files` (every changed path and its
   SHA-256), and `workstreams`:
   `["track_b", "four_product", "write_schema", "pacing"]`.
   There is no historical f56 file whitelist. Any frontend change fails this
   backend-only continuation. Source staging is an explicit separate action.
   A content-identical native enum correction can use an append-only
   `four-product-source-selection.json`, following the existing row-rerun
   selection pattern. It retains every baseline field except the reviewed
   source revision/review and adds the original canonical baseline hash as
   `supersedes_baseline_sha256`. The original baseline, source, gate, decision,
   receipts and allowances remain untouched. The successor source is staged
   under `four-product-v1/canonical-write-enums`; its new gate, decision and
   merged CI must bind that exact source before readiness. This grants no
   additional attempt and does not start or renew any clock.
   Captured CLI ingress `transport: "Auto"` is projected to the pinned wire
   enum `"auto"` without changing its meaning. Only unique case-insensitive
   matches to an existing enum are projected; unknown values still fail and
   direct outgoing wire validation remains case-exact.
6. Run the real-worker/real-guard private gate on the **actual post-PREP** snapshot
   and independently approved scope. Then require merged CI for that same exact
   revision, authenticated owner/batch/image/amendment evidence, and create-once
   readiness. Local synthetic tests are not substitutes for this private gate.

```text
python -m scripts.four_product_continuation prepare --work ROOT \
  --revision COMMIT --source-review PRIVATE_REVIEW --postprep-snapshot POSTPREP.json
python -m scripts.four_product_continuation stage --work ROOT \
  --revision COMMIT --approve stage
python -m scripts.four_product_continuation check --work ROOT --decision PRIVATE_DECISION
```

`decision_from_gate(...)` constructs a decision without writing it or starting
a clock. The caller must retain that reviewed private decision using create-once,
mode-0600 writes. Directories are mode 0700.

Only after every prerequisite passes, separately approved `ready`, `publish`,
`deploy`, and `activate` actions are available. `--approve ACTION` is mandatory.
`close` remains available after expiry or failed activation; a closed authority
cannot be reused. This implementation task itself authorizes none of those
live actions.

## Boundaries

* Products: Mueller 213030/245747 and Ford 225830/221315, as bound by the original
  batch; all four receive unique immutable attempt result paths. Every prior
  item, result, review, diagnostic, approval and audit remains preserved.
* One new 2-vCPU/900-second backend build, no frontend build. An attempt receipt
  is written **before** upload/build; the full 900 seconds is rechecked before
  each boundary. Unknown outcomes never permit another submission.
* The original unsliced authority remains supported, but the subsequently
  approved timing fallback uses **two distinct 600-second slices**, concurrency
  1, two items each, zero retries. Each native start and guard charge requires
  the full 600 seconds remaining in the same at-most-20-minute active window.
* Readiness anchors a 45-minute publication window and 90-minute operating
  window. Deployment readiness anchors the single 20-minute processing window.
  None can be renewed. Before writing either readiness receipt, the approved
  `ready` action verifies the target, runs shared
  `release.preflight_deployment` against captured backend/job shapes, then reads
  current model capacity, requiring
  exactly 30,000 TPM. That fresh metadata and its check timestamp are retained;
  missing or changed capacity blocks the clock without changing quota.
  The native no-send report is retained in readiness and explicitly labeled
  captured-shape-only: unknown future image/upload/schedule requests are not
  claimed as prevalidated and must pass their exact immediate pre-send gates.
* At most 8 internal and 4 optional-web inferences, 2 internal/1 web per product;
  one WebIQ `/web` discovery per eligible product, no browse, at most six direct
  pages with explicit balanced `2,2,1,1` item allowances. Public queries contain
  only reviewed manufacturer, MPN and attribute terms; no SharePoint.
  `execution_order` is Mueller 213030, Ford 225830, Mueller 245747, Ford 221315.
  Finish each product's internal/optional-web work before advancing. The bound
  `page_limits` also appears in each public scope's `max_direct_page_attempts`,
  preventing the first products from consuming the final vendor's page allowance.
* Cached parses only in the worker: **zero additional analysis**. The PREP charge
  and actual page usage are pinned dynamically, not assumed to remain 1/5.
* The $5 envelope includes the prior consumed build **$0.0232268896**, the PREP
  completed actual-page cost, one new build, the authorized worker slice(s), model requests and web
  searches. It does not stack old forecasts.
  `fixed_cost(original_completed_prep, approval)` uses the verified
  `analysis_receipt.actual_page_count` at the approved `analysis_page` rate and
  rounds the consumed build upward once. One actual page at 10,000 microdollars
  yields **23,227 + 198,000 + 10,000 = 231,227** fixed forecast microdollars.
  The original five-page/50,000-microdollar reservation and every ledger counter
  remain unchanged; unused reserved pages are not current PREP spend or a refund.
* Every app/job write uses `scripts.azure_write_schema` projections and
  validation. The existing owner key may bind only through the shared approved
  namespace-independent `gapfill_continuation.send_existing_secret_patch`
  transport with the four-product clock callback, never a copied GET payload, ad-hoc curl, new credential,
  permission or infrastructure change.
* Global guard failures stop the queue. In the four-product scope, missing
  WebIQ credentials are a **fatal native preflight failure** at the first web
  tier, before any search reservation: only the first product's two internal
  requests have run and the remaining three items stay untouched. This is not
  an eight-internal-request fallback; legacy-scope admission remains unchanged.
  Advisory usage/status errors do not
  stop or restart a worker; only authorization expiry stops the reserved
  execution. Pressure-definition gaps remain gaps, never fabricated findings.

## APIs and tests

`backend.real_pilot` provides `FOUR_PRODUCT_KEY`,
`FOUR_PRODUCT_AUDIT_KEY`, `validate_four_product(...)`, stable
`guard.four_product`, and `guard.active_recovery`. `before_execution` charges
one new execution; `prepare_recovery` captures all four prior states before
conditional recovery writes. The retained original `guard.recovery` is never
hidden or temporarily cleared.

Focused synthetic tests:

```text
python -m pytest tests/test_four_product_continuation.py \
  tests/test_four_product_image_smoke.py \
  tests/test_four_product_worker.py tests/test_four_product_private_gate.py tests/test_real_pilot.py \
  tests/test_gapfill_rerun.py --basetemp=.four-product-test-work
```

The private test is opt-in, with these environment variables:

* `DOCINTEL_TEST_FOUR_PRODUCT_WORK`: retained release root.
* `DOCINTEL_TEST_FOUR_PRODUCT_SNAPSHOT`: actual post-PREP snapshot root filename.
* `DOCINTEL_TEST_FOUR_PRODUCT_SCOPE`: root JSON with `amendment_scope` and
  `payload_plan`; it must contain the separately approved exact capacity receipt.
* `DOCINTEL_TEST_FOUR_PRODUCT_DOCUMENTS`: existing hash-identical document copies.
* `DOCINTEL_TEST_FOUR_PRODUCT_OUTPUT`: optional new `four-product-*.json` gate.

Gate output requires exact reviewed committed source. It exercises all 12
requests, the zero-key fatal/two-internal-request case, fatal queue stopping, zero
worker analysis, all four prior attempts, immutable old results, owner isolation,
and attempt-history export. Completions, discoveries and pages are always
**REPRODUCTION ONLY**, never actual extraction findings.

## Selected two-slice timing fallback

The later owner capture `four-product-capacity-owner-approval-v1.json` approved
**+7 requests / +303,895 input reservation units / +14,336 output tokens** and,
only if needed for timing, one additional 600-second execution. The unchanged
twelve-payload plan remains **303,895 input / 24,576 output**. The historical
single-worker private result (523.453546 seconds including local work;
559.453546 conservative) failed the newly required 90-second margin.
Reducing the 31-second spacing is not justified by that result.

The selected scope adds exactly these three fields:

```json
{
  "worker_slices": [["row-2", "row-4"], ["row-3", "row-5"]],
  "slice_authorization": "<unchanged parsed owner capture object>",
  "slice_authorization_sha256": "<canonical SHA-256 of that object>"
}
```

`slice_authorization` has exactly: `schema_version`, `approved`, `approved_by`,
`capacity_request_file`, `plan_sha256`, `additional_inference`,
`additional_input_tokens`, `additional_output_tokens`, `original_worker_seconds`,
`additional_worker_execution_authorized_only_for_two_slices`,
`additional_worker_seconds`, `full_sequence_minimum_slack_seconds`,
`timing_fallback_order`, `all_other_scope_and_limits_unchanged`,
`no_retry_authority`, `readiness_clock_started`, and `user_confirmation`.
The capture has **no invented approval timestamp**. The existing capacity
receipt retains its own `approved_at`; its unchanged plan digest and exact
additions must match the new capture. The two worker durations are 600, the
additional execution is 1, minimum slack is 90, and no-retry/unchanged-limits
flags must be true. The fallback-order strings are validated literally against
the retained capture. `readiness_clock_started` remains false.

Pass `worker_slices=...` and `slice_authorization=...` to both
`capacity_request(...)` and `amendment_scope(...)`. Only that bound authority
permits `capacity_approval.additional_executions=2`. The helper neither modifies
the original twelve-payload plan nor creates approval. Pass
`worker_slices=...` to `fixed_cost(completed_prep, approval, ...)`: it adds
**18,000 microdollars only**, giving **249,227** fixed forecast microdollars for
the actual one-page PREP. The retained five-page reservation is untouched.
The owner's updated total forecast is **$1.3985368896**, within the same $5
envelope; previous forecasts are not stacked.

Worker/guard contract:

* `before_execution(key)` appends one execution and sets
  `active_slice_index` (0 or 1) and `active_slice_item_keys` (the corresponding
  two-element list). Cross-slice operation keys/reservations are rejected.
* The first `prepare_recovery(fence)` captures all four prior states once in
  the existing root audit. The second preparation never rewrites that audit,
  prior state, first results, or counters.
* The worker runs `item_limit=2`, selects only the active slice, leaves later
  `recovery_ready` items queued, writes final batch state, then calls
  `guard.finish_slice()`. This method is a no-op for unsliced authority.
  Only `completed`/`unresolved` own-item states with persisted results and a
  duration within 600 seconds can produce worker-terminal success.
* Each slice gets create-once
  `operations/real-pilot-four-product-slices/<amendment-sha>/slice-1/`
  (or `slice-2/`) `attempt.json`, `prepared.json`, and `completed.json`.
  The attempt retains its entire pre-charge ledger. Completion binds attempt,
  prepared/root audit, own state/result hashes, remaining-item hashes and the
  terminal ledger. First-slice records/results/charges remain immutable when
  admitting slice two. Failed, incomplete, timed-out, repeated or third slices
  cannot create further authority.

The operator derives policy `{worker_executions:2,item_limit:2}` with all other
policy values unchanged. It configures/enables once, then calls
`start(..., slice_index=0)`, `observe(..., slice_index=0)`, and only after success
the corresponding index-1 calls. Both native starts retain their own original
job snapshot and exact schema-projected template. Each rechecks a full 600
seconds **after** native no-send construction and immediately before submission.
The second start first requires Azure `Succeeded` and a read-only verification
of the actual first worker-terminal audit, ledger and result/state hashes.
API/image proof admission still occurs before the single activation attempt.
One `finally` closes processing; no slice failure triggers a retry or new clock.

Local receipts use `four-product-worker-slice-1-{template,attempt,result,terminal}.json`
and the corresponding `slice-2` paths. Native start proofs use each slice's
`worker-slice-N-start` namespace; advisory usage and expiry-stop receipts are
also slice-specific. `worker-slice-1-guard-terminal` retains the verified remote
terminal digest before slice two. Observations stop only the specific execution
at its own 600-second limit or the original active deadline, whichever is first.
Readiness remains 45/90 minutes, with one active window of at most 20 minutes.

The private profiler runs the **real worker and guard twice** using the actual
post-PREP cache and captures the same aggregate 12 model / 4 WebIQ / 6 direct-page
native preflights. Call `validate_pacing(..., worker_slices=...)`. Its `slices`
list contains two objects with `slice_index`, `item_keys`, six complete
fingerprinted `requests`, two `web_delay_events`,
`simulated_worker_elapsed_seconds`, `measured_local_worker_seconds`,
`worker_elapsed_seconds`, `forecast_including_local_seconds`,
`remaining_margin_seconds`, `local_measurement`, and
`remote_storage_latency_measured`. Each conservative forecast is
`5*max(31,response)+response+startup+web/2+measured_local`, and must be **≤510**.
The top-level requests/timing totals must equal those two traces; top-level
remaining margin is the smaller slice margin. With 22/35/150 assumptions,
each conditional pre-local forecast is 287 seconds. This is a conservative
scenario, not proof of provider rate estimation or measured remote latency.
The parent must run the final actual-cache private gate on the reviewed source.

## Pacing is an independent readiness blocker

The following single-worker timing details are retained as historical evidence
and for unsliced compatibility; they do not satisfy the newer 90-second margin.

The retained deployment is **30,000 TPM**. Historical recovery's fixed
61-second interval puts request 12's earliest start at **671 seconds**, even
with zero service latency or bookkeeping. It cannot fit this 600-second worker.
Only the new four-product scope uses **31-second start spacing**.
`backend.real_pilot.FOUR_PRODUCT_INFERENCE_INTERVAL_SECONDS` exports that fixed
value; any other value in the new amendment is rejected. Historical recovery
keeps its 61-second pacing. The ledger continues reserving complete **byte-based**
request bounds without weakening or treating them as provider rate estimates.
Historical usage (including the observed 5,022-token maximum input) is not the
provider's rate estimate either. No tokenizer/codec dependency or quota increase
is required or authorized by this continuation.

The capacity receipt's `duration_assumptions` is explicit and immutable:

* `basis`: `"empirical_provider_latency_conditional"`.
* `deployment_tpm`: `30000`; `inference_interval_seconds`: `31`;
  `worker_timeout_seconds`: `600`.
* `historical_max_model_seconds`: `21.42772`.
* `model_response_allowance_seconds`: at least `22`.
* `startup_bookkeeping_seconds`: at least `35`.
* `web_network_allowance_seconds`: at least the provisional `150`.
* `forecast_seconds`: exactly
  `11 * max(31, response_allowance) + response_allowance + startup + web`.
* `provider_rate_estimate_verified`, `byte_bounds_used_as_rate_estimate`,
  `billing_usage_used_as_rate_estimate`: all `false`.

The provisional 22/35/150-second assumptions yield **548 seconds**, not a
guarantee. Higher measured/conditional allowances must be supplied when needed;
any forecast reaching 600 seconds blocks readiness. Responses longer than the
start interval are counted serially, not hidden behind the 31-second spacing.
The owner's exact capacity decision must include these duration assumptions.
The interleaved synthetic schedule with 37.5 seconds of web delay per product
ends at **512 seconds**, because 36 seconds of web delay overlaps pacing waits.
The conservative additive forecast remains 548 seconds, leaving **52 seconds
before measured local overhead**, not a provider-latency guarantee.
In particular, `4 searches × 15s + 6 pages × 15s = 150s` is only a
**scenario allowance**. WebIQ/direct-fetch timeouts apply to phases, not total
wall time; system DNS resolution has its own resolver timeout. Neither those
timeouts nor this simulation establish a 150-second network deadline.
If measured startup/bookkeeping needs 50–70 seconds, those owner-approved
assumptions produce 563–583 seconds **before** adding measured local overhead.
Do not silently retain 35 seconds or enlarge any deadline/quota to obtain a pass.
The operator additionally calls:

```text
validate_pacing(proof, actual_payload_plan,
              execution_order=scope["execution_order"],
              interval_seconds=scope["inference_interval_seconds"],
              duration_assumptions=scope["capacity_approval"]["duration_assumptions"])
```

The `proof` requires the same empirical `basis`, `duration_assumptions`,
`deployment_tpm=30000`, `quota_changed=false`, `ledger_byte_bounds_preserved=true`,
`worker_timeout_seconds=600`, the three false rate-estimate flags,
`worker_elapsed_seconds`, and twelve ordered `requests`. Each request binds
`item_key`, `tier`, `payload_sha256`, `input_byte_bound`, `output_tokens`,
`start_seconds`, and `end_seconds`. The trace must preserve sequential products,
31-second minimum start spacing, at most two starts in each rolling 60 seconds,
and the exact complete-payload byte reservations. It does **not** assert that
provider rate estimates equal those reservations or observed billing usage.
Four `web_delay_events` bind each product's full quarter of the web allowance
between its vendor-table response and web-model start. The output also requires
`simulated_worker_elapsed_seconds`, positive `measured_local_worker_seconds`,
`local_measurement="perf_counter_whole_real_worker_with_memory_store"`,
`remote_storage_latency_measured=false`, and `forecast_including_local_seconds`.
Whole-worker wall time measures actual local parsing, request preparation,
ledger/result serialization and in-memory persistence work. The gate adds that
entire measured interval to both simulated elapsed time and the conservative
forecast, without assuming it overlaps pacing. The resulting forecast must
remain strictly below 600 seconds. This does not measure remote Blob-storage
latency, Azure admission delay or future provider latency.
The proof must explicitly set `web_allowance_is_hard_deadline=false` and
`phase_timeouts_bound_dns=false`.

Per-item `perf_counter` measurements span each running-status write through its
finished-status write, including local parsing and result/ledger persistence.
The report retains `measured_item_local_seconds`, the four-item subtotal,
the row-2/row-3 Mueller subtotal, and shared local work outside item spans.
`prior_two_comparison="same_run_mueller_item_subtotal_not_historical_replay"`
prevents treating that subset as a replay or observation of the historical
two-product worker. It includes the current optional-web path, unlike that
historical internal-only run. Cold-process startup and remote persistence are
not measured by this comparison. `remaining_margin_seconds` explicitly equals
600 minus the conservative forecast including measured local work.

The private scope bundle supplies `pacing_profile` with these headers and a
positive `simulation_response_seconds` within the approved response allowance
(for example, 2.1 seconds is a synthetic test duration, not a provider forecast).
The gate records the **real worker pacing path** on a synthetic clock, applies
the full web-network allowance across the four products, independently measures
local work with `perf_counter`, and fills the request trace from actual prepared
payloads. The supplied forecast separately accounts for model latency,
startup/bookkeeping and web-network allowances.
It emits `pacing_evidence` only after validation. Synthetic timings and the
provisional 548-second forecast are not observed live performance or guarantees.
No readiness is allowed until the actual Ford cache, complete payload plan,
conditional duration forecast, and full private gate pass.
Passing timer-unit simulations alone never establishes readiness.

## Mandatory native-client no-send validation

Before each DI, model, WebIQ, direct-page GET or deployment/publication/start call, the shared
implementation must construct the exact production client and request against
the installed SDK with an in-memory/no-send transport. A failure is a release
blocker, not an optional-provider error. This does not authenticate, send a
provider request, or prove provider acceptance; any local transport stubs are
never represented as actual provider responses.

The private worker gate exercises `preflight_structured_request` on all twelve
complete prepared requests and `WebIQSearchClient.preflight_search` on all four
authorized public queries, plus `preflight_original_page` on all six balanced
direct-page GETs, before their corresponding reservations. Its `provider_preflights`
metadata binds item/tier/payload fingerprints, exact native request digests,
public host filters and installed SDK/transport versions. The six
`web_retrieval` entries bind each item and URL hash to the genuine native
`http.client`/`_PinnedHTTPSConnection` wire/header hashes, Python/OpenSSL versions,
and zero DNS/socket/TLS/authentication/send counters. Only safe metadata is
retained; these no-send checks do not prove DNS safety or page content.
Readiness rejects
missing, failed, changed or mismatched receipts. The live clients self-gate each
attempt too. The gate also proves an SDK preflight failure stops the queue
without reserving the failed provider operation. Invalid discovery URLs remain
policy-data rejections, not attempted retrievals or SDK-compatibility failures.

Deployment paths reuse the shared native CLI/HTTP no-send adapters; they do not
copy transports. The native job-start constructor receives the captured job
resource and its pinned 2025-01-01 start contract; PATCH/PUT remains on the pinned
2024-03-01 contract. Readiness and start both use
`release.writable_execution_template`: execution overrides omit volume
redefinitions, preserve the captured job unchanged, and normalize optional
null/read-only members against the action schema. Start projects the final
approved command/environment edits before saving the override. Unknown root members still
fail closed. Specific-execution stop uses the shared validated route,
not an unmodeled native stop path. Applicable full-window checks run again **after** no-send construction
and immediately before sending. Publication wrappers compose the existing
shared build-window callback with their own full-900-second publication check;
neither callback replaces the other. Exact dynamic upload/image/schedule requests
must be checked at their real boundaries, not replaced by prospective samples.
The shared `on_preflight` hook synchronously writes create-once
`four-product-<operation>-<operation_stage>-preflight.json` files, bound to the
decision and canonical attempt/resource/payload/execution digests. Publication
retains `publication_upload_metadata`, `binary_source_upload`, and
`publication_schedule_run`; secret binding retains distinct
`worker_secret_redacted` and `worker_secret_credential_bound` records.
PATCH/start/expiry-stop use `azure_write` under distinct operation names.
Metadata is saved before the final clock callback and send. Persistence failure
blocks submission; existing records are never replaced or treated as renewed
authority or successful service results. CLI fixture-response flags are
retained honestly, unlike native HTTP's no-response-fixture proofs. No
credentials, raw bodies, headers, or URLs are added to these records.
The continuation's DI proof uses the exact approved PDF bytes and `pages=1-5`;
focused CI must include the real `AzureCliCredential` production-argument
no-send regression. The guard enforces the frozen 31-field metadata-only DI
receipt and five-field CI envelope with named, successful, positive-integer run
IDs. These complete objects are covered by the canonical continuation-plan
hash, not an invented separate no-send hash. Rehashing a changed plan and its
linked authority/audits cannot make malformed proof or CI fields acceptable.

### Mechanical image-proof admission

`validate_image_smoke(work, decision) -> dict` reads the separate, owner-only,
non-symlink `four-product-image-sdk-smoke.json`. `activate()` calls it before
writing `activation-attempt`, configuring, enabling, or starting anything.
Invalid/missing evidence fails before entering activation/closure side effects.
The activation attempt pins the accepted envelope's canonical hash as
`image_sdk_smoke_sha256`. This consumer never runs the producer, a console
executor, provider clients, or another worker.

The parent must create this **exact seven-field envelope** once with
`release.save_once(path(work, "image-sdk-smoke"), envelope)`:

```python
envelope = {
    "schema_version": 1,
    **binding(decision),  # decision_sha256 and target
    "observed_api_revision": operator_verified_api_revision,
    "observed_api_image": operator_verified_immutable_api_image,
    "observed_worker_image": operator_verified_immutable_worker_image,
    "proof": actual_image_side_producer_result,
}
```

The observations are the parent's external deployment verification, not
container self-attestation. Both full image references must equal the
decision-bound published backend reference, including its immutable digest.
The API revision is the observed Azure revision name, distinct from the
reviewed Git revision. Existing resource-drift validation remains in place.

`proof` is the unchanged return value of the committed
`backend.sdk_image_smoke.collect_image_smoke(...)`, produced in the inspected
API image. Its exact fields are:

```text
schema_version, status, source_revision, image_digest, image_identity_basis,
code_sha256, lock_sha256, installed_sdk_versions, python_version, network,
blocked_operation_attempts, native_requests, request_basis,
authentication_performed, provider_send_performed, worker_execution_started
```

Admission requires `schema_version=1`, `status="validated_no_send"`, the reviewed
decision revision, the published digest, and these exact producer labels:

* `image_identity_basis="operator_verified_deployment_and_build_not_self_attested"`
* `network="python_dns_socket_process_and_real_credential_operations_denied"`
* `request_basis="complete_prepared_planning_inputs_actual_runtime_requests_self_gate"`

Authentication, provider-send, and worker-start flags must be false.
`blocked_operation_attempts` must contain exactly `network`, `credential`, and
`subprocess`, each integer zero. This is a Python-operation denial claim, **not**
a network-namespace claim; the existing startup smoke's `network="none"` is
not interchangeable evidence.

Every `code_sha256` entry is checked against bytes from
`git show <reviewed-revision>:<path>`. Paths must be canonical `backend/...`
paths or `uv.lock`/`pyproject.toml`; the minimum manifest is:

```text
uv.lock
pyproject.toml
backend/sdk_image_smoke.py
backend/sdk_preflight.py
backend/batch_worker.py
backend/core/llm.py
backend/core/websearch.py
backend/core/websearch_webiq.py
backend/models/enrichment.py
```

`lock_sha256` must equal the verified lock-file hash. Installed `openai`,
`httpx`, `azure-core`, `azure-identity`, `azure-ai-documentintelligence`,
`azure-storage-blob`, and `pydantic` versions must match that reviewed lock.
`httpx2` is additionally permitted/required when the native model transport
uses it, and must also match the lock. No host-version comparison is used.

`native_requests` must retain unchanged image-produced model, WebIQ, and
original-page receipts, with complete prepared-request metadata, matching
image-installed package versions, false authentication/send/fabrication flags,
and zero original-page network/credential counters. Page Python versions must
match the image proof; OpenSSL observations must agree within that image,
not with the operator host. No extra DI-image proof is required.

Local `validate_provider_preflights(...)` and deployment `preflight_recorder(...)`
remain **operator-host-only** interfaces. Never route image evidence through
them or relabel host/startup receipts as image evidence. The image receipt
grants no capacity, renews no authority, and is not a prerequisite for the
separately approved host-native Ford DI continuation. Runtime provider requests
still self-gate.
