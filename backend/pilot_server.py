"""Supervise the two loopback-only local prototype processes."""

import os
import signal
import socket
import subprocess
import sys
from pathlib import Path


class StartupError(ValueError):
    pass


def port_number(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        if not 1 <= value <= 65535:
            raise ValueError
        return value
    except ValueError:
        raise StartupError(f"{name} must be an integer from 1 to 65535.") from None


def available_port(port: int):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
    except OSError:
        raise StartupError(f"Loopback port {port} is unavailable. Choose another PILOT_BACKEND_PORT or PILOT_FRONTEND_PORT; no existing process was stopped.") from None


def supervise(commands: list[list[str]], *, environment: dict[str, str], cwd: Path) -> int:
    processes = []
    previous_handlers = {}

    def stop(signum, frame):
        raise SystemExit(128 + signum)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, stop)
        for command in commands:
            processes.append(subprocess.Popen(command, cwd=cwd, env=environment, start_new_session=True))
        pid, status = os.wait()
        exit_code = os.waitstatus_to_exitcode(status)
        for process in processes:
            if process.pid == pid:
                process.returncode = exit_code
        reported_code = 128 - exit_code if exit_code < 0 else exit_code
        print(f"A local server exited with code {reported_code}; stopping only this launcher's remaining processes. Check the preceding server output before restarting.", file=sys.stderr)
        return reported_code or 1
    finally:
        for signum in previous_handlers:
            signal.signal(signum, signal.SIG_IGN)
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def main() -> int:
    os.umask(0o077)
    root = Path(__file__).resolve().parents[1]
    try:
        home = Path(os.environ.get("DOCINTEL_PILOT_HOME", str(Path.home() / "Library/Application Support/DocIntel/prototype"))).expanduser().resolve()
        if not (home / "config.json").is_file():
            raise StartupError("Private pilot configuration is missing. Run the one-time 'backend.pilot setup' command in PILOT.md, or select an existing private DOCINTEL_PILOT_HOME. No servers started.")
        if home == root or root in home.parents:
            raise StartupError("DOCINTEL_PILOT_HOME must be outside the repository.")
        try:
            from backend.pilot import PilotConfig

            PilotConfig.from_private_home(home)
        except Exception:
            raise StartupError("Private pilot configuration is unreadable or invalid. Check the approved setup in PILOT.md; configuration values withheld. No servers started.") from None
        next_command = root / "frontend/node_modules/.bin/next"
        if not next_command.is_file() or not os.access(next_command, os.X_OK):
            raise StartupError("Installed frontend dependencies are missing. No automatic installation attempted; see the clean-install gate in PILOT.md.")
        backend_port = port_number("PILOT_BACKEND_PORT", 8011)
        frontend_port = port_number("PILOT_FRONTEND_PORT", 3100)
        if backend_port == frontend_port:
            raise StartupError("Backend and frontend ports must be different.")
        available_port(backend_port)
        available_port(frontend_port)
        environment = dict(os.environ, DOCINTEL_PILOT_API=f"http://127.0.0.1:{backend_port}", NEXT_TELEMETRY_DISABLED="1", PYTHONDONTWRITEBYTECODE="1")
        commands = [
            [sys.executable, "-m", "uvicorn", "backend.pilot_api:app", "--host", "127.0.0.1", "--port", str(backend_port), "--no-access-log"],
            [str(next_command), "dev", str(root / "frontend"), "--hostname", "127.0.0.1", "--port", str(frontend_port)],
        ]
        print(f"Starting local review at http://127.0.0.1:{frontend_port}/pilot", flush=True)
        return supervise(commands, environment=environment, cwd=root)
    except StartupError as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError:
        print("Could not start installed local executables. No dependencies were installed; check PILOT.md.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())