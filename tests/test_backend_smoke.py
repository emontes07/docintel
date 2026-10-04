from fastapi.testclient import TestClient

from backend.batch_api import development_app
from scripts.backend_smoke import SMOKE_ENVIRONMENT, smoke
from scripts.release import save


def test_startup_probe_distinguishes_missing_configuration_from_anonymous_access(monkeypatch):
    for name in SMOKE_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    client = TestClient(development_app)
    assert client.get("/api/v1/batches").status_code == 503
    for name, value in SMOKE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    assert client.get("/api/v1/batches").status_code == 401


def test_image_probe_passes_only_synthetic_hosted_configuration(tmp_path, monkeypatch):
    calls = []

    def command(arguments):
        calls.append(arguments)
        if arguments[1] == "run":
            return b"DOCINTEL_BACKEND_SDK_RANGE_OK\nDOCINTEL_BACKEND_NONROOT_STARTUP_OK\n"
        return b'[{"Id":"sha256:synthetic"}]'

    monkeypatch.setattr("scripts.backend_smoke.command", command)
    for name, value in [("source", {"revision": "synthetic"}), ("context", {}), ("context-modes", {})]:
        save(tmp_path / f"{name}.json", value)
    smoke(tmp_path, "synthetic-image")
    invocation = calls[0]
    assert invocation[:5] == ["docker", "run", "--rm", "--network", "none"]
    supplied = [invocation[index + 1] for index, value in enumerate(invocation) if value == "--env"]
    assert supplied == [f"{name}={value}" for name, value in SMOKE_ENVIRONMENT.items()]
