# Operations runbook

Operate from this checkout with `./run.sh`; do not use the archived operators or
`azd`. Existing resources only: subscription `41ff069d-9e57-40a1-971f-710630f3d6bc`,
group `rg-docintel-dev-erik3`, ACR `crc4ryis6hullf4`,
API `ca-backend-docintel-dev-erik3`, manual job `caj-docintel-batch-dev-erik3`.
No provisioning, role changes or frontend deployment is part of this path.

## Shared Front Door maintenance

Front Door consolidation is a separate, explicitly approved infrastructure
operation, not part of `./run.sh` or the normal application release path.
`infra/main.bicep` accepts `sharedFrontDoorProfileId`: when nonempty, its Front
Door module targets that profile's subscription/resource group and creates only
the environment's endpoint, origin group, Blob Private Link origin and route.
The profile itself is referenced as existing, not updated. Empty preserves the
legacy dedicated Premium profile and child names.

For DocIntel, retain these values in the deployment environment/parameter source:

| Parameter | Value |
| --- | --- |
| `sharedFrontDoorProfileId` / `SHARED_FRONT_DOOR_PROFILE_ID` | `/subscriptions/41ff069d-9e57-40a1-971f-710630f3d6bc/resourceGroups/rg-sora-video-dev-erik2/providers/Microsoft.Cdn/profiles/afd-sora-video-dev-erik2` |
| `frontDoorEndpointName` / `AZURE_FRONT_DOOR_ENDPOINT_NAME` | `docintel-dev-erik3-ep` |
| `frontDoorOriginGroupName` / `AZURE_FRONT_DOOR_ORIGIN_GROUP_NAME` | `docintel-storage-origin-group` |
| `frontDoorOriginName` / `AZURE_FRONT_DOOR_ORIGIN_NAME` | `docintel-storage-blob-origin` |

Always use **Incremental** deployment mode. Complete mode could remove unrelated
resources in the shared resource group. Keep shared child names unique across
environments and coordinate profile ownership with Sora; do not rename, modify
or remove Sora's existing endpoint, origin group, origin or route. Standard
cannot replace this Premium profile's Private Link origins.

Build both templates with `az bicep build`. The checked-in
`infra/shared-frontDoor.docintel.bicepparam` records the isolated DocIntel child
resources, without secrets. Preview it with:

```bash
az deployment group what-if \
  --subscription 41ff069d-9e57-40a1-971f-710630f3d6bc \
  --resource-group rg-sora-video-dev-erik2 \
  --mode Incremental \
  --parameters infra/shared-frontDoor.docintel.bicepparam
```

Apply only after reviewing the what-if. Do not apply the broad main template to
accomplish this isolated maintenance operation. Validate the provisioning safety
checks with `node --test tests/frontdoor-infra.test.mjs`.

The module emits a distinct request message for its shared Private Link origin.
The provisioning hooks approve only a single pending connection on the emitted
storage resource ID whose request message matches exactly. Missing configuration,
ambiguous matches and approval/read failures stop the hook. An isolated module
deployment must similarly identify and approve only its newly requested link.

Before any rollout, save both profiles' configuration and each DocIntel app's
`CDN_BLOB_URL`, image, ready revision, revision mode and traffic under ignored
`ops-backup/`. Keep backups out of Git and image build contexts. Verify a known
SAS-authorized Blob GET returns 200 through both the new DocIntel endpoint and
the unchanged Sora endpoint before changing either DocIntel app. Change only
`CDN_BLOB_URL`, preserve images and all other configuration, and verify ready
revisions, core workflows and gallery rendering.

Batch upload, extraction/enrichment and Excel exports use authenticated backend
and private Blob paths, not `CDN_BLOB_URL`. The legacy gallery image URL service
does use it. Private source PDFs are read through `BlobStore.read_bytes` by
`batch_worker.py`; neither that path nor `quality_pdf.py` uses `CDN_BLOB_URL`.
The checked-in batch UI has no PDF preview/download route; its download is the
authenticated Excel export. The legacy `/gallery/asset/...` stream also reads
directly through the storage service rather than the CDN setting. Source
dependency analysis is not proof of deployed UI acceptance.

If live UI validation is unavailable or the gallery is not deployed, an explicitly
approved alternative is an existing-blob GET and Range GET through the new
hostname. Require 200 and 206 respectively, and compare the returned range bytes
to the full GET. Use a read-only HTTPS user-delegation SAS valid for at most 15
minutes. Never print or persist it. When the local principal lacks Blob data
permission, native `az containerapp exec` can run the test in memory using the
backend's existing managed identity; do not grant roles or open storage networking.
For long commands, use a short `sh` console startup and send the command over
stdin. Native console startup-command length can otherwise produce a 404
handshake failure.

Rollback before deletion: restore each app's saved `CDN_BLOB_URL` with
`az containerapp update --set-env-vars`, preserving its image and other settings,
and verify the resulting revision is ready. Restore saved traffic/revision
configuration if traffic was explicitly changed. In Single revision mode, the
environment-variable restore creates a new revision; do not assume an inactive
old revision is automatically routable. Keep the original DocIntel profile and
its Private Link connection available throughout validation. Deleting that
profile requires **separate explicit approval**; recreating it afterward may
produce a different hostname and is not an instant rollback.

## Commands

```bash
IMAGE=$(./run.sh build quality-001)
./run.sh deploy "$IMAGE"
EXECUTION=$(./run.sh start "$RUN_ID")
./run.sh export "$QUALITY_BATCH_ID" ".cache/exports/$RUN_ID"
./run.sh cost "$EXECUTION"
```

`build TAG` runs plain `az acr build -f backend/Dockerfile` with repository-root
context, then prints the ACR digest; build logs go to stderr. Use a unique tag.
Root ignore patterns omit directory trailing slashes so both Docker and the native
Azure CLI packer exclude local environments, private outputs and credentials.
`deploy IMAGE` runs `az containerapp update --image` and
`az containerapp job update --image` with that same immutable digest, overriding
the job entrypoint to `/app/.venv/bin/python -m backend.quality_worker`.
It retains existing identity, secrets, networking and API environment; the job
uses one replica, one completion and zero automatic retries. `start RUN_ID`
persists the run environment with native `job update --set-env-vars`, then
starts exactly one manual execution; it does not
build, deploy, reset state or automatically retry a failed/uncertain start.
It prints the execution name for the cost/log command.
Execution-only `job start --env-vars` overrides were not applied by the deployed
CLI/API combination, even with a container selection; the resource update avoids
that unsupported path. Log retrieval explicitly selects the existing container
`caj-docintel-batch-dev-erik3`.

## Runtime and authenticated export

Keep existing Blob, managed-identity and Entra settings on both resources.
Before `start`, set `QUALITY_BATCH_ID` and `QUALITY_OWNER` (`tenant/object-id`)
to the existing owner-scoped batch. The wrapper supplies these and the run ID as
job environment. Optional `QUALITY_WEB_ENABLED`, `QUALITY_SMOKE_ONLY` and
`QUALITY_FORD_IMAGE_BLOB` are forwarded when set; other resource settings remain
unchanged. Use this environment contract, not `RealPilotGuard`, approval JSON or continuation files.
Live source/model configuration belongs in existing environment/secret references,
never Git. The backend image uses locked Python dependencies, `poppler-utils` for cached-PDF
page rendering (no new DI request), and a non-root user;
the frontend is unchanged.

Preferred existing-session authentication: set `API_BASE_URL` to the frontend's
HTTPS **origin** and `AUTH_COOKIE_FILE` to a private Netscape cookie jar saved from
the genuine owner browser session (for example under ignored `output/private/`).
Curl sends that jar to `/api/batches/...`; there is no token extraction or new
consent. Alternatively use backend `DOCINTEL_API_URL` and an already valid
`DOCINTEL_ACCESS_TOKEN` with delegated `Batch.Access`, never an ARM token. Keep
cookies/tokens out of Git, image contexts, logs and shell tracing.
When API credentials are unavailable, `EXPORT_MODE=console` with `QUALITY_OWNER`
uses existing Azure administrative app-exec permission, native `az containerapp
exec`, and system `script` for its terminal. It calls `BatchService.export` for
that batch/owner, decodes the workbook with the local `.venv` Python, and preserves
a uniquely named console diagnostic log with mode `0600`, including failures.
The command uses double-quoted Python code; the native console treats surrounding
single quotes literally.
These logs are not authority receipts; list deletion candidates and ask before
removing any. It adds no grant or endpoint and is not a
claim of end-user API authentication; perform that smoke with the genuine
browser session. The default remains HTTP cookie/header export.
`export` first performs an authenticated owner-scoped batch read, then downloads the
Excel export to the relative directory you supplied. HTTP/auth failures stop it;
redirects are not followed, and failed partial downloads are retained rather than
deleted. `cost EXECUTION` is plain `az containerapp job logs
show --execution ...`, displaying the worker's structured meter summary. If
execution logs are unavailable, `cost --file PATH` displays an already downloaded
`quality-runs/BATCH_ID/RUN_ID/cost.json` unchanged. Neither mode computes a second
estimate or creates reservations, receipts or an API endpoint.

## Limits, failures and CI

The current limits **replace** all previous allowances, ledgers, receipts,
deadlines and acceptance checks: **$10/run, $40/overnight, at most five runs,
three executions/run and four image builds**. The parent/operator and quality
worker enforce these limits using one meter; these shell commands add no second
budget system. A failed build or execution still counts; inspect uncertain
outcomes before retrying. Do not equate estimates with final Azure billing.
For rollback, deploy a previously known backend digest with `./run.sh deploy`;
deployment never starts work.

CI is offline unit/integration tests, owner-isolation/export assertions, a backend
container build and non-root startup. It has no Azure login, deployment, custom
write-schema, what-if, native preflight or receipt acceptance stage. Actual
provider tests are excluded. Historical operators, workflows, schemas and runbooks
are preserved under `tools/legacy`; `scripts/*.py` compatibility shims load them
only for old offline tests and reject operational CLI commands. The live shell
path and startup check import none of that code. No Azure operation or container
build is required to test the shell locally: `bash -n run.sh scripts/operations.sh`
and `.venv/bin/python -m pytest tests/test_plain_release.py -q`.
