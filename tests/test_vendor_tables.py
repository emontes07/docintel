from io import BytesIO
import json
import re
from zipfile import ZipFile

import pytest

from backend.core.vendor_tables import VendorTableConfig, read_vendor_table
from backend.models.enrichment import ProductKey
from backend.workbooks import WorkbookError, read_workbook, write_workbook


def product(mpn="00123", vendor="Synthetic"):
    return ProductKey(item_id="ITEM-1", vendor=vendor, mpn=mpn, hierarchy_node="Valve")


def config(**overrides):
    return VendorTableConfig(**{"sheet": "Vendor data", "header_row": 3, "mpn_column": "Part", "vendor_column": "Vendor", **overrides})


def workbook():
    return write_workbook({"Vendor data": [
        ["Synthetic vendor export"],
        ["Report metadata, not evidence"],
        ["Part", "Vendor", "Pressure", "Description"],
        ["00123", "Synthetic", "125", "Variant-specific rating"],
        ["00123-X", "Synthetic", "999", "Not the selected variant"],
        ["00123", "Other", "888", "Not the selected vendor"],
        ["123", "Synthetic", "777", "Lost zeros must not be invented"],
    ]})


def test_explicit_header_and_exact_identity_return_only_matching_rows():
    chunks = read_vendor_table(workbook(), config=config(), product=product(), source_id="vendor")
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.source_id == "vendor"
    assert chunk.sheet == "Vendor data" and chunk.row == 4
    assert chunk.cells == {"A4": "00123", "B4": "Synthetic", "C4": "125", "D4": "Variant-specific rating"}
    assert chunk.source_locator == "vendor#'Vendor data'!A4:D4"
    assert json.loads(chunk.text)["cells"][0] == {"cell": "A4", "column": "Part", "value": "00123"}
    assert "999" not in chunk.text and "888" not in chunk.text and "777" not in chunk.text


def test_configured_single_vendor_association_and_missing_matches():
    selected = config(vendor_column=None, expected_vendor="Synthetic")
    content = write_workbook({"Vendor data": [["Export"], ["Metadata"], ["Part", "Value"], ["00123", "125"]]})
    assert len(read_vendor_table(content, config=selected, product=product(), source_id="vendor")) == 1
    assert read_vendor_table(content, config=selected, product=product(vendor="Other"), source_id="vendor") == []
    assert read_vendor_table(content, config=selected, product=product(mpn="00123-X"), source_id="vendor") == []
    with pytest.raises(ValueError, match="vendor"):
        config(vendor_column=None)


@pytest.mark.parametrize("overrides", [
    {"sheet": "Missing"}, {"header_row": 2}, {"mpn_column": "Guess"}, {"vendor_column": "Guess"}
])
def test_table_configuration_is_not_guessed(overrides):
    with pytest.raises(WorkbookError):
        read_vendor_table(workbook(), config=config(**overrides), product=product(), source_id="vendor")


def rewrite_sheet(content, change, styles=None):
    output = BytesIO()
    with ZipFile(BytesIO(content)) as source, ZipFile(output, "w") as target:
        for name in source.namelist():
            raw = source.read(name)
            target.writestr(name, change(raw) if name == "xl/worksheets/sheet1.xml" else raw)
        if styles:
            target.writestr("xl/styles.xml", styles)
    return output.getvalue()


def test_metadata_dates_and_row_gaps_do_not_change_evidence_addresses():
    styles = '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>'
    content = rewrite_sheet(
        workbook(),
        lambda raw: re.sub(rb'<c r="A1".*?</c>', b'<c r="A1" s="1"><v>45000</v></c>', raw).replace(b'r="4"', b'r="40"').replace(b'r="A4"', b'r="A40"').replace(b'r="B4"', b'r="B40"').replace(b'r="C4"', b'r="C40"').replace(b'r="D4"', b'r="D40"'),
        styles,
    )
    # Keep physical XML rows in order after moving the matching record past the other variants.
    content = rewrite_sheet(content, lambda raw: re.sub(rb'(<row r="40">.*?</row>)(.*?)(</sheetData>)', rb'\2\1\3', raw))
    chunks = read_vendor_table(content, config=config(), product=product(), source_id="vendor")
    assert [chunk.row for chunk in chunks] == [40]
    assert chunks[0].cells["A40"] == "00123"
    assert "45000" not in chunks[0].text


def test_formulas_in_unmatched_rows_are_still_rejected():
    content = rewrite_sheet(workbook(), lambda raw: re.sub(rb'<c r="C5".*?</c>', b'<c r="C5"><f>1+1</f><v>2</v></c>', raw))
    with pytest.raises(WorkbookError, match="Formula"):
        read_vendor_table(content, config=config(), product=product(), source_id="vendor")


def test_duplicate_exact_rows_remain_separate_evidence_not_a_fabricated_consensus():
    content = write_workbook({"Vendor data": [["Part", "Value"], ["00123", "125"], ["00123", "150"]]})
    chunks = read_vendor_table(content, config=config(header_row=1, vendor_column=None, expected_vendor="Synthetic"), product=product(), source_id="vendor")
    assert [chunk.row for chunk in chunks] == [2, 3]
    assert [chunk.cells[f"B{chunk.row}"] for chunk in chunks] == ["125", "150"]


def test_vendor_row_bound_is_separate_from_customer_intake_bound():
    content = write_workbook({"Vendor data": [["Part", "Value"], *[["OTHER", "0"]] * 10000, ["00123", "125"]]})
    with pytest.raises(WorkbookError, match="10,000"):
        read_workbook(content)
    chunks = read_vendor_table(content, config=config(header_row=1, vendor_column=None, expected_vendor="Synthetic"), product=product(), source_id="vendor")
    assert len(chunks) == 1 and chunks[0].row == 10002


def test_same_mpn_with_internal_whitespace_is_not_normalized():
    content = write_workbook({"Vendor data": [["Part", "Value"], ["A  1", "125"]]})
    selected = config(header_row=1, vendor_column=None, expected_vendor="Synthetic")
    assert read_vendor_table(content, config=selected, product=product(mpn="A 1"), source_id="vendor") == []
    assert len(read_vendor_table(content, config=selected, product=product(mpn="A  1"), source_id="vendor")) == 1
