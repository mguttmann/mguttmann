#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Unit tests for publish_output.sh and push_with_retry.sh, stdlib unittest.
#
# A local BARE repository stands in for github.com: the scripts run as real
# subprocesses (bash, git) against a file:// remote, so clone, copy, commit,
# rebase and push are exercised for real, offline, without any token. A
# concurrent publisher is simulated two ways:
#   * a pre-receive hook that, on the first push only, pushes a commit from a
#     second clone while our push is in flight. The ref then no longer holds
#     the value our push expects, which is the local form of the production
#     failure of run 36358655683 ("cannot lock ref ... but expected ...");
#   * two clones that push alternately (the second one is behind).
# Git runs with no system config and a global config that only forbids
# guessing an identity, so the host's git settings cannot change the outcome.
#
# Run:  cd .github/scripts/tests && python3 -m unittest discover -p 'test_*.py'
# ---------------------------------------------------------------------------

from __future__ import annotations

import base64
import os
import subprocess
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
PUBLISH = SCRIPTS / "publish_output.sh"
PUSH = SCRIPTS / "push_with_retry.sh"

COMMITTER = "GitHub <noreply@github.com>"
AUTHOR = "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>"

RACE_HOOK = """#!/bin/bash
# Fires once: a concurrent publisher lands while this push is in flight.
[ -e "{marker}" ] && exit 0
touch "{marker}"
( unset GIT_DIR GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PUSH_OPTION_COUNT
  cd "{other}" && echo "{content}" > {name} && git add {name} \\
    && git -c user.name=other -c user.email=o@example.invalid commit -qm other \\
    && git push -q origin HEAD:refs/heads/output ) >&2
{tail}
"""


class GitFixture(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.runner_temp = self.tmp / "runner-temp"
        self.runner_temp.mkdir()
        # The only global setting: never guess an identity from the host
        # account. A GitHub runner has none to guess, so without this a
        # missing identity would pass here and fail only in CI.
        global_config = self.home / "gitconfig"
        global_config.write_text("[user]\n\tuseConfigOnly = true\n")
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(global_config),
            "RUNNER_TEMP": str(self.runner_temp),
            "PUSH_RETRY_PAUSE": "0",
        }
        self.remote = self.tmp / "remote.git"
        self.git("init", "-q", "--bare", "-b", "output", str(self.remote))
        self.remote_url = "file://%s" % self.remote

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args, cwd=None, check=True):
        env = dict(self.env, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        proc = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
        if check and proc.returncode != 0:
            self.fail("git %s failed: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def clone(self, name, branch="output"):
        path = self.tmp / name
        self.git("clone", "-q", "--branch", branch, self.remote_url, str(path))
        return path

    def seed(self, files):
        work = self.tmp / "seed"
        self.git("init", "-q", "-b", "output", str(work))
        for name, content in files.items():
            (work / name).write_text(content)
        self.git("add", ".", cwd=work)
        self.git("commit", "-qm", "seed", cwd=work)
        self.git("remote", "add", "origin", self.remote_url, cwd=work)
        self.git("push", "-q", "origin", "HEAD:refs/heads/output", cwd=work)
        return self.tip()

    def tip(self, branch="output"):
        return self.git("--git-dir", str(self.remote), "rev-parse", "refs/heads/%s" % branch)

    def remote_file(self, name, branch="output"):
        return self.git("--git-dir", str(self.remote), "show", "%s:%s" % (branch, name))

    def remote_files(self, branch="output"):
        return sorted(self.git("--git-dir", str(self.remote), "ls-tree", "-r", "--name-only",
                               branch).split())

    def staging(self, files, name="staging"):
        path = self.tmp / name
        for rel, content in files.items():
            (path / rel).parent.mkdir(parents=True, exist_ok=True)
            (path / rel).write_text(content)
        return path

    def publish(self, source, cwd=None, **extra):
        env = dict(self.env, PUBLISH_BRANCH="output", PUBLISH_SOURCE_DIR=str(source),
                   PUBLISH_MESSAGE="chore: publish", PUBLISH_REMOTE=self.remote_url)
        env.update(extra)
        proc = subprocess.run(["bash", str(PUBLISH)], env=env, capture_output=True, text=True,
                              cwd=str(cwd or self.tmp))
        return proc.returncode, proc.stdout + proc.stderr

    def push_with_retry(self, clone, branch="output", **extra):
        env = dict(self.env, **extra)
        proc = subprocess.run(["bash", str(PUSH), branch], env=env, capture_output=True,
                              text=True, cwd=str(clone))
        return proc.returncode, proc.stdout + proc.stderr

    def install_hook(self, text):
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text(text)
        hook.chmod(0o755)

    def install_race_hook(self, name="other.svg", content="other", tail="exit 0"):
        other = self.clone("other")
        self.install_hook(RACE_HOOK.format(marker=self.tmp / "fired", other=other,
                                           name=name, content=content, tail=tail))

    def is_ancestor(self, old, new):
        proc = subprocess.run(["git", "--git-dir", str(self.remote), "merge-base",
                               "--is-ancestor", old, new], env=self.env)
        return proc.returncode == 0


class PublishOutputTest(GitFixture):

    def test_publish_is_additive_and_never_prunes(self):
        old = self.seed({"a.svg": "a1", "b.svg": "b1"})
        code, out = self.publish(self.staging({"a.svg": "a2", "sub/c.svg": "c1"}))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.remote_files(), ["a.svg", "b.svg", "sub/c.svg"])
        self.assertEqual(self.remote_file("a.svg"), "a2")
        self.assertEqual(self.remote_file("b.svg"), "b1")
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "output^"), old)

    def test_config_of_the_callers_checkout_does_not_reach_the_publish(self):
        # In production the step runs inside the job's checkout, and
        # actions/checkout (v6+) attaches its persisted token to exactly that
        # repository through an includeIf.gitdir entry. A git command run
        # there would send that Authorization header next to the one this
        # script sets, and GitHub rejects the duplicate. Stand-in: the
        # included config rewrites the remote URL to a dead path, so any git
        # command that reads the checkout's config fails.
        self.seed({"a.svg": "a1"})
        checkout = (self.tmp / "checkout").resolve()
        self.git("init", "-q", str(checkout))
        included = self.tmp / "credentials.config"
        included.write_text('[url "file:///nonexistent-checkout-credential/"]\n'
                            "\tinsteadOf = %s\n" % self.remote_url)
        self.git("config", "includeIf.gitdir:%s/.git.path" % checkout, str(included),
                 cwd=checkout)
        self.assertNotEqual(
            subprocess.run(["git", "ls-remote", self.remote_url], cwd=str(checkout),
                           env=self.env, capture_output=True).returncode, 0,
            "stand-in inactive: the checkout's config must break git commands run inside it")
        code, out = self.publish(self.staging({"b.svg": "b1"}), cwd=checkout)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.remote_files(), ["a.svg", "b.svg"])

    def test_unchanged_content_makes_no_commit(self):
        old = self.seed({"a.svg": "a1", "b.svg": "b1"})
        code, out = self.publish(self.staging({"a.svg": "a1"}))
        self.assertEqual(code, 0, out)
        self.assertIn("No changes to commit.", out)
        self.assertEqual(self.tip(), old)

    def test_committer_and_author_match_the_replaced_action(self):
        self.seed({"a.svg": "a1"})
        code, out = self.publish(self.staging({"a.svg": "a2"}), PUBLISH_MESSAGE="chore: x")
        self.assertEqual(code, 0, out)
        log = self.git("--git-dir", str(self.remote), "log", "-1",
                       "--format=%cn <%ce>|%an <%ae>|%s", "output")
        self.assertEqual(log, "%s|%s|chore: x" % (COMMITTER, AUTHOR))

    def test_concurrent_publisher_is_rebased_onto_and_both_survive(self):
        old = self.seed({"a.svg": "a1"})
        self.install_race_hook()
        code, out = self.publish(self.staging({"mine.svg": "mine"}))
        self.assertEqual(code, 0, out)
        # The wording of the rejection depends on the git version of the
        # runner; any race signature the retry accepts proves the race.
        self.assertRegex(out, r"cannot lock ref|incorrect old value provided|failed to update ref")
        self.assertEqual(out.count("raced with a concurrent publisher"), 1, out)
        self.assertEqual(self.remote_files(), ["a.svg", "mine.svg", "other.svg"])
        self.assertTrue(self.is_ancestor(old, self.tip()))
        history = self.git("--git-dir", str(self.remote), "log", "--format=%s", "output").split("\n")
        self.assertEqual(history, ["chore: publish", "other", "seed"])
        # The rebased commit still carries the publisher identities.
        log = self.git("--git-dir", str(self.remote), "log", "-1", "--format=%cn <%ce>|%an <%ae>",
                       "output")
        self.assertEqual(log, "%s|%s" % (COMMITTER, AUTHOR))

    def test_without_retry_the_same_race_is_red(self):
        """Discrimination: one attempt (what crazy-max did) loses this race."""
        self.seed({"a.svg": "a1"})
        self.install_race_hook()
        code, out = self.publish(self.staging({"mine.svg": "mine"}), PUSH_MAX_ATTEMPTS="1")
        self.assertEqual(code, 1, out)
        self.assertIn("::error::", out)
        self.assertEqual(self.remote_files(), ["a.svg", "other.svg"])

    def test_github_race_message_is_retried(self):
        """The literal production signature of run 36358655683."""
        self.seed({"a.svg": "a1"})
        self.install_race_hook(tail=(
            "echo \"cannot lock ref 'refs/heads/output': is at d0d6c1d but expected 5adeed8\" >&2\n"
            "exit 1"))
        code, out = self.publish(self.staging({"mine.svg": "mine"}))
        self.assertEqual(code, 0, out)
        self.assertIn("cannot lock ref", out)
        self.assertEqual(self.remote_files(), ["a.svg", "mine.svg", "other.svg"])

    def test_real_conflict_on_the_same_file_fails_loud_and_overwrites_nothing(self):
        self.seed({"a.svg": "a1"})
        self.install_race_hook(name="a.svg", content="theirs")
        code, out = self.publish(self.staging({"a.svg": "mine"}))
        self.assertEqual(code, 1, out)
        self.assertIn("::error::Rebase onto the new output failed", out)
        self.assertEqual(self.remote_file("a.svg"), "theirs")

    def test_rejection_that_is_not_a_race_is_not_retried(self):
        old = self.seed({"a.svg": "a1"})
        self.install_hook("#!/bin/bash\necho 'GH006: Protected branch update failed' >&2\nexit 1\n")
        code, out = self.publish(self.staging({"mine.svg": "mine"}))
        self.assertEqual(code, 1, out)
        self.assertIn("not a concurrent update; not retrying", out)
        self.assertEqual(out.count("Protected branch update failed"), 1, out)
        self.assertEqual(self.tip(), old)

    def test_missing_branch_is_created_as_an_orphan(self):
        code, out = self.publish(self.staging({"x.svg": "x"}))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.remote_files(), ["x.svg"])
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-list", "--count", "output"), "1")

    def test_unreadable_remote_never_creates_an_orphan(self):
        code, out = self.publish(self.staging({"x.svg": "x"}),
                                 PUBLISH_REMOTE="file://%s" % (self.tmp / "does-not-exist.git"))
        self.assertEqual(code, 1, out)
        self.assertIn("git ls-remote exit", out)

    def test_missing_build_dir_fails(self):
        code, out = self.publish(self.tmp / "nope")
        self.assertEqual(code, 1, out)
        self.assertIn("does not exist", out)

    def test_token_never_reaches_output_url_or_disk(self):
        self.seed({"a.svg": "a1"})
        token = "tok-MARKER-4711"
        basic = base64.b64encode(("x-access-token:%s" % token).encode()).decode()
        capture = self.tmp / "captured"
        # While the push is in flight, record the clone's git config and the
        # extraheader the process environment carries.
        self.install_hook(
            "#!/bin/bash\n"
            "cat \"$RUNNER_TEMP\"/publish-output.*/.git/config > '%s'\n"
            "printf '%%s\\n' \"$GIT_CONFIG_KEY_0\" \"$GIT_CONFIG_VALUE_0\" >> '%s'\n"
            "exit 0\n" % (capture, capture))
        code, out = self.publish(self.staging({"a.svg": "a2"}), PUBLISH_TOKEN=token)
        self.assertEqual(code, 0, out)
        self.assertNotIn(token, out)
        self.assertEqual(out.count("::add-mask::%s" % basic), 1, out)
        self.assertEqual(out.count(basic), 1, out)
        captured = capture.read_text()
        lines = captured.rstrip("\n").split("\n")
        config, (key, value) = "\n".join(lines[:-2]), lines[-2:]
        self.assertIn("[remote \"origin\"]", config)
        self.assertNotIn(token, config)
        self.assertNotIn(basic, config)
        self.assertEqual(key, "http.https://github.com/.extraheader")
        self.assertEqual(value, "AUTHORIZATION: basic %s" % basic)
        self.assertEqual(list(self.runner_temp.iterdir()), [])     # work dir removed


class PushWithRetryTest(GitFixture):

    def two_clones(self):
        self.seed({"a.svg": "a1"})
        clones = self.clone("clone-a"), self.clone("clone-b")
        self.two_clones_identity(*clones)
        return clones

    def two_clones_identity(self, *clones):
        # Like the production callers (featured-from-pins, opencode-pr), which
        # set user.name/user.email in the checkout before they commit; the
        # rebase in a retry needs that identity too.
        for clone in clones:
            self.git("config", "user.name", "github-actions[bot]", cwd=clone)
            self.git("config", "user.email", "bot@example.invalid", cwd=clone)

    def commit(self, clone, name, content):
        (clone / name).write_text(content)
        self.git("add", name, cwd=clone)
        self.git("commit", "-qm", "%s in %s" % (name, clone.name), cwd=clone)

    def test_two_clones_pushing_alternately_both_land(self):
        a, b = self.two_clones()
        for round_no in range(2):
            self.commit(a, "a-%d.svg" % round_no, "a")
            self.commit(b, "b-%d.svg" % round_no, "b")
            code, out = self.push_with_retry(a)
            self.assertEqual(code, 0, out)
            code, out = self.push_with_retry(b)           # behind: must rebase
            self.assertEqual(code, 0, out)
            self.assertIn("(fetch first)", out)
            a, b = b, a                                   # the other one is behind next
        self.assertEqual(self.remote_files(),
                         ["a-0.svg", "a-1.svg", "a.svg", "b-0.svg", "b-1.svg"])
        merges = self.git("--git-dir", str(self.remote), "rev-list", "--merges", "output")
        self.assertEqual(merges, "")                      # linear, rebased, never merged

    def test_same_file_conflict_aborts_the_rebase_and_keeps_the_remote(self):
        a, b = self.two_clones()
        self.commit(a, "a.svg", "from a")
        self.commit(b, "a.svg", "from b")
        code, out = self.push_with_retry(a)
        self.assertEqual(code, 0, out)
        tip = self.tip()
        code, out = self.push_with_retry(b)
        self.assertEqual(code, 1, out)
        self.assertIn("::error::Rebase onto the new output failed", out)
        self.assertEqual(self.tip(), tip)
        self.assertEqual(self.remote_file("a.svg"), "from a")
        self.assertFalse((b / ".git" / "rebase-merge").exists())
        self.assertFalse((b / ".git" / "rebase-apply").exists())

    def test_attempts_are_bounded(self):
        a, _ = self.two_clones()
        self.commit(a, "x.svg", "x")
        # Every push loses: the hook always reports the production race text.
        self.install_hook("#!/bin/bash\necho \"cannot lock ref 'refs/heads/output'\" >&2\nexit 1\n")
        code, out = self.push_with_retry(a, PUSH_MAX_ATTEMPTS="3")
        self.assertEqual(code, 1, out)
        self.assertEqual(out.count("raced with a concurrent publisher"), 2, out)
        self.assertIn("lost the race 3 times in a row", out)

    def test_scripts_never_force_push(self):
        for script in (PUSH, PUBLISH):
            with self.subTest(script=script.name):
                code_lines = [line for line in script.read_text().splitlines()
                              if not line.lstrip().startswith("#")]
                text = "\n".join(code_lines)
                self.assertNotIn("--force", text)
                self.assertNotIn("+HEAD", text)
                self.assertNotIn("-X", text)


class MainRefGuardTest(GitFixture):
    """The main-branch commit step of the REAL workflows, run against a bare
    repository: a run on another ref must never move main (H6), a run on
    refs/heads/main pushes through the chokepoint and survives a race."""

    WORKFLOWS = SCRIPTS.parent / "workflows"

    def commit_step(self, name):
        # The README commit/push block, extracted verbatim from the workflow.
        lines = (self.WORKFLOWS / name).read_text(encoding="utf-8").splitlines()
        start = next(i for i, line in enumerate(lines)
                     if line.strip() == "if git diff --quiet -- README.md; then")
        indent = lines[start][:len(lines[start]) - len(lines[start].lstrip())]
        end = next(i for i in range(start + 1, len(lines)) if lines[i] == indent + "fi")
        body = textwrap.dedent("\n".join(lines[start:end + 1]))
        return "set -euo pipefail\n" + body + "\n"

    def prepare(self):
        work = self.tmp / "seed-main"
        self.git("init", "-q", "-b", "main", str(work))
        (work / "README.md").write_text("old\n")
        self.git("add", ".", cwd=work)
        self.git("commit", "-qm", "seed", cwd=work)
        self.git("remote", "add", "origin", self.remote_url, cwd=work)
        self.git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=work)
        clone = self.clone("run", branch="main")
        (clone / ".github" / "scripts").mkdir(parents=True)
        shutil.copy(PUSH, clone / ".github" / "scripts" / "push_with_retry.sh")
        (clone / "README.md").write_text("new\n")
        return clone

    def run_step(self, name, clone, ref):
        env = dict(self.env, GITHUB_REF=ref, GIT_AUTHOR_NAME="t",
                   GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@example.invalid")
        proc = subprocess.run(["bash", "-c", self.commit_step(name)], cwd=str(clone),
                              env=env, capture_output=True, text=True)
        return proc.returncode, proc.stdout + proc.stderr

    def check_other_ref(self, name):
        clone = self.prepare()
        before = self.tip("main")
        code, out = self.run_step(name, clone, "refs/heads/feature")
        self.assertEqual(code, 0, out)
        self.assertIn("::warning::Run is on refs/heads/feature", out)
        self.assertEqual(self.tip("main"), before)
        self.assertEqual(self.remote_file("README.md", branch="main"), "old")

    def check_main_with_race(self, name):
        clone = self.prepare()
        other = self.clone("other-main", branch="main")
        (other / "other.txt").write_text("other")
        self.git("add", "other.txt", cwd=other)
        self.git("commit", "-qm", "other", cwd=other)
        self.git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=other)
        code, out = self.run_step(name, clone, "refs/heads/main")
        self.assertEqual(code, 0, out)
        self.assertIn("(fetch first)", out)
        self.assertEqual(self.remote_file("README.md", branch="main"), "new")
        self.assertEqual(self.remote_file("other.txt", branch="main"), "other")

    def test_featured_run_on_another_ref_never_moves_main(self):
        self.check_other_ref("featured-from-pins.yml")

    def test_opencode_run_on_another_ref_never_moves_main(self):
        self.check_other_ref("opencode-pr.yml")

    def test_featured_run_on_main_survives_a_concurrent_commit(self):
        self.check_main_with_race("featured-from-pins.yml")

    def test_opencode_run_on_main_survives_a_concurrent_commit(self):
        self.check_main_with_race("opencode-pr.yml")


class WorkflowContractTest(unittest.TestCase):
    """Every push to a shared branch goes through the one chokepoint."""

    WORKFLOWS = SCRIPTS.parent / "workflows"

    def code_lines(self, path):
        return [line for line in path.read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith("#")]

    def test_no_workflow_pushes_around_the_chokepoint(self):
        for path in sorted(self.WORKFLOWS.glob("*.yml")):
            with self.subTest(workflow=path.name):
                text = "\n".join(self.code_lines(path))
                self.assertNotIn("git push", text)
                self.assertNotIn("crazy-max/ghaction-github-pages", text)
                self.assertNotIn("--force", text)

    def test_main_pushes_only_from_refs_heads_main(self):
        for name in ("featured-from-pins.yml", "opencode-pr.yml"):
            with self.subTest(workflow=name):
                lines = self.code_lines(self.WORKFLOWS / name)
                pushes = [i for i, line in enumerate(lines) if "push_with_retry.sh main" in line]
                self.assertEqual(len(pushes), 1)
                guard = [i for i, line in enumerate(lines)
                         if '"${GITHUB_REF}" != "refs/heads/main"' in line]
                self.assertEqual(len(guard), 1)
                self.assertLess(guard[0], pushes[0])

    def test_output_publishers_use_the_local_action(self):
        for name in ("snake.yml", "activity-composite.yml", "featured-from-pins.yml"):
            with self.subTest(workflow=name):
                text = (self.WORKFLOWS / name).read_text(encoding="utf-8")
                self.assertEqual(text.count("uses: ./.github/actions/publish-output"), 1)

if __name__ == "__main__":
    unittest.main()
