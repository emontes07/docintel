# SharePoint access: one-test plan for the IT walkthrough

**Plan only. No new diagnostic, credential acquisition, grant, consent, retry or
configuration change is authorized or performed.** SharePoint ingestion remains
disabled/unverified. Local or Blob-copy success is not SharePoint success.

## What the retained diagnostics establish

The older September 30/October 1 readiness record found the approved folder/file
and Graph metadata, but its error handler could not identify which content stage
returned 401. That record alone does not diagnose the cause.

The separately approved October 3 diagnostic was more specific:

| Stage | Result |
| --- | --- |
| A: Graph item metadata | 200 |
| B: Graph `/content` | 302 |
| C: redirected download | 401 |
| D: post-download metadata | Not reached |

It used the **existing approved Azure CLI user**, not the worker managed
identity. Three source HTTP requests were observed, zero verified document
bytes, no hash equality and no version verification. Its single allowance is
consumed; do not reuse it.

The sanitized reservation does not identify the user's object ID, token claims
or actual `Location` hostname. The configured approved tenant source host was
`m365cpi40611510.sharepoint.com`; the existing source helper only permits that
exact configured tenant host at stage C. Reaching HTTP 401 at C supports passage
through that check, **not** a separately captured redirect-host audit.
The older record says approved identity was verified, not what scopes it held.
Do not retroactively invent those missing facts.

The helper requests `https://graph.microsoft.com/.default`. That is an OAuth
token **request scope**, not proof of the issued token's audience, delegated
scopes or app roles. Metadata access does not establish content-download access,
and 401 alone does not prove a missing folder ACL, consent or Graph permission.

## What Mark and IT need to do first (plain language)

1. Mark grants the intended user access to the **specific approved folder and
   file**. Confirm the exact tenant, site, library, folder and file together;
   no parent-library or unrelated-folder exploration.
2. In the browser, that same user should be able to open/download the approved
   test file. An account that can only see metadata is not sufficient. A browser
   check is separate user activity, not proof the application can download it.
3. IT identifies which identity the eventual app will use. A successful user
   test does not grant access to, or validate, the worker's managed identity.
   Record the tenant and identity type; retain exact IDs privately.
4. IT checks existing Graph consent, SharePoint access and any conditional-access,
   device, session, tenant restriction or download-protection policies. Diagnose
   before proposing changes. Do not request blanket tenant-wide permissions as
   a guess, weaken security policies or forward a Graph token to SharePoint.
5. Agree one test file, its approved SHA-256, expected item/drive IDs and a time
   window. Obtain a **new explicit one-test approval** after access is granted.
   This plan and the old reservation do not authorize the test.

## Bounded single-test procedure — later, after approval

Reuse `backend.core.pilot_sources.SourceReference` and `retrieve_document`;
do not introduce another downloader or a general workflow framework. Keep
`reference.enabled` and hosted source state unchanged now. The eventual one-off
diagnostic uses a private in-memory reference only under its new approval.

**Limits:** one exact PDF, one credential path, one retrieval sequence, at most
four source HTTP requests, no automatic retries, no alternative identity,
resource, endpoint or file, no analysis/inference. Each source request has the
helper's 30-second timeout; metadata limit 64 KiB, file limit 10 MiB. A token
acquisition, if needed, is separate identity-provider activity and must be
included in the later approval; the four-request ceiling counts source requests,
not identity-provider requests.

Before stage A, IT verifies the actual identity and Graph token locally, without
logging/exporting the token. Record only sanitized facts: credential class,
tenant match, approved principal match/type, intended Graph audience match,
expiry validity and allowlisted granted `scp`/`roles` names (or required-scope
match booleans). Graph audiences can be represented by a resource URI or resource
application ID; compare against the tenant's valid Graph audience rather than
mistaking `.default` for `aud`. Reading JWT claims is not cryptographic validation
and token possession is not proof of folder access. If token facts are unavailable,
record “unverified” and stop for IT rather than guessing.

1. **A — metadata:** Graph GET for the exact drive/item with the existing field
   allowlist. Verify item ID, exact approved `webUrl`, nonempty ETag and bounded
   positive size. Send authorization **only to `graph.microsoft.com`**.
2. **B — content:** Graph GET `/content` with the same Graph credential. Accept
   only bounded 200 bytes or one 302. Automatic redirects remain disabled.
3. **C — redirect, if present:** validate HTTPS, no userinfo/fragment, no explicit
   port, and exact configured tenant SharePoint hostname. Send a **new request
   with no Authorization, Proxy-Authorization, Cookie or inherited credentials**.
   Never forward credentials across hosts, even “to fix 401.” No second redirect,
   login-page automation, URL rewriting, host broadening or bearer-token fallback.
   The signed URL is ephemeral secret material: never record its query/path.
4. **D — metadata:** only after bytes return, reread the exact Graph item using
   the Graph credential. Require unchanged ETag, matching before/after size and
   downloaded length. Validate PDF signature/trailer and exact approved SHA-256.

Stop immediately on the first rejected status or boundary. A 401/403 is a failure,
not permission to retry, switch identities or add scopes. An unknown/interrupted
outcome consumes the one-test allowance. Document the last completed stage;
further investigation needs a new approval. No document is submitted to AI.

## Evidence: metadata only, sanitized and private

Before the future test, review the thin diagnostic wrapper around the existing
helper for this allowlist; do not blindly persist full helper metadata, headers,
exceptions, token objects or HTTP tracing:

- UTC start/end, elapsed time, approval/reservation ID and code revision;
- approved source alias, identity type/match booleans and reviewed token facts;
- stage names, status codes, count, retry count zero, terminal error code;
- requested/redirect **hostname only** and allowed-host-match boolean;
- per-stage authorization-present boolean (true only at A/B/D), cookies false;
- bytes received, approved-hash-match, ETag-stable and version-verified booleans;
- content-type category and permitted request/correlation IDs if IT needs them;
- outcome `verified_bytes` or precise failed stage; no ingestion claim on failure.

Never save token values, cookies, Authorization headers, signed download URLs,
response bodies, PDF text/bytes, full private file URLs, directory listings,
unfiltered claims or full error messages in diagnostic output. Retain approved
identifiers and exact hashes only in the private case record; hash/redact
identifiers in shared notes. If the helper reports bytes before a later integrity
failure, report “bytes received, unverified” and do not store/use those bytes.

Only all four applicable stages plus hash/version checks establish access for
**that identity and file version**. It does not validate production worker access
or authorize ingestion. IT can then decide the separately reviewed next step.
