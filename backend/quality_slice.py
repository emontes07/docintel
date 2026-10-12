"""Build an internal Angle Valves slice only from already-approved stored sources."""

from __future__ import annotations

import hashlib
import gzip
import json
import re
from datetime import datetime, timezone

from backend.batch import SourceApplicability, SourceBinding
from backend.batch_store import Conflict, Missing, read_json, write_json
from backend.core.vendor_tables import parse_vendor_workbook
from backend.models.enrichment import Manifest, ProductKey

ANGLE_NODE = "Pipes, Valves & Fittings.Fittings.Brass Service Materials.Angle Valves"
REFERENCE = "Angle_Valve_Attributes.xlsx"
MAX_SLICE_ITEMS = 200
SLICE_VERSION = "prc-angle-valves-v2"
MUELLER_STOP = re.compile(r"\bANGLE\s+(?:MTR|METER)\s+STOP\b", re.I)
FORD_AV = re.compile(r"AV\d{2,}-.+", re.I)
MUELLER_DRAWING_REFERENCE = re.compile(r"\b(?:SEE|REF)\s+HS\s+((?:[A-Z]?\d{5}[A-Z]?\b\s*)+)", re.I)
# The stored documents' own printed model/series identity. A PDF binds to a row only
# when the row names this document or the MPN matches this printed series.
STORED_DOCUMENTS = {
    "mueller": {"source_id": "av-source-1", "identifier": "H14255N"},
    "ford": {"source_id": "av-source-4", "identifier": "AV11-xxxW-NL"},
}


def referenced_documents(vendor: str, values: dict[str, str]) -> list[str]:
    """Drawing/submittal identifiers named by a vendor row, in display form."""
    if vendor == "ford":
        submittal = values.get("Submittal File", "").strip()
        return [re.sub(r"\.pdf$", "", submittal, flags=re.I)] if submittal else []
    found = []
    for column, text in values.items():
        if column == "Part #":
            continue
        for match in MUELLER_DRAWING_REFERENCE.finditer(text):
            for token in match[1].split():
                token = token.upper()
                found.append(token if token[0].isalpha() else "H" + token)
    return list(dict.fromkeys(found))


def document_binding(vendor: str, mpn: str, values: dict[str, str]) -> dict | None:
    """Return the binding basis for the stored document, or None (vendor-row-only)."""
    from backend.quality_applicability import _series_match

    document = STORED_DOCUMENTS[vendor]
    references = referenced_documents(vendor, values)
    if any(_series_match(reference, document["identifier"]) or reference.casefold() == document["identifier"].casefold()
           for reference in references):
        return {"basis": "vendor_row_reference", "document": document["identifier"], "row_references": references}
    if _series_match(document["identifier"], mpn):
        return {"basis": "mpn_series", "document": document["identifier"], "row_references": references}
    return None


def _source_lookup(record: dict) -> dict[str, dict]:
    return {
        source["source_id"]: source
        for item in record["items"] for source in item.get("sources", [])
        if source.get("kind") == "blob"
    }


def _approved_rows(store, source: dict, *, sheet: str, header_row: int, mpn_header: str,
                   filter_row, namespace: str) -> list[tuple[int, str, dict[str, str]]]:
    content = store.read_bytes(source["blob"], max_bytes=32 * 1024 * 1024)[0]
    if hashlib.sha256(content).hexdigest() != source["sha256"]:
        raise ValueError(f"Approved source hash mismatch for {source['source_id']}")
    complete_index = parse_vendor_workbook(content)
    rows = complete_index.get(sheet)
    if rows is None:
        raise ValueError(f"Approved source {source['source_id']} lacks worksheet {sheet!r}")
    header = next((row for row in rows if row["row"] == header_row), None)
    if header is None:
        raise ValueError(f"Approved source {source['source_id']} lacks header row {header_row}")
    headers = {cell["column_index"]: str(cell["value"]) for cell in header["cells"]}
    try:
        mpn_column = next(column for column, name in headers.items() if name == mpn_header)
    except StopIteration:
        raise ValueError(f"Approved source {source['source_id']} lacks MPN column {mpn_header!r}") from None
    selected = []
    selected_rows = set()
    mpn_rows: dict[str, list[int]] = {}
    for row in rows:
        if row["row"] <= header_row:
            continue
        values = {headers[cell["column_index"]]: str(cell["value"])
                  for cell in row["cells"] if cell["column_index"] in headers and cell["value"] is not None}
        mpn = values.get(mpn_header, "").strip()
        if mpn and filter_row(values, mpn):
            selected.append((row["row"], mpn, values))
            selected_rows.add(row["row"])
            mpn_rows.setdefault(mpn, []).append(row["row"])
    if len({mpn for _, mpn, _ in selected}) != len(selected):
        raise ValueError(f"Approved source {source['source_id']} contains duplicate selected MPNs")
    index = {sheet: [row for row in rows if row["row"] == header_row or row["row"] in selected_rows]}
    owner_hash = hashlib.sha256(namespace.encode()).hexdigest()[:24]
    cache_key = f"quality-vendor-index/{owner_hash}/{source['sha256']}.json.gz"
    try:
        store.read_bytes(cache_key)
    except Missing:
        try:
            store.write_bytes(cache_key, gzip.compress(json.dumps(
                {"workbook": index, "mpn_rows": mpn_rows}, ensure_ascii=False, separators=(",", ":"),
            ).encode()))
        except Conflict:
            pass
    return selected


def _scope(source: dict, product: ProductKey, qualification: str, attributes: list[str]) -> dict:
    original = next((value for value in source.get("applicability", [])
                     if value.get("product", {}).get("item_id") == product.item_id), {})
    identity_terms = [product.mpn]
    return SourceApplicability(
        product=product, identity_terms=identity_terms, attribute_ids=attributes,
        qualification=qualification,
    ).model_dump(mode="json")


def _binding(
    source: dict, product: ProductKey, *, reference: str, tier: str, fmt: str, qualification: str,
    attributes: list[str], table: dict | None = None,
) -> dict:
    binding = dict(source)
    binding.update(
        reference=reference, products=[product.model_dump(mode="json")],
        source_tier=tier, format=fmt,
        applicability=[_scope(source, product, qualification, attributes)],
    )
    return SourceBinding.model_validate(binding | {"table": table}).model_dump(mode="json")


def build_approved_angle_valve_slice(store, source_batch_id: str, owner: str) -> dict:
    """Persist a bounded batch sourced only from the approved Mueller and Ford files."""
    source_record, _ = read_json(store, f"batches/{source_batch_id}.json")
    if source_record.get("owner") != owner:
        raise ValueError("Approved source batch owner does not match the requested owner")
    if not source_record.get("valid") or source_record.get("attribute_reference") != REFERENCE:
        raise ValueError("The source batch must be the validated Phase 3 Angle Valves pilot batch")
    sources = _source_lookup(source_record)
    required = {"av-source-1", "av-source-2", "av-source-4", "av-source-5"}
    if not required <= sources.keys():
        raise ValueError("The source batch does not contain all four approved Mueller/Ford sources")
    batch_id = hashlib.sha256(
        f"{SLICE_VERSION}:{owner}:{source_batch_id}".encode()
    ).hexdigest()
    try:
        existing, _ = read_json(store, f"batches/{batch_id}.json")
    except Missing:
        existing = None
    if existing is not None:
        if (existing.get("valid") and existing.get("input_hashes", {}).get("source_batch_id") == source_batch_id
                and existing.get("slice", {}).get("internal_only") is True):
            return existing
        raise Conflict("A different slice batch already exists at the deterministic batch ID")
    definitions = source_record["original_definitions"]
    pilot_manifest = Manifest.model_validate(source_record["items"][0]["manifest"])
    attributes = [definition.attribute_id for definition in pilot_manifest.attributes]

    source_specs = {
        "mueller": {
            "vendor_name": "Mueller Water Products, Inc. Mueller Co Llc",
            "pdf": sources["av-source-1"], "xlsx": sources["av-source-2"],
            "xlsx_table": sources["av-source-2"]["table"],
            "pdf_qualification": (
                "Approved stored Mueller drawing H14255N, bound only to 14255-series MPNs or rows that name "
                "it. Rows that reference a different drawing (for example H14250) keep the applicability "
                "question; variant-specific dimensions are never transferred."
            ),
        },
        "ford": {
            "vendor_name": "The Ford Meter Box Company,Inc",
            "pdf": sources["av-source-4"], "xlsx": sources["av-source-5"],
            "xlsx_table": sources["av-source-5"]["table"],
            "pdf_qualification": (
                "Approved stored Ford AV11-xxxW-NL submittal, bound only to rows whose Submittal File names "
                "it or whose MPN matches AV11-xxxW-NL. Other AV-series variants are vendor-row-only."
            ),
        },
    }
    selected: list[tuple[str, int, str, dict[str, str]]] = []
    for key, spec in source_specs.items():
        if key == "mueller":
            keep_row = lambda values, mpn: bool(MUELLER_STOP.search(values.get("Descr 1", " ")))
        else:
            keep_row = lambda values, mpn: bool(FORD_AV.fullmatch(mpn))
        table = spec["xlsx_table"]
        rows = _approved_rows(
            store, spec["xlsx"], sheet=table["sheet"], header_row=table["header_row"],
            mpn_header=table["mpn_column"], filter_row=keep_row, namespace=owner,
        )
        selected.extend((key, row, mpn, values) for row, mpn, values in rows)
    if len(selected) > MAX_SLICE_ITEMS:
        raise ValueError(f"The approved source rows produce {len(selected)} products; maximum is {MAX_SLICE_ITEMS}")
    if not selected:
        raise ValueError("No approved angle-valve source rows matched")

    products_by_vendor: dict[str, list[ProductKey]] = {"mueller": [], "ford": []}
    for key, _, mpn, _ in selected:
        item_id = "PRC-" + key[:2].upper() + "-" + hashlib.sha256((key + "\0" + mpn).encode()).hexdigest()[:16]
        products_by_vendor[key].append(ProductKey(
            item_id=item_id, vendor=source_specs[key]["vendor_name"], mpn=mpn, hierarchy_node=ANGLE_NODE,
        ))
    items = []
    missing: dict[str, dict] = {}
    bindings_summary = {"mueller": {"bound": 0, "vendor_only": 0}, "ford": {"bound": 0, "vendor_only": 0}}
    for index, (key, source_row, mpn, values) in enumerate(selected, 2):
        spec = source_specs[key]
        product = next(product for product in products_by_vendor[key] if product.mpn == mpn)
        basis = document_binding(key, mpn, values)
        stored = STORED_DOCUMENTS[key]["identifier"].casefold()
        references = referenced_documents(key, values)
        # Rows already covered by a stored document need no request when they name nothing else.
        for reference in references or ([] if basis else ["(none referenced)"]):
            if reference.casefold() == stored:
                continue
            entry = missing.setdefault(reference, {"document": reference, "vendor": key, "items": 0, "mpns": []})
            entry["items"] += 1
            entry["mpns"].append(mpn)
        bindings_summary[key]["bound" if basis else "vendor_only"] += 1
        pdf_binding = _binding(
            spec["pdf"], product, reference=f"approved:{key}:pdf", tier="internal_pdf", fmt="pdf",
            qualification=spec["pdf_qualification"] + f" Binding basis: {basis['basis']}.", attributes=attributes,
        ) if basis else None
        vendor_binding = _binding(
            spec["xlsx"], product, reference=f"approved:{key}:vendor", tier="vendor_table", fmt="xlsx",
            qualification="Exact MPN-matched row from the already-approved internal vendor workbook.",
            attributes=attributes, table=spec["xlsx_table"],
        )
        sources = [binding for binding in (pdf_binding, vendor_binding) if binding]
        manifest = Manifest(product=product, attributes=pilot_manifest.attributes,
                            source_ids=[binding["source_id"] for binding in sources])
        original = {
            "PIMITEM Number": product.item_id, "Vendor Name": product.vendor, "MPN": mpn,
            "Hierarchy Node": ANGLE_NODE, "Attributes to Fill": REFERENCE,
            "PDF Tech Spec": f"approved:{key}:pdf" if basis else "", "Vendor Tabular Data": f"approved:{key}:vendor",
        }
        items.append({
            "item_key": f"row-{index}", "row": index, "original": original,
            "manifest": manifest.model_dump(mode="json"), "sources": sources,
            "errors": [], "warnings": [], "document_binding": basis,
            "referenced_documents": references, "vendor": key,
        })
    items_prefix = f"batches/{batch_id}/items"
    routes = []
    for item in items:
        product = item["manifest"]["product"]
        pdfs = sorted((source["source_id"], source["sha256"])
                      for source in item["sources"] if source["format"] == "pdf")
        vendor = next((source for source in item["sources"] if source["format"] == "xlsx"), None)
        family_key = ("pdf-family-" + hashlib.sha256(
            json.dumps(pdfs, separators=(",", ":")).encode()
        ).hexdigest()[:24]) if pdfs else "no-pdf-" + (vendor["source_id"] if vendor else item["item_key"])
        routes.append({
            "item_key": item["item_key"], "product": product, "mpn": product["mpn"],
            "family_key": family_key,
            "vendor_binding": ({key: vendor.get(key) for key in
                                ("source_id", "sha256", "blob", "format", "source_tier", "table")}
                               if vendor else None),
        })
        write_json(store, f"{items_prefix}/{item['item_key']}.json", item)
    record = {
        "id": batch_id, "owner": owner, "created_at": datetime.now(timezone.utc).isoformat(),
        "state": "validated", "product_count": len(items), "item_count": len(items),
        "items_prefix": items_prefix,
        "item_keys": [item["item_key"] for item in items], "item_routes": routes, "items": [], "valid": True,
        "input_hashes": {
            "source_batch_id": source_batch_id,
            "approved_vendor_files": {key: spec["xlsx"]["sha256"] for key, spec in source_specs.items()},
            "approved_pdf_files": {key: spec["pdf"]["sha256"] for key, spec in source_specs.items()},
        },
        "attribute_reference": REFERENCE, "original_definitions": definitions,
        "mode": "live_inference", "progress": {"processed": 0, "total": len(items)},
        "slice": {"internal_only": True, "version": SLICE_VERSION, "vendor_files": ["av-source-2", "av-source-5"],
                  "pdf_files": ["av-source-1", "av-source-4"], "vendor_workbook_parses": 2,
                  "document_bindings": bindings_summary,
                  "missing_documents": sorted(missing.values(), key=lambda entry: (-entry["items"], entry["document"]))},
    }
    write_json(store, f"batches/{batch_id}.json", record)
    return record
