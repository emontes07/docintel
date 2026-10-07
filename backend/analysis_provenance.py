"""Bounded layout analysis with metadata-only provenance, using the existing mapper."""

import base64
import hashlib
import json
import uuid
from datetime import datetime, timezone
from importlib.metadata import version
from io import BytesIO

from backend.core.docintel import DocumentIntelligenceError, DocumentIntelligenceService, LAYOUT_MODEL_ID


API_VERSION = "2024-11-30"
REQUEST_OPTIONS = {"pages": "1-5"}
COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


def stamp():
    return datetime.now(timezone.utc).isoformat()


def make_preparation_client(*, endpoint, credential, transport=None):
    from azure.ai.documentintelligence import DocumentIntelligenceClient

    options = {"transport": transport} if transport is not None else {}
    return DocumentIntelligenceClient(
        endpoint=endpoint.rstrip("/"), credential=credential, api_version=API_VERSION,
        retry_total=0, retry_connect=0, retry_read=0, retry_status=0,
        connection_timeout=10, read_timeout=30, **options,
    )


def preparation_analyze_kwargs(source_bytes):
    if not isinstance(source_bytes, bytes):
        raise ValueError("Preparation request requires PDF bytes")
    return {"model_id": LAYOUT_MODEL_ID, "body": BytesIO(source_bytes), **REQUEST_OPTIONS}


def preflight_preparation(source_bytes, *, endpoint):
    from backend.sdk_preflight import preflight_document_intelligence

    return preflight_document_intelligence(
        source_bytes=source_bytes,
        client_factory=lambda *, credential, transport: make_preparation_client(
            endpoint=endpoint, credential=credential, transport=transport,
        ),
        analyze_kwargs=preparation_analyze_kwargs(source_bytes),
    )


class VerifiedOperatorCredential:
    """Check only allowlisted identity claims in memory; never export the token."""

    def __init__(self, credential, expected_identity):
        self._credential = credential
        self.expected_identity = dict(expected_identity)
        self.verified_identity = None

    def get_token(self, *scopes, **kwargs):
        if scopes != (COGNITIVE_SCOPE,):
            raise ValueError("Operator credential is restricted to Document Intelligence")
        token = self._credential.get_token(*scopes, **kwargs)
        try:
            parts = token.token.split(".")
            if len(parts) != 3 or len(token.token) > 65536:
                raise ValueError("Invalid token shape")
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            if (
                claims.get("oid") != self.expected_identity["principal_id"]
                or claims.get("tid") != self.expected_identity["tenant_id"]
                or claims.get("aud") not in ("https://cognitiveservices.azure.com", "https://cognitiveservices.azure.com/")
                or token.expires_on <= datetime.now(timezone.utc).timestamp()
            ):
                raise ValueError("Identity mismatch")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValueError("Existing operator credential identity/audience did not match; no fallback") from None
        self.verified_identity = {
            **self.expected_identity, "token_audience": claims["aud"],
            "identity_checked_at": stamp(), "sdk_token_identity_matched": True,
            "token_signature_validated_locally": False, "token_stored": False,
        }
        return token


class RecordedPreparationParser(DocumentIntelligenceService):
    """One submission, zero SDK retries; no credential fallback or source URL."""

    def __init__(self, *, endpoint, credential, submitted):
        super().__init__(endpoint=endpoint, credential=credential)
        self.submitted = submitted
        self.analysis_receipt = None
        self.no_send_receipt = None

    def _analyze(self, source_url, *, page_limit=None):
        if not isinstance(source_url, bytes) or page_limit != 5:
            raise ValueError("Preparation requires PDF bytes and exactly pages 1-5")
        if self.analysis_receipt is not None:
            raise ValueError("This preparation parser already attempted analysis; no retry")
        if not isinstance(self._credential, VerifiedOperatorCredential):
            raise ValueError("Explicit verified operator credential required")
        self.no_send_receipt = preflight_preparation(source_url, endpoint=self.endpoint)

        receipt = {
            "model_requested": LAYOUT_MODEL_ID, "api_version_requested": API_VERSION,
            "request_options": dict(REQUEST_OPTIONS), "sdk_retries": 0,
            "sdk_package": "azure-ai-documentintelligence", "sdk_version": version("azure-ai-documentintelligence"),
            "source_sha256": hashlib.sha256(source_url).hexdigest(), "source_bytes": len(source_url),
            "started_at": stamp(), "operation_id": None, "outcome": "submission_outcome_unknown",
        }
        self.analysis_receipt = receipt
        try:
            with make_preparation_client(endpoint=self.endpoint, credential=self._credential) as client:
                poller = client.begin_analyze_document(**preparation_analyze_kwargs(source_url))
                if self._credential.verified_identity is None:
                    raise ValueError("SDK did not establish the actual operator identity")
                operation_id = str(uuid.UUID(poller.details["operation_id"]))
                receipt.update(
                    operation_id=operation_id, accepted_at=stamp(),
                    analysis_identity=dict(self._credential.verified_identity),
                )
                self.submitted(dict(receipt))
                result = poller.result(timeout=120)
            serialized = json.dumps(
                result.as_dict(), sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"),
            ).encode()
            pages = [page.page_number for page in result.pages]
            receipt.update(
                completed_at=stamp(), model_returned=result.model_id,
                api_version_returned=result.api_version, actual_page_count=len(pages),
                returned_pages=[page if type(page) is int else None for page in pages],
                sdk_result_sha256=hashlib.sha256(serialized).hexdigest(),
                sdk_result_serialization="AnalyzeResult.as_dict; sorted ASCII JSON; compact separators",
                sdk_result_bytes=len(serialized), raw_http_response_retained=False,
            )
            if (
                result.api_version != API_VERSION or result.model_id != LAYOUT_MODEL_ID
                or not 1 <= len(pages) <= 5 or any(type(page) is not int for page in pages)
                or pages != list(range(1, len(pages) + 1))
            ):
                raise ValueError("Service result differs from the bounded request contract")
            receipt["outcome"] = "succeeded"
            return result
        except Exception as error:
            receipt.update(outcome="failed_or_unknown_no_retry", ended_at=stamp(), error_type=type(error).__name__)
            raise DocumentIntelligenceError("Preparation analysis did not complete; no retry authorized") from None
