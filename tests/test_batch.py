import json

import pytest

from backend.batch import BatchService
from backend.batch_store import Conflict, Missing, SQLiteStore, read_json, write_json
from backend.workbooks import read_workbook, write_workbook


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("Batch tests must not contact external services")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture
def service(tmp_path):
    store = SQLiteStore(tmp_path / "private")
    write_json(store, "configuration/sources.json", {"sources": [{"reference": "synthetic.pdf", "source_id": "synthetic", "kind": "sharepoint", "products": [{"item_id": "001", "vendor": "Synthetic", "mpn": "PART-1", "hierarchy_node": "Valve"}, {"item_id": "002", "vendor": "Synthetic", "mpn": "PART-2", "hierarchy_node": "Valve"}]}]})
    return BatchService(store)


def workbooks():
    manifest = write_workbook({"Products": [["PIMITEM Number", "Vendor Name", "MPN", "Hierarchy Node", "PDF Tech Spec", "Attributes to Fill", "Original"], ["001", "Synthetic", "PART-1", "Valve", "synthetic.pdf", "definitions.xlsx", "=untrusted"], ["002", "Synthetic", "PART-2", "Valve", "synthetic.pdf", "definitions.xlsx", "00100"]]})
    attributes = write_workbook({"Attributes": [["node", "potential_attribute_name", "potential_attribute_data_type", "unit", "potential_attribute_example_values"], ["Valve", "Pressure Rating", "Numeric", "PSI", "999999 reference-looking example"]]})
    return manifest, attributes


def test_explicit_synthetic_worker_does_not_scan_other_batches(service, monkeypatch):
    from backend.batch_worker import main
    monkeypatch.setattr("backend.batch_worker.configured_store", lambda: service.store)
    monkeypatch.setattr("sys.argv", ["worker", "--synthetic-acceptance", "--item-limit", "2"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    manifest, attributes = workbooks()
    selected = service.intake(manifest, attributes, "definitions.xlsx", "selected")
    other = service.intake(manifest, attributes, "definitions.xlsx", "other")
    service.submit(selected["id"], "selected", "11111111-1111-4111-8111-111111111111", "evidence_only", False)
    service.submit(other["id"], "other", "22222222-2222-4222-8222-222222222222", "evidence_only", False)
    monkeypatch.setattr("sys.argv", ["worker", "--synthetic-acceptance", "--item-limit", "1", "--batch-id", selected["id"]])
    assert main() == 0
    assert service.get(selected["id"], "selected")["state"] == "queued"
    assert service.get(other["id"], "other")["state"] == "queued"
    assert main() == 0
    assert service.get(selected["id"], "selected")["state"] == "completed"
    assert service.get(other["id"], "other")["state"] == "queued"


def test_multi_product_intake_is_explicit_and_examples_never_become_definitions(service):
    manifest, attributes = workbooks()
    result = service.intake(manifest, attributes, "definitions.xlsx", "actor")
    assert result["valid"] and result["product_count"] == 2
    assert result["items"][0]["manifest"]["product"]["item_id"] == "001"
    assert result["items"][0]["manifest"]["attributes"][0]["examples"] == []
    assert result["items"][0]["warnings"]
    assert service.intake(manifest, attributes, "definitions.xlsx", "actor")["id"] == result["id"]
    exported = read_workbook(service.export(result["id"], "actor"))
    assert exported["Inputs"][0]["Original"] == "=untrusted"
    with pytest.raises(Missing):
        service.get(result["id"], "another-actor")


def intake_contract(definition_rows, *, definition_headers=None, selection="definitions.xlsx", node="Valve", sources=None, registry=None):
    from backend.batch import validate_batch
    definition_headers = definition_headers or ["node", "potential_attribute_name", "potential_attribute_data_type"]
    manifest = write_workbook({"Products": [
        ["PIMITEM Number", "Vendor Name", "MPN", "Hierarchy Node", "Attributes to Fill", *(sources or {})],
        ["001", "Synthetic", "PART-1", node, selection, *(sources or {}).values()],
    ]})
    attributes = write_workbook({"Definitions": [definition_headers, *definition_rows]})
    return validate_batch(manifest, attributes, "definitions.xlsx", registry or [])


def test_intake_legacy_batch_inputs_and_blocked_source_registry_remain_valid():
    from backend.batch import validate_batch
    registry = [{
        "reference": "synthetic.pdf", "source_id": "synthetic", "kind": "sharepoint",
        "products": [
            {"item_id": "001", "vendor": "Synthetic", "mpn": "PART-1", "hierarchy_node": "Valve"},
            {"item_id": "002", "vendor": "Synthetic", "mpn": "PART-2", "hierarchy_node": "Valve"},
        ],
    }]
    result = validate_batch(*workbooks(), "definitions.xlsx", registry)
    assert result["valid"] and result["product_count"] == 2
    for item in result["items"]:
        assert item["errors"] == []
        assert item["manifest"]["attributes"][0]["unit"] == "PSI"
        assert item["manifest"]["attributes"][0]["examples"] == []
        assert item["manifest"]["source_ids"] == ["synthetic"]
        assert any("SharePoint download blocked" in warning for warning in item["warnings"])


def test_intake_hosted_api_synthetic_fixture_remains_valid(tmp_path):
    from backend.batch import validate_batch
    from scripts.release_fixture import prepare
    fixture = tmp_path / "synthetic-intake"
    prepare(fixture)
    registry = json.loads((fixture / "seed/configuration/sources.json").read_text())["sources"]
    result = validate_batch(
        (fixture / "manifest.xlsx").read_bytes(),
        (fixture / "definitions.xlsx").read_bytes(),
        "definitions.xlsx", registry,
    )
    assert result["valid"] and result["product_count"] == 2
    assert all(item["errors"] == [] for item in result["items"])
    assert all(item["manifest"]["attributes"][0]["unit_resolved"] is True for item in result["items"])


def test_intake_missing_sources_are_valid_and_units_remain_qualified_unresolved():
    result = intake_contract([["Valve", "Pressure", "Numeric"]])
    assert result["valid"]
    item = result["items"][0]
    assert item["manifest"]["source_ids"] == [] and item["sources"] == []
    definition = item["manifest"]["attributes"][0]
    assert definition["value_type"] == "number" and definition["unit"] is None
    assert definition["unit_resolved"] is False
    assert any("Definition clarification needed" in warning for warning in item["warnings"])
    assert any("No approved evidence" in warning for warning in item["warnings"])


@pytest.mark.parametrize("unit", ["", "PSI"])
def test_intake_explicit_numeric_units_are_not_inferred(unit):
    result = intake_contract([["Valve", "Pressure", "Numeric", unit]], definition_headers=["node", "potential_attribute_name", "potential_attribute_data_type", "unit"])
    definition = result["items"][0]["manifest"]["attributes"][0]
    assert result["valid"] and definition["unit_resolved"] is True
    assert definition["unit"] == (unit or None)


def test_intake_type_guidance_examples_and_source_basis_are_not_evidence():
    rows = [
        ["Valve", "Connections", "Enumerated", "EXAMPLE ONLY", "Definition research, not product evidence"],
        ["Valve", "Standards", "Multi-Select", "DO NOT COPY", "Generic standard context"],
    ]
    result = intake_contract(rows, definition_headers=["node", "potential_attribute_name", "potential_attribute_data_type", "potential_attribute_example_values", "source_basis"])
    assert result["valid"]
    item = result["items"][0]
    definitions = item["manifest"]["attributes"]
    assert [definition["value_type"] for definition in definitions] == ["string", "string"]
    assert [definition["type_guidance"] for definition in definitions] == ["Enumerated", "Multi-Select"]
    assert definitions[0]["definition_context"] == rows[0][4]
    assert all(definition["allowed_values"] == [] and definition["examples"] == [] for definition in definitions)
    assert item["sources"] == [] and item["manifest"]["source_ids"] == []
    assert result["original_definitions"][0]["potential_attribute_example_values"] == "EXAMPLE ONLY"


def test_intake_inherits_only_explicit_parent_chain_and_closest_definition_wins():
    rows = [
        ["Family", "Connection", "String", "", "Family definition"],
        ["Family", "Material", "Enumerated", "", "Family material"],
        ["Valve", "Connection", "String", "Family", "Child definition"],
        ["Other", "Not requested", "String", "", "Unrelated"],
    ]
    result = intake_contract(rows, definition_headers=["node", "potential_attribute_name", "potential_attribute_data_type", "parent_node", "description"])
    assert result["valid"]
    definitions = result["items"][0]["manifest"]["attributes"]
    assert [definition["attribute_id"] for definition in definitions] == ["Connection", "Material"]
    assert definitions[0]["definition_node"] == "Valve" and definitions[0]["description"] == "Child definition"
    assert definitions[1]["definition_node"] == "Family"
    no_parent = intake_contract([["Family", "Material", "String"]], node="Family.Child", selection="Material")
    assert not no_parent["valid"]


@pytest.mark.parametrize("rows", [
    [["Valve", "Material", "String", "Family"], ["Family", "Other", "String", "Valve"]],
    [["Valve", "Material", "String", "Family"], ["Valve", "Other", "String", "Different"]],
])
def test_intake_cyclic_or_conflicting_parent_links_are_errors(rows):
    result = intake_contract(rows, definition_headers=["node", "potential_attribute_name", "potential_attribute_data_type", "parent_node"])
    assert not result["valid"] and result["items"][0]["manifest"] is None


def test_intake_all_evidence_columns_require_exact_approved_product_and_tier():
    product = {"item_id": "001", "vendor": "Synthetic", "mpn": "PART-1", "hierarchy_node": "Valve"}
    registry = [
        {"reference": "spec.pdf", "source_id": "pdf", "kind": "sharepoint", "products": [product]},
        {"reference": "vendor.xlsx", "source_id": "table", "kind": "blob", "format": "xlsx", "source_tier": "vendor_table", "blob": "documents/vendor.xlsx", "sha256": "a" * 64, "products": [product], "table": {"sheet": "Rows", "mpn_column": "MPN", "expected_vendor": "Synthetic"}},
        *[
            {"reference": reference, "source_id": source_id, "kind": "web", "format": "web", "source_tier": tier, "url": f"https://{source_id}.invalid/approved", "products": [product]}
            for reference, source_id, tier in [("maker", "maker", "manufacturer_web"), ("distributor", "dist", "approved_web"), ("other", "other", "approved_web")]
        ],
    ]
    sources = {"PDF Tech Spec": "spec.pdf", "Vendor Tabular Data": "vendor.xlsx", "Vendor Website": "maker", "Distributors": "distributor", "Other Web Sources": "other"}
    result = intake_contract([["Valve", "Material", "String"]], sources=sources, registry=registry)
    assert result["valid"]
    assert result["items"][0]["manifest"]["source_ids"] == ["pdf", "table", "maker", "dist", "other"]
    assert any("URL alone is not evidence" in warning for warning in result["items"][0]["warnings"])
    for column in sources:
        invalid = intake_contract([["Valve", "Material", "String"]], sources={column: "not approved"}, registry=registry)
        assert not invalid["valid"]
    mismatch = intake_contract([["Valve", "Material", "String"]], sources={"PDF Tech Spec": "vendor.xlsx"}, registry=registry)
    assert not mismatch["valid"]


def test_intake_source_binding_preserves_legacy_blocked_and_explicit_applicability_contracts():
    from backend.batch import SourceBinding
    product = {"item_id": "001", "vendor": "Synthetic", "mpn": "PART-1", "hierarchy_node": "Valve"}
    legacy = SourceBinding(reference="blocked.pdf", source_id="blocked", kind="sharepoint", products=[product])
    assert legacy.format == "pdf" and legacy.source_tier == "internal_pdf"
    assert legacy.enabled is False and legacy.applicability == []
    scope = {"product": product, "identity_terms": ["PART-1"], "attribute_ids": [], "qualification": "Operator approved family scope only"}
    enabled = SourceBinding(**{**legacy.model_dump(), "enabled": True, "drive_id": "drive", "item_id": "document", "tenant_id": "tenant", "sha256": "a" * 64, "url": "https://synthetic.sharepoint.com/sites/catalog/spec.pdf", "applicability": [scope]})
    assert enabled.applicability[0].attribute_ids == []
    for changes in [
        {"enabled": True},
        {"sha256": "a" * 64},
        {"applicability": [{**scope, "qualification": " "}]},
        {"applicability": [{**scope, "identity_terms": [" "]}]},
        {"applicability": [{**scope, "product": {**product, "mpn": "OTHER"}}]},
        {"table": {"sheet": "Rows", "mpn_column": "MPN", "expected_vendor": "Synthetic"}},
    ]:
        with pytest.raises(ValueError):
            SourceBinding(**{**legacy.model_dump(), **changes})


def test_intake_enabled_sharepoint_requires_hash_and_canonical_url_before_retrieval():
    from backend.batch import SourceBinding
    product = {"item_id": "001", "vendor": "Synthetic", "mpn": "PART-1", "hierarchy_node": "Valve"}
    legacy = SourceBinding(reference="spec.pdf", source_id="spec", kind="sharepoint", products=[product])
    assert legacy.enabled is False and legacy.sha256 is None and legacy.blob is None
    approved = {
        **legacy.model_dump(), "enabled": True, "drive_id": "drive", "item_id": "document",
        "tenant_id": "tenant", "sha256": "a" * 64,
        "url": "https://synthetic.sharepoint.com/sites/catalog/spec.pdf",
    }
    assert SourceBinding(**approved).enabled is True
    for change in [
        {"sha256": None}, {"sha256": "a" * 63}, {"url": None},
        {"url": "http://synthetic.sharepoint.com/spec.pdf"},
        {"url": "https://synthetic.sharepoint.com/spec.pdf?download=1"},
        {"url": "https://other.invalid/spec.pdf"},
        {"drive_id": None}, {"item_id": None}, {"tenant_id": None},
    ]:
        with pytest.raises(ValueError):
            SourceBinding(**{**approved, **change})


def test_intake_enabled_sharepoint_uses_url_and_retrieval_compatible_source_id():
    from backend.batch import SourceBinding
    product = {"item_id": "001", "vendor": "Synthetic", "mpn": "PART-1", "hierarchy_node": "Valve"}
    legacy = SourceBinding(reference="file.pdf", source_id="Legacy.PDF_1", kind="sharepoint", products=[product])
    approved = {
        **legacy.model_dump(), "source_id": "vendor-spec-1", "enabled": True,
        "drive_id": "drive", "item_id": "document", "tenant_id": "tenant",
        "sha256": "a" * 64, "url": "https://synthetic.sharepoint.com/sites/catalog/file.pdf",
    }
    binding = SourceBinding(**approved)
    assert binding.reference == "file.pdf" and binding.url == approved["url"]
    assert legacy.source_id == "Legacy.PDF_1" and not legacy.enabled
    for source_id in ["Legacy.PDF_1", "UPPER", "vendor_spec"]:
        with pytest.raises(ValueError, match="source ID"):
            SourceBinding(**{**approved, "source_id": source_id})
    assert SourceBinding(**{
        **legacy.model_dump(), "kind": "blob", "blob": "documents/file.pdf", "sha256": "a" * 64,
    }).source_id == "Legacy.PDF_1"


def test_intake_source_binding_owner_requires_canonical_hosted_identity():
    from backend.batch import SourceBinding
    source = {
        "reference": "synthetic.pdf", "source_id": "synthetic", "kind": "sharepoint",
        "products": [{"item_id": "001", "vendor": "Synthetic", "mpn": "PART-1", "hierarchy_node": "Valve"}],
    }
    owner = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    assert SourceBinding(**source).owner is None
    assert SourceBinding(**source, owner=None).owner is None
    assert SourceBinding(**source, owner=owner).model_dump()["owner"] == owner
    for invalid in [
        "", "development:local-unverified", "tenant/object", owner.upper(),
        owner.replace("-", ""), owner.split("/")[0], owner + "/extra",
        " " + owner, owner + " ", owner.replace("/", "/{") + "}",
    ]:
        with pytest.raises(ValueError):
            SourceBinding(**source, owner=invalid)


@pytest.mark.parametrize("url", ["http://example.invalid", "https://user:password@example.invalid", "https://example.invalid/#fragment", "file:///private.xlsx", "https://example.invalid/a b"])
def test_intake_web_registration_rejects_nonapproved_url_shapes(url):
    from backend.batch import SourceBinding
    with pytest.raises(ValueError):
        SourceBinding(reference="web", source_id="web", kind="web", format="web", source_tier="manufacturer_web", url=url, products=[{"item_id": "001", "vendor": "Synthetic", "mpn": "PART-1", "hierarchy_node": "Valve"}])


def test_submission_requires_consent_and_is_idempotent(service):
    result = service.intake(*workbooks(), "definitions.xlsx", "actor")
    request_id = "11111111-1111-4111-8111-111111111111"
    with pytest.raises(ValueError):
        service.submit(result["id"], "actor", request_id, "live_inference", False)
    queued = service.submit(result["id"], "actor", request_id, "evidence_only", False)
    assert service.submit(result["id"], "actor", request_id, "evidence_only", False) == queued
    with pytest.raises(Conflict):
        service.submit(result["id"], "actor", request_id, "live_inference", True)


def test_store_conditional_updates_and_leases(service):
    store = service.store
    version = write_json(store, "test", {"value": 1})
    write_json(store, "test", {"value": 2}, version)
    with pytest.raises(Conflict):
        write_json(store, "test", {"value": 3}, version)
    with store.lease("batch") as renew:
        renew()
        with pytest.raises(Conflict):
            with store.lease("batch"):
                pass
    assert read_json(store, "test")[0] == {"value": 2}


def test_lost_response_replays_do_not_duplicate_submission_or_review(service):
    from backend.batch_worker import run_batch

    record = service.intake(*workbooks(), "definitions.xlsx", "actor")
    request_id = "11111111-1111-4111-8111-111111111111"
    service.submit(record["id"], "actor", request_id, "evidence_only", False)
    submitted = service.store.read_bytes(f"batches/{record['id']}.json")
    service.submit(record["id"], "actor", request_id, "evidence_only", False)
    assert service.store.read_bytes(f"batches/{record['id']}.json") == submitted
    assert len(service.store.keys("batches/")) == 1
    run_batch(service.store, record["id"])
    machine = service.store.read_bytes(f"results/{record['id']}/row-2.json")
    decision = {"attribute_id": "Pressure Rating", "decision": "reject", "reason": "Synthetic lost-response regression only"}
    service.review(record["id"], "row-2", "actor", decision)
    review_key = f"reviews/{record['id']}/row-2.json"
    reviewed = service.store.read_bytes(review_key)
    with pytest.raises(ValueError):
        service.review(record["id"], "row-2", "actor", decision)
    assert service.store.read_bytes(review_key) == reviewed
    assert len(read_json(service.store, review_key)[0]) == 1
    service.submit(record["id"], "actor", request_id, "evidence_only", False)
    run_batch(service.store, record["id"], processor=lambda *_: pytest.fail("Completed items must not run again"))
    assert service.store.read_bytes(f"results/{record['id']}/row-2.json") == machine


def test_finite_worker_preserves_blocked_sources_and_resumes_without_repeating(service):
    from backend.batch_worker import run_batch
    record = service.intake(*workbooks(), "definitions.xlsx", "actor")
    service.submit(record["id"], "actor", "11111111-1111-4111-8111-111111111111", "evidence_only", False)
    run_batch(service.store, record["id"], item_limit=1)
    assert service.get(record["id"], "actor")["state"] == "queued"
    first = service.detail(record["id"], "row-2", "actor")
    assert first["state"] == "unresolved"
    assert first["provenance"][0]["retained_stages"] == [200, 302, 401]
    assert first["machine_result"]["model_call_status"] == "not_attempted"
    run_batch(service.store, record["id"])
    assert service.detail(record["id"], "row-2", "actor") == first
    assert service.get(record["id"], "actor")["state"] == "completed"
    assert len(read_workbook(service.export(record["id"], "actor"))["Results"]) == 2
    assert service.items(record["id"], "actor", limit=1)["total"] == 2
    assert len(service.items(record["id"], "actor", limit=1)["items"]) == 1
    assert service.items(record["id"], "actor", view="pending")["total"] == 2


def test_blob_read_passes_installed_sdk_validation_without_network(monkeypatch):
    from unittest.mock import Mock
    from azure.core.pipeline.transport import RequestsTransport
    from azure.storage.blob import BlobClient
    from backend.batch_store import BlobStore

    class TransportReached(RuntimeError):
        pass

    requests = []
    def blocked_transport(self, request, **kwargs):
        requests.append(request)
        raise TransportReached("SDK validation passed; network blocked")

    monkeypatch.setattr(RequestsTransport, "send", blocked_transport)
    client = BlobClient("https://synthetic.invalid", "synthetic", "record")
    with pytest.raises(ValueError, match="Offset value must not be None"):
        client.download_blob(length=5)
    assert not requests
    store = object.__new__(BlobStore)
    store.container = Mock()
    store.container.get_blob_client.return_value = client
    with pytest.raises(TransportReached):
        store.read_bytes("record", max_bytes=4)
    assert len(requests) == 1
    assert requests[0].headers["x-ms-range"] == "bytes=0-4"


@pytest.mark.parametrize("content", [b"", b"a", b"1234", b"12345"])
def test_blob_read_preserves_bound_and_size_detection(content):
    from unittest.mock import Mock
    from backend.batch_store import BlobStore
    store = object.__new__(BlobStore)
    store.container = Mock()
    blob = store.container.get_blob_client.return_value
    response = blob.download_blob.return_value
    response.readall.return_value = content
    response.properties.etag = "synthetic-etag"
    if len(content) > 4:
        with pytest.raises(ValueError, match="exceeds read limit"):
            store.read_bytes("record", max_bytes=4)
    else:
        assert store.read_bytes("record", max_bytes=4) == (content, "synthetic-etag")
    blob.download_blob.assert_called_once_with(offset=0, length=5)


def test_blob_missing_read_still_maps_to_missing():
    from unittest.mock import Mock
    from azure.core.exceptions import ResourceNotFoundError
    from backend.batch_store import BlobStore
    store = object.__new__(BlobStore)
    store.container = Mock()
    store.container.get_blob_client.return_value.download_blob.side_effect = ResourceNotFoundError("synthetic missing blob")
    with pytest.raises(Missing):
        store.read_bytes("missing", max_bytes=4)
    store.container.get_blob_client.return_value.download_blob.assert_called_once_with(offset=0, length=5)


def test_blob_conditional_write_uses_blob_client_response():
    from unittest.mock import Mock
    from backend.batch_store import BlobStore
    store = object.__new__(BlobStore)
    store.container = Mock()
    store.container.get_blob_client.return_value.upload_blob.return_value = {"etag": "new-version"}
    assert store.write_bytes("test", b"value", "old-version") == "new-version"
    store.container.upload_blob.assert_not_called()
    assert store.container.get_blob_client.return_value.upload_blob.call_args.kwargs["etag"] == "old-version"


def test_live_budget_is_explicit_and_durable(service):
    from backend.batch_worker import BatchProcessor
    from datetime import datetime, timedelta, timezone
    processor = BatchProcessor(service.store)
    with pytest.raises(Missing):
        processor.reserve("inference")
    write_json(service.store, "configuration/live-approval.json", {"id": "11111111-1111-4111-8111-111111111111", "approved_by": "synthetic-operator", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "analysis_limit": 0, "inference_limit": 1})
    processor.reserve("inference")
    with pytest.raises(ValueError, match="exhausted"):
        BatchProcessor(service.store).reserve("inference")
    with pytest.raises(ValueError, match="exhausted"):
        processor.reserve("analysis")


def test_conflicts_qualifications_reviews_and_identity_survive_export(service):
    from backend.batch_worker import run_batch
    from backend.extract import run_enrichment
    from backend.models.enrichment import OfflineBundle, ReviewAnnotation
    from datetime import datetime, timezone
    record = service.intake(*workbooks(), "definitions.xlsx", "tenant/verified-object")
    service.submit(record["id"], "tenant/verified-object", "11111111-1111-4111-8111-111111111111", "evidence_only", False)

    def synthetic_processor(item, mode):
        payload = {"manifest": item["manifest"], "sources": [{"source_id": "synthetic", "product": item["manifest"]["product"], "document": {"source": "synthetic.pdf", "cache_key": "fixture-v1", "parsed_at": "2026-01-01T00:00:00Z", "paragraphs": [{"text": "Working pressure 100 PSI; test pressure 150 PSI.", "page_number": 1}]}}], "generated_response": {"candidates": [{"attribute_id": "Pressure Rating", "value": value, "unit": "PSI", "evidence_ids": ["synthetic:fixture-v1:paragraph=0"]} for value in [100, 150]]}}
        result = run_enrichment(OfflineBundle.model_validate(payload))
        result.attributes[0].review_annotations.append(ReviewAnnotation(candidate_index=0, text="Working pressure only, not maximum pressure.", author="Synthetic qualification", annotated_at=datetime.now(timezone.utc)))
        return result, {"state": "unresolved", "error": "Conflicting candidates", "provenance": []}

    run_batch(service.store, record["id"], processor=synthetic_processor)
    original = service.detail(record["id"], "row-2", "tenant/verified-object")["machine_sha256"]
    service.review(record["id"], "row-2", "tenant/verified-object", {"attribute_id": "Pressure Rating", "decision": "approve", "candidate_index": 0, "reason": "Synthetic review only", "reviewer": "spoofed"})
    assert service.detail(record["id"], "row-2", "tenant/verified-object")["machine_sha256"] == original
    exported = read_workbook(service.export(record["id"], "tenant/verified-object"))
    assert len(exported["Results"]) == 4
    assert len(exported["Evidence"]) == 2
    assert exported["Results"][0]["Qualifications"].startswith("Working pressure")
    assert exported["Reviews"][0]["Reviewer"] == "tenant/verified-object"
    assert exported["Reviews"][0]["Selected candidate index"] == "0"
    assert service.items(record["id"], "tenant/verified-object", view="pending")["total"] == 1


def test_compatible_parse_is_reused_and_content_changes_fail_closed(service, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from unittest.mock import Mock
    from backend.batch import digest
    from backend.batch_worker import BatchProcessor
    from backend.core.docintel import ParsedDocument
    from backend.models.enrichment import ProductKey

    content = b"%PDF-synthetic-fixture-only"
    source = {"source_id": "synthetic", "kind": "blob", "reference": "synthetic.pdf", "blob": "documents/synthetic.pdf", "sha256": digest(content)}
    version = service.store.write_bytes(source["blob"], content)
    product = ProductKey(item_id="001", vendor="Synthetic", mpn="PART-1", hierarchy_node="Valve")
    document = ParsedDocument(source="batchblob:///documents/synthetic.pdf", cache_key="sha256:" + digest(content), parsed_at=datetime.now(timezone.utc), paragraphs=[])
    parser = Mock()
    parser.return_value.extract_pdf_bytes.return_value = document
    monkeypatch.setattr("backend.batch_worker.DocumentIntelligenceService", parser)
    write_json(service.store, "configuration/live-approval.json", {"id": "11111111-1111-4111-8111-111111111111", "approved_by": "synthetic-operator", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "analysis_limit": 1})
    assert BatchProcessor(service.store).source(source, product, True)[1]["parsing"] == "fresh_analysis"
    assert BatchProcessor(service.store).source(source, product, False)[1]["parsing"] == "cache"
    parser.return_value.extract_pdf_bytes.assert_called_once()
    service.store.write_bytes(source["blob"], b"%PDF-changed-version", version)
    result, provenance = BatchProcessor(service.store).source(source, product, True)
    assert result.document is None and provenance["retrieval"] == "failed"
    parser.return_value.extract_pdf_bytes.assert_called_once()


def test_worker_concurrency_and_second_lease_cannot_duplicate(service):
    import threading
    from backend.batch_worker import BatchProcessor, run_batch

    record = service.intake(*workbooks(), "definitions.xlsx", "actor")
    service.submit(record["id"], "actor", "11111111-1111-4111-8111-111111111111", "evidence_only", False)
    barrier = threading.Barrier(2)
    calls = []
    mutex = threading.Lock()

    def processor(item, mode):
        with mutex:
            calls.append(item["item_key"])
        barrier.wait(timeout=10)
        with pytest.raises(Conflict):
            with service.store.lease(record["id"]):
                pass
        return BatchProcessor(service.store)(item, mode)

    run_batch(service.store, record["id"], concurrency=2, processor=processor)
    assert sorted(calls) == ["row-2", "row-3"]
    run_batch(service.store, record["id"], processor=processor)
    assert len(calls) == 2


def test_interrupted_reservation_never_repeats_model_work(service):
    from backend.batch_worker import run_batch
    record = service.intake(*workbooks(), "definitions.xlsx", "actor")
    service.submit(record["id"], "actor", "11111111-1111-4111-8111-111111111111", "evidence_only", False)
    write_json(service.store, f"items/{record['id']}/row-2.json", {"state": "running"})
    run_batch(service.store, record["id"])
    assert service.detail(record["id"], "row-2", "actor")["state"] == "interrupted"