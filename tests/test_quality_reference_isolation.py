"""Scoring references cannot reach extraction, refinement, or judge requests."""

import base64
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import json
import socket
from types import SimpleNamespace
from zipfile import ZipFile

from PIL import Image, PngImagePlugin
import pytest

from backend.batch import digest
from backend.batch_store import Conflict, Missing, read_json, write_json
from backend.core.docintel import ParsedDocument, ParsedParagraph
from backend.core.quality_model import ResponsesCompletion
from backend.models.enrichment import AttributeDefinition, Manifest, ProductKey
from backend.pilot import PARSER_VERSION
from backend import quality_scoring
from backend.quality_pipeline import QualityJudgment
from backend.quality_worker import FORD_PDF_SHA256, run_quality_batch
from backend.workbooks import write_workbook


SENTINEL = "SCORING_REFERENCE_ONLY_7fba206c_NEVER_MODEL_CONTEXT"
NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


def png(metadata=""):
    output = BytesIO()
    info = PngImagePlugin.PngInfo()
    if metadata:
        info.add_text("scoring-only", metadata)
    with Image.new("RGB", (2, 2), "white") as image:
        image.save(output, format="PNG", pnginfo=info)
    return output.getvalue()


def assert_no_reference(value):
    """Check nested JSON plus decoded image/workbook bytes, not just visible text."""
    if isinstance(value, dict):
        for key, entry in value.items():
            assert_no_reference(key)
            assert_no_reference(entry)
    elif isinstance(value, (list, tuple)):
        for entry in value:
            assert_no_reference(entry)
    elif isinstance(value, bytes):
        assert SENTINEL.encode() not in value, "Scoring reference leaked into model input bytes"
        if value.startswith(b"PK"):
            with ZipFile(BytesIO(value)) as archive:
                for name in archive.namelist():
                    assert_no_reference(archive.read(name))
        elif value.startswith(b"\x89PNG\r\n\x1a\n"):
            with Image.open(BytesIO(value)) as image:
                assert_no_reference(image.info)
    elif isinstance(value, str):
        assert SENTINEL not in value, "Scoring reference leaked into model input text"
        if value.startswith("data:") and ";base64," in value:
            assert_no_reference(base64.b64decode(value.split(";base64,", 1)[1], validate=True))
        elif value.startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except ValueError:
                pass
            else:
                assert_no_reference(parsed)
        else:
            try:
                decoded = base64.b64decode(value, validate=True)
            except ValueError:
                pass
            else:
                assert_no_reference(decoded)


class MemoryStore:
    def __init__(self):
        self.data = {}
        self.reads = []
        self.revision = 0

    def read_bytes(self, key, max_bytes=64 * 1024 * 1024):
        self.reads.append(key)
        if key not in self.data:
            raise Missing(key)
        data, revision = self.data[key]
        assert len(data) <= max_bytes
        return data, revision

    def write_bytes(self, key, value, version=None):
        if (key in self.data and self.data[key][1] != version) or (key not in self.data and version is not None):
            raise Conflict("Conditional write failed")
        self.revision += 1
        self.data[key] = (bytes(value), str(self.revision))
        return str(self.revision)

    def keys(self, prefix):
        return sorted(key for key in self.data if key.startswith(prefix))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("Isolation tests must not use sockets, DNS, or real credentials")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


def seed_reference_and_evidence():
    reference_workbook = write_workbook({"Draft": [
        ["item_id", "vendor", "attribute_name", "expected_data_type", "proposed_value", "unit",
         "source_tier", "source_locator", "system_confidence", "notes", "human_validation_status"],
        ["P1", "Synthetic", "Primary Material", "Enumerated", SENTINEL, "",
         SENTINEL, SENTINEL, SENTINEL, SENTINEL, SENTINEL],
    ]})
    normalized = quality_scoring.normalize_cowork_reference(reference_workbook)
    assert normalized["rows"][0]["candidates"][0]["value"] == SENTINEL
    scoring_metadata = {
        "normalized_reference": normalized,
        "workbook_base64": base64.b64encode(reference_workbook).decode(),
        "reference_image": "data:image/png;base64," + base64.b64encode(png(SENTINEL)).decode(),
    }
    current = Manifest(
        product=ProductKey(item_id="P1", vendor="Synthetic", mpn="MODEL-A", hierarchy_node="Valves"),
        attributes=[
            AttributeDefinition(attribute_id="Primary Material", description="Body material", value_type="string"),
            AttributeDefinition(attribute_id="Valve Type", description="Valve type", value_type="string"),
        ],
    )
    binding = {
        "source_id": "catalog", "kind": "blob", "format": "pdf", "source_tier": "internal_pdf",
        "blob": "documents/catalog.pdf", "sha256": FORD_PDF_SHA256, "products": [current.product.model_dump()],
    }
    item = {
        "item_key": "row-2", "row": 2, "manifest": current.model_dump(mode="json"), "sources": [binding],
        "original": {"PIMITEM Number": "P1", "MPN": "MODEL-A", "Vendor Name": "Synthetic"},
        "errors": [], "warnings": [], "scoring_reference": scoring_metadata,
        "scoring_reference_blob": "scoring/reference.xlsx",
    }
    record = {
        "id": "isolation", "owner": "owner", "items": [item], "state": "completed",
        "original_definitions": [], "input_hashes": {}, "attribute_reference": "attributes.xlsx",
        "scoring_reference": scoring_metadata, "scoring_reference_blob": "scoring/reference.xlsx",
    }
    store = MemoryStore()
    store.write_bytes("scoring/reference.xlsx", reference_workbook)
    write_json(store, "scoring/reference.json", normalized)
    store.write_bytes("scoring/reference.png", png(SENTINEL))
    clean_image = png()
    store.write_bytes("documents/catalog.png", clean_image)
    document = ParsedDocument(
        source="batchblob:///documents/catalog.pdf", cache_key="sha256:" + FORD_PDF_SHA256, parsed_at=NOW,
        paragraphs=[ParsedParagraph(text="Body: BRASS. Angle valve.", page_number=1)],
    )
    cache_key = "parses/" + digest((document.source + FORD_PDF_SHA256 + PARSER_VERSION).encode()) + ".json"
    write_json(store, cache_key, {
        "parser_version": PARSER_VERSION, "document": document.model_dump(mode="json"),
        "document_sha256": digest(document.model_dump_json().encode()),
    })
    write_json(store, "batches/isolation.json", record)
    return store, normalized, clean_image


def test_reference_is_absent_from_real_worker_model_requests_and_serialized_images(monkeypatch):
    store, reference, clean_image = seed_reference_and_evidence()
    captured = []
    phases = []

    def no_reference_loading(*args, **kwargs):
        pytest.fail("The live worker must never load or compare a scoring reference")

    class CompletionCapture:
        effort = "medium"
        deployment = "offline-model"

        def complete_structured(self, system, user, schema, **options):
            body = {"system": system, "user": user, "schema": schema.model_json_schema(), "options": options}
            assert_no_reference(body)
            assert_no_reference(json.dumps(body))
            captured.append(body)
            self.last_usage = {
                "model": self.deployment, "input_tokens": 100, "cached_input_tokens": 0,
                "output_tokens": 10, "reasoning_tokens": 0, "cost_usd": 0,
            }
            packet = json.loads(user)
            prefix = json.loads(options.get("cache_prefix") or "{}")
            packet["evidence"] = prefix.get("shared_source_documents", []) + packet.get("evidence", [])
            if schema == QualityJudgment:
                answer = {"decisions": [
                    {"candidate_id": candidate["candidate_id"], "decision": "accepted", "reason": "Source supports value."}
                    for candidate in packet["candidates"]
                ]}
            else:
                refinement = "first_pass_candidates" in packet
                answer = {"candidates": [{
                    "attribute_id": "Valve Type" if refinement else "Primary Material",
                    "value": "angle valve" if refinement else "brass",
                    "supporting_quote": "Angle valve." if refinement else "Body: BRASS.",
                    "evidence_ids": [packet["evidence"][0]["citation_id"]],
                    "origin": "literal",
                }]}
            return schema.model_validate(answer)

    with monkeypatch.context() as model_boundary:
        for name in ("load_cowork_reference", "normalize_cowork_reference", "reference_from_normalized",
                     "compare_reference", "compare_answer_key"):
            model_boundary.setattr(quality_scoring, name, no_reference_loading)
        summary = run_quality_batch(
            store, "isolation", "owner", "reference-isolation", execution_id="offline",
            completion=CompletionCapture(), ford_image_blob="documents/catalog.png",
            before_call=lambda context: phases.append(context["phase"]),
        )

    assert phases == ["extract", "refine", "judge"]
    assert summary["model_calls"] == len(captured) == 3
    assert [body["schema"]["title"] for body in captured] == [
        "QualityExtraction", "QualityExtraction", "QualityJudgment",
    ]
    for body in captured:
        images = body["options"]["images"]
        if body["schema"]["title"] == "QualityJudgment":
            assert images is None  # judges see cited text only
        else:
            assert len(images) == 1
            assert base64.b64decode(images[0].split(",", 1)[1]) == clean_image
        assert body["system"]
        prefix = json.loads(body["options"]["cache_prefix"])
        assert set(prefix) == {"definitions"} and prefix["definitions"]
        assert "definitions" not in json.loads(body["user"])
        assert_no_reference(body["options"])
        assert_no_reference(json.dumps(body))
    assert not any(key.startswith("scoring/") for key in store.reads)
    result = read_json(store, summary["products"][0]["result_key"])[0]
    assert_no_reference(result)
    assert [attribute["candidates"][0]["value"] for attribute in result["attributes"]] == ["brass", "angle valve"]
    assert all(attribute["candidates"][0]["judge_status"] == "accepted" for attribute in result["attributes"])

    scored = quality_scoring.compare_reference(
        [{"product_id": "P1", "attribute_id": "Primary Material", "value_type": "string"}],
        [{"product_id": "P1", "attribute_id": "Primary Material", "value": result["attributes"][0]["candidates"][0]["value"]}],
        quality_scoring.reference_from_normalized(reference), comparison_schema="six_class",
    )
    assert scored["counts"] == {"differ": 1}


def test_reference_is_absent_from_worker_tool_requests_continuation_and_native_judge(monkeypatch):
    store, _, clean_image = seed_reference_and_evidence()
    record, version = read_json(store, "batches/isolation.json")
    item = record["items"][0]
    product = item["manifest"]["product"]
    product["vendor"] = "Synthetic Ford"
    item["original"]["Vendor Name"] = product["vendor"]
    for binding in item["sources"]:
        binding["products"] = [product]
    item["manifest"]["source_ids"] = ["catalog", "vendor"]
    vendor_workbook = write_workbook({"Vendor": [
        ["MPN", "Valve Type"], [product["mpn"], "Angle valve"],
    ]})
    store.write_bytes("documents/vendor.xlsx", vendor_workbook)
    item["sources"].append({
        "source_id": "vendor", "kind": "blob", "format": "xlsx", "source_tier": "vendor_table",
        "blob": "documents/vendor.xlsx", "sha256": digest(vendor_workbook), "products": [product],
        "table": {"sheet": "Vendor", "mpn_column": "MPN", "expected_vendor": product["vendor"]},
        "applicability": [{"product": product, "attribute_ids": ["Valve Type"]}],
    })
    write_json(store, "batches/isolation.json", record, version)
    captured = []
    phases = []
    reasoning = {
        "type": "reasoning", "id": "offline-reasoning", "summary": [],
        "encrypted_content": "offline-encrypted-continuation",
    }
    vendor_call = {
        "type": "function_call", "id": "offline-function", "call_id": "offline-vendor-call",
        "name": "search_vendor_rows", "arguments": json.dumps({"attribute_id": "Valve Type"}),
        "status": "completed",
    }

    def no_reference_loading(*args, **kwargs):
        pytest.fail("Scoring references must never be loaded during the worker or tool loop")

    def create(**request):
        assert_no_reference(request)
        assert_no_reference(json.dumps(request, default=str))
        captured.append(deepcopy(request))
        inputs = request["extra_body"]["input"]
        content = inputs[0]["content"]
        prefix = json.loads(content[0]["text"])
        packet = json.loads(content[1]["text"])
        schema = request["text"]["format"]["name"]
        if schema == "ToolConclusion":
            retrieved = [entry for entry in inputs if entry.get("type") == "function_call_output"]
            if not retrieved:
                output = [reasoning, vendor_call]
                answer = None
            else:
                assert len(retrieved) == 1
                delivered = json.loads(retrieved[0]["output"])
                assert delivered["status"] == "retrieved"
                assert len(delivered["evidence"]) == 1
                entry = delivered["evidence"][0]
                assert entry["source_id"] == "vendor"
                answer = {
                    "candidates": [{
                        "attribute_id": "Valve Type", "value": "angle valve", "origin": "literal",
                        "supporting_quote": "Angle valve", "evidence_ids": [entry["evidence_id"]],
                    }],
                    "explanation": "Retrieved the current-product vendor row for the unresolved attribute.",
                }
        elif schema == "QualityJudgment":
            answer = {"decisions": [
                {"candidate_id": candidate["candidate_id"], "decision": "accepted",
                 "reason": "Synthetic quote and product source support this candidate."}
                for candidate in packet["candidates"]
            ]}
        else:
            assert schema == "QualityExtraction"
            answer = {"candidates": []}
            if packet["active_tier"] == "internal_pdf" and "first_pass_candidates" not in packet:
                entry = next(entry for entry in packet["evidence"] if entry["kind"] != "vendor_row")
                answer["candidates"] = [{
                    "attribute_id": "Primary Material", "value": "brass", "origin": "literal",
                    "supporting_quote": "Body: BRASS.", "evidence_ids": [entry["citation_id"]],
                }]
        if answer is not None:
            output = [{
                "type": "message", "id": "offline-message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(answer), "annotations": []}],
            }]
        return {
            "id": f"offline-response-{len(captured)}", "status": "completed", "model": "offline-model",
            "output": output,
            "usage": {
                "input_tokens": 100, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens": 10, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 110,
            },
        }

    completion = ResponsesCompletion.__new__(ResponsesCompletion)
    completion.client = SimpleNamespace(max_retries=0, responses=SimpleNamespace(create=create))
    completion.deployment, completion.effort, completion.max_output_tokens = "offline-model", "medium", 1000
    completion.pricing_usd_per_million = {"input": 2, "cached_input": 0.5, "cache_write": 2, "output": 8}
    completion.pricing_basis = "synthetic_test"
    completion.last_usage, completion.call_records, completion.usage_callback = {}, [], None
    with monkeypatch.context() as model_boundary:
        for name in ("load_cowork_reference", "normalize_cowork_reference", "reference_from_normalized",
                     "compare_reference", "compare_answer_key"):
            model_boundary.setattr(quality_scoring, name, no_reference_loading)
        summary = run_quality_batch(
            store, "isolation", "owner", "tool-reference-isolation", execution_id="offline",
            completion=completion, ford_image_blob="documents/catalog.png",
            before_call=lambda context: phases.append(context["phase"]),
            tool_loop_enabled=True,
            tool_prices={"search_usd": 0.0125, "browse_usd": 0.0125, "di_usd_per_page": 0.01},
        )

    assert phases == ["extract", "refine", "judge", "extract", "refine", "tool_loop", "tool_loop", "judge"]
    assert summary["model_calls"] == len(captured) == len(completion.call_records) == 8
    assert [request["text"]["format"]["name"] for request in captured] == [
        "QualityExtraction", "QualityExtraction", "QualityJudgment", "QualityExtraction", "QualityExtraction",
        "ToolConclusion", "ToolConclusion", "QualityJudgment",
    ]
    for request in captured:
        assert request["instructions"] and request["text"]["format"]["schema"]
        assert request["text"]["format"]["strict"] is True
        assert request["extra_body"]["prompt_cache_key"]
        assert request["extra_body"]["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
        prefix_part = request["extra_body"]["input"][0]["content"][0]
        assert prefix_part["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert json.loads(prefix_part["text"])["definitions"]
        assert_no_reference(request)
        assert_no_reference(json.dumps(request, default=str))
    for request in captured[:2]:
        images = [
            entry["image_url"] for entry in request["extra_body"]["input"][0]["content"]
            if entry["type"] == "input_image"
        ]
        assert len(images) == 1
        assert base64.b64decode(images[0].split(",", 1)[1]) == clean_image
    first_tool, continuation, native_judge = captured[5:]
    for request in (first_tool, continuation):
        assert {tool["name"] for tool in request["tools"]} == {
            "search_vendor_rows", "read_pdf_page", "web_search", "browse", "fetch_pdf",
        }
        assert all(tool["strict"] and tool["parameters"] for tool in request["tools"])
        assert request["parallel_tool_calls"] is False
        assert request["include"] == ["reasoning.encrypted_content"]
    pending_packet = json.loads(first_tool["input"][0]["content"])
    assert pending_packet["pass1_status"]["Valve Type"] == "unresolved"
    assert len(first_tool["input"]) == 1
    assert continuation["input"][1:3] == [reasoning, vendor_call]
    assert continuation["extra_body"]["input"][1:] == continuation["input"][1:]
    retrieved = continuation["input"][3]
    assert retrieved["type"] == "function_call_output" and retrieved["call_id"] == vendor_call["call_id"]
    delivered = json.loads(retrieved["output"])["evidence"]
    assert len(delivered) == 1 and delivered[0]["source_tier"] == "vendor_table"
    assert native_judge["tools"] == [] and native_judge["reasoning"] == {"effort": "low"}
    judge_packet = json.loads(native_judge["input"][0]["content"])
    assert [candidate["attribute_id"] for candidate in judge_packet["candidates"]] == ["Valve Type"]
    assert judge_packet["candidates"][0]["citation_ids"] == [judge_packet["evidence"][0]["citation_id"]]
    assert "evidence_ids" not in judge_packet["candidates"][0]
    assert not any(key.startswith("scoring/") for key in store.reads)
    assert "documents/vendor.xlsx" in store.reads
    result = read_json(store, summary["products"][0]["result_key"])[0]
    assert_no_reference(result)
    valve = next(attribute for attribute in result["attributes"] if attribute["attribute_id"] == "Valve Type")
    assert len(valve["candidates"]) == 1
    grounded = valve["candidates"][0]
    assert grounded["value"] == "angle valve" and grounded["judge_status"] == "accepted"
    assert grounded["grounding"]["quote"]["method"] == "vendor_cells"
    assert grounded["grounding"]["judge_cache"]["votes"][0]["decision"] == "accepted"
    loop_summary = next(entry for entry in result["quality_diagnostics"] if entry["operation"] == "tool_loop_summary")
    assert loop_summary["status"] == "completed" and loop_summary["steps"] == 4
    assert sorted(loop_summary["requested_attributes"]) == sorted(pending_packet["pass1_status"])
    assert loop_summary["cost_complete"] is True and loop_summary["tool_loop_cost_usd"] > 0


@pytest.mark.parametrize("encoding", ["nested_json", "cache_prefix", "image", "workbook", "base64"])
def test_isolation_capture_detects_injected_serialized_reference(encoding):
    if encoding == "nested_json":
        value = json.dumps({"input": json.dumps({"reference": SENTINEL})})
    elif encoding == "cache_prefix":
        escaped = "".join(f"\\u{ord(char):04x}" for char in SENTINEL)
        value = {"options": {"cache_prefix": '{"shared_source_documents":[{"text":"' + escaped + '"}]}'}}
        assert SENTINEL not in json.dumps(value)
    elif encoding == "image":
        value = {"input": [{"image_url": "data:image/png;base64," + base64.b64encode(png(SENTINEL)).decode()}]}
    elif encoding == "workbook":
        content = write_workbook({"Draft": [["Value"], [SENTINEL]]})
        value = {"input": [{"workbook_base64": base64.b64encode(content).decode()}]}
    else:
        value = base64.b64encode(SENTINEL.encode()).decode()
    with pytest.raises(AssertionError, match="Scoring reference leaked"):
        assert_no_reference(value)
