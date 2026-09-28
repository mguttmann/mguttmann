#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Unit tests for the Featured generator embedded in featured-from-pins.yml.
#
# The generator lives in a heredoc inside the workflow (single source of
# truth). These tests extract it from the REAL workflow file and run it as a
# subprocess against a fixture, so what is tested is what the job runs.
#
# Run:  cd .github/scripts/tests && python3 -m unittest discover -p 'test_*.py'
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / "workflows" / "featured-from-pins.yml"
EM, EN, FIGURE = chr(0x2014), chr(0x2013), chr(0x2012)
README = "# x\n\n<!-- FEATURED:START -->\nold\n<!-- FEATURED:END -->\n\ntail\n"


def extract_generator() -> str:
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if "cat > gen_featured.py <<'PYEOF'" in line)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "PYEOF")
    return textwrap.dedent("\n".join(lines[start + 1:end])) + "\n"


def pin(name, description, lang="Shell"):
    return {"name": name, "description": description, "url": "https://github.com/o/%s" % name,
            "primaryLanguage": {"name": lang}, "isFork": False, "stargazerCount": 0}


class FeaturedGeneratorTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.gen = self.tmp / "gen_featured.py"
        self.gen.write_text(extract_generator(), encoding="utf-8")
        self.readme = self.tmp / "README.md"
        self.readme.write_text(README, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def run_gen(self, nodes):
        raw = self.tmp / "pins_raw.json"
        raw.write_text(json.dumps({"data": {"user": {"pinnedItems": {"nodes": nodes}}}}),
                       encoding="utf-8")
        out = self.tmp / "pins.json"
        proc = subprocess.run([sys.executable, str(self.gen), "--pins-file", str(raw),
                               "--readme", str(self.readme), "--pins-out", str(out)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.readme.read_text(encoding="utf-8"), json.loads(out.read_text(encoding="utf-8"))

    def test_dashes_in_descriptions_never_reach_the_readme(self):
        desc = "a %s b %s c %s d" % (EM, EN, FIGURE)
        text, pins = self.run_gen([pin("one", desc)])
        self.assertIn("a - b - c - d", text)
        for char in (EM, EN, FIGURE):
            self.assertNotIn(char, text)
        # pins.json stays raw data for the site (documented limit).
        self.assertEqual(pins[0]["description"], desc)

    def test_shortened_description_does_not_end_on_a_dash(self):
        # The cut at MAX_DESC lands right after the dash. The en dash case
        # discriminates: the old rstrip set only knew the em dash, so the
        # README ended on "word \u2013\u2026".
        for dash in (EM, EN, FIGURE, "-"):
            with self.subTest(dash=hex(ord(dash))):
                desc = ("word " * 31) + dash + " tail that is cut off"
                text, _ = self.run_gen([pin("one", desc)])
                self.assertIn("word word…", text)
                for char in (EM, EN, FIGURE):
                    self.assertNotIn(char, text)
                self.assertNotIn(" -…", text)

    def test_empty_pins_text_carries_no_dash(self):
        text, _ = self.run_gen([])
        self.assertIn("No pinned repositories yet. Pin a repository", text)
        self.assertNotIn(EM, text)
        self.assertNotIn(EN, text)

    def test_block_is_idempotent(self):
        first, _ = self.run_gen([pin("one", "x %s y" % EM)])
        second, _ = self.run_gen([pin("one", "x %s y" % EM)])
        self.assertEqual(first, second)
        self.assertTrue(first.endswith("<!-- FEATURED:END -->\n\ntail\n"))


if __name__ == "__main__":
    unittest.main()
