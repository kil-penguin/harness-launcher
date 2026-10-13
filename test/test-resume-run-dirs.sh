#!/usr/bin/env zsh
# test-resume-run-dirs.sh — a Claude resume after /clear maps back to its
# isolated session when the agent ran in its mapped clone directory.
#
# harness-exec always passes `--cwd <caller dir>` (the caller's $PWD inside the
# harness, else the harness root). Isolation maps it to the same relative path
# in the clone, where Claude files its transcripts. Older sessions may retain
# source-tree run directories, which remain covered by the legacy fixtures below.
# The launcher records every run directory of an isolated session in
# <state>/sessions/<id>/run-dirs, and the restore resolver looks for a
# record-only owner's transcript under the session root and every valid
# recorded run directory. The records are trusted only as far as the L4/A4
# rules: regular non-symlink file, absolute lines resolving inside the
# session's own source root or session root.
set -e
# Tests never inherit the developer's terminal runtime (herdr, Orca, cmux).
unset HARNESS_TERMINAL_RUNTIME TERM_PROGRAM; unset -m 'HERDR_*' 'ORCA_*' 'CMUX_*' || true

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
HARNESS="$TMP/harness"
STATE="$TMP/state"
CFG="$TMP/claude-config"
OUTSIDE="$TMP/outside"
mkdir -p "$TMP/zdot" "$HARNESS/config" "$HARNESS/sub" "$HARNESS/sub2" "$HARNESS/sub3" "$HARNESS/sub4" "$OUTSIDE" "$TMP/bin"
git -C "$HARNESS" init -q -b main
git -C "$HARNESS" config user.email test@example.invalid
git -C "$HARNESS" config user.name test
print -r -- 'HARNESS_NAME="test"' 'HARNESS_PREFIX="test"' 'HARNESS_SESSION_ISOLATION_DEFAULT="1"' > "$HARNESS/config/launcher.env"
for d in sub sub2 sub3 sub4; do : > "$HARNESS/$d/.keep"; done
git -C "$HARNESS" add -A && git -C "$HARNESS" commit -qm initial

# Stub claude: files its transcript the way Claude Code does (the physical cwd
# with every non-alphanumeric byte turned into '-'); CLEAR_TO simulates /clear:
# a new transcript plus the SessionStart(source=clear) recorder hook.
cat > "$TMP/bin/claude" <<'STUB'
#!/usr/bin/env bash
cwd="$(pwd -P)"
{ printf 'SESSION=%s\n' "${HARNESS_SESSION_ROOT:-}"; printf 'CWD=%s\n' "$cwd"; printf 'ARG=%s\n' "$@"; } > "$STUB_LOG"
id="" prev=""
for a in "$@"; do
  case "$prev" in --session-id|--resume) id="$a" ;; esac
  prev="$a"
done
enc="$(printf '%s' "$cwd" | sed 's/[^A-Za-z0-9]/-/g')"
mkdir -p "$CLAUDE_CONFIG_DIR/projects/$enc"
[ -z "$id" ] || : > "$CLAUDE_CONFIG_DIR/projects/$enc/$id.jsonl"
if [ -n "${CLEAR_TO:-}" ]; then
  : > "$CLAUDE_CONFIG_DIR/projects/$enc/$CLEAR_TO.jsonl"
  printf '{"hook_event_name":"SessionStart","source":"clear","session_id":"%s"}' "$CLEAR_TO" | "$RECORDER"
fi
STUB
chmod +x "$TMP/bin/claude"

fail() { echo "FAIL: $*" >&2; exit 1; }

# exec_in <dir> <log> [ENV=val ...] -- launcher args : harness-exec from <dir>,
# no terminal, ambient isolation, bounded to 60 s. ZDOTDIR keeps the user's
# ~/.zshenv (which may put the real claude first on PATH) out of harness-exec.
exec_in() {
  local dir="$1" log="$2"; shift 2
  local -a envs=()
  while [[ "$1" != -- ]]; do envs+=("$1"); shift; done; shift
  rm -f "$log" "$log.err"
  (
    cd "$dir"
    perl -e 'alarm 60; exec @ARGV' env -i HOME="$HOME" PATH="$TMP/bin:/usr/bin:/bin" \
      TERM=xterm-256color LANG=en_US.UTF-8 ZDOTDIR="$TMP/zdot" HARNESS_SESSION_STATE_HOME="$STATE" \
      HARNESS_SESSION_ISOLATION=1 HARNESS_SESSION_HEARTBEAT_SECONDS=1 CLAUDE_CONFIG_DIR="$CFG" STUB_LOG="$log" \
      RECORDER="$ROOT/bin/harness-session-provider-record" "${envs[@]}" \
      /bin/zsh "$ROOT/bin/harness-exec" "$HARNESS" "$@"
  ) </dev/null >/dev/null 2>"$log.err"
}
session_of() { sed -n 's/^SESSION=//p' "$1"; }
cwd_of() { sed -n 's/^CWD=//p' "$1"; }
enc_of() { local r="${1:A}"; print -r -- "${r//[^A-Za-z0-9]/-}"; }
reject_msg='continuation requires'

# --- end to end: launch, /clear, restore -------------------------------------
typeset -A SESSION_BY_DIR
n=0
for dir in "$HARNESS" "$HARNESS/sub"; do
  n=$((n + 1))
  cleared="bbbbbbbb-0000-4000-8000-00000000000$n"
  exec_in "$dir" "$TMP/launch$n.log" CLEAR_TO="$cleared" -- base \
    || { cat "$TMP/launch$n.log.err" >&2; fail "fresh launch from $dir failed"; }
  root="$(session_of "$TMP/launch$n.log")"
  id="${root:t}"
  run_dir="${root:A}${dir#$HARNESS}"
  [[ -n "$root" && "$(cwd_of "$TMP/launch$n.log")" == "$run_dir" ]] || fail "agent did not run in the mapped clone dir $run_dir"
  grep -qxF "claude $cleared" "$STATE/sessions/$id/provider-sessions" || fail 'recorder did not record the /clear id'
  grep -qxF -- "$run_dir" "$STATE/sessions/$id/run-dirs" || fail "run dir $run_dir was not recorded"
  for try in 1 2; do
    exec_in "$dir" "$TMP/restore$n.log" -- base --resume "$cleared" \
      || { cat "$TMP/restore$n.log.err" >&2; fail "restore of /clear id from $dir (try $try) was not mapped"; }
    [[ "$(session_of "$TMP/restore$n.log")" == "$root" ]] || fail "restore from $dir landed in the wrong session"
    [[ "$(cwd_of "$TMP/restore$n.log")" == "$run_dir" ]] || fail "restore from $dir did not retain mapped clone cwd $run_dir"
  done
  [[ "$(grep -cxF -- "$run_dir" "$STATE/sessions/$id/run-dirs")" == 1 ]] || fail 'an identical run-dirs line must not be appended twice'
  [[ -f "$STATE/sessions/$id/run-dirs" && ! -L "$STATE/sessions/$id/run-dirs" ]] || fail 'run-dirs must be a regular file'
  SESSION_BY_DIR[$dir]="$id"
done
SA="${SESSION_BY_DIR[$HARNESS]}"
SB="${SESSION_BY_DIR[$HARNESS/sub]}"
[[ "$SA" != "$SB" ]] || fail 'the two launches must own different sessions'
echo 'ok: resume after /clear maps from the harness root and a subdirectory'

record() { print -r -- "claude $2" >> "$STATE/sessions/$1/provider-sessions"; }
transcript_in() { # <dir> <id>
  local enc; enc="$(enc_of "$1")"
  mkdir -p "$CFG/projects/$enc"; : > "$CFG/projects/$enc/$2.jsonl"
}
restore() { exec_in "$HARNESS" "$1" -- base --resume "$2"; }

# --- a run-dirs entry outside the source and session roots is ignored ---------
N1=cccccccc-0000-4000-8000-000000000001
record "$SA" "$N1"; transcript_in "$OUTSIDE" "$N1"
print -r -- "${OUTSIDE:A}" >> "$STATE/sessions/$SA/run-dirs"
if restore "$TMP/n1.log" "$N1"; then fail 'a run dir outside the harness must not count'; fi
grep -q -- "$reject_msg" "$TMP/n1.log.err" || fail 'outside run dir must keep the reject message'
# Relative and dot-dot lines do not count either.
print -r -- "outside" "$HARNESS/../outside" "${HARNESS:A}/../outside" >> "$STATE/sessions/$SA/run-dirs"
raw_enc="$HARNESS/../outside"; raw_enc="${raw_enc//[^A-Za-z0-9]/-}"
mkdir -p "$CFG/projects/$raw_enc"; : > "$CFG/projects/$raw_enc/$N1.jsonl"
mkdir -p "$CFG/projects/outside"; : > "$CFG/projects/outside/$N1.jsonl"
# A dot-dot line that resolves inside the harness must not widen the lookup to
# its raw spelling's project directory.
dot_line="${HARNESS:A}/sub3/.."; dot_enc="${dot_line//[^A-Za-z0-9]/-}"
print -r -- "$dot_line" >> "$STATE/sessions/$SA/run-dirs"
mkdir -p "$CFG/projects/$dot_enc"; : > "$CFG/projects/$dot_enc/$N1.jsonl"
if restore "$TMP/n1b.log" "$N1"; then fail 'relative or dot-dot run dirs must not count'; fi

# --- a symlinked run-dirs is ignored -------------------------------------------
N2=cccccccc-0000-4000-8000-000000000002
record "$SB" "$N2"; transcript_in "$HARNESS/sub3" "$N2"
cp "$STATE/sessions/$SB/run-dirs" "$TMP/rd-target"
print -r -- "${HARNESS:A}/sub3" >> "$TMP/rd-target"
find "$STATE/sessions/$SB" -name run-dirs -delete
ln -s "$TMP/rd-target" "$STATE/sessions/$SB/run-dirs"
if restore "$TMP/n2.log" "$N2"; then fail 'a symlinked run-dirs must be ignored'; fi
grep -q -- "$reject_msg" "$TMP/n2.log.err" || fail 'symlinked run-dirs must keep the reject message'
# control: the same content as a regular file maps
find "$STATE/sessions/$SB" -name run-dirs -delete
cp "$TMP/rd-target" "$STATE/sessions/$SB/run-dirs"
restore "$TMP/n2c.log" "$N2" || { cat "$TMP/n2c.log.err" >&2; fail 'control: a regular run-dirs with an inside line must map'; }
[[ "$(session_of "$TMP/n2c.log")" == "$(<"$STATE/sessions/$SB/session-root")" ]] || fail 'control restore landed in the wrong session'

# --- a forged line cannot make another session's transcript count -------------
N3=cccccccc-0000-4000-8000-000000000003
SA_ROOT="$(<"$STATE/sessions/$SA/session-root")"
transcript_in "$SA_ROOT" "$N3"   # A's session-root project dir; A does not record N3
record "$SB" "$N3"
print -r -- "${SA_ROOT:A}" "$SA_ROOT" "$STATE/sessions/$SA" >> "$STATE/sessions/$SB/run-dirs"
if restore "$TMP/n3.log" "$N3"; then fail "a forged run-dirs line must not borrow another session's transcript"; fi
grep -q -- "$reject_msg" "$TMP/n3.log.err" || fail 'forged line must keep the reject message'
# A line naming another session's worktree through the harness does not help either.
ln -s "$SA_ROOT" "$HARNESS/sub4/link-to-a"
print -r -- "${HARNESS:A}/sub4/link-to-a" >> "$STATE/sessions/$SB/run-dirs"
if restore "$TMP/n3b.log" "$N3"; then fail 'a run-dirs line through a symlink out of the harness must not count'; fi
find "$HARNESS/sub4" -name link-to-a -delete

# --- only canonical lines count ------------------------------------------------
# A line must equal its own resolved absolute form. A trailing slash or a
# trailing carriage return encodes to <dir>- and a symlinked alias to the
# alias's own project directory, none of which is the directory Claude used.
mkdir -p "$HARNESS/sub5"
D5="${HARNESS:A}/sub5"
ln -s "$D5" "$HARNESS/alias5"
N5=cccccccc-0000-4000-8000-000000000005
record "$SA" "$N5"
for bad in "$D5/" "$D5"$'\r' "${HARNESS:A}/alias5"; do
  transcript_in_raw() { local enc="${1//[^A-Za-z0-9]/-}"; mkdir -p "$CFG/projects/$enc"; : > "$CFG/projects/$enc/$2.jsonl"; }
  transcript_in_raw "$bad" "$N5"
  print -r -- "$bad" >> "$STATE/sessions/$SA/run-dirs"
  if restore "$TMP/n5.log" "$N5"; then fail "a non-canonical run-dirs line must not count: ${(q+)bad}"; fi
  grep -q -- "$reject_msg" "$TMP/n5.log.err" || fail "non-canonical line ${(q+)bad} must keep the reject message"
  find "$CFG/projects" -name "$N5.jsonl" -delete
done
find "$HARNESS" -maxdepth 1 -name alias5 -delete

# --- a forged source-tree line only confirms ids its session owns --------------
# Older harness-exec releases filed transcripts under source-tree directories.
# Session B's transcript lives under $D5; session A forges a canonical line for
# it. The owner count decides: an id recorded only by B is not A's (and B never
# ran in $D5, so it is rejected); recorded by both it is ambiguous; recorded by
# nobody it is rejected.
N6=cccccccc-0000-4000-8000-000000000006
N7=cccccccc-0000-4000-8000-000000000007
record "$SB" "$N6"; transcript_in "$D5" "$N6"; transcript_in "$D5" "$N7"
print -r -- "$D5" >> "$STATE/sessions/$SA/run-dirs"
if restore "$TMP/n6.log" "$N6"; then fail "a forged line in A must not map B's id (landed in $(session_of "$TMP/n6.log"))"; fi
grep -q -- "$reject_msg" "$TMP/n6.log.err" || fail "B-only id with a forged A line must keep the reject message"
record "$SA" "$N6"
if restore "$TMP/n6b.log" "$N6"; then fail 'an id recorded by A and B must not map'; fi
grep -q 'ambiguous' "$TMP/n6b.log.err" || fail 'an id recorded by A and B must be ambiguous'
if restore "$TMP/n7.log" "$N7"; then fail 'a forged line must not map an id with no owner'; fi
grep -q -- "$reject_msg" "$TMP/n7.log.err" || fail 'an unowned id must keep the reject message'

# --- the launcher never writes through a symlinked run-dirs -------------------
: > "$TMP/rd-victim"
find "$STATE/sessions/$SA" -name run-dirs -delete
ln -s "$TMP/rd-victim" "$STATE/sessions/$SA/run-dirs"
exec_in "$HARNESS/sub4" "$TMP/n4.log" -- base --resume "${(L)SA}" \
  || { cat "$TMP/n4.log.err" >&2; fail 'a symlinked run-dirs must not block the launch'; }
[[ "$(session_of "$TMP/n4.log")" == "$SA_ROOT" ]] || fail 'launch-id restore landed in the wrong session'
[[ ! -s "$TMP/rd-victim" ]] || fail 'the launcher wrote through a symlinked run-dirs'
find "$STATE/sessions/$SA" -name run-dirs -delete

# --- the recorder only records a directory inside the source or session root --
(
  export HARNESS_SESSION_STATE_HOME="$STATE"
  source "$ROOT/bin/aliases.zsh"
  HARNESS_SESSION_ID="$SA" HARNESS_SOURCE_ROOT="${HARNESS:A}" HARNESS_SESSION_ROOT="$SA_ROOT" \
    HARNESS_RUN_DIR="$OUTSIDE" _harness_launcher_isolated_record_run_dir
  HARNESS_SESSION_ID="$SA" HARNESS_SOURCE_ROOT="${HARNESS:A}" HARNESS_SESSION_ROOT="$SA_ROOT" \
    HARNESS_RUN_DIR="$SA_ROOT" _harness_launcher_isolated_record_run_dir
  HARNESS_SESSION_ID="../$SA" HARNESS_SOURCE_ROOT="${HARNESS:A}" HARNESS_SESSION_ROOT="$SA_ROOT" \
    HARNESS_RUN_DIR="$HARNESS" _harness_launcher_isolated_record_run_dir
) >"$TMP/rec.out" 2>&1 || fail 'the run-dir recorder must never fail'
[[ ! -s "$TMP/rec.out" ]] || fail 'the run-dir recorder must be silent'
[[ "$(cat "$STATE/sessions/$SA/run-dirs")" == "${SA_ROOT:A}" ]] || fail 'recorder must record only directories inside the source or session root'

echo 'PASS: test-resume-run-dirs'
