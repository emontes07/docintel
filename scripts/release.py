"""Import-only compatibility for archived release tests; use ./run.sh to operate."""

if __name__ == "__main__":
    import sys
    print("Archived operator. Use ./run.sh {build|deploy|start|export|cost}.")
    raise SystemExit(0 if sys.argv[1:] in (["--help"], ["-h"]) else 2)

from tools.legacy import load
load(globals())
