"""Opt-in, one-request WebIQ REST diagnostic. No application imports or persistence."""

import argparse
from datetime import datetime, timezone
import json
import os
from urllib.parse import quote, urlsplit, urlunsplit


ENDPOINT = "https://api.microsoft.ai/v3/search/web"
TIMEOUT_SECONDS = 15.0
MISSING = object()
RESPONSE_FIELDS = frozenset({
    "webResults", "querySignals", "instrumentationClickBase",
    "instrumentationCitationBase", "traceId", "errorCode",
})


def json_type(value: object) -> str:
    if value is MISSING:
        return "missing"
    if value is None:
        return "null"
    return {dict: "object", list: "array", str: "string", bool: "boolean",
            int: "number", float: "number"}.get(type(value), "unknown")


class InvalidResponse(ValueError):
    """The response does not match the consumed documented fields."""

    def __init__(self, reason: str, path: str, expected: str, actual: object, constraint: str | None = None):
        super().__init__(reason)
        self.details = {
            "reason": reason, "field_path": path,
            "expected_type": expected, "actual_type": json_type(actual),
        }
        if constraint is not None:
            self.details["constraint"] = constraint


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid arguments. Use --help; argument values are not logged.\n")


def safe_text(value: str, secret: str) -> str:
    for sensitive in (secret, quote(secret, safe="")):
        if sensitive:
            value = value.replace(sensitive, "[redacted]")
    return value[:500]


def url_summary(value: object, secret: str, path: str = "$.url") -> dict:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise InvalidResponse("invalid_field", path, "string", value, "http_url")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        parsed.port
    except ValueError:
        raise InvalidResponse("invalid_field", path, "string", value, "http_url") from None
    return {
        "url": safe_text(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), secret),
        "query_or_fragment_omitted": bool(parsed.query or parsed.fragment),
        "original_source_verified": False,
    }


def optional_timestamp(result: dict, field: str, path: str) -> str | None:
    value = result.get(field)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise InvalidResponse("invalid_field", path, "string or null", value, "iso8601_or_empty")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidResponse("invalid_field", path, "string or null", value, "iso8601_or_empty") from None
    return value


def summarize(payload: object, secret: str) -> dict:
    if not isinstance(payload, dict):
        raise InvalidResponse("unexpected_top_level_type", "$", "object", payload)
    if "errorCode" in payload:
        raise InvalidResponse("provider_error_envelope", "$.errorCode", "missing", payload["errorCode"])
    results = payload.get("webResults", MISSING)
    if results is MISSING:
        raise InvalidResponse("missing_web_results", "$.webResults", "array", results)
    if not isinstance(results, list):
        raise InvalidResponse("unexpected_web_results_type", "$.webResults", "array", results)
    if len(results) > 3:
        raise InvalidResponse("result_limit_exceeded", "$.webResults", "array", results, "at_most_3_items")
    summaries = []
    for index, result in enumerate(results):
        path = f"$.webResults[{index}]"
        if not isinstance(result, dict):
            raise InvalidResponse("invalid_result_item", path, "object", result)
        for field in ("title", "content"):
            value = result.get(field, MISSING)
            if not isinstance(value, str):
                raise InvalidResponse("invalid_field", f"{path}.{field}", "string", value)
        content = result["content"]
        entry = {
            "title": safe_text(result["title"], secret),
            "returned_url": url_summary(result.get("url", MISSING), secret, f"{path}.url"),
            "grounding_content_present": bool(content.strip()),
            "grounding_content_characters": len(content),
            "content_kind": "provider_returned_passage_unverified",
        }
        for field in ("crawledAt", "lastUpdatedAt"):
            timestamp = optional_timestamp(result, field, f"{path}.{field}")
            if timestamp is not None:
                entry[field] = safe_text(timestamp, secret)
        if result.get("clickUrl") is not None:
            entry["redirect_url"] = url_summary(result["clickUrl"], secret, f"{path}.clickUrl")
            entry["returned_url_equals_redirect"] = result.get("url") == result["clickUrl"]
        if result.get("instrumentationSuffix") is not None:
            if not isinstance(result["instrumentationSuffix"], str):
                raise InvalidResponse("invalid_field", f"{path}.instrumentationSuffix", "string or null", result["instrumentationSuffix"])
            entry["instrumentation_suffix_present"] = bool(result["instrumentationSuffix"])
        summaries.append(entry)
    summary = {
        "status": "success_results" if results else "success_zero_results",
        "result_count": len(results),
        "results": summaries,
        "interpretation": "Untrusted provider data; content presence does not prove attribute support. URLs were not followed or verified.",
    }
    for field in ("instrumentationClickBase", "instrumentationCitationBase"):
        if payload.get(field) is not None:
            summary[field] = url_summary(payload[field], secret, f"$.{field}")
    return summary


def process_response(response, secret: str) -> dict:
    diagnostics = {"json_decoded": None}
    try:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        diagnostics["content_type"] = content_type if content_type in {
            "application/json", "application/problem+json", "text/json",
            "text/plain", "text/html", "application/octet-stream",
        } else ("missing" if not content_type else "unrecognized_not_displayed")
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            diagnostics.update(json_decoded=False, reason="non_json_response",
                               field_path="$", expected_type="JSON value", actual_type="unavailable")
            return {"status": "malformed_response", "diagnostics": diagnostics}
        diagnostics.update(json_decoded=True, top_level_type=json_type(payload))
        if isinstance(payload, dict):
            diagnostics["schema_fields"] = sorted(RESPONSE_FIELDS.intersection(payload))
            diagnostics["other_fields_present"] = any(field not in RESPONSE_FIELDS for field in payload)
            results = payload.get("webResults", MISSING)
            diagnostics["web_results_present"] = results is not MISSING
            diagnostics["web_results_type"] = json_type(results)
            if isinstance(results, list):
                diagnostics["web_results_count"] = len(results)
        if content_type != "application/json":
            diagnostics["reason"] = "unexpected_content_type"
            return {"status": "malformed_response", "diagnostics": diagnostics}
        return summarize(payload, secret)
    except InvalidResponse as error:
        diagnostics.update(error.details)
        return {"status": "malformed_response", "diagnostics": diagnostics}
    except Exception:
        diagnostics["reason"] = "response_processing_error"
        return {"status": "diagnostic_error", "diagnostics": diagnostics}


def lookup(query: str, api_key: str) -> dict:
    import httpx

    try:
        with httpx.Client(
            timeout=httpx.Timeout(TIMEOUT_SECONDS),
            follow_redirects=False,
            trust_env=False,
            transport=httpx.HTTPTransport(retries=0),
        ) as client:
            response = client.post(
                ENDPOINT,
                headers={"x-apikey": api_key},
                json={"query": query, "maxResults": 3, "contentFormat": "passage", "maxLength": 2000},
            )
        code = response.status_code
        statuses = {
            401: "authentication_failed", 403: "permission_denied",
            429: "throttled", 408: "timeout", 504: "timeout",
        }
        if code in statuses:
            return {"status": statuses[code], "http_status": code}
        if 300 <= code < 400:
            summary = {"status": "http_redirect_not_followed", "http_status": code}
            location = response.headers.get("location")
            if location:
                try:
                    summary["redirect_url"] = url_summary(location, api_key)
                except InvalidResponse:
                    summary["redirect_location"] = "relative_or_invalid_not_displayed"
            return summary
        if code != 200:
            return {"status": "http_error", "http_status": code}
        summary = process_response(response, api_key)
        return {**summary, "http_status": code}
    except httpx.TimeoutException:
        return {"status": "timeout"}
    except httpx.RequestError:
        return {"status": "transport_error"}


def main(argv: list[str] | None = None) -> int:
    parser = SafeArgumentParser(description="One WebIQ lookup for caller-authorized PUBLIC search terms only.")
    parser.add_argument("--live", action="store_true", help="Authorize one external request; disabled by default")
    parser.add_argument("--query", help="Explicit public terms only; no customer IDs, documents, or manifests")
    args = parser.parse_args(argv)
    if not args.live:
        summary = {"status": "live_opt_in_required", "message": "No request made. Explicit --live and --query are required."}
    elif not args.query or not args.query.strip() or len(args.query) > 1000 or any(ord(char) < 32 for char in args.query):
        summary = {"status": "invalid_query", "message": "Supply --query with 1-1000 characters of authorized public search terms."}
    else:
        api_key = os.environ.get("WEBIQ_API_KEY", "")
        if not api_key.strip():
            summary = {"status": "missing_key", "message": "WEBIQ_API_KEY is absent from the process environment; no request made."}
        else:
            try:
                summary = lookup(args.query, api_key)
            except ImportError:
                summary = {"status": "dependency_missing", "message": "The installed environment needs httpx; nothing was installed."}
            except Exception:
                summary = {"status": "diagnostic_error", "message": "Diagnostic failed; sensitive exception details suppressed."}
    summary["observed_at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0 if summary["status"] in ("success_results", "success_zero_results") else 2


if __name__ == "__main__":
    raise SystemExit(main())
