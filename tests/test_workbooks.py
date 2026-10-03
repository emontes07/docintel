from io import BytesIO
import re
from zipfile import ZipFile

import pytest

from backend.workbooks import WorkbookError, read_workbook, write_workbook


def test_identifiers_and_formula_like_text_round_trip():
    values = ["00123", "=HYPERLINK(\"https://invalid\")", "+1", "@SUM(A1)", "-2", "\t=1", "text & <tag>"]
    content = write_workbook({"Inputs": [["Identifier", "Value"], *[[value, value] for value in values]]})
    assert [row["Identifier"] for row in read_workbook(content)["Inputs"]] == values
    with ZipFile(BytesIO(content)) as archive:
        assert b"<f>" not in archive.read("xl/worksheets/sheet1.xml")
        assert b't="inlineStr"' in archive.read("xl/worksheets/sheet1.xml")


def test_invalid_and_duplicate_headers_fail_closed():
    with pytest.raises(WorkbookError):
        read_workbook(b"not a workbook")
    with pytest.raises(WorkbookError, match="unique"):
        read_workbook(write_workbook({"Inputs": [["ID", "ID"], ["a", "b"]]}))


def styled_workbook(format_id, *, format_code=None, value="123"):
    original = write_workbook({"Inputs": [["ID"], ["placeholder"]]})
    output = BytesIO()
    number_formats = f'<numFmts count="1"><numFmt numFmtId="{format_id}" formatCode="{format_code}"/></numFmts>' if format_code else ""
    styles = f'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{number_formats}<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="{format_id}" applyAlignment="1"><alignment wrapText="1"/></xf></cellXfs></styleSheet>'
    with ZipFile(BytesIO(original)) as source, ZipFile(output, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name == "xl/worksheets/sheet1.xml":
                content = re.sub(rb'<c r="A2".*?</c>', f'<c r="A2" s="1"><v>{value}</v></c>'.encode(), content)
            target.writestr(name, content)
        target.writestr("xl/styles.xml", styles)
    return output.getvalue()


def test_wrap_text_style_is_not_a_numeric_format_and_lost_zeros_are_not_invented():
    assert read_workbook(styled_workbook(0))["Inputs"][0]["ID"] == "123"


@pytest.mark.parametrize("format_id,format_code,value,expected", [
    (164, "000000", "123", "000123"),
    (164, "000000", "00123", "000123"),
    (164, "000000", "0", "000000"),
    (164, "000000", "1.23E2", "000123"),
    (164, "000000", "123456789012345678901234567890", "123456789012345678901234567890"),
    (1, None, "123", "123"),
    (49, None, "00123", "00123"),
    (4, None, "1234.567", "1234.567"),
])
def test_supported_numeric_formats_preserve_identity_or_exact_numeric_value(format_id, format_code, value, expected):
    assert read_workbook(styled_workbook(format_id, format_code=format_code, value=value))["Inputs"][0]["ID"] == expected


@pytest.mark.parametrize("format_id,format_code,value", [
    (14, None, "45000"),
    (164, "mm-dd-yyyy", "45000"),
    (164, "00000", "12.34"),
    (164, "00000", "NaN"),
    (164, "00000", "Infinity"),
    (164, "00000", "1E999999"),
])
def test_dates_lossy_zero_formats_and_nonfinite_values_remain_rejected(format_id, format_code, value):
    with pytest.raises(WorkbookError):
        read_workbook(styled_workbook(format_id, format_code=format_code, value=value))


@pytest.mark.parametrize("replacement,match", [
    (b'<c r="A2"><f>1+1</f><v>2</v></c>', "Formula"),
    (b'<c r="A2" t="n" s="1"><v>123</v></c>', "Formatted"),
    (b'<c r="A3" t="n"><v>123</v></c>', "mismatched"),
])
def test_ambiguous_and_executable_cells_are_rejected(replacement, match):
    original = write_workbook({"Inputs": [["ID"], ["00123"]]})
    output = BytesIO()
    with ZipFile(BytesIO(original)) as source, ZipFile(output, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name == "xl/worksheets/sheet1.xml":
                content = re.sub(rb'<c r="A2".*?</c>', replacement, content)
            target.writestr(name, content)
    with pytest.raises(WorkbookError, match=match):
        read_workbook(output.getvalue())


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_entity_declarations_remain_forbidden_for_all_supported_entry_points(encoding):
    from backend.workbooks import read_workbook_cells
    original = write_workbook({"Inputs": [["ID"], ["00123"]]})
    output = BytesIO()
    with ZipFile(BytesIO(original)) as source, ZipFile(output, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name == "xl/sharedStrings.xml":
                continue
            target.writestr(name, content)
        target.writestr("xl/sharedStrings.xml", '<!DOCTYPE sst [<!ENTITY attack "untrusted">]><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>&attack;</t></si></sst>'.encode(encoding))
    with pytest.raises(WorkbookError, match="entity"):
        read_workbook_cells(output.getvalue(), max_rows=50001)


def test_rich_text_identity_excludes_phonetic_annotations():
    original = write_workbook({"Inputs": [["ID"], ["00123"]]})
    output = BytesIO()
    with ZipFile(BytesIO(original)) as source, ZipFile(output, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name == "xl/worksheets/sheet1.xml":
                content = re.sub(rb'<c r="A2".*?</c>', b'<c r="A2" t="inlineStr"><is><r><t>001</t></r><r><t>23</t></r><rPh sb="0" eb="5"><t>not identity</t></rPh></is></c>', content)
            target.writestr(name, content)
    assert read_workbook(output.getvalue())["Inputs"][0]["ID"] == "00123"


def test_long_export_evidence_is_chunked_without_loss():
    from backend.batch import export_workbook
    evidence = "=untrusted literal evidence " * 2500
    exported = read_workbook(export_workbook({"Evidence": [["Excerpt"], [evidence]]}))
    assert exported["Evidence"][0]["Excerpt"].startswith("Full value in Long text")
    assert "".join(row["Text"] for row in exported["Long text"]) == evidence