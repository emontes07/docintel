# Native SDK no-send gates

Before reserving provider budget or sending a DI, model, WebIQ, or original-page request,
construct and serialize the **actual production request** with the installed
native SDK. A sample prompt, hand-built HTTP body, or mocked SDK constructor is
not admission evidence.

`backend/sdk_preflight.py` uses genuine `AzureOpenAI`, `AsyncAzureOpenAI`,
`DocumentIntelligenceClient`, and `httpx.Client` constructors. Native requests
reach only an in-memory capture transport, which stops execution without a
provider response. Real authentication is replaced by local inert credentials.
No source, prompt, output, token, API key, or raw header is retained in a receipt.
The OpenAI transport is selected from the installed SDK's exported default
client (`httpx` or `httpx2`); no additional dependency is required.
Original-page retrieval additionally uses the genuine `_PinnedHTTPSConnection`
and standard-library `http.client` constructor/HTTP serializer with an inert
per-instance send function.

## Admission ordering and integration

1. Prepare the complete real input and validate authorization/limits.
2. Run the matching native no-send preflight below.
3. Persist the compact receipt when admission provenance is recorded.
4. Only then reserve budget/use the existing authorized reservation and send.

`SDKPreflightError` is **fatal/release-blocking**, not optional-provider
unavailability. Do not swallow it, proceed with a stale receipt, reserve a new
budget, reset a consumed claim, retry a provider, or change IAM. A preflight
does not authorize a continuation.

### Model

After `prepare_inference_request` and before inference reservation:

```python
from backend.core.llm import preflight_structured_request

receipt = preflight_structured_request(
    prepared.system, prepared.user, schema,
    endpoint=endpoint, deployment=deployment,
    sdk_max_retries=0, **prepared.request_parameters,
)
```

Pass the **full prepared prompt and actual Pydantic response schema**. This
shares message, strict JSON-schema, and constructor-option builders with the
production `LLMClient`. The existing GPT-5 producer supplies
`max_completion_tokens=2048` and `reasoning_effort="minimal"`; the gate does not
truncate evidence, replace the schema, alter grounding/applicability/Boolean
rules, or change token reservation accounting. `complete_structured` and its
async counterpart also self-gate before each model attempt and expose
`last_sdk_preflight`. That last-moment gate does not replace admission before
budget reservation.
The local request guard also rejects simultaneous `max_tokens` and
`max_completion_tokens`, invalid token-limit types/bounds, and (for the explicit
`gpt-5` deployment) legacy `max_tokens` or non-default temperature. These
model-specific checks are necessary because native SDK serialization alone
can accept options that the reasoning-model contract does not support.

In the worker this admission check must precede `guard.reserve`, creation of
the live `LLMClient`/real credential provider, and the provider call. A successful
last-moment client self-check cannot retroactively satisfy this ordering.

### WebIQ

```python
receipt = client.preflight_search(
    actual_public_query, allowed_domains=actual_allowed_hosts, authorized=True,
)
# Reserve only after successful preflight, then:
results = client.search(
    actual_public_query, allowed_domains=actual_allowed_hosts, authorized=True,
)
```

Preflight and production share request and native-client option builders.
`search` self-gates before its real stream. Exact allowed hosts remain a local
result filter (not an invented WebIQ API field), represented by a separate
digest in the receipt. The public query, result count, passage format, and
length are the actual serialized production body. Discovery is still
**not evidence**, and no original page is fetched by this gate.
Run `preflight_search` before the search reservation even for an optional tier:
invalid configuration/query/native serialization is a fatal release blocker,
not an optional-tier skip. The authorized runtime supplies the real key;
offline development/tests use only a synthetic key and must not read real keys.

### Document Intelligence

```python
from functools import partial
from backend.analysis_provenance import (
    make_preparation_client, preparation_analyze_kwargs,
)
from backend.sdk_preflight import preflight_document_intelligence

receipt = preflight_document_intelligence(
    source_bytes=actual_pdf_bytes,
    client_factory=partial(make_preparation_client, endpoint=endpoint),
    analyze_kwargs=preparation_analyze_kwargs(actual_pdf_bytes),
)
# Production uses make_preparation_client and preparation_analyze_kwargs too.
```

The shared production factory fixes endpoint, API version, retries/timeouts and
accepts the helper's inert `credential` and `transport` overrides. The helper
requires a genuine native DI client and checks that the factory honored both
overrides **before** entering its transport or executing authentication policies.
The request builder supplies `model_id`, actual binary PDF `body` (bytes or a
seekable binary stream), and bounded `pages`/other native options. The captured
body must exactly match `source_bytes`; the input stream position is restored.

Callers with shared production dictionaries can alternatively pass
`client_options=...` and `analyze_arguments=...`; do not mix the two interfaces.
Neither interface duplicates the producer's DI constructor/request parameters.
Existing verified-operator and native
Azure CLI argument regressions remain separate required checks: a no-send
transport does not prove the real identity or execute Azure CLI.

### Original-page retrieval

```python
from backend.core.websearch import preflight_original_page

receipt = preflight_original_page(
    actual_discovered_url, allowed_hosts=actual_allowed_hosts, authorized=True,
    timeout=15.0, max_bytes=262144,
)
# Reserve web_retrieval only after success, then call fetch_original_page
# with exactly these arguments.
```

The preflight and live fetch share URL/host/limit validation, a request/header
builder, and the native `connection.request` invocation. `fetch_original_page`
also self-gates **before DNS**. The gate instantiates the genuine production
`_PinnedHTTPSConnection`, lets `http.client` serialize the actual GET/path/Host
and approved headers, and captures its HTTP/1.1 bytes through an inert `send`.
It does not resolve a hostname, create/connect a socket, perform a TLS
handshake, authenticate, or fabricate an HTTP response. The local verifying SSL
context is constructed normally; address safety and server TLS validation
remain required in the unchanged live fetch.

Error classification is deliberate:

- `ExternalEvidenceError` remains URL/host/authorization/limit data-policy
  rejection. An unsafe discovery URL is **not attempted or reserved**, not a
  native SDK incompatibility and not a reason to weaken the allowlist.
- `SDKPreflightError(provider="web_retrieval")` is native constructor/request
  incompatibility and must become a **fatal release blocker**, not be swallowed
  as an optional provider/retrieval failure.

The returned receipt uses `status="validated_no_send"`, records Python
`http.client` and OpenSSL versions, and fingerprints the canonical endpoint,
path, complete serialized wire request, and exact header block. It exposes only
header names, never raw URLs/paths/header values. `request_sha256` follows the
method/URL/body convention; `wire_request_sha256` additionally binds the exact
HTTP framing and headers. GET has an empty body (`body_bytes=0`).
Allowed-host digest, timeout, response byte bound, and explicit zero
DNS/socket/TLS/credential/network counters are included. The receipt explicitly
does **not** claim DNS-address safety or source-content verification.

## Evidence and limits

Receipts include installed SDK/transport versions, method, API version, safe
shape metadata, byte counts, and SHA-256 fingerprints for the exact serialized
body and request target/body. They explicitly say no authentication, provider
send, or fabricated provider response occurred. They are **local compatibility
evidence only**, not successful analysis, model findings, service-side schema
acceptance, server authentication, availability, entitlement, remaining quota,
deploy authorization, or discovery grounding.

DI receipts are deterministic and can be pinned in an offline continuation plan
then compared exactly with an immediate pre-send rerun. They contain no
timestamps or generated request IDs. For the standard `prebuilt-layout` route,
the proof includes its native path/model, API-version/pages query,
`request_options={"pages": "1-5"}`, content type, and source fingerprint.
Private/custom route/model values are hashed rather than disclosed. Query
projection completeness is explicit; the full target remains fingerprinted.
`transport="in_memory_no_send"`, `transport_real_calls=0`, `network_calls=0`,
and `credential_real_calls=0` distinguish the single in-memory capture
(`captured_requests=1`) from a real transport/authentication/provider call.

The release/deployment owner must require these applicable request gates plus
its deployment-specific native command validation before live execution.

## Local evidence is not deployed-image evidence

Installed versions in a local receipt describe only that Python environment.
They do **not** prove which SDKs or request-building code run in a deployed
image. Live activation remains blocked until the release owner verifies:

- the reviewed code revision and dependency-lock digest used by the image;
- installed SDK/transport versions in that same-code image against the locked
  dependencies and expected receipt versions;
- a network-denied, credential-free native no-send image smoke using the same
  production factories, full prepared request/schema, and admission ordering.

Keep the image/code/lock identifiers with the smoke evidence. A missing lock,
version/code mismatch, missing smoke, or native incompatibility must not be
converted into an optional skip or a successful provider result. These checks
do not authorize deployment, grant a continuation, reset a consumed claim, or
permit a new reservation. Actual token/verified-identity regressions remain
owned by the preparation path; the synthetic preflight credential is never
evidence of real identity verification.

Focused offline regression suite:

```bash
python -m pytest -q tests/test_sdk_preflight.py tests/test_original_page_preflight.py \
  tests/test_websearch_webiq.py tests/test_response_validation.py
```

The native regression suite explicitly denies sockets, DNS, subprocess
execution, and real credential acquisition. SDK constructors are not mocked.
Any synthetic response used to compare production WebIQ serialization is
test-only and is never presented as a provider result.
