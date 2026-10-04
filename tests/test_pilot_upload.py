"""Synthetic offline copies only: never customer files, credentials, or services."""

import copy
import hashlib
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from backend.batch import SourceBinding
from backend.batch_store import Conflict, Missing, SQLiteStore, read_json, write_json
from backend.pilot_upload import (
    APPROVAL_KEY,
    MAX_APPROVAL_BYTES,
    MAX_DOCUMENT_BYTES,
    MAX_TOTAL_BYTES,
    REGISTRY_KEY,
    SESSION_KEY,
    PilotSourceUpload,
    UploadOutcomeUnknown,
    registry_sha256,
    validate_upload_approval,
)


TENANT = "11111111-1111-4111-8111-111111111111"
OPERATOR = "22222222-2222-4222-8222-222222222222"
OWNER = f"{TENANT}/{OPERATOR}"
PDF = b"%PDF-1.7\nSynthetic upload contract bytes only.\n%%EOF"
XLSX = b"PK\x03\x04Synthetic opaque XLSX upload contract bytes only."
PRODUCT = {"item_id": "SYNTHETIC-001", "vendor": "Synthetic", "mpn": "PART-001", "hierarchy_node": "Synthetic"}


class RecordingStore(SQLiteStore):
    """The real SQLite CAS protocol, with typed observation/fault injection."""

    def __init__(self, home: Path) -> None:
        super().__init__(home)
        self.writes: list[tuple[str, str | None]] = []
        self.before_write: Callable[[str, bytes, str | None], None] | None = None
        self.fail_after_key: str | None = None

    def read_bytes(self, key: str, max_bytes: int = 64 * 1024 * 1024) -> tuple[bytes, str]:
        return super().read_bytes(key, max_bytes)

    def write_bytes(self, key: str, value: bytes, version: str | None = None) -> str:
        if self.before_write is not None:
            self.before_write(key, value, version)
        token = super().write_bytes(key, value, version)
        self.writes.append((key, version))
        if key == self.fail_after_key:
            self.fail_after_key = None
            raise OSError("Synthetic secret provider diagnostic must not escape")
        return token


@dataclass
class Pilot:
    store: RecordingStore
    service: PilotSourceUpload
    approval: dict[str, Any]
    legacy: dict[str, Any]


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Pilot upload tests cannot contact services")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.delenv("DOCINTEL_REAL_PILOT_ENABLED", raising=False)
    monkeypatch.delenv("DOCINTEL_PILOT_UPLOAD_ENABLED", raising=False)
    monkeypatch.delenv("DOCINTEL_REAL_PILOT_OPERATOR_IDS", raising=False)


def blob(source_id: str, filename: str, content: bytes, *, xlsx: bool = False) -> dict[str, Any]:
    return {
        "reference": filename, "source_id": source_id, "kind": "blob",
        "products": [copy.deepcopy(PRODUCT)],
        "blob": f"documents/{filename}", "sha256": hashlib.sha256(content).hexdigest(),
        "format": "xlsx" if xlsx else "pdf",
        "source_tier": "vendor_table" if xlsx else "internal_pdf",
    }


def replace(store: RecordingStore, key: str, value: dict[str, Any]) -> None:
    _, version = read_json(store, key)
    write_json(store, key, value, version)


@pytest.fixture
def pilot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Pilot:
    # pytest owns its default external directory; SQLite refuses repo-local data.
    store = RecordingStore(tmp_path / "private-upload")
    legacy = {"sources": [blob("legacy-synthetic", "legacy.pdf", b"synthetic legacy")], "legacy_note": "preserve exactly"}
    sources = [blob("manual", "manual.pdf", PDF), blob("vendor", "vendor.xlsx", XLSX, xlsx=True)]
    sources.extend([
        {
            "source_id": "website", "reference": "synthetic-web", "kind": "web",
            "format": "web", "source_tier": "manufacturer_web",
            "url": "https://synthetic.invalid/product", "products": [copy.deepcopy(PRODUCT)],
        },
        {
            "source_id": "blocked-sharepoint", "reference": "synthetic-sharepoint",
            "kind": "sharepoint", "products": [copy.deepcopy(PRODUCT)],
        },
    ])
    approval = {
        "schema_version": 1, "approved": True,
        "id": "33333333-3333-4333-8333-333333333333",
        "approved_by": OPERATOR, "owner": OWNER,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "expected_registry_sha256": registry_sha256(legacy["sources"]),
        "sources": sources,
        "documents": [
            {"source_id": "manual", "filename": "manual.pdf", "bytes": len(PDF), "sha256": hashlib.sha256(PDF).hexdigest()},
            {"source_id": "vendor", "filename": "vendor.xlsx", "bytes": len(XLSX), "sha256": hashlib.sha256(XLSX).hexdigest()},
        ],
    }
    write_json(store, REGISTRY_KEY, legacy)
    write_json(store, APPROVAL_KEY, approval)
    store.writes.clear()
    monkeypatch.setenv("DOCINTEL_PILOT_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_OPERATOR_IDS", OPERATOR)
    return Pilot(store, PilotSourceUpload(store), approval, legacy)


def upload_all(pilot: Pilot) -> None:
    pilot.service.upload("manual", PDF, OWNER)
    pilot.service.upload("vendor", XLSX, OWNER)


@pytest.mark.parametrize("enabled", [None, "", "false", "TRUE", "1"])
def test_default_off(pilot: Pilot, monkeypatch: pytest.MonkeyPatch, enabled: str | None) -> None:
    if enabled is None:
        monkeypatch.delenv("DOCINTEL_PILOT_UPLOAD_ENABLED")
    else:
        monkeypatch.setenv("DOCINTEL_PILOT_UPLOAD_ENABLED", enabled)
    assert pilot.service.catalog(OWNER) is None
    with pytest.raises(ValueError, match="unavailable"):
        pilot.service.upload("manual", PDF, OWNER)
    with pytest.raises(ValueError, match="unavailable"):
        pilot.service.finalize(OWNER)
    assert not pilot.store.writes


def test_missing_approval_and_checks_never_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCINTEL_PILOT_UPLOAD_ENABLED", "true")
    store = RecordingStore(tmp_path / "unapproved")
    service = PilotSourceUpload(store)
    assert service.catalog(OWNER) is None
    with pytest.raises(ValueError, match="unavailable"):
        service.upload("manual", PDF, OWNER)
    assert not store.writes


@pytest.mark.parametrize("actor", [
    "development:local-unverified", "", "verified:true",
    f"{TENANT}/44444444-4444-4444-8444-444444444444",
    f"44444444-4444-4444-8444-444444444444/{OPERATOR}",
])
def test_wrong_or_unverified_owner_is_hidden(pilot: Pilot, actor: str) -> None:
    assert pilot.service.catalog(actor) is None
    for action in (lambda: pilot.service.upload("manual", PDF, actor), lambda: pilot.service.finalize(actor)):
        with pytest.raises(Missing, match="unavailable"):
            action()
    assert not pilot.store.writes


@pytest.mark.parametrize("operators", ["", "44444444-4444-4444-8444-444444444444", OPERATOR.upper() + "x"])
def test_operator_allowlist_required(pilot: Pilot, monkeypatch: pytest.MonkeyPatch, operators: str) -> None:
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_OPERATOR_IDS", operators)
    with pytest.raises(ValueError, match="unavailable"):
        pilot.service.upload("manual", PDF, OWNER)
    assert not pilot.store.writes


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"schema_version": "1"}, {"approved": 1}, {"approved": False},
    {"id": "not-a-uuid"}, {"approved_by": "44444444-4444-4444-8444-444444444444"},
    {"expires_at": "2020-01-01T00:00:00+00:00"}, {"expires_at": "2099-01-01T00:00:00"},
    {"expires_at": "not-a-date"}, {"extra": "secret"}, {"sources": []}, {"documents": []},
    {"expected_registry_sha256": "bad"},
    {"input_hashes": {"manifest": "a" * 64}}, {"input_hashes": {"manifest": "a" * 64, "attributes": "bad"}},
])
def test_invalid_expired_or_unapproved_metadata(pilot: Pilot, change: dict[str, Any]) -> None:
    pilot.approval.update(change)
    replace(pilot.store, APPROVAL_KEY, pilot.approval)
    pilot.store.writes.clear()
    with pytest.raises(ValueError, match="unavailable"):
        pilot.service.upload("manual", PDF, OWNER)
    assert not pilot.store.writes


@pytest.mark.parametrize("field,value", [
    ("bytes", True), ("bytes", "40"), ("bytes", 0), ("bytes", MAX_DOCUMENT_BYTES + 1),
    ("filename", "../manual.pdf"), ("filename", "path/manual.pdf"), ("filename", "manual.exe"),
    ("sha256", "a" * 64), ("extra", "secret"), ("source_id", "website"),
])
def test_document_schema_is_strict(pilot: Pilot, field: str, value: Any) -> None:
    pilot.approval["documents"][0][field] = value
    replace(pilot.store, APPROVAL_KEY, pilot.approval)
    pilot.store.writes.clear()
    with pytest.raises(ValueError, match="unavailable"):
        pilot.service.catalog(OWNER)
    assert not pilot.store.writes


@pytest.mark.parametrize("key", [
    "other/manual.pdf", "documents/../manual.pdf", "documents/a/../../manual.pdf",
    "documents/a/manual.pdf", "documents/a\\manual.pdf", "documents/%2e%2e.pdf",
    "documents//manual.pdf", "documents/manual.pdf?query", "documents/manual.exe",
])
def test_no_arbitrary_destinations(pilot: Pilot, key: str) -> None:
    pilot.approval["sources"][0]["blob"] = key
    replace(pilot.store, APPROVAL_KEY, pilot.approval)
    pilot.store.writes.clear()
    with pytest.raises(ValueError, match="unavailable"):
        pilot.service.upload("manual", PDF, OWNER)
    assert not pilot.store.writes


@pytest.mark.parametrize("mutation", ["total", "sources", "documents", "products", "web", "duplicate-id", "duplicate-reference", "duplicate-key", "invalid-binding"])
def test_upfront_finite_scope(pilot: Pilot, mutation: str) -> None:
    if mutation == "total":
        pilot.approval["documents"][0]["bytes"] = MAX_TOTAL_BYTES
    elif mutation == "sources":
        pilot.approval["sources"] *= 2
    elif mutation == "documents":
        pilot.approval["documents"] *= 3
    elif mutation == "products":
        pilot.approval["sources"][0]["products"] = [{**PRODUCT, "item_id": f"SYNTHETIC-{index}"} for index in range(5)]
    elif mutation == "web":
        for index in range(2):
            pilot.approval["sources"].append({**pilot.approval["sources"][2], "source_id": f"web-{index}", "reference": f"web-{index}"})
    elif mutation == "duplicate-id":
        pilot.approval["sources"][1]["source_id"] = "manual"
    elif mutation == "duplicate-reference":
        pilot.approval["sources"][1]["reference"] = "manual.pdf"
    elif mutation == "duplicate-key":
        pilot.approval["sources"][1]["blob"] = "documents/manual.pdf"
    else:
        pilot.approval["sources"][0]["products"] = []
    replace(pilot.store, APPROVAL_KEY, pilot.approval)
    pilot.store.writes.clear()
    with pytest.raises(ValueError, match="unavailable"):
        pilot.service.catalog(OWNER)
    assert not pilot.store.writes


@pytest.mark.parametrize("source_id", ["unknown", "website", "blocked-sharepoint", "../manual", "manual.pdf", "finalize"])
def test_unknown_and_non_blob_uploads_are_missing(pilot: Pilot, source_id: str) -> None:
    with pytest.raises(Missing, match="unavailable"):
        pilot.service.upload(source_id, PDF, OWNER)
    assert not pilot.store.writes


@pytest.mark.parametrize("content", [b"", PDF + b"x", b"x" * len(PDF)])
def test_size_and_hash_must_match_before_any_write(pilot: Pilot, content: bytes) -> None:
    with pytest.raises(ValueError, match="approved bytes"):
        pilot.service.upload("manual", content, OWNER)
    assert not pilot.store.writes


def test_catalog_safe_metadata_and_no_check_writes(pilot: Pilot) -> None:
    expected = [{**doc, "status": "pending"} for doc in pilot.approval["documents"]]
    assert pilot.service.catalog(OWNER) == expected
    assert not pilot.store.writes
    first = pilot.service.upload("manual", PDF, OWNER)
    assert first == {**expected[0], "status": "uploaded"}
    assert pilot.store.writes == [(SESSION_KEY, None), ("documents/manual.pdf", None)]
    before = list(pilot.store.writes)
    assert pilot.service.catalog(OWNER) == [{**expected[0], "status": "uploaded"}, expected[1]]
    assert pilot.store.writes == before


def test_duplicate_retries_and_restart_are_immutable(pilot: Pilot) -> None:
    first = pilot.service.upload("manual", PDF, OWNER)
    before = list(pilot.store.writes)
    restarted = PilotSourceUpload(pilot.store)
    assert restarted.upload("manual", PDF, OWNER) == {**first, "status": "existing"}
    assert pilot.store.writes == before
    assert pilot.store.read_bytes("documents/manual.pdf")[0] == PDF
    assert read_json(pilot.store, REGISTRY_KEY)[0] == pilot.legacy


def test_concurrent_duplicate_uploads(pilot: Pilot) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: PilotSourceUpload(pilot.store).upload("manual", PDF, OWNER), range(2)))
    assert {result["status"] for result in results} == {"uploaded", "existing"}
    assert pilot.store.writes.count((SESSION_KEY, None)) == 1
    assert pilot.store.writes.count(("documents/manual.pdf", None)) == 1


def test_existing_mismatched_bytes_never_overwritten(pilot: Pilot) -> None:
    pilot.store.write_bytes("documents/manual.pdf", b"different")
    before = list(pilot.store.writes)
    with pytest.raises(Conflict):
        pilot.service.upload("manual", PDF, OWNER)
    assert pilot.store.read_bytes("documents/manual.pdf")[0] == b"different"
    catalog = pilot.service.catalog(OWNER)
    assert catalog is not None and catalog[0]["status"] == "conflict"
    assert pilot.store.writes == before


def test_existing_identical_document_still_pins_session(pilot: Pilot) -> None:
    pilot.store.write_bytes("documents/manual.pdf", PDF)
    pilot.store.writes.clear()
    assert pilot.service.upload("manual", PDF, OWNER)["status"] == "existing"
    assert pilot.store.writes == [(SESSION_KEY, None)]


def test_incomplete_finalize_does_not_write(pilot: Pilot) -> None:
    with pytest.raises(Conflict, match="not all available"):
        pilot.service.finalize(OWNER)
    assert not pilot.store.writes
    pilot.service.upload("manual", PDF, OWNER)
    before = list(pilot.store.writes)
    with pytest.raises(Conflict, match="not all available"):
        pilot.service.finalize(OWNER)
    assert pilot.store.writes == before
    assert read_json(pilot.store, REGISTRY_KEY)[0] == pilot.legacy


def test_finalize_checks_actual_bytes_not_prior_success(pilot: Pilot) -> None:
    upload_all(pilot)
    _, version = pilot.store.read_bytes("documents/manual.pdf")
    pilot.store.write_bytes("documents/manual.pdf", b"tampered", version)
    before = list(pilot.store.writes)
    with pytest.raises(Conflict, match="not all available"):
        pilot.service.finalize(OWNER)
    assert pilot.store.writes == before
    assert read_json(pilot.store, REGISTRY_KEY)[0] == pilot.legacy


def test_finalize_preserves_legacy_registry_and_supports_retry(pilot: Pilot) -> None:
    upload_all(pilot)
    original_version = pilot.store.read_bytes(REGISTRY_KEY)[1]
    summary = pilot.service.finalize(OWNER)
    registry, version = read_json(pilot.store, REGISTRY_KEY)
    assert registry["legacy_note"] == pilot.legacy["legacy_note"]
    assert registry["sources"][0] == pilot.legacy["sources"][0]
    assert registry["sources"][1:] == [
        {**SourceBinding.model_validate(source).model_dump(mode="json"), "owner": OWNER}
        for source in pilot.approval["sources"]
    ]
    assert all(source["owner"] == OWNER for source in registry["sources"][1:])
    assert "owner" not in registry["sources"][0]
    assert summary == {
        "status": "ready", "documents": [{**doc, "status": "uploaded"} for doc in pilot.approval["documents"]],
        "document_count": 2, "total_bytes": len(PDF) + len(XLSX), "registry_sha256": registry_sha256(registry["sources"]),
    }
    assert (REGISTRY_KEY, original_version) in pilot.store.writes
    before = list(pilot.store.writes)
    assert PilotSourceUpload(pilot.store).finalize(OWNER) == summary
    assert pilot.store.writes == before
    assert pilot.store.read_bytes(REGISTRY_KEY)[1] == version


@pytest.mark.parametrize("stage", ["before", "uploaded", "finalized"])
def test_registry_drift_cannot_mint_a_different_merge(pilot: Pilot, stage: str) -> None:
    if stage != "before":
        upload_all(pilot)
    if stage == "finalized":
        pilot.service.finalize(OWNER)
    registry, _ = read_json(pilot.store, REGISTRY_KEY)
    registry["sources"].append(blob("unexpected", "unexpected.pdf", b"unexpected synthetic"))
    replace(pilot.store, REGISTRY_KEY, registry)
    before = list(pilot.store.writes)
    with pytest.raises(Conflict):
        pilot.service.upload("manual", PDF, OWNER)
    with pytest.raises(Conflict):
        pilot.service.finalize(OWNER)
    assert pilot.store.writes == before


@pytest.mark.parametrize("field", ["id", "expires_at", "input_hashes", "documents", "expected_registry_sha256"])
def test_approval_drift_is_pinned_across_instances(pilot: Pilot, field: str) -> None:
    pilot.service.upload("manual", PDF, OWNER)
    if field == "id":
        pilot.approval[field] = "44444444-4444-4444-8444-444444444444"
    elif field == "expires_at":
        pilot.approval[field] = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    elif field == "input_hashes":
        pilot.approval[field] = {"manifest": "a" * 64, "attributes": "b" * 64}
    elif field == "documents":
        pilot.approval[field][0]["filename"] = "renamed.pdf"
    else:
        pilot.approval[field] = "f" * 64
    replace(pilot.store, APPROVAL_KEY, pilot.approval)
    before = list(pilot.store.writes)
    with pytest.raises(Conflict):
        PilotSourceUpload(pilot.store).upload("vendor", XLSX, OWNER)
    with pytest.raises(Conflict):
        pilot.service.finalize(OWNER)
    assert pilot.store.writes == before
    with pytest.raises(Missing):
        pilot.store.read_bytes("documents/vendor.xlsx")


def test_approval_drift_during_session_pin_prevents_document_write(pilot: Pilot) -> None:
    def mutate(key: str, value: bytes, version: str | None) -> None:
        if key == SESSION_KEY:
            pilot.store.before_write = None
            pilot.approval["id"] = "44444444-4444-4444-8444-444444444444"
            replace(pilot.store, APPROVAL_KEY, pilot.approval)

    pilot.store.before_write = mutate
    with pytest.raises(Conflict):
        pilot.service.upload("manual", PDF, OWNER)
    with pytest.raises(Missing):
        pilot.store.read_bytes("documents/manual.pdf")


@pytest.mark.parametrize("collision", ["source_id", "reference"])
def test_conflicting_legacy_binding_is_rejected(pilot: Pilot, collision: str) -> None:
    pilot.legacy["sources"][0][collision] = pilot.approval["sources"][0][collision]
    replace(pilot.store, REGISTRY_KEY, pilot.legacy)
    pilot.approval["expected_registry_sha256"] = registry_sha256(pilot.legacy["sources"])
    replace(pilot.store, APPROVAL_KEY, pilot.approval)
    before = list(pilot.store.writes)
    with pytest.raises(Conflict):
        pilot.service.upload("manual", PDF, OWNER)
    assert pilot.store.writes == before


def test_exact_normalized_binding_is_not_duplicated(pilot: Pilot) -> None:
    pilot.legacy["sources"].append(copy.deepcopy(pilot.approval["sources"][0]))
    pilot.legacy["sources"][1]["enabled"] = 0
    pilot.legacy["sources"][1]["owner"] = OWNER
    replace(pilot.store, REGISTRY_KEY, pilot.legacy)
    pilot.approval["expected_registry_sha256"] = registry_sha256(pilot.legacy["sources"])
    replace(pilot.store, APPROVAL_KEY, pilot.approval)
    upload_all(pilot)
    pilot.service.finalize(OWNER)
    registry, _ = read_json(pilot.store, REGISTRY_KEY)
    assert len(registry["sources"]) == 5
    assert registry["sources"][1] == {**pilot.approval["sources"][0], "enabled": 0, "owner": OWNER}


def test_canonical_registry_and_approval_ignore_json_formatting(pilot: Pilot) -> None:
    expanded = [SourceBinding.model_validate(source).model_dump(mode="json") for source in pilot.legacy["sources"]]
    assert registry_sha256(expanded) == pilot.approval["expected_registry_sha256"]
    upload_all(pilot)
    _, version = pilot.store.read_bytes(APPROVAL_KEY)
    pilot.store.write_bytes(APPROVAL_KEY, json.dumps(pilot.approval, sort_keys=True, indent=2).encode(), version)
    assert pilot.service.finalize(OWNER)["status"] == "ready"
    registry, version = read_json(pilot.store, REGISTRY_KEY)
    registry["sources"].reverse()
    write_json(pilot.store, REGISTRY_KEY, registry, version)
    before = list(pilot.store.writes)
    assert pilot.service.finalize(OWNER)["status"] == "ready"
    assert pilot.store.writes == before


def test_finalize_cas_prevents_overwriting_concurrent_registry_change(pilot: Pilot) -> None:
    upload_all(pilot)

    def mutate(key: str, value: bytes, version: str | None) -> None:
        if key == REGISTRY_KEY:
            pilot.store.before_write = None
            registry, _ = read_json(pilot.store, REGISTRY_KEY)
            registry["sources"].append(blob("concurrent", "concurrent.pdf", b"concurrent"))
            replace(pilot.store, REGISTRY_KEY, registry)

    pilot.store.before_write = mutate
    with pytest.raises(Conflict, match="Registry changed"):
        pilot.service.finalize(OWNER)
    registry, _ = read_json(pilot.store, REGISTRY_KEY)
    assert registry["sources"][-1]["source_id"] == "concurrent"
    assert len(registry["sources"]) == 2


@pytest.mark.parametrize("key", [SESSION_KEY, "documents/manual.pdf", REGISTRY_KEY])
def test_unknown_write_outcome_explicit_and_identical_retry_safe(pilot: Pilot, key: str) -> None:
    if key == REGISTRY_KEY:
        upload_all(pilot)
    pilot.store.fail_after_key = key
    action = (lambda: pilot.service.finalize(OWNER)) if key == REGISTRY_KEY else (lambda: pilot.service.upload("manual", PDF, OWNER))
    with pytest.raises(UploadOutcomeUnknown, match="outcome is unknown") as error:
        action()
    assert "secret" not in str(error.value)
    assert action()["status"] in {"uploaded", "existing", "ready"}
    if key == SESSION_KEY:
        assert pilot.store.writes.count((SESSION_KEY, None)) == 1
    if key == "documents/manual.pdf":
        assert pilot.store.writes.count((key, None)) == 1


def test_unexpected_programming_errors_are_not_swallowed(pilot: Pilot) -> None:
    def fail(key: str, value: bytes, version: str | None) -> None:
        raise RuntimeError("synthetic programming failure")

    pilot.store.before_write = fail
    with pytest.raises(RuntimeError, match="programming failure"):
        pilot.service.upload("manual", PDF, OWNER)


def test_malformed_approval_error_does_not_expose_raw_metadata(pilot: Pilot) -> None:
    _, version = pilot.store.read_bytes(APPROVAL_KEY)
    pilot.store.write_bytes(APPROVAL_KEY, b'{"owner": "secret-not-json', version)
    before = list(pilot.store.writes)
    with pytest.raises(ValueError, match="^Pilot source upload is unavailable$"):
        pilot.service.catalog(OWNER)
    assert pilot.store.writes == before


def test_approval_optional_intake_hashes_and_exact_byte_boundary(pilot: Pilot) -> None:
    pilot.approval["input_hashes"] = {"manifest": "a" * 64, "attributes": "b" * 64}
    pilot.approval["documents"][0]["bytes"] = MAX_TOTAL_BYTES - len(XLSX)
    replace(pilot.store, APPROVAL_KEY, pilot.approval)
    before = list(pilot.store.writes)
    catalog = pilot.service.catalog(OWNER)
    assert catalog is not None and sum(doc["bytes"] for doc in catalog) == MAX_TOTAL_BYTES
    assert pilot.store.writes == before


def test_approved_binding_does_not_coerce_types(pilot: Pilot) -> None:
    pilot.approval["sources"][0]["enabled"] = 0
    replace(pilot.store, APPROVAL_KEY, pilot.approval)
    before = list(pilot.store.writes)
    with pytest.raises(ValueError, match="unavailable"):
        pilot.service.catalog(OWNER)
    assert pilot.store.writes == before


def test_approval_expiry_is_rechecked_after_upload(pilot: Pilot) -> None:
    upload_all(pilot)
    pilot.approval["expires_at"] = "2020-01-01T00:00:00+00:00"
    replace(pilot.store, APPROVAL_KEY, pilot.approval)
    before = list(pilot.store.writes)
    with pytest.raises(ValueError, match="unavailable"):
        pilot.service.finalize(OWNER)
    assert pilot.store.writes == before


def test_absent_registry_can_be_created_conditionally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCINTEL_PILOT_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_OPERATOR_IDS", OPERATOR)
    store = RecordingStore(tmp_path / "empty-registry")
    write_json(store, APPROVAL_KEY, {
        "schema_version": 1, "approved": True,
        "id": "33333333-3333-4333-8333-333333333333",
        "approved_by": OPERATOR, "owner": OWNER,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "expected_registry_sha256": registry_sha256([]),
        "sources": [blob("manual", "manual.pdf", PDF)],
        "documents": [{"source_id": "manual", "filename": "manual.pdf", "bytes": len(PDF), "sha256": hashlib.sha256(PDF).hexdigest()}],
    })
    service = PilotSourceUpload(store)
    service.upload("manual", PDF, OWNER)
    assert service.finalize(OWNER)["status"] == "ready"
    assert (REGISTRY_KEY, None) in store.writes


def test_conflict_race_cannot_overwrite_different_document(pilot: Pilot) -> None:
    def race(key: str, value: bytes, version: str | None) -> None:
        if key == "documents/manual.pdf":
            pilot.store.before_write = None
            pilot.store.write_bytes(key, b"concurrent different bytes")

    pilot.store.before_write = race
    with pytest.raises(Conflict):
        pilot.service.upload("manual", PDF, OWNER)
    assert pilot.store.read_bytes("documents/manual.pdf")[0] == b"concurrent different bytes"


def test_finalize_source_id_is_reserved_in_approval(pilot: Pilot) -> None:
    pilot.approval["sources"][0]["source_id"] = "finalize"
    pilot.approval["documents"][0]["source_id"] = "finalize"
    replace(pilot.store, APPROVAL_KEY, pilot.approval)
    before = list(pilot.store.writes)
    with pytest.raises(ValueError, match="unavailable"):
        pilot.service.catalog(OWNER)
    assert pilot.store.writes == before


def test_approval_read_is_bounded_to_small_metadata(pilot: Pilot) -> None:
    pilot.approval["sources"][0]["reference"] = "x" * MAX_APPROVAL_BYTES
    replace(pilot.store, APPROVAL_KEY, pilot.approval)
    before = list(pilot.store.writes)
    with pytest.raises(ValueError, match="^Pilot source upload is unavailable$"):
        pilot.service.catalog(OWNER)
    assert pilot.store.writes == before


def test_processing_enablement_does_not_enable_uploads(pilot: Pilot, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCINTEL_PILOT_UPLOAD_ENABLED")
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_ENABLED", "true")
    assert pilot.service.catalog(OWNER) is None
    with pytest.raises(ValueError, match="unavailable"):
        pilot.service.upload("manual", PDF, OWNER)
    assert not pilot.store.writes


def test_upload_and_finalize_work_while_processing_is_disabled(pilot: Pilot, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_ENABLED", "false")
    upload_all(pilot)
    assert pilot.service.finalize(OWNER)["status"] == "ready"


def test_standalone_approval_validation_needs_no_enabled_flags_or_allowlist(pilot: Pilot, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCINTEL_PILOT_UPLOAD_ENABLED")
    monkeypatch.delenv("DOCINTEL_REAL_PILOT_OPERATOR_IDS")
    before = list(pilot.store.writes)
    validated = validate_upload_approval(pilot.approval, owner=OWNER)
    assert validated == pilot.approval
    assert validated is not pilot.approval
    validated["documents"][0]["filename"] = "detached.pdf"
    assert pilot.approval["documents"][0]["filename"] == "manual.pdf"
    assert pilot.store.writes == before


def test_standalone_validation_rejects_owner_mismatch(pilot: Pilot) -> None:
    with pytest.raises(ValueError, match="unavailable"):
        validate_upload_approval(pilot.approval, owner="development:local-unverified")


@pytest.mark.parametrize("change", [
    {"expires_at": "2020-01-01T00:00:00+00:00"},
    {"schema_version": True},
    {"api_principal_id": "44444444-4444-4444-8444-444444444444"},
])
def test_standalone_validation_rejects_expiry_and_schema_drift(pilot: Pilot, change: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="unavailable"):
        validate_upload_approval({**pilot.approval, **change})


def test_standalone_approval_validation_enforces_metadata_bound(pilot: Pilot) -> None:
    pilot.approval["sources"][0]["reference"] = "x" * MAX_APPROVAL_BYTES
    with pytest.raises(ValueError, match="unavailable"):
        validate_upload_approval(pilot.approval)


@pytest.mark.parametrize("index", [0, 1, 2, 3])
def test_explicit_conflicting_source_owner_is_rejected(pilot: Pilot, index: int) -> None:
    pilot.approval["sources"][index]["owner"] = f"{TENANT}/44444444-4444-4444-8444-444444444444"
    replace(pilot.store, APPROVAL_KEY, pilot.approval)
    before = list(pilot.store.writes)
    with pytest.raises(ValueError, match="unavailable"):
        validate_upload_approval(pilot.approval)
    with pytest.raises(ValueError, match="unavailable"):
        pilot.service.upload("manual", PDF, OWNER)
    assert pilot.store.writes == before


@pytest.mark.parametrize("source_owner", [None, OWNER])
def test_source_owner_is_derived_or_exact_and_persisted(pilot: Pilot, source_owner: str | None) -> None:
    for source in pilot.approval["sources"]:
        source["owner"] = source_owner
    replace(pilot.store, APPROVAL_KEY, pilot.approval)
    upload_all(pilot)
    pilot.service.finalize(OWNER)
    registry, _ = read_json(pilot.store, REGISTRY_KEY)
    assert all(source["owner"] == OWNER for source in registry["sources"][1:])
    assert registry["sources"][0] == pilot.legacy["sources"][0]


def test_matching_global_source_is_not_reused_as_a_private_upload(pilot: Pilot) -> None:
    pilot.legacy["sources"].append(copy.deepcopy(pilot.approval["sources"][0]))
    replace(pilot.store, REGISTRY_KEY, pilot.legacy)
    pilot.approval["expected_registry_sha256"] = registry_sha256(pilot.legacy["sources"])
    replace(pilot.store, APPROVAL_KEY, pilot.approval)
    before = list(pilot.store.writes)
    with pytest.raises(Conflict):
        pilot.service.upload("manual", PDF, OWNER)
    assert read_json(pilot.store, REGISTRY_KEY)[0] == pilot.legacy
    assert pilot.store.writes == before
