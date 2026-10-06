"""Grounded text matching; normalization is not inference or unit conversion."""

import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from urllib.parse import parse_qs, urldefrag

from backend.models.enrichment import Evidence, EvidenceMatch
from backend.pdf_presentation import pdf_items


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
    originals: tuple[Evidence, ...] = ()
    header_labels: tuple[str, ...] = ()


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
                    or part.position[0] != previous.position[0]
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


def reconstructed_rows(evidence: list[Evidence], cited: set[str]) -> list[list[Fragment]]:
    return [[Fragment(item.anchor, item.text, originals=item.originals, header_labels=item.header_labels)]
            for item in pdf_items(evidence) if item.kind == "table_row"
            and {entry.evidence_id for entry in item.originals} <= cited]


def value_windows(evidence: list[Evidence], cited: set[str]) -> list[list[Fragment]]:
    items = pdf_items(evidence)
    values = {entry.evidence_id for item in items for entry in item.values}
    metadata = {entry.evidence_id for item in items for entry in item.originals} - values
    return windows(evidence, cited - metadata)


def match_text(text: str, spans: list[list[Fragment]]) -> EvidenceMatch | None:
    for span in spans:
        vendor = all(part.vendor for part in span)
        labels = tuple(label for part in span for label in part.header_labels)

        def comparison(value: str) -> str:
            if labels:
                value = unicodedata.normalize("NFKC", value)
            for label in labels:
                label = unicodedata.normalize("NFKC", label)
                value = re.sub(
                    rf"(?<!\w){re.escape(label)}\s*=\s*", lambda _, replacement=label + " ": replacement,
                    value, flags=re.IGNORECASE,
                )
            return value

        target, target_rules = normalized(comparison(text), vendor=vendor)
        if labels and unicodedata.normalize("NFKC", text) != text:
            target_rules.append("unicode_nfkc")
        if not target:
            continue
        haystack = []
        owners = []
        rules = []
        for part in span:
            tokens, applied = normalized(comparison(part.text), vendor=vendor)
            if labels and unicodedata.normalize("NFKC", part.text) != part.text:
                applied.append("unicode_nfkc")
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
            originals = [entry for part in matched for entry in (part.originals or (part.evidence,))]
            reconstructed = any(part.originals for part in matched)
            return EvidenceMatch(
                method="reconstructed_row" if reconstructed else "vendor_cells" if any(part.cell for part in matched) else
                "adjacent_fragments" if len(matched) > 1 else "verbatim" if literal else "normalized",
                normalization=sorted(set(applied + (["table_row_reconstruction", "row_header_separators"] if reconstructed else []))),
                evidence_ids=list(dict.fromkeys(entry.evidence_id for entry in originals)),
                source_locations=list(dict.fromkeys(entry.source_locator for entry in originals)),
                cells=[part.cell for part in matched if part.cell],
                normalized_sha256=hashlib.sha256(" ".join(target).encode()).hexdigest(),
            )
    return None


_QUOTE_FILLERS = frozenset({"a", "an", "the", "is", "are", "of", "for", "with"})
_QUOTE_QUALIFIERS = frozenset({
    "not", "no", "without", "never", "except", "only", "or", "and", "all",
    "maximum", "max", "minimum", "min", "working", "inlet", "outlet",
})
_COMPONENT_ROLES = frozenset({"body", "stem", "seat", "seal", "ring", "ball", "pin", "washer", "cap"})
_QUOTE_UNITS = frozenset({"in", "inch", "inches", '"', "'", "mm", "cm", "m", "ft", "psi", "bar", "kpa", "mpa"})


def _bound_tokens(tokens: list[str]) -> Counter[tuple[str, ...]]:
    bindings: Counter[tuple[str, ...]] = Counter()
    for index, token in enumerate(tokens):
        if token in {"not", "no", "without", "never", "except", "only", "all"}:
            bindings[(token, *tokens[index + 1:index + 2])] += 1
        elif token in {"and", "or"}:
            bindings[tuple(tokens[max(0, index - 1):index + 2])] += 1
        elif token in _QUOTE_UNITS and index:
            bindings[(tokens[index - 1], token)] += 1
    return bindings


def _quote_scopes(evidence: list[Evidence], cited: set[str]) -> list[list[Fragment]]:
    scopes = []
    for item in pdf_items(evidence):
        if item.context_only or not {entry.evidence_id for entry in item.originals} <= cited:
            continue
        if item.component_materials:
            by_id = {entry.evidence_id: entry for entry in item.values}
            for material_id, component in item.component_materials:
                scopes.append([Fragment(
                    item.anchor, "DESCRIPTION " + component + " MATERIAL " + by_id[material_id].text,
                    originals=item.originals,
                )])
        elif item.kind == "table_row":
            for field in item.text.split(": ", 1)[1].split(" | "):
                scopes.append([Fragment(item.anchor, field.replace("=", " "), originals=item.originals)])
        else:
            scopes.append([Fragment(item.anchor, item.text, originals=item.originals)])
    for entry in evidence:
        if entry.content_kind != "source_excerpt" or entry.evidence_id not in cited:
            continue
        if entry.source_tier == "vendor_table":
            scopes.append(fragments(entry))
        elif entry.source_tier != "internal_pdf":
            scopes.append([Fragment(entry, entry.text)])
    return scopes


def _contained_quote(text: str, part: Fragment) -> tuple[list[str], list[str]] | None:
    target, target_rules = normalized(text, vendor=part.vendor)
    source, source_rules = normalized(part.text, vendor=part.vendor)
    target = [token for token in target if token not in _QUOTE_FILLERS]
    source = [token for token in source if token not in _QUOTE_FILLERS]
    if len(target) < 3:
        return None
    requested, available = Counter(target), Counter(source)
    overlap = sum((requested & available).values()) / len(target)
    if overlap < 0.95 or requested - available:
        return None
    if Counter(token for token in target if token in _QUOTE_QUALIFIERS) != Counter(
        token for token in source if token in _QUOTE_QUALIFIERS
    ):
        return None
    if _bound_tokens(target) - _bound_tokens(source):
        return None
    # A token bag cannot bind two roles or rating limits to their respective values.
    for roles in ({"inlet", "outlet"}, {"minimum", "min", "maximum", "max", "working"}, _COMPONENT_ROLES):
        source_roles = set(source) & roles
        if len(source_roles) > 1 or set(target) & roles != source_roles:
            return None
    return target, sorted(set(target_rules + source_rules + [
        "order_tolerant_multiset", "closed_filler_words", "overlap_screen_0.95",
        "no_unsupported_content_tokens",
    ]))


def match_quote(text: str, evidence: list[Evidence], cited: set[str]) -> EvidenceMatch | None:
    """Keep literal matching; constrain syntactic paraphrases to a single logical scope."""
    exact = match_text(text, reconstructed_rows(evidence, cited) + windows(evidence, cited))
    if exact is not None:
        return exact
    clauses = [clause.strip() for clause in text.split(";") if clause.strip()]
    if not clauses:
        return None
    for scope in _quote_scopes(evidence, cited):
        matches: list[tuple[Fragment, list[str], list[str]]] = []
        for clause in clauses:
            found = next(
                ((part, *result) for part in scope if (result := _contained_quote(clause, part)) is not None),
                None,
            )
            if found is None:
                break
            matches.append(found)
        if len(matches) != len(clauses):
            continue
        originals = [
            entry for part, _, _ in matches for entry in (part.originals or (part.evidence,))
        ]
        tokens = [token for _, target, _ in matches for token in target]
        rules = [rule for _, _, applied in matches for rule in applied]
        vendor = all(part.vendor for part, _, _ in matches)
        return EvidenceMatch(
            method="vendor_cells" if vendor else "normalized",
            normalization=sorted(set(rules + (["clause_to_cell"] if vendor else []))),
            evidence_ids=list(dict.fromkeys(entry.evidence_id for entry in originals)),
            source_locations=list(dict.fromkeys(entry.source_locator for entry in originals)),
            cells=list(dict.fromkeys(part.cell for part, _, _ in matches if part.cell)),
            normalized_sha256=hashlib.sha256(" ".join(tokens).encode()).hexdigest(),
        )
    return None
