#!/usr/bin/env bash
# TUI v2 flow tests — drives bin/launcher.sh through the no-gum fallback menu.
# Covers: mode table labels, final-menu toggles (permission/chrome/happy),
# session flags, invalid-input reprompt, back navigation, repeat-last replay,
# MCP duplicate validation blocking, HARNESS_KIRO_BIN resolution, gateways.
set -euo pipefail

# Controller session identity must not bind these independent fake harnesses.
unset HARNESS_SOURCE_ROOT HARNESS_SESSION_ROOT HARNESS_SESSION_ID HARNESS_RUN_DIR

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCHER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cleanup() {
  [[ -n "${TEST_TEMP:-}" && -d "$TEST_TEMP" ]] && rm -rf "$TEST_TEMP"
}
trap cleanup EXIT

TEST_TEMP="$(mktemp -d)"
TEST_HARNESS="$TEST_TEMP/fake-harness"
TEST_BIN="$TEST_TEMP/bin"
mkdir -p "$TEST_HARNESS/config" "$TEST_BIN"
mkdir -p "$TEST_HARNESS/core/bin"
touch "$TEST_HARNESS/core/bin/start-ssh-mcp.sh"
TEST_WORKTREE="$TEST_HARNESS/.worktrees/sample"
mkdir -p "$TEST_WORKTREE"
TEST_WORKTREE_REAL="$(cd -P "$TEST_WORKTREE" && pwd -P)"
cat > "$TEST_HARNESS/config/launcher.env" <<'EOF'
HARNESS_NAME="test harness"
HARNESS_PREFIX="test"
EOF
mkdir -p "$TEST_HARNESS/.claude"
cat > "$TEST_HARNESS/.claude/settings.local.json" <<'EOF'
{"env":{"TEST_CLAUDE_MCP_TOKEN":"fixture-token"}}
EOF

TEST_BASE_PATH="$TEST_BIN:/usr/bin:/bin:/usr/sbin:/sbin"

write_stub() {
  # write_stub <name> — records EXEC line + env snapshot then exits.
  cat > "$TEST_BIN/$1" <<EOF
#!/usr/bin/env bash
{
  echo "EXEC:$1 \$*"
  echo "PWD:\$PWD"
  echo "BASE_URL:\${ANTHROPIC_BASE_URL:-}"
  echo "AUTH_TOKEN:\${ANTHROPIC_AUTH_TOKEN:-}"
  echo "OPUS_MODEL:\${ANTHROPIC_DEFAULT_OPUS_MODEL:-}"
  echo "PCT:\${CLAUDE_AUTOCOMPACT_PCT_OVERRIDE:-}"
  [[ "\${TEST_CLAUDE_MCP_TOKEN:-}" == fixture-token ]] && echo "MCP_AUTH:loaded"
  echo "HISTORY_SUMMARY_LEAK:\${HISTORY_SUMMARY_LEAK:-<UNSET>}"
  echo "CMUX_WORKSPACE_ID:\${CMUX_WORKSPACE_ID:-<UNSET>}"
  echo "CMUX_TAB_ID:\${CMUX_TAB_ID:-<UNSET>}"
  echo "CMUX_SURFACE_ID:\${CMUX_SURFACE_ID:-<UNSET>}"
} >> "\$TEST_STUB_FILE"
printf '%s\n' "\$@" > "\$TEST_STUB_FILE.argv"
exit 0
EOF
  chmod +x "$TEST_BIN/$1"
}

write_node_stub() {
  cat > "$TEST_BIN/node" <<'EOF'
#!/usr/bin/env bash
provider_url="${PROBE_PROVIDER_URL:-}"
case "$provider_url" in
  *"kiro.test"*) [[ "${TEST_KIRO_HEALTH:-0}" == "1" ]] && exit 0 || exit 1 ;;
  *"codex.test"*) [[ "${TEST_CODEX_HEALTH:-0}" == "1" ]] && exit 0 || exit 1 ;;
  *) exit 1 ;;
esac
EOF
  chmod +x "$TEST_BIN/node"
}

TEST_HOME="$TEST_TEMP/home"
mkdir -p "$TEST_HOME"

run_tui() {
  # run_tui <input> <out-file> <stub-file> [extra env as K=V ...]
  # HOME is isolated so the real ~/.claude.json cannot bleed user-scope MCP
  # servers into light-surface assertions.
  local input="$1" out_file="$2" stub_file="$3"; shift 3
  env -u HARNESS_DIR -u HARNESS_RUN_DIR -u HARNESS_PREFIX \
    -u CMUX_WORKSPACE_ID -u CMUX_TAB_ID -u CMUX_SURFACE_ID \
    -u HARNESS_CODEX_MCP_PROFILE -u HARNESS_MCP_SURFACE_POLICY "$@" \
    TEST_STUB_FILE="$stub_file" \
    PATH="$TEST_BASE_PATH" \
    HOME="$TEST_HOME" \
    HARNESS_CODEX_BIN="${HARNESS_CODEX_BIN_OVERRIDE:-$TEST_TEMP/missing-codex}" \
    HARNESS_DIR="$TEST_HARNESS" \
    HARNESS_NAME="test harness" \
    bash "$LAUNCHER_DIR/bin/launcher.sh" <<< "$input" > "$out_file" 2>&1 || true
}

run_seeded_tui_status() {
  # Preserve the seeded history and expose the launcher status.
  local input="$1" out_file="$2" stub_file="$3"; shift 3
  set +e
  env -u HARNESS_DIR -u HARNESS_RUN_DIR -u HARNESS_PREFIX \
    -u CMUX_WORKSPACE_ID -u CMUX_TAB_ID -u CMUX_SURFACE_ID \
    -u HARNESS_CODEX_MCP_PROFILE -u HARNESS_MCP_SURFACE_POLICY "$@" \
    TEST_STUB_FILE="$stub_file" \
    PATH="$TEST_BASE_PATH" \
    HOME="$TEST_HOME" \
    HARNESS_CODEX_BIN="${HARNESS_CODEX_BIN_OVERRIDE:-$TEST_TEMP/missing-codex}" \
    HARNESS_DIR="$TEST_HARNESS" \
    HARNESS_NAME="test harness" \
    bash "$LAUNCHER_DIR/bin/launcher.sh" <<< "$input" > "$out_file" 2>&1
  RUN_TUI_RC=$?
  set -e
}

fail() { echo "FAIL: $1"; [ -n "${2:-}" ] && { echo "--- output:"; cat "$2"; }; exit 1; }

reset_plan() { rm -f "$TEST_HARNESS/.harness/launcher-last" "$TEST_HARNESS/.harness/launcher-history"; }
HISTORY="$TEST_HARNESS/.harness/launcher-history"

assert_no_migration_temps() {
  local leftovers=("$HISTORY".migrate*)
  [[ ! -e "${leftovers[0]}" ]] || fail 'history migration must clean every scratch path' "${leftovers[0]}"
}

write_stub claude
write_node_stub
export CMUX_WORKSPACE_ID="test-workspace"
export CMUX_TAB_ID="test-tab"
export CMUX_SURFACE_ID="test-surface"

# --- 1. base start: session→mode(base)→start -------------------------------
# Launcher-owned Slack settings may precede native selectors; assertions below
# retain the exact selector/model/effort sequence after that settings block.
OUT="$TEST_TEMP/1.out"; STUB="$TEST_TEMP/1.stub"; reset_plan
run_tui $'1\n2\n1\n' "$OUT" "$STUB"
grep -q 'EXEC:claude .*--model sonnet --effort high --exclude-dynamic-system-prompt-sections' "$STUB" \
  || fail 'base mode should exec claude sonnet/high' "$OUT"
grep -Fq 'mcp__slack__send_message' "$STUB" || fail 'TUI must carry mandatory Slack ask rules' "$STUB"
grep -Fqx 'MCP_AUTH:loaded' "$STUB" || fail 'TUI Claude did not inherit harness-local MCP credentials' "$OUT"
FOREIGN_HARNESS="$TEST_TEMP/foreign-harness"
mkdir -p "$FOREIGN_HARNESS/.claude"
cat > "$FOREIGN_HARNESS/.claude/settings.local.json" <<'EOF'
{"env":{"TEST_CLAUDE_MCP_TOKEN":"wrong-profile-token"}}
EOF
FOREIGN_OUT="$TEST_TEMP/1-foreign.out"; FOREIGN_STUB="$TEST_TEMP/1-foreign.stub"; reset_plan
run_tui $'1\n2\n1\n' "$FOREIGN_OUT" "$FOREIGN_STUB" HARNESS_SOURCE_ROOT="$FOREIGN_HARNESS"
grep -Fqx 'MCP_AUTH:loaded' "$FOREIGN_STUB" || fail 'TUI Claude used ambient foreign profile credentials' "$FOREIGN_OUT"
grep -Fqx 'CMUX_WORKSPACE_ID:<UNSET>' "$STUB" \
  || fail 'TUI fixture must clear CMUX_WORKSPACE_ID' "$STUB"
grep -Fqx 'CMUX_TAB_ID:<UNSET>' "$STUB" \
  || fail 'TUI fixture must clear CMUX_TAB_ID' "$STUB"
grep -Fqx 'CMUX_SURFACE_ID:<UNSET>' "$STUB" \
  || fail 'TUI fixture must clear CMUX_SURFACE_ID' "$STUB"
[[ -f "$HISTORY" ]] || fail 'launch should save a history entry' "$OUT"
head -1 "$HISTORY" | grep -q 'MODE=base' || fail 'history entry should record MODE=base'
head -1 "$HISTORY" | grep -q 'RUNTIME=claude' || fail 'history entry should record RUNTIME=claude'
echo 'PASS: base mode launches and saves history entry'

OUT="$TEST_TEMP/1b.out"; STUB="$TEST_TEMP/1b.stub"
run_tui $'1\n1\n2\n1\n' "$OUT" "$STUB" HARNESS_RUN_DIR="$TEST_WORKTREE"
grep -Fqx "PWD:$TEST_WORKTREE_REAL" "$STUB" \
  || fail 'launchpad should start Claude in HARNESS_RUN_DIR' "$OUT"
echo 'PASS: launchpad starts Claude in the explicit worktree'

OUTSIDE_RUN_DIR="$TEST_TEMP/outside-worktree"
mkdir -p "$OUTSIDE_RUN_DIR"
OUT="$TEST_TEMP/1c.out"; STUB="$TEST_TEMP/1c.stub"
run_tui $'1\n2\n1\n' "$OUT" "$STUB" HARNESS_RUN_DIR="$OUTSIDE_RUN_DIR"
[[ ! -s "$STUB" ]] || fail 'launchpad must not start Claude outside HARNESS_DIR' "$STUB"
grep -Fq -- '--cwd must stay inside the registered harness' "$OUT" \
  || fail 'external HARNESS_RUN_DIR should fail clearly' "$OUT"
echo 'PASS: direct launchpad entrypoint rejects external run dirs'

SYMLINK_RUN_DIR="$TEST_HARNESS/.worktrees/escape"
ln -s "$OUTSIDE_RUN_DIR" "$SYMLINK_RUN_DIR"
OUT="$TEST_TEMP/1d.out"; STUB="$TEST_TEMP/1d.stub"
run_tui $'1\n2\n1\n' "$OUT" "$STUB" HARNESS_RUN_DIR="$SYMLINK_RUN_DIR"
[[ ! -s "$STUB" ]] || fail 'launchpad must not follow a worktree symlink outside HARNESS_DIR' "$STUB"
grep -Fq -- '--cwd must stay inside the registered harness' "$OUT" \
  || fail 'symlink escape should fail clearly' "$OUT"
echo 'PASS: direct launchpad entrypoint rejects symlink escapes'

OUT="$TEST_TEMP/1b.out"
# --- 2. mode labels derive from the shared table (B2 regression) ------------
grep -q 'Fast — haiku · low' "$OUT" || fail 'fast label must show the real model (haiku)' "$OUT"
grep -q 'Base — sonnet · high' "$OUT" || fail 'base label must show sonnet · high' "$OUT"
echo 'PASS: mode labels match the shared mode table'

# --- 3. launchpad history row replays identical launch ----------------------
OUT="$TEST_TEMP/3.out"; STUB="$TEST_TEMP/3.stub"
run_tui $'2\n' "$OUT" "$STUB"
grep -q 'EXEC:claude .*--model sonnet --effort high' "$STUB" || fail 'history row should replay same exec' "$OUT"
[[ "$(grep -c '^EXEC:' "$STUB")" -eq 1 ]] || fail 'history row should exec exactly once' "$OUT"
grep -q '↩ Claude · direct · new · sonnet · high' "$OUT" || fail 'launchpad should list the history row' "$OUT"
grep -q '1) ☁️  New — Claude Code 구성' "$OUT" || fail 'New composer entry must be option 1 (above history)' "$OUT"
grep -q '2) ↩ Claude' "$OUT" || fail 'history rows must follow the composer entries' "$OUT"
echo 'PASS: launchpad history row replays the previous launch'

# Fable is appended after Custom, preserving existing numbered menu choices.
OUT="$TEST_TEMP/fable.out"; STUB="$TEST_TEMP/fable.stub"; reset_plan
run_tui $'1\n8\n1\n' "$OUT" "$STUB"
grep -q 'EXEC:claude .*--model fable --effort high' "$STUB" || fail 'Fable preset must launch fable/high' "$OUT"
grep -q 'Fable.*fable.*high' "$OUT" || fail 'Fable label must match launch' "$OUT"
OUT="$TEST_TEMP/fable-custom.out"; STUB="$TEST_TEMP/fable-custom.stub"; reset_plan
run_tui $'1\n7\n6\n2\n1\n' "$OUT" "$STUB"
grep -q 'EXEC:claude .*--model fable --effort high' "$STUB" || fail 'Custom Fable high must launch fable/high' "$OUT"
echo 'PASS: Fable preset and Custom selection'

# Replaying custom Fable must retain the same provider boundary as the picker.
OUT="$TEST_TEMP/fable-replay.out"; STUB="$TEST_TEMP/fable-replay.stub"
run_tui $'2\n' "$OUT" "$STUB"
grep -q 'EXEC:claude .*--model fable --effort high' "$STUB" || fail 'direct Custom Fable history must replay' "$OUT"
mkdir -p "$TEST_HARNESS/config/.local"
printf 'KIRO_GATEWAY_URL="https://kiro.test"\n' > "$TEST_HARNESS/config/.local/kiro-gateway.env"
printf 'CODEX_GATEWAY_URL="https://codex.test"\n' > "$TEST_HARNESS/config/.local/codex-gateway.env"
for provider in kiro codex; do
  printf 'TS=1\tSUMMARY=Custom Fable\tRUNTIME=claude\tPROVIDER=%s\tSESSION=new\tMODE=custom\tC_MODEL=fable\tC_EFFORT=high\tMCP_SURFACE=full\n' "$provider" > "$HISTORY"
  OUT="$TEST_TEMP/fable-$provider.out"; STUB="$TEST_TEMP/fable-$provider.stub"
  run_seeded_tui_status $'2\n' "$OUT" "$STUB" TEST_KIRO_HEALTH=1 TEST_CODEX_HEALTH=1
  [[ ! -s "$STUB" ]] || fail 'Custom Fable gateway replay must reject without launching' "$OUT"
  grep -q 'fable는 Anthropic direct 전용' "$OUT" || fail 'Custom Fable gateway replay must explain rejection' "$OUT"
done
rm -rf "$TEST_HARNESS/config/.local"
echo 'PASS: Custom Fable history respects the direct provider boundary'

# --- 4. continue session flag ------------------------------------------------
OUT="$TEST_TEMP/4.out"; STUB="$TEST_TEMP/4.stub"; reset_plan
run_tui $'2\n2\n1\n' "$OUT" "$STUB"
grep -q 'EXEC:claude .*--continue --model sonnet' "$STUB" || fail 'continue should add --continue' "$OUT"
echo 'PASS: continue session flag preserved'

# --- 5. permission select + replace (no accumulation) ------------------------
OUT="$TEST_TEMP/5.out"; STUB="$TEST_TEMP/5.stub"; reset_plan
# final: 2=Permission → acceptEdits(2); final: 2=Permission → bypassPermissions(4); start
run_tui $'1\n2\n2\n2\n2\n4\n1\n' "$OUT" "$STUB"
grep -q -- '--permission-mode bypassPermissions' "$STUB" || fail 'permission re-selection should apply last choice' "$OUT"
grep -q 'acceptEdits' "$STUB" && fail 'permission re-selection must not accumulate old value' "$OUT"
echo 'PASS: permission mode replaces, never accumulates'

# --- 6. chrome toggle ---------------------------------------------------------
OUT="$TEST_TEMP/6.out"; STUB="$TEST_TEMP/6.stub"; reset_plan
run_tui $'1\n2\n3\n1\n' "$OUT" "$STUB"
grep -q -- '--chrome' "$STUB" || fail 'chrome toggle should add --chrome' "$OUT"
echo 'PASS: chrome toggle adds --chrome'

# Slack-enabled Happy cannot prove that its internal runtime keeps our policy.
write_stub happy
cat >> "$TEST_HARNESS/config/launcher.env" <<'EOF'
HARNESS_CODEX_APPS_ALLOWLIST="asdk_app_slacktest"
HARNESS_CODEX_SLACK_APPS="asdk_app_slacktest"
EOF
OUT="$TEST_TEMP/slack-happy.out"; STUB="$TEST_TEMP/slack-happy.stub"; reset_plan
run_tui $'1\n2\n5\n1\n' "$OUT" "$STUB"
grep -q 'Happy.*Slack.*native' "$OUT" || fail 'Slack-enabled Happy must report native-runtime requirement' "$OUT"
[[ -f "$STUB" ]] && grep -q '^EXEC:' "$STUB" && fail 'Slack-enabled Happy must never exec' "$OUT"
printf 'HARNESS_NAME="test harness"\nHARNESS_PREFIX="test"\n' > "$TEST_HARNESS/config/launcher.env"
reset_plan

# --- 7. happy toggle routes exec through happy --------------------------------
# final menu with happy: 1 Start / 2 Permission / 3 Chrome / 4 MCP surface / 5 Happy / 6 Back
write_stub happy
OUT="$TEST_TEMP/7.out"; STUB="$TEST_TEMP/7.stub"; reset_plan
run_tui $'1\n2\n5\n1\n' "$OUT" "$STUB"
grep -q 'EXEC:happy .*--model sonnet' "$STUB" || fail 'happy toggle should exec happy' "$OUT"
grep -q 'EXEC:claude' "$STUB" && fail 'happy launch must not also exec claude' "$OUT"
[[ "$(grep -c '^EXEC:' "$STUB")" -eq 1 ]] || fail 'happy launch should exec exactly once' "$OUT"
! grep -Fxq -- --settings "$STUB.argv" || fail 'happy must get no --settings (its flag passthrough is unverified)' "$STUB.argv"
echo 'PASS: happy wrapper launch'

# --- 8. invalid input reprompts instead of exiting (B4) -----------------------
OUT="$TEST_TEMP/8.out"; STUB="$TEST_TEMP/8.stub"; reset_plan
run_tui $'9\nx\n\n1\n2\n1\n' "$OUT" "$STUB"
grep -q 'EXEC:claude .*--model sonnet' "$STUB" || fail 'invalid input should reprompt, then proceed' "$OUT"
echo 'PASS: fallback menu reprompts on invalid input'

# --- 9. q at first menu exits cleanly with no exec ----------------------------
OUT="$TEST_TEMP/9.out"; STUB="$TEST_TEMP/9.stub"; reset_plan
run_tui $'q\n' "$OUT" "$STUB"
[[ -f "$STUB" ]] && grep -q '^EXEC:' "$STUB" && fail 'q at first menu must not exec anything' "$OUT"
echo 'PASS: q backs out without launching'

# --- 10. ultracode hint does not survive back-navigation (B6) ------------------
OUT="$TEST_TEMP/10.out"; STUB="$TEST_TEMP/10.stub"; reset_plan
# session New → mode ultracode(6) → final Back(6: Start/Perm/Chrome/MCP/Happy/Back) → mode fast(1) → start
run_tui $'1\n6\n6\n1\n1\n' "$OUT" "$STUB"
grep -q 'EXEC:claude .*--model haiku' "$STUB" || fail 'fast after back-nav should exec haiku' "$OUT"
grep -q 'ultracode는 세션 전용' "$OUT" && fail 'ultracode hint must not leak into non-ultracode launch' "$OUT"
echo 'PASS: no ultracode hint residue after back-navigation'

# --- 11. duplicate MCP server blocks launch (B1) -------------------------------
OUT="$TEST_TEMP/11.out"; STUB="$TEST_TEMP/11.stub"; reset_plan
cat > "$TEST_HARNESS/.mcp.json" <<'EOF'
{"mcpServers": {"dup": {"command": "x"}}}
EOF
cat > "$TEST_HARNESS/mcp.local.json" <<'EOF'
{"mcpServers": {"dup": {"command": "y"}}}
EOF
run_tui $'1\n2\n1\n' "$OUT" "$STUB"
grep -q "duplicate MCP server 'dup'" "$OUT" || fail 'duplicate MCP should print validation error' "$OUT"
[[ -f "$STUB" ]] && grep -q '^EXEC:' "$STUB" && fail 'duplicate MCP must block the launch' "$OUT"
rm -f "$TEST_HARNESS/.mcp.json" "$TEST_HARNESS/mcp.local.json"
echo 'PASS: duplicate MCP server blocks launch'

# --- 12. local MCP config is passed when valid ---------------------------------
OUT="$TEST_TEMP/12.out"; STUB="$TEST_TEMP/12.stub"; reset_plan
cat > "$TEST_HARNESS/mcp.local.json" <<'EOF'
{"mcpServers": {"only-local": {"command": "x"}}}
EOF
run_tui $'1\n2\n1\n' "$OUT" "$STUB"
grep -q -- "--mcp-config $TEST_HARNESS/.harness/claude/mcp-full.json" "$STUB" || fail 'valid local MCP config should be passed' "$OUT"
rm -f "$TEST_HARNESS/mcp.local.json"
echo 'PASS: valid local MCP config forwarded'

# --- 13. HARNESS_KIRO_BIN is honored by the TUI (B5) ----------------------------
mkdir -p "$TEST_TEMP/kirodir"
cat > "$TEST_TEMP/kirodir/my-kiro" <<'EOF'
#!/usr/bin/env bash
echo "EXEC:kiro $*" >> "$TEST_STUB_FILE"
echo "PWD:$PWD" >> "$TEST_STUB_FILE"
exit 0
EOF
chmod +x "$TEST_TEMP/kirodir/my-kiro"
rm -f "$TEST_BIN/claude" "$TEST_BIN/happy"   # kiro becomes the only runtime
OUT="$TEST_TEMP/13.out"; STUB="$TEST_TEMP/13.stub"; reset_plan
run_tui $'1\n2\n1\n' "$OUT" "$STUB" \
  HARNESS_KIRO_BIN="$TEST_TEMP/kirodir/my-kiro" HARNESS_RUN_DIR="$TEST_WORKTREE"
grep -q 'EXEC:kiro chat --model claude-sonnet-5 --effort high --agent harness' "$STUB" \
  || fail 'HARNESS_KIRO_BIN should surface kiro runtime in the TUI' "$OUT"
grep -Fqx "PWD:$TEST_WORKTREE_REAL" "$STUB" || fail 'TUI native Kiro should use HARNESS_RUN_DIR' "$STUB"
write_stub claude
echo 'PASS: HARNESS_KIRO_BIN resolved by TUI'

# --- 14. gateway provider flow (env exports + back-nav to provider) -------------
mkdir -p "$TEST_HARNESS/config/.local"
cat > "$TEST_HARNESS/config/.local/codex-gateway.env" <<'EOF'
CODEX_GATEWAY_URL="https://codex.test"
CODEX_GATEWAY_API_KEY="codex-test-key"
CODEX_OPUS_MODEL="gpt-test-opus"
EOF
OUT="$TEST_TEMP/14.out"; STUB="$TEST_TEMP/14.stub"; reset_plan
# provider: 1=direct 2=Codex gateway → select 2; session 1; mode base(2); start
run_tui $'2\n1\n2\n1\n' "$OUT" "$STUB" TEST_CODEX_HEALTH=1
grep -q 'BASE_URL:https://codex.test' "$STUB" || fail 'gateway launch should export ANTHROPIC_BASE_URL' "$OUT"
grep -q 'AUTH_TOKEN:codex-test-key' "$STUB" || fail 'gateway launch should export ANTHROPIC_AUTH_TOKEN' "$OUT"
grep -q 'OPUS_MODEL:gpt-test-opus' "$STUB" || fail 'codex gateway should export model overrides' "$OUT"
echo 'PASS: codex gateway provider exports env'

# --- 15. q at session returns to provider menu (B7) ------------------------------
OUT="$TEST_TEMP/15.out"; STUB="$TEST_TEMP/15.stub"; reset_plan
# provider 2(codex) → session q → provider 1(direct) → session 1 → base → start
run_tui $'2\nq\n1\n1\n2\n1\n' "$OUT" "$STUB" TEST_CODEX_HEALTH=1
[[ "$(grep -c 'Provider' "$OUT")" -ge 2 ]] || fail 'q at session should return to provider menu' "$OUT"
grep -q 'BASE_URL:$' "$STUB" || fail 'after backing out, direct provider should have no BASE_URL' "$OUT"
rm -rf "$TEST_HARNESS/config/.local"
echo 'PASS: session back-nav returns to provider menu'

# --- 16. history alone (no runtimes) → clear no-runtime error, no crash ------
OUT="$TEST_TEMP/16.out"; STUB="$TEST_TEMP/16.stub"
mkdir -p "$TEST_HARNESS/.harness"
printf 'TS=1\tSUMMARY=Claude · direct · new · sonnet · high\tRUNTIME=claude\tMODE=base\n' > "$HISTORY"
rm -f "$TEST_BIN/claude" "$TEST_BIN/happy"
run_tui $'1\n' "$OUT" "$STUB"
grep -q 'no runtime found' "$OUT" || fail 'history-only + no runtimes should print the no-runtime error' "$OUT"
grep -q 'unknown runtime' "$OUT" && fail 'history must never surface as an unknown runtime' "$OUT"
write_stub claude
reset_plan
echo 'PASS: history-only with zero runtimes fails with a clear error'

# --- 16b. legacy launcher-last migrates into history --------------------------
OUT="$TEST_TEMP/16b.out"; STUB="$TEST_TEMP/16b.stub"; reset_plan
printf 'RUNTIME=claude\nSUMMARY=Claude · direct · new · sonnet · high\nPROVIDER=direct\nSESSION=new\nMODE=base\n' \
  > "$TEST_HARNESS/.harness/launcher-last"
run_tui $'2\n' "$OUT" "$STUB"
[[ -f "$TEST_HARNESS/.harness/launcher-last" ]] && fail 'legacy plan file should be removed after migration' "$OUT"
head -1 "$HISTORY" | grep -q 'MODE=base' || fail 'legacy plan should migrate into history' "$OUT"
grep -q 'EXEC:claude .*--model sonnet --effort high' "$STUB" || fail 'migrated history row should replay' "$OUT"
echo 'PASS: legacy launcher-last migrates into history and replays'

# --- 17. MCP surface light toggle (claude) --------------------------------------
# no happy stub here → final menu: 1 Start / 2 Permission / 3 Chrome / 4 MCP surface / 5 Back
OUT="$TEST_TEMP/17.out"; STUB="$TEST_TEMP/17.stub"; reset_plan
cat > "$TEST_HARNESS/.mcp.json" <<'EOF'
{"mcpServers": {
  "ssh_rag": {"command": "bash", "args": ["${HARNESS_ROOT}/core/bin/start-ssh-mcp.sh", "rag"]},
  "tunnel_rag": {"type": "http", "url": "http://127.0.0.1:38206/mcp"},
  "local_service": {"type": "http", "url": "http://localhost:38100/mcp"},
  "normal_http": {"type": "http", "url": "https://x.test/mcp"}
}}
EOF
# An inherited opt-in must not change a harness whose launcher.env does not opt in.
run_tui $'1\n2\n4\n1\n' "$OUT" "$STUB" HARNESS_MCP_SURFACE_POLICY=single-full
LIGHT_FILE="$TEST_HARNESS/.harness/claude/mcp-light.json"
grep -q -- "--strict-mcp-config --mcp-config $LIGHT_FILE" "$STUB" \
  || fail 'light surface should pass --strict-mcp-config + generated file' "$OUT"
[[ -f "$LIGHT_FILE" ]] || fail 'light surface should generate mcp-light.json' "$OUT"
grep -q 'ssh_rag' "$LIGHT_FILE" && fail 'light surface must exclude SSH stdio wrappers' "$OUT"
grep -q 'tunnel_rag' "$LIGHT_FILE" && fail 'light surface must exclude 382xx tunnel servers' "$OUT"
grep -q 'local_service' "$LIGHT_FILE" || fail 'light surface must keep non-band loopback servers' "$OUT"
grep -q 'normal_http' "$LIGHT_FILE" || fail 'light surface must keep remote https servers' "$OUT"
head -1 "$HISTORY" | grep -q 'MCP_SURFACE=light' \
  || fail 'history entry should record MCP_SURFACE=light'
echo 'PASS: claude light MCP surface filters SSH servers'

# --- 17b. opt-in Claude has one full surface and no retired toggle ------------
printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' \
  'HARNESS_MCP_SURFACE_POLICY="single-full-compat"' > "$TEST_HARNESS/config/launcher.env"
OUT="$TEST_TEMP/17b.out"; STUB="$TEST_TEMP/17b.stub"; reset_plan
rm -f "$TEST_HARNESS/.harness/claude/mcp-light.json"
# final menu is Start / Permission / Chrome / Back under single-full.
run_tui $'1\n2\n1\n' "$OUT" "$STUB"
grep -q 'EXEC:claude .*--model sonnet --effort high' "$STUB" \
  || fail 'opt-in Claude should launch the full surface' "$OUT"
grep -q 'MCP surface:' "$OUT" && fail 'opt-in Claude final menu must not expose a surface row' "$OUT"
head -1 "$HISTORY" | grep -q 'MCP_SURFACE=full' \
  || fail 'opt-in Claude history should record canonical full surface' "$HISTORY"
grep -q 'mcp-light' "$HISTORY" && fail 'opt-in Claude history must drop the legacy suffix' "$HISTORY"
[[ ! -e "$TEST_HARNESS/.harness/claude/mcp-light.json" ]] \
  || fail 'opt-in Claude should not generate a light MCP config' "$OUT"
printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' > "$TEST_HARNESS/config/launcher.env"
echo 'PASS: opt-in Claude final menu and new history use one full surface'

# --- 18. MCP surface light toggle (kiro) -----------------------------------------
# kiro-only runtime → session 1 → mode base(2) → final: 1 Start / 2 Trust / 3 MCP / 4 Back
rm -f "$TEST_BIN/claude" "$TEST_BIN/happy"
OUT="$TEST_TEMP/18.out"; STUB="$TEST_TEMP/18.stub"; reset_plan
run_tui $'1\n2\n3\n1\n' "$OUT" "$STUB" HARNESS_KIRO_BIN="$TEST_TEMP/kirodir/my-kiro"
grep -q 'EXEC:kiro chat' "$STUB" || fail 'kiro light launch should exec kiro' "$OUT"
KIRO_MCP="$TEST_HARNESS/.harness/kiro/settings/mcp.json"
[[ -f "$KIRO_MCP" ]] || fail 'kiro prepare should generate settings/mcp.json' "$OUT"
grep -q 'ssh_rag' "$KIRO_MCP" && fail 'kiro light surface must exclude SSH stdio wrappers' "$OUT"
grep -q 'tunnel_rag' "$KIRO_MCP" && fail 'kiro light surface must exclude 382xx tunnel servers' "$OUT"
grep -q 'local_service' "$KIRO_MCP" || fail 'kiro light surface must keep non-band loopback servers' "$OUT"
grep -q 'normal_http' "$KIRO_MCP" || fail 'kiro light surface must keep remote https servers' "$OUT"
# full surface run right after must restore the SSH server (no light residue)
OUT="$TEST_TEMP/18b.out"; STUB="$TEST_TEMP/18b.stub"; reset_plan
run_tui $'1\n2\n1\n' "$OUT" "$STUB" HARNESS_KIRO_BIN="$TEST_TEMP/kirodir/my-kiro"
grep -q 'ssh_rag' "$KIRO_MCP" || fail 'full surface must restore SSH servers (light residue)' "$OUT"
rm -f "$TEST_HARNESS/.mcp.json"
write_stub claude
echo 'PASS: kiro light MCP surface filters SSH servers, full restores'

# --- 19. launchpad: dedupe by identity + older-row replay ----------------------
OUT="$TEST_TEMP/19.out"; STUB="$TEST_TEMP/19.stub"; reset_plan
run_tui $'1\n2\n1\n' "$OUT" "$STUB"                       # base → history[1]
OUT="$TEST_TEMP/19b.out"; STUB="$TEST_TEMP/19b.stub"
run_tui $'1\n1\n1\n1\n' "$OUT" "$STUB"                    # New Claude(1) → fast → history now: fast, base
[[ "$(wc -l < "$HISTORY")" -eq 2 ]] || fail 'two distinct configs should produce two history rows' "$OUT"
head -1 "$HISTORY" | grep -q 'MODE=fast' || fail 'newest config should be first in history' "$OUT"
OUT="$TEST_TEMP/19c.out"; STUB="$TEST_TEMP/19c.stub"
run_tui $'3\n' "$OUT" "$STUB"                             # older row (base, option 3: New1 fast2 base3) replays…
grep -q 'EXEC:claude .*--model sonnet --effort high' "$STUB" || fail 'older history row should replay base' "$OUT"
[[ "$(wc -l < "$HISTORY")" -eq 2 ]] || fail 'replay must dedupe, not append a third row' "$OUT"
head -1 "$HISTORY" | grep -q 'MODE=base' || fail '…and move to the top of the history' "$OUT"
reset_plan
echo 'PASS: launchpad dedupes by identity and reorders on replay'

# --- 20. light + happy are mutually exclusive -----------------------------------
# final menu with happy: 1 Start / 2 Perm / 3 Chrome / 4 MCP / 5 Happy / 6 Back
write_stub happy
cat > "$TEST_HARNESS/.mcp.json" <<'EOF'
{"mcpServers": {"ssh_rag": {"command": "bash", "args": ["${HARNESS_ROOT}/core/bin/start-ssh-mcp.sh", "rag"]}}}
EOF
OUT="$TEST_TEMP/20.out"; STUB="$TEST_TEMP/20.stub"; reset_plan
# light(4) then happy(5): happy wins, surface reverts to full → happy exec, no strict
run_tui $'1\n2\n4\n5\n1\n' "$OUT" "$STUB"
grep -q 'EXEC:happy' "$STUB" || fail 'happy after light should launch happy' "$OUT"
grep -q -- '--strict-mcp-config' "$STUB" && fail 'happy launch must never carry --strict-mcp-config' "$OUT"
head -1 "$HISTORY" | grep -q 'MCP_SURFACE=full' || fail 'happy toggle should revert surface to full in history' "$OUT"
# happy(5) then light(4): light wins, happy drops → claude exec with strict
OUT="$TEST_TEMP/20b.out"; STUB="$TEST_TEMP/20b.stub"; reset_plan
run_tui $'1\n2\n5\n4\n1\n' "$OUT" "$STUB"
grep -q 'EXEC:claude .*--strict-mcp-config' "$STUB" || fail 'light after happy should launch claude with strict config' "$OUT"
grep -q 'EXEC:happy' "$STUB" && fail 'light after happy must not exec happy' "$OUT"
# replay guard: a hand-edited history row with both set must fail, not launch full
OUT="$TEST_TEMP/20c.out"; STUB="$TEST_TEMP/20c.stub"; reset_plan
printf 'TS=1\tSUMMARY=Claude · direct · new · sonnet · high · mcp-light · happy\tRUNTIME=claude\tPROVIDER=direct\tSESSION=new\tMODE=base\tPERM=default\tMCP_SURFACE=light\tCHROME=0\tHAPPY=1\n' > "$HISTORY"
run_tui $'2\nq\n' "$OUT" "$STUB"
grep -q 'Happy 래퍼는 light MCP surface를 지원하지 않습니다' "$OUT" || fail 'replayed light+happy row must fail with a clear error' "$OUT"
[[ -f "$STUB" ]] && grep -q '^EXEC:' "$STUB" && fail 'replayed light+happy row must not exec anything' "$OUT"
rm -f "$TEST_BIN/happy" "$TEST_HARNESS/.mcp.json"; reset_plan
echo 'PASS: light and happy are mutually exclusive (toggles + replay guard)'

# --- 21. light generation fails on duplicate server names ----------------------
OUT="$TEST_TEMP/21.out"; STUB="$TEST_TEMP/21.stub"; reset_plan
cat > "$TEST_HARNESS/.mcp.json" <<'EOF'
{"mcpServers": {"dup": {"command": "x"}}}
EOF
cat > "$TEST_HARNESS/mcp.local.json" <<'EOF'
{"mcpServers": {"dup": {"command": "y"}}}
EOF
run_tui $'1\n2\n4\n1\n' "$OUT" "$STUB"
grep -q "duplicate MCP server 'dup'" "$OUT" || fail 'light surface must fail on duplicate server names' "$OUT"
[[ -f "$STUB" ]] && grep -q '^EXEC:' "$STUB" && fail 'light duplicate failure must block the launch' "$OUT"
rm -f "$TEST_HARNESS/.mcp.json" "$TEST_HARNESS/mcp.local.json"; reset_plan
echo 'PASS: light surface blocks launch on duplicate server names'

# --- 22. hidden history row (uninstalled runtime) must not shift replay mapping --
OUT="$TEST_TEMP/22.out"; STUB="$TEST_TEMP/22.stub"; reset_plan
mkdir -p "$TEST_HARNESS/.harness"
printf 'TS=2\tSUMMARY=Codex · new · base\tRUNTIME=codex\tSESSION=new\tCODEX_PROFILE=base\tCODEX_SURFACE=default\tCODEX_SAFETY=default\n' > "$HISTORY"
printf 'TS=1\tSUMMARY=Claude · direct · new · haiku · low\tRUNTIME=claude\tPROVIDER=direct\tSESSION=new\tMODE=fast\tPERM=default\tMCP_SURFACE=full\tCHROME=0\tHAPPY=0\n' >> "$HISTORY"
# codex is not installed → its row is hidden; option 2 (after the New Claude
# composer at option 1) must be the claude fast row
run_tui $'2\n' "$OUT" "$STUB"
grep -q 'Codex · new · base' "$OUT" && fail 'hidden runtime row must not be listed' "$OUT"
grep -q 'EXEC:claude .*--model haiku --effort low' "$STUB" || fail 'first visible row must replay the claude entry, not the hidden codex one' "$OUT"
reset_plan
echo 'PASS: hidden history rows do not shift replay mapping'

# --- 23. history trimmed to HISTORY_MAX (8) --------------------------------------
OUT="$TEST_TEMP/23.out"; STUB="$TEST_TEMP/23.stub"; reset_plan
mkdir -p "$TEST_HARNESS/.harness"
: > "$HISTORY"
for i in 1 2 3 4 5 6 7 8 9; do
  printf 'TS=%s\tSUMMARY=cfg%s\tRUNTIME=claude\tPROVIDER=direct\tSESSION=new\tMODE=m%s\tPERM=default\tMCP_SURFACE=full\tCHROME=0\tHAPPY=0\n' "$i" "$i" "$i" >> "$HISTORY"
done
# New Claude at 1 (composers on top), 9 hist rows below → compose a 10th config (base)
run_tui $'1\n1\n2\n1\n' "$OUT" "$STUB"
grep -q 'EXEC:claude .*--model sonnet' "$STUB" || fail 'composer must still launch with 9 history rows' "$OUT"
[[ "$(wc -l < "$HISTORY")" -eq 8 ]] || fail "10th distinct config must trim history to 8 (got $(wc -l < "$HISTORY"))"
head -1 "$HISTORY" | grep -q 'MODE=base' || fail 'newest config must be first after trimming'
grep -q 'SUMMARY=cfg7' "$HISTORY" || fail 'entry 7 must survive trimming'
grep -q 'SUMMARY=cfg8\|SUMMARY=cfg9' "$HISTORY" && fail 'oldest entries must be trimmed away'
reset_plan
echo 'PASS: history trimmed to HISTORY_MAX'

# --- 23b. opt-in mixed history migrates canonically and idempotently ----------
printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' \
  'HARNESS_MCP_SURFACE_POLICY="single-full"' > "$TEST_HARNESS/config/launcher.env"
OUT="$TEST_TEMP/23b.out"; STUB="$TEST_TEMP/23b.stub"; reset_plan
mkdir -p "$TEST_HARNESS/.harness"
cat > "$HISTORY" <<'EOF'
TS=100	SUMMARY=Claude old light	RUNTIME=claude	PROVIDER=direct	SESSION=new	MODE=base	MCP_SURFACE=light
TS=700	SUMMARY=Codex old garbage	RUNTIME=codex	SESSION=resume	CODEX_PROFILE=rich	CODEX_SURFACE=garbage	CODEX_SAFETY=never
TS=300	SUMMARY=trim me	RUNTIME=claude	PROVIDER=direct	SESSION=new	MODE=plan	MCP_SURFACE=full
TS=900	SUMMARY=Claude canonical duplicate	RUNTIME=claude	PROVIDER=direct	SESSION=new	MODE=base	MCP_SURFACE=full
TS=400	SUMMARY=Kiro old garbage	RUNTIME=kiro	SESSION=new	MODE=fast	MCP_SURFACE=garbage	KIRO_TRUST=0
TS=200	SUMMARY=Codex old default	RUNTIME=codex	SESSION=new	CODEX_PROFILE=base	CODEX_SURFACE=default	CODEX_SAFETY=default
TS=850	SUMMARY=Claude old garbage	RUNTIME=claude	PROVIDER=direct	SESSION=new	MODE=fast	PERM=acceptEdits	MCP_SURFACE=garbage	CHROME=1	HAPPY=1
TS=500	SUMMARY=KIRO_LIGHT_SENTINEL	RUNTIME=kiro	SESSION=resume	MODE=base	MCP_SURFACE=light	KIRO_TRUST=1
TS=600	SUMMARY=Codex missing surface	RUNTIME=codex	SESSION=fork	CODEX_PROFILE=fast	CODEX_SAFETY=full-auto
TS=750	SUMMARY=Claude missing surface	RUNTIME=claude	PROVIDER=direct	SESSION=continue	MODE=rich	PERM=bypassPermissions
TS=800	SUMMARY=Codex newer work duplicate	RUNTIME=codex	SESSION=new	CODEX_PROFILE=base	CODEX_SURFACE=work	CODEX_SAFETY=default
EOF
EXPECTED_HISTORY="$TEST_TEMP/23b.expected"
cat > "$EXPECTED_HISTORY" <<'EOF'
TS=900	SUMMARY=Claude · direct · new · sonnet · high	RUNTIME=claude	PROVIDER=direct	SESSION=new	MODE=base	C_MODEL=	C_EFFORT=	PERM=default	MCP_SURFACE=full	CHROME=0	HAPPY=0	CODEX_PROFILE=base	CODEX_SURFACE=full	CODEX_SAFETY=default	CODEX_CONTEXT=272k	KIRO_TRUST=0
TS=850	SUMMARY=Claude · direct · new · haiku · low · acceptEdits · chrome · happy	RUNTIME=claude	PROVIDER=direct	SESSION=new	MODE=fast	C_MODEL=	C_EFFORT=	PERM=acceptEdits	MCP_SURFACE=full	CHROME=1	HAPPY=1	CODEX_PROFILE=base	CODEX_SURFACE=full	CODEX_SAFETY=default	CODEX_CONTEXT=272k	KIRO_TRUST=0
TS=800	SUMMARY=Codex · new · base · 272K	RUNTIME=codex	PROVIDER=direct	SESSION=new	MODE=	C_MODEL=	C_EFFORT=	PERM=default	MCP_SURFACE=full	CHROME=0	HAPPY=0	CODEX_PROFILE=base	CODEX_SURFACE=full	CODEX_SAFETY=default	CODEX_CONTEXT=272k	KIRO_TRUST=0
TS=750	SUMMARY=Claude · direct · continue · opus[1m] · xhigh · bypassPermissions	RUNTIME=claude	PROVIDER=direct	SESSION=continue	MODE=rich	C_MODEL=	C_EFFORT=	PERM=bypassPermissions	MCP_SURFACE=full	CHROME=0	HAPPY=0	CODEX_PROFILE=base	CODEX_SURFACE=full	CODEX_SAFETY=default	CODEX_CONTEXT=272k	KIRO_TRUST=0
TS=700	SUMMARY=Codex · resume · rich · 272K · never	RUNTIME=codex	PROVIDER=direct	SESSION=resume	MODE=	C_MODEL=	C_EFFORT=	PERM=default	MCP_SURFACE=full	CHROME=0	HAPPY=0	CODEX_PROFILE=rich	CODEX_SURFACE=full	CODEX_SAFETY=never	CODEX_CONTEXT=272k	KIRO_TRUST=0
TS=600	SUMMARY=Codex · fork · fast · 272K · full-auto	RUNTIME=codex	PROVIDER=direct	SESSION=fork	MODE=	C_MODEL=	C_EFFORT=	PERM=default	MCP_SURFACE=full	CHROME=0	HAPPY=0	CODEX_PROFILE=fast	CODEX_SURFACE=full	CODEX_SAFETY=full-auto	CODEX_CONTEXT=272k	KIRO_TRUST=0
TS=500	SUMMARY=KIRO_LIGHT_SENTINEL	RUNTIME=kiro	PROVIDER=direct	SESSION=resume	MODE=base	C_MODEL=	C_EFFORT=	PERM=default	MCP_SURFACE=light	CHROME=0	HAPPY=0	CODEX_PROFILE=base	CODEX_SURFACE=full	CODEX_SAFETY=default	CODEX_CONTEXT=272k	KIRO_TRUST=1
TS=400	SUMMARY=Kiro · new · claude-haiku-4.5 · low	RUNTIME=kiro	PROVIDER=direct	SESSION=new	MODE=fast	C_MODEL=	C_EFFORT=	PERM=default	MCP_SURFACE=full	CHROME=0	HAPPY=0	CODEX_PROFILE=base	CODEX_SURFACE=full	CODEX_SAFETY=default	CODEX_CONTEXT=272k	KIRO_TRUST=0
EOF
run_seeded_tui_status $'q\n' "$OUT" "$STUB" HARNESS_KIRO_BIN="$TEST_TEMP/kirodir/my-kiro"
[[ "$RUN_TUI_RC" -eq 0 ]] || fail "mixed history migration should reach the launchpad (rc=$RUN_TUI_RC)" "$OUT"
cmp -s "$EXPECTED_HISTORY" "$HISTORY" || {
  diff -u "$EXPECTED_HISTORY" "$HISTORY"
  fail 'mixed history did not migrate to the exact canonical rows'
}
[[ "$(wc -l < "$HISTORY" | tr -d ' ')" -eq 8 ]] || fail 'migrated history must be capped at 8 rows' "$HISTORY"
awk -F'\t' '$3 == "RUNTIME=claude" || $3 == "RUNTIME=codex"' "$HISTORY" | \
  grep -Eq 'mcp-light|work-MCP|old |missing surface|duplicate' && \
  fail 'Claude/Codex history summaries must be recomputed without legacy suffixes' "$HISTORY"
grep -Eq 'mcp-light|work-MCP|old light|old garbage|missing surface|duplicate' "$OUT" && \
  fail 'launchpad labels must use recomputed canonical summaries' "$OUT"
grep -Fq '↩ KIRO_LIGHT_SENTINEL ·' "$OUT" \
  || fail 'Kiro light launchpad label must preserve its existing summary' "$OUT"
FIRST_HISTORY_SHA="$(shasum -a 256 "$HISTORY" | awk '{print $1}')"
run_seeded_tui_status $'q\n' "$TEST_TEMP/23b-second.out" "$TEST_TEMP/23b-second.stub" \
  HARNESS_KIRO_BIN="$TEST_TEMP/kirodir/my-kiro"
SECOND_HISTORY_SHA="$(shasum -a 256 "$HISTORY" | awk '{print $1}')"
[[ "$RUN_TUI_RC" -eq 0 && "$FIRST_HISTORY_SHA" = "$SECOND_HISTORY_SHA" ]] \
  || fail 'second migration must be byte-identical' "$HISTORY"
assert_no_migration_temps
echo 'PASS: mixed history canonicalizes, dedupes newest-first, caps at 8, and is idempotent'

# --- 23bb. Codex-gateway migration uses configured suffix without env leaks --
printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' \
  'HARNESS_MCP_SURFACE_POLICY="single-full"' > "$TEST_HARNESS/config/launcher.env"
mkdir -p "$TEST_HARNESS/config/.local" "$TEST_HARNESS/.harness"
cat > "$TEST_HARNESS/config/.local/codex-gateway.env" <<'EOF'
CODEX_GATEWAY_URL="https://codex.test"
export CODEX_GATEWAY_API_KEY="HISTORY_SECRET_DO_NOT_PRINT"
CODEX_CONTEXT_SUFFIX="[history-2m]"
export HISTORY_SUMMARY_LEAK="gateway-summary-state"
[ "${CHOICE_PROVIDER:-}" = "codex" ] && printf 'GATEWAY_ENV_OUTPUT:%s\n' "$CODEX_GATEWAY_API_KEY"
EOF
printf 'TS=910\tSUMMARY=stale gateway summary\tRUNTIME=claude\tPROVIDER=codex\tSESSION=new\tMODE=base\tMCP_SURFACE=light\n' > "$HISTORY"
OUT="$TEST_TEMP/23bb.out"; STUB="$TEST_TEMP/23bb.stub"
# New Claude → direct provider → new session → base → start. This consumes the
# migrated launchpad label, then proves migration state did not reach a direct child.
run_tui $'1\n1\n1\n2\n1\n' "$OUT" "$STUB"
grep -Fq 'SUMMARY=Claude · codex · new · sonnet[history-2m] · high' "$HISTORY" \
  || fail 'Codex gateway migration must persist the configured context suffix' "$HISTORY"
grep -Fq '↩ Claude · codex · new · sonnet[history-2m] · high ·' "$OUT" \
  || fail 'launchpad must display the persisted suffixed Codex gateway model' "$OUT"
grep -Fq 'SUMMARY=Claude · direct · new · sonnet · high' "$HISTORY" \
  || fail 'gateway migration must not change a new direct-provider summary' "$HISTORY"
grep -Fqx 'HISTORY_SUMMARY_LEAK:<UNSET>' "$STUB" \
  || fail 'gateway summary loading leaked an unrelated exported variable into direct launch state' "$STUB"
if grep -Fq 'HISTORY_SECRET_DO_NOT_PRINT' "$OUT" "$HISTORY" "$STUB"; then
  fail 'gateway summary loading exposed a configured secret' "$OUT"
fi
if grep -Fq 'gateway-summary-state' "$OUT" "$HISTORY"; then
  fail 'gateway summary loading printed unrelated gateway state' "$OUT"
fi
assert_no_migration_temps
rm -rf "$TEST_HARNESS/config/.local"
echo 'PASS: Codex gateway history uses configured suffix without secret output or state leaks'

# --- 23c. malformed history fails closed before any menu ----------------------
for malformed_case in bad-ts bad-runtime bad-tsv; do
  case "$malformed_case" in
    bad-ts)
      printf 'TS=nope\tSUMMARY=bad\tRUNTIME=claude\tMODE=base\n' > "$HISTORY"
      error_fragment='invalid TS' ;;
    bad-runtime)
      printf 'TS=1\tSUMMARY=bad\tRUNTIME=unknown\tMODE=base\n' > "$HISTORY"
      error_fragment='invalid runtime' ;;
    bad-tsv)
      printf 'TS=1\tSUMMARY=bad\tRUNTIME=claude\tBROKEN_FIELD\n' > "$HISTORY"
      error_fragment='malformed TSV field' ;;
  esac
  BEFORE_SHA="$(shasum -a 256 "$HISTORY" | awk '{print $1}')"
  run_seeded_tui_status $'q\n' "$TEST_TEMP/23c-$malformed_case.out" "$TEST_TEMP/23c-$malformed_case.stub"
  AFTER_SHA="$(shasum -a 256 "$HISTORY" | awk '{print $1}')"
  [[ "$RUN_TUI_RC" -ne 0 ]] || fail "$malformed_case history must return nonzero" "$TEST_TEMP/23c-$malformed_case.out"
  [[ "$BEFORE_SHA" = "$AFTER_SHA" ]] || fail "$malformed_case history must preserve original bytes" "$HISTORY"
  grep -q 'New — Claude Code 구성' "$TEST_TEMP/23c-$malformed_case.out" && \
    fail "$malformed_case history must fail before the launchpad menu" "$TEST_TEMP/23c-$malformed_case.out"
  grep -Fq "history migration failed: $error_fragment" "$TEST_TEMP/23c-$malformed_case.out" \
    || fail "$malformed_case history must report a clear cause" "$TEST_TEMP/23c-$malformed_case.out"
  assert_no_migration_temps
done
echo 'PASS: malformed timestamp, runtime, and TSV fail closed without changing history'

# --- 23d. atomic replace failure preserves history and stops before menu ------
FAIL_MV_BIN="$TEST_TEMP/fail-mv-bin"
mkdir -p "$FAIL_MV_BIN"
cat > "$FAIL_MV_BIN/mv" <<'EOF'
#!/usr/bin/env bash
printf 'MV:%s\t%s\n' "$1" "$2" >> "$TEST_MV_LOG"
exit 73
EOF
chmod +x "$FAIL_MV_BIN/mv"
printf 'TS=1\tSUMMARY=Claude old light\tRUNTIME=claude\tPROVIDER=direct\tSESSION=new\tMODE=base\tMCP_SURFACE=light\n' > "$HISTORY"
BEFORE_SHA="$(shasum -a 256 "$HISTORY" | awk '{print $1}')"
MV_LOG="$TEST_TEMP/23d.mv.log"
TEST_BASE_PATH="$FAIL_MV_BIN:$TEST_BIN:/usr/bin:/bin:/usr/sbin:/sbin" \
  run_seeded_tui_status $'q\n' "$TEST_TEMP/23d.out" "$TEST_TEMP/23d.stub" TEST_MV_LOG="$MV_LOG"
AFTER_SHA="$(shasum -a 256 "$HISTORY" | awk '{print $1}')"
[[ "$RUN_TUI_RC" -ne 0 ]] || fail 'atomic history replace failure must return nonzero' "$TEST_TEMP/23d.out"
[[ "$BEFORE_SHA" = "$AFTER_SHA" ]] || fail 'atomic history replace failure must preserve original bytes' "$HISTORY"
[[ -s "$MV_LOG" ]] || fail 'migration must attempt an atomic mv' "$TEST_TEMP/23d.out"
MV_SOURCE="$(sed -n 's/^MV:\([^[:space:]]*\).*/\1/p' "$MV_LOG")"
MV_TARGET="$(sed -n 's/^MV:[^[:space:]]*[[:space:]]*//p' "$MV_LOG")"
[[ "$(dirname "$MV_SOURCE")" = "$(dirname "$MV_TARGET")" ]] \
  || fail 'atomic history temp must be created in the history directory' "$MV_LOG"
[[ ! -e "$MV_SOURCE" ]] || fail 'failed migration must remove only its temp file' "$MV_LOG"
grep -Fq 'history migration failed: atomic replace failed' "$TEST_TEMP/23d.out" \
  || fail 'atomic replace failure must report a clear cause' "$TEST_TEMP/23d.out"
assert_no_migration_temps
grep -q 'New — Claude Code 구성' "$TEST_TEMP/23d.out" && \
  fail 'atomic replace failure must stop before the launchpad menu' "$TEST_TEMP/23d.out"
printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' > "$TEST_HARNESS/config/launcher.env"
reset_plan
echo 'PASS: atomic replace failure preserves original history and removes same-directory temp'

# --- 23e. invalid configured policy is rejected by the common helper ----------
printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' \
  'HARNESS_MCP_SURFACE_POLICY="invalid-policy"' > "$TEST_HARNESS/config/launcher.env"
run_seeded_tui_status $'q\n' "$TEST_TEMP/23e.out" "$TEST_TEMP/23e.stub" HARNESS_MCP_SURFACE_POLICY=single-full
[[ "$RUN_TUI_RC" -eq 2 ]] || fail "invalid configured policy must exit 2 (got $RUN_TUI_RC)" "$TEST_TEMP/23e.out"
grep -q "invalid HARNESS_MCP_SURFACE_POLICY 'invalid-policy'" "$TEST_TEMP/23e.out" \
  || fail 'invalid configured policy should report the common-helper error' "$TEST_TEMP/23e.out"
grep -q 'New — Claude Code 구성' "$TEST_TEMP/23e.out" && fail 'invalid policy must fail before menu' "$TEST_TEMP/23e.out"
printf '%s\n' 'HARNESS_NAME="test harness"' 'HARNESS_PREFIX="test"' > "$TEST_HARNESS/config/launcher.env"
echo 'PASS: launcher clears ambient policy and validates configured policy'

# --- 24. legacy plan is deleted, not merged, when history already exists ---------
OUT="$TEST_TEMP/24.out"; STUB="$TEST_TEMP/24.stub"; reset_plan
printf 'TS=1\tSUMMARY=Claude · direct · new · sonnet · high\tRUNTIME=claude\tPROVIDER=direct\tSESSION=new\tMODE=base\tPERM=default\tMCP_SURFACE=full\tCHROME=0\tHAPPY=0\n' > "$HISTORY"
printf 'RUNTIME=claude\nSUMMARY=LEGACYMARK\nPROVIDER=direct\nSESSION=new\nMODE=rich\n' > "$TEST_HARNESS/.harness/launcher-last"
run_tui $'2\n' "$OUT" "$STUB"
[[ -f "$TEST_HARNESS/.harness/launcher-last" ]] && fail 'legacy plan must be deleted even when history exists' "$OUT"
grep -q 'LEGACYMARK' "$HISTORY" && fail 'legacy plan must NOT be merged into an existing history' "$OUT"
grep -q 'EXEC:claude .*--model sonnet' "$STUB" || fail 'existing history must stay replayable' "$OUT"
reset_plan
echo 'PASS: legacy plan deleted without merging into existing history'

# --- 25. user-scope MCP servers survive the light surface -------------------------
OUT="$TEST_TEMP/25.out"; STUB="$TEST_TEMP/25.stub"; reset_plan
cat > "$TEST_HARNESS/.mcp.json" <<'EOF'
{"mcpServers": {
  "ssh_rag": {"command": "bash", "args": ["${HARNESS_ROOT}/core/bin/start-ssh-mcp.sh", "rag"]},
  "shared_name": {"type": "http", "url": "https://harness.test/mcp"}
}}
EOF
cat > "$TEST_HOME/.claude.json" <<'EOF'
{"mcpServers": {
  "user_tool": {"command": "/usr/local/bin/user-tool", "args": ["mcp"]},
  "user_tunnel": {"type": "http", "url": "http://127.0.0.1:38250"},
  "shared_name": {"type": "http", "url": "https://user.test/mcp"}
}, "projects": {}}
EOF
run_tui $'1\n2\n4\n1\n' "$OUT" "$STUB"
grep -q 'user_tool' "$LIGHT_FILE" || fail 'light surface must carry non-SSH user-scope servers' "$OUT"
grep -q 'user_tunnel' "$LIGHT_FILE" && fail 'SSH-tunnel-band user-scope servers must be dropped (path-less URL too)' "$OUT"
grep -q 'harness.test' "$LIGHT_FILE" || fail 'harness scope must win user-scope name collisions' "$OUT"
grep -q 'user.test' "$LIGHT_FILE" && fail 'user-scope duplicate of a harness server must not override it' "$OUT"
rm -f "$TEST_HARNESS/.mcp.json" "$TEST_HOME/.claude.json"; reset_plan
echo 'PASS: user-scope servers merge into the light surface (harness wins collisions)'

# --- 26. Kiro granular custom model/effort flow ----------------------------------
# claude + kiro both installed here → runtime 2 (kiro) → session 1 → mode 5
# (Custom) → model 2 → effort 4 → start 1. The catalog is pinned so the menu
# order is deterministic offline.
OUT="$TEST_TEMP/26.out"; STUB="$TEST_TEMP/26.stub"; reset_plan
run_tui $'2\n1\n5\n2\n4\n1\n' "$OUT" "$STUB" \
  HARNESS_KIRO_BIN="$TEST_TEMP/kirodir/my-kiro" \
  HARNESS_KIRO_MODEL_CATALOG="claude-opus-5 claude-sonnet-5 claude-haiku-4.5"
grep -q 'EXEC:kiro chat --model claude-sonnet-5 --effort xhigh --agent harness' "$STUB" \
  || fail 'custom flow should exec the picked model/effort pair' "$OUT"
head -1 "$HISTORY" | grep -q 'MODE=custom' || fail 'custom launch should record MODE=custom' "$OUT"
head -1 "$HISTORY" | grep -q 'C_MODEL=claude-sonnet-5' || fail 'custom launch should record C_MODEL' "$OUT"
head -1 "$HISTORY" | grep -q 'C_EFFORT=xhigh' || fail 'custom launch should record C_EFFORT' "$OUT"
grep -q 'Recommended' "$OUT" || fail 'custom menus should mark recommended entries' "$OUT"
echo 'PASS: Kiro custom flow selects an arbitrary model/effort pair'

# Replay of that custom row must reproduce the same pair, not fall back to base.
# The saved row is the newest history entry → menu row 3 (after the two
# composer entries for claude/kiro).
OUT="$TEST_TEMP/26b.out"; STUB="$TEST_TEMP/26b.stub"
run_seeded_tui_status $'3\n' "$OUT" "$STUB" \
  HARNESS_KIRO_BIN="$TEST_TEMP/kirodir/my-kiro" >/dev/null 2>&1 || true
grep -q 'EXEC:kiro chat --model claude-sonnet-5 --effort xhigh --agent harness' "$STUB" \
  || fail 'replaying a custom row should keep its model/effort pair' "$OUT"
echo 'PASS: custom Kiro row replays with the same model/effort'
reset_plan

# --- 27. Kiro preset labels carry operational intent (Codex-style) ---------------
OUT="$TEST_TEMP/27.out"; STUB="$TEST_TEMP/27.stub"; reset_plan
run_tui $'2\n1\n\n\n' "$OUT" "$STUB" HARNESS_KIRO_BIN="$TEST_TEMP/kirodir/my-kiro"
grep -q 'Fast — .* — claude-haiku-4.5 · low' "$OUT" \
  || fail 'Kiro mode menu should show intent between icon and model' "$OUT"
grep -q 'Rich — .* — claude-opus-5 · max' "$OUT" \
  || fail 'Kiro rich label should show the bumped opus model' "$OUT"
grep -q 'Custom — model/effort' "$OUT" || fail 'Kiro mode menu should offer Custom' "$OUT"
reset_plan
echo 'PASS: Kiro mode labels expose intent + model · effort and a Custom entry'

# --- 27b. an unresolvable Kiro row fails instead of launching something else ----
# Regression guard: the launcher used to rewrite an unknown mode to `base`, so a
# corrupt custom row silently launched a different model than it recorded.
OUT="$TEST_TEMP/27b.out"; STUB="$TEST_TEMP/27b.stub"; reset_plan
mkdir -p "$(dirname "$HISTORY")"
cat > "$HISTORY" <<'EOF'
TS=900	SUMMARY=Kiro · new · claude-sonnet-5 · xhigh · custom	RUNTIME=kiro	PROVIDER=direct	SESSION=new	MODE=custom	C_MODEL=	C_EFFORT=banana	PERM=default	MCP_SURFACE=full	CHROME=0	HAPPY=0	CODEX_PROFILE=base	CODEX_SURFACE=full	CODEX_SAFETY=default	CODEX_CONTEXT=272k	KIRO_TRUST=0
EOF
run_seeded_tui_status $'3\n' "$OUT" "$STUB" \
  HARNESS_KIRO_BIN="$TEST_TEMP/kirodir/my-kiro" >/dev/null 2>&1 || true
if [[ -s "$STUB" ]]; then
  fail 'a corrupt custom row must not launch any model' "$STUB"
fi
grep -qE 'custom 모드에는 model이 필요합니다|잘못된 effort' "$OUT" \
  || fail 'an unresolvable Kiro row should explain why it did not launch' "$OUT"
reset_plan
echo 'PASS: unresolvable Kiro row fails loudly instead of substituting a preset'

# --- launch record: the picker passes the same launch-record hook as the shortcut
# path, inside one --settings; grant, root and isolation are hook ARGUMENTS.
LR_PY="$(bash -c ". '$LAUNCHER_DIR/bin/harness-common.sh'; harness_python3_resolve")"
LR_STATE="$TEST_TEMP/lr-state"
LR_ROOT="$(cd -P "$TEST_HARNESS" && pwd -P)"
cat > "$TEST_TEMP/ps-clean" <<'PS'
1000 999 /bin/sh
998 997 claude
999 998 /bin/sh
997 996 -zsh
996 995 zsh
995 1 herdr
1 0 launchd
PS
lr_settings() { grep -Fx -A1 -- --settings "$1.argv" | sed -n 2p; }
lr_hook() { "$LR_PY" -c 'import json,sys; print(json.loads(sys.argv[1])["hooks"]["SessionStart"][0]["hooks"][0]["command"])' "$(lr_settings "$1")"; }
lr_run_hook() { # <command> <session-id>
  printf '{"hook_event_name":"SessionStart","source":"startup","session_id":"%s"}' "$2" \
    | env HARNESS_SESSION_STATE_HOME="$LR_STATE" /bin/sh -c "$1 --pstable $TEST_TEMP/ps-clean --start-pid 1000" > "$TEST_TEMP/lr-hook.out"
  [[ ! -s "$TEST_TEMP/lr-hook.out" ]] || fail 'launch-record hook printed output'
}
lr_record() { HARNESS_SESSION_STATE_HOME="$LR_STATE" "$LR_PY" "$LAUNCHER_DIR/bin/harness-launch-record" read claude "$1"; }
# opus (opus[1m]) + bypassPermissions: final 2=Permission, 4=bypassPermissions, 1=Start
OUT="$TEST_TEMP/lr1.out"; STUB="$TEST_TEMP/lr1.stub"; reset_plan
run_tui $'1\n4\n2\n4\n1\n' "$OUT" "$STUB"
grep -q -- '--permission-mode bypassPermissions' "$STUB" || fail 'lr1 expected a bypassPermissions launch' "$OUT"
[[ "$(grep -cFx -- --settings "$STUB.argv")" == 1 ]] || fail 'lr1 the picker must pass exactly one --settings' "$STUB.argv"
HOOK="$(lr_hook "$STUB")"
[[ "$HOOK" == *harness-launch-record*" claude "* && "$HOOK" == *"--permission bypassPermissions"* \
   && "$HOOK" == *"--source-root $LR_ROOT"* && "$HOOK" == *"--isolated 0"* && "$HOOK" == *"--context 1m"* ]] \
  || fail "lr1 hook command lacks its launch arguments: $HOOK" "$STUB.argv"
LR_ID=0c5d1f3e-0000-4000-8000-0000000000b1
lr_run_hook "$HOOK" $LR_ID
[[ "$(lr_record $LR_ID)" == $'permission=bypassPermissions\nsource_root='"$LR_ROOT"$'\nisolated=0\ncontext=1m' ]] \
  || fail "lr1 hook did not write the picker's grant: $(lr_record $LR_ID)"
grep -q '<launch settings>' "$OUT" || fail 'lr1 the banner must name the launcher settings' "$OUT"
grep -q 'harness-launch-record' "$OUT" && fail 'lr1 the banner must not print the settings JSON' "$OUT"
# base + default permission: a record without a grant (the user chose default)
OUT="$TEST_TEMP/lr2.out"; STUB="$TEST_TEMP/lr2.stub"; reset_plan
run_tui $'1\n2\n1\n' "$OUT" "$STUB"
HOOK="$(lr_hook "$STUB")"
[[ "$HOOK" == *harness-launch-record* && "$HOOK" != *--permission* && "$HOOK" != *--context* ]] \
  || fail "lr2 a default launch must pass the hook with no grant: $HOOK" "$STUB.argv"
grep -q 'EXEC:claude --model sonnet --effort high --exclude-dynamic-system-prompt-sections' "$STUB" \
  || fail 'lr2 launcher flag order changed' "$STUB"
LR_ID=0c5d1f3e-0000-4000-8000-0000000000b2
lr_run_hook "$HOOK" $LR_ID
[[ "$(lr_record $LR_ID)" == $'source_root='"$LR_ROOT"$'\nisolated=0' ]] || fail "lr2 unexpected record: $(lr_record $LR_ID)"
reset_plan
echo 'PASS: launch record — the picker records the Claude grant, root, isolation and 1M context'

echo 'ALL launcher TUI tests passed'

reset_plan
printf '{"enabled":true}\n' > "$TEST_HARNESS/config/ssot-session-hooks.json"
OUT="$TEST_TEMP/session-optin.out"; STUB="$TEST_TEMP/session-optin.stub"
run_tui $'1\n2\n1\n' "$OUT" "$STUB" HARNESS_SOURCE_ROOT="$TEST_TEMP/stale-canonical"
python3 - "$STUB.argv" "$TEST_TEMP/stale-canonical" <<'PYCODE'
import json,sys
from pathlib import Path
args=Path(sys.argv[1]).read_text().splitlines()
h=json.loads(args[args.index("--settings")+1])["hooks"]
assert "Stop" in h and "UserPromptSubmit" in h, "TUI session opt-in lost"
assert sys.argv[2] in h["SessionStart"][0]["hooks"][0]["command"], "TUI source binding changed"
PYCODE
echo 'PASS: TUI session opt-in preserves canonical launch binding'
