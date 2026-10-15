#!/usr/bin/env zsh
# test-restore-owner.sh — in an isolation-default profile, a restore of a
# session that lives in the canonical (non-isolated) harness root is identified
# (resolver rc 4): a Codex rollout that exists only in the source CODEX_HOME, or
# a Claude session whose launch record says isolated=0 for this harness. The
# launcher no longer continues it there: Codex goes through durable-history
# prepare, Claude (or a failed prepare) is refused. An isolated owner still
# wins and transcripts alone are not proof.
# This suite covers the compatibility resolver for older pinned releases.
# Current Native catalog/fresh-restore behavior has its own real-metadata tests.
set -e
unset HARNESS_TERMINAL_RUNTIME TERM_PROGRAM HARNESS_HOST_DEFAULT_MODE CLAUDECODE CODEX_THREAD_ID; unset -m 'HERDR_*' 'ORCA_*' 'CMUX_*' || true

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"; TMP="${TMP:A}"
trap 'rm -rf "$TMP"' EXIT
HARNESS="$TMP/harness"
OTHER="$TMP/other-harness"
STATE="$TMP/state"
CCONF="$TMP/claude-config"
mkdir -p "$HARNESS/config" "$OTHER" "$TMP/bin" "$CCONF/projects/-tmp-proj"
git -C "$HARNESS" init -q -b main
git -C "$HARNESS" config user.email test@example.invalid
git -C "$HARNESS" config user.name test
print -r -- 'HARNESS_NAME="test"' 'HARNESS_PREFIX="test"' 'HARNESS_SESSION_ISOLATION_DEFAULT="1"' > "$HARNESS/config/launcher.env"
git -C "$HARNESS" add -A && git -C "$HARNESS" commit -qm initial
PY="$(source "$ROOT/bin/harness-common.sh"; harness_python3_resolve)"

cat > "$TMP/bin/claude" <<'STUB'
#!/usr/bin/env bash
{ printf 'SESSION=%s\n' "${HARNESS_SESSION_ROOT:-}"; printf 'ARG=%s\n' "$@"; } > "$STUB_LOG"
STUB
chmod +x "$TMP/bin/claude"
fail() { echo "FAIL: $*" >&2; exit 1; }

ID=0b5d1f3e-0000-4000-8000-000000000001
CODEX_ID=01a0ef50-1bec-7852-afb3-a6b6559f47b6
UNKNOWN=99999999-2222-4333-8444-555555555555
source "$ROOT/bin/aliases.zsh"
export HARNESS_SESSION_STATE_HOME="$STATE" CLAUDE_CONFIG_DIR="$CCONF"

resolve() { # <args...> : sets OUT, RC
  OUT="$(_harness_launcher_resolve_restore "${HARNESS:A}" "$@")" && RC=0 || RC=$?
}
write_record() { # <id> <isolated> [source_root]
  printf '{"hook_event_name":"SessionStart","source":"startup","session_id":"%s"}' "$1" \
    | env HARNESS_SESSION_STATE_HOME="$STATE" \
        "$PY" "$ROOT/bin/harness-launch-record" claude --source-root "${3:-$HARNESS}" --isolated "$2"
}
source_rollout() { # <id>
  local dir="$HARNESS/.harness/codex/sessions/2026/09/30"
  mkdir -p "$dir"; : > "$dir/rollout-2026-09-30T01-02-03-$1.jsonl"
}

# --- B4 resolver: Codex --------------------------------------------------------
resolve codex resume $CODEX_ID; [[ $RC == 1 ]] || fail "no rollout anywhere must stay 1, got $RC"
source_rollout $CODEX_ID
resolve codex resume $CODEX_ID; [[ $RC == 4 && -z "$OUT" ]] || fail "source-only rollout must return 4, got $RC [$OUT]"
resolve codex --passthrough resume $CODEX_ID; [[ $RC == 4 ]] || fail "passthrough codex resume must return 4, got $RC"
resolve codex resume $UNKNOWN; [[ $RC == 1 ]] || fail "unknown codex id must stay 1, got $RC"
resolve codex resume not-a-uuid; [[ $RC == 1 ]] || fail "non-UUID must stay 1, got $RC"
# a symlinked rollout is not evidence
OTHER_ID=aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee
ln -s "$HARNESS/.harness/codex/sessions/2026/09/30/rollout-2026-09-30T01-02-03-$CODEX_ID.jsonl" \
  "$HARNESS/.harness/codex/sessions/2026/09/30/rollout-2026-09-30T01-02-03-$OTHER_ID.jsonl"
resolve codex resume $OTHER_ID; [[ $RC == 1 ]] || fail "symlinked rollout must stay 1, got $RC"
# a rollout in another harness root is not this harness's
resolve_other() { OUT="$(_harness_launcher_resolve_restore "${OTHER:A}" "$@")" && RC=0 || RC=$?; }
resolve_other codex resume $CODEX_ID; [[ $RC == 1 ]] || fail "rollout of another harness must stay 1, got $RC"
# an isolated owner wins over the source rollout
ISO=ABCDEF01-2222-4333-8444-555555555555
mkdir -p "$STATE/sessions/$ISO" "$STATE/worktrees/$ISO/.harness/codex/sessions/2026/09/30"
print -r -- "${HARNESS:A}" > "$STATE/sessions/$ISO/source-root"
: > "$STATE/worktrees/$ISO/.harness/codex/sessions/2026/09/30/rollout-2026-09-30T01-02-03-$CODEX_ID.jsonl"
resolve codex resume $CODEX_ID; [[ $RC == 0 && "$OUT" == "$ISO" ]] || fail "isolated owner must win, got $RC [$OUT]"
FORK=ABCDEF02-2222-4333-8444-555555555555
CHILD=44444444-2222-4333-8444-555555555555
mkdir -p "$STATE/sessions/$FORK" "$STATE/worktrees/$FORK/.harness/codex/sessions/2026/09/30"
print -r -- "${HARNESS:A}" > "$STATE/sessions/$FORK/source-root"
: > "$STATE/worktrees/$FORK/.harness/codex/sessions/2026/09/30/rollout-parent-$CODEX_ID.jsonl"
: > "$STATE/worktrees/$FORK/.harness/codex/sessions/2026/09/30/rollout-child-$CHILD.jsonl"
printf '{"schema":1,"session_id":"%s","source_root":"%s","imports":[{"runtime":"codex","native_id":"%s","source_owner":"%s"}]}' "$FORK" "${HARNESS:A}" "$CODEX_ID" "$ISO" > "$STATE/sessions/$FORK/restored-native-sessions"
chmod 600 "$STATE/sessions/$FORK/restored-native-sessions"
resolve codex resume $CODEX_ID; [[ $RC == 0 && "$OUT" == "$ISO" ]] || fail "imported parent must retain its original owner, got $RC [$OUT]"
resolve codex resume $CHILD; [[ $RC == 0 && "$OUT" == "$FORK" ]] || fail "new child must belong to fork workspace, got $RC [$OUT]"
rm -rf "$STATE/sessions/$FORK" "$STATE/worktrees/$FORK"
rm -rf "$STATE/sessions/$ISO" "$STATE/worktrees/$ISO"
echo 'PASS: B4 resolver returns 4 only for a source-only Codex rollout'

# --- B4 resolver: Claude -------------------------------------------------------
print -r -- '{"type":"assistant","isSidechain":false,"message":{"model":"claude-opus-5-5"}}' > "$CCONF/projects/-tmp-proj/$ID.jsonl"
resolve --resume $ID; [[ $RC == 1 ]] || fail "a transcript alone must not return 4, got $RC"
resolve base --passthrough --resume $ID; [[ $RC == 1 ]] || fail "a transcript alone must not return 4 (passthrough), got $RC"
write_record $ID 1
resolve --resume $ID; [[ $RC == 1 ]] || fail "a record saying isolated=1 must not return 4, got $RC"
write_record $ID 0 /somewhere/else
resolve --resume $ID; [[ $RC == 1 ]] || fail "a record for another harness must not return 4, got $RC"
write_record $ID 0
resolve --resume $ID; [[ $RC == 4 && -z "$OUT" ]] || fail "isolated=0 record must return 4, got $RC [$OUT]"
resolve base --passthrough --resume $ID; [[ $RC == 4 ]] || fail "isolated=0 record (passthrough) must return 4, got $RC"
resolve --resume $UNKNOWN; [[ $RC == 1 ]] || fail "unknown claude id must stay 1, got $RC"
# an isolated owner (session named by the id) wins over an isolated=0 record
mkdir -p "$STATE/sessions/${(U)ID}"; print -r -- "${HARNESS:A}" > "$STATE/sessions/${(U)ID}/source-root"
resolve --resume $ID; [[ $RC == 0 && "$OUT" == "${(U)ID}" ]] || fail "isolated owner must win over a record, got $RC [$OUT]"
rm -rf "$STATE/sessions/${(U)ID}"
echo 'PASS: B4 resolver returns 4 only with an isolated=0 record for this harness'

# --- routing: interactive default route ------------------------------------------
run_tty() { # <log> <args...>
  local log="$1"; shift
  rm -f "$log" "$log.err"
  ROOT="$ROOT" HARNESS="$HARNESS" STATE="$STATE" TMP="$TMP" CCONF="$CCONF" STUB_LOG="$log" ISO_FORCE="${ISO_FORCE:-}" TEST_ARGS="${(j: :)${(q)@}}" expect <<'EXPECT'
set timeout 30
log_user 0
spawn env ROOT=$env(ROOT) HARNESS=$env(HARNESS) STATE=$env(STATE) TMP=$env(TMP) CCONF=$env(CCONF) STUB_LOG=$env(STUB_LOG) ISO_FORCE=$env(ISO_FORCE) TEST_ARGS=$env(TEST_ARGS) PATH=$env(TMP)/bin:/usr/bin:/bin zsh -c {
  export PATH="$TMP/bin:/usr/bin:/bin" HARNESS_SESSION_STATE_HOME="$STATE" CLAUDE_CONFIG_DIR="$CCONF" HOME="$TMP/home"
  [[ -z "$ISO_FORCE" ]] || export HARNESS_SESSION_ISOLATION=1
  unset CMUX_WORKSPACE_ID CMUX_TAB_ID CMUX_SURFACE_ID
  source "$ROOT/bin/aliases.zsh"
  _harness_launcher_codex_history_request() { return 1; }
  _harness_launcher_run_codex_cli() { printf 'SESSION=%s\nARGS=%s\n' "${HARNESS_SESSION_ROOT:-}" "$*" > "$STUB_LOG"; }
  eval "set -- $TEST_ARGS"
  _harness_launcher_run "$HARNESS" "$@"
}
expect eof
catch wait result
exit [lindex $result 3]
EXPECT
}
session_count() { find "$STATE/sessions" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' '; }

# Codex: a source-only rollout no longer restores in place.  The owner check
# (rc 4) hands it to durable-history prepare for a fresh isolated session; the
# fixture rollout is empty, so prepare refuses and nothing launches.
before="$(session_count)"
if run_tty "$TMP/codex-tty.log" codex resume $CODEX_ID; then fail 'source-only Codex restore must not run in the canonical root'; fi
[[ ! -e "$TMP/codex-tty.log" ]] || fail 'refused source-only Codex restore must not launch'
if run_tty "$TMP/codex-tty2.log" codex --passthrough resume $CODEX_ID; then fail 'passthrough source-only Codex restore must not run in the canonical root'; fi
[[ ! -e "$TMP/codex-tty2.log" ]] || fail 'refused passthrough Codex restore must not launch'
# an unknown id keeps the reject message and does not launch
if run_tty "$TMP/codex-unk.log" codex resume $UNKNOWN; then fail 'unknown Codex id must still be rejected'; fi
[[ ! -e "$TMP/codex-unk.log" ]] || fail 'unknown Codex id must not launch'
# forced isolation keeps rejecting
if ISO_FORCE=1 run_tty "$TMP/codex-forced.log" codex resume $CODEX_ID; then fail 'forced isolation must still reject a source-only Codex restore'; fi
[[ ! -e "$TMP/codex-forced.log" ]] || fail 'forced isolation must not launch'
[[ "$(session_count)" == "$before" ]] || fail 'rejected restore must not create a session'
echo 'PASS: B4 default route no longer restores a source-only Codex rollout in the canonical root'

# Claude: an isolated=0 record is a canonical session; it is refused, as is a bare transcript.
rm -rf "$STATE/launch-records"
if run_tty "$TMP/claude-norec.log" --resume $ID; then fail 'Claude transcript without a record must be rejected'; fi
[[ ! -e "$TMP/claude-norec.log" ]] || fail 'Claude transcript without a record must not launch'
write_record $ID 1
if run_tty "$TMP/claude-iso.log" --resume $ID; then fail 'isolated=1 record must be rejected without an isolated owner'; fi
write_record $ID 0
if run_tty "$TMP/claude-rec.log" --resume $ID; then fail 'isolated=0 Claude restore must be refused, not run in the canonical root'; fi
[[ ! -e "$TMP/claude-rec.log" ]] || fail 'refused isolated=0 Claude restore must not launch'
[[ "$(session_count)" == "$before" ]] || fail 'refused Claude restore must not create a session'
if ISO_FORCE=1 run_tty "$TMP/claude-forced.log" --resume $ID; then fail 'forced isolation must still reject an isolated=0 Claude restore'; fi
[[ ! -e "$TMP/claude-forced.log" ]] || fail 'forced isolation must not launch'
echo 'PASS: B4 default route refuses an isolated=0 Claude session; forced isolation still rejects'

echo 'PASS: restore owner'
