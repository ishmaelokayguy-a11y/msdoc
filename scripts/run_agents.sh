#!/usr/bin/env bash
# Launch headless Claude Code agents, each in its own git worktree + branch.
#
#   scripts/run_agents.sh wave1                 # cfb, fib, pieces/props        (parallel)
#   scripts/run_agents.sh wave2                 # stories, inline + floating images
#   scripts/run_agents.sh robust                # robustness / fuzzing
#   scripts/run_agents.sh fix [focus ...]       # one fixer per focus (default list below)
#   scripts/run_agents.sh review agent/cfb ...  # independent reviewer per branch
#   scripts/run_agents.sh one NAME PROMPT_FILE  # any single prompt
#   scripts/run_agents.sh resume [NAME ...]     # continue interrupted agents (e.g. after a
#                                               # usage limit) in their own session, with context
#
# Env: MODEL=opus|sonnet (optional), MAX_PARALLEL (default 3), CLAUDE_ARGS (extra flags).
# Logs: logs/<name>.jsonl (stream-json, appended on resume); live view: scripts/watch.py
# When an agent exits for any reason, uncommitted changes in its worktree are
# committed as "WIP (auto-commit ...)" so no work is lost.
set -eo pipefail   # (no -u: macOS ships bash 3.2)
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"
mkdir -p logs
command -v claude >/dev/null || { echo "claude CLI not found"; exit 1; }
git rev-parse --verify -q main >/dev/null || { echo "need a 'main' branch with at least one commit"; exit 1; }
MAX_PARALLEL=${MAX_PARALLEL:-3}

wait_slot() { while [ "$(jobs -rp | wc -l)" -ge "$MAX_PARALLEL" ]; do sleep 5; done; }

autocommit() {  # name
  local wt=".claude/worktrees/$1"
  if [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]; then
    git -C "$wt" add -A && git -C "$wt" commit -q -m "WIP (auto-commit after agent '$1' exited)" \
      && echo "  ($1: uncommitted work saved as WIP commit)"
  fi
}

run_claude() {  # name logmode(>|>>) claude-args...
  local name=$1 mode=$2; shift 2
  local wt=".claude/worktrees/$name"
  ( set +e; cd "$wt"; rc=0
    if [ "$mode" = ">>" ]; then
      claude "$@" --permission-mode acceptEdits ${MODEL:+--model "$MODEL"} --output-format stream-json --verbose \
        ${CLAUDE_ARGS:-} >> "$ROOT/logs/$name.jsonl" 2>> "$ROOT/logs/$name.err" || rc=$?
    else
      claude "$@" --permission-mode acceptEdits ${MODEL:+--model "$MODEL"} --output-format stream-json --verbose \
        ${CLAUDE_ARGS:-} > "$ROOT/logs/$name.jsonl" 2> "$ROOT/logs/$name.err" || rc=$?
    fi
    cd "$ROOT"; autocommit "$name"
    echo "✓ $name finished (exit $rc)" ) &
}

launch() {  # name prompt_file [KEY=VALUE substitutions...]
  local name=$1 file=$2; shift 2
  local wt=".claude/worktrees/$name" br="agent/$name"
  if [ ! -d "$wt" ]; then
    if git rev-parse --verify -q "$br" >/dev/null; then git worktree add -q "$wt" "$br"
    else git worktree add -q -b "$br" "$wt" main; fi
  fi
  local prompt; prompt=$(cat "$file")
  for kv in "$@"; do prompt=${prompt//"{{${kv%%=*}}}"/"${kv#*=}"}; done
  prompt="$prompt

(You are agent '$name' working in git worktree $wt on branch $br.
Commit on this branch after every step that passes its tests — at least every
20-30 minutes of work. Your run can be cut off at any moment (usage limits),
and uncommitted work is only saved as an unreviewed WIP commit.)"
  wait_slot
  echo "→ $name ($file) in $wt"
  run_claude "$name" ">" -p "$prompt"
}

session_id() {  # name -> last session_id in its log
  python3 - "$ROOT/logs/$1.jsonl" <<'PY'
import json, sys
sid = ""
try:
    for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
        try: ev = json.loads(line)
        except ValueError: continue
        sid = ev.get("session_id") or sid
except FileNotFoundError:
    pass
print(sid)
PY
}

resume() {  # name
  local name=$1 sid; sid=$(session_id "$name")
  if [ -z "$sid" ] || [ ! -d ".claude/worktrees/$name" ]; then echo "· $name: no session/worktree to resume"; return; fi
  wait_slot
  echo "↻ $name (session $sid)"
  run_claude "$name" ">>" -p --resume "$sid" "You were interrupted (usage limit or crash). Continue the task from where you stopped.
First run 'git status' and 'git log --oneline main..HEAD': if there is a 'WIP (auto-commit ...)' commit,
check that work, fix it if needed and commit properly. Then continue. Commit after every passing step."
}

case "${1:-}" in
  wave1)  launch cfb prompts/10-cfb.md; launch fib prompts/11-fib.md; launch text prompts/12-text.md ;;
  wave2)  launch stories prompts/13-stories.md; launch inline-images prompts/14-inline-images.md
          launch floating-images prompts/15-floating-images.md ;;
  robust) launch robustness prompts/20-robustness.md ;;
  fix)    shift
          if [ $# -eq 0 ]; then set -- \
            "fields: field codes vs results, nested fields, field-like special characters" \
            "tables: cell and row marks, nested tables, empty cells" \
            "headers and footers: sections, inherited headers, header text boxes" \
            "notes and comments: footnotes, endnotes, annotations" \
            "inline images" "floating images and groups"; fi
          i=0; for f in "$@"; do i=$((i+1)); launch "fix-$i" prompts/30-fixer.md "FOCUS=$f"; done ;;
  review) shift; for b in "$@"; do slug=${b//\//-}; launch "review-$slug" prompts/90-reviewer.md "BRANCH=$b" "BRANCH_SLUG=$slug"; done ;;
  one)    launch "$2" "$3" ;;
  resume) shift
          if [ $# -eq 0 ]; then set -- $(ls .claude/worktrees 2>/dev/null); fi
          for n in "$@"; do resume "$n"; done ;;
  *)      sed -n '2,16p' "$0"; exit 1 ;;
esac
wait
echo "All agents finished. Inspect: scripts/status.sh ; merge: scripts/integrate.sh"
