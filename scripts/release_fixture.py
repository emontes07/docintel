import argparse
import hashlib
import json
import os
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend.batch import BatchService, digest
from backend.batch_store import Missing, SQLiteStore
from backend.batch_worker import run_batch
from backend.core.docintel import ParsedDocument, ParsedParagraph
from backend.pilot import PARSER_VERSION
from backend.workbooks import read_workbook, write_workbook


def synthetic_pdf():
    stream = b"BT /F1 12 Tf 40 700 Td (Synthetic acceptance only. PART-1 and PART-2.) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(content))
        content.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    start = len(content)
    content.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(content)


def prepare(output):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    content = synthetic_pdf()
    reference = "release-synthetic.pdf"
    key = "documents/" + reference
    location = "batchblob:///" + key
    document = ParsedDocument(
        source=location,
        cache_key="sha256:" + digest(content),
        parsed_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        paragraphs=[ParsedParagraph(text="Synthetic acceptance only. PART-1 and PART-2.", page_number=1)],
    )
    products = [dict(item_id=f"{index:03d}", vendor="Synthetic", mpn=f"PART-{index}", hierarchy_node="Valve") for index in [1, 2]]
    registry = {"sources": [dict(reference=reference, source_id="release-synthetic", kind="blob", blob=key, sha256=digest(content), products=products)]}
    cache_key = "parses/" + digest((location + digest(content) + PARSER_VERSION).encode()) + ".json"
    cache = dict(parser_version=PARSER_VERSION, origin="synthetic_fixture_not_service_analysis", document=document.model_dump(mode="json"), document_sha256=digest(document.model_dump_json().encode()))
    artifacts = {key: content, "configuration/sources.json": json.dumps(registry).encode(), cache_key: json.dumps(cache).encode()}
    for name, data in artifacts.items():
        destination = output / "seed" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    manifest = [["PIMITEM Number", "Vendor Name", "MPN", "Hierarchy Node", "PDF Tech Spec", "Attributes to Fill"]]
    manifest.extend([[product["item_id"], product["vendor"], product["mpn"], product["hierarchy_node"], reference, "definitions.xlsx"] for product in products])
    definitions = [["node", "potential_attribute_name", "potential_attribute_data_type", "unit"], ["Valve", "Pressure Rating", "Numeric", "PSI"]]
    (output / "manifest.xlsx").write_bytes(write_workbook({"Products": manifest}))
    (output / "definitions.xlsx").write_bytes(write_workbook({"Attributes": definitions}))
    hashes = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest() for path in output.rglob("*") if path.is_file()}
    (output / "sha256.json").write_text(json.dumps(hashes, indent=2))


def verify_export(content, batch_id, owner, machine_hashes):
    workbook = read_workbook(content)
    assert set(workbook) == {"Batch", "Inputs", "Definitions", "Results", "Evidence", "Provenance", "Errors", "Reviews"}
    metadata = {row["Key"]: row["Value"] for row in workbook["Batch"]}
    assert metadata["Batch ID"] == batch_id and metadata["Batch state"] == "completed"
    assert metadata["Attribute reference"] == "definitions.xlsx"
    assert "not approved master data" in metadata["Qualification"]
    assert [(row["PIMITEM Number"], row["MPN"]) for row in workbook["Inputs"]] == [("001", "PART-1"), ("002", "PART-2")]
    assert all(row["Vendor Name"] == "Synthetic" and row["Hierarchy Node"] == "Valve" and row["PDF Tech Spec"] == "release-synthetic.pdf" for row in workbook["Inputs"])
    assert workbook["Definitions"] == [{"node": "Valve", "potential_attribute_name": "Pressure Rating", "potential_attribute_data_type": "Numeric", "unit": "PSI"}]
    assert [(row["Row"], row["Item ID"], row["MPN"]) for row in workbook["Results"]] == [("2", "001", "PART-1"), ("3", "002", "PART-2")]
    assert all(row["Attribute"] == "Pressure Rating" and not row["Proposed value"] and not row["Candidate index"] for row in workbook["Results"])
    assert [row["Review status"] for row in workbook["Results"]] == ["reject", "pending"]
    assert len(workbook["Reviews"]) == 1
    review = workbook["Reviews"][0]
    assert review["Row"] == "2" and review["Attribute"] == "Pressure Rating" and review["Decision"] == "reject"
    assert review["Reviewer"] == owner and review["Reviewed at"]
    assert review["Identity status"] == ("development_unverified" if owner.startswith("development:") else "verified_entra")
    assert workbook["Results"][0]["Reviewer"] == owner
    assert workbook["Results"][0]["Reviewed at"] == review["Reviewed at"]
    assert {row["Row"]: row["Machine SHA256"] for row in workbook["Provenance"]} == machine_hashes
    for row in workbook["Provenance"]:
        assert row["Execution method"] == "evidence_only"
        sources = json.loads(row["Source provenance"])
        assert sources and all(source["parsing"] == "cache" and source["parse_origin"] == "synthetic_fixture_not_service_analysis" for source in sources)
    assert len(workbook["Errors"]) == 2
    assert all(row["State"] == "unresolved" and not row["Error"] for row in workbook["Errors"])
    assert all(row["Source ID"] == "release-synthetic" and "Synthetic acceptance only" in row["Excerpt"] for row in workbook["Evidence"])
    return True


def verify(output):
    with tempfile.TemporaryDirectory(prefix="docintel-release-check-") as temporary:
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")):
            store = SQLiteStore(Path(temporary))
            for path in (output / "seed").rglob("*"):
                if path.is_file():
                    store.write_bytes(path.relative_to(output / "seed").as_posix(), path.read_bytes())
            service = BatchService(store)
            owner = "development:synthetic-release"
            batch = service.intake((output / "manifest.xlsx").read_bytes(), (output / "definitions.xlsx").read_bytes(), "definitions.xlsx", owner)
            assert batch["valid"] and batch["product_count"] == 2
            request = "11111111-1111-4111-8111-111111111111"
            submitted = service.submit(batch["id"], owner, request, "evidence_only", False)
            assert service.submit(batch["id"], owner, request, "evidence_only", False) == submitted
            run_batch(store, batch["id"], item_limit=1, concurrency=1)
            assert service.get(batch["id"], owner)["state"] == "queued"
            first = service.detail(batch["id"], "row-2", owner)
            assert first["provenance"][0]["parsing"] == "cache"
            assert first["provenance"][0]["parse_origin"] == "synthetic_fixture_not_service_analysis"
            assert first["machine_result"]["model_call_status"] == "not_attempted"
            first_bytes = store.read_bytes(f"results/{batch['id']}/row-2.json")
            store = SQLiteStore(Path(temporary))
            service = BatchService(store)
            assert service.get(batch["id"], owner)["state"] == "queued"
            run_batch(store, batch["id"], item_limit=1, concurrency=1)
            assert service.get(batch["id"], owner)["state"] == "completed"
            assert service.detail(batch["id"], "row-2", owner) == first
            assert store.read_bytes(f"results/{batch['id']}/row-2.json") == first_bytes
            machine_hashes = {}
            result_records = {}
            for row in (2, 3):
                detail = service.detail(batch["id"], f"row-{row}", owner)
                assert detail["machine_result"]["manifest"]["product"]["item_id"] == f"{row - 1:03d}"
                assert detail["machine_result"]["model_call_status"] == "not_attempted"
                assert all(not attribute["candidates"] for attribute in detail["machine_result"]["attributes"])
                assert all(source["parsing"] == "cache" and source["parse_origin"] == "synthetic_fixture_not_service_analysis" for source in detail["provenance"])
                machine_hashes[str(row)] = detail["machine_sha256"]
                result_records[row] = store.read_bytes(f"results/{batch['id']}/row-{row}.json")
            assert not store.keys("analysis-attempts/") and not store.keys("budgets/")
            try:
                service.get(batch["id"], "other-owner")
            except Missing:
                pass
            else:
                raise AssertionError("Owner isolation failed")
            review = dict(attribute_id="Pressure Rating", decision="reject", reason="Synthetic acceptance only: no candidate; not a product decision")
            service.review(batch["id"], "row-2", owner, review)
            try:
                service.review(batch["id"], "row-2", owner, review)
            except ValueError:
                pass
            else:
                raise AssertionError("Duplicate review accepted")
            run_batch(store, batch["id"], processor=lambda *_: (_ for _ in ()).throw(AssertionError("Completed work repeated")))
            service = BatchService(SQLiteStore(Path(temporary)))
            for row, original in result_records.items():
                assert service.store.read_bytes(f"results/{batch['id']}/row-{row}.json") == original
            content = service.export(batch["id"], owner)
            verify_export(content, batch["id"], owner, machine_hashes)
    print("PASS: both synthetic products; no network/AI; durable continuation, deduplication, ownership, immutable results and workbook cells. Hosted acceptance remains unverified.")
    return content, batch["id"], owner, machine_hashes


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    options = parser.parse_args()
    prepare(options.output)
    if options.verify:
        verify(options.output)