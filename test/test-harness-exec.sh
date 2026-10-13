#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
TMP="$(cd "$TMP" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT
PREFIX="$TMP/prefix"
HARNESS="$TMP/test harness"
WORKTREE="$HARNESS/.worktrees/sample"
STUB_BIN="$TMP/stub-bin"
LOG="$TMP/exec.log"

mkdir -p "$HARNESS/config" "$WORKTREE" "$STUB_BIN"
HARNESS_REAL="$(cd "$HARNESS" && pwd -P)"
WORKTREE_REAL="$(cd "$WORKTREE" && pwd -P)"
cat > "$HARNESS/config/launcher.env" <<'EOF'
HARNESS_NAME="test harness"
HARNESS_PREFIX="th"
EOF

bash "$ROOT/test/lib/install-runtime-fixture.sh" "$ROOT" "$PREFIX"

cat > "$PREFIX/share/harness-launcher/codex-home-prepare.sh" <<'EOF'
#!/usr/bin/env bash
set -e
mkdir -p "$1/.harness/codex"
EOF
chmod +x "$PREFIX/share/harness-launcher/codex-home-prepare.sh"

cat > "$STUB_BIN/codex" <<'EOF'
#!/usr/bin/env bash
{
  printf 'PWD:%s\n' "$PWD"
  printf 'CODEX_HOME:%s\n' "${CODEX_HOME:-}"
  printf 'ARGV:'
  printf ' <%s>' "$@"
  printf '\n'
} > "$HARNESS_EXEC_TEST_LOG"
EOF
chmod +x "$STUB_BIN/codex"

PATH="$STUB_BIN:/usr/bin:/bin" \
HARNESS_CODEX_BIN="$STUB_BIN/codex" \
HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$TMP/missing-marketplace" \
HARNESS_EXEC_TEST_LOG="$LOG" \
  "$PREFIX/bin/harness-exec" "$HARNESS" --cwd "$WORKTREE" codex base

assert_line() {
  local expected="$1"
  if ! grep -Fqx "$expected" "$LOG"; then
    echo "FAIL: missing log line: $expected" >&2
    sed 's/^/  /' "$LOG" >&2
    exit 1
  fi
}

assert_line "PWD:$WORKTREE_REAL"
assert_line "CODEX_HOME:$HARNESS_REAL/.harness/codex"
if ! grep -Fq "ARGV: <--cd> <$WORKTREE_REAL> <-p> <base>" "$LOG"; then
  echo "FAIL: explicit worktree was not forwarded to Codex" >&2
  sed 's/^/  /' "$LOG" >&2
  exit 1
fi

echo "PASS: harness-exec launches a profile-scoped Codex session in an explicit worktree"

: > "$LOG"
(
  cd "$WORKTREE"
  PATH="$STUB_BIN:/usr/bin:/bin" \
    HARNESS_CODEX_BIN="$STUB_BIN/codex" \
    HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$TMP/missing-marketplace" \
    HARNESS_EXEC_TEST_LOG="$LOG" \
    "$PREFIX/bin/harness-exec" "$HARNESS" codex base
)
assert_line "PWD:$WORKTREE_REAL"
if ! grep -Fq "ARGV: <--cd> <$WORKTREE_REAL> <-p> <base>" "$LOG"; then
  echo "FAIL: implicit workspace cwd was not forwarded to Codex" >&2
  sed 's/^/  /' "$LOG" >&2
  exit 1
fi

echo "PASS: harness-exec uses the current workspace when it is inside the harness boundary"

: > "$LOG"
PATH="$STUB_BIN:/usr/bin:/bin" \
  HARNESS_CODEX_BIN="$STUB_BIN/codex" \
  HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$TMP/missing-marketplace" \
  HARNESS_EXEC_TEST_LOG="$LOG" \
  WORKTREE="$WORKTREE" \
  HARNESS="$HARNESS" \
  zsh -c '
    source "'$PREFIX'/share/harness-launcher/aliases.zsh"
    harness_register "$HARNESS"
    cd "$WORKTREE"
    th codex base
  '
assert_line "PWD:$WORKTREE_REAL"
if ! grep -Fq "ARGV: <--cd> <$WORKTREE_REAL> <-p> <base>" "$LOG"; then
  echo "FAIL: registered profile command did not preserve the current workspace" >&2
  sed 's/^/  /' "$LOG" >&2
  exit 1
fi

echo "PASS: registered profile commands use the current workspace"

for session in fork; do
  : > "$LOG"
  PATH="$STUB_BIN:/usr/bin:/bin" \
    HARNESS_CODEX_BIN="$STUB_BIN/codex" \
    HARNESS_EXEC_TEST_LOG="$LOG" \
    "$PREFIX/bin/harness-exec" "$HARNESS" --cwd "$WORKTREE" codex "$session"
  assert_line "PWD:$WORKTREE_REAL"
  grep -Fq "ARGV: <$session> <--cd> <$WORKTREE_REAL> <-p> <base>" "$LOG" || {
    echo "FAIL: Codex $session did not preserve the explicit worktree" >&2
    sed 's/^/  /' "$LOG" >&2
    exit 1
  }
done

echo "PASS: Codex fork preserves the explicit worktree"

: > "$LOG"
PATH="$STUB_BIN:/usr/bin:/bin" \
  HARNESS_CODEX_BIN="$STUB_BIN/codex" \
  HARNESS_EXEC_TEST_LOG="$LOG" \
  "$PREFIX/bin/harness-exec" "$HARNESS" --cwd "$WORKTREE" codex resume > "$TMP/empty-history.json"
[[ ! -s "$LOG" ]] || { echo 'FAIL: empty history catalog launched Native'; exit 1; }
grep -Fq '"entries": []' "$TMP/empty-history.json" || { echo 'FAIL: empty history catalog was not shown'; exit 1; }
echo 'PASS: Codex resume lists the source-local catalog before a Native selection'

if "$PREFIX/bin/harness-exec" "$HARNESS" --cwd >"$TMP/missing-cwd.out" 2>"$TMP/missing-cwd.err"; then
  echo "FAIL: harness-exec accepted --cwd without a directory" >&2
  exit 1
fi
grep -Fq -- '--cwd requires a directory' "$TMP/missing-cwd.err" || {
  echo "FAIL: missing --cwd value did not produce a clear error" >&2
  sed 's/^/  /' "$TMP/missing-cwd.err" >&2
  exit 1
}

echo "PASS: harness-exec rejects a missing --cwd value clearly"

INVALID_HARNESS="$TMP/not-a-harness"
mkdir -p "$INVALID_HARNESS"
if "$PREFIX/bin/harness-exec" "$INVALID_HARNESS" codex base >"$TMP/invalid.out" 2>"$TMP/invalid.err"; then
  echo "FAIL: harness-exec accepted a directory without config/launcher.env" >&2
  exit 1
fi
grep -Fq 'invalid harness directory' "$TMP/invalid.err"

echo "PASS: harness-exec requires a trusted launcher.env boundary"

OUTSIDE="$TMP/outside-worktree"
mkdir -p "$OUTSIDE"
: > "$LOG"
if PATH="$STUB_BIN:/usr/bin:/bin" \
  HARNESS_CODEX_BIN="$STUB_BIN/codex" \
  HARNESS_EXEC_TEST_LOG="$LOG" \
  "$PREFIX/bin/harness-exec" "$HARNESS" --cwd "$OUTSIDE" codex base \
  >"$TMP/outside.out" 2>"$TMP/outside.err"; then
  echo "FAIL: harness-exec accepted a cwd outside the registered harness" >&2
  exit 1
fi
[[ ! -s "$LOG" ]] || {
  echo "FAIL: outside cwd rejection launched Codex" >&2
  exit 1
}
grep -Fq -- '--cwd must stay inside the registered harness' "$TMP/outside.err"

echo "PASS: harness-exec rejects worktrees outside the profile boundary"

SYMLINK_ESCAPE="$HARNESS/.worktrees/escape"
ln -s "$OUTSIDE" "$SYMLINK_ESCAPE"
: > "$LOG"
if PATH="$STUB_BIN:/usr/bin:/bin" \
  HARNESS_CODEX_BIN="$STUB_BIN/codex" \
  HARNESS_EXEC_TEST_LOG="$LOG" \
  "$PREFIX/bin/harness-exec" "$HARNESS" --cwd "$SYMLINK_ESCAPE" codex base \
  >"$TMP/symlink.out" 2>"$TMP/symlink.err"; then
  echo "FAIL: harness-exec accepted a symlink escape" >&2
  exit 1
fi
[[ ! -s "$LOG" ]] || {
  echo "FAIL: symlink escape launched Codex" >&2
  exit 1
}
grep -Fq -- '--cwd must stay inside the registered harness' "$TMP/symlink.err"

echo "PASS: harness-exec rejects symlink escapes from the profile boundary"

CLAUDE_LOG="$TMP/claude.log"
FAKE_HOME="$TMP/home"
mkdir -p "$FAKE_HOME"
mkdir -p "$HARNESS/.claude"
cat > "$HARNESS/.claude/settings.local.json" <<'EOF'
{"env":{"GAMEDUO_SCM_TOKEN":"fixture-scm-token","HYPERDX_API_KEY":"fixture-hyperdx-key"}}
EOF
bash -c 'source "$1"; set -x; harness_export_local_env "$2"; set +x' _ \
  "$ROOT/bin/harness-common.sh" "$HARNESS" 2>"$TMP/bash-xtrace.err"
if grep -Eq 'fixture-(scm-token|hyperdx-key)' "$TMP/bash-xtrace.err"; then
  echo 'FAIL: Bash xtrace exposed harness-local MCP credentials' >&2
  exit 1
fi
cat > "$STUB_BIN/claude" <<'EOF'
#!/usr/bin/env bash
{
  printf 'PWD:%s\n' "$PWD"
  [[ "${GAMEDUO_SCM_TOKEN:-}" == fixture-scm-token && "${HYPERDX_API_KEY:-}" == fixture-hyperdx-key ]] && printf 'MCP_AUTH:loaded\n'
  printf 'ARGV:'
  printf ' <%s>' "$@"
  printf '\n'
} > "$HARNESS_EXEC_CLAUDE_LOG"
EOF
chmod +x "$STUB_BIN/claude"
PATH="$STUB_BIN:/usr/bin:/bin" \
HOME="$FAKE_HOME" \
HARNESS_EXEC_CLAUDE_LOG="$CLAUDE_LOG" \
  "$PREFIX/bin/harness-exec" "$HARNESS" --cwd "$WORKTREE" base

if ! grep -Fqx "PWD:$WORKTREE_REAL" "$CLAUDE_LOG"; then
  echo "FAIL: Claude did not start in the explicit worktree" >&2
  sed 's/^/  /' "$CLAUDE_LOG" >&2
  exit 1
fi
grep -Fqx 'MCP_AUTH:loaded' "$CLAUDE_LOG" || {
  echo "FAIL: Claude did not inherit harness-local MCP credentials in a nested worktree" >&2
  exit 1
}

echo "PASS: harness-exec launches Claude in the explicit worktree"

PROFILE_HOME="$TMP/profile-home"
PROFILE_BIN="$TMP/profile-bin"
HARNESS_PROFILE_HOME="$PROFILE_HOME" HARNESS_PROFILE_BIN_DIR="$PROFILE_BIN" \
  "$PREFIX/bin/harness-profile" register "$HARNESS" >/dev/null
: > "$CLAUDE_LOG"
(
  cd "$WORKTREE"
  PATH="$STUB_BIN:/usr/bin:/bin" \
  HOME="$FAKE_HOME" \
  HARNESS_PROFILE_HOME="$PROFILE_HOME" \
  HARNESS_EXEC_CLAUDE_LOG="$CLAUDE_LOG" \
    "$PREFIX/bin/harness-auto" claude base resume acceptEdits 'orca prompt'
)
if ! grep -Fqx "PWD:$WORKTREE_REAL" "$CLAUDE_LOG"; then
  echo "FAIL: harness-auto did not launch Claude in the selected worktree" >&2
  sed 's/^/  /' "$CLAUDE_LOG" >&2
  exit 1
fi
grep -Fqx 'MCP_AUTH:loaded' "$CLAUDE_LOG" || {
  echo "FAIL: harness-auto Claude did not inherit harness-local MCP credentials" >&2
  exit 1
}
grep -Fq 'ARGV: <--resume>' "$CLAUDE_LOG" || {
  echo "FAIL: harness-auto did not preserve the Claude resume selector" >&2
  sed 's/^/  /' "$CLAUDE_LOG" >&2
  exit 1
}
grep -Fq '<--permission-mode> <acceptEdits>' "$CLAUDE_LOG" || {
  echo "FAIL: harness-auto did not preserve the Claude permission selector" >&2
  sed 's/^/  /' "$CLAUDE_LOG" >&2
  exit 1
}
grep -Fq '<orca prompt>' "$CLAUDE_LOG" || {
  echo "FAIL: harness-auto did not preserve the Claude prompt" >&2
  sed 's/^/  /' "$CLAUDE_LOG" >&2
  exit 1
}

echo "PASS: harness-auto launches Claude through the real shared policy path"

KIRO_LOG="$TMP/kiro.log"
cat > "$PREFIX/share/harness-launcher/kiro-home-prepare.sh" <<'EOF'
#!/usr/bin/env bash
set -e
mkdir -p "$1/.harness/kiro"
EOF
chmod +x "$PREFIX/share/harness-launcher/kiro-home-prepare.sh"
cat > "$STUB_BIN/kiro-cli" <<'EOF'
#!/usr/bin/env bash
{
  printf 'PWD:%s\n' "$PWD"
  printf 'KIRO_HOME:%s\n' "${KIRO_HOME:-}"
  printf 'ARGV:'
  printf ' <%s>' "$@"
  printf '\n'
} > "$HARNESS_EXEC_KIRO_LOG"
EOF
chmod +x "$STUB_BIN/kiro-cli"
PATH="$STUB_BIN:/usr/bin:/bin" \
HOME="$FAKE_HOME" \
HARNESS_KIRO_BIN="$STUB_BIN/kiro-cli" \
HARNESS_EXEC_KIRO_LOG="$KIRO_LOG" \
  "$PREFIX/bin/harness-exec" "$HARNESS" --cwd "$WORKTREE" kiro-cli base

if ! grep -Fqx "PWD:$WORKTREE_REAL" "$KIRO_LOG"; then
  echo "FAIL: Kiro did not start in the explicit worktree" >&2
  sed 's/^/  /' "$KIRO_LOG" >&2
  exit 1
fi
grep -Fqx "KIRO_HOME:$HARNESS_REAL/.harness/kiro" "$KIRO_LOG"

echo "PASS: harness-exec launches Kiro in the explicit worktree"

TUI_LOG="$TMP/tui.log"
cat > "$PREFIX/share/harness-launcher/launcher.sh" <<'EOF'
#!/usr/bin/env bash
{
  printf 'HARNESS_DIR:%s\n' "${HARNESS_DIR:-}"
  printf 'HARNESS_RUN_DIR:%s\n' "${HARNESS_RUN_DIR:-}"
} > "$HARNESS_EXEC_TUI_LOG"
EOF
chmod +x "$PREFIX/share/harness-launcher/launcher.sh"
HOME="$FAKE_HOME" HARNESS_EXEC_TUI_LOG="$TUI_LOG" \
  "$PREFIX/bin/harness-exec" "$HARNESS" --cwd "$WORKTREE" --no-isolated

if ! grep -Fqx "HARNESS_RUN_DIR:$WORKTREE_REAL" "$TUI_LOG"; then
  echo "FAIL: launchpad did not receive the explicit worktree" >&2
  sed 's/^/  /' "$TUI_LOG" >&2
  exit 1
fi

echo "PASS: harness-exec forwards the explicit worktree to the launchpad"

# F1: an explicit --isolated launch runs in the session root. harness-exec
# must not inject the caller's cwd (or the canonical root) as --cwd, or the
# isolated run would work in the canonical checkout.
GIT_HARNESS="$TMP/git harness"
mkdir -p "$GIT_HARNESS/config" "$GIT_HARNESS/sub"
git -C "$GIT_HARNESS" init -q -b main
git -C "$GIT_HARNESS" config user.email test@example.invalid
git -C "$GIT_HARNESS" config user.name test
printf '%s\n' 'HARNESS_NAME="git harness"' 'HARNESS_PREFIX="gh"' > "$GIT_HARNESS/config/launcher.env"
: > "$GIT_HARNESS/sub/fixture"
git -C "$GIT_HARNESS" add config/launcher.env sub/fixture && git -C "$GIT_HARNESS" commit -qm initial
GIT_HARNESS_SUB_REAL="$(cd "$GIT_HARNESS/sub" && pwd -P)"
cat > "$STUB_BIN/claude" <<'EOF2'
#!/usr/bin/env bash
printf 'PWD:%s\nRUN:%s\nSESSION:%s\n' "$PWD" "${HARNESS_RUN_DIR:-}" "${HARNESS_SESSION_ROOT:-}" > "$HARNESS_EXEC_CLAUDE_LOG"
EOF2
chmod +x "$STUB_BIN/claude"
run_git_harness() {
  : > "$CLAUDE_LOG"
  (
    cd "$GIT_HARNESS/sub"
    PATH="$STUB_BIN:/usr/bin:/bin" HOME="$FAKE_HOME" \
      HARNESS_SESSION_STATE_HOME="$TMP/session-state" \
      HARNESS_EXEC_CLAUDE_LOG="$CLAUDE_LOG" \
      "$PREFIX/bin/harness-exec" "$GIT_HARNESS" "$@" 2>/dev/null
  )
}
run_git_harness --isolated --passthrough -p 'batch task'
session_root="$(sed -n 's/^SESSION://p' "$CLAUDE_LOG")"
[[ -n "$session_root" && "$session_root" == "$TMP/session-state/worktrees/"* ]] || {
  echo 'FAIL: explicit --isolated -p did not create an isolated session' >&2; sed 's/^/  /' "$CLAUDE_LOG" >&2; exit 1; }
grep -Fqx "PWD:$session_root" "$CLAUDE_LOG" && grep -Fqx "RUN:$session_root" "$CLAUDE_LOG" || {
  echo 'FAIL: explicit --isolated must run in the session root, not the injected caller cwd' >&2
  sed 's/^/  /' "$CLAUDE_LOG" >&2; exit 1; }

echo "PASS: harness-exec --isolated runs a non-interactive launch in the session root"

run_git_harness --passthrough -p 'batch task'
grep -Fqx "PWD:$GIT_HARNESS_SUB_REAL" "$CLAUDE_LOG" && grep -Fqx 'SESSION:' "$CLAUDE_LOG" || {
  echo 'FAIL: legacy non-interactive routing must keep the caller cwd and no session' >&2
  sed 's/^/  /' "$CLAUDE_LOG" >&2; exit 1; }

echo "PASS: harness-exec keeps the caller cwd for legacy non-interactive launches"

# The release dispatcher precreates a UUID and enters through
# --isolated-session, including for a fresh no-argument launch. Both isolated
# controls must default to the session root, even from a source subdirectory.
session_id="${session_root##*/}"
# Use the real TUI: a launcher stub misses its own run-directory boundary
# check, which rejected the canonical cwd injected for a precreated UUID.
cp -p "$ROOT/bin/launcher.sh" "$PREFIX/share/harness-launcher/launcher.sh"
MENU_OUT="$TMP/isolated-menu.out"
for caller in "$GIT_HARNESS/sub" "$TMP"; do
  (
    cd "$caller"
    PATH="$STUB_BIN:/usr/bin:/bin" HOME="$FAKE_HOME" \
      HARNESS_SESSION_STATE_HOME="$TMP/session-state" \
      HARNESS_SESSION_HEARTBEAT_SECONDS=0.1 \
      HARNESS_CODEX_BIN="$TMP/missing-codex" HARNESS_KIRO_BIN="$TMP/missing-kiro" \
      "$PREFIX/bin/harness-exec" "$GIT_HARNESS" --isolated-session "$session_id" \
      <<< 'q' > "$MENU_OUT" 2>&1
  ) || { echo 'FAIL: precreated UUID must open the real launcher menu' >&2; cat "$MENU_OUT" >&2; exit 1; }
  grep -Fq 'New session' "$MENU_OUT" || {
    echo 'FAIL: isolated entry did not reach the actual session picker' >&2; cat "$MENU_OUT" >&2; exit 1; }
done
echo "PASS: precreated UUID opens the real TUI from inside or outside the source"

run_git_harness --isolated-session "$session_id" --passthrough -p 'resumed task'
grep -Fqx "PWD:$session_root" "$CLAUDE_LOG" && grep -Fqx "RUN:$session_root" "$CLAUDE_LOG" || {
  echo 'FAIL: --isolated-session must default to the session root, not the caller cwd' >&2
  sed 's/^/  /' "$CLAUDE_LOG" >&2; exit 1; }
echo "PASS: harness-exec --isolated-session defaults to the session root"

run_git_harness --cwd "$GIT_HARNESS/sub" --isolated-session "$session_id" --passthrough -p 'product task'
grep -Fqx "PWD:$session_root/sub" "$CLAUDE_LOG" || {
  echo 'FAIL: explicit source subdirectory must map to the isolated clone' >&2
  sed 's/^/  /' "$CLAUDE_LOG" >&2; exit 1; }
echo "PASS: UUID shortcuts map an explicit source subdirectory to the clone"
