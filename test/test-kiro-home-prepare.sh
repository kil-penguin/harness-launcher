#!/usr/bin/env zsh
# test-kiro-home-prepare.sh — verify kiro-home-prepare.sh side effects.
#
# Regression guard: the harness agent MUST carry the merged MCP servers inline.
# A prior bug generated harness.json with "mcpServers": {} + useLegacyMcpJson:
# false, so `kiro-cli chat --agent harness` saw zero MCP servers even though
# settings/mcp.json was populated correctly.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCHER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PREPARE="$LAUNCHER_DIR/bin/kiro-home-prepare.sh"

cleanup() {
  [[ -n "${TEST_TEMP:-}" && -d "$TEST_TEMP" ]] && rm -rf "$TEST_TEMP"
}
trap cleanup EXIT

[[ -x "$PREPARE" ]] || { echo "FAIL: $PREPARE missing or not executable"; exit 1; }

TEST_TEMP="$(mktemp -d)"
TEST_HARNESS="$TEST_TEMP/fake-harness"
mkdir -p "$TEST_HARNESS/core/hooks"

cat > "$TEST_HARNESS/CLAUDE.md" <<'EOF'
# Fake harness rules
EOF

# Subagent defs: one read-only haiku, one workspace-write sonnet, plus _index
# (must be skipped). Tier resolution reads the launcher's real
# subagent-model-map.tsv, so these assert the actual shipped mapping.
mkdir -p "$TEST_HARNESS/.claude/agents"
cat > "$TEST_HARNESS/.claude/agents/_index.md" <<'EOF'
# index — must be skipped
EOF
cat > "$TEST_HARNESS/.claude/agents/scout.md" <<'EOF'
---
name: scout
description: read-only search agent
model: haiku
tools: Read, Glob, Grep
---
Scout body.
EOF
cat > "$TEST_HARNESS/.claude/agents/builder.md" <<'EOF'
---
name: builder
description: TDD implementer
model: sonnet
tools: Read, Edit, Write, Bash
---
Builder body.
EOF
cat > "$TEST_HARNESS/.claude/agents/judge.md" <<'EOF'
---
name: judge
description: review agent
model: opus
tools: Read, Glob, Grep
---
Judge body.
EOF

# Committed servers (shared)
cat > "$TEST_HARNESS/.mcp.json" <<'EOF'
{
  "mcpServers": {
    "atlassian": { "type": "http", "url": "http://localhost:38100/mcp" },
    "context7": { "command": "npx", "args": ["-y", "@upstash/context7-mcp"] }
  }
}
EOF

# First local overlay is loaded alongside the committed config.
cat > "$TEST_HARNESS/.mcp.local.json" <<'EOF'
{
  "mcpServers": {
    "local-docs": { "command": "npx", "args": ["-y", "@example/docs-mcp"] }
  }
}
EOF

# Second local overlay is also loaded.
cat > "$TEST_HARNESS/mcp.local.json" <<'EOF'
{
  "mcpServers": {
    "harness-rag": { "command": "ssh", "args": ["host", "serve-mcp"] }
  }
}
EOF

# Kiro-only overlay: loaded by Kiro preparation and by no other runtime, so a
# server that exists for one runtime does not have to be hidden per runtime
# elsewhere.
cat > "$TEST_HARNESS/mcp.kiro.local.json" <<'EOF'
{
  "mcpServers": {
    "kiro-only-read": { "command": "/bin/echo", "args": ["kiro-only"] }
  }
}
EOF

"$PREPARE" "$TEST_HARNESS"

KIRO_HOME="$TEST_HARNESS/.harness/kiro"
FAIL=0

# ─── settings output ──────────────────────────────────────────────────────────
cli_settings="$KIRO_HOME/settings/cli.json"
[[ -f "$cli_settings" ]] || { echo "FAIL: settings/cli.json missing"; exit 1; }

# ─── settings/mcp.json: merged + type field ──────────────────────────────────
mcp_settings="$KIRO_HOME/settings/mcp.json"
[[ -f "$mcp_settings" ]] || { echo "FAIL: settings/mcp.json missing"; exit 1; }
settings_count=$(python3 -c "import json;print(len(json.load(open('$mcp_settings'))['mcpServers']))")
[[ "$settings_count" == "5" ]] || { echo "FAIL: settings/mcp.json should merge 5 servers, got $settings_count"; FAIL=1; }
# stdio type inferred for command-only entries
python3 -c "import json,sys; d=json.load(open('$mcp_settings'))['mcpServers']; sys.exit(0 if d['harness-rag'].get('type')=='stdio' else 1)" \
  || { echo "FAIL: harness-rag should get type=stdio"; FAIL=1; }
echo "PASS: settings/mcp.json merges committed + local servers with type field"

# ─── Kiro-only overlay (mcp.kiro.local.json) ─────────────────────────────────
python3 -c "import json,sys; d=json.load(open('$mcp_settings'))['mcpServers']; sys.exit(0 if 'kiro-only-read' in d else 1)" \
  || { echo "FAIL: mcp.kiro.local.json server missing from the Kiro surface"; FAIL=1; }

# The overlay must stay invisible to the Claude surface generated from the same
# harness directory, on both Claude paths: the --mcp-config argument list and
# the generated light surface.
source "$LAUNCHER_DIR/bin/harness-common.sh"
if harness_mcp_local_configs "$TEST_HARNESS" | grep -q 'mcp\.kiro\.local\.json'; then
  echo "FAIL: Kiro-only overlay was offered to Claude as an --mcp-config input"
  FAIL=1
fi
if light_file="$(harness_claude_light_mcp_config "$TEST_HARNESS" "$LAUNCHER_DIR/bin")"; then
  python3 - "$light_file" <<'PY' || FAIL=1
import json, sys
servers = json.load(open(sys.argv[1])).get("mcpServers", {})
ok = True
if "kiro-only-read" in servers:
    print("FAIL: Kiro-only overlay leaked into the Claude MCP surface"); ok = False
if "atlassian" not in servers:
    print("FAIL: Claude surface lost the committed servers"); ok = False
sys.exit(0 if ok else 1)
PY
else
  echo "FAIL: harness_claude_light_mcp_config failed on a harness with a Kiro overlay"
  FAIL=1
fi

# Structural guard for every other runtime: only the Kiro preparation path may
# read the overlay filename.
other_readers=$(grep -rl 'mcp\.kiro\.local\.json' "$LAUNCHER_DIR/bin" 2>/dev/null | grep -v 'kiro-home-prepare\.sh' || true)
[[ -z "$other_readers" ]] || {
  echo "FAIL: Kiro-only overlay is read outside kiro-home-prepare.sh: $other_readers"
  FAIL=1
}
echo "PASS: Kiro-only overlay reaches Kiro and no other runtime"

# ─── agents/harness.json: MCP inlined (regression) ───────────────────────────
agent="$KIRO_HOME/agents/harness.json"
[[ -f "$agent" ]] || { echo "FAIL: agents/harness.json missing"; exit 1; }

agent_count=$(python3 -c "import json;print(len(json.load(open('$agent')).get('mcpServers',{})))")
[[ "$agent_count" == "5" ]] || {
  echo "FAIL: harness agent must inline all 5 merged MCP servers, got $agent_count"
  echo "      (empty mcpServers + useLegacyMcpJson:false = agent sees zero MCP)"
  FAIL=1
}

# The inlined set must equal the merged settings set exactly
python3 - "$agent" "$mcp_settings" <<'PY' || FAIL=1
import json, sys
agent = json.load(open(sys.argv[1])).get("mcpServers", {})
settings = json.load(open(sys.argv[2])).get("mcpServers", {})
if set(agent) != set(settings):
    print(f"FAIL: agent MCP set {sorted(agent)} != settings set {sorted(settings)}")
    sys.exit(1)
PY
echo "PASS: harness agent inlines the merged MCP servers"

# ─── per-subagent agents/<name>.json (direction A) ───────────────────────────
# _index.md must be skipped; scout/builder must generate with the tier-resolved
# Kiro model ID and read-only vs workspace allowedTools.
[[ ! -e "$KIRO_HOME/agents/_index.json" ]] || { echo "FAIL: _index.md must not generate an agent json"; FAIL=1; }

scout_agent="$KIRO_HOME/agents/scout.json"
builder_agent="$KIRO_HOME/agents/builder.json"
[[ -f "$scout_agent" ]] || { echo "FAIL: agents/scout.json missing"; FAIL=1; }
[[ -f "$builder_agent" ]] || { echo "FAIL: agents/builder.json missing"; FAIL=1; }

python3 - "$scout_agent" "$builder_agent" <<'PY' || FAIL=1
import json, sys
scout = json.load(open(sys.argv[1]))
builder = json.load(open(sys.argv[2]))
ok = True
if scout.get("model") != "claude-haiku-4.5":
    print(f"FAIL: scout (haiku) model={scout.get('model')} != claude-haiku-4.5"); ok = False
if builder.get("model") != "claude-sonnet-5":
    print(f"FAIL: builder (sonnet) model={builder.get('model')} != claude-sonnet-5"); ok = False
# read-only agent: no fs_write; workspace agent: has fs_write
if "fs_write" in scout.get("allowedTools", []):
    print("FAIL: read-only scout must not auto-approve fs_write"); ok = False
if "fs_write" not in builder.get("allowedTools", []):
    print("FAIL: workspace builder must auto-approve fs_write"); ok = False
# subagents must inline MCP like harness.json (delegate parity)
if not scout.get("mcpServers"):
    print("FAIL: scout must inline merged MCP servers"); ok = False
sys.exit(0 if ok else 1)
PY
echo "PASS: per-subagent JSONs carry tier-resolved model + read/write allowedTools"

# ─── opus tier resolves from the SSOT column ─────────────────────────────────
# (tsv ↔ in-script fallback parity is covered by test-subagent-model-map.sh)
judge_agent="$KIRO_HOME/agents/judge.json"
[[ -f "$judge_agent" ]] || { echo "FAIL: agents/judge.json missing"; FAIL=1; }
judge_model=$(python3 -c "import json;print(json.load(open('$judge_agent')).get('model'))" 2>/dev/null)
tsv_opus=$(awk -F'\t' '$1=="opus"{print $4}' "$LAUNCHER_DIR/bin/subagent-model-map.tsv")
[[ -n "$tsv_opus" ]] || { echo "FAIL: subagent-model-map.tsv has no opus row"; FAIL=1; }
[[ "$judge_model" == "$tsv_opus" ]] || {
  echo "FAIL: judge (opus) model=$judge_model != tsv opus column $tsv_opus"; FAIL=1
}
echo "PASS: opus-tier agent resolves to the tsv opus model ($tsv_opus)"

# Idempotent + source-removal reversal: dropping builder.md quarantines its json.
"$PREPARE" "$TEST_HARNESS" >/dev/null
[[ -f "$builder_agent" ]] || { echo "FAIL: builder.json should survive idempotent re-run"; FAIL=1; }
rm "$TEST_HARNESS/.claude/agents/builder.md"
"$PREPARE" "$TEST_HARNESS" >/dev/null
[[ ! -e "$builder_agent" ]] || { echo "FAIL: removing builder.md must drop generated builder.json"; FAIL=1; }
[[ -f "$scout_agent" ]] || { echo "FAIL: scout.json must remain after builder removal"; FAIL=1; }
[[ -f "$KIRO_HOME/agents/harness.json" ]] || { echo "FAIL: harness.json must never be quarantined"; FAIL=1; }
echo "PASS: subagent json idempotent; source removal quarantines only its own file"

# Reserved-name guard: a .claude/agents/harness.md must NOT clobber the
# launcher's own top-level harness.json (section 3). The harness agent is
# identified by inlining all 4 merged MCP servers; a subagent overwrite would
# replace it with a single-agent def.
cp "$KIRO_HOME/agents/harness.json" "$TEST_TEMP/harness-agent.reserved-before.json"
cat > "$TEST_HARNESS/.claude/agents/harness.md" <<'EOF'
---
name: harness
description: adversarial fixture that must not overwrite the launcher agent
model: opus
tools: Read, Edit, Write, Bash
---
Impostor body.
EOF
"$PREPARE" "$TEST_HARNESS" >/dev/null
cmp -s "$TEST_TEMP/harness-agent.reserved-before.json" "$KIRO_HOME/agents/harness.json" || {
  echo "FAIL: a source harness.md overwrote the launcher's own harness.json"
  FAIL=1
}
harness_mcp=$(python3 -c "import json;print(len(json.load(open('$KIRO_HOME/agents/harness.json')).get('mcpServers',{})))")
[[ "$harness_mcp" == "5" ]] || { echo "FAIL: harness.json lost its inlined MCP servers (got $harness_mcp)"; FAIL=1; }
rm "$TEST_HARNESS/.claude/agents/harness.md"
echo "PASS: reserved harness.md source cannot overwrite the launcher harness.json"

# Duplicate names across committed and local inputs must fail instead of letting
# a local machine silently replace a committed endpoint. Existing generated
# settings and agent files must remain byte-identical after rejection. Make the
# CLI settings intentionally stale to prove validation happens before *any*
# generated output is written.
printf '{"stale": true}\n' > "$cli_settings"
cp "$cli_settings" "$TEST_TEMP/cli-settings.before.json"
cp "$mcp_settings" "$TEST_TEMP/mcp-settings.before.json"
cp "$agent" "$TEST_TEMP/harness-agent.before.json"
cat > "$TEST_HARNESS/mcp.local.json" <<'EOF'
{
  "mcpServers": {
    "atlassian": { "command": "echo", "args": ["shadowed"] }
  }
}
EOF
set +e
"$PREPARE" "$TEST_HARNESS" >"$TEST_TEMP/duplicate-mcp.log" 2>&1
duplicate_rc=$?
set -e
[[ "$duplicate_rc" -ne 0 ]] || {
  echo "FAIL: Kiro preparation accepted a duplicate local MCP server name"
  FAIL=1
}
grep -q "duplicate MCP server 'atlassian'" "$TEST_TEMP/duplicate-mcp.log" || {
  echo "FAIL: duplicate Kiro MCP rejection did not identify the server"
  FAIL=1
}
cmp -s "$TEST_TEMP/cli-settings.before.json" "$cli_settings" || {
  echo "FAIL: duplicate rejection changed existing generated CLI settings"
  FAIL=1
}
cmp -s "$TEST_TEMP/mcp-settings.before.json" "$mcp_settings" || {
  echo "FAIL: duplicate rejection changed the existing generated MCP settings"
  FAIL=1
}
cmp -s "$TEST_TEMP/harness-agent.before.json" "$agent" || {
  echo "FAIL: duplicate rejection changed the existing generated agent"
  FAIL=1
}
echo "PASS: Kiro preparation rejects duplicates without changing generated output"

# A fresh harness with duplicate MCP names must not gain a generated Kiro home,
# including hidden temporary files, when validation rejects the configuration.
FRESH_HARNESS="$TEST_TEMP/fresh-duplicate-harness"
mkdir -p "$FRESH_HARNESS"
cat > "$FRESH_HARNESS/.mcp.json" <<'EOF'
{"mcpServers":{"committed":{"command":"echo"}}}
EOF
cat > "$FRESH_HARNESS/mcp.local.json" <<'EOF'
{"mcpServers":{"committed":{"command":"echo","args":["shadowed"]}}}
EOF
set +e
"$PREPARE" "$FRESH_HARNESS" >"$TEST_TEMP/fresh-duplicate-mcp.log" 2>&1
fresh_duplicate_rc=$?
set -e
[[ "$fresh_duplicate_rc" -ne 0 ]] || {
  echo "FAIL: fresh Kiro preparation accepted a duplicate MCP server name"
  FAIL=1
}
grep -q "duplicate MCP server 'committed'" "$TEST_TEMP/fresh-duplicate-mcp.log" || {
  echo "FAIL: fresh duplicate rejection did not identify the server"
  FAIL=1
}
[[ ! -e "$FRESH_HARNESS/.harness" ]] || {
  echo "FAIL: fresh duplicate rejection created generated Kiro state"
  FAIL=1
}
echo "PASS: fresh duplicate rejection creates no generated Kiro state"

# The Kiro-only overlay is subject to the same duplicate rule: it extends the
# committed configuration and must not silently shadow a committed server.
KIRO_DUP_HARNESS="$TEST_TEMP/kiro-overlay-duplicate-harness"
mkdir -p "$KIRO_DUP_HARNESS"
cat > "$KIRO_DUP_HARNESS/.mcp.json" <<'EOF'
{"mcpServers":{"committed":{"command":"echo"}}}
EOF
cat > "$KIRO_DUP_HARNESS/mcp.kiro.local.json" <<'EOF'
{"mcpServers":{"committed":{"command":"echo","args":["shadowed"]}}}
EOF
set +e
"$PREPARE" "$KIRO_DUP_HARNESS" >"$TEST_TEMP/kiro-overlay-duplicate.log" 2>&1
kiro_overlay_duplicate_rc=$?
set -e
[[ "$kiro_overlay_duplicate_rc" -ne 0 ]] || {
  echo "FAIL: Kiro overlay was allowed to shadow a committed MCP server"
  FAIL=1
}
grep -q "duplicate MCP server 'committed'" "$TEST_TEMP/kiro-overlay-duplicate.log" || {
  echo "FAIL: Kiro overlay duplicate rejection did not identify the server"
  FAIL=1
}
[[ ! -e "$KIRO_DUP_HARNESS/.harness" ]] || {
  echo "FAIL: Kiro overlay duplicate rejection created generated Kiro state"
  FAIL=1
}
echo "PASS: Kiro overlay cannot shadow committed MCP servers"

# ─── Authenticated HTTP servers become stdio bridges ────────────────────────
# Kiro ignores `headers` on http transport and falls back to OAuth discovery, so
# preparation rewrites those servers into an stdio bridge that carries them. The
# placeholder must survive verbatim: the bridge resolves it from the inherited
# environment, so no credential is written to disk.
HEADER_HARNESS="$TEST_TEMP/http-bridge-harness"
mkdir -p "$HEADER_HARNESS/.claude/agents"
cat > "$HEADER_HARNESS/.claude/agents/reader.md" <<'EOF'
---
name: reader
description: read-only agent so a subagent JSON is generated
model: haiku
tools: Read, Glob
---
Reader body.
EOF
cat > "$HEADER_HARNESS/.mcp.json" <<'EOF'
{
  "mcpServers": {
    "authed-http": {
      "type": "http",
      "url": "https://example.invalid/mcp",
      "timeout": 45000,
      "headers": {
        "Authorization": "Bearer ${TEST_BRIDGE_TOKEN:-}",
        "X-Bare": "${TEST_BRIDGE_TOKEN}",
        "X-Defaulted": "${TEST_BRIDGE_ABSENT:-committed-default}",
        "X-Unset": "${TEST_BRIDGE_ABSENT}",
        "X-Pair": "${TEST_BRIDGE_TOKEN}/${TEST_BRIDGE_ABSENT:-second}",
        "X-Plain": "literal-value"
      }
    },
    "plain-http": {
      "type": "http",
      "url": "http://127.0.0.1:38100/mcp"
    },
    "already-stdio": { "command": "/bin/echo", "args": ["hi"] }
  }
}
EOF
set +e
TEST_BRIDGE_TOKEN="resolved-secret" "$PREPARE" "$HEADER_HARNESS" \
  >"$TEST_TEMP/http-bridge.out" 2>"$TEST_TEMP/http-bridge.err"
bridge_rc=$?
set -e
[[ "$bridge_rc" -eq 0 ]] || { echo "FAIL: preparation failed on an authenticated http server"; cat "$TEST_TEMP/http-bridge.err"; FAIL=1; }

bridge_settings="$HEADER_HARNESS/.harness/kiro/settings/mcp.json"
python3 - "$bridge_settings" "$HEADER_HARNESS/.harness/kiro/agents/harness.json" <<'PY' || FAIL=1
import json, sys
servers = json.load(open(sys.argv[1]))["mcpServers"]
ok = True
bridged = servers["authed-http"]
if bridged.get("type") != "stdio" or bridged.get("command") != "npx":
    print(f"FAIL: authenticated http server not bridged: {bridged}"); ok = False
if "url" in bridged or "headers" in bridged:
    print(f"FAIL: bridged server kept http-only fields: {sorted(bridged)}"); ok = False
args = bridged.get("args", [])
for expected in ("mcp-remote@latest", "https://example.invalid/mcp",
                 "Authorization: Bearer ${TEST_BRIDGE_TOKEN}", "X-Bare: ${TEST_BRIDGE_TOKEN}",
                 "X-Defaulted: committed-default", "X-Unset: ${TEST_BRIDGE_ABSENT}",
                 "X-Pair: ${TEST_BRIDGE_TOKEN}/second", "X-Plain: literal-value"):
    if expected not in args:
        print(f"FAIL: bridge args missing {expected!r}: {args}"); ok = False
if any(":-" in arg for arg in args):
    print(f"FAIL: mcp-remote cannot substitute the ${{VAR:-}} form; args still contain it: {args}"); ok = False
if bridged.get("timeout") != 45000:
    print(f"FAIL: bridge dropped the configured timeout: {bridged.get('timeout')}"); ok = False
plain = servers["plain-http"]
if plain.get("type") != "http" or plain.get("url") != "http://127.0.0.1:38100/mcp":
    print(f"FAIL: header-free http server must stay untouched: {plain}"); ok = False
if servers["already-stdio"].get("command") != "/bin/echo":
    print(f"FAIL: stdio server must stay untouched: {servers['already-stdio']}"); ok = False
agent_args = json.load(open(sys.argv[2]))["mcpServers"]["authed-http"].get("args", [])
if "Authorization: Bearer ${TEST_BRIDGE_TOKEN}" not in agent_args:
    print(f"FAIL: inlined agent config lost the bridge header: {agent_args}"); ok = False
sys.exit(0 if ok else 1)
PY

# The resolved credential must not reach generated state or the launcher output.
if grep -rq 'resolved-secret' "$HEADER_HARNESS/.harness" "$TEST_TEMP/http-bridge.out" "$TEST_TEMP/http-bridge.err" 2>/dev/null; then
  echo "FAIL: a resolved credential was written to generated state or output"
  FAIL=1
fi
echo "PASS: authenticated http servers become stdio bridges without materializing secrets"

# The light surface filter must still drop an SSH-tunnel server before any
# translation can hide its loopback URL.
LIGHT_HARNESS="$TEST_TEMP/light-bridge-harness"
mkdir -p "$LIGHT_HARNESS"
cat > "$LIGHT_HARNESS/.mcp.json" <<'EOF'
{
  "mcpServers": {
    "tunnel": {
      "type": "http",
      "url": "http://127.0.0.1:38205/mcp",
      "headers": { "Authorization": "Bearer ${TEST_BRIDGE_TOKEN}" }
    },
    "keep": { "command": "/bin/echo", "args": ["hi"] }
  }
}
EOF
HARNESS_KIRO_MCP_PROFILE=light "$PREPARE" "$LIGHT_HARNESS" >/dev/null 2>&1
python3 - "$LIGHT_HARNESS/.harness/kiro/settings/mcp.json" <<'PY' || FAIL=1
import json, sys
servers = json.load(open(sys.argv[1]))["mcpServers"]
if "tunnel" in servers:
    print(f"FAIL: light surface kept an SSH-tunnel server: {sorted(servers)}"); sys.exit(1)
if "keep" not in servers:
    print(f"FAIL: light surface dropped a local server: {sorted(servers)}"); sys.exit(1)
PY
echo "PASS: light surface filters tunnel servers before bridge translation"

# ─── Summary ─────────────────────────────────────────────────────────────────
if [[ $FAIL -gt 0 ]]; then
  echo "Results: FAILED"
  exit 1
fi
echo "✓ All kiro-home-prepare tests passed"
