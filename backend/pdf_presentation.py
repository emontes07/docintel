"""Lossless citation projections over stored PDF cells and paragraphs."""

import re
from collections import defaultdict
from dataclasses import dataclass, replace
from urllib.parse import parse_qs, urldefrag

from backend.models.enrichment import Evidence


_HEADERS = {
    "no", "number", "item", "item no", "description", "material", "size",
    "meter connx size", "meter connection size", "part number", "quantity", "qty",
    "a", "b", "c", "d", "rev", "ecr",
    "valve size", "inlet size", "outlet size", "length", "height",
    "approx wt lbs", "selected submitted items",
}
_FREE_TEXT = re.compile(r"\b(?:note|part number|title|wetted parts)\b", re.IGNORECASE)
_DRAWING_CONTEXT = re.compile(
    r"\b(?:tolerances?|projection|surface preparation|break corners?|revision|rev|ecr)\b",
    re.IGNORECASE,
)


def label(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", "", " ".join(text.casefold().split())).split())


def pure_header(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    return label(text) in _HEADERS or len(lines) > 1 and all(label(line) in _HEADERS for line in lines)


def value_bearing(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and not pure_header(stripped) and not re.fullmatch(
        r"[0-9](?:\s+[0-9])*|[A-Za-z]", stripped,
    )


def scope(entry: Evidence, page: int) -> tuple:
    return (entry.source_id, entry.source_version, urldefrag(entry.source_locator)[0], page,
            None if entry.attribute_ids is None else tuple(entry.attribute_ids), entry.qualification)


@dataclass(frozen=True)
class PdfItem:
    anchor: Evidence
    text: str
    originals: tuple[Evidence, ...]
    values: tuple[Evidence, ...]
    kind: str
    location: dict[str, int]
    context_only: bool = False
    component_materials: tuple[tuple[str, str], ...] = ()
    header_labels: tuple[str, ...] = ()

    def prompt_entry(self) -> dict:
        entry = self.anchor.model_dump(mode="json")
        entry["text"] = self.text
        entry["presentation"] = {
            "kind": self.kind, **self.location, "context_only": self.context_only,
        }
        return entry


def pdf_items(evidence: list[Evidence], *, preserve_model_headers: bool = False) -> list[PdfItem]:
    """Keep originals untouched; header interpretation never becomes value evidence."""
    tables: dict[tuple, dict[int, dict[int, Evidence]]] = defaultdict(lambda: defaultdict(dict))
    paragraphs = []
    untouched = []
    for entry in evidence:
        if entry.source_tier != "internal_pdf" or entry.content_kind != "source_excerpt":
            continue
        _, fragment = urldefrag(entry.source_locator)
        fields = parse_qs(fragment)
        page = fields.get("page", [])
        if len(page) != 1 or not page[0].isdigit():
            untouched.append(entry)
            continue
        position = [fields.get(name, []) for name in ("table", "row", "column")]
        if all(len(value) == 1 and value[0].isdigit() for value in position):
            table, row, column = (int(value[0]) for value in position)
            key = (*scope(entry, int(page[0])), table)
            if column in tables[key][row]:
                raise ValueError("PDF evidence contains duplicate table coordinates in the same applicability scope")
            tables[key][row][column] = entry
        elif "paragraph" in fields:
            paragraphs.append((entry, int(page[0])))
        else:
            untouched.append(entry)

    cell_texts = {
        (key[:-1], " ".join(entry.text.split()))
        for key, rows in tables.items() for row in rows.values() for entry in row.values()
    }
    items = []
    standalone = {}

    def paragraph(entry: Evidence, page: int, location: dict[str, int]) -> None:
        identity = (scope(entry, page), " ".join(entry.text.split()))
        if identity in standalone:
            index = standalone[identity]
            item = items[index]
            if entry.evidence_id not in {original.evidence_id for original in item.originals}:
                items[index] = replace(item, originals=(*item.originals, entry), values=(*item.values, entry))
            return
        standalone[identity] = len(items)
        items.append(PdfItem(
            entry, entry.text, (entry,), (entry,), "paragraph", location,
            bool(_DRAWING_CONTEXT.search(entry.text)),
        ))

    for entry, page in paragraphs:
        if not value_bearing(entry.text):
            continue
        identity = (scope(entry, page), " ".join(entry.text.split()))
        if identity in cell_texts and not _FREE_TEXT.search(entry.text) and not _DRAWING_CONTEXT.search(entry.text):
            continue
        paragraph(entry, page, {"page": page})

    for key, rows in tables.items():
        headers: dict[int, Evidence] = {}
        previous_row = None
        for row_number, cells in sorted(rows.items()):
            if (previous_row is not None and row_number != previous_row + 1
                    and not (preserve_model_headers and any(label(h.text) == "part number" for h in headers.values()))):
                headers = {}
            previous_row = row_number
            if not cells:
                continue
            if len(cells) >= 2 and all(pure_header(cell.text) for cell in cells.values()):
                headers = cells
                continue
            data = {}
            for column, entry in sorted(cells.items()):
                identity = (scope(entry, key[3]), " ".join(entry.text.split()))
                if identity in standalone:
                    paragraph(entry, key[3], {"page": key[3]})
                    continue
                if _FREE_TEXT.search(entry.text):
                    paragraph(entry, key[3], {
                        "page": key[3], "table": key[-1], "row": row_number, "column": column,
                    })
                    continue
                data[column] = entry
            if not any(value_bearing(entry.text) for entry in data.values()):
                continue
            indexes = [column for column, heading in sorted(headers.items()) if label(heading.text) in {"no", "item no"}]
            for start, end in zip(indexes, indexes[1:] + [max(data, default=0) + 1]):
                if not any(value_bearing(entry.text) for column, entry in data.items() if start < column < end):
                    data = {column: entry for column, entry in data.items() if not start <= column < end}
            fields: dict[int, list[Evidence]] = {}
            for column, entry in data.items():
                preceding = [offset for offset in headers if offset <= column]
                inside = bool(headers) and min(headers) <= column <= max(headers)
                if headers and not inside and re.fullmatch(r"\d+|[A-Za-z]", entry.text.strip()):
                    continue
                offset = max(preceding) if preceding and inside else column
                fields.setdefault(offset, []).append(entry)
            text_parts = []
            origins = []
            values = []
            component = ""
            materials = []
            labels = []
            for offset, entries in fields.items():
                heading = headers.get(offset)
                name = label(heading.text) if heading else ""
                if name in {"no", "number", "item", "item no"}:
                    component = ""
                elif name == "description":
                    component = " ".join(entry.text for entry in entries)
                if heading is not None:
                    origins.append(heading)
                labels.append(heading.text if heading else "Column " + str(offset))
                text_parts.append(
                    f"{labels[-1]}="
                    + " ".join(entry.text for entry in entries)
                )
                origins.extend(entries)
                if name not in {"no", "number", "item", "item no"}:
                    values.extend(entries)
                if name == "material" and component:
                    materials.extend((entry.evidence_id, component) for entry in entries)
            if not values:
                continue
            items.append(PdfItem(
                values[0], f"Row {row_number}: " + " | ".join(text_parts),
                tuple(dict((entry.evidence_id, entry) for entry in origins).values()),
                tuple(values), "table_row", {"page": key[3], "table": key[-1], "row": row_number},
                context_only=not headers or any(_DRAWING_CONTEXT.search(heading.text) for heading in headers.values()),
                component_materials=tuple(materials),
                header_labels=tuple(labels),
            ))
    items.extend(PdfItem(entry, entry.text, (entry,), (entry,), "paragraph", {}) for entry in untouched)
    return items
