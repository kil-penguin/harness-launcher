#!/usr/bin/env bash
# test-terminal-runtime-launch.sh — every agent launch path scrubs foreign
# terminal-runtime variables, exports HARNESS_TERMINAL_RUNTIME, announces the
# run directory to herdr with one OSC 7, and starts a cmux title broker only in
# cmux.
#
# The launchers run from an installed copy of bin/ (test/lib/install-runtime-
# fixture.sh) with stub claude, codex and kiro-cli executables that dump their
# environment to a file. A fake title-sync helper stands in for the broker: on
# start it logs its argv and calls the stub `cmux` the way the real broker's
# first rename does, so "the broker was attempted" is observable. Launch paths:
#
#   exec-claude   harness-exec <harness> base      (direct Claude)
#   auto-claude   harness-auto claude base         (from inside the harness)
#   shell-claude  plain `claude` after harness_shell_enable
#   shell-codex   plain `codex -C <harness>` with harness_shell_enable off
#   exec-codex    harness-exec <harness> codex base
#   harness-codex harness-codex exec x             (SDK-host Codex executable)
#   exec-kiro     harness-exec <harness> kiro-cli
#   exec-tui      harness-exec <harness> --no-isolated (launcher.sh TUI, stubbed)
#   exec-claude-cwd  harness-exec <harness> --cwd <harness> base (run dir differs
#                 from the caller's directory)
#   exec-tui-cwd  harness-exec <harness> --cwd <harness> --no-isolated (TUI quit without a
#                 launch: the stub returns)
#
# In herdr the launcher announces the run directory before the agent starts
# (the stubs snapshot the capture on start) and the caller's directory once the
# agent or TUI returns: two OSC 7 sequences in total.
#
# Each path runs against four environments (herdr + stale cmux/Orca markers,
# Orca + stale cmux, cmux only, no markers). A launch with a terminal (a pty on
# stdin and stdout) reports the marker runtime; a launch without one is `plain`
# whatever the markers say.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
TMP="$(cd "$TMP" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT

case "$TMP" in
  *[!A-Za-z0-9._/-]*)
    echo "FAIL: TMPDIR contains bytes this test does not pre-encode: $TMP"
    exit 1
    ;;
esac

PREFIX="$TMP/prefix"
SHARE="$PREFIX/share/harness-launcher"
HOME_DIR="$TMP/home"
BIN_DIR="$HOME_DIR/.local/bin"
STUB_BIN="$TMP/stub-bin"
HARNESS="$HOME_DIR/alpha harness"
APP="$HARNESS/projects/app"
CASES="$TMP/cases"
SOCK="$TMP/herdr.sock"

mkdir -p "$HARNESS/config" "$HARNESS/.claude" "$APP" "$BIN_DIR" "$STUB_BIN" "$CASES"
cat > "$HARNESS/config/launcher.env" <<'EOF'
HARNESS_NAME="alpha test"
HARNESS_PREFIX="alpha"
EOF
HARNESS_REAL="$(cd "$HARNESS" && pwd -P)"
APP_REAL="$(cd "$APP" && pwd -P)"

python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1])' "$SOCK"
[[ -S "$SOCK" ]] || { echo "FAIL: socket fixture was not created"; exit 1; }

bash "$ROOT/test/lib/install-runtime-fixture.sh" "$ROOT" "$PREFIX"
HOME="$HOME_DIR" HARNESS_PROFILE_BIN_DIR="$BIN_DIR" \
  "$PREFIX/bin/harness-profile" register "$HARNESS" >/dev/null

# Home preparation is not under test; the stubs only create the directories.
cat > "$SHARE/codex-home-prepare.sh" <<'EOF'
#!/usr/bin/env bash
mkdir -p "$1/.harness/codex"
EOF
cat > "$SHARE/kiro-home-prepare.sh" <<'EOF'
#!/usr/bin/env bash
mkdir -p "$1/.harness/kiro"
EOF
# The TUI is stubbed: the launcher only has to reach it with the right env.
# Explicit shared selection bypasses the separately tested workspace chooser.
cat > "$SHARE/launcher.sh" <<'EOF'
#!/usr/bin/env bash
cat "$HARNESS_TERMINAL_TTY" > "$STUB_DUMP.osc0" 2>/dev/null || true
env | LC_ALL=C sort > "$STUB_DUMP"
EOF
# Fake broker: the same first two effects as the real one (a log line, then a
# rename through the stub cmux), then it idles until the launcher stops it.
cat > "$SHARE/codex-cmux-title-sync.py" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$BROKER_LOG"
cmux rename-tab "$3" "$4" >/dev/null 2>&1
exec sleep 30
EOF
chmod 755 "$SHARE/codex-home-prepare.sh" "$SHARE/kiro-home-prepare.sh" \
  "$SHARE/launcher.sh" "$SHARE/codex-cmux-title-sync.py"

for agent in claude codex kiro-cli; do
  cat > "$STUB_BIN/$agent" <<'EOF'
#!/usr/bin/env bash
cat "$HARNESS_TERMINAL_TTY" > "$STUB_DUMP.osc0" 2>/dev/null || true
env | LC_ALL=C sort > "$STUB_DUMP"
# A broker is started asynchronously; hold the agent until its rename lands
# (up to 30 s; the loop ends as soon as it does).
if [ -n "${CLAUDE_CMUX_TITLE_REQUEST_FILE:-}${CODEX_CMUX_TITLE_REQUEST_FILE:-}" ]; then
  for _ in $(seq 1 600); do [ -s "$STUB_CMUX_LOG" ] && break; sleep 0.05; done
fi
exit 0
EOF
  chmod 755 "$STUB_BIN/$agent"
done
cat > "$STUB_BIN/cmux" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_CMUX_LOG"
EOF
chmod 755 "$STUB_BIN/cmux"

# Every launch goes through run-bounded.py: stdin is /dev/null or a silent pty,
# the command runs in its own session under a hard time limit (exit 124 on
# timeout, which fails the case with a clear message), and OSC 7 output can only
# land in the HARNESS_TERMINAL_TTY capture file, never on a real terminal.
BOUNDED="$ROOT/test/lib/run-bounded.py"
LAUNCH_LIMIT=60

failures=0
checks=0
fail() {
  failures=$((failures + 1))
  printf 'FAIL: %s\n' "$1"
  shift
  [[ $# -eq 0 ]] || printf '%s\n' "$@"
}

FILTER='^(CMUX_|ORCA_|HERDR_|TERM_PROGRAM=|HARNESS_TERMINAL_RUNTIME=)'
CMUX_IDS=(CMUX_WORKSPACE_ID=workspace:7 CMUX_TAB_ID=tab:24 CMUX_SURFACE_ID=surface:24)

# input_env INPUT sets INPUT_ENV (the markers the launch inherits).
input_env() {
  case "$1" in
    a) INPUT_ENV=(HERDR_ENV=1 HERDR_PANE_ID=w1:p1 "HERDR_SOCKET_PATH=$SOCK"
         "${CMUX_IDS[@]}" CMUX_SOCKET_PATH=/tmp/cmux.sock
         ORCA_TERMINAL_HANDLE=term_1 ORCA_PANE_KEY=k TERM_PROGRAM=Orca) ;;
    b) INPUT_ENV=("${CMUX_IDS[@]}" ORCA_TERMINAL_HANDLE=term_1 ORCA_PANE_KEY=k) ;;
    c) INPUT_ENV=("${CMUX_IDS[@]}" CMUX_SOCKET_PATH=/tmp/cmux.sock) ;;
    d) INPUT_ENV=(NOT_A_MARKER=1) ;;
  esac
}

# expect_for INPUT MODE sets EXP_RT and EXP_LINES (the surviving marker lines).
expect_for() {
  local input="$1" mode="$2" rt
  if [[ "$mode" == tty ]]; then
    case "$input" in a) rt=herdr ;; b) rt=orca ;; c) rt=cmux ;; d) rt=plain ;; esac
  else
    rt=plain
  fi
  EXP_RT="$rt"
  EXP_LINES="HARNESS_TERMINAL_RUNTIME=$rt"
  case "$rt" in
    herdr) EXP_LINES="$EXP_LINES
HERDR_ENV=1
HERDR_PANE_ID=w1:p1
HERDR_SOCKET_PATH=$SOCK" ;;
    orca) EXP_LINES="$EXP_LINES
ORCA_PANE_KEY=k
ORCA_TERMINAL_HANDLE=term_1" ;;
    cmux)
      EXP_LINES="$EXP_LINES
CMUX_SOCKET_PATH=/tmp/cmux.sock
CMUX_SURFACE_ID=surface:24
CMUX_TAB_ID=tab:24
CMUX_WORKSPACE_ID=workspace:7" ;;
  esac
  EXP_LINES="$(printf '%s\n' "$EXP_LINES" | LC_ALL=C sort)"
}

# path_cmd PATH sets PATH_CMD (argv), PATH_CWD, OSC_DIR (the directory the
# agent starts in), CALLER_DIR (the caller's directory, announced again after
# the agent returns) and HAS_BROKER (1 when the path starts a cmux title broker).
path_cmd() {
  PATH_CWD="$APP"
  OSC_DIR="$APP_REAL"
  CALLER_DIR="$APP_REAL"
  HAS_BROKER=1
  case "$1" in
    exec-claude)   PATH_CMD=("$PREFIX/bin/harness-exec" "$HARNESS" base) ;;
    auto-claude)   PATH_CMD=("$PREFIX/bin/harness-auto" claude base) ;;
    shell-claude)  PATH_CMD=(/bin/zsh -c 'source "$1"; harness_shell_enable || exit 9; claude' _ "$SHARE/aliases.zsh") ;;
    shell-codex)   PATH_CMD=(/bin/zsh -c 'source "$1"; codex -C "$2" exec x; rc=$?; print -r -- "CMUX=${CMUX_WORKSPACE_ID-unset} RT=${HARNESS_TERMINAL_RUNTIME-unset}" > "$3"; exit $rc' _ "$SHARE/aliases.zsh" "$HARNESS" "$CASE_DIR/shell-after")
                   OSC_DIR="$HARNESS_REAL" ;;
    exec-codex)    PATH_CMD=("$PREFIX/bin/harness-exec" "$HARNESS" codex base) ;;
    harness-codex) PATH_CMD=("$PREFIX/bin/harness-codex" exec x) ;;
    exec-kiro)     PATH_CMD=("$PREFIX/bin/harness-exec" "$HARNESS" kiro-cli); HAS_BROKER=0 ;;
    exec-tui)      PATH_CMD=("$PREFIX/bin/harness-exec" "$HARNESS" --no-isolated); HAS_BROKER=0 ;;
    exec-claude-cwd) PATH_CMD=("$PREFIX/bin/harness-exec" "$HARNESS" --cwd "$HARNESS" base)
                   OSC_DIR="$HARNESS_REAL" ;;
    exec-tui-cwd)  PATH_CMD=("$PREFIX/bin/harness-exec" "$HARNESS" --cwd "$HARNESS" --no-isolated); HAS_BROKER=0
                   OSC_DIR="$HARNESS_REAL" ;;
  esac
}

# run_case PATH INPUT MODE runs one launch into $CASE_DIR (MODE tty | notty).
run_case() {
  local path="$1" input="$2" mode="$3"
  CASE_DIR="$CASES/$path.$input.$mode"
  mkdir -p "$CASE_DIR"
  input_env "$input"
  path_cmd "$path"
  local rc=0
  (
    cd "$PATH_CWD"
    python3 "$BOUNDED" "$mode" "$LAUNCH_LIMIT" "$CASE_DIR/out" env -i \
      HOME="$HOME_DIR" PATH="$STUB_BIN:$PATH" TERM=xterm-256color LANG=en_US.UTF-8 \
      HARNESS_CODEX_BIN="$STUB_BIN/codex" HARNESS_KIRO_BIN="$STUB_BIN/kiro-cli" \
      HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$TMP/missing-marketplace" \
      STUB_DUMP="$CASE_DIR/agent.env" BROKER_LOG="$CASE_DIR/broker.log" \
      STUB_CMUX_LOG="$CASE_DIR/cmux.log" HARNESS_TERMINAL_TTY="$CASE_DIR/osc" \
      "${INPUT_ENV[@]}" "${PATH_CMD[@]}" \
      </dev/null
  ) || rc=$?
  CASE_RC="$rc"
}

seq7() { printf '\033]7;file://%s\007' "$1"; }
enc() { printf '%s' "$1" | sed 's/ /%20/g'; }

check_case() {
  local path="$1" input="$2" mode="$3" label
  label="$path / $input / $mode"
  run_case "$path" "$input" "$mode"
  expect_for "$input" "$mode"
  checks=$((checks + 1))
  if [[ "$CASE_RC" -eq 124 ]]; then
    fail "$label: launch timed out after ${LAUNCH_LIMIT}s (blocked on input or a terminal?)" \
      "$(sed 's/^/  /' "$CASE_DIR/out")"
    return 0
  fi
  if [[ "$CASE_RC" -ne 0 || ! -s "$CASE_DIR/agent.env" ]]; then
    fail "$label: agent did not run (rc=$CASE_RC)" "$(sed 's/^/  /' "$CASE_DIR/out")"
    return 0
  fi
  # On a pty the real /dev/tty is the transcript: an OSC 7 in it would mean a
  # launch wrote to the terminal instead of the HARNESS_TERMINAL_TTY capture.
  if grep -q $'\033]7;' "$CASE_DIR/out"; then
    fail "$label: OSC 7 reached the terminal instead of the capture file"
  fi
  local actual
  actual="$(grep -E "$FILTER" "$CASE_DIR/agent.env" | LC_ALL=C sort || true)"
  if [[ "$actual" != "$EXP_LINES" ]]; then
    fail "$label: surviving runtime variables differ" "--- expected" "$EXP_LINES" "--- actual" "$actual"
  fi
  # OSC 7, herdr only: exactly one sequence naming the run directory when the
  # agent starts, then one naming the caller's directory after it returns.
  if [[ "$EXP_RT" == herdr ]]; then
    if ! seq7 "$(enc "$OSC_DIR")" | cmp -s - "$CASE_DIR/agent.env.osc0" 2>/dev/null; then
      fail "$label: OSC 7 at agent start is not exactly one sequence for $OSC_DIR" \
        "$(od -An -c "$CASE_DIR/agent.env.osc0" 2>&1 | head -20)"
    fi
    if ! { seq7 "$(enc "$OSC_DIR")"; seq7 "$(enc "$CALLER_DIR")"; } | cmp -s - "$CASE_DIR/osc" 2>/dev/null; then
      fail "$label: OSC 7 capture is not the run directory $OSC_DIR then the caller directory $CALLER_DIR" \
        "$(od -An -c "$CASE_DIR/osc" 2>&1 | head -20)"
    fi
  elif [[ -s "$CASE_DIR/osc" ]]; then
    fail "$label: unexpected OSC 7 output" "$(od -An -c "$CASE_DIR/osc" | head -20)"
  fi
  # cmux title broker: attempted only in cmux, and only on paths that have one.
  local broker_var=0
  grep -Eq '^(CLAUDE|CODEX)_CMUX_TITLE_REQUEST_FILE=' "$CASE_DIR/agent.env" && broker_var=1
  if [[ "$EXP_RT" == cmux && "$HAS_BROKER" == 1 ]]; then
    [[ "$broker_var" == 1 ]] || fail "$label: cmux launch did not start a title broker"
    grep -q '^rename-tab surface:24 alpha' "$CASE_DIR/cmux.log" 2>/dev/null \
      || fail "$label: stub cmux rename was not called" "$(cat "$CASE_DIR/cmux.log" 2>&1)"
  else
    [[ "$broker_var" == 0 ]] || fail "$label: a title broker was started"
    [[ ! -s "$CASE_DIR/cmux.log" && ! -s "$CASE_DIR/broker.log" ]] \
      || fail "$label: cmux or the broker helper was called" \
        "$(cat "$CASE_DIR/cmux.log" "$CASE_DIR/broker.log" 2>&1)"
  fi
  # The interactive shell behind the plain codex wrapper keeps its own env.
  if [[ "$path" == shell-codex ]]; then
    local want_cmux="unset"
    [[ "$input" == d ]] || want_cmux="workspace:7"
    [[ "$(cat "$CASE_DIR/shell-after" 2>/dev/null)" == "CMUX=$want_cmux RT=unset" ]] \
      || fail "$label: the wrapper changed the interactive shell environment" \
        "$(cat "$CASE_DIR/shell-after" 2>&1)"
  fi
  return 0
}

PATHS="exec-claude auto-claude shell-claude shell-codex exec-codex harness-codex exec-kiro exec-tui exec-claude-cwd exec-tui-cwd"
for path in $PATHS; do
  before="$failures"
  for input in a b c; do
    check_case "$path" "$input" tty
    check_case "$path" "$input" notty
  done
  check_case "$path" d tty
  [[ "$failures" -ne "$before" ]] || printf 'ok: %s\n' "$path"
done

# A plain `codex` outside any harness is not a harness launch and stays as is.
before="$failures"
CASE_DIR="$CASES/native-codex"
mkdir -p "$CASE_DIR"
input_env a
(
  cd "$APP"
  python3 "$BOUNDED" tty "$LAUNCH_LIMIT" "$CASE_DIR/out" env -i \
    HOME="$HOME_DIR" PATH="$STUB_BIN:$PATH" TERM=xterm-256color LANG=en_US.UTF-8 \
    STUB_DUMP="$CASE_DIR/agent.env" STUB_CMUX_LOG="$CASE_DIR/cmux.log" \
    HARNESS_TERMINAL_TTY="$CASE_DIR/osc" "${INPUT_ENV[@]}" \
    /bin/zsh -c 'source "$1"; codex exec x' _ "$SHARE/aliases.zsh" \
    </dev/null
) || fail "native codex: launch failed or timed out" "$(cat "$CASE_DIR/out")"
checks=$((checks + 1))
grep -qx 'CMUX_WORKSPACE_ID=workspace:7' "$CASE_DIR/agent.env" \
  && grep -qx 'ORCA_TERMINAL_HANDLE=term_1' "$CASE_DIR/agent.env" \
  && ! grep -q '^HARNESS_TERMINAL_RUNTIME=' "$CASE_DIR/agent.env" \
  && [[ ! -s "$CASE_DIR/osc" ]] \
  || fail "native codex: a launch outside any harness must keep the caller environment"
[[ "$failures" -ne "$before" ]] || echo 'ok: native codex outside a harness is untouched'

# --- Broker guards read the launch runtime ---------------------------------

# shellcheck source=../bin/harness-common.sh
source "$ROOT/bin/harness-common.sh"
GUARD_HELPER="$TMP/guard-helper"
cat > "$GUARD_HELPER" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GUARD_LOG"
exec sleep 30
EOF
chmod 755 "$GUARD_HELPER"
export GUARD_LOG="$TMP/guard.log"

# guard_case LABEL EXPECT(start|skip) [ENV=value ...]: both broker starts in a
# subshell with only the given runtime variables; EXPECT says whether they run.
guard_case() {
  local label="$1" expect="$2" started
  shift 2
  checks=$((checks + 1))
  started="$(
    unset HARNESS_TERMINAL_RUNTIME ORCA_TERMINAL_HANDLE TERM_PROGRAM \
      CMUX_WORKSPACE_ID CMUX_TAB_ID CMUX_SURFACE_ID HERDR_ENV HERDR_PANE_ID HERDR_SOCKET_PATH
    export CODEX_HOME="$TMP/guard-codex-home" HARNESS_PREFIX=alpha
    export CLAUDE_CMUX_TITLE_STATE_ROOT="$TMP/guard-claude-state"
    export "$@"
    harness_codex_cmux_broker_start "$GUARD_HELPER"
    codex_started="${CODEX_CMUX_TITLE_REQUEST_FILE:+1}"
    harness_codex_cmux_broker_stop
    harness_claude_cmux_broker_start "$GUARD_HELPER" "$TMP/guard-harness"
    claude_started="${CLAUDE_CMUX_TITLE_REQUEST_FILE:+1}"
    harness_claude_cmux_broker_stop
    echo "codex=${codex_started:-0} claude=${claude_started:-0}"
  )"
  case "$expect:$started" in
    start:"codex=1 claude=1"|skip:"codex=0 claude=0") ;;
    *) fail "broker guard: $label (expected $expect, got $started)" ;;
  esac
}
CMUX3=(CMUX_WORKSPACE_ID=workspace:7 CMUX_TAB_ID=tab:24 CMUX_SURFACE_ID=surface:24)
guard_case "cmux markers, no exported runtime (v0.34.0 behavior)" start "${CMUX3[@]}"
guard_case "exported runtime cmux with cmux markers" start HARNESS_TERMINAL_RUNTIME=cmux "${CMUX3[@]}"
guard_case "exported runtime plain overrides cmux markers" skip HARNESS_TERMINAL_RUNTIME=plain "${CMUX3[@]}"
guard_case "exported runtime herdr overrides cmux markers" skip HARNESS_TERMINAL_RUNTIME=herdr "${CMUX3[@]}"
guard_case "exported runtime orca overrides cmux markers" skip HARNESS_TERMINAL_RUNTIME=orca "${CMUX3[@]}"
guard_case "Orca marker with stale cmux markers, no exported runtime" skip ORCA_TERMINAL_HANDLE=term_1 "${CMUX3[@]}"
guard_case "TERM_PROGRAM=Orca with stale cmux markers" skip TERM_PROGRAM=Orca "${CMUX3[@]}"
guard_case "herdr socket with stale cmux markers" skip HERDR_ENV=1 HERDR_PANE_ID=w1:p1 "HERDR_SOCKET_PATH=$SOCK" "${CMUX3[@]}"
guard_case "no cmux markers" skip HARNESS_TERMINAL_RUNTIME=cmux
echo 'ok: broker guards'

# --- codex-cmux-title-sync.py session_start reads the launch runtime ----------

SYNC="$ROOT/bin/codex-cmux-title-sync.py"
STATE="$TMP/sync-state"
mkdir -p "$STATE" && chmod 700 "$STATE"
sleep 60 & OWNER_PID=$!
trap '{ kill "$OWNER_PID"; wait "$OWNER_PID"; } 2>/dev/null || true; rm -rf "$TMP"' EXIT
# hook_case LABEL EXPECT(write|skip) [ENV=value ...]
hook_case() {
  local label="$1" expect="$2" req="$STATE/req.$RANDOM"
  shift 2
  checks=$((checks + 1))
  : > "$req"; chmod 600 "$req"
  printf '%s\n' '{"hook_event_name":"SessionStart","session_id":"s-1"}' |
    perl -e 'alarm shift; exec @ARGV' 30 \
    env -i HOME="$HOME" PATH="$PATH" "$@" HARNESS_PREFIX=alpha CODEX_HOME="$TMP/sync-codex-home" \
      CMUX_SURFACE_ID=surface:1 CODEX_CMUX_TITLE_REQUEST_FILE="$req" \
      CODEX_CMUX_TITLE_STATE_DIR="$STATE" CODEX_CMUX_TITLE_OWNER_PID="$OWNER_PID" \
      python3 "$SYNC" || fail "session_start: $label exited non-zero or timed out"
  if [[ "$expect" == write && ! -s "$req" ]]; then
    fail "session_start: $label must write a broker request"
  elif [[ "$expect" == skip && -s "$req" ]]; then
    fail "session_start: $label must return without a broker request"
  fi
}
hook_case "no exported runtime (fallback)" write FOO=1
hook_case "runtime cmux" write HARNESS_TERMINAL_RUNTIME=cmux
hook_case "runtime herdr with a stale CMUX_SURFACE_ID" skip HARNESS_TERMINAL_RUNTIME=herdr
hook_case "runtime orca" skip HARNESS_TERMINAL_RUNTIME=orca
hook_case "runtime plain" skip HARNESS_TERMINAL_RUNTIME=plain
hook_case "empty runtime falls back to the cmux checks" write HARNESS_TERMINAL_RUNTIME=
hook_case "Orca handle without an exported runtime (L2 stays)" skip ORCA_TERMINAL_HANDLE=term_1
echo 'ok: session_start gating'

if [[ "$failures" -ne 0 ]]; then
  printf '\n%d failure(s) in %d checks\n' "$failures" "$checks"
  exit 1
fi
printf '\nPASS: terminal runtime on every launch path (%d checks)\n' "$checks"
