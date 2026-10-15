#!/usr/bin/env zsh
# test-canonical-continuation-refusal.sh — a continuation of a session that
# lives in the canonical harness checkout never silently runs there:
#   * Codex resume forms reach durable-history prepare (fresh isolation),
#     including the shell wrapper / Orca `--passthrough` form and presets;
#   * Claude (and Codex when prepare fails) is refused with two exact
#     alternatives, also without a TTY;
#   * fresh, print and client runs keep their routes.
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

# --- L1.1 Codex argv normalization ------------------------------------------------
req() { _harness_launcher_codex_history_request "$@" || true; }
[[ "$(req codex resume $CODEX_ID)" == "$CODEX_ID" ]] || fail 'exact codex resume must stay recognized'
[[ "$(req codex --passthrough resume $CODEX_ID)" == "$CODEX_ID" ]] || fail 'wrapper/Orca --passthrough resume must reach history'
[[ "$(req codex fast resume $CODEX_ID)" == "$CODEX_ID" ]] || fail 'preset resume must reach history'
[[ "$(req codex --passthrough fast resume ${(U)CODEX_ID})" == "$CODEX_ID" ]] || fail 'passthrough + preset resume must reach history (lowercased)'
[[ "$(req codex --passthrough resume --list)" == list ]] || fail 'passthrough resume --list must list'
[[ "$(req codex --passthrough continue)" == '' ]] || fail 'continue after --passthrough is prompt text'
[[ "$(req codex fast continue)" == continue ]] || fail 'preset continue must reach history'
[[ -z "$(req codex fast resume $CODEX_ID extra)" ]] || fail 'extra args keep the generic path'
[[ -z "$(req codex exec resume $CODEX_ID)" ]] || fail 'exec resume is not interactive history'
echo 'PASS: L1.1 Codex wrapper and preset resume forms reach history'

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

# --- L1.2 Codex: wrapper form reaches prepare; failure refuses with alternatives ------
CALLS="$TMP/calls"; : > "$CALLS"
_harness_launcher_codex_history_select() { print -r -- "native_id=$2"; }
_harness_launcher_codex_history_lease_acquire() { return 0; }
_harness_launcher_codex_history_lease_release() { return 0; }
_harness_launcher_codex_history_prepare() { print -r -- "prepare $2" >> "$CALLS"; return 1; }
run 0 codex --passthrough resume $CODEX_ID
grep -qx "prepare $CODEX_ID" "$CALLS" || fail 'wrapper --passthrough resume did not reach history prepare'
: > "$CALLS"
run 0 codex fast resume $CODEX_ID
grep -qx "prepare $CODEX_ID" "$CALLS" || fail 'preset resume did not reach history prepare'
# A resume form the history request does not recognize (model flag) falls to the
# owner check: rc 4 (canonical rollout) now prepares instead of the legacy route.
: > "$CALLS"
run 1 codex --model gpt-5.5 resume $CODEX_ID
grep -qx "prepare $CODEX_ID" "$CALLS" || fail 'rc 4 Codex continuation did not reach history prepare'
[[ $RC == 2 ]] || fail "failed prepare must refuse, got $RC"
want_alternatives 'codex prepare failure' codex --model gpt-5.5 resume $CODEX_ID
echo 'PASS: L1.2 Codex canonical continuation prepares fresh isolated history or refuses'
echo 'PASS: canonical continuation refusal'
