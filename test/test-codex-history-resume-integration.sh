#!/usr/bin/env zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# Never depend on the host Codex app or ~/.codex plugin cache.
export HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE="$TMP/no-marketplace"
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

# The import carries the origin's grant, but only for a top-level launch and a
# record bound to the catalog row's origin (state home, source root, session id).
# The real context check needs a terminal, so the top-level answer is stubbed in
# a copy of the launcher bin directory. Each case imports into a fresh fixture
# (a second import of one history resumes the first import's session instead).
TOP_BIN="$TMP/topbin"; NESTED_BIN="$TMP/nestedbin"
cp -R "$ROOT/bin" "$TOP_BIN"; cp -R "$ROOT/bin" "$NESTED_BIN"
printf '%s\n' '#!/usr/bin/env python3' 'print("top_level=1")' > "$TOP_BIN/harness_launch_context.py"
printf '%s\n' '#!/usr/bin/env python3' 'print("top_level=0 reason=agent_ancestor")' 'raise SystemExit(1)' > "$NESTED_BIN/harness_launch_context.py"
run_import_case() { # <name> <bin dir> <record session id | none>; sets CASE_LOG, CASE_ERR
  local name="$1" bindir="$2" bound="$3" root="$TMP/import-$1" harness state iso source rollout home_old
  harness="$root/harness"; state="$root/xdg/harness-launcher"; iso="$ISOLATION_ID"
  mkdir -p "$harness/config"
  git -C "$harness" init -q -b main
  git -C "$harness" config user.name fixture
  git -C "$harness" config user.email fixture@example.invalid
  print -r -- 'HARNESS_NAME="fixture"' 'HARNESS_PREFIX="fixture"' "HARNESS_SESSION_STATE_HOME=\"$state\"" > "$harness/config/launcher.env"
  print -r -- fixture > "$harness/tracked"
  git -C "$harness" add config/launcher.env tracked && git -C "$harness" commit -qm fixture
  source="$(cd "$harness" && pwd -P)"
  mkdir -p "$state/sessions/$iso" "$state/worktrees/$iso/.harness/codex/sessions/2026/10/10"
  print -r -- "$source" > "$state/sessions/$iso/source-root"
  print -r -- "$state/worktrees/$iso" > "$state/sessions/$iso/session-root"
  stat -f '%d %i' "$state/worktrees/$iso" > "$state/sessions/$iso/root-inode"
  home_old="$state/worktrees/$iso/.harness/codex"
  rollout="$home_old/sessions/2026/10/10/rollout-2026-10-10T01-02-03-$NATIVE_ID.jsonl"
  print -r -- '{"type":"session_meta","payload":{"id":"'"$NATIVE_ID"'"}}' > "$rollout"
  python3 "$ROOT/bin/codex-history.py" snapshot --source "$home_old" --catalog "$state/native-history" \
    --receipt "$state/sessions/$iso/native-history-snapshot.json" --source-root "$source" \
    --isolation-id "$iso" --runtime-revision "$REVISION" >/dev/null
  rm -rf "$home_old"
  print -r -- 'state=DELIVERED' > "$state/sessions/$iso/journal"
  if [[ "$bound" != none ]]; then
    printf '{"hook_event_name":"SessionStart","source":"startup","session_id":"%s"}' "$NATIVE_ID" \
      | env -u HARNESS_SESSION_ID HARNESS_SESSION_STATE_HOME="$state" HARNESS_LAUNCH_SOURCE_ROOT="$source" HARNESS_LAUNCH_ISOLATED=1 \
          HARNESS_SESSION_ID="$bound" HARNESS_LAUNCH_APPROVAL=never HARNESS_LAUNCH_SANDBOX=danger-full-access \
          python3 "$ROOT/bin/harness-launch-record" codex
  fi
  CASE_LOG="$root/codex.log"; CASE_ERR="$root/import.err"
  (
    export PATH="$TMP:$PATH" XDG_STATE_HOME="$root/xdg" HARNESS_SESSION_STATE_HOME="$state" FAKE_CODEX_LOG="$CASE_LOG" NATIVE_ID
    source "$ROOT/bin/aliases.zsh"
    _HARNESS_LAUNCHER_BIN="$bindir"
    _harness_launcher_run "$harness" codex resume "$NATIVE_ID"
  ) </dev/null >/dev/null 2>"$CASE_ERR" || { echo "FAIL: $name import launch failed"; cat "$CASE_ERR"; exit 1; }
}
run_import_case top "$TOP_BIN" "$ISOLATION_ID"
grep -Fq -- "-a never -s danger-full-access" "$CASE_LOG" || { echo 'FAIL: top-level import must carry the origin grant'; cat "$CASE_LOG" "$CASE_ERR"; exit 1; }
! grep -Fq relaunch "$CASE_ERR" || { echo 'FAIL: hint printed although the grant applied'; exit 1; }
run_import_case nested "$NESTED_BIN" "$ISOLATION_ID"
! grep -Fq -- "-a never" "$CASE_LOG" || { echo 'FAIL: nested import inherited the origin grant'; cat "$CASE_LOG"; exit 1; }
grep -Fq relaunch "$CASE_ERR" || { echo 'FAIL: nested import must print the default-permissions hint'; cat "$CASE_ERR"; exit 1; }
run_import_case mis-bound "$TOP_BIN" bbbbbbbb-2222-4333-8444-5555555555b2
! grep -Fq -- "-a never" "$CASE_LOG" || { echo 'FAIL: record bound to another session was applied'; exit 1; }
run_import_case real-context "$ROOT/bin" "$ISOLATION_ID"
! grep -Fq -- "-a never" "$CASE_LOG" || { echo 'FAIL: non-terminal import inherited the origin grant (real context check)'; exit 1; }
echo 'PASS: history import carries the bound origin grant only for a top-level launch'

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
