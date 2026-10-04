# Deployment And Maintenance

[README](README.md) | [Architecture](docs/ARCHITECTURE.md) | [User guide](docs/USER_GUIDE.md)
| [Batch findings](BATCH.md) | [Local pilot](PILOT.md)

> **WIP: NOT READY TO MERGE OR DEPLOY.** No commands below authorize deployment,
> provisioning, new role assignments, or customer processing. The committed lock
> lacks a consistent resolution of `azure-ai-documentintelligence==1.0.2` and the
> direct PyJWT crypto requirement. Clean installation/image creation remains
> blocked. Existing installed-dependency tests and Bicep compilation are not a
> release. Do not bypass lock verification, hooks, signing, or CI; do not change
> registries or repeat failed artifact-host probes as a workaround.

This is the canonical batch activation command guide. [BATCH.md](BATCH.md) owns
the consolidated evidence, cost/approval request and remaining gates;
[PILOT.md](PILOT.md) owns local single-product setup and operation. Legacy Docker
and template guides are not an alternate authorization path.

## Current State And Prerequisites

As of the last read-only inspection recorded in [BATCH.md](BATCH.md#existing-azure-state-read-only),
the Azure frontend/backend were healthy but served **older revisions**, no worker
job existed in the inspected group, and hosted batch authentication was not
accepted. This documentation update did not query or change Azure.

Before activation, approve the target subscription/group/region, existing Container
Apps environment, ACR and storage account, private network/DNS reachability,
retention/backup/data classification, costs/alerts, and operator access. Reconcile
the lock through approved artifact access, preserving unrelated version pins, then
prove a clean locked image build and offline regression pass. The previous public
artifact-host failure was fetching package metadata with `Socket is not connected`;
working-tree lock edits are not proof of a reproducible release.

Required maintainer tooling: approved Azure CLI and azd, Bicep compiler, image-build
capability, and authorized resource/role administration. Use the narrowest approved
permissions; do not prescribe blanket Owner access. No secret values belong in
commands, docs, logs, screenshots, or Git. The local frontend environment file is
no longer tracked; inherited literal `dummy` values were resolved as placeholders.
Keep excluded checkpoint history out of the publication branch.

SharePoint remains metadata 200 -> content 302 -> download 401. Do not add cookie,
auth-forwarding, or local-file workarounds. A separately approved access correction
and bounded retrieval acceptance must precede a claim of working SharePoint input.
Approved workbook type/unit mappings and full source associations are also gates.

## What Each Operation Means

| Operation | Effect and current limit |
| --- | --- |
| Bicep compilation | Validates template syntax/types locally; does not verify Azure permission, policy, quota, network, runtime or application behavior. |
| Infrastructure provisioning | Creates/changes resources and grants. The separate batch template must be reviewed and authorized; it is not wired into azd provisioning. |
| Local packaging | Isolated locked installation, offline tests and Next build; no image publication or Azure changes. |
| Image build/publication | Separate approved ACR builds from reviewed source/context, producing immutable digests; no app deployment. |
| Backend/frontend deployment | Applies approved digests and hosted settings; does not start the worker or prove hosted acceptance. |
| Worker deployment/update | Explicit separate manual job operation with the same tested immutable backend image; no automatic processing. |
| Local development | Loopback, explicit development identity, private SQLite; not hosted identity/persistence acceptance. |
| Hosted acceptance | Authorized synthetic tests on the deployed revisions, including identity isolation, Blob conditional writes, manual continuation and interruption recovery. |

## Image And Entry-Point Mapping

Verified against [azure.yaml](azure.yaml):

| Service | Actual build path | Runtime |
| --- | --- | --- |
| `backend` | Project/context repository root; [backend/Dockerfile](backend/Dockerfile); ACR remote build; Python 3.13, uv 0.11.31, `uv sync --locked`. | `fastapi run backend/main.py --port 80 --host 0.0.0.0`; batch router `/api/v1/batches`. |
| `frontend` | Project/context `frontend/`; [frontend/Dockerfile](frontend/Dockerfile); Node 22, `npm ci`, Next standalone build. | `node server.js`, port 3000. |
| Worker | Same tested backend image; [job module](infra/modules/containerAppJob.bicep) overrides the HTTP command. | `/app/.venv/bin/python -m backend.batch_worker --concurrency 2 --max-batches 1 --item-limit 1 --synthetic-acceptance --batch-id <validated-id>`; one-item slice followed by continuation. |

The alternate Dockerfiles under `docker/` and local Compose files are not selected
by azd. A successful local Next build does not establish clean backend packaging.
Stop the owned dev server before any production build: dev/build share `.next`.

Do **not** use `azd up`, `azd provision`, or the inherited `postprovision` hook for
this increment. [infra/main.bicep](infra/main.bicep) is a broader template, and
[azure.yaml](azure.yaml) contains automatic private-link approval in that hook.
Do not run `azd down` as batch cleanup; it can remove unrelated resources/data.

## Settings And Identity

Supply settings through the approved configuration/secret-reference process.
Examples are placeholders, not actual environments. Do not print `azd env get-values`
or dump runtime environments into shared logs.

| Setting | Where and purpose |
| --- | --- |
| `DOCINTEL_BATCH_STORAGE_URL` | Backend/worker: approved Blob account URL, e.g. `https://<storage-account>.blob.core.windows.net`. |
| `DOCINTEL_BATCH_CONTAINER` | Backend/worker: dedicated existing private container; no auto-create or public-container fallback. |
| `AZURE_CLIENT_ID` | Worker: its user-assigned managed identity client ID. Backend normally uses its system identity. |
| `DOCINTEL_AUTH_TENANT_ID` | Backend: exact tenant UUID for issuer/JWKS and token checks. |
| `DOCINTEL_AUTH_AUDIENCE` | Backend: API registration application/client ID, matching the access token audience. |
| `DOCINTEL_AUTH_CLIENT_ID` | Backend: authorized frontend client UUID (`azp` claim). |
| `DOCINTEL_BATCH_API_URL` | Frontend server: `https://<backend-host>/api/v1/batches`; not a browser-public token setting. |
| `DOCINTEL_PORTAL_ORIGIN` | Frontend: exact trusted HTTPS origin with no trailing slash for hosted mutation checks. |
| `DOCINTEL_API_SCOPE` | Frontend: `api://<api-application-id>/Batch.Access`; must not be left at the provider's unrelated fallback scope. |
| `AUTH_MICROSOFT_ENTRA_ID_ID` | Frontend: confidential client application ID. |
| `AUTH_MICROSOFT_ENTRA_ID_TENANT_ID` | Frontend: despite its name, consumed as the **full issuer URL**, `https://login.microsoftonline.com/<tenant-id>/v2.0`. |
| `AUTH_MICROSOFT_ENTRA_ID_SECRET` | Frontend: approved secret reference for the client credential, never its value in docs. |
| `AUTH_SECRET` | Frontend: approved secret reference protecting the auth cookie. |
| `DOCINTEL_BATCH_LIVE_ENABLED` | Worker: keep `false`; `true` alone does not override scope, UI consent, or durable approval/budget gates. |
| `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` | Worker only if separately authorized for fresh analysis: dedicated advertised DI endpoint. The batch template does not provision this service or grant access. |
| `LLM_ENDPOINT`, `LLM_DEPLOYMENT` | Worker only if separately authorized for inference: exact OpenAI-specific endpoint and structured-output deployment. Legacy `AI_FOUNDRY_ENDPOINT` is a fallback, not proof of correct routing. |

The API registration must issue v2 access tokens and expose delegated `Batch.Access`.
The frontend needs consent for that scope and callback
`https://<frontend-host>/api/auth/callback/microsoft-entra-id`. The API validates
signature, issuer, audience, tenant, expiry, object ID, client and scope. Access
tokens stay server-side/in encrypted HttpOnly auth cookies, not public session
JSON. Expiry requires signing in again; refresh-token rotation is not implemented.
Batch ownership is uploader-only, not shared-team review.

Hosted storage uses `ManagedIdentityCredential`, not local CLI credentials or
connection strings. The batch template creates a job identity, container-scoped
Blob Data Contributor and registry-scoped AcrPull grants; it does not inherit the
backend's existing broader roles or add Graph/AI grants. Confirm all scopes and
effective permissions through the approved change process. Legacy routes and
platform-wide protection remain a separate security gate.

Operator-managed `configuration/sources.json`, document bytes, and compatible parse
caches reside in the private store. Their contract is in [BATCH.md](BATCH.md#storage-and-execution).
`configuration/live-approval.json` is not an environment variable or portal form;
do not create/rotate it to reset consumed budgets. General live-catalog processing
is not enabled: any later live scope requires private identity/attributes/hash approval.

## Compile And Activate Only After Approval

Run from the clean publication checkout. `REVISION` is the full reviewed commit;
`WORK` and `FIXTURE` are fresh private directories outside Git. `CFG` is an
owner-only target JSON using [the example schema](scripts/release.example.json),
containing approved resource identifiers and secret-reference names, never secrets.
The job name is required explicitly. Commands below do not themselves grant approval.

### Local Packaging And Tests

Use an existing approved artifact-access environment with Python 3.13, uv 0.11.31,
Node 22 and public PyPI/npm access. Do not retry a known failing artifact path
without a concrete changed condition or inherit alternate-index overrides.

```sh
python3.13 scripts/release.py stage --revision "$REVISION" --work "$WORK"
python3.13 scripts/release.py package --work "$WORK" --package-access-approved
az bicep build --file infra/batch.bicep --stdout > /dev/null
```

Packaging resolves the missing DI/direct PyJWT lock metadata, rejects unrelated
version churn, then runs locked installation, focused backend tests, npm ci,
lint/type checks, frontend tests and Next build. PyJWT already exists transitively.
Review and commit only the required generated lock delta. Restage the final commit
in a new private directory and repeat packaging. Run the full offline suite with
that clean environment, then `python -m scripts.release_fixture --output "$FIXTURE"
--verify`. This does not build an image or prove Azure authentication.

### Image Build And Publication

After explicit ACR build/publication and cost approval, use the final clean receipts:

```sh
python3.13 scripts/release.py context --work "$WORK"
python3.13 scripts/release.py publish --config "$CFG" --work "$WORK" --approve publish
```

The tool builds the root/backend and frontend contexts separately in the approved
registry, records immutable digests and requires the committed lock/source hashes
to match clean checks. It never uses the inherited registry or mutable `latest`.
Each ACR build explicitly requests **two CPUs** and a **900-second** timeout in
the ARM `DockerBuildRequest`: `agentConfiguration: {"cpu": 2}`, `timeout: 900`.
Azure CLI **2.77.0 does not support `az acr build --cpu`**; the release helper
does not call that command, rely on historical defaults, upgrade the CLI, or
modify its SDK. It uses the existing registry's ARM API **2019-04-01**:

1. Create the immutable local attempt record and archive only the verified,
   curated context. Reject links, private evidence, secrets, or permission drift.
2. `az rest --method POST --url "$REGISTRY_ARM_ID/listBuildSourceUploadUrl?api-version=2019-04-01"`.
3. PUT that archive as `BlockBlob` to the returned `uploadUrl`; put only the
   returned `relativePath` in `sourceLocation`, unchanged. `SourceUploadDefinition`
   defines `uploadUrl` and `relativePath` as strings; the installed CLI uploads to
   the former and returns the latter directly. Neither contract requires a
   `source/` prefix or a `.tar.gz` suffix. Treat the relative path as opaque,
   subject to local safety bounds: at most 4096 UTF-8 bytes, no absolute/URI paths,
   traversal, empty segments, controls, backslashes, malformed escapes, or
   ambiguous encoded separators/double escaping. Compare decoded path segments
   at exact boundaries against the trusted HTTPS SAS Blob URL; do not reinterpret
   a suffix as a different blob or synthesize a new provider path.
   Before validation, save `*-publication[-replacement]-upload-metadata.json`
   containing only response/value types, byte lengths, path hashes, and shape
   flags. Rejections include this safe summary. No raw provider paths, URLs,
   query strings, or SAS signatures appear in these diagnostics.
   The short-lived SAS stays in
   process memory/stdin, never command arguments, receipts, or printed errors.
   No redirects or upload retries are enabled.
4. Save the exact request and its source/helper/context/archive hashes, then
   `az rest --method POST --url "$REGISTRY_ARM_ID/scheduleRun?api-version=2019-04-01" --body "@$REQUEST"`.
   A separate `x-docintel-publication-attempt-id` header carries the immutable
   local attempt ID; it is not an ARM idempotency key. CLI 2.77.0 generates its own
   `x-ms-client-request-id`, so the helper does not claim to persist that value.
   The request uses `type: DockerBuildRequest`, one exact revision-tagged image,
   `isPushEnabled: true`, `isArchiveEnabled: false`, Linux/amd64, and the above
   explicit CPU/timeout. The relative Dockerfile is `backend/Dockerfile` for the
   root context or `Dockerfile` for the frontend context.
5. Immediately preserve the returned run ID. Poll only that run with read-only
   GETs for at most twenty minutes, shortened by any replacement approval expiry.
   Verify actual `agentConfiguration.cpu == 2`, successful status, one exact
   output image, and agreement between its digest and the repository lookup.

Subprocesses are time-bounded and there is exactly one `scheduleRun` invocation
per attempt, with no application retry. Empty/unknown submission responses
(including HTTP 202 without a run ID), failed runs, missing CPU evidence, digest
mismatches, and observation timeouts all stop without resubmission. A local
observation timeout does not prove the remote run stopped; inspect the consumed
run read-only. The request's 900-second service timeout remains in force.
Only verified successful builds produce accepted image receipts. This route uses
the same existing ACR build/upload services and identity as the CLI; it creates no
new registry, task resource, agent pool, role assignment, or service. If existing
permissions are insufficient, stop—this tool never grants more.

The schema and examples are the official
[2019-04-01 RegistryTasks specification](https://github.com/Azure/azure-rest-api-specs/blob/main/specification/containerregistry/resource-manager/Microsoft.ContainerRegistry/RegistryTasks/stable/2019-04-01/containerregistry_build.json).
An offline release-host regression checks the installed CLI's real command
contract and SDK serialization in addition to transport simulations. It also
executes the installed `az rest` with the production arguments against a
loopback-only HTTP stub, using isolated `AZURE_CONFIG_DIR`, disabled telemetry,
and `--skip-authorization-header`. The stub verifies both component request bodies,
including CPU/timeout, and absence of an Authorization header. This proves real
CLI parsing/serialization, not Azure authorization or successful remote execution.
Representative opaque-path regressions are synthetic, not evidence of a failed
provider response's actual shape. A rejection by the former prefix/extension
restriction proves an unsupported local assumption; when the raw response was
not retained, its actual value remains unknown. This offline correction grants
no replacement attempt: consumed original/replacement receipts remain consumed,
and no frontend-only partial publication should be started without authority.

Full and component publication share atomic, create-once
`backend-publication-attempt.json` / `frontend-publication-attempt.json` records
in the same prescribed private `$WORK`. Each record is persisted before its ACR
request: at most one backend and one frontend attempt are permitted, including
failed or unknown outcomes and concurrent/rerun commands. Existing attempts,
results, progress or accepted publication records are not reset or migrated.
This enforcement is **work-directory scoped**. A different directory is not renewed
approval; never discard receipts or switch directories to evade a consumed
allowance. Review an unknown outcome without repeating the paid request.
Record those digests in the private target configuration before baseline capture.

#### One Explicit Replacement For The CLI-Rejected Backend Attempt

The separately authorized recovery retains the consumed original backend attempt.
It permits exactly **one additional backend attempt**, plus the still-unused
original frontend attempt, in the **same prescribed private work directory**.
It does not increase the approved $10 ceiling, two-CPU/900-second per-build
bounds, worker allowance, or service scope. Do not stage a new work directory or
replace the already reviewed source/check/context receipts to retry publication.

Supply an independently reviewed, owner-only JSON file using
`--publication-replacement-approval`. The private target must also contain
`publication_deadline`, a timezone-aware ISO timestamp representing the separately
approved absolute deadline. Missing, malformed, timezone-naive, or expired target
deadlines fail replacement closed. Do not publish the actual deadline or incident
source revision in repository files. Adding this target field changes
`fingerprint(config)`; bind the replacement approval to that exact updated private
target. Its exact required fields are:

| Field | Required value/binding |
| --- | --- |
| `schema_version` | Integer `1` |
| `approved` | Literal `true`, only after actual approval |
| `id`, `approved_by` | Canonical UUIDs; approver already in target `pilot_operator_ids` |
| `target` | Existing `fingerprint(config)` of the exact private target JSON |
| `work` | Canonical absolute path of the original prescribed work directory |
| `revision` | Exact full 40-character source commit from the original attempt and `source.json` |
| `component` | `"backend"` |
| `original_attempt` | Canonical absolute path to that work's `backend-publication-attempt.json` |
| `original_attempt_sha256` | SHA-256 of the original attempt's **unchanged raw file bytes**, not reserialized JSON |
| `original_failure` | `"unsupported_acr_build_cpu_argument"`; operator attests this pre-submission failure after reviewing remote state |
| `additional_attempts` | Integer `1` |
| `cpu`, `timeout_seconds` | Integers `2`, `900` |
| `expires_at` | Active timezone-aware ISO timestamp, no later than the private target's active `publication_deadline` |

No extra fields are accepted. An original queued/request/submission/result
receipt disqualifies this narrow pre-submission recovery. The tool cannot infer
absence of a remote run merely from absence of a receipt; the approver must
independently verify the incident. A full 900 seconds must remain before **each**
upload-URL request, context upload, and run submission, including an immediate
recheck after persisting the submission receipt. The same expiry bounds both the
backend replacement and original frontend in the full publication. If backend
processing leaves less than 900 seconds, frontend publication stops before
requesting an upload URL or uploading its context; there is no implicit extension.

```sh
python3.13 scripts/release.py publish \
  --config "$CFG" --work "$WORK" --approve publish \
  --publication-replacement-approval "$REPLACEMENT_APPROVAL"
```

The component actions accept the same option and retain their existing
`--previous-work`/preserved-image checks; the option never grants another frontend
attempt. The backend replacement always consumes the single fixed
`backend-publication-replacement-attempt.json`, even after failure, unknown
outcome, concurrency, or an edited approval UUID. The original record is neither
deleted, reset, nor migrated. Request/submission/queued/result files use the
corresponding `backend-publication-replacement-*` prefix; the frontend retains
its original `frontend-publication-*` prefix. Changing approval IDs, component
paths, or full-versus-component commands cannot renew either slot.

`source_revision` records the exact prior reviewed, staged/CI-validated source;
`tool_revision` and
`tool_sha256` separately record the executing fixed helper. The helper's new
revision does not silently replace the older reviewed image source. Existing
validation and publication receipts remain untouched.

#### Explicit Supplemental Metadata Preflight And Conditional Backend Attempt

Only a **new, explicit private authorization** can enable this narrowly scoped
supplement. It does not reopen the original or replacement backend records,
increase the approved spend ceiling, change services/roles, or change worker
allowances. It is accepted only for full `publish`, never component publication,
and is mutually exclusive with `--publication-replacement-approval`.

The owner-only file supplied through `--publication-supplemental-approval` must
contain exactly these fields:

| Field | Required value/binding |
| --- | --- |
| `schema_version`, `approved` | Integer `1`, literal `true` after actual approval |
| `id`, `approved_by` | Canonical UUIDs; approver in private target `pilot_operator_ids` |
| `target`, `work`, `revision` | Exact target fingerprint, canonical absolute prescribed work path, and original full reviewed source revision |
| `original_attempt` | Canonical absolute path to that work's `backend-publication-attempt.json` |
| `original_attempt_sha256` | SHA-256 of its unchanged raw file bytes |
| `replacement_attempt` | Canonical absolute path to that work's `backend-publication-replacement-attempt.json` |
| `replacement_attempt_sha256` | SHA-256 of its unchanged raw file bytes |
| `metadata_requests` | Integer `1` |
| `additional_backend_attempts` | Integer `1` |
| `cpu`, `timeout_seconds` | Integers `2`, `900` |
| `expires_at` | Active aware ISO timestamp bounded by the private target's active `publication_deadline` |

Both old records must match the exact backend source and bounds, without
upload/submission/run-result evidence that contradicts the narrowly approved
pre-upload failures. No old record is erased, edited, or reclassified.

```sh
python3.13 scripts/release.py publish \
  --config "$CFG" --work "$WORK" --approve publish \
  --publication-supplemental-approval "$SUPPLEMENTAL_APPROVAL"
```

The command first atomically creates `metadata-preflight-attempt.json`, then
performs exactly one `listBuildSourceUploadUrl` request on the existing approved
registry. This standalone metadata stage does **not** archive/upload context,
call `scheduleRun`, or reserve a backend build. The shared pure
`validate_publication_upload` function checks the required strings, bounded
opaque path, trusted HTTPS Blob URL, exact path binding, and SAS fields. For this
supplement, SAS expiry must cover the bounded twenty-minute observation window
(or the earlier approval expiry) plus a sixty-second safety margin; optional
`st` must already be active, and `sp` must allow write or create. These are local
structural/time checks, not cryptographic proof that Blob storage will accept
the signature.

`metadata-preflight-result.json` records the approval/source binding, safe shape
metadata, validation stage, and outcome. Failures identify a sanitized stage
(for example `source_blob_binding`, `sas_expiry`, or `sas_write_permission`),
not raw response data. A failed/unknown request or rejected response stops the
whole command, leaves the backend slot unreserved, and never obtains a second
preflight response. Do not patch around successive live failures or run frontend
publication merely to leave a partial release.

On successful validation only, the exact response remains **in process memory**
and is passed directly to the backend build. The command reserves the fixed
`backend-publication-supplemental-attempt.json` before upload and reuses that
response; it never requests another backend upload URL. It revalidates SAS/time
conditions before upload/queue and stops rather than refreshing an expiring SAS.
The ordinary frontend counter remains single-use; frontend runs only after the
backend succeeds and uses its one necessary original metadata acquisition,
validated by the same pure function before upload. Both components retain the
full 900-second remaining-window gates, CPU/timeout limits, immutable attempt and
run-ID receipts, read-only polling, digest checks, and distinct helper/source
provenance.

All publication paths reject existing supplemental/preflight attempt or result
evidence; changing flags, approval IDs, or command paths cannot renew the slots.
A process restart cannot reuse a saved SAS—none is saved—and cannot repeat the
preflight. No accepted `images.json` is written unless both images succeed; no
application deployment or worker execution is implicit.

### Provisioning And Deployment

Verify Entra registrations, consent, secret references, private network access,
retention and the rollback policy before mutation. Read-only checks and baseline
capture precede a reviewed what-if; app deployment does not start the worker:

```sh
python3.13 scripts/release.py identity-check --config "$CFG" --work "$WORK"
python3.13 scripts/release.py capture --config "$CFG" --work "$WORK"
python3.13 scripts/release.py what-if --config "$CFG" --work "$WORK"
```

Only after approval of the exact what-if, seed writes and deployment:

```sh
python3.13 scripts/release.py provision --config "$CFG" --work "$WORK" --approve provision
python3.13 scripts/release.py seed --config "$CFG" --work "$WORK" --fixture "$FIXTURE" --approve seed
python3.13 scripts/release.py deploy --config "$CFG" --work "$WORK" --approve deploy
```

Provisioning creates a manual synthetic-only job, private container, job identity,
container-scoped Blob Data Contributor and registry-scoped AcrPull. One replica,
1 vCPU/2 GiB, 600-second timeout, zero retries; no schedule/event trigger or AI grants.
Seeding refuses nonempty storage. Deployment checks intended healthy image revisions.
Stop on drift or failure; inspect partial state rather than blindly repeating writes.

#### Nonroot Backend Runtime Port

Deployment uses the **already published image**, without rebuilding it or changing
its Dockerfile/user/privileges. A successful Docker startup smoke does not prove
that the restricted hosted runtime permits UID 10001 to bind port 80. The backend
runtime explicitly overrides the image command with:

```text
command: ["/app/.venv/bin/fastapi"]
args: ["run", "backend/main.py", "--port", "8080", "--host", "0.0.0.0"]
```

In one ARM PATCH, the release helper aligns this command, the backend's
`API_PORT`/`NEXT_PUBLIC_API_PORT` self-references, existing port-80 HTTP/TCP probes,
and ingress `targetPort` to **8080**. Probe paths, headers, thresholds and timing
are preserved; unsupported probe ports fail closed. External HTTPS origins,
frontend routing, TLS-only `allowInsecure: false`, traffic, CORS, domains and all
other supported ingress settings remain unchanged. The image's nonroot user
and API processing/upload default-off guards are not bypassed. Worker commands
and settings are not changed.

Fresh `capture` records exact ingress alongside containers. Existing
`baseline.json` files are never rewritten. Before the first runtime correction,
`runtime-baseline.json` is written once with the original baseline snapshot/raw
hash, exact observed prior ingress, and prior intent/hash. An older snapshot
without ingress can be supplemented only while the backend is still on port 80
and matches its baseline or the exact recorded prior release intent. Missing
history or unrelated container/ingress drift stops the operation.

The old `*-intended.json` and `*-patch.json` files remain untouched. New
`*-runtime-intended.json` records are immutable, and new PATCH bodies use
content-addressed `*-patch-<hash>.json` files. Re-running `deploy` in the same work
directory recognizes the exact recorded low-port attempt or converged high-port
state, not arbitrary changes. A ready matching backend is not patched again;
an unhealthy backend still prevents frontend deployment.

Ingress write projection drops only known read-only fields `fqdn` and
`targetPortHttpScheme`, rejects unknown fields, and preserves all other supported
values. Pilot acceptance additionally checks the effective app and healthy
revision commands, internal-port settings, probes, and HTTPS ingress against the
nonroot-compatible runtime; an image-only health match is insufficient.

If the operator cannot reach private Blob storage, deploy the authenticated backend
first, then seed through its existing managed identity instead of granting the
operator data access or opening the storage firewall:

```sh
python3.13 scripts/release.py deploy --config "$CFG" --work "$WORK" --approve deploy
python3.13 scripts/release.py seed --config "$CFG" --work "$WORK" --fixture "$FIXTURE" --seed-via-backend --approve seed
```

This path requires the ready backend to use the published immutable image, hosted
mode and live AI false. It verifies the same three synthetic seed files, refuses a
nonempty container, uploads without overwrite and verifies the stored bytes. No
worker starts. A missing success receipt requires inspection, not a blind retry.

### Hosted Acceptance

Use the synthetic fixture and signed-in browser helpers in
[the acceptance module](frontend/tests/batch-hosted.acceptance.mjs). Submit once in
`evidence_only` mode with live AI false. After explicit execution approval:

```sh
python3.13 scripts/release.py start --config "$CFG" --work "$WORK" --batch-id "$BATCH_ID" --item-limit 1 --approve start
```

Observe completion and queued remainder before the second identical one-item command.
Record both executions and stable first-product results. Never start concurrently or
resubmit a batch to bypass interrupted work. The template omits a batch ID so an
unconfigured start exits before storage access. Cached synthetic evidence may yield
no candidates; test rejection without claiming an unexercised candidate approval.
DI, model calls, customer inputs and SharePoint retries remain out of scope.

### Separately Approved Four-Product Real Pilot

The existing `start` and `seed` actions remain synthetic-only. Real customer work
uses separate `pilot-configure`, `pilot-upload-enable`, `pilot-upload-disable`,
`pilot-enable`, `pilot-start`, and `pilot-disable` actions, each
requiring its own exact `--approve` value. These commands are tooling, not evidence
of an approved or completed execution. Do not run them until clean locked builds,
accepted source images, authentication, access and the actual cost approval exist.

Prepare these **owner-only, untracked private** files in the same approved `$WORK`
directory; keep it for every continuation and rollback:

* Existing `source.json` and `images.json` receipts must bind the exact reviewed
  source and immutable backend/frontend images.
* Actual `clean-checks.json`, `backend-smoke.json`, `frontend-smoke.json`,
  `context.json` and `context-modes.json` are mandatory. The tool verifies the
  published source/lock, clean backend locked-install and frontend npm-ci/build
  results, and each component's passing offline startup smoke against the exact
  context bytes and permissions. A preserved component follows its immutable
  publication chain to that component's original validation receipts. Existing
  interpreter test passes are not a substitute. Both deployed ready revisions
  must also report `Healthy` and contain the exact accepted image digest.
* `pilot-acceptance.json` records `target` (the release tool's `fingerprint` of the
  exact target configuration), `revision` (matching `images.json`), `backend_image`,
  `frontend_image`, `authenticated: true`, and `synthetic_accepted: true`. A verified
  operator records these only after the corresponding hosted acceptance succeeds;
  the tool never manufactures acceptance.
* `$PILOT_APPROVAL` is the exact approval object documented in
  `backend/real_pilot.py`, including verified operator UUID, separate API/worker
  principal UUIDs, exact batch/owner/input binding, actual conservative per-unit
  prices, positive microdollar spend ceiling, and a currently active interval of
  no more than twenty minutes. An unapproved draft, missing price, wrong batch,
  excess limit or expired packet cannot activate the pilot.

The exact `pilot-acceptance.json` schema is the following six fields; extra fields
are rejected. Values below describe the contract, not a completed receipt:

```text
target: SHA256(json.dumps(complete private target config, sort_keys=True).encode())
revision: exact images.json revision
backend_image: exact private config backend_image, including @sha256 digest
frontend_image: exact private config frontend_image, including @sha256 digest
authenticated: true only after verified hosted identity/owner-isolation acceptance
synthetic_accepted: true only after verified bounded hosted synthetic acceptance
```

Reuse the **already accepted, completed two-product synthetic batch** after the
new deployment: read its owner-isolated API records, verify unchanged immutable
machine-result hashes and validate the existing export. Do not start new synthetic
worker executions to refresh this evidence; the prior synthetic execution allowance
is exhausted. Neither pilot activation nor its preflight implicitly starts a
synthetic worker. The local acceptance booleans must reference the operator's real
read-only verification results; they are not proof by themselves.

**Bounded metadata bootstrap and authenticated upload:** the private target
configuration must additionally include `api_principal_id`, the independently
verified backend managed-identity principal UUID, distinct from its API registration
`api_client_id`. It is compared to the actual existing backend identity. Include it
before recording the target-bound acceptance receipt; this does not create or
change an identity or grant.

The target must also contain `pilot_operator_ids`, an independently reviewed list
of one to four canonical operator UUIDs. Every upload/configuration/real-pilot
activation checks `approved_by` against this allowlist before remote work. The
approval packet cannot authorize its own new operator merely by naming one.
This allowlist is part of the complete target fingerprint in acceptance; never
populate it automatically from an unverified candidate approval.

The owner-only upload approval uses the exact `backend/pilot_upload.py` schema:
approval/operator UUIDs, owner `tenant-UUID/object-UUID` whose object UUID is the
approver, active expiry, expected normalized registry SHA256, exact SourceBinding
declarations and document source IDs/filenames/byte counts/hashes. Optional input
hashes bind both workbooks. It contains **metadata only**, never PDF/XLSX bytes,
credentials or unrestricted upload paths.

```sh
python3.13 scripts/release.py pilot-configure --config "$CFG" --work "$WORK" \
  --upload-approval "$UPLOAD_APPROVAL" --approve pilot-configure
python3.13 scripts/release.py pilot-upload-enable --config "$CFG" --work "$WORK" \
  --upload-approval "$UPLOAD_APPROVAL" --approve pilot-upload-enable
```

`pilot-configure` accepts exactly one of `--upload-approval` or `--pilot-approval`.
The only writable keys are `configuration/pilot-source-upload.json` and
`configuration/real-pilot-approval.json`; there is no generic key/path option.
It validates metadata against the corresponding backend schema, requires existing
accepted source/images/runtime/authentication and the bound API identity, and
uses the existing backend managed identity. Metadata is limited to 64 KiB raw JSON,
8 KiB compressed/base64 and a 16 KiB complete console frame. Writes are append-only
conditional creates: existing different metadata is refused; an identical retry
can verify and confirm the same stored object. Private receipts pin the exact
metadata hash. No data document is sent through the console.

`pilot-upload-enable` verifies the actual stored upload approval hash through a
small read-only console check. It changes only `DOCINTEL_PILOT_UPLOAD_ENABLED=true`
and `DOCINTEL_REAL_PILOT_OPERATOR_IDS`; real-pilot processing must remain disabled.
It needs neither a real batch nor a real-pilot approval. Upload the exact approved
documents through the existing authenticated HTTPS batch API, finalize its bounded
registry merge, and perform ordinary authenticated intake. No new permissions,
storage firewall changes, resources, or secrets are involved.

Then restore the upload settings before enabling processing:

```sh
python3.13 scripts/release.py pilot-upload-disable --config "$CFG" --work "$WORK" \
  --approve pilot-upload-disable
python3.13 scripts/release.py pilot-configure --config "$CFG" --work "$WORK" \
  --pilot-approval "$PILOT_APPROVAL" --batch-id "$BATCH_ID" --approve pilot-configure
```

Upload disable restores only the two prior upload/operator entries, requires real
processing still disabled, and refuses drift. Neither upload activation nor
metadata bootstrap starts a worker. The synthetic seed's empty-container and
overwrite protections remain unchanged.

The preflight requires both the actual private approval record **and the persisted
intake batch**, not merely a predicted batch ID. To avoid an activation dependency
cycle, populate approved sources first through the separately approved authenticated
upload path, run ordinary authenticated intake while real-pilot processing is still
disabled, then seed/finalize the exact approval metadata and enable real-pilot
submission. Preseeding approval metadata against a predicted ID does not remove
the requirement to create and verify the identical intake record before activation.

Activation performs a read-only console preflight on the accepted backend: it
checks that the stored approval matches the local object, validates the exact
stored batch binding, and rejects an incompatible/invalidated/exhausted server
budget. Only a bounded confirmation marker returns; customer records and secrets
are not printed. The console validation runs in its own process and does not
persistently enable the API or reserve an execution.

```sh
python3.13 scripts/release.py pilot-enable --config "$CFG" --work "$WORK" \
  --pilot-approval "$PILOT_APPROVAL" --batch-id "$BATCH_ID" --approve pilot-enable

# After the authenticated owner queues that exact batch in real_pilot mode:
python3.13 scripts/release.py pilot-start --config "$CFG" --work "$WORK" \
  --pilot-approval "$PILOT_APPROVAL" --batch-id "$BATCH_ID" \
  --item-limit 2 --approve pilot-start
```

`pilot-enable` requires an idle manual worker and accepted authentication/images.
It adds only `DOCINTEL_REAL_PILOT_ENABLED=true`,
`DOCINTEL_REAL_PILOT_OPERATOR_IDS` and
`DOCINTEL_REAL_PILOT_WORKER_PRINCIPAL_ID` to the API, preserving its distinct
identity, all authentication settings and every unrelated value/secret reference.
Its immutable local baseline records exactly those three prior entries. It does
not copy worker endpoints or managed-identity client settings into the API.

`pilot-start` requires an idle job, zero retries, a 600-second timeout, one
container/replica and the approved backend digest. Its per-execution override uses:

```text
/app/.venv/bin/python -m backend.batch_worker --real-pilot --batch-id <exact-id>
  --concurrency 1 --max-batches 1 --item-limit <1..4, within approval>
```

The job's persistent template stays unchanged. The override sets real-pilot
enablement, trusted operator/worker IDs and the exact approved nonsecret worker
environment, including `AOAI_API_VERSION`; inherited secret references remain
references. `AZURE_CLIENT_ID: ""` selects a matching system-assigned worker
principal; a nonempty client UUID must match an attached user-assigned identity.
No assumption is made that the API and worker have the same principal.

Local `pilot-binding.json` pins the entire approval/target, and exclusive
`pilot-execution-attempt-N.json` receipts consume each attempted start before its
Azure request. At most `limits.executions` (hard ceiling two) are allowed. A lost
response leaves an unknown attempt and blocks automatic continuation. Inspect it;
do not delete receipts, change approval IDs or use a fresh work directory as a
retry mechanism. These local receipts supplement—not replace—the authoritative
immutable server ledger at `budgets/real-pilot.json`. The worker independently
reserves its execution and every service attempt. Analysis remains two documents,
ten total reserved pages; inference sixteen, searches eight, original-page
retrievals twelve and internal retrievals four, within approved token/spend bounds.
Actual usage is separate from conservative reservations and is never billing data.

#### Explicit Internal-Only Approval Scope

An independently approved internal-only fallback uses the same `real_pilot` batch
mode and CLI, with **`execution_scope: "internal_only"` in the immutable approval**.
Omitting this optional field retains the existing `"full"` behavior; it never
selects fallback automatically based on a failed web request or missing credential.
Any focused WebIQ entitlement check is a separate operator decision, not something
the worker runs to choose its scope.

For internal-only approval:

* Set `limits.search`, `limits.web_retrieval` and `limits.retrieval` to **zero**.
  The last field governs remote Graph/SharePoint retrieval; this fallback uses
  only approved, hash-checked Blob PDF/XLSX copies.
* `unit_prices_usd` contains exactly `analysis_page`, `input_token` and
  `output_token`, with the same required positive conservative decimal-string
  rates. No web/search price or WebIQ entitlement is required.
* Omit `WEBSEARCH_PROVIDER`, `WEBIQ_ENDPOINT`, `AI_FOUNDRY_PROJECT_ENDPOINT`,
  `BING_CONNECTION_ID`, `AZURE_SEARCH_ENDPOINT` and `AZURE_SEARCH_INDEX_NAME`
  from the approved environment. Do not include credentials.
  `customer_processing_approved` **must remain `true` in every scope**, including
  internal-only: approved customer-data handling and DI/model prerequisites are
  never bypassed. This common consent flag does **not** assert WebIQ entitlement
  or authorize external calls when the scope/budgets prohibit them.
* Retain the same DI/LLM endpoints/deployment/API-version, operator/owner/source
  hashes, identity, expiry, page/token/spend and execution-budget guards.

`pilot-start` sets `DOCINTEL_REAL_PILOT_EXECUTION_SCOPE` in the execution override
and removes inherited web-service settings and the WebIQ key/reference from that
override only. It does not modify the persistent job template or API settings.
The server approval remains authoritative; a supplied runtime scope that disagrees
with it is rejected.

The worker skips all web and remote SharePoint bindings without constructing WebIQ,
searching, fetching original pages or reserving prohibited calls. Website references
remain bound input metadata, never evidence. PDF/vendor-row citations and unresolved
attributes are retained truthfully; skipped-source provenance and consumption
metadata include the scope and flow into the existing export's Provenance sheet.
No frontend/API mode widening or new synthetic execution is involved.

The entire scope-bearing approval remains hash-bound to the existing singleton
ledger and local release binding. Changing scope, ID or batch after binding cannot
reset allowance or automatically convert an already-used full approval. An existing
incompatible approval/configuration is a stop condition, not permission to overwrite
it or create a new work directory.

For the real-pilot deployment named exactly `gpt-5` (whose original
`2025-08-07` snapshot the operator must verify), the worker explicitly requests
`reasoning_effort="minimal"` without increasing `max_completion_tokens=2048`.
[Microsoft's reasoning-model documentation](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning)
and the [original GPT-5 model reference](https://developers.openai.com/api/docs/models/gpt-5)
confirm this support. The explicit request parameters are recorded in inference
provenance/cache records and affect that deployment's cache identity. Generic
clients, synthetic processing and other deployment defaults remain unchanged.
The cap still includes reasoning and visible output, so minimal effort is not a
guarantee of a usable response; an empty/invalid response remains a counted,
non-retried failed attempt.

Real-pilot model requests use a lossless, versioned compact evidence projection.
Shared source metadata, applicability and qualifications are grouped only when
identical; repeated verbatim text is stored once. Every original excerpt retains
its own selectable row and citation reference, even when its text is duplicated.
Original evidence IDs and locations remain encoded by exact prefixes/suffixes;
request-local citation references are expanded back to the selected original IDs
before quote/applicability validation, caching or export.
Stored evidence and approved inputs are not rewritten or truncated. The effective
compact system/user/schema bytes plus the existing safety allowance still determine
the conservative reservation; measured token usage never refunds that reservation.
Verify the sum for every possible source-stage call across the actual selected
products, not merely each individual prompt or synthetic fixtures.

A capacity denial before reservation is reported as `RealPilotBudgetExceeded`,
with the requested/remaining units and `new_model_call=false` provenance.
It does not consume an inference attempt, masquerade as a model response, or abort
independent eligible sources/items. Authorization, approval drift, expiry and
duplicate-attempt failures remain fatal. Guard-stopped items retain a safe failure
status when the storage lease permits writing it; no later real-pilot item starts
after a fatal guard error. Interrupted items are never automatically
resubmitted. Correcting a prompt does not reopen a closed activation, replenish
executions or authorize another build.

The existing PDF request still selects `pages="1-5"` with a 120-second polling
bound. Offline SDK tests verify those arguments, not server handling when a PDF
has fewer pages. No undocumented out-of-range acceptance is assumed and no
automatic alternate-page retry is authorized.

After explicit completion/stop, restore only the three API settings, even if the
approval has expired:

```sh
python3.13 scripts/release.py pilot-disable --config "$CFG" --work "$WORK" \
  --approve pilot-disable
```

Disable refuses an active worker or changed pilot-managed settings. It preserves
unrelated intervening configuration changes and does not delete results, budgets,
identities or resources. Restoring the default-off API does not erase consumed
allowances or permit automatic reactivation. A partial mutation or unexpected
revision/configuration requires inspection rather than blind retries.

## Local Batch Development

Use existing installed dependencies while the lock gate remains open. Choose unused
loopback ports and a private directory outside Git. The following backend is the
batch-only development entry point, not the hosted main app:

```sh
export DOCINTEL_BATCH_MODE=development
export DOCINTEL_BATCH_HOME='<private-directory-outside-repository>'
export DOCINTEL_BATCH_LIVE_ENABLED=false
.venv/bin/python -m uvicorn backend.batch_api:development_app --host 127.0.0.1 --port 8012
```

In another terminal, from the repository root:

```sh
cd frontend
DOCINTEL_BATCH_DEV=true DOCINTEL_BATCH_API_URL=http://127.0.0.1:8012/api/v1/batches \
  ./node_modules/.bin/next dev --hostname 127.0.0.1 --port 3100
```

Open <http://127.0.0.1:3100/batches>. Configure synthetic associations/private data
as an operator before intake. In a third terminal with the **same private home**,
development mode and live-disabled settings, an authorized synthetic queue can be
processed by one finite invocation:

```sh
.venv/bin/python -m backend.batch_worker --concurrency 2 --max-batches 1 --item-limit 100
```

No automatic local worker is started by the API. `DOCINTEL_BATCH_DEV=true` permits
only the frontend development proxy; never configure it for hosted use.
`DOCINTEL_BATCH_MODE=development` selects private SQLite and unverified local
identity and is rejected when Azure runtime markers are present. Do not publicly
bind either server. Use [PILOT.md](PILOT.md#start-and-verify) instead for the distinct
single-product replay workflow; avoid launching both frontends on the same port.

## Hosted Acceptance

These are required future checks, not completed work:

1. Confirm deployed revisions/digests and all required settings, then test Entra
   sign-in/consent, expiry, negative signatures/audience/tenant/client/scope, and
   owner isolation. Local headers must not authorize hosted access.
2. Verify private Blob network access, no public access, conditional writes, leases,
   source hashes, cache integrity and durability across replica replacement.
3. Use a synthetic no-AI batch to exercise validation, idempotent submission,
  manual execution, slice continuation, concurrent fencing and interruption
   recovery. Verify no duplicated calls or writes before testing any live scope.
4. Check evidence, qualifications, approval/correction/rejection and qualified
   export against immutable results. Complete desktop/mobile, cold/warm and failure
   tests, recording **all attempts**, not only eventual success.
5. Measure catalog-scale throughput, scan/storage costs and quality with an approved
   evaluation set before claiming thousands-of-products performance or accuracy.

The retained browser sequence had three automation failures then three consecutive
warm read-only passes after repair. Two earlier manual Retry clicks remain
uncorrelated; cold start and real devices were not accepted. See the complete
[repeatability report](BATCH.md#browser-repeatability). Do not broaden permissions,
repeat paid calls, or change customer decisions to satisfy an acceptance check.

## Rollback And Maintenance

After separate authorization, use the captured baseline and guarded rollback:

```sh
python3.13 scripts/release.py stop --config "$CFG" --work "$WORK" --approve stop
python3.13 scripts/release.py rollback --config "$CFG" --work "$WORK" --approve rollback
```

Rollback stops active executions and restores pinned images and exact environment/
secret-reference settings, command, probes and captured ingress atomically per
app. A low-port baseline restores **port 80 together with its baseline image**,
never leaving ingress at 8080. The helper verifies the restored runtime/ingress
configuration before reporting convergence. It retains the job, identity, roles, container, inputs,
results, reviews and budgets. It refuses unrelated configuration drift.
An unauthenticated legacy baseline cannot be restored publicly. Either establish a
verified secure baseline or explicitly approve `--isolate-legacy-baseline`, which
disables ingress before restoring old images and causes an outage. Never silently
re-enable ingress while restoring that unsafe baseline. Never silently
authorize isolation, delete nonempty storage, or reset evidence to force a retry.
Sanitize logs before sharing; do not export signed URLs, bearer tokens, account
labels, document content, or secrets. No deployment, provisioning, rollback, grants,
AI calls, customer processing, package installation, or push was performed to
produce this guide.