"""Scoring references cannot reach extraction, refinement, or judge requests."""

import base64
from datetime import datetime, timezone
from io import BytesIO
import json
import socket
from zipfile import ZipFile

from PIL import Image, PngImagePlugin
import pytest

from backend.batch import digest
from backend.batch_store import Conflict, Missing, read_json, write_json
from backend.core.docintel import ParsedDocument, ParsedParagraph
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
        assert len(images) == 1
        assert base64.b64decode(images[0].split(",", 1)[1]) == clean_image
        assert body["system"]
        prefix = json.loads(body["options"]["cache_prefix"])
        assert prefix["definitions"]
        assert "shared_source_documents" in prefix
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
