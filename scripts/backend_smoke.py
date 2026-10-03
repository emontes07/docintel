"""Verify the installed Blob SDK contract in the built backend image offline."""

import argparse
import json
from pathlib import Path

from scripts.release import command, fingerprint, require, save


def smoke(work, image):
    code = '''from unittest.mock import Mock, patch
from azure.storage.blob import BlobClient
from azure.core.pipeline.transport import RequestsTransport
from backend.batch_store import BlobStore
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
    output = command(["docker", "run", "--rm", "--network", "none", "--entrypoint", "/app/.venv/bin/python", image, "-c", code])
    require("DOCINTEL_BACKEND_SDK_RANGE_OK" in output.decode().splitlines(), "Backend SDK runtime check failed")
    source = json.loads((work / "source.json").read_text())
    report = {"revision": source["revision"], "passed": True, "network": "none", "sdk_range": "bytes=0-4", "image_id": json.loads(command(["docker", "image", "inspect", image]))[0]["Id"], "context_sha256": fingerprint(json.loads((work / "context.json").read_text())), "modes_sha256": fingerprint(json.loads((work / "context-modes.json").read_text()))}
    save(work / "backend-smoke.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--image", required=True)
    options = parser.parse_args()
    smoke(options.work, options.image)