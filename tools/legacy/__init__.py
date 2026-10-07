"""Archived operators, available only for historical offline regression imports."""

from pathlib import Path


def load(namespace):
    source = Path(__file__).parent / "scripts" / Path(namespace["__file__"]).name
    namespace["__legacy_source__"] = str(source)
    # Preserve the original module globals so historical monkeypatches still work.
    exec(compile(source.read_bytes(), str(source), "exec"), namespace)
    if source.name == "azure_write_schema.py":
        namespace["ASSETS"] = source.with_name("azure_write_schemas")
