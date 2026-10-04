# Standalone WebIQ REST Diagnostic

This diagnostic is not an enrichment adapter, attribute extractor, or customer-data
validation. It imports no application code and does not enable or integrate the
application's WebIQ provider. The separate application adapter now implements the
documented bounded REST contract, but requires explicit per-call authorization;
running this diagnostic does not authorize or enable a customer/pilot lookup.

## Authoritative Contract

References read for this implementation:

- https://webiq.microsoft.ai/llms.txt
- https://webiq.microsoft.ai/documentation/api-reference/web/?view=md
- https://webiq.microsoft.ai/documentation/authentication/?view=md
- https://webiq.microsoft.ai/documentation/error-handling/?view=md

These references establish a REST contract independently of the accelerator's remote
MCP integration. They specify `POST https://api.microsoft.ai/v3/search/web` with
`x-apikey` authentication, not the former speculative `/search` bearer-key contract.
The response envelope is `webResults`, not `results`; grounding is `content`, not
`snippet`. The prior statement that no standalone contract was established is superseded
by these references. The application adapter was checked against the public
https://webiq.microsoft.ai/llms-full.txt Web Search reference and the diagnostic's
consumed schema. The individual web-reference URL currently renders the marketing
landing page; the public full-text reference supplies the contract.

### Application integration status

`backend/core/websearch_webiq.py` now implements one `x-apikey` POST with explicit
`authorized=True`, exact approved HTTPS result hosts, at most three results and
2,000 characters per passage, phase timeouts at most 15 seconds, zero retries,
no redirects, and no environment proxies. `WebIQSearchResult.content` is explicitly
`provider_returned_passage_unverified`; legacy `snippet` remains empty. Empty
results and sanitized failures remain distinct. No passage alone establishes an
attribute fact, original-source authenticity, or verified quotation.

`backend/core/websearch.py:fetch_original_page` separately retrieves one explicitly
authorized, query-free HTTPS text/HTML source from an exact host allowlist. All DNS
answers must be public; a single validated address is pinned for the connection
while retaining TLS hostname verification and SNI. There are no redirects,
credentials, cookies, proxies or retries. The response is capped at 262,144 bytes;
oversize, compressed, binary/PDF, unsupported-encoding, or incomplete responses
fail closed. It returns untrusted plain text, source URL, SHA-256 of original response
bytes, original media type, byte size, and local retrieval time—not verified attributes.
HTML uses stdlib-only static text normalization (`text_normalization=html_visible_text_v1`):
script/style/template, navigation and other non-content subtrees, comments, and
explicitly hidden elements are discarded; entities are decoded; paragraph, line-break
and table-row boundaries become newlines. Plain text remains decoded source text
(`decoded_plain_text`). The hash always covers the original bytes, not normalized text.
Empty normalized content fails closed. No browser, external stylesheet, script, image,
or other resource is loaded. This is not complete CSS/layout visibility evaluation or
prompt-injection prevention; visible source instructions remain untrusted. Citation
paragraph ordinals refer only to normalized text, not original page/table coordinates.
Socket phase timeouts are not a total deadline; OS DNS resolution has its own
resolver timeout. Offline mocked pinning tests are not a live network certification.

Neither utility persists content. Legacy raw-content persistence remains forbidden.
Any retention must occur only in the separately authorized private pilot evidence
path, under its own storage policy; the caller owns public vendor/MPN query
construction, unresolved-attribute selection, identity/support validation and budget
reservation. No live WebIQ call, customer approval, production readiness, cloud
configuration, or deployment is established by these synthetic integration tests.

The reference calls `passage` model-selected query-contextual paragraph extractions.
The diagnostic reports only presence and character counts, without displaying passages
or claiming they are verified quotations, usable attribute evidence, or generated answers.
It requires a JSON object with a `webResults` array (at most three records) and string
`title`, HTTP(S) `url`, and `content` fields per record. Missing or incompatible consumed
fields are reported as `malformed_response`, not guessed or treated as empty success.
Optional crawl/update dates may be absent, null, or empty. Unexpected date formats fail
validation. Crawl/update dates are not relabeled as publication or retrieval timestamps.

The response reference explicitly describes crawl/update dates as optional with possible
empty values, and redirect/instrumentation fields as conditional. It does not provide a
formal nullable schema. The diagnostic retains its existing tolerance for absent/null
optional metadata (and empty dates); this is not a claim that the provider guarantees
null values. Required `title`, `url`, and `content` fields still reject absence or null.
An empty string `content` is allowed and reports no grounding content.

## Structural Failure Details

For HTTP 200 failures, `diagnostics.reason` distinguishes `non_json_response`,
`unexpected_content_type`, `unexpected_top_level_type`, `missing_web_results`,
`unexpected_web_results_type`, `invalid_result_item`, and `invalid_field`.
`result_limit_exceeded` retains the requested three-result bound;
`provider_error_envelope` rejects a top-level `errorCode` even with HTTP 200.
Unexpected exceptions in response processing produce `status: diagnostic_error` with
`reason: response_processing_error`, not a provider schema diagnosis.

Failure diagnostics contain only structural information:

- Normalized content type, with parameters removed. Known media types are allowlisted;
	unknown types are labeled `unrecognized_not_displayed` to avoid reflecting arbitrary
	header text. Missing content type is labeled `missing`.
- `json_decoded`: true or false, or null if an internal failure prevents determining it.
	JSON decoding is attempted even for an unexpected media type; a decoded body does not
	bypass the required `application/json` check.
- Top-level JSON type and allowlisted schema field names. Unknown field names are not
	echoed; only `other_fields_present` is reported. No field values are displayed.
- `webResults` presence, JSON type, and array count when applicable.
- The failing field path, expected/actual types (including `missing` versus `null`),
	and a fixed constraint label for invalid URL/date formats or excess result count.

No raw body is printed when JSON decoding fails. A failure discards any partially
constructed result summaries. No exception messages or stack traces are emitted.
Arbitrary wrappers are never unwrapped, and missing results never become empty success.

The reported manual observation of `malformed_response` with HTTP 200 does not identify
which former rejection branch ran. It establishes neither a valid search response nor
successful result validation. The next separately authorized manual run can locate the
structural mismatch; the existing output cannot establish its cause.

## Commands

From the repository root, using the existing environment:

```bash
.venv/bin/python -m pytest tests/test_webiq_diagnostic.py -q -p no:cacheprovider
.venv/bin/python scripts/webiq_diagnostic.py
```

The second command is intentionally offline and returns exit code 2 with
`live_opt_in_required`. No application settings or dotenv files are loaded.

For ONE caller-authorized public lookup, first make `WEBIQ_API_KEY` available securely
in the local process environment. Do not put it in the command or paste it into chat.

```bash
.venv/bin/python scripts/webiq_diagnostic.py --live --query "Ford Meter Box angle valve specifications"
```

The tool cannot establish whether arbitrary terms are confidential. The caller must
supply public terms only. It never adds manifests, internal IDs, excerpts, or conversation
history. `site:` is a search operator, not a confidentiality or technical boundary.

There is one POST with `maxResults=3`, `contentFormat="passage"`, and `maxLength=2000`.
HTTPX has an explicit 15-second timeout for each network phase, zero transport retries,
no redirects, and no environment-proxy configuration. A phase timeout is not a total
wall-clock deadline. Missing opt-in, query, or key stops before constructing an HTTP client.
No setup, retry, polling, browsing, instrumentation ping, or provider fallback occurs.

Output is a concise JSON summary: success with results, success with zero results,
malformed response, authentication/permission failure, throttling, timeout, redirect,
or other sanitized failure. Exit code 0 means a valid search response, including zero
results; 2 means no live opt-in or a configuration/request/response failure.

Local `observed_at` is separate from provider `crawledAt` and `lastUpdatedAt`; unknown
dates are omitted. Titles and URLs are untrusted labels, escaped and bounded for output.
URL query strings/fragments are omitted; credentials in URL authority are rejected.
Returned, click-redirect, and instrumentation URLs are identified separately and are
never followed, composed into ping requests, or called verified original-source URLs.
No raw passages, payloads, headers, keys, provider error bodies, or exception dumps are
printed or persisted. The tool does not write files.

A live lookup can establish request authorization, response compatibility, returned
locations, and grounding-content availability. It cannot establish original-source
authenticity, passage accuracy, product identity, attribute support, customer-data
approval, production readiness, or integration with the enrichment pipeline.

## Checkpoint Scope

This diagnostic and its mocked tests are independent of the offline enrichment work.
They require the existing declared `httpx` and `pytest` dependencies, not the parser,
Azure SDK additions, local settings changes, or an enrichment manifest. Offline fixture
replay does not establish real parsing or model inference; neither is performed here.
