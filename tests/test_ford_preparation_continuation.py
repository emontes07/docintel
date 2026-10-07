"""Once-only continuation of the already charged reservation; no live operations."""

import ast
import base64
import copy
import io
import json
import os
import zlib
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock

import pytest
from azure.core.pipeline.transport import HttpTransport
from azure.identity._credentials import azure_cli

from backend import analysis_provenance as provenance
from backend.batch_store import Missing, read_json, write_json
from backend.batch_worker import BatchProcessor
from scripts import ford_analysis_preparation as prep
from tests.test_ford_analysis_preparation import (
    ClosedStore, authorize, case, fake_credential, no_live, synthetic_sdk,  # noqa: F401
)


def continuation_authorization(proposal, packet):
    return {
        "schema_version": 1, "approved": True, "approved_by": packet["approved_by"],
        "approved_at": prep.now(), "continuation_plan_sha256": prep.sha(prep.canonical(proposal)),
        "original_packet_sha256": proposal["original_packet_sha256"],
        "reservation_id": proposal["reservation_id"], "one_continuation_only": True,
        "no_new_reservation": True, "no_retry_or_refund": True, "parent_live_executor_only": True,
    }


@pytest.fixture
def stopped(case):
    store, packet, _ = case
    original = read_json(store, prep.APPROVAL_KEY)[0]
    packet.update(
        approved_by=original["approved_by"], analysis_endpoint="https://synthetic.cognitiveservices.azure.com",
        storage_program_sha256=prep.sha(prep.storage_source().encode()),
    )
    auth = authorize(packet, original["approved_by"])
    claim = prep.claim_preparation(store, packet, auth, runtime_check=lambda: None)
    write_json(store, prep.PREFIX + "packet.json", packet)
    write_json(store, prep.PREFIX + "storage-program.json", {
        "source": prep.storage_source(), "sha256": packet["storage_program_sha256"],
    })
    failure = {
        "status": "failed_or_unknown_no_retry", "allowance_refunded": False,
        "submitted_transport_errors": [], "recorded_at": prep.now(),
        "analysis_metadata": {
            "operation_id": None, "error_type": "ClientAuthenticationError",
            "source_sha256": prep.SOURCE_SHA256, "source_bytes": claim["source_bytes"],
            "request_options": prep.REQUEST_OPTIONS,
        },
    }
    proposal = {
        "schema_version": 1, "purpose": "one_continuation_of_existing_ford_reservation",
        "original_packet_sha256": prep.sha(prep.canonical(packet)),
        "original_authorization_sha256": prep.sha(prep.canonical(auth)),
        "claim_sha256": prep.sha(prep.canonical(claim)), "reservation_id": claim["reservation_id"],
        "failure": failure, "history_sha256": {
            key: prep.record_sha(raw) for key, (raw, _) in store.records.items() if key != prep.SOURCE_KEY
        },
        "continuation_program_sha256": prep.sha(prep.continuation_source().encode()),
        "new_reservations": 0, "new_reserved_microdollars": 0, "max_submissions": 1, "sdk_retries": 0,
        "native_no_send_proof": provenance.preflight_preparation(
            store.read_bytes(prep.SOURCE_KEY)[0], endpoint=packet["analysis_endpoint"],
        ),
        "local_code_sha256": prep.continuation_code_hashes(),
    }
    proposal["ci_proof"] = {
        "schema_version": 1, "status": "passed", "source_revision": "f" * 40,
        "code_sha256": proposal["local_code_sha256"],
        "checks": [{"name": "SYNTHETIC ONLY", "run_id": 1, "conclusion": "success"}],
    }
    return store, packet, auth, claim, proposal, continuation_authorization(proposal, packet)


def finish_synthetic(stopped, monkeypatch, *, submitted=None):
    store, packet, auth, claim, proposal, approval = stopped
    sdk = synthetic_sdk(monkeypatch, store)
    saved = {}
    prep.begin_continuation(store, proposal, approval, prep.continuation_source(), runtime_check=lambda: None)
    payload = prep.analyze_local(
        store.read_bytes(prep.SOURCE_KEY)[0], packet, auth, claim,
        parser_factory=lambda accepted: provenance.RecordedPreparationParser(
            endpoint=packet["analysis_endpoint"], credential=fake_credential(packet), submitted=accepted,
        ),
        submitted=submitted or (lambda value: prep.submit_continuation(
            store, proposal, approval, value, runtime_check=lambda: None,
        )),
        save_local=lambda name, value: saved.setdefault(name, copy.deepcopy(value)),
    )
    result = prep.complete_continuation(store, proposal, approval, payload, runtime_check=lambda: None)
    return sdk, saved, result, payload


def test_continuation_reuses_exact_charge_and_original_cache_contract(stopped, monkeypatch):
    store, packet, _, claim, proposal, approval = stopped
    before = copy.deepcopy(store.records)
    before_ledger = read_json(store, prep.BUDGET_KEY)[0]
    sdk, saved, result, payload = finish_synthetic(stopped, monkeypatch)
    ledger = read_json(store, prep.BUDGET_KEY)[0]
    assert len(sdk.calls) == 1
    assert ledger["attempted"] == before_ledger["attempted"]
    assert ledger["reserved"] == before_ledger["reserved"]
    assert ledger["executions"] == before_ledger["executions"]
    assert set(ledger["reservations"]) == set(before_ledger["reservations"])
    for key, reservation in before_ledger["reservations"].items():
        if key != claim["reservation_id"]:
            assert ledger["reservations"][key] == reservation
    for key, value in before.items():
        if key != prep.BUDGET_KEY:
            assert store.records[key] == value
    assert saved["analysis-receipt.json"]["reservation_id"] == claim["reservation_id"]
    assert result["continuation"]["new_reservations"] == result["continuation"]["new_reserved_microdollars"] == 0
    assert result["continuation"]["original_completed_canonical_sha256"] == prep.record_sha(
        store.read_bytes(prep.PREFIX + "completed.json")[0]
    )
    assert read_json(store, prep.PREFIX + "continuation/failure-before.json")[0] == proposal["failure"]
    batch = read_json(store, "batches/" + packet["batch_id"] + ".json")[0]
    for _, binding in prep.selected_sources(batch):
        assert BatchProcessor(store).cached(prep.cache_keys()[1], binding, prep.LOCATION)[0] is not None
    with pytest.raises(ValueError):
        prep.begin_continuation(store, proposal, approval, prep.continuation_source(), runtime_check=lambda: None)
    with pytest.raises(ValueError):
        prep.complete_continuation(store, proposal, approval, payload, runtime_check=lambda: None)
    assert len(sdk.calls) == 1


@pytest.mark.parametrize("change", [
    "new-reservation", "more-pages", "prior-reservation", "old-execution", "measured-usage",
    "failed-known-operation", "submitted", "cached", "authority", "program",
    "failed-ci", "bad-native-source", "native-sent", "extra-native-field",
])
def test_changed_stopped_state_or_authority_blocks_before_sdk(stopped, monkeypatch, change):
    store, _, _, claim, proposal, approval = stopped
    sdk = synthetic_sdk(monkeypatch, store)
    if change in ("new-reservation", "more-pages", "prior-reservation", "old-execution", "measured-usage"):
        ledger, version = read_json(store, prep.BUDGET_KEY)
        if change == "new-reservation":
            ledger["reservations"]["extra"] = {}
        elif change == "more-pages":
            ledger["reserved"]["analysis_pages"] += 1
        elif change == "prior-reservation":
            ledger["reservations"]["historical"]["status"] = "changed"
        elif change == "old-execution":
            ledger["executions"]["0"]["started_at"] = "changed"
        else:
            ledger["actual_usage"]["analysis_pages"] += 1
        write_json(store, prep.BUDGET_KEY, ledger, version)
    elif change == "failed-known-operation":
        proposal["failure"]["analysis_metadata"]["operation_id"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        approval = continuation_authorization(proposal, read_json(store, prep.PREFIX + "packet.json")[0])
    elif change == "submitted":
        write_json(store, prep.PREFIX + "submitted.json", {"operation_id": "already-submitted"})
    elif change == "cached":
        write_json(store, prep.cache_keys()[1], {})
    elif change == "authority":
        approval["reservation_id"] = "wrong"
    elif change in ("failed-ci", "bad-native-source", "native-sent", "extra-native-field"):
        if change == "failed-ci":
            proposal["ci_proof"]["status"] = "failed"
        elif change == "bad-native-source":
            proposal["native_no_send_proof"]["body_sha256"] = "0" * 64
        elif change == "native-sent":
            proposal["native_no_send_proof"]["provider_send_performed"] = True
        else:
            proposal["native_no_send_proof"]["token"] = "must-not-be-transferred"
        approval = continuation_authorization(proposal, read_json(store, prep.PREFIX + "packet.json")[0])
    else:
        proposal["continuation_program_sha256"] = "0" * 64
        approval = continuation_authorization(proposal, read_json(store, prep.PREFIX + "packet.json")[0])
    with pytest.raises(ValueError):
        prep.begin_continuation(store, proposal, approval, prep.continuation_source(), runtime_check=lambda: None)
    assert sdk.calls == []
    with pytest.raises(Missing):
        store.read_bytes(prep.PREFIX + "continuation/attempt.json")


def test_lost_continuation_start_acknowledgement_cannot_repeat(stopped, monkeypatch):
    store, _, _, _, proposal, approval = stopped
    sdk = synthetic_sdk(monkeypatch, store)
    before = store.read_bytes(prep.BUDGET_KEY)
    prep.begin_continuation(store, proposal, approval, prep.continuation_source(), runtime_check=lambda: None)
    assert store.read_bytes(prep.BUDGET_KEY) == before
    with pytest.raises(ValueError):
        prep.begin_continuation(store, proposal, approval, prep.continuation_source(), runtime_check=lambda: None)
    assert sdk.calls == []


def test_lost_submitted_ack_retains_parse_without_cache_or_retry(stopped, monkeypatch):
    store, packet, auth, claim, proposal, approval = stopped
    sdk = synthetic_sdk(monkeypatch, store)
    prep.begin_continuation(store, proposal, approval, prep.continuation_source(), runtime_check=lambda: None)
    saved, attempts = {}, []

    def submitted(value):
        attempts.append(value)
        prep.submit_continuation(store, proposal, approval, value, runtime_check=lambda: None)
        raise TimeoutError("Synthetic acknowledgement loss")

    with pytest.raises(ValueError, match="never retry"):
        prep.analyze_local(
            store.read_bytes(prep.SOURCE_KEY)[0], packet, auth, claim,
            parser_factory=lambda accepted: provenance.RecordedPreparationParser(
                endpoint=packet["analysis_endpoint"], credential=fake_credential(packet), submitted=accepted,
            ),
            submitted=submitted, save_local=lambda name, value: saved.setdefault(name, copy.deepcopy(value)),
        )
    assert len(attempts) == len(sdk.calls) == 1
    assert saved["parsed.json"] and saved["analysis-receipt.json"]
    with pytest.raises(Missing):
        store.read_bytes(prep.cache_keys()[1])


@pytest.mark.parametrize("endpoint", [
    "https://synthetic.cognitiveservices.azure.com",
    "https://synthetic.cognitiveservices.azure.com/",
])
def test_native_sdk_shared_builder_and_actual_cli_wrapper_never_send(case, monkeypatch, endpoint):
    _, packet, _ = case
    token = fake_credential(packet)._credential.get_token()
    commands, requests = [], []

    def cli(arguments, timeout):
        commands.append(arguments)
        assert "--tenant" in arguments and "--subscription" not in arguments
        return json.dumps({"accessToken": token.token, "expires_on": int(token.expires_on)})

    class NoSendObserved(Exception):
        pass

    def send(request, **kwargs):
        requests.append(request)
        raise NoSendObserved()

    monkeypatch.setattr(azure_cli, "_run_command", cli)
    monkeypatch.setattr("azure.identity.AzureCliCredential", azure_cli.AzureCliCredential)
    credential = prep.operator_credential({
        "tenant": packet["tenant_id"], "subscription": packet["subscription_id"],
    }, packet)
    transport = MagicMock(spec=HttpTransport)
    transport.send.side_effect = send
    content = b"%PDF-1.7 synthetic\n%%EOF"
    with provenance.make_preparation_client(
        endpoint=endpoint, credential=credential, transport=transport,
    ) as client:
        with pytest.raises(NoSendObserved):
            client.begin_analyze_document(**provenance.preparation_analyze_kwargs(content))
    assert len(commands) == len(requests) == 1
    request = requests[0]
    assert request.method == "POST" and request.body.getvalue() == content
    query = parse_qs(urlsplit(request.url).query)
    assert query["pages"] == ["1-5"] and query["api-version"] == ["2024-11-30"]
    assert "prebuilt-layout:analyze" in request.url
    assert urlsplit(request.url).path == "/documentintelligence/documentModels/prebuilt-layout:analyze"
    assert credential.verified_identity["principal_id"] == packet["analysis_identity"]["principal_id"]


def test_continuation_approval_does_not_allow_old_narrow_envelope(stopped):
    store, packet, original_auth, _, proposal, _ = stopped
    with pytest.raises(ValueError, match="Exact continuation"):
        prep.begin_continuation(store, proposal, original_auth, prep.continuation_source(), runtime_check=lambda: None)


def test_native_preflight_failure_blocks_real_parser_client_construction(case, monkeypatch):
    from backend.sdk_preflight import SDKPreflightError

    store, packet, _ = case
    client = MagicMock()
    monkeypatch.setattr(provenance, "make_preparation_client", client)

    def incompatible(*args, **kwargs):
        raise SDKPreflightError("document_intelligence", "SyntheticIncompatibility")

    monkeypatch.setattr(provenance, "preflight_preparation", incompatible)
    credential = fake_credential(packet)
    parser = provenance.RecordedPreparationParser(
        endpoint="https://synthetic.cognitiveservices.azure.com/", credential=credential,
        submitted=lambda event: pytest.fail("No operation may be submitted"),
    )
    with pytest.raises(SDKPreflightError):
        parser.extract_pdf_bytes(store.read_bytes(prep.SOURCE_KEY)[0], source=prep.LOCATION, page_limit=5)
    client.assert_not_called()
    assert credential.verified_identity is None and parser.analysis_receipt is None


def test_focused_ci_must_cover_actual_corrected_files(monkeypatch):
    hashes = {"synthetic.py": prep.sha(b"reviewed")}
    proof = {
        "schema_version": 1, "status": "passed", "source_revision": "a" * 40,
        "code_sha256": hashes, "checks": [{"name": "focused backend", "run_id": 123, "conclusion": "success"}],
    }
    monkeypatch.setattr(prep.subprocess, "check_output", lambda *args, **kwargs: b"reviewed")
    prep.verify_continuation_ci(proof, hashes)
    for altered in ({**proof, "status": "pending"}, {**proof, "code_sha256": {}},
                    {**proof, "checks": []}):
        with pytest.raises(ValueError):
            prep.verify_continuation_ci(altered, hashes)
    monkeypatch.setattr(prep.subprocess, "check_output", lambda *args, **kwargs: b"different")
    with pytest.raises(ValueError, match="CI revision"):
        prep.verify_continuation_ci(proof, hashes)


def test_exact_private_stopped_continuation_gate(monkeypatch):
    value = os.environ.get("DOCINTEL_TEST_FORD_CONTINUATION_WORK")
    if not value:
        pytest.skip("Actual 35-record stopped-state reproduction is opt-in")
    work = Path(value).resolve(strict=True)
    output = os.environ["DOCINTEL_TEST_FORD_CONTINUATION_OUTPUT"]
    assert Path(output).name == output
    root = Path(__file__).resolve().parents[1]
    assert not work.is_relative_to(root)
    local = work / (output.removesuffix(".json") + "-SYNTHETIC-local")
    Path(os.path.relpath(local)).mkdir(mode=0o700)
    filenames = (
        "target.json", "ford-preparation-stopped-outcome.json", "ford-preparation-stopped-verification.json",
        "ford-preparation-plan-v4.json", "ford-preparation-approved-v4.json", "ford-preparation-claim.json",
        "ford-preparation-local-failure.json", "ford-local-parse-readiness.json",
        "four-product-preparation-identity-authorization-20261006T221236Z.json",
        "four-product-existing-role-evidence-20261006.json",
    )
    original_local = {name: (work / name).read_bytes() for name in filenames}
    for name, raw in original_local.items():
        prep.release.save_once(local / name, json.loads(raw))
    snapshot, records, packet, original_auth, claim, failure = prep.continuation_inputs(local)
    assert len(records) == 35 and packet["helper_sha256"] == (
        "a58e2965326d13630c88cddf9b4e8b4bfeace30a65d0b9eee75985b6b7687e7d"
    )
    assert claim["reservation_id"] == "e832fabf2978a6d95718e5bfeb4f33d1cc0e9bb8210d0ab1cc2e67dc8f706eae"
    store = ClosedStore()
    for key, raw in records.items():
        store.write_bytes(key, raw)
    content = Path(packet["local_pdf"]["path"]).read_bytes()
    assert len(content) == 156227 and prep.sha(content) == prep.SOURCE_SHA256
    store.records[prep.SOURCE_KEY] = (content, claim["source_etag"])
    before = copy.deepcopy(store.records)
    ledger_before = read_json(store, prep.BUDGET_KEY)[0]
    assert ledger_before["attempted"]["analysis"] == 2 and ledger_before["reserved"]["analysis_pages"] == 10
    ci_revision = "f" * 40
    ci = {
        "schema_version": 1, "status": "passed", "source_revision": ci_revision,
        "code_sha256": prep.continuation_code_hashes(),
        "checks": [{"name": "SYNTHETIC focused CI ONLY", "run_id": 1, "conclusion": "success"}],
    }
    ci_path, owner_path = local / "SYNTHETIC-ci.json", local / "SYNTHETIC-owner-approval.json"
    prep.release.save_once(ci_path, ci)
    prep.release.save_once(owner_path, {"label": "SYNTHETIC ONLY; NOT ACTUAL CONTINUATION AUTHORIZATION"})
    real_git = prep.subprocess.check_output

    def git(command, **kwargs):
        if command[:2] == ["git", "show"] and command[2].startswith(ci_revision + ":"):
            return (root / command[2].partition(":")[2]).read_bytes()
        return real_git(command, **kwargs)

    monkeypatch.setattr(prep.subprocess, "check_output", git)
    config = prep.release.load_config(local / "target.json")
    native_preflight = provenance.preflight_preparation
    import azure.ai.documentintelligence

    native_client = azure.ai.documentintelligence.DocumentIntelligenceClient
    sdk = synthetic_sdk(
        monkeypatch, store, text="SYNTHETIC ONLY\n" + "\n".join(prep.sha(str(i).encode()) for i in range(120)),
    )
    no_send_proofs = []

    def no_send(source_bytes, *, endpoint):
        with monkeypatch.context() as context:
            context.setattr(azure.ai.documentintelligence, "DocumentIntelligenceClient", native_client)
            proof = native_preflight(source_bytes, endpoint=endpoint)
        no_send_proofs.append(proof)
        return proof

    monkeypatch.setattr(provenance, "preflight_preparation", no_send)
    proposal = prep.continuation_plan(local, config, ci_path=ci_path, owner_approval_path=owner_path)
    approval = continuation_authorization(proposal, packet)
    prep.release.save_once(local / "SYNTHETIC-continuation-plan.json", proposal)
    prep.release.save_once(local / "SYNTHETIC-narrow-authorization.json", approval)
    monkeypatch.setattr(prep, "verify_runtime", lambda *args: {"label": "SYNTHETIC readonly cloud checks"})
    fake_token = fake_credential(packet)._credential.get_token()
    cli_calls = []

    def cli(arguments, timeout):
        cli_calls.append(arguments)
        assert "--tenant" in arguments and "--subscription" not in arguments
        return json.dumps({"accessToken": fake_token.token, "expires_on": int(fake_token.expires_on)})

    monkeypatch.setattr(azure_cli, "_run_command", cli)
    monkeypatch.setattr("azure.identity.AzureCliCredential", azure_cli.AzureCliCredential)
    monkeypatch.setattr("backend.batch_store.configured_store", lambda: store)
    for name, val in {
        "DOCINTEL_BATCH_MODE": "hosted", "DOCINTEL_BATCH_LIVE_ENABLED": "false",
        "DOCINTEL_REAL_PILOT_ENABLED": "false", "DOCINTEL_OPTIONAL_WEB_GAPFILL_ENABLED": "false",
        "DOCINTEL_PILOT_UPLOAD_ENABLED": "false",
        "DOCINTEL_BATCH_STORAGE_URL": f"https://{config['storage']}.blob.core.windows.net",
        "DOCINTEL_BATCH_CONTAINER": config["container"],
    }.items():
        monkeypatch.setenv(name, val)
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    runtime_files = {
        name: real_git(["git", "show", f"{packet['api_source_revision']}:{name}"], cwd=root)
        for name in packet["runtime_code_sha256"]
    }

    class RemotePath:
        def __init__(self, prefix, name):
            assert prefix == "/app"
            self.name = name

        def read_bytes(self):
            return runtime_files[self.name]

    phases, frames = [], {}

    def console(arguments, code, marker, **kwargs):
        assert arguments[:3] == ["az", "containerapp", "exec"]
        packed = ast.parse(code).body[0].value.args[0].args[0].args[0].value
        program = zlib.decompress(base64.b85decode(packed)).decode()
        assert fake_token.token not in program
        tree = ast.parse(program)
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "p")
        wire = json.loads(assignment.value.args[0].value)
        phase = wire["phase"]
        phases.append(phase)
        frames[phase] = len(base64.b64encode(code.encode())) + 80
        assert frames[phase] <= 16384
        if phase == "start":
            assert not sdk.calls
        if phase == "complete":
            assert (local / "ford-preparation-continuation-local-parsed.json").is_file()
            assert (local / "ford-preparation-continuation-local-analysis-receipt.json").is_file()
        tree.body = [node for node in tree.body if not (
            isinstance(node, ast.ImportFrom) and node.module == "pathlib"
        )]
        stream = io.StringIO()
        with redirect_stdout(stream):
            exec(compile(tree, "<exact-continuation-console-with-inert-path-store>", "exec"), {
                "__name__": "synthetic_continuation_console", "Path": RemotePath,
            })
        assert marker in stream.getvalue()
        return stream.getvalue().encode()

    monkeypatch.setattr(prep.release, "console_code", console)
    result = prep.execute_continuation(local, config, proposal, approval)
    assert phases == ["start", "submitted", "complete"] and len(sdk.calls) == 1
    assert len(no_send_proofs) >= 4 and all(proof == no_send_proofs[0] for proof in no_send_proofs)
    assert cli_calls
    ledger = read_json(store, prep.BUDGET_KEY)[0]
    for field in ("attempted", "reserved", "executions", "actual_billed_microdollars"):
        assert ledger[field] == ledger_before[field]
    assert set(ledger["reservations"]) == set(ledger_before["reservations"])
    for key, record in before.items():
        if key != prep.BUDGET_KEY:
            assert store.records[key] == record
    for name, raw in original_local.items():
        assert (work / name).read_bytes() == raw
    assert prep.release.private_json(local / "ford-preparation-local-failure.json") == failure
    for key, (raw, _) in store.records.items():
        assert fake_token.token.encode() not in raw
        if key.endswith(".json"):
            json.loads(raw)
    with pytest.raises(ValueError, match="already attempted"):
        prep.execute_continuation(local, config, proposal, approval)
    assert len(sdk.calls) == 1
    for _, binding in prep.selected_sources(read_json(store, "batches/" + packet["batch_id"] + ".json")[0]):
        BatchProcessor(store).cached(prep.cache_keys()[1], binding, prep.LOCATION)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in local.iterdir())
    assert local.stat().st_mode & 0o777 == 0o700
    report = {
        "label": "SYNTHETIC CONTINUATION ONLY; NO ACTUAL APPROVAL, CI, ANALYSIS OR HOSTED CACHE",
        "status": "offline_stopped_continuation_gate_passed", "real_network_calls": 0,
        "real_credential_calls": 0, "real_analysis_calls": 0, "snapshot_records": len(records),
        "source_sha256": prep.SOURCE_SHA256, "source_bytes": len(content),
        "original_packet_sha256": proposal["original_packet_sha256"], "reservation_id": claim["reservation_id"],
        "original_api_program_reused_unchanged": True,
        "original_35_records_unchanged_except_actual_usage_update": True,
        "prior_12_reservations_and_four_executions_preserved": True,
        "new_reservations": 0, "new_reserved_microdollars": 0,
        "simulated_analysis_calls": len(sdk.calls), "storage_phases": phases,
        "console_frame_bytes": frames, "native_no_send_proof": no_send_proofs[0],
        "native_no_send_reproductions": len(no_send_proofs),
        "native_cli_factory_intercepted_calls": len(cli_calls),
        "code_sha256": prep.continuation_code_hashes(),
        "continuation_program_sha256": proposal["continuation_program_sha256"],
        "both_original_production_cached_checks_passed": True,
        "original_failure_preserved": True, "repeat_rejected": True,
        "synthetic_local_directory": str(local),
        "synthetic_completion": result["continuation"],
    }
    prep.release.save_once(work / output, report)
