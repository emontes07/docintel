"""Import an attested prior parse into a NEW isolated private SQLite store only.

No configured store, Azure credential, provider, analysis or live ledger is used.
The JSON specification pins four original artifacts and the request options.
"""

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.batch_store import SQLiteStore
from backend.parse_import import (
    Artifact, MAX_ARTIFACT_BYTES, ParseImportError, append_import_records,
    prepare_parse_import, sha256, snapshot_records,
)


def read_specification(path: Path) -> dict:
    spec = json.loads(path.read_bytes())
    if set(spec) != {"source", "parsed", "cache", "receipt", "request_options"}:
        raise ParseImportError(["invalid_import_specification"])
    arguments = {}
    for name in ("source", "parsed", "cache", "receipt"):
        entry = spec[name]
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ParseImportError(["invalid_artifact_specification"])
        artifact_path = Path(entry["path"])
        if not artifact_path.is_absolute() or not artifact_path.is_file():
            raise ParseImportError(["artifact_requires_existing_absolute_file"])
        with artifact_path.open("rb") as stream:
            content = stream.read(MAX_ARTIFACT_BYTES + 1)
        arguments[name] = Artifact(entry["path"], entry["sha256"], content)
    return {**arguments, "request_options": spec["request_options"]}


def persist_isolated_store(destination: Path, records: dict[str, bytes]) -> None:
    """Atomically append all records to a new, non-hosted store; never open an old one."""
    destination = destination.expanduser().absolute()
    repository = Path(__file__).resolve().parents[1]
    if destination.exists() or destination.is_symlink():
        raise ParseImportError(["destination_must_be_new"])
    resolved = destination.resolve()
    if resolved == repository or repository in resolved.parents:
        raise ParseImportError(["private_destination_must_be_outside_repository"])
    if any(os.environ.get(name) for name in ("CONTAINER_APP_NAME", "IDENTITY_ENDPOINT")):
        raise ParseImportError(["hosted_import_forbidden"])
    # Exclusive directory creation also prevents a racing invocation opening a live store.
    Path(os.path.relpath(destination)).mkdir(mode=0o700)
    store = SQLiteStore(Path(os.path.relpath(destination)))
    with store.connect() as connection:
        connection.executemany(
            "INSERT INTO records VALUES (?,?,?)",
            [(key, raw, "isolated-import-" + sha256(raw)) for key, raw in records.items()],
        )
    for key, raw in records.items():
        if store.read_bytes(key, max_bytes=len(raw))[0] != raw:
            raise ParseImportError(["persisted_record_integrity_failed"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--specification", type=Path, required=True)
    parser.add_argument("--new-isolated-store", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--snapshot-sha256")
    args = parser.parse_args(argv)
    try:
        if bool(args.snapshot) != bool(args.snapshot_sha256):
            raise ParseImportError(["snapshot_requires_independent_sha256"])
        prepared = prepare_parse_import(**read_specification(args.specification))
        prior = {}
        if args.snapshot:
            content = args.snapshot.read_bytes()
            if sha256(content) != args.snapshot_sha256:
                raise ParseImportError(["snapshot_hash_mismatch"])
            prior = snapshot_records(content)
        records = append_import_records(prior, prepared)
        persist_isolated_store(args.new_isolated_store, records)
    except ParseImportError as error:
        print(json.dumps({"status": "NO_GO", "blockers": error.codes, "new_analysis_submissions": 0}))
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"status": "NO_GO", "blockers": ["invalid_or_unreadable_local_input"], "new_analysis_submissions": 0}))
        return 2
    print(json.dumps({
        "status": "IMPORTED_OFFLINE_ONLY", "cache_key": prepared.cache_key,
        "fingerprint": prepared.fingerprint, "preserved_prior_records": len(prior),
        "new_analysis_submissions": 0, "freshly_analyzed": False,
        "hosted_ingestion_verified": False,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
