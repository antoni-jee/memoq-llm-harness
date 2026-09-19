"""Write history-diff.txt: what the latest /export added to history.txt.

Usage:
    1. python tools\\history_diff.py --snapshot     (before re-exporting)
    2. /export history.txt                          (in the Claude Code CLI)
    3. python tools\\history_diff.py                (writes history-diff.txt)

Step 1 stores the current history.txt as a baseline. Step 2 overwrites
history.txt with the full current transcript. Step 3 diffs the two and leaves
only the new material in history-diff.txt, then drops the baseline.

Everything is read and written as UTF-8 explicitly: these transcripts carry
Cyrillic and box-drawing characters, and Windows PowerShell's default ANSI
codepage would mangle both.
"""

from __future__ import annotations

import difflib
import io
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY = os.path.join(ROOT, "history.txt")
BASELINE = os.path.join(ROOT, ".history_baseline.txt")
DIFF = os.path.join(ROOT, "history-diff.txt")


def read(path: str) -> str:
    with io.open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def write(path: str, text: str) -> None:
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def snapshot() -> int:
    if not os.path.exists(HISTORY):
        print("No history.txt to snapshot.")
        return 1
    write(BASELINE, read(HISTORY))
    print("Baseline saved: %s (%d bytes)" % (BASELINE, os.path.getsize(BASELINE)))
    print("Now run  /export history.txt  then re-run this script.")
    return 0


def build_diff() -> int:
    if not os.path.exists(BASELINE):
        print("No baseline at %s - run with --snapshot first." % BASELINE)
        return 1
    if not os.path.exists(HISTORY):
        print("No history.txt found.")
        return 1

    old_text, new_text = read(BASELINE), read(HISTORY)
    if old_text == new_text:
        print("history.txt is unchanged - did the /export run?")
        return 1

    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)

    # The common case is pure append: the new export is the old one plus more.
    # Detect that exactly rather than inferring it from diff opcodes.
    if new_text.startswith(old_text):
        added = new_lines[len(old_lines):]
        mode = "appended"
        removed_count = 0
    else:
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        added, removed_count = [], 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("insert", "replace"):
                added.extend(new_lines[j1:j2])
            if tag in ("delete", "replace"):
                removed_count += i2 - i1
        mode = "merged"

    header = [
        "Difference between the previous history.txt and the current export.",
        "Generated: %s" % datetime.now().isoformat(timespec="seconds"),
        "Previous: %d lines / %d bytes" % (len(old_lines), len(old_text.encode())),
        "Current:  %d lines / %d bytes" % (len(new_lines), len(new_text.encode())),
        "New:      %d lines (%s)" % (len(added), mode),
    ]
    if removed_count:
        header.append("Note:     %d line(s) present before are gone now; only "
                      "additions are listed below." % removed_count)
    header += ["=" * 72, ""]

    write(DIFF, "\n".join(header) + "".join(added))
    print("Wrote %s (%d new lines, %d bytes)"
          % (DIFF, len(added), os.path.getsize(DIFF)))

    os.remove(BASELINE)
    print("Baseline removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(snapshot() if "--snapshot" in sys.argv else build_diff())
