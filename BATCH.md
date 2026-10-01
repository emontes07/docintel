# Cloud Batch Integration

[README](README.md) | [User guide](docs/USER_GUIDE.md) |
[Architecture](docs/ARCHITECTURE.md) | [Deployment runbook](DEPLOYMENT.md) |
[Local-only pilot](PILOT.md)

## Status and Scope

This increment implements workbook intake, durable batch submission, a finite worker,
item exceptions, qualified Excel export, and hosted reviewer-claim validation code.
It is **not deployed or approved for customer processing**. No Azure resources,
permissions, customer workbooks, pilot decisions, or AI budgets were changed.

The portal starts at `/batches` (also the home page). Local `/pilot` remains a
development-only workflow. Both use the same evidence/review component. Batch
processing reuses the existing document parser, enrichment contracts, candidate
validation, provenance, qualifications, and review application logic.

## Walkthrough

1. Sign in with a delegated `Batch.Access` token. Upload one product manifest and
   one attribute workbook; explicitly bind the attribute-workbook reference used
   in the manifest. Validation does not submit AI work.
2. Inspect per-row association/type errors and unavailable-source warnings.
   All validation errors must be resolved before submission.
3. Submit once. Default `evidence_only` uses compatible cached parses, makes no AI
   calls, and does not manufacture attribute proposals. Live enrichment requires
  confirmation, the disabled-by-default server switch, exact approved pilot scope,
   and a separate expiring operator approval with durable budgets.
4. The scheduled worker picks up queued batches. A slice processes at most 100
   items with two threads, then exits. API status includes finished/unresolved/
   failed counts; item pages contain at most 100 rows (UI uses 50).
5. Filter unresolved, failed, or pending-review products. Open an item for its exact
   input row, identifiers, source status, original proposals, citations, and human
   decisions. Hosted reviewer and time come from validated server claims.
6. Export Excel. `Inputs`, `Definitions`, `Results`, `Evidence`, `Provenance`,
   `Errors`, `Reviews`, and `Batch` preserve original cells, all conflicting
   candidates, qualifications, selected approval candidate, corrections, identity,
   source versions, hashes, and exceptions. Oversized values are losslessly split
   into keyed ordered `Long text` rows. Every cell is a literal inline string,
   never an executable formula. Export is a documented per-item view, not a
   transactionally consistent whole-batch snapshot while reviews are changing.

The earlier synthetic browser run exercised upload, validation, submission, finite
execution, exception filtering, authenticated Excel download, and a persisted
synthetic rejection with unchanged machine hash. Desktop/mobile layout and
390-pixel page-overflow checks were performed. That eventual success was not an
unconditional repeatability pass: the user reported two Retry clicks whose causes
and restarted operations remain uncorrelated. See Browser Repeatability below.
No customer batch was submitted.

## Workbook Contract

Manifest columns: `PIMITEM Number`, `Vendor Name`, `MPN`, `Hierarchy Node`,
`PDF Tech Spec`, `Attributes to Fill`; all other columns are retained verbatim.
The supplied `Party ID` is retained but not substituted for the product identifier.
`Attributes to Fill` may be the exact uploaded-workbook reference, an exact
attribute name, or a JSON array of names. PDF references are exact registry names
or a JSON array of names. No URLs or filesystem paths are fetched from cells.

Definitions require `node`, `potential_attribute_name`, and
`potential_attribute_data_type`. `node` must exactly equal the manifest hierarchy.
Add explicit `value_type` (`string`, `number`, `integer`, `boolean`) when the declared
type is Enumerated or Multi-Select. Numeric definitions require a `unit` column;
an explicit blank means dimensionless. `allowed_values` is an optional JSON array.
Examples are preserved as input but NEVER used as allowed values or model answers.
Multi-Select currently needs an explicitly approved scalar representation; native
set-valued attributes are not supported by the reused enrichment contract.

The supplied customer definition workbook lacks a complete machine-readable unit/
enumeration contract. Exact hierarchy and typed mapping approval remains a gate;
the system does not infer those mappings from example answers.

Only data-only XLSX is accepted: one populated worksheet, row-1 unique headers,
contiguous rows, at most 10,000 products/256 columns/10 MiB per workbook/50 MiB
expanded content. Formulas, macros, external workbook links, merged cells, XML
entities, formatted numeric/date cells, ambiguous addresses and blank row gaps
are rejected rather than silently losing identifier or row provenance. Preserve
identifiers as text. Original workbook bytes are retained privately, not exposed
as public files. Supplementary vendor tables/websites remain retained inputs,
not retrieved sources in this increment.

## Storage and Execution

Hosted storage uses only `ManagedIdentityCredential`, explicit account URL, and a
dedicated existing private container. There is no connection-string, local-disk,
CLI-credential, public-container, or auto-provisioning fallback. The backend uses
its system identity; the proposed job explicitly selects its user-assigned identity.

Private keys: `configuration/sources.json`, `inputs/<batch>/`, `batches/`, `items/`,
`results/`, `reviews/`, `documents/`, `parses/`, `locks/`, `requests/`,
`analysis-attempts/`, `budgets/`. Machine results are immutable; reviews are separate
ETag-conditional records. Batch IDs bind owner, workbook hashes/reference and the
same registry snapshot used for validation. Request UUIDs bind batch and mode.

An operator must populate `configuration/sources.json` with this shape:

```json
{"sources":[{"reference":"synthetic.pdf","source_id":"synthetic","kind":"sharepoint","products":[{"item_id":"001","vendor":"Synthetic","mpn":"PART-1","hierarchy_node":"Valve"}]}]}
```

Blob entries use `kind: "blob"`, `blob: "documents/<approved-key>.pdf"`, a 64-character
lowercase `sha256`, and complete `products` associations. Duplicate source IDs or
references fail validation. SharePoint entries cannot claim a local blob/hash.
Registry/documents/compatible-cache upload is an operator task, not a portal upload
capability. No customer documents have been uploaded by this increment.

Parse reuse requires exact location, PDF SHA256, parser version, parsed-document
hash and source/cache-key agreement. Caches retain original parse time/origin.
Fresh analysis reserves a durable one-shot marker before calling DI. Interrupted
remote work is not automatically repeated. Batch leases renew every 15 seconds;
ETags and per-item reservations fence competing workers. Completed work is skipped
on restart; remaining items resume. An interrupted reservation without a result
becomes `interrupted`, requiring operator investigation, not blind retry.

Live processing remains constrained to the previously approved pilot identity,
two qualified attributes, and exact approved PDF hash. Optional
`configuration/live-approval.json` requires UUID `id`, `approved_by`, timezone-aware
`expires_at`, `analysis_limit` (0-1) and `inference_limit` (0-2). These are maximum
remaining ceilings, not newly granted budgets. Do not create/rotate approvals to
reset usage or run the old pilot in parallel against the same remaining allowance.
An operator must reconcile prior usage before approving any future live work.

The worker never sends a whole catalog in a model prompt. Each invocation receives
one product manifest and its explicitly associated sources. Storage scans/filtering
are bounded at intake but not indexed across all batches: very large histories
need load testing and a separately approved indexed queue/status design. No
high-volume cloud throughput/SLA claim is made.

## Existing Azure State (Read Only)

Last observed environment: an existing East US development resource group.
Resource/account/identity names are redacted here; use the approved environment's
values, never a name copied from historical examples. This documentation update
did not repeat Azure inspection.

- Frontend: `https://<frontend-host>`.
- Backend: `https://<backend-host>`.
- Older backend ready revision suffix: `--azd-1786594547`.
- Older frontend ready revision suffix: `--azd-1786594614`.
- Both active, healthy/running, single-revision mode, 100% traffic when inspected.
- No Container Apps Job exists in this resource group. Current images do not
  establish that the new batch/pilot code is deployed.
- Auth configuration inspection returned no established platform enforcement.
  Hosted sign-in/consent/token exchange has NOT been accepted end to end.

The existing backend system identity has Storage Blob Data Contributor and Blob
Delegator at the existing storage account, and Cognitive Services OpenAI User at
the existing model resource. Those broad pre-existing grants were
not added or changed. The new worker receives no inherited backend AI permissions.

## Approval Request

The separate [infra/batch.bicep](infra/batch.bicep) compiled with no diagnostics and
is NOT connected to `azd provision`. It targets approved existing storage, ACR,
and Container Apps environment parameters. Always override its environment-specific
default job name using the [canonical runbook](DEPLOYMENT.md#compile-and-activate-only-after-approval).
Proposed additions:

| Addition | Exact Scope / Impact |
| --- | --- |
| Private Blob container | `<storage-account>/blobServices/default/containers/<batch-container>`; no public access |
| Job | `<batch-job>`; same immutable backend image, finite module command, 1 vCPU/2 GiB, 600-second timeout, zero replica retries, every five minutes |
| User-assigned identity | `<batch-job>-identity`; attached only to job |
| Blob Data Contributor | Job identity, only the new container; backend already has account-level access |
| AcrPull | Job identity, only the approved existing registry |

No new storage account, Cosmos account, Search service, DI account, public data
endpoint, Graph grant, AI grant, registry password, or backend ARM job-start grant
is requested by this template. Private endpoint/DNS reachability from the existing
Container Apps environment must be verified before approval. Retention, backup,
operator write access and data classification for the container require approval.

Consumption impact: Blob capacity/transactions and existing log ingestion grow.
The schedule creates up to 288 short executions/day even when idle. A deliberately
conservative timeout bound is 172,800 vCPU-seconds and 345,600 GiB-seconds/day
(two scheduled executions can overlap); this is not expected idle usage. Apply
current East US consumption rates/free grants to those quantities before approval.
There is no quoted price or new fixed database tier. AI cost is zero while disabled;
future approved DI pages and model tokens are separately metered. Budget/alert and
retention configuration are not silently provisioned.

Entra changes require a tenant administrator/application owner:

- API app registration with v2 access tokens and delegated `Batch.Access` scope;
  audience is that API's client/application ID, issuer is the exact tenant v2 URL.
- Frontend confidential app delegated permission/consent for that scope, authorized
  client ID, and redirect URI `<frontend URL>/api/auth/callback/microsoft-entra-id`.
- Approved secret references for existing NextAuth client secret and AUTH_SECRET.
  Do not paste secret values into chat, scripts, commits or command history.

The canonical [settings and identity table](DEPLOYMENT.md#settings-and-identity)
lists backend, frontend, worker, and live-only settings by purpose, with no secret
values. In particular, the existing frontend TENANT_ID setting is consumed as the
provider's full issuer URL, not a bare UUID.

Tokens stay in the encrypted HttpOnly NextAuth JWT cookie and server proxy, not
public session JSON. Expired tokens require sign-in again; refresh-token rotation
is not implemented. Backend validates RS256 signature, issuer, audience, expiry,
tenant, object ID, authorized client and delegated scope. `X-DocIntel-Local` is
never hosted authorization. Batch ownership is uploader-only; cross-user sharing
is not implemented. Existing unrelated legacy API routes retain their own behavior
and require a separate access-control review before a whole-site production claim.

## Deployment Mapping and Release Gates

`azd deploy backend`: `azure.yaml` backend project/context `.` ->
`backend/Dockerfile` -> ACR remote build -> backend Container App ->
`fastapi run backend/main.py --port 80 --host 0.0.0.0` -> `/api/v1/batches` router.
The image now selects Python 3.13, pinned uv 0.11.31, selective backend/manifest
copies and `uv sync --locked`. It no longer silently accepts a stale frozen lock.

`azd deploy frontend`: project/context `frontend/` -> `frontend/Dockerfile` ->
Node 22 `npm ci`/Next standalone build -> frontend Container App `node server.js`.
Root and frontend Docker contexts exclude nested env files and customer XLSX/PDFs.
Neither deploy command creates or updates the worker. Worker image updates must be
explicit and use the same tested immutable backend image.

**Release blocked:** committed lock does not match the DI dependency and new direct
PyJWT crypto requirement. Installed-dependency tests are not a clean-install proof.
The existing package-host failure was not retried. Unrelated working manifest/lock
edits and private feed choices are not included in this increment. Restore approved
artifact access, resolve/review the public-source lock, then prove a clean image
build before any deployment. Do not bypass lock verification or change registries
as a workaround. Openpyxl/xlsxwriter were not installed; XLSX handling is bounded
stdlib OOXML with tests.

SharePoint remains blocked at the retained metadata 200 -> content 302 -> fresh
download 401 boundary. No new retrieval, cookie/auth workaround, bytes/hash claim,
or local-file fallback occurred. Tests use synthetic adapters. Actual cause/access
resolution and a separately approved re-test remain external gates.

Other acceptance gates: approved workbook mappings and source registry, actual
private Blob ETag/lease/network tests, Entra sign-in and negative-token acceptance
on Azure, worker scheduler/restart acceptance, resource/cost approval, clean lock,
and legacy surface security review. These are not substituted by local tests.

## Commands After Approval Only

Use the single [deployment command runbook](DEPLOYMENT.md#compile-and-activate-only-after-approval)
and its [rollback section](DEPLOYMENT.md#rollback-and-maintenance). Commands were
moved there and parameterized to prevent conflicting instructions or accidental
reuse of private environment names. Do not run `azd up`, `azd provision`, or the
inherited postprovision hook. Preserve existing settings, roles, private data and
budgets. No deployment, rollback, grant, deletion, push or customer batch is
authorized by this report; start future acceptance with synthetic data/live disabled.

## Offline Verification

For the preceding `f7be1bd` staged-source snapshot: **258 offline backend tests passed**, four integration
tests deselected; **4 frontend tests passed**; full ESLint, TypeScript, and Next
production build passed. The snapshot used existing installed dependencies, not a
new package install. The two unrelated working-tree tests were intentionally not
included. Final Bicep compilation returned zero diagnostics.

Protected private baseline verification passed through a read-only SQLite
connection: both original runs, their reviews, budgets, and all 12 recorded file
hashes match. Browser acceptance used only temporary synthetic data and unverified
development identity; hosted Entra/Blob acceptance remains blocked as listed above.
An inherited tracked frontend `.env.local` remains unchanged and is excluded from
both image contexts; review existing environment-file history separately before
making a whole-repository secret-hygiene claim.

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -m 'not integration' -q
cd frontend
./node_modules/.bin/eslint .
./node_modules/.bin/tsc --noEmit --incremental false
node --test tests/*.test.mjs
npm run build
```

Use existing installed dependencies until the lock gate is resolved. Stop the
owned dev server before production builds because both use `.next`. Development
batch testing requires explicit `DOCINTEL_BATCH_MODE=development`, a private
`DOCINTEL_BATCH_HOME` outside Git, and loopback binding. The API rejects this mode
in Azure. Frontend development additionally requires `DOCINTEL_BATCH_DEV=true`
and a loopback `DOCINTEL_BATCH_API_URL`. No local launcher changes are required.

## Browser Repeatability

Status on 2026-10-01: **three consecutive warm-server read-only attempts passed
after fixing a new automation defect; the two user-reported Retry incidents remain
unresolved.** This supersedes any unconditional browser-acceptance interpretation
of the earlier report. No production/mobile-device/cloud reliability claim follows.

### Retained Evidence And Initial Failures

The retained Copilot transcript `c16d7e02-cdfb-4aa5-9bfe-32d7354e199b`, browser
events, and frontend/backend terminal output were inspected. The transcript has
tool invocations and completion flags, but older detailed tool-result payloads are
not consistently retained; `success:true` marks tool completion, not assertion
success. Available debug output contains session-start entries, not Retry UI events
or agent/request failure telemetry. Neither reported Retry click can be assigned
to browser automation, an agent/task restart, or an application operation. Both
clicks remain separate unresolved incidents, with no established user-facing cause.

Known observations are not substitutes for that missing correlation:

| Earlier observation | Classification and follow-up | Remaining limit |
| --- | --- | --- |
| Local-pilot mobile call at 2026-09-30 00:47:31 UTC used a label selector; the 00:47:40 call used an exact combobox role and explicit replay-text readiness. | Automation selector/readiness was changed. | Initial detailed failure payload is unavailable; neither call is identified as a user's Retry. |
| Earlier application retry experiments initially read already-visible results; a later recorded request-ID comparison and stale development cache fix were reported. | Separate application-operation retry tests, not evidence of the two UI Retry clicks. | Earlier commentary is retained, but full payloads are incomplete. |
| Stabilization mobile checks around 2026-10-01 03:25 UTC were followed by desktop checks and an eventual-pass summary. | The completion record does not prove first-attempt acceptance. | No retained error or Retry event establishes which operation was restarted. |
| Initial batch navigation took 10,475 ms; first route compilation took 9.2 s and the browser navigation deadline was 10 s. | Cold development readiness/tool timeout. Use document readiness followed by actual API and DOM checks. | Cold compilation was not retested in the six-attempt sequence below; no timeout was increased. |
| Batch validation returned 403, followed by a Submit-button wait failure. | A real same-origin defect: browser `127.0.0.1` versus reconstructed Next `localhost`. Fixed in `f7be1bd`; valid loopback requests subsequently reached validation, while an alien Origin remained 403. | Could affect legitimate users of that configuration; not established as mobile-specific or as either Retry cause. |
| Two detail page errors read undefined `PIMITEM Number` while frontend changes were hot-reloaded against an older running backend. | Development frontend/backend response-shape mismatch; restarting the backend loaded the matching contract. | Not repeated against matched running versions; no production skew acceptance claim. |
| Selecting All and immediately reading rows returned the preceding Pending row, while a later snapshot showed both rows. | Confirmed automation synchronization flaw. The new harness waits for the exact GET response and the exact rendered product-ID list. | No production filtering defect was established. |
| Browser request context used unsupported `Storage.getCookies`; sandbox lacked `structuredClone`. | Tool compatibility failures; same-origin browser fetch and supported operations were used. | Not mapped to either reported Retry. |

Other retained failures were setup/check failures, not proven mobile failures: a
nonexistent test-file command, a synthetic `/var` versus `/private/var` source-key
mismatch, and missing generated Next types during an earlier dev/build interaction.
They are not silently recast as passes. This investigation neither built alongside
the running dev server nor installed dependencies.

### Controlled Attempts

The dependency-free `frontend/tests/batch-browser.acceptance.mjs` exports
`runBatchBrowserAttempt(page, { batchId, attempt, startup })` for a Playwright-compatible
page. In the integrated browser tool, its function body was evaluated directly
(without the module `export`) and invoked sequentially in one execution for attempts
1-3, then in one execution for attempts 4-6 after the one-line fix. A deferred tool
result was resumed, not re-executed. There were no manual Retry clicks, automatic
retry wrappers, sleeps, timeout increases, or disabled assertions.

All six used already-running frontend/backend servers, warm route compilation,
fresh document navigation per viewport, and the same browser context. None was a
cold server/browser start or a physical mobile device. Each attempted desktop
1440x1000 followed by mobile 390x844. The only data was the existing temporary
two-product Synthetic batch `557b2ad7cccd922f2018b1d32a43f180472fdca33fe5e1ce32abb752d40f9735`,
in `evidence_only` mode with its prior synthetic rejection. No new customer data,
AI calls, workbook submission, or review decisions were made by browser acceptance.

| Attempt | UTC start-end, 2026-10-01 | Startup | Desktop / mobile checks | Overall outcome | Non-batch aborted requests |
| --- | --- | --- | --- | --- | --- |
| 1 | 12:37:18.708-12:37:22.922 | Warm | Both completed | **Failed:** harness `URL is not defined` | 2 environment HEAD, 2 Next RSC |
| 2 | 12:37:22.922-12:37:26.929 | Warm | Both completed | **Failed:** same harness error | 2 environment HEAD |
| 3 | 12:37:26.930-12:37:30.984 | Warm | Both completed | **Failed:** same harness error | 2 environment HEAD, 1 Next RSC |
| 4 | 12:38:19.744-12:38:24.066 | Warm | Both passed | **Passed** after fix | 2 environment HEAD, 2 Next RSC |
| 5 | 12:38:24.066-12:38:28.142 | Warm | Both passed | **Passed** | 2 environment HEAD |
| 6 | 12:38:28.142-12:38:32.132 | Warm | Both passed | **Passed** | 2 environment HEAD |

Every attempt observed All=2, Pending=1, restored All=2, loaded Pressure Rating and
the recorded review, and no queue/detail page-level horizontal overflow in both
viewports. Every export returned 200, `no-store`, XLSX MIME type and ZIP signature
(5,833-5,834 bytes). Batch summary and row-2 result/review were unchanged. Every
attempt recorded zero console errors, page errors, HTTP errors, failed batch API
requests, or attempted writes. The first three nevertheless **failed**, because
their final failure-classification assertion could not execute.

The new harness's failure was the unavailable global `URL` constructor in the tool
sandbox, distinct from the browser context where `URL` exists. The one-line fix
compares against the known batch API base instead. A focused VM check without
`URL` proved that batch failures remain fatal and unrelated requests are classified
separately. This affects automation only and does not explain the historical Retry
clicks. The first local syntax-check command used the wrong retained working
directory and failed with module-not-found; rerunning from the root passed without
a source change.

Non-batch failures are retained, not erased or counted as batch failures: all were
`net::ERR_ABORTED`. The environment HEAD originates from the existing layout's
best-effort resource preload; the other requests targeted Next's `?_rsc=` navigation
URLs. Cancellation timing is consistent with navigation, but its precise initiator
was not instrumented. Those events did not prevent the asserted queue/detail/API
readiness. Zero console warnings were captured inside these six attempt windows;
the browser's surrounding event history also contains preload warnings. This is
not a claim of zero browser/network events outside the checked workflow.

### Write Safety And Regression

The harness aborts and records every non-GET/HEAD/OPTIONS `/api/**` request, and
fails on any attempted write. Before/after comparisons protect the checked recorded
batch and review. It uses exact accessible roles, arms response waits before each
action, and waits for exact rendered IDs after filtering. Error listeners belong
to each attempt and are removed afterward; reports retain errors even on failure.

The added offline lost-response regression in `tests/test_batch.py` replays the
same submission request, then the same synthetic review. It verifies one batch,
one review, unchanged persisted versions/bytes, rejection of the second decision,
and no completed-item reprocessing. These writes occur only in pytest's temporary
synthetic store, never in the recorded browser fixture or customer storage.
All **11 batch tests passed**, including conditional-write/lease and concurrent
worker protection. The focused harness syntax/sandbox check passed. The existing
broader verification above remains evidence for `f7be1bd`, not a fresh full-build
claim for this test/documentation-only follow-up.

Remaining gate: collect timestamped Retry UI/agent/tool failure telemetry on a
future recurrence, correlate it to the operation and server requests, then retest
that exact path. Cold-start, real-device, hosted identity/storage, and deployed
repeatability remain unverified. No app fix for the uncorrelated Retry incidents
is justified by the retained evidence. No deployment, grants, customer processing,
dependency changes, or additional AI consumption occurred.

## Documentation Handoff

The documentation workstream followed the settled implementation and repeatability
findings. It changes documentation only, not runtime behavior or activation state.
The presentation reference informed organization only; the prose and Mermaid
diagrams are original to DocIntel.

| File | Coverage |
| --- | --- |
| [README.md](README.md) | Concise Excel-led entry point, business workflow, local versus hosted status, gates, template lineage and MIT license link. |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | Exact workbook headers and a two-product synthetic example; validation, execution/cost gates, progress, exceptions, immutable proposals, decisions and actual Excel sheets/columns. |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Original target and last-observed-state Mermaid diagrams; code/infra traceability; blocked SharePoint, conditional DI/model calls, durable state, worker, review/export, and inactive optional components. |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Single command authority for compile, reviewed provisioning, separate API/frontend/job deployment, local development, hosted acceptance and rollback; named settings with placeholders, no secret values. |
| [PILOT.md](PILOT.md) | Cross-links, local-only identity limits, placeholder paths/source selection, and corrected browser acceptance qualifications. |
| [DOCKER.md](DOCKER.md) | Prominent legacy-reference warning; not an alternative to current activation gates. |
| [docs/ENRICHMENT.md](docs/ENRICHMENT.md) | Retained single-product CLI/evidence/review contracts and prior README diagnostic caveats, including the reported connectivity observation without customer document locations. |
| [BATCH.md](BATCH.md) | Consolidated findings, redacted environment names, canonical runbook links and this handoff. All six browser outcomes and both uncorrelated Retry incidents remain recorded. |

Checks performed for documentation:

- Relative file links and Markdown heading anchors checked; shell blocks parsed
  with `bash -n`, without executing deployment or live commands. Image/entry-point
  and command arguments were compared with `azure.yaml`, both selected Dockerfiles,
  the worker CLI, and Bicep job definition. No new Bicep compilation or Azure check
  is claimed for this documentation-only change.
- 31 UI labels, 18 named runtime settings and documented export header names
  matched their code owners. An initial check incorrectly included a shell image
  placeholder among runtime settings; the table-scoped check passed afterward.
- The exact two-product Markdown tables were converted to XLSX and accepted by
  the actual importer with blocked-SharePoint warnings. A mismatched workbook
  reference was rejected. A temporary synthetic no-AI worker/export check confirmed
  unresolved rows, no proposed values, pending reviews and populated export headers.
  Networking was blocked; no recorded browser fixture or customer store was written.
- Both Mermaid diagrams parsed and rendered as nonempty SVG with the existing
  installed editor bundle (20 and 7 nodes). A temporary loopback renderer was used;
  no package/image-generation dependency or remote render service was added.
  No screenshots or customer images were added to the repository.
- Whitespace and scoped diff review before the documentation commit. The existing
  README diagnostic edits were retained in the linked technical guide; unrelated
  code, migration notes, dependency manifest/lock, and local files remain outside
  this documentation checkpoint.

Remaining mismatch: source implements the batch paths, but the last observed Azure
site serves older revisions and has no scheduled batch job. Hosted auth, private
Blob persistence/leases/networking, scheduler/restart acceptance, clean images,
SharePoint bytes, general live scope and catalog-scale measurements remain gates.
The earlier local tests are not rebranded as deployment or production acceptance.
No push, deployment, grants, customer processing, AI calls or dependency changes
were performed. The scoped documentation commit hash is supplied in the accompanying
handoff response; it cannot be embedded in its own commit without changing the hash.