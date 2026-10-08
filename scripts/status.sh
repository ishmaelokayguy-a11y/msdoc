#!/usr/bin/env bash
# Show agent branches, their commits ahead of main, and the final result line of each log.
cd "$(git rev-parse --show-toplevel)"
for br in $(git for-each-ref --format='%(refname:short)' refs/heads/agent/); do
  name=${br#agent/}
  ahead=$(git rev-list --count main.."$br")
  printf '%-28s +%-3s commits  ' "$br" "$ahead"
  if [ -f "logs/$name.jsonl" ]; then
    python3 - "logs/$name.jsonl" <<'PY'
import json, sys
last = None
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    try: ev = json.loads(line)
    except Exception: continue
    if ev.get("type") == "result": last = ev
if last:
    cost = last.get("total_cost_usd")
    print(f"done  turns={last.get('num_turns')} " + (f"${cost:.2f} " if cost else "") + (last.get("result") or "")[:200].replace("\n", " "))
else:
    print("running…")
PY
  else echo "no log"; fi
done
