"""Pinned official request bodies and transport gates; no live writes or credentials."""

import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys

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
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("No network"))
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: pytest.fail("No network"))
    schema._documents.cache_clear()
    schema._contract.cache_clear()
    yield
    schema._documents.cache_clear()
    schema._contract.cache_clear()


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
        assert arguments[arguments.index("--request") + 1] == "PUT"
        assert arguments[arguments.index("--header") + 1] == "x-ms-blob-type: BlockBlob"
        assert arguments[arguments.index("--upload-file") + 1] == str(archive)
        return subprocess.CompletedProcess(arguments, 0, stdout=b"201", stderr=b"")
    monkeypatch.setattr(release.subprocess, "run", transport)
    assert release.upload_publication_context(upload, archive, 30) == upload["relativePath"]
    invalid = {**upload, "uploadUrl": "https://management.azure.com/unapproved?sig=SYNTHETIC-SAS"}
    with pytest.raises(release.PublicationUploadError):
        release.upload_publication_context(invalid, archive, 30)
    assert len(calls) == 1


def test_stdlib_only_direct_script_cli_keeps_preinstall_staging_importable():
    result = subprocess.run([sys.executable, "-S", str(release.ROOT / "scripts/release.py"), "--help"],
                            capture_output=True, check=False, timeout=20)
    assert result.returncode == 0 and not result.stderr
