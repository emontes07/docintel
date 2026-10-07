"""Explicit optional gapfill runtime policy; never a substitute for live approval.

The normal worker reads this only with a separate, exact server-side opt-in.
The existing real-pilot guard still owns all durable reservations and authority.
"""

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.core.websearch import exact_https_hosts

OPTIONAL_WEB_MAX_INPUT_TOKENS = 26000
OPTIONAL_WEB_ENABLED_ENV = "DOCINTEL_OPTIONAL_WEB_GAPFILL_ENABLED"
OPTIONAL_WEB_POLICY_ENV = "DOCINTEL_OPTIONAL_WEB_GAPFILL_POLICY_JSON"
MAX_POLICY_BYTES = 65536


class PublicWebScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manufacturer: str = Field(min_length=1, max_length=120)
    mpn: str = Field(min_length=1, max_length=120)
    attribute_terms: dict[str, str] = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    allowed_hosts: list[str] = Field(min_length=1)
    max_direct_page_attempts: int = Field(default=2, strict=True, ge=0, le=2)

    @model_validator(mode="after")
    def public_terms(self):
        terms = [self.manufacturer, self.mpn, *self.attribute_terms.values()]
        if any(
            not term.strip() or len(term) > 120
            or any(ord(char) < 32 or ord(char) == 127 for char in term)
            or "://" in term
            for term in terms
        ):
            raise ValueError("Explicit public manufacturer, MPN and attribute terms are required")
        exact_https_hosts(self.allowed_hosts)
        return self

    def query(self, pending: list[str]) -> str:
        """Never read a manifest, document, customer value, or internal identity term."""
        terms = [self.attribute_terms[name] for name in pending if name in self.attribute_terms]
        if not terms:
            raise ValueError("No approved public pending attribute terms")
        query = " ".join(dict.fromkeys([self.manufacturer, self.mpn, *terms]))
        if len(query) > 1000:
            raise ValueError("Complete public query exceeds its bound")
        return query


class OptionalWebPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    items: dict[str, PublicWebScope] = Field(min_length=1, max_length=4)
    max_cost_microdollars: int = Field(strict=True, ge=0)
    max_search_calls: int = Field(default=2, strict=True, ge=0, le=4)
    max_direct_page_attempts: int = Field(default=4, strict=True, ge=0, le=6)
    max_inference_calls: int = Field(default=2, strict=True, ge=0, le=4)
    max_input_tokens: int = Field(
        default=OPTIONAL_WEB_MAX_INPUT_TOKENS, strict=True, ge=1, le=OPTIONAL_WEB_MAX_INPUT_TOKENS,
    )
    max_output_tokens: int = Field(default=2048, strict=True, ge=2048, le=2048)


def configured_optional_web_policy(environ: Mapping[str, str] | None = None) -> OptionalWebPolicy | None:
    """Read a bounded server policy only for exact ``true`` opt-in; perform no I/O."""
    values = os.environ if environ is None else environ
    if values.get(OPTIONAL_WEB_ENABLED_ENV) != "true":
        return None
    raw = values.get(OPTIONAL_WEB_POLICY_ENV, "")
    if not raw or len(raw.encode()) > MAX_POLICY_BYTES:
        raise ValueError("Optional web opt-in requires bounded server policy JSON")
    try:
        return OptionalWebPolicy.model_validate_json(raw)
    except ValueError:
        raise ValueError("Optional web opt-in requires valid server policy JSON") from None
