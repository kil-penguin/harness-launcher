#!/usr/bin/env bash
# Catches missing opt-in registration, lost Claude settings and env-only opt-in.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/project/config"
source "$ROOT/bin/harness-common.sh"
printf 'HARNESS_PREFIX=alpha\n' > "$TMP/project/config/launcher.env"
printf '{"enabled":true}\n' > "$TMP/project/config/ssot-session-hooks.json"
settings="$(harness_claude_launch_settings "$ROOT/bin" "$TMP/project" true default '')"
python3 - "$settings" <<'PY'
import json, sys
data = json.loads(sys.argv[1])
assert data['alwaysThinkingEnabled'] is True
ss = data['hooks']['SessionStart']
assert len(ss) == 1 and len(ss[0]['hooks']) == 1, 'record + callback must be one command'
row = ss[0]['hooks'][0]
assert 'harness-launch-record' in row['command'] and row['command'].endswith(' --callback'), row
assert 'harness-session-hook' not in row['command']
assert row['timeout'] == 10, row
sub = data['hooks']['SubagentStart']
assert len(sub) == 1 and sub[0]['hooks'][0]['timeout'] == 5
assert 'harness-session-hook' in sub[0]['hooks'][0]['command'] and '--runtime claude' in sub[0]['hooks'][0]['command']
for event in ('UserPromptSubmit', 'Stop'):
    handlers = data['hooks'].get(event, [])
    assert len(handlers) == 1, f'{event} collector not registered'
    assert 'harness-session-hook' in handlers[0]['hooks'][0]['command']
    assert '--runtime claude' in handlers[0]['hooks'][0]['command']
PY
printf 'HARNESS_PREFIX=beta\n' > "$TMP/project/config/launcher.env"
rm "$TMP/project/config/ssot-session-hooks.json"
settings="$(HARNESS_SSOT_SESSION_HOOKS=1 harness_claude_launch_settings "$ROOT/bin" "$TMP/project" false default '')"
python3 - "$settings" <<'PY'
import json, sys
data = json.loads(sys.argv[1])
assert set(data['hooks']) == {'SessionStart'}
row = data['hooks']['SessionStart'][0]['hooks'][0]
assert row['timeout'] == 5 and '--callback' not in row['command'], row
PY
printf 'PASS: SSOT hooks require literal per-profile opt-in and preserve Claude launch settings\n'
