#!/usr/bin/env bash
# Rebase+retry push na runtime/evidence. Buyer a exit zapisují naráz.
set -euo pipefail
WORK="${1:?worktree path}"
MAX="${2:-6}"
ok=0
for attempt in $(seq 1 "$MAX"); do
  if git -C "$WORK" push origin HEAD:runtime/evidence; then
    ok=1
    break
  fi
  echo "runtime/evidence push attempt ${attempt}/${MAX} rejected; fetch and rebase"
  git fetch origin runtime/evidence:refs/remotes/origin/runtime/evidence
  git -C "$WORK" rebase origin/runtime/evidence
  sleep $((attempt * 2))
done
if [[ "$ok" -ne 1 ]]; then
  echo "FAIL-CLOSED: push na runtime/evidence selhal po ${MAX} pokusech" >&2
  exit 1
fi
