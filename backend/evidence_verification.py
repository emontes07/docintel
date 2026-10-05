"""Grounded text matching; normalization is not inference or unit conversion."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from urllib.parse import parse_qs, urldefrag

from backend.models.enrichment import Evidence, EvidenceMatch


_NUMBER = r"[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)"
_TOKEN = re.compile(
    rf'''(?<!\w){_NUMBER}(?:/\d+)?(?!\w)|[^\W_]+|(?<=\d)["']|[/%<>≤≥±=+−]''',
    re.UNICODE,
)


@dataclass(frozen=True)
class Fragment:
    evidence: Evidence
    text: str
    region: tuple[str, ...] | None = None
    position: tuple[int, ...] = ()
    cell: str | None = None
    vendor: bool = False


def normalized(text: str, *, vendor: bool = False) -> tuple[list[str], list[str]]:
    rules = []
    value = unicodedata.normalize("NFKC", text)
    if value != text:
        rules.append("unicode_nfkc")
    folded = value.casefold()
    if folded != value:
        rules.append("casefold")
    value = folded.replace("\u2044", "/").replace("\u2212", "-")
    if value != folded:
        rules.append("unicode_numeric_symbols")
    spaced = re.sub(r"(?<=\d)\s*/\s*(?=\d)", "/", value)
    spaced = re.sub(r"(?<!\w)([+-])\s+(?=\d)", r"\1", spaced)
    if spaced != value:
        rules.append("numeric_spacing")
    value = spaced
    marks = re.sub(r"(?<=\d)(?:′′|”|“)", '"', value)
    marks = re.sub(r"(?<=\d)(?:′|’)", "'", marks)
    if marks != value:
        rules.append("unicode_unit_marks")
    value = marks
    if vendor:
        # Inch marks only following a number are unit aliases, never arbitrary quotes.
        changed = re.sub(r'(?<=\d)\s*(?:"|′′|”|“|inches\b|inch\b|in\.?(?!\w))', " in", value)
        if changed != value:
            rules.append("inch_unit_alias")
        value = changed
        # NFKC expands a vulgar fraction after an integer without a separator.
        # Insert that separator from the original character before rationalization.
        mixed = re.sub(r"(\d)([¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞])", r"\1 \2", text)
        if mixed != text:
            value, nested = normalized(mixed, vendor=True)
            return value, sorted(set(rules + nested + ["numeric_format"]))
        mixed_value = re.sub(
            r"(?<![\w./])(\d+)[ -]+(\d+)/(\d+)(?![\w./])",
            lambda match: str(Fraction(int(match[1])) + Fraction(int(match[2]), int(match[3])))
            if int(match[3]) else match[0],
            value,
        )
        if mixed_value != value:
            rules.append("numeric_format")
        value = mixed_value
    tokens = _TOKEN.findall(value)
    if vendor:
        converted = []
        for token in tokens:
            if re.fullmatch(rf"{_NUMBER}(?:/\d+)?", token):
                if re.fullmatch(r"[+-]?0\d+", token):
                    converted.append(token)
                    continue
                try:
                    numbers = token.replace(",", "").split("/")
                    number = str(Fraction(numbers[0]) / Fraction(numbers[1])) if len(numbers) == 2 else str(Fraction(numbers[0]))
                except ZeroDivisionError:
                    number = token
                if number != token:
                    rules.append("numeric_format")
                token = number
            converted.append(token)
        tokens = converted
    if re.search(r"\s", value) and " ".join(value.split()) != value:
        rules.append("whitespace")
    # Decimal points, fraction slashes, signs and comparison symbols stay in tokens.
    if " ".join(tokens) != " ".join(value.split()):
        rules.append("punctuation_hyphen_spacing")
    return tokens, sorted(set(rules))


def _column(address: str) -> int:
    result = 0
    for char in re.match(r"[A-Z]+", address)[0]:
        result = result * 26 + ord(char) - ord("A") + 1
    return result


def fragments(entry: Evidence) -> list[Fragment]:
    root, fragment = urldefrag(entry.source_locator)
    scope = (entry.source_id, entry.source_version, entry.source_tier, root)
    if entry.source_tier == "vendor_table" and entry.text.lstrip().startswith("{"):
        try:
            row = json.loads(entry.text)
        except json.JSONDecodeError as error:
            raise ValueError("Vendor row evidence must contain a valid cell record") from error
        if not isinstance(row, dict) or not isinstance(row.get("sheet"), str) or type(row.get("row")) is not int or not isinstance(row.get("cells"), list):
            raise ValueError("Vendor row evidence must identify its sheet, row and cells")
        parts = []
        seen = set()
        for cell in row["cells"]:
            if not isinstance(cell, dict) or not isinstance(cell.get("cell"), str) or not isinstance(cell.get("value"), str):
                raise ValueError("Vendor row evidence must contain addressed string cell values")
            address = cell["cell"]
            if not re.fullmatch(r"[A-Z]+[1-9]\d*", address) or int(re.search(r"\d+", address)[0]) != row["row"] or address in seen:
                raise ValueError("Vendor cell addresses must be unique and belong to the cited row")
            seen.add(address)
            parts.append(Fragment(
                entry, cell["value"], (*scope, "vendor", row["sheet"], str(row["row"])),
                (_column(address),), address, True,
            ))
        if not parts:
            raise ValueError("Vendor row evidence contains no cell values")
        return sorted(parts, key=lambda part: part.position)
    locator = parse_qs(fragment)
    page = locator.get("page", [])
    if len(page) == 1 and page[0].isdigit():
        paragraph = locator.get("paragraph", [])
        table, row, column = (locator.get(key, []) for key in ("table", "row", "column"))
        if len(paragraph) == 1 and paragraph[0].isdigit():
            return [Fragment(entry, entry.text, (*scope, page[0], "paragraph"), (int(paragraph[0]),))]
        if all(len(value) == 1 and value[0].isdigit() for value in (table, row, column)):
            return [Fragment(entry, entry.text, (*scope, page[0], "table", table[0]), (int(row[0]), int(column[0])))]
    return [Fragment(entry, entry.text, vendor=entry.source_tier == "vendor_table")]


def windows(evidence: list[Evidence], cited: set[str]) -> list[list[Fragment]]:
    """Only cited, consecutive fragments in a known region may be joined."""
    selected = []
    regions: dict[tuple[str, ...], list[Fragment]] = {}
    for entry in evidence:
        if entry.content_kind != "source_excerpt":
            continue
        # Uncited vendor JSON cannot invalidate a different candidate.
        if entry.evidence_id not in cited and entry.source_tier == "vendor_table":
            continue
        for part in fragments(entry):
            if part.region is None:
                if entry.evidence_id in cited:
                    selected.append([part])
            else:
                regions.setdefault(part.region, []).append(part)
    for parts in regions.values():
        run = []
        previous = None
        for part in sorted(parts, key=lambda item: item.position):
            gap = previous is not None and (
                part.position == previous.position
                or (part.region[-1] == "paragraph" and part.position[0] != previous.position[0] + 1)
                or (len(part.position) == 2 and (
                    part.position[0] == previous.position[0] and part.position[1] != previous.position[1] + 1
                    or part.position[0] != previous.position[0] and (
                        part.position[0] != previous.position[0] + 1 or part.position[1] != 0
                    )
                ))
            )
            if part.evidence.evidence_id not in cited or gap:
                if run:
                    selected.append(run)
                    run = []
            if part.evidence.evidence_id in cited:
                run.append(part)
            previous = part
        if run:
            selected.append(run)
    return selected


def match_text(text: str, spans: list[list[Fragment]]) -> EvidenceMatch | None:
    for span in spans:
        vendor = all(part.vendor for part in span)
        target, target_rules = normalized(text, vendor=vendor)
        if not target:
            continue
        haystack = []
        owners = []
        rules = []
        for part in span:
            tokens, applied = normalized(part.text, vendor=vendor)
            haystack.extend(tokens)
            owners.extend([part] * len(tokens))
            rules.extend([applied] * len(tokens))
        for start in range(len(haystack) - len(target) + 1):
            end = start + len(target)
            if haystack[start:end] != target:
                continue
            matched = []
            for part in owners[start:end]:
                if part not in matched:
                    matched.append(part)
            literal = len(matched) == 1 and re.search(
                rf"(?<!\w){re.escape(text)}(?!\w)", matched[0].text,
            ) is not None
            applied = [] if literal else sorted(set(target_rules + [
                rule for group in rules[start:end] for rule in group
            ]))
            return EvidenceMatch(
                method="vendor_cells" if any(part.cell for part in matched) else
                "adjacent_fragments" if len(matched) > 1 else "verbatim" if literal else "normalized",
                normalization=applied,
                evidence_ids=list(dict.fromkeys(part.evidence.evidence_id for part in matched)),
                source_locations=list(dict.fromkeys(part.evidence.source_locator for part in matched)),
                cells=[part.cell for part in matched if part.cell],
                normalized_sha256=hashlib.sha256(" ".join(target).encode()).hexdigest(),
            )
    return None
