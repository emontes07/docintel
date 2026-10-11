# Amortized quality batches and sharding

The quality worker's default remains the per-product pipeline. Enable the
internal family/vendor path with `QUALITY_AMORTIZED_PIPELINE_ENABLED=true`.
For the internal approved Angle Valves slice only, a one-shot worker invocation
with `QUALITY_PREPARE_APPROVED_SLICE=true` derives product rows from the
registered Mueller/Ford XLSX blobs and existing Phase 3 definitions, persists a
new private batch record, and exits before any model call. It is not a general
customer intake path.
Batch API submission and model tiering are separate optional features and remain
off unless explicitly enabled by their caller. `QUALITY_MODEL_TIERING_ENABLED`
routes the file-profile and unique-phrase extraction stages to
`QUALITY_VENDOR_MODEL_DEPLOYMENT`; enabling it requires explicit prices for
all four vendor-model token classes. PDF-family extraction, residual calls and
judging remain on the standard deployment. `QUALITY_AZURE_BATCH_ENABLED` is
reserved and defaults off; setting it true currently fails explicitly because
this worker does not implement Azure OpenAI Batch submission.

## Per-execution environment

`./run.sh start RUN_ID` reads the job's immutable image and base environment,
merges run-specific overrides, and passes them to
`az containerapp job start --image ... --command /app/.venv/bin/python
--args=-mbackend.quality_worker --env-vars ...`; it does not update the shared
job definition. Supplying the image and worker command/args is required: the
Azure CLI otherwise omits execution environment overrides or starts the image's
default API entrypoint. Use `./run.sh start-shards RUN_ID` with
`QUALITY_SHARD_COUNT=N` (1–32) to start one execution per shard. Each receives
its own `QUALITY_RUN_ID`,
`QUALITY_SHARD_INDEX`, `QUALITY_SHARD_COUNT`, and therefore its own cost meter
under `quality-runs/<batch>/<run-id>/cost.json`. Family groups are assigned
deterministically and balanced by item count. Start the shards concurrently in
the orchestration client when the deployment TPM budget permits.

Shard identity, run ID, and the persisted per-item completion checkpoint make a
restart idempotent. On restart with the same shard run ID, valid completed result
records are skipped; incomplete or missing result objects are retried. A shard
must not be restarted with a different index/count under the same run ID.

The job template's deployed `replicaTimeout` is **7,200 seconds**. The
`infra/batch.bicep` default is aligned to 7,200 seconds, and `parallelism` is a
bounded parameter (1–32). Keep ACA `parallelism=1` when using
`start-shards`, which creates independently addressed job executions with
per-execution environment. ACA internal replicas do not receive distinct shard
environment by themselves and must not all process the same shard.

## Amortized stages and caches

- The family stage is keyed by PDF source identity/version, the complete
  definition set, judge policy, deployment and amortization policy version.
  Only text-only, non-table, non-variant-specific candidates are family-wide.
  Candidate transfer is grounded again against each target product's evidence
  and applicability map. The cached value is an extraction result, not product
  approval.
- Variant dimensions and connections remain per-product residual fields when a
  family candidate indicates an ambiguous mapping. The Ford AV11 333W/444W size
  distinction remains a regression test. The Mueller H14250 versus H14255N
  applicability question remains Low-confidence/unresolved unless the same
  drawing/family identity is proved by evidence.
- A versioned, gzip-compressed vendor workbook index is persisted by approved
  source hash. One profile is stored per workbook and definitions policy.
  Candidate phrase maps are stored per `(source hash, definitions hash, policy
  version)`. Exact row/cell evidence is still loaded and cited separately for
  each item. Workbook parsing/index metrics are emitted in the run summary.
- Per-item results and checkpoints are written as soon as each item completes;
  the result stream and web-yield accumulator are bounded-memory. Cost state
  stays shard-local; there is no cross-shard read/modify/write cost object.

## Cost safety

`QUALITY_RUN_CAP_USD` and `QUALITY_SESSION_CAP_USD` apply independently to each
shard meter. Since the caps are per-shard, the orchestration client must allocate
the session budget across the shard count; do not set the full session cap on
every concurrent shard. `QUALITY_OVERNIGHT_PRIOR_COST_USD` is the already-spent
session amount used by each meter. Web remains off by default for these runs;
DI remains cached-only and does not analyze new pages.
