#!/usr/bin/env bash
# collection.d manifests: writing rules (P4), reconcile over sessions and every
# registered harness (P5), retirement of a worktree (P6).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(cd "$(mktemp -d)" && pwd -P)"
trap '[[ -z "${HOLDER:-}" ]] || { kill "$HOLDER" 2>/dev/null || true; }; chmod -R u+w "$TMP" 2>/dev/null; rm -rf "$TMP"' EXIT

export HOME="$TMP/home"
mkdir -p "$HOME"
unset CLAUDE_CONFIG_DIR
export HARNESS_AGENT_USAGE_TEST_MODE=1
unset HARNESS_AGENT_USAGE_STATE_DIR  # the manifests live under this test HOME
GUARD="$TMP/guard"
export HARNESS_AGENT_USAGE_GUARD_DIR="$GUARD"
export HARNESS_AGENT_USAGE_TEST_OWNER_UID="$(id -u)"
export HARNESS_SESSION_STATE_HOME="$HOME/.local/state/harness-launcher"
STATE="$HARNESS_SESSION_STATE_HOME"
COLL="$HOME/.agent-usage/collection.d"
unset HARNESS_PYTHON_BIN HARNESS_SESSION_ROOT HARNESS_SESSION_ID HARNESS_SOURCE_ROOT HARNESS_RUN_DIR

# shellcheck source=../bin/harness-common.sh
source "$ROOT/bin/harness-common.sh"
PY="$(harness_python3_resolve)"
fail() { echo "FAIL: $*" >&2; exit 1; }

mkdir -p "$GUARD/$(id -u)"; chmod 755 "$GUARD" "$GUARD/$(id -u)"; : > "$GUARD/$(id -u)/agent-assertion.jwt"

make_harness() { # <dir> <prefix> <collect 0|1>
  mkdir -p "$1/config/.local"
  { printf 'HARNESS_NAME="%s"\nHARNESS_PREFIX="%s"\n' "$2" "$2"; [[ "$3" != 1 ]] || printf 'HARNESS_AGENT_USAGE_COLLECT="1"\n'; } > "$1/config/launcher.env"
}
make_session() { # <uuid> <source> -> creates record + worktree
  mkdir -p "$STATE/sessions/$1" "$STATE/worktrees/$1/.harness/codex/sessions"
  printf '%s\n' "$2" > "$STATE/sessions/$1/source-root"
  printf '%s\n' "$STATE/worktrees/$1" > "$STATE/sessions/$1/session-root"
}
j() { "$PY" -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$1" "$2"; }
inode() { "$PY" -I -c 'import os,sys; s=os.stat(sys.argv[1]); print(s.st_ino, s.st_mtime_ns)' "$1"; }

COMP="$HOME/comp-harness"; PERS="$HOME/pers-harness"
make_harness "$COMP" comp 1; make_harness "$PERS" pers 0
ID1=0f3a9c12-ab34-4cde-8f01-aaaaaaaaaaaa   # source COMP (collecting)
ID2=1b2c3d4e-5f60-4718-9a0b-bbbbbbbbbbbb   # source PERS (personal)
make_session "$ID1" "$COMP"; make_session "$ID2" "$PERS"

# ---- P4/P5 reconcile ------------------------------------------------------
[[ ! -e "$HOME/.agent-usage" ]] || fail 'precondition'
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ -d "$COLL" ]] || fail 'collection.d must always be created'
[[ "$(stat -f %Lp "$HOME/.agent-usage")" == 700 && "$(stat -f %Lp "$COLL")" == 700 ]] || fail 'agent-usage dirs must be 0700'
M1="$COLL/harness-launcher-$ID1.json"; M2="$COLL/harness-launcher-$ID2.json"
[[ -f "$M1" && -f "$M2" ]] || fail 'every session gets a manifest'
[[ "$(stat -f %Lp "$M1")" == 600 ]] || fail 'manifest must be 0600'
[[ "$(j "$M1" "d['schema']")" == 1 ]] || fail 'schema 1'
[[ "$(j "$M1" "[(i['provider'], i['root'], i['label']) for i in d['include']]")" == "[('codex', '$STATE/worktrees/$ID1/.harness/codex/sessions', 'hl-0f3a9c12ab34'), ('codex', '$STATE/worktrees/$ID1/.harness/codex/archived_sessions', 'hl-0f3a9c12ab34-a')]" ]] \
  || fail "collecting session include roots: $(cat "$M1")"
[[ "$(j "$M1" "d['exclude']")" == '[]' ]] || fail 'collecting session has no exclusions'
ENC2="$("$PY" -I "$ROOT/bin/harness_agent_usage.py" encode "$STATE/worktrees/$ID2")"
[[ "$(j "$M2" "d['include']")" == '[]' ]] || fail 'personal session must not include anything'
[[ "$(j "$M2" "d['exclude']")" == "[{'path': '$STATE/worktrees/$ID2/.harness/codex'}, {'projectDirPrefix': '$ENC2', 'provider': 'claude'}]" ]] \
  || fail "personal session exclusions: $(cat "$M2")"
PPERS="$COLL/harness-launcher-profile-pers.json"
ENCPERS="$("$PY" -I "$ROOT/bin/harness_agent_usage.py" encode "$PERS")"
[[ "$(j "$PPERS" "d['exclude']")" == "[{'path': '$PERS/.harness/codex'}, {'projectDirPrefix': '$ENCPERS', 'provider': 'claude'}]" ]] \
  || fail "personal profile manifest: $(cat "$PPERS")"
[[ ! -e "$COLL/harness-launcher-profile-comp.json" ]] || fail 'collecting profile must have no profile manifest'
[[ -z "$(ls -A "$COLL" | grep -v '^harness-launcher-.*\.json$' || true)" ]] || fail 'no temp files may remain'

# unchanged content is not rewritten
before="$(inode "$M1") $(inode "$M2") $(inode "$PPERS")"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ "$before" == "$(inode "$M1") $(inode "$M2") $(inode "$PPERS")" ]] || fail 'unchanged manifests must not be rewritten'
# a changed one is replaced atomically (new inode) and stays 0600
chmod 644 "$M1"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ "$(stat -f %Lp "$M1")" == 600 ]] || fail 'a loose manifest must be repaired to 0600'

# a registered harness with a real path that differs from the registered one
LINK="$TMP/pers-link"; ln -s "$PERS" "$LINK"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$LINK"
[[ "$(j "$PPERS" "len(d['exclude'])")" == 4 ]] || fail 'realpath variant exclusions expected for a linked registration'
[[ "$(j "$PPERS" "[e.get('path') for e in d['exclude'] if 'path' in e]")" == "['$LINK/.harness/codex', '$PERS/.harness/codex']" ]] || fail "linked variants: $(cat "$PPERS")"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"

# an unregistered source root is still judged by its own tracked launcher.env
# (read from the session record), never by the registry
make_harness "$HOME/stray" stray 0
ID3=2c3d4e5f-6071-4829-8a1b-cccccccccccc; make_session "$ID3" "$HOME/stray"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ "$(j "$COLL/harness-launcher-$ID3.json" "d['include']")" == '[]' && "$(j "$COLL/harness-launcher-$ID3.json" "len(d['exclude'])")" == 2 ]] || fail 'non-collecting unregistered source must be excluded'
[[ -f "$COLL/harness-launcher-profile-stray.json" ]] || fail 'a session source root evaluated inactive gets its profile manifest'
make_harness "$HOME/stray" stray 1
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ "$(j "$COLL/harness-launcher-$ID3.json" "len(d['include'])")" == 2 ]] || fail 'collecting unregistered source must be included'
rm -rf "$STATE/sessions/$ID3" "$STATE/worktrees/$ID3"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ ! -e "$COLL/harness-launcher-$ID3.json" ]] || fail 'manifest of a vanished record without transcripts must be removed'

# flipping the profile flips the manifests
make_harness "$COMP" comp 0
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ "$(j "$M1" "d['include']")" == '[]' && -f "$COLL/harness-launcher-profile-comp.json" ]] || fail 'profile turned off: exclude manifests expected'
make_harness "$COMP" comp 1
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ "$(j "$M1" "len(d['include'])")" == 2 && ! -e "$COLL/harness-launcher-profile-comp.json" ]] || fail 'profile turned on: include manifest, no profile exclusion'

# non-collecting machine: collecting profile degrades to exclusion
rm -rf "$GUARD"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ "$(j "$M1" "d['include']")" == '[]' ]] || fail 'a non-company device must not include roots'
mkdir -p "$GUARD/$(id -u)"; chmod 755 "$GUARD" "$GUARD/$(id -u)"; : > "$GUARD/$(id -u)/agent-assertion.jwt"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"

# manifest entries obey the collector's validation
"$PY" -I - "$M1" "$M2" "$PPERS" <<'PY' || fail 'manifest entry validation'
import json, re, sys, os
for path in sys.argv[1:]:
    d = json.load(open(path))
    assert d["schema"] == 1 and set(d) <= {"schema", "include", "exclude"}
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,128}\.json", os.path.basename(path))
    assert os.path.getsize(path) <= 65536
    for i in d["include"]:
        assert i["provider"] == "codex" and os.path.isabs(i["root"]) and os.path.realpath(i["root"]) == i["root"]
        assert os.path.basename(i["root"]) in ("sessions", "archived_sessions")
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", i["label"])
    for e in d["exclude"]:
        if "path" in e: assert os.path.isabs(e["path"])
        else: assert e["provider"] == "claude" and re.fullmatch(r"-[A-Za-z0-9-]{1,255}", e["projectDirPrefix"])
PY

# a failing manifest write never fails the launch
chmod 500 "$COLL"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS" 2>/dev/null || fail 'reconcile must be fail-open'
chmod 700 "$COLL"

# ---- policy does not depend on the in-process registry (H1) ----------------
# A launch through harness-exec runs a fresh shell with an empty registry: the
# personal profile alone must not rewrite the collecting sessions.
before="$(inode "$M1") $(cat "$M1")"
harness_agent_usage_reconcile "$ROOT/bin" "$PERS"
[[ "$before" == "$(inode "$M1") $(cat "$M1")" ]] || fail "reconcile with only the personal harness must leave collecting sessions alone: $(cat "$M1")"
# no harness passed at all: the session source roots still carry the policy
rm -f "$M1" "$M2"
harness_agent_usage_reconcile "$ROOT/bin"
[[ -f "$M1" && "$(j "$M1" "len(d['include'])")" == 2 ]] || fail 'reconcile without passed harnesses must evaluate session source roots'
[[ -f "$M2" && "$(j "$M2" "d['include']")" == '[]' ]] || fail 'personal session must still be excluded without passed harnesses'
# the renderer never defaults an unevaluated source to inactive
before="$(inode "$M1") $(cat "$M1")"
"$PY" -I "$ROOT/bin/harness_agent_usage.py" reconcile --state-home "$STATE" --harness "$PERS=0"
[[ "$before" == "$(inode "$M1") $(cat "$M1")" ]] || fail "a session whose source was not evaluated must be left untouched: $(cat "$M1")"
# a session source root outside HOME is never evaluated
OUTSIDE="$TMP/outside-harness"; make_harness "$OUTSIDE" outside 0
ID5=4e5f6071-8293-4a41-8d3e-eeeeeeeeeeee; make_session "$ID5" "$OUTSIDE"
harness_agent_usage_reconcile "$ROOT/bin"
[[ ! -e "$COLL/harness-launcher-$ID5.json" && ! -e "$COLL/harness-launcher-profile-outside.json" ]] || fail 'a source root outside HOME must not be evaluated'
rm -rf "$STATE/sessions/$ID5" "$STATE/worktrees/$ID5"

# ---- several state homes share collection.d (H2) ---------------------------
STATE_B="$HOME/.local/state/harness-launcher-b"
IDB=5f607182-93a4-4b52-9e4f-ffffffffffff
mkdir -p "$STATE_B/sessions/$IDB" "$STATE_B/worktrees/$IDB/.harness/codex/sessions"
printf '%s\n' "$COMP" > "$STATE_B/sessions/$IDB/source-root"
printf '%s\n' "$STATE_B/worktrees/$IDB" > "$STATE_B/sessions/$IDB/session-root"
HARNESS_SESSION_STATE_HOME="$STATE_B" harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
MB="$COLL/harness-launcher-$IDB.json"
[[ -f "$MB" && "$(j "$MB" "len(d['include'])")" == 2 ]] || fail 'state home B session manifest expected'
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ -f "$MB" ]] || fail "reconcile in state home A must not remove state home B's manifest"
[[ -f "$M1" ]] || fail 'state home A manifests stay'
# a personal session of B keeps its exclusion through A's sweep too
printf '%s\n' "$PERS" > "$STATE_B/sessions/$IDB/source-root"
HARNESS_SESSION_STATE_HOME="$STATE_B" harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ -f "$MB" && "$(j "$MB" "d['include']")" == '[]' ]] || fail "A's sweep must not remove B's exclusion manifest"
# B's own sweep removes it once B's record is gone
rm -rf "$STATE_B"
HARNESS_SESSION_STATE_HOME="$STATE_B" harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ ! -e "$MB" ]] || fail "B's sweep must remove B's orphaned manifest"
# a manifest naming paths outside this state home is never swept
FOREIGN_M="$COLL/harness-launcher-6a7b8c9d-0e1f-4a2b-8c3d-121212121212.json"
printf '{"schema":1,"include":[{"provider":"codex","root":"%s/other/sessions","label":"hl-6a7b8c9d0e1f"}],"exclude":[]}\n' "$HOME" > "$FOREIGN_M"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ -f "$FOREIGN_M" ]] || fail 'a manifest outside this state home must not be removed'
rm -f "$FOREIGN_M"

# ---- malformed manifests and CRLF config never stop the run (LOW) ---------
BAD="$COLL/harness-launcher-7b8c9d0e-1f20-4b3c-9d4e-232323232323.json"
printf '[1, 2]\n' > "$BAD"
rm -f "$PPERS"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS" 2>"$TMP/bad.err" || fail 'reconcile must be fail-open on a non-dict manifest'
grep -q Traceback "$TMP/bad.err" && fail "no traceback on a malformed manifest: $(cat "$TMP/bad.err")"
[[ -f "$PPERS" ]] || fail 'profile manifests are still written after a malformed manifest'
rm -f "$BAD"
CRLF="$HOME/crlf-harness"; mkdir -p "$CRLF/config"
printf 'HARNESS_NAME="crlf"\r\nHARNESS_PREFIX="crlf"\r\n' > "$CRLF/config/launcher.env"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS" "$CRLF"
[[ -f "$COLL/harness-launcher-profile-crlf.json" ]] || fail 'a CRLF launcher.env prefix must still name the profile manifest'

# ---- cross-process lock (M1) ----------------------------------------------
LOCK="$HOME/.agent-usage/.harness-launcher.lock"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ -f "$LOCK" && ! -L "$LOCK" && "$(stat -f %Lp "$LOCK")" == 600 ]] || fail 'lock file must be a 0600 regular file outside collection.d'
[[ ! -e "$COLL/.harness-launcher.lock" ]] || fail 'the lock must not live in collection.d'
"$PY" -I -c 'import fcntl,os,sys,time
fd=os.open(sys.argv[1], os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_EX); print("held", flush=True); time.sleep(20)' "$LOCK" > "$TMP/holder.out" &
HOLDER=$!
for _ in $(seq 1 100); do [[ -s "$TMP/holder.out" ]] && break; sleep 0.05; done
rm -f "$M1"
started=$SECONDS
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS" 2>"$TMP/lock.err" || fail 'a held lock must be fail-open'
elapsed=$(( SECONDS - started ))
kill "$HOLDER" 2>/dev/null; wait "$HOLDER" 2>/dev/null || true
(( elapsed <= 4 )) || fail "lock wait must be bounded (took ${elapsed}s)"
[[ ! -e "$M1" ]] || fail 'a run that could not take the lock must not write'
[[ "$(grep -c 'WARN' "$TMP/lock.err")" == 1 ]] || fail "one WARN line when the lock is busy: $(cat "$TMP/lock.err")"
harness_agent_usage_sync_session "$ROOT/bin" "$ID1" "$COMP"
[[ -f "$M1" ]] || fail 'sync-session works once the lock is free'
rm -f "$LOCK"; : > "$TMP/lock-target"; ln -s "$TMP/lock-target" "$LOCK"
rm -f "$M1"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS" 2>/dev/null || fail 'a linked lock must be fail-open'
[[ ! -e "$M1" ]] || fail 'a symlinked lock file must be refused'
rm -f "$LOCK"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ -f "$M1" ]] || fail 'reconcile recovers after the link is gone'

# ---- P6 retirement --------------------------------------------------------
# collecting session: both include roots move to the archive, same labels
mkdir -p "$STATE/archives/$ID1/codex/sessions" "$STATE/archives/$ID1/codex/archived_sessions"
rm -rf "$STATE/worktrees/$ID1"
harness_agent_usage_sync_session "$ROOT/bin" "$ID1" "$COMP"
[[ "$(j "$M1" "[(i['root'], i['label']) for i in d['include']]")" == "[('$STATE/archives/$ID1/codex/sessions', 'hl-0f3a9c12ab34'), ('$STATE/archives/$ID1/codex/archived_sessions', 'hl-0f3a9c12ab34-a')]" ]] \
  || fail "retired collecting session must repoint both roots to the archive: $(cat "$M1")"
# archive missing -> nothing to collect, manifest removed
rm -rf "$STATE/archives/$ID1"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ ! -e "$M1" ]] || fail 'manifest must go when the archive is gone'
# personal session: the exclusion outlives the worktree while Claude transcripts exist
rm -rf "$STATE/worktrees/$ID2"
mkdir -p "$HOME/.claude/projects/$ENC2-sub"
harness_agent_usage_sync_session "$ROOT/bin" "$ID2" "$PERS"
[[ -f "$M2" && "$(j "$M2" "d['include']")" == '[]' ]] || fail 'personal exclusion must survive worktree deletion while transcripts exist'
rm -rf "$HOME/.claude/projects/$ENC2-sub"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
[[ ! -e "$M2" ]] || fail 'personal manifest must be removed once no transcript directory remains'

# ---- foreign state home / harness (test fixtures outside this HOME) -------
# A launcher run whose state home or harness lives outside HOME must leave this
# HOME's manifests alone: no new manifests and no stale-removal of real ones.
ID3=2c3d4e5f-6071-4829-8b1c-cccccccccccc
make_session "$ID3" "$COMP"
harness_agent_usage_reconcile "$ROOT/bin" "$COMP" "$PERS"
M3="$COLL/harness-launcher-$ID3.json"
[[ -f "$M3" ]] || fail 'precondition: real session manifest'
FOREIGN="$TMP/foreign"
make_harness "$FOREIGN/harness" foreign 0
mkdir -p "$FOREIGN/state/sessions"
snapshot="$(ls "$COLL")"
HARNESS_SESSION_STATE_HOME="$FOREIGN/state" harness_agent_usage_reconcile "$ROOT/bin" "$FOREIGN/harness"
[[ "$(ls "$COLL")" == "$snapshot" ]] || fail "foreign reconcile must not add or remove manifests: $(ls "$COLL")"
HARNESS_SESSION_STATE_HOME="$FOREIGN/state" harness_agent_usage_sync_session "$ROOT/bin" "$ID3" "$COMP"
[[ "$(ls "$COLL")" == "$snapshot" ]] || fail 'foreign sync-session must not touch this HOME'
# a foreign harness with this HOME's real state home: no policy, no rewrite
before="$(for f in "$COLL"/*; do inode "$f"; done)"
harness_agent_usage_reconcile "$ROOT/bin" "$FOREIGN/harness"
[[ "$before" == "$(for f in "$COLL"/*; do inode "$f"; done)" ]] || fail 'reconcile without a registered harness under HOME must not rewrite real sessions'
[[ "$(j "$M3" "len(d['include'])")" == 2 ]] || fail 'collecting session must keep its include roots'
# a session whose source is outside HOME (a fixture in the real state home)
ID4=3d4e5f60-7182-4930-9c2d-dddddddddddd
make_session "$ID4" "$FOREIGN/harness"
harness_agent_usage_sync_session "$ROOT/bin" "$ID4" "$FOREIGN/harness"
[[ ! -e "$COLL/harness-launcher-$ID4.json" ]] || fail 'sync-session must skip a source outside HOME'
rm -rf "$STATE/sessions/$ID4" "$STATE/worktrees/$ID4"
mv "$HOME/.agent-usage" "$TMP/agent-usage.saved"
HARNESS_SESSION_STATE_HOME="$FOREIGN/state" harness_agent_usage_reconcile "$ROOT/bin" "$FOREIGN/harness"
[[ ! -e "$HOME/.agent-usage" ]] || fail 'foreign reconcile must not create ~/.agent-usage'
mv "$TMP/agent-usage.saved" "$HOME/.agent-usage"

echo 'PASS: collection.d manifests (write rules, reconcile, retirement)'
