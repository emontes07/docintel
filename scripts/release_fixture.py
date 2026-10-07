"""Import-only compatibility for historical synthetic fixture tests."""

if __name__ == "__main__":
    raise SystemExit("Archived release fixture. CI uses offline application tests.")

from tools.legacy import load
load(globals())
