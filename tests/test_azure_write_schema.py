"""Pinned official request bodies and transport gates; no live writes or credentials."""

import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import azure_write_schema as schema
from scripts import gapfill_continuation as gap
from scripts import release


EXAMPLES = [
    ("containerApps", "PATCH", "ContainerApps_Patch.json", "containerAppEnvelope"),
    ("containerApps", "PUT", "ContainerApps_CreateOrUpdate.json", "containerAppEnvelope"),
    ("jobs", "PATCH", "Job_Patch.json", "JobEnvelope"),
    ("jobs", "PUT", "Job_CreateorUpdate.json", "JobEnvelope"),
]


@pytest.fixture(autouse=True)
def offline(monkeypatch, request):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("No network"))
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: pytest.fail("No network"))
    schema._documents.cache_clear()
    schema._contract.cache_clear()
    schema.action_contract.cache_clear()
    if "installed_native" not in request.node.name:
        monkeypatch.setattr(release, "preflight_azure", lambda *a, **k: {"no_send": True})
    yield
    schema._documents.cache_clear()
    schema._contract.cache_clear()
    schema.action_contract.cache_clear()


def example(filename, member):
    candidate = schema.ASSETS / "vendor" / schema.SPEC_DIRECTORY / "examples" / filename
    return json.loads(candidate.read_text())["parameters"][member]


def url(kind):
    return ("https://management.azure.com/subscriptions/subscription/resourceGroups/group/"
            f"providers/Microsoft.App/{kind}/resource?api-version=2024-03-01")


@pytest.mark.parametrize("kind,method,filename,member", EXAMPLES)
def test_all_four_official_wire_examples_validate_unchanged(kind, method, filename, member):
    payload = example(filename, member)
    original = copy.deepcopy(payload)
    schema.validate_request(method, url(kind), payload)
    result = schema.build_payload(kind, method, **payload)
    assert result == payload == original and result is not payload


@pytest.mark.parametrize("method", ["PATCH", "PUT"])
def test_container_apps_wire_location_is_required_even_for_patch(method):
    with pytest.raises(schema.WriteSchemaError, match="required"):
        schema.build_payload("containerApps", method, properties={})
    schema.build_payload("containerApps", method, location="westus", properties={})


def test_patch_uses_job_patch_schema_not_get_or_put_model():
    schema.build_payload("jobs", "PUT", location="westus", properties={"workloadProfileName": "Consumption"})
    with pytest.raises(schema.WriteSchemaError, match="additionalProperties"):
        schema.build_payload("jobs", "PATCH", properties={"workloadProfileName": "Consumption"})
    with pytest.raises(schema.WriteSchemaError, match="additionalProperties"):
        schema.build_payload("jobs", "PATCH", location="westus")


@pytest.mark.parametrize("kind,method,filename,member", EXAMPLES)
@pytest.mark.parametrize("mutation", [
    "root", "properties", "nested", "readonly", "readonly_identity", "secret", "typed_map",
])
def test_unknown_and_readonly_nested_properties_fail_safely(kind, method, filename, member, mutation):
    payload = example(filename, member)
    private = "SYNTHETIC-INSTANCE-KEY-TOKEN"
    if mutation == "root":
        payload[private] = private
    elif mutation == "properties":
        payload["properties"][private] = private
    elif mutation == "nested":
        payload["properties"]["template"] = {"containers": [{"name": "worker", "resources": {private: private}}]}
    elif mutation == "readonly":
        payload["properties"]["template"] = {
            "containers": [{"name": "worker", "resources": {"ephemeralStorage": private}}],
        }
    elif mutation == "readonly_identity":
        payload["identity"] = {"type": "SystemAssigned", "principalId": private}
    elif mutation == "secret":
        payload["properties"].setdefault("configuration", {}).setdefault("secrets", []).append(
            {"name": "approved-reference", "value": private, private: private},
        )
    elif mutation == "typed_map":
        payload["tags"] = {private: {"value": private}}
    with pytest.raises(schema.WriteSchemaError) as error:
        schema.validate_request(method, url(kind), payload)
    assert private not in str(error.value) and private not in repr(vars(error.value))


@pytest.mark.parametrize("field,value", [("identitySettings", []), ("dapr", None)])
def test_original_worker_failure_is_rejected_in_raw_write_payload(field, value):
    payload = {"properties": {"configuration": {"replicaTimeout": 600, "triggerType": "Manual", field: value}}}
    with pytest.raises(schema.WriteSchemaError, match="additionalProperties"):
        schema.validate_request("PATCH", url("jobs"), payload)


@pytest.mark.parametrize("field,value,reason", [
    ("replicaTimeout", True, "type"), ("replicaTimeout", "600", "type"),
    ("replicaTimeout", 2 ** 31, "format"), ("triggerType", "UNSUPPORTED", "enum"),
])
def test_declared_types_formats_and_enums_are_enforced(field, value, reason):
    configuration = {"replicaTimeout": 600, "triggerType": "Manual", field: value}
    with pytest.raises(schema.WriteSchemaError, match=reason):
        schema.build_payload("jobs", properties={"configuration": configuration})


@pytest.mark.parametrize("value", [2 ** 31, "8080", True])
def test_nested_probe_port_type_and_format_are_derived(value):
    containers = [{"name": "worker", "probes": [{"httpGet": {"port": value}, "type": "Readiness"}]}]
    with pytest.raises(schema.WriteSchemaError):
        schema.build_payload("jobs", properties={"template": {"containers": containers}})


@pytest.mark.parametrize("kind,method,filename,member", EXAMPLES)
def test_central_azure_validates_file_and_inline_bodies_before_transport(
    tmp_path, monkeypatch, kind, method, filename, member,
):
    payload = example(filename, member)
    body = tmp_path / "body.json"
    body.write_text(json.dumps(payload))
    calls = []
    monkeypatch.setattr(release, "command", lambda arguments, **kwargs: calls.append(arguments) or b'{"ok":true}')
    assert release.azure("rest", "--method", method, "--url", url(kind), "--body", "@" + str(body)) == {"ok": True}
    assert release.azure("rest", "-m", method.lower(), "-u", url(kind), "-b", json.dumps(payload)) == {"ok": True}
    payload["SYNTHETIC-KEY"] = "SYNTHETIC-TOKEN"
    body.write_text(json.dumps(payload))
    with pytest.raises(schema.WriteSchemaError):
        release.azure("rest", "--method=" + method, "--url=" + url(kind), "--body=@" + str(body))
    assert len(calls) == 2


@pytest.mark.parametrize("target", [
    "https://storage.blob.core.windows.net/container/blob?sig=SYNTHETIC-SECRET",
    "https://management.azure.com/subscriptions/sub/resourceGroups/group/providers/Microsoft.Other/jobs/job?api-version=2024-03-01",
    url("jobs").replace("2024-03-01", "2025-01-01"),
    url("jobs") + "&api-version=2025-01-01",
    url("jobs").replace("/resource?", "/resource/start?"),
    url("jobs").replace("management.azure.com", "management.azure.com.invalid"),
])
@pytest.mark.parametrize("method", ["PATCH", "PUT"])
def test_unknown_targets_and_blob_put_never_get_an_azure_rest_exemption(monkeypatch, target, method):
    monkeypatch.setattr(release, "command", lambda *a, **k: pytest.fail("No transport"))
    with pytest.raises(schema.WriteSchemaError) as error:
        release.azure("rest", "--method", method, "--url", target, "--body", "{}")
    assert "SYNTHETIC" not in str(error.value)


@pytest.mark.parametrize("body", ['{"properties":{},"properties":{}}', '{"secret":"SYNTHETIC"', '{"value":NaN}'])
def test_duplicate_malformed_and_nonfinite_bodies_never_reach_transport(monkeypatch, body):
    monkeypatch.setattr(release, "command", lambda *a, **k: pytest.fail("No transport"))
    with pytest.raises(schema.WriteSchemaError) as error:
        release.azure("rest", "--method", "PATCH", "--url", url("jobs"), "--body", body)
    assert "SYNTHETIC" not in str(error.value)


@pytest.mark.parametrize("arguments", [
    ("rest", "--method", "GET", "--method", "PATCH"),
    ("rest", "--meth", "PATCH"),
    ("rest", "-mPATCH"),
    ("rest", "--method=PUT", "--url-parameters=api-version=2025-01-01"),
])
def test_ambiguous_or_unvalidated_write_overrides_are_rejected(monkeypatch, arguments):
    monkeypatch.setattr(release, "command", lambda *a, **k: pytest.fail("No transport"))
    with pytest.raises(schema.WriteSchemaError):
        release.azure(*arguments)


@pytest.mark.parametrize("arguments", [
    ("containerapp", "update"), ("containerapp", "create"),
    ("containerapp", "job", "update"), ("containerapp", "job", "create"),
    ("containerapp", "ingress", "disable"), ("containerapp", "secret", "set"),
])
def test_implicit_native_cli_writes_cannot_bypass_the_rest_contract(monkeypatch, arguments):
    monkeypatch.setattr(release, "command", lambda *a, **k: pytest.fail("No transport"))
    with pytest.raises(schema.WriteSchemaError, match="operation"):
        release.azure(*arguments)


def test_unsupported_null_ingress_is_rejected_not_silently_omitted():
    with pytest.raises(schema.WriteSchemaError, match="type"):
        release.build_update_payload(
            {"location": "westus"}, [{"name": "api", "image": "image"}],
            configuration={"ingress": None},
        )


@pytest.mark.parametrize("damage", ["schema", "manifest", "missing"])
def test_pinned_source_integrity_is_a_release_blocker(tmp_path, monkeypatch, damage):
    copied = tmp_path / "schemas"
    shutil.copytree(schema.ASSETS, copied)
    target = copied / ("provenance.json" if damage == "manifest" else "vendor/" + schema.SPEC_DIRECTORY + "Jobs.json")
    if damage == "missing":
        target.unlink()
    else:
        target.write_bytes(target.read_bytes() + b"\n")
    monkeypatch.setattr(schema, "ASSETS", copied)
    with pytest.raises(schema.WriteSchemaError, match="integrity"):
        schema.build_payload("jobs", properties={})


def test_unknown_schema_constructs_and_external_refs_fail_closed():
    with pytest.raises(schema.WriteSchemaError, match="schema"):
        schema._compile({"type": "object", "oneOf": [{"type": "object"}]})
    with pytest.raises(schema.WriteSchemaError, match="schema"):
        schema._dereference({"$ref": "https://untrusted.invalid/schema.json#/definitions/Body"}, "source.json")


@pytest.mark.parametrize("unimplemented", [
    {"oneOf": [{"type": "object"}]},
    {"readOnly": True, "oneOf": [{"type": "object"}]},
    {"readOnly": True, "properties": {"child": {"x-unimplemented-validation": True}}},
    {"properties": {"child": {"additionalProperties": {"const": "SYNTHETIC-PRIVATE"}}}},
    {"allOf": [{"properties": {"child": {"x-ms-unknown-extension": True}}}]},
])
def test_unsupported_vocabulary_fails_closed_at_every_schema_depth(unimplemented):
    with pytest.raises(schema.WriteSchemaError, match="schema") as error:
        schema._compile(unimplemented)
    assert "SYNTHETIC" not in str(error.value)


def test_ci_regeneration_reproduces_all_four_contracts_from_pinned_operations():
    actual = schema.check_generated()
    assert actual == schema.generate_contracts()
    assert actual["source_commit"] == schema.SPEC_COMMIT
    assert actual["source_provenance_sha256"] == schema.PROVENANCE_SHA256
    assert sum(len(methods) for methods in actual["contracts"].values()) == 4
    for kind, method in schema.OPERATIONS:
        contract = actual["contracts"][kind][method]
        assert contract["operation_id"] == schema.OPERATIONS[kind, method][1]
        assert contract["schema"]["$schema"] == "http://json-schema.org/draft-04/schema#"
        assert contract["schema"] == schema.request_schema(kind, method)
        identity = contract["schema"]["properties"]["identity"]["properties"]
        assert identity["principalId"] == {"readOnly": True, "not": {}}
        assert identity["userAssignedIdentities"]["type"] == ["object", "null"]


@pytest.mark.parametrize("kind,method", list(schema.OPERATIONS))
def test_explicit_azure_nullable_identity_map_is_supported(kind, method):
    body = {"identity": {"type": "SystemAssigned", "userAssignedIdentities": None}}
    if kind == "containerApps" or method == "PUT":
        body["location"] = "westus"
    schema.validate_payload(kind, method, body)


@pytest.mark.parametrize("damage", ["missing", "weakened_schema", "source_pin", "duplicate_key"])
def test_generated_contract_drift_blocks_runtime_validation(tmp_path, monkeypatch, damage):
    copied = tmp_path / "schemas"
    shutil.copytree(schema.ASSETS, copied)
    candidate = copied / schema.GENERATED_FILE
    value = json.loads(candidate.read_text())
    if damage == "missing":
        candidate.unlink()
    elif damage == "duplicate_key":
        raw = candidate.read_text()
        candidate.write_text('{"schema_version":99,' + raw[1:])
    else:
        if damage == "weakened_schema":
            value["contracts"]["jobs"]["PATCH"]["schema"]["additionalProperties"] = True
        else:
            value["source_commit"] = "0" * 40
        candidate.write_text(json.dumps(value))
    monkeypatch.setattr(schema, "ASSETS", copied)
    with pytest.raises(schema.WriteSchemaError, match="integrity"):
        schema.validate_payload("jobs", "PATCH", {"properties": {}})


def test_generated_contract_comparison_is_not_json_key_order_sensitive(tmp_path, monkeypatch):
    copied = tmp_path / "schemas"
    shutil.copytree(schema.ASSETS, copied)
    candidate = copied / schema.GENERATED_FILE
    def reorder(value):
        if isinstance(value, dict):
            return {key: reorder(child) for key, child in reversed(list(value.items()))}
        if isinstance(value, list):
            return [reorder(child) for child in value]
        return value
    candidate.write_text(json.dumps(reorder(json.loads(candidate.read_text())), indent=1))
    monkeypatch.setattr(schema, "ASSETS", copied)
    schema.check_generated()


def test_stdlib_only_ci_regeneration_command_has_no_dependency_or_network_setup():
    result = subprocess.run(
        [sys.executable, "-S", "-m", "scripts.azure_write_schema", "--check-generated"],
        cwd=release.ROOT, capture_output=True, check=False, timeout=30,
    )
    assert result.returncode == 0 and b"Verified four generated write contracts" in result.stdout
    assert not result.stderr


@pytest.mark.parametrize("job", [False, True])
def test_schema_derived_builders_preserve_supported_values_and_remove_get_metadata(job):
    observed = {"location": "westus"}
    containers = [{
        "name": "worker", "image": "registry.invalid/backend@sha256:" + "a" * 64,
        "imageType": "ContainerImage", "resources": {"cpu": 2, "memory": "4Gi", "ephemeralStorage": "8Gi"},
        "env": [{"name": "SETTING", "value": "keep", "secretRef": None}],
    }]
    original = copy.deepcopy(containers)
    result = release.build_update_payload(observed, containers, job=job)
    schema.validate_request("PATCH", url("jobs" if job else "containerApps"), result)
    container = result["properties"]["template"]["containers"][0]
    assert container["resources"] == {"cpu": 2, "memory": "4Gi"}
    assert container["env"] == [{"name": "SETTING", "value": "keep"}]
    assert "imageType" not in container and containers == original
    assert ("location" in result) is not job


@pytest.mark.parametrize("resource", [None, {}, {"location": None}, {"location": 123}, {"location": ""}])
def test_api_builder_requires_observed_location_and_does_not_invent_one(resource):
    with pytest.raises(ValueError, match="observed resource location"):
        release.build_update_payload(resource, [{"name": "api", "image": "image"}])


@pytest.mark.parametrize("value", [False, [], "SYNTHETIC-SECRET"])
def test_get_ingress_adapter_rejects_wrong_types_without_disclosing_values(value):
    with pytest.raises(ValueError, match="Ingress must be an object") as error:
        release.writable_ingress(value)
    assert "SYNTHETIC" not in str(error.value)


def test_get_null_normalization_does_not_mutate_retained_input():
    containers = [{"name": "api", "image": "image", "env": [{"name": "SETTING", "value": "retained", "secretRef": None}]}]
    assert release.writable_containers(containers)[0]["env"] == [{"name": "SETTING", "value": "retained"}]
    body = release.build_update_payload({"location": "westus"}, containers)
    assert body["properties"]["template"]["containers"][0]["env"] == [{"name": "SETTING", "value": "retained"}]
    assert containers[0]["env"][0]["secretRef"] is None


@pytest.mark.parametrize("job", [False, True])
def test_existing_release_api_and_worker_paths_submit_only_valid_payloads(tmp_path, monkeypatch, job):
    kind = "jobs" if job else "containerApps"
    config = {"subscription": "subscription", "group": "group", "backend": "resource", "job": "resource",
              "registry": "synthetic", "backend_image": "synthetic.azurecr.io/docintel/backend@sha256:" + "a" * 64}
    resource = {"id": url(kind).split("management.azure.com")[1].split("?")[0], "location": "westus",
                "properties": {"configuration": {
                    "triggerType": "Manual", "replicaTimeout": 600, "replicaRetryLimit": 0,
                    "identitySettings": [], "dapr": None,
                }, "template": {"containers": [{
                    "name": "worker", "image": "old", "imageType": "ContainerImage",
                    "resources": {"cpu": 2, "memory": "4Gi", "ephemeralStorage": "8Gi"},
                    "env": [{"name": "DOCINTEL_BATCH_LIVE_ENABLED", "value": "false"}],
                }]}}}
    if not job:
        resource["properties"]["configuration"] = {
            "activeRevisionsMode": "Single", "dapr": None, "identitySettings": [],
            "secrets": [{"name": "existing-reference"}],
            "ingress": {"external": False, "targetPort": 8080, "transport": "auto",
                        "fqdn": "synthetic.invalid", "targetPortHttpScheme": None},
        }
    writes = []
    def command(arguments, **kwargs):
        if arguments[1] != "rest":
            return json.dumps([] if "execution" in arguments else resource).encode()
        target = arguments[arguments.index("--url") + 1]
        body = json.loads(Path(arguments[arguments.index("--body") + 1][1:]).read_text())
        schema.validate_request("PATCH", target, body)
        writes.append(body)
        return b"{}"
    monkeypatch.setattr(release, "command", command)
    monkeypatch.setattr(release, "private_path", lambda path: path)
    if job:
        release.save_once(tmp_path / "worker-baseline.json", {
            "target": release.fingerprint(config), "resource": resource,
        })
        release.update_worker_image(config, tmp_path)
    else:
        release.patch_app(config, "backend", resource["properties"]["template"]["containers"], tmp_path)
    assert len(writes) == 1
    assert "configuration" not in writes[0]["properties"]
    assert "ephemeralStorage" not in writes[0]["properties"]["template"]["containers"][0]["resources"]
    assert ("location" in writes[0]) is not job


def test_gap_secure_transport_validates_before_key_token_or_curl(tmp_path, monkeypatch):
    config = {"subscription": "subscription", "group": "group", "job": "resource"}
    resource = {"id": url("jobs").split("management.azure.com")[1].split("?")[0]}
    payload = {"properties": {"configuration": {"triggerType": "Manual", "replicaTimeout": 600, "identitySettings": []}}}
    monkeypatch.setattr(gap, "existing_webiq_key", lambda: pytest.fail("No key read"))
    monkeypatch.setattr(release, "azure", lambda *a, **k: pytest.fail("No token request"))
    monkeypatch.setattr(gap.subprocess, "run", lambda *a, **k: pytest.fail("No curl"))
    with pytest.raises(schema.WriteSchemaError, match="additionalProperties"):
        gap.secure_worker_patch(tmp_path, config, {}, resource, payload)


def test_blob_publication_remains_a_separate_specific_binary_put_contract(tmp_path, monkeypatch):
    archive = tmp_path / "context.tar.gz"
    archive.write_bytes(b"SYNTHETIC-ARCHIVE")
    upload = {"relativePath": "source/archive.tar.gz",
              "uploadUrl": "https://storage.blob.core.windows.net/build/source/archive.tar.gz?sig=SYNTHETIC-SAS"}
    calls = []
    def transport(arguments, **kwargs):
        calls.append(arguments)
        assert "SYNTHETIC-SAS" not in repr(arguments)
        assert b"SYNTHETIC-SAS" in kwargs["input"]
        packet = json.loads(kwargs["input"])
        assert packet["method"] == "PUT" and packet["headers"]["x-ms-blob-type"] == "BlockBlob"
        assert packet["archive"] == str(archive)
        return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps({
            "status": "validated", "no_send": arguments[-1] == "--native-http-no-send", "http_status": 201,
        }).encode(), stderr=b"")
    monkeypatch.setattr(release.subprocess, "run", transport)
    assert release.upload_publication_context(upload, archive, 30) == upload["relativePath"]
    invalid = {**upload, "uploadUrl": "https://management.azure.com/unapproved?sig=SYNTHETIC-SAS"}
    with pytest.raises(release.PublicationUploadError):
        release.upload_publication_context(invalid, archive, 30)
    assert len(calls) == 2


def test_stdlib_only_direct_script_cli_keeps_preinstall_staging_importable():
    result = subprocess.run([sys.executable, "-S", str(release.ROOT / "scripts/release.py"), "--help"],
                            capture_output=True, check=False, timeout=20)
    assert result.returncode == 0 and not result.stderr


ACR_URL = ("https://management.azure.com/subscriptions/00000000-0000-4000-8000-000000000000/"
           "resourceGroups/group/providers/Microsoft.ContainerRegistry/registries/registry")
JOB_ID = ("/subscriptions/00000000-0000-4000-8000-000000000000/resourceGroups/group/"
          "providers/Microsoft.App/jobs/worker")


@pytest.mark.parametrize("field,value", [
    ("type", "FileTaskRunRequest"), ("agentConfiguration", {"cpu": 4}), ("timeout", 901),
    ("unexpected", True), ("platform", {"os": "not-linux"}),
])
def test_acr_selected_official_variant_rejects_wrong_type_budget_or_unknown_member(field, value):
    payload = release.publication_request("backend", "a" * 40, "source")
    payload[field] = value
    with pytest.raises(schema.WriteSchemaError):
        schema.validate_request("POST", ACR_URL + "/scheduleRun?api-version=2019-04-01", payload)


@pytest.mark.parametrize("suffix,payload", [
    ("/listBuildSourceUploadUrl?api-version=2019-04-01", {}),
    ("/unknown?api-version=2019-04-01", None),
    ("/scheduleRun?api-version=2025-01-01", {}),
])
def test_no_unmodeled_post_or_bodyless_operation_bypass(suffix, payload):
    with pytest.raises(schema.WriteSchemaError):
        schema.validate_request("POST", ACR_URL + suffix, payload)


def test_installed_native_cli_start_uses_captured_get_and_validates_original_template(tmp_path):
    if not shutil.which("az"):
        pytest.skip("Release-host installed Azure CLI required")
    template = {"containers": [{"name": "worker", "image": "registry.invalid/image@sha256:" + "a" * 64,
                                 "resources": {"cpu": 1, "memory": "2Gi"}}]}
    job = {"id": JOB_ID, "name": "worker", "properties": {
        "configuration": {"triggerType": "Manual"}, "template": copy.deepcopy(template),
    }}
    candidate = tmp_path / "execution.json"
    candidate.write_text(json.dumps(template))
    args = ("containerapp", "job", "start", "--subscription", "00000000-0000-4000-8000-000000000000",
            "-g", "group", "-n", "worker", "--yaml", str(candidate))
    result = release.preflight_azure(args, resource_snapshot=job)
    assert result["no_send"] and result["network_requests_sent"] == 0
    assert result["request"]["method"] == "POST"
    assert result["provider_response_fabricated"] is True
    assert result["fixture_response_counts"] == {"get": 1, "write": 1}
    assert result["authentication_performed"] is False and result["provider_send_performed"] is False
    template["containers"][0]["resources"]["cpu"] = "1"
    candidate.write_text(json.dumps(template))
    with pytest.raises(schema.WriteSchemaError):
        release.preflight_azure(args, resource_snapshot=job)
    assert job["properties"]["template"]["containers"][0]["resources"]["cpu"] == 1


def test_installed_native_cli_parser_rejects_unknown_option_before_live_command(monkeypatch):
    if not shutil.which("az"):
        pytest.skip("Release-host installed Azure CLI required")
    monkeypatch.setattr(release, "command", lambda *a, **k: pytest.fail("No live command"))
    with pytest.raises(ValueError):
        release.azure("rest", "--method", "POST", "--url",
                      ACR_URL + "/listBuildSourceUploadUrl?api-version=2019-04-01",
                      "--headers", "valid=value", "unexpected-positional")


def test_installed_native_cli_stop_is_canonical_and_unknown_legacy_route_rejected():
    if not shutil.which("az"):
        pytest.skip("Release-host installed Azure CLI required")
    args = ("containerapp", "job", "stop", "--subscription", "00000000-0000-4000-8000-000000000000",
            "-g", "group", "-n", "worker", "--job-execution-name", "execution")
    canonical = release.canonical_job_stop(args)
    assert "/jobs/worker/executions/execution/stop?" in canonical[-1]
    assert release.preflight_azure(args)["no_send"]
    with pytest.raises(schema.WriteSchemaError):
        schema.validate_request("POST", "https://management.azure.com" + JOB_ID
                                + "/stop/execution?api-version=2025-01-01", None)


@pytest.mark.parametrize("contract", ["binary_blob", "job_secret"])
def test_installed_native_http_request_prepares_without_network_or_owner_keys(tmp_path, monkeypatch, contract):
    if not shutil.which("az"):
        pytest.skip("Release-host native requests runtime required")
    monkeypatch.setenv("WEBIQ_API_KEY", "MUST-NOT-BE-READ")
    if contract == "binary_blob":
        archive = tmp_path / "archive.tar.gz"
        archive.write_bytes(b"synthetic")
        packet = {"contract": contract, "method": "PUT", "timeout": 30,
                  "url": "https://storage.blob.core.windows.net/container/blob?sig=NO-SEND",
                  "archive": str(archive),
                  "headers": {"x-ms-version": "2024-08-04", "x-ms-blob-type": "BlockBlob",
                              "Content-Type": "application/octet-stream", "Content-Length": archive.stat().st_size}}
    else:
        packet = {"contract": contract, "method": "PATCH", "timeout": 30,
                  "url": "https://management.azure.com" + JOB_ID + "?api-version=2024-03-01",
                  "payload": {"properties": {"configuration": {"triggerType": "Manual", "replicaTimeout": 600,
                                                               "secrets": [{"name": "existing-reference"}]}}},
                  "headers": {"Content-Type": "application/json"}}
    result = release.native_http(packet)
    assert result["no_send"] is True and result["status"] == "validated"
    expected_body = (archive.read_bytes() if contract == "binary_blob"
                     else json.dumps(packet["payload"], separators=(",", ":")).encode())
    assert result["body_sha256"] == hashlib.sha256(expected_body).hexdigest()
    assert result["body_bytes"] == len(expected_body)
    assert result["request_sha256"] == hashlib.sha256(
        (packet["method"] + "\n" + packet["url"] + "\n").encode() + expected_body,
    ).hexdigest()
    assert result["validation_status"] == "validated_no_send"
    assert result["provider_response_fabricated"] is False
    assert result["fixture_response_counts"] == {"get": 0, "write": 0}
    assert result["network_calls"] == result["credential_real_calls"] == result["transport_real_calls"] == 0
    assert "MUST-NOT-BE-READ" not in json.dumps(result)
    packet["headers"]["Content-Length"] = 0 if contract == "binary_blob" else "invalid"
    with pytest.raises(ValueError):
        release.native_http(packet)


def test_native_preflight_precedes_existing_window_check_and_send(monkeypatch):
    events = []
    monkeypatch.setattr(release, "preflight_azure", lambda *a, **k: events.append("no-send"))
    monkeypatch.setattr(release, "command", lambda *a, **k: events.append("send") or b"{}")
    def window():
        events.append("window")
        raise ValueError("Full window expired")
    with pytest.raises(ValueError, match="window"):
        release.azure("rest", "--method", "POST", "--url",
                      ACR_URL + "/listBuildSourceUploadUrl?api-version=2019-04-01", before_send=window)
    assert events == ["no-send", "window"]


def test_installed_native_deployment_readiness_uses_only_supplied_shapes(tmp_path, monkeypatch):
    if not shutil.which("az"):
        pytest.skip("Release-host installed Azure CLI required")
    monkeypatch.setattr(release, "azure", lambda *a, **k: pytest.fail("No Azure calls"))
    monkeypatch.setattr(gap, "existing_webiq_key", lambda: pytest.fail("No key reads"))
    template = {"containers": [{"name": "worker", "image": "registry.invalid/retained"}]}
    worker = {"id": JOB_ID, "name": "worker", "properties": {
        "configuration": {"triggerType": "Manual"}, "template": copy.deepcopy(template),
    }}
    backend = {"id": JOB_ID.replace("/jobs/worker", "/containerApps/api"), "location": "westus",
               "properties": {"template": copy.deepcopy(template)}}
    before = copy.deepcopy((worker, backend, template))
    config = {"subscription": "00000000-0000-4000-8000-000000000000", "group": "group",
              "job": "worker", "registry": "registry"}
    result = release.preflight_deployment(config, backend, worker, template)
    assert result["no_send"] and len(result["requests"]) == 4
    assert "actual_binary_archive" in result["pending_dynamic_boundary"]
    assert (worker, backend, template) == before
    assert not list(release.ROOT.glob(".native-write-preflight-*"))
    assert not list(release.ROOT.glob(".native-deployment-template-*"))


@pytest.mark.parametrize("arguments", [
    ("deployment", "group", "create"), ("storage", "blob", "upload-batch"),
    ("acr", "build"), ("rest", "--method", "DELETE", "--url", "https://management.azure.com"),
])
def test_unmodeled_bootstrap_writes_never_bypass_native_contract(monkeypatch, arguments):
    monkeypatch.setattr(release, "command", lambda *a, **k: pytest.fail("No live command"))
    with pytest.raises(schema.WriteSchemaError):
        release.azure(*arguments)


@pytest.mark.parametrize("arguments", [
    ("cognitiveservices", "account", "deployment", "show"),
    ("cognitiveservices", "account", "show"), ("role", "assignment", "list"),
    ("ad", "signed-in-user", "show"),
])
def test_existing_capacity_and_preparation_reads_are_preserved(monkeypatch, arguments):
    calls = []
    monkeypatch.setattr(release, "command", lambda *a, **k: calls.append(a) or b"{}")
    assert release.azure(*arguments) == {}
    assert len(calls) == 1


def test_native_construction_failure_stops_before_owner_key_or_token(monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("Native client cannot construct request")
    monkeypatch.setattr(release, "native_http", fail)
    monkeypatch.setattr(release, "azure", lambda *a, **k: pytest.fail("No token request"))
    monkeypatch.setattr(gap, "existing_webiq_key", lambda: pytest.fail("No key read"))
    config = {"subscription": "00000000-0000-4000-8000-000000000000", "group": "group", "job": "worker"}
    payload = {"properties": {"configuration": {"triggerType": "Manual", "replicaTimeout": 600,
                                               "secrets": [{"name": "existing"}]}}}
    with pytest.raises(ValueError, match="Native client"):
        gap.send_existing_secret_patch(config, {"id": JOB_ID}, payload,
                                       secret_ref="existing", check_window=lambda: None)


def test_execution_override_projection_retains_job_only_settings_in_original_snapshot():
    template = {"containers": [{"name": "worker", "image": "retained",
                                 "resources": {"cpu": 1, "memory": "2Gi", "ephemeralStorage": "4Gi"}}],
                "initContainers": None, "volumes": [{"name": "retained", "storageType": "EmptyDir"}]}
    original = copy.deepcopy(template)
    result = release.writable_execution_template(template)
    assert template == original
    assert "volumes" not in result and "initContainers" not in result
    assert "ephemeralStorage" not in result["containers"][0]["resources"]
    schema.validate_action_payload("jobStart", result)
    template["unmodeled"] = None
    with pytest.raises(schema.WriteSchemaError, match="additionalProperties"):
        release.writable_execution_template(template)


@pytest.mark.parametrize("body", [None, b'{"b":2, "a":1}', '{"b":2, "a":1}', io.BytesIO(b"binary-body")])
def test_wire_fingerprints_hash_exact_bytes_and_restore_stream_position(body):
    request = SimpleNamespace(method="POST", url="https://management.azure.com/path?api-version=2024-03-01",
                              body=body, headers={"Content-Type": "application/json"})
    expected = body.getvalue() if isinstance(body, io.BytesIO) else body
    expected = expected.encode() if isinstance(expected, str) else expected or b""
    receipt = schema._wire_fingerprints(request)
    assert receipt["body_bytes"] == len(expected)
    assert receipt["body_sha256"] == hashlib.sha256(expected).hexdigest()
    assert receipt["request_sha256"] == hashlib.sha256(
        (request.method + "\n" + request.url + "\n").encode() + expected,
    ).hexdigest()
    assert receipt["api_version"] == "2024-03-01"
    assert request.url not in json.dumps(receipt)
    if isinstance(body, io.BytesIO):
        assert body.tell() == 0 and body.read() == expected


@pytest.mark.parametrize("fail_receipt", [False, True])
def test_azure_receipt_hook_is_isolated_and_precedes_clock_and_send(monkeypatch, fail_receipt):
    events = []
    proof = {"status": "validated", "no_send": True, "request": {"body_sha256": "synthetic-hash"}}
    original = copy.deepcopy(proof)
    monkeypatch.setattr(release, "preflight_azure", lambda *a, **k: events.append("preflight") or proof)
    monkeypatch.setattr(release, "command", lambda *a, **k: events.append("send") or b'{"actual_result":true}')
    def record(value):
        events.append("receipt")
        assert value["operation_stage"] == "azure_write"
        value["request"]["body_sha256"] = "caller-copy"
        if fail_receipt:
            raise OSError("SYNTHETIC-PRIVATE-RECEIPT-PATH")
    def send():
        return release.azure(
            "rest", "--method", "POST", "--url", ACR_URL + "/listBuildSourceUploadUrl?api-version=2019-04-01",
            on_preflight=record, before_send=lambda: events.append("window"),
        )
    if fail_receipt:
        with pytest.raises(ValueError, match="receipt persistence failed") as error:
            send()
        assert "SYNTHETIC" not in str(error.value)
        assert events == ["preflight", "receipt"]
    else:
        assert send() == {"actual_result": True}
        assert events == ["preflight", "receipt", "window", "send"]
    assert proof == original


@pytest.mark.parametrize("fail_stage", [None, "worker_secret_redacted", "worker_secret_credential_bound"])
def test_secret_receipt_hook_never_receives_keys_and_failed_receipt_blocks_send(monkeypatch, fail_stage):
    events = []
    retained = []
    key, token = "SYNTHETIC-OWNER-KEY", "SYNTHETIC-ACCESS-TOKEN"
    config = {"subscription": "00000000-0000-4000-8000-000000000000", "group": "group",
              "job": "worker", "tenant": "tenant"}
    payload = {"properties": {"configuration": {"triggerType": "Manual", "replicaTimeout": 600,
                                               "secrets": [{"name": "existing"}]}}}
    original = copy.deepcopy(payload)
    def native(packet, *, send=False):
        if send:
            events.append("send")
            return {"http_status": 200}
        return {"status": "validated", "no_send": True, "body_sha256": release.fingerprint(packet["payload"])}
    def record(receipt):
        stage = receipt["operation_stage"]
        events.append(stage)
        assert key not in json.dumps(receipt) and token not in json.dumps(receipt)
        if stage == fail_stage:
            raise OSError("SYNTHETIC-PRIVATE-ERROR")
        retained.append(copy.deepcopy(receipt))
    monkeypatch.setattr(release, "native_http", native)
    monkeypatch.setattr(gap, "existing_webiq_key", lambda: events.append("key") or key)
    monkeypatch.setattr(release, "azure", lambda *a, **k: events.append("token") or {"accessToken": token})
    def send():
        gap.send_existing_secret_patch(
            config, {"id": JOB_ID}, payload, secret_ref="existing",
            check_window=lambda: events.append("window"), on_preflight=record,
        )
    if fail_stage is not None:
        with pytest.raises(ValueError) as error:
            send()
        assert "SYNTHETIC" not in str(error.value) and "send" not in events
        if fail_stage == "worker_secret_redacted":
            assert events == [fail_stage]
    else:
        send()
        assert events == ["worker_secret_redacted", "window", "key", "token",
                          "worker_secret_credential_bound", "window", "send"]
        assert len(retained) == 2 and retained[0]["body_sha256"] != retained[1]["body_sha256"]
    assert payload == original
