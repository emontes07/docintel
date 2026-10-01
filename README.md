# DocIntel

Turn product workbooks and explicitly associated technical documents into
traceable attribute proposals, human decisions, and a qualified Excel export.
DocIntel addresses the manual work of finding missing product attributes while
keeping source evidence, conflicting values, and uncertainty visible.

> **WIP SOURCE CHECKPOINT: NOT READY TO MERGE OR DEPLOY.** The committed dependency
> lock is inconsistent with Document Intelligence and PyJWT requirements; a clean
> locked installation/image is not verified. SharePoint download remains blocked.
> Hosted batch authentication, private persistence, and worker execution have not
> passed Azure acceptance. Local tests and Bicep compilation do not remove these
> gates. Do not run `azd up`, provision, deploy, grant access, or process customer
> batches without separate approval. Never bypass the lock, hooks, CI, or signing.

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
Entra sign-in, private Blob batch state, and a scheduled Container Apps Job.
Document Intelligence parsing and Azure OpenAI structured extraction are distinct
service calls, not a Foundry agent. Live work remains disabled by default and
restricted to a previously approved pilot scope, not arbitrary catalog products.

Local development is an explicit loopback-only mode with unverified identity and
private SQLite state. The separate local pilot can replay prior results. Its
local authentication controls do **not** secure hosted use.

| Capability | Evidence as of 2026-10-01 |
| --- | --- |
| Intake, queue, finite worker, review, Excel export | Implemented; locally exercised with temporary synthetic data. |
| Validation, leases, replay safety | Offline tests; no duplicate batch/review writes in the focused regression. |
| Desktop/mobile browser workflow | Initial attempts 1-3 failed on an automation sandbox defect; corrected attempts 4-6 passed consecutively on warm servers. Two earlier user Retry incidents remain uncorrelated. No cold-start or real-device acceptance claim. |
| Azure frontend/backend | Last inspection found healthy **older revisions**, not deployment of this batch increment. No job existed in the inspected group. |
| Hosted identity, Blob leases, scheduler | Implementation/template present; Azure end-to-end acceptance still blocked. |
| AI and document sources | Prior bounded parsing/inference observations and two synthetic model cases are not catalog accuracy evidence. SharePoint retained metadata 200, content 302, download 401; no active fallback. |
| Clean installation and release | Blocked on a reconciled, approved dependency lock and clean-image verification. Unrelated working lock edits do not establish reproducibility. |

## Guides

| Reader | Start here |
| --- | --- |
| Business operator | [User guide](docs/USER_GUIDE.md): workbooks, validation, execution choices, exceptions, review, export. |
| Developer or architect | [Architecture](docs/ARCHITECTURE.md): original Mermaid diagrams, implemented wiring, blocked connections, deployed-state distinction. |
| Azure maintainer | [Deployment](DEPLOYMENT.md): settings, command ownership, gated activation, acceptance and rollback. |
| Release reviewer | [Consolidated batch findings](BATCH.md): evidence, costs, security gates, complete browser attempt history. |
| Local pilot operator | [Local pilot](PILOT.md): private setup, replay/live controls, conservative budgets, local-only security. |
| Extraction developer | [Enrichment contracts](docs/ENRICHMENT.md): supplied-evidence CLI, validation, diagnostic limitations. |

## Lineage And License

DocIntel is derived from Microsoft's
[Azure-Samples/visionary-lab](https://github.com/Azure-Samples/visionary-lab)
template; its inherited gallery/media, infrastructure, and configuration surfaces
are not all part of the batch workflow. Preserve the Microsoft notice and terms
in [LICENSE.md](LICENSE.md). Legacy routes require a separate security review.
The inherited tracked frontend environment file is unchanged and excluded from
image contexts; repository-history secret review remains a gate, not a completed
whole-repository hygiene claim.