"""Finite append-only quality worker using stored manifests and existing parses.

Run ``python -m backend.quality_worker`` with QUALITY_BATCH_ID, QUALITY_OWNER and
QUALITY_RUN_ID. No Document Intelligence, SharePoint or legacy pilot admission
path is invoked. Failed cache loads are explicit evidence gaps, never new parses.
The CLI defaults to one Mueller smoke followed by all products in the same job;
the smoke candidate is reused within the vendor tier's three-call limit.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import quote

from backend.batch import BatchService, digest, now
from backend.batch_store import Conflict, Missing, configured_store, read_json, write_json
from backend.core.docintel import ParsedDocument
from backend.core.vendor_tables import VendorTableConfig, read_vendor_table
from backend.evidence_verification import Fragment, match_text
from backend.extract import source_evidence
from backend.models.enrichment import Candidate, Evidence, Manifest, OfflineSource, RetrievalOutcome
from backend.pilot import PARSER_VERSION
from backend.quality_pipeline import (
    SYSTEM, QualityExtraction, complete_quality_call, expand_citations,
    ground_candidate, product_packet, run_product,
)

FORD_PDF_SHA256 = "b50c311840c19d96fd994a2a8f281f243c37e63257aad81c41821e0df6910cfa"
FORD_PDF_BLOB = "documents/av-source-4.pdf"
SMOKE_ATTRIBUTE = "Operating Head Style"


class QualitySmokeError(RuntimeError):
    def __init__(self, message: str, *, candidates=None):
        super().__init__(message)
        self.candidates = [candidate.model_dump(mode="json") for candidate in candidates or []]


def _identity(text: str, term: str) -> bool:
    pattern = r"\s+".join(re.escape(part) for part in term.split())
    return bool(pattern) and re.search(rf"(?<![\w/-]){pattern}(?![\w/-])", text, re.I) is not None


def scope_document(document: ParsedDocument, product, binding: dict) -> ParsedDocument:
    """Retain family prose and exact-model rows, not neighboring model cells."""
    document = document.model_copy(deep=True)
    others = [p["mpn"] for p in binding.get("products", []) if p != product.model_dump()]
    suppressed_paragraph_texts = set()
    key_headers = {"mpn", "model", "model number", "model no", "part number", "part no",
                   "catalog number", "catalog no", "catalogue number", "product number", "item number"}
    for table in document.tables:
        headers = {(r, c): " ".join(re.findall(r"[a-z]+", cell.casefold()))
                   for r, row in enumerate(table.cells) for c, cell in enumerate(row)}
        headers = {position: label for position, label in headers.items() if label in key_headers}
        model_rows = [r for r, row in enumerate(table.cells)
                      if any(_identity(" ".join(row), mpn) for mpn in [product.mpn, *others])]
        product_table = bool(model_rows) or any(
            label == "mpn" or label.startswith(("model", "catalog")) for label in headers.values()
        )
        if product_table:
            # DI repeats table cells as paragraphs. Remove those duplicates so an
            # unlabelled size from a neighboring model cannot survive row filtering.
            suppressed_paragraph_texts.update(
                " ".join(text.split()).casefold() for row in table.cells
                for text in [*row, " ".join(row)] if text.strip()
            )
            matched_columns = {
                c for r in model_rows for c, cell in enumerate(table.cells[r])
                if any(_identity(cell, mpn) for mpn in [product.mpn, *others])
            }
            header_columns = {c for _, c in headers}
            columns = header_columns & matched_columns or header_columns or matched_columns
            first_data = min(r for r, _ in headers) + 1 if headers else min(model_rows)
            for index, row in enumerate(table.cells):
                if index < first_data or any(r == index for r, _ in headers):
                    continue
                keys = [row[c].strip() for c in columns if c < len(row) and row[c].strip()]
                if keys and not any(_identity(key, product.mpn) for key in keys):
                    others.extend(key for key in keys if key not in others)
                    table.cells[index] = [""] * len(row)
        else:
            # An unkeyed family dimension schedule cannot identify this variant.
            dimension_schedule = False
            for index, row in enumerate(table.cells):
                text = " ".join(row)
                if "METER CONNX SIZE" in text.upper():
                    dimension_schedule = True
                elif dimension_schedule and (not text.strip() or "PART NUMBER" in text.upper()):
                    dimension_schedule = False
                if dimension_schedule:
                    suppressed_paragraph_texts.update(" ".join(cell.split()).casefold() for cell in row if cell.strip())
                    table.cells[index] = [""] * len(row)
    for paragraph in document.paragraphs:
        if (" ".join(paragraph.text.split()).casefold() in suppressed_paragraph_texts
                or any(_identity(paragraph.text, mpn) for mpn in others) and not _identity(paragraph.text, product.mpn)):
            paragraph.text = ""
    document.raw_text = ""
    return document


class CachedEvidenceLoader:
    def __init__(self, store):
        self.store = store
        self.parses = None
        self.contents = {}

    def cached_document(self, binding: dict) -> ParsedDocument:
        location = "batchblob:///" + binding["blob"]
        stem = location + binding["sha256"] + PARSER_VERSION
        keys = ["parses/" + digest((stem + suffix).encode()) + ".json" for suffix in ("", ":pages=1-5", ":pages=1")]
        for key in keys:
            try:
                cached, _ = read_json(self.store, key)
            except Missing:
                continue
            return self._validate_cache(cached, binding, location)
        # Existing bounded catalog preparations may have a different page suffix.
        if self.parses is None:
            self.parses = self.store.keys("parses/")
        for key in self.parses:
            cached, _ = read_json(self.store, key)
            source = cached.get("document", {})
            if source.get("source") == location and source.get("cache_key") == "sha256:" + binding["sha256"]:
                return self._validate_cache(cached, binding, location)
        raise Missing("Compatible cached parse unavailable; DI is disabled.")

    @staticmethod
    def _validate_cache(cached, binding, location):
        document = ParsedDocument.model_validate(cached["document"])
        if (cached["parser_version"] != PARSER_VERSION or document.source != location
                or document.cache_key != "sha256:" + binding["sha256"]
                or digest(document.model_dump_json().encode()) != cached["document_sha256"]):
            raise ValueError("Stored parse integrity or source association mismatch")
        return document

    def load(self, item: dict) -> tuple[list[Evidence], list[RetrievalOutcome], list[dict]]:
        manifest = Manifest.model_validate(item["manifest"])
        product = manifest.product
        observed = datetime.now(timezone.utc)
        evidence, retrieval, provenance = [], [], []
        for binding in item.get("sources", []):
            if binding.get("format") not in {"pdf", "xlsx"}:
                continue
            tier = binding.get("source_tier", "internal_pdf")
            entry = {"source_id": binding["source_id"], "source_tier": tier, "new_di": False}
            try:
                if binding.get("kind") != "blob":
                    raise ValueError("Only stored document copies are read; no SharePoint retrieval")
                if product.model_dump() not in binding.get("products", []):
                    raise ValueError("Source is not associated with this product")
                scope = next((s for s in binding.get("applicability", []) if s["product"] == product.model_dump()), {})
                qualification = scope.get("qualification", "Exact product association; shared family wording requires review.")
                if binding["format"] == "pdf":
                    document = scope_document(self.cached_document(binding), product, binding)
                    source = OfflineSource(
                        source_id=binding["source_id"], product=product, document=document,
                        qualification=qualification, attribute_ids=scope.get("attribute_ids"),
                        provider_retrieved_at=document.parsed_at,
                    )
                    evidence.extend(source_evidence(source, observed))
                    entry["parsing"] = "cached_only"
                else:
                    blob = binding["blob"]
                    if blob not in self.contents:
                        self.contents[blob] = self.store.read_bytes(blob, max_bytes=32 * 1024 * 1024)[0]
                    content = self.contents[blob]
                    if digest(content) != binding["sha256"]:
                        raise ValueError("Vendor workbook hash mismatch")
                    rows = read_vendor_table(content, config=VendorTableConfig.model_validate(binding["table"]),
                                             product=product, source_id=binding["source_id"])
                    if not rows:
                        raise ValueError("No full exact-MPN vendor row matched")
                    for row in rows:
                        evidence.append(Evidence(
                            evidence_id=f"{binding['source_id']}:{binding['sha256']}:{row.sheet}:{row.row}",
                            source_id=binding["source_id"], source_tier="vendor_table", content_kind="source_excerpt",
                            source_locator=f"batchblob:///{blob}#sheet={quote(row.sheet, safe='')}&row={row.row}&cells={','.join(row.cells)}",
                            source_version="sha256:" + binding["sha256"], text=row.text, observed_at=observed,
                            provider_retrieved_at=observed, qualification=qualification,
                            attribute_ids=scope.get("attribute_ids"),
                        ))
                    entry["rows"] = [row.row for row in rows]
                retrieval.append(RetrievalOutcome(source_tier=tier, source_id=binding["source_id"], status="success"))
                entry["status"] = "success"
            except (Missing, ValueError, KeyError) as error:
                code = "cache_or_source_unavailable"
                retrieval.append(RetrievalOutcome(source_tier=tier, source_id=binding["source_id"], status="failed", error_code=code))
                entry.update(status="failed", error=code, explanation=str(error))
            provenance.append(entry)
        return evidence, retrieval, provenance


def _image(store, blob: str | None) -> list[str] | None:
    if not blob:
        return None
    content, _ = store.read_bytes(blob, max_bytes=16 * 1024 * 1024)
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    else:
        raise ValueError("QUALITY_FORD_IMAGE_BLOB must point to a rendered PNG/JPEG, not the cached PDF hash.")
    from PIL import Image

    try:
        with Image.open(BytesIO(content)) as image:
            if image.width * image.height > 16_000_000:
                raise ValueError("Image pixel limit exceeded")
            image.verify()
    except Exception as error:
        raise ValueError("QUALITY_FORD_IMAGE_BLOB must contain a valid bounded PNG/JPEG.") from error
    return [f"data:{mime};base64," + base64.b64encode(content).decode("ascii")]


def render_ford_page(content: bytes) -> str:
    """Render only the approved existing PDF's first page, entirely in memory."""
    if not content.startswith(b"%PDF-") or digest(content) != FORD_PDF_SHA256:
        raise ValueError("Ford image rendering requires the hash-verified approved PDF.")
    # With no output prefix, Poppler sends PNG to stdout; "-" reads PDF stdin.
    rendered = subprocess.run(
        ["pdftoppm", "-f", "1", "-l", "1", "-singlefile", "-scale-to", "2400", "-png", "-"],
        input=content, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=120,
    ).stdout
    if not rendered.startswith(b"\x89PNG\r\n\x1a\n") or len(rendered) > 16 * 1024 * 1024:
        raise ValueError("Ford renderer did not return a bounded PNG image.")
    return "data:image/png;base64," + base64.b64encode(rendered).decode("ascii")


def image_input(store, blob: str | None) -> tuple[list[str] | None, dict]:
    diagnostic = {"operation": "image_input", "tier": "internal_pdf", "phase": "catalog_image"}
    override_error = None
    if blob:
        try:
            images = _image(store, blob)
            diagnostic.update(status="available", text_only=False, image_source="image_override",
                              reason="Ford catalog image override is available; each model request explicitly names the target MPN.")
            return images, diagnostic
        except Exception as error:
            override_error = error
            diagnostic["override_error_type"] = type(error).__name__
    try:
        content, _ = store.read_bytes(FORD_PDF_BLOB, max_bytes=16 * 1024 * 1024)
        images = [render_ford_page(content)]
    except Exception as error:
        display_error = override_error or error
        diagnostic.update(
            status="missing" if isinstance(display_error, Missing) else "invalid" if isinstance(display_error, ValueError) else "unavailable",
            error_type=type(display_error).__name__, render_error_type=type(error).__name__, text_only=True,
            image_source="cached_pdf_render",
            reason="Ford page rendering was unavailable; cached PDF text processing continued. Ensure the approved PDF is readable and pdftoppm is installed, or supply a valid existing image override, then retry with a new run ID.",
        )
        return None, diagnostic
    diagnostic.update(status="available", text_only=False, image_source="cached_pdf_render", page=1,
                      reason="Page 1 of the hash-verified existing Ford PDF was rendered in memory; each model request explicitly names the target MPN. No new DI or image upload was used.")
    return images, diagnostic


def run_model_smoke(record, loader, completion, *, run_id, usage_callback=None, before_call=None) -> dict:
    """One model observation; ambiguous or unsupported answers do not gate work."""
    item = next((item for item in record["items"]
                 if item["manifest"]["product"]["item_id"].removeprefix("PIMITEM-") == "213030"), None)
    if item is None:
        raise QualitySmokeError("Model smoke requires the stored Mueller 213030 product.")
    manifest = Manifest.model_validate(item["manifest"])
    if "mueller" not in manifest.product.vendor.casefold() or manifest.product.mpn.split() != ["014255", "215N"]:
        raise QualitySmokeError("Model smoke product identity must be Mueller 213030 / 014255 215N.")
    definition = next((a for a in manifest.attributes if a.attribute_id == SMOKE_ATTRIBUTE), None)
    if definition is None:
        raise QualitySmokeError("Stored manifest is missing Operating Head Style.")
    evidence, _, _ = loader.load(item)
    vendor = [entry for entry in evidence if entry.source_tier == "vendor_table"]
    target = None
    cell_value = None
    for entry in vendor:
        row = json.loads(entry.text)
        if row.get("row") == 1096:
            cell = next((cell for cell in row["cells"] if cell["cell"] == "T1096"), None)
            if cell:
                target, cell_value = entry, str(cell["value"])
                break
    if target is None or (cell_value or "").strip().casefold() != "lockwing":
        raise QualitySmokeError("Stored Mueller vendor cell T1096 must contain Lockwing; no model call was made.")
    packet = product_packet(manifest, evidence, "vendor_table", [SMOKE_ATTRIBUTE])
    packet["smoke_target"] = {"attribute": SMOKE_ATTRIBUTE, "source_row": 1096, "source_cell": "T1096"}
    response = complete_quality_call(
        completion, SYSTEM, packet, QualityExtraction,
        context={"operation": "model", "item_id": manifest.product.item_id, "item_key": item["item_key"],
                 "run_id": run_id, "call_id": f"{run_id}:{item['item_key']}:1",
                 "tier": "vendor_table", "phase": "smoke", "call_index": 1},
        usage_callback=usage_callback, before_call=before_call,
    )
    candidates = [proposal for proposal in response.candidates if proposal.attribute_id == SMOKE_ATTRIBUTE]
    grounded, rejected = [], []
    matches_expectation = False
    for proposal in candidates:
        try:
            candidate = ground_candidate(expand_citations(proposal, packet), definition, vendor)
        except (ValueError, TypeError) as error:
            rejected.append({"proposal": proposal.model_dump(mode="json"), "reason": str(error)})
            continue
        cell_match = match_text(candidate.supporting_quote or "", [[
            Fragment(target, cell_value, cell="T1096", vendor=True),
        ]])
        expected = (
            str(candidate.value).strip().casefold() == "lockwing"
            and target.evidence_id in candidate.evidence_ids and cell_match is not None
        )
        matches_expectation = matches_expectation or expected
        if expected and cell_match is not None and candidate.grounding is not None:
            candidate.grounding["quote"] = cell_match.model_dump(mode="json")
        if not expected:
            candidate.qualification = (
                (candidate.qualification + " ") if candidate.qualification else ""
            ) + (
                "Canary expected Lockwing at T1096; the grounded model interpretation differs. "
                "Review head style versus locking feature."
            )
        grounded.append(candidate)
    first = grounded[0] if grounded else None
    grounding = ((first.grounding or {}).get("quote", {})) if first else {}
    status = (
        "no_grounded_candidate" if not grounded else
        "multiple_grounded_interpretations" if len(grounded) > 1 else
        "passed" if matches_expectation and not rejected else "disagreed_with_expectation"
    )
    return {"status": status,
            "item_id": manifest.product.item_id, "mpn": manifest.product.mpn,
            "attribute_id": SMOKE_ATTRIBUTE, "value": first.value if first else None, "source_row": 1096,
            "source_cells": grounding.get("cells", []), "supporting_quote": first.supporting_quote if first else None,
            "grounding": grounding, "model_calls": 1,
            "expected_source": {"value": cell_value, "source_cells": ["T1096"],
                                "evidence_ids": [target.evidence_id], "supporting_quote": cell_value},
            "candidate": first.model_dump(mode="json") if first else None,
            "candidates": [candidate.model_dump(mode="json") for candidate in grounded],
            "observed_candidates": [candidate.model_dump(mode="json") for candidate in response.candidates],
            "rejected_candidates": rejected,
            "qualification": "Canary observation, not an approval or readiness gate; retain grounded interpretations, reject unsupported answers, and continue to full extraction and judging."}


def run_quality_batch(
    store, batch_id: str, owner: str, run_id: str, *, completion=None,
    usage_callback=None, web=None, ford_image_blob: str | None = None, before_call=None,
    smoke_only: bool = False,
    smoke_first: bool = False,
    cost_summary=None,
    execution_id: str | None = None,
) -> dict:
    """Append new immutable results and preserve the complete previous state chain."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
        raise ValueError("QUALITY_RUN_ID must be a safe unique identifier")
    execution_id = execution_id or uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", execution_id):
        raise ValueError("QUALITY_EXECUTION_ID must be a safe execution identifier")
    record = BatchService(store).get(batch_id, owner)
    prefix = f"quality-runs/{batch_id}/{run_id}"
    execution_prefix = f"{prefix}/executions/{execution_id}"
    started = {"run_id": run_id, "execution_id": execution_id, "batch_id": batch_id, "owner": owner, "started_at": now(),
               "smoke_only": smoke_only, "smoke_first": smoke_first and not smoke_only}
    write_json(store, execution_prefix + "/started.json", started)
    try:
        write_json(store, prefix + "/started.json", started)
    except Conflict:
        pass
    prior_usage_keys = [
        key for key in store.keys(prefix + "/usage/")
        if re.fullmatch(r"\d+\.json", key.rsplit("/", 1)[-1])
    ]
    prior_usage = [read_json(store, key)[0] for key in prior_usage_keys]
    sequence_offset = max((int(key.rsplit("/", 1)[-1][:-5]) for key in prior_usage_keys), default=0)
    try:
        prior_summary, _ = read_json(store, prefix + "/summary.json")
    except Missing:
        prior_summary = {}

    def latest(key, value):
        try:
            previous, version = read_json(store, key)
        except Missing:
            previous, version = None, None
        if previous is not None and key == prefix + "/summary.json":
            archive_id = previous.get("execution_id") or "legacy"
            try:
                write_json(store, f"{prefix}/executions/{archive_id}/summary.json", previous)
            except Conflict:
                pass
        write_json(store, key, value, version)

    loader = CachedEvidenceLoader(store)
    usage_records, products = [], []

    def call_context(usage):
        sequence = sequence_offset + len(usage_records) + 1
        return {**usage, "run_id": run_id, "batch_id": batch_id, "sequence": sequence,
                "execution_id": execution_id, "call_id": f"{run_id}:{sequence:04d}"}

    def before_usage(context):
        if before_call:
            before_call(call_context(context))

    def persist_usage(usage):
        entry = call_context(usage)
        entry["recorded_at"] = now()
        if entry.get("operation") == "model":
            for field in ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens",
                          "reasoning_tokens", "cost_usd", "usage_reported", "pricing_basis"):
                entry.setdefault(field, None)
        error = None
        try:
            enriched = usage_callback(entry) if usage_callback else None
            if isinstance(enriched, dict):
                entry.update(enriched)
        except Exception as raised:
            error = raised
            entry["meter_error"] = type(raised).__name__
        entry.update(call_context(entry))
        usage_records.append(entry)
        print(json.dumps({"quality_usage": entry}, ensure_ascii=True), flush=True)
        write_json(store, f"{prefix}/usage/{entry['sequence']:04d}.json", entry)
        if error:
            raise error
        return entry

    if web is not None:
        web.usage_callback = persist_usage
        web.before_call = before_usage
    failure = None
    smoke = None
    smoke_recorded = False
    try:
        if completion is None:
            from backend.core.quality_model import ResponsesCompletion
            completion = ResponsesCompletion()
        if smoke_only or smoke_first:
            smoke = run_model_smoke(record, loader, completion, run_id=run_id, usage_callback=persist_usage, before_call=before_usage)
            write_json(store, execution_prefix + "/smoke.json", smoke)
            latest(prefix + "/smoke.json", smoke)
            smoke_recorded = True
        has_ford = any(source.get("sha256") == FORD_PDF_SHA256 for item in record["items"] for source in item.get("sources", []))
        image, image_diagnostic = image_input(store, ford_image_blob) if not smoke_only and has_ford else (None, None)
        for item in [] if smoke_only else record["items"]:
            manifest = Manifest.model_validate(item["manifest"])
            item_key = item["item_key"]
            state_key = f"items/{batch_id}/{item_key}.json"
            try:
                prior, revision = read_json(store, state_key)
            except Missing:
                prior, revision = None, None
            attempt = digest(f"quality-v1:{batch_id}:{run_id}:{execution_id}:{item_key}".encode())
            result_key = f"results/{batch_id}/{item_key}/attempts/{attempt}.json"
            start = now()
            evidence, retrieval, provenance = loader.load(item)
            ford = any(s.get("sha256") == FORD_PDF_SHA256 for s in item.get("sources", []))
            reuse_smoke = smoke is not None and smoke.get("item_id") == manifest.product.item_id
            result = run_product(
                manifest, evidence, completion, run_id=run_id, item_key=item_key, web=web,
                images=image if ford else None, usage_callback=persist_usage,
                before_call=before_usage, retrieval=retrieval,
                initial_candidates={"vendor_table": [
                    Candidate.model_validate(candidate) for candidate in smoke["candidates"]
                ]} if reuse_smoke else None,
                initial_diagnostics=[entry for entry in usage_records if entry.get("phase") == "smoke"
                                     and entry.get("item_id") == manifest.product.item_id] if reuse_smoke else None,
            )
            result.quality_diagnostics.extend(
                entry for entry in usage_records
                if entry.get("operation") != "model" and entry.get("item_id") == manifest.product.item_id
            )
            if ford and image_diagnostic is not None:
                result.input_diagnostics.append({
                    **image_diagnostic, "item_id": manifest.product.item_id, "target_mpn": manifest.product.mpn,
                })
            write_json(store, result_key, result.model_dump(mode="json"))
            status = "completed" if all(a.status in {"existing", "proposed"} for a in result.attributes) else "unresolved"
            state = {"state": status, "run_id": run_id, "execution_id": execution_id,
                     "result_key": result_key, "started_at": start, "finished_at": now(),
                     "provenance": provenance, "reviewable_attributes": [a.attribute_id for a in result.attributes if a.candidates],
                     "quality_diagnostics": result.quality_diagnostics, "input_diagnostics": result.input_diagnostics}
            if prior is not None:
                state["previous_attempt"] = prior
            write_json(store, state_key, state, revision)
            products.append({"item_key": item_key, "item_id": manifest.product.item_id, "mpn": manifest.product.mpn,
                             "result_key": result_key, "state": status, "execution_id": execution_id,
                             "input_diagnostics": result.input_diagnostics,
                             "candidates": sum(len(a.candidates) for a in result.attributes)})
            write_json(store, f"{execution_prefix}/products/{item_key}.json", products[-1])
            latest(f"{prefix}/products/{item_key}.json", products[-1])
    except Exception as error:
        failure = error
        if (smoke_only or smoke_first) and smoke is None:
            smoke = {"status": "failed", "error": type(error).__name__,
                     "attribute_id": SMOKE_ATTRIBUTE, "expected_value": "Lockwing",
                     "source_row": 1096, "source_cells": ["T1096"],
                     "observed_candidates": error.candidates if isinstance(error, QualitySmokeError) else [],
                     "reason": str(error) if isinstance(error, QualitySmokeError) else "Model request or usage recording failed; inspect persisted usage diagnostics."}
    if (smoke_only or smoke_first) and not smoke_recorded:
        write_json(store, execution_prefix + "/smoke.json", smoke)
        latest(prefix + "/smoke.json", smoke)
    cost = None
    if cost_summary is not None:
        try:
            cost = cost_summary()
        except Exception as error:
            failure = failure or error
            cost = {"status": "unavailable", "error": type(error).__name__}
    all_usage = [*prior_usage, *usage_records]
    latest_products = {entry["item_key"]: entry for entry in prior_summary.get("products", [])}
    latest_products.update({entry["item_key"]: entry for entry in products})
    cumulative_products = [latest_products[item["item_key"]] for item in record["items"]
                           if item["item_key"] in latest_products]
    summary = {"schema_version": 1, "run_id": run_id, "execution_id": execution_id, "batch_id": batch_id, "owner": owner,
               "state": "failed" if failure else "completed", "finished_at": now(), "products": cumulative_products,
               "execution_products": products,
               "usage": all_usage, "model_calls": sum(e.get("operation") == "model" for e in all_usage),
               "execution_model_calls": sum(e.get("operation") == "model" for e in usage_records),
               "new_di_calls": 0, "error": type(failure).__name__ if failure else None,
               "smoke_only": smoke_only, "smoke_first": smoke_first and not smoke_only, "smoke": smoke,
               "cost": cost}
    write_json(store, execution_prefix + "/summary.json", summary)
    latest(prefix + "/summary.json", summary)
    if failure:
        raise failure
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", default=os.environ.get("QUALITY_BATCH_ID"))
    parser.add_argument("--owner", default=os.environ.get("QUALITY_OWNER"))
    parser.add_argument("--run-id", default=os.environ.get("QUALITY_RUN_ID"))
    parser.add_argument("--execution-id", default=os.environ.get("QUALITY_EXECUTION_ID"))
    parser.add_argument("--smoke-only", action="store_true", default=os.environ.get("QUALITY_SMOKE_ONLY", "false").lower() == "true",
                        help="Run only the one-call Mueller T1096 Lockwing model smoke; do not extract products.")
    parser.add_argument("--smoke-first", action=argparse.BooleanOptionalAction,
                        default=os.environ.get("QUALITY_SMOKE_FIRST", "true").lower() == "true",
                        help="Run the Mueller smoke then all products, reusing its candidate within the three-call tier limit.")
    args = parser.parse_args(argv)
    if not all((args.batch_id, args.owner, args.run_id)):
        parser.error("QUALITY_BATCH_ID, QUALITY_OWNER and QUALITY_RUN_ID (or CLI equivalents) are required")
    from backend.quality_web import QualityWeb
    from backend.quality_cost import QualityCostMeter

    store = configured_store()
    cost_run_id = os.environ.get("QUALITY_COST_RUN_ID") or args.run_id
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", cost_run_id):
        parser.error("QUALITY_COST_RUN_ID must be a safe run identifier")
    prices = {
        "run_base_cost_usd": os.environ.get("QUALITY_RUN_BASE_COST_USD", "0"),
        "overnight_prior_cost_usd": os.environ.get("QUALITY_OVERNIGHT_PRIOR_COST_USD", "0"),
        "worker_usd_per_second": os.environ.get("QUALITY_WORKER_USD_PER_SECOND", "0"),
        "search_usd": os.environ.get("QUALITY_WEB_SEARCH_USD_PER_CALL", "0.0125"),
        "browse_usd": os.environ.get("QUALITY_WEB_BROWSE_USD_PER_CALL", "0.0125"),
    }
    meter = QualityCostMeter(store, args.batch_id, cost_run_id, **prices)

    def priced_usage(entry):
        return {**meter.record(entry), "cost_run_id": cost_run_id}

    def cost_snapshot():
        return {
            **meter.summary(), "cost_run_id": cost_run_id,
            "worker_usd_per_second": float(prices["worker_usd_per_second"]),
            "worker_rate_configured": "QUALITY_WORKER_USD_PER_SECOND" in os.environ,
            "overnight_prior_cost_usd": float(prices["overnight_prior_cost_usd"]),
            "web_search_usd_per_call": float(prices["search_usd"]),
            "web_browse_usd_per_call": float(prices["browse_usd"]),
            "compute_reconciliation": "Replace this elapsed worker compute estimate with measured execution-time cost; do not add both.",
        }

    try:
        summary = run_quality_batch(
            store, args.batch_id, args.owner, args.run_id,
            web=QualityWeb() if not args.smoke_only and os.environ.get("QUALITY_WEB_ENABLED", "true").lower() == "true" else None,
            ford_image_blob=os.environ.get("QUALITY_FORD_IMAGE_BLOB"),
            smoke_only=args.smoke_only,
            smoke_first=args.smoke_first and not args.smoke_only,
            usage_callback=priced_usage, before_call=meter.before_call, cost_summary=cost_snapshot,
            execution_id=args.execution_id,
        )
    finally:
        print(json.dumps({"quality_cost": meter.summary()}, ensure_ascii=True), flush=True)
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
