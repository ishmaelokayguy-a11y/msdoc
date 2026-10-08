#!/usr/bin/env bash
# Merge agent branches into main one by one, keeping only merges that pass
# tests and the ratchet; then raise the baseline. Run in the MAIN checkout.
#
#   scripts/integrate.sh                    # all agent/* branches except reviews
#   scripts/integrate.sh agent/cfb agent/fib
set -o pipefail   # (no -u: macOS ships bash 3.2)
cd "$(git rev-parse --show-toplevel)"
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || { echo "checkout main first"; exit 1; }
git diff --quiet && git diff --cached --quiet || { echo "main has uncommitted changes"; exit 1; }
if [ $# -eq 0 ]; then
  set -- $(git for-each-ref --format='%(refname:short)' refs/heads/agent/ | grep -v '^agent/review-')
fi
[ $# -eq 0 ] && { echo "no agent/* branches"; exit 0; }
for br in "$@"; do
  [ "$(git rev-list --count main.."$br")" -eq 0 ] && { echo "· $br: nothing new"; continue; }
  echo "=== $br"
  if ! git merge --no-ff --no-edit "$br" >/dev/null 2>&1; then
    echo "  ✗ merge conflict — left for manual resolution:"; git diff --name-only --diff-filter=U | sed 's/^/    /'
    git merge --abort; continue
  fi
  if python3 -m pytest -q -x >/tmp/msdoc-pytest.log 2>&1 && python3 oracle/compare.py --check --quiet; then
    echo "  ✓ merged"
  else
    echo "  ✗ tests or ratchet failed — merge reverted (see /tmp/msdoc-pytest.log, reports/latest.md)"
    git reset -q --hard ORIG_HEAD
  fi
done
python3 oracle/compare.py --update-baseline --quiet
echo "Agent branches can now be rebased on main:  git -C .claude/worktrees/<name> rebase main"
