# Ford preparation: existing operator analyzes; API identity stores

**Parent-only live execution.** The user approved this exact split route for
**one** new Document Intelligence analysis, as preparation **before any readiness
clock**. No deployment, worker, extraction/LLM, WebIQ, grant, identity attachment
or additional credential is authorized by this helper.

## Existing permissions — do not grant anything

The retained private evidence is
`four-product-existing-role-evidence-20261006.json` (mode `0600`). Role evidence
does not itself establish readiness or authorize processing.

- The existing API system-assigned identity handles private Blob reads, the
  reservation and cache/provenance writes. It has **no DI role**, and needs none
  for this route.
- The existing approved operator has unconditional **Cognitive Services User**
  (`a97b65f3-24c7-4388-baec-2e87135dc908`) at the dedicated
  `di-docintel-pilot-erik3` resource. Only an explicit local `AzureCliCredential`
  for that operator/tenant/subscription performs DI analysis.
- The worker **already has** the same DI role. Do not add it again, attach an
  identity, or start a worker. Future worker execution must reuse both cached
  PDFs and perform **zero fresh analyses**.
- The operator has **no Blob data grant**. The helper never asks that credential
  for a Storage token; all storage stays inside the existing API console.

No `.env` file or WebIQ key is needed. No tokenizer is needed for preparation.

## Exact analysis and accounting

Source: `av-source-4`, `documents/av-source-4.pdf`, SHA-256:

```text
b50c311840c19d96fd994a2a8f281f243c37e63257aad81c41821e0df6910cfa
```

One shared cache serves `PIMITEM-225830` / `AV11-333W-NL` and
`PIMITEM-221315` / `AV11-444W-NL`.

The API verifies the current approved Blob bytes/hash first. It exclusively
claims the deterministic preparation key, archives the original ledger and
CAS-appends **one analysis reservation / five pages / $0.05** using the existing
$0.01/page price and remaining capacity. No new allowance is minted.

The latest `gapfill-actual-outcome.json` baseline has 29 records, observed
`2026-10-06T19:07:52.317123+00:00`; its capture hash is
`caa2cefc13739ece9dd13fac81b8602775f9f23213c866414e6646e24f9b240d`.
Current analysis/pages advance **1/5 → 2/10**. Reserved cost advances
**1,016,296 → 1,066,296 microdollars**. Four prior worker execution records,
every prior reservation and all other counters/history remain intact.
The other 28 records remain canonically unchanged. Actual returned pages are
recorded separately; there is **no refund** for fewer pages or failure.

The local PDF is the already-approved original copy identified by the retained
Ford readiness manifest. Its hash and byte length must equal the Blob values
acknowledged by the API before the local SDK is constructed.

## One-use split sequence

1. Parent performs the existing read-only account/operator/resource checks.
   Scoped role listing uses `--scope ... --include-inherited`, **not `--all`**.
2. API console performs the claim/archive/CAS charge. It returns metadata only,
   never PDF bytes or credentials. No SDK submission is allowed without an
   unambiguous acknowledged claim.
3. Local `AzureCliCredential(tenant_id=..., subscription=...)` makes the single
   `prebuilt-layout` request, API `2024-11-30`, `pages=1-5`. SDK total/connect/
   read/status retries are all zero; result wait is bounded to 120 seconds.
   Existing production byte parsing and mapping are reused.
4. The credential wrapper checks the actual credential token's `oid`, `tid` and
   Cognitive Services audience **in local memory only**. It records allowlisted
   identity metadata, not the token. This is not a claim of local JWT signature
   verification; the DI service authenticates the actual request.
5. The accepted operation's metadata is saved privately locally, then appended
   through the API console. If this acknowledgement is lost, it is not retried.
   The same existing SDK poller may finish so its mapped result can be retained
   locally; the helper then stops before cache finalization.
6. The new mapped parse and complete receipt are saved locally, mode `0600`,
   **before any cache persistence**. The API validates the submitted operation,
   actual operator, Blob/local-copy equivalence and result hashes; records
   measured usage; creates one bounded cache; and checks unchanged production
   `BatchProcessor.cached()` against both Ford bindings.

All transport phases are bounded by the existing 16-KiB console frame. The
reviewed storage-only Python program and plan are appended as private audit
records during the first API phase, hash-bound to the owner-reviewed plan.
Later phases load that same verified program into their console process, avoiding
retransmission of code alongside the mapped parse. **No app files are installed
or modified, no deployment occurs, and no DI credential/code runs in the API.**

Operator bearer tokens are never put in console programs, arguments, Blob
records, receipts, logs or files. The explicit wrapper rejects Storage scopes.
Only allowlisted identity metadata and private mapped-parse evidence cross the
storage boundary. No credential fallback is implemented.

Any existing claim, changed semantic history, transport uncertainty, invalid
scope, cache conflict or exhausted capacity stops processing. All attempts are
create-once. **Never rerun the entire helper after an unknown outcome, refund
the reservation, overwrite a receipt, or request another analysis automatically.**
The original ledger archive and locally retained parse enable read-only diagnosis.

## Parent invocation

```sh
PY=/Users/erikmontes/Desktop/repo/docintel/.venv/bin/python
WORK="/Users/erikmontes/Library/Application Support/DocIntel/releases/angle-valves-internal-20261004"
API_SOURCE=1f6130e7fb9dfb00bcd9e1447300d8dded9895c2
```

Plan creation is offline and create-once:

```sh
"$PY" -B -m scripts.ford_analysis_preparation plan \
  --work "$WORK" --api-source "$API_SOURCE" \
  --plan-name ford-preparation-plan-v4.json
```

Plans v1–v3 and their gates remain preserved but are superseded for this changed
identity route. The plan pins the existing API source/image, exact helper and
storage-program hashes, latest semantic history, both identities and local PDF.

The parent records a **narrow envelope derived from the already-granted split
approval**, with exactly these fields (no new approval decision is implied):

```text
schema_version: 1
approved: true
approved_by: plan.approved_by (existing operator)
approved_at: actual timezone-qualified authorization timestamp
packet_sha256: SHA-256 of canonical sorted compact ASCII plan JSON
contract: exact plan.contract
parent_live_executor_only: true
existing_operator_di_access_verified: true
operator_analysis_api_storage_approved: true
```

Do not pass the broader four-product owner capture directly as this envelope.
The separate private
`four-product-preparation-identity-authorization-20261006T221236Z.json` records
the explicit split-identity approval; it is neither a readiness receipt nor the
narrow packet-bound envelope above. The parent must derive and record that
envelope against the reviewed v4 plan before execution.
The helper independently rechecks the existing operator's grant and the actual
SDK token identity. Store the envelope mode `0600`, create-once.

**Only the parent executes:**

```sh
"$PY" -B -m scripts.ford_analysis_preparation execute \
  --work "$WORK" --api-source "$API_SOURCE" \
  --plan-name ford-preparation-plan-v4.json \
  --authorization "$WORK/<parent-recorded-split-preparation-approval>.json"
```

Pending inference-capacity additions do not block this existing-capacity
preparation. They remain a separate future gate; no readiness clock or model/
search execution starts here.

## Provenance and continuation interface

Store prefix:
`operations/ford-analysis-preparation/<source-sha256>/`

Immutable records include `attempt.json`, `authorization.json`, `ledger-before.json`,
`reserved.json`, `packet.json`, `storage-program.json`, `submitted.json`, `parsed.json`,
`analysis-receipt.json` and `completed.json`. Missing completion means not ready.

Receipt `analysis_identity` records the actual existing operator's principal,
tenant, subscription and credential type, with token-identity-match metadata.
`storage_identity` records the API principal. `input_provenance` explicitly states
that the analyzed input was the approved local copy equal to the verified Blob,
and retains the Blob binding, hash, ETag and verification timestamp.

The receipt also preserves model/requested and returned API versions, explicit
options, operation ID, timestamps, SDK package/version, actual page numbers/count
and SDK/mapped-result hashes. `sdk_result_sha256` hashes sorted compact ASCII JSON
of `AnalyzeResult.as_dict()`; it is **not a raw HTTP-response hash**.

The unchanged bounded production cache key is:

```text
parses/2d9239607c598350d1cdd3ca96ee84acc4e0eb5c2cf11ee5c312492b774c63f3.json
```

`document.source` is `batchblob:///documents/av-source-4.pdf`. This is a **new**
analysis, not a relabeled legacy local parse. No full-document cache is fabricated.

For authorization, JSON history and cache/ledger comparisons use `record_sha`
(parsed, canonical JSON), including `cache_canonical_sha256`,
`ledger_after_canonical_sha256` and `ledger_before_canonical_sha256`. Key order
and whitespace are not changes. Raw byte hashes are capture-integrity metadata;
PDF and implementation-code hashes remain byte hashes. The mapped digest uses
the existing `ParsedDocument.model_dump_json()` normalization.

## Offline validation

```sh
"$PY" -B -m pytest tests/test_ford_analysis_preparation.py tests/test_docintel.py \
  -q -p no:cacheprovider
DOCINTEL_TEST_FORD_PREPARATION_WORK="$WORK" \
DOCINTEL_TEST_FORD_PREPARATION_OUTPUT="new-unused-split-gate.json" \
"$PY" -B -m pytest tests/test_private_ford_preparation.py -q -p no:cacheprovider
```

The opt-in gate uses all 29 retained records and real approved PDF bytes in an
isolated in-memory store, a **synthetic SDK response**, fake CLI identity/role
reads, and the exact compiled storage program for every API phase. It verifies
charge-before-SDK, both identity roles, no bearer transfer, local retention
before cache, no retry/refund, canonical preservation and both cache checks.
Its new mode-0700 `SYNTHETIC-local` directory contains only mode-0600 reproduction
artifacts. These are **not** actual Ford analysis or live cache/authority records.
