"""Native original-page GET serialization without DNS, sockets or TLS traffic."""

from email.message import Message
import hashlib
import http.client
import json
import socket
import ssl
import subprocess
import sys

import pytest

from backend.core import websearch
from backend.sdk_preflight import SDKPreflightError


HOST = "vendor.example"
URL = f"https://{HOST}/product%2Fspecification"
PRIVATE = "DO-NOT-EXPOSE-THIS-PATH-OR-HEADER"
PUBLIC_ADDRESS = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("DNS, sockets, TLS, and subprocesses are forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", denied)
    monkeypatch.setattr(ssl.SSLSocket, "do_handshake", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)


def preflight(url=URL, **kwargs):
    return websearch.preflight_original_page(
        url, **{"allowed_hosts": [HOST], "authorized": True, **kwargs},
    )


def test_genuine_pinned_constructor_and_native_http_serialization(monkeypatch):
    native_request = http.client.HTTPConnection.request
    observed = []

    def observe(connection, method, url, *args, **kwargs):
        assert type(connection) is websearch._PinnedHTTPSConnection
        assert connection.host == HOST
        assert connection.port == 443
        assert connection.timeout == 15
        assert connection._context.check_hostname is True
        assert connection._context.verify_mode == ssl.CERT_REQUIRED
        observed.append((method, url, kwargs))
        return native_request(connection, method, url, *args, **kwargs)

    monkeypatch.setattr(http.client.HTTPConnection, "request", observe)
    receipt = preflight()
    expected_wire = (
        "GET /product%2Fspecification HTTP/1.1\r\n"
        "Host: vendor.example\r\n"
        "Accept: text/plain, text/html\r\n"
        "Accept-Encoding: identity\r\n"
        "User-Agent: DocIntel-Authorized-Evidence/1.0\r\n\r\n"
    ).encode()
    assert len(observed) == 1
    assert observed[0][:2] == ("GET", "/product%2Fspecification")
    assert receipt["status"] == "validated_no_send" and receipt["provider"] == "web_retrieval"
    assert receipt["sdk_package"] == "http.client"
    assert receipt["sdk_version"] == ".".join(map(str, sys.version_info[:3]))
    assert receipt["transport_version"] == ssl.OPENSSL_VERSION
    assert receipt["wire_request_sha256"] == hashlib.sha256(expected_wire).hexdigest()
    assert receipt["wire_request_bytes"] == len(expected_wire)
    assert receipt["request_sha256"] == hashlib.sha256(b"GET\n" + URL.encode() + b"\n").hexdigest()
    assert receipt["body_bytes"] == 0
    assert receipt["header_names"] == ["accept", "accept-encoding", "host", "user-agent"]
    assert receipt["captured_requests"] == 1
    for name in (
        "network_calls", "transport_real_calls", "dns_calls", "socket_connect_calls",
        "tls_handshakes", "credential_real_calls",
    ):
        assert receipt[name] == 0
    for name in (
        "provider_send_performed", "authentication_performed", "provider_response_fabricated",
        "dns_address_safety_verified", "source_content_verified",
    ):
        assert receipt[name] is False
    assert URL not in json.dumps(receipt)


def test_actual_target_host_filter_and_limits_are_bound_without_disclosure():
    first = preflight(f"https://{HOST}/{PRIVATE}", timeout=3, max_bytes=32)
    repeated = preflight(f"https://{HOST}/{PRIVATE}", timeout=3, max_bytes=32)
    changed = preflight(f"https://{HOST}/changed", timeout=3, max_bytes=32)
    broader_filter = preflight(
        f"https://{HOST}/{PRIVATE}", allowed_hosts=[HOST, "other.example"],
        timeout=3, max_bytes=32,
    )
    assert first == repeated
    assert first["wire_request_sha256"] != changed["wire_request_sha256"]
    assert first["endpoint_sha256"] != changed["endpoint_sha256"]
    assert first["allowed_hosts_sha256"] != broader_filter["allowed_hosts_sha256"]
    assert first["timeout_seconds"] == 3 and first["response_max_bytes"] == 32
    assert PRIVATE not in json.dumps(first)


@pytest.mark.parametrize("url", [
    "http://vendor.example/product", "https://other.example/product",
    "https://vendor.example/product?token=private", "https://vendor.example/product#fragment",
    "https://user:password@vendor.example/product", "https://vendor.example:444/product",
    "https://vendor.example/%0d%0aInjected", "https://127.0.0.1/",
])
def test_discovery_url_rejection_is_data_policy_not_sdk_failure(url):
    with pytest.raises(websearch.ExternalEvidenceError) as caught:
        preflight(url)
    assert caught.value.code == "unsafe_url"
    assert not isinstance(caught.value, SDKPreflightError)


@pytest.mark.parametrize("kwargs,code", [
    ({"authorized": False}, "authorization_required"),
    ({"allowed_hosts": ["*.example"]}, "invalid_allowed_hosts"),
    ({"timeout": 16}, "invalid_limits"),
    ({"timeout": float("nan")}, "invalid_limits"),
    ({"max_bytes": True}, "invalid_limits"),
])
def test_existing_policy_and_bounds_remain_first(kwargs, code):
    with pytest.raises(websearch.ExternalEvidenceError) as caught:
        preflight(**kwargs)
    assert caught.value.code == code


def test_actual_native_header_serialization_error_is_fatal_and_sanitized(monkeypatch):
    build = websearch._original_page_request

    def bad_headers(*args, **kwargs):
        result = build(*args, **kwargs)
        result[3]["headers"]["Accept"] = "text/plain\n" + PRIVATE
        return result

    monkeypatch.setattr(websearch, "_original_page_request", bad_headers)
    with pytest.raises(SDKPreflightError) as caught:
        preflight()
    assert caught.value.provider == "web_retrieval"
    assert caught.value.error_type == "ValueError"
    assert PRIVATE not in str(caught.value)


def test_native_failure_is_not_downgraded_to_fetch_transport_error(monkeypatch):
    send = websearch._send_original_page_request

    def incompatible_request(connection, arguments):
        return connection.request(**{**arguments, "unknown_native_parameter": PRIVATE})

    monkeypatch.setattr(websearch, "_send_original_page_request", incompatible_request)
    with pytest.raises(SDKPreflightError) as caught:
        websearch.fetch_original_page(URL, allowed_hosts=[HOST], authorized=True)
    assert caught.value.error_type == "TypeError"
    assert PRIVATE not in str(caught.value)
    monkeypatch.setattr(websearch, "_send_original_page_request", send)


def test_fetch_uses_identical_native_request_after_no_send_and_before_response(monkeypatch):
    """Only the second transport/response is an explicit synthetic test fixture."""
    proofs, wires = [], []
    native_preflight = websearch._preflight_original_page_request

    def record(*args, **kwargs):
        proof = native_preflight(*args, **kwargs)
        proofs.append(proof)
        return proof

    def addresses(host):
        assert len(proofs) == 1
        assert host == HOST
        return [PUBLIC_ADDRESS]

    def send(connection, data):
        assert len(proofs) == 1
        wires.append(data)

    class SyntheticResponse:
        status = 200
        headers = Message()
        headers["Content-Type"] = "text/plain; charset=utf-8"

        def getheader(self, name, default=None):
            return self.headers.get(name, default)

        def read(self, maximum):
            return b"SYNTHETIC TEST RESPONSE, NOT A PRODUCT FINDING"

        def close(self):
            pass

    monkeypatch.setattr(websearch, "_preflight_original_page_request", record)
    monkeypatch.setattr(websearch, "_public_addresses", addresses)
    monkeypatch.setattr(websearch._PinnedHTTPSConnection, "send", send)
    monkeypatch.setattr(websearch._PinnedHTTPSConnection, "getresponse", lambda self: SyntheticResponse())
    evidence = websearch.fetch_original_page(URL, allowed_hosts=[HOST], authorized=True)
    assert evidence.text == "SYNTHETIC TEST RESPONSE, NOT A PRODUCT FINDING"
    assert len(wires) == 1
    assert proofs[0]["wire_request_sha256"] == hashlib.sha256(wires[0]).hexdigest()
