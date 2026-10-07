"""Import-only compatibility for historical receipt tests."""

if __name__ == "__main__":
    raise SystemExit("Archived smoke helper. See DEPLOYMENT.md.")

from tools.legacy import load
load(globals())
