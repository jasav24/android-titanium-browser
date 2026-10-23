#!/usr/bin/env bash
# Prepare or update the durable automation state branch in a linked worktree.
#
# Usage:
#   state_branch.sh prepare <worktree-path> <branch>
#   state_branch.sh save    <worktree-path> <branch> <commit-message>
#
# The state branch is an orphan branch that only contains state.json, builds/,
# merges/ and reports/. It is never merged into main.

set -euo pipefail
export GIT_TERMINAL_PROMPT=0

command="${1:-}"
path="${2:-}"
branch="${3:-}"

if [[ -z "$command" || -z "$path" || -z "$branch" ]]; then
  echo "usage: $0 prepare|save <worktree-path> <branch> [message]" >&2
  exit 64
fi

repo="$(git rev-parse --show-toplevel)"
mkdir -p "$(dirname "$path")"

prepare() {
  git -C "$repo" worktree prune

  if [[ -e "$path" ]]; then
    if git -C "$path" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      echo "reusing existing state worktree at $path"
      return 0
    fi
    echo "state path exists but is not a Git worktree: $path" >&2
    exit 65
  fi

  if git -C "$repo" fetch --no-tags --quiet origin \
      "+refs/heads/${branch}:refs/remotes/origin/${branch}" 2>/dev/null; then
    echo "checked out existing remote state branch origin/${branch}"
    git -C "$repo" worktree add --detach "$path" "refs/remotes/origin/${branch}"
    return 0
  fi

  if git -C "$repo" show-ref --verify --quiet "refs/heads/${branch}"; then
    echo "remote state branch not found; reusing local ${branch}"
    git -C "$repo" worktree add --detach "$path" "refs/heads/${branch}"
    return 0
  fi

  echo "creating orphan state branch ${branch}"
  git -C "$repo" worktree add --detach "$path" HEAD
  git -C "$path" checkout --orphan "$branch"
  git -C "$path" rm -rf --ignore-unmatch .
  find "$path" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
}

save() {
  message="${4:-automation state update}"
  if ! git -C "$path" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "not a Git worktree: $path" >&2
    exit 65
  fi

  git -C "$path" add -A
  if git -C "$path" rev-parse --verify HEAD >/dev/null 2>&1; then
    if git -C "$path" diff --cached --quiet; then
      echo "state branch has no changes"
      return 0
    fi
  fi

  git -C "$path" \
    -c user.name="automation-bot" \
    -c user.email="automation-bot@users.noreply.github.com" \
    commit -m "$message"

  for attempt in 1 2 3 4 5; do
    if git -C "$path" push --porcelain origin "HEAD:refs/heads/${branch}"; then
      echo "pushed state branch ${branch}"
      return 0
    fi
    echo "state push attempt ${attempt} failed; rebasing on origin/${branch}" >&2
    git -C "$path" fetch --no-tags --quiet origin \
      "+refs/heads/${branch}:refs/remotes/origin/${branch}" || true
    if git -C "$path" show-ref --verify --quiet "refs/remotes/origin/${branch}"; then
      if ! git -C "$path" rebase "refs/remotes/origin/${branch}"; then
        git -C "$path" rebase --abort || true
        echo "could not rebase state branch; concurrent update needs manual repair" >&2
        exit 66
      fi
    fi
  done
  echo "failed to push state branch after 5 attempts" >&2
  exit 67
}

case "$command" in
  prepare) prepare ;;
  save) save "$@" ;;
  *)
    echo "unknown command: $command" >&2
    exit 64
    ;;
esac
