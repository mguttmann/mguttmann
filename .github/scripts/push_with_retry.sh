#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# push_with_retry.sh BRANCH  ·  THE push chokepoint for shared branches
# ---------------------------------------------------------------------------
# Pushes HEAD of the current repository to `origin` BRANCH and survives a
# concurrent publisher: several workflows push to `main` (featured-from-pins,
# opencode-pr) and to `output` (snake, activity-composite, featured-from-pins)
# in separate concurrency groups, so two of them can push within the same
# second. Measured 2026-09-27: run 36358655683 failed with
#   ! [remote rejected] output -> output (cannot lock ref 'refs/heads/output':
#   is at d0d6c1d... but expected 5adeed8...)
#
# Behaviour:
#   * A push is NEVER forced (no --force, no --force-with-lease): whatever the
#     other publisher pushed stays.
#   * Only a push rejected with a RACE signature is retried: the remote moved
#     between our fetch and our push. Then: pause (attempt x PUSH_RETRY_PAUSE
#     seconds), fetch BRANCH, rebase our commits onto it, push again. At most
#     PUSH_MAX_ATTEMPTS pushes in total.
#   * A real conflict (both sides changed the same lines of the same file)
#     aborts the rebase and fails LOUD (::error::, exit 1). Nothing is resolved
#     automatically, nothing is overwritten.
#   * Any other rejection (authentication, a protected branch, the network)
#     fails at once with ::error::, exit 1, without a retry.
#
# The remote URL carries no credentials; authentication comes from the caller
# (persisted checkout credentials, or an extraheader in the process env), and
# the git output printed here never contains a token.
#
# Environment (optional):
#   PUSH_MAX_ATTEMPTS  default 5
#   PUSH_RETRY_PAUSE   default 5 (seconds; the n-th retry waits n x this)
# ---------------------------------------------------------------------------
set -euo pipefail
set +x

branch="${1:?usage: push_with_retry.sh BRANCH}"
max_attempts="${PUSH_MAX_ATTEMPTS:-5}"
pause="${PUSH_RETRY_PAUSE:-5}"
export GIT_TERMINAL_PROMPT=0

# The rejections that mean "the remote moved under us", and nothing else.
race_signatures='cannot lock ref|incorrect old value provided|failed to update ref|\(fetch first\)|\(non-fast-forward\)|stale info'

err_file="$(mktemp)"
trap 'rm -f "$err_file"' EXIT

attempt=1
while :; do
  if git push origin "HEAD:refs/heads/${branch}" 2>"$err_file"; then
    cat "$err_file" >&2
    echo "Pushed to ${branch} (attempt ${attempt} of ${max_attempts})."
    exit 0
  fi
  cat "$err_file" >&2

  if ! grep -Eq "$race_signatures" "$err_file"; then
    echo "::error::Push to ${branch} was rejected for a reason that is not a concurrent update; not retrying."
    exit 1
  fi
  if [ "$attempt" -ge "$max_attempts" ]; then
    echo "::error::Push to ${branch} lost the race ${attempt} times in a row; giving up. Nothing was overwritten."
    exit 1
  fi

  wait_s=$((attempt * pause))
  echo "::warning::Push to ${branch} raced with a concurrent publisher (attempt ${attempt} of ${max_attempts}); rebasing onto the new tip in ${wait_s}s."
  sleep "$wait_s"
  git fetch --no-tags origin "refs/heads/${branch}"
  if ! git rebase FETCH_HEAD; then
    git rebase --abort || true
    echo "::error::Rebase onto the new ${branch} failed: a concurrent change touched the same file. Nothing was pushed, nothing was overwritten."
    exit 1
  fi
  attempt=$((attempt + 1))
done
