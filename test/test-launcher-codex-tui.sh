#!/usr/bin/env bash
# test-launcher-codex-tui.sh — verify launcher.sh runtime-first TUI flow for Codex CLI.
#
# When both `claude` and `codex` are in PATH, the launcher must:
#   1. Prompt: Select runtime (Claude Code / Codex CLI)
#   2. If Codex selected → Session menu, Mode menu, Safety menu
#   3. exec codex with: subcmd (if any), --cd, -p <profile>, safety flags
#   4. CODEX_HOME=$HARNESS_DIR/.harness/codex
#   5. Always run codex-home-prepare.sh first
#
# When `codex` is missing, the runtime menu must auto-skip → existing Claude flow.

set -euo pipefail

# Controller session identity must not bind these independent fake harnesses.
unset HARNESS_SOURCE_ROOT HARNESS_SESSION_ROOT HARNESS_SESSION_ID HARNESS_RUN_DIR

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCHER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cleanup() { [[ -n "${TEST_TEMP:-}" && -d "$TEST_TEMP" ]] && rm -rf "$TEST_TEMP"; }
trap cleanup EXIT

TEST_TEMP="$(mktemp -d)"
TEST_HARNESS="$TEST_TEMP/fake-harness"
TEST_BIN="$TEST_TEMP/bin"
TEST_LAUNCHER_BIN="$TEST_TEMP/launcher-bin"
TEST_BROKEN_BIN="$TEST_TEMP/broken-bin"
mkdir -p "$TEST_HARNESS/config" "$TEST_BIN" "$TEST_LAUNCHER_BIN" "$TEST_BROKEN_BIN"
source "$LAUNCHER_DIR/bin/harness-common.sh"
TEST_PYTHON_FIXTURE="$(harness_python3_resolve)" || exit $?
# Direct launcher fixtures must not inherit the controller harness cwd.
unset HARNESS_RUN_DIR
ln -s "$TEST_PYTHON_FIXTURE" "$TEST_BIN/harness-python"
cat > "$TEST_BROKEN_BIN/python3" <<'EOF'
#!/usr/bin/env bash
exit 127
EOF
chmod +x "$TEST_BROKEN_BIN/python3"
TEST_WORKTREE="$TEST_HARNESS/.worktrees/sample"
mkdir -p "$TEST_WORKTREE"
TEST_WORKTREE_REAL="$(cd -P "$TEST_WORKTREE" && pwd -P)"
cp "$LAUNCHER_DIR/bin/launcher.sh" "$TEST_LAUNCHER_BIN/launcher.sh"
cp "$LAUNCHER_DIR/bin/harness-common.sh" "$TEST_LAUNCHER_BIN/harness-common.sh"
cp "$LAUNCHER_DIR/bin/slack-approval-policy.py" "$TEST_LAUNCHER_BIN/slack-approval-policy.py"
cp "$LAUNCHER_DIR/bin/mcp_paths.py" "$TEST_LAUNCHER_BIN/mcp_paths.py"
cat > "$TEST_LAUNCHER_BIN/codex-home-prepare.sh" <<'EOF'
#!/usr/bin/env bash
set -e
mkdir -p "$1/.harness/codex"
printf '# prepared by test stub\n' > "$1/.harness/codex/AGENTS.md"
echo "PREPARE_MCP_PROFILE:${HARNESS_CODEX_MCP_PROFILE:-<UNSET>}" >> "$TEST_STUB_FILE"
echo "PREPARE_GLOBAL_MCP_ALLOWLIST:${HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST:-<UNSET>}" >> "$TEST_STUB_FILE"
echo "PREPARE_APPS_ALLOWLIST:${HARNESS_CODEX_APPS_ALLOWLIST:-<UNSET>}" >> "$TEST_STUB_FILE"
echo "PREPARE_CONTEXT:${HARNESS_CODEX_CONTEXT:-<UNSET>}" >> "$TEST_STUB_FILE"
if [[ -f "$TEST_STUB_FILE.fail-prepare-once" ]]; then
  rm "$TEST_STUB_FILE.fail-prepare-once"
  exit 1
fi
EOF
chmod +x "$TEST_LAUNCHER_BIN/launcher.sh" "$TEST_LAUNCHER_BIN/codex-home-prepare.sh"

cat > "$TEST_HARNESS/config/launcher.env" <<'EOF'
HARNESS_NAME="test harness"
HARNESS_PREFIX="test"
HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST=" tui-one, tui-two,tui-one "
EOF
echo "# fake rules" > "$TEST_HARNESS/CLAUDE.md"

cat > "$TEST_BIN/claude" <<'EOF'
#!/usr/bin/env bash
{
  echo "EXEC:claude"
  echo "ARGS:$*"
  echo "CODEX_CONTEXT:${HARNESS_CODEX_CONTEXT:-<UNSET>}"
} >> "$TEST_STUB_FILE"
exit 0
EOF
chmod +x "$TEST_BIN/claude"

cat > "$TEST_BIN/codex" <<'EOF'
#!/usr/bin/env bash
{
  echo "EXEC:codex"
  echo "ARGS:$*"
  echo "CODEX_HOME:${CODEX_HOME:-}"
  echo "MCP_PROFILE:${HARNESS_CODEX_MCP_PROFILE:-<UNSET>}"
  echo "HARNESS_PREFIX:${HARNESS_PREFIX:-<UNSET>}"
  for v in APPROVAL SANDBOX BYPASS PROFILE SOURCE_ROOT ISOLATED PERMISSION; do
    n="HARNESS_LAUNCH_$v"; echo "LAUNCH_$v:${!n-<UNSET>}"
  done
} >> "$TEST_STUB_FILE"
exit 0
EOF
chmod +x "$TEST_BIN/codex"

HAPPY_BIN="$TEST_TEMP/bin-happy"
mkdir -p "$HAPPY_BIN"
cat > "$HAPPY_BIN/happy" <<'EOF'
#!/usr/bin/env bash
{
  echo "EXEC:happy"
  echo "ARGS:$*"
  echo "CODEX_HOME:${CODEX_HOME:-}"
  echo "HARNESS_PREFIX:${HARNESS_PREFIX:-<UNSET>}"
  for v in APPROVAL SANDBOX BYPASS PROFILE SOURCE_ROOT ISOLATED PERMISSION; do
    n="HARNESS_LAUNCH_$v"; echo "LAUNCH_$v:${!n-<UNSET>}"
  done
} >> "$TEST_STUB_FILE"
exit 0
EOF
chmod +x "$HAPPY_BIN/happy"

# Profile configs drive the drift-proof TUI labels.
mkdir -p "$TEST_HARNESS/.harness/codex"
printf 'model = "gpt-6-luna"\nmodel_reasoning_effort = "low"\n' > "$TEST_HARNESS/.harness/codex/fast.config.toml"
printf 'model = "gpt-5.6-terra"\nmodel_reasoning_effort = "medium"\n' > "$TEST_HARNESS/.harness/codex/base.config.toml"
printf 'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "high"\n' > "$TEST_HARNESS/.harness/codex/sol.config.toml"
printf 'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "high"\n' > "$TEST_HARNESS/.harness/codex/plan.config.toml"
printf 'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "xhigh"\n' > "$TEST_HARNESS/.harness/codex/rich.config.toml"

run_tui() {
  local input="$1" stub_file="$2" extra_path="${3:-}"
  rm -f "$TEST_HARNESS/.harness/launcher-last" "$TEST_HARNESS/.harness/launcher-history"
  local path_value="$TEST_BIN:/usr/bin:/bin"
  [[ -n "$extra_path" ]] && path_value="$extra_path:$path_value"
  [[ -n "${TUI_EXTRA_PATH:-}" ]] && path_value="$TUI_EXTRA_PATH:$path_value"
  env -u HARNESS_DIR -u HARNESS_RUN_DIR -u HARNESS_PREFIX \
    -u HARNESS_CODEX_MCP_PROFILE -u HARNESS_CODEX_CONTEXT -u HARNESS_MCP_SURFACE_POLICY \
    TEST_STUB_FILE="$stub_file" \
    PATH="$path_value" \
    HARNESS_CODEX_BIN="$TEST_BIN/codex" \
    HARNESS_DIR="$TEST_HARNESS" \
    HARNESS_NAME="test harness" \
    HARNESS_PREFIX="test" \
    HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="${TUI_CALLER_GLOBAL_MCP_ALLOWLIST:-}" \
    HARNESS_CODEX_APPS_ALLOWLIST="${TUI_CALLER_APPS_ALLOWLIST:-}" \
    HARNESS_PYTHON_BIN="${TUI_HARNESS_PYTHON_BIN:-}" \
    HARNESS_RUN_DIR="${HARNESS_RUN_DIR_OVERRIDE:-}" \
    bash "$TEST_LAUNCHER_BIN/launcher.sh" <<< "$input" > "$stub_file.tui.log" 2>&1
}

# Astra is appended so existing numeric profile choices stay stable.
printf 'model = "gpt-6-astra"\nmodel_reasoning_effort = "medium"\n' > "$TEST_HARNESS/.harness/codex/astra.config.toml"
STUB_ASTRA="$TEST_TEMP/out-astra.txt"
: > "$STUB_ASTRA"
run_tui $'2\n1\n6\n1\n1\n' "$STUB_ASTRA"
grep -qE '^ARGS:.*-p astra' "$STUB_ASTRA" || {
  echo 'FAIL: Astra TUI selection must launch native -p astra'; exit 1;
}
grep -q 'astra.*gpt-6-astra.*medium' "$STUB_ASTRA.tui.log" || {
  echo 'FAIL: Astra TUI label must show the generated model and effort'; exit 1;
}
echo 'PASS: Astra TUI selection and generated label'


# Case 1: runtime=Codex, session=New, mode=Base, safety=Default
STUB1="$TEST_TEMP/out1-codex-base.txt"
: > "$STUB1"
run_tui $'2\n1\n2\n1\n1\n' "$STUB1"
grep -q "^EXEC:codex" "$STUB1" || {
  echo "FAIL: case1 — expected codex exec; got:"; cat "$STUB1"; cat "$STUB1.tui.log"; exit 1;
}
grep -qE "^ARGS:.*--cd $TEST_HARNESS" "$STUB1" || {
  echo "FAIL: case1 — missing --cd"; cat "$STUB1"; exit 1;
}
if grep -qE "^ARGS:.*--remote " "$STUB1"; then
  echo "FAIL: case1 — must not use local app-server --remote"; cat "$STUB1"; exit 1;
fi
grep -qE "^ARGS:.*-p base" "$STUB1" || {
  echo "FAIL: case1 — missing -p base"; cat "$STUB1"; exit 1;
}
grep -q "^CODEX_HOME:$TEST_HARNESS/.harness/codex\$" "$STUB1" || {
  echo "FAIL: case1 — CODEX_HOME mismatch"; cat "$STUB1"; exit 1;
}
grep -q '^PREPARE_MCP_PROFILE:<UNSET>$' "$STUB1" || {
  echo "FAIL: case1 — default MCP surface leaked into Codex preparation"; cat "$STUB1"; exit 1;
}
grep -q '^PREPARE_GLOBAL_MCP_ALLOWLIST:tui-one,tui-two$' "$STUB1" || {
  echo "FAIL: case1 — TUI did not normalize the launcher global MCP allowlist before prepare"; cat "$STUB1"; exit 1;
}
grep -q '^PREPARE_CONTEXT:auto$' "$STUB1" || {
  echo "FAIL: case1 — default Codex context must be auto"; cat "$STUB1"; exit 1;
}
grep -q '^MCP_PROFILE:<UNSET>$' "$STUB1" || {
  echo "FAIL: case1 — default MCP surface leaked into Codex execution"; cat "$STUB1"; exit 1;
}
grep -q '^HARNESS_PREFIX:test$' "$STUB1" || {
  echo "FAIL: case1 — HARNESS_PREFIX was not exported to Codex"; cat "$STUB1"; exit 1;
}
[[ -d "$TEST_HARNESS/.harness/codex" ]] || {
  echo "FAIL: case1 — CODEX_HOME directory not prepared"; exit 1;
}
[[ -e "$TEST_HARNESS/.harness/codex/AGENTS.md" ]] || {
  echo "FAIL: case1 — AGENTS.md not created (prepare not invoked)"; exit 1;
}
echo "PASS: case1 — runtime=Codex base → direct TUI + codex --cd ... -p base + CODEX_HOME prepared"

# Each TUI invocation is a fresh process, so exercise the same inherited
# caller environment across three consecutive launches. Omitted and explicit
# empty config values must not carry the prior normalized value to prepare.
TUI_SEQUENCE_CONFIGURED="$TEST_TEMP/out1-global-configured.txt"
TUI_SEQUENCE_ABSENT="$TEST_TEMP/out1-global-absent.txt"
TUI_SEQUENCE_EMPTY="$TEST_TEMP/out1-global-empty.txt"
printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' \
  'HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST=" tui-sequence-one, tui-sequence-two,tui-sequence-one "' \
  > "$TEST_HARNESS/config/launcher.env"
TUI_CALLER_GLOBAL_MCP_ALLOWLIST="inherited-only" run_tui $'2\n1\n2\n1\n1\n' "$TUI_SEQUENCE_CONFIGURED"
printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' \
  > "$TEST_HARNESS/config/launcher.env"
TUI_CALLER_GLOBAL_MCP_ALLOWLIST="inherited-only" run_tui $'2\n1\n2\n1\n1\n' "$TUI_SEQUENCE_ABSENT"
printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' \
  'HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST=""' \
  > "$TEST_HARNESS/config/launcher.env"
TUI_CALLER_GLOBAL_MCP_ALLOWLIST="inherited-only" run_tui $'2\n1\n2\n1\n1\n' "$TUI_SEQUENCE_EMPTY"
tui_prepare_values=(
  "$(sed -n 's/^PREPARE_GLOBAL_MCP_ALLOWLIST://p' "$TUI_SEQUENCE_CONFIGURED")"
  "$(sed -n 's/^PREPARE_GLOBAL_MCP_ALLOWLIST://p' "$TUI_SEQUENCE_ABSENT")"
  "$(sed -n 's/^PREPARE_GLOBAL_MCP_ALLOWLIST://p' "$TUI_SEQUENCE_EMPTY")"
)
[[ "${tui_prepare_values[*]}" = "tui-sequence-one,tui-sequence-two <UNSET> <UNSET>" ]] || {
  echo "FAIL: TUI global MCP allowlist leaked across consecutive launches"
  cat "$TUI_SEQUENCE_CONFIGURED" "$TUI_SEQUENCE_ABSENT" "$TUI_SEQUENCE_EMPTY"
  exit 1
}
echo "PASS: TUI isolates global MCP allowlist across consecutive launches"

TUI_APPS_CONFIGURED="$TEST_TEMP/out1-apps-configured.txt"
TUI_APPS_ABSENT="$TEST_TEMP/out1-apps-absent.txt"
TUI_APPS_EMPTY="$TEST_TEMP/out1-apps-empty.txt"
printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' \
  'HARNESS_CODEX_APPS_ALLOWLIST=" tui_app_one, tui_app_two,tui_app_one "' \
  | sed 's/tui_app_/asdk_app_tui_/g' > "$TEST_HARNESS/config/launcher.env"
TUI_CALLER_APPS_ALLOWLIST="asdk_app_inherited" run_tui $'2\n1\n2\n1\n1\n' "$TUI_APPS_CONFIGURED"
printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' > "$TEST_HARNESS/config/launcher.env"
TUI_CALLER_APPS_ALLOWLIST="asdk_app_inherited" run_tui $'2\n1\n2\n1\n1\n' "$TUI_APPS_ABSENT"
printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' \
  'HARNESS_CODEX_APPS_ALLOWLIST=""' > "$TEST_HARNESS/config/launcher.env"
TUI_CALLER_APPS_ALLOWLIST="asdk_app_inherited" run_tui $'2\n1\n2\n1\n1\n' "$TUI_APPS_EMPTY"
tui_apps_prepare_values=(
  "$(sed -n 's/^PREPARE_APPS_ALLOWLIST://p' "$TUI_APPS_CONFIGURED")"
  "$(sed -n 's/^PREPARE_APPS_ALLOWLIST://p' "$TUI_APPS_ABSENT")"
  "$(sed -n 's/^PREPARE_APPS_ALLOWLIST://p' "$TUI_APPS_EMPTY")"
)
[[ "${tui_apps_prepare_values[*]}" = "asdk_app_tui_one,asdk_app_tui_two <UNSET> <UNSET>" ]] || {
  echo "FAIL: TUI app allowlist leaked across launches"
  cat "$TUI_APPS_CONFIGURED" "$TUI_APPS_ABSENT" "$TUI_APPS_EMPTY"
  exit 1
}
echo "PASS: TUI isolates and normalizes app allowlist across launches"

printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' \
  'HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST=" tui-fixture,tui-fixture "' \
  > "$TEST_HARNESS/config/launcher.env"
TUI_PYTHON_FIXTURE="$TEST_TEMP/out1-python-fixture.txt"
TUI_EXTRA_PATH="$TEST_BROKEN_BIN" TUI_HARNESS_PYTHON_BIN="$TEST_BIN/harness-python" \
  run_tui $'2\n1\n2\n1\n1\n' "$TUI_PYTHON_FIXTURE" || {
  echo "FAIL: TUI configured allowlist did not use HARNESS_PYTHON_BIN"; cat "$TUI_PYTHON_FIXTURE"; exit 1;
}
grep -q '^PREPARE_GLOBAL_MCP_ALLOWLIST:tui-fixture$' "$TUI_PYTHON_FIXTURE" || {
  echo "FAIL: TUI prepare did not receive normalized fixture allowlist"; cat "$TUI_PYTHON_FIXTURE"; exit 1;
}

TUI_PYTHON_INVALID="$TEST_TEMP/out1-python-invalid.txt"
TUI_EXTRA_PATH="$TEST_BROKEN_BIN" TUI_HARNESS_PYTHON_BIN="$TEST_BIN/not-an-interpreter" \
  run_tui $'2\n1\n2\n1\n1\n' "$TUI_PYTHON_INVALID"
[[ ! -s "$TUI_PYTHON_INVALID" ]] || {
  echo "FAIL: invalid TUI HARNESS_PYTHON_BIN reached prepare or Codex"; cat "$TUI_PYTHON_INVALID"; exit 1;
}
grep -q 'requires Python 3.11 or newer' "$TUI_PYTHON_INVALID.tui.log" || {
  echo "FAIL: invalid TUI HARNESS_PYTHON_BIN did not report the resolver failure"; cat "$TUI_PYTHON_INVALID.tui.log"; exit 1;
}
echo "PASS: TUI global MCP normalization honors and validates HARNESS_PYTHON_BIN"

# Case 1a: an external orchestrator can pin the Codex launch to a profile-local worktree.
STUB1A="$TEST_TEMP/out1a-codex-worktree.txt"
: > "$STUB1A"
HARNESS_RUN_DIR_OVERRIDE="$TEST_WORKTREE" run_tui $'2\n1\n2\n1\n1\n' "$STUB1A"
grep -qE "^ARGS:.*--cd $TEST_WORKTREE_REAL" "$STUB1A" || {
  echo "FAIL: case1a — Codex ignored HARNESS_RUN_DIR"; cat "$STUB1A"; exit 1;
}
echo "PASS: case1a — Codex TUI uses the explicit worktree"
for expected in \
  "fast — Quick · shallow — gpt-6-luna · low" \
  "base — Everyday · Recommended — gpt-5.6-terra · medium" \
  "sol — Stronger · slower — gpt-6.1-sol · high" \
  "plan — Planning · deep — gpt-6.1-sol · high" \
  "rich — Deep · slowest — gpt-6.1-sol · xhigh"; do
  grep -Fq "$expected" "$STUB1.tui.log" || {
    echo "FAIL: Codex profile intent label missing: $expected"
    cat "$STUB1.tui.log"
    exit 1
  }
done
if grep -Eq 'luna6|sol6' "$STUB1.tui.log"; then
  echo "FAIL: obsolete GPT-6 choices remain in TUI"; exit 1
fi
echo "PASS: Codex TUI mode labels match GPT-6 routing"

# Case 1b: runtime=Codex, base mode, work via the final-menu MCP surface toggle
# (same UX as the claude/kiro light toggle). The surface must be selected
# before Codex home preparation and execution.
# final menu: 1 Start / 2 MCP surface / [3 Happy] / Back
STUB1B="$TEST_TEMP/out1b-codex-work-surface.txt"
: > "$STUB1B"
run_tui $'2\n1\n2\n1\n2\n1\n' "$STUB1B"
grep -q '^EXEC:codex' "$STUB1B" || {
  echo "FAIL: case1b — expected codex exec; got:"; cat "$STUB1B"; cat "$STUB1B.tui.log"; exit 1;
}
grep -q '^MCP_PROFILE:work$' "$STUB1B" || {
  echo "FAIL: case1b — expected work MCP surface before Codex exec"; cat "$STUB1B"; cat "$STUB1B.tui.log"; exit 1;
}
grep -q '^PREPARE_MCP_PROFILE:work$' "$STUB1B" || {
  echo "FAIL: case1b — expected work MCP surface before Codex preparation"; cat "$STUB1B"; cat "$STUB1B.tui.log"; exit 1;
}
grep -q 'MCP surface: work' "$STUB1B.tui.log" || {
  echo "FAIL: case1b — MCP surface toggle was not shown as work"; cat "$STUB1B.tui.log"; exit 1;
}
grep -q 'work — base' "$STUB1B.tui.log" && {
  echo "FAIL: case1b — work must no longer be a Profile menu entry"; cat "$STUB1B.tui.log"; exit 1;
}
echo "PASS: case1b — Codex work MCP surface toggle exports before execution"

# Case 1c: work surface combines with a non-base profile (rich), like claude light.
STUB1C="$TEST_TEMP/out1c-codex-work-rich.txt"
: > "$STUB1C"
run_tui $'2\n1\n5\n1\n2\n1\n' "$STUB1C"
grep -qE "^ARGS:.*-p rich" "$STUB1C" || {
  echo "FAIL: case1c — expected -p rich with work surface"; cat "$STUB1C"; exit 1;
}
grep -q '^MCP_PROFILE:work$' "$STUB1C" || {
  echo "FAIL: case1c — work surface must combine with rich profile"; cat "$STUB1C"; cat "$STUB1C.tui.log"; exit 1;
}
echo "PASS: case1c — work MCP surface combines with any profile"

STUB500="$TEST_TEMP/out500-codex.txt"
: > "$STUB500"
run_tui $'2\n1\n2\n1\n3\n3\n1\n' "$STUB500"
grep -q '^PREPARE_CONTEXT:500k$' "$STUB500" || { echo "FAIL: TUI 500k"; exit 1; }
echo "PASS: TUI 500k reaches preparation"

# Case 1d: 1M is an explicit final-menu choice and reaches preparation.
# With no Happy binary: Start(1), MCP(2), Context(3), Back(4).
STUB1D="$TEST_TEMP/out1d-codex-1m.txt"
: > "$STUB1D"
run_tui $'2\n1\n2\n1\n3\n3\n3\n1\n' "$STUB1D"
grep -q '^PREPARE_CONTEXT:1m$' "$STUB1D" || {
  echo "FAIL: case1d — 1M context selection did not reach preparation"; cat "$STUB1D"; cat "$STUB1D.tui.log"; exit 1;
}
grep -q 'Context: 1M' "$STUB1D.tui.log" || {
  echo "FAIL: case1d — final menu did not show selected 1M context"; cat "$STUB1D.tui.log"; exit 1;
}
echo "PASS: case1d — TUI exposes and exports the explicit 1M context choice"

# Case 1e: a failed 1M Codex preparation must not leak the context selector
# into a subsequent Claude launch from the retry loop.
STUB1E="$TEST_TEMP/out1e-context-retry-isolation.txt"
: > "$STUB1E"
: > "$STUB1E.fail-prepare-once"
run_tui $'2\n1\n2\n1\n3\n1\n1\n1\n2\n1\n' "$STUB1E"
grep -q '^EXEC:claude$' "$STUB1E" || {
  echo "FAIL: case1e — retry did not reach Claude"; cat "$STUB1E"; cat "$STUB1E.tui.log"; exit 1;
}
grep -q '^CODEX_CONTEXT:<UNSET>$' "$STUB1E" || {
  echo "FAIL: case1e — failed Codex launch leaked HARNESS_CODEX_CONTEXT"; cat "$STUB1E"; exit 1;
}
echo "PASS: case1e — retry loop clears the Codex context selector"

# Case 2: runtime=Codex, session=Continue last, mode=Plan, safety=Default
STUB2="$TEST_TEMP/out2-codex-continue.txt"
: > "$STUB2"
run_tui $'2\n2\n4\n1\n1\n' "$STUB2"
grep -qE "^ARGS:resume( |.*--cd)" "$STUB2" || {
  echo "FAIL: case2 — expected 'resume' as first codex arg"; cat "$STUB2"; exit 1;
}
grep -qE "^ARGS:.*--last" "$STUB2" || {
  echo "FAIL: case2 — expected --last for continue"; cat "$STUB2"; exit 1;
}
grep -qE "^ARGS:.*-p plan" "$STUB2" || {
  echo "FAIL: case2 — expected -p plan"; cat "$STUB2"; exit 1;
}
echo "PASS: case2 — Codex continue + plan → codex resume --last -p plan"

# Case 3: runtime=Codex, safety=Full auto
STUB3="$TEST_TEMP/out3-codex-fullauto.txt"
: > "$STUB3"
run_tui $'2\n1\n2\n2\n1\n' "$STUB3"
grep -qE "^ARGS:.*--full-auto" "$STUB3" || {
  echo "FAIL: case3 — expected --full-auto"; cat "$STUB3"; exit 1;
}
echo "PASS: case3 — Safety=Full auto → --full-auto flag"

# Case 3b: runtime=Codex, Happy=yes → exec happy codex with same Codex args
# final menu with happy visible: 1 Start / 2 MCP surface / 3 Happy / 4 Context / 5 Back
STUB3B="$TEST_TEMP/out3b-codex-happy.txt"
: > "$STUB3B"
run_tui $'2\n1\n2\n1\n3\n1\n' "$STUB3B" "$HAPPY_BIN"
grep -q "^EXEC:happy" "$STUB3B" || {
  echo "FAIL: case3b — expected happy exec; got:"; cat "$STUB3B"; cat "$STUB3B.tui.log"; exit 1;
}
grep -qE "^ARGS:codex$" "$STUB3B" || {
  echo "FAIL: case3b — expected happy codex without unsupported Codex CLI args"; cat "$STUB3B"; exit 1;
}
grep -q "^CODEX_HOME:$TEST_HARNESS/.harness/codex\$" "$STUB3B" || {
  echo "FAIL: case3b — CODEX_HOME mismatch"; cat "$STUB3B"; exit 1;
}
grep -q '^HARNESS_PREFIX:test$' "$STUB3B" || {
  echo "FAIL: case3b — HARNESS_PREFIX was not exported through Happy"; cat "$STUB3B"; exit 1;
}
echo "PASS: case3b — runtime=Codex + Happy=yes → exec happy codex"
cat >> "$TEST_HARNESS/config/launcher.env" <<'EOF'
HARNESS_CODEX_APPS_ALLOWLIST="asdk_app_slacktest"
HARNESS_CODEX_SLACK_APPS="asdk_app_slacktest"
EOF
STUB_SLACK_HAPPY="$TEST_TEMP/out-slack-happy.txt"
: > "$STUB_SLACK_HAPPY"
run_tui $'2\n1\n2\n1\n3\n1\n' "$STUB_SLACK_HAPPY" "$HAPPY_BIN" || true
! grep -q '^EXEC:' "$STUB_SLACK_HAPPY" || { echo 'FAIL: Slack Happy TUI executed'; exit 1; }
grep -q 'Happy.*Slack.*native' "$STUB_SLACK_HAPPY.tui.log" || { echo 'FAIL: Slack Happy TUI missing guidance'; cat "$STUB_SLACK_HAPPY.tui.log"; exit 1; }
printf 'HARNESS_NAME="test harness"\nHARNESS_PREFIX="test"\n' > "$TEST_HARNESS/config/launcher.env"
echo 'PASS: Slack Happy TUI is rejected'


# Case 3c: Happy installed, but non-base Codex mode must not offer Happy prompt
STUB3C="$TEST_TEMP/out3c-codex-rich-happy-installed.txt"
: > "$STUB3C"
run_tui $'2\n1\n5\n1\n1\n' "$STUB3C" "$HAPPY_BIN"
grep -q "^EXEC:codex" "$STUB3C" || {
  echo "FAIL: case3c — expected native codex exec; got:"; cat "$STUB3C"; cat "$STUB3C.tui.log"; exit 1;
}
grep -qE "^ARGS:.*-p rich" "$STUB3C" || {
  echo "FAIL: case3c — expected native codex rich profile"; cat "$STUB3C"; exit 1;
}
if grep -q "^EXEC:happy" "$STUB3C"; then
  echo "FAIL: case3c — Happy prompt/path should be skipped for non-base Codex profile"; cat "$STUB3C"; exit 1;
fi
echo "PASS: case3c — Happy installed + Codex rich skips Happy prompt and runs native Codex"

# Case 4: only Claude in PATH (no codex) → runtime menu auto-skips, Claude flow runs
NO_CODEX_BIN="$TEST_TEMP/bin-noco"
mkdir -p "$NO_CODEX_BIN"
cp "$TEST_BIN/claude" "$NO_CODEX_BIN/claude"
chmod +x "$NO_CODEX_BIN/claude"

STUB4="$TEST_TEMP/out4-claude-only.txt"
: > "$STUB4"
rm -f "$TEST_HARNESS/.harness/launcher-last" "$TEST_HARNESS/.harness/launcher-history"
env -u HARNESS_RUN_DIR TEST_STUB_FILE="$STUB4" \
PATH="$NO_CODEX_BIN:/usr/bin:/bin" \
HARNESS_CODEX_BIN="$TEST_TEMP/missing-codex" \
HARNESS_DIR="$TEST_HARNESS" \
HARNESS_NAME="test harness" \
bash "$TEST_LAUNCHER_BIN/launcher.sh" <<< $'1\n2\n1\n' >"$STUB4.tui.log" 2>&1 || true
grep -q "^EXEC:claude" "$STUB4" || {
  echo "FAIL: case4 — runtime auto-skip didn't reach Claude exec"; cat "$STUB4"; cat "$STUB4.tui.log"; exit 1;
}
echo "PASS: case4 — codex absent → runtime menu auto-skips to Claude flow"

# Case 5: runtime=Claude (with both runtimes available) routes to Claude
STUB5="$TEST_TEMP/out5-claude-via-tui.txt"
: > "$STUB5"
run_tui $'1\n1\n2\n1\n' "$STUB5"
grep -q "^EXEC:claude" "$STUB5" || {
  echo "FAIL: case5 — expected claude exec when runtime=Claude"; cat "$STUB5"; exit 1;
}
if grep -q "^EXEC:codex" "$STUB5"; then
  echo "FAIL: case5 — codex was launched even though runtime=Claude was selected"; cat "$STUB5"; exit 1;
fi
echo "PASS: case5 — runtime=Claude routes to Claude exec"

# Case 6: Happy toggled on, then Back → safety changed to full-auto.
# Compatibility is broken, so the hidden toggle must auto-clear and Start
# must launch native codex with --full-auto (not fail into plan_reset).
STUB6="$TEST_TEMP/out6-happy-residue.txt"
: > "$STUB6"
# final(compatible): Happy=3, Back=5 → safety full-auto=2 → final(incompatible): Start=1
run_tui $'2\n1\n2\n1\n3\n5\n2\n1\n' "$STUB6" "$HAPPY_BIN"
grep -q "^EXEC:codex" "$STUB6" || {
  echo "FAIL: case6 — expected native codex exec after happy auto-clear"; cat "$STUB6"; cat "$STUB6.tui.log"; exit 1;
}
grep -qE "^ARGS:.*--full-auto" "$STUB6" || {
  echo "FAIL: case6 — expected --full-auto"; cat "$STUB6"; exit 1;
}
if grep -q "^EXEC:happy" "$STUB6"; then
  echo "FAIL: case6 — happy must auto-clear when compatibility breaks"; cat "$STUB6"; exit 1;
fi
if grep -q "시작 실패" "$STUB6.tui.log"; then
  echo "FAIL: case6 — start must succeed, not fail into plan reset"; cat "$STUB6.tui.log"; exit 1;
fi
echo "PASS: case6 — stale Happy toggle auto-clears when compatibility breaks"

# Case 6b: Happy on, then work toggle → happy must drop; codex runs with work.
STUB6B="$TEST_TEMP/out6b-happy-work-exclusive.txt"
: > "$STUB6B"
# final: Happy on(3) → MCP surface work(2) → Start(1)
run_tui $'2\n1\n2\n1\n3\n2\n1\n' "$STUB6B" "$HAPPY_BIN"
grep -q "^EXEC:codex" "$STUB6B" || {
  echo "FAIL: case6b — expected native codex exec after work toggle drops happy"; cat "$STUB6B"; cat "$STUB6B.tui.log"; exit 1;
}
grep -q '^MCP_PROFILE:work$' "$STUB6B" || {
  echo "FAIL: case6b — expected work surface"; cat "$STUB6B"; exit 1;
}
if grep -q "^EXEC:happy" "$STUB6B"; then
  echo "FAIL: case6b — happy must not survive the work toggle"; cat "$STUB6B"; exit 1;
fi
echo "PASS: case6b — work toggle drops Happy (mutually exclusive)"

# Case 7: replaying a 0.12.0-era work history row (CODEX_SURFACE=work written
# by the removed Profile-menu flow) must still launch with the work surface.
# run_tui wipes the history, so drive the launcher directly with a seeded row.
STUB7="$TEST_TEMP/out7-legacy-work-history.txt"
: > "$STUB7"
mkdir -p "$TEST_HARNESS/.harness"
printf 'TS=1\tSUMMARY=Codex · new · base · work-MCP\tRUNTIME=codex\tSESSION=new\tMODE=\tPERM=default\tMCP_SURFACE=full\tCHROME=0\tHAPPY=0\tCODEX_PROFILE=base\tCODEX_SURFACE=work\tCODEX_SAFETY=default\tKIRO_TRUST=0\n' \
  > "$TEST_HARNESS/.harness/launcher-history"
# launchpad: New Claude(1), New Codex(2), hist row(3)
TEST_STUB_FILE="$STUB7" \
PATH="$TEST_BIN:/usr/bin:/bin" \
HARNESS_CODEX_BIN="$TEST_BIN/codex" \
HARNESS_DIR="$TEST_HARNESS" \
HARNESS_NAME="test harness" \
bash "$TEST_LAUNCHER_BIN/launcher.sh" <<< $'3\n' > "$STUB7.tui.log" 2>&1 || true
grep -q '^PREPARE_MCP_PROFILE:work$' "$STUB7" || {
  echo "FAIL: case7 — 0.12-era work history row must prepare with work surface"; cat "$STUB7"; cat "$STUB7.tui.log"; exit 1;
}
grep -q '^MCP_PROFILE:work$' "$STUB7" || {
  echo "FAIL: case7 — 0.12-era work history row must exec with work surface"; cat "$STUB7"; exit 1;
}
grep -qE "^ARGS:.*-p base" "$STUB7" || {
  echo "FAIL: case7 — expected -p base from the replayed row"; cat "$STUB7"; exit 1;
}
rm -f "$TEST_HARNESS/.harness/launcher-history"
echo "PASS: case7 — 0.12-era CODEX_SURFACE=work history row replays correctly"

# Case 7b: context is part of exact launch history and survives replay.
STUB7B="$TEST_TEMP/out7b-context-history.txt"
: > "$STUB7B"
printf 'TS=2\tSUMMARY=Codex · new · base · 1M\tRUNTIME=codex\tSESSION=new\tCODEX_PROFILE=base\tCODEX_SURFACE=default\tCODEX_SAFETY=default\tCODEX_CONTEXT=1m\n' \
  > "$TEST_HARNESS/.harness/launcher-history"
TEST_STUB_FILE="$STUB7B" \
PATH="$TEST_BIN:/usr/bin:/bin" \
HARNESS_CODEX_BIN="$TEST_BIN/codex" \
HARNESS_DIR="$TEST_HARNESS" \
HARNESS_NAME="test harness" \
bash "$TEST_LAUNCHER_BIN/launcher.sh" <<< $'3\n' > "$STUB7B.tui.log" 2>&1 || true
grep -q '^PREPARE_CONTEXT:1m$' "$STUB7B" || {
  echo "FAIL: case7b — replayed history did not preserve 1M context"; cat "$STUB7B"; cat "$STUB7B.tui.log"; exit 1;
}
rm -f "$TEST_HARNESS/.harness/launcher-history"
echo "PASS: case7b — launch history preserves the selected context"

# Old rows predate the context field; new rows retain their exact choice.
for spec in 'resume:legacy:272k' 'new:legacy:272k' 'fork:legacy:272k' 'new:auto:auto' 'new:500k:500k' 'new:272k:272k' 'new:1m:1m'; do
  session="${spec%%:*}"; rest="${spec#*:}"; field="${rest%%:*}"; expected="${rest##*:}"
  stub="$TEST_TEMP/history-$session-$field"; : > "$stub"
  printf 'TS=3\tSUMMARY=history\tRUNTIME=codex\tSESSION=%s\tCODEX_PROFILE=base' "$session" > "$TEST_HARNESS/.harness/launcher-history"
  [[ "$field" == legacy ]] || printf '\tCODEX_CONTEXT=%s' "$field" >> "$TEST_HARNESS/.harness/launcher-history"
  printf '\n' >> "$TEST_HARNESS/.harness/launcher-history"
  TEST_STUB_FILE="$stub" PATH="$TEST_BIN:/usr/bin:/bin" HARNESS_CODEX_BIN="$TEST_BIN/codex" HARNESS_DIR="$TEST_HARNESS" HARNESS_NAME="test harness" \
    bash "$TEST_LAUNCHER_BIN/launcher.sh" <<< $'3\n' > "$stub.tui.log" 2>&1 || true
  grep -q "^PREPARE_CONTEXT:$expected$" "$stub" || { echo "FAIL: history $spec"; cat "$stub"; exit 1; }
done
rm -f "$TEST_HARNESS/.harness/launcher-history"
echo "PASS: legacy and explicit context history replay"

# Pre-history launcher-last migration must retain the historical default too.
for field in legacy auto 272k 500k 1m; do
  stub="$TEST_TEMP/migration-$field"; : > "$stub"
  rm -f "$TEST_HARNESS/.harness/launcher-history"
  printf 'SUMMARY=legacy\nRUNTIME=codex\nSESSION=resume\nCODEX_PROFILE=base\n' > "$TEST_HARNESS/.harness/launcher-last"
  if [[ "$field" == legacy ]]; then expected=272k; else
    expected="$field"; printf 'CODEX_CONTEXT=%s\n' "$field" >> "$TEST_HARNESS/.harness/launcher-last"
  fi
  TEST_STUB_FILE="$stub" PATH="$TEST_BIN:/usr/bin:/bin" HARNESS_CODEX_BIN="$TEST_BIN/codex" HARNESS_DIR="$TEST_HARNESS" HARNESS_NAME="test harness" \
    bash "$TEST_LAUNCHER_BIN/launcher.sh" <<< $'3\n' > "$stub.tui.log" 2>&1 || true
  grep -q "^PREPARE_CONTEXT:$expected$" "$stub" || { echo "FAIL: migrated history $field"; cat "$stub"; exit 1; }
done
rm -f "$TEST_HARNESS/.harness/launcher-history"
echo "PASS: legacy launcher-last context migration"

# Case 8: opt-in Codex has one full surface. The retired toggle must be absent,
# prepare/exec must not receive a profile, and new history must be canonical.
printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' \
  'HARNESS_MCP_SURFACE_POLICY="single-full"' > "$TEST_HARNESS/config/launcher.env"
STUB8="$TEST_TEMP/out8-codex-single-full.txt"
: > "$STUB8"
run_tui $'2\n1\n2\n1\n1\n' "$STUB8"
grep -q '^EXEC:codex' "$STUB8" || {
  echo "FAIL: case8 — expected opt-in Codex exec"; cat "$STUB8"; cat "$STUB8.tui.log"; exit 1;
}
grep -q '^PREPARE_MCP_PROFILE:<UNSET>$' "$STUB8" || {
  echo "FAIL: case8 — opt-in Codex preparation must use the full surface"; cat "$STUB8"; exit 1;
}
grep -q '^MCP_PROFILE:<UNSET>$' "$STUB8" || {
  echo "FAIL: case8 — opt-in Codex execution must use the full surface"; cat "$STUB8"; exit 1;
}
grep -q 'MCP surface:' "$STUB8.tui.log" && {
  echo "FAIL: case8 — opt-in Codex final menu must not expose a surface row"; cat "$STUB8.tui.log"; exit 1;
}
head -1 "$TEST_HARNESS/.harness/launcher-history" | grep -q 'CODEX_SURFACE=full' || {
  echo "FAIL: case8 — opt-in Codex history must record CODEX_SURFACE=full";
  cat "$TEST_HARNESS/.harness/launcher-history"; exit 1;
}
grep -q 'work-MCP' "$TEST_HARNESS/.harness/launcher-history" && {
  echo "FAIL: case8 — opt-in Codex history must drop the legacy suffix";
  cat "$TEST_HARNESS/.harness/launcher-history"; exit 1;
}
echo "PASS: case8 — opt-in Codex final menu and new history use one full surface"

# Case 8b: full is the opt-in Happy-compatible default. With the retired
# surface row gone, the final menu is Start / Happy / Back.
STUB8B="$TEST_TEMP/out8b-codex-single-full-happy.txt"
: > "$STUB8B"
run_tui $'2\n1\n2\n1\n2\n1\n' "$STUB8B" "$HAPPY_BIN"
grep -q '^EXEC:happy$' "$STUB8B" || {
  echo "FAIL: case8b — opt-in full surface must remain Happy-compatible";
  cat "$STUB8B"; cat "$STUB8B.tui.log"; exit 1;
}
head -1 "$TEST_HARNESS/.harness/launcher-history" | grep -q 'CODEX_SURFACE=full' || {
  echo "FAIL: case8b — opt-in Happy history must remain canonical full";
  cat "$TEST_HARNESS/.harness/launcher-history"; exit 1;
}
echo "PASS: case8b — opt-in Codex Happy accepts the canonical full surface"

# Launch record: the picker exports the same HARNESS_LAUNCH_* facts as the
# shortcut path, so the SessionStart hook can record the grant for a restore.
TEST_HARNESS_REAL="$(cd -P "$TEST_HARNESS" && pwd -P)"
launch_env_is() {
  local stub="$1" label="$2"; shift 2
  local pair
  for pair in "$@"; do
    grep -qxF "LAUNCH_$pair" "$stub" || {
      echo "FAIL: $label — expected LAUNCH_$pair"; cat "$stub"; cat "$stub.tui.log"; exit 1;
    }
  done
}
STUB_LR1="$TEST_TEMP/out-lr1-bypass.txt"; : > "$STUB_LR1"
run_tui $'2\n1\n3\n4\n1\n' "$STUB_LR1"
grep -qE '^ARGS:.*-p sol --dangerously-bypass-approvals-and-sandbox' "$STUB_LR1" || {
  echo "FAIL: lr1 — expected sol + bypass"; cat "$STUB_LR1"; exit 1;
}
launch_env_is "$STUB_LR1" lr1 BYPASS:1 PROFILE:sol "SOURCE_ROOT:$TEST_HARNESS_REAL" ISOLATED:0 \
  APPROVAL:'<UNSET>' SANDBOX:'<UNSET>' PERMISSION:'<UNSET>'
STUB_LR2="$TEST_TEMP/out-lr2-full-auto.txt"; : > "$STUB_LR2"
run_tui $'2\n1\n2\n2\n1\n' "$STUB_LR2"
launch_env_is "$STUB_LR2" lr2 APPROVAL:on-request SANDBOX:workspace-write BYPASS:'<UNSET>' PROFILE:base
STUB_LR3="$TEST_TEMP/out-lr3-never.txt"; : > "$STUB_LR3"
run_tui $'2\n1\n5\n3\n1\n' "$STUB_LR3"
launch_env_is "$STUB_LR3" lr3 APPROVAL:never SANDBOX:'<UNSET>' BYPASS:'<UNSET>' PROFILE:rich
# A grant inherited from a parent launch never survives into a default launch.
STUB_LR4="$TEST_TEMP/out-lr4-inherited.txt"; : > "$STUB_LR4"
export HARNESS_LAUNCH_BYPASS=1 HARNESS_LAUNCH_APPROVAL=never HARNESS_LAUNCH_PERMISSION=bypassPermissions
run_tui $'2\n1\n2\n1\n1\n' "$STUB_LR4"
unset HARNESS_LAUNCH_BYPASS HARNESS_LAUNCH_APPROVAL HARNESS_LAUNCH_PERMISSION
launch_env_is "$STUB_LR4" lr4 BYPASS:'<UNSET>' APPROVAL:'<UNSET>' PERMISSION:'<UNSET>' PROFILE:base ISOLATED:0
# Happy runs codex with no -p and the default safety: no profile and no grant,
# even when a parent launch left some in the environment.
STUB_LR5="$TEST_TEMP/out-lr5-happy.txt"; : > "$STUB_LR5"
export HARNESS_LAUNCH_BYPASS=1 HARNESS_LAUNCH_PROFILE=rich
run_tui $'2\n1\n2\n1\n2\n1\n' "$STUB_LR5" "$HAPPY_BIN"
unset HARNESS_LAUNCH_BYPASS HARNESS_LAUNCH_PROFILE
grep -q '^EXEC:happy$' "$STUB_LR5" || { echo "FAIL: lr5 — expected happy"; cat "$STUB_LR5"; cat "$STUB_LR5.tui.log"; exit 1; }
launch_env_is "$STUB_LR5" lr5 PROFILE:'<UNSET>' BYPASS:'<UNSET>' APPROVAL:'<UNSET>' SANDBOX:'<UNSET>' \
  "SOURCE_ROOT:$TEST_HARNESS_REAL" ISOLATED:0
echo "PASS: launch record — the picker exports the Codex grant, profile, root and isolation"

# Slack opt-in must record the effective native user-approval grant, including bypass.
printf '%s\n' 'HARNESS_CODEX_APPS_ALLOWLIST="asdk_app_slack"' 'HARNESS_CODEX_SLACK_APPS="asdk_app_slack"' >> "$TEST_HARNESS/config/launcher.env"
STUB_SLACK="$TEST_TEMP/out-slack-record.txt"; : > "$STUB_SLACK"
run_tui $'2\n1\n5\n4\n1\n' "$STUB_SLACK"
launch_env_is "$STUB_SLACK" slack APPROVAL:on-request SANDBOX:danger-full-access BYPASS:'<UNSET>' PROFILE:rich
STUB_SLACK_NEVER="$TEST_TEMP/out-slack-never-record.txt"; : > "$STUB_SLACK_NEVER"
run_tui $'2\n1\n5\n3\n1\n' "$STUB_SLACK_NEVER"
launch_env_is "$STUB_SLACK_NEVER" slack-never APPROVAL:on-request SANDBOX:'<UNSET>' BYPASS:'<UNSET>' PROFILE:rich
echo "✓ All codex TUI tests passed"
