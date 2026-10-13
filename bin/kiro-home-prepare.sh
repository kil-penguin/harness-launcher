#!/usr/bin/env bash
# kiro-home-prepare.sh — populate $HARNESS_DIR/.harness/kiro/ for Kiro CLI native launch.
#
# Usage: kiro-home-prepare.sh <harness-dir>
#
# Idempotent: re-runs only rewrite files when content would change.

set -euo pipefail

HARNESS_DIR="${1:?HARNESS_DIR required (positional arg 1)}"
[[ -d "$HARNESS_DIR" ]] || { echo "harness dir not found: $HARNESS_DIR" >&2; exit 1; }

KIRO_HOME="$HARNESS_DIR/.harness/kiro"
# Parse and render MCP configuration outside the harness first. A validation
# failure must not create `.harness/kiro` or leave temporary runtime files.
STAGING_DIR="$(mktemp -d "${TMPDIR:-/tmp}/harness-launcher-kiro.XXXXXX")"
cleanup_staging() {
  rm -rf "$STAGING_DIR"
}
trap cleanup_staging EXIT

LAUNCHER_BIN_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$LAUNCHER_BIN_DIR/harness-common.sh"
PYTHON_BIN="$(harness_python3_resolve)" || exit 1
HARNESS_OBSERVABILITY_ACTIVE=0
HARNESS_OBSERVABILITY_PROFILE=""
if harness_observability_load "$HARNESS_DIR"; then
  HARNESS_OBSERVABILITY_ACTIVE=1
else
  observability_status=$?
  [[ "$observability_status" -eq 1 ]] || exit "$observability_status"
fi
KIRO_OBSERVABILITY_HOOK="$LAUNCHER_BIN_DIR/kiro-observability-hook.py"

# Atomic swap helper: only overwrite if content differs
atomic_write() {
  local dest="$1" tmp_file="$2"
  if [[ -f "$dest" ]] && cmp -s "$tmp_file" "$dest"; then
    rm -f "$tmp_file"
  else
    mv "$tmp_file" "$dest"
  fi
}

# ─── 1. settings/mcp.json ───────────────────────────────────────────────────
# Merge committed and local MCP sources into kiro-cli format. Local files extend
# the committed configuration; duplicate server names fail rather than override.
# mcp.kiro.local.json is read here and nowhere else, so a server that only Kiro
# should reach is declared once instead of being suppressed per runtime.

mcp_out="$KIRO_HOME/settings/mcp.json"
tmp_mcp="$STAGING_DIR/mcp.json"

"$PYTHON_BIN" - "$HARNESS_DIR" "$LAUNCHER_BIN_DIR" > "$tmp_mcp" <<'PY'
import json, os, re, sys

harness = sys.argv[1]
sys.path.insert(0, sys.argv[2])
from mcp_paths import normalize_servers
merged = {}
seen = {}

for name in (".mcp.json", ".mcp.local.json", "mcp.local.json", "mcp.kiro.local.json"):
    path = os.path.join(harness, name)
    if not os.path.isfile(path):
        continue
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for srv_name, spec in (data.get("mcpServers") or {}).items():
        if srv_name in seen:
            print(
                f"ERROR: duplicate MCP server '{srv_name}' in {seen[srv_name]} and {path}; "
                "rename the local server instead of overriding committed .mcp.json",
                file=sys.stderr,
            )
            raise SystemExit(1)
        seen[srv_name] = path
        merged[srv_name] = spec

# Light MCP surface: drop SSH-backed servers — stdio start-ssh-mcp.sh wrappers
# and loopback HTTP on the SSH-tunnel port band 38200–38299. Same rule as the
# Claude light surface in harness-common.sh.
def _is_ssh_backed(spec):
    args = spec.get("args") or []
    if spec.get("command") == "bash" and args and "start-ssh-mcp.sh" in str(args[0]):
        return True
    m = re.match(r"https?://(127\.0\.0\.1|localhost):(\d+)(/|$)", str(spec.get("url") or ""))
    return bool(m) and 38200 <= int(m.group(2)) <= 38299

if os.environ.get("HARNESS_KIRO_MCP_PROFILE") == "light":
    merged = {name: spec for name, spec in merged.items() if not _is_ssh_backed(spec)}

try:
    merged = normalize_servers(merged, harness)
except ValueError as error:
    print(f"ERROR: {error}", file=sys.stderr)
    raise SystemExit(1)

# Kiro CLI ignores `headers` for http transport — the upstream agent schema says
# only `url` is taken into account — and then attempts OAuth discovery, so an
# authenticated HTTP MCP fails with "the server does not advertise OAuth
# endpoints" even though Claude and Codex accept the same definition. Translate
# those servers into an equivalent stdio bridge that carries the headers.
#
# The placeholder text is passed through deliberately: mcp-remote resolves
# ${VAR} in a header from its own environment, which the bridge inherits from the
# session. The credential therefore never reaches argv or a generated file.
# mcp-remote understands only the bare ${VAR} form, so the harness idiom
# ${VAR:-default} is normalized first: a variable present in this environment
# becomes ${VAR} for the bridge to substitute, and otherwise the committed
# default is inlined. A variable that is neither set nor defaulted stays ${VAR},
# which the bridge leaves alone so the server fails to authenticate rather than
# silently sending a broken credential.
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

def _bridge_header_value(value):
    def replace(match):
        name, default = match.group(1), match.group(2)
        if os.environ.get(name):
            return "${%s}" % name
        if default:
            return default
        return "${%s}" % name

    return _PLACEHOLDER.sub(replace, value)

def _to_stdio_bridge(spec):
    # Explicit @latest: package-runner MCPs follow the registry rather than a
    # pin, and an explicit tag re-resolves where a bare name may reuse an npx
    # cache entry. The ${VAR} header substitution relied on below was verified
    # on 0.14.2 and 0.14.3.
    bridge = {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "mcp-remote@latest", str(spec.get("url") or "")],
    }
    for header, value in (spec.get("headers") or {}).items():
        rendered = _bridge_header_value(value) if isinstance(value, str) else value
        bridge["args"] += ["--header", f"{header}: {rendered}"]
    for passthrough in ("timeout", "disabled", "env"):
        if passthrough in spec:
            bridge[passthrough] = spec[passthrough]
    return bridge

for srv_name, spec in list(merged.items()):
    is_http = spec.get("type") == "http" or ("url" in spec and "command" not in spec)
    headers = spec.get("headers")
    if is_http and isinstance(headers, dict) and headers:
        merged[srv_name] = _to_stdio_bridge(spec)

# Convert to kiro-cli format: ensure "type" field
output = {}
for name, spec in sorted(merged.items()):
    entry = dict(spec)
    if "type" not in entry:
        if "url" in entry:
            entry["type"] = "http"
        elif "command" in entry:
            entry["type"] = "stdio"
    output[name] = entry

json.dump({"mcpServers": output}, sys.stdout, indent=2)
sys.stdout.write("\n")
PY

# MCP parsing and duplicate validation succeeded. It is now safe to materialize
# the per-harness runtime home and atomically install generated files.
mkdir -p "$KIRO_HOME/settings" "$KIRO_HOME/agents" "$KIRO_HOME/steering" "$KIRO_HOME/skills"
atomic_write "$mcp_out" "$tmp_mcp"

# ─── 2. settings/cli.json ───────────────────────────────────────────────────
# Generate only after MCP validation succeeds, so a rejected local duplicate
# leaves every pre-existing generated file untouched.

cli_json="$KIRO_HOME/settings/cli.json"
tmp_cli="$(mktemp "$KIRO_HOME/.cli.json.XXXXXX")"
cat > "$tmp_cli" <<'JSON'
{
  "chat.enableThinking": true,
  "chat.enableTodoList": true,
  "chat.enableContextUsageIndicator": true,
  "chat.enableDelegate": true,
  "chat.enableKnowledge": true
}
JSON
atomic_write "$cli_json" "$tmp_cli"

# ─── 3. agents/harness.json ──────────────────────────────────────────────────
# Per-harness agent with hooks, resources, allowedTools

agents_out="$KIRO_HOME/agents/harness.json"
tmp_agents="$(mktemp "$KIRO_HOME/.harness.json.XXXXXX")"
hooks_dir="$HARNESS_DIR/core/hooks"

"$PYTHON_BIN" - "$HARNESS_DIR" "$hooks_dir" "$mcp_out" "$HARNESS_OBSERVABILITY_ACTIVE" "$HARNESS_OBSERVABILITY_PROFILE" "$KIRO_OBSERVABILITY_HOOK" "$PYTHON_BIN" > "$tmp_agents" <<'PY'
import json, os, sys

harness = sys.argv[1]
hooks_dir = sys.argv[2]
mcp_out = sys.argv[3]
observability_active = sys.argv[4] == "1"
observability_profile = sys.argv[5]
observability_hook = sys.argv[6]
python_bin = sys.argv[7]

def has_hook(name):
    return os.path.isfile(os.path.join(hooks_dir, name))

# Inline the merged MCP servers (from section 2) so the agent sees them.
# useLegacyMcpJson stays False: legacy mode would also merge default/global
# mcp.json, leaking servers across scopes. Inline keeps this agent hermetic.
mcp_servers = {}
if os.path.isfile(mcp_out):
    with open(mcp_out) as f:
        mcp_servers = json.load(f).get("mcpServers") or {}

# Build existing harness hooks plus metadata-only observer hooks.
hooks = {"agentSpawn": [], "userPromptSubmit": []}

if has_hook("session-start.sh"):
    hooks["agentSpawn"].append({"command": f'bash "{hooks_dir}/session-start.sh"'})
if observability_active:
    hooks["agentSpawn"].append(
        {"command": f'"{python_bin}" "{observability_hook}" agentSpawn {observability_profile} >/dev/null 2>&1 || :'}
    )
    hooks["postToolUse"] = [
        {"command": f'"{python_bin}" "{observability_hook}" postToolUse {observability_profile} >/dev/null 2>&1 || :'}
    ]

prompt_hooks = []
if has_hook("prompt-keyword-routing.sh"):
    prompt_hooks.append({"command": f'bash "{hooks_dir}/prompt-keyword-routing.sh"'})
if has_hook("user-prompt-session-end-detect.sh"):
    prompt_hooks.append({"command": f'bash "{hooks_dir}/user-prompt-session-end-detect.sh"'})
hooks["userPromptSubmit"] = prompt_hooks

# Resources: load CLAUDE.md + steering + rules
resources = [
    "file://CLAUDE.md",
    "file://.claude/rules/**/*.md",
    "file://.kiro/steering/**/*.md",
]

# AllowedTools: read-only tools auto-approved
allowed_tools = ["fs_read", "web_search"]

agent = {
    "name": "harness",
    "description": f"Harness-managed Kiro CLI agent for {os.path.basename(harness)}",
    "prompt": None,
    "mcpServers": mcp_servers,
    "tools": ["*"],
    "toolAliases": {},
    "allowedTools": allowed_tools,
    "resources": resources,
    "hooks": hooks,
    "toolsSettings": {},
    "useLegacyMcpJson": False,
}

json.dump(agent, sys.stdout, indent=2)
sys.stdout.write("\n")
PY

atomic_write "$agents_out" "$tmp_agents"

# ─── 3b. per-subagent agents/<name>.json ─────────────────────────────────────
# Convert .claude/agents/*.md (Claude subagent defs) to Kiro agent JSONs so
# `chat.enableDelegate` can route to them with a per-agent model. The Claude
# frontmatter model TIER resolves to a Kiro model ID via subagent-model-map.tsv
# (column 4). The Kiro/Q agent schema has NO per-agent effort field, so effort
# is intentionally left to the session default (`--effort`) / `/effort` — see
# domains/knowledge/harness/model-routing.md for the runtime divergence.
subagents_src="$HARNESS_DIR/.claude/agents"
map_tsv="$LAUNCHER_BIN_DIR/subagent-model-map.tsv"
if [[ -d "$subagents_src" ]]; then
  "$PYTHON_BIN" - "$subagents_src" "$KIRO_HOME/agents" "$mcp_out" "$hooks_dir" "$map_tsv" "$HARNESS_OBSERVABILITY_ACTIVE" "$HARNESS_OBSERVABILITY_PROFILE" "$KIRO_OBSERVABILITY_HOOK" "$PYTHON_BIN" <<'PY'
import glob, json, os, shutil, sys

src_dir, out_dir, mcp_out, hooks_dir, map_tsv = sys.argv[1:6]
observability_active = sys.argv[6] == "1"
observability_profile = sys.argv[7]
observability_hook = sys.argv[8]
python_bin = sys.argv[9]
quarantine_dir = os.path.join(os.path.dirname(out_dir), ".surface-quarantine", "agents")
owned_marker = "_generated-by-kiro-home-prepare"

# tier -> kiro model ID, from the shared SSOT (column 4). Fallback mirrors the
# launcher's proven production IDs if the table is missing.
model_map = {
    "haiku": "claude-haiku-4.5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5",
    "default": "claude-sonnet-5",
}
if map_tsv and os.path.exists(map_tsv):
    loaded = {}
    with open(map_tsv, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) >= 4 and cols[0] and cols[3].strip():
                loaded[cols[0].strip().lower()] = cols[3].strip()
    if loaded:
        model_map = loaded

def map_model(tier):
    t = (tier or "").strip().lower()
    return model_map.get(t, model_map.get("default", "claude-sonnet-5"))

def parse_frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    meta, lines = {}, text[3:end].lstrip("\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or ":" not in line:
            i += 1; continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val == ">":
            buf = []; i += 1
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                if lines[i].strip():
                    buf.append(lines[i].strip())
                i += 1
            meta[key] = " ".join(buf); continue
        meta[key] = val; i += 1
    return meta

mcp_servers = {}
if os.path.isfile(mcp_out):
    with open(mcp_out) as f:
        mcp_servers = json.load(f).get("mcpServers") or {}

def has_hook(name):
    return os.path.isfile(os.path.join(hooks_dir, name))

hooks = {"agentSpawn": [], "userPromptSubmit": []}
if has_hook("session-start.sh"):
    hooks["agentSpawn"].append({"command": f'bash "{hooks_dir}/session-start.sh"'})
prompt_hooks = []
if has_hook("prompt-keyword-routing.sh"):
    prompt_hooks.append({"command": f'bash "{hooks_dir}/prompt-keyword-routing.sh"'})
if has_hook("user-prompt-session-end-detect.sh"):
    prompt_hooks.append({"command": f'bash "{hooks_dir}/user-prompt-session-end-detect.sh"'})
hooks["userPromptSubmit"] = prompt_hooks
if observability_active:
    hooks["agentSpawn"].append({"command": f'"{python_bin}" "{observability_hook}" agentSpawn {observability_profile} >/dev/null 2>&1 || :'})
    hooks["postToolUse"] = [{"command": f'"{python_bin}" "{observability_hook}" postToolUse {observability_profile} >/dev/null 2>&1 || :'}]

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "Bash"}

def allowed_tools(meta):
    # Read-only agent => auto-approve reads only. Workspace agent => also writes.
    blocked = {t.strip() for t in meta.get("disallowedTools", "").split(",") if t.strip()}
    allowed = {t.strip() for t in meta.get("tools", "").split(",") if t.strip()}
    read_only = bool(blocked & WRITE_TOOLS) or (allowed and not (allowed & WRITE_TOOLS))
    return ["fs_read", "web_search"] if read_only else ["fs_read", "fs_write", "web_search"]

generated = []
for path in sorted(glob.glob(os.path.join(src_dir, "*.md"))):
    name = os.path.basename(path)[:-3]
    # Skip _index and the reserved "harness" name: a .claude/agents/harness.md
    # would otherwise map to harness.json and clobber the launcher's own
    # top-level agent (written unmarked by section 3) via the overwrite path.
    if name in ("_index", "harness"):
        continue
    with open(path, encoding="utf-8") as f:
        meta = parse_frontmatter(f.read())
    if not meta.get("name"):
        continue
    agent = {
        "$schema": "https://raw.githubusercontent.com/aws/amazon-q-developer-cli/refs/heads/main/schemas/agent-v1.json",
        owned_marker: True,
        "name": meta["name"],
        "description": meta.get("description", "").strip(),
        "prompt": None,
        "model": map_model(meta.get("model", "")),
        "mcpServers": mcp_servers,
        "tools": ["*"],
        "toolAliases": {},
        "allowedTools": allowed_tools(meta),
        "resources": ["file://CLAUDE.md", "file://.claude/rules/**/*.md", "file://.kiro/steering/**/*.md"],
        "hooks": hooks,
        "toolsSettings": {},
        "useLegacyMcpJson": False,
    }
    out_path = os.path.join(out_dir, f"{name}.json")
    new_content = json.dumps(agent, indent=2) + "\n"
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            old = f.read()
        if old == new_content:
            generated.append(f"{name}.json"); continue
        # Only overwrite files we own; quarantine anything hand-authored.
        try:
            owned = json.loads(old).get(owned_marker) is True
        except Exception:
            owned = False
        if not owned:
            os.makedirs(quarantine_dir, exist_ok=True)
            dest = os.path.join(quarantine_dir, f"{name}.json")
            suffix = 1
            while os.path.lexists(dest):
                dest = os.path.join(quarantine_dir, f"{name}.json.{suffix}"); suffix += 1
            shutil.move(out_path, dest)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    generated.append(f"{name}.json")

# Reversible-quarantine any generated (owned) agent JSON whose source .md is gone.
# harness.json is the launcher's own top-level agent — never touch it.
#
# Unlike codex (whose agents/ is a pure generated dir, so it quarantines
# anything not produced this run), this Kiro agents/ dir legitimately co-hosts
# hand-authored / Q-CLI-native agent JSONs (e.g. default.json). So we only move
# files that carry our own ownership marker: a non-owned file is left in place
# (`if not owned: continue`). The marker can in principle be forged onto a
# hand-authored file, but the worst case is a *reversible* quarantine of that
# one file — no data loss — which is the safer trade-off than deleting a user's
# real Kiro agent. The forward loop's reserved-name skip protects harness.json.
keep = set(generated) | {"harness.json"}
for path in sorted(glob.glob(os.path.join(out_dir, "*.json"))):
    base = os.path.basename(path)
    if base in keep:
        continue
    try:
        with open(path, encoding="utf-8") as f:
            owned = json.load(f).get(owned_marker) is True
    except Exception:
        owned = False
    if not owned:
        continue
    os.makedirs(quarantine_dir, exist_ok=True)
    dest = os.path.join(quarantine_dir, base)
    suffix = 1
    while os.path.lexists(dest):
        dest = os.path.join(quarantine_dir, f"{base}.{suffix}"); suffix += 1
    shutil.move(path, dest)
PY
fi

# ─── 4. steering/AGENTS.md ───────────────────────────────────────────────────
# Compiler-first (same as codex), fallback to rules concatenation, then CLAUDE.md

steering_md="$KIRO_HOME/steering/AGENTS.md"
compiler="$HARNESS_DIR/core/scripts/harness_compile.py"
compiled=0

if [[ -f "$compiler" && -f "$HARNESS_DIR/.claude/source/runtime-contract.yaml" ]]; then
  tmp_steering="$(mktemp "$KIRO_HOME/.AGENTS.md.XXXXXX")"
  if "$PYTHON_BIN" "$compiler" --write-codex "$HARNESS_DIR" >/dev/null 2>&1; then
    # Compiler writes to .harness/codex/AGENTS.md — copy content for kiro
    codex_agents="$HARNESS_DIR/.harness/codex/AGENTS.md"
    if [[ -f "$codex_agents" ]]; then
      cp "$codex_agents" "$tmp_steering"
      atomic_write "$steering_md" "$tmp_steering"
      compiled=1
    else
      rm -f "$tmp_steering"
    fi
  else
    rm -f "$tmp_steering"
  fi
fi

if [[ "$compiled" -eq 0 ]]; then
  rules_dir="$HARNESS_DIR/.claude/rules"
  rule_files=()
  if [[ -d "$rules_dir" ]]; then
    for f in "$rules_dir"/*.md; do
      [[ -e "$f" ]] || continue
      [[ "$(basename "$f")" == "_index.md" ]] && continue
      rule_files+=("$f")
    done
  fi

  if [[ ${#rule_files[@]} -gt 0 ]]; then
    tmp_steering="$(mktemp "$KIRO_HOME/.AGENTS.md.XXXXXX")"
    {
      echo "# Generated by kiro-home-prepare.sh — do not edit manually."
      echo "# Source-of-truth: $HARNESS_DIR/.claude/rules/*.md"
      echo
      for f in "${rule_files[@]}"; do
        echo "## $(basename "$f" .md)"
        echo
        cat "$f"
        echo
      done
    } > "$tmp_steering"
    atomic_write "$steering_md" "$tmp_steering"
  elif [[ -f "$HARNESS_DIR/CLAUDE.md" ]]; then
    ln -sfn "../../CLAUDE.md" "$steering_md"
  fi
fi

# ─── 5. Skills merge ─────────────────────────────────────────────────────────
# Symlink merge: global kiro skills + harness .claude/skills → $KIRO_HOME/skills/
# Kiro CLI reads skills from $KIRO_HOME/skills/ (same format as Claude skills)

SKILLS_DIR="$KIRO_HOME/skills"
MARKER="$SKILLS_DIR/.harness-managed"

# Clean previous managed entries
if [[ -f "$MARKER" ]]; then
  while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    [[ -L "$SKILLS_DIR/$entry" ]] && rm "$SKILLS_DIR/$entry"
  done < "$MARKER"
fi
: > "$MARKER"

link_skill_dir() {
  local src_root="$1"
  [[ -d "$src_root" ]] || return 0
  local src name
  for src in "$src_root"/*/; do
    [[ -d "$src" ]] || continue
    [[ -f "$src/SKILL.md" ]] || continue
    name="$(basename "$src")"
    ln -sfn "${src%/}" "$SKILLS_DIR/$name"
    echo "$name" >> "$MARKER"
  done
}

# Global kiro skills (user-scope)
link_skill_dir "$HOME/.kiro/skills"
# Harness-local Claude skills (portable format)
link_skill_dir "$HARNESS_DIR/.claude/skills"
# User-scope Claude skills (if any)
link_skill_dir "$HOME/.claude/skills"
