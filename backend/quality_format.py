"""Deterministic display/scoring form for string attribute values.

Only presentation changes: inch notation, a trailing INLET/OUTLET word already
implied by an inlet/outlet attribute, the MTR abbreviation, and the case of
all-capital text. Supporting quotes are never rewritten. Values exactly equal to
an allowed enumeration value are returned unchanged.
"""

from __future__ import annotations

import re
import unicodedata

FORMAT_POLICY = "display-format-v1"
_OTHER = re.compile(r"^(Other:\s*)", re.I)
_INCH = re.compile(r"(?<=\d)\s*(?:inches|inch|in\b|[\"\u2033\u201d])", re.I)
_TRAILING = {"Inlet": re.compile(r"\s+INLET\s*$", re.I), "Outlet": re.compile(r"\s+OUTLET\s*$", re.I)}
_MTR = re.compile(r"\bMTR\b", re.I)
_TECHNICAL = re.compile(
    r"\b(?:AWWA|ASTM|UNS|NSF|ANSI|EPDM|PTFE|PVC|PEP|CTS|FPT|MPT|FIP|MIP|PSI|NL|IPS|IP|NPT|OD|ID|PE|DI)\b", re.I,
)
# Letter-digit codes (NSF61, C83600, D2000) keep their printed capitals.
_CODE = re.compile(r"\b(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9-]+\b")
_DIMENSION_X = re.compile(r'(?<=[\d"])(\s*)[Xx](?=\s*\d)')


def _sentence_case(text: str) -> str:
    letters = [char for char in text if char.isalpha()]
    if not letters or not all(char.isupper() for char in letters):
        return text
    lowered = text.lower()
    if len(lowered) == len(text):
        keep = [False] * len(text)
        for match in _CODE.finditer(text):
            keep[match.start():match.end()] = [True] * (match.end() - match.start())
        lowered = "".join(original if kept else low for original, low, kept in zip(text, lowered, keep))
    lowered = lowered[:1].upper() + lowered[1:]
    return _TECHNICAL.sub(lambda match: match[0].upper(), lowered)


def canonical_text(attribute_id: str, text: str) -> str:
    body = unicodedata.normalize("NFKC", text).replace("\u2033", '"').replace("\u201d", '"')
    body = _INCH.sub('"', body)
    for prefix, pattern in _TRAILING.items():
        if attribute_id.startswith(prefix + " ") and "Size" not in attribute_id:
            stripped = pattern.sub("", body)
            if stripped.strip():
                body = stripped
    body = _MTR.sub(lambda match: "METER" if match[0].isupper() else "meter", body)
    body = " ".join(body.split())
    return _DIMENSION_X.sub(lambda match: match[1] + "x", _sentence_case(body))


def canonical_value(attribute_id: str, value, allowed_values: list | tuple = ()):
    """Return the canonical display value; non-strings and exact allowed values are unchanged."""
    if not isinstance(value, str) or value in allowed_values:
        return value
    match = _OTHER.match(value)
    if match:
        return "Other: " + canonical_text(attribute_id, value[match.end():])
    return canonical_text(attribute_id, value)


def comparison_key(attribute_id: str, value) -> str:
    """Case-insensitive key used for run-to-run stability scoring."""
    if isinstance(value, bool) or not isinstance(value, str):
        return repr(value)
    return canonical_value(attribute_id, value).casefold()
