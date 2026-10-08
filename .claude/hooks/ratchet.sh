#!/usr/bin/env bash
# Stop hook: block finishing if any corpus file scores below the shared baseline.
# Skips when src/ is identical to main (e.g. plain chat in the main checkout).
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" || exit 0
cat > /dev/null  # hook input JSON (unused)
if git rev-parse --verify -q main >/dev/null && git diff --quiet main -- src 2>/dev/null \
   && [ -z "$(git ls-files --others --exclude-standard src)" ]; then
  exit 0
fi
out=$(python3 oracle/compare.py --check --quiet --label hook 2>&1)
rc=$?
if [ $rc -ne 0 ]; then
  echo "$out" | tail -40 >&2
  echo "Do not finish yet: fix the regressions above (python3 oracle/compare.py --diff <id>)." >&2
  exit 2
fi
exit 0
