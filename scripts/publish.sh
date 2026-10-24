#!/usr/bin/env bash
# Create a GitHub repository for this project and push the history to it.
#
#   scripts/publish.sh <github-owner>/<repo> [--private]
#
# Requires either the `gh` CLI (authenticated) or an existing remote plus a
# working credential helper. The script never stores a token in the repository.
set -euo pipefail

REPO_SPEC="${1:-}"
VISIBILITY="--public"
if [ "${2:-}" = "--private" ]; then
  VISIBILITY="--private"
fi

if [ -z "$REPO_SPEC" ]; then
  echo "usage: scripts/publish.sh <owner>/<repo> [--private]" >&2
  exit 2
fi

cd "$(dirname "$0")/.."

# 1. the tree must be clean and tested
if [ -n "$(git status --porcelain)" ]; then
  echo "working tree is not clean; commit or stash first" >&2
  git status --short
  exit 1
fi
python3 -m pytest tests/ -q

# 2. git identity (a fresh clone of this workspace may not have one)
git config user.name  >/dev/null 2>&1 || git config user.name  "silicon"
git config user.email >/dev/null 2>&1 || git config user.email "silicon@localhost"

# 3. create the remote repository
if git remote get-url origin >/dev/null 2>&1; then
  echo "remote 'origin' already exists: $(git remote get-url origin)"
elif command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  gh repo create "$REPO_SPEC" "$VISIBILITY" --source=. --remote=origin --push
  echo "pushed to https://github.com/$REPO_SPEC"
  exit 0
else
  echo "no 'origin' remote and no authenticated gh CLI." >&2
  echo "Create the repository on GitHub, then run:" >&2
  echo "  git remote add origin git@github.com:$REPO_SPEC.git" >&2
  echo "  git push -u origin main" >&2
  exit 1
fi

# 4. push
git push -u origin main
echo "pushed to $(git remote get-url origin)"
