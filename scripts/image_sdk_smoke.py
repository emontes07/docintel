"""Import-only compatibility for historical receipt tests."""

if __name__ == "__main__":
    raise SystemExit("Archived SDK helper. Not part of deployment or CI.")

from tools.legacy import load
load(globals())
