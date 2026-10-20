#!/usr/bin/env zsh
set -e

# After a reboot APFS renumbers the volume device, so every recorded
# root-inode names a stale device. An OPEN isolated owner whose runtime lease
# nobody holds must still resume, and only through the launcher entry
# (harness-exec <root> --isolated-session <uid>); the session root stays usable.
unset CMUX_WORKSPACE_ID CMUX_TAB_ID CMUX_SURFACE_ID
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
HARNESS="$TMP/harness"
STATE="$TMP/state"
export CLAUDE_CONFIG_DIR="$TMP/claude-config"
unset CLAUDE_CODE_PLUGIN_CACHE_DIR CLAUDE_CODE_USE_COWORK_PLUGINS HARNESS_CLAUDE_PLUGIN_MIRROR
mkdir -p "$HARNESS/config"
git -C "$HARNESS" init -q -b main
git -C "$HARNESS" config user.email test@example.invalid
git -C "$HARNESS" config user.name test
print -r -- 'HARNESS_NAME="test"' 'HARNESS_PREFIX="test"' > "$HARNESS/config/launcher.env"
print -r -- tracked > "$HARNESS/tracked.txt"
git -C "$HARNESS" add config/launcher.env tracked.txt && git -C "$HARNESS" commit -qm initial

git init -q --bare -b main "$TMP/origin.git"
git -C "$HARNESS" remote add origin "$TMP/origin.git"
git -C "$HARNESS" push -q origin main
git -C "$HARNESS" fetch -q origin main:refs/remotes/origin/main

cat > "$TMP/claude" <<'STUB'
#!/usr/bin/env bash
printf 'PWD=%s\nSESSION=%s\n' "$PWD" "${HARNESS_SESSION_ROOT:-}" > "$ISOLATED_LOG"
STUB
chmod +x "$TMP/claude"

created="$(HARNESS_SESSION_STATE_HOME="$STATE" "$ROOT/bin/session-isolation.sh" create "$HARNESS")"
id="$(print -r -- "$created" | sed -n 's/^HARNESS_SESSION_ID=//p')"
session_root="$(print -r -- "$created" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
[[ -n "$id" && -d "$session_root" ]] || { echo 'FAIL: fixture session was not created'; exit 1; }
record="$STATE/sessions/$id"
[[ "$(sed -n 's/^state=//p' "$record/journal")" == OPEN ]] || { echo 'FAIL: fixture session must be OPEN'; exit 1; }
read -r dev ino < "$record/root-inode"
print -r -- "$((dev + 1)) $ino" > "$record/root-inode"

# Nobody holds the lease: a bare resume is refused, the launcher entry takes it.
if HARNESS_SESSION_STATE_HOME="$STATE" "$ROOT/bin/session-isolation.sh" resume "$HARNESS" "$id" 2>/dev/null; then
  echo 'FAIL: a direct resume without the launcher lease must be refused'; exit 1
fi
(
  export PATH="$TMP:$PATH" HARNESS_SESSION_STATE_HOME="$STATE" ISOLATED_LOG="$TMP/log"
  source "$ROOT/bin/aliases.zsh"
  _harness_launcher_run "$HARNESS" --isolated-session "$id" base
) 2>"$TMP/resume.err" || { cat "$TMP/resume.err"; echo 'FAIL: renumbered OPEN owner must resume through the launcher entry'; exit 1; }
[[ "$(sed -n 's/^SESSION=//p' "$TMP/log")" == "$session_root" && "$(sed -n 's/^PWD=//p' "$TMP/log")" == "$session_root" ]] || {
  echo 'FAIL: resume must run in the recorded session root'; exit 1; }
[[ -d "$session_root" ]] || { echo 'FAIL: the renumbered root must not be retired'; exit 1; }

# The broker keeps trusting the renumbered root and still refuses a replaced one.
fresh() {
  created="$(HARNESS_SESSION_STATE_HOME="$STATE" "$ROOT/bin/session-isolation.sh" create "$HARNESS")"
  fid="$(print -r -- "$created" | sed -n 's/^HARNESS_SESSION_ID=//p')"
  froot="$(print -r -- "$created" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
  read -r fdev fino < "$STATE/sessions/$fid/root-inode"
  printf 'work\n' > "$froot/work.txt"
}
fresh
print -r -- "$((fdev + 1)) $((fino + 1))" > "$STATE/sessions/$fid/root-inode"
rc=0
HARNESS_SESSION_STATE_HOME="$STATE" "$ROOT/bin/session-isolation.sh" submit "$fid" 2>"$TMP/submit.err" || rc=$?
[[ "$rc" == 10 ]] || { cat "$TMP/submit.err"; echo "FAIL: a different inode must refuse submit with exit 10 (rc=$rc)"; exit 1; }
fresh
print -r -- "$((fdev + 1)) $fino" > "$STATE/sessions/$fid/root-inode"
HARNESS_SESSION_STATE_HOME="$STATE" "$ROOT/bin/session-isolation.sh" submit "$fid" >/dev/null 2>"$TMP/submit.err" \
  || { cat "$TMP/submit.err"; echo 'FAIL: the renumbered root must submit'; exit 1; }
echo 'PASS: a renumbered OPEN isolated owner resumes only through the launcher entry'
