#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# publish_output.sh  ·  Additive publish of a directory to a shared branch
# ---------------------------------------------------------------------------
# Used by .github/actions/publish-output for every workflow that publishes to
# the shared `output` branch (snake, activity-composite, featured-from-pins).
# It replaces crazy-max/ghaction-github-pages@v5 with `keep_history: true` and
# `allow_empty_commit: false`, and keeps that semantics 1:1:
#   * the existing branch is cloned (depth 1) and the build dir is copied ON
#     TOP of it: files of other publishers are never removed (no prune), a
#     file of the same name is overwritten with the new content;
#   * no change -> no commit ("No changes to commit"), exit 0;
#   * committer `GitHub <noreply@github.com>`, author `github-actions[bot]`;
#   * the push is never forced; a missing branch is created as an orphan.
# What crazy-max does NOT do and this does: a push that loses the race
# against a concurrent publisher is rebased onto the new tip and retried
# (push_with_retry.sh, the one chokepoint shared with the `main` pushes).
#
# Environment:
#   PUBLISH_BRANCH      target branch (required)
#   PUBLISH_SOURCE_DIR  directory whose CONTENTS are published (required)
#   PUBLISH_MESSAGE     commit message (required)
#   PUBLISH_TOKEN       token for github.com (optional; without it no auth)
#   PUBLISH_REMOTE      remote URL (optional, default
#                       https://github.com/$GITHUB_REPOSITORY.git; the tests
#                       point it at a local bare repository)
#
# TOKEN HANDLING: the token is never part of a URL, never written to a file
# and never printed. It travels as an http extraheader that exists only in
# the environment of this process (GIT_CONFIG_COUNT/KEY/VALUE), and its
# base64 form is masked in the Actions log before it is used.
# ---------------------------------------------------------------------------
set -euo pipefail
set +x

branch="${PUBLISH_BRANCH:?PUBLISH_BRANCH is required}"
message="${PUBLISH_MESSAGE:?PUBLISH_MESSAGE is required}"
source_dir="${PUBLISH_SOURCE_DIR:?PUBLISH_SOURCE_DIR is required}"
remote="${PUBLISH_REMOTE:-https://github.com/${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}.git}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export GIT_TERMINAL_PROMPT=0

if [ ! -d "$source_dir" ]; then
  echo "::error::Build dir '${source_dir}' does not exist; nothing to publish."
  exit 1
fi
source_dir="$(cd "$source_dir" && pwd)"

if [ -n "${PUBLISH_TOKEN:-}" ]; then
  basic="$(printf 'x-access-token:%s' "$PUBLISH_TOKEN" | base64 | tr -d '\n')"
  echo "::add-mask::${basic}"
  export GIT_CONFIG_COUNT=1
  export GIT_CONFIG_KEY_0="http.https://github.com/.extraheader"
  export GIT_CONFIG_VALUE_0="AUTHORIZATION: basic ${basic}"
  unset basic
fi

# Same identities as crazy-max, also used for the rebase in a retry.
export GIT_COMMITTER_NAME="GitHub"
export GIT_COMMITTER_EMAIL="noreply@github.com"
export GIT_AUTHOR_NAME="github-actions[bot]"
export GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com"

work="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/publish-output.XXXXXX")"
trap 'rm -rf "$work"' EXIT

# Every git command below runs in the scratch dir, never in the caller's
# checkout: actions/checkout attaches its persisted token to the checkout
# (includeIf.gitdir), and a git command run there would send that
# Authorization header next to ours, which GitHub rejects as a duplicate.
cd "$work"

set +e
git ls-remote --exit-code --heads "$remote" "refs/heads/${branch}" >/dev/null
rc=$?
set -e
if [ "$rc" -eq 0 ]; then
  echo "Cloning ${branch} (depth 1)."
  git clone --quiet --depth 1 --branch "$branch" "$remote" .
elif [ "$rc" -eq 2 ]; then
  echo "Branch ${branch} does not exist yet; creating it as an orphan."
  git init --quiet .
  git checkout --quiet --orphan "$branch"
  git remote add origin "$remote"
else
  echo "::error::Could not read the remote branch list (git ls-remote exit ${rc}); not publishing."
  exit 1
fi

# Additive copy: new and changed files land, nothing is ever deleted.
cp -R "${source_dir}/." .

git add --all .
if git diff --cached --quiet; then
  echo "No changes to commit."
  exit 0
fi
git commit --quiet -m "$message"
bash "${here}/push_with_retry.sh" "$branch"
