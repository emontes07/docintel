# Excel Batch User Guide

[README](../README.md) | [Architecture](ARCHITECTURE.md) | [Deployment](../DEPLOYMENT.md)

> The batch increment is implemented but not activated or accepted in Azure.
> Use only an operator-authorized environment and approved data. The last observed
> Azure site serves older revisions. Local development uses an unverified identity.
> See [release gates and evidence](../BATCH.md) before interpreting a successful demo
> as permission to process customer workbooks.

## Before You Start

You need two data-only `.xlsx` workbooks and operator-configured document
associations. In the intended hosted environment, use **Sign in** with the approved
Entra account and delegated `Batch.Access` permission. The uploader owns the batch;
cross-user sharing is not implemented. Expired access tokens require sign-in again.

The operator must register each document against all four identity fields: item
ID, vendor, MPN, and hierarchy node. A filename match alone is insufficient.
**Configured document associations** lists reference names, kinds, and association
counts. There is no PDF upload, source URL fetch, or document-registration button
on this page. The workbook's `PDF Tech Spec` cell selects registered references;
it does not instruct the service to fetch an arbitrary URL or workstation file.

## Prepare The Workbooks

Each workbook must contain exactly one populated worksheet; its name is not fixed.
Use unique, nonblank headers in row 1 and contiguous data rows. Keep identifiers as
text, especially leading zeros. Limits per workbook: 10 MiB compressed, 50 MiB
expanded, 500 ZIP entries, 256 columns, and 10,000 data rows. These are safety bounds,
not tested throughput. Formulas, macros, merged cells, external workbook links,
Excel error cells, formatted numeric/date cells, malformed addresses, and blank
row gaps are rejected. Supply literal data, not an evaluated formula workbook.

### Product Manifest

| Required column | Meaning |
| --- | --- |
| `PIMITEM Number` | Product item ID as text. |
| `Vendor Name` | Exact associated vendor name. |
| `MPN` | Exact manufacturer part number. |
| `Hierarchy Node` | Exact `node` in the definitions workbook and source association. |
| `PDF Tech Spec` | One registered reference or a JSON array such as `["synthetic.pdf"]`. |
| `Attributes to Fill` | Exact bound definitions-workbook reference, one attribute name, or a JSON array of attribute names. |

Duplicate item/vendor/MPN combinations are errors. Attribute selections must be
nonempty and unique. Optional `existing_values` is a JSON object keyed by attribute
name; existing values are preserved rather than generated again. Other columns
remain in the exported inputs. `Party ID` is not substituted for the item ID.
Supplementary vendor-table and website columns are retained but not retrieved.

### Attribute Definitions

| Column | Rule |
| --- | --- |
| `node` | Required; exact hierarchy match. |
| `potential_attribute_name` | Required; one definition per node/name pair. |
| `potential_attribute_data_type` | Required; `String`, `Numeric`, or `Boolean` maps to the scalar type when `value_type` is absent. |
| `value_type` | Explicit `string`, `number`, `integer`, or `boolean`; required for Enumerated/Multi-Select declarations. |
| `unit` | Column required for number/integer definitions; blank explicitly means dimensionless. |
| `allowed_values` | Optional JSON array of permitted scalar values, not example answers. |
| `description` | Optional; otherwise the attribute name is used. |
| `potential_attribute_example_values` | Retained input only; never evidence, allowed values, or model answers. |

Multi-select needs a separately approved scalar representation; native set-valued
attributes are not supported. Do not infer units or enumerations from examples.
Incomplete real-workbook mappings still require operator approval.

### Synthetic Example

Use `manifest.xlsx`, with one sheet named `Products`:

| PIMITEM Number | Vendor Name | MPN | Hierarchy Node | PDF Tech Spec | Attributes to Fill |
| --- | --- | --- | --- | --- | --- |
| 001 | Synthetic | PART-1 | Valve | synthetic.pdf | definitions.xlsx |
| 002 | Synthetic | PART-2 | Valve | synthetic.pdf | definitions.xlsx |

Use `definitions.xlsx`, with one sheet named `Attributes`:

| node | potential_attribute_name | potential_attribute_data_type | unit |
| --- | --- | --- | --- |
| Valve | Pressure Rating | Numeric | PSI |

These are workbook cells, not CSV input. Both products must already be associated
with `synthetic.pdf` in the operator's registry. With a registered SharePoint entry,
this example validates with a blocked-download warning and produces no supported
pressure candidate in no-AI mode. It is not a reference answer or live execution
authorization. A configured Blob source additionally needs approved bytes, exact
hash, and a compatible parse for no-AI processing.

## Validate, Then Submit Once

1. Open **Batch Enrichment** at `/batches` (also the home page).
2. Choose **Product manifest** and **Attribute definitions**. Check **Manifest
   attribute-workbook reference**: it defaults to the uploaded definitions filename,
   but must exactly match the reference used in the manifest example.
3. Select **Validate batch**. This checks and privately retains the inputs; it does
   not queue AI work. Workbook-level errors appear in an alert. Row-level errors
   and source warnings appear under **Exceptions** in **Batch queue**.
4. Use **Item filter** > **Failures / validation errors** to inspect invalid rows.
   Correct the source workbook or ask the operator to fix associations, then
   validate again. There is no inline workbook editor. **Submit batch** stays
   disabled until all validation errors are resolved. Warnings can remain.
5. Choose **Batch execution**, then select **Submit batch** once. For live mode,
   the **Authorize live execution for approved products** checkbox is also required.

| Choice or term | What actually happens |
| --- | --- |
| **Evidence validation only (no AI)** | Default `evidence_only`; uses explicitly associated bytes and compatible cached parses. No new parsing or inference call; no manufactured candidates. A missing cache is recorded, not silently analyzed. This can perform storage reads. |
| **Live enrichment (approved scope only)** | Can incur DI page and model-token charges. Requires UI consent, server enablement, exact previously approved pilot product/attribute/hash scope, and unexpired operator approval with durable remaining budgets. Choosing it does not authorize a general catalog run. |
| Cached parsing | Reuses a parse only when source location/hash, parser version, and parsed-document hash match. Original parse provenance is retained. Live extraction can still incur inference cost when parsing is cached. |
| Replay | A separate [local pilot](../PILOT.md) or [supplied-evidence CLI](ENRICHMENT.md) mode that reuses recorded/supplied candidates. It is not a batch dropdown option and is not fresh model verification. |

The batch page does not calculate a price or expose fresh-parse/budget controls.
Live budgets are operator-managed and conservative; interruptions do not authorize
another call. A blocked live request is not a successful enrichment. The worker can
record a no-generation result with a live-not-authorized exception: inspect the
requested method, exceptions, and actual result, not just the selected dropdown.

An unchanged owner/workbook/reference/source-registry snapshot identifies the same
batch. Submission has a durable request key; repeated submission cannot change its
execution mode or restart completed work. Do not alter workbooks, clear storage,
or change keys merely to force another live attempt. There is no batch Cancel,
Retry failed items, bulk approval, or change-mode control.

## Monitor Progress And Exceptions

Choose a **Saved batch**; the option shows time, product count, and state. The
**Refresh batches** icon reloads the catalog/history. A selected queued/running
batch refreshes its detail approximately every ten seconds. Progress totals are
written at slice completion, not a per-token or per-item live stream.

| State | Interpretation |
| --- | --- |
| `invalid` / `validated` | Intake failed / ready to submit; not processed. |
| `queued` / `running` | Awaiting a worker slice / a slice is active. |
| `completed` | Batch processing finished, including exceptions; not necessarily resolved or human-approved. |
| Item `unresolved` | Missing/failed evidence, conflict, extraction issue, or scope/budget restriction. |
| Item `failed` / `interrupted` | Execution failed / reservation survived without a known result. Inspect with the operator; no automatic resubmission. |

The default slice handles at most 100 items with two threads. Remaining untouched
items resume in a later execution; completed items are skipped. A persisted result
after an interrupted status write is recovered as unresolved for inspection.
An interrupted reservation without a result is not blindly rerun. In Azure the
proposed schedule is every five minutes, **but that job was not deployed at last
inspection**. Local API startup alone does not run a worker.

Use **All products**, **Failures / validation errors**, **Unresolved attributes**,
and **Pending review** to focus the queue; **Previous page** / **Next page** navigate
50 rows. Pending review means at least one non-existing attribute lacks a recorded
decision, not necessarily that it has a candidate. Review does not erase a machine
exception or change an unresolved item's original processing state.

## Review A Product

Open **Pending review** or **Open evidence** for a processed item. **Product evidence
and review** shows identity, **Original workbook row**, retrieval/parsing status,
exceptions, and **Immutable machine-result hash**. Verify product identity first.

For each attribute compare every **Original model proposal** to its **Source
excerpt** and **Full source locator**. Conflicts remain separate candidates. Missing
page information is not invented. **Post-generation qualification · Not source
evidence** is a separate reviewer note, not a citation or approval. A valid schema,
literal value match, or citation does not establish semantic correctness.

Choose **Decision** and enter a **Reason**, then **Record decision**:

- **Approve candidate** selects an existing candidate; when several exist, choose
  **Candidate**. Approval is disabled without candidates.
- **Correct** records a **Corrected value** with the definition's type and unit.
  It does not rewrite or make the original proposal supported by evidence.
- **Reject** records rejection and its reason without inventing a replacement.

The hosted backend assigns reviewer identity and time from verified claims; the
batch form does not ask you to type an identity. Development identities are visibly
unverified. A saved decision replaces the form; there is no edit/undo-review flow.
Duplicate decisions are rejected. Existing input values are displayed without a
new-review form. Machine candidates, evidence, and qualifications remain preserved.

## Export And Interpret Excel

**Export Excel** is available for a selected batch, including incomplete batches.
It is an authenticated, non-cacheable download, not a direct public Blob link.
All cells are literal text. The export is a per-item snapshot: processing or reviews
may advance during download. It is not a transactionally frozen approved catalog.

| Worksheet | Actual columns or retained data |
| --- | --- |
| `Batch` | `Key`, `Value`: batch ID/state, export start, input hashes, attribute reference, consistency, qualification. |
| `Inputs` | All original manifest columns/cells. |
| `Definitions` | All original definition columns/cells, including examples. |
| `Results` | `Row`, `Item ID`, `Vendor`, `MPN`, `Attribute`, `Status`, `Candidate index`, `Proposed value`, `Unit`, `Evidence IDs`, `Qualifications`, `Review status`, `Reviewed value`, `Reviewed unit`, `Reviewer`, `Reviewed at`, `Reason`, `Error`. |
| `Evidence` | `Row`, `Evidence ID`, `Source ID`, `Locator`, `Version`, `Excerpt`, `Observed at`. |
| `Provenance` | `Row`, `Execution method`, `Machine SHA256`, `Source provenance`. |
| `Errors` | `Row`, `State`, `Error`, `Warnings`. |
| `Reviews` | `Row`, `Attribute`, `Decision`, `Selected candidate index`, `Corrected value`, `Corrected unit`, `Reviewer`, `Identity status`, `Reviewed at`, `Reason`. |
| `Long text` (when needed) | `Sheet`, `Row`, `Column`, `Part`, `Encoding`, `Text`; ordered lossless chunks for oversized/control-character values. The original cell points here. |

`Results.Status` describes extraction (`existing`, `proposed`, `conflict`,
`missing_evidence`, `retrieval_failed`, or `extraction_failed`), not approval.
`Review status` is `pending`, `approve`, `correct`, or `reject`. An unprocessed row
can instead show its processing state and `pending` without an available decision.
Existing values can also export `pending` because no decision was recorded; inspect
`Status` rather than interpreting every pending row as actionable.

Approval does not populate `Reviewed value`: use `Reviews.Selected candidate index`
(zero-based) to identify the selected proposal. Correction populates the corrected
value/unit. All candidate rows remain exported even after one is selected. Preserve
`Evidence IDs`, source versions, qualifications, exceptions, and identity status
when passing the workbook downstream. Review-ready does not mean approved for import.

## Troubleshooting

| Observed category | Action |
| --- | --- |
| Unsupported XLSX, header/type/unit/hierarchy/association errors | Correct literal workbook cells or operator mappings; revalidate before submitting. |
| `sharepoint_download_401` / **Download blocked (401)** | No document bytes were retrieved. Ask the source owner/operator to resolve approved access; do not use cookies, a local-file substitute, or web fallback. |
| `compatible_parse_unavailable_analysis_not_authorized` | No reusable parse for no-AI mode. Operator action/authorization is required; repeatedly submitting cannot create a parse. |
| `source_not_found` / `source_or_parse_failed_no_retry` | Operator checks registered private document, exact hash, cache compatibility, and permissions. Do not silently substitute a different version. |
| `live_processing_not_authorized_for_this_scope` / `explicit_live_approval_or_budget_unavailable` | Scope, enablement, expiry, or budget gate. The checkbox alone cannot clear it. |
| `invalid_response`, `model_failed`, or no supported candidate | Retain the failed/empty result; inspect source evidence and sanitized failure. Do not treat valid citations as correctness or retry paid work without approval. |
| Queued indefinitely, failed, or interrupted | Ask the operator to inspect worker activation, storage leases, and execution history. There is no portal worker-start button. |
| 401, 403, or missing batch | Sign in again if expired; operator verifies consent, token claims, configured portal origin, and ownership. Do not enable local bypasses in Azure. |
| Review conflict or lost response | Reopen the saved batch/item and inspect the recorded decision before doing anything else. No duplicate review is appended. |
| Browser/test failure | Record timestamp, viewport, operation, request status, and console/tool error. The prior two manual Retry incidents remain unresolved; three later warm read-only passes do not prove cold/mobile-device reliability. |

See [all repeatability outcomes](../BATCH.md#browser-repeatability) and the
[maintainer's acceptance gates](../DEPLOYMENT.md#hosted-acceptance).