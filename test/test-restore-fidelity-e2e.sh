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
git -C "$HARNESS" init -q -b main
git -C "$HARNESS" config user.email test@example.invalid
git -C "$HARNESS" config user.name test
git -C "$HARNESS" add config/launcher.env && git -C "$HARNESS" commit -qm fixture
printf '{"type":"assistant","isSidechain":false,"message":{"model":"claude-opus-5-5"},"effort":"xhigh"}\n' > "$CCONF/projects/-tmp-proj/$CLAUDE_ID.jsonl"
# A well-formed rollout: the wrapper form reaches durable-history preparation like `codex resume <id>`.
{ printf '{"type":"session_meta","payload":{"id":"%s"}}\n' "$CODEX_ID"
  printf '{"type":"turn_context","payload":{"model":"gpt-6.1-sol","effort":"medium"}}\n'; } \
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
# The wrapper form reaches durable-history preparation like `codex resume <id>`:
# a canonical-only rollout no longer restores in place. It runs as a fresh
# isolated session (model/effort come from the history restored into that
# session's home, which this fixture's stub home-prepare does not populate).
[[ "$line" == *"<resume>"*"<$CODEX_ID>" ]] || fail "herdr codex restore lost the id: $line"
find "$STATE/sessions" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | grep -q . || fail 'canonical-only Codex restore did not create a fresh isolated session'
grep -Fxq 'marker:<unset>' <(sed -n '/^codex:/,/^marker/p' "$LOG") || fail 'host-default marker reached codex'
echo 'PASS: E2 shell `codex resume <id>` restores a canonical-only rollout as a fresh isolated session through harness-auto'

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

echo 'PASS: restore fidelity end to end'
