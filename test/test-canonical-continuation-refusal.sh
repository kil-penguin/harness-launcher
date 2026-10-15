#!/usr/bin/env zsh
# test-canonical-continuation-refusal.sh — in an isolation-default profile a
# continuation of a session that lives in the canonical harness checkout never
# silently runs there:
#   * an isolated owner is restored first, for every Codex spelling;
#   * only the pure Codex forms `codex resume <id>` / `codex --passthrough
#     resume <id>` reach durable-history prepare (fresh isolation);
#   * Claude, any other Codex form (preset, -s, ...) and a failed prepare are
#     refused with exact alternatives, also without a TTY;
#   * fresh, print and client runs keep their routes;
#   * a profile without isolation-default keeps the base behavior.
# Model CLIs are doubles at the external boundary.
set -e
unset HARNESS_TERMINAL_RUNTIME TERM_PROGRAM HARNESS_HOST_DEFAULT_MODE CLAUDECODE CODEX_THREAD_ID; unset -m 'HERDR_*' 'ORCA_*' 'CMUX_*' || true

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"; TMP="${TMP:A}"
trap 'rm -rf "$TMP"' EXIT
HARNESS="$TMP/harness"; STATE="$TMP/state"; CCONF="$TMP/claude-config"
mkdir -p "$HARNESS/config" "$TMP/bin" "$CCONF/projects/-tmp-proj"
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
source "$ROOT/bin/aliases.zsh"
export PATH="$TMP/bin:$PATH" HARNESS_SESSION_STATE_HOME="$STATE" CLAUDE_CONFIG_DIR="$CCONF" HOME="$TMP/home" STUB_LOG="$TMP/claude.log"
mkdir -p "$HOME"

# --- the early history request is unchanged: only the exact form ---------------------
req() { _harness_launcher_codex_history_request "$@" || true; }
[[ "$(req codex resume $CODEX_ID)" == "$CODEX_ID" ]] || fail 'exact codex resume must stay recognized'
[[ -z "$(req codex --passthrough resume $CODEX_ID)" ]] || fail 'wrapper/Orca form must reach the owner check first, not the early history request'
[[ -z "$(req codex fast resume $CODEX_ID)" ]] || fail 'preset form must reach the owner check first'
echo 'PASS: only the exact codex resume form keeps the early history request'

# --- fixtures: a canonical-owned session ---------------------------------------------
print -r -- '{"type":"assistant","isSidechain":false,"message":{"model":"claude-opus-5-5"}}' > "$CCONF/projects/-tmp-proj/$ID.jsonl"
printf '{"hook_event_name":"SessionStart","source":"startup","session_id":"%s"}' "$ID" \
  | env HARNESS_SESSION_STATE_HOME="$STATE" "$PY" "$ROOT/bin/harness-launch-record" claude --source-root "$HARNESS" --isolated 0
mkdir -p "$HARNESS/.harness/codex/sessions/2026/09/30"
: > "$HARNESS/.harness/codex/sessions/2026/09/30/rollout-2026-09-30T01-02-03-$CODEX_ID.jsonl"

# The model CLI boundary: record what would have been launched.
_harness_launcher_run_session() { printf 'SESSION=%s\nARGS=%s\n' "${HARNESS_SESSION_ROOT:-}" "$*" > "$STUB_LOG"; return 0; }
run() { # <tty:0|1> <args...> : sets ERR, RC
  local tty="$1"; shift
  rm -f "$STUB_LOG"
  ERR="$( { (
    if [[ "$tty" == 1 ]]; then harness_claude_stdio_is_tty() { return 0; }; fi
    _harness_launcher_run "$HARNESS" "$@"
  ) 2>&1 >/dev/null; } )" && RC=0 || RC=$?
}
sessions() { find "$STATE/sessions" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' '; }
want_alternatives() { # <label> <original args>
  local label="$1"; shift
  [[ $RC == 2 ]] || fail "$label: want refusal rc 2, got $RC [$ERR]"
  [[ ! -e "$STUB_LOG" ]] || fail "$label: refused continuation must not launch"
  [[ "$ERR" == *'fresh isolated'* ]] || fail "$label: message lacks the fresh isolated alternative: $ERR"
  [[ "$ERR" == *"test --no-isolated $*"* ]] || fail "$label: message lacks the exact --no-isolated alternative for '$*': $ERR"
}

# --- L1.2/L1.3 Claude: refused in a TTY and without a TTY ---------------------------
before="$(sessions)"
run 1 --resume $ID;  want_alternatives 'claude tty' --resume $ID
run 0 --resume $ID;  want_alternatives 'claude non-tty' --resume $ID
run 0 base --passthrough --resume $ID; want_alternatives 'claude passthrough non-tty' base --passthrough --resume $ID
[[ "$(sessions)" == "$before" ]] || fail 'refusal must not create a session'
echo 'PASS: L1.2/L1.3 Claude canonical continuation is refused with both alternatives (TTY and non-TTY)'

# non-TTY continuation whose owner is not found now refuses; print/fresh/client stay.
run 0 --resume 99999999-2222-4333-8444-555555555555
[[ $RC == 2 && ! -e "$STUB_LOG" ]] || fail "non-TTY unknown owner must refuse, got $RC"
run 0 --remote-control; [[ $RC == 0 && -e "$STUB_LOG" ]] || fail "--remote-control stays legacy (rc=$RC) [$ERR]"
grep -q '^SESSION=$' "$STUB_LOG" || fail '--remote-control must stay in the canonical root'
run 0 -p 'hello';  [[ $RC == 0 && -e "$STUB_LOG" ]] || fail "-p print stays legacy (rc=$RC) [$ERR]"
run 0 -p --resume $ID;  [[ $RC == 0 && -e "$STUB_LOG" ]] || fail "-p with --resume stays legacy print (rc=$RC) [$ERR]"
echo 'PASS: L1.3 fresh and print runs unchanged'

# --- L1.2 Codex, rc 4 (canonical-only rollout) ----------------------------------------
CALLS="$TMP/calls"; : > "$CALLS"
_harness_launcher_codex_history_select() { print -r -- "native_id=$2"; }
_harness_launcher_codex_history_lease_acquire() { return 0; }
_harness_launcher_codex_history_lease_release() { return 0; }
_harness_launcher_codex_history_prepare() { print -r -- "prepare $2" >> "$CALLS"; return 1; }
_harness_launcher_run_codex_cli() { printf 'SESSION=%s\nARGS=%s\n' "${HARNESS_SESSION_ROOT:-}" "$*" > "$STUB_LOG"; }
# Pure forms reach prepare (the stub fails it, so the run refuses with the cause).
for form in 'codex --passthrough resume' 'codex resume'; do
  : > "$CALLS"
  run 1 ${=form} $CODEX_ID
  grep -qx "prepare $CODEX_ID" "$CALLS" || fail "'$form <id>' did not reach history prepare"
  [[ $RC == 2 && ! -e "$STUB_LOG" ]] || fail "'$form <id>' with failed prepare must refuse, got $RC"
  [[ "$ERR" == *'cannot be prepared safely'* ]] || fail "failed prepare must say why: $ERR"
  # The exact form is handled by the unchanged early history request (base behavior).
  [[ "$form" == 'codex resume' || "$ERR" == *"test --no-isolated $form $CODEX_ID"* ]] || fail "failed prepare lacks the --no-isolated alternative: $ERR"
done
# Every other Codex form would lose its flags in the rewrite and let the recorded
# grant be re-applied: refused without touching history, with the pure form named.
for form in 'codex plan resume' 'codex fast resume' 'codex -s read-only resume' 'codex -m gpt-5.5 resume' 'codex -a never resume' '-c x=1 resume'; do
  : > "$CALLS"
  [[ "$form" == -c* ]] && form="codex $form"
  run 1 ${=form} $CODEX_ID
  [[ ! -s "$CALLS" ]] || fail "'$form <id>' must not reach history prepare"
  [[ $RC == 2 && ! -e "$STUB_LOG" ]] || fail "'$form <id>' must refuse without launching, got $RC [$ERR]"
  [[ "$ERR" == *'fresh isolated'* && "$ERR" == *"test --no-isolated ${form} $CODEX_ID"* && "$ERR" == *"test codex resume $CODEX_ID"* ]] || fail "'$form <id>' refusal lacks the exact alternatives: $ERR"
done
# Web planner argv with a context word before --passthrough.
run 1 codex plan 1m --passthrough resume $CODEX_ID --some-flag
[[ $RC == 2 && ! -e "$STUB_LOG" ]] || fail "planner argv must refuse, got $RC"
echo 'PASS: L1.2 pure Codex forms prepare history; other Codex forms are refused with the pure alternative'

# Copy-pasteable alternative: quoting and a --cwd that was already shifted.
run 1 --cwd "$HARNESS" --resume $ID
[[ $RC == 2 ]] || fail "--cwd continuation must refuse, got $RC"
[[ "$ERR" == *"test --cwd $HARNESS --no-isolated --resume $ID"* ]] || fail "refusal must keep --cwd: $ERR"
run 1 --resume $ID 'two words'
[[ "$ERR" == *"--no-isolated --resume $ID two\\ words"* || "$ERR" == *"--no-isolated --resume $ID 'two words'"* ]] || fail "refusal must quote args: $ERR"
echo 'PASS: L3 the alternative is quoted and keeps --cwd'

# --- HIGH-1: an isolated owner is restored first, for every spelling -------------------
# Real history helpers again (nothing history-related is stubbed from here on).
source "$ROOT/bin/aliases.zsh"
_harness_launcher_run_session() { printf 'SESSION=%s\nARGS=%s\n' "${HARNESS_SESSION_ROOT:-}" "$*" > "$STUB_LOG"; return 0; }
_harness_launcher_run_codex_cli() { printf 'SESSION=%s\nARGS=%s\n' "${HARNESS_SESSION_ROOT:-}" "$*" > "$STUB_LOG"; }
OWNER_ID=aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee
created="$(HARNESS_SESSION_STATE_HOME="$STATE" bash "$ROOT/bin/session-isolation.sh" create "$HARNESS")" || fail 'cannot create an isolated owner'
OWNER_SESSION="$(print -r -- "$created" | sed -n 's/^HARNESS_SESSION_ID=//p')"
OWNER_ROOT="$(print -r -- "$created" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
mkdir -p "$OWNER_ROOT/.harness/codex/sessions/2026/09/29"
: > "$OWNER_ROOT/.harness/codex/sessions/2026/09/29/rollout-2026-09-29T01-02-03-$OWNER_ID.jsonl"
for journal_state in OPEN ABANDONED; do
  sed -i.bak "1s/.*/state=$journal_state/" "$STATE/sessions/$OWNER_SESSION/journal"
  for form in 'codex --passthrough resume' 'codex fast resume' 'codex plan resume'; do
    run 1 ${=form} $OWNER_ID
    [[ $RC == 0 && -e "$STUB_LOG" ]] || fail "$journal_state owner: '$form <id>' was not restored (rc=$RC) [$ERR]"
    [[ "$(sed -n 's/^SESSION=//p' "$STUB_LOG")" == "$OWNER_ROOT" ]] || fail "$journal_state owner: '$form <id>' landed in $(sed -n 's/^SESSION=//p' "$STUB_LOG")"
  done
done
echo 'PASS: HIGH-1 isolated owner (OPEN and ABANDONED) is restored for wrapper and preset forms'

# --- M3: a profile without isolation-default keeps the base behavior ------------------
HARNESS0="$TMP/harness0"; mkdir -p "$HARNESS0/config"
git -C "$HARNESS0" init -q -b main
git -C "$HARNESS0" config user.email test@example.invalid
git -C "$HARNESS0" config user.name test
print -r -- 'HARNESS_NAME="test0"' 'HARNESS_PREFIX="test0"' 'HARNESS_SESSION_ISOLATION_DEFAULT="0"' > "$HARNESS0/config/launcher.env"
git -C "$HARNESS0" add -A && git -C "$HARNESS0" commit -qm initial
mkdir -p "$HARNESS0/.harness/codex/sessions/2026/09/30"
: > "$HARNESS0/.harness/codex/sessions/2026/09/30/rollout-2026-09-30T01-02-03-$CODEX_ID.jsonl"
run_off() { # <tty> <args...>
  local tty="$1"; shift
  rm -f "$STUB_LOG"
  ERR="$( { (
    if [[ "$tty" == 1 ]]; then harness_claude_stdio_is_tty() { return 0; }; fi
    _harness_launcher_codex_history_prepare() { print -r -- "prepare $2" >> "$CALLS"; return 1; }
    _harness_launcher_run "$HARNESS0" "$@"
  ) 2>&1 >/dev/null; } )" && RC=0 || RC=$?
}
: > "$CALLS"
run_off 0 --resume $ID;                         [[ $RC == 0 && -e "$STUB_LOG" ]] || fail "isolation off: non-TTY --resume must stay legacy (rc=$RC) [$ERR]"
run_off 1 codex --passthrough resume $CODEX_ID; [[ $RC == 0 && -e "$STUB_LOG" ]] || fail "isolation off: wrapper resume must stay in place (rc=$RC) [$ERR]"
run_off 1 codex plan resume $CODEX_ID;          [[ $RC == 0 && -e "$STUB_LOG" ]] || fail "isolation off: preset resume must stay in place (rc=$RC) [$ERR]"
[[ ! -s "$CALLS" ]] || fail 'isolation off: history prepare must not run for non-exact forms'
echo 'PASS: M3 isolation-off profile keeps the base behavior'
echo 'PASS: canonical continuation refusal'
