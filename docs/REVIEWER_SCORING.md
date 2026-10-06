# Reviewer answer keys, scoring, and “delta since last run”

This is an **offline, owner-scoped review workflow**, separate from extraction
and from ordinary batch review. It reads existing results and writes only under
`answer-keys/<hashed authenticated owner>/`. It does not submit batches, call
providers, update machine results, approve master data, or change live authority.
No machine proposal—including a description-inferred Boolean—starts approved.

## Reviewer walkthrough

1. An authenticated owner captures a scoring snapshot of an existing valid batch.
   The snapshot copies the exact current result attempts and their content hashes.
   Unprocessed items remain visible, not silently excluded.
2. The owner registers the **trusted, unedited sanitized reviewer workbook that
   was sent or will be sent** against that snapshot. Registration accepts its
   existing read-only evidence/proposal columns; no technical template is required.
   It stores private row bindings and read-only row/content hashes. Record the
   returned package ID privately with the review task. The customer workbook
   contains no internal paths, hashes, receipts, session/attempt identifiers,
   snapshot keys, or candidate-index columns, including hidden columns.
3. Edit only the decision cells on `Review`:
   - **Approve:** accept the displayed proposal and enter a `Reason`. Its
     candidate is selected in the server-side private binding, not the workbook.
     One candidate binds automatically at registration (not an approval).
     Multiple candidates require explicit private selection by the registering
     owner; otherwise Approve is rejected and the reviewer can supply Correct.
     Explicit human approval counts toward reviewed value accuracy, but never
     turns inferred evidence into literal evidence or certification.
   - **Correct:** enter the final `Correction`, exact `Correction unit`, and
     `Reason`. Corrections can establish a reference
     answer even when the machine abstained. Use `true` or `false` for Boolean
     corrections (native Excel Boolean cells also work), numeric text for numbers,
     and preserve strings as text. Boolean cells are not interpreted as numeric 0/1.
   - **Reject:** enter a `Reason`, leaving corrections blank. This
     rejects **that customer row and its privately bound proposal only**, not
     other candidates sharing the attribute, and does not invent a reference
     answer. If no candidate is bound (including a row with no proposal), the
     rejection is recorded at row level but judges no machine candidate.
   - Leave every decision field blank for an unreviewed row.
4. Upload the completed workbook to its original snapshot with its private package
   ID. Import is atomic:
   any error prevents the entire answer-key version from being written. Errors
   identify the worksheet row and the validation rule, not private cell contents.
5. Read the score JSON. To revise a review, re-upload the complete worksheet with
   `previous_version` pointing to the old key. Blank decisions in the new version
   are deliberately unreviewed; old decisions are **not implicitly inherited**.
   The old immutable version remains available. Branching revisions are allowed;
   there is no mutable “latest” key and callers must choose the version to score.
6. Capture a later snapshot and request its delta with the previous snapshot ID.
   Both original attempts remain intact. The response includes stable-key changes,
   old/new values and source provenance, and old/new scores using the same key.

### Import column contract (schema version 1)

The customer-visible decision identity and input columns are:

```text
Product ID | MPN | Attribute | Decision | Correction | Correction unit | Reason
```

Existing read-only proposal, status, action and evidence columns are retained.
Identity is exactly **Product ID + Attribute**;
MPN is validated context, not an alternative identity. Snapshots with ambiguous
Product ID + Attribute identities are rejected even if vendor or MPN differs.

Keep every exported header, row and context sheet. Only `Decision`, `Correction`,
`Correction unit` and `Reason` may change. Review row/column
order may change. Duplicate/unknown/missing identities, altered MPN or read-only
proposal/evidence context, unknown headers/worksheets, wrong types/units,
blank corrections, and invalid decisions are rejected. Every
decision needs a 1–2000-character reason. The existing bounded data-only XLSX
reader rejects actual formulas, macros, external workbook links, merged cells,
Excel errors, duplicate cell/header addresses, and malformed archives. Formula-like
**text** is safe literal text, not executable spreadsheet content. Workbook limits
are 10 MiB compressed, 50 MiB expanded, and 10,000 data rows per worksheet.

A trusted baseline may cover only a subset of the snapshot (for example, the
reviewable proposals already sent). Snapshot slots absent from that registered
baseline remain **unreviewed**, never disappear from metric denominators, and
cannot acquire decisions from an upload. Every registered baseline row must still
be retained in the edited upload, including rows whose decisions remain blank.

Other template sheets are context only, never an imported source of truth.
Import compares that context and its hashes with the registered trusted baseline, then binds
decisions to the stored full product, attribute, item, source attempt and result
hash. No client sidecar is required. Upload to the **original snapshot endpoint**,
not a “current batch” endpoint: the server never guesses which attempt was reviewed.
The workflow host must retain the snapshot and package IDs with the review task.
If two attempts have identical visible context, a workbook alone cannot distinguish
them; the explicit authenticated snapshot selection supplies that binding.

This registration adapter supports the existing sanitized layout; technical
columns are neither required nor accepted. Registration requires blank decision
cells and is distinct from importing human decisions. Never register an already
edited response as its own trusted baseline. Keep the original baseline separately.
The optional download is a data-only copy retaining baseline cell content, not
styles or hidden package metadata. The already-sent original workbook is accepted
when its read-only cell content matches; it need not be replaced with that copy.

## Public API

All routes use the existing `actor` authentication dependency and `Cache-Control:
no-store`. Owner identity is never accepted from the workbook or request body.
Unknown and other-owner record IDs return 404. Validation returns an explicit
422, and a snapshot read race returns 409 so the caller can retry.

Paths below are relative to `/api/v1/batches`:

| Method | Path | Input / output |
|---|---|---|
| POST | `/{batch_id}/scoring/snapshots` | Empty JSON `{}`; immutable snapshot |
| GET | `/scoring/records` | Owner's snapshot, registered-package, and answer-key version IDs |
| GET | `/scoring/snapshots/{snapshot_id}` | Snapshot JSON |
| POST | `/scoring/snapshots/{snapshot_id}/reviewer-packages` | Multipart `baseline`, `confirm_trusted_baseline=true`, optional private `candidate_bindings` JSON; immutable registered package |
| GET | `/scoring/snapshots/{snapshot_id}/template?package_id={package_id}` | Data-only registered-baseline XLSX download |
| POST | `/scoring/snapshots/{snapshot_id}/answer-keys` | Multipart `workbook`, private `package_id`, optional `previous_version`; immutable answer-key version JSON |
| GET | `/scoring/answer-keys/{version_id}` | Immutable decisions and provenance |
| GET | `/scoring/snapshots/{snapshot_id}/score?answer_key_id={version_id}` | Overall, per-attribute, per-evidence-tier, and inferred/literal metrics |
| GET | `/scoring/snapshots/{snapshot_id}/delta?previous_snapshot_id={previous_id}&answer_key_id={version_id}` | Explicit “since last run” comparison |

Choose the prior run explicitly from `/scoring/records`; no “last” run is guessed
from a mutable pointer or from another owner's history. Omit `answer_key_id` to
inspect output coverage without scoring unreviewed proposals as correct. Record
lists are newest first; re-capturing the exact same attempts reuses the snapshot.

Programmatic entry point: `backend.answer_key.AnswerKeyService(store)` exposes
`snapshot`, `register_baseline`, `template`, `ingest`, `get_snapshot`, `get_version`, `list_records`,
`score`, and `delta`. It uses the existing conditional-create private store
contract. Neither an upload nor a score invokes the batch worker.
Template downloads are read-only; package creation occurs only on baseline
registration. If exactly one package is registered for a snapshot, `package_id`
may be omitted; multiple packages require explicit selection. Example private
candidate bindings (never workbook columns):

```json
[{"product_id":"SYNTHETIC-001","attribute_id":"Pressure","candidate_index":0}]
```

Bindings are optional for unique candidates; null leaves Approve unavailable.
Unknown/duplicate identities and invalid indices are rejected. Registration is an
owner assertion that the baseline belongs to the selected snapshot; the server
does not guess source attempts from product descriptions or model output.

## Metric definitions and denominator caveats

The unit of requested work is a **slot**: one exact product tuple
`(item_id, vendor, mpn, hierarchy_node)` + attribute ID. Identity is case-sensitive;
rows do not match by spreadsheet position or fuzzy names. A definition change
prevents automatic gold transfer to a later snapshot.

| Field | Meaning |
|---|---|
| `eligible_slots` | Every requested slot, including existing values, missing results, failures, abstentions and conflicts |
| `reviewed_slots` / `unreviewed_slots` | Slots with/without an applicable explicit human judgment |
| `directly_reviewed_slots` / `transferred_gold_slots` | Human judgment bound to this exact attempt / earlier human gold reused to score a different attempt; these sum to reviewed slots |
| `gold_slots` | Approve/Correct judgments supplying a typed value and unit |
| `rejected_slots` | Explicit row rejections; no reference value or rejection of alternatives is inferred |
| `output_slots` / `abstentions` | Slots with/without emitted candidates in the selected slice |
| `conflicts` / `reviewed_conflicts` | Emitted slots with conflict status or multiple candidates, with/without requiring review |
| `single_outputs` | Emitted, nonconflicting slots |
| `scorable_single_outputs` | Explicitly judged single outputs, including rejected outputs, segmented by evidence basis |
| `correct_single_outputs` | Single outputs agreeing exactly with explicit human gold value and unit |
| `candidate_count` / `judged_candidates` / `correct_candidates` | All proposals / proposals with applicable human judgments / matching proposals; alternatives in conflicts remain candidate-level |
| `literal_candidates` / `inferred_candidates` | Evidence basis, preserved independently of reviewer approval |
| `reviewed_inferred_candidates` / `inferred_value_agreements` | Explicitly judged inference proposals / those agreeing with the human reference; included in reviewed value accuracy, never labeled literal evidence or certification |
| `unjudged_candidates` | Proposals without an applicable value reference or an explicit rejection of that privately bound candidate; alternatives to a rejected candidate remain here |
| `existing_slots` / `no_result_slots` | Diagnostic categories; existing master values are not new machine proposals |

Ratios are fractions from 0 to 1. A zero denominator gives **null**, never 100%:
Unreviewed proposals never enter accuracy. Inferred proposals receive **no
automatic credit**. Explicit human Approve or agreement with a typed Correct
reference counts as reviewed value accuracy. Disagreement is an error, and a
privately bound Reject counts only against that candidate. Literal and inferred
basis segments remain separate: value agreement does not establish literal
evidence, certification, or calibrated confidence.

- **accuracy** = correct single outputs / scorable single outputs.
- **candidate_accuracy** = correct candidates / judged candidates.
- **review_coverage** = reviewed slots / eligible slots.
- **gold_coverage** = gold slots / eligible slots.
- **output_coverage** = output slots / eligible slots.
- **correct_coverage** = correct single outputs / gold slots. Gold-known
  abstentions and conflicts do not count as successful single-output value
  agreement; unreviewed inference never supplies its own gold reference.

Strings compare exactly, including case and whitespace. Booleans never equal
numeric 0/1. Integers and equivalent floating-point numbers may agree. Units must
agree exactly; no hidden conversion, tolerance, synonym, or fuzzy matching exists.

Tier and evidence-basis slices use the full requested workload as their coverage
denominator. A multi-tier candidate appears once in each supporting tier:
**tier totals must not be summed**. These slices describe value accuracy of
candidates *supported by* each tier, not causal attribution or source retrieval
success. A tier-filtered view never converts an underlying conflict into a
successful single output. `unattributed` is explicit if no tier is available.

Approve/Correct gold may score a later attempt only for the exact stable key and
unchanged definition. Reject applies only to the originally reviewed immutable
attempt/hash and privately bound candidate; another attempt or alternative
candidate requires review rather than inheriting “wrong.” A row-only rejection
without a bound candidate never contributes to the accuracy denominator.
Transferred gold is **not** a claim that the newer prediction was reviewed; its
count is reported separately from directly reviewed attempts.
This measures **human-reviewed value agreement**, not quotation correctness,
certification, calibrated model confidence, population accuracy, or an
independently blinded evaluation. Review selection bias remains visible through
review and gold coverage.

## Synthetic validation

The tests generate 12 fictional items / 24 slots entirely in memory, block
network access, and exercise ingestion, owner isolation, append-only revisions,
literal/inferred metrics, conflicts, abstentions, invalid workbook contracts,
attempt races, and before/after deltas. No customer workbook or private ID is a
fixture. Run with the project's existing interpreter:

```sh
python -m pytest -q -p no:cacheprovider tests/test_answer_key.py tests/test_answer_key_api.py
```

This implementation provides an authenticated API/JSON view and XLSX reviewer
export, not a frontend screen or standalone CLI. It requires no authentication,
deployment, model, or provider-configuration changes.
