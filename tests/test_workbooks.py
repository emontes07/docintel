from io import BytesIO
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


@pytest.mark.parametrize("replacement,match", [
    (b'<c r="A2"><f>1+1</f><v>2</v></c>', "Formula"),
    (b'<c r="A2" t="n" s="1"><v>123</v></c>', "Formatted"),
    (b'<c r="A3" t="n"><v>123</v></c>', "mismatched"),
])
def test_ambiguous_and_executable_cells_are_rejected(replacement, match):
    import re
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


def test_long_export_evidence_is_chunked_without_loss():
    from backend.batch import export_workbook
    evidence = "=untrusted literal evidence " * 2500
    exported = read_workbook(export_workbook({"Evidence": [["Excerpt"], [evidence]]}))
    assert exported["Evidence"][0]["Excerpt"].startswith("Full value in Long text")
    assert "".join(row["Text"] for row in exported["Long text"]) == evidence