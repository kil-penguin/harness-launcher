#!/usr/bin/env zsh
# test-orca-resume.sh — Orca restores an agent as `<override> <default args>
# --resume <id>` (Claude) or `<override> codex resume <id>` (Codex). For
# isolation-default profiles the launcher must (a) give a fresh isolated Claude
# launch a session id it can map back, and (b) map that restore argv to the
# owning isolated session instead of rejecting it.
set -e
# Tests never inherit the developer's terminal runtime (herdr, Orca, cmux).
unset HARNESS_TERMINAL_RUNTIME TERM_PROGRAM; unset -m 'HERDR_*' 'ORCA_*' 'CMUX_*' || true

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
HARNESS="$TMP/harness"
OTHER="$TMP/other-harness"
STATE="$TMP/state"
mkdir -p "$HARNESS/config" "$OTHER" "$TMP/bin"
git -C "$HARNESS" init -q -b main
git -C "$HARNESS" config user.email test@example.invalid
git -C "$HARNESS" config user.name test
print -r -- 'HARNESS_NAME="test"' 'HARNESS_PREFIX="test"' 'HARNESS_SESSION_ISOLATION_DEFAULT="1"' > "$HARNESS/config/launcher.env"
git -C "$HARNESS" add -A && git -C "$HARNESS" commit -qm initial

cat > "$TMP/bin/claude" <<'STUB'
#!/usr/bin/env bash
{ printf 'SESSION=%s\n' "${HARNESS_SESSION_ROOT:-}"; printf 'ARG=%s\n' "$@"; } > "$STUB_LOG"
if [[ -n "${WAIT_START:-}" ]]; then
  touch "$WAIT_START"
  while [[ ! -e "$WAIT_RELEASE" ]]; do sleep 0.05; done
fi
STUB
chmod +x "$TMP/bin/claude"

fail() { echo "FAIL: $*" >&2; exit 1; }

# run <log> [ENV=val ...] -- args : non-tty launcher call (ambient isolation route)
run() {
  local log="$1"; shift
  local -a envs=()
  while [[ "$1" != -- ]]; do envs+=("$1"); shift; done; shift
  rm -f "$log" "$log.err"
  (
    export PATH="$TMP/bin:/usr/bin:/bin" HARNESS_SESSION_STATE_HOME="$STATE" STUB_LOG="$log" "${envs[@]}"
    source "$ROOT/bin/aliases.zsh"
    _harness_launcher_run "$HARNESS" "$@"
  ) >/dev/null 2>"$log.err"
}

# run_tty <log> args : interactive launcher call (profile-default route)
run_tty() {
  local log="$1"; shift
  rm -f "$log" "$log.err"
  ROOT="$ROOT" HARNESS="$HARNESS" STATE="$STATE" TMP="$TMP" STUB_LOG="$log" TEST_ARGS="${(j: :)${(q)@}}" expect <<'EXPECT'
set timeout 20
log_user 0
spawn env ROOT=$env(ROOT) HARNESS=$env(HARNESS) STATE=$env(STATE) TMP=$env(TMP) STUB_LOG=$env(STUB_LOG) TEST_ARGS=$env(TEST_ARGS) PATH=$env(TMP)/bin:/usr/bin:/bin zsh -c {
  export PATH="$TMP/bin:/usr/bin:/bin" HARNESS_SESSION_STATE_HOME="$STATE"
  unset CMUX_WORKSPACE_ID CMUX_TAB_ID CMUX_SURFACE_ID
  source "$ROOT/bin/aliases.zsh"
  eval "set -- $TEST_ARGS"
  _harness_launcher_run "$HARNESS" "$@"
}
expect eof
catch wait result
exit [lindex $result 3]
EXPECT
}

argv_has() { grep -qxF -- "ARG=$2" "$1"; }
arg_after() { awk -v k="ARG=$2" 'f{print; exit} $0==k{f=1}' "$1"; }
session_count() { find "$STATE/sessions" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' '; }
UUID_RE='^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'

# --- fresh launch: --name kept, lowercase --session-id appended -------------
run_tty "$TMP/fresh.log" base || fail 'fresh interactive isolated launch failed'
root="$(sed -n 's/^SESSION=//p' "$TMP/fresh.log")"
ID="$(basename "$root")"
[[ "$ID" =~ $UUID_RE ]] || fail "session dir is not a UUID: $ID"
argv_has "$TMP/fresh.log" --name || fail 'fresh launch lost the bootstrap --name'
lower="${(L)ID}"
[[ "$(arg_after "$TMP/fresh.log" --session-id)" == "ARG=$lower" ]] || fail 'fresh launch must carry --session-id <lowercase id>'
# --session-id comes after --name (bootstrap eligibility rejects --session-id).
name_line="$(grep -n '^ARG=--name$' "$TMP/fresh.log" | cut -d: -f1)"
sid_line="$(grep -n '^ARG=--session-id$' "$TMP/fresh.log" | cut -d: -f1)"
(( sid_line > name_line )) || fail '--session-id must be appended after --name'

# An argv that already names a session is left alone.
run "$TMP/own.log" HARNESS_SESSION_ISOLATION=1 -- base --session-id 11111111-2222-4333-8444-555555555555 || fail 'explicit --session-id launch failed'
[[ "$(grep -c '^ARG=--session-id$' "$TMP/own.log")" == 1 ]] || fail 'must not add a second --session-id'

# Session-selecting flags outside the isolate route get no --session-id.
for flags in "--from-pr 12" "--teleport abc" "--bg" "--remote-control"; do
  run "$TMP/legacyflag.log" HARNESS_SESSION_ISOLATION=1 -- base ${=flags} || { cat "$TMP/legacyflag.log.err" >&2; fail "launch with $flags failed"; }
  ! grep -q '^ARG=--session-id$' "$TMP/legacyflag.log" || fail "--session-id must not be added next to $flags"
done

# --- Claude resume mapping ---------------------------------------------------
before="$(session_count)"
for form in "--resume $lower" "--resume=$lower" "-r $lower" "-r$lower" "--resume ${(U)lower}"; do
  run "$TMP/resume.log" HARNESS_SESSION_ISOLATION=1 -- base ${=form} || { cat "$TMP/resume.log.err" >&2; fail "resume form '$form' was not mapped"; }
  [[ "$(sed -n 's/^SESSION=//p' "$TMP/resume.log")" == "$root" ]] || fail "resume form '$form' did not land in the owning session root"
done
[[ "$(session_count)" == "$before" ]] || fail 'resume must not create a session'
# A caller resume after --passthrough maps to the owning session too.
run "$TMP/resume-pt.log" HARNESS_SESSION_ISOLATION=1 -- base --passthrough --resume=$lower \
  || { cat "$TMP/resume-pt.log.err" >&2; fail 'passthrough resume was not mapped'; }
[[ "$(sed -n 's/^SESSION=//p' "$TMP/resume-pt.log")" == "$root" ]] || fail 'passthrough resume used the wrong root'
! grep -q '^ARG=--session-id$' "$TMP/resume-pt.log" || fail 'passthrough resume must not get --session-id'
[[ "$(session_count)" == "$before" ]] || fail 'passthrough resume must not create a session'
# A fresh isolated passthrough launch gets exactly one launcher --session-id,
# ahead of the caller argv.
run "$TMP/fresh-pt.log" HARNESS_SESSION_ISOLATION=1 -- base --passthrough --permission-mode plan \
  || { cat "$TMP/fresh-pt.log.err" >&2; fail 'fresh isolated passthrough launch failed'; }
[[ "$(grep -c '^ARG=--session-id$' "$TMP/fresh-pt.log")" == 1 ]] || fail 'fresh passthrough launch needs one --session-id'
sid_line="$(grep -n '^ARG=--session-id$' "$TMP/fresh-pt.log" | cut -d: -f1)"
pm_line="$(grep -n '^ARG=--permission-mode$' "$TMP/fresh-pt.log" | cut -d: -f1)"
(( sid_line < pm_line )) || fail '--session-id must precede the passthrough argv'
[[ "$(arg_after "$TMP/fresh-pt.log" --permission-mode)" == 'ARG=plan' ]] || fail 'passthrough permission mode rewritten'
# Routing stops at the first bare word, so a caller session flag after it
# still reaches a fresh isolated launch; the launcher must not add its own.
run "$TMP/fresh-pt-own.log" HARNESS_SESSION_ISOLATION=1 -- base --passthrough 'prompt text' --session-id=11111111-2222-4333-8444-555555555555 \
  || { cat "$TMP/fresh-pt-own.log.err" >&2; fail 'fresh passthrough launch with caller session id failed'; }
! grep -q '^ARG=--session-id$' "$TMP/fresh-pt-own.log" || fail 'launcher --session-id added next to a caller session flag'
grep -qxF 'ARG=--session-id=11111111-2222-4333-8444-555555555555' "$TMP/fresh-pt-own.log" || fail 'caller session id lost'
before="$(session_count)"
# default (profile) route, two consecutive restores
for n in 1 2; do
  run_tty "$TMP/resume-tty.log" base --resume "$lower" || fail "profile-default restore $n was rejected"
  [[ "$(sed -n 's/^SESSION=//p' "$TMP/resume-tty.log")" == "$root" ]] || fail "profile-default restore $n used the wrong root"
done

# --- rejection paths ---------------------------------------------------------
reject_msg='this profile isolates fresh sessions'
UNKNOWN=99999999-2222-4333-8444-555555555555
if run "$TMP/unknown.log" HARNESS_SESSION_ISOLATION=1 -- base --resume $UNKNOWN; then fail 'unknown id must be rejected'; fi
grep -q -- "$reject_msg\|continuation requires" "$TMP/unknown.log.err" || fail 'unknown id must keep the unchanged reject message'
[[ ! -e "$TMP/unknown.log" ]] || fail 'unknown id must not launch'
if run "$TMP/nonuuid.log" HARNESS_SESSION_ISOLATION=1 -- base --resume not-a-uuid; then fail 'non-UUID id must be rejected'; fi
grep -q -- "continuation requires" "$TMP/nonuuid.log.err" || fail 'non-UUID id must keep the unchanged reject message'
if run_tty "$TMP/unknown-tty.log" base --resume $UNKNOWN; then fail 'unknown id (default route) must be rejected'; fi
[[ ! -e "$TMP/unknown-tty.log" ]] || fail 'unknown id (default route) must not launch'

# Another harness's source-root is ignored.
cp "$STATE/sessions/$ID/source-root" "$TMP/source-root.bak"
print -r -- "$OTHER" > "$STATE/sessions/$ID/source-root"
if run "$TMP/foreign.log" HARNESS_SESSION_ISOLATION=1 -- base --resume $lower; then fail 'foreign source-root must be ignored'; fi
grep -q -- "continuation requires" "$TMP/foreign.log.err" || fail 'foreign source-root must keep the unchanged reject message'
cp "$TMP/source-root.bak" "$STATE/sessions/$ID/source-root"

# Explicit --no-isolated still wins (legacy launch, no isolated root).
run "$TMP/noiso.log" HARNESS_SESSION_ISOLATION=1 -- --no-isolated base --resume $lower || fail '--no-isolated launch failed'
[[ -z "$(sed -n 's/^SESSION=//p' "$TMP/noiso.log")" ]] || fail '--no-isolated must not map into an isolated session'

# Profiles with default 0 are unaffected.
cp "$HARNESS/config/launcher.env" "$TMP/launcher.env.bak"
print -r -- 'HARNESS_SESSION_ISOLATION_DEFAULT="0"' >> "$HARNESS/config/launcher.env"
run "$TMP/default0.log" -- base --resume $lower || fail 'default-0 resume failed'
[[ -z "$(sed -n 's/^SESSION=//p' "$TMP/default0.log")" ]] || fail 'default-0 profile must not isolate a resume'
cp "$TMP/launcher.env.bak" "$HARNESS/config/launcher.env"

# --- specific failure messages ----------------------------------------------
# Held lease.
run "$TMP/lease-owner.log" HARNESS_SESSION_ISOLATION=1 WAIT_START="$TMP/ls" WAIT_RELEASE="$TMP/lr" -- base --resume $lower & owner=$!
for _ in {1..300}; do [[ -f "$TMP/ls" ]] && break; sleep 0.05; done
[[ -f "$TMP/ls" ]] || { kill $owner 2>/dev/null || true; fail 'lease owner did not start'; }
if run "$TMP/lease.log" HARNESS_SESSION_ISOLATION=1 -- base --resume $lower; then touch "$TMP/lr"; wait $owner; fail 'held lease must be rejected'; fi
touch "$TMP/lr"; wait $owner
grep -q 'already active' "$TMP/lease.log.err" || fail 'held lease must say the session is already active'
[[ ! -e "$TMP/lease.log" ]] || fail 'held lease must not launch'

# Delivered session.
cp "$STATE/sessions/$ID/journal" "$TMP/journal.bak"
printf 'state=DELIVERED\nidentity=%s\nheartbeat=2026-01-01T00:00:00Z\n' "$(printf '%064d' 0)" > "$STATE/sessions/$ID/journal"
if run "$TMP/delivered.log" HARNESS_SESSION_ISOLATION=1 -- base --resume $lower; then fail 'delivered session must be rejected'; fi
grep -q 'DELIVERED' "$TMP/delivered.log.err" || fail 'delivered session needs its own message'
[[ ! -e "$TMP/delivered.log" ]] || fail 'delivered session must not launch'
cp "$TMP/journal.bak" "$STATE/sessions/$ID/journal"

# --- Codex rollout mapping ---------------------------------------------------
CODEX_ID=aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee
rollout_dir="$STATE/worktrees/$ID/.harness/codex/sessions/2026/09/29"
mkdir -p "$rollout_dir"
: > "$rollout_dir/rollout-2026-09-29T01-02-03-$CODEX_ID.jsonl"
run_codex() { # <log> [ENV=val ...] -- args
  local log="$1"; shift
  local -a envs=()
  while [[ "$1" != -- ]]; do envs+=("$1"); shift; done; shift
  rm -f "$log" "$log.err"
  (
    export PATH="$TMP/bin:/usr/bin:/bin" HARNESS_SESSION_STATE_HOME="$STATE" STUB_LOG="$log" "${envs[@]}"
    source "$ROOT/bin/aliases.zsh"
    # Exercise the owning-root resolver retained for older pinned runtimes.
    # Current durable catalog/restore uses real metadata in its own integration
    # suite; these deliberately empty rollout fixtures carry only filenames.
    _harness_launcher_codex_history_request() { return 1; }
    _harness_launcher_run_codex_cli() { printf 'SESSION=%s\nARGS=%s\n' "${HARNESS_SESSION_ROOT:-}" "$*" > "$STUB_LOG"; }
    _harness_launcher_run "$HARNESS" "$@"
  ) >/dev/null 2>"$log.err"
}
run_codex "$TMP/codex.log" HARNESS_SESSION_ISOLATION=1 -- codex resume $CODEX_ID || { cat "$TMP/codex.log.err" >&2; fail 'codex rollout id was not mapped'; }
[[ "$(sed -n 's/^SESSION=//p' "$TMP/codex.log")" == "$root" ]] || fail 'codex resume landed in the wrong root'
if run_codex "$TMP/codex-unknown.log" HARNESS_SESSION_ISOLATION=1 -- codex resume $UNKNOWN; then fail 'unknown codex id must be rejected'; fi
grep -q 'continuation requires' "$TMP/codex-unknown.log.err" || fail 'unknown codex id must keep the unchanged reject message'

# Duplicate owner is invalid: a second session holds the same rollout id.
run "$TMP/second.log" HARNESS_SESSION_ISOLATION=1 -- base || fail 'second fresh session failed'
ID2="$(basename "$(sed -n 's/^SESSION=//p' "$TMP/second.log")")"
mkdir -p "$STATE/worktrees/$ID2/.harness/codex/sessions/2026/09/29"
: > "$STATE/worktrees/$ID2/.harness/codex/sessions/2026/09/29/rollout-2026-09-29T01-02-03-$CODEX_ID.jsonl"
if run_codex "$TMP/codex-dup.log" HARNESS_SESSION_ISOLATION=1 -- codex resume $CODEX_ID; then fail 'duplicate rollout owner must be invalid'; fi
grep -q 'ambiguous' "$TMP/codex-dup.log.err" || fail 'duplicate owner must say ambiguous'
[[ ! -e "$TMP/codex-dup.log" ]] || fail 'duplicate owner must not launch'

# Garbage-collected workspace (session record kept, worktree retired).
rm -rf "$STATE/worktrees/$ID2" "$STATE/worktrees/$ID"
if run "$TMP/gc.log" HARNESS_SESSION_ISOLATION=1 -- base --resume ${(L)ID2}; then fail 'retired workspace must be rejected'; fi
grep -q 'missing or retired' "$TMP/gc.log.err" || fail 'retired workspace needs its own message'
grep -q 'could not restore isolated session' "$TMP/gc.log.err" || fail 'mapped restore failure must be reported'
[[ ! -e "$TMP/gc.log" ]] || fail 'retired workspace must not launch'


# --- Claude provider-session records (/clear ids) ----------------------------
# A resume id created by /clear is recorded in the owning session's
# provider-sessions by harness-session-provider-record. It maps back only when
# exactly one current-harness session owns it and, for a record-only owner,
# its transcript exists.
CLAUDE_CFG="$TMP/claude-config"
run "$TMP/pa.log" HARNESS_SESSION_ISOLATION=1 -- base || fail 'fresh session A failed'
run "$TMP/pb.log" HARNESS_SESSION_ISOLATION=1 -- base || fail 'fresh session B failed'
SA="$(basename "$(sed -n 's/^SESSION=//p' "$TMP/pa.log")")"
SB="$(basename "$(sed -n 's/^SESSION=//p' "$TMP/pb.log")")"
root_of() { cat "$STATE/sessions/$1/session-root"; }
enc_of() { local r="${1:A}"; print -r -- "${r//[^A-Za-z0-9]/-}"; }
transcript_for() { # <session> <id> : create the transcript at Claude's project path
  local enc
  enc="$(enc_of "$(root_of "$1")")"
  mkdir -p "$CLAUDE_CFG/projects/$enc"; : > "$CLAUDE_CFG/projects/$enc/$2.jsonl"
}
record() { print -r -- "claude $2" >> "$STATE/sessions/$1/provider-sessions"; }
resume_in() { # <log> <id> : ambient isolated resume with the Claude config dir
  run "$1" HARNESS_SESSION_ISOLATION=1 CLAUDE_CONFIG_DIR="$CLAUDE_CFG" -- base --resume "$2"
}
CL1=aaaaaaaa-0000-4000-8000-000000000001
CL2=aaaaaaaa-0000-4000-8000-000000000002
CL3=aaaaaaaa-0000-4000-8000-000000000003

# (a) the launch id still maps (no transcript needed, no record needed)
resume_in "$TMP/ra.log" "${(L)SA}" || { cat "$TMP/ra.log.err" >&2; fail 'launch id no longer maps'; }
[[ "$(sed -n 's/^SESSION=//p' "$TMP/ra.log")" == "$(root_of $SA)" ]] || fail 'launch id landed in the wrong session'
# its own launch id recorded at startup is still one owner
record $SA "${(L)SA}"
resume_in "$TMP/ra2.log" "${(L)SA}" || fail 'own recorded launch id must map as one owner'

# (b) an id recorded by /clear maps to the recording session when its transcript exists
record $SA $CL1; transcript_for $SA $CL1
resume_in "$TMP/rb.log" ${(U)CL1} || { cat "$TMP/rb.log.err" >&2; fail 'recorded /clear id with transcript was not mapped'; }
[[ "$(sed -n 's/^SESSION=//p' "$TMP/rb.log")" == "$(root_of $SA)" ]] || fail 'recorded id landed in the wrong session'
# default (profile) route maps it too
CLAUDE_CONFIG_DIR="$CLAUDE_CFG" run_tty "$TMP/rb-tty.log" base --resume $CL1 || fail 'profile-default recorded id was rejected'
[[ "$(sed -n 's/^SESSION=//p' "$TMP/rb-tty.log")" == "$(root_of $SA)" ]] || fail 'profile-default recorded id used the wrong root'

# (c) recorded without a transcript keeps the unchanged reject message
record $SA $CL2
if resume_in "$TMP/rc.log" $CL2; then fail 'recorded id without transcript must be rejected'; fi
grep -q 'continuation requires' "$TMP/rc.log.err" || fail 'no-transcript reject must keep the unchanged message'
[[ ! -e "$TMP/rc.log" ]] || fail 'no-transcript reject must not launch'
# a transcript stored under another session's project dir does not count
transcript_for $SB $CL2
if resume_in "$TMP/rc2.log" $CL2; then fail 'transcript of another session must not count'; fi
# a transcript that is a symlink does not count
enc_a="$(enc_of "$(root_of $SA)")"
: > "$TMP/real.jsonl"; ln -s "$TMP/real.jsonl" "$CLAUDE_CFG/projects/$enc_a/$CL2.jsonl"
if resume_in "$TMP/rc3.log" $CL2; then fail 'symlinked transcript must not count'; fi
grep -q 'continuation requires' "$TMP/rc3.log.err" || fail 'symlinked transcript must keep the reject message'

# (d) an id naming session A's directory that is also recorded in B is ambiguous
record $SB "${(L)SA}"
if resume_in "$TMP/rd.log" "${(L)SA}"; then fail 'directory owner plus recorder must be invalid'; fi
grep -q 'ambiguous' "$TMP/rd.log.err" || fail 'directory plus recorder must say ambiguous'
[[ ! -e "$TMP/rd.log" ]] || fail 'ambiguous id must not launch'

# (e) an id recorded in two sessions is ambiguous
record $SA $CL3; record $SB $CL3; transcript_for $SA $CL3
if resume_in "$TMP/re.log" $CL3; then fail 'id recorded in two sessions must be invalid'; fi
grep -q 'ambiguous' "$TMP/re.log.err" || fail 'two recorders must say ambiguous'

# (f) a record in another harness's session is ignored
CL4=aaaaaaaa-0000-4000-8000-000000000004
record $SB $CL4; transcript_for $SB $CL4
resume_in "$TMP/rf0.log" $CL4 || fail 'control: recorded id in B must map before source-root change'
cp "$STATE/sessions/$SB/source-root" "$TMP/sb-source-root.bak"
print -r -- "$OTHER" > "$STATE/sessions/$SB/source-root"
if resume_in "$TMP/rf.log" $CL4; then fail 'record in a foreign-harness session must be ignored'; fi
grep -q 'continuation requires' "$TMP/rf.log.err" || fail 'foreign record must keep the reject message'
# a foreign record does not make an otherwise unique owner ambiguous
CL5=aaaaaaaa-0000-4000-8000-000000000005
record $SA $CL5; record $SB $CL5; transcript_for $SA $CL5
resume_in "$TMP/rf2.log" $CL5 || fail 'foreign record must not add an owner'
cp "$TMP/sb-source-root.bak" "$STATE/sessions/$SB/source-root"

# Untrusted records: other lines, wrong provider, symlinked file are ignored.
CL6=aaaaaaaa-0000-4000-8000-000000000006
print -r -- "codex $CL6" > "$STATE/sessions/$SA/provider-sessions.tmp"
{ print -r -- "codex $CL6"; print -r -- "claude $CL6 extra"; print -r -- "junk"; } >> "$STATE/sessions/$SA/provider-sessions"
transcript_for $SA $CL6
if resume_in "$TMP/rg.log" $CL6; then fail 'non-exact record lines must be ignored'; fi
find "$STATE/sessions/$SA" -name 'provider-sessions.tmp' -delete
cp "$STATE/sessions/$SA/provider-sessions" "$TMP/pslinktarget"
find "$STATE/sessions/$SA" -name provider-sessions -delete
print -r -- "claude $CL6" > "$TMP/pslinktarget"
ln -s "$TMP/pslinktarget" "$STATE/sessions/$SA/provider-sessions"
if resume_in "$TMP/rh.log" $CL6; then fail 'symlinked provider-sessions must be ignored'; fi

echo 'PASS: test-orca-resume'
