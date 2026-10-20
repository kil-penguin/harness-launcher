#!/usr/bin/env bash
set -euo pipefail

# root_intact accepts a whole-volume device renumbering (same inode, root on
# the device of its worktrees directory) and nothing else.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# The script dispatches on argv when run, so load only the two functions.
source <(awk '/^root_identity\(\)/{on=1} /^use_session_git\(\)/{on=0} on' "$ROOT/bin/session-isolation.sh")
declare -F root_intact >/dev/null || { echo 'FAIL: root_intact not extracted'; exit 1; }

mkdir -p "$TMP/worktrees/id" "$TMP/sessions/id"
root="$TMP/worktrees/id"; dir="$TMP/sessions/id"
read -r dev ino < <(root_identity "$root")

orig_root_identity() { stat -f '%d %i' "$1" 2>/dev/null || stat -c '%d %i' "$1"; }
# Seam: report $FAKE_DEV as the device of $FAKE_PATH (no real mount needed).
FAKE_PATH="" FAKE_DEV=""
root_identity() {
  local value; value="$(orig_root_identity "$1")" || return 1
  if [[ "$1" == "$FAKE_PATH" ]]; then printf '%s %s\n' "$FAKE_DEV" "${value#* }"; else printf '%s\n' "$value"; fi
}

expect() {  # expect <ok|refuse> <label>
  local rc=0; root_intact "$dir" "$root" || rc=$?
  if [[ "$1" == ok && "$rc" != 0 ]]; then echo "FAIL: $2 must be accepted"; exit 1; fi
  if [[ "$1" == refuse && "$rc" == 0 ]]; then echo "FAIL: $2 must be refused"; exit 1; fi
}

printf '%s %s\n' "$dev" "$ino" > "$dir/root-inode"
expect ok 'exact record'
printf '%s %s\n' "$((dev + 1))" "$ino" > "$dir/root-inode"
expect ok 'renumbered device, same inode, root on its worktrees device'
printf '%s %s\n' "$((dev + 1))" "$((ino + 1))" > "$dir/root-inode"
expect refuse 'different inode'
printf '%s %s\n' "$dev" "$((ino + 1))" > "$dir/root-inode"
expect refuse 'different inode, same device'
printf '%s %s\n' "$((dev + 1))" "$ino" > "$dir/root-inode"
FAKE_PATH="$root" FAKE_DEV="$((dev + 5))"
expect refuse 'root on a device other than its worktrees directory'
FAKE_PATH="" FAKE_DEV=""
printf 'garbage\n' > "$dir/root-inode"
expect refuse 'malformed record'
printf '%s %s\n' "$((dev + 1))" "$ino" > "$dir/root-inode"
rmdir "$root"; mkdir "$root"
expect refuse 'root replaced by a fresh directory'
rm -rf "$root"; ln -s "$TMP" "$root"
expect refuse 'root replaced by a symlink'
echo 'PASS: root_intact tolerates device renumbering only for the same inode on the worktrees device'
