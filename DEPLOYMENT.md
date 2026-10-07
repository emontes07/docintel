# Operations runbook

Operate from this checkout with `./run.sh`; do not use the archived operators or
`azd`. Existing resources only: subscription `41ff069d-9e57-40a1-971f-710630f3d6bc`,
group `rg-docintel-dev-erik3`, ACR `crc4ryis6hullf4`,
API `ca-backend-docintel-dev-erik3`, manual job `caj-docintel-batch-dev-erik3`.
No provisioning, role changes or frontend deployment is part of this path.

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
`deploy IMAGE` runs `az containerapp update --image` and
`az containerapp job update --image` with that same immutable digest, overriding
the job entrypoint to `/app/.venv/bin/python -m backend.quality_worker`.
It retains existing identity, secrets, networking and API environment; the job
uses one replica, one completion and zero automatic retries. `start RUN_ID`
starts exactly one manual execution with `QUALITY_RUN_ID`; it does not
build, deploy, reset state or automatically retry a failed/uncertain start.
It prints the execution name for the cost/log command.

## Runtime and authenticated export

Keep existing Blob, managed-identity and Entra settings on both resources.
Before `start`, set `QUALITY_BATCH_ID` and `QUALITY_OWNER` (`tenant/object-id`)
to the existing owner-scoped batch. The wrapper supplies these and the run ID as
execution environment. Optional `QUALITY_WEB_ENABLED`, `QUALITY_SMOKE_ONLY` and
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
that batch/owner, decodes the workbook with the local `.venv` Python, and removes
the transient console transcript. It adds no grant or endpoint and is not a
claim of end-user API authentication; perform that smoke with the genuine
browser session. The default remains HTTP cookie/header export.
`export` first performs an authenticated owner-scoped batch read, then downloads the
Excel export to the relative directory you supplied. HTTP/auth failures stop it;
redirects are not followed. `cost EXECUTION` is plain `az containerapp job logs
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
