#!/usr/bin/env bash
# Regression coverage for opt-in isolated harness sessions.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ISOLATION="$ROOT/bin/session-isolation.sh"
TMP="$(mktemp -d)"
trap '[[ ! -d "$TMP/csmnt" ]] || hdiutil detach -quiet -force "$TMP/csmnt" 2>/dev/null; chmod -R u+w "$TMP" 2>/dev/null; rm -rf "$TMP"' EXIT

SOURCE="$TMP/source"
STATE="$TMP/state"
mkdir -p "$SOURCE/config/.local" "$SOURCE/projects/product" "$SOURCE/core/bin"
git -C "$SOURCE" init -q -b main
git -C "$SOURCE" config user.email test@example.invalid
git -C "$SOURCE" config user.name test
printf '%s\n' 'HARNESS_NAME="test"' 'HARNESS_PREFIX="test"' > "$SOURCE/config/launcher.env"
printf '%s\n' 'tracked' > "$SOURCE/tracked.txt"
printf '%s\n' one two three > "$SOURCE/merge.txt"
printf '%s\n' rename-me > "$SOURCE/rename-old.txt"
printf '%s\n' 'secret' > "$SOURCE/config/.local/secret.env"
cat > "$SOURCE/core/bin/auto-deliver.sh" <<'EOF'
#!/usr/bin/env bash
[[ " $* " == *' --staged-only '* && " $* " == *' --dry-run '* && " $* " == *' --no-push '* ]] || exit 91
[[ "${HARNESS_SESSION_TEST_VERIFY_FAIL:-0}" != 1 ]] || exit 92
[[ -z "${HARNESS_TEST_VERIFIER:-}" ]] || "$HARNESS_TEST_VERIFIER"
EOF
chmod +x "$SOURCE/core/bin/auto-deliver.sh"
git -C "$SOURCE" add config/launcher.env tracked.txt merge.txt rename-old.txt core/bin/auto-deliver.sh
git -C "$SOURCE" commit -qm initial
printf '%s\n' 'canonical dirty' > "$SOURCE/tracked.txt"

create() {
  HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" create "$SOURCE"
}

first="$(create)"
second="$(create)"
first_root="$(printf '%s\n' "$first" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
second_root="$(printf '%s\n' "$second" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
first_id="$(printf '%s\n' "$first" | sed -n 's/^HARNESS_SESSION_ID=//p')"
second_id="$(printf '%s\n' "$second" | sed -n 's/^HARNESS_SESSION_ID=//p')"

[[ -n "$first_id" && "$first_root" != "$second_root" ]] || { echo 'FAIL: isolated sessions must have separate roots'; exit 1; }
[[ "$(git -C "$first_root" rev-parse --absolute-git-dir)" != "$(git -C "$SOURCE" rev-parse --absolute-git-dir)" ]] || { echo 'FAIL: session must not share canonical refs or index'; exit 1; }
[[ -z "$(git -C "$first_root" remote)" ]] || { echo 'FAIL: session workspace must not retain a canonical push transport'; exit 1; }
source_main_before="$(git -C "$SOURCE" rev-parse main)"
git -C "$first_root" update-ref refs/heads/main HEAD
[[ "$(git -C "$SOURCE" rev-parse main)" == "$source_main_before" ]] || { echo 'FAIL: session ref writes must not move canonical main'; exit 1; }
[[ "$(<"$first_root/tracked.txt")" == tracked ]] || { echo 'FAIL: canonical dirty files must not be imported'; exit 1; }
[[ ! -e "$first_root/config/.local/secret.env" ]] || { echo 'FAIL: config/.local must not be copied'; exit 1; }
SOURCE_REAL="$(cd "$SOURCE" && pwd -P)"
[[ -L "$first_root/projects" && "$(readlink "$first_root/projects")" == "$SOURCE_REAL/projects" ]] || { echo 'FAIL: projects access must be preserved safely'; exit 1; }
[[ -f "$STATE/sessions/$first_id/journal" ]] || { echo 'FAIL: create must write an OPEN journal'; exit 1; }
grep -qx 'state=OPEN' "$STATE/sessions/$first_id/journal" || { echo 'FAIL: journal must start OPEN'; exit 1; }

HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" transition "$second_id" SUBMITTED submit-one
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" transition "$second_id" SUBMITTED submit-one
grep -qx 'state=SUBMITTED' "$STATE/sessions/$second_id/journal" || { echo 'FAIL: idempotent submit must retain SUBMITTED'; exit 1; }
if HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resume "$SOURCE" "$second_id" 2>"$TMP/submitted-resume.err"; then
  echo 'FAIL: SUBMITTED sessions must not reopen'; exit 1
fi
grep -q 'cannot resume SUBMITTED session; run harness-session integrate or recover' "$TMP/submitted-resume.err" || { echo 'FAIL: SUBMITTED rejection must name the recovery action'; exit 1; }

printf '%s\n' dirty > "$first_root/new.txt"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" exit "$first_id"
grep -qx 'state=ABANDONED' "$STATE/sessions/$first_id/journal" || { echo 'FAIL: dirty exit must be ABANDONED'; exit 1; }
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" recover "$first_id" | grep -qx "HARNESS_SESSION_ROOT=$first_root" || { echo 'FAIL: abandoned session must be recoverable'; exit 1; }

stale="$(create)"
stale_id="$(printf '%s\n' "$stale" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf '%s\n' '2000-01-01T00:00:00Z' > "$STATE/sessions/$stale_id/heartbeat"
stale_list="$(HARNESS_SESSION_STATE_HOME="$STATE" HARNESS_SESSION_STALE_SECONDS=1 "$ISOLATION" list)"
printf '%s\n' "$stale_list" | grep -qx "$stale_id ABANDONED" || { printf '%s\n' "$stale_list"; echo 'FAIL: stale OPEN session must become ABANDONED in list'; exit 1; }
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" recover "$stale_id" | grep -q '^HARNESS_SESSION_ROOT=' || { echo 'FAIL: stale abandoned session must retain workspace'; exit 1; }

REMOTE="$TMP/remote.git"
git init --bare -q "$REMOTE"
git -C "$SOURCE" add tracked.txt && git -C "$SOURCE" commit -qm source-clean
git -C "$SOURCE" remote add origin "$REMOTE"
git -C "$SOURCE" push -q -u origin main
git --git-dir="$REMOTE" symbolic-ref HEAD refs/heads/main
clean_close="$(create)"; clean_close_id="$(printf '%s\n' "$clean_close" | sed -n 's/^HARNESS_SESSION_ID=//p')"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" close "$clean_close_id"
grep -qx state=CLOSED "$STATE/sessions/$clean_close_id/journal" || { echo 'FAIL: closing a clean session must terminate it without submission'; exit 1; }
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resume "$SOURCE" "$clean_close_id" >/dev/null
grep -qx state=OPEN "$STATE/sessions/$clean_close_id/journal" || { echo 'FAIL: retained CLOSED sessions must reopen with the same UUID'; exit 1; }
clean_exit="$(create)"; clean_exit_id="$(printf '%s\n' "$clean_exit" | sed -n 's/^HARNESS_SESSION_ID=//p')"
clean_exit_root="$(printf '%s\n' "$clean_exit" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" exit "$clean_exit_id"
grep -qx state=CLOSED "$STATE/sessions/$clean_exit_id/journal" || { echo 'FAIL: a clean normal exit must not remain OPEN until stale'; exit 1; }
grep -qx 1 "$STATE/sessions/$clean_exit_id/lease-v1" || { echo 'FAIL: new sessions must opt into the runtime lease contract'; exit 1; }

tampered_root="$(create)"; tampered_root_id="$(printf '%s\n' "$tampered_root" | sed -n 's/^HARNESS_SESSION_ID=//p')"; tampered_root_path="$(printf '%s\n' "$tampered_root" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" exit "$tampered_root_id"
printf '%s\n' "$SOURCE" > "$STATE/sessions/$tampered_root_id/session-root"
if HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resume "$SOURCE" "$tampered_root_id"; then echo 'FAIL: resume must reject a journal-controlled canonical root'; exit 1; fi
grep -qx state=CLOSED "$STATE/sessions/$tampered_root_id/journal" || { echo 'FAIL: rejected tampered root must not reopen the journal'; exit 1; }
printf '%s\n' "$tampered_root_path" > "$STATE/sessions/$tampered_root_id/session-root"
if HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resume "$SOURCE" '../not-a-uuid'; then echo 'FAIL: resume must reject path traversal IDs'; exit 1; fi

# Terminal roots survive the grace period, then GC retires only an exact,
# unlocked launcher-owned direct child. Journals remain as audit evidence.
HARNESS_SESSION_RETENTION_SECONDS=86400 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" gc
[[ -d "$clean_exit_root" ]] || { echo 'FAIL: GC must retain a fresh CLOSED root during grace'; exit 1; }
python3 - "$STATE/sessions/$clean_exit_id/journal" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
lines = p.read_text().splitlines()
p.write_text("\n".join("heartbeat=2000-01-01T00:00:00Z" if line.startswith("heartbeat=") else line for line in lines) + "\n")
PY
exec 8>"$STATE/sessions/$clean_exit_id/runtime.lock"
/usr/bin/lockf -s -t 0 8
HARNESS_SESSION_RETENTION_SECONDS=0 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" gc
[[ -d "$clean_exit_root" ]] || { echo 'FAIL: GC must retain a terminal root while its runtime lease is held'; exit 1; }
exec 8>&-
HARNESS_SESSION_RETENTION_SECONDS=0 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" gc
[[ ! -e "$clean_exit_root" && -f "$STATE/sessions/$clean_exit_id/journal" ]] || { echo 'FAIL: GC must retire an expired unlocked CLOSED root and retain its journal'; exit 1; }

legacy="$(create)"; legacy_id="$(printf '%s\n' "$legacy" | sed -n 's/^HARNESS_SESSION_ID=//p')"; legacy_root="$(printf '%s\n' "$legacy" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" exit "$legacy_id"
printf 'legacy\n' > "$STATE/sessions/$legacy_id/lease-v1"
python3 - "$STATE/sessions/$legacy_id/journal" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
p.write_text(p.read_text().replace(next(line for line in p.read_text().splitlines() if line.startswith("heartbeat=")), "heartbeat=2000-01-01T00:00:00Z"))
PY
HARNESS_SESSION_RETENTION_SECONDS=0 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" gc
[[ -d "$legacy_root" ]] || { echo 'FAIL: GC must retain records outside the exact lease-v1 contract'; exit 1; }
suffix_tomb="$STATE/worktrees/.retired-$legacy_id-1-userdata"
exact_tomb="$STATE/worktrees/.retired-$legacy_id-1"
mkdir "$suffix_tomb" "$exact_tomb"; printf keep > "$suffix_tomb/sentinel"
HARNESS_SESSION_RETENTION_SECONDS=0 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" gc
[[ -f "$suffix_tomb/sentinel" ]] || { echo 'FAIL: tomb cleanup must reject a numeric prefix with arbitrary suffix'; exit 1; }
[[ -d "$exact_tomb" ]] || { echo 'FAIL: a tombstone without archive proof must be retained'; exit 1; }

malformed="$(create)"; malformed_id="$(printf '%s\n' "$malformed" | sed -n 's/^HARNESS_SESSION_ID=//p')"; malformed_root="$(printf '%s\n' "$malformed" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" exit "$malformed_id"
python3 - "$STATE/sessions/$malformed_id/journal" <<'PY'
from pathlib import Path
import sys

p = Path(sys.argv[1])
lines = p.read_text().splitlines()
lines = ["heartbeat=2000-01-01T00:00:00Z" if line.startswith("heartbeat=") else line for line in lines]
p.write_text("\n".join(lines + ["garbage=1"]) + "\n")
PY
HARNESS_SESSION_RETENTION_SECONDS=0 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" gc
[[ -d "$malformed_root" ]] || { echo 'FAIL: GC must retain a journal with unknown fields'; exit 1; }
sed -i '' '/^garbage=/d;/^identity=/d' "$STATE/sessions/$malformed_id/journal"
HARNESS_SESSION_RETENTION_SECONDS=0 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" gc
[[ -d "$malformed_root" ]] || { echo 'FAIL: GC must retain a journal missing identity'; exit 1; }
printf 'identity=\nidentity=duplicate\n' >> "$STATE/sessions/$malformed_id/journal"
HARNESS_SESSION_RETENTION_SECONDS=0 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" gc
[[ -d "$malformed_root" ]] || { echo 'FAIL: GC must retain duplicate journal fields'; exit 1; }

future="$(create)"; future_id="$(printf '%s\n' "$future" | sed -n 's/^HARNESS_SESSION_ID=//p')"; future_root="$(printf '%s\n' "$future" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" exit "$future_id"
sed -i '' 's/^heartbeat=.*/heartbeat=2999-01-01T00:00:00Z/' "$STATE/sessions/$future_id/journal"
HARNESS_SESSION_RETENTION_SECONDS=0 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" gc
[[ -d "$future_root" ]] || { echo 'FAIL: GC must retain future-dated terminal journals'; exit 1; }
ADVANCE_CLONE="$TMP/create-advance"
git clone -q "$REMOTE" "$ADVANCE_CLONE"
git -C "$ADVANCE_CLONE" config user.email test@example.invalid
git -C "$ADVANCE_CLONE" config user.name test
printf '%s\n' remote-latest > "$ADVANCE_CLONE/remote-latest.txt"
git -C "$ADVANCE_CLONE" add remote-latest.txt
git -C "$ADVANCE_CLONE" commit -qm remote-latest
git -C "$ADVANCE_CLONE" push -q origin main
latest_session="$(create)"
latest_root="$(printf '%s\n' "$latest_session" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
[[ -f "$latest_root/remote-latest.txt" ]] || { echo 'FAIL: create must fetch and base a new session on latest origin/main'; exit 1; }
closed="$(create)"; closed_root="$(printf '%s\n' "$closed" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; closed_id="$(printf '%s\n' "$closed" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf close > "$closed_root/closed.txt"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" close "$closed_id"
git --git-dir="$REMOTE" show main:closed.txt >/dev/null || { echo 'FAIL: public close command must submit and deliver the current session'; exit 1; }
third="$(create)"
third_root="$(printf '%s\n' "$third" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
third_id="$(printf '%s\n' "$third" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf '%s\n' broker-change > "$third_root/broker.txt"
git -C "$third_root" add broker.txt
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$third_id"
[[ -s "$STATE/sessions/$third_id/submission.patch" && -s "$STATE/sessions/$third_id/manifest" ]] || { echo 'FAIL: submit must persist immutable patch and manifest'; exit 1; }
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$third_id"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$third_id"
git --git-dir="$REMOTE" show main:broker.txt | grep -qx broker-change || { echo 'FAIL: broker must deliver submitted paths'; exit 1; }
grep -qx 'state=DELIVERED' "$STATE/sessions/$third_id/journal" || { echo 'FAIL: successful readback must mark DELIVERED'; exit 1; }
if HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resume "$SOURCE" "$third_id" 2>"$TMP/delivered-resume.err"; then
  echo 'FAIL: DELIVERED sessions must be permanently terminal'; exit 1
fi
grep -q 'cannot resume DELIVERED session; start a fresh isolated session' "$TMP/delivered-resume.err" || { echo 'FAIL: DELIVERED rejection must require a fresh session'; exit 1; }
if grep -R -- '--force' "$STATE/sessions/$third_id"; then echo 'FAIL: broker must never record force push'; exit 1; fi
python3 - "$STATE/sessions/$third_id/journal" <<'PY'
from pathlib import Path
import sys

p = Path(sys.argv[1])
p.write_text(p.read_text().replace(next(line for line in p.read_text().splitlines() if line.startswith("heartbeat=")), "heartbeat=2000-01-01T00:00:00Z"))
PY
mv "$STATE/sessions/$third_id/delivered-manifest" "$TMP/delivered-manifest"
HARNESS_SESSION_RETENTION_SECONDS=0 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" gc
[[ -d "$third_root" ]] || { echo 'FAIL: GC must retain DELIVERED roots with incomplete delivery evidence'; exit 1; }
: > "$STATE/sessions/$third_id/delivered-manifest"
HARNESS_SESSION_RETENTION_SECONDS=0 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" gc
[[ -d "$third_root" ]] || { echo 'FAIL: GC must retain DELIVERED roots with an empty manifest'; exit 1; }
printf 'F\0' > "$STATE/sessions/$third_id/delivered-manifest"
HARNESS_SESSION_RETENTION_SECONDS=0 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" gc
[[ -d "$third_root" ]] || { echo 'FAIL: GC must retain DELIVERED roots with a truncated manifest'; exit 1; }
mv "$TMP/delivered-manifest" "$STATE/sessions/$third_id/delivered-manifest"
mv "$STATE/sessions/$third_id/delivered-sha" "$TMP/delivered-sha"
printf 'garbage\n' > "$STATE/sessions/$third_id/delivered-sha"
HARNESS_SESSION_RETENTION_SECONDS=0 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" gc
[[ -d "$third_root" ]] || { echo 'FAIL: GC must retain DELIVERED roots with a malformed delivered SHA'; exit 1; }
mv "$TMP/delivered-sha" "$STATE/sessions/$third_id/delivered-sha"

tampered="$(create)"; tampered_root="$(printf '%s\n' "$tampered" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; tampered_id="$(printf '%s\n' "$tampered" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf tampered > "$tampered_root/tampered.txt"; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$tampered_id"
chmod u+w "$STATE/sessions/$tampered_id/submission.patch"; printf '\n' >> "$STATE/sessions/$tampered_id/submission.patch"
if HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$tampered_id"; then echo 'FAIL: mutable submission bytes must fail identity verification'; exit 1; fi
grep -qx state=CONFLICT "$STATE/sessions/$tampered_id/journal" || { echo 'FAIL: tampered submission must become CONFLICT'; exit 1; }

same_a="$(create)"; same_b="$(create)"
same_a_root="$(printf '%s\n' "$same_a" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; same_b_root="$(printf '%s\n' "$same_b" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
same_a_id="$(printf '%s\n' "$same_a" | sed -n 's/^HARNESS_SESSION_ID=//p')"; same_b_id="$(printf '%s\n' "$same_b" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf '%s\n' first > "$same_a_root/same.txt"; printf '%s\n' second > "$same_b_root/same.txt"
git -C "$same_a_root" add same.txt; git -C "$same_b_root" add same.txt
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$same_a_id"; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$same_b_id"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$same_a_id"
if HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$same_b_id"; then echo 'FAIL: same-file submission must conflict explicitly'; exit 1; fi
grep -qx 'state=CONFLICT' "$STATE/sessions/$same_b_id/journal" || { echo 'FAIL: same-file conflict must be durable'; exit 1; }

line_a="$(create)"; line_b="$(create)"
line_a_root="$(printf '%s\n' "$line_a" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; line_b_root="$(printf '%s\n' "$line_b" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
line_a_id="$(printf '%s\n' "$line_a" | sed -n 's/^HARNESS_SESSION_ID=//p')"; line_b_id="$(printf '%s\n' "$line_b" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf '%s\n' ONE two three > "$line_a_root/merge.txt"; printf '%s\n' one two THREE > "$line_b_root/merge.txt"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$line_a_id"; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$line_b_id"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$line_a_id"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$line_b_id"
[[ "$(git --git-dir="$REMOTE" show main:merge.txt)" == $'ONE\ntwo\nTHREE' ]] || { echo 'FAIL: 3-way integration must preserve disjoint line edits'; exit 1; }

renamed="$(create)"; renamed_root="$(printf '%s\n' "$renamed" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; renamed_id="$(printf '%s\n' "$renamed" | sed -n 's/^HARNESS_SESSION_ID=//p')"
git -C "$renamed_root" mv rename-old.txt rename-new.txt
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$renamed_id"; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$renamed_id"
git --git-dir="$REMOTE" cat-file -e main:rename-old.txt 2>/dev/null && { echo 'FAIL: rename source must be deleted'; exit 1; }
[[ "$(git --git-dir="$REMOTE" show main:rename-new.txt)" == rename-me ]] || { echo 'FAIL: rename destination must retain its blob'; exit 1; }

advance="$(create)"; advance_root="$(printf '%s\n' "$advance" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; advance_id="$(printf '%s\n' "$advance" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf '%s\n' candidate > "$advance_root/candidate.txt"; git -C "$advance_root" add candidate.txt
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$advance_id"
COUNT="$TMP/verify-count"; VERIFY="$TMP/verify-advance.sh"
cat > "$VERIFY" <<'EOF'
#!/usr/bin/env bash
n=0; [[ -f "$COUNT" ]] && n="$(<"$COUNT")"; n=$((n + 1)); printf '%s\n' "$n" > "$COUNT"
if [[ "$n" == 1 ]]; then
  d="$(mktemp -d)"; git clone -q "$REMOTE" "$d"; git -C "$d" config user.email test@example.invalid; git -C "$d" config user.name test; printf '%s\n' advance > "$d/advance.txt"; git -C "$d" add advance.txt; git -C "$d" commit -qm advance; git -C "$d" push -q origin HEAD:main; rm -rf "$d"
fi
EOF
chmod +x "$VERIFY"
COUNT="$COUNT" REMOTE="$REMOTE" HARNESS_TEST_VERIFIER="$VERIFY" HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$advance_id"
[[ "$(<"$COUNT")" -ge 2 ]] || { echo 'FAIL: remote advance must force verifier re-run'; exit 1; }

readback="$(create)"; readback_root="$(printf '%s\n' "$readback" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; readback_id="$(printf '%s\n' "$readback" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf '%s\n' readback > "$readback_root/readback.txt"; git -C "$readback_root" add readback.txt; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$readback_id"
if HARNESS_SESSION_TEST_READBACK_FAIL=1 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$readback_id"; then echo 'FAIL: readback outage must defer delivery'; exit 1; fi
grep -qx 'state=INTEGRATING' "$STATE/sessions/$readback_id/journal" || { echo 'FAIL: post-push readback outage must remain indeterminate'; exit 1; }
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" recover "$readback_id" | grep -qx state=DELIVERED || { echo 'FAIL: recover must prove an indeterminate pushed commit from fresh remote history'; exit 1; }

disjoint_a="$(create)"; disjoint_b="$(create)"; da_root="$(printf '%s\n' "$disjoint_a" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; db_root="$(printf '%s\n' "$disjoint_b" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; da_id="$(printf '%s\n' "$disjoint_a" | sed -n 's/^HARNESS_SESSION_ID=//p')"; db_id="$(printf '%s\n' "$disjoint_b" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf a > "$da_root/disjoint-a"; printf b > "$db_root/disjoint-b"; git -C "$da_root" add disjoint-a; git -C "$db_root" add disjoint-b
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$da_id"; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$db_id"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$da_id" & pa=$!; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$db_id" & pb=$!; wait "$pa"; wait "$pb"
git --git-dir="$REMOTE" show main:disjoint-a >/dev/null && git --git-dir="$REMOTE" show main:disjoint-b >/dev/null || { echo 'FAIL: disjoint concurrent submissions must both deliver'; exit 1; }

git -C "$SOURCE" fetch -q origin main:refs/remotes/origin/main
paths="$(create)"; paths_root="$(printf '%s\n' "$paths" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; paths_id="$(printf '%s\n' "$paths" | sed -n 's/^HARNESS_SESSION_ID=//p')"
rm "$paths_root/tracked.txt"; printf '#!/bin/sh\n' > "$paths_root/space file"; chmod +x "$paths_root/space file"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$paths_id"; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$paths_id"
git --git-dir="$REMOTE" cat-file -e main:tracked.txt 2>/dev/null && { echo 'FAIL: deleted path must not survive delivery'; exit 1; }
mode="$(git --git-dir="$REMOTE" ls-tree main -- 'space file' | awk '{print $1}')"; [[ "$mode" == 100755 ]] || { echo 'FAIL: executable mode and space path must survive delivery'; exit 1; }

git -C "$SOURCE" fetch -q origin main:refs/remotes/origin/main
weird="$(create)"; weird_root="$(printf '%s\n' "$weird" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; weird_id="$(printf '%s\n' "$weird" | sed -n 's/^HARNESS_SESSION_ID=//p')"
weird_name=$'tab\tand\nnewline'; printf target > "$weird_root/$weird_name"; ln -s "$weird_name" "$weird_root/link"; git -C "$weird_root" add -A
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$weird_id"; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$weird_id"
[[ "$(git --git-dir="$REMOTE" show "main:$weird_name")" == target ]] || { echo 'FAIL: tab/newline path must survive'; exit 1; }
[[ "$(git --git-dir="$REMOTE" show main:link)" == "$weird_name" ]] || { echo 'FAIL: symlink blob must survive'; exit 1; }

git -C "$SOURCE" fetch -q origin main:refs/remotes/origin/main
verify_fail="$(create)"; verify_fail_root="$(printf '%s\n' "$verify_fail" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; verify_fail_id="$(printf '%s\n' "$verify_fail" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf fail > "$verify_fail_root/verifier-failure"; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$verify_fail_id"
if HARNESS_SESSION_TEST_VERIFY_FAIL=1 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$verify_fail_id"; then echo 'FAIL: verifier failure must fail integration'; exit 1; fi
grep -qx 'state=CONFLICT' "$STATE/sessions/$verify_fail_id/journal" || { echo 'FAIL: verifier failure must reach terminal CONFLICT'; exit 1; }
printf stale > "$STATE/sessions/$verify_fail_id/pending-sha"; printf stale > "$STATE/sessions/$verify_fail_id/push-ack"; printf stale > "$STATE/sessions/$verify_fail_id/.candidate-manifest"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" resume "$SOURCE" "$verify_fail_id" | grep -qx "HARNESS_SESSION_ROOT=$verify_fail_root" || { echo 'FAIL: --isolated-session resume must retain a conflicted workspace'; exit 1; }
[[ ! -e "$STATE/sessions/$verify_fail_id/pending-sha" && ! -e "$STATE/sessions/$verify_fail_id/push-ack" && ! -e "$STATE/sessions/$verify_fail_id/.candidate-manifest" ]] || { echo 'FAIL: reopening a conflict must discard stale broker receipts'; exit 1; }
grep -qx 'state=OPEN' "$STATE/sessions/$verify_fail_id/journal" || { echo 'FAIL: conflict recovery must reopen the same UUID'; exit 1; }
printf repaired > "$verify_fail_root/verifier-failure"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" close "$verify_fail_id"
[[ "$(git --git-dir="$REMOTE" show main:verifier-failure)" == repaired ]] || { echo 'FAIL: recovered conflict must support a fresh submission'; exit 1; }

verifier_tamper="$(create)"; verifier_tamper_root="$(printf '%s\n' "$verifier_tamper" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; verifier_tamper_id="$(printf '%s\n' "$verifier_tamper" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$verifier_tamper_root/core/bin/auto-deliver.sh"
printf blocked > "$verifier_tamper_root/verifier-tamper"; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$verifier_tamper_id"
if HARNESS_SESSION_TEST_VERIFY_FAIL=1 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$verifier_tamper_id"; then echo 'FAIL: candidate must not replace its repository-owned baseline verifier'; exit 1; fi

remote_arg="$(create)"; remote_arg_root="$(printf '%s\n' "$remote_arg" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; remote_arg_id="$(printf '%s\n' "$remote_arg" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf no > "$remote_arg_root/remote-override"; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$remote_arg_id"
if HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$remote_arg_id" "$TMP/other.git"; then echo 'FAIL: public integrate must reject caller-supplied remote or verifier'; exit 1; fi
grep -qx state=SUBMITTED "$STATE/sessions/$remote_arg_id/journal" || { echo 'FAIL: rejected remote override must not mutate journal'; exit 1; }

git -C "$SOURCE" fetch -q origin main:refs/remotes/origin/main
retry="$(create)"; retry_root="$(printf '%s\n' "$retry" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; retry_id="$(printf '%s\n' "$retry" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf retry > "$retry_root/retry-exhausted"; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$retry_id"
RETRY_COUNT="$TMP/retry-count"; RETRY_VERIFY="$TMP/retry-advance.sh"
cat > "$RETRY_VERIFY" <<'EOF'
#!/usr/bin/env bash
n=0; [[ -f "$RETRY_COUNT" ]] && n="$(<"$RETRY_COUNT")"; n=$((n + 1)); printf '%s\n' "$n" > "$RETRY_COUNT"
d="$(mktemp -d)"; git clone -q "$REMOTE" "$d"; git -C "$d" config user.email test@example.invalid; git -C "$d" config user.name test
touch "$d/remote-advance-$n"; git -C "$d" add .; git -C "$d" commit -qm "advance $n"; git -C "$d" push -q origin HEAD:main; rm -rf "$d"
EOF
chmod +x "$RETRY_VERIFY"
if RETRY_COUNT="$RETRY_COUNT" REMOTE="$REMOTE" HARNESS_TEST_VERIFIER="$RETRY_VERIFY" HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$retry_id"; then echo 'FAIL: repeated remote advance must exhaust bounded retries'; exit 1; fi
[[ "$(<"$RETRY_COUNT")" == 2 ]] || { echo 'FAIL: remote retry count must be bounded at two attempts'; exit 1; }
grep -qx 'state=CONFLICT' "$STATE/sessions/$retry_id/journal" || { echo 'FAIL: retry exhaustion must reach terminal CONFLICT'; exit 1; }

git -C "$SOURCE" fetch -q origin main:refs/remotes/origin/main
crash_a="$(create)"; crash_b="$(create)"
crash_a_root="$(printf '%s\n' "$crash_a" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; crash_b_root="$(printf '%s\n' "$crash_b" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
crash_a_id="$(printf '%s\n' "$crash_a" | sed -n 's/^HARNESS_SESSION_ID=//p')"; crash_b_id="$(printf '%s\n' "$crash_b" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf a > "$crash_a_root/crash-a"; printf b > "$crash_b_root/crash-b"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$crash_a_id"; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$crash_b_id"
if HARNESS_SESSION_TEST_CRASH_BEFORE_PUSH=1 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$crash_a_id" >/dev/null 2>&1; then echo 'FAIL: pre-push crash injection must terminate integration'; exit 1; fi
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$crash_b_id"
git --git-dir="$REMOTE" show main:crash-b >/dev/null || { echo 'FAIL: process crash must release integration lock'; exit 1; }
crash_recovery="$(HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" recover "$crash_a_id")"
printf '%s\n' "$crash_recovery" | grep -qx state=SUBMITTED || { printf '%s\n' "$crash_recovery"; grep '^state=' "$STATE/sessions/$crash_a_id/journal"; echo 'FAIL: pre-push crash must reconcile to retryable SUBMITTED'; exit 1; }
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$crash_a_id"

postpush="$(create)"; postpush_root="$(printf '%s\n' "$postpush" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; postpush_id="$(printf '%s\n' "$postpush" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf delivered > "$postpush_root/postpush-crash"; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$postpush_id"
if HARNESS_SESSION_TEST_CRASH_AFTER_PUSH=1 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$postpush_id" >/dev/null 2>&1; then echo 'FAIL: post-push crash injection must terminate integration'; exit 1; fi
grep -qx state=INTEGRATING "$STATE/sessions/$postpush_id/journal" || { echo 'FAIL: post-push crash fixture must retain INTEGRATING'; exit 1; }
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" recover "$postpush_id" | grep -qx state=DELIVERED || { echo 'FAIL: post-push crash must reconcile exact remote delivery'; exit 1; }
git --git-dir="$REMOTE" show main:postpush-crash | grep -qx delivered || { echo 'FAIL: reconciled post-push commit missing remotely'; exit 1; }

preack="$(create)"; preack_root="$(printf '%s\n' "$preack" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; preack_id="$(printf '%s\n' "$preack" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf delivered > "$preack_root/preack-crash"; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$preack_id"
if HARNESS_SESSION_TEST_CRASH_BEFORE_ACK=1 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$preack_id" >/dev/null 2>&1; then echo 'FAIL: pre-ack crash injection must terminate integration'; exit 1; fi
[[ ! -f "$STATE/sessions/$preack_id/push-ack" ]] || { echo 'FAIL: pre-ack crash fixture must stop before local acknowledgement'; exit 1; }
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" recover "$preack_id" | grep -qx state=DELIVERED || { echo 'FAIL: recovery must prove a pushed candidate from fresh remote history without a local acknowledgement'; exit 1; }
git --git-dir="$REMOTE" show main:preack-crash | grep -qx delivered || { echo 'FAIL: pre-ack recovery lost the remotely accepted commit'; exit 1; }

postpush_race="$(create)"; postpush_race_root="$(printf '%s\n' "$postpush_race" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; postpush_race_id="$(printf '%s\n' "$postpush_race" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf delivered > "$postpush_race_root/postpush-race"; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" submit "$postpush_race_id"
HARNESS_SESSION_TEST_POST_PUSH_DELAY=2 HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" integrate "$postpush_race_id" & race_pid=$!
for _ in {1..100}; do [[ -f "$STATE/sessions/$postpush_race_id/push-ack" ]] && break; sleep 0.05; done
[[ -f "$STATE/sessions/$postpush_race_id/push-ack" ]] || { echo 'FAIL: successful push must persist an acknowledgement before readback'; exit 1; }
race_advance="$TMP/postpush-race-advance"; git clone -q "$REMOTE" "$race_advance"; git -C "$race_advance" config user.email test@example.invalid; git -C "$race_advance" config user.name test
printf advanced > "$race_advance/after-race"; git -C "$race_advance" add after-race; git -C "$race_advance" commit -qm after-race; git -C "$race_advance" push -q origin HEAD:main
wait "$race_pid"
grep -qx state=DELIVERED "$STATE/sessions/$postpush_race_id/journal" || { echo 'FAIL: a later remote descendant must not invalidate an acknowledged delivery'; exit 1; }
git --git-dir="$REMOTE" merge-base --is-ancestor "$(<"$STATE/sessions/$postpush_race_id/delivered-sha")" main || { echo 'FAIL: acknowledged delivery must remain in remote history'; exit 1; }

echo 'PASS: isolated sessions retain clean canonical base and durable recovery state'

# Headless clone variant: machine-local files are copied, never linked back to
# the canonical root; the settings env block (secrets) is dropped; projects/
# is not linked. Interactive clones keep the symlinks.
mkdir -p "$SOURCE/.claude"
printf '%s\n' '{"env":{"LOCAL_SECRET":"leak"},"permissions":{"allow":["Read"]}}' > "$SOURCE/.claude/settings.local.json"
printf '%s\n' '{"mcpServers":{"docs":{"command":"echo"}}}' > "$SOURCE/.mcp.local.json"
interactive_root="$(create | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
[[ -L "$interactive_root/.claude/settings.local.json" && -L "$interactive_root/.mcp.local.json" && -L "$interactive_root/projects" ]] || { echo 'FAIL: interactive clones must keep linking machine-local files'; exit 1; }
headless_root="$(HARNESS_HEADLESS=1 HARNESS_PYTHON_BIN="$(command -v python3)" create | sed -n "s/^HARNESS_SESSION_ROOT=//p")"
[[ -n "$headless_root" && ! -e "$headless_root/projects" && ! -L "$headless_root/projects" ]] || { echo 'FAIL: headless clones must not link projects/'; exit 1; }
[[ -f "$headless_root/.mcp.local.json" && ! -L "$headless_root/.mcp.local.json" ]] || { echo 'FAIL: headless clones must copy .mcp.local.json'; exit 1; }
cmp -s "$SOURCE/.mcp.local.json" "$headless_root/.mcp.local.json" || { echo 'FAIL: headless MCP copy must match the source'; exit 1; }
[[ -f "$headless_root/.claude/settings.local.json" && ! -L "$headless_root/.claude/settings.local.json" ]] || { echo 'FAIL: headless clones must copy settings.local.json'; exit 1; }
! grep -q LOCAL_SECRET "$headless_root/.claude/settings.local.json" || { echo 'FAIL: headless settings copy must drop the env block'; exit 1; }
grep -q '"Read"' "$headless_root/.claude/settings.local.json" || { echo 'FAIL: headless settings copy must keep non-env settings'; exit 1; }

echo 'PASS: headless clones copy machine-local files without links or env secrets'

# Headless clone hardening: no cwd module hijack, no symlink-following writes.
PY_ABS="$(command -v python3)"
mkdir -p "$TMP/hijack"
printf '%s\n' "open('$TMP/hijack-ran', 'w').write('x')" 'from importlib import import_module' > "$TMP/hijack/json.py"
hijack_root="$(cd "$TMP/hijack" && HARNESS_HEADLESS=1 HARNESS_PYTHON_BIN="$PY_ABS" HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" create "$SOURCE" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
[[ -n "$hijack_root" && ! -e "$TMP/hijack-ran" ]] || { echo 'FAIL: headless settings copy must ignore a json.py in the cwd'; exit 1; }
grep -q '"Read"' "$hijack_root/.claude/settings.local.json" || { echo 'FAIL: hijack-safe copy must still write settings'; exit 1; }

headless_link_source() {
  local name="$1" src="$TMP/$1"
  mkdir -p "$src"
  git -C "$src" init -q -b main
  git -C "$src" config user.email test@example.invalid
  git -C "$src" config user.name test
  printf '%s\n' "$src"
}
expect_headless_refusal() {
  local src="$1" label="$2" before after
  before="$(find "$STATE/sessions" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')"
  if HARNESS_HEADLESS=1 HARNESS_PYTHON_BIN="$PY_ABS" HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" create "$src" >/dev/null 2>"$TMP/refusal.err"; then
    echo "FAIL: headless create must refuse $label"; exit 1
  fi
  grep -q 'headless clone refused' "$TMP/refusal.err" || { echo "FAIL: $label refusal must be explained"; exit 1; }
  after="$(find "$STATE/sessions" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')"
  [[ "$before" == "$after" ]] || { echo "FAIL: refused headless create must not leave a session record"; exit 1; }
}
# Tracked dangling symlink at .claude/settings.local.json.
dangling_src="$(headless_link_source dangling-src)"
mkdir -p "$dangling_src/.claude" "$TMP/outside-dangling"
ln -s "$TMP/outside-dangling/settings.local.json" "$dangling_src/.claude/settings.local.json"
git -C "$dangling_src" add -A && git -C "$dangling_src" commit -qm link
rm "$dangling_src/.claude/settings.local.json"; printf '%s\n' '{}' > "$dangling_src/.claude/settings.local.json"
expect_headless_refusal "$dangling_src" 'a dangling settings symlink'
[[ ! -e "$TMP/outside-dangling/settings.local.json" ]] || { echo 'FAIL: headless copy wrote through a dangling symlink'; exit 1; }
# Tracked symlinked .claude directory.
dir_src="$(headless_link_source dir-src)"
mkdir -p "$TMP/outside-dir"
ln -s "$TMP/outside-dir" "$dir_src/.claude"
git -C "$dir_src" add -A && git -C "$dir_src" commit -qm link
rm "$dir_src/.claude"; mkdir -p "$dir_src/.claude"; printf '%s\n' '{}' > "$dir_src/.claude/settings.local.json"
expect_headless_refusal "$dir_src" 'a symlinked .claude directory'
[[ ! -e "$TMP/outside-dir/settings.local.json" ]] || { echo 'FAIL: headless copy wrote through a symlinked parent'; exit 1; }

echo 'PASS: headless clones ignore cwd modules and refuse symlinked local-file paths'

# Headless clones own their object files (no hardlinks into the source) and a
# launcher-owned git dir in the record.
nolink_root="$(HARNESS_HEADLESS=1 HARNESS_PYTHON_BIN="$PY_ABS" create | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
linked="$(find "$nolink_root/.git/objects" -type f -links +1 | head -n 1)"
[[ -z "$linked" ]] || { echo "FAIL: headless clone objects must not be hardlinked: $linked"; exit 1; }
nolink_id="${nolink_root##*/}"
[[ -e "$STATE/sessions/$nolink_id/headless" && -d "$STATE/sessions/$nolink_id/trusted.git" ]] || { echo 'FAIL: headless create must write the marker and trusted git dir'; exit 1; }

# Interactive broker git never runs fsmonitor or hooks from the session config.
fsm_out="$(create)"; fsm_root="$(printf '%s\n' "$fsm_out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; fsm_id="$(printf '%s\n' "$fsm_out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf '#!/bin/sh\ntouch "%s"\n' "$TMP/fsmonitor-ran" > "$TMP/fsmonitor.sh"; chmod +x "$TMP/fsmonitor.sh"
git -C "$fsm_root" config core.fsmonitor "$TMP/fsmonitor.sh"
printf 'fsm\n' > "$fsm_root/fsm.txt"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" exit "$fsm_id"
[[ ! -e "$TMP/fsmonitor-ran" ]] || { echo 'FAIL: broker git must disable core.fsmonitor on the session root'; exit 1; }

echo 'PASS: headless clones own their objects and broker git ignores session config hooks'

# Headless broker git never reads the session .git.
headless_session() {
  local out; out="$(HARNESS_HEADLESS=1 HARNESS_PYTHON_BIN="$PY_ABS" create)"
  hs_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; hs_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
}
payload="$TMP/payload-ran"
printf '#!/bin/sh\ntouch "%s"\ncat\n' "$payload" > "$TMP/payload.sh"; chmod +x "$TMP/payload.sh"
deliver_headless() {
  HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" exit "$hs_id"
  HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" recover "$hs_id" >/dev/null
  HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" close "$hs_id"
  grep -qx state=DELIVERED "$STATE/sessions/$hs_id/journal" || { echo "FAIL: headless session $hs_id was not delivered"; exit 1; }
}
# Agent commit plus working-tree change, with payloads in .git/config,
# .git/config.worktree, hooks, .git/modules and .gitattributes.
headless_session
printf 'committed\n' > "$hs_root/hl-committed.txt"
git -C "$hs_root" add hl-committed.txt && git -C "$hs_root" -c user.name=t -c user.email=t@example.invalid commit -qm agent
printf 'worktree\n' > "$hs_root/hl-worktree.txt"
# Committed excluded paths are dropped by the pathspec, never delivered.
mkdir -p "$hs_root/config/.local" "$hs_root/projects"
printf 'secret\n' > "$hs_root/.claude/settings.local.json"; printf 'secret\n' > "$hs_root/config/.local/x"; printf 'secret\n' > "$hs_root/projects/x"
git -C "$hs_root" add -f .claude/settings.local.json config/.local/x projects/x
git -C "$hs_root" -c user.name=t -c user.email=t@example.invalid commit -qm planted
# Payloads last: nothing after this point may run git on the session itself.
printf '* filter=evil diff=evil\n' > "$hs_root/.gitattributes"
printf '[filter "evil"]\n\tclean = %s\n\tprocess = %s\n[diff "evil"]\n\ttextconv = %s\n[core]\n\tfsmonitor = %s\n\thooksPath = %s\n' \
  "$TMP/payload.sh" "$TMP/payload.sh" "$TMP/payload.sh" "$TMP/payload.sh" "$TMP" >> "$hs_root/.git/config"
printf '[core]\n\tfsmonitor = %s\n' "$TMP/payload.sh" > "$hs_root/.git/config.worktree"
printf '#!/bin/sh\ntouch "%s"\n' "$payload" > "$hs_root/.git/hooks/pre-commit"; chmod +x "$hs_root/.git/hooks/pre-commit"
mkdir -p "$hs_root/.git/modules/x"; printf '[core]\n\tfsmonitor = %s\n' "$TMP/payload.sh" > "$hs_root/.git/modules/x/config"
deliver_headless
[[ ! -e "$payload" ]] || { echo 'FAIL: a session git config, hook, module or attribute payload ran'; exit 1; }
for delivered_path in hl-committed.txt hl-worktree.txt; do
  git --git-dir="$REMOTE" show "main:$delivered_path" >/dev/null || { echo "FAIL: $delivered_path must be delivered"; exit 1; }
done
for excluded in .claude/settings.local.json config/.local/x projects/x; do
  ! git --git-dir="$REMOTE" cat-file -e "main:$excluded" 2>/dev/null || { echo "FAIL: excluded $excluded was delivered"; exit 1; }
done
# A symlinked session .git does not affect broker git.
headless_session
printf 'linked\n' > "$hs_root/hl-linked.txt"
mv "$hs_root/.git" "$TMP/moved-git-$hs_id"; mkdir -p "$TMP/evil-git"; ln -s "$TMP/evil-git" "$hs_root/.git"
deliver_headless
git --git-dir="$REMOTE" show main:hl-linked.txt >/dev/null || { echo 'FAIL: a symlinked .git must not stop headless delivery'; exit 1; }
[[ -z "$(ls -A "$TMP/evil-git")" ]] || { echo 'FAIL: broker git wrote through a symlinked session .git'; exit 1; }
# Headless marker without its trusted git dir fails closed.
headless_session
printf 'x\n' > "$hs_root/hl-missing.txt"
rm -rf "$STATE/sessions/$hs_id/trusted.git"
rc=0; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" exit "$hs_id" 2>/dev/null || rc=$?
[[ "$rc" == 6 ]] || { echo "FAIL: a headless record without trusted.git must refuse (rc=$rc)"; exit 1; }
rc=0; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" close "$hs_id" 2>/dev/null || rc=$?
[[ "$rc" == 6 ]] || { echo "FAIL: close must refuse without trusted.git (rc=$rc)"; exit 1; }

echo 'PASS: headless broker git uses the launcher-owned git dir, never the session .git'

# Interactive: committed changes under excluded paths refuse the submission; normal commits deliver.
for excluded in .claude/settings.local.json config/.local/x projects/x config/.LOCAL/y Projects/y $'project\xc5\xbf/y'; do
  out="$(create)"; ex_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; ex_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
  rm -rf "$ex_root/projects"
  mkdir -p "$(dirname "$ex_root/$excluded")"; printf 'secret\n' > "$ex_root/$excluded"
  git -C "$ex_root" add -f -- "$excluded"
  git -C "$ex_root" -c user.name=t -c user.email=t@example.invalid commit -qm planted
  before="$(git --git-dir="$REMOTE" rev-parse main)"
  rc=0; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" close "$ex_id" 2>"$TMP/excluded.err" || rc=$?
  [[ "$rc" == 7 ]] || { echo "FAIL: committing $excluded must refuse with exit 7 (got $rc)"; exit 1; }
  grep -q 'excluded path' "$TMP/excluded.err" || { echo 'FAIL: excluded-path refusal must be explained'; exit 1; }
  [[ "$(git --git-dir="$REMOTE" rev-parse main)" == "$before" ]] || { echo "FAIL: $excluded reached the remote"; exit 1; }
done
out="$(create)"; ok_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; ok_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf 'committed\n' > "$ok_root/committed-normal.txt"
git -C "$ok_root" add committed-normal.txt && git -C "$ok_root" -c user.name=t -c user.email=t@example.invalid commit -qm normal
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" close "$ok_id"
git --git-dir="$REMOTE" show main:committed-normal.txt >/dev/null || { echo 'FAIL: committed normal paths must be delivered'; exit 1; }

# Interactive close when the harness .gitignore ignores the machine-local links.
git clone -q "$REMOTE" "$TMP/ignore-clone"
printf '%s\n' '.claude/settings.local.json' 'mcp.local.json' '/projects' > "$TMP/ignore-clone/.gitignore"
git -C "$TMP/ignore-clone" add .gitignore
git -C "$TMP/ignore-clone" -c user.name=t -c user.email=t@example.invalid commit -qm ignore-local
git -C "$TMP/ignore-clone" push -q origin HEAD:main
printf '{}\n' > "$SOURCE/mcp.local.json"
out="$(create)"; ig_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; ig_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
[[ -L "$ig_root/mcp.local.json" && -L "$ig_root/.claude/settings.local.json" ]] || { echo 'FAIL: fixture must link the gitignored local files'; exit 1; }
printf 'ignored-layout\n' > "$ig_root/ignored-layout.txt"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" close "$ig_id"
git --git-dir="$REMOTE" show main:ignored-layout.txt >/dev/null || { echo 'FAIL: interactive close must deliver beside gitignored local links'; exit 1; }
! git --git-dir="$REMOTE" cat-file -e main:mcp.local.json 2>/dev/null || { echo 'FAIL: a gitignored local link was delivered'; exit 1; }
# Headless, same layout. (An interactive test above wrote through its
# settings link into the source copy; give it valid JSON again.)
printf '{}\n' > "$SOURCE/.claude/settings.local.json"
headless_session
printf 'headless-ignored\n' > "$hs_root/headless-ignored.txt"
deliver_headless
git --git-dir="$REMOTE" show main:headless-ignored.txt >/dev/null || { echo 'FAIL: headless close must deliver beside gitignored local copies'; exit 1; }
! git --git-dir="$REMOTE" cat-file -e main:mcp.local.json 2>/dev/null || { echo 'FAIL: a gitignored local copy was delivered'; exit 1; }

# A user git template without info/ (hooks-only init.templateDir) must not
# break headless create; the trusted git dir ignores user config entirely.
mkdir -p "$TMP/tplhome" "$TMP/tpl/hooks"
printf '#!/bin/sh\ntouch "%s"\n' "$TMP/template-hook-ran" > "$TMP/tpl/hooks/post-checkout"; chmod +x "$TMP/tpl/hooks/post-checkout"
printf '[init]\n\ttemplateDir = %s\n' "$TMP/tpl" > "$TMP/tplhome/.gitconfig"
tpl_out="$(HOME="$TMP/tplhome" HARNESS_HEADLESS=1 HARNESS_PYTHON_BIN="$PY_ABS" HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" create "$SOURCE")" || { echo 'FAIL: headless create must survive a hooks-only git template'; exit 1; }
tpl_id="$(printf '%s\n' "$tpl_out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
grep -qx '/projects' "$STATE/sessions/$tpl_id/trusted.git/info/exclude" || { echo 'FAIL: trusted.git must ignore the excluded paths'; exit 1; }
[[ ! -e "$STATE/sessions/$tpl_id/trusted.git/hooks/post-checkout" ]] || { echo 'FAIL: user template hooks must not reach trusted.git'; exit 1; }
# Interactive add never reads into non-ignored excluded paths.
out="$(create)"; il_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; il_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
mkdir -p "$il_root/config/.local"; printf 'x\n' > "$il_root/config/.local/locked"; chmod 000 "$il_root/config/.local/locked"
git init -q "$il_root/config/.local/nested"
printf 'beside-local\n' > "$il_root/beside-local.txt"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" close "$il_id" || { echo 'FAIL: unreadable content under an excluded path must not stop interactive close'; exit 1; }
git --git-dir="$REMOTE" show main:beside-local.txt >/dev/null || { echo 'FAIL: interactive close beside excluded content must deliver'; exit 1; }
chmod 600 "$il_root/config/.local/locked"

echo 'PASS: submissions touching excluded paths are refused; committed work is delivered'

# Fail closed: a git failure injected anywhere on the delivery path stops the
# close, keeps the work (never CLOSED as if empty) and delivers nothing.
# FAIL_GIT makes matching git calls exit 128; AFTER_GIT runs AFTER_CMD (with
# the git arguments) after a matching call succeeds.
REAL_GIT="$(command -v git)"
mkdir -p "$TMP/failgit"
cat > "$TMP/failgit/git" <<EOF
#!/bin/bash
if [[ -n "\${FAIL_GIT:-}" && " \$* " == *"\$FAIL_GIT"* ]]; then echo "injected git failure: \$FAIL_GIT" >&2; exit 128; fi
"$REAL_GIT" "\$@"; rc=\$?
if [[ -n "\${AFTER_GIT:-}" && " \$* " == *"\$AFTER_GIT"* ]]; then bash -c "\$AFTER_CMD" _ "\$@"; fi
exit \$rc
EOF
chmod +x "$TMP/failgit/git"
FAILPATH="$TMP/failgit:$PATH"
# <mode> <commit|worktree> <FAIL_GIT pattern> <path>...
fail_closed_row() {
  local mode="$1" how="$2" pattern="$3" out id root before rc=0 path state; shift 3
  if [[ "$mode" == headless ]]; then out="$(HARNESS_HEADLESS=1 HARNESS_PYTHON_BIN="$PY_ABS" create)"; else out="$(create)"; fi
  root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
  rm -rf "$root/projects"
  for path; do mkdir -p "$(dirname "$root/$path")"; printf 'fail-closed\n' > "$root/$path"; done
  if [[ "$how" == commit ]]; then
    git -C "$root" add -f -- "$@"; git -C "$root" -c user.name=t -c user.email=t@example.invalid commit -qm fail-closed
  fi
  before="$(git --git-dir="$REMOTE" rev-parse main)"
  if [[ "$mode" == headless ]]; then
    PATH="$FAILPATH" FAIL_GIT="$pattern" HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" exit "$id" 2>>"$TMP/fail-closed.err" || :
    PATH="$FAILPATH" FAIL_GIT="$pattern" HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" recover "$id" >/dev/null 2>>"$TMP/fail-closed.err" || :
  fi
  PATH="$FAILPATH" FAIL_GIT="$pattern" HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" close "$id" 2>>"$TMP/fail-closed.err" || rc=$?
  state="$(sed -n 's/^state=//p' "$STATE/sessions/$id/journal")"
  [[ "$rc" != 0 ]] || { echo "FAIL: $mode close with failing git '$pattern' must fail (state $state)"; exit 1; }
  [[ "$state" != CLOSED && "$state" != DELIVERED ]] || { echo "FAIL: $mode close with failing git '$pattern' ended $state"; exit 1; }
  [[ "$(git --git-dir="$REMOTE" rev-parse main)" == "$before" ]] || { echo "FAIL: $mode close with failing git '$pattern' delivered"; exit 1; }
}
alias_path=$'project\xc5\xbf/x'
fail_closed_row headless worktree '--name-only --no-renames -z' fc1.txt "$alias_path"
fail_closed_row headless worktree '--cached --quiet' fc2.txt
fail_closed_row headless worktree '--name-status' fc3.txt
fail_closed_row headless worktree 'ls-tree' fc4.txt
fail_closed_row headless worktree 'icase)' fc5.txt
fail_closed_row interactive commit '--name-only' fc6.txt projects/x
fail_closed_row interactive worktree 'status --porcelain' fc7.txt
fail_closed_row interactive worktree 'reset -q' fc8.txt
fail_closed_row interactive worktree 'icase)' fc9.txt
fail_closed_row interactive commit 'ls-tree' fc10.txt

# Clone-time fences that cannot be applied refuse the session.
journals() { find "$STATE/sessions" -name journal | wc -l | tr -d ' '; }
# <AFTER_GIT pattern> <AFTER_CMD> <headless|interactive> [source]
create_refused_row() {
  local count rc=0
  count="$(journals)"
  if [[ "$3" == headless ]]; then
    PATH="$FAILPATH" AFTER_GIT="$1" AFTER_CMD="$2" HARNESS_HEADLESS=1 HARNESS_PYTHON_BIN="$PY_ABS" HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" create "${4:-$SOURCE}" >/dev/null 2>>"$TMP/fail-closed.err" || rc=$?
  else
    PATH="$FAILPATH" AFTER_GIT="$1" AFTER_CMD="$2" HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" create "${4:-$SOURCE}" >/dev/null 2>>"$TMP/fail-closed.err" || rc=$?
  fi
  [[ "$rc" != 0 && "$(journals)" == "$count" ]] || { echo "FAIL: create must refuse when '$2' follows git '$1' (rc=$rc)"; exit 1; }
}
# Read-only info/exclude: the interactive clone exclude cannot be written.
create_refused_row ' clone ' 'chmod a-w "${@: -1}/.git/info/exclude" "${@: -1}/.git/info" 2>/dev/null' interactive
# Read-only trusted.git: its info/exclude cannot be written.
create_refused_row ' init -q --bare ' 'chmod a-w "${@: -1}"' headless
# A tracked projects symlink that cannot be removed refuses the headless clone.
LINKSRC="$TMP/linksrc"
git init -q -b main "$LINKSRC"; ln -s "$TMP" "$LINKSRC/projects"
git -C "$LINKSRC" add projects; git -C "$LINKSRC" -c user.name=t -c user.email=t@example.invalid commit -qm link
create_refused_row ' checkout -q --detach ' 'chflags -h uchg "$2/projects"' headless "$LINKSRC"
find "$STATE" -exec chflags -h nouchg {} + 2>/dev/null; chmod -R u+w "$STATE"

echo 'PASS: injected git and filesystem failures on the delivery path fail closed'

# A path that looks like pathspec magic is delivered and read back as itself.
out="$(create)"; mg_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; mg_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf 'magic\n' > "$mg_root/:magic"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" close "$mg_id" 2>"$TMP/magic.err" || { cat "$TMP/magic.err"; echo 'FAIL: a path named like pathspec magic must be delivered'; exit 1; }
grep -qx state=DELIVERED "$STATE/sessions/$mg_id/journal" || { echo 'FAIL: magic-named path session must end DELIVERED'; exit 1; }
git --git-dir="$REMOTE" cat-file -e 'main::magic' || { echo 'FAIL: :magic must reach the remote'; exit 1; }

# The state home may sit on another volume than the source checkout the work
# lands in. A name only the source volume folds onto an excluded entry is
# still excluded. Needs a case-sensitive APFS image; skipped where hdiutil
# cannot attach one.
CSIMG="$TMP/cs.sparseimage"
if hdiutil create -quiet -size 64m -fs 'Case-sensitive APFS' -type SPARSE -volname hlcs "$TMP/cs" >/dev/null 2>&1 \
    && hdiutil attach -quiet -nobrowse -mountpoint "$TMP/csmnt" "$CSIMG" >/dev/null 2>&1; then
  CS_STATE="$TMP/csmnt/state"
  [[ ! -e "$CS_STATE" ]] && mkdir -p "$CS_STATE/x" && [[ ! -e "$CS_STATE/X" ]] || { echo 'FAIL: the image must be case-sensitive'; exit 1; }
  for mode in headless interactive; do
    if [[ "$mode" == headless ]]; then
      out="$(HARNESS_HEADLESS=1 HARNESS_PYTHON_BIN="$PY_ABS" HARNESS_SESSION_STATE_HOME="$CS_STATE" "$ISOLATION" create "$SOURCE")"
    else
      out="$(HARNESS_SESSION_STATE_HOME="$CS_STATE" "$ISOLATION" create "$SOURCE")"
    fi
    cs_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; cs_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
    rm -rf "$cs_root/projects"; mkdir -p "$cs_root/$alias_path"; printf 'cs\n' > "$cs_root/$alias_path/x"
    printf 'cs normal\n' > "$cs_root/cs-$mode.txt"
    before="$(git --git-dir="$REMOTE" rev-parse main)"
    if [[ "$mode" == headless ]]; then
      HARNESS_SESSION_STATE_HOME="$CS_STATE" "$ISOLATION" exit "$cs_id"
      HARNESS_SESSION_STATE_HOME="$CS_STATE" "$ISOLATION" recover "$cs_id" >/dev/null
      HARNESS_SESSION_STATE_HOME="$CS_STATE" "$ISOLATION" close "$cs_id" || { echo 'FAIL: headless close on a case-sensitive state volume'; exit 1; }
      git --git-dir="$REMOTE" show "main:cs-$mode.txt" >/dev/null || { echo 'FAIL: normal path beside a source-volume alias must be delivered'; exit 1; }
    else
      # Untracked: unstaged back to HEAD, so the submission is the normal file.
      HARNESS_SESSION_STATE_HOME="$CS_STATE" "$ISOLATION" close "$cs_id" || { echo 'FAIL: interactive close on a case-sensitive state volume'; exit 1; }
    fi
    ! git --git-dir="$REMOTE" cat-file -e "main:$alias_path/x" 2>/dev/null || { echo "FAIL: $mode delivered a source-volume alias of projects/"; exit 1; }
  done
  hdiutil detach -quiet "$TMP/csmnt" || hdiutil detach -quiet -force "$TMP/csmnt"
  echo 'PASS: names the source volume folds onto an excluded entry are excluded across volumes'
else
  echo 'SKIP: no case-sensitive APFS image (hdiutil unavailable)'
fi

# Headless verifiers run in a sandbox with a minimal environment; the
# interactive verifier is unchanged.
git clone -q "$REMOTE" "$TMP/vfix"
cat >> "$TMP/vfix/core/bin/auto-deliver.sh" <<'EOF'
if [[ -e verifier-env-probe ]]; then
  env | sed 's/^/VERIFIER-ENV /'
  if ( : > "$HOME/verifier-wrote-home" ) 2>/dev/null; then echo VERIFIER-WROTE-HOME; fi
  # Test suites use multiprocessing (POSIX semaphores named /mp-*).
  python3 -c 'import multiprocessing as m; e = m.Event(); e.set(); print("VERIFIER-MP-OK" if e.is_set() else "")' 2>&1 \
    | sed 's/^/VERIFIER-MP /'
fi
EOF
git -C "$TMP/vfix" -c user.name=t -c user.email=t@example.invalid commit -qam verifier-env-probe
git -C "$TMP/vfix" push -q origin HEAD:main
VHOME="$TMP/vhome"; mkdir -p "$VHOME"
for mode in headless interactive; do
  if [[ "$mode" == headless ]]; then out="$(HARNESS_HEADLESS=1 HARNESS_PYTHON_BIN="$PY_ABS" create)"; else out="$(create)"; fi
  v_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; v_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
  printf '%s\n' "$mode" > "$v_root/verifier-env-probe"
  if [[ "$mode" == headless ]]; then
    HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" exit "$v_id"
    HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" recover "$v_id" >/dev/null
  fi
  rm -f "$VHOME/verifier-wrote-home"
  HOME="$VHOME" SSH_AUTH_SOCK=/tmp/agent.sock GH_TOKEN=leak-gh GITHUB_TOKEN=leak-github NPM_TOKEN=leak-npm LANG=en_US.UTF-8 \
    GIT_ASKPASS=/tmp/askpass HOMEBREW_GITHUB_API_TOKEN=leak-brew ANTHROPIC_API_KEY=leak-anthropic OPENAI_API_KEY=leak-openai \
    CLAUDE_CODE_OAUTH_TOKEN=leak-claude \
    HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" close "$v_id" > "$TMP/venv-$mode.out" || { echo "FAIL: $mode verifier-env close"; exit 1; }
  grep -qx state=DELIVERED "$STATE/sessions/$v_id/journal" || { echo "FAIL: $mode verifier-env session must be delivered"; exit 1; }
  if [[ "$mode" == headless ]]; then
    keys="$(sed -n 's/^VERIFIER-ENV \([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$TMP/venv-$mode.out" | sort -u | tr '\n' ' ')"
    for key in $keys; do
      case "$key" in
        HOME|PATH|LANG|LC_*|TMPDIR|GIT_CONFIG_GLOBAL|TEST_HARNESS_DIR|HARNESS_POST_COMMIT_PUSH|HARNESS_POST_COMMIT_CODEX_SYNC|HARNESS_RAG_ENABLED|HARNESS_SESSION_BACKLINK|PWD|OLDPWD|SHLVL|_) ;;
        *) echo "FAIL: the headless verifier environment carries $key"; exit 1 ;;
      esac
    done
    grep -qx 'VERIFIER-ENV LANG=en_US.UTF-8' "$TMP/venv-$mode.out" || { echo 'FAIL: the headless verifier keeps LANG'; exit 1; }
    v_tmp="$(sed -n 's/^VERIFIER-ENV TMPDIR=//p' "$TMP/venv-$mode.out")"
    [[ "$v_tmp" == */verifier-tmp.* && ! -e "$v_tmp" ]] || { echo "FAIL: the headless verifier needs its own removed temp dir (got '$v_tmp')"; exit 1; }
    ! grep -q VERIFIER-WROTE-HOME "$TMP/venv-$mode.out" && [[ ! -e "$VHOME/verifier-wrote-home" ]] || { echo 'FAIL: the headless verifier wrote to HOME'; exit 1; }
    grep -qx 'VERIFIER-MP VERIFIER-MP-OK' "$TMP/venv-$mode.out" \
      || { echo 'FAIL: the headless verifier must allow multiprocessing semaphores'; grep '^VERIFIER-MP' "$TMP/venv-$mode.out"; exit 1; }
  else
    grep -qx 'VERIFIER-ENV GH_TOKEN=leak-gh' "$TMP/venv-$mode.out" && grep -q VERIFIER-WROTE-HOME "$TMP/venv-$mode.out" \
      || { echo 'FAIL: the interactive verifier must run as before'; exit 1; }
  fi
done
echo 'PASS: headless verifiers run sandboxed with a minimal environment; interactive verifiers are unchanged'

# A moved source checkout has its own exit code and keeps the session.
out="$(HARNESS_HEADLESS=1 HARNESS_PYTHON_BIN="$PY_ABS" create)"
ms_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; ms_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf 'moved\n' > "$ms_root/moved.txt"
mv "$SOURCE" "$SOURCE.moved"
rc=0; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" exit "$ms_id" 2>/dev/null || rc=$?
exit_rc="$rc"; exit_state="$(sed -n 's/^state=//p' "$STATE/sessions/$ms_id/journal")"
HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" recover "$ms_id" >/dev/null
rc=0; HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" close "$ms_id" 2>"$TMP/moved.err" || rc=$?
mv "$SOURCE.moved" "$SOURCE"
[[ "$exit_rc" == 8 && "$exit_state" == ABANDONED ]] || { echo "FAIL: exit with a moved source must keep the work and exit 8 (got $exit_rc, $exit_state)"; exit 1; }
[[ "$rc" == 8 ]] && grep -q 'source checkout' "$TMP/moved.err" || { echo "FAIL: close with a moved source must exit 8 and say so (got $rc)"; exit 1; }
grep -qx state=OPEN "$STATE/sessions/$ms_id/journal" || { echo 'FAIL: a session whose source moved must stay open'; exit 1; }
echo 'PASS: a moved source checkout exits 8 and keeps the session'

# discard: an ABANDONED or CONFLICT session is retired to DISCARDED, its work
# kept first as a binary patch against its base.
iso() { HARNESS_SESSION_STATE_HOME="$STATE" "$ISOLATION" "$@"; }
jfield() { sed -n "s/^$2=//p" "$STATE/sessions/$1/journal"; }
git -C "$SOURCE" fetch -q origin main:refs/remotes/origin/main
# expect_discard_refused <id> <state> <message part> [lines]: exit 2, a
# one-line refusal (state refusals; I/O failures may add the tool's own error
# line before it), nothing changed.
expect_discard_refused() {
  local rc=0
  iso discard "$1" 2>"$TMP/discard.err" || rc=$?
  [[ "$rc" == 2 && "$(wc -l < "$TMP/discard.err" | tr -d ' ')" == "${4:-1}" ]] && grep -q -- "$3" "$TMP/discard.err" \
    || { cat "$TMP/discard.err"; echo "FAIL: discard of $2 must refuse with exit 2 and one line naming '$3' (rc=$rc)"; exit 1; }
  [[ "$(jfield "$1" state)" == "$2" ]] || { echo "FAIL: a refused discard changed $2"; exit 1; }
}
for mode in headless interactive; do
  if [[ "$mode" == headless ]]; then headless_session; d_id="$hs_id"; d_root="$hs_root"
  else out="$(create)"; d_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; d_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"; fi
  # Modified, deleted, committed, untracked text and binary files, a mode
  # change and a symlink.
  printf 'discarded edit\n' >> "$d_root/merge.txt"; rm "$d_root/rename-new.txt"
  printf 'committed\n' > "$d_root/discard-committed.txt"
  git -C "$d_root" add discard-committed.txt && git -C "$d_root" -c user.name=t -c user.email=t@example.invalid commit -qm agent
  printf 'untracked\n' > "$d_root/discard-untracked.txt"; printf '\000\001\002binary' > "$d_root/discard.bin"
  chmod +x "$d_root/merge.txt"; ln -s merge.txt "$d_root/discard-link"
  iso exit "$d_id" || :
  [[ "$(jfield "$d_id" state)" == ABANDONED ]] || { echo "FAIL: $mode discard fixture must be ABANDONED"; exit 1; }
  d_index="$STATE/sessions/$d_id/trusted.git/index"; [[ "$mode" == headless ]] || d_index="$(git -C "$d_root" rev-parse --absolute-git-dir)/index"
  d_index_before="$(shasum "$d_index")"
  iso discard "$d_id" || { echo "FAIL: $mode discard of ABANDONED must succeed"; exit 1; }
  [[ "$(shasum "$d_index")" == "$d_index_before" ]] || { echo "FAIL: $mode discard must not write the session's broker index"; exit 1; }
  d_dir="$STATE/sessions/$d_id"
  [[ "$(jfield "$d_id" state)" == DISCARDED && -z "$(jfield "$d_id" identity)" ]] || { echo "FAIL: $mode discard must end DISCARDED"; exit 1; }
  [[ -f "$d_dir/discarded.patch" && ! -L "$d_dir/discarded.patch" && -s "$d_dir/discarded.patch" ]] || { echo "FAIL: $mode discard must keep the patch"; exit 1; }
  grep -Eqx '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z' "$d_dir/discarded-at" || { echo "FAIL: $mode discard must record discarded-at"; exit 1; }
  fresh="$TMP/discard-fresh-$mode"; git clone -q "$REMOTE" "$fresh"; git -C "$fresh" checkout -q --detach "$(<"$d_dir/base-sha")"
  git -C "$fresh" apply --check "$d_dir/discarded.patch" && git -C "$fresh" apply "$d_dir/discarded.patch" || { echo "FAIL: $mode patch must apply at base"; exit 1; }
  for f in merge.txt discard-committed.txt discard-untracked.txt discard.bin; do
    cmp -s "$fresh/$f" "$d_root/$f" || { echo "FAIL: $mode patch must reproduce $f"; exit 1; }
  done
  [[ ! -e "$fresh/rename-new.txt" ]] || { echo "FAIL: $mode patch must reproduce the deletion"; exit 1; }
  [[ -x "$fresh/merge.txt" ]] || { echo "FAIL: $mode patch must reproduce the mode change"; exit 1; }
  [[ -L "$fresh/discard-link" && "$(readlink "$fresh/discard-link")" == merge.txt ]] || { echo "FAIL: $mode patch must reproduce the symlink"; exit 1; }
  grep -qx "$d_id DISCARDED" <<< "$(iso list)" || { echo "FAIL: list must show $mode DISCARDED"; exit 1; }
  rc=0; iso resume "$SOURCE" "$d_id" 2>"$TMP/discard.err" || rc=$?
  [[ "$rc" == 2 ]] && grep -q 'cannot resume DISCARDED session; start a fresh isolated session' "$TMP/discard.err" || { echo "FAIL: resume of DISCARDED must refuse (rc=$rc)"; exit 1; }
  rc=0; iso recover "$d_id" 2>"$TMP/discard.err" || rc=$?
  [[ "$rc" == 2 ]] && grep -q 'start a fresh isolated session' "$TMP/discard.err" || { echo "FAIL: recover of DISCARDED must refuse (rc=$rc)"; exit 1; }
  expect_discard_refused "$d_id" DISCARDED DISCARDED
  HARNESS_SESSION_RETENTION_SECONDS=0 iso gc
  [[ ! -e "$d_root" && -f "$d_dir/journal" && -f "$d_dir/discarded.patch" ]] || { echo "FAIL: gc must retire a $mode DISCARDED work tree and keep the record"; exit 1; }
done
# CONFLICT keeps the identity it carried.
out="$(create)"; c_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; c_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf 'conflicted\n' > "$c_root/discard-conflict.txt"; iso submit "$c_id"
chmod u+w "$STATE/sessions/$c_id/submission.patch"; printf '\n' >> "$STATE/sessions/$c_id/submission.patch"
iso integrate "$c_id" 2>/dev/null && { echo 'FAIL: tampered submission fixture must conflict'; exit 1; }
c_identity="$(jfield "$c_id" identity)"
[[ "$(jfield "$c_id" state)" == CONFLICT && "$c_identity" =~ ^[0-9a-f]{64}$ ]] || { echo 'FAIL: CONFLICT fixture must carry an identity'; exit 1; }
iso discard "$c_id" || { echo 'FAIL: discard of CONFLICT must succeed'; exit 1; }
[[ "$(jfield "$c_id" state)" == DISCARDED && "$(jfield "$c_id" identity)" == "$c_identity" ]] || { echo 'FAIL: discard of CONFLICT must keep its identity'; exit 1; }
grep -q discard-conflict.txt "$STATE/sessions/$c_id/discarded.patch" || { echo 'FAIL: CONFLICT discard must keep the work'; exit 1; }
# Refusals: OPEN, SUBMITTED, INTEGRATING, CLOSED, DELIVERED (DISCARDED above).
out="$(create)"; r_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; r_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf 'refuse\n' > "$r_root/discard-refuse.txt"
expect_discard_refused "$r_id" OPEN 'exit or close'
iso submit "$r_id"
expect_discard_refused "$r_id" SUBMITTED recover
iso transition "$r_id" INTEGRATING "$(jfield "$r_id" identity)"
expect_discard_refused "$r_id" INTEGRATING recover
out="$(create)"; cl_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"; iso exit "$cl_id"
expect_discard_refused "$cl_id" CLOSED CLOSED
expect_discard_refused "$third_id" DELIVERED DELIVERED
[[ ! -e "$STATE/sessions/$r_id/discarded.patch" && ! -e "$STATE/sessions/$cl_id/discarded.patch" ]] || { echo 'FAIL: a refused discard must not write evidence'; exit 1; }
# Interactive evidence starts from the session's own index: a force-added
# ignored file is kept and a `git rm --cached` file now ignored stays deleted,
# exactly as submit would deliver them.
out="$(create)"; ix_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; ix_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf 'ign/\nmerge.txt\n' >> "$ix_root/.gitignore"; mkdir -p "$ix_root/ign"; printf 'forced\n' > "$ix_root/ign/x"
git -C "$ix_root" add .gitignore && git -C "$ix_root" add -f ign/x && git -C "$ix_root" rm -q --cached merge.txt
git -C "$ix_root" -c user.name=t -c user.email=t@example.invalid commit -qm index-only
ix_index_before="$(shasum "$(git -C "$ix_root" rev-parse --absolute-git-dir)/index")"
iso exit "$ix_id"; iso discard "$ix_id" || { echo 'FAIL: index-seeded discard must succeed'; exit 1; }
[[ "$(shasum "$(git -C "$ix_root" rev-parse --absolute-git-dir)/index")" == "$ix_index_before" ]] || { echo 'FAIL: discard must not write the session index'; exit 1; }
fresh="$TMP/discard-fresh-index"; git clone -q "$REMOTE" "$fresh"; git -C "$fresh" checkout -q --detach "$(<"$STATE/sessions/$ix_id/base-sha")"
git -C "$fresh" apply "$STATE/sessions/$ix_id/discarded.patch" || { echo 'FAIL: index-seeded patch must apply at base'; exit 1; }
[[ "$(<"$fresh/ign/x")" == forced ]] || { echo 'FAIL: a force-added ignored file must be kept in the patch'; exit 1; }
[[ ! -e "$fresh/merge.txt" ]] || { echo 'FAIL: a git rm --cached file must stay deleted in the patch'; exit 1; }
# DISCARDED is reachable only through discard; a reopen clears stale evidence.
out="$(create)"; m_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; m_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf 'm\n' > "$m_root/m.txt"; iso exit "$m_id"
: > "$STATE/sessions/$m_id/discarded.patch"; date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE/sessions/$m_id/discarded-at"
rc=0; iso transition "$m_id" DISCARDED 2>"$TMP/discard.err" || rc=$?
[[ "$rc" == 2 ]] && grep -q 'use harness-session discard' "$TMP/discard.err" && [[ "$(jfield "$m_id" state)" == ABANDONED ]] \
  || { echo "FAIL: transition to DISCARDED must refuse (rc=$rc)"; exit 1; }
iso recover "$m_id" >/dev/null
[[ ! -e "$STATE/sessions/$m_id/discarded.patch" && ! -e "$STATE/sessions/$m_id/discarded-at" ]] || { echo 'FAIL: reopening must remove stale discard evidence'; exit 1; }
# A held runtime lease and a patch that cannot be written refuse and change nothing.
out="$(create)"; a_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; a_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
printf 'keep me\n' > "$a_root/discard-keep.txt"; iso exit "$a_id"
exec 8>"$STATE/sessions/$a_id/runtime.lock"; /usr/bin/lockf -s -t 0 8
expect_discard_refused "$a_id" ABANDONED 'in use'
exec 8>&-
[[ ! -e "$STATE/sessions/$a_id/discarded.patch" ]] || { echo 'FAIL: a leased discard must not write evidence'; exit 1; }
chmod a-w "$STATE/sessions/$a_id"
expect_discard_refused "$a_id" ABANDONED patch 2
chmod u+w "$STATE/sessions/$a_id"
rc=0; PATH="$FAILPATH" FAIL_GIT='--binary' iso discard "$a_id" 2>/dev/null || rc=$?
[[ "$rc" == 2 && "$(jfield "$a_id" state)" == ABANDONED && ! -e "$STATE/sessions/$a_id/discarded.patch" && -f "$a_root/discard-keep.txt" ]] \
  || { echo "FAIL: a failed patch write must refuse and change nothing (rc=$rc)"; exit 1; }
# An interrupted discard (TERM while git stages) removes its temporaries.
rc=0; PATH="$FAILPATH" AFTER_GIT=' add -A ' AFTER_CMD='kill -TERM "$(ps -o ppid= -p "$PPID")"' iso discard "$a_id" 2>/dev/null || rc=$?
leftover="$(find "$STATE" -maxdepth 1 -name 'discard.*'; find "$STATE/sessions/$a_id" -name '.discarded.patch.*')"
[[ "$rc" != 0 && -z "$leftover" && "$(jfield "$a_id" state)" == ABANDONED ]] || { echo "FAIL: an interrupted discard must clean up and change nothing (rc=$rc): $leftover"; exit 1; }
iso discard "$a_id" && [[ "$(jfield "$a_id" state)" == DISCARDED ]] || { echo 'FAIL: discard must succeed once the patch can be written'; exit 1; }
echo 'PASS: discard keeps the work as a patch, retires ABANDONED/CONFLICT to DISCARDED and refuses everything else'

# H1b: a session root replaced by a symlink or by another directory is never
# staged; sessions without the inode record fall back to a physical-path check.
mkdir -p "$TMP/swap-target"
git -C "$TMP/swap-target" init -q -b main
printf 'stolen\n' > "$TMP/swap-target/stolen.txt"
expect_root_refused() {  # expect_root_refused <id> <why>
  local rc=0
  iso submit "$1" 2>"$TMP/swap.err" || rc=$?
  [[ "$rc" == 10 ]] && grep -q 'session root' "$TMP/swap.err" \
    || { cat "$TMP/swap.err"; echo "FAIL: submit of a $2 root must refuse with exit 10 (rc=$rc)"; exit 1; }
  [[ "$(jfield "$1" state)" == OPEN && ! -e "$STATE/sessions/$1/submission.patch" ]] \
    || { echo "FAIL: a refused $2 root must stay OPEN with no patch"; exit 1; }
}
for kind in symlink directory legacy-symlink; do
  out="$(create)"
  s_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; s_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
  [[ -s "$STATE/sessions/$s_id/root-inode" ]] || { echo 'FAIL: create must record the session root inode'; exit 1; }
  printf 'x\n' > "$s_root/swap-$kind.txt"
  mv "$s_root" "$TMP/swapped-$kind"
  case "$kind" in
    symlink) ln -s "$TMP/swap-target" "$s_root" ;;
    directory) mkdir "$s_root" && cp -R "$TMP/swapped-$kind/." "$s_root/" ;;
    legacy-symlink) mv "$STATE/sessions/$s_id/root-inode" "$TMP/root-inode-$s_id" && ln -s "$TMP/swap-target" "$s_root" ;;
  esac
  expect_root_refused "$s_id" "$kind"
done
[[ -z "$(git -C "$TMP/swap-target" status --porcelain --untracked-files=no)" ]] || { echo 'FAIL: the symlink target was staged'; exit 1; }
# A session from an older launcher (no inode record) with its own root still submits.
out="$(create)"
s_root="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"; s_id="$(printf '%s\n' "$out" | sed -n 's/^HARNESS_SESSION_ID=//p')"
mv "$STATE/sessions/$s_id/root-inode" "$TMP/root-inode-$s_id"
printf 'legacy\n' > "$s_root/legacy.txt"
iso submit "$s_id" && [[ "$(jfield "$s_id" state)" == SUBMITTED ]] || { echo 'FAIL: a legacy session with its real root must submit'; exit 1; }
echo 'PASS: submit refuses a session root replaced by a symlink or another directory'
