#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Unit tests for check_dashes.py, stdlib unittest only.
#
# Every case runs the REAL script as a subprocess against a throwaway git
# repository, because what CI runs is the script, not a function. The last
# test runs it against this repository itself: that is the mechanical form
# of the owner's text rule, not a claim about it.
#
# The dash characters are built from code points so this file stays free of
# them.
#
# Run:  cd .github/scripts/tests && python3 -m unittest discover -p 'test_*.py'
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "check_dashes.py"
REPO_ROOT = Path(__file__).resolve().parents[3]
EM, EN = chr(0x2014), chr(0x2013)


def git_env() -> dict:
    env = dict(os.environ)
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})
    return env


class CheckDashesTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, env=git_env())

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, rel: str, content, track: bool = True):
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
        if track:
            subprocess.run(["git", "-C", str(self.root), "add", rel], check=True, env=git_env())

    def run_check(self, root=None):
        proc = subprocess.run([sys.executable, str(SCRIPT), str(root or self.root)],
                              capture_output=True, text=True, env=git_env())
        return proc.returncode, proc.stdout + proc.stderr

    def test_clean_tree_passes(self):
        self.put("README.md", "# Title\n\nplain - hyphen, colon: fine\n")
        self.put(".github/workflows/x.yml", "name: x # comment - ok\n")
        code, out = self.run_check()
        self.assertEqual(code, 0, out)
        self.assertIn("No em or en dash in 2 tracked text file(s)", out)

    def test_em_dash_in_readme_fails_with_path_and_line(self):
        self.put("README.md", "one\ntwo\nthree %s four\n" % EM)
        code, out = self.run_check()
        self.assertEqual(code, 1, out)
        self.assertIn("README.md:3:7: em dash", out)
        self.assertIn("::error file=README.md,line=3,col=7::", out)

    def test_en_dash_under_github_fails(self):
        self.put(".github/workflows/x.yml", "# a %s b\nname: x\n" % EN)
        self.put(".github/scripts/y.py", 'print("ok")\nprint("a %s b")\n' % EM)
        code, out = self.run_check()
        self.assertEqual(code, 1, out)
        self.assertIn(".github/workflows/x.yml:1:5: en dash", out)
        self.assertIn(".github/scripts/y.py:2:10: em dash", out)
        self.assertIn("2 em/en dash hit(s)", out)

    def test_every_tracked_text_file_is_checked(self):
        self.put(".gitignore", "# a %s b\n" % EM)
        code, out = self.run_check()
        self.assertEqual(code, 1, out)
        self.assertIn(".gitignore:1:5: em dash", out)

    def test_entities_count_in_markdown_only(self):
        for form in ("&mdash;", "&ndash;", "&#8212;", "&#08211;", "&#x2014;", "&#X2013;"):
            with self.subTest(form=form):
                self.put("doc.md", "x %s y\n" % form)
                code, out = self.run_check()
                self.assertEqual(code, 1, out)
                self.assertIn("doc.md:1:3: dash entity %s" % form, out)
        self.put("doc.md", "x &amp;mdash; y &middot; z\n")
        self.put("script.py", 's = "&mdash;"\n')
        code, out = self.run_check()
        self.assertEqual(code, 0, out)

    def test_binary_and_untracked_files_are_skipped(self):
        self.put("image.png", b"\x89PNG\0\0" + EM.encode("utf-8"))
        self.put("latin1.txt", "caf\xe9 ".encode("latin-1") + b"\x96")
        self.put("untracked.md", "a %s b\n" % EM, track=False)
        self.put("README.md", "clean\n")
        code, out = self.run_check()
        self.assertEqual(code, 0, out)
        self.assertIn("(2 binary file(s) skipped)", out)

    def test_this_repository_follows_the_text_rule(self):
        """The owner's text rule, held by a test against the real tree."""
        code, out = self.run_check(REPO_ROOT)
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
