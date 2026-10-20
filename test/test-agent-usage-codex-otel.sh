#!/usr/bin/env bash
# Codex: the company [otel] table of ~/.codex/config.toml is inherited into the
# generated home only for a collecting profile without local observability (P2).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREPARE="$ROOT/bin/codex-home-prepare.sh"
TMP="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

export HOME="$TMP/home"
mkdir -p "$HOME/.codex"
export HARNESS_AGENT_USAGE_TEST_MODE=1 HARNESS_AGENT_USAGE_STATE_DIR="$TMP/agent-usage"
GUARD="$TMP/guard"
export HARNESS_AGENT_USAGE_GUARD_DIR="$GUARD" HARNESS_AGENT_USAGE_TEST_OWNER_UID="$(id -u)"
export HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$TMP/no-marketplace"
unset HARNESS_PYTHON_BIN HARNESS_SOURCE_ROOT HARNESS_SESSION_ROOT HARNESS_SESSION_ID HARNESS_RUN_DIR
export HARNESS_SESSION_STATE_HOME="$HOME/.local/state/harness-launcher"
source "$ROOT/bin/harness-common.sh"
PY="$(harness_python3_resolve)"
mkdir -p "$GUARD/$(id -u)"; chmod 755 "$GUARD" "$GUARD/$(id -u)"; : > "$GUARD/$(id -u)/agent-assertion.jwt"

SECRET='Bearer sk-test-SECRET-0123456789'
write_company() { # <environment>
  cat > "$HOME/.codex/config.toml" <<EOF
model = "something"
[otel]
environment = "$1"
log_user_prompt = false
unrelated_key = "must-not-be-copied"
exporter = { otlp-http = { endpoint = "https://collector.example.invalid/v1/logs", protocol = "binary", headers = { "Authorization" = "$SECRET" } } }
trace_exporter = "none"
metrics_exporter = { otlp-http = { endpoint = "https://collector.example.invalid/v1/metrics", protocol = "binary", headers = { "Authorization" = "$SECRET" } } }
[mcp_servers.leak]
command = "echo"
EOF
}

H="$TMP/h"
mkdir -p "$H/config/.local"
printf '# fixture\n' > "$H/CLAUDE.md"
launcher_env() { printf 'HARNESS_NAME="fx"\nHARNESS_PREFIX="fx"\n%s\n' "$1" > "$H/config/launcher.env"; }
CONFIG="$H/.harness/codex/config.toml"
run_prepare() { "$PREPARE" "$H" >"$TMP/out" 2>"$TMP/err"; }

# collecting + observability off -> company [otel] inherited, whitelisted, 0600
write_company prod
launcher_env 'HARNESS_AGENT_USAGE_COLLECT="1"'
run_prepare || { cat "$TMP/err" >&2; fail 'prepare failed'; }
grep -q '^\[otel\]$' "$CONFIG" || fail 'company [otel] must be inherited'
grep -q '^environment = "prod"$' "$CONFIG" || fail 'environment copied'
grep -q "$SECRET" "$CONFIG" || fail 'exporter headers copied'
grep -q '^trace_exporter = "none"$' "$CONFIG" || fail 'string exporters copied'
grep -q 'unrelated_key' "$CONFIG" && fail 'only whitelisted keys may be copied'
grep -q 'mcp_servers.leak' "$CONFIG" && fail 'only the otel table may be copied'
[[ "$(stat -f %Lp "$CONFIG")" == 600 ]] || fail 'generated config must stay 0600'
[[ "$(grep -c '^\[otel\]$' "$CONFIG")" == 1 ]] || fail 'exactly one [otel]'
grep -q "$SECRET" "$TMP/out" "$TMP/err" && fail 'bearer value must never be printed'

# key rotation / environment change regenerates
write_company staging
run_prepare || fail 'prepare failed (rotate)'
grep -q '^environment = "staging"$' "$CONFIG" || fail 'a changed company [otel] must regenerate the config'
SECRET2='Bearer sk-test-ROTATED-9876543210'
sed -i.bak "s|$SECRET|$SECRET2|" "$HOME/.codex/config.toml"; rm -f "$HOME/.codex/config.toml.bak"
run_prepare || fail 'prepare failed (key rotate)'
grep -q "$SECRET2" "$CONFIG" && ! grep -q "$SECRET" "$CONFIG" || fail 'a rotated key must regenerate the config'

# idempotent when nothing changed
before="$(stat -f '%i %m' "$CONFIG")"; run_prepare
[[ "$before" == "$(stat -f '%i %m' "$CONFIG")" ]] || fail 'unchanged inputs must not rewrite config.toml'

# profile off -> no [otel]
launcher_env ''
run_prepare || fail 'prepare failed (profile off)'
grep -q '^\[otel\]$' "$CONFIG" && fail 'personal profile must not inherit company [otel]'
# local override off
launcher_env 'HARNESS_AGENT_USAGE_COLLECT="1"'
printf 'HARNESS_AGENT_USAGE_COLLECT=0\n' > "$H/config/.local/agent-usage.env"
run_prepare || fail 'prepare failed (override off)'
grep -q '^\[otel\]$' "$CONFIG" && fail 'local override 0 must drop the company [otel]'
rm -f "$H/config/.local/agent-usage.env"
# activation flips it back on
run_prepare || fail 'prepare failed (back on)'
grep -q '^\[otel\]$' "$CONFIG" || fail 'activation must inherit again'

# observability on -> loopback profile, never the company table
printf 'HARNESS_OBSERVABILITY_ENABLED=1\nHARNESS_OTLP_HTTP_ENDPOINT=http://127.0.0.1:4318\n' > "$H/config/.local/observability.env"
chmod 600 "$H/config/.local/observability.env"
run_prepare || fail 'prepare failed (observability)'
grep -q 'endpoint = "http://127.0.0.1:4318/v1/logs"' "$CONFIG" || fail 'observability keeps the loopback exporter'
grep -q "collector.example.invalid" "$CONFIG" && fail 'observability must not mix in the company exporter'
rm -f "$H/config/.local/observability.env"

# missing ~/.codex/config.toml, no [otel], unparsable -> no table + visible WARN
for variant in missing none broken; do
  rm -f "$HOME/.codex/config.toml"
  case "$variant" in
    none) printf 'model = "x"\n' > "$HOME/.codex/config.toml" ;;
    broken) printf '[otel\nenvironment = "%s"\n' "$SECRET" > "$HOME/.codex/config.toml" ;;
  esac
  run_prepare || fail "prepare must not fail ($variant)"
  grep -q '^\[otel\]$' "$CONFIG" && fail "no [otel] expected ($variant)"
  grep -q 'WARN: .*company OTel' "$TMP/err" || fail "a visible WARN is required ($variant)"
  grep -q "$SECRET" "$TMP/out" "$TMP/err" && fail "secret printed ($variant)"
done

# helper interpreter unavailable (below 3.11) -> disabled with a WARN, not silent
write_company prod
# Prepare itself needs a modern interpreter, so the test hook only makes the
# helper lookup (harness_python3_resolve) fail.
HARNESS_AGENT_USAGE_TEST_PYTHON_FAIL=1 run_prepare || fail 'prepare must not fail without helper python'
grep -q '^\[otel\]$' "$CONFIG" && fail 'no [otel] without the helper interpreter'
grep -q 'WARN: .*company OTel.*python' "$TMP/err" || fail 'python WARN required'

# M2: the flag is read from the canonical source root. An isolated clone that
# opts itself in does not inherit the company table...
S="$TMP/source"; mkdir -p "$S/config/.local"
printf 'HARNESS_NAME="fx"\nHARNESS_PREFIX="fx"\n' > "$S/config/launcher.env"
launcher_env 'HARNESS_AGENT_USAGE_COLLECT="1"'
HARNESS_SESSION_ROOT="$H" HARNESS_SOURCE_ROOT="$S" run_prepare || fail 'prepare failed (clone opt-in)'
grep -q '^\[otel\]$' "$CONFIG" && fail 'a clone flag must not inherit the company [otel] when the source root does not opt in'
# ...a clone of a collecting source does...
printf 'HARNESS_AGENT_USAGE_COLLECT="1"\n' >> "$S/config/launcher.env"
HARNESS_SESSION_ROOT="$H" HARNESS_SOURCE_ROOT="$S" run_prepare || fail 'prepare failed (collecting source)'
grep -q '^\[otel\]$' "$CONFIG" || fail 'a clone of a collecting source inherits the company [otel]'
# ...and a source root inherited from another harness's session shell never
# decides for this harness.
launcher_env ''
HARNESS_SOURCE_ROOT="$S" run_prepare || fail 'prepare failed (stale source root)'
grep -q '^\[otel\]$' "$CONFIG" && fail 'a stale HARNESS_SOURCE_ROOT of another harness must not turn company OTel on'
launcher_env 'HARNESS_AGENT_USAGE_COLLECT="1"'

# non-ASCII values are written as UTF-8 TOML strings (LOW)
printf '[otel]\nenvironment = "프로드"\n' > "$TMP/unicode.toml"
[[ "$("$PY" -I "$ROOT/bin/harness_agent_usage.py" codex-otel "$TMP/unicode.toml")" == $'[otel]\nenvironment = "프로드"' ]] || fail 'non-ASCII otel value must be kept literally'

# warm-probe validator accepts a company table only when told one is expected
PROBE_OUT="$("$PY" - "$ROOT" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("warm", os.path.join(sys.argv[1], "bin", "codex-surface-warm.py"))
warm = importlib.util.module_from_spec(spec); spec.loader.exec_module(warm)
table = {"environment": "prod", "log_user_prompt": False, "exporter": "none"}
os.environ.pop("HARNESS_AGENT_USAGE_COMPANY_OTEL", None)
a = warm.otel_matches(table, "")
os.environ["HARNESS_AGENT_USAGE_COMPANY_OTEL"] = "1"
b = warm.otel_matches(table, "")
c = warm.otel_matches({"environment": "prod", "mcp_servers": {}}, "")
d = warm.otel_matches(None, "")
print(a, b, c, d)
PY
)"
[[ "$PROBE_OUT" == "False True False True" ]] || fail "warm validator company-table handling: $PROBE_OUT"

echo 'PASS: Codex inherits the company [otel] only for a collecting profile'
