"""Operator-selected XLSX rows are evidence, never attribute definitions."""

import json
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.models.enrichment import ProductKey
from backend.workbooks import WorkbookError, read_workbook_cells


class VendorTableConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheet: str = Field(min_length=1)
    header_row: int = Field(default=1, ge=1, le=50000)
    mpn_column: str = Field(min_length=1)
    vendor_column: str | None = None
    expected_vendor: str | None = None

    @model_validator(mode="after")
    def validate_association(self):
        if any(value is not None and not value.strip() for value in (self.sheet, self.mpn_column, self.vendor_column, self.expected_vendor)):
            raise ValueError("Table configuration must not contain blank names")
        if not self.vendor_column and not self.expected_vendor:
            raise ValueError("A vendor column or explicit expected_vendor association is required")
        return self


class VendorTableChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    source_locator: str
    sheet: str
    row: int
    cells: dict[str, str]
    text: str


def parse_vendor_workbook(content: bytes) -> dict[str, list[dict]]:
    """Parse a workbook once into address-preserving, JSON-safe cell values."""
    sheets = read_workbook_cells(content, max_rows=50001)
    return {
        name: [
            {"row": row.number, "cells": [
                {"column_index": column, "address": cell.address, "value": cell.value,
                 "kind": cell.kind, "number_format": cell.number_format}
                for column, cell in sorted(row.cells.items()) if cell.value
            ]}
            for row in rows
        ] for name, rows in sheets.items()
    }


def build_vendor_mpn_index(workbook: dict[str, list[dict]], config: VendorTableConfig) -> dict[str, list[int]]:
    """Build the exact-text MPN -> row-number index once for a configured worksheet."""
    rows = workbook.get(config.sheet)
    if rows is None:
        raise WorkbookError("Configured vendor worksheet was not found")
    header = next((row for row in rows if row["row"] == config.header_row), None)
    if header is None:
        raise WorkbookError("Configured vendor header row was not found")
    columns = {str(cell["value"]): cell["column_index"] for cell in header["cells"]}
    if config.mpn_column not in columns:
        raise WorkbookError("Configured vendor identity column was not found")
    mpn_column = columns[config.mpn_column]
    index: dict[str, list[int]] = {}
    for row in rows:
        if row["row"] <= config.header_row:
            continue
        cell = next((entry for entry in row["cells"] if entry["column_index"] == mpn_column), None)
        if cell is not None:
            index.setdefault(str(cell["value"]), []).append(row["row"])
    return index


def read_vendor_table_index(
    workbook: dict[str, list[dict]], *, config: VendorTableConfig, product: ProductKey, source_id: str,
    mpn_rows: Mapping[str, list[int]] | None = None,
) -> list[VendorTableChunk]:
    """Select exact MPN/vendor rows from an already-parsed workbook index."""
    if config.sheet not in workbook:
        raise WorkbookError("Configured vendor worksheet was not found")
    rows = workbook[config.sheet]
    header = next((row for row in rows if row["row"] == config.header_row), None)
    if header is None:
        raise WorkbookError("Configured vendor header row was not found")
    headers = {cell["column_index"]: str(cell["value"]) for cell in header["cells"]}
    if (not headers or len(set(headers.values())) != len(headers)
            or any(not title.strip() for title in headers.values())):
        raise WorkbookError("Vendor headers must be nonblank and unique")
    columns = {title: column for column, title in headers.items()}
    if config.mpn_column not in columns or (config.vendor_column and config.vendor_column not in columns):
        raise WorkbookError("Configured vendor identity column was not found")
    if config.expected_vendor is not None and config.expected_vendor != product.vendor:
        return []
    mpn_column = columns[config.mpn_column]
    vendor_column = columns[config.vendor_column] if config.vendor_column else None
    chunks = []
    if mpn_rows is not None:
        selected_rows = set(mpn_rows.get(product.mpn, []))
        candidate_rows = (row for row in rows if row["row"] in selected_rows)
    else:
        candidate_rows = (row for row in rows if row["row"] > config.header_row)
    for row in candidate_rows:
        cells_by_col = {cell["column_index"]: cell for cell in row["cells"]}
        mpn = cells_by_col.get(mpn_column)
        if mpn is None or str(mpn["value"]) != product.mpn:
            continue
        if vendor_column is not None:
            vendor = cells_by_col.get(vendor_column)
            if vendor is None or str(vendor["value"]) != product.vendor:
                continue
        if any(cell["column_index"] not in headers for cell in row["cells"]):
            raise WorkbookError("Matched vendor data column has no header")
        cells = {cell["address"]: str(cell["value"]) for cell in row["cells"]
                 if cell["column_index"] in headers and cell["value"] is not None}
        if not cells:
            continue
        quoted_sheet = "'" + config.sheet.replace("'", "''") + "'"
        addresses = list(cells)
        locator = f"{source_id}#{quoted_sheet}!{addresses[0]}:{addresses[-1]}"
        text = json.dumps(
            {"sheet": config.sheet, "row": row["row"], "cells": [
                {"cell": cell["address"], "column": headers[cell["column_index"]],
                 "value": str(cell["value"])}
                for cell in row["cells"] if cell["address"] in cells
            ]}, ensure_ascii=False,
        )
        chunks.append(VendorTableChunk(source_id=source_id, source_locator=locator, sheet=config.sheet,
                                       row=row["row"], cells=cells, text=text))
    return chunks


def read_vendor_table(
    content: bytes, *, config: VendorTableConfig, product: ProductKey, source_id: str,
    workbook_index: dict[str, list[dict]] | None = None,
    mpn_rows: Mapping[str, list[int]] | None = None,
) -> list[VendorTableChunk]:
    """Return only exact MPN/vendor matches, preserving original workbook addresses."""
    index = workbook_index if workbook_index is not None else parse_vendor_workbook(content)
    return read_vendor_table_index(index, config=config, product=product, source_id=source_id, mpn_rows=mpn_rows)
