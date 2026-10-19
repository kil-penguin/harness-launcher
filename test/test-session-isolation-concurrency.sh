#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ISOLATION="$ROOT/bin/session-isolation.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SOURCE="$TMP/source"; REMOTE="$TMP/remote.git"; STATE="$TMP/state"

git init -q -b main "$SOURCE"
git -C "$SOURCE" config user.email test@example.invalid
git -C "$SOURCE" config user.name test
mkdir -p "$SOURCE/core/bin"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$SOURCE/core/bin/auto-deliver.sh"; chmod +x "$SOURCE/core/bin/auto-deliver.sh"
printf seed > "$SOURCE/seed"; git -C "$SOURCE" add seed core/bin/auto-deliver.sh; git -C "$SOURCE" commit -qm initial
git init --bare -q "$REMOTE"; git -C "$SOURCE" remote add origin "$REMOTE"; git -C "$SOURCE" push -q -u origin main
# This synthetic origin publishes main regardless of the host init.defaultBranch.
git --git-dir="$REMOTE" symbolic-ref HEAD refs/heads/main

ids=()
for i in $(seq 1 20); do
  created="$(HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" create "$SOURCE")"
  id="$(printf '%s\n' "$created" | sed -n 's/^HARNESS_SESSION_ID=//p')"
  session_root="$(printf '%s\n' "$created" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
  printf '%s\n' "$i" > "$session_root/parallel-$i"
  HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$id"
  ids+=("$id")
done

pids=()
for id in "${ids[@]}"; do
  HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$id" &
  pids+=("$!")
done
for pid in "${pids[@]}"; do wait "$pid"; done
for i in $(seq 1 20); do git --git-dir="$REMOTE" show "main:parallel-$i" >/dev/null || { echo "FAIL: missing parallel-$i"; exit 1; }; done
for id in "${ids[@]}"; do grep -qx state=DELIVERED "$STATE/sessions/$id/journal" || { echo "FAIL: $id not delivered"; exit 1; }; done
if find "$SOURCE/.git" "$STATE" -name index.lock -print | grep -q .; then echo 'FAIL: concurrent sessions left a shared index.lock'; exit 1; fi

git -C "$SOURCE" fetch -q origin main:refs/remotes/origin/main
STATE_A="$TMP/host-a"; STATE_B="$TMP/host-b"
a="$(HARNESS_SESSION_STATE_HOME="$STATE_A" "$ISOLATION" create "$SOURCE")"; b="$(HARNESS_SESSION_STATE_HOME="$STATE_B" "$ISOLATION" create "$SOURCE")"
a_id="$(printf '%s\n' "$a" | sed -n 's/^HARNESS_SESSION_ID=//p')"; b_id="$(printf '%s\n' "$b" | sed -n 's/^HARNESS_SESSION_ID=//p')"
a_root="$(printf '%s\n' "$a" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; b_root="$(printf '%s\n' "$b" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
printf a > "$a_root/host-a"; printf b > "$b_root/host-b"
HARNESS_SESSION_STATE_HOME="$STATE_A" "$ISOLATION" submit "$a_id"; HARNESS_SESSION_STATE_HOME="$STATE_B" "$ISOLATION" submit "$b_id"
HARNESS_SESSION_STATE_HOME="$STATE_A" "$ISOLATION" integrate "$a_id" & pa=$!
HARNESS_SESSION_STATE_HOME="$STATE_B" "$ISOLATION" integrate "$b_id" & pb=$!
wait "$pa"; wait "$pb"
git --git-dir="$REMOTE" show main:host-a >/dev/null && git --git-dir="$REMOTE" show main:host-b >/dev/null || { echo 'FAIL: independent host brokers must converge through remote CAS'; exit 1; }

echo 'PASS: 20 local sessions and two independent host brokers converge without index collisions'
