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
commands, docs, logs, screenshots, or Git. Existing tracked environment-file history
needs a separate security review even though image contexts exclude env files.

SharePoint remains metadata 200 -> content 302 -> download 401. Do not add cookie,
auth-forwarding, or local-file workarounds. A separately approved access correction
and bounded retrieval acceptance must precede a claim of working SharePoint input.
Approved workbook type/unit mappings and full source associations are also gates.

## What Each Operation Means

| Operation | Effect and current limit |
| --- | --- |
| Bicep compilation | Validates template syntax/types locally; does not verify Azure permission, policy, quota, network, runtime or application behavior. |
| Infrastructure provisioning | Creates/changes resources and grants. The separate batch template must be reviewed and authorized; it is not wired into azd provisioning. |
| Backend deployment | Builds/deploys the API image; does not deploy or update the worker. |
| Frontend deployment | Builds/deploys the portal image; does not prove Entra sign-in or API permission. |
| Worker deployment/update | Explicit separate job operation with the same tested immutable backend image. Scheduling can start processing queued work. |
| Local development | Loopback, explicit development identity, private SQLite; not hosted identity/persistence acceptance. |
| Hosted acceptance | Authorized synthetic tests on the deployed revisions, including identity isolation, Blob conditional writes, scheduler and interruption recovery. |

## Image And Entry-Point Mapping

Verified against [azure.yaml](azure.yaml):

| Service | Actual build path | Runtime |
| --- | --- | --- |
| `backend` | Project/context repository root; [backend/Dockerfile](backend/Dockerfile); ACR remote build; Python 3.13, uv 0.11.31, `uv sync --locked`. | `fastapi run backend/main.py --port 80 --host 0.0.0.0`; batch router `/api/v1/batches`. |
| `frontend` | Project/context `frontend/`; [frontend/Dockerfile](frontend/Dockerfile); Node 22, `npm ci`, Next standalone build. | `node server.js`, port 3000. |
| Worker | Same tested backend image; [job module](infra/modules/containerAppJob.bicep) overrides the HTTP command. | `/app/.venv/bin/python -m backend.batch_worker --concurrency 2 --max-batches 1 --item-limit 100`; exits after a finite slice. |

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
is not enabled: exact prior pilot identity/attributes/hash remain enforced in code.

## Compile And Activate Only After Approval

All shell examples run from the repository root. Set the named shell variables to
approved environment values; no personal resource names or default job name should
be assumed. Compiler output is discarded here; no resources are changed:

```sh
az bicep build --file infra/batch.bicep --stdout > /dev/null
```

After the clean-image, identity, network, cost and change approvals are complete,
review a what-if against the intended subscription/group. `DOCINTEL_BACKEND_IMAGE`
must identify the reviewed immutable image, not a mutable convenience tag.

```sh
az deployment group what-if --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" --template-file infra/batch.bicep \
  --parameters storageAccountName="$DOCINTEL_STORAGE_ACCOUNT" \
  registryName="$DOCINTEL_REGISTRY" environmentName="$DOCINTEL_ENVIRONMENT" \
  jobName="$DOCINTEL_JOB_NAME" containerName="$DOCINTEL_BATCH_CONTAINER" \
  backendImage="$DOCINTEL_BACKEND_IMAGE"
```

The template's compiled default job name is environment-specific; always override
it as above. Approval covers a private container, job identity, two scoped grants,
and a job scheduled every five minutes: 1 vCPU, 2 GiB, 600-second timeout, zero
replica retries. At most 288 scheduled executions/day and possible overlap require
cost and lease testing; see [cost qualifications](BATCH.md#approval-request).

Capture previous image digests/revisions and settings before deployment. Confirm
the selected azd environment matches the authorized subscription/group. These two
commands deploy only their respective app; they do not activate the worker:

```sh
azd deploy backend
azd deploy frontend
```

Apply reviewed hosted settings/secret references through the approved process.
Only with a reviewed immutable backend image available and no unauthorized queued
work may the following provisioning step activate the scheduled worker:

```sh
az deployment group create --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" --template-file infra/batch.bicep \
  --parameters storageAccountName="$DOCINTEL_STORAGE_ACCOUNT" \
  registryName="$DOCINTEL_REGISTRY" environmentName="$DOCINTEL_ENVIRONMENT" \
  jobName="$DOCINTEL_JOB_NAME" containerName="$DOCINTEL_BATCH_CONTAINER" \
  backendImage="$DOCINTEL_BACKEND_IMAGE"
```

For a later approved worker-image update, update it explicitly, never assuming an
API deployment changed the job:

```sh
az containerapp job update --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" --name "$DOCINTEL_JOB_NAME" \
  --image "$DOCINTEL_BACKEND_IMAGE"
```

Dedicated DI account creation/role grants are **not** part of this activation.
Reuse only an approved dedicated account; a new one needs separate geography/SKU,
cost, identity, and resource-scoped permission review. Keep local/key auth disabled.
Cognitive Services User is broader than analysis-only access; Data Reader alone
does not grant analysis submission. Do not modify Foundry or broaden grants to
work around denial. Local byte-input pilot constraints remain in [PILOT.md](PILOT.md).

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
   scheduled execution, slice continuation, concurrent fencing and interruption
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

Only after separate authorization: inspect active executions, stop each by name,
and disable/remove the new schedule before rolling back app images. Retain private
inputs/results/reviews/budgets; do not reset usage or delete data to force a retry.
No blanket cleanup is appropriate. The commands below remove only the specifically
approved new job, not the store or pre-existing resources:

```sh
az containerapp job execution list --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" --name "$DOCINTEL_JOB_NAME"
az containerapp job stop --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" --name "$DOCINTEL_JOB_NAME" \
  --job-execution-name "$DOCINTEL_EXECUTION_NAME"
az containerapp job delete --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" --name "$DOCINTEL_JOB_NAME" --yes
az containerapp update --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" --name "$DOCINTEL_BACKEND_APP" \
  --image "$DOCINTEL_PREVIOUS_BACKEND_IMAGE"
az containerapp update --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" --name "$DOCINTEL_FRONTEND_APP" \
  --image "$DOCINTEL_PREVIOUS_FRONTEND_IMAGE"
```

Review removal of new identity/grants separately; preserve pre-existing roles.
Sanitize logs before sharing; do not export signed URLs, bearer tokens, account
labels, document content, or secrets. No deployment, provisioning, rollback, grants,
AI calls, customer processing, package installation, or push was performed to
produce this guide.