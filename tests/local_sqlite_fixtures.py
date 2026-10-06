"""Explicit pytest-only placement adapter for legacy synthetic batch fixtures.

Load with ``-p tests.local_sqlite_fixtures`` and a repository-local --basetemp.
Only the three known fixture homes in the two legacy batch modules are adapted;
all SQLite operations, CAS behavior, leases, and production placement checks
outside those exact fixture homes remain unchanged.
"""

from pathlib import Path

import pytest

from backend.batch_store import SQLiteStore


@pytest.fixture(autouse=True)
def isolated_legacy_sqlite_home(request, monkeypatch, tmp_path):
    if request.module.__name__ not in {"tests.test_batch", "tests.test_batch_api"}:
        return
    repo = Path(__file__).resolve().parents[1]
    root = tmp_path.resolve()
    if repo not in root.parents:
        return
    original = SQLiteStore.__init__
    homes = {root / name for name in ("private", "state", "private-upload")}

    def initialize_fixture(store, home):
        selected = Path(home).expanduser().resolve()
        if selected not in homes:
            return original(store, home)
        selected.mkdir(mode=0o700, exist_ok=True)
        store.path = selected / "batches.sqlite3"
        with store.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, value BLOB NOT NULL, version TEXT NOT NULL)")
        store.path.chmod(0o600)

    monkeypatch.setattr(SQLiteStore, "__init__", initialize_fixture)
