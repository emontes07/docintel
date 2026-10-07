"""Finite durable batch worker; never an HTTP server."""

import argparse
import json
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic, sleep
from urllib.parse import quote, urlsplit

from azure.identity import ManagedIdentityCredential, get_bearer_token_provider
from azure.core.exceptions import AzureError
from pydantic import ValidationError

from backend.batch import digest, now
from backend.batch_store import Conflict, Missing, configured_store, read_json, write_json
from backend.core.docintel import DocumentIntelligenceError, DocumentIntelligenceService, ParsedDocument
from backend.core.llm import LLMClient, LLMSchemaValidationError, COGNITIVE_SERVICES_SCOPE, preflight_structured_request
from backend.core.websearch import WebSearchError, validate_original_url
from backend.core.websearch_policy import OptionalWebPolicy, configured_optional_web_policy
from backend.extract import ExecutionConfigurationError, run_enrichment
from backend.models.enrichment import Evidence, ExtractionResponse, LiveBundle, Manifest, OfflineBundle, OfflineSource, ProductKey, ReviewAnnotation
from backend.pilot import PARSER_VERSION, QUALIFICATIONS
from backend.multisource import OptionalTierSkipped, WEB_TIERS, attribute_resolved, run_cascade
from backend.pdf_presentation import pdf_items
from backend.telemetry import ItemTelemetry, milliseconds, record_accounting
from backend.real_pilot import BUDGET_KEY, RealPilotBudgetExceeded, RealPilotGuard, binding_digest
from backend.sdk_preflight import SDKPreflightError
from backend.response_validation import (
    CitationReferences, ResponseValidationError, map_citations, parsed_content, response_diagnostic, schema_issues,
)


COMPACT_PROMPT_FORMAT = "real-evidence-rows-v3"
COMPACT_PROMPT_INSTRUCTIONS = """
Evidence rows use evidence_columns as headers. Cite the FIRST element of each
supporting row in candidate.evidence_ids: a JSON list such as ["E1", "E2"].
Group indexes, text indexes, source IDs and location suffixes are NOT citations.
Never combine several references into one string. Every candidate needs at least
one known row reference. Order and duplicates do not change citation meaning.
Surrounding whitespace and lowercase e are accepted; use canonical E1 spelling.
Exact original IDs are aliases unless they collide with a row-reference label.
Each row inherits metadata, attribute_ids and qualification from its group.
text_index selects evidence_texts. PDF table_row items reconstruct one physical
row with source header labels; their reference restores ALL contributing original
cells and headers. Paragraph items preserve notes and title text separately.
location identifies the anchor, not the full citation; presentation records its
page/table/row. Row numbers, column labels and part-index numbers are not product
values. Do not mix materials or variants merely because they share a row.
context_only items contain drawing metadata; use only explicit product facts.
Select only supporting rows; do not infer citations from a group or invent IDs.
"""


def compact_inference_prompt(user: str) -> tuple[str, CitationReferences]:
    payload = json.loads(user)
    pdf = [Evidence.model_validate(entry) for entry in payload["evidence"]
           if entry.get("source_tier") == "internal_pdf"]
    projected = pdf_items(pdf)
    entries = [entry for entry in payload["evidence"] if entry.get("source_tier") != "internal_pdf"]
    entries.extend(item.prompt_entry() for item in projected)
    origins = {item.anchor.evidence_id: [entry.evidence_id for entry in item.originals] for item in projected}
    groups = {}
    for entry in entries:
        shared = {name: value for name, value in entry.items()
                  if name not in {"evidence_id", "source_locator", "text", "presentation"}}
        key = json.dumps(shared, sort_keys=True, ensure_ascii=False)
        if key not in groups:
            groups[key] = (shared, [])
        groups[key][1].append(entry)
    references: dict[str, str | list[str]] = {}
    compact = []
    metadata = []
    texts = {}
    for group_index, (shared, entries) in enumerate(groups.values()):
        id_prefix = os.path.commonprefix([entry["evidence_id"] for entry in entries])
        locator_prefix = os.path.commonprefix([entry["source_locator"] for entry in entries])
        metadata.append({
            **shared, "evidence_id_prefix": id_prefix, "source_locator_prefix": locator_prefix,
        })
        for entry in entries:
            text = entry["text"]
            if text not in texts:
                texts[text] = len(texts)
            reference = f"E{len(references) + 1}"
            original_ids = origins.get(entry["evidence_id"], [entry["evidence_id"]])
            references[reference] = original_ids[0] if len(original_ids) == 1 else original_ids
            id_suffix = entry["evidence_id"][len(id_prefix):]
            locator_suffix = entry["source_locator"][len(locator_prefix):]
            location = id_suffix if id_suffix == locator_suffix else [id_suffix, locator_suffix]
            compact.append([reference, group_index, texts[text], location, entry.get("presentation")])
    payload.update(
        evidence=compact, evidence_groups=metadata, evidence_texts=list(texts),
        evidence_columns=["evidence_id", "group", "text_index", "location", "presentation"],
        prompt_format=COMPACT_PROMPT_FORMAT,
    )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")), references


@dataclass(frozen=True)
class PreparedInferenceRequest:
    system: str
    user: str
    references: CitationReferences
    request_parameters: dict
    version: str
    accounting: dict[str, int]

    @property
    def input_bound(self) -> int:
        return self.accounting["max_input_tokens"]


def prepare_inference_request(system: str, user: str, schema, *, deployment: str) -> PreparedInferenceRequest:
    """Pure complete-payload reservation API, shared by readiness and execution.

    UTF-8 byte lengths conservatively upper-bound tokens; this is not a tokenizer.
    No evidence, definitions, metadata, instructions or schema are truncated.
    """
    compact, references = compact_inference_prompt(user)
    system += COMPACT_PROMPT_INSTRUCTIONS
    parameters = {"max_completion_tokens": 2048}
    if deployment == "gpt-5":
        parameters["reasoning_effort"] = "minimal"
    response_schema = schema.model_json_schema()
    version_input = system + compact + response_schema.__repr__()
    if "reasoning_effort" in parameters:
        version_input += "\n" + json.dumps({"deployment": deployment, **parameters}, sort_keys=True)
    accounting = {
        "system_utf8_bytes": len(system.encode()),
        "user_utf8_bytes": len(compact.encode()),
        "response_schema_utf8_bytes": len(json.dumps(response_schema).encode()),
        "framing_allowance": 4096,
        "max_output_tokens": 2048,
    }
    accounting["max_input_tokens"] = sum(accounting[name] for name in (
        "system_utf8_bytes", "user_utf8_bytes", "response_schema_utf8_bytes", "framing_allowance",
    ))
    return PreparedInferenceRequest(
        system, compact, references, parameters, digest(version_input.encode()), accounting,
    )


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
        if binding.get("format", "pdf") != "pdf":
            provenance.update(error="source_requires_separately_approved_real_pilot", retrieval="not_attempted")
            return OfflineSource(
                source_id=binding["source_id"], product=product, error_code="access_denied",
                source_tier=binding.get("source_tier", "internal_pdf"),
            ), provenance
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


REAL_PROMPT_VERSION = "multisource-qualified-v1"


def identity_matches(text, terms):
    return bool(terms) and all(
        re.search(rf"(?<![\w./-]){re.escape(term)}(?![\w./-])", text, re.IGNORECASE)
        for term in terms
    )


class RealBatchProcessor(BatchProcessor):
    """Explicit approved real pilot; the legacy synthetic path remains unchanged."""

    def __init__(self, store, record, guard, *, optional_web_policy: OptionalWebPolicy | None = None):
        super().__init__(store)
        self.record = record
        self.guard = guard
        self.downloads = {}
        self.inference_provenance = []
        self.operation_provenance = []
        self._active_tier = None
        self._last_recovery_inference = None
        self.optional_web_policy = optional_web_policy.model_copy(deep=True) if optional_web_policy else None
        self.optional_attempted = {"search": 0, "web_retrieval": 0, "inference": 0}
        self.optional_item_attempted = {}
        self.optional_cost = 0
        if self.optional_web_policy:
            if (guard.execution_scope != "full"
                    or (guard.recovery is not None and guard.gapfill is None
                        and getattr(guard, "four_product", None) is None)
                    or self.optional_web_policy.batch_sha256 != binding_digest(record)):
                raise ExecutionConfigurationError("Optional web requires a separately authorized full, exact-batch scope")
            selected = {entry["item_key"]: entry for entry in record["items"]}
            for item_key, public in self.optional_web_policy.items.items():
                if item_key not in selected or public.mpn != selected[item_key]["manifest"]["product"]["mpn"]:
                    raise ExecutionConfigurationError("Public web scope does not match the selected product")
                item = selected[item_key]
                if (not set(public.attribute_terms) <= {entry["attribute_id"] for entry in item["manifest"]["attributes"]}
                        or not set(public.source_ids) <= {entry["source_id"] for entry in item["sources"] if entry["kind"] == "web"}):
                    raise ExecutionConfigurationError("Public web scope contains unapproved attributes or sources")

    def cached(self, key, binding, location):
        document, origin = super().cached(key, binding, location)
        try:
            self.guard.check_recovery_cache(key)
        except (ValueError, Missing) as error:
            raise ExecutionConfigurationError("Recovery parse cache binding changed") from error
        return document, origin

    def reserve_real(self, *args, **kwargs):
        try:
            paced = self.guard.recovery is not None and args[0] == "inference"
            if paced and self._last_recovery_inference is not None:
                four = getattr(self.guard, "four_product", None)
                interval = four["inference_interval_seconds"] if four is not None else 61
                delay = interval - (monotonic() - self._last_recovery_inference)
                if delay > 0:
                    sleep(delay)
            reservation = self.guard.reserve(*args, **kwargs)
            if "reserved_usage" in reservation and "reserved_microdollars" in reservation:
                self.operation_provenance.append({
                    "source_tier": self._active_tier, "operation": args[0],
                    "reserved_usage": dict(reservation["reserved_usage"]),
                    "reserved_microdollars": reservation["reserved_microdollars"],
                })
            if paced:
                self._last_recovery_inference = monotonic()
            return reservation
        except RealPilotBudgetExceeded as error:
            if self.optional_web_policy is not None:
                raise ExecutionConfigurationError("Global reservation guard stopped optional web execution") from error
            raise
        except (ValueError, Missing) as error:
            raise ExecutionConfigurationError("Real-pilot authorization or consumption guard stopped execution") from error

    def key(self, item, purpose, version):
        return self.guard.operation_key(
            item, tier=purpose, source_version=version, prompt_version=REAL_PROMPT_VERSION,
        )

    def optional_guard_state(self):
        """Revalidate authority even when a local optional cap will skip the call."""
        try:
            with self.guard._mutex, self.store.lease(BUDGET_KEY):
                approval, ledger, _ = self.guard._fresh_ledger()
                if self.guard._execution_id not in ledger["executions"]:
                    raise ValueError("Optional web requires a reserved worker execution")
                return approval
        except (ValueError, Missing) as error:
            raise ExecutionConfigurationError("Optional web execution guard stopped execution") from error

    def reserve_optional(self, operation, key, *, item_key, max_input_tokens=0, max_output_tokens=0):
        policy = self.optional_web_policy
        if policy is None:
            raise ExecutionConfigurationError("Optional web policy was not supplied")
        approval = self.optional_guard_state()
        public = policy.items.get(item_key)
        if public is None:
            raise OptionalTierSkipped("optional_public_scope_not_selected")
        limits = {
            "search": (policy.max_search_calls, 1),
            "web_retrieval": (policy.max_direct_page_attempts, public.max_direct_page_attempts),
            "inference": (policy.max_inference_calls, 1),
        }
        total_limit, item_limit = limits[operation]
        if (self.optional_attempted[operation] >= total_limit
                or self.optional_item_attempted.get((item_key, operation), 0) >= item_limit):
            raise OptionalTierSkipped("optional_operation_capacity")
        if operation == "inference" and max_input_tokens > policy.max_input_tokens:
            raise OptionalTierSkipped("optional_complete_input_capacity")
        cost = self.guard._cost(approval, operation, max_input_tokens, max_output_tokens, 0)
        if self.optional_cost + cost > policy.max_cost_microdollars:
            raise OptionalTierSkipped("optional_cost_capacity")
        try:
            reservation = self.reserve_real(
                operation, key, item_key=item_key,
                max_input_tokens=max_input_tokens, max_output_tokens=max_output_tokens,
            )
        except RealPilotBudgetExceeded as error:
            # These are cumulative/global guard denials, not the local optional budget.
            raise ExecutionConfigurationError("Global reservation guard stopped optional web execution") from error
        self.optional_attempted[operation] += 1
        self.optional_item_attempted[(item_key, operation)] = self.optional_item_attempted.get((item_key, operation), 0) + 1
        self.optional_cost += cost
        return reservation

    def internal_copy(self, binding):
        return (binding["kind"], binding.get("format"), binding.get("source_tier")) in {
            ("blob", "pdf", "internal_pdf"), ("blob", "xlsx", "vendor_table"),
        }

    def document_bytes(self, binding, item, provenance):
        if self.guard.execution_scope == "internal_only" and not self.internal_copy(binding):
            raise ExecutionConfigurationError("Internal-only execution permits approved Blob PDF/XLSX copies only")
        if binding["kind"] == "blob":
            content, etag = self.store.read_bytes(binding["blob"], max_bytes=10 * 1024 * 1024)
            if digest(content) != binding["sha256"]:
                raise ValueError("Approved source content changed")
            location = "batchblob:///" + binding["blob"]
            provenance.update(
                retrieval="succeeded", location=location, etag=etag,
                sha256=digest(content), origin="approved_copy_not_sharepoint_ingestion",
            )
            return content, location
        from backend.core.pilot_sources import SourceReference, retrieve_document

        cache_id = digest(json.dumps(binding, sort_keys=True).encode())
        if cache_id not in self.downloads:
            if not binding.get("enabled"):
                raise ValueError("SharePoint source disabled pending approved access")
            self.reserve_real(
                "retrieval", self.key(item, "retrieval", cache_id), item_key=item["item_key"],
            )
            reference = SourceReference(
                source_id=binding["source_id"], kind="sharepoint",
                location=binding["url"], expected_sha256=binding["sha256"],
                drive_id=binding["drive_id"], item_id=binding["item_id"],
                tenant_id=binding.get("tenant_id"), enabled=True,
            )
            self.downloads[cache_id] = retrieve_document(
                reference, format=binding["format"],
                credential=ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID") or None),
            )
        downloaded = self.downloads[cache_id]
        provenance["download"] = downloaded.metadata
        if downloaded.metadata["status"] != "success":
            provenance["error"] = downloaded.metadata.get("error_code", "retrieval_failed")
            raise ValueError("Approved source download did not succeed")
        provenance.update(retrieval="succeeded", sha256=downloaded.metadata["sha256"])
        return downloaded.content, binding["url"]

    def real_document(self, binding, item, scope, provenance):
        product = ProductKey.model_validate(item["manifest"]["product"])
        content, location = self.document_bytes(binding, item, provenance)
        if binding["format"] == "xlsx":
            return self.table_source(binding, content, location, product, scope, provenance)
        if not content.startswith(b"%PDF-"):
            raise ValueError("Approved PDF bytes are invalid")
        cache_key = "parses/" + digest((location + binding["sha256"] + PARSER_VERSION + ":pages=1-5").encode()) + ".json"
        full_cache_key = "parses/" + digest((location + binding["sha256"] + PARSER_VERSION).encode()) + ".json"
        try:
            document, origin = self.cached(full_cache_key, binding, location)
            provenance.update(parsing="cache", parse_origin=origin)
            cache_key = full_cache_key
        except Missing:
            pass
        with self.store.lease(cache_key):
            try:
                document, origin = self.cached(cache_key, binding, location)
                provenance.update(parsing="cache", parse_origin=origin)
            except Missing:
                reservation = self.reserve_real(
                    "analysis", self.key(item, "analysis", location + binding["sha256"]),
                    item_key=item["item_key"], analysis_pages=5,
                )
                parser = DocumentIntelligenceService(
                    credential=ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID") or None),
                )
                document = parser.extract_pdf_bytes(content, source=location, page_limit=5)
                page_count = getattr(parser, "last_page_count", None)
                if page_count is not None:
                    try:
                        self.guard.record_usage(reservation["reservation_id"], analysis_pages=page_count)
                    except (ValueError, Missing) as error:
                        raise ExecutionConfigurationError("Real-pilot page usage invalidated its reservation") from error
                if document.cache_key != "sha256:" + binding["sha256"] or document.source != location:
                    raise ValueError("Parser source/version mismatch")
                write_json(self.store, cache_key, {
                    "parser_version": PARSER_VERSION, "origin": "real_pilot_analysis_first_five_pages",
                    "document": document.model_dump(mode="json"),
                    "document_sha256": digest(document.model_dump_json().encode()),
                })
                provenance.update(parsing="fresh_analysis", reservation=reservation["reservation_id"], analyzed_pages=page_count)
        provenance["limitation"] = (
            "Compatible prior parse reused; shared-family values need explicit applicability."
            if cache_key == full_cache_key else
            "Only the first five PDF pages are eligible; shared-family values need explicit applicability."
        )
        text = document.raw_text + "\n" + "\n".join(paragraph.text for paragraph in document.paragraphs)
        if not identity_matches(text, scope["identity_terms"]):
            raise ValueError("Approved product/family identity not established")
        document = document.model_copy(deep=True)
        other_mpns = [entry["mpn"] for entry in binding["products"] if entry != product.model_dump()]
        for paragraph in document.paragraphs:
            if any(identity_matches(paragraph.text, [mpn]) for mpn in other_mpns) and not identity_matches(paragraph.text, [product.mpn]):
                paragraph.text = ""
        for table in document.tables:
            if any(identity_matches(" ".join(row), [mpn]) for row in table.cells for mpn in [product.mpn, *other_mpns]):
                table.cells = [
                    row if identity_matches(" ".join(row), [product.mpn]) or not any(identity_matches(" ".join(row), [mpn]) for mpn in other_mpns)
                    else [""] * len(row) for row in table.cells
                ]
        return OfflineSource(
            source_id=binding["source_id"], source_tier=binding["source_tier"],
            product=product, document=document, attribute_ids=scope["attribute_ids"],
            qualification=scope["qualification"] + " " + provenance["limitation"],
            provider_retrieved_at=datetime.now(timezone.utc),
        )

    def table_source(self, binding, content, location, product, scope, provenance):
        from backend.core.vendor_tables import VendorTableConfig, read_vendor_table

        config = VendorTableConfig.model_validate(binding.get("table"))
        chunks = read_vendor_table(content, config=config, product=product, source_id=binding["source_id"])
        if not chunks:
            raise ValueError("No exact vendor/part row in the approved spreadsheet")
        observed = datetime.now(timezone.utc)
        evidence = [
            Evidence(
                evidence_id=f"{binding['source_id']}:{binding['sha256']}:{chunk.sheet}:{chunk.row}",
                source_id=binding["source_id"],
                source_locator=location + "#sheet=" + quote(chunk.sheet, safe="")
                + f"&row={chunk.row}&cells=" + ",".join(chunk.cells),
                source_version="sha256:" + binding["sha256"],
                source_tier="vendor_table", content_kind="source_excerpt",
                text=chunk.text, observed_at=observed, provider_retrieved_at=observed,
                attribute_ids=scope["attribute_ids"], qualification=scope["qualification"]
                + " Exact part-number row in the explicitly associated vendor workbook; other variants excluded.",
            )
            for chunk in chunks
        ]
        provenance.update(
            parsing="vendor_table_rows", matched_rows=[chunk.row for chunk in chunks],
            sheet=config.sheet, mpn_column=config.mpn_column,
        )
        return OfflineSource(
            source_id=binding["source_id"], product=product, source_tier="vendor_table",
            excerpts=evidence,
        )

    def web_sources(self, binding, item, scope, pending, provenance):
        if self.guard.execution_scope == "internal_only":
            raise ExecutionConfigurationError("Web tiers are excluded by internal-only approval")
        from backend.core.websearch import WebSearchError, fetch_original_page, preflight_original_page
        from backend.core.websearch_webiq import WebIQSearchClient

        product = ProductKey.model_validate(item["manifest"]["product"])
        host = urlsplit(binding["url"]).hostname
        if not host:
            raise ValueError("Approved web host is missing")
        optional = self.optional_web_policy is not None
        public = None
        if optional:
            self.optional_guard_state()
            public = self.optional_web_policy.items.get(item["item_key"])
            if public is None or binding["source_id"] not in public.source_ids:
                provenance.update(retrieval="not_attempted", skip_reason="optional_public_scope_not_selected")
                return []
            validate_original_url(binding["url"], public.allowed_hosts)
            pending = [name for name in pending if name in public.attribute_terms]
            if not pending:
                provenance.update(retrieval="not_attempted", skip_reason="optional_no_public_pending_attributes")
                return []
        query = public.query(pending) if public else " ".join([*scope["identity_terms"], product.mpn, *pending])
        key = self.key(item, "search", binding["url"] + ":" + digest(query.encode()))
        try:
            client = WebIQSearchClient()
            if getattr(self.guard, "four_product", None) is not None:
                try:
                    provenance["sdk_preflight"] = client.preflight_search(
                        query, allowed_domains=[host], authorized=True,
                    )
                except SDKPreflightError as error:
                    raise ExecutionConfigurationError("Native WebIQ request validation blocked execution") from error
            elif optional:
                client.validate_configuration()
            if optional:
                self.reserve_optional("search", key, item_key=item["item_key"])
            else:
                self.reserve_real("search", key, item_key=item["item_key"])
            discovered = client.search(query, allowed_domains=[host], authorized=True)
        except OptionalTierSkipped as error:
            provenance.update(retrieval="not_attempted", skip_reason=error.code)
            return []
        except WebSearchError as error:
            discovered = []
            provenance.update(error="web_discovery_failed", discovery_error=error.code)
            if optional:
                provenance.update(retrieval="failed", skip_reason="optional_provider_unavailable")
                return []
        provenance.update(
            retrieval="discovery_failed" if provenance.get("discovery_error") else "discovery_succeeded", discovery_count=len(discovered),
            discovery_limitations="WebIQ content is unverified discovery only, never attribute evidence.",
            discovery=[{
                "location": urlsplit(result.url)._replace(query="", fragment="").geturl(),
                "retrieved_at": result.retrieved_at.isoformat(),
                "content_characters": len(getattr(result, "content", "")),
            } for result in discovered],
        )
        if public is not None and getattr(self.guard, "four_product", None) is not None:
            urls = [result.url for result in discovered]
            if urlsplit(binding["url"]).path.strip("/"):
                urls.append(binding["url"])
            urls = list(dict.fromkeys(urls))[:public.max_direct_page_attempts]
            if not urls:
                provenance.update(retrieval="not_attempted", skip_reason="optional_no_product_page_leads")
                return []
        else:
            # Historical runs retain supplied-reference-first retrieval.
            urls = list(dict.fromkeys([binding["url"], *[result.url for result in discovered]]))[:2 if optional else 3]
        sources = []
        for url in urls:
            source_id = binding["source_id"] + "-" + digest(url.encode())[:12]
            attempted = False
            try:
                if getattr(self.guard, "four_product", None) is not None:
                    proof = preflight_original_page(url, allowed_hosts=[host], authorized=True)
                    provenance.setdefault("page_sdk_preflights", []).append({"source_id": source_id, **proof})
                reserve = self.reserve_optional if optional else self.reserve_real
                reserve(
                    "web_retrieval", self.key(item, "web_retrieval", url),
                    item_key=item["item_key"],
                )
                attempted = True
                original = fetch_original_page(url, allowed_hosts=[host], authorized=True)
                provenance.setdefault("original_pages", []).append({
                    "source_id": source_id, "url": original.final_url,
                    "retrieved_at": original.retrieved_at.isoformat(),
                    "content_sha256": original.content_hash, "media_type": original.media_type,
                })
                identity = [public.manufacturer, public.mpn] if public else scope["identity_terms"] + [product.mpn]
                if not identity_matches(original.text, identity):
                    raise ValueError("Original web source does not establish exact product identity")
                excerpt = Evidence(
                    evidence_id=source_id + ":" + original.content_hash,
                    source_id=source_id, source_locator=original.final_url + "#section=visible-text",
                    source_version="sha256:" + original.content_hash,
                    source_tier=binding["source_tier"], content_kind="source_excerpt",
                    text=original.text, observed_at=datetime.now(timezone.utc),
                    provider_retrieved_at=original.retrieved_at,
                    attribute_ids=pending if optional else scope["attribute_ids"], qualification=scope["qualification"]
                    + " Independently retrieved normalized web text; WebIQ discovery passage was not used as product evidence.",
                    discovery_method="supplied_reference" if url == binding["url"] else "webiq",
                )
                sources.append(OfflineSource(
                    source_id=source_id, product=product, source_tier=binding["source_tier"],
                    excerpts=[excerpt],
                ))
            except OptionalTierSkipped as error:
                provenance.setdefault("page_errors", []).append({
                    "source_id": source_id, "error": error.code, "retrieval": "not_attempted",
                })
                break
            except SDKPreflightError as error:
                raise ExecutionConfigurationError("Native original-page request validation blocked execution") from error
            except ExecutionConfigurationError:
                raise
            except (WebSearchError, ValueError, OSError) as error:
                code = error.code if isinstance(error, WebSearchError) else "web_source_unavailable_or_inapplicable"
                detail: dict[str, str | int] = {
                    "source_id": source_id, "error": code,
                    "retrieval": "failed" if attempted else "not_attempted",
                }
                if isinstance(error, RealPilotBudgetExceeded):
                    detail.update(
                        error="budget_exhausted", budget=error.dimension,
                        requested=error.requested, remaining=error.remaining,
                    )
                provenance.setdefault("page_errors", []).append(detail)
                sources.append(OfflineSource(
                    source_id=source_id, product=product, source_tier=binding["source_tier"],
                    error_code="parse_failed",
                ))
        provenance.update(parsing="original_web_text", retrieval="succeeded" if any(source.excerpts for source in sources) else "failed")
        return sources

    def completion(self, item):
        processor = self

        class Completion:
            def validation_failure(self, issues, stage="evidence_validation"):
                diagnostic = self.diagnostic_context(issues, stage)
                diagnostic.reservation_id = self.reservation_id
                diagnostic.source_tier = self.source_tier
                diagnostic.prompt_format = COMPACT_PROMPT_FORMAT
                path = f"response-diagnostics/{processor.record['id']}/{item['item_key']}/{self.reservation_id}.json"
                try:
                    write_json(processor.store, path, diagnostic.model_dump(mode="json"))
                except (Conflict, AzureError, OSError) as error:
                    raise ExecutionConfigurationError("Validation diagnostic persistence failed; execution stopped") from error
                return diagnostic

            def diagnostic_context(self, issues, stage="evidence_validation"):
                from backend.models.enrichment import ResponseValidationDiagnostic

                if self.cached_diagnostic_context is not None:
                    diagnostic = ResponseValidationDiagnostic.model_validate(self.cached_diagnostic_context)
                    diagnostic.issues = issues[:32]
                    diagnostic.stage = stage
                    diagnostic.truncated |= len(issues) > 32
                    return diagnostic
                return response_diagnostic(
                    payload=self.response_payload, references=self.references,
                    issues=issues, stage=stage, raw_response_sha256=self.last_response_sha256,
                )

            def validated_response(self, response):
                if self.from_cache:
                    return
                write_json(processor.store, self.cache_key, {
                    "response": response.model_dump(mode="json"), "usage": self.response_usage,
                    "reservation_id": self.reservation_id, "request_parameters": self.request_parameters,
                    "prompt_format": COMPACT_PROMPT_FORMAT,
                    "raw_response_sha256": self.last_response_sha256,
                    "sanitized_response_context": self.diagnostic_context([]).model_dump(mode="json"),
                })

            def complete_structured(self, system, user, schema):
                from backend.core.config import settings
                from backend.core.llm import LLM_API_VERSION

                if os.environ.get("AOAI_API_VERSION") != LLM_API_VERSION:
                    raise ValueError("Approved model API version differs from the installed client")
                request = prepare_inference_request(system, user, schema, deployment=settings.LLM_DEPLOYMENT)
                system, user, references = request.system, request.user, request.references
                self.references = references
                self.response_payload = None
                self.last_response_sha256 = None
                self.from_cache = False
                self.cached_diagnostic_context = None
                groups = json.loads(user)["evidence_groups"]
                self.source_tier = groups[0]["source_tier"] if groups else None
                processor._active_tier = self.source_tier
                optional = processor.optional_web_policy is not None and self.source_tier in WEB_TIERS
                if optional:
                    processor.optional_guard_state()
                request_parameters = request.request_parameters
                key = processor.key(item, "inference", request.version)
                cache_key = "real-inferences/" + key + ".json"
                self.cache_key = cache_key
                self.request_parameters = request_parameters
                try:
                    stored, _ = read_json(processor.store, cache_key)
                    processor.inference_provenance.append({
                        "method": "compatible_response_cache", "key": key, "new_model_call": False,
                        "request_parameters": request_parameters,
                        "prompt_format": COMPACT_PROMPT_FORMAT,
                        "source_tier": self.source_tier,
                    })
                    self.reservation_id = stored["reservation_id"]
                    self.last_response_sha256 = stored.get("raw_response_sha256")
                    self.response_payload = stored["response"]
                    self.from_cache = True
                    self.cached_diagnostic_context = stored.get("sanitized_response_context")
                    return schema.model_validate(stored["response"])
                except Missing:
                    pass
                input_bound = request.input_bound
                sdk_preflight = None
                if getattr(processor.guard, "four_product", None) is not None:
                    try:
                        sdk_preflight = preflight_structured_request(
                            system, user, schema,
                            endpoint=settings.LLM_ENDPOINT or settings.AI_FOUNDRY_ENDPOINT,
                            deployment=settings.LLM_DEPLOYMENT,
                            sdk_max_retries=0, **request_parameters,
                        )
                    except SDKPreflightError as error:
                        processor.inference_provenance.append({
                            "method": "native_sdk_preflight_blocked", "new_model_call": False,
                            "source_tier": self.source_tier, "error_type": error.error_type,
                            "accounting": request.accounting,
                        })
                        raise ExecutionConfigurationError("Native model request validation blocked execution") from error
                try:
                    reserve = processor.reserve_optional if optional else processor.reserve_real
                    reservation = reserve(
                        "inference", key, item_key=item["item_key"],
                        max_input_tokens=input_bound, max_output_tokens=2048,
                    )
                except OptionalTierSkipped as error:
                    processor.inference_provenance.append({
                        "method": "optional_skipped", "skip_reason": error.code,
                        "new_model_call": False, "source_tier": self.source_tier,
                        "prompt_format": COMPACT_PROMPT_FORMAT,
                        "input_bound": input_bound, "accounting": request.accounting,
                        "evidence_truncated": False,
                    })
                    raise
                except RealPilotBudgetExceeded as error:
                    processor.inference_provenance.append({
                        "method": "budget_blocked", "new_model_call": False,
                        "prompt_format": COMPACT_PROMPT_FORMAT, "input_bound": input_bound,
                        "budget": error.dimension, "requested": error.requested, "remaining": error.remaining,
                    })
                    raise
                self.reservation_id = reservation["reservation_id"]
                try:
                    client = LLMClient(
                        endpoint=settings.LLM_ENDPOINT or settings.AI_FOUNDRY_ENDPOINT,
                        token_provider=get_bearer_token_provider(
                            ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID") or None),
                            COGNITIVE_SERVICES_SCOPE,
                        ),
                    )
                    client.sync_client = client.sync_client.with_options(max_retries=0)
                except Exception:
                    if optional:
                        processor.inference_provenance.append({
                            "method": "optional_failed", "skip_reason": "optional_model_unavailable",
                            "reservation_id": self.reservation_id, "new_model_call": False,
                            "source_tier": self.source_tier, "accounting": request.accounting,
                        })
                    raise
                model_started = monotonic()
                sdk_blocked = False
                try:
                    try:
                        try:
                            response = client.complete_structured(system, user, schema, max_retries=1, **request_parameters)
                        finally:
                            model_ms = milliseconds(model_started, monotonic())
                        self.response_payload = response.model_dump(mode="json")
                        response = map_citations(schema.model_validate(self.response_payload), references)
                    except SDKPreflightError as error:
                        sdk_blocked = True
                        raise ExecutionConfigurationError("Native model request revalidation blocked execution") from error
                    except (LLMSchemaValidationError, ValidationError, ResponseValidationError) as error:
                        raw_hash = getattr(client, "last_response_sha256", None)
                        self.last_response_sha256 = raw_hash if isinstance(raw_hash, str) else None
                        if isinstance(error, LLMSchemaValidationError):
                            self.response_payload = parsed_content(error.raw_content)
                            issues = schema_issues(error.errors)
                        else:
                            issues = error.issues if isinstance(error, ResponseValidationError) else schema_issues(error)
                        failure = ResponseValidationError(issues)
                        failure.diagnostic = self.validation_failure(
                            issues, "evidence_validation" if isinstance(error, ResponseValidationError)
                            else "structured_response_parsing",
                        )
                        raise failure from error
                    finally:
                        self.response_usage = None if sdk_blocked else client.last_usage
                        if self.response_usage is not None:
                            try:
                                processor.guard.record_usage(reservation["reservation_id"], **self.response_usage)
                            except (ValueError, Missing) as error:
                                raise ExecutionConfigurationError("Real-pilot measured usage exceeded or invalidated its reservation") from error
                        processor.inference_provenance.append({
                            "method": "native_sdk_preflight_blocked" if sdk_blocked else "model",
                            "reservation_id": reservation["reservation_id"],
                            "usage": self.response_usage, "new_model_call": not sdk_blocked,
                            "request_parameters": request_parameters,
                            "prompt_format": COMPACT_PROMPT_FORMAT,
                            "source_tier": self.source_tier,
                            "accounting": request.accounting,
                            "elapsed_ms": model_ms,
                            **({"sdk_preflight": sdk_preflight} if sdk_preflight is not None else {}),
                        })
                    raw_hash = getattr(client, "last_response_sha256", None)
                    self.last_response_sha256 = raw_hash if isinstance(raw_hash, str) else None
                    return response
                except (ExecutionConfigurationError, Conflict):
                    raise
                except Exception:
                    if optional:
                        processor.inference_provenance.append({
                            "method": "optional_failed", "skip_reason": "optional_model_failed",
                            "reservation_id": self.reservation_id, "new_model_call": True,
                            "source_tier": self.source_tier, "accounting": request.accounting,
                        })
                    raise
                finally:
                    client.sync_client.close()

        return Completion()

    def __call__(self, item, mode):
        item_started = monotonic()
        if mode != "real_pilot":
            raise ValueError("Real processor cannot execute another mode")
        try:
            self.guard.check_recovery_item(item)
        except ValueError as error:
            raise ExecutionConfigurationError("Recovery excludes this product") from error
        manifest = Manifest.model_validate(item["manifest"])
        self.inference_provenance = []
        self.operation_provenance = []
        self._active_tier = None
        provenance = []
        internal_only = self.guard.execution_scope == "internal_only"
        if internal_only:
            provenance = [{
                "source_id": binding["source_id"], "reference": binding["reference"],
                "source_tier": binding["source_tier"], "execution_scope": "internal_only",
                "retrieval": "not_attempted", "parsing": "not_attempted",
                "skip_reason": "internal_only_approved_copies_only",
                "limitation": "External web and SharePoint retrieval were excluded by approval; references were retained as metadata only.",
            } for binding in item["sources"] if not self.internal_copy(binding)]

        def load(tier, pending):
            self._active_tier = tier
            sources = []
            for binding in item["sources"]:
                if binding["source_tier"] != tier:
                    continue
                if internal_only and not self.internal_copy(binding):
                    continue
                entry = {
                    "source_id": binding["source_id"], "reference": binding["reference"],
                    "source_tier": tier, "retrieval": "not_attempted", "parsing": "not_attempted",
                    "execution_scope": self.guard.execution_scope,
                }
                provenance.append(entry)
                scope = next((scope for scope in binding["applicability"] if scope["product"] == manifest.product.model_dump()), None)
                if scope is None or not set(scope["attribute_ids"]) & set(pending):
                    entry["error"] = "no_approved_product_attribute_applicability"
                    continue
                try:
                    if binding["kind"] == "web":
                        sources.extend(self.web_sources(binding, item, scope, [name for name in pending if name in scope["attribute_ids"]], entry))
                    else:
                        sources.append(self.real_document(binding, item, scope, entry))
                except RealPilotBudgetExceeded as error:
                    entry.update(
                        retrieval="failed", error="budget_exhausted", budget=error.dimension,
                        requested=error.requested, remaining=error.remaining,
                    )
                    sources.append(OfflineSource(
                        source_id=binding["source_id"], product=manifest.product,
                        source_tier=tier, error_code="parse_failed",
                    ))
                except (Conflict, ExecutionConfigurationError):
                    raise
                except (Missing, ValueError, OSError, DocumentIntelligenceError, AzureError, WebSearchError):
                    entry.update(retrieval="failed", error=entry.get("error") or "source_failed_or_product_inapplicable")
                    sources.append(OfflineSource(
                        source_id=binding["source_id"], product=manifest.product,
                        source_tier=tier, error_code="parse_failed",
                    ))
            return sources

        result = run_cascade(manifest, load, self.completion(item))
        if result.telemetry is not None:
            record_accounting(
                result.telemetry, self.inference_provenance, self.operation_provenance,
                unit_prices=getattr(self.guard, "_approval", {}).get("unit_prices_usd"),
            )
            result.telemetry.elapsed_ms = milliseconds(item_started, monotonic())
        unresolved = result.extraction_error or any(
            not attribute_resolved(attribute) for attribute in result.attributes
        ) or any(entry.get("error") or entry.get("page_errors") or entry.get("skip_reason") for entry in provenance)
        coverage = {state: [attribute.attribute_id for attribute in result.attributes if attribute.status == state]
                    for state in ("existing", "proposed", "conflict", "missing_evidence", "retrieval_failed", "extraction_failed", "definition_clarification_needed")}
        coverage["inferred_review_required"] = [
            attribute.attribute_id for attribute in result.attributes
            if any(candidate.evidence_basis == "inferred_from_description" for candidate in attribute.candidates)
        ]
        cited = {evidence.evidence_id: evidence for evidence in result.evidence}
        for name, predicate in [
            ("internally_supported", lambda evidence: evidence.source_tier in {"internal_pdf", "vendor_table"}),
            ("externally_supported", lambda evidence: evidence.source_tier in {"manufacturer_web", "approved_web"}),
            ("webiq_discovered_support", lambda evidence: evidence.discovery_method == "webiq"),
        ]:
            coverage[name] = [
                attribute.attribute_id for attribute in result.attributes
                if attribute.status == "proposed" and any(
                    predicate(cited[key]) for candidate in attribute.candidates
                    if candidate.evidence_basis == "literal" for key in candidate.evidence_ids
                )
            ]
        return result, {
            "state": "unresolved" if unresolved else "completed",
            "error": "Inspect source failures, applicability, and unresolved attributes" if unresolved else "",
            "provenance": provenance, "consumption": self.guard.metadata(),
            "execution_scope": self.guard.execution_scope,
            "inference_provenance": self.inference_provenance,
            "coverage": coverage,
        }


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
            guard = None
            if record["mode"] == "real_pilot":
                if concurrency != 1 or not 1 <= item_limit <= 4:
                    raise ValueError("Real pilot requires one thread and at most four items")
                guard = RealPilotGuard(store, record)
                four = getattr(guard, "four_product", None)
                if guard.recovery is not None:
                    expected_items = 4 if four is not None else 2
                    if item_limit != expected_items:
                        raise ValueError(
                            "Four-product recovery requires an exact four-item worker slice"
                            if four is not None else "Recovery requires an exact two-item worker slice"
                        )
                if processor is None:
                    try:
                        policy = configured_optional_web_policy()
                    except ValueError as error:
                        raise ExecutionConfigurationError("Invalid optional web runtime policy") from error
                    process = (
                        RealBatchProcessor(store, record, guard, optional_web_policy=policy)
                        if policy is not None else RealBatchProcessor(store, record, guard)
                    )
                guard.before_execution(str(uuid.uuid4()))
                guard.prepare_recovery(fence)
            record["state"] = "running"
            version = write_json(store, path, record, version)
            pending = []
            for item in record["items"]:
                key = f"items/{batch_id}/{item['item_key']}.json"
                try:
                    state, item_version = read_json(store, key)
                    if (guard is not None and guard.active_recovery is not None
                            and state["state"] == "recovery_ready"
                            and item["item_key"] in guard.active_recovery["selected_item_keys"]
                            and state.get("recovery_sha256") == guard.recovery_sha256):
                        pending.append(item)
                    if state["state"] == "running":
                        try:
                            read_json(store, state.get("result_key", f"results/{batch_id}/{item['item_key']}.json"))
                            state.update(state="unresolved", error="Recovered persisted result after interrupted status update; inspect before review")
                        except Missing:
                            state.update(state="interrupted", error="Prior worker stopped after reservation; remote completion unknown. No automatic resubmission")
                        fence()
                        write_json(store, key, state, item_version)
                except Missing:
                    if guard is None or guard.recovery is None:
                        pending.append(item)
                    else:
                        raise Conflict("Recovery status disappeared; no automatic resubmission")
            if guard is not None and getattr(guard, "four_product", None) is not None:
                order = guard.four_product["execution_order"]
                pending.sort(key=lambda item: order.index(item["item_key"]))

            def execute(item):
                item_started = monotonic()
                key = f"items/{batch_id}/{item['item_key']}.json"
                fence()
                if guard is not None and guard.recovery is not None:
                    state, item_version = read_json(store, key)
                    if state.get("state") != "recovery_ready" or state.get("recovery_sha256") != guard.recovery_sha256:
                        raise Conflict("Recovery item changed; no automatic retry")
                else:
                    state, item_version = {}, None
                state.update(id=item["item_key"], batch_id=batch_id, state="running", started_at=now(), requested_mode=record["mode"])
                item_version = write_json(store, key, state, item_version)
                try:
                    result, outcome = process(item, record["mode"])
                    if result.telemetry is None:
                        result.telemetry = ItemTelemetry(elapsed_ms=milliseconds(item_started, monotonic()))
                    fence()
                    result_key = guard.result_key(item["item_key"]) if guard else f"results/{batch_id}/{item['item_key']}.json"
                    write_json(store, result_key, result.model_dump(mode="json"))
                    state.update(outcome)
                    state["reviewable_attributes"] = [attribute.attribute_id for attribute in result.attributes if attribute.status != "existing"]
                except ExecutionConfigurationError:
                    if record["mode"] == "real_pilot":
                        state.update(
                            state="failed", finished_at=now(),
                            error="Execution guard stopped this item; prior reservations remain consumed. No automatic retry.",
                        )
                        fence()
                        write_json(store, key, state, item_version)
                    raise
                except Conflict:
                    raise
                except Exception:
                    state.update(state="failed", error="Item execution failed; provider details withheld. No automatic retry")
                fence()
                state["finished_at"] = now()
                state["elapsed_ms"] = milliseconds(item_started, monotonic())
                write_json(store, key, state, item_version)

            if record["mode"] == "real_pilot":
                for item in pending[:item_limit]:
                    execute(item)
            else:
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    list(pool.map(execute, pending[:item_limit]))
            fence()
            record["updated_at"] = now()
            states = [read_json(store, key)[0]["state"] for key in store.keys(f"items/{batch_id}/")]
            record["state"] = "deferred" if "deferred" in states else "queued" if len(pending) > item_limit else "completed"
            record["progress"] = {"finished": sum(state not in {"running", "queued", "recovery_ready", "deferred"} for state in states), "unresolved": states.count("unresolved"), "failed": states.count("failed") + states.count("interrupted")}
            if "deferred" in states:
                record["progress"]["deferred"] = states.count("deferred")
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
    parser.add_argument("--real-pilot", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_batches <= 10:
        parser.error("max-batches must be 1-10")
    if args.batch_id is not None and not re.fullmatch(r"[a-f0-9]{64}", args.batch_id):
        parser.error("batch-id must be a SHA-256 identifier")
    if args.synthetic_acceptance and (not args.batch_id or args.max_batches != 1 or not 1 <= args.item_limit <= 2 or not 1 <= args.concurrency <= 2):
        parser.error("Synthetic acceptance requires an explicit batch-id, one batch, and concurrency/item limits of 1-2")
    if args.real_pilot and (args.synthetic_acceptance or not args.batch_id or args.max_batches != 1 or args.concurrency != 1 or not 1 <= args.item_limit <= 4):
        parser.error("Real pilot requires an explicit batch-id, one batch, one thread and at most four items")
    try:
        store = configured_store()
        count = 0
        paths = [f"batches/{args.batch_id}.json"] if args.batch_id else store.keys("batches/")
        for path in paths:
            record, _ = read_json(store, path)
            if args.real_pilot and record.get("mode") != "real_pilot":
                raise ValueError("Selected batch is not approved real-pilot mode")
            if record.get("mode") == "real_pilot" and not args.real_pilot:
                continue
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