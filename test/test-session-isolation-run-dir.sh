#!/usr/bin/env bash
set -euo pipefail

# Break protected: an isolated TUI may retain a source-relative cwd only when
# the session broker can bind it to this session's recorded clone and source.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ISOLATION="$ROOT/bin/session-isolation.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
STATE="$TMP/state"
SOURCE="$TMP/source"
OUTSIDE="$TMP/outside"

mkdir -p "$SOURCE/config" "$SOURCE/nested" "$SOURCE/projects/product" "$OUTSIDE"
git -C "$SOURCE" init -q -b main
git -C "$SOURCE" config user.email test@example.invalid
git -C "$SOURCE" config user.name test
: > "$SOURCE/config/launcher.env"
: > "$SOURCE/nested/file"
git -C "$SOURCE" add -A
git -C "$SOURCE" commit -qm initial

created="$(HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" create "$SOURCE")"
id="$(printf '%s\n' "$created" | sed -n 's/^HARNESS_SESSION_ID=//p')"
session_root="$(printf '%s\n' "$created" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
[[ -n "$id" && -n "$session_root" ]] || { echo 'FAIL: fixture session was not created' >&2; exit 1; }

want_nested="$(cd "$session_root/nested" && pwd -P)"
got_nested="$(HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resolve-run-dir "$SOURCE" "$id" "$SOURCE/nested")"
[[ "$got_nested" == "$want_nested" ]] || { echo "FAIL: source nested cwd must map to clone; got $got_nested" >&2; exit 1; }

# A second validation at the actual TUI receives the already mapped clone cwd.
got_again="$(HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resolve-run-dir "$SOURCE" "$id" "$want_nested")"
[[ "$got_again" == "$want_nested" ]] || { echo 'FAIL: clone cwd revalidation changed the directory' >&2; exit 1; }
# Previous releases wrote owned 0644 records; safe read permissions stay valid.
chmod 644 "$STATE/sessions/$id/root-inode"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resolve-run-dir "$SOURCE" "$id" "$SOURCE/nested" >/dev/null
chmod 640 "$STATE/sessions/$id/root-inode"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resolve-run-dir "$SOURCE" "$id" "$SOURCE/nested" >/dev/null
chmod 666 "$STATE/sessions/$id/root-inode"
if HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resolve-run-dir "$SOURCE" "$id" "$SOURCE/nested" >/dev/null 2>&1; then
  echo 'FAIL: writable root inode record accepted' >&2; exit 1
fi
chmod 600 "$STATE/sessions/$id/root-inode"

want_project="$(cd "$SOURCE/projects/product" && pwd -P)"
got_project="$(HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resolve-run-dir "$SOURCE" "$id" "$SOURCE/projects/product")"
[[ "$got_project" == "$want_project" ]] || { echo "FAIL: broker-created projects link must retain product cwd; got $got_project" >&2; exit 1; }

# Valid source/session environment must not authorize a different config root.
mkdir -p "$OUTSIDE/config"
printf ': > "%s"\n' "$TMP/wrong-config-sourced" > "$OUTSIDE/config/launcher.env"
if HARNESS_DIR="$OUTSIDE" HARNESS_NAME=test HARNESS_SOURCE_ROOT="$SOURCE" HARNESS_SESSION_ID="$id" HARNESS_RUN_DIR="$want_nested" HARNESS_SESSION_STATE_HOME="$STATE" bash "$ROOT/bin/launcher.sh" </dev/null >/dev/null 2>&1; then
  echo 'FAIL: TUI accepted an unrelated harness config root' >&2; exit 1
fi
[[ ! -e "$TMP/wrong-config-sourced" ]] || { echo 'FAIL: TUI sourced unrelated config before broker validation' >&2; exit 1; }

rm "$STATE/sessions/$id/root-inode"
if HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resolve-run-dir "$SOURCE" "$id" "$SOURCE/nested" >/dev/null 2>&1; then
  echo 'FAIL: a missing root inode must not fall back to a path-only binding' >&2
  exit 1
fi
root_identity() { stat -f '%d %i' "$1"; }
root_identity "$session_root" > "$STATE/sessions/$id/root-inode"
chmod 600 "$STATE/sessions/$id/root-inode"

if HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resolve-run-dir "$SOURCE" "$id" "$OUTSIDE" >/dev/null 2>&1; then
  echo 'FAIL: an outside cwd must not receive a session binding' >&2
  exit 1
fi
if HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resolve-run-dir "$OUTSIDE" "$id" "$SOURCE/nested" >/dev/null 2>&1; then
  echo 'FAIL: a source mismatch must not receive a session binding' >&2
  exit 1
fi

reject() {
  if HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resolve-run-dir "$SOURCE" "$id" "$1" >/dev/null 2>&1; then
    echo "FAIL: $2" >&2; exit 1
  fi
}
for record in source-root session-root journal; do
  chmod 666 "$STATE/sessions/$id/$record"
  reject "$SOURCE/nested" "writable $record accepted"
  chmod 644 "$STATE/sessions/$id/$record"
  ln "$STATE/sessions/$id/$record" "$TMP/hardlink"
  reject "$SOURCE/nested" "hardlinked $record accepted"
  rm "$TMP/hardlink"
done
mv "$STATE/sessions" "$STATE/real-sessions"
ln -s "$STATE/real-sessions" "$STATE/sessions"
reject "$SOURCE/nested" 'symlink session parent accepted'
rm "$STATE/sessions"
mv "$STATE/real-sessions" "$STATE/sessions"
ln -s "$STATE" "$TMP/linked-state"
if HARNESS_SESSION_STATE_HOME="$TMP/linked-state" "$ISOLATION" resolve-run-dir "$SOURCE" "$id" "$SOURCE/nested" >/dev/null 2>&1; then
  echo 'FAIL: symlink state home accepted' >&2; exit 1
fi
ln -s "$OUTSIDE" "$session_root/escape"
mkdir -p "$SOURCE/escape"
reject "$SOURCE/escape" 'non-projects symlink escape accepted'
reject "$session_root/escape" 'direct clone symlink escape accepted'
cp "$STATE/sessions/$id/root-inode" "$TMP/inode-backup"
printf '0 0\n' > "$STATE/sessions/$id/root-inode"
reject "$SOURCE/nested" 'tampered inode accepted'
cp "$TMP/inode-backup" "$STATE/sessions/$id/root-inode"
mv "$STATE/sessions/$id/root-inode" "$TMP/inode-real"
ln -s "$TMP/inode-real" "$STATE/sessions/$id/root-inode"
reject "$SOURCE/nested" 'symlink inode accepted'
rm "$STATE/sessions/$id/root-inode"
mv "$TMP/inode-real" "$STATE/sessions/$id/root-inode"
cp "$STATE/sessions/$id/session-root" "$TMP/root-backup"
printf '%s\n' "$OUTSIDE" > "$STATE/sessions/$id/session-root"
reject "$SOURCE/nested" 'tampered root record accepted'
cp "$TMP/root-backup" "$STATE/sessions/$id/session-root"
cp "$STATE/sessions/$id/journal" "$TMP/journal-backup"
printf 'state=TAMPERED\nidentity=\nheartbeat=2026-01-01T00:00:00Z\n' > "$STATE/sessions/$id/journal"
reject "$SOURCE/nested" 'tampered journal accepted with otherwise valid mapping'
cp "$TMP/journal-backup" "$STATE/sessions/$id/journal"

rm -rf "$session_root/nested"
if HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resolve-run-dir "$SOURCE" "$id" "$SOURCE/nested" >/dev/null 2>&1; then
  echo 'FAIL: a missing clone subdirectory must not fall back to source cwd' >&2
  exit 1
fi

rm "$session_root/projects"
ln -s "$OUTSIDE" "$session_root/projects"
if HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resolve-run-dir "$SOURCE" "$id" "$SOURCE/projects/product" >/dev/null 2>&1; then
  echo 'FAIL: a swapped projects symlink must not receive a session binding' >&2
  exit 1
fi

printf 'state=TAMPERED\nidentity=\nheartbeat=2026-01-01T00:00:00Z\n' > "$STATE/sessions/$id/journal"
if HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resolve-run-dir "$SOURCE" "$id" "$SOURCE/projects/product" >/dev/null 2>&1; then
  echo 'FAIL: a tampered journal must not receive a session binding' >&2
  exit 1
fi


echo 'PASS: session broker binds isolated run directories without ambient trust'
