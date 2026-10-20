#!/usr/bin/env zsh
# test-launcher-direct.sh — verify direct Anthropic OAuth mode-mapping (no gateway)
# Modes: fast, base, plan, opus, rich
# Expected behavior:
#   fast  → --model haiku --effort low
#   base  → --model sonnet --effort high
#   plan  → --model opusplan --effort high
#   opus  → --model opus[1m] --effort high (rich model, one effort step down)
#   rich  → --model opus[1m] --effort xhigh + forced thinking via --settings
# xhigh/max are rejected by the API when thinking is disabled, so the launcher
# forces alwaysThinkingEnabled for those efforts instead of relying on the
# user's settings.json.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCHER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Cleanup on exit
cleanup() {
  [[ -n "$TEST_TEMP" && -d "$TEST_TEMP" ]] && rm -rf "$TEST_TEMP"
}
trap cleanup EXIT

TEST_TEMP="$(mktemp -d)"
TEST_HARNESS="$TEST_TEMP/fake-harness"

# Setup fake harness
mkdir -p "$TEST_HARNESS/config"
mkdir -p "$TEST_HARNESS/.claude"
cat > "$TEST_HARNESS/.claude/settings.local.json" <<'EOF'
{"env":{"TEST_CLAUDE_MCP_TOKEN":"fixture-token"}}
EOF
cat > "$TEST_HARNESS/config/launcher.env" <<'EOF'
HARNESS_NAME="test harness"
HARNESS_PREFIX="test"
EOF

# Setup stub claude that captures args to a file
CLAUDE_STUB="$TEST_TEMP/claude"
cat > "$CLAUDE_STUB" <<'EOF'
#!/bin/bash
# Stub claude: capture all args and env vars to a file
echo "ARGS:$@" >> "$TEST_STUB_FILE"
echo "EFFORT:${CLAUDE_CODE_EFFORT_LEVEL:-}" >> "$TEST_STUB_FILE"
echo "CCT:${CLAUDE_CODE_TMPDIR-<unset>}" >> "$TEST_STUB_FILE"
[[ "${TEST_CLAUDE_MCP_TOKEN:-}" == fixture-token ]] && echo "MCP_AUTH:loaded" >> "$TEST_STUB_FILE"
exit 0
EOF
chmod +x "$CLAUDE_STUB"


# Helper: extract --model value from args line
extract_model() {
  local args="$1"
  echo "$args" | sed -n 's/.*--model \([^ ]*\).*/\1/p'
}

# Helper: extract --effort value from args line
extract_effort() {
  local args="$1"
  echo "$args" | sed -n 's/.*--effort \([^ ]*\).*/\1/p'
}

# Helper: check if --exclude-dynamic-system-prompt-sections flag is present
extract_has_flag() {
  local args="$1"
  if echo "$args" | grep -q "\--exclude-dynamic-system-prompt-sections"; then
    echo "true"
  else
    echo "false"
  fi
}

# Helper: check whether the launch forces thinking on via --settings.
# xhigh/max are rejected by the API when thinking is disabled, so the launcher
# must not depend on the user's settings.json for those efforts.
extract_forces_thinking() {
  local args="$1"
  if echo "$args" | grep -q -- '--settings {"alwaysThinkingEnabled":true,'; then
    echo "true"
  else
    echo "false"
  fi
}

# Helper: run a mode and capture output
run_mode() {
  local mode="$1" expected_model="$2" expected_effort="$3" expected_thinking="$4"
  local stub_file="$TEST_TEMP/output-$mode.txt"

  # Run in subshell with stub claude in PATH
  (
    export TEST_STUB_FILE="$stub_file"
    export PATH="$TEST_TEMP:$PATH"
    source "$LAUNCHER_DIR/bin/aliases.zsh"
    _harness_launcher_run "$TEST_HARNESS" "$mode"
  ) 2>/dev/null || true

  if [[ ! -f "$stub_file" ]]; then
    echo "FAIL: $mode — no stub output file"
    return 1
  fi

  local args_line=$(grep "^ARGS:" "$stub_file" | head -1 | cut -d: -f2-)

  # Extract model and effort from args
  local actual_model=$(extract_model "$args_line")
  local actual_effort=$(extract_effort "$args_line")
  local has_flag=$(extract_has_flag "$args_line")
  local forces_thinking=$(extract_forces_thinking "$args_line")

  if [[ "$actual_model" != "$expected_model" ]]; then
    echo "FAIL: $mode — expected --model $expected_model, got '$actual_model'"
    echo "  Full args: $args_line"
    return 1
  fi

  if [[ "$actual_effort" != "$expected_effort" ]]; then
    echo "FAIL: $mode — expected --effort $expected_effort, got '$actual_effort'"
    echo "  Full args: $args_line"
    return 1
  fi

  if [[ "$has_flag" != "true" ]]; then
    echo "FAIL: $mode — expected --exclude-dynamic-system-prompt-sections flag, not found"
    echo "  Full args: $args_line"
    return 1
  fi

  if [[ "$forces_thinking" != "$expected_thinking" ]]; then
    echo "FAIL: $mode — expected forced-thinking $expected_thinking, got '$forces_thinking'"
    echo "  Full args: $args_line"
    return 1
  fi

  echo "PASS: $mode → --model $expected_model --effort $expected_effort + flag (thinking forced: $expected_thinking)"
  return 0
}

# Run mode tests. rich uses xhigh, which the API rejects when thinking is
# disabled, so only rich must carry the forced-thinking --settings override.
run_mode "fable" "fable" "high" false || exit 1
run_mode "fast" "haiku" "low"      false || exit 1
run_mode "base" "sonnet" "high"    false || exit 1
run_mode "plan" "opusplan" "high"  false || exit 1
run_mode "opus" "opus[1m]" "high"  false || exit 1
run_mode "rich" "opus[1m]" "xhigh" true  || exit 1
grep -Fqx 'MCP_AUTH:loaded' "$TEST_TEMP/output-base.txt" || {
  echo 'FAIL: direct Claude did not inherit harness-local MCP credentials' >&2
  exit 1
}
(
  unset TEST_CLAUDE_MCP_TOKEN
  export TEST_STUB_FILE="$TEST_TEMP/output-parent-env.txt"
  export PATH="$TEST_TEMP:$PATH"
  source "$LAUNCHER_DIR/bin/aliases.zsh"
  _harness_launcher_run "$TEST_HARNESS" base >/dev/null 2>&1
  [[ -z "${TEST_CLAUDE_MCP_TOKEN:-}" ]]
) || {
  echo 'FAIL: direct Claude left the MCP credential in the parent shell' >&2
  exit 1
}
(
  unset TEST_CLAUDE_MCP_TOKEN
  export TEST_STUB_FILE="$TEST_TEMP/output-management-env.txt"
  export PATH="$TEST_TEMP:$PATH"
  source "$LAUNCHER_DIR/bin/aliases.zsh"
  _harness_launcher_run "$TEST_HARNESS" claude-management mcp list >/dev/null 2>&1
  [[ -z "${TEST_CLAUDE_MCP_TOKEN:-}" ]]
) || {
  echo 'FAIL: Claude management left the MCP credential in the parent shell' >&2
  exit 1
}
grep -Fqx 'MCP_AUTH:loaded' "$TEST_TEMP/output-management-env.txt" || {
  echo 'FAIL: Claude management did not inherit harness-local MCP credentials' >&2
  exit 1
}
(
  source "$LAUNCHER_DIR/bin/harness-common.sh"
  set -x
  harness_export_local_env "$TEST_HARNESS"
  set +x
) 2>"$TEST_TEMP/xtrace-auth.err"
if grep -Fq 'fixture-token' "$TEST_TEMP/xtrace-auth.err"; then
  echo 'FAIL: MCP credential was printed by shell xtrace' >&2
  exit 1
fi

# Explicit effort tokens must get the same treatment as mode-derived efforts.
run_effort_token() {  # <token> <expected_thinking>
  local token="$1" expected_thinking="$2"
  local stub_file="$TEST_TEMP/output-effort-$token.txt"
  (
    export TEST_STUB_FILE="$stub_file"
    export PATH="$TEST_TEMP:$PATH"
    source "$LAUNCHER_DIR/bin/aliases.zsh"
    _harness_launcher_run "$TEST_HARNESS" "$token"
  ) 2>/dev/null || true
  local args_line=$(grep "^ARGS:" "$stub_file" | head -1 | cut -d: -f2-)
  local forces_thinking=$(extract_forces_thinking "$args_line")
  if [[ "$forces_thinking" != "$expected_thinking" ]]; then
    echo "FAIL: effort '$token' — expected forced-thinking $expected_thinking, got '$forces_thinking'"
    echo "  Full args: $args_line"
    return 1
  fi
  echo "PASS: effort '$token' → forced-thinking $expected_thinking"
  return 0
}

run_effort_token low    false || exit 1
run_effort_token medium false || exit 1
run_effort_token high   false || exit 1
run_effort_token xhigh  true  || exit 1
run_effort_token max    true  || exit 1

# Local MCP overlay: Claude Code should receive local/private MCP config files
# in addition to its normal project .mcp.json auto-discovery. Duplicate server
# names across committed/local config must fail instead of silently overriding.
cat > "$TEST_HARNESS/.mcp.json" <<'EOF'
{ "mcpServers": { "committed": { "type": "http", "url": "https://committed.test/mcp" } } }
EOF
cat > "$TEST_HARNESS/.mcp.local.json" <<'EOF'
{ "mcpServers": { "local_private": { "command": "local-private", "args": ["mcp"] } } }
EOF
cat > "$TEST_HARNESS/mcp.local.json" <<'EOF'
{ "mcpServers": { "legacy_local": { "type": "http", "url": "https://legacy-local.test/mcp" } } }
EOF
stub_file="$TEST_TEMP/output-local-mcp.txt"
(
  export TEST_STUB_FILE="$stub_file"
  export PATH="$TEST_TEMP:$PATH"
  source "$LAUNCHER_DIR/bin/aliases.zsh"
  _harness_launcher_run "$TEST_HARNESS" base
) 2>/dev/null || true
args_line=$(grep "^ARGS:" "$stub_file" | head -1 | cut -d: -f2-)
case "$args_line" in
  *"--mcp-config $TEST_HARNESS/.harness/claude/mcp-full.json"*)
    jq -e '.mcpServers | has("committed") and has("local_private") and has("legacy_local")' \
      "$TEST_HARNESS/.harness/claude/mcp-full.json" >/dev/null || exit 1
    echo "PASS: Claude launcher renders merged MCP definitions" ;;
  *)
    echo "FAIL: Claude launcher did not pass merged MCP configuration"
    echo "  Full args: $args_line"
    exit 1 ;;
esac

cat > "$TEST_HARNESS/.mcp.local.json" <<'EOF'
{ "mcpServers": { "committed": { "type": "http", "url": "https://local.test/mcp" } } }
EOF
dup_stub_file="$TEST_TEMP/output-duplicate-mcp.txt"
dup_err_file="$TEST_TEMP/output-duplicate-mcp.err"
if (
  export TEST_STUB_FILE="$dup_stub_file"
  export PATH="$TEST_TEMP:$PATH"
  source "$LAUNCHER_DIR/bin/aliases.zsh"
  _harness_launcher_run "$TEST_HARNESS" base
) 2> "$dup_err_file"; then
  echo "FAIL: duplicate MCP server name across committed/local configs should fail"
  exit 1
fi
grep -q "duplicate MCP server 'committed'" "$dup_err_file" || {
  echo "FAIL: duplicate MCP error message missing"
  cat "$dup_err_file"
  exit 1
}
if [[ -s "$dup_stub_file" ]]; then
  echo "FAIL: claude should not launch when MCP config validation fails"
  cat "$dup_stub_file"
  exit 1
fi
echo "PASS: Claude launcher rejects duplicate MCP server names before launch"

# Light MCP surface shortcut: `<prefix> base light` generates a filtered config
# and launches with --strict-mcp-config; SSH-backed servers are excluded.
cat > "$TEST_HARNESS/.mcp.local.json" <<'EOF'
{ "mcpServers": {
  "ssh_rag": { "command": "bash", "args": ["core/bin/start-ssh-mcp.sh", "rag"] },
  "tunnel_rag": { "type": "http", "url": "http://127.0.0.1:38206/mcp" }
} }
EOF
light_stub_file="$TEST_TEMP/output-light.txt"
mkdir -p "$TEST_TEMP/home"
(
  export TEST_STUB_FILE="$light_stub_file"
  export PATH="$TEST_TEMP:$PATH"
  export HOME="$TEST_TEMP/home"
  export HARNESS_MCP_SURFACE_POLICY=single-full
  source "$LAUNCHER_DIR/bin/aliases.zsh"
  _harness_launcher_run "$TEST_HARNESS" base light
) 2>/dev/null || true
args_line=$(grep "^ARGS:" "$light_stub_file" | head -1 | cut -d: -f2-)
LIGHT_FILE="$TEST_HARNESS/.harness/claude/mcp-light.json"
case "$args_line" in
  *"--strict-mcp-config --mcp-config $LIGHT_FILE"*) ;;
  *)
    echo "FAIL: light shortcut should pass --strict-mcp-config + generated file"
    echo "  Full args: $args_line"
    exit 1 ;;
esac
case "$args_line" in
  *".mcp.local.json"*)
    echo "FAIL: light shortcut must not also append raw local overlay files"
    echo "  Full args: $args_line"
    exit 1 ;;
esac
grep -q "ssh_rag" "$LIGHT_FILE" && { echo "FAIL: light surface must exclude SSH stdio wrappers"; exit 1; }
grep -q "tunnel_rag" "$LIGHT_FILE" && { echo "FAIL: light surface must exclude 382xx tunnel servers"; exit 1; }
grep -q "committed" "$LIGHT_FILE" || { echo "FAIL: light surface must keep committed non-SSH servers"; exit 1; }
grep -q "legacy_local" "$LIGHT_FILE" || { echo "FAIL: light surface must keep local non-SSH servers"; exit 1; }
grep -Fqx 'MCP_AUTH:loaded' "$light_stub_file" || { echo 'FAIL: light Claude did not inherit harness-local MCP credentials'; exit 1; }
if grep -Fq 'fixture-token' "$light_stub_file" "$LIGHT_FILE"; then
  echo 'FAIL: light Claude exposed the MCP credential in argv or generated config' >&2
  exit 1
fi
echo "PASS: light shortcut filters SSH servers with --strict-mcp-config"

# Light shortcut must fail closed on duplicate server names (no launch).
cat > "$TEST_HARNESS/.mcp.local.json" <<'EOF'
{ "mcpServers": { "committed": { "type": "http", "url": "https://local.test/mcp" } } }
EOF
light_dup_stub="$TEST_TEMP/output-light-dup.txt"
light_dup_err="$TEST_TEMP/output-light-dup.err"
if (
  export TEST_STUB_FILE="$light_dup_stub"
  export PATH="$TEST_TEMP:$PATH"
  export HOME="$TEST_TEMP/home"
  source "$LAUNCHER_DIR/bin/aliases.zsh"
  _harness_launcher_run "$TEST_HARNESS" base light
) 2> "$light_dup_err"; then
  echo "FAIL: light shortcut must fail on duplicate server names"
  exit 1
fi
grep -q "duplicate MCP server 'committed'" "$light_dup_err" || {
  echo "FAIL: light duplicate error message missing"
  cat "$light_dup_err"
  exit 1
}
if [[ -s "$light_dup_stub" ]]; then
  echo "FAIL: claude must not launch when light generation fails"
  exit 1
fi
echo "PASS: light shortcut fails closed on duplicate server names"

# A project may opt into a single full MCP surface. Compat accepts the retired
# Claude selector with a warning; strict rejects it before the Claude command.
printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' \
  'HARNESS_MCP_SURFACE_POLICY="single-full-compat"' > "$TEST_HARNESS/config/launcher.env"
cat > "$TEST_HARNESS/.mcp.local.json" <<'EOF'
{ "mcpServers": { "compat_local": { "command": "compat-local" } } }
EOF
compat_stub="$TEST_TEMP/output-light-compat.txt"
compat_err="$TEST_TEMP/output-light-compat.err"
(
  export TEST_STUB_FILE="$compat_stub"
  export PATH="$TEST_TEMP:$PATH"
  source "$LAUNCHER_DIR/bin/aliases.zsh"
  _harness_launcher_run "$TEST_HARNESS" base light
) 2>"$compat_err" || exit 1
compat_args="$(grep '^ARGS:' "$compat_stub" | head -1 | cut -d: -f2-)"
grep -Fq 'light MCP surface is deprecated for this project; using full' "$compat_err" || {
  echo "FAIL: compat light did not warn"; exit 1
}
case "$compat_args" in
  *--strict-mcp-config*) echo "FAIL: compat light retained strict MCP config"; exit 1 ;;
  *"--mcp-config $TEST_HARNESS/.harness/claude/mcp-full.json"*) ;;
  *) echo "FAIL: compat light did not use the full MCP path: $compat_args"; exit 1 ;;
esac
echo "PASS: compat light warns and uses the full Claude MCP path"

printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' \
  'HARNESS_MCP_SURFACE_POLICY="single-full"' > "$TEST_HARNESS/config/launcher.env"
strict_stub="$TEST_TEMP/output-light-strict.txt"
strict_err="$TEST_TEMP/output-light-strict.err"
set +e
(
  export TEST_STUB_FILE="$strict_stub"
  export PATH="$TEST_TEMP:$PATH"
  source "$LAUNCHER_DIR/bin/aliases.zsh"
  _harness_launcher_run "$TEST_HARNESS" base light
) 2>"$strict_err"
strict_rc=$?
set -e
[[ "$strict_rc" -ne 0 ]] || { echo "FAIL: strict light must fail"; exit 1; }
[[ "$strict_rc" -eq 2 ]] || { echo "FAIL: strict light must exit 2, got $strict_rc"; exit 1; }
grep -Fq 'light MCP surface is retired for this project' "$strict_err" || {
  echo "FAIL: strict light error missing"; exit 1
}
[[ ! -s "$strict_stub" ]] || { echo "FAIL: strict light launched Claude"; exit 1; }
echo "PASS: strict light fails before Claude launches"

# Both opt-in modes retain Kiro gateway's existing Claude light launch. This
# catches changing the shared light parser without guarding provider_name=kiro.
for kiro_policy in single-full-compat single-full; do
  printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' \
    "HARNESS_MCP_SURFACE_POLICY=\"$kiro_policy\"" > "$TEST_HARNESS/config/launcher.env"
  mkdir -p "$TEST_HARNESS/config/.local"
  printf '%s\n' 'KIRO_GATEWAY_URL="http://gateway.test"' > "$TEST_HARNESS/config/.local/kiro-gateway.env"
  kiro_stub_file="$TEST_TEMP/output-kiro-$kiro_policy.txt"
  kiro_err_file="$TEST_TEMP/output-kiro-$kiro_policy.err"
  : > "$kiro_stub_file"
  (
    export TEST_STUB_FILE="$kiro_stub_file"
    export PATH="$TEST_TEMP:$PATH"
    source "$LAUNCHER_DIR/bin/aliases.zsh"
    _harness_launcher_probe_provider_health() { return 0; }
    _harness_launcher_run "$TEST_HARNESS" kiro base light
  ) 2>"$kiro_err_file" || { echo "FAIL: Kiro $kiro_policy light must launch"; exit 1; }
  grep -q '^ARGS:.*--strict-mcp-config ' "$kiro_stub_file" || {
    echo "FAIL: Kiro $kiro_policy light lost the strict Claude light launch"; cat "$kiro_stub_file"; exit 1
  }
  [[ ! -s "$kiro_err_file" ]] || {
    echo "FAIL: Kiro $kiro_policy light emitted selector retirement output"; cat "$kiro_err_file"; exit 1
  }
done
echo "PASS: Kiro gateway light remains a strict Claude light launch for compat and strict policies"

# The no-argument launchpad path crosses a second process boundary before
# native Codex starts. Preserve the short harness prefix for that process too.
tui_stub_dir="$TEST_TEMP/tui-launcher"
tui_stub_log="$TEST_TEMP/tui-launcher-env.txt"
mkdir -p "$tui_stub_dir"
cat > "$tui_stub_dir/launcher.sh" <<'EOF'
#!/usr/bin/env bash
printf 'HARNESS_PREFIX:%s\n' "${HARNESS_PREFIX:-<UNSET>}" > "$TUI_STUB_LOG"
EOF
chmod +x "$tui_stub_dir/launcher.sh"
(
  export TUI_STUB_LOG="$tui_stub_log"
  source "$LAUNCHER_DIR/bin/aliases.zsh"
  _HARNESS_LAUNCHER_BIN="$tui_stub_dir"
  _harness_launcher_run "$TEST_HARNESS"
)
grep -q '^HARNESS_PREFIX:test$' "$tui_stub_log" || {
  echo "FAIL: no-argument launchpad did not receive HARNESS_PREFIX=test"
  cat "$tui_stub_log"
  exit 1
}
echo "PASS: no-argument launchpad receives the short harness prefix"

# F: durable scratchpad. Interactive Claude gets CLAUDE_CODE_TMPDIR=<state>/cct
# in its own environment only; a preset value is kept; an unsafe or too long
# base leaves the variable unset. tmutil is a stub so no real exclusion is made.
CCT_ROOT="$(mktemp -d /tmp/hlcct.XXXXXX)"
trap 'chmod -R u+rwx "$CCT_ROOT" 2>/dev/null; rm -rf "$CCT_ROOT"; cleanup' EXIT
mkdir -p "$CCT_ROOT/bin" "$CCT_ROOT/home"
cat > "$CCT_ROOT/bin/tmutil" <<'EOF'
#!/bin/sh
echo "$*" >> "$CCT_TMUTIL_LOG"
EOF
chmod +x "$CCT_ROOT/bin/tmutil"
run_cct() {  # <xdg-state-home> [ENV=value ...]; prints the stub CCT line
  local xdg="$1"; shift
  local out="$CCT_ROOT/out.txt"
  rm -f "$out"
  (
    export TEST_STUB_FILE="$out" CCT_TMUTIL_LOG="$CCT_ROOT/tmutil.log" HOME="$CCT_ROOT/home"
    export PATH="$CCT_ROOT/bin:$TEST_TEMP:$PATH" XDG_STATE_HOME="$xdg"
    unset CLAUDE_CODE_TMPDIR
    local pair preset=0; for pair in "$@"; do export "$pair"; [[ "$pair" == CLAUDE_CODE_TMPDIR=* ]] && preset=1; done
    source "$LAUNCHER_DIR/bin/aliases.zsh"
    _harness_launcher_run "$TEST_HARNESS" base
    # never exported to the user's interactive shell
    [[ "$preset" == 1 || -z "${CLAUDE_CODE_TMPDIR:-}" ]] || exit 9
  ) >/dev/null 2>&1 || { echo "FAIL: F launch failed or leaked the variable" >&2; exit 1; }
  grep '^CCT:' "$out" | head -1
}
XS="$CCT_ROOT/s"; mkdir -p "$XS"
[[ "$(run_cct "$XS")" == "CCT:$XS/cct" ]] || { echo "FAIL: F unset -> $XS/cct"; exit 1; }
[[ "$(stat -f %Lp "$XS/cct")" == 700 && -O "$XS/cct" ]] || { echo 'FAIL: F base must be 0700 and owned'; exit 1; }
[[ -e "$XS/cct/.metadata_never_index" ]] || { echo 'FAIL: F missing .metadata_never_index'; exit 1; }
[[ "$(cat "$CCT_ROOT/tmutil.log")" == "addexclusion $XS/cct" ]] || { echo 'FAIL: F tmutil addexclusion once'; cat "$CCT_ROOT/tmutil.log"; exit 1; }
run_cct "$XS" >/dev/null
[[ "$(wc -l < "$CCT_ROOT/tmutil.log" | tr -d ' ')" == 1 ]] || { echo 'FAIL: F tmutil must run only once'; exit 1; }
echo "PASS: F unset -> durable base, 0700, never-index, tmutil once, not exported to the shell"

XP="$CCT_ROOT/p"; mkdir -p "$XP"
[[ "$(run_cct "$XP" CLAUDE_CODE_TMPDIR=/preset/dir)" == "CCT:/preset/dir" ]] || { echo 'FAIL: F preset value must be preserved'; exit 1; }
[[ ! -e "$XP/cct" ]] || { echo 'FAIL: F created the base despite a preset value'; exit 1; }
echo "PASS: F preset CLAUDE_CODE_TMPDIR preserved"

XL="$CCT_ROOT/l"; mkdir -p "$XL" "$CCT_ROOT/elsewhere"; ln -s "$CCT_ROOT/elsewhere" "$XL/cct"
[[ "$(run_cct "$XL")" == "CCT:<unset>" ]] || { echo 'FAIL: F symlinked base must leave the variable unset'; exit 1; }
XF="$CCT_ROOT/f"; mkdir -p "$XF"; : > "$XF/cct"
[[ "$(run_cct "$XF")" == "CCT:<unset>" ]] || { echo 'FAIL: F non-directory base must leave the variable unset'; exit 1; }
XW="$CCT_ROOT/w"; mkdir -p "$XW/cct"; chmod 755 "$XW/cct"
[[ "$(run_cct "$XW")" == "CCT:$XW/cct" && "$(stat -f %Lp "$XW/cct")" == 700 ]] || { echo 'FAIL: F existing wide base must be tightened to 0700'; exit 1; }
echo "PASS: F symlink / non-directory base unset; wide base tightened"

XLONG="$CCT_ROOT/$(printf 'x%.0s' {1..40})"; mkdir -p "$XLONG"
[[ "$(run_cct "$XLONG")" == "CCT:<unset>" && ! -e "$XLONG/cct" ]] || { echo 'FAIL: F base longer than 40 bytes must stay unset'; exit 1; }
echo "PASS: F long base unset"

XR="$CCT_ROOT/r"; mkdir -p "$XR"; chmod 500 "$XR"
[[ "$(run_cct "$XR")" == "CCT:<unset>" ]] || { chmod 700 "$XR"; echo 'FAIL: F unwritable state home must not export'; exit 1; }
chmod 700 "$XR"
echo "PASS: F unwritable state home unset, launch still succeeds"

# Headless never gets the durable base; the picker path (launcher.sh) calls the helper.
( source "$LAUNCHER_DIR/bin/harness-common.sh"; HARNESS_HEADLESS=1 XDG_STATE_HOME="$XS" harness_claude_export_durable_tmpdir; [[ -z "${CLAUDE_CODE_TMPDIR:-}" ]] ) \
  || { echo 'FAIL: F headless must not set the durable base'; exit 1; }
grep -q 'harness_claude_export_durable_tmpdir' "$LAUNCHER_DIR/bin/launcher.sh" || { echo 'FAIL: F launcher.sh must export the durable base'; exit 1; }
echo "PASS: F headless skipped; picker path wired"

echo "✓ All direct OAuth tests passed"
