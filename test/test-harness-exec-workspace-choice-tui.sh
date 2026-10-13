#!/usr/bin/env bash
set -euo pipefail

# Real public entrypoint, chooser, fallback TUI, broker and home preparation;
# only the external native model CLI is doubled. Never use the caller's homes.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
HARNESS="$TMP/alpha" STATE="$TMP/state" BIN="$TMP/bin" TEST_HOME="$TMP/home"
PYTHON_BIN="$(python3 -c 'import sys; print(sys.executable)')"
mkdir -p "$HARNESS/config" "$HARNESS/nested" "$HARNESS/projects/beta" "$BIN" "$TEST_HOME"
git -C "$HARNESS" init -q -b main
git -C "$HARNESS" config user.email test@example.invalid
git -C "$HARNESS" config user.name test
printf 'HARNESS_NAME="alpha"\nHARNESS_PREFIX="alpha"\n' > "$HARNESS/config/launcher.env"
: > "$HARNESS/nested/file"; : > "$HARNESS/projects/beta/file"
git -C "$HARNESS" add -A && git -C "$HARNESS" commit -qm initial

run_choice() {
  local cwd="$1" log="$2" runtime="$3"
  rm -f "$BIN/claude" "$BIN/codex"
  cat > "$BIN/$runtime" <<'CLI'
#!/usr/bin/env bash
if [[ "${1:-}" == --version ]]; then echo 'codex-cli 0.120.0'; exit 0; fi
printf '%s\n' "$PWD" >> "$TUI_CWD_LOG"
printf '%s\n' "$@" > "$TUI_CWD_LOG.args"
CLI
  chmod +x "$BIN/$runtime"
  ROOT="$ROOT" HARNESS="$HARNESS" STATE="$STATE" BIN="$BIN" TEST_HOME="$TEST_HOME" PYTHON_BIN="$PYTHON_BIN" TUI_CWD_LOG="$log" RUN_CWD="$cwd" RUNTIME="$runtime" expect <<'EXPECT'
set timeout 20
log_user 0
spawn env -i PATH=$env(BIN):/usr/bin:/bin HOME=$env(TEST_HOME) ZDOTDIR=$env(TEST_HOME) TERM=dumb ROOT=$env(ROOT) HARNESS=$env(HARNESS) RUN_CWD=$env(RUN_CWD) HARNESS_PYTHON_BIN=$env(PYTHON_BIN) HARNESS_SESSION_STATE_HOME=$env(STATE) TUI_CWD_LOG=$env(TUI_CWD_LOG) HARNESS_CLAUDE_PLUGIN_MIRROR=0 zsh -f -c {
  cd "$RUN_CWD"
  "$ROOT/bin/harness-exec" "$HARNESS"
}
proc require {pattern} {
  expect {
    -re $pattern { return }
    timeout { puts stderr "FAIL: timed out waiting for $pattern: $expect_out(buffer)"; exit 1 }
    eof { puts stderr "FAIL: exited before $pattern: $expect_out(buffer)"; exit 1 }
  }
}
require "Workspace"
send -- "1\r"
require "Session"
send -- "1\r"
if {$env(RUNTIME) == "codex"} {
  require "Profile"
  send -- "2\r"
  require "Safety"
  send -- "1\r"
} else {
  require "Mode"
  send -- "2\r"
}
require "Start now"
send -- "1\r"
expect {
  eof {}
  timeout { puts stderr "FAIL: runtime did not exit"; exit 1 }
}
catch wait result
exit [lindex $result 3]
EXPECT
}

for runtime in claude codex; do
  clone_log="$TMP/$runtime-clone.log"
  run_choice "$HARNESS/nested" "$clone_log" "$runtime"
  clone_cwd="$(tail -1 "$clone_log")"
  state_real="$(cd "$STATE" && pwd -P)"
  case "$clone_cwd" in "$state_real"/worktrees/*/nested) ;; *) echo "FAIL: $runtime TUI clone cwd: $clone_cwd" >&2; exit 1;; esac
  project_log="$TMP/$runtime-project.log"
  run_choice "$HARNESS/projects/beta" "$project_log" "$runtime"
  project_cwd="$(cd "$HARNESS/projects/beta" && pwd -P)"
  [[ "$(tail -1 "$project_log")" == "$project_cwd" ]] || { echo "FAIL: $runtime projects cwd" >&2; exit 1; }
  if [[ "$runtime" == codex ]]; then
    "$PYTHON_BIN" - "$clone_log.args" "$clone_cwd" "$project_log.args" "$project_cwd" <<'PY'
import sys
from pathlib import Path
for args_file, cwd in zip(sys.argv[1::2], sys.argv[2::2]):
    args = Path(args_file).read_text().splitlines()
    assert args[args.index('--cd') + 1] == cwd, args
PY
  fi
done
echo 'PASS: real chooser/TUI launches Claude and Codex in clone and projects workspaces'
