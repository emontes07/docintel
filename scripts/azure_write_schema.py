"""Import-only compatibility for historical write-schema tests."""

if __name__ == "__main__":
    raise SystemExit("Archived schema tool. Not part of deployment or CI.")

from tools.legacy import load
load(globals())
