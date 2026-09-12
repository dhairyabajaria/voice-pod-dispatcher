#!/bin/sh
# tests/fake_git.sh -- stand-in for git. Creates directories for `worktree add`
# and prints a fixed sha for `rev-parse`. Touches no real repository.
set -u
LOG="${VP_FAKE_GIT_LOG:-/dev/null}"
echo "$*" >> "$LOG"

if [ "${1:-}" = "-C" ]; then
  shift
  shift
fi

case "${1:-}" in
  worktree)
    shift
    if [ "${1:-}" = "add" ]; then
      shift
      mkdir -p "$1"
    fi
    exit 0
    ;;
  rev-parse)
    echo "abcdef1234567890abcdef1234567890abcdef12"
    exit 0
    ;;
esac
exit 0
