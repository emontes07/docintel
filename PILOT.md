# Local Run and Review Prototype

[README](README.md) | [Excel batch user guide](docs/USER_GUIDE.md) |
[Batch findings](BATCH.md) | [Hosted deployment gates](DEPLOYMENT.md)

This is the separate single-product, local-only workflow. Use the Excel batch
guide for workbook intake and qualified Excel export. The loopback controls and
locally entered reviewer labels below do not secure a hosted deployment.

**WIP: NOT READY TO MERGE OR DEPLOY.** The dependency lock remains inconsistent
with the source checkpoint. Public artifact-host connectivity prevented a clean
installation. This prototype uses the existing installed Python and Node
dependencies; no alternate registry, new package, or lock edit was used.

## Start and Verify

Run from the repository root using the existing `.venv` and
`frontend/node_modules`:

```sh
bash scripts/check-pilot.sh
bash scripts/pilot-dev.sh
```

Open <http://127.0.0.1:3100/pilot>. The dedicated local FastAPI entry point is
`backend.pilot_api:app`, bound to `127.0.0.1:8011`. It reuses the backend parser,
enrichment contracts, LLM client, validators, and review function. It deliberately
does not mount legacy gallery, Blob, Cosmos, or environment endpoints.

Use `PILOT_FRONTEND_PORT` and `PILOT_BACKEND_PORT` to select other free ports;
the launcher supplies the matching loopback proxy destination. Do not run the
legacy public-bind launch commands for this demo. Stop both servers with Ctrl-C.

The launcher validates existing private configuration and installed executables,
then checks that both ports are free and distinct. Missing/invalid configuration
and occupied ports produce actionable errors without printing setting values,
installing dependencies, or stopping an existing process. The supervisor owns
separate process groups: Ctrl-C, SIGTERM, or either server exiting shuts down only
its own children. It never searches for or kills a process by port. There is a
small check-to-bind race; a server startup failure still shuts down its sibling.
Nothing in startup submits analysis, inference, or a review decision.

Private state defaults to the current user's private application-data directory.
`DOCINTEL_PILOT_HOME` can
explicitly select another private directory **outside the repository**. No
temporary-session path or shell variable is required for normal operation.

## One-Time Private Setup

The previously tested workstation was configured; a new workstation is not assumed
to be ready. Setup refuses to overwrite an existing directory. To reconstruct, explicitly
provide the durable original successful pilot folder, its separate annotated
result, and the preserved SharePoint source-readiness JSON:

```sh
.venv/bin/python -m backend.pilot setup \
  --prior-pilot "$PRIOR_PILOT_DIR" \
  --annotations "$ANNOTATED_RESULT_PATH" \
  --sharepoint-record "$SHAREPOINT_READINESS_PATH"
```

These are operator-supplied CLI paths, never accepted by web endpoints. Setup
checks the approved product/two-attribute scope, source SHA-256, pending original
result, and notes-only annotated copy. It copies the prior result/annotations
and a compatible parse into the private store. Original artifacts are not
modified. The configuration retains the explicitly configured original local
PDF path; keep that customer-material folder read-only and available.

The known approved product association is imported from the successful pilot's
manifest. It is not inferred from model proposals, filenames, or expected
answers. Each run checks configured source identity and approved content hash
before parsing, then requires the exact configured MPN and manufacturer identity in parsed evidence
before inference. This is a bounded association check, not a general product
matching service or workbook importer.

## Command Workflow

Set the path variables to approved private locations outside Git. Select
`PILOT_SOURCE_ID` from the configured catalog; the value is not inferred from a
filename. Live commands below require the separate explicit authorization and
remaining budgets described here; this documentation is not that authorization.

```sh
.venv/bin/python -m backend.pilot catalog
.venv/bin/python -m backend.pilot run --source "$PILOT_SOURCE_ID"
.venv/bin/python -m backend.pilot run --source "$PILOT_SOURCE_ID" --live --confirm-live
.venv/bin/python -m backend.pilot run --source "$PILOT_SOURCE_ID" --live --fresh-parse --confirm-live
.venv/bin/python -m backend.pilot show RUN_ID
.venv/bin/python -m backend.pilot review RUN_ID --input "$REVIEW_INPUT_PATH"
.venv/bin/python -m backend.pilot export RUN_ID --output "$REVIEW_OUTPUT_PATH"
```

Replay loads prior generated candidates and compatible parsed evidence; it makes
no analysis or inference calls. Live mode defaults to compatible cached parsing;
`--fresh-parse` explicitly requests another analysis. Model input contains only
product identity, definitions (without examples), and source excerpts. Prior
answers and post-generation qualifications are never model input. Text endpoint
precedence remains `LLM_ENDPOINT`, then `AI_FOUNDRY_ENDPOINT`; `LLM_DEPLOYMENT`
and `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` retain existing configuration/auth.

Live verification disables both SDK inference retries and structured-output
retries. DI uses its existing zero-retry submission with normal operation polling.
The private SQLite ledger allows at most two analysis and three inference
reservations. Reservations are conservative: failures or interruptions do not
refund them even when remote completion is unknown. Do not reset the store to
evade this budget. Imported historical work is not counted as new live work.

Request UUIDs are durable idempotency keys. Reusing a key cannot resubmit work;
changing parameters under the same key is rejected. Only one operation can be
queued/running across processes sharing the store. A completed matching operation
is returned by default. Creating another requires `--confirm-repeat` or the
explicit UI checkbox, in addition to live confirmation. No automatic retries or
fallbacks occur. A process crash leaves its operation blocked rather than
resubmitting. After stopping **all** workers and inspecting its recorded state:

```sh
.venv/bin/python -m backend.pilot abandon RUN_ID --workers-stopped
```

This records interruption without claiming remote cancellation or refunding a
submission. A subsequent distinct operation still needs explicit repeat consent.

## Review and Export

**Start a new run** contains only the next operation's source, execution method,
and consent controls. **Review a saved run** contains the selected record's
execution metadata, stages, evidence, and decisions. Changing new-run controls
does not change saved-run provenance. Parsing outcome and method are separate:
the historical `live`/`cached` stage values indicate success only when matching
recorded parsing metadata exists. Unknown or inconsistent values are not shown
as successful. Historical records are not rewritten for presentation.

Source filenames and mapped page/paragraph/table positions are visible beside
excerpts. Full local paths, raw parse provenance, and private export locations
remain in expandable details and in the exported JSON. No local-file browser
links are created. Export feedback belongs to its saved run, not another selected
record. Proposal, excerpt, qualification, and unverified human decision remain
separate sections.

1. Select the configured pilot, **Local file**, and **Replay prior pilot**.
2. Run replay or select a recorded run. Stage labels distinguish cached parsing,
   new parsing, replayed output, new inference, validation, and failures.
3. Inspect each original proposal, exact supporting excerpt, mapped page/paragraph
   or table/cell locator, and separately labeled post-generation qualification.
4. Choose approve, correct, or reject. Enter an honest reviewer label and reason.
   Approval selects a candidate; correction must satisfy the existing definition
   and unit. No decision is selected or submitted automatically.
5. Export JSON. Schema `docintel.review.v1` includes source identity/hash/ETag,
   stages, parsing origin, immutable machine result/hash, separate review records,
   reviewed projection, qualifications, timestamps, and unverified reviewer status.

Example review input (a deliberate demo-only rejection, not a human approval):

```json
{
  "attribute_id": "Pressure Rating",
  "decision": "reject",
  "reviewer": "Demo operator (locally entered, unverified)",
  "reason": "Demo-only review exercise, not a product-data judgment."
}
```

For correction supply `corrected_value` and `corrected_unit`; do not supply a
candidate index. For approval supply `candidate_index`; do not supply a corrected
value. Reviews are append-only, one decision per attribute in this prototype.
Corrections never replace original candidates. There is no master-data write.
The UI saves each export into the private store's `exports` directory and displays
its location; it does not depend on browser downloads or accept output paths.
CLI export rejects repository destinations and existing filenames.

## Source Status

- **Local:** working, explicit read-only PDF source; content SHA-256 checked on
  every run. Compatible cached parses are keyed by source ID, canonical location,
  content hash, and parser/mapping version, with a separate parsed-content hash.
- **SharePoint:** adapter implemented and offline-tested using recorded drive/item
  IDs. Prior Graph metadata returned 200 and `/content` returned 302; the fresh
  redirected URL returned 401 without a Graph bearer header. It is disabled in
  the demo configuration until a concrete authorized correction establishes
  retrieval. No additional content sequence was attempted for this implementation.
  No cloud/local byte equality is claimed. Source selection never falls back.
- Graph A/B/C/D outcomes are retained separately. The token is sent only to Graph;
  the unmodified fresh HTTPS download URL is used without auth forwarding and is
  never logged or persisted. Metadata failure after download retains the fact and
  hash of byte retrieval but blocks extraction; ETag is never a content hash.
- The manifest was absent only from the inspected shared **Files** child folder.
  No parent/site-wide search was performed during prototype work. The local
  approved association remains derived from the original successful pilot.

## Validation and Release Limits

`scripts/check-pilot.sh` runs mocked source/workflow/API/supervisor tests plus the
existing parser, enrichment, and endpoint-configuration tests, full frontend
ESLint and TypeScript checks, and network-free gallery mapping regressions. Normal tests
make no external calls. Synthetic test values are intentionally not pilot answers.
Coverage includes credential boundaries, unavailable versus empty evidence,
source/cache/version integrity, live/replay gates, duplicate prevention, budget
limits, positive extraction, abstention, corrections, qualifications, export,
endpoint configuration, and sanitized failures. Existing enrichment tests cover
conflicts, existing values, unsupported candidates, and citation validation.

Interactive browser verification covers desktop/mobile layouts, source gating,
replay/cached/live labels, intercepted corrections, lost-response UUID reuse after
reload, and private versioned export. Corrections during stabilization were
synthetic or intercepted before reaching the real backend. Both customer live
attributes remain pending; no new AI requests or customer decisions were made.
Playwright is provided by the editor, not an added package dependency.

Those eventual browser results are not unconditional first-attempt acceptance.
Two historical user Retry clicks remain uncorrelated with a tool, agent, or app
operation. The later batch investigation recorded three automation failures before
three corrected warm read-only passes. See [all outcomes and limits](BATCH.md#browser-repeatability);
cold-start and physical-device repeatability remain unverified.

The restored auth route exports the existing NextAuth handlers. Gallery metadata
now uses the existing structured metadata contract. Full TypeScript and ESLint
pass; normal production compilation, lint/type gates, and static-page generation
also pass using installed dependencies. The former build-time lint/type bypasses
were removed. This is not clean-install or shared-use security certification.
Restart verification compares both durable runs/reviews, machine results,
qualifications, budget counters, original pilot artifact hashes, and local
settings hashes. No private product identity/hash markers were found in public
or generated browser assets. Unknown static/config/download routes are rejected.

The new API requires loopback client/Host, a local request marker, and no browser
Origin; Next proxies only allowlisted routes to loopback after same-origin/Host
checks. Pilot endpoints reject arbitrary paths/URLs and disable HTTP/service-worker
caching. This reduces accidental exposure; it is **not production authentication**,
OS-user isolation, encrypted storage, tamper-proof auditing, or a multi-user job
system. The private directory is mode 0700 and files are private to the local user.
Reviewer names are locally entered, not verified identities. Local administrators
and other processes under the same OS user can access the store.

Production authentication/authorization, hosted deployment, Copilot Studio,
WebIQ, batch processing, other products, and shared use remain out of scope of
this local pilot. The separate batch implementation has its own [activation gates](BATCH.md).
Do not merge, deploy, push, or expose either development server publicly.

## Local Acceptance Evidence

The retained terminal capture at **2026-09-30 21:02:09 -0600** records
`Missing documented test: tests/test_llm_config.py`, followed by
`Command exited with code 1`. This was an intentional failing documentation-path
check whose explicit `exit 1` terminated the tool shell, not an application
server failure. The corrected documentation uses `tests/test_config.py`.
The application remained available afterward; its controlled Ctrl-C exit was
130 and the next real launch started at 21:05 local time. Without the screenshot's
terminal identifier/timestamp, that warning cannot be definitively matched to
this recovered shell event. No shell profiles or editor settings were changed.

A separate regression demonstrated that child signal exits were collapsed to
code 1. The launcher now preserves `128 + signal`, keeps normal nonzero child
exit codes, and treats an unexpected clean server exit as a launch failure (1).
Its diagnostic includes the observed code and asks the operator to inspect the
preceding server output. Ctrl-C and SIGTERM still clean only owned process groups.

Acceptance used installed dependencies, recorded customer results read-only,
and a separate synthetic private store. Browser checks covered saved replay/live
switching, independent new-run controls, explicit live consent, duplicate replay,
lost-response UUID reuse after reload, test correction with immutable proposals,
qualified private export, and persistence across shutdown/restart. The synthetic
analysis/inference counters stayed zero. An initial synthetic fixture using the
macOS `/var` alias correctly failed source association before parsing; a new
canonical-path fixture was used without rewriting that failed record.

Desktop 1440x1000 and mobile 390x844 checks included a read-only mocked
292-character filename, expandable full locator, screenshots, horizontal-overflow
checks, and mobile excerpt/qualification/decision bounds. No browser mock or
synthetic decision was applied to the customer store. Both real live attributes
remain pending. Controlled shutdowns released both ports; startup rejected an
occupied port with actionable code 2. SIGTERM shutdown returned 143.

## Clean-Install Gate: BLOCKED

The committed manifest pins `azure-ai-documentintelligence==1.0.2`; the committed
98-package lock has neither that package nor its root `requires-dist` entry.
Its only registry is `https://pypi.org/simple`. Existing unrelated working-tree
manifest/lock changes are not the repair baseline. The earlier artifact-host
metadata timeout is unresolved; stabilization made no new registry attempts and
did not change the working virtual environment or lock.

Once an administrator confirms normal approved artifact access, use an isolated
committed snapshot, not the working tree. The following is a procedure to run
then, **not an installation that has been verified here**. Use an approved,
already-installed Python 3.13 interpreter and uv (the inspected version is
0.11.31); no Python download, alternative index, or upgrade flag is needed.

```sh
umask 077
snapshot=$(mktemp -d "${TMPDIR:-/tmp}/docintel-clean.XXXXXX")
git archive HEAD | tar -x -C "$snapshot"
cd "$snapshot"
cp uv.lock uv.lock.before
env -i HOME="$HOME" PATH="$PATH" uv lock --no-config \
  --default-index https://pypi.org/simple --no-python-downloads \
  --python /approved/path/to/python3.13
```

Do not continue on timeout or resolver failure. The empty environment prevents
unrelated `UV_INDEX*`/private-feed overrides; an approved proxy/certificate setup,
if required, must be explicitly supplied by the environment owner. Inspect the
lock diff before installation. This structured gate must pass:

```sh
/approved/path/to/python3.13 - <<'PY'
import tomllib
from pathlib import Path

before = tomllib.loads(Path('uv.lock.before').read_text())
after = tomllib.loads(Path('uv.lock').read_text())
manifest = tomllib.loads(Path('pyproject.toml').read_text())
def identities(lock):
    return {(entry['name'], entry['version'], tuple(sorted(entry['source'].items())))
            for entry in lock['package']}
assert identities(before) <= identities(after), 'Existing version/source changed'
assert all(entry.get('source', {}).get('registry', 'https://pypi.org/simple')
           == 'https://pypi.org/simple' for entry in after['package'])
document = [entry for entry in after['package']
            if entry['name'] == 'azure-ai-documentintelligence']
assert len(document) == 1 and document[0]['version'] == '1.0.2'
root = next(entry for entry in after['package']
            if entry['name'] == manifest['project']['name'])
assert any(entry['name'] == 'azure-ai-documentintelligence'
           and entry.get('specifier') == '==1.0.2'
           for entry in root['metadata']['requires-dist'])
print('Added packages requiring review:', sorted(
    {entry['name'] for entry in after['package']}
    - {entry['name'] for entry in before['package']}))
PY
```

Accept only the DI package and required missing transitive dependencies; reject
unrelated additions, version/source changes, or arbitrary lock churn. Do not
hand-edit the lock. Then, in that same isolated snapshot and approved environment:

```sh
env -i HOME="$HOME" PATH="$PATH" uv lock --check --no-config \
  --default-index https://pypi.org/simple --no-python-downloads \
  --python /approved/path/to/python3.13
env -i HOME="$HOME" PATH="$PATH" UV_PROJECT_ENVIRONMENT="$snapshot/.venv" \
  uv sync --locked --no-config --default-index https://pypi.org/simple \
  --no-python-downloads --python /approved/path/to/python3.13
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -m 'not integration' \
  tests/test_pilot_sources.py tests/test_pilot.py tests/test_pilot_api.py \
  tests/test_pilot_server.py tests/test_docintel.py tests/test_enrichment.py \
  tests/test_config.py
```

Do not copy customer files or local settings into that snapshot. Record platform,
Python/uv versions, lock diff, resolver/sync results, and test results before
proposing only the minimal lock repair as a separate authorized change. A Node
clean install and production security remain separate unverified gates.

## SharePoint Administrator Handoff: BLOCKED

Retained evidence from `2026-10-01T00:26:48.621372+00:00` shows Graph metadata
200, Graph content 302, then fresh HTTPS download 401 with no forwarded Graph
Authorization header. No cloud bytes/hash were established. Site, drive/item,
version metadata, and detailed stage evidence remain in the private controlled
retrieval record, not in this repository. Request/correlation headers and the
authentication challenge were not retained, so the precise reason cannot be
reconstructed. No repeated download was made just to collect them.

The implementation follows Microsoft's [driveItem content download flow](https://learn.microsoft.com/en-us/graph/api/driveitem-get-content):
use the fresh preauthenticated Location immediately, without Authorization.
No concrete code correction was identified, so no new retrieval was justified.

Administrator question: why did this Graph-issued download URL reject the
approved caller context? Check applicable [site/sensitivity-label block-download
policy](https://learn.microsoft.com/en-us/sharepoint/block-download-from-sites),
[unmanaged-device/Conditional Access web-only restrictions](https://learn.microsoft.com/en-us/sharepoint/control-access-from-unmanaged-devices),
and the issuance/validity of the download URL. These are diagnostic possibilities,
not established causes of this 401. Use the retained timestamp and approved
identity in authorized administrative logs. Do not add permissions, switch
identity, import browser cookies, or bypass policy. Keep the source disabled
until a concrete authorized correction warrants a separately approved bounded
verification. Local PDF success is not proof of SharePoint access or byte equality.