"""Offline REST diagnostic tests using only synthetic data and mocked HTTP."""

import importlib.util
import json
from pathlib import Path
import socket
from unittest.mock import MagicMock, Mock

import httpx
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "webiq_diagnostic.py"
SECRET = "synthetic-secret-not-a-real-key"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setenv("WEBIQ_API_KEY", SECRET)


@pytest.fixture
def diagnostic():
    spec = importlib.util.spec_from_file_location("webiq_diagnostic", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def http(monkeypatch):
    client = Mock()
    factory = MagicMock()
    factory.return_value.__enter__.return_value = client
    monkeypatch.setattr(httpx, "Client", factory)
    return factory, client


def result(**changes):
    return {"title": "Synthetic public product", "url": "https://example.test/product", "content": "Untrusted source passage.", **changes}


def execute(diagnostic, http, capsys, payload=None, status=200):
    factory, client = http
    client.post.return_value = httpx.Response(status, content=json.dumps(payload), headers={"content-type": "application/json"})
    exit_code = diagnostic.main(["--live", "--query", "synthetic public product"])
    summary = json.loads(capsys.readouterr().out)
    client.post.assert_called_once()
    return exit_code, summary


def test_valid_results(diagnostic, http, capsys):
    code, summary = execute(diagnostic, http, capsys, {"webResults": [result()]})
    assert code == 0
    assert summary["status"] == "success_results"
    item = summary["results"][0]
    assert item["title"] == "Synthetic public product"
    assert item["grounding_content_characters"] == len("Untrusted source passage.")
    assert item["grounding_content_present"] is True
    assert "crawledAt" not in item and "lastUpdatedAt" not in item
    assert "Untrusted source passage." not in json.dumps(summary)
    assert summary["observed_at"]
    factory, client = http
    assert factory.call_args.kwargs["follow_redirects"] is False
    assert factory.call_args.kwargs["trust_env"] is False
    assert factory.call_args.kwargs["timeout"].read == 15
    assert client.post.call_args.args == (diagnostic.ENDPOINT,)
    assert client.post.call_args.kwargs["headers"] == {"x-apikey": SECRET}
    assert client.post.call_args.kwargs["json"] == {
        "query": "synthetic public product", "maxResults": 3, "contentFormat": "passage", "maxLength": 2000,
    }


def test_optional_timestamps_and_redirect_metadata(diagnostic, http, capsys):
    payload = {
        "webResults": [result(crawledAt="2026-01-01T00:00:00Z", lastUpdatedAt="", clickUrl="https://redirect.test/click?tracking=123", instrumentationSuffix="private-tracking")],
        "instrumentationClickBase": "https://tracking.test/ping?key=123",
    }
    code, summary = execute(diagnostic, http, capsys, payload)
    assert code == 0
    item = summary["results"][0]
    assert item["crawledAt"] == "2026-01-01T00:00:00Z"
    assert "lastUpdatedAt" not in item
    assert item["redirect_url"]["original_source_verified"] is False
    assert item["redirect_url"]["query_or_fragment_omitted"] is True
    assert summary["instrumentationClickBase"]["url"] == "https://tracking.test/ping"
    assert "private-tracking" not in json.dumps(summary)


def test_empty_results(diagnostic, http, capsys):
    code, summary = execute(diagnostic, http, capsys, {"webResults": []})
    assert code == 0 and summary["status"] == "success_zero_results"


@pytest.mark.parametrize("payload", [None, [], {}, {"results": []}, {"webResults": None},
    {"webResults": [result(content=None)]}, {"webResults": [result(url="javascript:run()")]},
    {"webResults": [result(title=3)]}, {"webResults": [result(crawledAt="invalid")]},
    {"webResults": [result(lastUpdatedAt=123)]}, {"webResults": [result()] * 4}])
def test_malformed_payload(diagnostic, http, capsys, payload):
    code, summary = execute(diagnostic, http, capsys, payload)
    assert code == 2 and summary["status"] == "malformed_response"
    assert "results" not in summary


@pytest.mark.parametrize(("status", "expected"), [(401, "authentication_failed"), (403, "permission_denied"), (429, "throttled"), (500, "http_error"), (504, "timeout")])
def test_http_failures(diagnostic, http, capsys, status, expected):
    code, summary = execute(diagnostic, http, capsys, {"technicalDetails": SECRET}, status)
    assert code == 2 and summary["status"] == expected
    assert SECRET not in json.dumps(summary)


@pytest.mark.parametrize("error", [httpx.ReadTimeout(SECRET), httpx.ConnectError(SECRET), RuntimeError(SECRET)])
def test_exception_details_are_suppressed(diagnostic, http, capsys, error):
    http[1].post.side_effect = error
    assert diagnostic.main(["--live", "--query", "public terms"]) == 2
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert json.loads(output.out)["status"] in ("timeout", "transport_error", "diagnostic_error")
    http[1].post.assert_called_once()


@pytest.mark.parametrize("arguments", [[], ["--query", "public terms"], ["--live"], ["--live", "--query", " "]])
def test_no_network_without_explicit_gate(diagnostic, http, capsys, arguments):
    assert diagnostic.main(arguments) == 2
    http[0].assert_not_called()
    assert json.loads(capsys.readouterr().out)["status"] in ("live_opt_in_required", "invalid_query")


def test_missing_key(diagnostic, http, capsys, monkeypatch):
    monkeypatch.delenv("WEBIQ_API_KEY")
    assert diagnostic.main(["--live", "--query", "public terms"]) == 2
    http[0].assert_not_called()
    assert json.loads(capsys.readouterr().out)["status"] == "missing_key"


def test_import_does_not_create_http_client(diagnostic, http):
    spec = importlib.util.spec_from_file_location("webiq_diagnostic_import_check", SCRIPT)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    http[0].assert_not_called()


def test_reflected_secret_is_redacted(diagnostic, http, capsys):
    code, summary = execute(diagnostic, http, capsys, {"webResults": [result(title=SECRET, url=f"https://example.test/{SECRET}?key={SECRET}", content=SECRET)]})
    assert code == 0 and SECRET not in json.dumps(summary)


def test_invalid_arguments_do_not_echo_values(diagnostic, capsys):
    with pytest.raises(SystemExit):
        diagnostic.main(["--invalid", SECRET])
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err


def test_redirect_is_not_followed(diagnostic, http, capsys):
    http[1].post.return_value = httpx.Response(302, headers={"location": "https://redirect.test/path?token=hidden"})
    assert diagnostic.main(["--live", "--query", "public terms"]) == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "http_redirect_not_followed"
    assert summary["redirect_url"]["original_source_verified"] is False
    http[1].post.assert_called_once()


def test_non_json_response(diagnostic, http, capsys):
    http[1].post.return_value = httpx.Response(200, text=SECRET)
    assert diagnostic.main(["--live", "--query", "public terms"]) == 2
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert json.loads(output.out)["status"] == "malformed_response"


def test_invalid_json_response(diagnostic, http, capsys):
    http[1].post.return_value = httpx.Response(200, content=b"not json", headers={"content-type": "application/json"})
    assert diagnostic.main(["--live", "--query", "public terms"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "malformed_response"


def test_actual_httpx_request_uses_mock_transport_once(diagnostic, monkeypatch, capsys):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["x-apikey"] == SECRET
        assert request.headers["content-type"] == "application/json"
        assert request.method == "POST"
        assert str(request.url) == diagnostic.ENDPOINT
        return httpx.Response(200, json={"webResults": [result(content="", crawledAt=None)]})

    transport = Mock(return_value=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx, "HTTPTransport", transport)
    assert diagnostic.main(["--live", "--query", "public terms"]) == 0
    transport.assert_called_once_with(retries=0)
    assert len(requests) == 1
    item = json.loads(capsys.readouterr().out)["results"][0]
    assert item["grounding_content_present"] is False
    assert item["grounding_content_characters"] == 0
    assert "crawledAt" not in item


def test_default_path_without_http_mocks(diagnostic, capsys):
    assert diagnostic.main([]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "live_opt_in_required"


@pytest.mark.parametrize(("payload", "reason", "path", "expected", "actual"), [
    (None, "unexpected_top_level_type", "$", "object", "null"),
    ([], "unexpected_top_level_type", "$", "object", "array"),
    (SECRET, "unexpected_top_level_type", "$", "object", "string"),
    (True, "unexpected_top_level_type", "$", "object", "boolean"),
    (42, "unexpected_top_level_type", "$", "object", "number"),
    ({}, "missing_web_results", "$.webResults", "array", "missing"),
    ({"results": []}, "missing_web_results", "$.webResults", "array", "missing"),
    ({"webResults": None}, "unexpected_web_results_type", "$.webResults", "array", "null"),
    ({"webResults": {}}, "unexpected_web_results_type", "$.webResults", "array", "object"),
    ({"webResults": SECRET}, "unexpected_web_results_type", "$.webResults", "array", "string"),
    ({"webResults": [None]}, "invalid_result_item", "$.webResults[0]", "object", "null"),
    ({"webResults": [SECRET]}, "invalid_result_item", "$.webResults[0]", "object", "string"),
    ({"webResults": [result(title=42)]}, "invalid_field", "$.webResults[0].title", "string", "number"),
    ({"webResults": [result(content={})]}, "invalid_field", "$.webResults[0].content", "string", "object"),
    ({"webResults": [result()] * 4}, "result_limit_exceeded", "$.webResults", "array", "array"),
    ({"errorCode": SECRET}, "provider_error_envelope", "$.errorCode", "missing", "string"),
])
def test_structural_failure_details(diagnostic, http, capsys, payload, reason, path, expected, actual):
    code, summary = execute(diagnostic, http, capsys, payload)
    assert code == 2 and summary["status"] == "malformed_response"
    assert summary["http_status"] == 200
    details = summary["diagnostics"]
    assert details["content_type"] == "application/json"
    assert details["json_decoded"] is True
    assert details["reason"] == reason
    assert details["field_path"] == path
    assert details["expected_type"] == expected
    assert details["actual_type"] == actual
    assert SECRET not in json.dumps(summary)
    if isinstance(payload, dict):
        assert details["web_results_present"] == ("webResults" in payload)
        assert details["web_results_type"] == diagnostic.json_type(payload.get("webResults", diagnostic.MISSING))
        if isinstance(payload.get("webResults"), list):
            assert details["web_results_count"] == len(payload["webResults"])


@pytest.mark.parametrize("field", ["title", "url", "content"])
@pytest.mark.parametrize("state", ["missing", "null"])
def test_required_fields_remain_required(diagnostic, http, capsys, field, state):
    item = result()
    if state == "missing":
        del item[field]
    else:
        item[field] = None
    code, summary = execute(diagnostic, http, capsys, {"webResults": [item]})
    assert code == 2
    details = summary["diagnostics"]
    assert details["reason"] == "invalid_field"
    assert details["field_path"] == f"$.webResults[0].{field}"
    assert details["expected_type"] == "string"
    assert details["actual_type"] == state


@pytest.mark.parametrize("field", ["crawledAt", "lastUpdatedAt"])
@pytest.mark.parametrize("value", [None, "", "2026-09-29T12:00:00Z"])
def test_optional_date_coverage(diagnostic, http, capsys, field, value):
    code, summary = execute(diagnostic, http, capsys, {"webResults": [result(**{field: value})]})
    assert code == 0
    assert (field in summary["results"][0]) == bool(value)


@pytest.mark.parametrize(("field", "value", "expected", "actual", "constraint"), [
    ("url", "javascript:" + SECRET, "string", "string", "http_url"),
    ("url", "https://user:password@example.test", "string", "string", "http_url"),
    ("url", "https://example.test:invalid", "string", "string", "http_url"),
    ("url", "https://[invalid", "string", "string", "http_url"),
    ("url", "https://example.test/\n", "string", "string", "http_url"),
    ("url", "", "string", "string", "http_url"),
    ("crawledAt", SECRET, "string or null", "string", "iso8601_or_empty"),
    ("lastUpdatedAt", 123, "string or null", "number", "iso8601_or_empty"),
    ("clickUrl", [], "string", "array", "http_url"),
    ("instrumentationSuffix", {}, "string or null", "object", None),
])
def test_invalid_consumed_fields(diagnostic, http, capsys, field, value, expected, actual, constraint):
    code, summary = execute(diagnostic, http, capsys, {"webResults": [result(**{field: value})]})
    assert code == 2
    details = summary["diagnostics"]
    assert details["field_path"] == f"$.webResults[0].{field}"
    assert details["expected_type"] == expected
    assert details["actual_type"] == actual
    assert details.get("constraint") == constraint
    assert SECRET not in json.dumps(summary)


@pytest.mark.parametrize("field", ["instrumentationClickBase", "instrumentationCitationBase"])
def test_invalid_instrumentation_base(diagnostic, http, capsys, field):
    code, summary = execute(diagnostic, http, capsys, {"webResults": [], field: {SECRET: SECRET}})
    assert code == 2
    assert summary["diagnostics"]["field_path"] == f"$.{field}"
    assert summary["diagnostics"]["actual_type"] == "object"
    assert SECRET not in json.dumps(summary)


def test_absent_and_null_optional_fields(diagnostic, http, capsys):
    payload = {
        "webResults": [result(crawledAt=None, lastUpdatedAt=None, clickUrl=None, instrumentationSuffix=None)],
        "instrumentationClickBase": None, "instrumentationCitationBase": None,
    }
    code, summary = execute(diagnostic, http, capsys, payload)
    assert code == 0
    assert "redirect_url" not in summary["results"][0]
    assert "instrumentation_suffix_present" not in summary["results"][0]
    assert "instrumentationClickBase" not in summary
    assert "instrumentationCitationBase" not in summary


@pytest.mark.parametrize(("content_type", "normalized"), [
    ("Application/JSON; charset=utf-8", "application/json"),
    ("text/html; charset=utf-8", "text/html"),
    ("text/plain", "text/plain"),
    ("", "missing"),
    ("application/" + SECRET, "unrecognized_not_displayed"),
])
def test_non_json_structural_details(diagnostic, http, capsys, content_type, normalized):
    http[1].post.return_value = httpx.Response(200, content=SECRET, headers={"content-type": content_type})
    assert diagnostic.main(["--live", "--query", "public terms"]) == 2
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    summary = json.loads(output.out)
    assert summary["diagnostics"] == {
        "content_type": normalized, "json_decoded": False, "reason": "non_json_response",
        "field_path": "$", "expected_type": "JSON value", "actual_type": "unavailable",
    }
    http[1].post.assert_called_once()


def test_json_body_does_not_override_wrong_content_type(diagnostic, http, capsys):
    http[1].post.return_value = httpx.Response(200, content='{"webResults": []}', headers={"content-type": "text/plain"})
    assert diagnostic.main(["--live", "--query", "public terms"]) == 2
    details = json.loads(capsys.readouterr().out)["diagnostics"]
    assert details["reason"] == "unexpected_content_type"
    assert details["json_decoded"] is True
    assert details["top_level_type"] == "object"
    assert details["web_results_count"] == 0


def test_failure_exposes_only_allowlisted_names_and_no_values(diagnostic, http, capsys):
    private = "synthetic-private-content"
    payload = {
        "webResults": [result(title=private, url=f"https://example.test/{private}", content=private), {"content": private}],
        "traceId": private, "querySignals": {private: private}, SECRET: SECRET, private: private,
    }
    code, summary = execute(diagnostic, http, capsys, payload)
    assert code == 2
    details = summary["diagnostics"]
    assert details["schema_fields"] == ["querySignals", "traceId", "webResults"]
    assert details["other_fields_present"] is True
    assert details["field_path"] == "$.webResults[1].title"
    assert "results" not in summary
    assert private not in json.dumps(summary)
    assert SECRET not in json.dumps(summary)


@pytest.mark.parametrize("error_type", [ValueError, TypeError, RuntimeError, KeyError])
def test_processing_exception_is_not_a_schema_failure(diagnostic, http, capsys, monkeypatch, error_type):
    monkeypatch.setattr(diagnostic, "summarize", Mock(side_effect=error_type(SECRET)))
    code, summary = execute(diagnostic, http, capsys, {"webResults": []})
    assert code == 2 and summary["status"] == "diagnostic_error"
    assert summary["http_status"] == 200
    assert summary["diagnostics"]["reason"] == "response_processing_error"
    assert summary["diagnostics"]["json_decoded"] is True
    assert SECRET not in json.dumps(summary)


def test_unexpected_decoder_exception_is_processing_failure(diagnostic, http, capsys):
    response = Mock(status_code=200, headers={"content-type": "application/json"})
    response.json.side_effect = RuntimeError(SECRET)
    http[1].post.return_value = response
    assert diagnostic.main(["--live", "--query", "public terms"]) == 2
    output = capsys.readouterr()
    summary = json.loads(output.out)
    assert summary["status"] == "diagnostic_error"
    assert summary["diagnostics"]["reason"] == "response_processing_error"
    assert summary["diagnostics"]["json_decoded"] is None
    assert SECRET not in output.out + output.err
