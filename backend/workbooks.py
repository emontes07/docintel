"""Bounded, data-only XLSX intake and formula-safe XLSX export."""

import posixpath
import re
from io import BytesIO
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED
import xml.etree.ElementTree as ET

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"m": MAIN}
MAX_BYTES = 10 * 1024 * 1024


class WorkbookError(ValueError):
    pass


def read_workbook(content: bytes) -> dict[str, list[dict[str, str]]]:
    if len(content) > MAX_BYTES:
        raise WorkbookError("Workbook exceeds 10 MiB")
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 500 or sum(entry.file_size for entry in entries) > 50 * 1024 * 1024:
                raise WorkbookError("Workbook expanded size exceeds the intake limit")
            if len({entry.filename for entry in entries}) != len(entries):
                raise WorkbookError("Duplicate workbook archive entries")
            if any("vbaproject" in entry.filename.lower() or "externallinks/" in entry.filename.lower() for entry in entries):
                raise WorkbookError("Macros and external workbook links are unsupported")

            def xml(name):
                raw = archive.read(name)
                if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                    raise WorkbookError("XML entity declarations are forbidden")
                return ET.fromstring(raw)

            shared = ["".join(node.itertext()) for node in xml("xl/sharedStrings.xml").findall("m:si", NS)] if "xl/sharedStrings.xml" in archive.namelist() else []
            relationships = {node.attrib["Id"]: node.attrib["Target"] for node in xml("xl/_rels/workbook.xml.rels") if node.attrib.get("TargetMode") != "External"}
            output = {}
            for sheet in xml("xl/workbook.xml").findall("m:sheets/m:sheet", NS):
                target = relationships[sheet.attrib[f"{{{REL}}}id"]]
                name = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
                if not name.startswith("xl/worksheets/"):
                    raise WorkbookError("Unsupported worksheet relationship")
                root = xml(name)
                if root.findall(".//m:f", NS) or root.find("m:mergeCells", NS) is not None:
                    raise WorkbookError("Formula cells and merged cells are unsupported; provide data-only workbooks")
                rows = root.findall("m:sheetData/m:row", NS)
                if len(rows) > 10001:
                    raise WorkbookError("A worksheet may contain at most 10,000 data rows")
                matrix = []
                for row in rows:
                    values = {}
                    for cell in row.findall("m:c", NS):
                        address = cell.attrib.get("r", "")
                        match = re.fullmatch(r"([A-Z]+)[1-9][0-9]*", address)
                        if not match:
                            raise WorkbookError("Cell address missing or invalid")
                        column = 0
                        for letter in match[1]:
                            column = column * 26 + ord(letter) - 64
                        if column > 256:
                            raise WorkbookError("At most 256 columns are supported")
                        value = cell.find("m:v", NS)
                        text = value.text or "" if value is not None else ""
                        kind = cell.attrib.get("t", "n")
                        if text and kind not in {"s", "inlineStr", "str"} and cell.attrib.get("s", "0") != "0":
                            raise WorkbookError("Formatted numeric/date cells are unsupported; preserve input values as explicit text")
                        if kind == "s":
                            text = shared[int(text)]
                        elif kind == "inlineStr":
                            text = "".join(cell.find("m:is", NS).itertext())
                        elif kind == "e":
                            raise WorkbookError("Workbook contains an Excel error cell")
                        if len(text) > 32767:
                            raise WorkbookError("Cell exceeds Excel text limit")
                        if column in values or int(re.search(r"[0-9]+$", address)[0]) != int(row.attrib["r"]):
                            raise WorkbookError("Duplicate or mismatched cell address")
                        values[column] = text
                    if any(values.values()):
                        if int(row.attrib["r"]) != len(matrix) + 1:
                            raise WorkbookError("Use a row-1 header and contiguous data rows; blank gaps would lose row provenance")
                        matrix.append(values)
                if not matrix:
                    continue
                header = matrix[0]
                if len(set(header.values())) != len(header) or any(not value.strip() for value in header.values()):
                    raise WorkbookError("Headers must be nonblank and unique")
                if any(set(row) - set(header) for row in matrix[1:]):
                    raise WorkbookError("Data column has no header")
                output[sheet.attrib["name"]] = [{title: row.get(column, "") for column, title in header.items()} for row in matrix[1:]]
            return output
    except WorkbookError:
        raise
    except (BadZipFile, KeyError, ValueError, IndexError, AttributeError, ET.ParseError):
        raise WorkbookError("Invalid or unsupported XLSX workbook") from None


def write_workbook(sheets: dict[str, list[list[object]]]) -> bytes:
    stream = BytesIO()
    content_types = ET.Element("Types", xmlns="http://schemas.openxmlformats.org/package/2006/content-types")
    ET.SubElement(content_types, "Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml")
    ET.SubElement(content_types, "Default", Extension="xml", ContentType="application/xml")
    ET.SubElement(content_types, "Override", PartName="/xl/workbook.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml")
    workbook = ET.Element("workbook", xmlns=MAIN)
    sheet_list = ET.SubElement(workbook, "sheets")
    relationships = ET.Element("Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships")
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for index, (name, rows) in enumerate(sheets.items(), 1):
            if not name or len(name) > 31 or re.search(r"[\\/*?:\[\]]", name):
                raise WorkbookError("Invalid export sheet name")
            ET.SubElement(sheet_list, "sheet", name=name, sheetId=str(index), attrib={f"{{{REL}}}id": f"rId{index}"})
            ET.SubElement(relationships, "Relationship", Id=f"rId{index}", Type=f"{REL}/worksheet", Target=f"worksheets/sheet{index}.xml")
            ET.SubElement(content_types, "Override", PartName=f"/xl/worksheets/sheet{index}.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml")
            worksheet = ET.Element("worksheet", xmlns=MAIN)
            data = ET.SubElement(worksheet, "sheetData")
            for row_index, row in enumerate(rows, 1):
                if row_index > 1048576:
                    raise WorkbookError("Export exceeds Excel row limit")
                row_node = ET.SubElement(data, "row", r=str(row_index))
                for column, value in enumerate(row, 1):
                    text = "" if value is None else str(value)
                    if len(text) > 32767 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
                        raise WorkbookError("Export value exceeds Excel text constraints; JSON remains available")
                    letters = ""
                    while column:
                        column, remainder = divmod(column - 1, 26)
                        letters = chr(65 + remainder) + letters
                    cell = ET.SubElement(row_node, "c", r=f"{letters}{row_index}", t="inlineStr")
                    inline = ET.SubElement(cell, "is")
                    ET.SubElement(inline, "t", attrib={"{http://www.w3.org/XML/1998/namespace}space": "preserve"}).text = text
            archive.writestr(f"xl/worksheets/sheet{index}.xml", ET.tostring(worksheet, encoding="utf-8", xml_declaration=True))
        archive.writestr("xl/workbook.xml", ET.tostring(workbook))
        archive.writestr("xl/_rels/workbook.xml.rels", ET.tostring(relationships))
        archive.writestr("[Content_Types].xml", ET.tostring(content_types))
        root = ET.Element("Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships")
        ET.SubElement(root, "Relationship", Id="rId1", Type=f"{REL}/officeDocument", Target="xl/workbook.xml")
        archive.writestr("_rels/.rels", ET.tostring(root))
    return stream.getvalue()