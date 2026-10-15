#!/usr/bin/env zsh
# test-restore-fidelity-e2e.sh — the full chain a host restore takes: the
# interactive `claude --resume <id>` / `codex resume <id>` shell function ->
# harness-auto -> harness-exec -> launcher -> agent. Runs real routing scripts
# against fake agents and checks the argv and environment the agent receives.
set -eu
unset HARNESS_TERMINAL_RUNTIME TERM_PROGRAM HARNESS_HOST_DEFAULT_MODE; unset -m 'HERDR_*' 'ORCA_*' 'CMUX_*' || true

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"; TMP="$(cd "$TMP" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT
PREFIX="$TMP/prefix"; HOME_DIR="$TMP/home"; PROFILE_BIN="$HOME_DIR/.local/bin"
HARNESS="$HOME_DIR/alpha"; PROJECT="$HARNESS/projects/app"; NATIVE_BIN="$TMP/native-bin"
STATE="$TMP/state"; CCONF="$TMP/claude-config"; LOG="$TMP/native.log"
mkdir -p "$HARNESS/config" "$PROJECT" "$PROFILE_BIN" "$NATIVE_BIN" "$CCONF/projects/-tmp-proj" "$HARNESS/.harness/codex/sessions/2026/09/30"
cat > "$HARNESS/config/launcher.env" <<'ENV'
HARNESS_NAME="alpha test"
HARNESS_PREFIX="alpha"
HARNESS_SESSION_ISOLATION_DEFAULT="0"
ENV
bash "$ROOT/test/lib/install-runtime-fixture.sh" "$ROOT" "$PREFIX"
HOME="$HOME_DIR" HARNESS_PROFILE_BIN_DIR="$PROFILE_BIN" "$PREFIX/bin/harness-profile" register "$HARNESS" >/dev/null
printf '#!/usr/bin/env bash\nmkdir -p "$1/.harness/codex"\n' > "$PREFIX/share/harness-launcher/codex-home-prepare.sh"
chmod 755 "$PREFIX/share/harness-launcher/codex-home-prepare.sh"
for runtime in claude codex; do
  cat > "$NATIVE_BIN/$runtime" <<'STUB'
#!/usr/bin/env bash
{
  printf '%s:' "$(basename "$0")"; printf ' <%s>' "$@"; printf '\n'
  printf 'marker:<%s>\n' "${HARNESS_HOST_DEFAULT_MODE-unset}"
} >> "$HARNESS_TEST_LOG"
STUB
  chmod 755 "$NATIVE_BIN/$runtime"
done
fail() { echo "FAIL: $1" >&2; sed 's/^/  log: /' "$LOG" >&2 2>/dev/null || true; exit 1; }

CLAUDE_ID=0b5d1f3e-0000-4000-8000-000000000001
CODEX_ID=01a0ef50-1bec-7852-afb3-a6b6559f47b6
printf '{"type":"assistant","isSidechain":false,"message":{"model":"claude-opus-5-5"},"effort":"xhigh"}\n' > "$CCONF/projects/-tmp-proj/$CLAUDE_ID.jsonl"
printf '{"type":"turn_context","payload":{"model":"gpt-6.1-sol","effort":"medium"}}\n' \
  > "$HARNESS/.harness/codex/sessions/2026/09/30/rollout-2026-09-30T01-02-03-$CODEX_ID.jsonl"

export HOME="$HOME_DIR" PATH="$NATIVE_BIN:/usr/bin:/bin" HARNESS_TEST_LOG="$LOG" HARNESS_SESSION_STATE_HOME="$STATE"
export CLAUDE_CONFIG_DIR="$CCONF" HARNESS_CODEX_MCP_PROFILE="" _HARNESS_LAUNCHER_SHELL_AUTO_ENABLED=9
source "$PREFIX/share/harness-launcher/aliases.zsh"
harness_register "$HARNESS"
harness_shell_enable

: > "$LOG"
(cd "$PROJECT" && claude --resume $CLAUDE_ID) </dev/null >/dev/null 2>"$TMP/claude.err" || fail 'herdr claude restore failed'
line="$(grep '^claude:' "$LOG")"
[[ "$line" == *"<--model> <claude-opus-5-5>"* && "$line" == *"<--effort> <xhigh>"* && "$line" == *"<--resume> <$CLAUDE_ID>"* ]] \
  || fail "herdr claude restore lost the model/effort/id: $line"
[[ "$line" != *"<sonnet>"* ]] || fail 'herdr claude restore kept the host default model'
grep -Fxq 'marker:<unset>' <(sed -n '/^claude:/,/^marker/p' "$LOG") || fail 'host-default marker reached claude'
echo 'PASS: E1 shell `claude --resume <id>` restores model and effort through harness-auto'

: > "$LOG"
(cd "$PROJECT" && codex resume $CODEX_ID) </dev/null >/dev/null 2>"$TMP/codex.err" || fail 'herdr codex restore failed'
line="$(grep '^codex:' "$LOG")"
[[ "$line" == *"<-p> <base> <-m> <gpt-6.1-sol> <-c> <model_reasoning_effort=\"medium\">"*"<resume> <$CODEX_ID>" ]] \
  || fail "herdr codex restore lost the model/effort: $line"
grep -Fxq 'marker:<unset>' <(sed -n '/^codex:/,/^marker/p' "$LOG") || fail 'host-default marker reached codex'
echo 'PASS: E2 shell `codex resume <id>` restores model and effort through harness-auto'

# A caller flag keeps the launcher out of it.
: > "$LOG"
(cd "$PROJECT" && claude --resume $CLAUDE_ID --model haiku) </dev/null >/dev/null 2>&1 || fail 'claude restore with caller model failed'
line="$(grep '^claude:' "$LOG")"
[[ "$line" == *"<--model> <haiku>"* && "$line" != *"claude-opus-5-5"* ]] || fail "caller model must win: $line"
echo 'PASS: E3 a caller --model wins end to end'

# `<prefix> --resume <id>` (Orca without keyword defaults, or a user) skips the TUI.
: > "$LOG"
(cd "$PROJECT" && alpha --resume $CLAUDE_ID) </dev/null >/dev/null 2>"$TMP/alpha.err" || fail 'prefix restore failed'
line="$(grep '^claude:' "$LOG")"
[[ "$line" == *"<--resume> <$CLAUDE_ID>"* && "$line" == *"<--model> <claude-opus-5-5>"* ]] || fail "prefix restore dropped the id or model: $line"
echo 'PASS: E4 `<prefix> --resume <id>` launches directly with the restored settings'

# --- isolation-default profile (HARNESS_SESSION_ISOLATION_DEFAULT=1) ------------------
# The same wrapper chain on a TTY: a canonical-only rollout is no longer resumed in
# place. The pure wrapper form becomes a fresh isolated session through durable
# history; the launcher still runs the real routing scripts against the fake agent.
HARNESS2="$HOME_DIR/beta"; PROJECT2="$HARNESS2/projects/app"
mkdir -p "$HARNESS2/config" "$PROJECT2" "$HARNESS2/.harness/codex/sessions/2026/09/30"
cat > "$HARNESS2/config/launcher.env" <<'ENV'
HARNESS_NAME="beta test"
HARNESS_PREFIX="beta"
HARNESS_SESSION_ISOLATION_DEFAULT="1"
ENV
git -C "$HARNESS2" init -q -b main
git -C "$HARNESS2" config user.email test@example.invalid
git -C "$HARNESS2" config user.name test
git -C "$HARNESS2" add config/launcher.env && git -C "$HARNESS2" commit -qm fixture
{ printf '{"type":"session_meta","payload":{"id":"%s"}}\n' "$CODEX_ID"
  printf '{"type":"turn_context","payload":{"model":"gpt-6.1-sol","effort":"medium"}}\n'; } \
  > "$HARNESS2/.harness/codex/sessions/2026/09/30/rollout-2026-09-30T01-02-03-$CODEX_ID.jsonl"
HOME="$HOME_DIR" HARNESS_PROFILE_BIN_DIR="$PROFILE_BIN" "$PREFIX/bin/harness-profile" register "$HARNESS2" >/dev/null
cat > "$TMP/tty-run.zsh" <<'TTY'
source "$PREFIX/share/harness-launcher/aliases.zsh"
harness_register "$HARNESS2"
harness_shell_enable
cd "$PROJECT2" && codex resume "$CODEX_ID"
TTY
: > "$LOG"
before_sessions="$(find "$STATE/sessions" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
export PREFIX HARNESS2 PROJECT2 CODEX_ID
expect -c 'set timeout 60; log_user 0; spawn zsh '"$TMP"'/tty-run.zsh; expect eof; catch wait r; exit [lindex $r 3]' \
  || fail 'isolation-default wrapper restore failed on a TTY'
line="$(grep '^codex:' "$LOG")"
[[ "$line" == *"<resume>"*"<$CODEX_ID>" ]] || fail "isolation-default wrapper restore lost the id: $line"
after_sessions="$(find "$STATE/sessions" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
(( after_sessions == before_sessions + 1 )) || fail 'isolation-default canonical-only Codex restore did not create a fresh isolated session'
echo 'PASS: E5 isolation-default wrapper restore of a canonical-only rollout starts a fresh isolated session'

echo 'PASS: restore fidelity end to end'
