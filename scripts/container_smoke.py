"""Offline startup check, executed inside the backend container as its default user."""

import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request


def main():
    if os.getuid() == 0 or os.getgid() == 0:
        raise RuntimeError("Backend container must run as a non-root user")
    if shutil.which("pdftoppm") is None:
        raise RuntimeError("Cached-PDF rendering requires pdftoppm in the backend image")
    environment = {
        **os.environ,
        "DOCINTEL_BATCH_MODE": "hosted",
        "DOCINTEL_AUTH_TENANT_ID": "11111111-1111-4111-8111-111111111111",
        "DOCINTEL_AUTH_AUDIENCE": "offline-test-api",
        "DOCINTEL_AUTH_CLIENT_ID": "22222222-2222-4222-8222-222222222222",
        "DOCINTEL_BATCH_LIVE_ENABLED": "false",
        "DOCINTEL_REAL_PILOT_ENABLED": "false",
        "DOCINTEL_PILOT_UPLOAD_ENABLED": "false",
    }
    process = subprocess.Popen(
        ["/app/.venv/bin/fastapi", "run", "backend/main.py", "--port", "80", "--host", "0.0.0.0"],
        env=environment,
    )
    try:
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError("Backend exited before becoming healthy")
            try:
                with urllib.request.urlopen("http://127.0.0.1:80/api/v1/health", timeout=1) as response:
                    if response.status != 200:
                        raise RuntimeError("Backend health check failed")
                break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.5)
        else:
            raise RuntimeError("Backend did not become healthy")
        try:
            urllib.request.urlopen("http://127.0.0.1:80/api/v1/batches", timeout=2)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise RuntimeError("Anonymous request must return 401") from error
        else:
            raise RuntimeError("Anonymous batch access unexpectedly succeeded")
        print("Non-root startup, health and authentication checks passed.")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    main()
