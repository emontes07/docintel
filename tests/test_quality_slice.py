import hashlib
import json
from uuid import UUID

from backend.batch import BatchService, SourceApplicability, SourceBinding
from backend.batch_store import Conflict, Missing
from backend.models.enrichment import AttributeDefinition, Manifest, ProductKey
from backend.quality_slice import build_approved_angle_valve_slice
from backend.workbooks import write_workbook

OWNER = "459aa71a-1d56-4551-b73d-afd96d3b32c6/551d7844-0f61-464a-9257-92ef234f1116"
BASE_ID = "d" * 64


class MemoryStore:
    def __init__(self, base_record, documents):
        self.values = {"batches/" + BASE_ID + ".json": json.dumps(base_record).encode(), **documents}

    def read_bytes(self, key, max_bytes=64 * 1024 * 1024):
        if key not in self.values:
            raise Missing(key)
        return self.values[key], "etag"

    def write_bytes(self, key, value, version=None):
        if key in self.values and version is None:
            raise Conflict(key)
        self.values[key] = value
        return "etag"


def workbook(rows, headers):
    return write_workbook({"Catalog": [headers, *rows]})


def base_record(mueller, ford, pdf1, pdf4):
    attrs = [AttributeDefinition(attribute_id=f"Attribute {i}", description=f"Attribute {i}", value_type="string")
             for i in range(24)]
    products = [
        ProductKey(item_id="PIMITEM-1", vendor="Mueller Water Products, Inc. Mueller Co Llc",
                   mpn="014255    215N", hierarchy_node="Pipes, Valves & Fittings.Fittings.Brass Service Materials.Angle Valves"),
        ProductKey(item_id="PIMITEM-2", vendor="Mueller Water Products, Inc. Mueller Co Llc",
                   mpn="014255    203N", hierarchy_node="Pipes, Valves & Fittings.Fittings.Brass Service Materials.Angle Valves"),
        ProductKey(item_id="PIMITEM-3", vendor="The Ford Meter Box Company,Inc",
                   mpn="AV11-333W-NL", hierarchy_node="Pipes, Valves & Fittings.Fittings.Brass Service Materials.Angle Valves"),
        ProductKey(item_id="PIMITEM-4", vendor="The Ford Meter Box Company,Inc",
                   mpn="AV11-444W-NL", hierarchy_node="Pipes, Valves & Fittings.Fittings.Brass Service Materials.Angle Valves"),
    ]
    specs = [
        ("mueller-pdf", "av-source-1", "documents/av-source-1.pdf", pdf1, "pdf", "internal_pdf", None),
        ("mueller-xlsx", "av-source-2", "documents/av-source-2.xlsx", mueller, "xlsx", "vendor_table",
         {"sheet": "Catalog", "header_row": 1, "mpn_column": "Part #", "vendor_column": None,
          "expected_vendor": products[0].vendor}),
        ("ford-pdf", "av-source-4", "documents/av-source-4.pdf", pdf4, "pdf", "internal_pdf", None),
        ("ford-xlsx", "av-source-5", "documents/av-source-5.xlsx", ford, "xlsx", "vendor_table",
         {"sheet": "Catalog", "header_row": 1, "mpn_column": "2nd Item Number", "vendor_column": None,
          "expected_vendor": products[2].vendor}),
    ]
    bindings = {}
    for ref, source_id, blob, raw, fmt, tier, table in specs:
        applicable = [p for p in products if ("mueller" in ref) == ("mueller" in p.vendor.casefold())]
        scopes = [SourceApplicability(product=p, identity_terms=[p.mpn], attribute_ids=[a.attribute_id for a in attrs],
                                      qualification="Original approved source association.").model_dump(mode="json")
                  for p in applicable]
        bindings[source_id] = SourceBinding(
            reference=ref, source_id=source_id, owner=OWNER, kind="blob", products=applicable,
            format=fmt, source_tier=tier, blob=blob, sha256=hashlib.sha256(raw).hexdigest(),
            applicability=[SourceApplicability.model_validate(x) for x in scopes], table=table,
        ).model_dump(mode="json")
    items = []
    for index, product in enumerate(products, 2):
        key = "mueller" if "mueller" in product.vendor.casefold() else "ford"
        source_ids = ("av-source-1", "av-source-2") if key == "mueller" else ("av-source-4", "av-source-5")
        manifest = Manifest(product=product, attributes=attrs, source_ids=list(source_ids))
        items.append({"item_key": f"row-{index}", "manifest": manifest.model_dump(mode="json"),
                      "sources": [bindings[source_id] for source_id in source_ids]})
    return {"id": BASE_ID, "owner": OWNER, "valid": True, "attribute_reference": "Angle_Valve_Attributes.xlsx",
            "items": items, "original_definitions": [a.model_dump(mode="json") for a in attrs]}


def test_slice_uses_only_approved_angle_rows_and_reuses_the_vendor_parse(monkeypatch):
    mueller = workbook([
        ["014255 215N", '5/8X3/4 ANGLE MTR STOP', "LOW LEAD BRASS"],
        ["014267 216N", '5/8 X 3/4 X1" ANGLE METER STOP', "LOW LEAD BRASS"],
        ["NOT-ANGLE", "ANGLE TEE", "BRASS"],
    ], ["Part #", "Descr 1", "DescrGen1"])
    ford = workbook([
        ["AV11-333W-NL", "3/4 ANGLE KEY VALVE FIP/FIP"],
        ["AV11-444W-NL", "1IN ANGLE KEY VALVE FIP/FIP"],
        ["AV21-333W-NL", "3/4 ANGLE KEY VALVE FLARE/FIP"],
        ["OTHER-1", "NOT AN AV SERIES ITEM"],
    ], ["2nd Item Number", "Description"])
    pdf1, pdf4 = b"approved mueller pdf", b"approved ford pdf"
    record = base_record(mueller, ford, pdf1, pdf4)
    docs = {
        "documents/av-source-2.xlsx": mueller, "documents/av-source-5.xlsx": ford,
        "documents/av-source-1.pdf": pdf1, "documents/av-source-4.pdf": pdf4,
    }
    store = MemoryStore(record, docs)
    import backend.quality_slice as module
    calls = []
    original = module.parse_vendor_workbook

    def counted(content):
        calls.append(hashlib.sha256(content).hexdigest())
        return original(content)

    monkeypatch.setattr(module, "parse_vendor_workbook", counted)
    result = build_approved_angle_valve_slice(store, BASE_ID, OWNER)
    assert result["product_count"] == 5 and result["valid"]
    metadata = BatchService(store).get(result["id"], OWNER, include_items=False)
    assert metadata["items"] == [] and metadata["item_count"] == 5
    assert len(list(BatchService(store).iter_items(metadata))) == 5
    hydrated = BatchService(store).get(result["id"], OWNER)["items"]
    assert all(len(item["manifest"]["attributes"]) == 24 for item in hydrated)
    assert [item["manifest"]["product"]["mpn"] for item in hydrated] == [
        "014255 215N", "014267 216N", "AV11-333W-NL", "AV11-444W-NL", "AV21-333W-NL",
    ]
    assert len(calls) == 2
    assert hydrated[0]["sources"][0]["applicability"][0]["qualification"].find("H14250") >= 0
    assert all(len(source["products"]) == 1 for item in hydrated for source in item["sources"])
    again = build_approved_angle_valve_slice(store, BASE_ID, OWNER)
    assert again["id"] == result["id"] and len(calls) == 2
    index_keys = [key for key in store.values if key.startswith("quality-vendor-index/")]
    assert len(index_keys) == 2
