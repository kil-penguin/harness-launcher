#!/usr/bin/env zsh
# test-codex-home-prepare.sh — verify codex-home-prepare.sh side effects.

set -e

unset HARNESS_CODEX_CONTEXT HARNESS_CODEX_APPS_ALLOWLIST HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCHER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PREPARE="$LAUNCHER_DIR/bin/codex-home-prepare.sh"

LEGACY_COMPILER_FIXTURE="$LAUNCHER_DIR/test/fixtures/legacy-harness-compiler.py"
TEST_GROUP="${HARNESS_HOME_PREPARE_TEST_GROUP:-}"

if [[ -z "$TEST_GROUP" ]]; then
  PYTHON_BIN="${HARNESS_PYTHON_BIN:-$(command -v python3)}"
  "$PYTHON_BIN" "$LAUNCHER_DIR/test/test_home_prepare_runner.py" -v
  exec "$PYTHON_BIN" "$LAUNCHER_DIR/test/home_prepare_test_runner.py" "$@"
fi

case "$TEST_GROUP" in
  config-skills|hooks|generated) ;;
  *) echo "FAIL: unknown home prepare test group: $TEST_GROUP" >&2; exit 2 ;;
esac

cleanup() {
  [[ -n "${TEST_TEMP:-}" && -d "$TEST_TEMP" ]] && rm -rf "$TEST_TEMP"
}
trap cleanup EXIT

[[ -x "$PREPARE" ]] || { echo "FAIL: $PREPARE missing or not executable"; exit 1; }
[[ -f "$LEGACY_COMPILER_FIXTURE" ]] || { echo "FAIL: $LEGACY_COMPILER_FIXTURE missing"; exit 1; }

TEST_TEMP="$(mktemp -d)"
TEST_HARNESS="$TEST_TEMP/fake-harness"
mkdir -p "$TEST_HARNESS"
mkdir -p "$TEST_HARNESS/core/bin"
touch "$TEST_HARNESS/core/bin/start-google-workspace-mcp.sh"

CODEX_BIN_STUB="$TEST_TEMP/codex"
cat > "$CODEX_BIN_STUB" <<'EOF'
#!/usr/bin/env bash
echo "codex-cli 0.153.2"
EOF
chmod +x "$CODEX_BIN_STUB"
export HARNESS_CODEX_BIN="$CODEX_BIN_STUB"

if [[ "$TEST_GROUP" == "config-skills" ]]; then
cat > "$TEST_HARNESS/CLAUDE.md" <<'EOF'
# Fake harness rules

<!-- BEGIN HARNESS-CODEX-RESPONSE-LANGUAGE -->
stale managed response language block
<!-- END HARNESS-CODEX-RESPONSE-LANGUAGE -->
EOF

cat > "$TEST_HARNESS/.mcp.json" <<'EOF'
{
  "mcpServers": {
    "atlassian": { "type": "http", "url": "http://localhost:38100/mcp" },
    "context7": { "command": "npx", "args": ["-y", "@upstash/context7-mcp"] },
    "glider": {
      "type": "stdio",
      "command": "/usr/local/bin/glider",
      "args": ["mcp", "serve"]
    },
    "google_workspace": {
      "command": "bash",
      "args": ["${HARNESS_ROOT}/core/bin/start-google-workspace-mcp.sh"],
      "env": { "FOO": "bar" }
    }
  }
}
EOF

"$PREPARE" "$TEST_HARNESS"

CODEX_HOME="$TEST_HARNESS/.harness/codex"

[[ -d "$CODEX_HOME" ]] || { echo "FAIL: CODEX_HOME dir not created"; exit 1; }
echo "PASS: CODEX_HOME directory created"

# AGENTS.md: with no rules, materializes CLAUDE.md plus a Codex-only response
# language supplement. This keeps Claude surfaces untouched while Codex's
# global-scope discovery sees the preference.
[[ -f "$CODEX_HOME/AGENTS.md" ]] || { echo "FAIL: AGENTS.md should be materialized when no rules"; exit 1; }
[[ ! -L "$CODEX_HOME/AGENTS.md" ]] || { echo "FAIL: AGENTS.md should not be symlink when Codex supplement is appended"; exit 1; }
grep -q "# Fake harness rules" "$CODEX_HOME/AGENTS.md" || { echo "FAIL: AGENTS.md missing CLAUDE.md fallback content"; exit 1; }
grep -q "Default to Korean for conversational responses" "$CODEX_HOME/AGENTS.md" || {
  echo "FAIL: AGENTS.md missing Codex response language supplement"; exit 1;
}
lang_block_count=$(grep -c "BEGIN HARNESS-CODEX-RESPONSE-LANGUAGE" "$CODEX_HOME/AGENTS.md")
[[ "$lang_block_count" == "1" ]] || { echo "FAIL: AGENTS.md should contain exactly one managed language block, got $lang_block_count"; exit 1; }
if grep -q "stale managed response language block" "$CODEX_HOME/AGENTS.md"; then
  echo "FAIL: AGENTS.md should replace stale managed language block"; exit 1;
fi
echo "PASS: AGENTS.md materialized with Codex response language supplement"

# config.toml structure
config="$CODEX_HOME/config.toml"
[[ -f "$config" ]] || { echo "FAIL: config.toml missing"; exit 1; }
grep -q '^model = "gpt-5.6-terra"' "$config" || { echo "FAIL: top-level model should use Terra"; exit 1; }
# Auto leaves both values to native Codex configuration.
if grep -qE '^model_(context_window|auto_compact_token_limit) =' "$config"; then
  echo "FAIL: auto must omit both context keys"; exit 1
fi
echo "PASS: default context follows Codex settings"

TEST_HARNESS_1M="$TEST_TEMP/fake-harness-1m"
mkdir -p "$TEST_HARNESS_1M"
echo "# rules" > "$TEST_HARNESS_1M/CLAUDE.md"
HARNESS_CODEX_CONTEXT=1m "$PREPARE" "$TEST_HARNESS_1M"
config_1m="$TEST_HARNESS_1M/.harness/codex/config.toml"
grep -q '^model_context_window = 1000000$' "$config_1m" || {
  echo "FAIL: 1M selection must request model_context_window 1000000"; exit 1;
}
grep -q '^model_auto_compact_token_limit = 414000$' "$config_1m" || {
  echo "FAIL: 1M auto-compact limit must remain 414000"; exit 1;
}
echo "PASS: explicit 1M context keeps the validated 414K auto-compact line"
grep -A1 '^\[tools.update_plan\]$' "$config" | grep -qx 'enabled = true' || {
  echo "FAIL: native task tracker must enable tools.update_plan"; exit 1;
}
echo "PASS: native task tracker explicitly enabled"
# Codex 0.134.0+ rejects `--profile` when config.toml still contains inline
# [profiles.*] tables. Profiles must live in separate <name>.config.toml files.
if grep -q '^\[profiles\.' "$config"; then
  echo "FAIL: config.toml still has legacy [profiles.*] table (Codex 0.134.0+ rejects --profile)"; exit 1;
fi
echo "PASS: config.toml has no legacy [profiles.*] tables"

# Per-profile overlay files: top-level keys (NOT nested under [profiles.<name>])
for p in fast base sol plan rich astra; do
  pf="$CODEX_HOME/$p.config.toml"
  [[ -f "$pf" ]] || { echo "FAIL: $p.config.toml missing"; exit 1; }
  if grep -q '^\[profiles' "$pf"; then
    echo "FAIL: $p.config.toml must use top-level keys, not a [profiles.$p] table"; exit 1;
  fi
done
grep -q '^model = "gpt-6-astra"' "$CODEX_HOME/astra.config.toml" || { echo "FAIL: astra model"; exit 1; }
[[ ! -e "$CODEX_HOME/luna6.config.toml" && ! -e "$CODEX_HOME/sol6.config.toml" ]] || { echo "FAIL: obsolete GPT-6 profiles remain"; exit 1; }
grep -q '^model_reasoning_effort = "medium"' "$CODEX_HOME/astra.config.toml" || { echo "FAIL: astra effort"; exit 1; }
grep -q '^model = "gpt-6-luna"' "$CODEX_HOME/fast.config.toml" || { echo "FAIL: fast model should use GPT-6 Luna"; exit 1; }
grep -q '^model = "gpt-5.6-terra"' "$CODEX_HOME/base.config.toml" || { echo "FAIL: base model should use Terra"; exit 1; }
grep -q '^model = "gpt-6.1-sol"' "$CODEX_HOME/sol.config.toml" || { echo "FAIL: sol model should use GPT-6.1 Sol"; exit 1; }
grep -q '^model = "gpt-6.1-sol"' "$CODEX_HOME/plan.config.toml" || { echo "FAIL: plan model should use GPT-6.1 Sol"; exit 1; }
grep -q '^model = "gpt-6.1-sol"' "$CODEX_HOME/rich.config.toml" || { echo "FAIL: rich model should use GPT-6.1 Sol"; exit 1; }
grep -q '^model_reasoning_effort = "low"' "$CODEX_HOME/fast.config.toml" || { echo "FAIL: fast effort wrong"; exit 1; }
grep -q '^model_reasoning_effort = "medium"' "$CODEX_HOME/base.config.toml" || { echo "FAIL: base effort wrong"; exit 1; }
grep -q '^model_reasoning_effort = "high"' "$CODEX_HOME/sol.config.toml" || { echo "FAIL: sol effort wrong"; exit 1; }
grep -q '^model_reasoning_effort = "high"' "$CODEX_HOME/plan.config.toml" || { echo "FAIL: plan effort wrong"; exit 1; }
grep -q '^sandbox_mode = "read-only"' "$CODEX_HOME/plan.config.toml" || { echo "FAIL: plan sandbox_mode missing"; exit 1; }
grep -q '^approval_policy = "on-request"' "$CODEX_HOME/plan.config.toml" || { echo "FAIL: plan approval_policy missing"; exit 1; }
grep -q '^model_reasoning_effort = "xhigh"' "$CODEX_HOME/rich.config.toml" || { echo "FAIL: rich effort wrong"; exit 1; }
echo "PASS: per-profile *.config.toml files (top-level keys; fast/base/sol/plan/rich)"

# mcp_servers schema (HTTP, stdio, env)
grep -q '^\[mcp_servers.atlassian\]' "$config" || { echo "FAIL: atlassian section missing"; exit 1; }
grep -q '^url = "http://localhost:38100/mcp"' "$config" || { echo "FAIL: atlassian url missing"; exit 1; }
grep -q '^\[mcp_servers.context7\]' "$config" || { echo "FAIL: context7 section missing"; exit 1; }
grep -q '^command = "npx"' "$config" || { echo "FAIL: context7 command missing"; exit 1; }
grep -q '^args = \["-y", "@upstash/context7-mcp"\]' "$config" || { echo "FAIL: context7 args missing"; exit 1; }
grep -q '^\[mcp_servers.glider\]' "$config" || { echo "FAIL: glider section missing"; exit 1; }
grep -q '^command = "/usr/local/bin/glider"' "$config" || { echo "FAIL: glider command missing"; exit 1; }
grep -q '^args = \["mcp", "serve"\]' "$config" || { echo "FAIL: glider args missing"; exit 1; }
grep -q '^\[mcp_servers.google_workspace\]' "$config" || { echo "FAIL: google_workspace section missing"; exit 1; }
grep -q '^\[mcp_servers.google_workspace.env\]' "$config" || { echo "FAIL: env subtable missing"; exit 1; }
grep -q '^FOO = "bar"' "$config" || { echo "FAIL: env value missing"; exit 1; }
echo "PASS: mcp_servers TOML schema (HTTP, stdio, env)"
echo "PASS: glider stdio MCP converted"

# HTTP Authorization Bearer headers → bearer_token_env_var. Codex streamable_http
# only accepts bearer_token_env_var (the env-var *name*), never a literal
# bearer_token. `${VAR}` and `${VAR:-default}` both map to the name; any other
# Authorization shape (or no headers) emits nothing — keeping the change additive.
TEST_HARNESS_AUTH="$TEST_TEMP/fake-harness-auth"
mkdir -p "$TEST_HARNESS_AUTH"
echo "# rules" > "$TEST_HARNESS_AUTH/CLAUDE.md"
cat > "$TEST_HARNESS_AUTH/.mcp.json" <<'EOF'
{
  "mcpServers": {
    "bearer_default": {
      "type": "http",
      "url": "https://example.test/api/mcp",
      "headers": { "Authorization": "Bearer ${HYPERDX_API_KEY:-}" }
    },
    "bearer_plain": {
      "type": "http",
      "url": "https://other.test/mcp",
      "headers": { "Authorization": "Bearer ${OTHER_TOKEN}" }
    },
    "bearer_literal": {
      "type": "http",
      "url": "https://lit.test/mcp",
      "headers": { "Authorization": "Bearer sk-static-literal" }
    },
    "no_auth": { "type": "http", "url": "https://noauth.test/mcp" }
  }
}
EOF
"$PREPARE" "$TEST_HARNESS_AUTH"
config_auth="$TEST_HARNESS_AUTH/.harness/codex/config.toml"
[[ -f "$config_auth" ]] || { echo "FAIL: auth config.toml missing"; exit 1; }
# ${VAR:-default} form → bearer_token_env_var = "VAR"
grep -q '^bearer_token_env_var = "HYPERDX_API_KEY"' "$config_auth" || {
  echo "FAIL: \${VAR:-} Bearer header not mapped to bearer_token_env_var"; exit 1;
}
# ${VAR} form → bearer_token_env_var = "VAR"
grep -q '^bearer_token_env_var = "OTHER_TOKEN"' "$config_auth" || {
  echo "FAIL: \${VAR} Bearer header not mapped to bearer_token_env_var"; exit 1;
}
# Codex rejects a literal bearer_token for streamable_http — must NEVER emit one
if grep -qE '^bearer_token[[:space:]]*=' "$config_auth"; then
  echo "FAIL: literal bearer_token emitted (Codex rejects it for streamable_http)"; exit 1;
fi
# Non-\${VAR} Authorization (literal) and header-less servers must emit nothing
[[ "$(grep -c '^bearer_token_env_var = ' "$config_auth")" == "2" ]] || {
  echo "FAIL: expected exactly 2 bearer_token_env_var lines (literal/no-auth must not emit)"; exit 1;
}
echo "PASS: HTTP Authorization Bearer \${VAR}/\${VAR:-} → bearer_token_env_var (no literal bearer_token)"

# Local MCP overlay: .mcp.local.json and mcp.local.json are gitignored/private
# overlays that must merge into the same generated Codex config without
# allowing silent overrides of committed .mcp.json entries.
TEST_HARNESS_LOCAL="$TEST_TEMP/fake-harness-local-mcp"
mkdir -p "$TEST_HARNESS_LOCAL"
echo "# rules" > "$TEST_HARNESS_LOCAL/CLAUDE.md"
cat > "$TEST_HARNESS_LOCAL/.mcp.json" <<'EOF'
{
  "mcpServers": {
    "committed": { "type": "http", "url": "https://committed.test/mcp" }
  }
}
EOF
cat > "$TEST_HARNESS_LOCAL/.mcp.local.json" <<'EOF'
{
  "mcpServers": {
    "local_private": { "command": "local-private", "args": ["mcp"] }
  }
}
EOF
cat > "$TEST_HARNESS_LOCAL/mcp.local.json" <<'EOF'
{
  "mcpServers": {
    "legacy_local": { "type": "http", "url": "https://legacy-local.test/mcp" }
  }
}
EOF
"$PREPARE" "$TEST_HARNESS_LOCAL"
config_local="$TEST_HARNESS_LOCAL/.harness/codex/config.toml"
grep -q '^\[mcp_servers.committed\]' "$config_local" || { echo "FAIL: committed .mcp.json server missing from merged config"; exit 1; }
grep -q '^\[mcp_servers.local_private\]' "$config_local" || { echo "FAIL: .mcp.local.json server missing from merged config"; exit 1; }
grep -q '^\[mcp_servers.legacy_local\]' "$config_local" || { echo "FAIL: mcp.local.json server missing from merged config"; exit 1; }
echo "PASS: Codex MCP config merges .mcp.json + .mcp.local.json + mcp.local.json"

TEST_HARNESS_DUP="$TEST_TEMP/fake-harness-duplicate-mcp"
mkdir -p "$TEST_HARNESS_DUP"
echo "# rules" > "$TEST_HARNESS_DUP/CLAUDE.md"
cat > "$TEST_HARNESS_DUP/.mcp.json" <<'EOF'
{ "mcpServers": { "dupe": { "type": "http", "url": "https://committed.test/mcp" } } }
EOF
cat > "$TEST_HARNESS_DUP/.mcp.local.json" <<'EOF'
{ "mcpServers": { "dupe": { "type": "http", "url": "https://local.test/mcp" } } }
EOF
if "$PREPARE" "$TEST_HARNESS_DUP" > "$TEST_TEMP/dup.out" 2> "$TEST_TEMP/dup.err"; then
  echo "FAIL: duplicate MCP server name across committed/local configs should fail"; exit 1;
fi
grep -q "duplicate MCP server 'dupe'" "$TEST_TEMP/dup.err" || {
  echo "FAIL: duplicate MCP error message missing"; cat "$TEST_TEMP/dup.err"; exit 1;
}
echo "PASS: duplicate MCP server names across committed/local configs fail fast"

# Idempotent: re-run leaves config.toml mtime unchanged when input unchanged
mtime_before=$(stat -f %m "$config" 2>/dev/null || stat -c %Y "$config")
sleep 1.1
"$PREPARE" "$TEST_HARNESS"
mtime_after=$(stat -f %m "$config" 2>/dev/null || stat -c %Y "$config")
[[ "$mtime_before" == "$mtime_after" ]] || {
  echo "FAIL: config.toml mtime changed on no-op re-run ($mtime_before -> $mtime_after)"; exit 1;
}
echo "PASS: idempotent (mtime unchanged on no-op re-run)"

# Regenerates on .mcp.json change
cat > "$TEST_HARNESS/.mcp.json" <<'EOF'
{ "mcpServers": { "newone": { "type": "http", "url": "http://example.com/mcp" } } }
EOF
"$PREPARE" "$TEST_HARNESS"
grep -q '^\[mcp_servers.newone\]' "$config" || { echo "FAIL: regen didn't add newone"; exit 1; }
if grep -q '^\[mcp_servers.atlassian\]' "$config"; then
  echo "FAIL: regen didn't drop atlassian"; exit 1;
fi
echo "PASS: regenerates on .mcp.json change (adds new, drops old)"

# auth.json + skills symlinks (best-effort: only if global source exists)
if [[ -f "$HOME/.codex/auth.json" ]]; then
  [[ -L "$CODEX_HOME/auth.json" ]] || { echo "FAIL: auth.json symlink missing"; exit 1; }
  echo "PASS: auth.json symlink present"
else
  echo "SKIP: auth.json — ~/.codex/auth.json not present"
fi
# Skills merge: $CODEX_HOME/skills must be a real directory containing
# per-skill symlinks from both global ~/.codex/skills/* and per-harness
# .claude/skills/*. Use a mocked HOME so the test is self-contained.
FAKE_HOME="$TEST_TEMP/fake-home"
FAKE_HARNESS_S="$TEST_TEMP/fake-harness-skills"
mkdir -p "$FAKE_HOME/.codex/skills/global-skill" \
         "$FAKE_HOME/.agents/skills/agents-global-skill" \
         "$FAKE_HOME/.codex/skills/.system/system-skill" \
         "$FAKE_HOME/.codex/superpowers/skills/brainstorming" \
         "$FAKE_HARNESS_S/.claude/skills/harness-skill" \
         "$FAKE_HARNESS_S/.codex-only/skills/codex-only-skill" \
         "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/browser/.codex-plugin" \
         "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/browser/scripts" \
         "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/browser/skills/browser" \
         "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/chrome/.codex-plugin" \
         "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/chrome/extension-host/macos/arm64" \
         "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/chrome/scripts" \
         "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/chrome/skills/chrome" \
         "$FAKE_HOME/.codex/plugins/cache/openai-bundled/chrome" \
         "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/computer-use/.codex-plugin" \
         "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/computer-use/skills/computer-use"
echo "# fake rules" > "$FAKE_HARNESS_S/CLAUDE.md"
cat > "$FAKE_HOME/.codex/skills/global-skill/SKILL.md" <<'EOF'
---
name: global-skill
description: Globally available skill
---
EOF
cat > "$FAKE_HOME/.agents/skills/agents-global-skill/SKILL.md" <<'EOF'
---
name: agents-global-skill
description: Global skill installed by skills CLI
---
EOF
cat > "$FAKE_HOME/.codex/skills/.system/system-skill/SKILL.md" <<'EOF'
---
name: system-skill
description: Codex system bundle
---
EOF
cat > "$FAKE_HARNESS_S/.claude/skills/harness-skill/SKILL.md" <<'EOF'
---
name: harness-skill
description: Per-harness Claude skill
---
EOF
cat > "$FAKE_HARNESS_S/.codex-only/skills/codex-only-skill/SKILL.md" <<'EOF'
---
name: codex-only-skill
description: Per-harness skill visible only to Codex
---
EOF
cat > "$FAKE_HOME/.codex/superpowers/skills/brainstorming/SKILL.md" <<'EOF'
---
name: brainstorming
description: Superpowers brainstorming skill
---
EOF
cat > "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/browser/.codex-plugin/plugin.json" <<'EOF'
{"name":"browser","version":"0.1.0-test","skills":"./skills/"}
EOF
cat > "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/browser/skills/browser/SKILL.md" <<'EOF'
---
name: browser
description: Browser test skill
---
EOF
cat > "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/browser/scripts/browser-client.mjs" <<'EOF'
export const fakeBrowserClient = "browser-current";
EOF
cat > "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/chrome/.codex-plugin/plugin.json" <<'EOF'
{"name":"chrome","version":"0.1.0-test","skills":"./skills/"}
EOF
cat > "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/chrome/skills/chrome/SKILL.md" <<'EOF'
---
name: chrome
description: Chrome test skill
---
EOF
cat > "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/chrome/scripts/browser-client.mjs" <<'EOF'
export const fakeBrowserClient = "chrome-current";
EOF
cat > "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/chrome/scripts/extension-id.json" <<'EOF'
{
  "extensionId": "fake-chrome-extension-id",
  "extensionHostName": "com.openai.codexextension"
}
EOF
cat > "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/chrome/extension-host/macos/arm64/Codex for Chrome" <<'EOF'
#!/bin/sh
trap 'exit 0' TERM INT
while :; do sleep 1; done
EOF
chmod +x "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/chrome/extension-host/macos/arm64/Codex for Chrome"
cp -pR "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/chrome" \
        "$FAKE_HOME/.codex/plugins/cache/openai-bundled/chrome/0.1.0-test"
cat > "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/computer-use/.codex-plugin/plugin.json" <<'EOF'
{"name":"computer-use","version":"1.0.0-test","skills":"./skills/"}
EOF
cat > "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/computer-use/skills/computer-use/SKILL.md" <<'EOF'
---
name: computer-use
description: Computer Use test skill
---
EOF
cat > "$FAKE_HARNESS_S/.mcp.json" <<'EOF'
{
  "mcpServers": {
    "node_repl": {
      "command": "/fake/node_repl",
      "env": {
        "NODE_REPL_BROWSER_CLIENT_MARKETPLACE_NAME": "openai-bundled",
        "NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      }
    }
  }
}
EOF
mkdir -p "$FAKE_HARNESS_S/.harness/codex/plugins/cache/openai-bundled/chrome/0.0.1-stale"
mkdir -p "$FAKE_HARNESS_S/.harness/codex/node_repl/sessions"
mkdir -p "$FAKE_HARNESS_S/.harness/codex/browser/sessions"
mkdir -p "$FAKE_HOME/.codex/plugins/cache/openai-bundled/chrome/0.0.1-stale"
ln -s 0.0.1-stale "$FAKE_HARNESS_S/.harness/codex/plugins/cache/openai-bundled/chrome/latest"
ln -s 0.0.1-stale "$FAKE_HOME/.codex/plugins/cache/openai-bundled/chrome/latest"
"$FAKE_HOME/.codex/plugins/cache/openai-bundled/chrome/0.1.0-test/extension-host/macos/arm64/Codex for Chrome" chrome-extension://fake-chrome-extension-id/ &
FAKE_EXTENSION_HOST_PID=$!
HOME="$FAKE_HOME" HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled" "$PREPARE" "$FAKE_HARNESS_S"
SKILLS_OUT="$FAKE_HARNESS_S/.harness/codex/skills"
FAKE_CONFIG="$FAKE_HARNESS_S/.harness/codex/config.toml"

[[ -d "$SKILLS_OUT" && ! -L "$SKILLS_OUT" ]] || {
  echo "FAIL: skills should be a real directory, not a single symlink"; exit 1;
}
echo "PASS: skills is a real directory"

[[ -L "$SKILLS_OUT/global-skill" ]] || { echo "FAIL: global-skill symlink missing"; exit 1; }
[[ -f "$SKILLS_OUT/global-skill/SKILL.md" ]] || { echo "FAIL: global-skill resolves to file"; exit 1; }
echo "PASS: ~/.codex/skills/global-skill linked"

[[ -L "$SKILLS_OUT/agents-global-skill" ]] || { echo "FAIL: agents-global-skill symlink missing"; exit 1; }
[[ -f "$SKILLS_OUT/agents-global-skill/SKILL.md" ]] || { echo "FAIL: agents-global-skill resolves to file"; exit 1; }
echo "PASS: ~/.agents/skills/agents-global-skill linked"

[[ -L "$SKILLS_OUT/harness-skill" ]] || { echo "FAIL: harness-skill symlink missing"; exit 1; }
[[ -f "$SKILLS_OUT/harness-skill/SKILL.md" ]] || { echo "FAIL: harness-skill resolves to file"; exit 1; }
echo "PASS: \$HARNESS_DIR/.claude/skills/harness-skill linked"

[[ -L "$SKILLS_OUT/codex-only-skill" ]] || { echo "FAIL: codex-only-skill symlink missing"; exit 1; }
[[ -f "$SKILLS_OUT/codex-only-skill/SKILL.md" ]] || { echo "FAIL: codex-only-skill resolves to file"; exit 1; }
echo "PASS: \$HARNESS_DIR/.codex-only/skills/codex-only-skill linked"

[[ -L "$SKILLS_OUT/.system" ]] || { echo "FAIL: .system bundle symlink missing"; exit 1; }
echo "PASS: ~/.codex/skills/.system bundle linked"

[[ -L "$SKILLS_OUT/superpowers" ]] || { echo "FAIL: superpowers namespace symlink missing"; exit 1; }
[[ -f "$SKILLS_OUT/superpowers/brainstorming/SKILL.md" ]] || {
  echo "FAIL: superpowers namespace symlink does not resolve to nested skills"; exit 1;
}
echo "PASS: ~/.codex/superpowers/skills namespace linked"

[[ -f "$SKILLS_OUT/.harness-managed" ]] || { echo "FAIL: .harness-managed marker missing"; exit 1; }
grep -qx 'global-skill' "$SKILLS_OUT/.harness-managed"  || { echo "FAIL: marker missing global-skill"; exit 1; }
grep -qx 'agents-global-skill' "$SKILLS_OUT/.harness-managed" || { echo "FAIL: marker missing agents-global-skill"; exit 1; }
grep -qx 'harness-skill' "$SKILLS_OUT/.harness-managed" || { echo "FAIL: marker missing harness-skill"; exit 1; }
grep -qx 'codex-only-skill' "$SKILLS_OUT/.harness-managed" || { echo "FAIL: marker missing codex-only-skill"; exit 1; }
grep -qx 'superpowers' "$SKILLS_OUT/.harness-managed" || { echo "FAIL: marker missing superpowers"; exit 1; }
echo "PASS: .harness-managed marker tracks all linked entries"

[[ -f "$FAKE_HARNESS_S/.harness/codex/plugins/cache/openai-bundled/computer-use/1.0.0-test/.codex-plugin/plugin.json" ]] || {
  echo "FAIL: computer-use plugin cache not materialized"; exit 1;
}
[[ -f "$FAKE_HARNESS_S/.harness/codex/plugins/cache/openai-bundled/computer-use/1.0.0-test/skills/computer-use/SKILL.md" ]] || {
  echo "FAIL: computer-use plugin skill not materialized"; exit 1;
}
[[ -f "$FAKE_HARNESS_S/.harness/codex/plugins/cache/openai-bundled/chrome/0.1.0-test/.codex-plugin/plugin.json" ]] || {
  echo "FAIL: chrome plugin cache should be materialized for harness Codex"; exit 1;
}
[[ -L "$FAKE_HARNESS_S/.harness/codex/plugins/cache/openai-bundled/chrome/latest" ]] || {
  echo "FAIL: harness chrome plugin latest symlink missing"; exit 1;
}
extension_host_config="$FAKE_HOME/.codex/plugins/cache/openai-bundled/chrome/0.1.0-test/extension-host/macos/arm64/extension-host-config.json"
[[ -f "$extension_host_config" ]] || {
  echo "FAIL: current macOS 'Codex for Chrome' host was not discovered/configured"; exit 1;
}
grep -q '"extensionId": "fake-chrome-extension-id"' "$extension_host_config" || {
  echo "FAIL: Chrome extension host config missing fixture extension id"; exit 1;
}
kill "$FAKE_EXTENSION_HOST_PID" 2>/dev/null || true
wait "$FAKE_EXTENSION_HOST_PID" 2>/dev/null || true
echo "PASS: terminal Codex materializes plugins and configures current macOS 'Codex for Chrome' host"
lock_file="$FAKE_HOME/.codex/.codex-home-prepare-global.lock"
/usr/bin/lockf -s -k -t 0 "$lock_file" true || {
  echo "FAIL: global Codex cache kernel lock was not released"; exit 1;
}

# Older Codex Desktop plugin caches used `ChatGPT for Chrome` as the macOS
# native-host executable name. Keep those caches operational on machines where
# the app bundle is no longer present but the versioned global cache remains.
LEGACY_CHROME_HOME="$TEST_TEMP/fake-home-legacy-chrome-host"
LEGACY_CHROME_SOURCE="$TEST_TEMP/bundled-source-legacy-chrome-host"
LEGACY_CHROME_HARNESS="$TEST_TEMP/fake-harness-legacy-chrome-host"
mkdir -p "$LEGACY_CHROME_HOME" "$LEGACY_CHROME_HARNESS"
cp -pR "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled" "$LEGACY_CHROME_SOURCE"
mv "$LEGACY_CHROME_SOURCE/plugins/chrome/extension-host/macos/arm64/Codex for Chrome" \
   "$LEGACY_CHROME_SOURCE/plugins/chrome/extension-host/macos/arm64/ChatGPT for Chrome"
echo "# legacy chrome host rules" > "$LEGACY_CHROME_HARNESS/CLAUDE.md"
HOME="$LEGACY_CHROME_HOME" HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$LEGACY_CHROME_SOURCE" \
  "$PREPARE" "$LEGACY_CHROME_HARNESS"
legacy_extension_host_config="$LEGACY_CHROME_HOME/.codex/plugins/cache/openai-bundled/chrome/0.1.0-test/extension-host/macos/arm64/extension-host-config.json"
[[ -f "$legacy_extension_host_config" ]] || {
  echo "FAIL: legacy macOS 'ChatGPT for Chrome' host was not discovered/configured"; exit 1;
}
echo "PASS: legacy macOS 'ChatGPT for Chrome' host remains supported"

grep -q '/usr/bin/lockf' "$PREPARE" || {
  echo "FAIL: global Codex cache must use the macOS kernel advisory lock"; exit 1;
}
if grep -q '_global_codex_lock_is_stale\|initializing_grace\|trap .*lock_dir' "$PREPARE"; then
  echo "FAIL: PID/mtime stale-reclaim or signal cleanup heuristics must not replace kernel locking"; exit 1;
fi

# Kernel-lock adversarial test: hold the first marketplace copy inside the
# critical section, terminate the outer prepare shell, then start a contender.
# The protected child must retain the advisory lock until it finishes; the
# contender must never enter while the first copy is still active.
RACE_HOME="$TEST_TEMP/fake-home-lock-race"
RACE_SOURCE="$TEST_TEMP/bundled-source-lock-race"
RACE_HARNESS_A="$TEST_TEMP/fake-harness-lock-race-a"
RACE_HARNESS_B="$TEST_TEMP/fake-harness-lock-race-b"
RACE_BIN="$TEST_TEMP/lock-race-bin"
RACE_OWNER_CLAIM="$TEST_TEMP/lock-owner-claim"
RACE_FIRST_BLOCKED="$TEST_TEMP/lock-first-blocked"
RACE_FIRST_RELEASE="$TEST_TEMP/lock-first-release"
RACE_CONTENDER_ENTERED="$TEST_TEMP/lock-contender-entered"
RACE_LOG_A="$TEST_TEMP/lock-race-a.log"
RACE_LOG_B="$TEST_TEMP/lock-race-b.log"
REAL_CP="$(command -v cp)"
mkdir -p "$RACE_HOME" "$RACE_HARNESS_A" "$RACE_HARNESS_B" "$RACE_BIN"
cp -pR "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled" "$RACE_SOURCE"
echo "# lock race a" > "$RACE_HARNESS_A/CLAUDE.md"
echo "# lock race b" > "$RACE_HARNESS_B/CLAUDE.md"
cat > "$RACE_BIN/cp" <<'EOF'
#!/usr/bin/env bash
set -e
is_marketplace_copy=0
for arg in "$@"; do
  case "$arg" in
    "$RACE_SOURCE"|"$RACE_SOURCE/"|"$RACE_SOURCE/.") is_marketplace_copy=1; break ;;
  esac
done
if [[ "$is_marketplace_copy" -eq 1 ]]; then
  if mkdir "$RACE_OWNER_CLAIM" 2>/dev/null; then
    : > "$RACE_FIRST_BLOCKED"
    while [[ ! -e "$RACE_FIRST_RELEASE" ]]; do sleep 0.02; done
  else
    : > "$RACE_CONTENDER_ENTERED"
  fi
fi
exec "$REAL_CP" "$@"
EOF
chmod +x "$RACE_BIN/cp"
RACE_OWNER_CLAIM="$RACE_OWNER_CLAIM" RACE_FIRST_BLOCKED="$RACE_FIRST_BLOCKED" \
RACE_FIRST_RELEASE="$RACE_FIRST_RELEASE" RACE_SOURCE="$RACE_SOURCE" \
RACE_CONTENDER_ENTERED="$RACE_CONTENDER_ENTERED" REAL_CP="$REAL_CP" \
PATH="$RACE_BIN:$PATH" HOME="$RACE_HOME" HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$RACE_SOURCE" \
  "$PREPARE" "$RACE_HARNESS_A" >"$RACE_LOG_A" 2>&1 &
race_pid_a=$!
for _ in {1..250}; do
  [[ -e "$RACE_FIRST_BLOCKED" ]] && break
  sleep 0.02
done
[[ -e "$RACE_FIRST_BLOCKED" ]] || {
  kill "$race_pid_a" 2>/dev/null || true
  echo "FAIL: kernel lock test did not enter the protected marketplace copy"; exit 1;
}
kill -TERM "$race_pid_a" 2>/dev/null || true
sleep 0.1
RACE_OWNER_CLAIM="$RACE_OWNER_CLAIM" RACE_FIRST_BLOCKED="$RACE_FIRST_BLOCKED" \
RACE_FIRST_RELEASE="$RACE_FIRST_RELEASE" RACE_SOURCE="$RACE_SOURCE" \
RACE_CONTENDER_ENTERED="$RACE_CONTENDER_ENTERED" REAL_CP="$REAL_CP" \
PATH="$RACE_BIN:$PATH" HOME="$RACE_HOME" HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$RACE_SOURCE" \
  "$PREPARE" "$RACE_HARNESS_B" >"$RACE_LOG_B" 2>&1 &
race_pid_b=$!
sleep 1
race_overlap=0
[[ -e "$RACE_CONTENDER_ENTERED" ]] && race_overlap=1
: > "$RACE_FIRST_RELEASE"
set +e
wait "$race_pid_a"; race_rc_a=$?
wait "$race_pid_b"; race_rc_b=$?
set -e
[[ "$race_overlap" -eq 0 ]] || {
  echo "FAIL: contender entered after SIGTERM released the lock before protected work stopped"; exit 1;
}
if grep -q 'reclaiming stale Codex global cache lock' "$RACE_LOG_A" "$RACE_LOG_B"; then
  echo "FAIL: live kernel lock must never be reclaimed through stale owner heuristics"; exit 1;
fi
[[ "$race_rc_a" -eq 0 || "$race_rc_a" -eq 143 ]] || {
  echo "FAIL: terminated first prepare returned unexpected status $race_rc_a"; exit 1;
}
[[ "$race_rc_b" -eq 0 ]] || {
  echo "FAIL: contender did not complete after protected work released the kernel lock"; exit 1;
}
echo "PASS: kernel lock survives outer SIGTERM until protected work finishes"

FAKE_HARNESS_PAR_A="$TEST_TEMP/fake-harness-parallel-a"
FAKE_HARNESS_PAR_B="$TEST_TEMP/fake-harness-parallel-b"
mkdir -p "$FAKE_HARNESS_PAR_A" "$FAKE_HARNESS_PAR_B"
echo "# fake rules" > "$FAKE_HARNESS_PAR_A/CLAUDE.md"
echo "# fake rules" > "$FAKE_HARNESS_PAR_B/CLAUDE.md"
HOME="$FAKE_HOME" HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled" "$PREPARE" "$FAKE_HARNESS_PAR_A" &
pid_a=$!
HOME="$FAKE_HOME" HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled" "$PREPARE" "$FAKE_HARNESS_PAR_B" &
pid_b=$!
set +e
wait "$pid_a"; rc_a=$?
wait "$pid_b"; rc_b=$?
set -e
[[ "$rc_a" -eq 0 && "$rc_b" -eq 0 ]] || {
  echo "FAIL: concurrent prepare should not race on global Codex cache"; exit 1;
}
/usr/bin/lockf -s -k -t 0 "$FAKE_HOME/.codex/.codex-home-prepare-global.lock" true || {
  echo "FAIL: global Codex cache kernel lock remained held after concurrent prepare"; exit 1;
}
echo "PASS: concurrent prepare serializes global Codex cache updates"
[[ ! -e "$FAKE_HARNESS_S/.harness/codex/plugins/cache/openai-bundled/browser" ]] || {
  echo "FAIL: browser plugin cache should remain pruned for terminal Codex"; exit 1;
}
grep -q '^\[plugins."chrome@openai-bundled"\]' "$FAKE_HARNESS_S/.harness/codex/config.toml" || {
  echo "FAIL: chrome plugin config missing"; exit 1;
}
if grep -q '^\[plugins."browser@openai-bundled"\]' "$FAKE_HARNESS_S/.harness/codex/config.toml"; then
  echo "FAIL: browser plugin config should be omitted for terminal Codex TUI"; exit 1;
fi
grep -q '^\[plugins."computer-use@openai-bundled"\]' "$FAKE_HARNESS_S/.harness/codex/config.toml" || {
  echo "FAIL: computer-use plugin config missing"; exit 1;
}
echo "PASS: terminal Codex exposes Computer Use and Chrome while Browser stays pruned"

grep -q '^\[mcp_servers.node_repl\]' "$FAKE_CONFIG" || {
  echo "FAIL: node_repl MCP should be preserved for Chrome bridge support"; exit 1;
}
grep -q '^BROWSER_USE_AVAILABLE_BACKENDS = "chrome"' "$FAKE_CONFIG" || {
  echo "FAIL: node_repl Chrome backend env missing"; exit 1;
}
grep -q '^NODE_REPL_NATIVE_PIPE_CONNECT_TIMEOUT_MS = "5000"' "$FAKE_CONFIG" || {
  echo "FAIL: node_repl native pipe timeout missing"; exit 1;
}
if grep -q '^NODE_REPL_TRUSTED_CODE_PATHS = ' "$FAKE_CONFIG"; then
  echo "FAIL: launcher must not trust entire per-harness or global Codex homes"; exit 1;
fi
expected_browser_sha="$(shasum -a 256 "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/chrome/scripts/browser-client.mjs" | awk '{print $1}')"
grep -q "^NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S = \"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa $expected_browser_sha\"" "$FAKE_CONFIG" || {
  echo "FAIL: node_repl should trust only the exact configured/browser-client SHA allowlist"; exit 1;
}
echo "PASS: node_repl Chrome bridge uses exact browser-client SHA trust without broad code paths"

# First-run convergence: bundled marketplace sync must finish under the global
# lock before config derives Chrome browser-client trust and plugin version.
FIRST_RUN_HOME="$TEST_TEMP/fake-home-first-run"
FIRST_RUN_SOURCE="$TEST_TEMP/bundled-source-first-run"
FIRST_RUN_HARNESS="$TEST_TEMP/fake-harness-first-run"
mkdir -p "$FIRST_RUN_HOME" "$FIRST_RUN_HARNESS"
cp -pR "$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled" "$FIRST_RUN_SOURCE"
echo "# first-run rules" > "$FIRST_RUN_HARNESS/CLAUDE.md"
cat > "$FIRST_RUN_HARNESS/.mcp.json" <<'EOF'
{
  "mcpServers": {
    "node_repl": { "command": "/fake/node_repl" }
  }
}
EOF
HOME="$FIRST_RUN_HOME" HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$FIRST_RUN_SOURCE" "$PREPARE" "$FIRST_RUN_HARNESS"
first_run_config="$FIRST_RUN_HARNESS/.harness/codex/config.toml"
first_run_sha="$(shasum -a 256 "$FIRST_RUN_SOURCE/plugins/chrome/scripts/browser-client.mjs" | awk '{print $1}')"
grep -q "^NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S = \"$first_run_sha\"" "$first_run_config" || {
  echo "FAIL: first prepare run did not derive browser-client SHA after marketplace sync"; exit 1;
}
grep -q '^BROWSER_USE_CODEX_APP_VERSION = "0.1.0-test"' "$first_run_config" || {
  echo "FAIL: first prepare run did not derive Chrome plugin version after marketplace sync"; exit 1;
}
echo "PASS: first prepare run converges marketplace sync, browser SHA, and plugin version"

# Complete marketplace authority: a non-Chrome plugin content change with the
# Chrome manifest unchanged must refresh both marketplace and materialized cache
# in one prepare run.
printf '%s\n' 'updated computer-use skill without manifest bump' \
  > "$FIRST_RUN_SOURCE/plugins/computer-use/skills/computer-use/SKILL.md"
HOME="$FIRST_RUN_HOME" HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$FIRST_RUN_SOURCE" "$PREPARE" "$FIRST_RUN_HARNESS"
first_run_marketplace_skill="$FIRST_RUN_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/computer-use/skills/computer-use/SKILL.md"
first_run_cached_skill="$FIRST_RUN_HARNESS/.harness/codex/plugins/cache/openai-bundled/computer-use/1.0.0-test/skills/computer-use/SKILL.md"
grep -q 'updated computer-use skill without manifest bump' "$first_run_marketplace_skill" || {
  echo "FAIL: marketplace did not refresh when computer-use changed under unchanged Chrome manifest"; exit 1;
}
grep -q 'updated computer-use skill without manifest bump' "$first_run_cached_skill" || {
  echo "FAIL: materialized computer-use cache did not refresh in the same prepare run"; exit 1;
}
echo "PASS: complete marketplace/plugin changes refresh in one prepare run"

# Re-run after dropping a per-harness skill should remove its symlink
rm -rf "$FAKE_HARNESS_S/.claude/skills/harness-skill"
HOME="$FAKE_HOME" HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled" "$PREPARE" "$FAKE_HARNESS_S"
[[ ! -L "$SKILLS_OUT/harness-skill" ]] || { echo "FAIL: stale harness-skill symlink not removed"; exit 1; }
[[ -L "$SKILLS_OUT/global-skill" ]] || { echo "FAIL: global-skill should still be present"; exit 1; }
echo "PASS: re-run drops sources that were removed"

# Migration: old single-symlink layout should be replaced with directory
rm -rf "$FAKE_HARNESS_S/.harness/codex/skills"
ln -s "$FAKE_HOME/.codex/skills" "$FAKE_HARNESS_S/.harness/codex/skills"
[[ -L "$FAKE_HARNESS_S/.harness/codex/skills" ]] || { echo "FAIL: setup precondition"; exit 1; }
HOME="$FAKE_HOME" HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled" "$PREPARE" "$FAKE_HARNESS_S"
[[ -d "$SKILLS_OUT" && ! -L "$SKILLS_OUT" ]] || {
  echo "FAIL: migration from old symlink layout failed"; exit 1;
}
echo "PASS: migrates from old single-symlink layout"

# Missing .mcp.json should not break — per-profile files still generated
TEST_HARNESS2="$TEST_TEMP/fake-harness-2"
mkdir -p "$TEST_HARNESS2"
echo "# rules" > "$TEST_HARNESS2/CLAUDE.md"
"$PREPARE" "$TEST_HARNESS2"
config2="$TEST_HARNESS2/.harness/codex/config.toml"
[[ -f "$config2" ]] || { echo "FAIL: config.toml missing for harness without .mcp.json"; exit 1; }
[[ -f "$TEST_HARNESS2/.harness/codex/rich.config.toml" ]] || { echo "FAIL: rich.config.toml missing for harness without .mcp.json"; exit 1; }
if grep -q '^\[mcp_servers' "$config2"; then
  echo "FAIL: should not have mcp_servers when .mcp.json missing"; exit 1;
fi
echo "PASS: works without .mcp.json (no mcp_servers section, per-profile files intact)"
fi

if [[ "$TEST_GROUP" == "hooks" ]]; then
# Codex hooks infrastructure: config.toml must enable hooks feature,
# and hooks.json must reference Claude harness's core/hooks/*.sh by absolute path.
# Source-of-truth: Claude harness owns hook scripts; Codex layer references them.
TEST_HARNESS3="$TEST_TEMP/fake-harness-hooks"
mkdir -p "$TEST_HARNESS3/core/hooks" "$TEST_HARNESS3/.claude/source"
echo "# rules" > "$TEST_HARNESS3/CLAUDE.md"
mkdir -p "$TEST_HARNESS3/.codex"
cat > "$TEST_HARNESS3/.codex/hooks.json" <<'EOF'
{
  "hooks": {
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/pre-tool-budget-guard.sh\""
          }
        ]
      }
    ]
  }
}
EOF
# Stub hook scripts so the generator can verify they exist
for h in session-start user-prompt-session-end-detect prompt-keyword-routing \
         pre-bash-irreversible-guard pre-bash-gh-auth pre-bash-pr-gate pre-bash-worktree-gate \
         pre-tool-budget-guard pre-tool-opus-guard pre-edit-config-protection \
         pre-edit-codex-output-guard pre-write-memory-block pre-tool-scoped-context \
         post-edit-codex-resync suggest-compact post-bash-audit post-bash-commit-detect \
         post-tool-auto-pilot-reinject session-end cmux-title-persist codex-subagent-status \
         pre-tool-peer-checkout-guard; do
  : > "$TEST_HARNESS3/core/hooks/$h.sh"
done
cat > "$TEST_HARNESS3/.claude/source/hooks.yaml" <<'EOF'
hooks:
  source: .claude/settings.json
  mode: settings-driven
  codex_exclusions:
    - SessionEnd
    - post-edit-codex-resync.sh
    - cmux-title-persist.sh
EOF
cat > "$TEST_HARNESS3/.claude/settings.json" <<'EOF'
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/session-start.sh\"",
            "timeout": 10000
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/prompt-keyword-routing.sh\""
          },
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/user-prompt-session-end-detect.sh\"",
            "timeout": 5000
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/pre-tool-budget-guard.sh\""
          }
        ]
      },
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/pre-tool-opus-guard.sh\""
          },
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/pre-edit-codex-output-guard.sh\""
          }
        ]
      },
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/pre-write-memory-block.sh\""
          }
        ]
      },
      {
        "matcher": "Read|Edit|Write|MultiEdit|Grep|Glob",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/pre-tool-scoped-context.sh\""
          }
        ]
      },
      {
        "matcher": "Bash|Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/pre-tool-peer-checkout-guard.sh\""
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/pre-bash-irreversible-guard.sh\"",
            "timeout": 2000
          },
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/pre-bash-gh-auth.sh\""
          },
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/pre-bash-pr-gate.sh\""
          },
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/pre-bash-worktree-gate.sh\""
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/post-edit-codex-resync.sh\"",
            "timeout": 2000
          }
        ]
      },
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/suggest-compact.sh\"",
            "timeout": 3000
          },
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/post-tool-auto-pilot-reinject.sh\"",
            "timeout": 1000
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/post-bash-audit.sh\"",
            "timeout": 3000
          },
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/post-bash-commit-detect.sh\"",
            "timeout": 3000
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/session-end.sh\"",
            "timeout": 20000
          },
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/cmux-title-persist.sh\"",
            "timeout": 3000
          }
        ]
      }
    ]
  }
}
EOF
"$PREPARE" "$TEST_HARNESS3"
config3="$TEST_HARNESS3/.harness/codex/config.toml"
hooks_json="$TEST_HARNESS3/.harness/codex/hooks.json"
legacy_backup_count="$(find "$TEST_HARNESS3" -maxdepth 1 -type d -name '.codex.legacy-*' | wc -l | tr -d ' ')"
[[ ! -e "$TEST_HARNESS3/.codex" ]] || { echo "FAIL: legacy project .codex directory should be quarantined"; exit 1; }
[[ "$legacy_backup_count" == "1" ]] || {
  echo "FAIL: expected one .codex.legacy-* quarantine backup, got $legacy_backup_count"; exit 1;
}
echo "PASS: legacy project .codex quarantined so Codex does not load stale root hooks"

grep -q '^\[features\]' "$config3" || { echo "FAIL: [features] section missing"; exit 1; }
grep -q '^hooks = true' "$config3" || { echo "FAIL: hooks feature flag missing"; exit 1; }
grep -q '^goals = true' "$config3" || { echo "FAIL: goals feature flag missing"; exit 1; }
grep -q '^multi_agent = true' "$config3" || { echo "FAIL: multi_agent feature flag missing"; exit 1; }
if grep -q '^codex_hooks = true' "$config3"; then
  echo "FAIL: deprecated codex_hooks feature flag present"; exit 1;
fi
echo "PASS: [features].hooks, goals, and multi_agent enabled in config.toml"

# ChatGPT Apps/connectors disabled at feature-flag level — harness sessions
# don't need them and the user opts deny-by-default globally.
grep -q '^apps = false' "$config3" || { echo "FAIL: [features].apps = false missing"; exit 1; }
echo "PASS: [features].apps disabled in config.toml"

# Opt-in path: HARNESS_CODEX_APPS_ALLOWLIST names the app ids a harness may use.
# The feature flag turns on, but [apps._default] keeps every unnamed app off.
HARNESS_CODEX_APPS_ALLOWLIST="asdk_app_testone,asdk_app_testtwo" "$PREPARE" "$TEST_HARNESS3"
grep -q '^apps = true' "$config3" || {
  echo "FAIL: [features].apps should be true when HARNESS_CODEX_APPS_ALLOWLIST is set"; exit 1;
}
grep -q '^\[apps\._default\]' "$config3" || { echo "FAIL: [apps._default] section missing"; exit 1; }
python3.13 - "$config3" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    config = tomllib.load(stream)

apps = config["apps"]
assert apps["_default"] == {"enabled": False}, apps["_default"]
assert apps["asdk_app_testone"] == {"enabled": True}, apps
assert apps["asdk_app_testtwo"] == {"enabled": True}, apps
assert config["features"]["apps"] is True
PY
echo "PASS: [features].apps opt-in with per-app allowlist and deny-by-default"
HARNESS_CODEX_APPS_ALLOWLIST="asdk_app_testone,asdk_app_testtwo" HARNESS_CODEX_SLACK_APPS="asdk_app_testone" "$PREPARE" "$TEST_HARNESS3"
python3 - "$config3" <<'PYSLACK'
import sys, tomllib
with open(sys.argv[1], 'rb') as file:
    config = tomllib.load(file)
assert config['approvals_reviewer'] == 'user'
assert config['approval_policy'] == 'on-request'
slack = config['apps']['asdk_app_testone']
assert slack['approvals_reviewer'] == 'user'
assert slack['tools']['slack_slack_send_message']['approval_mode'] == 'prompt'
assert slack['tools']['slack_slack_update_canvas']['approval_mode'] == 'prompt'
assert 'slack_slack_read_thread' not in slack['tools']
assert 'slack_slack_send_message_draft' not in slack['tools']
assert config['apps']['asdk_app_testtwo'] == {'enabled': True}
PYSLACK
echo "PASS: Slack generated config exact mutation tools require native user approval"


# Without the allowlist the harness returns to the deny-by-default shape.
"$PREPARE" "$TEST_HARNESS3"
grep -q '^apps = false' "$config3" || {
  echo "FAIL: [features].apps should return to false without an allowlist"; exit 1;
}
if grep -q '^\[apps\.' "$config3"; then
  echo "FAIL: no [apps.*] table should be emitted without an allowlist"; exit 1;
fi
echo "PASS: apps deny-by-default restored when allowlist is unset"

python3.13 - "$config3" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    config = tomllib.load(stream)

assert config["features"]["context_management"] == {"experimental_mode": True}
PY
echo "PASS: [features.context_management].experimental_mode enabled in config.toml"

cat > "$CODEX_BIN_STUB" <<'EOF'
#!/usr/bin/env bash
echo "codex-cli 0.152.1"
EOF
chmod +x "$CODEX_BIN_STUB"
"$PREPARE" "$TEST_HARNESS3"
python3.13 - "$config3" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    config = tomllib.load(stream)

assert "context_management" not in config["features"]
PY

cat > "$CODEX_BIN_STUB" <<'EOF'
#!/usr/bin/env bash
echo "codex-cli 0.153.0"
EOF
chmod +x "$CODEX_BIN_STUB"
"$PREPARE" "$TEST_HARNESS3"
python3.13 - "$config3" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    config = tomllib.load(stream)

assert config["features"]["context_management"] == {"experimental_mode": True}
PY
echo "PASS: context management follows the resolved Codex CLI 0.153.0 capability floor"

for selection in 272k 500k 1m auto 500k 272k auto; do
  HARNESS_CODEX_CONTEXT="$selection" "$PREPARE" "$TEST_HARNESS3"
  python3.13 - "$config3" "$selection" <<'PYTEST'
import sys, tomllib
with open(sys.argv[1], "rb") as stream:
    data = tomllib.load(stream)
values = {"272k": (272000, 217600), "500k": (500000, 400000), "1m": (1000000, 414000)}
if sys.argv[2] == "auto":
    assert "model_context_window" not in data and "model_auto_compact_token_limit" not in data
else:
    assert (data["model_context_window"], data["model_auto_compact_token_limit"]) == values[sys.argv[2]]
PYTEST
done
echo "PASS: all context modes repair warm generated config"

grep -q '^\[marketplaces.openai-bundled\]' "$config3" || {
  echo "FAIL: openai-bundled marketplace missing"; exit 1;
}
grep -q "^source = \"$HOME/.codex/.tmp/bundled-marketplaces/openai-bundled\"" "$config3" || {
  echo "FAIL: openai-bundled marketplace source missing"; exit 1;
}
grep -q '^\[plugins\."computer-use@openai-bundled"\]' "$config3" || {
  echo "FAIL: computer-use plugin section missing"; exit 1;
}
grep -q '^\[plugins\."chrome@openai-bundled"\]' "$config3" || {
  echo "FAIL: chrome plugin should be enabled for terminal Codex"; exit 1;
}
if grep -q '^\[plugins\."browser@openai-bundled"\]' "$config3"; then
  echo "FAIL: browser plugin should not be enabled in terminal Codex config"; exit 1;
fi
grep -A1 '^\[plugins\."computer-use@openai-bundled"\]' "$config3" | grep -q '^enabled = true' || {
  echo "FAIL: computer-use plugin not enabled"; exit 1;
}
grep -A1 '^\[plugins\."chrome@openai-bundled"\]' "$config3" | grep -q '^enabled = true' || {
  echo "FAIL: chrome plugin not enabled"; exit 1;
}
echo "PASS: terminal Codex enables Computer Use and Chrome; Browser omitted"

grep -q '^\[tui\]' "$config3" || { echo "FAIL: [tui] section missing"; exit 1; }
grep -q '^terminal_title = \["activity", "thread-title", "project-name"\]' "$config3" || {
  echo "FAIL: [tui].terminal_title missing expected harness defaults"; exit 1;
}
echo "PASS: [tui].terminal_title configured in config.toml"

grep -q '^status_line = \["thread-title", "model-with-reasoning", "git-branch", "context-remaining", "branch-changes", "run-state", "task-progress", "five-hour-limit", "weekly-limit"\]' "$config3" || {
  echo "FAIL: [tui].status_line missing expected harness defaults"; exit 1;
}
echo "PASS: [tui].status_line configured in config.toml"

cat >> "$config3" <<'EOF'

[hooks.state."test-preserve-hook"]
trusted_hash = "sha256:preserved"
EOF
"$PREPARE" "$TEST_HARNESS3"
grep -q '^\[hooks\.state\."test-preserve-hook"\]' "$config3" || {
  echo "FAIL: hooks.state trust section was not preserved across config regeneration"; exit 1;
}
grep -q '^trusted_hash = "sha256:preserved"' "$config3" || {
  echo "FAIL: hooks.state trusted_hash was not preserved across config regeneration"; exit 1;
}
echo "PASS: Codex hooks.state trust sections preserved across config regeneration"

[[ -f "$hooks_json" ]] || { echo "FAIL: hooks.json not generated"; exit 1; }
python3 -c "import json; json.load(open('$hooks_json'))" 2>/dev/null \
  || { echo "FAIL: hooks.json is not valid JSON"; exit 1; }
echo "PASS: hooks.json generated and valid JSON"

# The launcher-owned cmux title helper is one direct SessionStart hook. It is
# installed beside codex-home-prepare.sh and runs under the selected Python,
# without the Claude-output adapter because it intentionally emits no output.
python3 - "$hooks_json" "$TEST_HARNESS3" "$PREPARE" <<'PY' || exit 1
import json
import os
import shlex
import sys

path, harness, prepare = sys.argv[1:4]
with open(path, encoding="utf-8") as stream:
    hooks = json.load(stream).get("hooks", {})
title_hooks = [
    hook
    for entry in hooks.get("SessionStart", [])
    for hook in entry.get("hooks", [])
    if "codex-cmux-title-sync.py" in hook.get("command", "")
]
if len(title_hooks) != 1:
    print(f"FAIL: expected one cmux title SessionStart hook, got {title_hooks}")
    sys.exit(1)
hook = title_hooks[0]
command = hook["command"]
expected_helper = os.path.join(os.path.dirname(prepare), "codex-cmux-title-sync.py")
parts = shlex.split(command)
if len(parts) != 2 or parts[1] != expected_helper:
    print(f"FAIL: cmux title hook must use installed sibling {expected_helper}: {command}")
    sys.exit(1)
if not os.path.isabs(parts[0]) or not os.access(parts[0], os.X_OK):
    print(f"FAIL: cmux title hook must use the selected absolute Python executable: {command}")
    sys.exit(1)
if hook.get("timeout") != 3000:
    print(f"FAIL: cmux title hook must use a 3000ms startup timeout: {hook}")
    sys.exit(1)
if "codex-hook-adapter.sh" in command:
    print(f"FAIL: cmux title hook must be direct, not adapter-wrapped: {command}")
    sys.exit(1)
print("OK")
PY
echo "PASS: SessionStart wires one direct installed cmux title synchronizer"

# Verify each event has the expected hook entries
python3 - "$hooks_json" "$TEST_HARNESS3" <<'PY' || exit 1
import json, sys, os
path, harness = sys.argv[1], sys.argv[2]
with open(path) as f: data = json.load(f)
hooks = data.get("hooks", {})
expected = {
    "SessionStart": ["session-start.sh", "codex-subagent-status.sh"],
    "SubagentStart": ["codex-subagent-status.sh"],
    "SubagentStop": ["codex-subagent-status.sh"],
    "UserPromptSubmit": ["user-prompt-session-end-detect.sh", "prompt-keyword-routing.sh"],
    "PreToolUse": ["pre-bash-irreversible-guard.sh", "pre-bash-gh-auth.sh",
                   "pre-bash-pr-gate.sh", "pre-bash-worktree-gate.sh",
                   "pre-tool-budget-guard.sh", "pre-tool-opus-guard.sh",
                   "pre-edit-codex-output-guard.sh", "pre-write-memory-block.sh",
                   "pre-tool-scoped-context.sh"],
    "PostToolUse": ["suggest-compact.sh", "post-bash-audit.sh",
                    "post-bash-commit-detect.sh", "post-tool-auto-pilot-reinject.sh"],
    "Stop": ["session-end.sh"],
}
for event, scripts in expected.items():
    found = []
    for entry in hooks.get(event, []):
        for h in entry.get("hooks", []):
            cmd = h.get("command", "")
            for s in scripts:
                if s in cmd:
                    found.append(s)
                    abs_expected = os.path.join(harness, "core/hooks", s)
                    if abs_expected not in cmd:
                        print(f"FAIL: {event} hook {s} should reference absolute path {abs_expected}, got: {cmd}")
                        sys.exit(1)
    missing = set(scripts) - set(found)
    if missing:
        print(f"FAIL: {event} missing hooks: {missing}")
        sys.exit(1)
stop_scripts = {
    os.path.basename(match.group(1))
    for entry in hooks.get("Stop", [])
    for hook in entry.get("hooks", [])
    for match in [__import__("re").search(r"core/hooks/([^\"'\s]+?\.sh)", hook.get("command", ""))]
    if match
}
if stop_scripts != {"session-end.sh"}:
    print(f"FAIL: Stop scripts must be exactly session-end.sh, got {sorted(stop_scripts)}")
    sys.exit(1)
print("OK")
PY
echo "PASS: hooks.json wires all expected hooks via absolute paths to core/hooks/"

python3 - "$hooks_json" <<'PY' || exit 1
import json, sys
data = json.load(open(sys.argv[1]))
blob = json.dumps(data)
for excluded in ("post-edit-codex-resync.sh", "cmux-title-persist.sh"):
    if excluded in blob:
        print(f"FAIL: excluded hook should not be wired for Codex: {excluded}")
        sys.exit(1)
print("OK")
PY
echo "PASS: hooks.yaml exclusions respected for Codex hooks"

# A missing source policy keeps the historical safety defaults. Partial or
# legacy harnesses must not acquire a per-turn Stop hook or post-edit resync.
TEST_HARNESS_DEFAULTS="$TEST_TEMP/fake-harness-hook-defaults"
mkdir -p "$TEST_HARNESS_DEFAULTS/core/hooks" "$TEST_HARNESS_DEFAULTS/.claude"
echo "# rules" > "$TEST_HARNESS_DEFAULTS/CLAUDE.md"
for h in session-end post-edit-codex-resync; do
  : > "$TEST_HARNESS_DEFAULTS/core/hooks/$h.sh"
done
cat > "$TEST_HARNESS_DEFAULTS/.claude/settings.json" <<'EOF'
{
  "hooks": {
    "Stop": [{"hooks": [{"type": "command", "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/session-end.sh\""}]}],
    "PostToolUse": [{"hooks": [{"type": "command", "command": "bash \"$CLAUDE_PROJECT_DIR/core/hooks/post-edit-codex-resync.sh\""}]}]
  }
}
EOF
"$PREPARE" "$TEST_HARNESS_DEFAULTS"
python3 - "$TEST_HARNESS_DEFAULTS/.harness/codex/hooks.json" <<'PY' || exit 1
import json, sys
blob = json.dumps(json.load(open(sys.argv[1])))
for excluded in ("session-end.sh", "post-edit-codex-resync.sh"):
    if excluded in blob:
        print(f"FAIL: missing hooks.yaml must retain default exclusion for {excluded}")
        sys.exit(1)
print("OK")
PY
echo "PASS: missing hooks.yaml retains legacy Stop and post-edit exclusions"

# Legacy synthesis has no settings.json matcher to preserve. The two strict
# guards must still be split and widened, while ordinary Bash hooks stay raw.
TEST_HARNESS_LEGACY="$TEST_TEMP/fake-harness-hook-legacy"
mkdir -p "$TEST_HARNESS_LEGACY/core/hooks" "$TEST_HARNESS_LEGACY/.claude"
echo "# rules" > "$TEST_HARNESS_LEGACY/CLAUDE.md"
for h in pre-bash-irreversible-guard pre-bash-pr-gate pre-bash-harness-main-only-guard; do
  : > "$TEST_HARNESS_LEGACY/core/hooks/$h.sh"
done
"$PREPARE" "$TEST_HARNESS_LEGACY"
python3 - "$TEST_HARNESS_LEGACY/.harness/codex/hooks.json" <<'PY' || exit 1
import json, sys
data = json.load(open(sys.argv[1]))
expected = {
    "pre-bash-irreversible-guard.sh": "Bash",
    "pre-bash-pr-gate.sh": "^(Bash|code_mode_exec|exec)$",
    "pre-bash-harness-main-only-guard.sh": "^(Bash|code_mode_exec|exec)$",
}
found = {}
for entry in data["hooks"].get("PreToolUse", []):
    for hook in entry.get("hooks", []):
        for script in expected:
            if script in hook.get("command", ""):
                if script in found:
                    print(f"FAIL: legacy hook was duplicated: {script}")
                    sys.exit(1)
                found[script] = entry.get("matcher")
if found != expected:
    print(f"FAIL: legacy strict matcher split mismatch: {found}")
    sys.exit(1)
PY
echo "PASS: legacy strict guards split from Bash-only hooks"

# The peer-checkout guard keeps its Claude matcher through the direct hook and,
# for Codex code mode (exec|code_mode_exec), also runs through the strict
# composite adapter so it sees each resolved command.
PEER_HARNESS="$TEST_TEMP/fake-harness-peer-guard"
mkdir -p "$PEER_HARNESS/core/hooks" "$PEER_HARNESS/.claude"
echo "# rules" > "$PEER_HARNESS/CLAUDE.md"
: > "$PEER_HARNESS/core/hooks/pre-tool-peer-checkout-guard.sh"
cat > "$PEER_HARNESS/.claude/settings.json" <<'EOF'
{"hooks":{"PreToolUse":[{"matcher":"Bash|Edit|Write|MultiEdit","hooks":[{"type":"command","command":"bash \"$CLAUDE_PROJECT_DIR/core/hooks/pre-tool-peer-checkout-guard.sh\""}]}],
 "PostToolUse":[{"matcher":"Bash|Edit|Write|MultiEdit","hooks":[{"type":"command","command":"bash \"$CLAUDE_PROJECT_DIR/core/hooks/pre-tool-peer-checkout-guard.sh\""}]}]}}
EOF
"$PREPARE" "$PEER_HARNESS"
python3 - "$PEER_HARNESS/.harness/codex/hooks.json" <<'PY' || exit 1
import json, sys
data = json.load(open(sys.argv[1]))
pre = [e for e in data["hooks"]["PreToolUse"]
       if any("pre-tool-peer-checkout-guard.sh" in h["command"] for h in e["hooks"])]
direct = [e for e in pre if all("codex-pretool-adapter.py" not in h["command"] for h in e["hooks"])]
strict = [e for e in pre if any("codex-pretool-adapter.py" in h["command"] for h in e["hooks"])]
if len(direct) != 1 or direct[0].get("matcher") != "Bash|Edit|Write|apply_patch":
    print(f"FAIL: peer guard lost its direct Bash/Edit/Write/apply_patch entry: {pre}"); sys.exit(1)
if len(strict) != 1 or strict[0].get("matcher") != "^(code_mode_exec|exec)$" or len(strict[0]["hooks"]) != 1:
    print(f"FAIL: peer guard is not emitted through the adapter for exec|code_mode_exec: {pre}"); sys.exit(1)
post = [e for e in data["hooks"]["PostToolUse"]
        if any("pre-tool-peer-checkout-guard.sh" in h["command"] for h in e["hooks"])]
if len(post) != 1 or "codex-hook-adapter.sh" not in post[0]["hooks"][0]["command"]:
    print(f"FAIL: peer guard PostToolUse changed: {post}"); sys.exit(1)
PY
echo "PASS: peer-checkout guard is emitted for exec|code_mode_exec through the pretool adapter"

# A strict command guard is unsafe without the installed composite adapter.
# Both source modes must fail before they emit a direct Bash fallback, while a
# harness without strict hooks remains valid (covered by the defaults fixture).
MISSING_ADAPTER_BIN="$TEST_TEMP/missing-pretool-adapter-bin"
cp -R "$LAUNCHER_DIR/bin" "$MISSING_ADAPTER_BIN"
rm -f "$MISSING_ADAPTER_BIN/codex-pretool-adapter.py"
PREPARE_WITHOUT_PRETOOL="$MISSING_ADAPTER_BIN/codex-home-prepare.sh"
for strict_source in settings legacy; do
  STRICT_HARNESS="$TEST_TEMP/fake-harness-missing-pretool-$strict_source"
  mkdir -p "$STRICT_HARNESS/core/hooks" "$STRICT_HARNESS/.claude"
  echo "# rules" > "$STRICT_HARNESS/CLAUDE.md"
  : > "$STRICT_HARNESS/core/hooks/pre-bash-pr-gate.sh"
  if [[ "$strict_source" == "settings" ]]; then
    cat > "$STRICT_HARNESS/.claude/settings.json" <<'EOF'
{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"bash \"$CLAUDE_PROJECT_DIR/core/hooks/pre-bash-pr-gate.sh\""}]}]}}
EOF
  fi
  set +e
  "$PREPARE_WITHOUT_PRETOOL" "$STRICT_HARNESS" >"$TEST_TEMP/missing-pretool-$strict_source.out" 2>"$TEST_TEMP/missing-pretool-$strict_source.err"
  strict_rc=$?
  set -e
  [[ "$strict_rc" != "0" ]] || {
    echo "FAIL: $strict_source strict hooks must fail when pretool adapter is absent"; exit 1;
  }
  [[ ! -f "$STRICT_HARNESS/.harness/codex/hooks.json" ]] || {
    echo "FAIL: $strict_source must not emit insecure hooks.json without pretool adapter"; exit 1;
  }
done
echo "PASS: strict hook generation fails closed without pretool adapter"

# Adapter wrapping: SessionStart, UserPromptSubmit, and PostToolUse may emit
# Claude-format JSON ({"additionalContext": ...}) which Codex rejects. Those
# events MUST be routed through codex-hook-adapter.sh. Two command-sensitive,
# non-rewriting PreToolUse guards use the strict composite-exec adapter so they
# see each resolved cmd/workdir while preserving exit-2 blocking.
python3 - "$hooks_json" <<'PY' || exit 1
import json, sys
data = json.load(open(sys.argv[1]))
adapted = {"SessionStart", "UserPromptSubmit", "PostToolUse"}
strict_pretool = {
    "pre-bash-harness-main-only-guard.sh",
    "pre-bash-pr-gate.sh",
}
for event in adapted:
    for entry in data["hooks"].get(event, []):
        for h in entry.get("hooks", []):
            cmd = h.get("command", "")
            if "codex-cmux-title-sync.py" in cmd:
                if "codex-hook-adapter.sh" in cmd:
                    print(f"FAIL: cmux title hook must not use adapter: {cmd}")
                    sys.exit(1)
                continue
            if "codex-hook-adapter.sh" not in cmd:
                print(f"FAIL: {event} hook not wrapped via adapter: {cmd}")
                sys.exit(1)
            if event not in cmd:
                print(f"FAIL: {event} adapter call missing event arg: {cmd}")
                sys.exit(1)
peer_adapter_entries = peer_direct_entries = 0
for entry in data["hooks"].get("PreToolUse", []):
    scripts = {
        name
        for hook in entry.get("hooks", [])
        for name in strict_pretool
        if name in hook.get("command", "")
    }
    if scripts and entry.get("matcher") != "^(Bash|code_mode_exec|exec)$":
        print(f"FAIL: strict PreToolUse hook must use the exact raw/composite matcher: {entry}")
        sys.exit(1)
    if scripts and len(entry.get("hooks", [])) != 1:
        print(f"FAIL: strict PreToolUse hook must be split from Bash-only hooks: {entry}")
        sys.exit(1)
    for h in entry.get("hooks", []):
        cmd = h.get("command", "")
        matched = {name for name in strict_pretool if name in cmd}
        if matched and "codex-pretool-adapter.py" not in cmd:
            print(f"FAIL: strict PreToolUse hook not normalized: {cmd}")
            sys.exit(1)
        # The peer-checkout guard keeps its direct Bash/Edit/Write/apply_patch entry and
        # additionally runs through the adapter for code mode only.
        peer = "pre-tool-peer-checkout-guard.sh" in cmd
        if peer and "codex-pretool-adapter.py" in cmd:
            peer_adapter_entries += 1
            if entry.get("matcher") != "^(code_mode_exec|exec)$" or len(entry.get("hooks", [])) != 1:
                print(f"FAIL: peer guard adapter entry must be code-mode only and split: {entry}")
                sys.exit(1)
        elif peer:
            peer_direct_entries += 1
            if entry.get("matcher") != "Bash|Edit|Write|apply_patch":
                print(f"FAIL: peer guard direct entry lost its matcher: {entry}")
                sys.exit(1)
        elif not matched and "codex-pretool-adapter.py" in cmd:
            print(f"FAIL: rewriting/unapproved PreToolUse hook uses strict adapter: {cmd}")
            sys.exit(1)
if (peer_adapter_entries, peer_direct_entries) != (1, 1):
    print(f"FAIL: peer guard must have one direct and one code-mode adapter entry: {(peer_adapter_entries, peer_direct_entries)}")
    sys.exit(1)
for entry in data["hooks"].get("Stop", []):
    for h in entry.get("hooks", []):
        cmd = h.get("command", "")
        if "codex-hook-adapter.sh" in cmd or "codex-pretool-adapter.py" in cmd:
            print(f"FAIL: Stop hook should stay direct: {cmd}")
            sys.exit(1)
PY
echo "PASS: lifecycle adapters, strict command guards, and direct Stop are separated"

# Exercise the generated commands as Codex does: an adapted PostToolUse hook
# records its adapter parent, then the direct Stop hook from that same parent
# blocks once and stays silent during stop-hook recursion.
HOOK_OWNER_STATE="$TEST_TEMP/codex-hook-owner-pid"
cat > "$TEST_HARNESS3/core/hooks/post-tool-auto-pilot-reinject.sh" <<EOF
#!/usr/bin/env bash
printf '%s' "\$HARNESS_HOOK_OWNER_PID" > "$HOOK_OWNER_STATE"
printf '%s\n' '{"additionalContext":"post complete"}'
EOF
cat > "$TEST_HARNESS3/core/hooks/session-end.sh" <<EOF
#!/usr/bin/env bash
payload="\$(cat)"
if [[ "\$payload" == *'"stop_hook_active":true'* ]]; then
  exit 0
fi
if [[ "\$(cat "$HOOK_OWNER_STATE" 2>/dev/null)" == "\$PPID" ]]; then
  printf '%s\n' '{"decision":"block","reason":"delivery required"}'
fi
EOF
chmod +x "$TEST_HARNESS3/core/hooks/post-tool-auto-pilot-reinject.sh" \
  "$TEST_HARNESS3/core/hooks/session-end.sh"
generated_commands="$(python3 - "$hooks_json" <<'PY'
import json, sys
hooks = json.load(open(sys.argv[1]))["hooks"]
def command(event, script):
    for entry in hooks[event]:
        for hook in entry["hooks"]:
            if script in hook["command"]:
                return hook["command"]
    raise SystemExit(f"missing {event} {script}")
print(json.dumps({
    "post": command("PostToolUse", "post-tool-auto-pilot-reinject.sh"),
    "stop": command("Stop", "session-end.sh"),
}))
PY
 )"
post_command="$(printf '%s' "$generated_commands" | jq -r '.post')"
stop_command="$(printf '%s' "$generated_commands" | jq -r '.stop')"
/bin/zsh -c "exec $post_command" <<< '{}' >/dev/null
stop_output_file="$TEST_TEMP/codex-stop-output.json"
/bin/zsh -c "exec $stop_command" <<< '{}' > "$stop_output_file"
stop_output="$(jq -S -c . < "$stop_output_file")"
[[ "$stop_output" == '{"decision":"block","reason":"delivery required"}' ]] || {
  echo "FAIL: generated direct Stop must block after adapted PostToolUse: $stop_output"; exit 1;
}
stop_recursion_file="$TEST_TEMP/codex-stop-recursion-output.json"
/bin/zsh -c "exec $stop_command" <<< '{"stop_hook_active":true}' > "$stop_recursion_file"
stop_recursion_output="$(cat "$stop_recursion_file")"
[[ -z "$stop_recursion_output" ]] || {
  echo "FAIL: generated direct Stop must be silent during stop_hook_active recursion: $stop_recursion_output"; exit 1;
}
echo "PASS: generated adapted PostToolUse and direct Stop preserve delivery block and recursion silence"

# Bash matcher should be present and gate Bash-only hooks
python3 - "$hooks_json" <<'PY' || exit 1
import json, sys
data = json.load(open(sys.argv[1]))
bash_only = ["pre-bash-irreversible-guard.sh", "pre-bash-gh-auth.sh",
             "pre-bash-pr-gate.sh", "pre-bash-worktree-gate.sh",
             "post-bash-audit.sh", "post-bash-commit-detect.sh"]
for event in ("PreToolUse", "PostToolUse"):
    for entry in data["hooks"].get(event, []):
        cmds = " ".join(h.get("command","") for h in entry.get("hooks", []))
        if any(b in cmds for b in bash_only):
            m = entry.get("matcher", "")
            if "Bash" not in m:
                print(f"FAIL: {event} entry with bash-specific hook lacks Bash matcher: {entry}")
                sys.exit(1)
PY
echo "PASS: Bash-specific hooks gated by Bash matcher"

# Idempotent: re-running should not change hooks.json mtime
mtime_h_before=$(stat -f %m "$hooks_json" 2>/dev/null || stat -c %Y "$hooks_json")
sleep 1.1
"$PREPARE" "$TEST_HARNESS3"
mtime_h_after=$(stat -f %m "$hooks_json" 2>/dev/null || stat -c %Y "$hooks_json")
[[ "$mtime_h_before" == "$mtime_h_after" ]] || {
  echo "FAIL: hooks.json mtime changed on no-op re-run"; exit 1;
}
echo "PASS: hooks.json idempotent on no-op re-run"
fi

if [[ "$TEST_GROUP" == "generated" ]]; then
# Subagents: .claude/agents/*.md should be converted to $CODEX_HOME/agents/*.toml
# with model mapping (haiku→Luna+low, sonnet→Terra+medium, opus→Sol+medium)
# and sandbox derived from tools/disallowedTools.
TEST_HARNESS4="$TEST_TEMP/fake-harness-agents"
mkdir -p "$TEST_HARNESS4/.claude/agents"
echo "# rules" > "$TEST_HARNESS4/CLAUDE.md"
cat > "$TEST_HARNESS4/.claude/agents/explorer.md" <<'EOF'
---
name: explorer
model: haiku
description: >
  Fast codebase search and analysis. Read-only investigation.
tools: Read, Glob, Grep
disallowedTools: Write, Edit, Bash, Agent
---

## Role

Quickly search and analyze codebases.
EOF
cat > "$TEST_HARNESS4/.claude/agents/reviewer.md" <<'EOF'
---
name: reviewer
model: opus
description: >
  Code review specialist. Read-only review.
tools: Read, Glob, Grep, Agent
disallowedTools: Write, Edit, Bash
---

## Role

Review changed code read-only.
EOF
cat > "$TEST_HARNESS4/.claude/agents/implementer.md" <<'EOF'
---
name: implementer
model: sonnet
description: >
  TDD implementation specialist.
tools: Read, Glob, Grep, Edit, Write, Bash, Agent
---

## Role

Implement code changes with strict TDD.
EOF
# _index.md should be ignored
echo "# index — not an agent" > "$TEST_HARNESS4/.claude/agents/_index.md"

HARNESS_CODEX_CONTEXT=1m "$PREPARE" "$TEST_HARNESS4"
agents_out="$TEST_HARNESS4/.harness/codex/agents"

[[ -d "$agents_out" ]] || { echo "FAIL: agents output dir missing"; exit 1; }
echo "PASS: agents output directory created"

[[ -f "$agents_out/explorer.toml" ]] || { echo "FAIL: explorer.toml missing"; exit 1; }
[[ -f "$agents_out/reviewer.toml" ]] || { echo "FAIL: reviewer.toml missing"; exit 1; }
[[ -f "$agents_out/implementer.toml" ]] || { echo "FAIL: implementer.toml missing"; exit 1; }
[[ -f "$agents_out/_index.toml" ]] && { echo "FAIL: _index.md should be skipped"; exit 1; }
echo "PASS: 3 agents converted, _index.md skipped"

# An explicit 1M main-session request must not expand launcher-generated
# subagents. They use the routine 272K cost guardrail independently.
grep -q '^model_context_window = 1000000$' "$TEST_HARNESS4/.harness/codex/config.toml" || {
  echo "FAIL: 1M main config missing while testing subagent context cap"; exit 1;
}
for agent_toml in "$agents_out"/*.toml; do
  grep -q '^model_context_window = 272000$' "$agent_toml" || {
    echo "FAIL: generated subagent must cap context at 272000"; exit 1;
  }
  grep -q '^model_auto_compact_token_limit = 217600$' "$agent_toml" || {
    echo "FAIL: generated subagent must compact at 217600"; exit 1;
  }
done
echo "PASS: 1M main config keeps generated subagents at 272K with 80% compact"

# explorer: haiku → Luna+low, read-only sandbox
grep -q '^name = "explorer"' "$agents_out/explorer.toml" || { echo "FAIL: explorer name"; exit 1; }
grep -q '^model = "gpt-6-luna"' "$agents_out/explorer.toml" || { echo "FAIL: explorer model"; exit 1; }
grep -q '^model_reasoning_effort = "low"' "$agents_out/explorer.toml" || { echo "FAIL: explorer effort"; exit 1; }
grep -q '^sandbox_mode = "read-only"' "$agents_out/explorer.toml" || { echo "FAIL: explorer sandbox"; exit 1; }
grep -q '^developer_instructions = """' "$agents_out/explorer.toml" || { echo "FAIL: explorer developer_instructions"; exit 1; }
grep -q 'Quickly search and analyze' "$agents_out/explorer.toml" || { echo "FAIL: explorer body content"; exit 1; }
echo "PASS: explorer (haiku) → Luna + low + read-only"

# reviewer: opus → Sol+medium, read-only sandbox
grep -q '^model = "gpt-6.1-sol"' "$agents_out/reviewer.toml" || { echo "FAIL: reviewer model"; exit 1; }
grep -q '^model_reasoning_effort = "medium"' "$agents_out/reviewer.toml" || { echo "FAIL: reviewer effort"; exit 1; }
grep -q '^sandbox_mode = "read-only"' "$agents_out/reviewer.toml" || { echo "FAIL: reviewer sandbox"; exit 1; }
echo "PASS: reviewer (opus) → Sol + medium + read-only"

# implementer: sonnet → Terra+medium, workspace-write sandbox
grep -q '^model = "gpt-5.6-terra"' "$agents_out/implementer.toml" || { echo "FAIL: implementer model"; exit 1; }
grep -q '^model_reasoning_effort = "medium"' "$agents_out/implementer.toml" || { echo "FAIL: implementer effort"; exit 1; }
grep -q '^sandbox_mode = "workspace-write"' "$agents_out/implementer.toml" || { echo "FAIL: implementer sandbox"; exit 1; }
echo "PASS: implementer (sonnet) → Terra + medium + workspace-write"

# Idempotent
mtime_a_before=$(stat -f %m "$agents_out/explorer.toml" 2>/dev/null || stat -c %Y "$agents_out/explorer.toml")
sleep 1.1
HARNESS_CODEX_CONTEXT=1m "$PREPARE" "$TEST_HARNESS4"
mtime_a_after=$(stat -f %m "$agents_out/explorer.toml" 2>/dev/null || stat -c %Y "$agents_out/explorer.toml")
[[ "$mtime_a_before" == "$mtime_a_after" ]] || {
  echo "FAIL: agents toml mtime changed on no-op re-run"; exit 1;
}
echo "PASS: agents idempotent on no-op re-run"

# Removing source agent should drop the generated toml
rm "$TEST_HARNESS4/.claude/agents/reviewer.md"
HARNESS_CODEX_CONTEXT=1m "$PREPARE" "$TEST_HARNESS4"
[[ ! -f "$agents_out/reviewer.toml" ]] || { echo "FAIL: stale reviewer.toml not removed"; exit 1; }
[[ -f "$agents_out/explorer.toml" ]] || { echo "FAIL: explorer.toml should remain"; exit 1; }
echo "PASS: removing source agent drops generated toml"

# AGENTS.md generation when .claude/rules/* exist: concatenate rule contents
# into $CODEX_HOME/AGENTS.md as a real generated file (not symlink). Codex's
# global-scope discovery loads it; the harness-root AGENTS.md → CLAUDE.md
# symlink continues to feed the project-scope walk.
TEST_HARNESS_R="$TEST_TEMP/fake-harness-rules"
mkdir -p "$TEST_HARNESS_R/.claude/rules"
echo "# CLAUDE rules" > "$TEST_HARNESS_R/CLAUDE.md"
cat > "$TEST_HARNESS_R/.claude/rules/cascading-updates.md" <<'EOF'
# Cascading Updates

When Edit/Write to core/hooks/foo.sh, also touch core/hooks/test/test-foo.sh.
EOF
cat > "$TEST_HARNESS_R/.claude/rules/_index.md" <<'EOF'
# Rules Index — should be skipped
EOF

"$PREPARE" "$TEST_HARNESS_R"
agents_file="$TEST_HARNESS_R/.harness/codex/AGENTS.md"

[[ -f "$agents_file" ]] || { echo "FAIL: rules-AGENTS.md not generated"; exit 1; }
[[ -L "$agents_file" ]] && { echo "FAIL: rules-AGENTS.md should be a real file, not symlink"; exit 1; }
grep -q "Generated by codex-home-prepare.sh" "$agents_file" || {
  echo "FAIL: rules-AGENTS.md missing generator header"; exit 1;
}
grep -q "When Edit/Write to core/hooks" "$agents_file" || {
  echo "FAIL: rules-AGENTS.md missing rule body"; exit 1;
}
grep -q "Default to Korean for conversational responses" "$agents_file" || {
  echo "FAIL: rules-AGENTS.md missing Codex response language supplement"; exit 1;
}
lang_block_count=$(grep -c "BEGIN HARNESS-CODEX-RESPONSE-LANGUAGE" "$agents_file")
[[ "$lang_block_count" == "1" ]] || { echo "FAIL: rules-AGENTS.md should contain exactly one managed language block, got $lang_block_count"; exit 1; }
if grep -q "Rules Index" "$agents_file"; then
  echo "FAIL: rules-AGENTS.md should skip _index.md"; exit 1;
fi
echo "PASS: AGENTS.md generated from .claude/rules/* (skips _index.md)"

# Idempotent
mtime_b=$(stat -f %m "$agents_file" 2>/dev/null || stat -c %Y "$agents_file")
sleep 1.1
"$PREPARE" "$TEST_HARNESS_R"
mtime_a=$(stat -f %m "$agents_file" 2>/dev/null || stat -c %Y "$agents_file")
[[ "$mtime_b" == "$mtime_a" ]] || { echo "FAIL: AGENTS.md mtime changed on no-op re-run"; exit 1; }
echo "PASS: rules-AGENTS.md idempotent"

# Adding a new rule must show up in regenerated AGENTS.md
cat > "$TEST_HARNESS_R/.claude/rules/budget.md" <<'EOF'
# Budget Rule

CLAUDE.md ≤ 60 lines.
EOF
"$PREPARE" "$TEST_HARNESS_R"
grep -q "CLAUDE.md ≤ 60 lines" "$agents_file" || {
  echo "FAIL: new rule not picked up on regen"; exit 1;
}
echo "PASS: new rule auto-picked-up on regen"

# Compiler integration: when a harness has canonical .claude/source files and
# core/scripts/harness_compile.py, codex-home-prepare must delegate AGENTS.md
# generation to the compiler so the Codex runtime gets the XML contract first.
TEST_HARNESS_COMP="$TEST_TEMP/fake-harness-compiler"
mkdir -p "$TEST_HARNESS_COMP/.claude/source" \
         "$TEST_HARNESS_COMP/.claude/rules" \
         "$TEST_HARNESS_COMP/core/scripts"
echo "# compiler harness" > "$TEST_HARNESS_COMP/CLAUDE.md"
cp "$LEGACY_COMPILER_FIXTURE" "$TEST_HARNESS_COMP/core/scripts/harness_compile.py"
cat > "$TEST_HARNESS_COMP/.claude/source/runtime-contract.yaml" <<'EOF'
runtime_contract:
  id: harness-runtime-v1
  source_of_truth: Claude harness source is canonical. Runtime surfaces are generated.
  codex_response_language: korean
  forbidden_edits:
    - path: .harness/codex/**
      reason: Generated Codex adapter layer.
  before_work:
    - Read relevant domain knowledge before repo work.
    - Check cascading update rules before editing.
  completion_gate:
    - Run concrete verification before claiming done.
    - Report failed or skipped verification.
EOF
cat > "$TEST_HARNESS_COMP/.claude/source/budgets.yaml" <<'EOF'
budgets:
  claude_md_max_lines: 80
  always_on_total_max_lines: 160
  codex_agents_max_bytes: 8192
  skill_md_max_bytes: 10000
EOF
cat > "$TEST_HARNESS_COMP/.claude/rules/cascading-updates.md" <<'EOF'
# Cascading Updates

compiler cascading rule body
EOF
cat > "$TEST_HARNESS_COMP/.claude/rules/runtime-contract.md" <<'EOF'
stale generated runtime duplicate should be skipped
EOF
cat > "$TEST_HARNESS_COMP/.claude/rules/_index.md" <<'EOF'
compiler index should be skipped
EOF

"$PREPARE" "$TEST_HARNESS_COMP"
compiler_agents="$TEST_HARNESS_COMP/.harness/codex/AGENTS.md"
[[ -f "$compiler_agents" && ! -L "$compiler_agents" ]] || {
  echo "FAIL: compiler AGENTS.md should be real generated file"; exit 1;
}
grep -q "Generated by harness_compile.py" "$compiler_agents" || {
  echo "FAIL: compiler AGENTS.md missing compiler header"; exit 1;
}
grep -q '<runtime_contract id="harness-runtime-v1">' "$compiler_agents" || {
  echo "FAIL: legacy compiler fallback did not preserve XML runtime contract"; exit 1;
}
grep -q "compiler cascading rule body" "$compiler_agents" || {
  echo "FAIL: compiler AGENTS.md missing non-runtime rule body"; exit 1;
}
if grep -q "compiler index should be skipped" "$compiler_agents"; then
  echo "FAIL: compiler AGENTS.md should skip _index.md"; exit 1;
fi
if grep -q "stale generated runtime duplicate" "$compiler_agents"; then
  echo "FAIL: compiler AGENTS.md should skip generated runtime-contract.md rule"; exit 1;
fi
runtime_count=$(grep -c '<runtime_contract id=' "$compiler_agents")
[[ "$runtime_count" == "1" ]] || {
  echo "FAIL: legacy compiler fallback should include runtime contract once, got $runtime_count"; exit 1;
}
grep -q "Default to Korean for conversational responses" "$compiler_agents" || {
  echo "FAIL: compiler AGENTS.md missing Codex response language supplement"; exit 1;
}
lang_block_count=$(grep -c "BEGIN HARNESS-CODEX-RESPONSE-LANGUAGE" "$compiler_agents")
[[ "$lang_block_count" == "1" ]] || { echo "FAIL: compiler AGENTS.md should contain exactly one managed language block, got $lang_block_count"; exit 1; }
echo "PASS: legacy compiler fallback preserves full AGENTS.md generation"

# A compiler that supports the project-aware route must be selected directly,
# leaving the root-owned runtime contract out of CODEX_HOME/AGENTS.md.
TEST_HARNESS_COMP_NEW="$TEST_TEMP/fake-harness-compiler-new"
mkdir -p "$TEST_HARNESS_COMP_NEW/.claude/source" "$TEST_HARNESS_COMP_NEW/core/scripts"
echo "# new compiler harness" > "$TEST_HARNESS_COMP_NEW/CLAUDE.md"
echo "runtime_contract: {}" > "$TEST_HARNESS_COMP_NEW/.claude/source/runtime-contract.yaml"
NEW_COMPILER_LOG="$TEST_TEMP/new-compiler.log"
cat > "$TEST_HARNESS_COMP_NEW/core/scripts/harness_compile.py" <<'PY'
#!/usr/bin/env python3
import os
import sys
from pathlib import Path

Path(os.environ["NEW_COMPILER_LOG"]).open("a").write(sys.argv[1] + "\n")
if sys.argv[1] != "--write-codex-project":
    raise SystemExit(2)
agents = Path(sys.argv[2]) / ".harness" / "codex" / "AGENTS.md"
agents.parent.mkdir(parents=True, exist_ok=True)
agents.write_text("# Generated by harness_compile.py\n\n# Cascading Updates\n\nnew route rule body\n")
PY
chmod +x "$TEST_HARNESS_COMP_NEW/core/scripts/harness_compile.py"
NEW_COMPILER_LOG="$NEW_COMPILER_LOG" "$PREPARE" "$TEST_HARNESS_COMP_NEW"
new_compiler_agents="$TEST_HARNESS_COMP_NEW/.harness/codex/AGENTS.md"
[[ "$(<"$NEW_COMPILER_LOG")" == "--write-codex-project" ]] || {
  echo "FAIL: supported compiler did not use project-aware route"; exit 1;
}
grep -q "new route rule body" "$new_compiler_agents" && ! grep -q '<runtime_contract id=' "$new_compiler_agents" || {
  echo "FAIL: project-aware compiler output retained runtime contract"; exit 1;
}
echo "PASS: supported compiler uses project-aware route without duplicate contract"

# A launcher newer than the harness must fall back to the full legacy route
# when the compiler does not recognize --write-codex-project.
TEST_HARNESS_COMP_OLD="$TEST_TEMP/fake-harness-compiler-old"
mkdir -p "$TEST_HARNESS_COMP_OLD/.claude/source" "$TEST_HARNESS_COMP_OLD/core/scripts"
echo "# old compiler harness" > "$TEST_HARNESS_COMP_OLD/CLAUDE.md"
echo "runtime_contract: {}" > "$TEST_HARNESS_COMP_OLD/.claude/source/runtime-contract.yaml"
OLD_COMPILER_LOG="$TEST_TEMP/old-compiler.log"
cat > "$TEST_HARNESS_COMP_OLD/core/scripts/harness_compile.py" <<'PY'
#!/usr/bin/env python3
import os
import sys
from pathlib import Path

Path(os.environ["OLD_COMPILER_LOG"]).open("a").write(sys.argv[1] + "\n")
if sys.argv[1] != "--write-codex":
    raise SystemExit(2)
agents = Path(sys.argv[2]) / ".harness" / "codex" / "AGENTS.md"
agents.parent.mkdir(parents=True, exist_ok=True)
agents.write_text("# Generated by old harness compiler\n<runtime_contract id=\"harness-runtime-v1\">\n  <source_of_truth>legacy contract remains complete</source_of_truth>\n</runtime_contract>\n")
PY
chmod +x "$TEST_HARNESS_COMP_OLD/core/scripts/harness_compile.py"
OLD_COMPILER_LOG="$OLD_COMPILER_LOG" "$PREPARE" "$TEST_HARNESS_COMP_OLD"
old_compiler_agents="$TEST_HARNESS_COMP_OLD/.harness/codex/AGENTS.md"
grep -qx -- '--write-codex-project' "$OLD_COMPILER_LOG" && grep -qx -- '--write-codex' "$OLD_COMPILER_LOG" || {
  echo "FAIL: old compiler did not receive project route then legacy fallback"; exit 1;
}
grep -q '<runtime_contract id="harness-runtime-v1">' "$old_compiler_agents" || {
  echo "FAIL: legacy compiler fallback did not preserve full runtime contract"; exit 1;
}
echo "PASS: unsupported project compiler route falls back to full legacy contract"

# Migration from legacy symlink layout: if AGENTS.md exists as symlink AND
# rules now exist, we must replace it with the generated file.
TEST_HARNESS_M="$TEST_TEMP/fake-harness-migrate"
mkdir -p "$TEST_HARNESS_M/.harness/codex" "$TEST_HARNESS_M/.claude/rules"
echo "# rules" > "$TEST_HARNESS_M/CLAUDE.md"
ln -s "../../CLAUDE.md" "$TEST_HARNESS_M/.harness/codex/AGENTS.md"
echo "# fresh rule" > "$TEST_HARNESS_M/.claude/rules/r1.md"
[[ -L "$TEST_HARNESS_M/.harness/codex/AGENTS.md" ]] || { echo "FAIL: migration setup precondition"; exit 1; }
"$PREPARE" "$TEST_HARNESS_M"
[[ ! -L "$TEST_HARNESS_M/.harness/codex/AGENTS.md" ]] || {
  echo "FAIL: legacy symlink not replaced on migration"; exit 1;
}
grep -q "fresh rule" "$TEST_HARNESS_M/.harness/codex/AGENTS.md" || {
  echo "FAIL: migrated AGENTS.md missing rule content"; exit 1;
}
echo "PASS: legacy AGENTS.md symlink migrates to generated file"

# Commands → Codex skills: portable orchestration commands (no Claude-state
# dependency) become skills under $CODEX_HOME/skills/ with explicit-invocation
# policy. Claude-coupled commands (auto-pilot, cost-analysis, ralph) are
# skipped so we don't ship dead config.
TEST_HARNESS_C="$TEST_TEMP/fake-harness-cmds"
mkdir -p "$TEST_HARNESS_C/.claude/commands"
echo "# rules" > "$TEST_HARNESS_C/CLAUDE.md"

# Portable: pure orchestration, no Claude refs
cat > "$TEST_HARNESS_C/.claude/commands/daily-pipeline.md" <<'EOF'
---
description: Run daily workflow in sequence
---
# Daily Pipeline

Run daily-sync → worklog → slack-format.

<command_contract>
  <completion_gate>Run daily workflow only after prerequisites pass.</completion_gate>
</command_contract>
EOF

# Skipped: references CLAUDE_PROJECT_DIR (Claude-coupled)
cat > "$TEST_HARNESS_C/.claude/commands/auto-pilot.md" <<'EOF'
---
description: Toggle auto-pilot
---
Read $CLAUDE_PROJECT_DIR/core/bin/auto-pilot.sh
Marker: ~/.claude/.auto-pilot-active-$PPID
EOF

# Skipped: invokes Claude Skill tool
cat > "$TEST_HARNESS_C/.claude/commands/ralph.md" <<'EOF'
---
description: Ralph wrapper
---
Skill(skill="ralph-loop:ralph-loop", args="...")
EOF

# Skipped: _index
cat > "$TEST_HARNESS_C/.claude/commands/_index.md" <<'EOF'
# Commands index
EOF

"$PREPARE" "$TEST_HARNESS_C"
skills_out="$TEST_HARNESS_C/.harness/codex/skills"

[[ -d "$skills_out/daily-pipeline" ]] || { echo "FAIL: portable command not converted to skill"; exit 1; }
[[ -f "$skills_out/daily-pipeline/SKILL.md" ]] || { echo "FAIL: SKILL.md missing for daily-pipeline"; exit 1; }
grep -q "^name: daily-pipeline" "$skills_out/daily-pipeline/SKILL.md" || { echo "FAIL: SKILL.md frontmatter name"; exit 1; }
grep -q "Run daily-sync" "$skills_out/daily-pipeline/SKILL.md" || { echo "FAIL: SKILL.md body content"; exit 1; }
grep -q "<execution_policy>" "$skills_out/daily-pipeline/SKILL.md" || { echo "FAIL: command contract was not converted to skill execution policy"; exit 1; }
if grep -q "<command_contract>" "$skills_out/daily-pipeline/SKILL.md"; then
  echo "FAIL: command contract leaked into generated skill"; exit 1;
fi
echo "PASS: portable command → SKILL.md generated"

# Explicit-only invocation policy
[[ -f "$skills_out/daily-pipeline/agents/openai.yaml" ]] || { echo "FAIL: agents/openai.yaml missing"; exit 1; }
grep -q "allow_implicit_invocation: false" "$skills_out/daily-pipeline/agents/openai.yaml" || {
  echo "FAIL: explicit-invocation policy missing"; exit 1;
}
echo "PASS: command-skill marked explicit-invocation only"

# Claude-coupled commands must NOT be converted
[[ ! -d "$skills_out/auto-pilot" ]] || { echo "FAIL: Claude-coupled auto-pilot was wrongly converted"; exit 1; }
[[ ! -d "$skills_out/ralph" ]] || { echo "FAIL: Claude-coupled ralph was wrongly converted"; exit 1; }
[[ ! -d "$skills_out/_index" ]] || { echo "FAIL: _index.md should be skipped"; exit 1; }
echo "PASS: Claude-coupled commands skipped (auto-pilot, ralph, _index)"

# Removing source command must drop generated skill
rm "$TEST_HARNESS_C/.claude/commands/daily-pipeline.md"
"$PREPARE" "$TEST_HARNESS_C"
[[ ! -d "$skills_out/daily-pipeline" ]] || { echo "FAIL: removed command's skill not cleaned up"; exit 1; }
echo "PASS: removing source command drops generated skill"
fi

if [[ "$TEST_GROUP" == "config-skills" ]]; then
# ---------------------------------------------------------------------------
# Claude plugin cache merge: ~/.claude/plugins/cache/<mp>/<plugin>/<version>/
# skills must be linked into $CODEX_HOME/skills, newest version auto-resolved.
# ---------------------------------------------------------------------------
CPLUG="$FAKE_HOME/.claude/plugins/cache"

# Single-skill plugin: SKILL.md at version root. Two versions present; newest wins.
mkdir -p "$CPLUG/vid-mp/watchx/0.9.0" "$CPLUG/vid-mp/watchx/0.10.0"
cat > "$CPLUG/vid-mp/watchx/0.9.0/SKILL.md" <<'EOF'
---
name: watchx
description: old
---
old body
EOF
cat > "$CPLUG/vid-mp/watchx/0.10.0/SKILL.md" <<'EOF'
---
name: watchx
description: new
---
new body
EOF

# Multi-skill plugin: skills/<name>/SKILL.md under the version dir.
mkdir -p "$CPLUG/multi-mp/bundle/2.0.0/skills/alpha" \
         "$CPLUG/multi-mp/bundle/2.0.0/skills/beta"
cat > "$CPLUG/multi-mp/bundle/2.0.0/skills/alpha/SKILL.md" <<'EOF'
---
name: alpha
description: a
---
a
EOF
cat > "$CPLUG/multi-mp/bundle/2.0.0/skills/beta/SKILL.md" <<'EOF'
---
name: beta
description: b
---
b
EOF

# Stale version-pin self-heal regression: a dangling manual link in ~/.codex/skills
# pointing at a now-missing version must not survive as a broken link; the engine
# must produce a working link to the current version instead.
mkdir -p "$CPLUG/pin-mp/pinned/1.2.0"
cat > "$CPLUG/pin-mp/pinned/1.2.0/SKILL.md" <<'EOF'
---
name: pinned
description: p
---
p
EOF
mkdir -p "$FAKE_HOME/.codex/skills"
ln -sfn "$CPLUG/pin-mp/pinned/1.0.0" "$FAKE_HOME/.codex/skills/pinned"  # dangling (1.0.0 absent)

HOME="$FAKE_HOME" HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$FAKE_HOME/.codex/.tmp/bundled-marketplaces/openai-bundled" "$PREPARE" "$FAKE_HARNESS_S"

# 1. Single-skill plugin linked, resolving to the NEWEST version (0.10.0 > 0.9.0).
[[ -L "$SKILLS_OUT/watchx" ]] || { echo "FAIL: watchx symlink missing"; exit 1; }
[[ -f "$SKILLS_OUT/watchx/SKILL.md" ]] || { echo "FAIL: watchx resolves to file"; exit 1; }
grep -q "new body" "$SKILLS_OUT/watchx/SKILL.md" || { echo "FAIL: watchx not pointing at newest version (sort -V)"; exit 1; }
echo "PASS: Claude-plugin single-skill merged at newest version"

# 2. Multi-skill plugin: both skills linked.
[[ -f "$SKILLS_OUT/alpha/SKILL.md" ]] || { echo "FAIL: multi-skill alpha not linked"; exit 1; }
[[ -f "$SKILLS_OUT/beta/SKILL.md" ]] || { echo "FAIL: multi-skill beta not linked"; exit 1; }
echo "PASS: Claude-plugin multi-skill merged"

# 3. Stale manual pin self-heals: pinned resolves to a real SKILL.md (current version).
[[ -f "$SKILLS_OUT/pinned/SKILL.md" ]] || { echo "FAIL: stale version pin did not self-heal"; exit 1; }
echo "PASS: stale Claude-plugin version pin self-healed"
fi

if [[ "$TEST_GROUP" == "generated" ]]; then
# ---------------------------------------------------------------------------
# nounset regression: prep runs under `set -euo pipefail`. On bash 3.2 (macOS
# system /bin/bash), a bare "${managed_names[@]}" expansion errors with
# "unbound variable" when the array is still empty. Run prep via /bin/bash
# against a harness that has NO skill sources, so record_managed_skill's loop
# is first reached with an empty array. Must not abort.
# ---------------------------------------------------------------------------
if [[ -x /bin/bash ]] && /bin/bash -uc 'a=(); for _ in "${a[@]}"; do :; done' 2>/dev/null; then
  echo "SKIP: /bin/bash does not exhibit empty-array nounset behavior; regression not applicable here"
else
  NOUNSET_HARNESS="$TEST_TEMP/fake-harness-nounset"
  NOUNSET_HOME="$TEST_TEMP/fake-home-nounset"
  mkdir -p "$NOUNSET_HARNESS" "$NOUNSET_HOME/.codex/skills/probe-skill" "$NOUNSET_HOME/.claude"
  echo "# rules" > "$NOUNSET_HARNESS/CLAUDE.md"
  # Exactly one skill source so record_managed_skill is reached while managed_names
  # is still empty — that first record is where bash 3.2 nounset trips on "${arr[@]}".
  printf -- '---\nname: probe-skill\ndescription: x\n---\nbody\n' \
    > "$NOUNSET_HOME/.codex/skills/probe-skill/SKILL.md"
  if HOME="$NOUNSET_HOME" /bin/bash "$PREPARE" "$NOUNSET_HARNESS" >/dev/null 2>"$TEST_TEMP/nounset.err"; then
    echo "PASS: prep survives empty managed_names under bash 3.2 nounset"
  else
    echo "FAIL: prep aborted under bash 3.2 nounset (empty managed_names):"
    cat "$TEST_TEMP/nounset.err" >&2
    exit 1
  fi
fi

fi

echo "✓ codex-home-prepare $TEST_GROUP group passed"
