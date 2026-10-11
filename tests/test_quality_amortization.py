import hashlib
import json
from datetime import datetime, timezone
from io import BytesIO

from backend.core.vendor_tables import VendorTableConfig, parse_vendor_workbook, read_vendor_table, read_vendor_table_index
from backend.models.enrichment import AttributeDefinition, Candidate, Evidence, Manifest, ProductKey
from backend.quality_amortization import (
    PROFILED_ROLES, VendorProfile, ProfileColumn, PhraseBatch, PhraseCandidate, assign_family_shards, family_binding_key,
    normalized_phrase, vendor_candidates_for_item, vendor_profile_and_candidates, _family_safe,
)
from backend.quality_worker import CachedEvidenceLoader
from backend.workbooks import write_workbook

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


class MemoryStore:
    def __init__(self, content):
        self.data = {"documents/vendor.xlsx": content}

    def read_bytes(self, key, max_bytes=64 * 1024 * 1024):
        if key not in self.data:
            from backend.batch_store import Missing
            raise Missing(key)
        return self.data[key], "version"

    def write_bytes(self, key, value, version=None):
        if key in self.data and version is None:
            from backend.batch_store import Conflict
            raise Conflict(key)
        self.data[key] = value
        return "version"

    def keys(self, prefix):
        return [key for key in self.data if key.startswith(prefix)]

    def read_json(self, key):
        from backend.batch_store import read_json
        return read_json(self, key)


def sample_workbook():
    return write_workbook({"Vendor data": [
        ["Synthetic export"], ["Metadata"],
        ["Part", "Vendor", "Pressure", "Description"],
        ["00123", "Synthetic", "125", "Angle meter stop"],
        ["00124", "Synthetic", "150", "Angle meter stop"],
    ]})


def test_indexed_vendor_selection_is_byte_equivalent_and_reuses_the_parse():
    content = sample_workbook()
    config = VendorTableConfig(sheet="Vendor data", header_row=3, mpn_column="Part",
                               vendor_column="Vendor")
    product = ProductKey(item_id="ITEM-1", vendor="Synthetic", mpn="00123", hierarchy_node="Valve")
    index = parse_vendor_workbook(content)
    indexed = read_vendor_table_index(index, config=config, product=product, source_id="vendor")
    direct = read_vendor_table(content, config=config, product=product, source_id="vendor",
                               workbook_index=index)
    assert [chunk.model_dump() for chunk in indexed] == [chunk.model_dump() for chunk in direct]
    store = MemoryStore(content)
    loader = CachedEvidenceLoader(store, namespace="test-owner")
    binding = {"sha256": "a" * 64, "blob": "documents/vendor.xlsx"}
    indexed = loader.vendor_index(binding | {"table": config.model_dump(mode="json")}, content)
    assert indexed["workbook"] == index
    assert indexed["mpn_rows"]["00123"] == [4]
    assert loader.vendor_index(binding | {"table": config.model_dump(mode="json")}, content) is indexed
    assert loader.vendor_workbook_parses == 1


def test_family_key_uses_source_hashes_not_item_identity():
    source = {"source_id": "approved-pdf", "sha256": "a" * 64, "format": "pdf", "kind": "blob"}
    first = {"item_key": "row-2", "sources": [source]}
    second = {"item_key": "row-205", "sources": [source]}
    assert family_binding_key(first) == family_binding_key(second)
    assert normalized_phrase("  ANGLE   METER Stop ") == "angle meter stop"


def test_family_shards_are_deterministic_balanced_and_never_split_a_family():
    items = []
    for family, count in (("mueller", 105), ("ford", 63), ("small", 7), ("other", 2)):
        source = {"source_id": family, "sha256": hashlib.sha256(family.encode()).hexdigest(),
                  "format": "pdf", "kind": "blob"}
        for index in range(count):
            items.append({"item_key": f"{family}-{index}", "sources": [source]})
    first = assign_family_shards(items, 2)
    assert first == assign_family_shards(items, 2)
    assert set(first.values()) == {0, 1}
    assert len({first[item["item_key"]] for item in items if item["item_key"].startswith("mueller-")}) == 1
    assert len({first[item["item_key"]] for item in items if item["item_key"].startswith("ford-")}) == 1
    counts = [sum(value == shard for value in first.values()) for shard in (0, 1)]
    assert max(counts) - min(counts) <= 51


def test_family_candidate_filter_rejects_variant_attributes_and_table_rows():
    def evidence(locator, text):
        return Evidence(
            evidence_id="e", source_id="pdf", source_version="sha256:" + "a" * 64,
            source_tier="internal_pdf", content_kind="source_excerpt", source_locator=locator,
            text=text, observed_at=NOW,
        )
    common = evidence("batchblob:///drawing.pdf#page=1&paragraph=24", "All wetted parts are brass.")
    table = evidence("batchblob:///drawing.pdf#page=1&table=0&row=2&column=1", "3/4")
    item = Candidate(attribute_id="Primary Material", value="No-lead brass", evidence_ids=["e"],
                     origin="derived", supporting_quote="All wetted parts are brass.")
    dimensional = Candidate(attribute_id="Inlet Size", value='Other: 3/4"', evidence_ids=["e"],
                             origin="derived", supporting_quote="All wetted parts are brass.")
    assert _family_safe(item, [common], {"AV11-333W-NL", "AV11-444W-NL"})
    assert not _family_safe(dimensional, [common], {"AV11-333W-NL", "AV11-444W-NL"})
    assert not _family_safe(item, [table], {"AV11-333W-NL", "AV11-444W-NL"})
    qualified = evidence("batchblob:///drawing.pdf#page=1&paragraph=24", "For AV11-333W-NL use brass.")
    assert not _family_safe(item, [qualified], {"AV11-333W-NL", "AV11-444W-NL"})


def test_variant_vendor_phrases_keep_exact_ford_rows_and_do_not_transfer_dimensions():
    definition = AttributeDefinition(attribute_id="Inlet Size", description="Inlet Size", value_type="string",
                                    type_guidance="Enumerated")
    phrase_map = {
        "Description\u0000" + normalized_phrase("3/4 ANGLE KEY VALVE FIP/FIP"): [{
            "phrase_id": "P00001", "attribute_id": "Inlet Size", "value": "Other: 3/4",
            "quote": "3/4", "origin": "derived", "normalization_rule": "exact_enum_or_explicit_other_v1",
        }],
        "Description\u0000" + normalized_phrase("1IN ANGLE KEY VALVE FIP/FIP"): [{
            "phrase_id": "P00002", "attribute_id": "Inlet Size", "value": "Other: 1IN",
            "quote": "1IN", "origin": "derived", "normalization_rule": "exact_enum_or_explicit_other_v1",
        }],
    }

    def product(mpn, row, text):
        manifest = Manifest(product=ProductKey(item_id="PRC-" + mpn, vendor="Ford", mpn=mpn, hierarchy_node="Angle"),
                            attributes=[definition])
        evidence = Evidence(
            evidence_id=f"vendor:{row}", source_id="av-source-5", source_version="sha256:" + "a" * 64,
            source_tier="vendor_table", content_kind="source_excerpt",
            source_locator=f"batchblob:///documents/av-source-5.xlsx#sheet=Customer&row={row}&cells=A{row},B{row}",
            text=json.dumps({"sheet": "Customer", "row": row, "cells": [
                {"cell": f"A{row}", "column": "2nd Item Number", "value": mpn},
                {"cell": f"B{row}", "column": "Description", "value": text},
            ]}), observed_at=NOW, provider_retrieved_at=NOW,
        )
        return manifest, evidence

    small, small_row = product("AV11-333W-NL", 899, "3/4 ANGLE KEY VALVE FIP/FIP")
    large, large_row = product("AV11-444W-NL", 900, "1IN ANGLE KEY VALVE FIP/FIP")
    c1, e1, rejected1 = vendor_candidates_for_item(phrase_map, small, [small_row])
    c2, e2, rejected2 = vendor_candidates_for_item(phrase_map, large, [large_row])
    assert [c.value for c in c1] == ["Other: 3/4"] and c1[0].evidence_ids == [e1[0].evidence_id]
    assert [c.value for c in c2] == ["Other: 1IN"] and c2[0].evidence_ids == [e2[0].evidence_id]
    assert small_row.evidence_id != large_row.evidence_id
    assert rejected1 == rejected2 == []
    assert "cells=A899,B899" in e1[0].source_locator and e1[0].text.count('"cell"') == 2
    assert "cells=A900,B900" in e2[0].source_locator and e2[0].text.count('"cell"') == 2

    duplicated_identity = json.loads(small_row.text)
    duplicated_identity["cells"].append({
        "cell": "C899", "column": "Legacy Item Number", "value": "AV11-333W-NL",
    })
    duplicate_row = small_row.model_copy(update={"text": json.dumps(duplicated_identity)})
    duplicate_candidates, duplicate_evidence, duplicate_rejections = vendor_candidates_for_item(
        phrase_map, small, [duplicate_row],
    )
    assert [candidate.value for candidate in duplicate_candidates] == ["Other: 3/4"]
    assert "cells=A899,C899,B899" in duplicate_evidence[0].source_locator
    assert duplicate_rejections == []


class ProfileCompletion:
    deployment, effort, max_output_tokens = "offline", "low", 8000
    last_usage = {}

    def __init__(self):
        self.calls = []

    def complete_structured(self, system, user, schema, **kwargs):
        self.calls.append((schema, json.loads(user), kwargs))
        if schema is VendorProfile:
            headers = self.calls[-1][1]["headers"]
            roles = {"Part": "identity", "Vendor": "identity", "Pressure": "attribute_data",
                     "Description": "product_description"}
            return VendorProfile(columns=[ProfileColumn(column=h, role=roles[h]) for h in headers])
        phrase = next(p for p in self.calls[-1][1]["phrases"] if p["column"] == "Pressure")
        return PhraseBatch(candidates=[PhraseCandidate(
            phrase_id=phrase["phrase_id"], attribute_id="Pressure Rating", value=125, unit="psi",
            quote="125", origin="literal", normalization_rule="literal_number_exact_unit_v1",
        )])


def test_vendor_profile_and_phrase_mapping_are_cached_per_file_and_definitions():
    content = sample_workbook()
    store = MemoryStore(content)
    loader = CachedEvidenceLoader(store, namespace="test-owner")
    manifest = Manifest(
        product=ProductKey(item_id="ITEM-1", vendor="Synthetic", mpn="00123", hierarchy_node="Valve"),
        attributes=[AttributeDefinition(attribute_id="Pressure Rating", description="Pressure Rating",
                                        value_type="number", unit="psi")],
    )
    item = {
        "item_key": "row-2", "product": manifest.product.model_dump(mode="json"),
        "vendor_binding": {
            "source_id": "vendor", "sha256": "a" * 64, "blob": "documents/vendor.xlsx",
            "format": "xlsx", "kind": "blob", "source_tier": "vendor_table",
            "table": {"sheet": "Vendor data", "header_row": 3, "mpn_column": "Part",
                      "vendor_column": "Vendor", "expected_vendor": None},
        },
    }
    completion = ProfileCompletion()
    first, first_stats = vendor_profile_and_candidates(
        store, loader, [item], manifest, completion, run_id="one", namespace="owner",
    )
    second, second_stats = vendor_profile_and_candidates(
        store, loader, [item], manifest, completion, run_id="two", namespace="owner",
    )
    assert len(completion.calls) == 2  # one profile, one phrase batch, both cached on replay
    assert first_stats["profile_cache"] == first_stats["phrase_cache"] == "miss"
    assert second_stats["profile_cache"] == second_stats["phrase_cache"] == "hit"
    assert first_stats["unique_phrases"] == 2
    phrase = "Pressure\u0000125"
    assert first[phrase] == second[phrase]
    assert first[phrase][0]["value"] == 125
    assert PROFILED_ROLES == {"product_description", "attribute_data", "component_detail"}
