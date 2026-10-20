#!/usr/bin/env zsh
set -e

# A managed launcher precreates a UUID then enters through --isolated-session.
# The interactive path must reach its runtime without foreground GC.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
HARNESS="$TMP/harness" STATE="$TMP/state" BIN="$TMP/bin"
mkdir -p "$HARNESS/config" "$BIN"
git -C "$HARNESS" init -q -b main
git -C "$HARNESS" config user.email test@example.invalid
git -C "$HARNESS" config user.name test
print -r -- 'HARNESS_NAME="test"' 'HARNESS_PREFIX="test"' > "$HARNESS/config/launcher.env"
git -C "$HARNESS" add -A && git -C "$HARNESS" commit -qm initial

cp "$ROOT/bin/aliases.zsh" "$ROOT/bin/harness-common.sh" "$ROOT/bin/session-isolation.sh" "$ROOT/bin/harness_session_creation.py" "$BIN/"
cp "$ROOT/bin/launcher.sh" "$ROOT/bin/harness-workspace-choice" "$BIN/"
chmod +x "$BIN/session-isolation.sh" "$BIN/harness-workspace-choice"
mv "$BIN/session-isolation.sh" "$BIN/session-isolation-real.sh"
cat > "$BIN/session-isolation.sh" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == gc ]]; then printf 'GC\n' >> "$GC_LOG"; fi
exec "$(dirname "$0")/session-isolation-real.sh" "$@"
EOF
chmod +x "$BIN/session-isolation.sh"
cat > "$BIN/claude" <<'EOF'
#!/usr/bin/env bash
printf 'RUNTIME\n' >> "$GC_LOG"
EOF
chmod +x "$BIN/claude"

created="$(HARNESS_SESSION_STATE_HOME="$STATE" "$BIN/session-isolation-real.sh" create "$HARNESS")"
id="$(print -r -- "$created" | sed -n 's/^HARNESS_SESSION_ID=//p')"
ROOT="$ROOT" BIN="$BIN" HARNESS="$HARNESS" STATE="$STATE" ID="$id" GC_LOG="$TMP/order" expect <<'EXPECT'
set timeout 12
log_user 0
spawn env PATH=$env(BIN):/usr/bin:/bin HARNESS_SESSION_STATE_HOME=$env(STATE) GC_LOG=$env(GC_LOG) zsh -c {
  source "$BIN/aliases.zsh"
  _harness_launcher_run_session() { printf 'RUNTIME\n' >> "$GC_LOG"; }
  _harness_launcher_run "$HARNESS" --isolated-session "$ID" base
}
expect eof
catch wait result
exit [lindex $result 3]
EXPECT
[[ "$(cat "$TMP/order")" == RUNTIME ]] || { echo 'FAIL: precreated interactive session ran foreground GC before runtime' >&2; cat "$TMP/order" >&2; exit 1; }

mkdir -p "$HARNESS/nested"
: > "$HARNESS/nested/fixture"
git -C "$HARNESS" add -A && git -C "$HARNESS" commit -qm nested
failed="$(HARNESS_SESSION_STATE_HOME="$STATE" "$BIN/session-isolation-real.sh" create "$HARNESS")"
failed_id="$(print -r -- "$failed" | sed -n 's/^HARNESS_SESSION_ID=//p')"
failed_root="$(print -r -- "$failed" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
rm -rf "$failed_root/nested"
if ROOT="$ROOT" BIN="$BIN" HARNESS="$HARNESS" STATE="$STATE" ID="$failed_id" GC_LOG="$TMP/failed-order" expect <<'EXPECT'
set timeout 12
log_user 0
spawn env PATH=$env(BIN):/usr/bin:/bin HARNESS_SESSION_STATE_HOME=$env(STATE) GC_LOG=$env(GC_LOG) zsh -c {
  source "$BIN/aliases.zsh"
  _harness_launcher_run_session() { printf 'RUNTIME\n' >> "$GC_LOG"; }
  _harness_launcher_run "$HARNESS" --cwd "$HARNESS/nested" --isolated-session "$ID" base
}
expect eof
catch wait result
exit [lindex $result 3]
EXPECT
then
  echo 'FAIL: an absent clone cwd must refuse before runtime launch' >&2
  exit 1
fi
[[ ! -e "$TMP/failed-order" ]] || { echo 'FAIL: cwd binding failure reached runtime' >&2; exit 1; }
grep -Eqx 'state=(CLOSED|ABANDONED)' "$STATE/sessions/$failed_id/journal" || {
  echo 'FAIL: cwd binding failure must finish the acquired isolated session consistently' >&2
  cat "$STATE/sessions/$failed_id/journal" >&2
  exit 1
}
print -r -- ": > '$TMP/config-sentinel'" > "$HARNESS/config/launcher.env"
ROOT="$ROOT" BIN="$BIN" HARNESS="$HARNESS" STATE="$STATE" expect <<'EXPECT'
set timeout 12
log_user 0
spawn env PATH=$env(BIN):/usr/bin:/bin HARNESS_SESSION_STATE_HOME=$env(STATE) zsh -c {
  source "$BIN/aliases.zsh"
  _harness_launcher_run "$HARNESS"
}
expect "Workspace"
send -- "q\r"
expect eof
catch wait result
exit [lindex $result 3]
EXPECT
[[ ! -e "$TMP/config-sentinel" ]] || { echo 'FAIL: cancel evaluated launcher.env before workspace choice' >&2; exit 1; }
echo 'PASS: precreated interactive session skips foreground GC'
