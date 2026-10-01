"""Private, single-product run/review orchestration and local CLI."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from backend.core.docintel import DocumentIntelligenceService, ParsedDocument
from backend.core.pilot_sources import SourceReference, retrieve_pdf
from backend.extract import apply_reviews, run_enrichment
from backend.models.enrichment import (
    Contract, EnrichmentResult, ExtractionResponse, LiveBundle, Manifest,
    OfflineBundle, OfflineSource, ProductKey, ReviewAnnotation, ReviewDecision,
)


PARSER_VERSION = "prebuilt-layout:2024-11-30:mapping-v1"
QUALIFICATIONS = {
    "Pressure Rating": "The source describes the AWWA C800 working-pressure requirement, not an established maximum, burst, or test pressure.",
    "Seal / Softgoods Material": "The source identifies the inverted-key O-ring material; do not generalize it to all seals or softgoods.",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def document_hash(document: ParsedDocument) -> str:
    return hashlib.sha256(document.model_dump_json().encode()).hexdigest()


def private_home() -> Path:
    return Path(os.environ.get("DOCINTEL_PILOT_HOME", str(Path.home() / "Library/Application Support/DocIntel/prototype"))).expanduser().resolve()


def private_scope(home: Path) -> dict:
    path = home / "approved-scope.json"
    if not path.exists():
        return {}
    scope = json.loads(path.read_text())
    return {key: scope[key] for key in ("approved_product", "document_identity_terms")}


class PilotConfig(Contract):
    schema_version: Literal[1] = 1
    manifest: Manifest
    sources: list[SourceReference]
    replay_source_id: str
    replay_sha256: str
    imported_artifacts: dict[str, str]
    approved_product: ProductKey = Field(default_factory=lambda: ProductKey(item_id="SYNTHETIC-001", mpn="MODEL-001", vendor="Synthetic Manufacturer", hierarchy_node="synthetic-test"))
    document_identity_terms: list[str] = Field(default_factory=lambda: ["Synthetic"], min_length=1)
    analysis_limit: int = Field(default=2, ge=0, le=2)
    inference_limit: int = Field(default=3, ge=0, le=3)

    @classmethod
    def from_private_home(cls, home: Path):
        return cls.model_validate({**json.loads((home / "config.json").read_text()), **private_scope(home)})

    @model_validator(mode="after")
    def approved_scope(self):
        product = self.manifest.product
        if product != self.approved_product or not all(term.strip() for term in self.document_identity_terms):
            raise ValueError("Product must match the private approved scope and document identity terms")
        if {item.attribute_id for item in self.manifest.attributes} != set(QUALIFICATIONS):
            raise ValueError("Only the two approved attributes are supported")
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise ValueError("Source IDs must be unique")
        if self.replay_source_id not in {source.source_id for source in self.sources if source.kind == "local"}:
            raise ValueError("Replay requires the configured local source")
        return self


class RunRequest(Contract):
    source_id: str
    mode: Literal["offline_replay", "live_inference"] = "offline_replay"
    parse_mode: Literal["reuse", "fresh"] = "reuse"
    request_id: uuid.UUID
    confirm_live: bool = False
    confirm_repeat: bool = False

    @model_validator(mode="after")
    def explicit_execution(self):
        if self.mode == "live_inference" and not self.confirm_live:
            raise ValueError("Live execution requires explicit confirmation")
        if self.mode == "offline_replay" and (self.parse_mode != "reuse" or self.confirm_live):
            raise ValueError("Replay never submits analysis or inference")
        return self


class ReviewInput(Contract):
    attribute_id: str
    decision: Literal["approve", "correct", "reject"]
    reviewer: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)
    candidate_index: int | None = Field(default=None, ge=0)
    corrected_value: str | int | float | bool | None = None
    corrected_unit: str | None = None


class PilotError(ValueError):
    pass


class PilotService:
    def __init__(self, home: Path | None = None):
        self.home = (home or private_home()).resolve()
        repository = Path(__file__).resolve().parents[1]
        if self.home == repository or repository in self.home.parents:
            raise PilotError("Pilot artifacts must be outside the repository")
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.home.chmod(0o700)
        self.config = PilotConfig.from_private_home(self.home)
        self.database = self.home / "runs.sqlite3"
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, request TEXT NOT NULL,
                    state TEXT NOT NULL, stages TEXT NOT NULL, created_at TEXT NOT NULL,
                    machine TEXT, retrieval TEXT, parsing TEXT,
                    analysis_calls INTEGER NOT NULL DEFAULT 0,
                    inference_calls INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    run_id TEXT NOT NULL, attribute_id TEXT NOT NULL, decision TEXT NOT NULL,
                    PRIMARY KEY (run_id, attribute_id), FOREIGN KEY (run_id) REFERENCES runs(id)
                );
            """)
        self.database.chmod(0o600)

    def connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def source(self, source_id: str) -> SourceReference:
        source = next((item for item in self.config.sources if item.source_id == source_id), None)
        if source is None:
            raise PilotError("Unknown configured source")
        return source

    def catalog(self) -> dict:
        from backend.core.config import settings

        with self.connect() as connection:
            totals = connection.execute("SELECT COALESCE(SUM(analysis_calls),0), COALESCE(SUM(inference_calls),0) FROM runs").fetchone()
        return {
            "product": self.config.manifest.product.model_dump(),
            "sources": [{"source_id": item.source_id, "kind": item.kind, "location": item.location, "availability": item.availability, "enabled": item.enabled, "replay_available": item.source_id == self.config.replay_source_id} for item in self.config.sources],
            "live_configured": bool((settings.LLM_ENDPOINT or settings.AI_FOUNDRY_ENDPOINT) and settings.LLM_DEPLOYMENT),
            "analysis_configured": bool(settings.AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT),
            "budget": {"analysis_used": totals[0], "analysis_limit": self.config.analysis_limit, "inference_used": totals[1], "inference_limit": self.config.inference_limit},
            "reviewer_identity": "locally_entered_unverified",
        }

    def begin(self, request: RunRequest) -> tuple[str, bool]:
        request = RunRequest.model_validate(request.model_dump())
        source = self.source(request.source_id)
        if not source.enabled:
            raise PilotError("Configured source unavailable; resolve recorded access failure before enabling another retrieval")
        if request.mode == "offline_replay" and source.source_id != self.config.replay_source_id:
            raise PilotError("Replay is available only for the configured prior local pilot")
        fingerprint = hashlib.sha256(json.dumps([source.model_dump(), request.mode, request.parse_mode], sort_keys=True).encode()).hexdigest()
        run_id = str(request.request_id)
        stages = {name: {"status": "not_attempted"} for name in ("retrieval", "association", "parsing", "inference", "validation")}
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise PilotError("Request ID already belongs to a different operation")
                return run_id, False
            active = connection.execute("SELECT id FROM runs WHERE state IN ('queued','running')").fetchone()
            if active:
                raise PilotError("A run is already active; inspect it before starting another")
            prior = connection.execute("SELECT id FROM runs WHERE fingerprint=? ORDER BY created_at DESC LIMIT 1", (fingerprint,)).fetchone()
            if prior and not request.confirm_repeat:
                return prior["id"], False
            connection.execute("INSERT INTO runs (id,fingerprint,request,state,stages,created_at) VALUES (?,?,?,?,?,?)", (run_id, fingerprint, request.model_dump_json(), "queued", json.dumps(stages), now()))
        return run_id, True

    def stage(self, run_id: str, name: str, status: str, **details):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stages = json.loads(connection.execute("SELECT stages FROM runs WHERE id=?", (run_id,)).fetchone()[0])
            stages[name] = {"status": status, **details}
            connection.execute("UPDATE runs SET stages=? WHERE id=?", (json.dumps(stages), run_id))

    def reserve(self, run_id: str, kind: Literal["analysis", "inference"]):
        column = "analysis_calls" if kind == "analysis" else "inference_calls"
        limit = self.config.analysis_limit if kind == "analysis" else self.config.inference_limit
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            total = connection.execute(f"SELECT COALESCE(SUM({column}),0) FROM runs").fetchone()[0]
            if total >= limit:
                raise PilotError("Configured live submission budget exhausted")
            connection.execute(f"UPDATE runs SET {column}={column}+1 WHERE id=?", (run_id,))

    def cache_path(self, source: SourceReference) -> Path:
        key = hashlib.sha256((source.source_id + source.location + source.expected_sha256 + PARSER_VERSION).encode()).hexdigest()
        return self.home / f"parse-{key}.json"

    def execute(self, run_id: str, *, retrieve=retrieve_pdf, parser=None, enrichment=run_enrichment):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["state"] != "queued":
                return
            connection.execute("UPDATE runs SET state='running' WHERE id=?", (run_id,))
        request = RunRequest.model_validate_json(row["request"])
        source = self.source(request.source_id)
        current_stage = "retrieval"
        try:
            self.stage(run_id, current_stage, "running")
            retrieved = retrieve(source)
            with self.connect() as connection:
                connection.execute("UPDATE runs SET retrieval=? WHERE id=?", (json.dumps(retrieved.metadata), run_id))
            if retrieved.metadata["status"] != "success":
                raise PilotError(retrieved.metadata.get("explanation", "Configured source unavailable; inspect the recorded retrieval stage/status. No alternate source used."))
            self.stage(run_id, current_stage, "succeeded")
            current_stage = "association"
            digest = hashlib.sha256(retrieved.content).hexdigest()
            if digest != source.expected_sha256 or retrieved.metadata.get("content_version") != "sha256:" + digest or retrieved.metadata.get("source_id") != source.source_id or retrieved.metadata.get("location") != source.location:
                raise PilotError("Source identity or approved content association mismatch")
            self.stage(run_id, current_stage, "succeeded", basis="configured product association and approved PDF SHA-256")
            current_stage = "parsing"
            self.stage(run_id, current_stage, "running")
            cache = self.cache_path(source)
            if request.parse_mode == "reuse" and cache.exists():
                cached = json.loads(cache.read_text())
                if cached["parser_version"] != PARSER_VERSION:
                    raise PilotError("Incompatible parse cache")
                document = ParsedDocument.model_validate(cached["document"])
                if cached.get("document_sha256") != document_hash(document):
                    raise PilotError("Parsed cache integrity check failed")
                parsing = {"status": "cached", "origin": cached["origin"], "parsed_at": document.parsed_at.isoformat(), "parser_version": PARSER_VERSION}
            else:
                if request.mode != "live_inference":
                    raise PilotError("Replay requires a compatible cached parse; no analysis submitted")
                self.reserve(run_id, "analysis")
                document = (parser or DocumentIntelligenceService()).extract_pdf_bytes(retrieved.content, source=source.location)
                parsing = {"status": "live", "origin": run_id, "parsed_at": document.parsed_at.isoformat(), "parser_version": PARSER_VERSION}
            if document.source != source.location or document.cache_key != "sha256:" + digest:
                raise PilotError("Parsed source location or content version mismatch")
            text = "\n".join([paragraph.text for paragraph in document.paragraphs] + [cell for table in document.tables for row in table.cells for cell in row])
            mpn = self.config.manifest.product.mpn
            if not re.search(rf"(?<![\w-]){re.escape(mpn)}(?![\w-])", text) or not all(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE) for term in self.config.document_identity_terms):
                raise PilotError("Parsed source does not establish the privately configured product association")
            if parsing["status"] == "live" and not cache.exists():
                with cache.open("x") as handle:
                    json.dump({"parser_version": PARSER_VERSION, "origin": run_id, "document": document.model_dump(mode="json"), "document_sha256": document_hash(document)}, handle)
                cache.chmod(0o600)
            parsed_path = self.home / f"parsed-{run_id}.json"
            with parsed_path.open("x") as handle:
                handle.write(document.model_dump_json(indent=2))
            parsed_path.chmod(0o600)
            parsing["document_sha256"] = document_hash(document)
            with self.connect() as connection:
                connection.execute("UPDATE runs SET parsing=? WHERE id=?", (json.dumps(parsing), run_id))
            self.stage(run_id, current_stage, parsing["status"], origin=parsing["origin"], paragraphs=len(document.paragraphs), tables=len(document.tables))
            manifest = self.config.manifest.model_copy(deep=True)
            manifest.source_ids = [source.source_id]
            supplied_source = OfflineSource(source_id=source.source_id, product=manifest.product, document=document, provider_retrieved_at=datetime.now(timezone.utc))
            current_stage = "inference"
            self.stage(run_id, current_stage, "running")
            if request.mode == "offline_replay":
                replay_bytes = (self.home / "prior-result.json").read_bytes()
                if hashlib.sha256(replay_bytes).hexdigest() != self.config.replay_sha256:
                    raise PilotError("Preserved replay result integrity check failed")
                original = EnrichmentResult.model_validate_json(replay_bytes)
                if original.manifest != self.config.manifest:
                    raise PilotError("Replay manifest does not match configured pilot")
                response = ExtractionResponse(candidates=[candidate for attribute in original.attributes for candidate in attribute.candidates])
                bundle = OfflineBundle(manifest=manifest, sources=[supplied_source], generated_response=response)
                result = enrichment(bundle)
            else:
                from backend.core.config import settings

                if not (settings.LLM_ENDPOINT or settings.AI_FOUNDRY_ENDPOINT) or not settings.LLM_DEPLOYMENT:
                    raise PilotError("Live text endpoint or deployment is not configured")
                self.reserve(run_id, "inference")
                bundle = LiveBundle(execution_mode="live_inference", manifest=manifest, sources=[supplied_source])
                result = enrichment(bundle, execution_mode="live_inference", no_retries=True)
            self.stage(run_id, current_stage, "replayed" if request.mode == "offline_replay" else result.model_call_status, origin="preserved successful pilot" if request.mode == "offline_replay" else "new model request")
            current_stage = "validation"
            validation_status = "not_attempted" if result.model_call_status == "failed" else "failed" if result.extraction_error else "passed"
            self.stage(run_id, current_stage, validation_status, failure=result.failure.model_dump(mode="json") if result.failure else None)
            for attribute in result.attributes:
                for index in range(len(attribute.candidates)):
                    attribute.review_annotations.append(ReviewAnnotation(candidate_index=index, text=QUALIFICATIONS[attribute.attribute_id], author="Local prototype post-generation qualification; not source evidence or approval", annotated_at=datetime.now(timezone.utc)))
            with self.connect() as connection:
                connection.execute("UPDATE runs SET machine=?,state=? WHERE id=? AND machine IS NULL", (result.model_dump_json(), "failed" if result.extraction_error else "completed", run_id))
        except Exception as error:
            actions = {
                "retrieval": "Check the configured source and recorded retrieval stage/status.",
                "association": "Check the approved product identity and PDF content version.",
                "parsing": "Check the Document Intelligence endpoint, existing access, and parse-cache integrity before explicitly authorizing another analysis.",
                "inference": "Check the text endpoint/deployment and existing access before explicitly authorizing another inference request.",
                "validation": "Inspect cited evidence and attribute definitions; do not approve unsupported proposals.",
            }
            message = str(error) if isinstance(error, PilotError) else f"{current_stage.capitalize()} failed. {actions[current_stage]} Sensitive provider details withheld; no automatic retry."
            self.stage(run_id, current_stage, "failed", explanation=message)
            with self.connect() as connection:
                connection.execute("UPDATE runs SET state='failed' WHERE id=?", (run_id,))

    def list_runs(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute("SELECT id,state,request,created_at FROM runs ORDER BY created_at DESC").fetchall()
        return [{"id": row["id"], "state": row["state"], "created_at": row["created_at"], "request": json.loads(row["request"])} for row in rows]

    def abandon(self, run_id: str):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state,stages FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["state"] not in ("queued", "running"):
                raise PilotError("Only an interrupted queued or running operation can be abandoned")
            stages = json.loads(row["stages"])
            stages["interruption"] = {"status": "failed", "explanation": "Operator confirmed workers stopped. No retry or refund of live reservations; remote completion may be unknown."}
            connection.execute("UPDATE runs SET state='failed',stages=? WHERE id=?", (json.dumps(stages), run_id))

    def get(self, run_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise PilotError("Run not found")
            reviews = [ReviewDecision.model_validate_json(item[0]) for item in connection.execute("SELECT decision FROM reviews WHERE run_id=? ORDER BY attribute_id", (run_id,))]
        machine = EnrichmentResult.model_validate_json(row["machine"]) if row["machine"] else None
        return {
            "schema_version": "docintel.review.v1", "id": row["id"], "state": row["state"],
            "created_at": row["created_at"], "request": json.loads(row["request"]),
            "stages": json.loads(row["stages"]),
            "source": json.loads(row["retrieval"]) if row["retrieval"] else None,
            "parsing": json.loads(row["parsing"]) if row["parsing"] else None,
            "machine_result": machine.model_dump(mode="json") if machine else None,
            "machine_sha256": hashlib.sha256(row["machine"].encode()).hexdigest() if machine else None,
            "reviewed_result": apply_reviews(machine, reviews).model_dump(mode="json") if machine else None,
            "review_records": [review.model_dump(mode="json") for review in reviews],
            "reviewer_identity": "locally_entered_unverified", "master_data_written": False,
        }

    def review(self, run_id: str, supplied: ReviewInput) -> dict:
        decision = ReviewDecision(**supplied.model_dump(), reviewed_at=datetime.now(timezone.utc))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT machine,state FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["state"] != "completed" or not row["machine"]:
                raise PilotError("Only a completed reviewable run accepts decisions")
            previous = [ReviewDecision.model_validate_json(item[0]) for item in connection.execute("SELECT decision FROM reviews WHERE run_id=?", (run_id,))]
            try:
                apply_reviews(EnrichmentResult.model_validate_json(row["machine"]), previous + [decision])
            except ValueError:
                raise PilotError("Invalid decision, correction type/unit, or attribute already reviewed") from None
            connection.execute("INSERT INTO reviews VALUES (?,?,?)", (run_id, decision.attribute_id, decision.model_dump_json()))
        return self.get(run_id)


    def export_snapshot(self, run_id: str) -> dict:
        result = self.get(run_id)
        directory = self.home / "exports"
        directory.mkdir(mode=0o700, exist_ok=True)
        output = directory / f"{uuid.uuid4()}-review-v1.json"
        with output.open("x") as handle:
            json.dump(result, handle, indent=2)
        output.chmod(0o600)
        return {"schema_version": result["schema_version"], "location": str(output), "run_id": run_id}


def initialize(home: Path, prior_pilot: Path, annotations: Path, sharepoint_record: Path):
    repository = Path(__file__).resolve().parents[1]
    home = home.resolve()
    if home == repository or repository in home.parents or home.exists():
        raise PilotError("Choose a new private directory outside the repository")
    original_bytes = (prior_pilot / "result.json").read_bytes()
    original = EnrichmentResult.model_validate_json(original_bytes)
    document = ParsedDocument.model_validate_json((prior_pilot / "parsed.json").read_text())
    annotated = EnrichmentResult.model_validate_json(annotations.read_text())
    if original.extraction_error or original.model_call_status != "succeeded" or any(item.review for item in original.attributes):
        raise PilotError("Import requires successful, pending original machine results")
    annotated_without_notes = annotated.model_copy(deep=True)
    for attribute in annotated_without_notes.attributes:
        attribute.review_annotations = []
    if annotated_without_notes != original:
        raise PilotError("Annotated artifact differs beyond review annotations")
    local_path = Path(document.source).expanduser().resolve(strict=True)
    digest = hashlib.sha256(local_path.read_bytes()).hexdigest()
    if document.cache_key != "sha256:" + digest:
        raise PilotError("Original source does not match successful pilot hash")
    source_id = original.manifest.source_ids[0]
    cloud = json.loads(sharepoint_record.read_text())
    item = cloud["pdf_metadata"]
    config = PilotConfig(
        **private_scope(prior_pilot),
        manifest=original.manifest,
        sources=[
            SourceReference(source_id=source_id, kind="local", location=str(local_path), expected_sha256=digest, availability="Local file; approved pilot content"),
            SourceReference(source_id="ford-sharepoint", kind="sharepoint", location=item["webUrl"], expected_sha256=digest, drive_id=cloud["drive_id"], item_id=item["id"], tenant_id=item["sharepointIds"]["tenantId"], enabled=False, availability="Blocked at redirected download: HTTP 401. No cloud/local hash equality established."),
        ],
        replay_source_id=source_id, replay_sha256=hashlib.sha256(original_bytes).hexdigest(),
        imported_artifacts={"original_result": str(prior_pilot / "result.json"), "parsed": str(prior_pilot / "parsed.json"), "annotations": str(annotations), "source_readiness": str(sharepoint_record)},
    )
    home.mkdir(parents=True, mode=0o700)
    for name, content in (("config.json", config.model_dump_json(indent=2).encode()), ("prior-result.json", original_bytes), ("prior-annotations.json", annotations.read_bytes())):
        with (home / name).open("xb") as handle:
            handle.write(content)
        (home / name).chmod(0o600)
    service = PilotService(home)
    cache = service.cache_path(config.sources[0])
    with cache.open("x") as handle:
        json.dump({"parser_version": PARSER_VERSION, "origin": "imported_verified_pilot", "document": document.model_dump(mode="json"), "document_sha256": document_hash(document)}, handle)
    cache.chmod(0o600)
    return service


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup")
    setup.add_argument("--prior-pilot", type=Path, required=True)
    setup.add_argument("--annotations", type=Path, required=True)
    setup.add_argument("--sharepoint-record", type=Path, required=True)
    commands.add_parser("catalog")
    run = commands.add_parser("run")
    run.add_argument("--source", required=True)
    run.add_argument("--live", action="store_true")
    run.add_argument("--fresh-parse", action="store_true")
    run.add_argument("--confirm-live", action="store_true")
    run.add_argument("--confirm-repeat", action="store_true")
    run.add_argument("--request-id", type=uuid.UUID, default=None)
    show = commands.add_parser("show")
    show.add_argument("run_id")
    abandon = commands.add_parser("abandon")
    abandon.add_argument("run_id")
    abandon.add_argument("--workers-stopped", action="store_true", required=True)
    review = commands.add_parser("review")
    review.add_argument("run_id")
    review.add_argument("--input", type=Path, required=True)
    export = commands.add_parser("export")
    export.add_argument("run_id")
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "setup":
            service = initialize(private_home(), args.prior_pilot, args.annotations, args.sharepoint_record)
            print(json.dumps(service.catalog(), indent=2))
            return 0
        service = PilotService()
        if args.command == "catalog":
            print(json.dumps(service.catalog(), indent=2))
        elif args.command == "run":
            request = RunRequest(source_id=args.source, mode="live_inference" if args.live else "offline_replay", parse_mode="fresh" if args.fresh_parse else "reuse", confirm_live=args.confirm_live, confirm_repeat=args.confirm_repeat, request_id=args.request_id or uuid.uuid4())
            run_id, created = service.begin(request)
            if created:
                service.execute(run_id)
            result = service.get(run_id)
            print(json.dumps({"id": run_id, "state": result["state"], "stages": result["stages"]}, indent=2))
            return 0 if result["state"] == "completed" else 1
        elif args.command == "show":
            print(json.dumps(service.get(args.run_id), indent=2))
        elif args.command == "abandon":
            service.abandon(args.run_id)
            print("Interrupted run recorded as failed; live reservations retained")
        elif args.command == "review":
            result = service.review(args.run_id, ReviewInput.model_validate_json(args.input.read_text()))
            print(json.dumps(result["review_records"], indent=2))
        elif args.command == "export":
            output = args.output.expanduser().resolve()
            repository = Path(__file__).resolve().parents[1]
            if output == repository or repository in output.parents:
                raise PilotError("Exports must remain outside the repository")
            with output.open("x") as handle:
                json.dump(service.get(args.run_id), handle, indent=2)
            output.chmod(0o600)
            print("Private versioned review export written")
        return 0
    except Exception:
        parser.exit(2, "Pilot operation failed; check private configuration, inputs, and live confirmation. Sensitive details withheld.\n")


if __name__ == "__main__":
    raise SystemExit(main())