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
Each ACR build has a 900-second timeout. Absolute Dockerfile paths point into the
verified context; only successful builds produce digest/run-ID receipts. A failure
stops publication without automatically repeating either build.
Record those digests in the private target configuration before baseline capture.

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
secret-reference settings. It retains the job, identity, roles, container, inputs,
results, reviews and budgets. It refuses unrelated configuration drift.
An unauthenticated legacy baseline cannot be restored publicly. Either establish a
verified secure baseline or explicitly approve `--isolate-legacy-baseline`, which
disables ingress before restoring old images and causes an outage. Never silently
authorize isolation, delete nonempty storage, or reset evidence to force a retry.
Sanitize logs before sharing; do not export signed URLs, bearer tokens, account
labels, document content, or secrets. No deployment, provisioning, rollback, grants,
AI calls, customer processing, package installation, or push was performed to
produce this guide.