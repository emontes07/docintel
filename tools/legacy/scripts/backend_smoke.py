"""Verify non-root startup with synthetic hosted authentication and offline Blob SDK checks."""

import argparse
import json
from pathlib import Path

from scripts.release import command, fingerprint, require, save

SMOKE_ENVIRONMENT = {
    "DOCINTEL_BATCH_MODE": "hosted",
    "DOCINTEL_AUTH_TENANT_ID": "11111111-1111-4111-8111-111111111111",
    "DOCINTEL_AUTH_AUDIENCE": "synthetic-batch-api",
    "DOCINTEL_AUTH_CLIENT_ID": "22222222-2222-4222-8222-222222222222",
    "DOCINTEL_BATCH_LIVE_ENABLED": "false",
    "DOCINTEL_REAL_PILOT_ENABLED": "false",
    "DOCINTEL_PILOT_UPLOAD_ENABLED": "false",
}


def smoke(work, image):
    code = '''import os, subprocess, time, urllib.request, urllib.error
from unittest.mock import Mock, patch
from azure.storage.blob import BlobClient
from azure.core.pipeline.transport import RequestsTransport
from backend.batch_store import BlobStore
assert os.getuid() == 10001 and os.getgid() == 10001
process = subprocess.Popen(
    ["/app/.venv/bin/fastapi", "run", "backend/main.py", "--port", "80", "--host", "127.0.0.1"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    for attempt in range(60):
        assert process.poll() is None, "Non-root API exited during startup"
        try:
            with urllib.request.urlopen("http://127.0.0.1:80/api/v1/health", timeout=1) as response:
                assert response.status == 200
            break
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.5)
    else:
        raise AssertionError("Non-root API did not become responsive")
    try:
        urllib.request.urlopen("http://127.0.0.1:80/api/v1/batches", timeout=2)
    except urllib.error.HTTPError as error:
        assert error.code == 401
    else:
        raise AssertionError("Anonymous batch API unexpectedly accessible")
    print("DOCINTEL_BACKEND_NONROOT_STARTUP_OK")
finally:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
class NetworkBlocked(RuntimeError):
    pass
def blocked(self, request, **kwargs):
    assert request.headers["x-ms-range"] == "bytes=0-4"
    raise NetworkBlocked()
client = BlobClient("https://synthetic.invalid", "synthetic", "record")
with patch.object(RequestsTransport, "send", blocked):
    try:
        client.download_blob(length=5)
    except ValueError as error:
        assert "Offset value must not be None" in str(error)
    else:
        raise AssertionError("Missing-offset regression not reproduced")
    store = object.__new__(BlobStore)
    store.container = Mock()
    store.container.get_blob_client.return_value = client
    try:
        store.read_bytes("record", max_bytes=4)
    except NetworkBlocked:
        print("DOCINTEL_BACKEND_SDK_RANGE_OK")
    else:
        raise AssertionError("SDK boundary not exercised")
'''
    environment = [argument for name, value in SMOKE_ENVIRONMENT.items() for argument in ["--env", f"{name}={value}"]]
    output = command(["docker", "run", "--rm", "--network", "none", *environment, "--entrypoint", "/app/.venv/bin/python", image, "-c", code])
    require("DOCINTEL_BACKEND_SDK_RANGE_OK" in output.decode().splitlines(), "Backend SDK runtime check failed")
    require("DOCINTEL_BACKEND_NONROOT_STARTUP_OK" in output.decode().splitlines(), "Backend non-root startup failed")
    source = json.loads((work / "source.json").read_text())
    report = {"revision": source["revision"], "passed": True, "network": "none", "uid": 10001, "gid": 10001, "startup": "health_200_anonymous_batch_401", "sdk_range": "bytes=0-4", "image_id": json.loads(command(["docker", "image", "inspect", image]))[0]["Id"], "context_sha256": fingerprint(json.loads((work / "context.json").read_text())), "modes_sha256": fingerprint(json.loads((work / "context-modes.json").read_text()))}
    save(work / "backend-smoke.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--image", required=True)
    options = parser.parse_args()
    smoke(options.work, options.image)