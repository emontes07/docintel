"""Import-only compatibility for historical receipt tests."""

if __name__ == "__main__":
    raise SystemExit("Archived smoke helper. CI uses scripts/container_smoke.py.")

from tools.legacy import load
load(globals())
