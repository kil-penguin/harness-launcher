#!/usr/bin/env bash
# Real isolated-session lifecycle: create writes the manifest, GC repoints a
# collecting session to its archive and keeps a personal exclusion (P6).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ISOLATION="$ROOT/bin/session-isolation.sh"
TMP="$(cd "$(mktemp -d)" && pwd -P)"
trap 'chmod -R u+w "$TMP" 2>/dev/null; rm -rf "$TMP"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

export HOME="$TMP/home"
mkdir -p "$HOME"
export HARNESS_AGENT_USAGE_TEST_MODE=1
unset HARNESS_AGENT_USAGE_STATE_DIR  # the manifests live under this test HOME
GUARD="$TMP/guard"
export HARNESS_AGENT_USAGE_GUARD_DIR="$GUARD" HARNESS_AGENT_USAGE_TEST_OWNER_UID="$(id -u)"
export HARNESS_SESSION_STATE_HOME="$HOME/.local/state/harness-launcher" HARNESS_SESSION_RETENTION_SECONDS=0
unset CLAUDE_CONFIG_DIR HARNESS_PYTHON_BIN
mkdir -p "$GUARD/$(id -u)"; chmod 755 "$GUARD" "$GUARD/$(id -u)"; : > "$GUARD/$(id -u)/agent-assertion.jwt"
printf '#!/bin/sh\nexit 1\n' > "$TMP/native-unavailable"; chmod 700 "$TMP/native-unavailable"
export HARNESS_CODEX_BIN="$TMP/native-unavailable"
STATE="$HARNESS_SESSION_STATE_HOME"; COLL="$HOME/.agent-usage/collection.d"
source "$ROOT/bin/harness-common.sh"
PY="$(harness_python3_resolve)"
# session-isolation.sh shells out to a bare python3; pin it to the resolved interpreter.
mkdir -p "$TMP/bin"; ln -s "$PY" "$TMP/bin/python3"; export PATH="$TMP/bin:$PATH"
j() { "$PY" -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$1" "$2"; }

make_source() { # <dir> <collect 0|1>
  mkdir -p "$1/config"
  git -C "$1" init -q -b main
  { printf 'HARNESS_NAME="s"\nHARNESS_PREFIX="s"\n'; [[ "$2" != 1 ]] || printf 'HARNESS_AGENT_USAGE_COLLECT="1"\n'; } > "$1/config/launcher.env"
  printf '.harness/\n' > "$1/.gitignore"
  git -C "$1" add -A
  git -C "$1" -c user.name=t -c user.email=t@example.invalid commit -q -m init
}
make_source "$HOME/comp" 1; make_source "$HOME/pers" 0

new_session() { # <source> -> sets ID WORK
  local out
  out="$("$ISOLATION" create "$1")"
  ID="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
  WORK="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
}

# create writes the manifest before the agent starts
new_session "$HOME/comp"; COMP_ID="$ID"; COMP_WORK="$WORK"
LABEL="hl-$(printf '%s' "$COMP_ID" | tr -d - | cut -c1-12 | tr 'A-F' 'a-f')"
[[ "$(j "$COLL/harness-launcher-$(printf '%s' "$COMP_ID" | tr 'A-F' 'a-f').json" "[i['label'] for i in d['include']]")" == "['$LABEL', '$LABEL-a']" ]] \
  || fail 'create must write the collecting session manifest'
new_session "$HOME/pers"; PERS_ID="$ID"; PERS_WORK="$WORK"
PERS_M="$COLL/harness-launcher-$(printf '%s' "$PERS_ID" | tr 'A-F' 'a-f').json"
[[ "$(j "$PERS_M" "len(d['exclude'])")" == 2 && "$(j "$PERS_M" "d['include']")" == '[]' ]] || fail 'create must write the personal exclusion manifest'
ENC="$("$PY" -I "$ROOT/bin/harness_agent_usage.py" encode "$PERS_WORK")"
mkdir -p "$HOME/.claude/projects/$ENC"

# Real GC (sessions that never ran Codex retire through the archive path):
# the collecting session has nothing left to collect, the personal exclusion
# stays while its Claude transcripts exist.
"$ISOLATION" transition "$COMP_ID" CLOSED; "$ISOLATION" transition "$PERS_ID" CLOSED
"$ISOLATION" gc
[[ ! -e "$COMP_WORK" && ! -e "$PERS_WORK" ]] || fail 'precondition: GC retired both worktrees'
COMP_M="$COLL/harness-launcher-$(printf '%s' "$COMP_ID" | tr 'A-F' 'a-f').json"
[[ ! -e "$COMP_M" ]] || fail 'GC must drop a collecting manifest that has no archived Codex data'
[[ -f "$PERS_M" && "$(j "$PERS_M" "d['include']")" == '[]' ]] || fail 'personal exclusion must outlive the worktree while Claude transcripts exist'

# Archive layout the manifest relies on: <archive>/codex/{sessions,archived_sessions}
new_session "$HOME/comp"; COMP2_ID="$ID"; COMP2_WORK="$WORK"
LABEL2="hl-$(printf '%s' "$COMP2_ID" | tr -d - | cut -c1-12 | tr 'A-F' 'a-f')"
COMP2_M="$COLL/harness-launcher-$(printf '%s' "$COMP2_ID" | tr 'A-F' 'a-f').json"
mkdir -p "$COMP2_WORK/.harness/codex/sessions" "$COMP2_WORK/.harness/codex/archived_sessions"
printf 'original\n' > "$COMP2_WORK/.harness/codex/sessions/current.jsonl"
printf 'old\n' > "$COMP2_WORK/.harness/codex/archived_sessions/old.jsonl"
"$ISOLATION" transition "$COMP2_ID" CLOSED
"$PY" -I "$ROOT/bin/harness_session_archive.py" archive --root "$COMP2_WORK" --state "$STATE" --owner "$COMP2_ID" --source "$HOME/comp"
[[ -f "$STATE/archives/$COMP2_ID/codex/sessions/current.jsonl" ]] || fail 'archive layout assumption (<archive>/codex/sessions) broken'
rm -rf "$COMP2_WORK"
harness_agent_usage_sync_session "$ROOT/bin" "$COMP2_ID" "$HOME/comp"
[[ "$(j "$COMP2_M" "[(i['root'], i['label']) for i in d['include']]")" == "[('$STATE/archives/$COMP2_ID/codex/sessions', '$LABEL2'), ('$STATE/archives/$COMP2_ID/codex/archived_sessions', '$LABEL2-a')]" ]] \
  || fail "retired collecting session must repoint both roots to the real archive: $(cat "$COMP2_M")"

echo 'PASS: session create/GC keep the collection manifests in step'
