#!/usr/bin/env zsh
# test-restore-fidelity-claude.sh — a host restore of a Claude session
# (`claude --resume <id>` typed by herdr, Orca or `<prefix> --resume <id>`)
# keeps the session's model and effort (read from its transcript) and its
# permission grant (read only from the launcher-owned launch record).
set -e
unset HARNESS_TERMINAL_RUNTIME TERM_PROGRAM HARNESS_HOST_DEFAULT_MODE CLAUDECODE CODEX_THREAD_ID CODEX_SANDBOX HARNESS_LAUNCH_PERMISSION HARNESS_LAUNCH_APPROVAL HARNESS_LAUNCH_SANDBOX HARNESS_LAUNCH_BYPASS HARNESS_LAUNCH_SOURCE_ROOT HARNESS_LAUNCH_ISOLATED HARNESS_LAUNCH_PROFILE HARNESS_LAUNCH_NESTED; unset -m 'HERDR_*' 'ORCA_*' 'CMUX_*' || true

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCHER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP="$(mktemp -d)"; TMP="${TMP:A}"
trap 'rm -rf "$TMP"' EXIT
HARNESS="$TMP/harness"
BIN="$TMP/bin"
STATE="$TMP/state"
CCONF="$TMP/claude-config"
mkdir -p "$HARNESS/config" "$HARNESS/.claude" "$BIN" "$CCONF/projects/-tmp-proj"
print -r -- 'HARNESS_NAME="test"' 'HARNESS_PREFIX="test"' > "$HARNESS/config/launcher.env"
print -r -- '{"mcpServers":{"fixture":{"type":"stdio","command":"true"}}}' > "$HARNESS/.mcp.json"
PY="$(source "$LAUNCHER_DIR/bin/harness-common.sh"; harness_python3_resolve)"

cat > "$BIN/claude" <<'STUB'
#!/usr/bin/env bash
{
  for arg in "$@"; do printf 'ARG:%s\n' "$arg"; done
  for v in HARNESS_HOST_DEFAULT_MODE HARNESS_LAUNCH_PERMISSION HARNESS_LAUNCH_SOURCE_ROOT HARNESS_LAUNCH_ISOLATED; do
    printf 'ENV:%s=%s\n' "$v" "${!v-<unset>}"
  done
} > "$TEST_STUB_FILE"
STUB
chmod +x "$BIN/claude"

fail() { echo "FAIL: $1" >&2; [[ -f "${2:-}" ]] && sed 's/^/  /' "$2" >&2; [[ -f "${2:-}.err" ]] && sed 's/^/  err: /' "$2.err" >&2; exit 1; }

# run_claude <stub-file> [ENV=val ...] -- <launcher args...>
run_claude() {
  local out="$1"; shift
  local -a envs=()
  while [[ "$1" != -- ]]; do envs+=("$1"); shift; done; shift
  : > "$out"
  (
    export TEST_STUB_FILE="$out" PATH="$BIN:/usr/bin:/bin" HOME="$TMP/home" CLAUDE_CONFIG_DIR="$CCONF"
    export HARNESS_SESSION_STATE_HOME="$STATE" "${envs[@]}"
    source "$LAUNCHER_DIR/bin/aliases.zsh"
    _harness_launcher_run "$HARNESS" "$@"
  ) </dev/null >/dev/null 2>"$out.err"
}
argv_of() { sed -n 's/^ARG://p' "$1"; }
count_arg() { argv_of "$1" | grep -Fxc -- "$2" || true; }
value_after() { argv_of "$1" | grep -Fx -A1 -- "$2" | sed -n 2p; }
has_arg() { argv_of "$1" | grep -Fxq -- "$2"; }
env_of() { sed -n "s/^ENV:$2=//p" "$1"; }
settings_json() { argv_of "$1" | grep -Fx -A1 -- --settings | sed -n 2p; }
json_has() { "$PY" -c 'import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if eval(sys.argv[2]) else 1)' "$1" "$2"; }

ID=0b5d1f3e-0000-4000-8000-000000000001
transcript() { # <id> <model> <effort> [extra jsonl line]
  {
    printf '{"type":"assistant","isSidechain":false,"message":{"model":"%s"},"effort":"%s"}\n' "$2" "$3"
    [[ -z "${4:-}" ]] || print -r -- "$4"
  } > "$CCONF/projects/-tmp-proj/$1.jsonl"
}
write_record() { # <id> <permission|""> [source_root] [isolated]
  local -a grant=()
  [[ -z "$2" ]] || grant=(--permission "$2")
  printf '{"hook_event_name":"SessionStart","source":"startup","session_id":"%s"}' "$1" \
    | env HARNESS_SESSION_STATE_HOME="$STATE" \
        "$PY" "$LAUNCHER_DIR/bin/harness-launch-record" claude --source-root "${3:-$HARNESS}" --isolated "${4:-0}" "${grant[@]}"
}
hook_command() { # <stub-file> : the SessionStart hook command in the launcher --settings
  "$PY" -c 'import json,sys; print(json.loads(sys.argv[1])["hooks"]["SessionStart"][0]["hooks"][0]["command"])' "$(settings_json "$1")"
}
# Process tables for the hook's ancestry check (test-only arguments appended to
# the launcher's hook command): a pane-shell launch, and a launch inside an agent.
cat > "$TMP/ps-clean" <<'PS'
1000 999 /bin/sh
998 997 claude
999 998 /bin/sh
997 996 -zsh
996 995 zsh
995 1 herdr
1 0 launchd
PS
cat > "$TMP/ps-nested" <<'PS'
1000 999 /bin/sh
999 998 /bin/sh
998 997 claude
997 996 /bin/zsh
996 900 /bin/zsh
900 899 claude
899 1 launchd
1 0 launchd
PS
run_hook() { # <command> <session-id> <source> [ENV=val ...]   (TABLE selects the process table)
  local cmd="$1" id="$2" src="$3"; shift 3
  printf '{"hook_event_name":"SessionStart","source":"%s","session_id":"%s"}' "$src" "$id" \
    | env HARNESS_SESSION_STATE_HOME="$STATE" "$@" /bin/sh -c "$cmd --pstable ${TABLE:-$TMP/ps-clean} --start-pid 1000" > "$TMP/hook.out"
  [[ ! -s "$TMP/hook.out" ]] || fail 'hook printed output (would become session context)'
}
record_of() { HARNESS_SESSION_STATE_HOME="$STATE" "$PY" "$LAUNCHER_DIR/bin/harness-launch-record" read claude "$1" | grep -Ev "^(source|agent_pid|agent_start|task_id|parent_native)="; }

# R1: herdr types `claude --resume <id>`; shell routing hands the launcher
# `base --passthrough --resume <id>` marked as a host default.
transcript $ID claude-opus-5-5 xhigh '{"type":"cost-state","modelUsage":{"claude-opus-5-5[1m]":{}}}'
OUT="$TMP/r1"
run_claude "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- base --passthrough --resume $ID
[[ -s "$OUT" ]] || fail 'R1 claude was not launched directly' "$OUT"
[[ "$(count_arg "$OUT" --model)" == 1 && "$(value_after "$OUT" --model)" == 'claude-opus-5-5[1m]' ]] || fail 'R1 model not restored (or base sonnet kept)' "$OUT"
[[ "$(count_arg "$OUT" --effort)" == 1 && "$(value_after "$OUT" --effort)" == xhigh ]] || fail 'R1 effort not restored' "$OUT"
[[ "$(value_after "$OUT" --resume)" == "$ID" ]] || fail 'R1 resume id lost' "$OUT"
has_arg "$OUT" sonnet && fail 'R1 host default sonnet survived' "$OUT"
json_has "$(settings_json "$OUT")" 'd.get("alwaysThinkingEnabled") is True' || fail 'R1 restored xhigh must enable thinking' "$OUT"
[[ "$(env_of "$OUT" HARNESS_HOST_DEFAULT_MODE)" == '<unset>' ]] || fail 'R1 host-default marker leaked into the agent' "$OUT"
echo 'PASS: R1 herdr restore keeps model, effort and enables thinking for xhigh'

# R2: the same argv without the marker is a user's explicit `base`.
OUT="$TMP/r2"
run_claude "$OUT" -- base --passthrough --resume $ID
[[ "$(value_after "$OUT" --model)" == sonnet && "$(value_after "$OUT" --effort)" == high ]] || fail 'R2 explicit base was overridden' "$OUT"
echo 'PASS: R2 explicit base is not overridden'

# R3: forms of a pure resume all restore; anything else is not a pure resume.
for form in "--resume=$ID" "-r $ID" "--resume $ID"; do
  run_claude "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- base --passthrough ${=form}
  [[ "$(value_after "$OUT" --model)" == claude-opus-5-5* ]] || fail "R3 '$form' not restored" "$OUT"
done
for form in "--resume $ID --model haiku" "--resume $ID --effort low" "--resume $ID --permission-mode plan" \
            "--resume $ID extra" "--resume 0b5d1f3e-0000-4000-8000-000000000002 --resume $ID" "--resume session-123" "--resume" "--continue"; do
  run_claude "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- base --passthrough ${=form}
  [[ "$(value_after "$OUT" --model)" != claude-opus-5-5* ]] || fail "R3 '$form' must not restore the transcript model" "$OUT"
done
run_claude "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- base --passthrough --resume $ID --model haiku
[[ "$(count_arg "$OUT" --model)" == 1 && "$(value_after "$OUT" --model)" == haiku ]] || fail 'R3 caller --model must win' "$OUT"
run_claude "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- base --passthrough --resume $ID --effort low
[[ "$(value_after "$OUT" --effort)" == low && "$(count_arg "$OUT" --effort)" == 1 ]] || fail 'R3 caller --effort must win' "$OUT"
# the marker only covers a leading `base`: `rich` is an explicit choice
run_claude "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- rich --passthrough --resume $ID
[[ "$(value_after "$OUT" --model)" == 'opus[1m]' ]] || fail 'R3 explicit rich must stand' "$OUT"
echo 'PASS: R3 only a pure single-UUID resume restores; caller flags win'

# R4: Orca / `<prefix> --resume <id>` (no keyword) launches directly and restores.
run_claude "$OUT" -- --resume $ID
[[ -s "$OUT" ]] || fail 'R4 `--resume <id>` did not launch directly (id dropped into the TUI?)' "$OUT"
[[ "$(value_after "$OUT" --resume)" == "$ID" ]] || fail 'R4 resume id lost' "$OUT"
[[ "$(value_after "$OUT" --model)" == 'claude-opus-5-5[1m]' && "$(value_after "$OUT" --effort)" == xhigh ]] || fail 'R4 model/effort not restored' "$OUT"
json_has "$(settings_json "$OUT")" 'd.get("alwaysThinkingEnabled") is True' || fail 'R4 xhigh must enable thinking' "$OUT"
# Orca with keyword defaults keeps them.
run_claude "$OUT" -- rich --resume $ID
[[ "$(value_after "$OUT" --model)" == 'opus[1m]' && "$(count_arg "$OUT" --model)" == 1 ]] || fail 'R4 Orca keyword default was overridden' "$OUT"
run_claude "$OUT" -- opus bypass --resume $ID
[[ "$(value_after "$OUT" --effort)" == high ]] || fail 'R4 explicit opus effort overridden' "$OUT"
echo 'PASS: R4 Orca restore with and without keyword defaults; `<prefix> --resume <id>` no longer opens the TUI'

# R5: a session with no transcript keeps launcher defaults and still launches.
run_claude "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- base --passthrough --resume 0b5d1f3e-0000-4000-8000-0000000000ff
[[ "$(value_after "$OUT" --model)" == sonnet && "$(value_after "$OUT" --effort)" == high ]] || fail 'R5 defaults lost without a transcript' "$OUT"
echo 'PASS: R5 no transcript keeps defaults'

# R6: gateway / synthetic / malformed models are never passed to a direct launch.
transcript $ID glm-4.6 high
run_claude "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- base --passthrough --resume $ID
[[ "$(value_after "$OUT" --model)" == sonnet ]] || fail 'R6 gateway model reached a direct launch' "$OUT"
transcript $ID '-claude-evil' high
run_claude "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- base --passthrough --resume $ID
[[ "$(value_after "$OUT" --model)" == sonnet ]] || fail 'R6 leading-dash model reached the launch' "$OUT"
echo 'PASS: R6 gateway and malformed models are skipped'

# R7: zsh re-validates helper output. A stand-in helper prints hostile values.
FAKEBIN="$TMP/fakebin"; mkdir -p "$FAKEBIN"; cp "$LAUNCHER_DIR"/bin/* "$FAKEBIN/" 2>/dev/null || true
cat > "$FAKEBIN/harness-restore-probe" <<'FAKE'
#!/usr/bin/env python3
print("model=--dangerously-skip-permissions")
print("effort=turbo; rm -rf /")
print("model=claude-ok-1 --model evil")
FAKE
transcript $ID claude-opus-5-5 high
(
  export TEST_STUB_FILE="$TMP/r7" PATH="$BIN:/usr/bin:/bin" HOME="$TMP/home" CLAUDE_CONFIG_DIR="$CCONF" HARNESS_SESSION_STATE_HOME="$STATE" HARNESS_HOST_DEFAULT_MODE=base
  source "$LAUNCHER_DIR/bin/aliases.zsh"; _HARNESS_LAUNCHER_BIN="$FAKEBIN"
  _harness_launcher_run "$HARNESS" base --passthrough --resume $ID
) </dev/null >/dev/null 2>"$TMP/r7.err"
[[ "$(value_after "$TMP/r7" --model)" == sonnet && "$(value_after "$TMP/r7" --effort)" == high ]] || fail 'R7 hostile probe output was used' "$TMP/r7"
has_arg "$TMP/r7" --dangerously-skip-permissions && fail 'R7 injected a flag' "$TMP/r7"
echo 'PASS: R7 hostile helper output is re-validated and ignored'

# R8: permission comes only from the launch record, never from the transcript.
transcript $ID claude-opus-5-5 xhigh '{"type":"permission-mode","permissionMode":"bypassPermissions"}'
sed -i.bak 's/"effort":"xhigh"}/"effort":"xhigh","permissionMode":"bypassPermissions"}/' "$CCONF/projects/-tmp-proj/$ID.jsonl"
run_claude "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- base --passthrough --resume $ID
has_arg "$OUT" --permission-mode && fail 'R8 forged transcript permissionMode escalated' "$OUT"
has_arg "$OUT" bypassPermissions && fail 'R8 forged transcript permissionMode escalated' "$OUT"
[[ "$(grep -c 'relaunch' "$OUT.err")" == 1 ]] || fail 'R8 expected exactly one relaunch hint on stderr' "$OUT"
grep -Fq "test rich bypass --passthrough --resume $ID" "$OUT.err" || fail 'R8 relaunch hint must be the exact command' "$OUT"
write_record $ID bypassPermissions
run_claude "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- base --passthrough --resume $ID
[[ "$(value_after "$OUT" --permission-mode)" == bypassPermissions && "$(count_arg "$OUT" --permission-mode)" == 1 ]] || fail 'R8 recorded grant not reapplied' "$OUT"
! grep -Fq 'relaunch' "$OUT.err" || fail 'R8 hint printed although a record exists' "$OUT"
write_record $ID acceptEdits
run_claude "$OUT" -- --resume $ID
[[ "$(value_after "$OUT" --permission-mode)" == acceptEdits ]] || fail 'R8 recorded (narrower) grant not reapplied' "$OUT"
# a record without a permission means the user chose the default mode: restore it
# quietly (the hint is only for sessions with no record at all)
write_record $ID ""
run_claude "$OUT" -- --resume $ID
has_arg "$OUT" --permission-mode && fail 'R8 a record with no grant must not add a permission mode' "$OUT"
! grep -Fq 'relaunch' "$OUT.err" || fail 'a record without a grant means default mode; the hint must stay quiet' "$OUT"
# a record for another harness root is not this session's grant
write_record $ID bypassPermissions /somewhere/else
run_claude "$OUT" -- --resume $ID
has_arg "$OUT" --permission-mode && fail 'R8 record of another harness applied' "$OUT"
# a caller permission mode wins over (and prevents) restore
write_record $ID bypassPermissions
run_claude "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- base --passthrough --resume $ID --permission-mode plan
[[ "$(value_after "$OUT" --permission-mode)" == plan && "$(count_arg "$OUT" --permission-mode)" == 1 ]] || fail 'R8 caller permission mode must win' "$OUT"
echo 'PASS: R8 grant only from the launch record; one exact relaunch hint otherwise'

# R9: a fresh launch injects the launch-record hook, merged into the launcher's
# single --settings. Grant, root and isolation are ARGUMENTS of the hook command
# (M2), never the environment: Claude applies settings.local.json's env block.
rm -rf "$STATE/launch-records"
run_claude "$OUT" -- rich bypass
[[ "$(env_of "$OUT" HARNESS_LAUNCH_PERMISSION)" == '<unset>' ]] || fail 'M2 the Claude grant must not travel through the environment' "$OUT"
[[ "$(count_arg "$OUT" --settings)" == 1 ]] || fail 'R9 launcher settings must be one merged --settings' "$OUT"
S="$(settings_json "$OUT")"
json_has "$S" 'd.get("alwaysThinkingEnabled") is True' || fail 'R9 thinking setting lost in the merge' "$OUT"
json_has "$S" 'd["hooks"]["SessionStart"][0]["hooks"][0]["type"] == "command"' || fail 'R9 SessionStart hook missing' "$OUT"
HOOK="$(hook_command "$OUT")"
[[ "$HOOK" == *harness-launch-record* && "$HOOK" == *"--permission bypassPermissions"* && "$HOOK" == *"--source-root $HARNESS"* \
   && "$HOOK" == *"--isolated 0"* && "$HOOK" != *--nested* ]] || fail "M2 hook command lacks its launch arguments: $HOOK" "$OUT"
NEWID=0b5d1f3e-0000-4000-8000-0000000000aa
run_hook "$HOOK" $NEWID startup
[[ "$(record_of $NEWID)" == $'permission=bypassPermissions\nsource_root='"$HARNESS"$'\nisolated=0\ncontext=1m' ]] || fail 'R9 hook did not write the record (rich = opus[1m])'
# M2: a forged environment (settings.local.json env block) cannot add or change a grant
run_claude "$OUT" -- base
HOOK="$(hook_command "$OUT")"
[[ "$HOOK" != *--permission* ]] || fail 'R9 a launch without a grant must pass none' "$OUT"
json_has "$(settings_json "$OUT")" '"alwaysThinkingEnabled" not in d and "SessionStart" in d["hooks"]' || fail 'R9 hook-only settings expected' "$OUT"
FORGED=0b5d1f3e-0000-4000-8000-0000000000ab
run_hook "$HOOK" $FORGED startup HARNESS_LAUNCH_PERMISSION=bypassPermissions HARNESS_LAUNCH_SOURCE_ROOT=/evil HARNESS_LAUNCH_ISOLATED=1
[[ "$(record_of $FORGED)" == $'source_root='"$HARNESS"$'\nisolated=0' ]] || fail "M2 forged HARNESS_LAUNCH_* env changed the record: $(record_of $FORGED)"
# a caller permission mode is what actually launched
run_claude "$OUT" -- base --passthrough --permission-mode plan
[[ "$(hook_command "$OUT")" == *"--permission plan"* ]] || fail 'R9 caller permission mode not in the hook command' "$OUT"
# a restore re-records the grant it reapplied
write_record $ID bypassPermissions
run_claude "$OUT" -- --resume $ID
[[ "$(hook_command "$OUT")" == *"--permission bypassPermissions"* ]] || fail 'R9 restored grant not in the hook command' "$OUT"
echo 'PASS: R9 the record hook carries the launch grant as arguments, not environment'

# R10 (M1): a launch inside an agent is nested and can never raise a record. The
# launcher no longer guesses from the environment: the hook decides from process
# ancestry, so a stale agent environment does not make a pane launch nested and
# `env -u CLAUDECODE` does not make a nested one top-level.
NEST=0b5d1f3e-0000-4000-8000-0000000000b1
run_claude "$OUT" -- base --passthrough --permission-mode plan
run_hook "$(hook_command "$OUT")" $NEST startup
[[ "$(record_of $NEST)" == *permission=plan* ]] || fail 'R10 setup: plan record missing'
for nested_env in CLAUDECODE=1 CODEX_THREAD_ID=t-1 HARNESS_LAUNCH_SOURCE_ROOT=/parent HARNESS_LAUNCH_BYPASS=1 HARMLESS=1; do
  run_claude "$OUT" "$nested_env" -- rich bypass
  HOOK="$(hook_command "$OUT")"
  [[ "$HOOK" != *--nested* ]] || fail "M1 the hook command must not carry an environment-derived nesting flag ($nested_env): $HOOK" "$OUT"
  # top-level ancestry: whatever the environment says, the launch may raise
  TABLE="$TMP/ps-clean" run_hook "$HOOK" $NEST resume
  [[ "$(record_of $NEST)" == *permission=bypassPermissions* ]] || fail "M1 a pane launch with a stale $nested_env must stay top-level: $(record_of $NEST)"
  write_record $NEST plan
  # nested ancestry: whatever the environment says (even nothing), it cannot raise
  TABLE="$TMP/ps-nested" run_hook "$HOOK" $NEST resume
  [[ "$(record_of $NEST)" == *permission=plan* && "$(record_of $NEST)" != *bypass* ]] || fail "M1 nested launch ($nested_env) raised the record: $(record_of $NEST)"
done
# nested and no record: created without a grant
TABLE="$TMP/ps-nested" run_hook "$HOOK" 0b5d1f3e-0000-4000-8000-0000000000b2 startup
[[ "$(record_of 0b5d1f3e-0000-4000-8000-0000000000b2)" == $'source_root='"$HARNESS"$'\nisolated=0\ncontext=1m' ]] || fail 'M1 nested launch created a record with a grant (context=1m is not a grant)'
# the herdr path (a pane shell with no agent ancestor) is top-level and restores
run_claude "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- base --passthrough --resume $NEST
[[ "$(value_after "$OUT" --permission-mode)" == plan ]] || fail 'M1 a herdr restore (no agent env) must reapply the recorded grant' "$OUT"
# the user's explicit relaunch from the hint is top-level and records the grant
run_claude "$OUT" -- rich bypass --passthrough --resume $NEST
HOOK="$(hook_command "$OUT")"
[[ "$HOOK" == *"--permission bypassPermissions"* ]] || fail "M1 relaunch from the hint lost its grant: $HOOK" "$OUT"
TABLE="$TMP/ps-clean" run_hook "$HOOK" $NEST resume
[[ "$(record_of $NEST)" == *permission=bypassPermissions* ]] || fail 'M1 top-level relaunch did not record the grant'
echo 'PASS: R10 nested launches never raise a record; top-level restore and relaunch still work'

# R11: 1M context survives a restore. The launcher records `--context 1m` in the
# hook command when it launched with a [1m] model; a restore appends [1m] to a
# transcript model that has no suffix (a long session can have no cost-state left).
rm -rf "$STATE/launch-records"
run_claude "$OUT" -- rich
[[ "$(hook_command "$OUT")" == *"--context 1m"* ]] || fail 'R11 an opus[1m] launch must put --context 1m on the hook command' "$OUT"
run_claude "$OUT" -- opus bypass
[[ "$(hook_command "$OUT")" == *"--context 1m"* ]] || fail 'R11 opus[1m] (opus mode) must record 1m' "$OUT"
run_claude "$OUT" -- base
[[ "$(hook_command "$OUT")" != *--context* ]] || fail 'R11 a sonnet launch must not record 1m' "$OUT"
run_claude "$OUT" -- rich --passthrough --model sonnet
[[ "$(hook_command "$OUT")" != *--context* ]] || fail 'R11 the final --model (caller sonnet) decides the context' "$OUT"
run_claude "$OUT" -- base --passthrough --model 'claude-opus-5-5[1m]'
[[ "$(hook_command "$OUT")" == *"--context 1m"* ]] || fail 'R11 a caller --model ending [1m] must record 1m' "$OUT"
run_claude "$OUT" -- base --passthrough '--model=claude-opus-5-5[1m]'
[[ "$(hook_command "$OUT")" == *"--context 1m"* ]] || fail 'R11 --model=<m>[1m] must record 1m' "$OUT"
CTXID=0b5d1f3e-0000-4000-8000-0000000000c1
transcript $CTXID claude-opus-5-5 xhigh
write_context_record() { # <id> [context]
  local -a ctx=(); [[ -z "${2:-}" ]] || ctx=(--context "$2")
  printf '{"hook_event_name":"SessionStart","source":"startup","session_id":"%s"}' "$1" \
    | env HARNESS_SESSION_STATE_HOME="$STATE" "$PY" "$LAUNCHER_DIR/bin/harness-launch-record" claude --source-root "$HARNESS" --isolated 0 "${ctx[@]}"
}
write_context_record $CTXID 1m
run_claude "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- base --passthrough --resume $CTXID
[[ "$(value_after "$OUT" --model)" == 'claude-opus-5-5[1m]' && "$(count_arg "$OUT" --model)" == 1 ]] || fail 'R11 record context=1m must restore [1m]' "$OUT"
run_claude "$OUT" -- --resume $CTXID
[[ "$(value_after "$OUT" --model)" == 'claude-opus-5-5[1m]' ]] || fail 'R11 non-passthrough restore must restore [1m]' "$OUT"
write_context_record $CTXID
run_claude "$OUT" -- --resume $CTXID
[[ "$(value_after "$OUT" --model)" == claude-opus-5-5 ]] || fail 'R11 a record without context must not add [1m]' "$OUT"
# the transcript already says [1m]: not doubled
transcript $CTXID claude-opus-5-5 xhigh '{"type":"cost-state","modelUsage":{"claude-opus-5-5[1m]":{}}}'
write_context_record $CTXID 1m
run_claude "$OUT" -- --resume $CTXID
[[ "$(value_after "$OUT" --model)" == 'claude-opus-5-5[1m]' ]] || fail 'R11 [1m] doubled or lost' "$OUT"
# a record context never invents a model
rm -f "$CCONF/projects/-tmp-proj/$CTXID.jsonl"
run_claude "$OUT" -- --resume $CTXID
has_arg "$OUT" --model && fail 'R11 a record context must not invent a model without a transcript' "$OUT"
echo 'PASS: R11 1M context is recorded by the launcher and restored'

echo 'PASS: Claude restore fidelity'
