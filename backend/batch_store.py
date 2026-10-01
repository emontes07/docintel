"""Durable batch records with conditional writes and renewable worker leases."""

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError, ResourceModifiedError, HttpResponseError
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient, ContentSettings


class Conflict(ValueError):
    pass


class Missing(KeyError):
    pass


class SQLiteStore:
    def __init__(self, home: Path):
        home = home.expanduser().resolve()
        repo = Path(__file__).resolve().parents[1]
        if home == repo or repo in home.parents:
            raise ValueError("Development batch storage must be outside the repository")
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = home / "batches.sqlite3"
        with self.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, value BLOB NOT NULL, version TEXT NOT NULL)")
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def read_bytes(self, key, max_bytes=64 * 1024 * 1024):
        with self.connect() as connection:
            row = connection.execute("SELECT value,version FROM records WHERE key=?", (key,)).fetchone()
        if row is None:
            raise Missing(key)
        if len(row[0]) > max_bytes:
            raise ValueError("Stored record exceeds read limit")
        return bytes(row[0]), row[1]

    def write_bytes(self, key, value, version=None):
        token = str(uuid.uuid4())
        with self.connect() as connection:
            if version is None:
                try:
                    connection.execute("INSERT INTO records VALUES (?,?,?)", (key, value, token))
                except sqlite3.IntegrityError:
                    raise Conflict("Record already exists") from None
            elif connection.execute("UPDATE records SET value=?,version=? WHERE key=? AND version=?", (value, token, key, version)).rowcount != 1:
                raise Conflict("Record changed; reload before retrying")
        return token

    def keys(self, prefix):
        with self.connect() as connection:
            return [row[0] for row in connection.execute("SELECT key FROM records WHERE key LIKE ? ORDER BY key", (prefix + "%",))]

    @contextmanager
    def lease(self, key):
        lock_key = "locks/" + key
        try:
            raw, version = self.read_bytes(lock_key)
            if float(raw) > time.time():
                raise Conflict("Batch is already leased")
        except Missing:
            version = None
        version = self.write_bytes(lock_key, str(time.time() + 60).encode(), version)
        mutex = threading.Lock()

        def renew():
            nonlocal version
            with mutex:
                version = self.write_bytes(lock_key, str(time.time() + 60).encode(), version)

        try:
            yield renew
        finally:
            try:
                self.write_bytes(lock_key, b"0", version)
            except Conflict:
                pass


class BlobStore:
    def __init__(self, account_url: str, container: str):
        if not account_url.startswith("https://") or not container or container in {"images", "videos", "$web"}:
            raise ValueError("A dedicated private batch container is required")
        self.credential = ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID") or None)
        self.container = BlobServiceClient(account_url, credential=self.credential).get_container_client(container)
        if self.container.get_container_properties().get("public_access"):
            raise ValueError("Batch storage must not allow public access")

    def read_bytes(self, key, max_bytes=64 * 1024 * 1024):
        try:
            response = self.container.get_blob_client(key).download_blob(length=max_bytes + 1)
            content = response.readall()
            if len(content) > max_bytes:
                raise ValueError("Stored record exceeds read limit")
            return content, response.properties.etag
        except ResourceNotFoundError:
            raise Missing(key) from None

    def write_bytes(self, key, value, version=None):
        try:
            options = {"etag": version, "match_condition": MatchConditions.IfNotModified} if version else {}
            response = self.container.get_blob_client(key).upload_blob(value, overwrite=version is not None, content_settings=ContentSettings(content_type="application/octet-stream"), **options)
            return response["etag"]
        except (ResourceExistsError, ResourceModifiedError):
            raise Conflict("Record already exists or changed") from None

    def keys(self, prefix):
        return [blob.name for blob in self.container.list_blobs(name_starts_with=prefix)]

    @contextmanager
    def lease(self, key):
        blob = self.container.get_blob_client("locks/" + key)
        try:
            blob.upload_blob(b"", overwrite=False)
        except ResourceExistsError:
            pass
        try:
            lease = blob.acquire_lease(lease_duration=60)
        except HttpResponseError as error:
            if error.status_code == 409:
                raise Conflict("Batch is already leased") from None
            raise
        mutex = threading.Lock()

        def renew():
            with mutex:
                lease.renew()

        try:
            yield renew
        finally:
            try:
                lease.release()
            except HttpResponseError:
                pass


def read_json(store, key):
    value, version = store.read_bytes(key)
    return json.loads(value), version


def write_json(store, key, value, version=None):
    return store.write_bytes(key, json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode(), version)


def configured_store():
    if os.environ.get("DOCINTEL_BATCH_MODE") == "development":
        if os.environ.get("CONTAINER_APP_NAME") or os.environ.get("IDENTITY_ENDPOINT"):
            raise ValueError("Development storage is forbidden in Azure")
        home = os.environ.get("DOCINTEL_BATCH_HOME")
        if not home:
            raise ValueError("Set an explicit private DOCINTEL_BATCH_HOME for development")
        return SQLiteStore(Path(home))
    return BlobStore(os.environ["DOCINTEL_BATCH_STORAGE_URL"], os.environ["DOCINTEL_BATCH_CONTAINER"])