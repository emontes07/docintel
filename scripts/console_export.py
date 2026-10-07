"""Encode a native app-console export command and decode its workbook response."""

import base64
from io import BytesIO
from pathlib import Path
import re
import sys
import zipfile


BEGIN = "DOCINTEL_WORKBOOK_BEGIN"
END = "DOCINTEL_WORKBOOK_END"


def export_command(batch_id, owner):
    def literal(value):
        escaped = value.encode("unicode_escape").decode("ascii")
        return "'" + escaped.replace("'", "\\'").replace('"', "\\x22") + "'"

    code = (
        "import base64; from backend.batch import BatchService; "
        "from backend.batch_store import configured_store; "
        f"data=BatchService(configured_store()).export({literal(batch_id)},{literal(owner)}); "
        f"print({BEGIN!r}); print(base64.b64encode(data).decode('ascii')); print({END!r})"
    )
    return '/app/.venv/bin/python -c "' + code + '"'


def decode_export(transcript, destination):
    text = Path(transcript).read_text(errors="replace")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    lines = [line.strip() for line in text.splitlines()]
    if lines.count(BEGIN) != 1 or lines.count(END) != 1:
        raise ValueError("Console did not return one complete workbook")
    start, end = lines.index(BEGIN), lines.index(END)
    if end <= start + 1:
        raise ValueError("Console returned an empty workbook")
    content = base64.b64decode("".join(lines[start + 1:end]), validate=True)
    with zipfile.ZipFile(BytesIO(content)) as workbook:
        if "[Content_Types].xml" not in workbook.namelist() or workbook.testzip() is not None:
            raise ValueError("Console returned an invalid workbook")
    Path(destination).write_bytes(content)


def main():
    if len(sys.argv) != 4 or sys.argv[1] not in {"command", "decode"}:
        raise SystemExit("Usage: console_export.py command BATCH OWNER | decode TRANSCRIPT OUTPUT")
    try:
        if sys.argv[1] == "command":
            print(export_command(sys.argv[2], sys.argv[3]))
        else:
            decode_export(sys.argv[2], sys.argv[3])
    except (OSError, ValueError, zipfile.BadZipFile):
        raise SystemExit("Native console export failed; workbook output was not replaced.") from None


if __name__ == "__main__":
    main()
