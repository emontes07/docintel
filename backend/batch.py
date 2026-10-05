"""Excel-led batch validation, immutable results, reviews, and qualified export."""

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.batch_store import Conflict, Missing, read_json, write_json
from backend.core.vendor_tables import VendorTableConfig
from backend.extract import apply_reviews
from backend.models.enrichment import AttributeDefinition, EnrichmentResult, Manifest, ProductKey, ResponseValidationDiagnostic, ReviewDecision
from backend.workbooks import WorkbookError, read_workbook, write_workbook


class SourceApplicability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: ProductKey
    identity_terms: list[str] = Field(min_length=1)
    attribute_ids: list[str] = Field(default_factory=list)
    qualification: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scope(self):
        if not self.qualification.strip() or any(not value.strip() for value in self.identity_terms + self.attribute_ids):
            raise ValueError("Applicability terms and qualification must not be blank")
        if len(set(self.identity_terms)) != len(self.identity_terms) or len(set(self.attribute_ids)) != len(self.attribute_ids):
            raise ValueError("Applicability terms and attribute IDs must be unique")
        return self


class SourceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str = Field(min_length=1)
    source_id: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    owner: str | None = None
    kind: Literal["blob", "sharepoint", "web"]
    products: list[ProductKey] = Field(min_length=1)
    format: Literal["pdf", "xlsx", "web"] = "pdf"
    source_tier: Literal["internal_pdf", "vendor_table", "manufacturer_web", "approved_web"] = "internal_pdf"
    blob: str | None = None
    sha256: str | None = None
    drive_id: str | None = None
    item_id: str | None = None
    tenant_id: str | None = None
    enabled: bool = False
    url: str | None = None
    applicability: list[SourceApplicability] = Field(default_factory=list)
    table: VendorTableConfig | None = None

    @model_validator(mode="after")
    def validate_source(self):
        if self.owner is not None:
            try:
                parts = self.owner.split("/")
                if len(parts) != 2 or any(str(uuid.UUID(part)) != part for part in parts):
                    raise ValueError
            except ValueError:
                raise ValueError("Source owner must be canonical tenantUUID/objectUUID") from None
        if not self.reference.strip():
            raise ValueError("Source reference must not be blank")
        if self.kind == "blob" and (not self.blob or not self.blob.startswith("documents/") or ".." in self.blob.split("/") or not re.fullmatch(r"[a-f0-9]{64}", self.sha256 or "")):
            raise ValueError("Blob source requires a private document key and exact SHA256")
        if self.sha256 is not None and not re.fullmatch(r"[a-f0-9]{64}", self.sha256):
            raise ValueError("Source content hash must be an exact SHA256")
        if self.kind == "sharepoint":
            if self.blob or (not self.enabled and self.sha256):
                raise ValueError("Blocked SharePoint cannot claim local bytes or a content hash")
            if self.enabled:
                if not re.fullmatch(r"[a-z0-9-]+", self.source_id):
                    raise ValueError("Enabled SharePoint source ID must contain only lowercase letters, digits, and hyphens")
                if not self.sha256 or any(not value or not value.strip() for value in (self.drive_id, self.item_id, self.tenant_id)):
                    raise ValueError("Enabled SharePoint requires an exact SHA256 and approved drive, item, and tenant identifiers")
                location = urlsplit(self.url or "")
                if (
                    location.scheme != "https"
                    or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.sharepoint\.com", location.hostname or "")
                    or location.netloc.lower() != location.hostname
                    or any(character.isspace() or ord(character) < 32 for character in self.url or "")
                    or any(character in (self.url or "") for character in ("?", "#", "\\"))
                ):
                    raise ValueError("Enabled SharePoint requires an approved canonical HTTPS tenant SharePoint URL")
        elif any(value is not None for value in (self.drive_id, self.item_id, self.tenant_id)):
            raise ValueError("SharePoint identifiers require a SharePoint source")
        if self.kind == "web":
            location = urlsplit(self.url or "")
            if self.format != "web" or location.scheme != "https" or not location.hostname or location.username or location.password or location.fragment or any(character.isspace() for character in self.url or ""):
                raise ValueError("Web sources require an operator-approved HTTPS location without credentials or fragments")
            if self.blob or self.sha256:
                raise ValueError("A registered web URL cannot claim retrieved bytes or a content hash")
        elif self.format == "web" or (self.url is not None and not (self.kind == "sharepoint" and self.enabled)):
            raise ValueError("Locations require web or enabled SharePoint sources; web format requires a web source")
        expected_tiers = {"pdf": {"internal_pdf"}, "xlsx": {"vendor_table"}, "web": {"manufacturer_web", "approved_web"}}
        if self.source_tier not in expected_tiers[self.format]:
            raise ValueError("Source tier must match the registered evidence format")
        if self.table is not None and self.format != "xlsx":
            raise ValueError("Vendor table configuration requires XLSX format")
        if any(scope.product not in self.products for scope in self.applicability):
            raise ValueError("Applicability must reference an explicitly associated product")
        return self


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def selections(value):
    if value.startswith("["):
        output = json.loads(value)
        if not isinstance(output, list) or not all(isinstance(item, str) and item for item in output):
            raise ValueError("Selection must be a JSON array of names")
        return output
    return [value] if value else []


def validate_batch(manifest_bytes, attribute_bytes, attribute_reference, registry):
    products = read_workbook(manifest_bytes)
    definitions = read_workbook(attribute_bytes)
    if len(products) != 1 or len(definitions) != 1:
        raise WorkbookError("Select workbooks containing exactly one data worksheet")
    rows = next(iter(products.values()))
    attributes = next(iter(definitions.values()))
    if not rows or not attributes:
        raise WorkbookError("Both workbooks require data rows")
    required = {"PIMITEM Number", "Vendor Name", "MPN", "Hierarchy Node", "Attributes to Fill"}
    if not required <= set(rows[0]):
        raise WorkbookError("Manifest headers do not match the supplied Files Manifest contract")
    required_definitions = {"node", "potential_attribute_name", "potential_attribute_data_type"}
    if not required_definitions <= set(attributes[0]):
        raise WorkbookError("Attribute headers do not match the supplied attribute workbook contract")
    parsed_definitions = {}
    definition_errors = {}
    parents = {}
    for row in attributes:
        if row.get("parent_node"):
            parents.setdefault(row["node"], set()).add(row["parent_node"])

    def ancestry(node):
        chain = []
        while node:
            if node in chain or len(parents.get(node, set())) > 1:
                raise ValueError("Hierarchy has cyclic or conflicting explicit parent_node links")
            chain.append(node)
            node = next(iter(parents.get(node, set())), "")
        return chain

    for row in attributes:
        key = (row["node"], row["potential_attribute_name"])
        try:
            if key in parsed_definitions or key in definition_errors:
                raise ValueError("Duplicate definition for node/attribute")
            guidance = row["potential_attribute_data_type"]
            value_type = row.get("value_type") or {"Boolean": "boolean", "Numeric": "number", "String": "string", "Enumerated": "string", "Multi-Select": "string"}.get(guidance)
            if not value_type:
                raise ValueError("An explicit value_type is required for unknown type guidance")
            unit_resolved = value_type not in {"number", "integer"} or "unit" in row
            if row.get("unit") and not row["unit"].strip():
                raise ValueError("Units must be explicit text or an explicitly empty dimensionless cell")
            allowed = json.loads(row.get("allowed_values") or "[]")
            definition = AttributeDefinition(
                attribute_id=key[1], description=row.get("description") or key[1],
                value_type=value_type, unit=row.get("unit") or None, allowed_values=allowed,
                definition_context=row.get("source_basis") or None,
                type_guidance=guidance, definition_node=key[0], unit_resolved=unit_resolved,
            )
            parsed_definitions[key] = definition
        except (ValueError, TypeError):
            definition_errors[key] = "Provide one scalar typed definition; examples are never allowed values or reference answers"
    items = []
    seen = set()
    try:
        approved_sources = [SourceBinding.model_validate(source).model_dump(mode="json") for source in registry]
        if len({source["reference"] for source in approved_sources}) != len(approved_sources) or len({source["source_id"] for source in approved_sources}) != len(approved_sources):
            raise ValueError("Duplicate source registration")
    except (ValueError, TypeError):
        raise WorkbookError("Source registry must contain unique, valid operator-approved bindings") from None
    source_lookup = {source["reference"]: source for source in approved_sources}
    source_columns = {
        "PDF Tech Spec": {"internal_pdf"},
        "Vendor Tabular Data": {"vendor_table"},
        "Vendor Website": {"manufacturer_web"},
        "Distributors": {"approved_web"},
        "Other Web Sources": {"manufacturer_web", "approved_web"},
    }
    for index, original in enumerate(rows, 2):
        errors = []
        warnings = []
        manifest = None
        sources = []
        try:
            product = ProductKey(item_id=original["PIMITEM Number"], vendor=original["Vendor Name"], mpn=original["MPN"], hierarchy_node=original["Hierarchy Node"])
            identity = (product.item_id, product.vendor, product.mpn)
            if identity in seen:
                errors.append("Duplicate product identity")
            seen.add(identity)
            chain = ancestry(product.hierarchy_node)
            requested = original["Attributes to Fill"]
            if requested == attribute_reference:
                names = list(dict.fromkeys(row["potential_attribute_name"] for node in chain for row in attributes if row["node"] == node))
            else:
                names = selections(requested)
            if not names or len(names) != len(set(names)):
                errors.append("Explicit unique attribute selection or exact workbook binding is required")
            selected = []
            for name in names:
                key = next(((node, name) for node in chain if (node, name) in parsed_definitions or (node, name) in definition_errors), None)
                if key is None or key in definition_errors:
                    errors.append(f"Unresolved definition: {name}. Check exact hierarchy, explicit parent_node links, and type guidance")
                else:
                    definition = parsed_definitions[key]
                    selected.append(definition)
                    if not definition.unit_resolved:
                        warnings.append(f"Definition clarification needed: {name}; confirm the expected unit. No dimensionless or physical unit is inferred")
            for column, tiers in source_columns.items():
                for reference in selections(original.get(column, "")):
                    source = source_lookup.get(reference)
                    if source is None or product.model_dump() not in source["products"] or source["source_tier"] not in tiers:
                        errors.append(f"{column} reference needs an operator-approved product/source association and matching evidence tier")
                    elif source not in sources:
                        sources.append(source)
                        if source["kind"] == "sharepoint" and not source["enabled"]:
                            warnings.append("SharePoint download blocked: retained metadata 200 / content 302 / download 401; no fallback")
                        if source["kind"] == "web":
                            warnings.append("Approved web location is retrieval scope only; a URL alone is not evidence")
                        if source["format"] == "xlsx" and not source["table"]:
                            warnings.append("Vendor evidence unresolved: operator-selected worksheet/header/MPN configuration is missing")
            if not sources:
                warnings.append("No approved evidence sources supplied; missing attributes remain unresolved")
            existing = json.loads(original.get("existing_values") or "{}")
            if not errors:
                manifest = Manifest(product=product, attributes=selected, source_ids=[source["source_id"] for source in sources], existing_values=existing).model_dump(mode="json")
        except (ValueError, TypeError):
            errors.append("Invalid product, explicit hierarchy, selection, or existing_values contract; preserve identifiers as text")
        items.append({"item_key": f"row-{index}", "row": index, "original": original, "manifest": manifest, "sources": sources, "errors": errors, "warnings": warnings})
    return {"items": items, "valid": not any(item["errors"] for item in items), "product_count": len(items), "input_hashes": {"manifest": digest(manifest_bytes), "attributes": digest(attribute_bytes)}, "attribute_reference": attribute_reference, "original_definitions": attributes}


class BatchService:
    def __init__(self, store):
        self.store = store

    def catalog(self, actor=None):
        try:
            record, _ = read_json(self.store, "configuration/sources.json")
            sources = [SourceBinding.model_validate(source).model_dump(mode="json") for source in record["sources"]]
            if len({source["reference"] for source in sources}) != len(sources) or len({source["source_id"] for source in sources}) != len(sources):
                raise ValueError("Source references and IDs must be unique")
            return [source for source in sources if actor is None or source.get("owner") in {None, actor}]
        except Missing:
            return []

    def intake(self, manifest_bytes, attribute_bytes, attribute_reference, actor):
        catalog = self.catalog(actor)
        validation = validate_batch(manifest_bytes, attribute_bytes, attribute_reference, catalog)
        batch_id = digest((actor + ":" + attribute_reference + ":" + validation["input_hashes"]["manifest"] + ":" + validation["input_hashes"]["attributes"] + ":" + digest(json.dumps(catalog, sort_keys=True).encode())).encode())
        record = {"id": batch_id, "owner": actor, "created_at": now(), "state": "validated" if validation["valid"] else "invalid", **validation}
        try:
            for name, content in [("manifest.xlsx", manifest_bytes), ("attributes.xlsx", attribute_bytes)]:
                try:
                    self.store.write_bytes(f"inputs/{batch_id}/{name}", content)
                except Conflict:
                    pass
            write_json(self.store, f"batches/{batch_id}.json", record)
        except Conflict:
            record, _ = read_json(self.store, f"batches/{batch_id}.json")
        return record

    def get(self, batch_id, actor):
        record, _ = read_json(self.store, f"batches/{batch_id}.json")
        if record["owner"] != actor:
            raise Missing(batch_id)
        return record

    def list(self, actor):
        return [self.summary(record) for path in self.store.keys("batches/") if (record := read_json(self.store, path)[0])["owner"] == actor]

    @staticmethod
    def summary(record):
        return {key: record[key] for key in ["id", "state", "created_at", "product_count", "valid", "mode", "progress"] if key in record}

    def submit(self, batch_id, actor, request_id, mode, confirm_live):
        uuid.UUID(request_id)
        if mode not in {"evidence_only", "live_inference", "real_pilot"} or (mode != "evidence_only" and not confirm_live):
            raise ValueError("Live execution requires explicit consent")
        path = f"batches/{batch_id}.json"
        record, version = read_json(self.store, path)
        self.get(batch_id, actor)
        if not record["valid"]:
            raise ValueError("Resolve all validation errors before submitting")
        if mode == "real_pilot" and not record.get("mode"):
            from backend.real_pilot import RealPilotGuard

            RealPilotGuard(self.store, record)
        request_key = f"requests/{digest(actor.encode())}/{request_id}.json"
        fingerprint = {"batch_id": batch_id, "mode": mode}
        try:
            write_json(self.store, request_key, fingerprint)
        except Conflict:
            if read_json(self.store, request_key)[0] != fingerprint:
                raise Conflict("Request ID is already bound to another batch or mode") from None
        if record.get("mode"):
            if record["mode"] != mode:
                raise Conflict("This workbook batch was already submitted with another execution method")
            return record
        record.update(state="queued", mode=mode, request_id=request_id, submitted_at=now(), submitted_by=actor)
        write_json(self.store, path, record, version)
        return record

    def items(self, batch_id, actor, offset=0, limit=50, view="all"):
        if offset < 0 or not 1 <= limit <= 100 or view not in {"all", "failed", "unresolved", "pending"}:
            raise ValueError("Invalid item paging/filter")
        record = self.get(batch_id, actor)
        output = []
        selected = record["items"][offset:offset + limit] if view == "all" else record["items"]
        for item in selected:
            try:
                detail, _ = read_json(self.store, f"items/{batch_id}/{item['item_key']}.json")
                try:
                    reviews, _ = read_json(self.store, f"reviews/{batch_id}/{item['item_key']}.json")
                except Missing:
                    reviews = []
                pending = bool(set(detail.get("reviewable_attributes", [])) - {review["attribute_id"] for review in reviews})
                output.append({**{key: item[key] for key in ["item_key", "row", "original", "errors", "warnings"]}, "state": detail["state"], "pending_review": pending, "error": detail.get("error")})
            except Missing:
                output.append({**{key: item[key] for key in ["item_key", "row", "original", "errors", "warnings"]}, "state": "queued" if record.get("mode") else record["state"], "pending_review": False})
        if view != "all":
            output = [item for item in output if (view == "failed" and (item["errors"] or item["state"] in {"failed", "interrupted"})) or (view == "unresolved" and item["state"] == "unresolved") or (view == "pending" and item["pending_review"])]
        return {"items": output if view == "all" else output[offset:offset + limit], "total": len(record["items"]) if view == "all" else len(output)}

    def detail(self, batch_id, item_key, actor):
        record = self.get(batch_id, actor)
        if item_key not in {item["item_key"] for item in record["items"]}:
            raise Missing(item_key)
        detail, _ = read_json(self.store, f"items/{batch_id}/{item_key}.json")
        item = next(item for item in record["items"] if item["item_key"] == item_key)
        detail.update(original=item["original"], row=item["row"], requested_mode=record.get("mode", "not_submitted"))
        try:
            result, _ = read_json(self.store, f"results/{batch_id}/{item_key}.json")
            try:
                reviews, _ = read_json(self.store, f"reviews/{batch_id}/{item_key}.json")
            except Missing:
                reviews = []
            machine = EnrichmentResult.model_validate(result)
            detail.update(machine_result=result, machine_sha256=digest(machine.model_dump_json(exclude_unset=True).encode()), reviewed_result=apply_reviews(machine, [ReviewDecision.model_validate(review) for review in reviews]).model_dump(mode="json"), reviewer_identity="verified_entra" if not actor.startswith("development:") else "development_unverified")
        except Missing:
            detail.update(machine_result=None, reviewed_result=None)
        diagnostics = {
            digest(json.dumps(value, sort_keys=True).encode()): value
            for value in (detail.get("machine_result") or {}).get("validation_diagnostics", [])
        }
        for key in self.store.keys(f"response-diagnostics/{batch_id}/{item_key}/"):
            value, _ = read_json(self.store, key)
            value = ResponseValidationDiagnostic.model_validate(value).model_dump(mode="json")
            diagnostics[digest(json.dumps(value, sort_keys=True).encode())] = value
        detail["validation_diagnostics"] = list(diagnostics.values())
        return detail

    def review(self, batch_id, item_key, actor, value):
        detail = self.detail(batch_id, item_key, actor)
        if not detail["machine_result"]:
            raise ValueError("No machine result is available for review")
        review = ReviewDecision.model_validate({**value, "reviewer": actor, "reviewed_at": now()})
        key = f"reviews/{batch_id}/{item_key}.json"
        try:
            reviews, version = read_json(self.store, key)
        except Missing:
            reviews, version = [], None
        apply_reviews(EnrichmentResult.model_validate(detail["reviewed_result"]), [review])
        write_json(self.store, key, [*reviews, review.model_dump(mode="json")], version)
        return self.detail(batch_id, item_key, actor)

    def export(self, batch_id, actor):
        exported_at = now()
        record = self.get(batch_id, actor)
        columns = list(record["items"][0]["original"])
        inputs = [columns, *[[item["original"].get(column, "") for column in columns] for item in record["items"]]]
        results = [["Row", "Item ID", "Vendor", "MPN", "Attribute", "Status", "Candidate index", "Proposed value", "Unit", "Evidence IDs", "Qualifications", "Review status", "Reviewed value", "Reviewed unit", "Reviewer", "Reviewed at", "Reason", "Error", "Source tiers", "Supporting quote", "Model confidence (not measured accuracy)"]]
        evidence_rows = [["Row", "Evidence ID", "Source ID", "Locator", "Version", "Excerpt", "Observed at", "Source tier", "Applicability", "Approved attributes", "Discovery method"]]
        errors = [["Row", "State", "Error", "Warnings", "Definition clarifications", "Validation diagnostics"]]
        diagnostics_sheet = [["Row", "Diagnostic", "Part", "Parts", "Sanitized diagnostic JSON"]]
        provenance = [["Row", "Execution method", "Machine SHA256", "Source provenance", "Consumption reservations and usage", "Attribute coverage", "Inference provenance"]]
        for item in record["items"]:
            try:
                detail = self.detail(batch_id, item["item_key"], actor)
            except Missing:
                detail = {"state": record["state"], "error": "; ".join(item["errors"]), "reviewed_result": None}
            clarifications = [
                attribute["definition_clarification"]
                for attribute in (detail.get("reviewed_result") or {}).get("attributes", [])
                if attribute.get("definition_clarification")
            ]
            diagnostic_summaries = []
            for index, diagnostic in enumerate(detail.get("validation_diagnostics", [])):
                diagnostic_summaries.extend(
                    f"{issue['field_path']}: {issue['message']}" for issue in diagnostic["issues"]
                )
                serialized = json.dumps(diagnostic, ensure_ascii=True)
                chunks = [serialized[offset:offset + 30000] for offset in range(0, len(serialized), 30000)]
                diagnostics_sheet.extend(
                    [item["row"], index + 1, part + 1, len(chunks), chunk]
                    for part, chunk in enumerate(chunks)
                )
            diagnostic_summary = "\n".join(diagnostic_summaries)
            if len(diagnostic_summary) > 30000:
                diagnostic_summary = diagnostic_summary[:30000] + "\nSee Diagnostics sheet for complete sanitized diagnostics."
            errors.append([
                item["row"], detail["state"], detail.get("error", ""), "; ".join(item["warnings"]),
                "\n".join(clarifications), diagnostic_summary,
            ])
            provenance.append([item["row"], record.get("mode", "not_submitted"), detail.get("machine_sha256", ""), json.dumps(detail.get("provenance", [])), json.dumps(detail.get("consumption")), json.dumps(detail.get("coverage")), json.dumps(detail.get("inference_provenance"))])
            machine = detail.get("reviewed_result")
            if not machine:
                results.append([item["row"], item["original"]["PIMITEM Number"], item["original"]["Vendor Name"], item["original"]["MPN"], "", detail["state"], "", "", "", "", "", "pending", "", "", "", "", "", detail.get("error", "")])
                continue
            for evidence in machine["evidence"]:
                evidence_rows.append([item["row"], *[evidence.get(key) for key in ["evidence_id", "source_id", "source_locator", "source_version", "text", "observed_at", "source_tier", "qualification"]], json.dumps(evidence.get("attribute_ids")), evidence.get("discovery_method")])
            for attribute in machine["attributes"]:
                review = attribute["review"] or {}
                for index, candidate in enumerate(attribute["candidates"] or [{}]):
                    notes = "\n".join(filter(None, [
                        candidate.get("qualification"),
                        *[note["text"] for note in attribute["review_annotations"] if note["candidate_index"] == index],
                    ]))
                    results.append([item["row"], machine["manifest"]["product"]["item_id"], machine["manifest"]["product"]["vendor"], machine["manifest"]["product"]["mpn"], attribute["attribute_id"], attribute["status"], index if candidate else "", candidate.get("value", machine["manifest"]["existing_values"].get(attribute["attribute_id"], "")), candidate.get("unit", ""), json.dumps(candidate.get("evidence_ids", [])), notes, review.get("decision", "pending"), review.get("corrected_value", ""), review.get("corrected_unit", ""), review.get("reviewer", ""), review.get("reviewed_at", ""), review.get("reason", ""), detail.get("error", "")])
                    tiers = sorted({entry["source_tier"] for entry in machine["evidence"] if entry["evidence_id"] in candidate.get("evidence_ids", [])})
                    results[-1].extend([", ".join(tiers), candidate.get("supporting_quote"), candidate.get("confidence")])
        definition_columns = list(record["original_definitions"][0])
        reviews_sheet = [["Row", "Attribute", "Decision", "Selected candidate index", "Corrected value", "Corrected unit", "Reviewer", "Identity status", "Reviewed at", "Reason"]]
        for item in record["items"]:
            try:
                reviews, _ = read_json(self.store, f"reviews/{batch_id}/{item['item_key']}.json")
            except Missing:
                continue
            for review in reviews:
                reviews_sheet.append([item["row"], review["attribute_id"], review["decision"], review["candidate_index"], review["corrected_value"], review["corrected_unit"], review["reviewer"], "development_unverified" if actor.startswith("development:") else "verified_entra", review["reviewed_at"], review["reason"]])
        metadata = [["Key", "Value"], ["Batch ID", batch_id], ["Export started at", exported_at], ["Batch state", record["state"]], ["Input hashes", json.dumps(record["input_hashes"])], ["Attribute reference", record["attribute_reference"]], ["Consistency", "Per-item snapshot; reviews or processing may advance during export"], ["Qualification", "Machine proposals are not approved master data; inspect every exception and review"]]
        sheets = {"Batch": metadata, "Inputs": inputs, "Definitions": [definition_columns, *[[row.get(column, "") for column in definition_columns] for row in record["original_definitions"]]], "Results": results, "Evidence": evidence_rows, "Provenance": provenance, "Errors": errors, "Reviews": reviews_sheet}
        if len(diagnostics_sheet) > 1:
            sheets["Diagnostics"] = diagnostics_sheet
        return export_workbook(sheets)


def export_workbook(sheets):
    chunks = [["Sheet", "Row", "Column", "Part", "Encoding", "Text"]]
    for name, rows in sheets.items():
        for row_index, row in enumerate(rows, 1):
            for column_index, value in enumerate(row):
                text = "" if value is None else str(value)
                encoding = "text"
                if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
                    text = json.dumps(text, ensure_ascii=True)
                    encoding = "json-string"
                if len(text) > 32767 or encoding != "text":
                    for part, start in enumerate(range(0, len(text), 30000), 1):
                        chunks.append([name, row_index, column_index + 1, part, encoding, text[start:start + 30000]])
                    row[column_index] = f"Full value in Long text: {name}, row {row_index}, column {column_index + 1}"
    if len(chunks) > 1:
        sheets["Long text"] = chunks
    return write_workbook(sheets)