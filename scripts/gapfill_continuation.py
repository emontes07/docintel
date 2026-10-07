"""Import-only compatibility for historical tests."""

if __name__ == "__main__":
    raise SystemExit("Archived operator. Use ./run.sh instead.")

from tools.legacy import load
load(globals())
