#!/usr/bin/env bash
# Company usage-collection policy: decision table (P1), Claude --settings env
# (P3) and the encoding the collector matches against (P7).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(cd "$(mktemp -d)" && pwd -P)"
trap 'chmod -R u+w "$TMP" 2>/dev/null; rm -rf "$TMP"' EXIT

export HOME="$TMP/home"
mkdir -p "$HOME"
# fixture state home; the controller session identity must not leak in
export HARNESS_SESSION_STATE_HOME="$TMP/state"
unset HARNESS_SESSION_ROOT HARNESS_SESSION_ID HARNESS_SOURCE_ROOT HARNESS_RUN_DIR
export HARNESS_AGENT_USAGE_TEST_MODE=1
export HARNESS_AGENT_USAGE_STATE_DIR="$TMP/agent-usage"
GUARD="$TMP/guard"
export HARNESS_AGENT_USAGE_GUARD_DIR="$GUARD"
export HARNESS_AGENT_USAGE_TEST_OWNER_UID="$(id -u)"
unset HARNESS_PYTHON_BIN

# shellcheck source=../bin/harness-common.sh
source "$ROOT/bin/harness-common.sh"
PY="$(harness_python3_resolve)"

fail() { echo "FAIL: $*" >&2; exit 1; }

make_guard() {
  rm -rf "$GUARD"
  mkdir -p "$GUARD/$(id -u)"
  chmod 755 "$GUARD" "$GUARD/$(id -u)"
  : > "$GUARD/$(id -u)/agent-assertion.jwt"
}

make_harness() { # <dir> <collect-line or empty>
  mkdir -p "$1/config/.local"
  { printf 'HARNESS_NAME="fixture"\nHARNESS_PREFIX="fx"\n'; [[ -z "${2:-}" ]] || printf '%s\n' "$2"; } > "$1/config/launcher.env"
}

# expect <label> <active> <reason> <harness_dir> [source_root]
expect() {
  local label="$1" active="$2" reason="$3"; shift 3
  HARNESS_AGENT_USAGE_ACTIVE="" HARNESS_AGENT_USAGE_REASON=""
  harness_agent_usage_policy "$@" 2>"$TMP/warn" || true
  [[ "$HARNESS_AGENT_USAGE_ACTIVE" == "$active" && "$HARNESS_AGENT_USAGE_REASON" == "$reason" ]] \
    || fail "$label: got ACTIVE=$HARNESS_AGENT_USAGE_ACTIVE REASON=$HARNESS_AGENT_USAGE_REASON want $active/$reason"
}

# ---- P1 decision table --------------------------------------------------
H="$TMP/h"
make_guard
make_harness "$H" ''
expect 'no flag' 0 profile_off "$H"
make_harness "$H" 'HARNESS_AGENT_USAGE_COLLECT="0"'
expect 'flag 0' 0 profile_off "$H"
make_harness "$H" 'HARNESS_AGENT_USAGE_COLLECT="1"'
expect 'flag + guard' 1 active "$H"
make_harness "$H" "HARNESS_AGENT_USAGE_COLLECT='1'"
expect 'single quoted flag' 1 active "$H"
make_harness "$H" 'HARNESS_AGENT_USAGE_COLLECT=1'
expect 'bare flag' 1 active "$H"
make_harness "$H" 'HARNESS_AGENT_USAGE_COLLECT="yes"'
expect 'flag with other value' 0 profile_off "$H"
make_harness "$H" $'HARNESS_AGENT_USAGE_COLLECT="1"\r'
expect 'CRLF flag line' 1 active "$H"
make_harness "$H" 'HARNESS_AGENT_USAGE_COLLECT="1" # inline comment'
expect 'inline comment stays fail-closed' 0 profile_off "$H"

make_harness "$H" 'HARNESS_AGENT_USAGE_COLLECT="1"'
OVR="$H/config/.local/agent-usage.env"
printf 'HARNESS_AGENT_USAGE_COLLECT=0\n' > "$OVR"
expect 'override 0' 0 local_off "$H"
printf '# note\n\nHARNESS_AGENT_USAGE_COLLECT=1\n' > "$OVR"
expect 'override 1' 1 active "$H"
for body in 'HARNESS_AGENT_USAGE_COLLECT=2' 'HARNESS_AGENT_USAGE_COLLECT=' 'HARNESS_AGENT_USAGE_OTHER=1' \
  'garbage' $'HARNESS_AGENT_USAGE_COLLECT=1\nHARNESS_AGENT_USAGE_COLLECT=1'; do
  printf '%s\n' "$body" > "$OVR"
  expect "invalid override <$body>" 0 invalid_config "$H"
  grep -q 'invalid_config' "$TMP/warn" || fail "invalid override must WARN (<$body>)"
done
printf '$(touch %s/pwned)\n' "$TMP" > "$OVR"
expect 'override is data, never sourced' 0 invalid_config "$H"
[[ ! -e "$TMP/pwned" ]] || fail 'override file was executed'
rm -f "$OVR"

# the override is read from the source root, not the (clone) harness dir
S="$TMP/source"; make_harness "$S" 'HARNESS_AGENT_USAGE_COLLECT="1"'
printf 'HARNESS_AGENT_USAGE_COLLECT=0\n' > "$S/config/.local/agent-usage.env"
expect 'override from source root' 0 local_off "$H" "$S"
rm -f "$S/config/.local/agent-usage.env"
expect 'no override at source root' 1 active "$H" "$S"

# the opt-in flag is read from the canonical source root too (M2): an agent
# editing its clone's tracked launcher.env cannot switch company OTel on
make_harness "$S" ''
make_harness "$H" 'HARNESS_AGENT_USAGE_COLLECT="1"'
expect 'clone flag ignored, source root decides' 0 profile_off "$H" "$S"
# an isolated worktree resolves its source root from the session record even
# without a hint
SID=0f3a9c12-ab34-4cde-8f01-aaaaaaaaaaaa
W="$HARNESS_SESSION_STATE_HOME/worktrees/$SID"
mkdir -p "$HARNESS_SESSION_STATE_HOME/sessions/$SID"
make_harness "$W" 'HARNESS_AGENT_USAGE_COLLECT="1"'
printf '%s\n' "$S" > "$HARNESS_SESSION_STATE_HOME/sessions/$SID/source-root"
expect 'worktree flag ignored via session record' 0 profile_off "$W"
expect 'worktree flag ignored via session record (hint is the worktree)' 0 profile_off "$W" "$W"
make_harness "$S" 'HARNESS_AGENT_USAGE_COLLECT="1"'
make_harness "$W" ''
expect 'worktree follows a collecting source' 1 active "$W"
make_harness "$H" 'HARNESS_AGENT_USAGE_COLLECT="1"'

# company device checks
make_guard; rm "$GUARD/$(id -u)/agent-assertion.jwt"
expect 'jwt missing' 0 no_company_device "$H"
make_guard; rm "$GUARD/$(id -u)/agent-assertion.jwt"; mkdir "$GUARD/$(id -u)/agent-assertion.jwt"
expect 'jwt is a directory' 0 no_company_device "$H"
make_guard; rm "$GUARD/$(id -u)/agent-assertion.jwt"; : > "$TMP/real.jwt"; ln -s "$TMP/real.jwt" "$GUARD/$(id -u)/agent-assertion.jwt"
expect 'jwt is a link' 0 no_company_device "$H"
rm -rf "$GUARD"
expect 'guard dir missing' 0 no_company_device "$H"
make_guard; chmod 775 "$GUARD/$(id -u)"
expect 'uid dir group-writable' 0 no_company_device "$H"
make_guard; chmod 757 "$GUARD/$(id -u)"
expect 'uid dir other-writable' 0 no_company_device "$H"
make_guard; chmod 775 "$GUARD"
expect 'parent group-writable' 0 no_company_device "$H"
make_guard
HARNESS_AGENT_USAGE_TEST_OWNER_UID=$(( $(id -u) + 1 )) expect 'owner differs' 0 no_company_device "$H"
make_guard; mv "$GUARD/$(id -u)" "$TMP/uid-real"; ln -s "$TMP/uid-real" "$GUARD/$(id -u)"
expect 'uid dir is a link' 0 no_company_device "$H"
make_guard; mv "$GUARD" "$TMP/guard-real"; ln -s "$TMP/guard-real" "$GUARD"
expect 'parent is a link' 0 no_company_device "$H"
rm -f "$GUARD"
make_guard
HARNESS_AGENT_USAGE_TEST_UNAME=Linux expect 'not darwin' 0 no_company_device "$H"
expect 'guard ok again' 1 active "$H"

# The guard override is a test hook only: outside test mode it must be ignored.
unset HARNESS_AGENT_USAGE_TEST_MODE
make_guard
expect_ignored() {
  local a b
  HARNESS_AGENT_USAGE_GUARD_DIR="$GUARD" harness_agent_usage_policy "$H" 2>/dev/null || true; a="$HARNESS_AGENT_USAGE_REASON"
  HARNESS_AGENT_USAGE_GUARD_DIR="$TMP/does-not-exist" harness_agent_usage_policy "$H" 2>/dev/null || true; b="$HARNESS_AGENT_USAGE_REASON"
  [[ "$a" == "$b" ]] || fail "guard override honored outside test mode ($a vs $b)"
}
expect_ignored
export HARNESS_AGENT_USAGE_TEST_MODE=1

# ---- P3 Claude --settings env ------------------------------------------
settings_env() { # <harness_dir> [source_root] -> prints compact JSON of env or "none"
  local out
  out="$(harness_claude_launch_settings "$ROOT/bin" "${2:-$1}" false "" "" "$1")"
  "$PY" - "$out" <<'PY'
import json, sys
raw = sys.argv[1]
data = json.loads(raw) if raw else {}
print(json.dumps(data["env"], sort_keys=True) if "env" in data else "none")
PY
}

make_guard
make_harness "$H" ''
[[ "$(settings_env "$H")" == '{"CLAUDE_CODE_ENABLE_TELEMETRY": "0"}' ]] || fail "personal profile must disable Claude telemetry via --settings env: $(settings_env "$H")"
make_harness "$H" 'HARNESS_AGENT_USAGE_COLLECT="1"'
[[ "$(settings_env "$H")" == none ]] || fail "collecting profile must not set an env block: $(settings_env "$H")"
printf 'HARNESS_AGENT_USAGE_COLLECT=0\n' > "$H/config/.local/agent-usage.env"
[[ "$(settings_env "$H")" == '{"CLAUDE_CODE_ENABLE_TELEMETRY": "0"}' ]] || fail 'local override 0 must disable Claude telemetry'
rm -f "$H/config/.local/agent-usage.env"
rm -rf "$GUARD"
[[ "$(settings_env "$H")" == '{"CLAUDE_CODE_ENABLE_TELEMETRY": "0"}' ]] || fail 'non-company device must disable Claude telemetry'
make_guard
# a clone's config/.local is gone; the source root's override still wins
printf 'HARNESS_AGENT_USAGE_COLLECT=0\n' > "$S/config/.local/agent-usage.env"
[[ "$(settings_env "$H" "$S")" == '{"CLAUDE_CODE_ENABLE_TELEMETRY": "0"}' ]] || fail 'source-root override must apply to the launch settings'
rm -f "$S/config/.local/agent-usage.env"
[[ "$(settings_env "$H" "$S")" == none ]] || fail 'collecting source root must not set an env block'

# local observability on a personal profile: the generic loopback endpoint and
# empty headers go into --settings (a shell export of them would lose to the
# user settings)
make_harness "$H" ''
printf 'HARNESS_OBSERVABILITY_ENABLED=1\nHARNESS_OTLP_HTTP_ENDPOINT=http://127.0.0.1:4318\n' > "$H/config/.local/observability.env"
[[ "$(settings_env "$H")" == '{"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318", "OTEL_EXPORTER_OTLP_HEADERS": ""}' ]] || fail "observed personal profile must pin the generic loopback endpoint and blank headers: $(settings_env "$H")"
make_harness "$H" 'HARNESS_AGENT_USAGE_COLLECT="1"'
[[ "$(settings_env "$H")" == none ]] || fail 'collecting profile with observability must not set an env block'
rm -f "$H/config/.local/observability.env"

# the launch-record hook is still emitted next to the env block
make_harness "$H" ''
harness_claude_launch_settings "$ROOT/bin" "$H" false "" "" "$H" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); assert "SessionStart" in d["hooks"], d' \
  || fail 'launch-record hook must survive the env block'

# ---- child-process discard exports -------------------------------------
discard_env() { # <harness_dir> [source] -> prints the six vars, or "none"
  ( unset OTEL_EXPORTER_OTLP_LOGS_ENDPOINT OTEL_EXPORTER_OTLP_METRICS_ENDPOINT OTEL_EXPORTER_OTLP_TRACES_ENDPOINT \
      OTEL_EXPORTER_OTLP_LOGS_HEADERS OTEL_EXPORTER_OTLP_METRICS_HEADERS OTEL_EXPORTER_OTLP_TRACES_HEADERS
    harness_agent_usage_export_discard "$@"
    if [[ -z "${OTEL_EXPORTER_OTLP_LOGS_ENDPOINT+x}" ]]; then echo none; exit 0; fi
    env | grep '^OTEL_EXPORTER_OTLP_' | LC_ALL=C sort )
}
make_harness "$H" ''
want=$'OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:9/v1/logs\nOTEL_EXPORTER_OTLP_LOGS_HEADERS=\nOTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://127.0.0.1:9/v1/metrics\nOTEL_EXPORTER_OTLP_METRICS_HEADERS=\nOTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:9/v1/traces\nOTEL_EXPORTER_OTLP_TRACES_HEADERS='
[[ "$(discard_env "$H")" == "$want" ]] || fail "inactive policy must export discard signal endpoints: $(discard_env "$H")"
make_harness "$H" 'HARNESS_AGENT_USAGE_COLLECT="1"'
[[ "$(discard_env "$H")" == none ]] || fail 'active policy must not export discard endpoints'
make_harness "$H" ''
printf 'HARNESS_OBSERVABILITY_ENABLED=1\nHARNESS_OTLP_HTTP_ENDPOINT=http://127.0.0.1:4318\n' > "$H/config/.local/observability.env"
# observed personal profile (H4): Claude children of Codex point every signal
# at the same loopback collector, with empty signal headers
want_obs=$'OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs\nOTEL_EXPORTER_OTLP_LOGS_HEADERS=\nOTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://127.0.0.1:4318/v1/metrics\nOTEL_EXPORTER_OTLP_METRICS_HEADERS=\nOTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:4318/v1/traces\nOTEL_EXPORTER_OTLP_TRACES_HEADERS='
[[ "$(discard_env "$H")" == "$want_obs" ]] || fail "observed inactive profile must export loopback signal endpoints: $(discard_env "$H")"
make_harness "$H" 'HARNESS_AGENT_USAGE_COLLECT="1"'
[[ "$(discard_env "$H")" == none ]] || fail 'observed collecting profile exports nothing'
make_harness "$H" ''
rm -f "$H/config/.local/observability.env"
# M2: a clone that opts itself in still gets the discard set
make_harness "$S" ''
make_harness "$H" 'HARNESS_AGENT_USAGE_COLLECT="1"'
[[ "$(discard_env "$H" "$S")" == "$want" ]] || fail 'discard follows the source root flag, not the clone'
[[ "$(settings_env "$H" "$S")" == '{"CLAUDE_CODE_ENABLE_TELEMETRY": "0"}' ]] || fail 'Claude settings env follows the source root flag, not the clone'
make_harness "$H" ''

# ---- P7 claude_encode ---------------------------------------------------
enc="$("$PY" -I "$ROOT/bin/harness_agent_usage.py" encode '/Users/gameduo/.local/state/kilhyeonjun-harness/launcher-runtimes/session-state/worktrees/A2C5415D-3D9E-496D-9979-611B9DF59087')"
[[ "$enc" == '-Users-gameduo--local-state-kilhyeonjun-harness-launcher-runtimes-session-state-worktrees-A2C5415D-3D9E-496D-9979-611B9DF59087' ]] || fail "claude_encode mismatch: $enc"
[[ "$("$PY" -I "$ROOT/bin/harness_agent_usage.py" encode '/a b/c.d_e')" == '-a-b-c-d-e' ]] || fail 'claude_encode must dash every non-alphanumeric'

echo 'PASS: agent usage policy table, Claude settings env, claude_encode'
