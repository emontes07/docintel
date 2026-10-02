import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.pilot_server import StartupError, available_port, main, port_number, supervise


def test_missing_config_is_actionable_and_does_not_create_state(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing"
    monkeypatch.setenv("DOCINTEL_PILOT_HOME", str(missing))
    assert main() == 2
    assert "configuration is missing" in capsys.readouterr().err
    assert not missing.exists()


def test_invalid_config_does_not_print_values(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.json").write_text('{"private-setting":"SECRET"}')
    monkeypatch.setenv("DOCINTEL_PILOT_HOME", str(tmp_path))
    assert main() == 2
    error = capsys.readouterr().err
    assert "invalid" in error
    assert "SECRET" not in error


def test_occupied_loopback_port_is_not_disrupted():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(StartupError, match="no existing process was stopped"):
            available_port(port)
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_port_validation(value, monkeypatch):
    monkeypatch.setenv("PILOT_BACKEND_PORT", value)
    with pytest.raises(StartupError):
        port_number("PILOT_BACKEND_PORT", 8011)


@pytest.mark.parametrize(("wait_status", "expected"), [(7 << 8, 7), (0, 1), (signal.SIGTERM, 143)])
def test_failed_child_cleans_up_only_started_groups(monkeypatch, tmp_path, wait_status, expected):
    children = []
    killed = []
    def spawn(command, **options):
        assert options["start_new_session"] is True
        child = SimpleNamespace(pid=500 + len(children), returncode=None, wait=lambda timeout: 0)
        children.append(child)
        return child
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(os, "wait", lambda: (500, wait_status))
    monkeypatch.setattr(os, "killpg", lambda pid, signum: killed.append((pid, signum)))
    assert supervise([["first"], ["second"]], environment={}, cwd=tmp_path) == expected
    assert killed == [(500, signal.SIGTERM), (501, signal.SIGTERM)]


def test_real_child_failure_does_not_leave_sibling_running(tmp_path):
    program = "from pathlib import Path; import os,sys; from backend.pilot_server import supervise; raise SystemExit(supervise([[sys.executable,'-c','raise SystemExit(7)'],[sys.executable,'-c','import signal; signal.pause()']],environment=dict(os.environ),cwd=Path.cwd()))"
    result = subprocess.run([sys.executable, "-c", program], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode == 7
    assert "stopping only this launcher's" in result.stderr