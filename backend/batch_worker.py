"""Finite durable batch worker; never an HTTP server."""

import argparse
import json
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from azure.identity import ManagedIdentityCredential, get_bearer_token_provider

from backend.batch import digest, now
from backend.batch_store import Conflict, Missing, configured_store, read_json, write_json
from backend.core.docintel import DocumentIntelligenceService, ParsedDocument
from backend.core.llm import LLMClient, COGNITIVE_SERVICES_SCOPE
from backend.extract import run_enrichment
from backend.models.enrichment import ExtractionResponse, LiveBundle, Manifest, OfflineBundle, OfflineSource, ProductKey, ReviewAnnotation
from backend.pilot import PARSER_VERSION, QUALIFICATIONS


class ManagedCompletion:
    def __init__(self):
        self.client = LLMClient(token_provider=get_bearer_token_provider(ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID") or None), COGNITIVE_SERVICES_SCOPE))
        self.client.sync_client = self.client.sync_client.with_options(max_retries=0)

    def complete_structured(self, system, user, schema):
        return self.client.complete_structured(system, user, schema, max_retries=1)


class BatchProcessor:
    def __init__(self, store):
        self.store = store
        self.mutex = threading.Lock()
        self.source_locks = {}

    def reserve(self, operation):
        approval, _ = read_json(self.store, "configuration/live-approval.json")
        approval_id = str(uuid.UUID(approval["id"]))
        if not approval.get("approved_by") or datetime.fromisoformat(approval["expires_at"]) <= datetime.now(timezone.utc):
            raise ValueError("Explicit unexpired operator approval required")
        limit = approval.get(f"{operation}_limit", 0)
        if type(limit) is not int or not 0 <= limit <= {"analysis": 1, "inference": 2}[operation]:
            raise ValueError("Operation budget exceeds the remaining pilot ceiling")
        key = f"budgets/{approval_id}.json"
        for attempt in range(3):
            try:
                used, version = read_json(self.store, key)
            except Missing:
                used, version = {"analysis": 0, "inference": 0}, None
            if used[operation] >= limit:
                raise ValueError("Approved operation budget exhausted")
            used[operation] += 1
            try:
                write_json(self.store, key, used, version)
                return approval_id
            except Conflict:
                if attempt == 2:
                    raise

    def cached(self, key, binding, location):
        cached, _ = read_json(self.store, key)
        document = ParsedDocument.model_validate(cached["document"])
        if cached["parser_version"] != PARSER_VERSION or digest(document.model_dump_json().encode()) != cached["document_sha256"] or document.source != location or document.cache_key != "sha256:" + binding["sha256"]:
            raise ValueError("Incompatible parse cache or source association")
        return document, cached["origin"]

    def source(self, binding, product, allow_analysis):
        provenance = {"source_id": binding["source_id"], "kind": binding["kind"], "reference": binding["reference"], "retrieval": "failed", "parsing": "not_attempted"}
        if binding["kind"] == "sharepoint":
            provenance.update(error="sharepoint_download_401", retained_stages=[200, 302, 401], sha256=None)
            return OfflineSource(source_id=binding["source_id"], product=product, error_code="access_denied"), provenance
        key = binding.get("blob", "")
        if binding["kind"] != "blob" or not key.startswith("documents/") or ".." in key.split("/") or not binding.get("sha256"):
            provenance["error"] = "unapproved_source_configuration"
            return OfflineSource(source_id=binding["source_id"], product=product, error_code="not_found"), provenance
        try:
            content, etag = self.store.read_bytes(key, max_bytes=10 * 1024 * 1024)
            if len(content) > 10 * 1024 * 1024 or not content.startswith(b"%PDF-") or digest(content) != binding["sha256"]:
                raise ValueError("Source content version mismatch")
            location = "batchblob:///" + key
            provenance.update(retrieval="succeeded", sha256=digest(content), etag=etag, location=location)
            cache_key = "parses/" + digest((location + binding["sha256"] + PARSER_VERSION).encode()) + ".json"
            with self.mutex:
                lock = self.source_locks.setdefault(cache_key, threading.Lock())
            with lock:
                try:
                    document, origin = self.cached(cache_key, binding, location)
                    provenance.update(parsing="cache", parse_origin=origin)
                except Missing:
                    if not allow_analysis:
                        provenance["error"] = "compatible_parse_unavailable_analysis_not_authorized"
                        return OfflineSource(source_id=binding["source_id"], product=product, error_code="parse_failed"), provenance
                    with self.store.lease(cache_key):
                        try:
                            document, origin = self.cached(cache_key, binding, location)
                            provenance.update(parsing="cache", parse_origin=origin)
                        except Missing:
                            write_json(self.store, "analysis-attempts/" + cache_key, {"state": "reserved_no_automatic_retry", "started_at": now()})
                            approval_id = self.reserve("analysis")
                            document = DocumentIntelligenceService(credential=ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID") or None)).extract_pdf_bytes(content, source=location)
                            if document.source != location or document.cache_key != "sha256:" + binding["sha256"]:
                                raise ValueError("Parsed source association mismatch")
                            write_json(self.store, cache_key, {"parser_version": PARSER_VERSION, "origin": "batch_fresh_analysis", "approval_id": approval_id, "document": document.model_dump(mode="json"), "document_sha256": digest(document.model_dump_json().encode())})
                            provenance.update(parsing="fresh_analysis", parse_origin="batch_fresh_analysis")
                if document.source != location or document.cache_key != "sha256:" + binding["sha256"]:
                    raise ValueError("Parsed source association mismatch")
                provenance.update(parsed_at=document.parsed_at.isoformat(), parser_version=PARSER_VERSION)
                return OfflineSource(source_id=binding["source_id"], product=product, document=document, provider_retrieved_at=datetime.now(timezone.utc)), provenance
        except Missing:
            provenance["error"] = "source_not_found"
        except Exception:
            provenance["error"] = "source_or_parse_failed_no_retry"
        return OfflineSource(source_id=binding["source_id"], product=product, error_code="parse_failed"), provenance

    def __call__(self, item, mode):
        manifest = Manifest.model_validate(item["manifest"])
        product = manifest.product
        approved = False
        identity_terms = []
        try:
            approval, _ = read_json(self.store, "configuration/live-approval.json")
            identity_terms = approval["document_identity_terms"]
            approved = (
                product == ProductKey.model_validate(approval["product"])
                and isinstance(identity_terms, list) and bool(identity_terms)
                and all(isinstance(term, str) and term.strip() for term in identity_terms)
                and bool(re.fullmatch(r"[a-f0-9]{64}", approval["sha256"]))
                and {attribute.attribute_id for attribute in manifest.attributes} <= set(QUALIFICATIONS)
                and bool(item["sources"])
                and all(source.get("sha256") == approval["sha256"] for source in item["sources"])
            )
        except (Missing, KeyError, ValueError, TypeError):
            approved = False
        live = mode == "live_inference" and approved and os.environ.get("DOCINTEL_BATCH_LIVE_ENABLED") == "true"
        supplied = [self.source(binding, product, live) for binding in item["sources"]]
        sources = [entry[0] for entry in supplied]
        provenance = [entry[1] for entry in supplied]
        errors = [entry["error"] for entry in provenance if entry.get("error")]
        if mode == "live_inference" and not live:
            errors.append("live_processing_not_authorized_for_this_scope")
        if live and not errors:
            combined = "\n".join(source.document.raw_text + "\n" + "\n".join(paragraph.text for paragraph in source.document.paragraphs) for source in sources)
            if product.mpn not in combined or not all(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", combined, re.IGNORECASE) for term in identity_terms):
                errors.append("approved_product_identity_not_established_in_document")
        if live and not errors:
            try:
                self.reserve("inference")
            except (Missing, ValueError, KeyError):
                errors.append("explicit_live_approval_or_budget_unavailable")
        if live and not errors:
            completion = ManagedCompletion()
            try:
                result = run_enrichment(LiveBundle(execution_mode="live_inference", manifest=manifest, sources=sources), execution_mode="live_inference", completion=completion)
            finally:
                completion.client.sync_client.close()
        else:
            result = run_enrichment(OfflineBundle(manifest=manifest, sources=sources, generated_response=ExtractionResponse(candidates=[])))
        if approved:
            for attribute in result.attributes:
                for index in range(len(attribute.candidates)):
                    attribute.review_annotations.append(ReviewAnnotation(candidate_index=index, text=QUALIFICATIONS[attribute.attribute_id], author="Post-generation qualification; not source evidence or approval", annotated_at=datetime.now(timezone.utc)))
        if result.extraction_error:
            errors.append(result.extraction_error)
        unresolved = errors or any(attribute.status in {"missing_evidence", "retrieval_failed", "extraction_failed", "conflict"} for attribute in result.attributes)
        return result, {"state": "unresolved" if unresolved else "completed", "error": "; ".join(errors), "provenance": provenance}


def run_batch(store, batch_id, *, concurrency=2, item_limit=100, processor=None):
    if not 1 <= concurrency <= 4 or not 1 <= item_limit <= 1000:
        raise ValueError("Worker limits: concurrency 1-4; items 1-1000")
    process = processor or BatchProcessor(store)
    path = f"batches/{batch_id}.json"
    with store.lease(batch_id) as renew:
        stopped = threading.Event()
        lost = threading.Event()

        def heartbeat():
            while not stopped.wait(15):
                try:
                    renew()
                except Exception:
                    lost.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()

        def fence():
            if lost.is_set():
                raise Conflict("Worker lease lost")
            renew()

        try:
            record, version = read_json(store, path)
            if record["state"] not in {"queued", "running"}:
                return
            record["state"] = "running"
            version = write_json(store, path, record, version)
            pending = []
            for item in record["items"]:
                key = f"items/{batch_id}/{item['item_key']}.json"
                try:
                    state, item_version = read_json(store, key)
                    if state["state"] == "running":
                        try:
                            read_json(store, f"results/{batch_id}/{item['item_key']}.json")
                            state.update(state="unresolved", error="Recovered persisted result after interrupted status update; inspect before review")
                        except Missing:
                            state.update(state="interrupted", error="Prior worker stopped after reservation; remote completion unknown. No automatic resubmission")
                        fence()
                        write_json(store, key, state, item_version)
                except Missing:
                    pending.append(item)

            def execute(item):
                key = f"items/{batch_id}/{item['item_key']}.json"
                fence()
                state = {"id": item["item_key"], "batch_id": batch_id, "state": "running", "started_at": now(), "requested_mode": record["mode"]}
                item_version = write_json(store, key, state)
                try:
                    result, outcome = process(item, record["mode"])
                    fence()
                    write_json(store, f"results/{batch_id}/{item['item_key']}.json", result.model_dump(mode="json"))
                    state.update(outcome)
                    state["reviewable_attributes"] = [attribute.attribute_id for attribute in result.attributes if attribute.status != "existing"]
                except Conflict:
                    raise
                except Exception:
                    state.update(state="failed", error="Item execution failed; provider details withheld. No automatic retry")
                fence()
                state["finished_at"] = now()
                write_json(store, key, state, item_version)

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                list(pool.map(execute, pending[:item_limit]))
            fence()
            record["state"] = "queued" if len(pending) > item_limit else "completed"
            record["updated_at"] = now()
            states = [read_json(store, key)[0]["state"] for key in store.keys(f"items/{batch_id}/")]
            record["progress"] = {"finished": sum(state not in {"running", "queued"} for state in states), "unresolved": states.count("unresolved"), "failed": states.count("failed") + states.count("interrupted")}
            write_json(store, path, record, version)
        finally:
            stopped.set()
            thread.join()


def main():
    parser = argparse.ArgumentParser(description="Process finite queued batch slices; no HTTP server")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--max-batches", type=int, default=1)
    parser.add_argument("--item-limit", type=int, default=100)
    parser.add_argument("--batch-id")
    parser.add_argument("--synthetic-acceptance", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_batches <= 10:
        parser.error("max-batches must be 1-10")
    if args.batch_id is not None and not re.fullmatch(r"[a-f0-9]{64}", args.batch_id):
        parser.error("batch-id must be a SHA-256 identifier")
    if args.synthetic_acceptance and (not args.batch_id or args.max_batches != 1 or not 1 <= args.item_limit <= 2 or not 1 <= args.concurrency <= 2):
        parser.error("Synthetic acceptance requires an explicit batch-id, one batch, and concurrency/item limits of 1-2")
    try:
        store = configured_store()
        count = 0
        paths = [f"batches/{args.batch_id}.json"] if args.batch_id else store.keys("batches/")
        for path in paths:
            record, _ = read_json(store, path)
            if args.synthetic_acceptance:
                products = [item["manifest"]["product"] for item in record["items"]]
                expected = [{"item_id": f"{index:03d}", "vendor": "Synthetic", "mpn": f"PART-{index}", "hierarchy_node": "Valve"} for index in (1, 2)]
                if record["mode"] != "evidence_only" or products != expected or os.environ.get("DOCINTEL_BATCH_LIVE_ENABLED", "false") != "false":
                    raise ValueError("Only the two-product no-AI synthetic acceptance batch may execute")
            if record["state"] not in {"queued", "running"}:
                continue
            try:
                run_batch(store, record["id"], concurrency=args.concurrency, item_limit=args.item_limit)
                count += 1
            except Conflict:
                continue
            if count >= args.max_batches:
                break
        return 0
    except Exception:
        print("Batch worker failed. Check private storage, managed identity, and configured limits; no sensitive details logged.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())