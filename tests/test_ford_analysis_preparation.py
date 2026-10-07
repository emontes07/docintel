"""Synthetic SDK only: preparation charges no worker and cannot repeat analysis."""

import copy
import base64
import json
import socket
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.exceptions import ClientAuthenticationError
from azure.core.pipeline.transport import HttpTransport
from azure.identity._credentials import azure_cli

from backend import analysis_provenance
from backend.batch_store import Conflict, Missing, read_json, write_json
from backend.batch_worker import BatchProcessor
from scripts import ford_analysis_preparation as prep
from tests.test_real_pilot import MemoryStore


class ClosedStore(MemoryStore):
    def read_bytes(self, key, max_bytes=None):
        raw, version = super().read_bytes(key)
        if max_bytes is not None and len(raw) > max_bytes:
            raise ValueError("Read bound exceeded")
        return raw, version


@pytest.fixture(autouse=True)
def no_live(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Only a synthetic SDK and isolated store are permitted")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(prep.release, "azure", forbidden)
    monkeypatch.setattr(prep.release, "console_code", forbidden)
    monkeypatch.setattr("azure.identity.ManagedIdentityCredential", forbidden)
    monkeypatch.setattr("azure.identity.AzureCliCredential", forbidden)


def authorize(packet, approved_by):
    return {
        "schema_version": 1, "approved": True, "approved_by": approved_by,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "packet_sha256": prep.sha(prep.canonical(packet)), "contract": copy.deepcopy(prep.CONTRACT),
        "parent_live_executor_only": True, "existing_operator_di_access_verified": True,
        "operator_analysis_api_storage_approved": True,
    }


def packet_for(store, batch_id):
    approval = read_json(store, prep.APPROVAL_KEY)[0]
    return {
        "contract": copy.deepcopy(prep.CONTRACT), "batch_id": batch_id,
        "history_hash_semantics": "sha256_sorted_compact_ascii_json",
        "history_sha256": {key: prep.record_sha(raw) for key, (raw, _) in store.records.items()
                           if key != prep.SOURCE_KEY},
        "tenant_id": "synthetic-tenant", "subscription_id": "synthetic-subscription",
        "analysis_identity": {
            "kind": "approved_operator", "credential": "AzureCliCredential",
            "principal_id": approval["approved_by"], "tenant_id": "synthetic-tenant",
            "subscription_id": "synthetic-subscription",
        },
        "storage_identity": {
            "kind": "api_system_assigned", "credential": "ManagedIdentityCredential",
            "principal_id": approval["identities"]["api_principal_id"], "tenant_id": "synthetic-tenant",
        },
        "local_pdf": {"path": "/synthetic/approved.pdf", "sha256": prep.SOURCE_SHA256,
                      "bytes": len(store.read_bytes(prep.SOURCE_KEY)[0])},
    }


@pytest.fixture
def case(monkeypatch):
    content = b"%PDF-1.7\nsynthetic preparation document\n%%EOF"
    digest = prep.sha(content)
    monkeypatch.setattr(prep, "SOURCE_SHA256", digest)
    monkeypatch.setattr(prep, "PREFIX", "operations/ford-analysis-preparation/" + digest + "/")
    monkeypatch.setattr(prep, "CONTRACT", {**prep.CONTRACT, "source_sha256": digest})
    batch_id = "a" * 64
    items = []
    for index, (item_id, mpn) in enumerate(prep.PRODUCTS.items(), 4):
        product = {"item_id": item_id, "mpn": mpn, "vendor": "Synthetic", "hierarchy_node": "Valve"}
        items.append({
            "item_key": f"row-{index}", "manifest": {"product": product},
            "sources": [{
                "source_id": prep.SOURCE_ID, "kind": "blob", "format": "pdf",
                "source_tier": "internal_pdf", "blob": prep.SOURCE_KEY, "sha256": digest,
            }],
        })
    approval = {
        "id": "b" * 32, "approved_by": "c" * 32, "owner": "synthetic-owner", "batch_id": batch_id,
        "expires_at": "2020-01-01T00:00:00+00:00",
        "identities": {"api_principal_id": "synthetic-api", "worker_principal_id": "synthetic-worker"},
        "limits": {"analysis": 2, "analysis_pages": 10, "spend_microdollars": 2000000},
        "unit_prices_usd": {"analysis_page": "0.01", "input_token": "0.000002", "output_token": "0.000020"},
    }
    ledger = {
        "approval_id": approval["id"], "approval_sha256": prep.sha(prep.canonical(approval)), "invalidated": None,
        "attempted": {"analysis": 1, "inference": 11, "search": 0, "web_retrieval": 0, "retrieval": 0},
        "reserved": {"analysis_pages": 5, "input_tokens": 257868, "output_tokens": 22528, "microdollars": 1016296},
        "actual_usage": {"analysis_pages": 1, "input_tokens": 55810, "output_tokens": 14597},
        "estimated_usage_cost_microdollars": 100000,
        "executions": {str(index): {"started_at": "prior"} for index in range(4)},
        "reservations": {"historical": {"status": "unknown-preserved", "execution_id": "0"}},
        "recovery": {"retained": "never replaced"}, "row_rerun": {"retained": "never replaced"},
    }
    store = ClosedStore()
    write_json(store, prep.APPROVAL_KEY, approval)
    write_json(store, prep.BUDGET_KEY, ledger)
    write_json(store, f"batches/{batch_id}.json", {"id": batch_id, "owner": approval["owner"], "items": items})
    store.write_bytes("results/immutable-failure.json", b'{"status":"failed","detail":"original attempt"}')
    store.write_bytes("review/immutable-human-review.json", b'{"status":"pending","detail":"original review"}')
    store.write_bytes(prep.SOURCE_KEY, content)
    packet = packet_for(store, batch_id)
    return store, packet, authorize(packet, approval["approved_by"])


def synthetic_sdk(monkeypatch, store, *, failure=None, pages=(1,), api_version="2024-11-30", model="prebuilt-layout",
                  text="SYNTHETIC SDK RESULT ONLY; NOT ACTUAL FORD ANALYSIS"):
    """Returns metadata-only probes; the fake content is not a Ford finding."""
    import azure.ai.documentintelligence
    from azure.ai.documentintelligence.models import AnalyzeResult

    payload = {
        "apiVersion": api_version, "modelId": model,
        "content": text,
        "pages": [{"pageNumber": page} for page in pages],
    }
    result = AnalyzeResult(copy.deepcopy(payload))
    calls, options, observations = [], [], []
    monkeypatch.setattr(analysis_provenance, "preflight_preparation", lambda *args, **kwargs: {
        "status": "passed", "service": "document_intelligence", "network_calls": 0,
        "real_credential_calls": 0, "synthetic_fixture_only": True,
    })

    class Client:
        def __init__(self, **kwargs):
            options.append(kwargs)
            self.credential = kwargs["credential"]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def begin_analyze_document(self, model_id, **kwargs):
            ledger = read_json(store, prep.BUDGET_KEY)[0]
            observations.append(copy.deepcopy(ledger))
            assert ledger["attempted"]["analysis"] == 2
            assert ledger["reserved"]["analysis_pages"] == 10
            assert len(ledger["executions"]) == 4
            assert read_json(store, prep.PREFIX + "attempt.json")[0]["state"] == "attempt_claimed_no_retry"
            self.credential.get_token(analysis_provenance.COGNITIVE_SCOPE)
            calls.append((model_id, kwargs["body"].getvalue(), {k: v for k, v in kwargs.items() if k != "body"}))
            if failure is not None:
                raise failure

            def complete(**kw):
                assert kw == {"timeout": 120}
                return result

            return SimpleNamespace(details={"operation_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}, result=complete)

    monkeypatch.setattr(azure.ai.documentintelligence, "DocumentIntelligenceClient", Client)
    return SimpleNamespace(calls=calls, options=options, reservations=observations, payload=payload)


def fake_credential(packet, **overrides):
    claims = {
        "oid": packet["analysis_identity"]["principal_id"],
        "tid": packet["analysis_identity"]["tenant_id"],
        "aud": "https://cognitiveservices.azure.com",
        **overrides,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    token = "synthetic-header." + encoded + ".OPERATOR-BEARER-MUST-NEVER-LEAVE-LOCAL-SDK"
    inner = SimpleNamespace(get_token=lambda *args, **kwargs: SimpleNamespace(
        token=token, expires_on=datetime.now(timezone.utc).timestamp() + 3600,
    ))
    return analysis_provenance.VerifiedOperatorCredential(inner, packet["analysis_identity"])


def test_native_sdk_reproduces_conflicting_cli_selectors_before_di_transport(case, monkeypatch):
    _, packet, _ = case
    identity = packet["analysis_identity"]
    commands = []

    def rejected_command(arguments, timeout):
        commands.append(arguments)
        assert "--tenant" in arguments and "--subscription" in arguments
        raise ClientAuthenticationError(message="Please specify only one of subscription and tenant, not both")

    monkeypatch.setattr(azure_cli, "_run_command", rejected_command)
    credential = analysis_provenance.VerifiedOperatorCredential(
        azure_cli.AzureCliCredential(
            tenant_id=identity["tenant_id"], subscription=identity["subscription_id"],
        ),
        identity,
    )
    transport = MagicMock(spec=HttpTransport)
    client = DocumentIntelligenceClient(
        "https://synthetic.cognitiveservices.azure.com", credential=credential,
        transport=transport, retry_total=0,
    )
    try:
        with pytest.raises(ClientAuthenticationError, match="only one"):
            client.begin_analyze_document(
                "prebuilt-layout", body=BytesIO(b"%PDF-1.7 synthetic\n%%EOF"), pages="1-5",
            )
    finally:
        client.close()
    assert len(commands) == 1
    transport.send.assert_not_called()


def test_operator_factory_uses_native_cli_with_only_tenant_selector(case, monkeypatch):
    _, packet, _ = case
    identity = packet["analysis_identity"]
    commands = []
    token = fake_credential(packet)._credential.get_token()

    def successful_command(arguments, timeout):
        commands.append(arguments)
        assert "--tenant" in arguments and "--subscription" not in arguments
        assert arguments[arguments.index("--tenant") + 1] == identity["tenant_id"]
        return json.dumps({"accessToken": token.token, "expires_on": int(token.expires_on)})

    monkeypatch.setattr(azure_cli, "_run_command", successful_command)
    monkeypatch.setattr("azure.identity.AzureCliCredential", azure_cli.AzureCliCredential)
    credential = prep.operator_credential(
        {"tenant": identity["tenant_id"], "subscription": identity["subscription_id"]}, packet,
    )
    credential.get_token(analysis_provenance.COGNITIVE_SCOPE)
    assert len(commands) == 1
    assert credential.verified_identity["principal_id"] == identity["principal_id"]
    assert credential.verified_identity["subscription_id"] == identity["subscription_id"]


def run(case, **kwargs):
    store, packet, authorization = case
    if not hasattr(store, "local_records"):
        store.local_records = {}
    kwargs.setdefault("save_local", lambda name, value: store.local_records.setdefault(name, copy.deepcopy(value)))
    return prep.run_preparation(
        store, packet, authorization, runtime_check=lambda: None,
        parser_factory=lambda submitted: analysis_provenance.RecordedPreparationParser(
            endpoint="https://synthetic.cognitiveservices.azure.com/",
            credential=fake_credential(packet), submitted=submitted,
        ),
        **kwargs,
    )


def test_single_analysis_reserved_before_sdk_and_fans_out_unchanged_cache(case, monkeypatch):
    store, packet, authorization = case
    original = copy.deepcopy(store.records)
    sdk = synthetic_sdk(monkeypatch, store)
    result = run(case)
    assert len(sdk.calls) == 1
    assert sdk.calls[0] == ("prebuilt-layout", original[prep.SOURCE_KEY][0], {"pages": "1-5"})
    assert sdk.options[0]["api_version"] == "2024-11-30"
    assert all(sdk.options[0][name] == 0 for name in ("retry_total", "retry_connect", "retry_read", "retry_status"))
    assert result["worker_executions_charged"] == 0 and result["readiness_clock_started"] is False
    assert result["ford_extraction_authorized"] is False
    for key, record in original.items():
        if key != prep.BUDGET_KEY:
            assert store.read_bytes(key) == record
    assert store.read_bytes(prep.PREFIX + "ledger-before.json")[0] == original[prep.BUDGET_KEY][0]
    ledger = read_json(store, prep.BUDGET_KEY)[0]
    before = json.loads(original[prep.BUDGET_KEY][0])
    assert ledger["executions"] == before["executions"]
    assert ledger["reservations"]["historical"] == before["reservations"]["historical"]
    assert ledger["reserved"] == {**before["reserved"], "analysis_pages": 10, "microdollars": 1066296}
    assert ledger["actual_usage"] == {**before["actual_usage"], "analysis_pages": 2}
    assert ledger["estimated_usage_cost_microdollars"] == 110000
    receipt = result["analysis_receipt"]
    assert receipt["sdk_result_sha256"] == prep.sha(prep.canonical(sdk.payload))
    assert receipt["raw_http_response_retained"] is False
    assert "content" not in receipt
    assert receipt["actual_page_count"] == 1 and receipt["api_version_returned"] == "2024-11-30"
    assert receipt["source"] == prep.LOCATION and receipt["source_sha256"] == prep.SOURCE_SHA256
    assert receipt["started_at"] <= receipt["accepted_at"] <= receipt["completed_at"] <= receipt["mapped_at"]
    batch = read_json(store, f"batches/{packet['batch_id']}.json")[0]
    for _, binding in prep.selected_sources(batch):
        doc, origin = BatchProcessor(store).cached(result["cache_key"], binding, prep.LOCATION)
        assert prep.sha(doc.model_dump_json().encode()) == receipt["mapped_result_sha256"]
        assert store.read_bytes(prep.PREFIX + "parsed.json")[0] == doc.model_dump_json().encode()
        assert origin == "ford_preparation_analysis_first_five_pages"
    assert result["cache_key"] == prep.cache_keys()[1]
    with pytest.raises(Missing):
        store.read_bytes(prep.cache_keys()[0])
    with pytest.raises(ValueError):
        run(case)
    assert len(sdk.calls) == 1


@pytest.mark.parametrize("failure", [
    RuntimeError("provider-secret-must-not-persist"), TimeoutError("private-response-body"),
])
def test_failure_is_consumed_without_refund_retry_or_secret_error_text(case, monkeypatch, failure):
    store, _, _ = case
    sdk = synthetic_sdk(monkeypatch, store, failure=failure)
    with pytest.raises(ValueError, match="never retry"):
        run(case)
    ledger = read_json(store, prep.BUDGET_KEY)[0]
    assert ledger["attempted"]["analysis"] == 2 and ledger["reserved"]["analysis_pages"] == 10
    assert ledger["actual_usage"]["analysis_pages"] == 1
    assert len(ledger["executions"]) == 4
    failure_record = store.local_records["failure.json"]
    assert failure_record["allowance_refunded"] is False
    assert "provider-secret" not in json.dumps(failure_record) and "private-response" not in json.dumps(failure_record)
    with pytest.raises(ValueError):
        run(case)
    assert len(sdk.calls) == 1


@pytest.mark.parametrize(("pages", "api", "model"), [
    ((), "2024-11-30", "prebuilt-layout"), ((1, 2, 3, 4, 5, 6), "2024-11-30", "prebuilt-layout"),
    ((2,), "2024-11-30", "prebuilt-layout"), ((1,), "2023-07-31", "prebuilt-layout"),
    ((1,), "2024-11-30", "prebuilt-read"),
])
def test_service_contract_mismatch_never_creates_compatible_cache(case, monkeypatch, pages, api, model):
    store, _, _ = case
    sdk = synthetic_sdk(monkeypatch, store, pages=pages, api_version=api, model=model)
    with pytest.raises(ValueError):
        run(case)
    assert len(sdk.calls) == 1
    with pytest.raises(Missing):
        store.read_bytes(prep.cache_keys()[1])
    assert read_json(store, prep.BUDGET_KEY)[0]["reserved"]["analysis_pages"] == 10
    failed = store.local_records["failure.json"]["analysis_metadata"]
    assert failed["actual_page_count"] == len(pages)
    assert failed["sdk_result_sha256"] == prep.sha(prep.canonical(sdk.payload))
    if len(pages) > 5:
        assert not any(key.endswith("completed.json") for key in store.records)


@pytest.mark.parametrize("mutation", ["source", "history", "authorization", "timing", "products", "cache_exists", "attempt_exists", "active_gapfill"])
def test_scope_and_history_changes_stop_before_charge_or_sdk(case, monkeypatch, mutation):
    store, packet, auth = case
    sdk = synthetic_sdk(monkeypatch, store)
    if mutation in ("source", "history"):
        key = prep.SOURCE_KEY if mutation == "source" else "results/immutable-failure.json"
        _, version = store.read_bytes(key)
        store.write_bytes(key, b"changed", version)
    elif mutation == "authorization":
        auth["approved"] = False
    elif mutation == "timing":
        auth["contract"]["timing_exception"] = "worker_execution"
    elif mutation == "products":
        auth["contract"]["products"] = {}
    elif mutation == "cache_exists":
        store.write_bytes(prep.cache_keys()[1], b"existing cache must not be overwritten")
    elif mutation == "attempt_exists":
        store.write_bytes(prep.PREFIX + "attempt.json", b"previous unknown outcome")
    else:
        store.write_bytes(prep.ABSENT_ACTIVATIONS[0], b"active or attempted gapfill")
    before = copy.deepcopy(store.records)
    with pytest.raises(ValueError):
        run(case)
    assert store.records == before and sdk.calls == []


@pytest.mark.parametrize(("field", "value"), [("analysis", 2), ("analysis_pages", 10), ("microdollars", 1980000), ("invalidated", "prior_failure")])
def test_existing_capacity_is_never_replenished(case, monkeypatch, field, value):
    store, packet, auth = case
    ledger, version = read_json(store, prep.BUDGET_KEY)
    if field == "analysis":
        ledger["attempted"][field] = value
    elif field == "invalidated":
        ledger[field] = value
    else:
        ledger["reserved"][field] = value
    write_json(store, prep.BUDGET_KEY, ledger, version)
    packet["history_sha256"][prep.BUDGET_KEY] = prep.record_sha(store.read_bytes(prep.BUDGET_KEY)[0])
    auth["packet_sha256"] = prep.sha(prep.canonical(packet))
    sdk = synthetic_sdk(monkeypatch, store)
    before = copy.deepcopy(store.records)
    with pytest.raises(ValueError):
        run(case)
    assert store.records == before and sdk.calls == []


def test_closed_runtime_failure_prevents_store_mutations(case):
    store, packet, auth = case
    before = copy.deepcopy(store.records)
    with pytest.raises(ValueError, match="processing enabled"):
        prep.run_preparation(
            store, packet, auth, parser_factory=Mock(side_effect=AssertionError("must not create SDK")),
            runtime_check=Mock(side_effect=ValueError("processing enabled")),
        )
    assert store.records == before


def test_charge_write_failure_cannot_submit_or_reclaim_attempt(case, monkeypatch):
    store, _, _ = case
    original = store.write_bytes
    sdk = synthetic_sdk(monkeypatch, store)

    def fail_charge(key, value, version=None):
        if key == prep.BUDGET_KEY:
            raise Conflict("conditional write failed")
        return original(key, value, version)

    monkeypatch.setattr(store, "write_bytes", fail_charge)
    with pytest.raises(Conflict):
        run(case)
    monkeypatch.setattr(store, "write_bytes", original)
    assert read_json(store, prep.BUDGET_KEY)[0]["attempted"]["analysis"] == 1
    with pytest.raises(ValueError):
        run(case)
    assert not sdk.calls


def test_cache_write_failure_preserves_new_mapped_analysis_without_resubmission(case, monkeypatch):
    store, _, _ = case
    original = store.write_bytes
    sdk = synthetic_sdk(monkeypatch, store)

    def fail_cache(key, value, version=None):
        if key == prep.cache_keys()[1]:
            raise Conflict("no cache overwrite")
        return original(key, value, version)

    monkeypatch.setattr(store, "write_bytes", fail_cache)
    with pytest.raises(ValueError):
        run(case)
    assert store.read_bytes(prep.PREFIX + "parsed.json")[0]
    assert read_json(store, prep.PREFIX + "analysis-receipt.json")[0]["sdk_result_sha256"]
    assert read_json(store, prep.BUDGET_KEY)[0]["actual_usage"]["analysis_pages"] == 2
    with pytest.raises(ValueError):
        run(case)
    assert len(sdk.calls) == 1


def test_termination_after_reservation_is_still_not_retryable(case, monkeypatch):
    store, _, _ = case
    sdk = synthetic_sdk(monkeypatch, store, failure=SystemExit("interruption"))
    with pytest.raises(SystemExit):
        run(case)
    assert read_json(store, prep.BUDGET_KEY)[0]["attempted"]["analysis"] == 2
    with pytest.raises(ValueError):
        run(case)
    assert len(sdk.calls) == 1


def test_later_unrelated_records_are_not_directory_membership_drift(case, monkeypatch):
    store, _, _ = case
    store.write_bytes("later-track-b/independent-private-receipt.json", b"new receipt")
    sdk = synthetic_sdk(monkeypatch, store)
    assert run(case)["status"] == "prepared_cache_verified_for_both_products"
    assert len(sdk.calls) == 1
    assert store.read_bytes("later-track-b/independent-private-receipt.json")[0] == b"new receipt"


@pytest.mark.parametrize("grant", ["correct", "wrong_principal", "wrong_scope", "conditional", "absent"])
def test_existing_operator_di_role_is_verified_read_only_or_blocks(monkeypatch, grant):
    config = {"subscription": "synthetic-subscription", "group": "synthetic-group"}
    packet = {"analysis_endpoint": "https://synthetic-di.cognitiveservices.azure.com/",
              "analysis_identity": {"principal_id": "synthetic-operator"}}
    resource_id = "/subscriptions/synthetic-subscription/resourceGroups/synthetic-group/providers/Microsoft.CognitiveServices/accounts/synthetic-di"
    resource = {
        "id": resource_id, "kind": "FormRecognizer",
        "properties": {"endpoint": packet["analysis_endpoint"], "disableLocalAuth": True},
    }
    role = {
        "principalId": packet["analysis_identity"]["principal_id"], "scope": resource_id,
        "roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/a97b65f3-24c7-4388-baec-2e87135dc908",
    }
    if grant == "wrong_principal":
        role["principalId"] = "different-identity"
    elif grant == "wrong_scope":
        role["scope"] = resource_id + "-other"
    elif grant == "conditional":
        role["condition"] = "unreviewed-condition"
    calls = []

    def read_only(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("cognitiveservices", "account", "show"):
            return resource
        assert args[:3] == ("role", "assignment", "list")
        assert "--scope" in args and "--include-inherited" in args
        assert "--all" not in args
        return [] if grant == "absent" else [role]

    monkeypatch.setattr(prep.release, "azure", read_only)
    if grant == "correct":
        proof = prep.verify_existing_di_access(config, packet)
        assert proof["existing_grant_only"] is True and proof["permissions_changed"] is False
    else:
        with pytest.raises(ValueError, match="BLOCKED"):
            prep.verify_existing_di_access(config, packet)
    assert len(calls) == 2


def test_no_retry_even_on_reused_parser_instance(case, monkeypatch):
    store, packet, authorization = case
    sdk = synthetic_sdk(monkeypatch, store)
    instances = []

    def factory(submitted):
        parser = analysis_provenance.RecordedPreparationParser(
            endpoint="https://synthetic.cognitiveservices.azure.com/", credential=fake_credential(packet), submitted=submitted,
        )
        instances.append(parser)
        return parser

    prep.run_preparation(store, packet, authorization, parser_factory=factory, runtime_check=lambda: None)
    with pytest.raises(ValueError, match="already attempted"):
        instances[0].extract_pdf_bytes(store.read_bytes(prep.SOURCE_KEY)[0], source=prep.LOCATION, page_limit=5)
    assert len(sdk.calls) == 1


def test_concurrent_budget_change_before_sdk_is_fail_closed(case, monkeypatch):
    store, packet, auth = case
    sdk = synthetic_sdk(monkeypatch, store)
    checks = []

    def runtime():
        checks.append(1)
        if len(checks) == 3:
            ledger, version = read_json(store, prep.BUDGET_KEY)
            ledger["invalidated"] = "concurrent_change"
            write_json(store, prep.BUDGET_KEY, ledger, version)

    with pytest.raises(ValueError, match="never retry"):
        prep.run_preparation(store, packet, auth, runtime_check=runtime, parser_factory=Mock())
    assert sdk.calls == []
    assert read_json(store, prep.BUDGET_KEY)[0]["attempted"]["analysis"] == 2
    assert read_json(store, prep.BUDGET_KEY)[0]["reserved"]["analysis_pages"] == 10


def reversed_json(value):
    if isinstance(value, dict):
        return {key: reversed_json(child) for key, child in reversed(list(value.items()))}
    if isinstance(value, list):
        return [reversed_json(child) for child in value]
    return value


def test_json_key_order_and_whitespace_are_not_history_changes(case, monkeypatch):
    store, packet, _ = case
    for key, expected in packet["history_sha256"].items():
        value, version = read_json(store, key)
        raw = json.dumps(reversed_json(value), indent=3, ensure_ascii=False).encode()
        assert prep.record_sha(raw) == expected
        store.write_bytes(key, raw, version)
    sdk = synthetic_sdk(monkeypatch, store)
    completed = run(case)
    assert len(sdk.calls) == 1
    assert completed["raw_hashes_are_capture_integrity_only"] is True
    for key, canonical_field, raw_field in (
        (prep.BUDGET_KEY, "ledger_after_canonical_sha256", "ledger_after_sha256"),
        (prep.cache_keys()[1], "cache_canonical_sha256", "cache_sha256"),
    ):
        value, version = read_json(store, key)
        reordered = json.dumps(reversed_json(value), indent=4).encode()
        store.write_bytes(key, reordered, version)
        assert prep.record_sha(store.read_bytes(key)[0]) == completed[canonical_field]
        assert prep.sha(store.read_bytes(key)[0]) != completed[raw_field]
    batch = read_json(store, "batches/" + packet["batch_id"] + ".json")[0]
    for _, binding in prep.selected_sources(batch):
        BatchProcessor(store).cached(prep.cache_keys()[1], binding, prep.LOCATION)


def test_split_route_records_operator_and_api_without_transferring_token(case, monkeypatch):
    store, packet, authorization = case
    sdk = synthetic_sdk(monkeypatch, store)
    result = run(case)
    identity = result["analysis_receipt"]["analysis_identity"]
    assert identity["principal_id"] == packet["analysis_identity"]["principal_id"]
    assert identity["credential"] == "AzureCliCredential"
    assert identity["sdk_token_identity_matched"] is True
    assert identity["token_stored"] is False and identity["token_signature_validated_locally"] is False
    assert result["analysis_receipt"]["storage_identity"] == packet["storage_identity"]
    assert result["analysis_receipt"]["input_provenance"]["kind"] == "approved_local_copy_equal_to_verified_blob"
    saved = b"\n".join(raw for raw, _ in store.records.values()) + prep.canonical(store.local_records)
    assert b"OPERATOR-BEARER-MUST-NEVER-LEAVE" not in saved
    assert b"synthetic-header." not in saved
    assert len(sdk.calls) == 1


@pytest.mark.parametrize("claims", [{"oid": "wrong-operator"}, {"tid": "wrong-tenant"}, {"aud": "https://storage.azure.com"}])
def test_actual_operator_claim_mismatch_stops_sdk_submission_without_fallback(case, monkeypatch, claims):
    store, packet, authorization = case
    sdk = synthetic_sdk(monkeypatch, store)
    with pytest.raises(ValueError, match="never retry"):
        prep.run_preparation(
            store, packet, authorization, runtime_check=lambda: None,
            parser_factory=lambda submitted: analysis_provenance.RecordedPreparationParser(
                endpoint="https://synthetic.cognitiveservices.azure.com/",
                credential=fake_credential(packet, **claims), submitted=submitted,
            ),
        )
    assert sdk.calls == []
    assert read_json(store, prep.BUDGET_KEY)[0]["attempted"]["analysis"] == 2
    with pytest.raises(Missing):
        store.read_bytes(prep.PREFIX + "submitted.json")


def test_operator_credential_cannot_request_a_blob_token(case):
    _, packet, _ = case
    with pytest.raises(ValueError, match="restricted"):
        fake_credential(packet).get_token("https://storage.azure.com/.default")


def test_claim_transport_unknown_cannot_start_local_sdk_or_repeat_claim(case, monkeypatch):
    store, packet, auth = case
    sdk = synthetic_sdk(monkeypatch, store)
    claim = prep.claim_preparation(store, packet, auth, runtime_check=lambda: None)
    assert claim["source_sha256"] == prep.SOURCE_SHA256
    assert "content" not in claim and "token" not in claim
    with pytest.raises(ValueError):
        prep.claim_preparation(store, packet, auth, runtime_check=lambda: None)
    assert sdk.calls == []
    assert read_json(store, prep.BUDGET_KEY)[0]["reserved"]["analysis_pages"] == 10


def test_submitted_transport_unknown_retains_new_local_parse_without_retry(case, monkeypatch):
    store, packet, auth = case
    sdk = synthetic_sdk(monkeypatch, store)
    claim = prep.claim_preparation(store, packet, auth, runtime_check=lambda: None)
    saved, attempts = {}, []

    def unknown(value):
        attempts.append(value)
        prep.persist_submitted(store, packet, auth, value, runtime_check=lambda: None)
        raise TimeoutError("lost acknowledgement")

    with pytest.raises(ValueError, match="never retry"):
        prep.analyze_local(
            store.read_bytes(prep.SOURCE_KEY)[0], packet, auth, claim,
            parser_factory=lambda submitted: analysis_provenance.RecordedPreparationParser(
                endpoint="https://synthetic.cognitiveservices.azure.com/",
                credential=fake_credential(packet), submitted=submitted,
            ),
            submitted=unknown, save_local=lambda name, value: saved.setdefault(name, copy.deepcopy(value)),
        )
    assert len(sdk.calls) == len(attempts) == 1
    assert saved["parsed.json"]["source"] == prep.LOCATION
    assert saved["analysis-receipt.json"]["sdk_result_sha256"]
    assert saved["failure.json"]["submitted_transport_errors"] == ["TimeoutError"]
    with pytest.raises(Missing):
        store.read_bytes(prep.cache_keys()[1])
    assert read_json(store, prep.BUDGET_KEY)[0]["reserved"]["analysis_pages"] == 10


def test_local_copy_must_match_claimed_blob_before_sdk(case, monkeypatch):
    store, packet, auth = case
    sdk = synthetic_sdk(monkeypatch, store)
    claim = prep.claim_preparation(store, packet, auth, runtime_check=lambda: None)
    with pytest.raises(ValueError, match="differs"):
        prep.analyze_local(
            b"%PDF-wrong copy", packet, auth, claim,
            parser_factory=Mock(side_effect=AssertionError("must not create SDK")),
            submitted=Mock(), save_local=Mock(),
        )
    assert sdk.calls == []


def test_local_parse_is_saved_before_any_cache_persistence(case, monkeypatch):
    store, _, _ = case
    synthetic_sdk(monkeypatch, store)
    original_write = store.write_bytes
    saved = {}

    def store_write(key, raw, version=None):
        if key == prep.PREFIX + "parsed.json" or key == prep.cache_keys()[1]:
            assert "parsed.json" in saved and "analysis-receipt.json" in saved
        return original_write(key, raw, version)

    monkeypatch.setattr(store, "write_bytes", store_write)
    run(case, save_local=lambda name, value: saved.setdefault(name, copy.deepcopy(value)))


@pytest.mark.parametrize("field", ["operator_bearer_token", "credential", "raw_http_headers"])
def test_unexpected_credential_metadata_is_rejected_before_console_serialization(case, monkeypatch, field):
    store, packet, auth = case
    synthetic_sdk(monkeypatch, store)
    result = run(case)
    receipt = copy.deepcopy(result["analysis_receipt"])
    receipt[field] = "OPERATOR-BEARER-MUST-NEVER-LEAVE"
    with pytest.raises(ValueError, match="Unexpected analysis metadata"):
        prep.validate_wire_receipt(receipt, packet)


def test_completed_storage_ack_loss_cannot_repeat_completion_or_analysis(case, monkeypatch):
    store, packet, auth = case
    sdk = synthetic_sdk(monkeypatch, store)
    result = run(case)
    original = copy.deepcopy(store.records)
    claim = read_json(store, prep.PREFIX + "reserved.json")[0]
    payload = {
        "claim_sha256": prep.sha(prep.canonical(claim)),
        "document": store.local_records["parsed.json"],
        "receipt": store.local_records["analysis-receipt.json"],
    }
    with pytest.raises(ValueError):
        prep.complete_preparation(store, packet, auth, payload, runtime_check=lambda: None)
    with pytest.raises(ValueError):
        run(case)
    assert len(sdk.calls) == 1 and store.records == original
    assert result["status"] == "prepared_cache_verified_for_both_products"
