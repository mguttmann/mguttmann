#!/usr/bin/env python3
"""Regenerate the 'Open-source contributions' (opencode) block in README.md
from the LIVE pull requests Manuel opened on anomalyco/opencode.

Public data only -> uses `gh` with the built-in GITHUB_TOKEN (or local auth).
Rewrites ONLY the text between the OPENCODE:START / OPENCODE:END markers, so it
can never damage the rest of the README. Honest framing throughout: opencode is a
fork + open PRs (contribution), never presented as merged/owned.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = "anomalyco/opencode"
AUTHOR = "mguttmann"
COPPER = "C8A06A"
SURFACE = "14141B"
README = Path("README.md")
START = "<!-- OPENCODE:START -->"
END = "<!-- OPENCODE:END -->"


def kfmt(n: int) -> str:
    return f"{round(n / 1000, 1)}k" if n >= 1000 else str(n)


def strip_prefix(title: str) -> str:
    # drop a conventional-commit prefix like "feat:" / "fix(scope):"
    return re.sub(r"^[a-z]+(\([^)]*\))?!?:\s*", "", title).strip()


def fetch_prs():
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--repo", REPO, "--author", AUTHOR,
             "--state", "all", "--limit", "200",
             "--json", "number,state,additions,deletions,title"],
            capture_output=True, text=True, check=True,
        ).stdout
        return json.loads(out)
    except Exception as e:  # network / permission / parse -> safe no-op
        print(f"opencode_pr: could not fetch PRs ({e}); leaving README unchanged", file=sys.stderr)
        return None


def build_block(prs) -> str:
    total = len(prs)
    opens = [p for p in prs if p["state"] == "OPEN"]
    closed = sum(1 for p in prs if p["state"] == "CLOSED")
    merged = sum(1 for p in prs if p["state"] == "MERGED")

    badge_contrib = (
        f'https://img.shields.io/badge/opencode-contributing%20via%20PRs-{COPPER}'
        f'?style=for-the-badge&logo=github&logoColor={SURFACE}&labelColor={SURFACE}'
    )

    if opens:
        head = max(opens, key=lambda p: p["additions"])
        num, add, dele = head["number"], head["additions"], head["deletions"]
        quote = strip_prefix(head["title"])
        badge_pr = (
            f'https://img.shields.io/badge/open%20PR-%23{num}%20·%20%2B{kfmt(add)}'
            f'%20lines%20proposed-{COPPER}?style=for-the-badge&labelColor={SURFACE}'
        )
        counts = f"{total} PRs opened in total ({len(opens)} open, {closed} closed"
        counts += f", {merged} merged)." if merged else ")."
        return (
            f'{START}\n'
            f'<div align="center">\n\n'
            f'<a href="https://github.com/{REPO}/pull/{num}">\n'
            f'  <img src="{badge_contrib}" alt="opencode — contributing via pull requests" />\n'
            f'</a>\n'
            f'<a href="https://github.com/{REPO}/pull/{num}">\n'
            f'  <img src="{badge_pr}" alt="Open PR #{num} — +{kfmt(add)} lines proposed" />\n'
            f'</a>\n\n'
            f'</div>\n\n'
            f'<!-- LANG: prose below — translate for DE variant. Keep all qualifiers exact. -->\n'
            f'**[opencode](https://github.com/{REPO})** &nbsp;·&nbsp; <sub>open-source contribution (TypeScript)</sub>\n\n'
            f'Active **open-source contributor** to [`{REPO}`](https://github.com/{REPO})\n'
            f'via pull requests — working from [my fork](https://github.com/{AUTHOR}/opencode).\n'
            f'Headlining the **[open PR #{num}](https://github.com/{REPO}/pull/{num})**:\n'
            f'*"{quote}"* —\n'
            f'**~{kfmt(add)} lines proposed** (`+{add:,} / −{dele:,}`), **currently open** and awaiting upstream\n'
            f'review (proposed changes, not yet accepted). {counts}\n'
            f'{END}'
        )

    # no open PR right now — honest fallback
    counts = f"{total} PRs opened in total ({closed} closed"
    counts += f", {merged} merged)." if merged else ")."
    return (
        f'{START}\n'
        f'<div align="center">\n\n'
        f'<a href="https://github.com/{REPO}/pulls?q=is%3Apr+author%3A{AUTHOR}">\n'
        f'  <img src="{badge_contrib}" alt="opencode — contributing via pull requests" />\n'
        f'</a>\n\n'
        f'</div>\n\n'
        f'<!-- LANG: prose below — translate for DE variant. Keep all qualifiers exact. -->\n'
        f'**[opencode](https://github.com/{REPO})** &nbsp;·&nbsp; <sub>open-source contribution (TypeScript)</sub>\n\n'
        f'Active **open-source contributor** to [`{REPO}`](https://github.com/{REPO})\n'
        f'via pull requests — working from [my fork](https://github.com/{AUTHOR}/opencode).\n'
        f'{counts} No PR currently open; proposed changes, none merged.\n'
        f'{END}'
    )


def main() -> int:
    prs = fetch_prs()
    if prs is None:
        return 0  # safe no-op, README untouched
    text = README.read_text(encoding="utf-8")
    if START not in text or END not in text:
        print("opencode_pr: markers not found in README.md", file=sys.stderr)
        return 1
    block = build_block(prs)
    new = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _m: block, text, count=1, flags=re.S)
    if new != text:
        README.write_text(new, encoding="utf-8")
        print("opencode_pr: README block updated")
    else:
        print("opencode_pr: no change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
