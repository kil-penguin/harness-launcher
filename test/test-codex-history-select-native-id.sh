#!/usr/bin/env zsh
set -euo pipefail

# The Codex history selector asks the catalog helper for just the requested id
# when it is given a UUID; list and continue keep the full catalog.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ID_ONE='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
mkdir -p "$TMP/bin"
cat > "$TMP/bin/codex-history.py" <<'PY'
#!/usr/bin/env python3
import json, os, pathlib, sys
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
print(json.dumps({"schema_version": 1, "entries": [
 {"native_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "home": "/live/home", "source_root": "/fixture", "isolation_id": "22222222-2222-4222-8222-222222222222", "origin_runtime": "b" * 40, "snapshot_id": None, "updated_at": "2026-10-10T02:00:00Z", "status": "live", "reason": None, "source_local": True}
], "warnings": []}))
PY
chmod +x "$TMP/bin/codex-history.py"
export HARNESS_SESSION_STATE_HOME="$TMP/state" ARGV_LOG="$TMP/argv.log"
source "$ROOT/bin/aliases.zsh"
typeset -g _HARNESS_LAUNCHER_BIN="$TMP/bin"

: > "$ARGV_LOG"
out="$(_harness_launcher_codex_history_select /fixture "$ID_ONE")"
[[ "$out" == *"native_id=$ID_ONE"* ]] || { echo "FAIL: UUID selection lost its row: $out"; exit 1; }
grep -Fq -- "--native-id $ID_ONE" "$ARGV_LOG" || { echo 'FAIL: a UUID selection must pass --native-id to the catalog'; cat "$ARGV_LOG"; exit 1; }

: > "$ARGV_LOG"
out="$(_harness_launcher_codex_history_select /fixture "${ID_ONE:u}")"
grep -Fq -- "--native-id ${ID_ONE:l}" "$ARGV_LOG" || { echo 'FAIL: the UUID is passed lower-cased'; cat "$ARGV_LOG"; exit 1; }

for mode in list continue; do
  : > "$ARGV_LOG"
  _harness_launcher_codex_history_select /fixture "$mode" >/dev/null || true
  grep -q -- '--native-id' "$ARGV_LOG" && { echo "FAIL: $mode must keep the full catalog"; exit 1; }
  grep -q '^catalog ' "$ARGV_LOG" || { echo "FAIL: $mode must still read the catalog"; exit 1; }
done
echo 'PASS: codex history select passes --native-id only for a UUID'
