#!/usr/bin/env zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
HARNESS="$TMP/harness"
STATE="$TMP/xdg/harness-launcher"
NATIVE_ID='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
ISOLATION_ID='11111111-2222-4333-8444-555555555555'
REVISION="$(printf 'a%.0s' {1..40})"

mkdir -p "$HARNESS/config"
git -C "$HARNESS" init -q -b main
git -C "$HARNESS" config user.name fixture
git -C "$HARNESS" config user.email fixture@example.invalid
print -r -- 'HARNESS_NAME="fixture"' 'HARNESS_PREFIX="fixture"' "HARNESS_SESSION_STATE_HOME=\"$STATE\"" > "$HARNESS/config/launcher.env"
print -r -- fixture > "$HARNESS/tracked"
git -C "$HARNESS" add config/launcher.env tracked && git -C "$HARNESS" commit -qm fixture
SOURCE="$(cd "$HARNESS" && pwd -P)"

mkdir -p "$STATE/sessions/$ISOLATION_ID" "$STATE/worktrees/$ISOLATION_ID/.harness/codex/sessions/2026/10/10"
print -r -- "$SOURCE" > "$STATE/sessions/$ISOLATION_ID/source-root"
print -r -- "$STATE/worktrees/$ISOLATION_ID" > "$STATE/sessions/$ISOLATION_ID/session-root"
stat -f '%d %i' "$STATE/worktrees/$ISOLATION_ID" > "$STATE/sessions/$ISOLATION_ID/root-inode"
HOME_OLD="$STATE/worktrees/$ISOLATION_ID/.harness/codex"
ROLLOUT="$HOME_OLD/sessions/2026/10/10/rollout-2026-10-10T01-02-03-$NATIVE_ID.jsonl"
print -r -- '{"type":"session_meta","payload":{"id":"'"$NATIVE_ID"'"}}' > "$ROLLOUT"
print -r -- '{"token":"must-not-restore"}' > "$HOME_OLD/auth.json"

python3 "$ROOT/bin/codex-history.py" snapshot --source "$HOME_OLD" --catalog "$STATE/native-history" \
  --receipt "$STATE/sessions/$ISOLATION_ID/native-history-snapshot.json" --source-root "$SOURCE" \
  --isolation-id "$ISOLATION_ID" --runtime-revision "$REVISION" >/dev/null
rm -rf "$HOME_OLD"
print -r -- 'state=DELIVERED' > "$STATE/sessions/$ISOLATION_ID/journal"
python3 "$ROOT/bin/codex-history.py" catalog --source-root "$SOURCE" --state-home "$STATE" > "$TMP/catalog.json"
grep -Fq "$NATIVE_ID" "$TMP/catalog.json" || { echo 'FAIL: fixture snapshot is absent from local catalog'; cat "$TMP/catalog.json"; exit 1; }

cat > "$TMP/codex" <<'EOF'
#!/usr/bin/env sh
printf 'home=%s\nargs=%s\n' "$CODEX_HOME" "$*" > "$FAKE_CODEX_LOG"
test -f "$CODEX_HOME/sessions/2026/10/10/rollout-2026-10-10T01-02-03-$NATIVE_ID.jsonl" && echo artifact=yes >> "$FAKE_CODEX_LOG" || echo artifact=no >> "$FAKE_CODEX_LOG"
if test -f "$CODEX_HOME/auth.json" && grep -Fq must-not-restore "$CODEX_HOME/auth.json"; then echo auth=restored >> "$FAKE_CODEX_LOG"; else echo auth=excluded >> "$FAKE_CODEX_LOG"; fi
EOF
chmod +x "$TMP/codex"

(
  export PATH="$TMP:$PATH" XDG_STATE_HOME="$TMP/xdg" HARNESS_SESSION_STATE_HOME="$STATE" FAKE_CODEX_LOG="$TMP/codex.log" NATIVE_ID
  source "$ROOT/bin/aliases.zsh"
  _harness_launcher_run "$HARNESS" codex resume "$NATIVE_ID"
)

grep -Fq "home=$STATE/worktrees/" "$TMP/codex.log" || { echo 'FAIL: historical resume did not use a fresh local isolation'; cat "$TMP/codex.log"; exit 1; }
grep -Fq "args=resume " "$TMP/codex.log" && grep -Fq "$NATIVE_ID" "$TMP/codex.log" || { echo 'FAIL: native Codex did not receive the original UUID'; cat "$TMP/codex.log"; exit 1; }
grep -Fqx artifact=yes "$TMP/codex.log" || { echo 'FAIL: generated native home did not receive archived history'; exit 1; }
grep -Fqx auth=excluded "$TMP/codex.log" || { echo 'FAIL: generated native home received archived authority'; exit 1; }
[[ "$(<"$STATE/sessions/$ISOLATION_ID/journal")" == 'state=DELIVERED' ]] || { echo 'FAIL: historical journal was rewritten'; exit 1; }
find "$STATE/sessions" -name native-history-restore.json -type f | grep -q . || { echo 'FAIL: fresh session has no private restore receipt'; exit 1; }

assert_invalid_history() {
  local name="$1" payload="$2" case_root case_harness case_state rollout before after rc=0
  case_root="$TMP/invalid-$name"
  case_harness="$case_root/harness"
  case_state="$case_root/state"
  mkdir -p "$case_harness/config" "$case_harness/.harness/codex/sessions/2026/10/10"
  git -C "$case_harness" init -q -b main
  git -C "$case_harness" config user.name fixture
  git -C "$case_harness" config user.email fixture@example.invalid
  print -r -- 'HARNESS_NAME="fixture"' 'HARNESS_PREFIX="fixture"' "HARNESS_SESSION_STATE_HOME=\"$case_state\"" > "$case_harness/config/launcher.env"
  print -r -- fixture > "$case_harness/tracked"
  git -C "$case_harness" add config/launcher.env tracked && git -C "$case_harness" commit -qm fixture
  rollout="$case_harness/.harness/codex/sessions/2026/10/10/rollout-$NATIVE_ID.jsonl"
  print -rn -- "$payload" > "$rollout"
  before="$(shasum -a 256 "$rollout" | awk '{print $1}')"
  if (
    export PATH="$TMP:$PATH" HARNESS_SESSION_STATE_HOME="$case_state" FAKE_CODEX_LOG="$case_root/native.log" NATIVE_ID
    source "$ROOT/bin/aliases.zsh"
    _harness_launcher_run "$case_harness" codex resume "$NATIVE_ID"
  ); then rc=0; else rc=$?; fi
  [[ "$rc" == 2 ]] || { echo "FAIL: $name history must fail closed with rc 2 (got $rc)"; exit 1; }
  [[ ! -e "$case_root/native.log" ]] || { echo "FAIL: $name history launched fake native Codex"; exit 1; }
  [[ ! -e "$case_state/worktrees" ]] || { echo "FAIL: $name history created a fresh isolation"; exit 1; }
  after="$(shasum -a 256 "$rollout" | awk '{print $1}')"
  [[ "$after" == "$before" ]] || { echo "FAIL: $name history rewrote original rollout"; exit 1; }
}

assert_invalid_history empty ''
assert_invalid_history malformed '{not-json'
assert_invalid_history headerless '{"type":"response_item","id":"no-session-meta"}'

echo 'PASS: public durable Codex resume restores a snapshot into a fresh local home'
