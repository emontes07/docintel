# DocIntel

Turn product workbooks and explicitly associated technical documents into
traceable attribute proposals, human decisions, and a qualified Excel export.
DocIntel addresses the manual work of finding missing product attributes while
keeping source evidence, conflicting values, and uncertainty visible.

> **Operations:** use the ten-line [`run.sh`](run.sh) and the one-page
> [runbook](DEPLOYMENT.md): build, deploy, start, export and cost. Deployment uses
> the existing API and manual job, sharing one immutable backend image; the
> frontend is unchanged. Historical approval files, receipts, deadlines and
> acceptance gates are superseded and archived under `tools/legacy`.

## The Batch Workflow

1. Upload a **Product manifest** and **Attribute definitions** workbook. Validate
   exact product/source associations, hierarchy, types, and units before submission.
2. Submit one batch. The portal provides queue status, progress, exceptions,
   filters, and paging. A finite worker processes bounded slices durably.
3. Open individual products to inspect proposals, source excerpts, conflicts, and
   qualifications. Record an approval, correction, or rejection separately from
   the original machine result.
4. **Export Excel** with inputs, proposals, evidence, provenance, exceptions, and
   review decisions. Cited/schema-valid output is neither automatically correct
   nor approved master data. There are no automatic master-data writes.

The product screen is a **drill-down for evidence and review**, not a requirement
to submit thousands of products individually. Large catalogs are the intended
workload, not a measured performance claim: intake is bounded to 10,000 product
rows, the UI pages 50 items, and the default worker handles 100 items with two
threads per slice. History/filter scans are not an indexed enterprise queue.

## Where It Runs

The intended experience is an Azure-hosted Next.js portal and FastAPI backend,
Entra sign-in, private Blob batch state, and a manually started Container Apps Job.
Document Intelligence parsing and Azure OpenAI structured extraction are distinct
service calls, not a Foundry agent. Live work remains disabled by default and
limited to the configured quality run, not arbitrary catalog products.

Local development is an explicit loopback-only mode with unverified identity and
private SQLite state. The separate local pilot can replay prior results. Its
local authentication controls do **not** secure hosted use.

| Capability | Recorded evidence / current scope |
| --- | --- |
| Intake, queue, finite worker, review, Excel export | Implemented; locally exercised with temporary synthetic data. |
| Validation, leases, replay safety | Offline tests; no duplicate batch/review writes in the focused regression. |
| Desktop/mobile browser workflow | Initial attempts 1-3 failed on an automation sandbox defect; corrected attempts 4-6 passed consecutively on warm servers. Two earlier user Retry incidents remain uncorrelated. No cold-start or real-device acceptance claim. |
| Azure frontend/backend | Accepted synthetic foundation: frontend source `984598a`, backend/worker source `4370b15`. Exact digests and receipts remain private. These are not deployment claims for later changes. |
| Hosted identity, Blob leases, manual worker | Two finite synthetic slices, unchanged prior results, owner isolation, attributable rejection, and user-confirmed native Excel download passed. |
| AI and document sources | Multi-source intake, attribute-level fallback, and bounded real-pilot controls extend the existing pipeline. Real-source access, WebIQ customer-processing entitlement, and human product review remain separate gates. |
| Clean installation and release | Foundation lock/schema/permissions/Blob-read defects were repaired. Each new source revision still needs locked CI and non-root image startup verification before publication. |

## Guides

| Reader | Start here |
| --- | --- |
| Business operator | [User guide](docs/USER_GUIDE.md): workbooks, validation, execution choices, exceptions, review, export. |
| Developer or architect | [Architecture](docs/ARCHITECTURE.md): original Mermaid diagrams, implemented wiring, blocked connections, deployed-state distinction. |
| Azure maintainer | [Operations runbook](DEPLOYMENT.md): five commands, environment, authenticated export and cost. |
| Release reviewer | [Historical findings](tools/legacy/BATCH.md): superseded evidence, not current release gates. |
| Local pilot operator | [Historical pilot](tools/legacy/PILOT.md): preserved reference, not the current execution path. |
| Extraction developer | [Enrichment contracts](docs/ENRICHMENT.md): supplied-evidence CLI, validation, diagnostic limitations. |

## Lineage And License

DocIntel is derived from Microsoft's
[Azure-Samples/visionary-lab](https://github.com/Azure-Samples/visionary-lab)
template; its inherited gallery/media, infrastructure, and configuration surfaces
are not all part of the batch workflow. Preserve the Microsoft notice and terms
in [LICENSE.md](LICENSE.md). Legacy routes require a separate security review.
The local frontend environment file is no longer tracked and is excluded from
image contexts. Inherited literal `dummy` values were resolved as placeholders,
not exposed credentials. The clean publication branch excludes the unpublished
development history; never merge the checkpoint branch into it.