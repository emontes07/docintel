# Ford retained-parse readiness (offline Track B)

**NO-GO for importing the retained Ford analysis into the production Blob
binding. No new analysis, inference, retrieval, deployment, authentication change
or live-ledger write was performed.** The two Ford products remain deferred.

## Verified evidence and limitation

The approved local `001162_AV11-xxxW-NL_techspec.pdf` is 156,227 bytes, SHA-256:

```text
b50c311840c19d96fd994a2a8f281f243c37e63257aad81c41821e0df6910cfa
```

The retained September 30 `ParsedDocument` has the original local source path,
`sha256:` content version, original analysis time, 49 paragraphs, one table and
represented page 1. Its raw file SHA-256 is
`0e353ecf0369935cc5c1ebb17b6a33bc1d409deb824763a8e6c37f701662df7f`;
the normalized production document digest is
`63f611ea1037fc2c7cae1e2f43ccd4e5c536fae817f09774f09ec0eaf5b7a3a8`.
These are different serializations, **not** raw Azure AnalyzeResult hashes.

The retained envelope SHA-256 is
`090728396dfc7926649f917d2883964df2251f2b241f8f3a11144d6dfef26975`.
Its `origin=imported_verified_pilot` and
`parser_version=prebuilt-layout:2024-11-30:mapping-v1` are intact. Its document
equals the original. The success receipt SHA-256 is
`ee74b716ae0681e964cb2c12ac593069afb83de215fad20ecabd7585e4c078d2`;
it records one successful `prebuilt-layout` submission and zero SDK retries.

However, `backend.pilot` assigned the wrapper's `PARSER_VERSION` during a later
import. The original success receipt does **not** independently record API
version, mapping version, request options, source path/hash or parsed-file hash.
There is no retained raw service AnalyzeResult/operation receipt in this evidence
set. Today's SDK defaults, the later wrapper and represented page count cannot
prove the exact historical request options.

The unchanged production cache contract also requires exact source equality:

| Binding | Full-document cache key | Result |
| --- | --- | --- |
| Original local source | `parses/ff2ac5709377630d521dacec8dd6fba7991629b9755a4206a952bb44c54198ae.json` | Legacy envelope accepted |
| Approved Blob copy | `parses/d4ba2c514724aabefd16bc07fcb89f72eaee79605719590828669af3cc7d8e5d.json` | Local `document.source` incompatible |

Renaming the cache file, rewriting `document.source`, changing its digest or
claiming fresh Blob/SharePoint analysis would manufacture compatibility. None
is done. No worker/core/model schema change is included.

## Import tooling

`backend/parse_import.py` validates independently pinned original PDF, parsed
JSON, cache-envelope and success-receipt bytes. It requires a contemporaneous,
independently retained receipt whose `analysis` object binds:

- `outcome=succeeded`, `submissions=1`, `sdk_retries=0`;
- `model`, `api_version`, `mapping_version` matching `PARSER_VERSION`;
- actual `request_options` (only `{}` or `{"pages":"1-5"}` fit the current cache);
- `source`, `source_sha256`, `parsed_sha256`.

Do **not** retrofit these fields into the old receipt. A success receipt is not
an authorization/reservation record, and import does not authorize new work.

For adequate evidence, the importer retains all four raw artifacts, the original
envelope and origin, source and analysis timestamp. A separate, deterministic
fingerprint binds bytes, parser model/API/options/mapping, original locations and
the unchanged production cache key. `imported_at` is distinct from
`original_parsed_at`; provenance explicitly says `freshly_analyzed=false`.
Full-document and first-five-page keys differ. Unrepresented options fail closed.
Even a verified local import is **not** a Blob/SharePoint cache hit: it retains
the original source binding, so an explicitly reviewed consumer adapter would
still be needed for that path.

`scripts/import_cached_parse.py` accepts a private JSON specification:

```json
{
  "source": {"path": "/private/original.pdf", "sha256": "<64 hex>"},
  "parsed": {"path": "/private/parsed.json", "sha256": "<64 hex>"},
  "cache": {"path": "/private/cache.json", "sha256": "<64 hex>"},
  "receipt": {"path": "/private/success-receipt.json", "sha256": "<64 hex>"},
  "request_options": {}
}
```

```sh
python scripts/import_cached_parse.py \
  --specification /private/import-specification.json \
  --new-isolated-store /private/new-isolated-import \
  --snapshot /private/retained-snapshot.json \
  --snapshot-sha256 <independently-pinned-snapshot-hash>
```

The optional snapshot uses the existing `records -> {base64, sha256}` format.
Every retained record is copied byte-for-byte without interpreting allowances,
results, failures or reservations. Duplicate import keys are rejected. The
destination must be new, private and outside the repository; existing/hosted
stores are refused. No configured store, credentials or provider are used.
Import refusal exits 2 **before** creating the destination.

## Offline validation

- `tests/test_parse_import.py`: synthetic contract, pin, parser/options, receipt,
  source-binding, append-only and CLI regressions.
- `tests/test_private_parse_import.py`: opt-in exact two-Ford gate using the
  private retained records referenced by `row-rerun-snapshot.json`, preserving
  **all** records and original bytes in an isolated in-memory copy. A separate
  synthetic-only case verifies real SQLite persistence, private permissions,
  production `cached()` acceptance and refusal to overwrite an existing store.

```sh
DOCINTEL_TEST_PARSE_IMPORT_ROOT="/private/release-root" \
DOCINTEL_TEST_PARSE_IMPORT_OUTPUT="new-unique-ford-import-gate" \
python -B -m pytest tests/test_private_parse_import.py -q -p no:cacheprovider
```

The private test proves the **NO-GO/refusal**, not successful Ford extraction.
It verifies both exact products lack a compatible Blob parse, demonstrates the
legacy local hit/source-association rejection, runs the new CLI and records
zero service/analysis/inference attempts. The gate writes only new private,
exclusive-create specification/metadata receipts; the separate persistence case
creates a clearly labeled **SYNTHETIC** private store, not a Ford import. Private
inputs, prompts and records do not enter Git or CI.

Recorded October 6 result: **37 tests passed** (35 synthetic regressions, one
exact two-Ford refusal gate and one synthetic persistence case). The private
`ford-parse-import-20261006-v2-gate.json` binds tested code hashes and proves all
**19** records from the October 6 04:05:23 UTC retained snapshot stayed unchanged.
Both `AV11-333W-NL` and `AV11-444W-NL` report `NO_GO_IMPORT_REFUSED`; the CLI exits
2 before creating a Ford store. This historical snapshot does not establish
current hosted capacity or authorize another execution.

## Precise next approval, if original request evidence cannot be recovered

Request **one new Document Intelligence analysis submission total**, shared by
the two Ford products, for the exact PDF hash above at the approved
`av-source-4` Blob-copy binding. Use `prebuilt-layout`, explicitly fixed API
`2024-11-30`, mapping-v1 and the production bounded options `pages=1-5`, maximum
five billable pages, SDK retries zero, no automatic retry after any outcome.
Retain the actual API/options/operation receipt and raw/mapped-result hashes
with source bytes/hash and original location.

This is a **request, not granted authorization**. An operator must separately
approve the charge ceiling/time window and an append-only reservation under the
existing consumed ledger; no historical allowance is reset. No LLM inference,
SharePoint, external retrieval or WebIQ call is included. After that single
parse, repeat the exact two-Ford offline gate before considering a separate
enrichment execution approval.
