#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d /private/tmp/harness-transcript-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
export HOME="$TMP/home" HARNESS_SESSION_STATE_HOME="$TMP/state" HARNESS_SESSION_RETENTION_SECONDS=0
mkdir -p "$HOME" "$TMP/source"; chmod 700 "$HOME"
git -C "$TMP/source" init -q -b main
printf '.harness/\n' > "$TMP/source/.gitignore"
git -C "$TMP/source" add .gitignore
git -C "$TMP/source" -c user.name=Fixture -c user.email=fixture@example.invalid commit -q -m fixture
git init --bare -q "$TMP/remote.git"
git -C "$TMP/source" remote add origin "$TMP/remote.git"
git -C "$TMP/source" push -q -u origin main
OUT="$("$ROOT/bin/session-isolation.sh" create "$TMP/source")"
ID="$(printf '%s\n' "$OUT" | sed -n 's/^HARNESS_SESSION_ID=//p')"
WORK="$(printf '%s\n' "$OUT" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
NATIVE=11111111-1111-4111-8111-111111111111
REL="codex/sessions/2026/10/10/rollout-fixture-$NATIVE.jsonl"
mkdir -p "$WORK/.harness/$(dirname "$REL")"
printf '{"type":"session_meta","payload":{"id":"%s","cwd":"%s"}}\n' "$NATIVE" "$WORK" > "$WORK/.harness/$REL"
printf '{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"retain exact conversation"}]}}\n' >> "$WORK/.harness/$REL"
printf 'fixture-auth-excluded\n' > "$WORK/.harness/codex/auth.json"
cp "$WORK/.harness/$REL" "$TMP/original.jsonl"
"$ROOT/bin/session-isolation.sh" close "$ID"
"$ROOT/bin/session-isolation.sh" gc
[[ -d "$WORK" ]] || { echo 'FAIL: transcript-only backup must not authorize Native root deletion without full restoration proof'; exit 1; }
cmp "$TMP/original.jsonl" "$WORK/.harness/$REL" || { echo 'FAIL: held Native original must remain unchanged'; exit 1; }
cmp "$TMP/original.jsonl" "$HARNESS_SESSION_STATE_HOME/archives/$ID/$REL" || { echo 'FAIL: GC deleted the only native transcript without exact durable backup'; exit 1; }
[[ ! -e "$HARNESS_SESSION_STATE_HOME/archives/$ID/codex/auth.json" ]] || { echo 'FAIL: transcript archive must exclude auth'; exit 1; }
python3 - "$HARNESS_SESSION_STATE_HOME/archives/$ID" "$ID" <<'PY'
from pathlib import Path
import hashlib,json,stat,sys
p=Path(sys.argv[1]);m=json.loads((p/'manifest.json').read_text());assert m['session_id']==sys.argv[2]
assert stat.S_IMODE(p.stat().st_mode)==0o700
assert len(m['files'])==1
for item in m['files']:
 f=p/item['path'];b=f.read_bytes();assert item['bytes']==len(b) and item['sha256']==hashlib.sha256(b).hexdigest();assert stat.S_IMODE(f.stat().st_mode)==0o600
PY

OUT="$("$ROOT/bin/session-isolation.sh" create "$TMP/source")"
ID="$(printf '%s\n' "$OUT" | sed -n 's/^HARNESS_SESSION_ID=//p')";WORK="$(printf '%s\n' "$OUT" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
mkdir -p "$WORK/.harness/codex/sessions"
printf 'original\n' > "$WORK/.harness/codex/sessions/current.jsonl"
"$ROOT/bin/session-isolation.sh" close "$ID"
python3 "$ROOT/bin/harness_session_archive.py" archive --root "$WORK" --state "$HARNESS_SESSION_STATE_HOME" --owner "$ID" --source "$TMP/source"
printf 'new turn\n' >> "$WORK/.harness/codex/sessions/current.jsonl"
"$ROOT/bin/session-isolation.sh" gc
[[ -d "$WORK" ]] && grep -q 'new turn' "$WORK/.harness/codex/sessions/current.jsonl" || { echo 'FAIL: an older archive must never authorize deleting newer live history'; exit 1; }

OUT="$("$ROOT/bin/session-isolation.sh" create "$TMP/source")"
ID="$(printf '%s\n' "$OUT" | sed -n 's/^HARNESS_SESSION_ID=//p')";WORK="$(printf '%s\n' "$OUT" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
mkdir -p "$WORK/.harness/codex/sessions";mkfifo "$WORK/.harness/codex/sessions/unsafe.jsonl"
"$ROOT/bin/session-isolation.sh" close "$ID";"$ROOT/bin/session-isolation.sh" gc
[[ -d "$WORK" && ! -e "$HARNESS_SESSION_STATE_HOME/archives/$ID/manifest.json" ]] || { echo 'FAIL: unreadable native entry must retain the root, not omit data'; exit 1; }
TOMB="$HARNESS_SESSION_STATE_HOME/worktrees/.retired-$ID-123456"
mkdir "$TOMB";printf preserve > "$TOMB/sentinel"
"$ROOT/bin/session-isolation.sh" gc
[[ -f "$TOMB/sentinel" ]] || { echo 'FAIL: crash tomb with no archive proof must survive'; exit 1; }
OUT="$("$ROOT/bin/session-isolation.sh" create "$TMP/source")"
ID="$(printf '%s\n' "$OUT" | sed -n 's/^HARNESS_SESSION_ID=//p')";WORK="$(printf '%s\n' "$OUT" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
mkdir -p "$WORK/.harness/codex/sessions"
printf '{"type":"session_meta","payload":{"id":"%s"}}\n' "$NATIVE" > "$WORK/.harness/codex/sessions/$NATIVE.jsonl"
"$ROOT/bin/session-isolation.sh" close "$ID"
# Web's Python boundary strips ps padding. Force macOS-style trailing padding
# on every platform so both normalized callers and older stored holds are covered.
export HARNESS_TEST_REAL_PS="$(command -v ps)"
mkdir "$TMP/bin"
cat > "$TMP/bin/ps" <<'SH'
#!/usr/bin/env bash
"$HARNESS_TEST_REAL_PS" "$@" | sed 's/^[[:space:]]*//;s/[[:space:]]*$/    /'
SH
chmod +x "$TMP/bin/ps"
export PATH="$TMP/bin:$PATH"
START="$(ps -o lstart= -p "$$" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
python3 - "$HARNESS_SESSION_STATE_HOME/sessions/$ID/resume-hold-test.json" "$ID" "$TMP/source" "$$" "$START" <<'PY'
import json,sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({'pid':int(sys.argv[4]),'start':sys.argv[5]+'    ','source_root':sys.argv[3],'session_id':sys.argv[2],'request_id':'test-request'}))
PY
"$ROOT/bin/session-isolation.sh" gc
[[ -d "$WORK" ]] || { echo 'FAIL: a verified live resume hold must retain the root'; exit 1; }
[[ -f "$HARNESS_SESSION_STATE_HOME/sessions/$ID/resume-hold-test.json" ]] || { echo 'FAIL: old padded live holds must not be removed as stale'; exit 1; }
"$ROOT/bin/session-isolation.sh" release-hold "$ID" test-request
[[ ! -e "$HARNESS_SESSION_STATE_HOME/sessions/$ID/resume-hold-test-request.json" ]] || { echo 'FAIL: release-hold must remove only its own private hold'; exit 1; }
"$ROOT/bin/session-isolation.sh" reserve-hold "$ID" test-reserved "$$" "$START" "$TMP/source"
[[ -f "$HARNESS_SESSION_STATE_HOME/sessions/$ID/resume-hold-test-reserved.json" ]] || { echo 'FAIL: reserve-hold must publish a validated owner hold'; exit 1; }
python3 - "$HARNESS_SESSION_STATE_HOME/sessions/$ID/resume-hold-test-reserved.json" "$START" <<'PY'
import json,sys
assert json.load(open(sys.argv[1]))['start'] == sys.argv[2]
PY
"$ROOT/bin/session-isolation.sh" release-hold "$ID" test-reserved
"$ROOT/bin/session-isolation.sh" reserve-hold "$ID" test-padded "$$" "  $START    " "$TMP/source"
"$ROOT/bin/session-isolation.sh" release-hold "$ID" test-padded
if "$ROOT/bin/session-isolation.sh" reserve-hold "$ID" test-empty-start 2147483647 '    ' "$TMP/source"; then
  echo 'FAIL: whitespace-only time and missing PID must not authorize a hold'; exit 1
fi
[[ ! -e "$HARNESS_SESSION_STATE_HOME/sessions/$ID/resume-hold-test-empty-start.json" ]] || { echo 'FAIL: rejected reservation must not publish a hold'; exit 1; }
printf '{not json}\n' > "$HARNESS_SESSION_STATE_HOME/sessions/$ID/resume-hold-invalid.json"
"$ROOT/bin/session-isolation.sh" gc
[[ -d "$WORK" ]] || { echo 'FAIL: an invalid hold must fail closed and retain the root'; exit 1; }
rm "$HARNESS_SESSION_STATE_HOME/sessions/$ID/resume-hold-invalid.json"
python3 - "$HARNESS_SESSION_STATE_HOME/sessions/$ID/resume-hold-reused.json" "$ID" "$TMP/source" "$$" <<'PY'
import json,sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({'pid':int(sys.argv[4]),'start':'not this process','source_root':sys.argv[3],'session_id':sys.argv[2],'request_id':'reused'}))
PY
"$ROOT/bin/session-isolation.sh" gc
[[ ! -e "$HARNESS_SESSION_STATE_HOME/sessions/$ID/resume-hold-reused.json" ]] || { echo 'FAIL: a reused PID hold must be removed'; exit 1; }
echo 'PASS: exact transcript archive, auth exclusion, fail-closed GC and tomb preservation'
