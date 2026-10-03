"""Operator-selected XLSX rows are evidence, never attribute definitions."""

import json

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


def read_vendor_table(
    content: bytes, *, config: VendorTableConfig, product: ProductKey, source_id: str
) -> list[VendorTableChunk]:
    """Return only exact MPN/vendor matches, preserving original workbook addresses."""
    sheets = read_workbook_cells(content, max_rows=50001)
    if config.sheet not in sheets:
        raise WorkbookError("Configured vendor worksheet was not found")
    rows = sheets[config.sheet]
    header_row = next((row for row in rows if row.number == config.header_row), None)
    if header_row is None:
        raise WorkbookError("Configured vendor header row was not found")
    headers = {column: cell.text for column, cell in header_row.cells.items() if cell.value}
    if not headers or len(set(headers.values())) != len(headers) or any(not title.strip() for title in headers.values()):
        raise WorkbookError("Vendor headers must be nonblank and unique")
    columns = {title: column for column, title in headers.items()}
    if config.mpn_column not in columns or (config.vendor_column and config.vendor_column not in columns):
        raise WorkbookError("Configured vendor identity column was not found")
    if config.expected_vendor is not None and config.expected_vendor != product.vendor:
        return []
    mpn_column = columns[config.mpn_column]
    vendor_column = columns[config.vendor_column] if config.vendor_column else None
    chunks = []
    for row in rows:
        if row.number <= config.header_row:
            continue
        mpn = row.cells.get(mpn_column)
        if mpn is None or mpn.text != product.mpn:
            continue
        if vendor_column is not None:
            vendor = row.cells.get(vendor_column)
            if vendor is None or vendor.text != product.vendor:
                continue
        if any(cell.value and column not in headers for column, cell in row.cells.items()):
            raise WorkbookError("Matched vendor data column has no header")
        cells = {cell.address: cell.text for column, cell in sorted(row.cells.items()) if column in headers and cell.value}
        if not cells:
            continue
        quoted_sheet = "'" + config.sheet.replace("'", "''") + "'"
        addresses = list(cells)
        locator = f"{source_id}#{quoted_sheet}!{addresses[0]}:{addresses[-1]}"
        text = json.dumps(
            {"sheet": config.sheet, "row": row.number, "cells": [
                {"cell": cell.address, "column": headers[column], "value": cells[cell.address]}
                for column, cell in sorted(row.cells.items()) if cell.address in cells
            ]},
            ensure_ascii=False,
        )
        chunks.append(VendorTableChunk(source_id=source_id, source_locator=locator, sheet=config.sheet, row=row.number, cells=cells, text=text))
    return chunks
