#!/usr/bin/env zsh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
TMP="$(cd "$TMP" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT

PREFIX="$TMP/prefix"
HOME_DIR="$TMP/home"
PROFILE_BIN="$HOME_DIR/.local/bin"
HARNESS="$HOME_DIR/alpha harness"
PROJECT="$HARNESS/projects/app"
NATIVE_BIN="$TMP/native-bin"
ROUTE_LOG="$TMP/route.log"
NATIVE_LOG="$TMP/native.log"

mkdir -p "$HARNESS/config" "$PROJECT" "$PROFILE_BIN" "$NATIVE_BIN"
cat > "$HARNESS/config/launcher.env" <<'EOF'
HARNESS_NAME="alpha test"
HARNESS_PREFIX="alpha"
HARNESS_SESSION_ISOLATION_DEFAULT="${TEST_ISOLATION_DEFAULT:-0}"
EOF
mkdir -p "$HARNESS/.claude"
cat > "$HARNESS/.claude/settings.local.json" <<'EOF'
{"env":{"HARNESS_SHELL_TEST_SECRET":"loaded"}}
EOF

bash "$ROOT/test/lib/install-runtime-fixture.sh" "$ROOT" "$PREFIX"
HOME="$HOME_DIR" HARNESS_PROFILE_BIN_DIR="$PROFILE_BIN" \
  "$PREFIX/bin/harness-profile" register "$HARNESS" >/dev/null

cp "$PREFIX/share/harness-launcher/harness-exec" "$TMP/real-harness-exec"
cat > "$PREFIX/share/harness-launcher/harness-exec" <<'EOF'
#!/usr/bin/env bash
{
  printf 'HARNESS:%s\n' "$1"
  printf 'PWD:%s\n' "$(pwd -P)"
  shift
  printf 'ARGV:'
  printf ' <%s>' "$@"
  printf '\n'
} >> "$HARNESS_SHELL_ROUTE_LOG"
EOF
chmod 755 "$PREFIX/share/harness-launcher/harness-exec"

for runtime in codex claude; do
  cat > "$NATIVE_BIN/$runtime" <<'EOF'
#!/usr/bin/env bash
printf '%s:' "$(basename "$0")" >> "$HARNESS_SHELL_NATIVE_LOG"
printf ' <%s>' "$@" >> "$HARNESS_SHELL_NATIVE_LOG"
printf '\n' >> "$HARNESS_SHELL_NATIVE_LOG"
printf '%s-pwd:<%s>\n' "$(basename "$0")" "$(pwd -P)" >> "$HARNESS_SHELL_NATIVE_LOG"
if [[ -n "${HARNESS_SHELL_TEST_SECRET:-}" ]]; then
  printf '%s-env:<set>\n' "$(basename "$0")" >> "$HARNESS_SHELL_NATIVE_LOG"
fi
EOF
  chmod 755 "$NATIVE_BIN/$runtime"
done

export HOME="$HOME_DIR"
export PATH="$NATIVE_BIN:/usr/bin:/bin"
export HARNESS_SHELL_ROUTE_LOG="$ROUTE_LOG"
export HARNESS_SHELL_NATIVE_LOG="$NATIVE_LOG"
export HARNESS_CODEX_MCP_PROFILE=""
export _HARNESS_LAUNCHER_SHELL_AUTO_ENABLED=9
source "$PREFIX/share/harness-launcher/aliases.zsh"
harness_register "$HARNESS"

cat > "$PREFIX/share/harness-launcher/codex-home-prepare.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod 755 "$PREFIX/share/harness-launcher/codex-home-prepare.sh"

: > "$NATIVE_LOG"
codex --cd "$HARNESS" baseline-wrapper
baseline_codex_line="$(tail -1 "$NATIVE_LOG")"
: > "$NATIVE_LOG"

harness_shell_enable
[[ "${(t)_HARNESS_LAUNCHER_SHELL_AUTO_ENABLED}" != *-export* ]] || {
  echo 'FAIL: enable retained an inherited export attribute on its shell-local flag' >&2
  exit 1
}
env | grep -q '^_HARNESS_LAUNCHER_SHELL_AUTO_ENABLED=' && {
  echo 'FAIL: shell-local enable state leaked into the child environment' >&2
  exit 1
}
(( $+functions[claude] )) || { echo 'FAIL: enable did not define claude function' >&2; exit 1; }

(
  cd "$PROJECT"
  codex exec task
  codex -a never
  claude --resume session-123
  claude -p plan
  claude rich
)
# The stub captures raw launcher transport, including its Claude selector.
# The real harness-exec/native boundary is exercised separately below.
for expected in \
  'ARGV: <codex> <--passthrough> <exec> <task>' \
  'ARGV: <codex> <--passthrough> <-a> <never>' \
  'ARGV: <claude> <base> <--passthrough> <--resume> <session-123>' \
  'ARGV: <claude> <base> <--passthrough> <-p> <plan>' \
  'ARGV: <claude> <base> <--passthrough> <rich>'; do
  grep -Fqx "$expected" "$ROUTE_LOG" || {
    echo "FAIL: plain command did not route through harness-auto verbatim: $expected" >&2; exit 1
  }
done
[[ ! -s "$NATIVE_LOG" ]] || {
  echo 'FAIL: routed commands bypassed the harness' >&2; exit 1
}
echo 'PASS: enabled plain codex and claude route by current directory with raw launcher argv'

(
  cd "$TMP"
  codex --version
  codex -V
  codex --help
)
for expected in 'codex: <--version>' 'codex: <-V>' 'codex: <--help>'; do
  grep -Fqx "$expected" "$NATIVE_LOG" || {
    echo "FAIL: codex informational flag did not run natively: $expected" >&2; exit 1
  }
done
: > "$NATIVE_LOG"
echo 'PASS: codex --version, -V, and --help run native Codex anywhere'

(
  cd "$PROJECT"
  claude mcp list
  claude mcp add demo -- command --flag
  claude mcp remove demo
  claude auth status
  claude auth login
  claude auth logout
  claude plugin list
  claude plugin install demo
  claude doctor
)
for expected in \
  'ARGV: <claude-management> <mcp> <list>' \
  'ARGV: <claude-management> <mcp> <add> <demo> <--> <command> <--flag>' \
  'ARGV: <claude-management> <mcp> <remove> <demo>' \
  'ARGV: <claude-management> <auth> <status>' \
  'ARGV: <claude-management> <auth> <login>' \
  'ARGV: <claude-management> <auth> <logout>' \
  'ARGV: <claude-management> <plugin> <list>' \
  'ARGV: <claude-management> <plugin> <install> <demo>' \
  'ARGV: <claude-management> <doctor>'; do
  grep -Fqx "$expected" "$ROUTE_LOG" || {
    echo "FAIL: plain Claude management command did not preserve native argv: $expected" >&2
    exit 1
  }
done
echo 'PASS: plain Claude management commands use the profile-scoped native route'

(
  cd "$PROJECT"
  claude 'mcp list'
  claude -p doctor
  claude -- mcp list
)
for expected in \
  'ARGV: <claude> <base> <--passthrough> <mcp list>' \
  'ARGV: <claude> <base> <--passthrough> <-p> <doctor>' \
  'ARGV: <claude> <base> <--passthrough> <--> <mcp> <list>'; do
  grep -Fqx "$expected" "$ROUTE_LOG" || {
    echo "FAIL: Claude prompt syntax was misclassified as management: $expected" >&2
    exit 1
  }
done
echo 'PASS: Claude prompts and post-- tokens remain on the harness session route'

: > "$ROUTE_LOG"
if (
  cd "$TMP"
  codex exec task
) >"$TMP/outside.out" 2>"$TMP/outside.err"; then
  echo 'FAIL: enabled plain codex ran outside every registered boundary' >&2
  exit 1
fi
grep -Fq 'no registered harness contains the current directory' "$TMP/outside.err" || {
  echo 'FAIL: outside-boundary failure was not explained' >&2; exit 1
}
[[ ! -s "$ROUTE_LOG" && ! -s "$NATIVE_LOG" ]] || {
  echo 'FAIL: outside-boundary command launched a runtime' >&2; exit 1
}
if (
  cd "$TMP"
  claude mcp list
) >"$TMP/outside-claude.out" 2>"$TMP/outside-claude.err"; then
  echo 'FAIL: enabled plain Claude management command ran outside every registered boundary' >&2
  exit 1
fi
grep -Fq 'no registered harness contains the current directory' "$TMP/outside-claude.err" || {
  echo 'FAIL: outside-boundary Claude management failure was not explained' >&2; exit 1
}
echo 'PASS: enabled plain commands fail closed outside registered boundaries'

(
  cd "$PROJECT"
  command codex native-codex
  command claude native-claude
)
grep -Fqx 'codex: <native-codex>' "$NATIVE_LOG" || {
  echo 'FAIL: command codex did not preserve the native escape hatch' >&2; exit 1
}
grep -Fqx 'claude: <native-claude>' "$NATIVE_LOG" || {
  echo 'FAIL: command claude did not preserve the native escape hatch' >&2; exit 1
}
echo 'PASS: command codex and command claude bypass auto-routing explicitly'

: > "$NATIVE_LOG"
if (
  cd "$PROJECT"
  codex --cd "$TMP" --version
) >"$TMP/outside-cd.out" 2>"$TMP/outside-cd.err"; then
  echo 'FAIL: routed codex accepted --cd outside its selected harness' >&2
  exit 1
fi
grep -Fq 'outside selected harness boundary' "$TMP/outside-cd.err" || {
  echo 'FAIL: outside --cd rejection was not explained' >&2; exit 1
}
if (
  cd "$PROJECT"
  codex -C "$TMP" --version
) >"$TMP/outside-C.out" 2>"$TMP/outside-C.err"; then
  echo 'FAIL: routed codex accepted -C outside its selected harness' >&2
  exit 1
fi
if (
  cd "$PROJECT"
  codex "--cd=$TMP" --version
) >"$TMP/outside-cd-equals.out" 2>"$TMP/outside-cd-equals.err"; then
  echo 'FAIL: routed codex accepted --cd= outside its selected harness' >&2
  exit 1
fi
(
  cd "$TMP"
  codex --cd "$PROJECT" --version
) >"$TMP/outside-pwd.out" 2>"$TMP/outside-pwd.err" && {
  echo 'FAIL: external PWD selected a harness through --cd' >&2; exit 1
}
grep -Fq 'no registered harness contains the current directory' "$TMP/outside-pwd.err" || {
  echo 'FAIL: external PWD did not remain the profile-selection authority' >&2; exit 1
}
(
  cd "$PROJECT"
  codex --cd "$PROJECT" internal-cd
  codex "--cd=$PROJECT" internal-cd-equals
  codex -- --cd "$TMP" literal-prompt
)
grep -Fq "<--cd> <$PROJECT> <internal-cd>" "$ROUTE_LOG" || {
  echo 'FAIL: routed codex rejected an internal nested --cd' >&2; exit 1
}
grep -Fq "<--cd=$PROJECT> <internal-cd-equals>" "$ROUTE_LOG" || {
  echo 'FAIL: routed codex rejected an internal nested --cd=' >&2; exit 1
}
grep -Fq "<--> <--cd> <$TMP> <literal-prompt>" "$ROUTE_LOG" || {
  echo 'FAIL: routed codex treated post-- prompt text as a working-directory option' >&2; exit 1
}
(
  cd "$PROJECT"
  command codex --cd "$TMP" native-outside-cd
)
grep -Fqx "codex: <--cd> <$TMP> <native-outside-cd>" "$NATIVE_LOG" || {
  echo 'FAIL: native escape did not bypass routed --cd validation' >&2; exit 1
}
echo 'PASS: routed Codex validates explicit working directories against PWD ownership'

: > "$NATIVE_LOG"
harness_shell_disable
harness_shell_disable
(( ! $+functions[claude] )) || { echo 'FAIL: disable left the claude wrapper active' >&2; exit 1; }
(
  cd "$PROJECT"
  codex disabled-codex
  claude disabled-claude
)
grep -Fqx 'codex: <disabled-codex>' "$NATIVE_LOG" || {
  echo 'FAIL: disabled codex did not use the native runtime' >&2; exit 1
}
grep -Fqx 'claude: <disabled-claude>' "$NATIVE_LOG" || {
  echo 'FAIL: disabled claude did not use the native runtime' >&2; exit 1
}
codex --cd "$HARNESS" baseline-wrapper
[[ "$(tail -1 "$NATIVE_LOG")" == "$baseline_codex_line" ]] || {
  echo 'FAIL: disable did not restore the prior registered --cd wrapper behavior' >&2; exit 1
}
echo 'PASS: disable restores native plain-command behavior'

# The opt-in state must remain local to the interactive shell. Exercise the
# real harness-exec, which starts a child Zsh and sources aliases.zsh again;
# each command must reach exactly one native runtime rather than recurse.
cp "$TMP/real-harness-exec" "$PREFIX/share/harness-launcher/harness-exec"
: > "$NATIVE_LOG"
harness_shell_enable
(
  export TEST_ISOLATION_DEFAULT=1 HARNESS_SESSION_ISOLATION=1
  export HARNESS_SESSION_STATE_HOME="$TMP/isolation-state"
  cd "$PROJECT"
  claude mcp list
)
grep -Fqx 'claude: <mcp> <list>' "$NATIVE_LOG" || {
  echo 'FAIL: Claude management command entered default session isolation' >&2; exit 1
}
grep -Fqx "claude-pwd:<$PROJECT>" "$NATIVE_LOG" || {
  echo 'FAIL: Claude management command changed the original project PWD' >&2; exit 1
}
[[ ! -e "$TMP/isolation-state" ]] || {
  echo 'FAIL: Claude management command created isolation state' >&2; exit 1
}
: > "$NATIVE_LOG"
(
  cd "$PROJECT"
  codex real-codex
  claude real-claude
  claude mcp list
)
[[ "$(grep -c '^codex:' "$NATIVE_LOG")" -eq 1 ]] || {
  echo 'FAIL: real harness-exec did not reach Codex exactly once' >&2; exit 1
}
[[ "$(grep -c '^claude:.*<real-claude>' "$NATIVE_LOG")" -eq 1 ]] || {
  echo 'FAIL: real harness-exec did not reach the Claude session exactly once' >&2; exit 1
}
grep -Fq '<real-codex>' "$NATIVE_LOG" && grep -Fq '<real-claude>' "$NATIVE_LOG" || {
  echo 'FAIL: real harness-exec lost runtime arguments' >&2; exit 1
}
grep -Fqx 'claude: <mcp> <list>' "$NATIVE_LOG" || {
  echo 'FAIL: real harness-exec changed native Claude management arguments' >&2; exit 1
}
grep -Fqx 'claude-env:<set>' "$NATIVE_LOG" || {
  echo 'FAIL: native Claude management route did not load profile-local environment' >&2; exit 1
}
harness_shell_disable
echo 'PASS: real harness-exec preserves session and management routes without recursion'

source "$PREFIX/share/harness-launcher/aliases.zsh"
harness_shell_enable
harness_shell_disable
(( ! $+functions[claude] )) || {
  echo 'FAIL: re-sourcing aliases lost launcher ownership of the Claude wrapper' >&2
  exit 1
}
echo 'PASS: re-sourcing aliases preserves shell-local wrapper ownership'

claude() { return 37; }
if harness_shell_enable >"$TMP/collision.out" 2>"$TMP/collision.err"; then
  echo 'FAIL: enable replaced a pre-existing claude function' >&2
  exit 1
fi
grep -Fq 'claude function already exists' "$TMP/collision.err" || {
  echo 'FAIL: function collision did not produce a clear error' >&2; exit 1
}
claude || [[ $? -eq 37 ]] || {
  echo 'FAIL: function collision changed the existing claude function' >&2; exit 1
}
echo 'PASS: enable preserves a pre-existing claude function'

unfunction claude
claude() { _harness_launcher_auto_runtime claude "$@"; }
if harness_shell_enable >"$TMP/same-body.out" 2>"$TMP/same-body.err"; then
  echo 'FAIL: enable claimed a same-body user claude function' >&2
  exit 1
fi
grep -Fq 'claude function already exists' "$TMP/same-body.err" || {
  echo 'FAIL: same-body function collision was not explained' >&2; exit 1
}
(( $+functions[claude] )) || {
  echo 'FAIL: same-body user claude function was deleted' >&2; exit 1
}
unfunction claude
echo 'PASS: enable never infers Claude wrapper ownership from function text'

alias claude='return 41'
if harness_shell_enable >"$TMP/alias-collision.out" 2>"$TMP/alias-collision.err"; then
  echo 'FAIL: enable replaced a pre-existing claude alias' >&2
  exit 1
fi
grep -Fq 'claude alias already exists' "$TMP/alias-collision.err" || {
  echo 'FAIL: alias collision did not produce a clear error' >&2; exit 1
}
unalias claude
echo 'PASS: enable preserves a pre-existing claude alias'

if (
  codex() { return 38; }
  if harness_shell_enable; then
    exit 0
  fi
  codex || [[ $? -eq 38 ]] || exit 0
  exit 1
) >"$TMP/codex-collision.out" 2>"$TMP/codex-collision.err"; then
  echo 'FAIL: enable accepted a replaced codex function' >&2
  exit 1
fi
grep -Fq 'codex function is not launcher-owned' "$TMP/codex-collision.err" || {
  echo 'FAIL: replaced codex function did not produce a clear error' >&2; exit 1
}
echo 'PASS: enable rejects a replaced codex function atomically'

harness_shell_enable
harness_shell_enable
claude() { return 39; }
if harness_shell_disable >"$TMP/changed-disable.out" 2>"$TMP/changed-disable.err"; then
  echo 'FAIL: disable claimed ownership of a changed claude function' >&2
  exit 1
fi
claude || [[ $? -eq 39 ]] || {
  echo 'FAIL: disable deleted a user-replaced claude function' >&2; exit 1
}
unfunction claude
harness_shell_disable
echo 'PASS: repeated enable is idempotent and disable preserves replaced functions'

claude() { return 40; }
harness_shell_disable
claude || [[ $? -eq 40 ]] || {
  echo 'FAIL: no-op disable changed an unowned claude function' >&2; exit 1
}
unfunction claude
echo 'PASS: repeated disable is a no-op when the launcher owns no Claude wrapper'
