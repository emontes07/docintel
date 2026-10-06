"""Synthetic-only scoring: no customer workbook reads and no provider access."""

import copy
import hashlib
import json
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
import xml.etree.ElementTree as ET

import pytest

from backend.answer_key import AnswerKeyError, AnswerKeyService, _BOUND_COLUMNS as REVIEW_COLUMNS, _binding
from backend.batch_store import Conflict, Missing, read_json, write_json
from backend.workbooks import MAIN, read_workbook, write_workbook

OWNER = "synthetic-owner"


class MemoryStore:
    """The same conditional-create/read contract as the durable stores."""

    def __init__(self):
        self.records = {}
        self.revision = 0

    def read_bytes(self, key, max_bytes=64 * 1024 * 1024):
        if key not in self.records:
            raise Missing(key)
        value, version = self.records[key]
        if len(value) > max_bytes:
            raise ValueError("Read bound")
        return value, version

    def write_bytes(self, key, value, version=None):
        if key in self.records and self.records[key][1] != version:
            raise Conflict("Already exists or changed")
        if key not in self.records and version is not None:
            raise Conflict("Missing")
        self.revision += 1
        self.records[key] = bytes(value), str(self.revision)
        return str(self.revision)

    def keys(self, prefix):
        return sorted(key for key in self.records if key.startswith(prefix))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("Reviewer scoring must remain offline")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def seed():
    """12 items x 2 attributes: literal, inferred, conflicts, absent and failed."""
    store = MemoryStore()
    batch_id = "synthetic-batch"
    items = []
    for index in range(12):
        product = {"item_id": f"{index:03}", "vendor": "Synthetic", "mpn": f"PART-{index}", "hierarchy_node": "Valve"}
        manifest = {
            "product": product, "source_ids": ["synthetic-source"], "existing_values": {},
            "attributes": [
                {"attribute_id": "Pressure", "description": "", "value_type": "number", "unit": "PSI"},
                {"attribute_id": "Lead Free", "description": "", "value_type": "boolean"},
            ],
        }
        item_key = f"row-{index + 2}"
        items.append({"item_key": item_key, "manifest": manifest, "errors": [], "original": {}, "row": index + 2})
        evidence = [{
            "evidence_id": "e1", "source_id": "synthetic-source", "source_locator": "synthetic.pdf#page=1",
            "source_version": "synthetic-v1", "source_tier": "internal_pdf", "content_kind": "source_excerpt",
            "text": "Pressure 100 PSI; lead-free product.", "observed_at": "2026-01-01T00:00:00Z",
        }]
        if index == 1:
            evidence.append({**evidence[0], "evidence_id": "e2", "source_tier": "vendor_table"})
        pressure = {"attribute_id": "Pressure", "value": 100, "unit": "PSI", "evidence_ids": [e["evidence_id"] for e in evidence]}
        lead = {"attribute_id": "Lead Free", "value": True, "evidence_ids": ["e1"]}
        if index % 2 == 0:
            lead.update(evidence_basis="inferred_from_description", inference_rule="lead_free_description_v1", supporting_quote="lead-free product")
        attributes = [
            {"attribute_id": "Pressure", "status": "proposed", "candidates": [pressure]},
            {"attribute_id": "Lead Free", "status": "proposed", "candidates": [lead]},
        ]
        if index == 2:
            attributes[0].update(status="conflict", candidates=[pressure, {**pressure, "value": 200}])
        if index == 3:
            attributes[0].update(status="missing_evidence", candidates=[])
        if index == 4:
            manifest["existing_values"] = {"Pressure": 75}
            attributes[0].update(status="existing", candidates=[])
        if index != 11:
            result = {
                "execution_mode": "offline_replay", "candidate_source": "supplied_response",
                "model_call_status": "not_attempted", "manifest": manifest,
                "observed_at": "2026-01-01T00:00:00Z", "retrieval": [], "evidence": evidence, "attributes": attributes,
            }
            write_json(store, f"results/{batch_id}/{item_key}.json", result)
    write_json(store, f"batches/{batch_id}.json", {"id": batch_id, "owner": OWNER, "items": items})
    return AnswerKeyService(store), batch_id


@pytest.fixture
def scoring():
    service, batch_id = seed()
    snapshot = service.snapshot(batch_id, OWNER)
    register_synthetic_baseline(service, snapshot["id"])
    return service, snapshot


def synthetic_baseline(service, snapshot_id):
    columns = ["Product ID", "MPN", "Attribute", "Decision", "Correction", "Correction unit", "Reason",
               "Status", "Proposed value", "Unit", "Evidence basis", "Supporting quote", "Evidence",
               "Type", "Expected unit"]
    rows = [columns]
    evidence = [["Product ID", "MPN", "Attribute", "Quote"]]
    for slot in service.get_snapshot(snapshot_id, OWNER)["body"]["slots"]:
        candidates = slot["candidates"]
        quote = "Synthetic publicly shareable evidence."
        rows.append([
            slot["product"]["item_id"], slot["product"]["mpn"], slot["attribute_id"], "", "", "", "",
            slot["status"], "; ".join(str(candidate["value"]) for candidate in candidates),
            slot["definition"]["unit"], "; ".join(candidate["evidence_basis"] for candidate in candidates),
            quote if candidates else "", "Internal document" if candidates else "",
            slot["definition"]["value_type"], slot["definition"]["unit"],
        ])
        evidence.append([slot["product"]["item_id"], slot["product"]["mpn"], slot["attribute_id"], quote if candidates else ""])
    return write_workbook({"Review": rows, "Evidence": evidence, "Instructions": [["Topic", "Guidance"], ["Review", "Edit Decision, Correction, Correction unit and Reason only."]]})


def register_synthetic_baseline(service, snapshot_id):
    return service.register_baseline(snapshot_id, OWNER, synthetic_baseline(service, snapshot_id), [
        {"product_id": "002", "attribute_id": "Pressure", "candidate_index": 0},
    ])


def sheet_rows(service, snapshot_id):
    return [
        dict(zip(REVIEW_COLUMNS, [*_binding(snapshot_id, slot), "", "", "", "", ""]))
        for slot in service.get_snapshot(snapshot_id, OWNER)["body"]["slots"]
    ]


def workbook(rows):
    return write_workbook({"Review": [REVIEW_COLUMNS, *[[row.get(column, "") for column in REVIEW_COLUMNS] for row in rows]]})


def public_sheets(service, snapshot_id):
    sheets = read_workbook(service.template(snapshot_id, OWNER))
    private = {(row["Item ID"], row["Attribute"]): row for row in reviewed_rows(service, snapshot_id)}
    for row in sheets["Review"]:
        judgment = private[row["Product ID"], row["Attribute"]]
        row.update({
            "Decision": judgment["Decision"],
            "Correction": judgment["Corrected value"], "Correction unit": judgment["Corrected unit"],
            "Reason": judgment["Reason"],
        })
    return sheets


def public_workbook(sheets):
    return write_workbook({
        name: [list(rows[0]), *[list(row.values()) for row in rows]]
        for name, rows in sheets.items() if rows
    })


def reviewed_rows(service, snapshot_id):
    rows = sheet_rows(service, snapshot_id)
    for row in rows:
        row["Reason"] = "Synthetic human judgment."
        row["Decision"] = "Approve"
        row["Candidate index"] = "0"
        if row["Item ID"] == "001" and row["Attribute"] == "Pressure":
            row.update({"Decision": "Correct", "Candidate index": "", "Corrected value": "200", "Corrected unit": "PSI"})
        elif row["Item ID"] in {"003", "004", "011"}:
            row.update({"Decision": "", "Candidate index": "", "Reason": ""})
        elif row["Item ID"] == "005" and row["Attribute"] == "Pressure":
            row.update({"Decision": "Reject", "Candidate index": "0"})
    return rows


def test_snapshot_template_is_unreviewed_immutable_and_owner_scoped(scoring):
    service, snapshot = scoring
    assert len(snapshot["body"]["slots"]) == 24
    assert snapshot == service.snapshot("synthetic-batch", OWNER)
    assert all(not row["Decision"] for row in sheet_rows(service, snapshot["id"]))
    metrics = service.score(snapshot["id"], OWNER)["overall"]
    assert metrics["reviewed_slots"] == 0 and metrics["accuracy"] is None
    assert metrics["unreviewed_slots"] == 24 and metrics["abstentions"] == 4
    assert metrics["candidate_count"] == 21 and metrics["conflicts"] == 1
    for operation in (
        lambda: service.snapshot("synthetic-batch", "other"),
        lambda: service.get_snapshot(snapshot["id"], "other"),
        lambda: service.template(snapshot["id"], "other"),
        lambda: service.score(snapshot["id"], "other"),
    ):
        with pytest.raises(Missing):
            operation()
    assert service.list_records("other") == {"snapshots": [], "versions": [], "packages": []}


def test_ingest_persists_versions_without_mutating_machine_results(scoring):
    service, snapshot = scoring
    before = copy.deepcopy(service.store.records)
    content = workbook(reviewed_rows(service, snapshot["id"]))
    version = service._ingest_bound(snapshot["id"], OWNER, content)
    assert AnswerKeyService(service.store).get_version(version["id"], OWNER) == version
    assert service._ingest_bound(snapshot["id"], OWNER, content) == version
    for key, record in before.items():
        assert service.store.records[key] == record
    rows = reviewed_rows(service, snapshot["id"])
    next(row for row in rows if row["Decision"])["Reason"] = "A new explicit review."
    newer = service._ingest_bound(snapshot["id"], OWNER, workbook(rows), version["id"])
    assert newer["id"] != version["id"] and newer["body"]["previous_version"] == version["id"]
    assert service.get_version(version["id"], OWNER) == version
    assert len(service.list_records(OWNER)["versions"]) == 2
    with pytest.raises(Missing):
        service.get_version(version["id"], "other")


def test_metrics_have_explicit_denominators_and_tier_basis_slices(scoring):
    service, snapshot = scoring
    version = service._ingest_bound(snapshot["id"], OWNER, workbook(reviewed_rows(service, snapshot["id"])))
    score = service.score(snapshot["id"], OWNER, version["id"])
    metrics = score["overall"]
    assert metrics["eligible_slots"] == 24
    assert metrics["reviewed_slots"] == 18 and metrics["unreviewed_slots"] == 6
    assert metrics["directly_reviewed_slots"] == 18 and metrics["transferred_gold_slots"] == 0
    assert metrics["gold_slots"] == 17 and metrics["rejected_slots"] == 1
    assert metrics["output_slots"] == 20 and metrics["abstentions"] == 4
    assert metrics["conflicts"] == metrics["reviewed_conflicts"] == 1
    assert metrics["scorable_single_outputs"] == 17
    assert metrics["correct_single_outputs"] == 15
    assert metrics["accuracy"] == 15 / 17 and metrics["correct_coverage"] == 15 / 17
    assert metrics["judged_candidates"] == 19 and metrics["correct_candidates"] == 16
    assert metrics["candidate_accuracy"] == 16 / 19
    assert metrics["unjudged_candidates"] == 2
    assert metrics["literal_candidates"] == 15 and metrics["inferred_candidates"] == 6
    assert metrics["existing_slots"] == 1 and metrics["no_result_slots"] == 2
    assert score["by_attribute"]["Pressure"]["eligible_slots"] == 12
    assert score["by_tier"]["vendor_table"]["eligible_slots"] == 24
    assert score["by_tier"]["vendor_table"]["output_slots"] == 1
    assert score["by_tier"]["vendor_table"]["accuracy"] == 0
    assert score["by_tier"]["manufacturer_web"]["accuracy"] is None
    assert score["by_evidence_basis"]["inferred_from_description"]["candidate_count"] == 6
    inferred = score["by_evidence_basis"]["inferred_from_description"]
    assert inferred["accuracy"] == 1 and inferred["candidate_accuracy"] == 1
    assert inferred["reviewed_inferred_candidates"] == inferred["inferred_value_agreements"] == 5


@pytest.mark.parametrize(("column", "value", "message"), [
    ("Decision", "approved", "Decision must"),
    ("Reason", " ", "requires a Reason"),
    ("Candidate index", "999", "existing zero-based"),
    ("Candidate index", "0.0", "existing zero-based"),
    ("Item key", "unknown", "Unknown item"),
    ("Attribute", "Unknown", "Unknown item"),
    ("Item ID", "999", "binding differs"),
    ("Vendor", "Other", "binding differs"),
    ("MPN", "Other", "binding differs"),
    ("Hierarchy node", "Other", "binding differs"),
    ("Snapshot ID", "0" * 64, "binding differs"),
    ("Attempt key", "other-attempt", "binding differs"),
    ("Result SHA256", "0" * 64, "binding differs"),
    ("Corrected value", "25", "Approve cannot contain"),
])
def test_invalid_cells_are_atomic_with_explicit_row_errors(scoring, column, value, message):
    service, snapshot = scoring
    rows = reviewed_rows(service, snapshot["id"])
    target = next(row for row in rows if row["Item ID"] == "000" and row["Attribute"] == "Pressure")
    target[column] = value
    before = copy.deepcopy(service.store.records)
    with pytest.raises(AnswerKeyError, match=message):
        service._ingest_bound(snapshot["id"], OWNER, workbook(rows))
    assert service.store.records == before


@pytest.mark.parametrize(("changes", "message"), [
    ({"Corrected value": ""}, "nonblank"),
    ({"Corrected value": "NaN"}, "must match"),
    ({"Corrected value": "inf"}, "must match"),
    ({"Corrected unit": "bar"}, "must match"),
    ({"Candidate index": "0"}, "leave Candidate index blank"),
])
def test_bad_corrections_are_rejected(scoring, changes, message):
    service, snapshot = scoring
    rows = reviewed_rows(service, snapshot["id"])
    next(row for row in rows if row["Decision"] == "Correct").update(changes)
    with pytest.raises(AnswerKeyError, match=message):
        service._ingest_bound(snapshot["id"], OWNER, workbook(rows))


@pytest.mark.parametrize("mode", ["duplicate", "missing", "unknown_header", "blank", "unreviewed_reason", "reject_index"])
def test_invalid_workbook_shapes_and_ambiguous_reviews(scoring, mode):
    service, snapshot = scoring
    rows = reviewed_rows(service, snapshot["id"])
    if mode == "duplicate":
        rows.append(rows[0])
    elif mode == "missing":
        rows.pop()
    elif mode == "unknown_header":
        content = write_workbook({"Review": [["Unknown"], ["data"]]})
    elif mode == "blank":
        rows = sheet_rows(service, snapshot["id"])
    elif mode == "unreviewed_reason":
        next(row for row in rows if not row["Decision"])["Reason"] = "Not a decision"
    else:
        next(row for row in rows if row["Decision"] == "Reject")["Candidate index"] = "999"
    content = content if mode == "unknown_header" else workbook(rows)
    with pytest.raises(AnswerKeyError):
        service._ingest_bound(snapshot["id"], OWNER, content)
    assert not service.list_records(OWNER)["versions"]


def test_formula_in_any_sheet_is_rejected(scoring):
    service, snapshot = scoring
    content = workbook(reviewed_rows(service, snapshot["id"]))
    buffer = BytesIO()
    with ZipFile(BytesIO(content)) as source, ZipFile(buffer, "w", ZIP_DEFLATED) as target:
        for entry in source.infolist():
            raw = source.read(entry.filename)
            if entry.filename == "xl/worksheets/sheet1.xml":
                raw = raw.replace(
                    b"<is>", f'<f xmlns="{MAIN}">1+1</f><is>'.encode(), 1,
                )
            target.writestr(entry, raw)
    with pytest.raises(AnswerKeyError, match="Formula"):
        service._ingest_bound(snapshot["id"], OWNER, buffer.getvalue())


def append_attempt(service, value=200):
    key = "results/synthetic-batch/row-3.json"
    result, _ = read_json(service.store, key)
    result["attributes"][0]["candidates"][0]["value"] = value
    attempt_key = f"results/synthetic-batch/row-3/attempts/{hashlib.sha256(str(value).encode()).hexdigest()}.json"
    write_json(service.store, attempt_key, result)
    write_json(service.store, "items/synthetic-batch/row-3.json", {"result_key": attempt_key})
    snapshot = service.snapshot("synthetic-batch", OWNER)
    register_synthetic_baseline(service, snapshot["id"])
    return snapshot


def test_delta_compares_stable_product_attribute_across_attempts_and_scores_same_gold(scoring):
    service, old = scoring
    version = service._ingest_bound(old["id"], OWNER, workbook(reviewed_rows(service, old["id"])))
    old_bytes = copy.deepcopy(service.get_snapshot(old["id"], OWNER))
    new = append_attempt(service)
    delta = service.delta(new["id"], old["id"], OWNER, version["id"])
    assert delta["counts"] == {"added": 0, "removed": 0, "changed": 2, "unchanged": 22}
    pressure = next(row for row in delta["rows"] if row["product"]["item_id"] == "001" and row["attribute_id"] == "Pressure")
    assert {"candidates", "attempt_key", "result_sha256"} <= set(pressure["changed_fields"])
    assert delta["after_score"]["overall"]["correct_single_outputs"] == 16
    assert delta["before_score"]["overall"]["correct_single_outputs"] == 15
    assert delta["after_score"]["overall"]["transferred_gold_slots"] == 2
    assert delta["after_score"]["overall"]["directly_reviewed_slots"] == 16
    assert service.get_snapshot(old["id"], OWNER) == old_bytes
    # Historical workbook stays valid against its old snapshot, not the newer attempt.
    assert service._ingest_bound(old["id"], OWNER, workbook(reviewed_rows(service, old["id"])))["body"]["snapshot_id"] == old["id"]
    with pytest.raises(AnswerKeyError, match="binding differs"):
        service._ingest_bound(new["id"], OWNER, workbook(reviewed_rows(service, old["id"])))
    with pytest.raises(Missing):
        service.delta(new["id"], old["id"], "other", version["id"])


def test_cross_attempt_rejection_does_not_invent_gold(scoring):
    service, old = scoring
    rows = sheet_rows(service, old["id"])
    row = next(row for row in rows if row["Item ID"] == "001" and row["Attribute"] == "Pressure")
    row.update({"Decision": "Reject", "Candidate index": "0", "Reason": "Wrong value"})
    version = service._ingest_bound(old["id"], OWNER, workbook(rows))
    new = append_attempt(service)
    assert service.score(old["id"], OWNER, version["id"])["overall"]["rejected_slots"] == 1
    assert service.score(new["id"], OWNER, version["id"])["overall"]["reviewed_slots"] == 0
    with pytest.raises(AnswerKeyError, match="another snapshot"):
        service._ingest_bound(new["id"], OWNER, workbook(reviewed_rows(service, new["id"])), version["id"])


def test_correct_can_supply_gold_for_abstention_and_boolean(scoring):
    service, snapshot = scoring
    rows = sheet_rows(service, snapshot["id"])
    for row in rows:
        if row["Item ID"] == "011":
            row.update({"Decision": "Correct", "Reason": "Human reference answer",
                        "Corrected value": "false" if row["Attribute"] == "Lead Free" else "125",
                        "Corrected unit": "" if row["Attribute"] == "Lead Free" else "PSI"})
    version = service._ingest_bound(snapshot["id"], OWNER, workbook(rows))
    metrics = service.score(snapshot["id"], OWNER, version["id"])["overall"]
    assert metrics["gold_slots"] == 2 and metrics["correct_coverage"] == 0
    assert metrics["accuracy"] is None and metrics["reviewed_slots"] == 2


def test_integrity_check_rejects_modified_immutable_record(scoring):
    service, snapshot = scoring
    key = next(key for key in service.store.records if "/snapshots/" in key)
    record, revision = read_json(service.store, key)
    record["body"]["slots"][0]["status"] = "tampered"
    write_json(service.store, key, record, revision)
    with pytest.raises(AnswerKeyError, match="integrity"):
        service.get_snapshot(snapshot["id"], OWNER)


def test_source_pointer_race_does_not_publish_snapshot(scoring, monkeypatch):
    service, snapshot = scoring
    original_read = service.store.read_bytes
    reads = 0

    def racing_read(key, max_bytes=64 * 1024 * 1024):
        nonlocal reads
        if key == "items/synthetic-batch/row-2.json":
            reads += 1
            if reads == 2:
                return json.dumps({"result_key": f"results/synthetic-batch/row-2/attempts/{'a' * 64}.json"}).encode(), "race"
        return original_read(key, max_bytes)

    monkeypatch.setattr(service.store, "read_bytes", racing_read)
    with pytest.raises(Conflict, match="pointer advanced"):
        service.snapshot("synthetic-batch", OWNER)
    assert len(service.list_records(OWNER)["snapshots"]) == 1


def test_delta_matches_product_not_row_and_reports_added_removed_and_definition_change(scoring):
    service, old = scoring
    version = service._ingest_bound(old["id"], OWNER, workbook(reviewed_rows(service, old["id"])))
    batch, _ = read_json(service.store, "batches/synthetic-batch.json")
    batch["id"] = "synthetic-second-batch"
    batch["items"].reverse()
    # Drop one product, change one identity, and move every row.
    batch["items"].pop()
    for index, item in enumerate(batch["items"], 20):
        old_key = item["item_key"]
        item["item_key"] = f"row-{index}"
        if item["manifest"]["product"]["item_id"] == "001":
            item["manifest"]["attributes"][0]["description"] = "New definition; needs new gold"
        if item["manifest"]["product"]["item_id"] == "002":
            item["manifest"]["product"]["item_id"] = "NEW"
        try:
            result, _ = read_json(service.store, f"results/synthetic-batch/{old_key}.json")
        except Missing:
            continue
        result["manifest"] = item["manifest"]
        write_json(service.store, f"results/{batch['id']}/{item['item_key']}.json", result)
    write_json(service.store, f"batches/{batch['id']}.json", batch)
    new = service.snapshot(batch["id"], OWNER)
    delta = service.delta(new["id"], old["id"], OWNER, version["id"])
    assert delta["counts"]["added"] == 2 and delta["counts"]["removed"] == 4
    changed = next(row for row in delta["rows"] if row["product"]["item_id"] == "001" and row["attribute_id"] == "Pressure")
    assert "definition" in changed["changed_fields"]
    assert changed["before"]["item_key"] != changed["after"]["item_key"]
    assert delta["after_score"]["overall"]["rejected_slots"] == 0
    assert delta["after_score"]["by_attribute"]["Pressure"]["reviewed_slots"] == 5


@pytest.mark.parametrize(("value_type", "value", "allowed", "valid"), [
    ("boolean", "1", [], False),
    ("boolean", "false", [], True),
    ("integer", "1.5", [], False),
    ("integer", "0", [], True),
    ("string", "Blue", ["Red"], False),
    ("string", "Red", ["Red"], True),
])
def test_correction_contracts_preserve_types_and_allowed_values(value_type, value, allowed, valid):
    from backend.answer_key import _parse_correction

    definition = {"attribute_id": "Synthetic", "description": "", "value_type": value_type, "allowed_values": allowed}
    if valid:
        parsed = _parse_correction(value, "", definition)
        assert type(parsed) is {"boolean": bool, "integer": int, "string": str}[value_type]
    else:
        with pytest.raises(AnswerKeyError, match="must match"):
            _parse_correction(value, "", definition)


def test_context_sheet_is_not_imported_as_truth_and_unknown_sheet_is_rejected(scoring):
    service, snapshot = scoring
    rows = reviewed_rows(service, snapshot["id"])
    sheets = {
        "Review": [REVIEW_COLUMNS, *[[row[column] for column in REVIEW_COLUMNS] for row in rows]],
        "Candidates": [["Untrusted display"], ["Approved by machine"]],
    }
    version = service._ingest_bound(snapshot["id"], OWNER, write_workbook(sheets))
    assert service.score(snapshot["id"], OWNER, version["id"])["overall"]["reviewed_slots"] == 18
    sheets["Unknown"] = [["Header"], ["content"]]
    with pytest.raises(AnswerKeyError, match="Unknown worksheet"):
        service._ingest_bound(snapshot["id"], OWNER, write_workbook(sheets))


@pytest.mark.parametrize(("attribute", "valid"), [("Lead Free", True), ("Pressure", False)])
@pytest.mark.parametrize("public", [False, True])
def test_native_excel_boolean_correction_is_typed_not_numeric(scoring, attribute, valid, public):
    service, snapshot = scoring
    sheets = read_workbook(service.template(snapshot["id"], OWNER)) if public else {}
    rows = sheets["Review"] if public else sheet_rows(service, snapshot["id"])
    item_column = "Product ID" if public else "Item ID"
    index = next(index for index, row in enumerate(rows, 2) if row[item_column] == "011" and row["Attribute"] == attribute)
    rows[index - 2].update({"Decision": "Correct", "Reason": "Synthetic reference",
                          "Correction" if public else "Corrected value": "false",
                          "Correction unit" if public else "Corrected unit": "" if attribute == "Lead Free" else "PSI"})
    content = public_workbook(sheets) if public else workbook(rows)
    address = f"{'E' if public else 'L'}{index}"
    buffer = BytesIO()
    with ZipFile(BytesIO(content)) as source, ZipFile(buffer, "w", ZIP_DEFLATED) as target:
        for entry in source.infolist():
            raw = source.read(entry.filename)
            if entry.filename == "xl/worksheets/sheet1.xml":
                root = ET.fromstring(raw)
                cell = root.find(f".//{{{MAIN}}}c[@r='{address}']")
                assert cell is not None
                cell.clear()
                cell.attrib.update(r=address, t="b")
                ET.SubElement(cell, f"{{{MAIN}}}v").text = "0"
                raw = ET.tostring(root)
            target.writestr(entry, raw)
    ingest = service.ingest if public else service._ingest_bound
    if valid:
        version = ingest(snapshot["id"], OWNER, buffer.getvalue())
        assert next(iter(version["body"]["judgments"].values()))["value"] is False
    else:
        with pytest.raises(AnswerKeyError, match="Boolean"):
            ingest(snapshot["id"], OWNER, buffer.getvalue())


def test_public_package_has_server_only_bindings_and_roundtrips(scoring):
    service, snapshot = scoring
    content = service.template(snapshot["id"], OWNER)
    sheets = read_workbook(content)
    assert len(sheets["Review"]) == 24
    assert {"Product ID", "MPN", "Attribute", "Decision", "Correction", "Correction unit", "Reason"} <= set(sheets["Review"][0])
    assert not ({"Snapshot ID", "Item key", "Attempt key", "Result SHA256", "Candidate index"} & set(sheets["Review"][0]))
    assert all(not row["Decision"] for row in sheets["Review"])
    with ZipFile(BytesIO(content)) as archive:
        visible = b"\n".join(archive.read(name) for name in archive.namelist())
    for value in (snapshot["id"], OWNER, "results/synthetic-batch", "row-2", "synthetic-v1"):
        assert value.encode() not in visible
    before = copy.deepcopy(service.store.records)
    assert service.template(snapshot["id"], OWNER) == content
    assert service.store.records == before  # GET/export never writes metadata.
    reviewed = public_workbook(public_sheets(service, snapshot["id"]))
    version = service.ingest(snapshot["id"], OWNER, reviewed)
    assert version["body"]["workbook_sha256"] == hashlib.sha256(reviewed).hexdigest()
    assert version["body"]["package_id"] == service._package(snapshot["id"], OWNER)["id"]
    assert service.score(snapshot["id"], OWNER, version["id"])["overall"]["reviewed_slots"] == 18


@pytest.mark.parametrize(("column", "value"), [
    ("Product ID", "unknown"),
    ("MPN", "changed"),
    ("Attribute", "unknown"),
    ("Proposed value", "999"),
    ("Supporting quote", "Unverified replacement"),
    ("Evidence basis", "literal replacement"),
])
def test_public_package_cannot_rebind_or_edit_proposal_context(scoring, column, value):
    service, snapshot = scoring
    sheets = public_sheets(service, snapshot["id"])
    sheets["Review"][0][column] = value
    before = copy.deepcopy(service.store.records)
    with pytest.raises(AnswerKeyError, match="Review row 2"):
        service.ingest(snapshot["id"], OWNER, public_workbook(sheets))
    assert service.store.records == before


@pytest.mark.parametrize("mode", ["duplicate", "missing", "context", "sheet"])
def test_public_package_rejects_duplicates_missing_rows_and_context_changes(scoring, mode):
    service, snapshot = scoring
    sheets = public_sheets(service, snapshot["id"])
    if mode == "duplicate":
        sheets["Review"].append(sheets["Review"][0])
    elif mode == "missing":
        sheets["Review"].pop()
    elif mode == "context":
        first = sheets["Evidence"][0]
        first[next(iter(first))] = "changed"
    else:
        sheets["Unexpected"] = [{"Header": "value"}]
    with pytest.raises(AnswerKeyError):
        service.ingest(snapshot["id"], OWNER, public_workbook(sheets))
    assert not service.list_records(OWNER)["versions"]


def test_public_package_cannot_silently_score_a_different_attempt(scoring):
    service, original = scoring
    old_workbook = public_workbook(public_sheets(service, original["id"]))
    later = append_attempt(service, value=250)
    with pytest.raises(AnswerKeyError, match="(?i)read-only"):
        service.ingest(later["id"], OWNER, old_workbook)
    version = service.ingest(original["id"], OWNER, old_workbook)
    assert version["body"]["snapshot_id"] == original["id"]


def test_public_product_attribute_identity_must_be_unambiguous():
    service, batch_id = seed()
    batch, revision = read_json(service.store, f"batches/{batch_id}.json")
    duplicate = copy.deepcopy(batch["items"][-1])
    duplicate["item_key"] = "another-row"
    duplicate["manifest"]["product"]["vendor"] = "Other synthetic vendor"
    duplicate["manifest"]["product"]["mpn"] = "OTHER-MPN"
    batch["items"].append(duplicate)
    write_json(service.store, f"batches/{batch_id}.json", batch, revision)
    with pytest.raises(AnswerKeyError, match="Product ID"):
        service.snapshot(batch_id, OWNER)
    assert not service.list_records(OWNER)["snapshots"]


@pytest.mark.parametrize("column", ["Candidate index", "Snapshot ID", "Attempt key", "Receipt", "Session ID"])
def test_registration_rejects_technical_columns_even_when_hidden(column):
    service, batch_id = seed()
    snapshot = service.snapshot(batch_id, OWNER)
    sheets = read_workbook(synthetic_baseline(service, snapshot["id"]))
    for row in sheets["Review"]:
        row[column] = "private"
    raw = public_workbook(sheets)
    buffer = BytesIO()
    with ZipFile(BytesIO(raw)) as source, ZipFile(buffer, "w", ZIP_DEFLATED) as target:
        for entry in source.infolist():
            content = source.read(entry.filename)
            if entry.filename == "xl/worksheets/sheet1.xml":
                content = content.replace(b"<sheetData>", b'<cols><col min="16" max="16" hidden="1"/></cols><sheetData>', 1)
            target.writestr(entry, content)
    with pytest.raises(AnswerKeyError, match="technical"):
        service.register_baseline(snapshot["id"], OWNER, buffer.getvalue())
    assert not service.list_records(OWNER)["packages"]


@pytest.mark.parametrize("value", ["a" * 64, "/Users/operator/private/document.pdf", "batchblob://private/source",
                                  "https://synthetic.blob.core.windows.net/private/source.pdf"])
def test_registration_rejects_private_content(value):
    service, batch_id = seed()
    snapshot = service.snapshot(batch_id, OWNER)
    sheets = read_workbook(synthetic_baseline(service, snapshot["id"]))
    sheets["Review"][0]["Evidence"] = value
    with pytest.raises(AnswerKeyError, match="private"):
        service.register_baseline(snapshot["id"], OWNER, public_workbook(sheets))


@pytest.mark.parametrize("bindings", [
    {},
    [{"product_id": "unknown", "attribute_id": "Pressure", "candidate_index": 0}],
    [{"product_id": "002", "attribute_id": "Pressure", "candidate_index": 99}],
    [{"product_id": "002", "attribute_id": "Pressure", "candidate_index": True}],
    [{"product_id": "002", "attribute_id": "Pressure", "candidate_index": 0, "owner": "spoofed"}],
    [{"product_id": "002", "attribute_id": "Pressure", "candidate_index": 0}] * 2,
])
def test_private_baseline_candidate_bindings_are_validated(bindings):
    service, batch_id = seed()
    snapshot = service.snapshot(batch_id, OWNER)
    with pytest.raises(AnswerKeyError):
        service.register_baseline(snapshot["id"], OWNER, synthetic_baseline(service, snapshot["id"]), bindings)
    assert not service.list_records(OWNER)["packages"]


def test_registration_is_owner_scoped_immutable_and_never_imports_decisions(scoring):
    service, snapshot = scoring
    original = service._package(snapshot["id"], OWNER)
    sheets = public_sheets(service, snapshot["id"])
    with pytest.raises(AnswerKeyError, match="must be blank"):
        service.register_baseline(snapshot["id"], OWNER, public_workbook(sheets))
    with pytest.raises(Missing):
        service.register_baseline(snapshot["id"], "other", synthetic_baseline(service, snapshot["id"]))
    assert service._get("packages", original["id"], OWNER) == original
    assert not service.list_records(OWNER)["versions"]


def test_ambiguous_baseline_needs_explicit_package_selection_and_private_candidate_binding():
    service, batch_id = seed()
    snapshot = service.snapshot(batch_id, OWNER)
    baseline = synthetic_baseline(service, snapshot["id"])
    first = service.register_baseline(snapshot["id"], OWNER, baseline)
    assert service.register_baseline(snapshot["id"], OWNER, baseline) == first
    sheets = public_sheets(service, snapshot["id"])
    with pytest.raises(AnswerKeyError, match="privately bound"):
        service.ingest(snapshot["id"], OWNER, public_workbook(sheets), package_id=first["id"])
    second = service.register_baseline(snapshot["id"], OWNER, baseline, [
        {"product_id": "002", "attribute_id": "Pressure", "candidate_index": 0},
    ])
    with pytest.raises(AnswerKeyError, match="package_id"):
        service.template(snapshot["id"], OWNER)
    assert service.template(snapshot["id"], OWNER, first["id"]) == service.template(snapshot["id"], OWNER, second["id"])
    version = service.ingest(snapshot["id"], OWNER, public_workbook(sheets), package_id=second["id"])
    assert version["body"]["package_id"] == second["id"]
    assert all("readonly_row_sha256" in binding for binding in second["body"]["bindings"].values())


@pytest.mark.parametrize("updates", [
    {"Decision": "Autoapproved"},
    {"Reason": ""},
    {"Decision": "", "Reason": "Not reviewed"},
    {"Decision": "Correct", "Correction": ""},
    {"Decision": "Correct", "Correction": "maybe"},
    {"Decision": "Correct", "Correction": "false", "Correction unit": "PSI"},
])
def test_sanitized_upload_validates_human_decisions(scoring, updates):
    service, snapshot = scoring
    sheets = public_sheets(service, snapshot["id"])
    sheets["Review"][0].update(updates)
    with pytest.raises(AnswerKeyError):
        service.ingest(snapshot["id"], OWNER, public_workbook(sheets))
    assert not service.list_records(OWNER)["versions"]


def test_existing_reviewer_generator_layout_registers_and_scores_with_omissions_unreviewed():
    from backend.models.enrichment import EnrichmentResult
    from backend.reviewer_workbook import build_reviewer_package

    service, batch_id = seed()
    snapshot = service.snapshot(batch_id, OWNER)
    results = [
        EnrichmentResult.model_validate_json(service.store.read_bytes(key)[0])
        for key in service.store.keys(f"results/{batch_id}/")
    ]
    generated = build_reviewer_package(results)
    sheets = read_workbook(generated.workbook)
    assert len(sheets["Review"]) == 22  # The unprocessed twelfth product is not in this existing package.
    assert "Candidate index" not in sheets["Review"][0]
    registered = service.register_baseline(snapshot["id"], OWNER, generated.workbook)
    row = next(row for row in sheets["Review"] if row["Product ID"] == "000" and row["Attribute"] == "Pressure")
    row.update(Decision="Approve", Reason="Synthetic independent reviewer checked the literal proposal.")
    version = service.ingest(snapshot["id"], OWNER, public_workbook(sheets), package_id=registered["id"])
    metrics = service.score(snapshot["id"], OWNER, version["id"])["overall"]
    assert metrics["eligible_slots"] == 24 and metrics["reviewed_slots"] == 1
    assert metrics["unreviewed_slots"] == 23 and metrics["accuracy"] == 1
    assert metrics["correct_single_outputs"] == 1 and metrics["inferred_value_agreements"] == 0


def test_unregistered_snapshot_rows_cannot_be_added_to_a_subset_baseline():
    service, batch_id = seed()
    snapshot = service.snapshot(batch_id, OWNER)
    sheets = read_workbook(synthetic_baseline(service, snapshot["id"]))
    omitted = sheets["Review"].pop()
    registered = service.register_baseline(snapshot["id"], OWNER, public_workbook(sheets))
    omitted.update(Decision="Correct", Correction="123", Reason="Cannot add an unregistered row")
    sheets["Review"].append(omitted)
    with pytest.raises(AnswerKeyError, match="unknown"):
        service.ingest(snapshot["id"], OWNER, public_workbook(sheets), package_id=registered["id"])


def test_customer_rejection_judges_only_the_privately_bound_candidate(scoring):
    service, snapshot = scoring
    sheets = read_workbook(service.template(snapshot["id"], OWNER))
    row = next(row for row in sheets["Review"] if row["Product ID"] == "002" and row["Attribute"] == "Pressure")
    row.update(Decision="Reject", Reason="The displayed candidate is not acceptable.")
    version = service.ingest(snapshot["id"], OWNER, public_workbook(sheets))
    judgment = next(iter(version["body"]["judgments"].values()))
    assert judgment["decision"] == "Reject" and judgment["candidate_index"] == 0
    score = service.score(snapshot["id"], OWNER, version["id"])
    assert score["overall"]["reviewed_slots"] == 1
    assert score["overall"]["judged_candidates"] == 1
    assert score["overall"]["unjudged_candidates"] == 20
    assert score["by_attribute"]["Pressure"]["candidate_count"] == 10
    assert score["by_attribute"]["Pressure"]["judged_candidates"] == 1
    assert score["by_attribute"]["Pressure"]["unjudged_candidates"] == 9
    assert score["overall"]["accuracy"] is None  # Rejecting a conflict row is not a single-output judgment.


@pytest.mark.parametrize("product_id", ["002", "011"])
def test_row_rejection_without_a_candidate_binding_never_rejects_alternatives(product_id):
    service, batch_id = seed()
    snapshot = service.snapshot(batch_id, OWNER)
    package = service.register_baseline(snapshot["id"], OWNER, synthetic_baseline(service, snapshot["id"]))
    sheets = read_workbook(service.template(snapshot["id"], OWNER))
    row = next(row for row in sheets["Review"] if row["Product ID"] == product_id and row["Attribute"] == "Pressure")
    row.update(Decision="Reject", Reason="Reject this row; no privately selected proposal was supplied.")
    version = service.ingest(snapshot["id"], OWNER, public_workbook(sheets), package_id=package["id"])
    judgment = next(iter(version["body"]["judgments"].values()))
    assert judgment["decision"] == "Reject" and judgment["candidate_index"] is None
    metrics = service.score(snapshot["id"], OWNER, version["id"])["overall"]
    assert metrics["reviewed_slots"] == metrics["rejected_slots"] == 1
    assert metrics["judged_candidates"] == 0 and metrics["unjudged_candidates"] == 21
    assert metrics["accuracy"] is None and metrics["candidate_accuracy"] is None


@pytest.mark.parametrize(("decision", "correction", "matches"), [
    ("Approve", "", 1),
    ("Correct", "true", 1),
    ("Correct", "false", 0),
])
def test_explicit_human_judgment_scores_inferred_value_without_auto_credit_or_certification(scoring, decision, correction, matches):
    service, snapshot = scoring
    unreviewed = service.score(snapshot["id"], OWNER)
    for metrics in (
        unreviewed["overall"], unreviewed["by_tier"]["internal_pdf"],
        unreviewed["by_evidence_basis"]["inferred_from_description"],
    ):
        assert metrics["judged_candidates"] == metrics["correct_candidates"] == 0
        assert metrics["candidate_accuracy"] is None
    sheets = read_workbook(service.template(snapshot["id"], OWNER))
    row = next(row for row in sheets["Review"] if row["Product ID"] == "000" and row["Attribute"] == "Lead Free")
    row.update(Decision=decision, Correction=correction, Reason="An explicit synthetic human value judgment, not certification.")
    version = service.ingest(snapshot["id"], OWNER, public_workbook(sheets))
    score = service.score(snapshot["id"], OWNER, version["id"])
    for metrics in (
        score["overall"], score["by_tier"]["internal_pdf"],
        score["by_evidence_basis"]["inferred_from_description"],
    ):
        assert metrics["judged_candidates"] == 1
        assert metrics["correct_candidates"] == matches
        assert metrics["candidate_accuracy"] == matches / 1
        assert metrics["scorable_single_outputs"] == 1
        assert metrics["correct_single_outputs"] == matches
        assert metrics["accuracy"] == matches / 1
    assert score["overall"]["reviewed_slots"] == 1 and score["overall"]["unreviewed_slots"] == 23
    assert score["by_evidence_basis"]["literal"]["candidate_accuracy"] is None
    assert score["by_evidence_basis"]["inferred_from_description"]["reviewed_inferred_candidates"] == 1
    assert score["by_evidence_basis"]["inferred_from_description"]["inferred_value_agreements"] == matches
    original = next(slot for slot in service.get_snapshot(snapshot["id"], OWNER)["body"]["slots"]
                    if slot["product"]["item_id"] == "000" and slot["attribute_id"] == "Lead Free")
    assert original["candidates"][0]["evidence_basis"] == "inferred_from_description"
