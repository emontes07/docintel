"""Bind credential-free native request captures to the inspected backend image."""

from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
import hashlib
from importlib.metadata import version
from pathlib import Path, PurePosixPath
import re
import socket
import ssl
import subprocess
import sys
import tomllib
from typing import NoReturn
from unittest.mock import patch

from azure import identity

from backend.sdk_preflight import _openai_http_module


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = (
    "openai", "httpx", "azure-core", "azure-identity",
    "azure-ai-documentintelligence", "azure-storage-blob", "pydantic",
)
REQUIRED_FILES = frozenset({
    "uv.lock", "pyproject.toml", "backend/sdk_image_smoke.py",
    "backend/sdk_preflight.py", "backend/batch_worker.py",
    "backend/core/llm.py", "backend/core/websearch.py",
    "backend/core/websearch_webiq.py", "backend/models/enrichment.py",
})


class ImageSmokeError(RuntimeError):
    """Image compatibility was not established; activation must remain closed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ImageSmokeError(message)


def _provider_packages() -> tuple[str, ...]:
    return tuple(dict.fromkeys((*PACKAGES, _openai_http_module().__name__)))


def _runtime_binding(expected_files: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    _require(REQUIRED_FILES <= expected_files.keys(), "Incomplete reviewed image source manifest")
    actual_files = {}
    for name, expected in expected_files.items():
        path = PurePosixPath(name)
        _require(
            not path.is_absolute() and ".." not in path.parts and str(path) == name
            and (name in {"uv.lock", "pyproject.toml"} or name.startswith("backend/")),
            "Invalid image source manifest path",
        )
        target = ROOT / name
        _require(
            target.is_file() and not target.is_symlink() and target.resolve().is_relative_to(ROOT.resolve()),
            "Reviewed image source file is absent or indirect",
        )
        actual_files[name] = hashlib.sha256(target.read_bytes()).hexdigest()
        _require(actual_files[name] == expected, "Image source or dependency lock differs from reviewed source")
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    installed = {}
    for package in _provider_packages():
        locked = {entry["version"] for entry in lock["package"] if entry["name"] == package}
        _require(len(locked) == 1, f"{package} does not have one unambiguous locked version")
        installed[package] = version(package)
        _require(installed[package] in locked, f"Installed {package} differs from the image dependency lock")
    return actual_files, installed


@contextmanager
def _deny_external_operations() -> Generator[dict[str, int], None, None]:
    attempts = {"network": 0, "credential": 0, "subprocess": 0}

    def denied(kind: str) -> Callable[..., NoReturn]:
        def reject(*_args: object, **_kwargs: object) -> NoReturn:
            attempts[kind] += 1
            raise ImageSmokeError(f"Forbidden {kind} operation during native image smoke")
        return reject

    with ExitStack() as stack:
        for connection in (socket.socket, ssl.SSLSocket):
            for name in ("connect", "connect_ex", "send", "sendall", "sendto", "sendmsg", "write", "do_handshake"):
                if hasattr(connection, name):
                    stack.enter_context(patch.object(connection, name, denied("network")))
        for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            stack.enter_context(patch.object(socket, name, denied("network")))
        stack.enter_context(patch.object(subprocess, "Popen", denied("subprocess")))
        for credential in (
            identity.AzureCliCredential, identity.DefaultAzureCredential,
            identity.ManagedIdentityCredential, identity.EnvironmentCredential,
            identity.WorkloadIdentityCredential,
        ):
            for method in ("get_token", "get_token_info"):
                if hasattr(credential, method):
                    stack.enter_context(patch.object(credential, method, denied("credential")))
        yield attempts


def collect_image_smoke(
    *,
    source_revision: str,
    image_digest: str,
    expected_files: Mapping[str, str],
    capture_requests: Callable[[], Sequence[dict]],
) -> dict:
    """Run in a separate process inside the inspected API image, not the worker.

    The operator supplies source/build/digest bindings verified outside the
    container. This process verifies source bytes and installed locked packages,
    not its own registry identity. The callback uses production preflight APIs
    with complete prepared planning inputs; actual runtime requests self-gate.
    """
    _require(re.fullmatch(r"[0-9a-f]{40}", source_revision) is not None, "Invalid reviewed source revision")
    _require(re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is not None, "Immutable image digest required")
    files, installed = _runtime_binding(expected_files)
    with _deny_external_operations() as attempts:
        receipts = list(capture_requests())
    _require(not any(attempts.values()), "A forbidden operation was attempted during image smoke")
    _require(bool(receipts), "No native image requests were captured")
    providers = set()
    for receipt in receipts:
        _require(
            receipt.get("status") == "validated_no_send"
            and all(receipt.get(key) is False for key in (
                "authentication_performed", "provider_send_performed", "provider_response_fabricated",
            )),
            "Native image capture is missing or not credential-free/no-send",
        )
        providers.add(receipt.get("provider"))
        for package_field, version_field in (("sdk_package", "sdk_version"), ("transport_package", "transport_version")):
            package = receipt.get(package_field)
            expected = {
                **installed, "http.client": ".".join(map(str, sys.version_info[:3])),
                "ssl": ssl.OPENSSL_VERSION,
            }.get(package)
            _require(expected is not None and receipt.get(version_field) == expected, "Native capture version differs from inspected runtime")
    _require(
        {"model", "webiq", "web_retrieval"} <= providers,
        "Image smoke must cover model, discovery, and original-page production request builders",
    )
    after_files, after_installed = _runtime_binding(expected_files)
    _require((files, installed) == (after_files, after_installed), "Image runtime changed during smoke")
    return {
        "schema_version": 1,
        "status": "validated_no_send",
        "source_revision": source_revision,
        "image_digest": image_digest,
        "image_identity_basis": "operator_verified_deployment_and_build_not_self_attested",
        "code_sha256": files,
        "lock_sha256": files["uv.lock"],
        "installed_sdk_versions": installed,
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "network": "python_dns_socket_process_and_real_credential_operations_denied",
        "blocked_operation_attempts": attempts,
        "native_requests": receipts,
        "request_basis": "complete_prepared_planning_inputs_actual_runtime_requests_self_gate",
        "authentication_performed": False,
        "provider_send_performed": False,
        "worker_execution_started": False,
    }
