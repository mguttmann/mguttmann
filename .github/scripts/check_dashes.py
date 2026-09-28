#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# check_dashes.py  ·  Enforces the owner's text rule on the whole repository
# ---------------------------------------------------------------------------
# The rule: no em dash (U+2014) and no en dash (U+2013) in anything this
# repository publishes. A rule that is only written down ages quietly; this
# check turns it into a red CI job.
#
# Scope: EVERY tracked file (`git ls-files`), not only README.md and .github/,
# so the rule holds mechanically for the whole tree. A file that is not valid
# UTF-8 or that contains a NUL byte is treated as binary and skipped (counted
# in the summary). In Markdown files the HTML entity forms (&mdash; &ndash;
# and their decimal and hexadecimal numeric references) count as well,
# because GitHub renders them as the dash itself.
#
# Output per hit: `path:line:col: <kind>` plus a GitHub `::error` annotation
# with file and line. Exit 1 on at least one hit, 0 otherwise.
#
# The two characters are built from code points, so this file itself stays
# free of them. Standard library only.
#
# Usage:  python3 .github/scripts/check_dashes.py [REPO_ROOT]
# ---------------------------------------------------------------------------

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)
CHARS = {EM_DASH: "em dash", EN_DASH: "en dash"}
ENTITY = re.compile(r"&(?:mdash|ndash);|&#0*(?:8212|8211);|&#[xX]0*201[34];")
ENTITY_SUFFIXES = (".md",)


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                         check=True, capture_output=True).stdout
    return sorted(p for p in out.decode("utf-8", "surrogateescape").split("\0") if p)


def scan_text(path: str, text: str) -> list[tuple[str, int, int, str]]:
    hits = []
    check_entities = path.lower().endswith(ENTITY_SUFFIXES)
    for lineno, line in enumerate(text.splitlines(), 1):
        for col, char in enumerate(line, 1):
            if char in CHARS:
                hits.append((path, lineno, col, CHARS[char]))
        if check_entities:
            for m in ENTITY.finditer(line):
                hits.append((path, lineno, m.start() + 1, "dash entity %s" % m.group()))
    return hits


def scan(root: Path) -> tuple[list[tuple[str, int, int, str]], int, int]:
    hits, checked, skipped = [], 0, 0
    for rel in tracked_files(root):
        target = root / rel
        if not target.is_file():          # a deleted but still tracked path, or a submodule
            continue
        data = target.read_bytes()
        try:
            if b"\0" in data:
                raise ValueError("NUL byte")
            text = data.decode("utf-8")
        except ValueError:
            skipped += 1
            continue
        checked += 1
        hits.extend(sorted(scan_text(rel, text), key=lambda h: (h[1], h[2])))
    return hits, checked, skipped


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path.cwd()
    hits, checked, skipped = scan(root)
    for path, line, col, kind in hits:
        print("%s:%d:%d: %s" % (path, line, col, kind))
        print("::error file=%s,line=%d,col=%d::Owner text rule: %s (use a hyphen, a comma, "
              "a colon or parentheses instead)" % (path, line, col, kind))
    if hits:
        print("%d em/en dash hit(s) in %d checked file(s)." % (len(hits), checked))
        return 1
    print("No em or en dash in %d tracked text file(s) (%d binary file(s) skipped)."
          % (checked, skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
