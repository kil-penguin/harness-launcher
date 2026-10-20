#!/usr/bin/env zsh
# test-restore-fidelity-codex.sh — a host restore of a Codex session
# (`codex resume <id>`) keeps the session's model, effort and context (read from
# its rollout) and its approval/sandbox grant (only from the launcher-owned
# launch record). A fake Codex binary records exactly what `resume` receives.
set -e
unset HARNESS_TERMINAL_RUNTIME TERM_PROGRAM HARNESS_HOST_DEFAULT_MODE HARNESS_CODEX_CONTEXT CLAUDECODE CODEX_THREAD_ID CODEX_SANDBOX HARNESS_LAUNCH_PERMISSION HARNESS_LAUNCH_APPROVAL HARNESS_LAUNCH_SANDBOX HARNESS_LAUNCH_BYPASS HARNESS_LAUNCH_SOURCE_ROOT HARNESS_LAUNCH_ISOLATED HARNESS_LAUNCH_PROFILE HARNESS_LAUNCH_NESTED; unset -m 'HERDR_*' 'ORCA_*' 'CMUX_*' || true

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCHER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP="$(mktemp -d)"; TMP="${TMP:A}"
trap 'rm -rf "$TMP"' EXIT
HARNESS="$TMP/harness"
BIN="$TMP/bin"
STATE="$TMP/state"
mkdir -p "$HARNESS/config" "$BIN" "$HARNESS/.harness/codex/sessions/2026/09/30"
print -r -- 'HARNESS_NAME="test"' 'HARNESS_PREFIX="test"' > "$HARNESS/config/launcher.env"
PY="$(source "$LAUNCHER_DIR/bin/harness-common.sh"; harness_python3_resolve)"
REAL_CODEX="$(command -v codex 2>/dev/null || true)"
cp "$LAUNCHER_DIR/bin/harness-restore-probe" "$LAUNCHER_DIR/bin/harness-launch-record" "$LAUNCHER_DIR/bin/harness_launch_plan.py" "$BIN/"

cat > "$BIN/codex" <<'STUB'
#!/usr/bin/env bash
{
  for arg in "$@"; do printf 'ARG:%s\n' "$arg"; done
  for v in HARNESS_HOST_DEFAULT_MODE HARNESS_LAUNCH_APPROVAL HARNESS_LAUNCH_SANDBOX HARNESS_LAUNCH_NETWORK HARNESS_LAUNCH_BYPASS HARNESS_LAUNCH_SOURCE_ROOT HARNESS_LAUNCH_ISOLATED HARNESS_LAUNCH_PROFILE HARNESS_LAUNCH_NESTED HARNESS_CODEX_CONTEXT; do
    printf 'ENV:%s=%s\n' "$v" "${!v-<unset>}"
  done
} > "$TEST_STUB_FILE"
STUB
cat > "$BIN/codex-home-prepare.sh" <<'STUB'
#!/usr/bin/env bash
mkdir -p "$1/.harness/codex"
printf '%s\n' "${HARNESS_CODEX_CONTEXT-<unset>}" > "$TEST_STUB_FILE.context"
STUB
chmod +x "$BIN/codex" "$BIN/codex-home-prepare.sh"
# the generated profile registry a real prepare leaves behind
for name in fast base sol astra plan rich; do : > "$HARNESS/.harness/codex/$name.config.toml"; done

fail() { echo "FAIL: $1" >&2; [[ -f "${2:-}" ]] && sed 's/^/  /' "$2" >&2; [[ -f "${2:-}.err" ]] && sed 's/^/  err: /' "$2.err" >&2; exit 1; }

# run_codex <stub-file> [ENV=val ...] -- <args after `codex`...>
run_codex() {
  local out="$1"; shift
  local -a envs=()
  while [[ "$1" != -- ]]; do envs+=("$1"); shift; done; shift
  : > "$out"; rm -f "$out.context"
  (
    export TEST_STUB_FILE="$out" PATH="$BIN:/usr/bin:/bin" HOME="$TMP/home" HARNESS_CODEX_BIN="$BIN/codex"
    export HARNESS_SESSION_STATE_HOME="$STATE" HARNESS_PROFILE_HOME="$TMP/profiles" "${envs[@]}"
    unset HARNESS_CODEX_MCP_PROFILE HARNESS_SESSION_ISOLATION
    source "$LAUNCHER_DIR/bin/aliases.zsh"
    _HARNESS_LAUNCHER_BIN="${FAKE_BIN:-$BIN}"
    _harness_launcher_run "$HARNESS" codex "$@"
  ) </dev/null >/dev/null 2>"$out.err"
}
argv_of() { sed -n 's/^ARG://p' "$1"; }
count_arg() { argv_of "$1" | grep -Fxc -- "$2" || true; }
has_arg() { argv_of "$1" | grep -Fxq -- "$2"; }
value_after() { argv_of "$1" | grep -Fx -A1 -- "$2" | sed -n 2p; }
env_of() { sed -n "s/^ENV:$2=//p" "$1"; }
index_of() { argv_of "$1" | grep -nFx -- "$2" | head -1 | cut -d: -f1; }
argv_line() { argv_of "$1" | tr '\n' ' '; }

ID=01a0ef50-1bec-7852-afb3-a6b6559f47b6
ROLL="$HARNESS/.harness/codex/sessions/2026/09/30/rollout-2026-09-30T00-00-00-$ID.jsonl"
rollout() { # <model> <effort> [extra jsonl line]
  {
    printf '{"type":"turn_context","payload":{"model":"%s","effort":"%s","approval_policy":"never","sandbox_policy":{"type":"danger-full-access"}}}\n' "$1" "$2"
    [[ -z "${3:-}" ]] || print -r -- "$3"
  } > "$ROLL"
}
write_record() { # <id> [ENV=val ...]
  local id="$1"; shift
  printf '{"hook_event_name":"SessionStart","source":"startup","session_id":"%s"}' "$id" \
    | env HARNESS_SESSION_STATE_HOME="$STATE" HARNESS_LAUNCH_SOURCE_ROOT="$HARNESS" HARNESS_LAUNCH_ISOLATED=0 "$@" \
        "$PY" "$LAUNCHER_DIR/bin/harness-launch-record" codex
}
MODEL_ARGS_RE='^--cd .* -p base -m gpt-6\.1-sol -c model_reasoning_effort="medium" '

# X1: herdr types `codex resume <id>` -> `codex --passthrough resume <id>`.
rollout gpt-6.1-sol medium
OUT="$TMP/x1"
run_codex "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- --passthrough resume $ID
[[ -s "$OUT" ]] || fail 'X1 codex was not launched' "$OUT"
[[ "$(argv_line "$OUT")" =~ $MODEL_ARGS_RE ]] || fail "X1 model/effort not passed after the profile flags: $(argv_line "$OUT")" "$OUT"
[[ "$(argv_line "$OUT")" == *"resume $ID " ]] || fail 'X1 resume <id> not last' "$OUT"
[[ "$(env_of "$OUT" HARNESS_HOST_DEFAULT_MODE)" == '<unset>' ]] || fail 'X1 host-default marker leaked into the agent' "$OUT"
echo 'PASS: X1 herdr restore passes -m and -c after the profile flags'

# X2: Orca without keyword defaults (`codex resume <id>`, subcommand first).
run_codex "$OUT" -- resume $ID
line="$(argv_line "$OUT")"
[[ "$line" == "resume --cd "*" -p base -m gpt-6.1-sol -c model_reasoning_effort=\"medium\" $ID " ]] || fail "X2 unexpected argv: $line" "$OUT"
echo 'PASS: X2 `resume <id>` restores model and effort'

# X3: anything explicit is not restored.
for words in "sol --passthrough resume $ID" "fast resume $ID" "--passthrough resume $ID -m gpt-5.6-terra" "--passthrough resume $ID -p fast" \
             "--passthrough resume $ID --profile=fast" "--passthrough resume $ID -a on-request" "--passthrough resume $ID -s read-only" \
             "--passthrough resume $ID --full-auto" "--passthrough resume $ID --dangerously-bypass-approvals-and-sandbox" \
             "never resume $ID" "bypass resume $ID" "full-auto resume $ID" "--passthrough resume --last" "--passthrough resume" \
             "--passthrough resume 01a0ef50-1bec-7852-afb3-a6b6559f47b7 $ID" "--passthrough resume not-a-uuid" "resume $ID extra"; do
  run_codex "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- ${=words}
  has_arg "$OUT" gpt-6.1-sol && fail "X3 '$words' must not restore" "$OUT"
  has_arg "$OUT" model_reasoning_effort=\"medium\" && fail "X3 '$words' must not restore effort" "$OUT"
done
echo 'PASS: X3 explicit keywords, caller flags and non-pure resumes are not restored'

# X4: a caller -m still wins (single -m, the caller's).
run_codex "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- --passthrough resume $ID -m gpt-5.6-terra
[[ "$(count_arg "$OUT" -m)" == 1 && "$(value_after "$OUT" -m)" == gpt-5.6-terra ]] || fail 'X4 caller -m must win' "$OUT"
echo 'PASS: X4 caller -m wins'

# X5: no rollout keeps defaults, still resumes.
rm -f "$ROLL"
run_codex "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- --passthrough resume $ID
[[ "$(argv_line "$OUT")" == *" -p base resume $ID " ]] || fail 'X5 defaults changed without a rollout' "$OUT"
has_arg "$OUT" -m && fail 'X5 unexpected -m' "$OUT"
echo 'PASS: X5 no rollout keeps defaults'

# X6: malformed model / hostile helper output.
rollout '-bad' minimal
run_codex "$OUT" -- resume $ID
has_arg "$OUT" -bad && fail 'X6 leading-dash model reached codex' "$OUT"
has_arg "$OUT" -m && fail 'X6 -m passed for a rejected model' "$OUT"
has_arg "$OUT" 'model_reasoning_effort="minimal"' || fail 'X6 valid codex-only effort dropped' "$OUT"
FAKE_BIN="$TMP/fakebin"; mkdir -p "$FAKE_BIN"; cp "$BIN"/* "$FAKE_BIN/"
cat > "$FAKE_BIN/harness-restore-probe" <<'FAKE'
#!/usr/bin/env python3
print("model=--dangerously-bypass-approvals-and-sandbox")
print("model=gpt-6 -a never")
print('effort=medium"; approval_policy="never')
print("context=9m")
FAKE
rollout gpt-6.1-sol medium
run_codex "$OUT" -- resume $ID
[[ "$(argv_line "$OUT")" == "resume --cd "*" -p base $ID " ]] || fail "X6 hostile helper output was used: $(argv_line "$OUT")" "$OUT"
unset FAKE_BIN
echo 'PASS: X6 malformed and hostile values are re-validated away'

# X7: grant only from the launch record. A forged rollout does not escalate.
rollout gpt-6.1-sol medium
run_codex "$OUT" -- resume $ID
has_arg "$OUT" -a && fail 'X7 rollout approval_policy escalated' "$OUT"
has_arg "$OUT" -s && fail 'X7 rollout sandbox escalated' "$OUT"
has_arg "$OUT" --dangerously-bypass-approvals-and-sandbox && fail 'X7 rollout escalated to bypass' "$OUT"
[[ "$(grep -c 'relaunch' "$OUT.err")" == 1 ]] || fail 'X7 expected exactly one relaunch hint' "$OUT"
grep -Fq "test codex sol bypass --passthrough resume $ID" "$OUT.err" || fail 'X7 relaunch hint must be the exact command' "$OUT"
write_record $ID HARNESS_LAUNCH_APPROVAL=never HARNESS_LAUNCH_SANDBOX=danger-full-access
run_codex "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- --passthrough resume $ID
[[ "$(argv_line "$OUT")" == *"-p base -m gpt-6.1-sol "*"-a never -s danger-full-access resume $ID "* ]] || fail "X7 recorded grant not reapplied: $(argv_line "$OUT")" "$OUT"
! grep -Fq relaunch "$OUT.err" || fail 'X7 hint printed although a record exists' "$OUT"
[[ "$(env_of "$OUT" HARNESS_LAUNCH_APPROVAL)" == never && "$(env_of "$OUT" HARNESS_LAUNCH_SANDBOX)" == danger-full-access ]] || fail 'X7 restored grant not re-exported' "$OUT"
write_record $ID HARNESS_LAUNCH_BYPASS=1
run_codex "$OUT" -- resume $ID
[[ "$(count_arg "$OUT" --dangerously-bypass-approvals-and-sandbox)" == 1 ]] || fail 'X7 recorded bypass not reapplied' "$OUT"
has_arg "$OUT" -a && fail 'X7 bypass must not add -a' "$OUT"
# a record with no grant and no profile means the user chose defaults: quiet
write_record $ID
run_codex "$OUT" -- resume $ID
has_arg "$OUT" -a && fail 'X7 a record with no grant must not add -a' "$OUT"
! grep -Fq relaunch "$OUT.err" || fail 'a record without a grant means default mode; the hint must stay quiet' "$OUT"
# a record for another harness root is not this session's grant
write_record $ID HARNESS_LAUNCH_BYPASS=1 HARNESS_LAUNCH_SOURCE_ROOT=/somewhere/else
run_codex "$OUT" -- resume $ID
has_arg "$OUT" --dangerously-bypass-approvals-and-sandbox && fail 'X7 record of another harness applied' "$OUT"
echo 'PASS: X7 grant only from the launch record; one exact relaunch hint otherwise'

# X8: context mode restored unless a keyword already chose one.
rollout gpt-6.1-sol medium '{"type":"event_msg","payload":{"info":{"model_context_window":950000}}}'
run_codex "$OUT" -- resume $ID
[[ "$(cat "$OUT.context")" == 272k ]] || fail "X8 legacy fallback not restored: $(cat "$OUT.context")" "$OUT"
run_codex "$OUT" -- 272k resume $ID
[[ "$(cat "$OUT.context")" == 272k ]] || fail 'X8 explicit context keyword must win' "$OUT"
for mode in auto 272k 500k 1m; do
  write_record $ID HARNESS_LAUNCH_CONTEXT=$mode
  run_codex "$OUT" -- resume $ID
  [[ "$(cat "$OUT.context")" == "$mode" ]] || fail "X8 recorded $mode not restored" "$OUT"
done
echo 'PASS: X8 context mode restored'

# X9: a fresh launch exports the grant it launched with (flags only).
rm -rf "$STATE/launch-records"
run_codex "$OUT" -- sol bypass
[[ "$(env_of "$OUT" HARNESS_LAUNCH_BYPASS)" == 1 && "$(env_of "$OUT" HARNESS_LAUNCH_APPROVAL)" == '<unset>' ]] || fail 'X9 bypass not exported' "$OUT"
[[ "$(env_of "$OUT" HARNESS_LAUNCH_SOURCE_ROOT)" == "$HARNESS" && "$(env_of "$OUT" HARNESS_LAUNCH_ISOLATED)" == 0 ]] || fail 'X9 source root/isolated not exported' "$OUT"
run_codex "$OUT" -- never
[[ "$(env_of "$OUT" HARNESS_LAUNCH_APPROVAL)" == never && "$(env_of "$OUT" HARNESS_LAUNCH_BYPASS)" == '<unset>' ]] || fail 'X9 never not exported' "$OUT"
run_codex "$OUT" -- full-auto
[[ "$(env_of "$OUT" HARNESS_LAUNCH_APPROVAL)" == on-request && "$(env_of "$OUT" HARNESS_LAUNCH_SANDBOX)" == workspace-write ]] || fail 'X9 full-auto not exported' "$OUT"
run_codex "$OUT" -- --passthrough -a untrusted --sandbox=read-only
[[ "$(env_of "$OUT" HARNESS_LAUNCH_APPROVAL)" == untrusted && "$(env_of "$OUT" HARNESS_LAUNCH_SANDBOX)" == read-only ]] || fail 'X9 caller flags not exported' "$OUT"
run_codex "$OUT" -- base
[[ "$(env_of "$OUT" HARNESS_LAUNCH_APPROVAL)$(env_of "$OUT" HARNESS_LAUNCH_SANDBOX)$(env_of "$OUT" HARNESS_LAUNCH_BYPASS)" == '<unset><unset><unset>' ]] || fail 'X9 exported a grant that was not launched with' "$OUT"
run_codex "$OUT" HARNESS_LAUNCH_BYPASS=1 HARNESS_LAUNCH_APPROVAL=never -- base
[[ "$(env_of "$OUT" HARNESS_LAUNCH_BYPASS)$(env_of "$OUT" HARNESS_LAUNCH_APPROVAL)" == '<unset><unset>' ]] || fail 'X9 inherited grant leaked into a new launch' "$OUT"
echo 'PASS: X9 launch grant exported from launcher flags; inherited grants dropped'

# X10: the real Codex CLI accepts the restored flags in both argv shapes the
# launcher builds (root flags before `resume`, and flags after the subcommand).
if [[ -n "$REAL_CODEX" ]]; then
  "$REAL_CODEX" -p base -m gpt-6.1-sol -c 'model_reasoning_effort="medium"' -a never -s danger-full-access resume --help >/dev/null 2>&1 \
    || fail 'X10 real codex rejected the passthrough-shaped restore flags'
  "$REAL_CODEX" resume -p base -m gpt-6.1-sol -c 'model_reasoning_effort="medium"' -a never -s danger-full-access --help >/dev/null 2>&1 \
    || fail 'X10 real codex rejected the subcommand-shaped restore flags'
  echo "PASS: X10 real codex CLI ($("$REAL_CODEX" --version 2>/dev/null)) accepts the restored flags"
else
  echo 'SKIP: X10 real codex CLI not installed'
fi

# X11 (M3): the launched profile is recorded and reapplied.
rollout gpt-6.1-sol high
write_record $ID HARNESS_LAUNCH_PROFILE=plan
run_codex "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- --passthrough resume $ID
line="$(argv_line "$OUT")"
[[ "$line" == *" -p plan -m gpt-6.1-sol -c model_reasoning_effort=\"high\" resume $ID " ]] || fail "M3 recorded profile plan not reapplied before model/effort: $line" "$OUT"
[[ "$line" != *" -p base "* ]] || fail 'M3 a plan session was restored on base' "$OUT"
! grep -Fq relaunch "$OUT.err" || fail 'M3 hint printed although the profile was restored' "$OUT"
run_codex "$OUT" -- resume $ID
[[ "$(argv_line "$OUT")" == "resume --cd "*" -p plan -m gpt-6.1-sol "* ]] || fail 'M3 subcommand-first restore lost the recorded profile' "$OUT"
# a profile that left the registry falls back to base plus the hint
mv "$HARNESS/.harness/codex/plan.config.toml" "$TMP/plan.bak"
run_codex "$OUT" -- --passthrough resume $ID
[[ "$(argv_line "$OUT")" == *" -p base "* && "$(argv_line "$OUT")" != *" -p plan "* ]] || fail 'M3 vanished profile must fall back to base' "$OUT"
[[ "$(grep -c relaunch "$OUT.err")" == 1 ]] || fail 'M3 vanished profile must print the hint' "$OUT"
mv "$TMP/plan.bak" "$HARNESS/.harness/codex/plan.config.toml"
# a symlinked registry entry is not a profile
mv "$HARNESS/.harness/codex/plan.config.toml" "$TMP/plan.bak"; ln -s "$TMP/plan.bak" "$HARNESS/.harness/codex/plan.config.toml"
run_codex "$OUT" -- --passthrough resume $ID
[[ "$(argv_line "$OUT")" == *" -p base "* ]] || fail 'M3 symlinked profile must not be used' "$OUT"
find "$HARNESS/.harness/codex/plan.config.toml" -delete; mv "$TMP/plan.bak" "$HARNESS/.harness/codex/plan.config.toml"
# an unknown recorded profile is dropped
write_record $ID HARNESS_LAUNCH_PROFILE=../../evil
run_codex "$OUT" -- --passthrough resume $ID
[[ "$(argv_line "$OUT")" == *" -p base "* ]] || fail 'M3 unknown profile applied' "$OUT"
# the launched profile is exported for the hook
run_codex "$OUT" -- sol bypass
[[ "$(env_of "$OUT" HARNESS_LAUNCH_PROFILE)" == sol ]] || fail 'M3 launched profile not exported' "$OUT"
run_codex "$OUT" -- base
[[ "$(env_of "$OUT" HARNESS_LAUNCH_PROFILE)" == base ]] || fail 'M3 default profile not exported' "$OUT"
run_codex "$OUT" -- --passthrough -p fast
[[ "$(env_of "$OUT" HARNESS_LAUNCH_PROFILE)" == '<unset>' ]] || fail 'M3 a caller profile is not the launcher profile' "$OUT"
echo 'PASS: X11 launched profile recorded and reapplied while it exists'

# X12 (M1): nesting is decided by the hook from process ancestry, so the launcher
# neither exports a nesting flag nor lets a stale agent environment change anything.
for nested_env in NONE=1 CODEX_THREAD_ID=t-1 CLAUDECODE=1 HARNESS_LAUNCH_BYPASS=1; do
  run_codex "$OUT" "$nested_env" -- sol
  [[ "$(env_of "$OUT" HARNESS_LAUNCH_NESTED)" == '<unset>' ]] || fail "M1 launcher exported an environment-derived nesting flag ($nested_env)" "$OUT"
done
# the herdr path (no agent env) is top-level and restores the recorded grant
write_record $ID HARNESS_LAUNCH_APPROVAL=never HARNESS_LAUNCH_SANDBOX=danger-full-access HARNESS_LAUNCH_PROFILE=base
run_codex "$OUT" HARNESS_HOST_DEFAULT_MODE=base -- --passthrough resume $ID
[[ "$(argv_line "$OUT")" == *"-a never -s danger-full-access resume $ID "*  ]] || fail 'M1 herdr restore must reapply the grant' "$OUT"
echo 'PASS: X12 nested launches are flagged; a herdr restore stays top-level'

write_record $ID HARNESS_LAUNCH_APPROVAL=never HARNESS_LAUNCH_SANDBOX=workspace-write HARNESS_LAUNCH_NETWORK=enabled
run_codex "$OUT" -- --passthrough resume $ID
has_arg "$OUT" sandbox_workspace_write.network_access=true || fail 'network grant not restored' "$OUT"
[[ "$(env_of "$OUT" HARNESS_LAUNCH_NETWORK)" == enabled ]] || fail 'restored network not exported to hook' "$OUT"
run_codex "$OUT" HARNESS_LAUNCH_NETWORK=enabled -- base
[[ "$(env_of "$OUT" HARNESS_LAUNCH_NETWORK)" == '<unset>' ]] || fail 'inherited network leaked' "$OUT"
# Y: a history import (HARNESS_CODEX_HISTORY_NATIVE_ID == the restore id) reads the
# origin record only from the re-selected catalog row's own state home, bound to
# the row's origin, and only for a top-level launch. These cases drive the two
# decision functions directly; test-codex-history-resume-integration.sh runs the
# whole import.
cp "$LAUNCHER_DIR/bin/codex-history.py" "$LAUNCHER_DIR/bin/harness_launch_context.py" "$BIN/"
YXDG="$TMP/xdg"; YLEGACY="$YXDG/harness-launcher"; mkdir -p "$YLEGACY"
TOP_BIN="$TMP/topbin"; NESTED_BIN="$TMP/nestedbin"
for d in "$TOP_BIN" "$NESTED_BIN"; do mkdir -p "$d"; cp "$BIN"/* "$d/"; done
printf '%s\n' '#!/usr/bin/env python3' 'print("top_level=1")' > "$TOP_BIN/harness_launch_context.py"
printf '%s\n' '#!/usr/bin/env python3' 'print("top_level=0 reason=agent_ancestor")' 'raise SystemExit(1)' > "$NESTED_BIN/harness_launch_context.py"
yrecord() { # <state-home> <id> <isolated 0|1> <session-id|-> [ENV=val ...]
  local state="$1" id="$2" iso="$3" sid="$4"; shift 4
  local -a extra=(); [[ "$sid" == - ]] || extra=(HARNESS_SESSION_ID="$sid")
  printf '{"hook_event_name":"SessionStart","source":"startup","session_id":"%s"}' "$id" \
    | env -u HARNESS_SESSION_ID HARNESS_SESSION_STATE_HOME="$state" HARNESS_LAUNCH_SOURCE_ROOT="$HARNESS" HARNESS_LAUNCH_ISOLATED="$iso" "${extra[@]}" "$@" \
        "$PY" "$LAUNCHER_DIR/bin/harness-launch-record" codex
}
yrollout() { # <home> <id>
  mkdir -p "$1/sessions/2026/10/10"
  printf '{"type":"session_meta","payload":{"id":"%s"}}\n' "$2" > "$1/sessions/2026/10/10/rollout-2026-10-10T01-02-03-$2.jsonl"
}
yisolated() { # <state-home> <iso-uuid> <native-id>: a live isolated session home in that pool
  local state="$1" iso="$2" nid="$3"
  mkdir -p "$state/sessions/$iso" "$state/worktrees/$iso/.harness/codex"
  print -r -- "$HARNESS" > "$state/sessions/$iso/source-root"
  print -r -- "$state/worktrees/$iso" > "$state/sessions/$iso/session-root"
  stat -f '%d %i' "$state/worktrees/$iso" > "$state/sessions/$iso/root-inode"
  yrollout "$state/worktrees/$iso/.harness/codex" "$nid"
}
yresolve() { # <bin dir> <id> -> "approval=.. sandbox=.." or NONE
  (
    export HOME="$TMP/home" XDG_STATE_HOME="$YXDG" HARNESS_SESSION_STATE_HOME="$STATE" PATH="$BIN:/usr/bin:/bin"
    source "$LAUNCHER_DIR/bin/aliases.zsh"
    _HARNESS_LAUNCHER_BIN="$1"
    local inherited_harness_session_id="" restore_approval restore_sandbox restore_network restore_bypass restore_isolated restore_profile restore_permission restore_window restore_context o
    o="$(_harness_launcher_codex_import_origin "$HARNESS" "$2")" || { echo NONE; exit 0; }
    _harness_launcher_restore_launch_record_bound codex "$2" "$HARNESS" "${o%% *}" "${o#* }" || { echo NONE; exit 0; }
    echo "approval=$restore_approval sandbox=$restore_sandbox"
  ) </dev/null 2>/dev/null
}
GRANT='approval=never sandbox=danger-full-access'
G_ENV=(HARNESS_LAUNCH_APPROVAL=never HARNESS_LAUNCH_SANDBOX=danger-full-access)

# Y1: canonical origin: the record lives in the legacy XDG home.
rm -rf "$STATE/launch-records" "$YLEGACY/launch-records"
YC=01a0ef50-1bec-7852-afb3-a6b6559f4c01
yrollout "$HARNESS/.harness/codex" "$YC"
yrecord "$STATE" "$YC" 0 - HARNESS_LAUNCH_APPROVAL=on-request          # downgraded copy in the current pool
yrecord "$YLEGACY" "$YC" 0 - "${G_ENV[@]}"
[[ "$(yresolve "$TOP_BIN" "$YC")" == "$GRANT" ]] || fail "Y1 canonical origin grant from the legacy home not applied: $(yresolve "$TOP_BIN" "$YC")"
[[ "$(yresolve "$NESTED_BIN" "$YC")" == NONE ]] || fail 'Y1 nested launch inherited the grant'
[[ "$(yresolve "$BIN" "$YC")" == NONE ]] || fail 'Y1 non-tty launch inherited the grant (real context check)'
yrecord "$YLEGACY" "$YC" 1 11111111-2222-4333-8444-555555555551 "${G_ENV[@]}"
[[ "$(yresolve "$TOP_BIN" "$YC")" == NONE ]] || fail 'Y1 isolated record accepted for a canonical row'
rm -f "$HARNESS/.harness/codex/sessions/2026/10/10/rollout-2026-10-10T01-02-03-$YC.jsonl"
echo 'PASS: Y1 canonical origin record from the legacy home; nested/non-tty/mis-bound refused'

# Y2: live isolated origin in the current pool.
YL=01a0ef50-1bec-7852-afb3-a6b6559f4c02; ISO_L=aaaaaaaa-2222-4333-8444-5555555555a1
yisolated "$STATE" "$ISO_L" "$YL"
yrecord "$STATE" "$YL" 1 "$ISO_L" "${G_ENV[@]}"
[[ "$(yresolve "$TOP_BIN" "$YL")" == "$GRANT" ]] || fail 'Y2 isolated origin grant not applied'
yrecord "$STATE" "$YL" 1 bbbbbbbb-2222-4333-8444-5555555555b2 "${G_ENV[@]}"
[[ "$(yresolve "$TOP_BIN" "$YL")" == NONE ]] || fail 'Y2 record bound to another session was applied'
yrecord "$STATE" "$YL" 0 - "${G_ENV[@]}"
[[ "$(yresolve "$TOP_BIN" "$YL")" == NONE ]] || fail 'Y2 isolated=0 record applied to an isolated row'
yrecord "$STATE" "$YL" 1 "${(U)ISO_L}" "${G_ENV[@]}"
[[ "$(yresolve "$TOP_BIN" "$YL")" == "$GRANT" ]] || fail 'Y2 upper-case session binding must match'
echo 'PASS: Y2 live isolated origin in the current pool bound to the row'

# Y3: live isolated origin in the legacy pool; a record only in the other pool is ignored.
YG=01a0ef50-1bec-7852-afb3-a6b6559f4c03; ISO_G=cccccccc-2222-4333-8444-5555555555c3
yisolated "$YLEGACY" "$ISO_G" "$YG"
yrecord "$STATE" "$YG" 1 "$ISO_G" "${G_ENV[@]}"
[[ "$(yresolve "$TOP_BIN" "$YG")" == NONE ]] || fail 'Y3 record in the other pool was applied'
yrecord "$YLEGACY" "$YG" 1 "$ISO_G" "${G_ENV[@]}"
[[ "$(yresolve "$TOP_BIN" "$YG")" == "$GRANT" ]] || fail 'Y3 legacy-pool origin grant not applied'
echo 'PASS: Y3 legacy-pool origin uses the legacy record only'

# Y4: snapshot rows: the origin pool is the manifest source, whichever store holds it.
YS=01a0ef50-1bec-7852-afb3-a6b6559f4c04; ISO_S=dddddddd-2222-4333-8444-5555555555d4
yisolated "$YLEGACY" "$ISO_S" "$YS"
REV="$(printf 'a%.0s' {1..40})"
snap() { "$PY" "$LAUNCHER_DIR/bin/codex-history.py" snapshot --source "$YLEGACY/worktrees/$ISO_S/.harness/codex" --catalog "$1" \
  --receipt "$2" --source-root "$HARNESS" --isolation-id "$ISO_S" --runtime-revision "$REV" >/dev/null; }
mkdir -p "$TMP/receipts"
snap "$YLEGACY/native-history" "$TMP/receipts/s1.json"
mkdir -p "$STATE/native-history/snapshots"; chmod 700 "$STATE/native-history" "$STATE/native-history/snapshots"
for d in "$YLEGACY/native-history/snapshots"/*(N/); do cp -Rp "$d" "$STATE/native-history/snapshots/"; done
rm -rf "$YLEGACY/worktrees/$ISO_S/.harness/codex"
printf 'state=DELIVERED\n' > "$YLEGACY/sessions/$ISO_S/journal"
yrecord "$STATE" "$YS" 1 "$ISO_S" "${G_ENV[@]}"
[[ "$(yresolve "$TOP_BIN" "$YS")" == NONE ]] || fail 'Y4 snapshot in both pools: current-pool record applied for a legacy-sourced snapshot'
yrecord "$YLEGACY" "$YS" 1 "$ISO_S" "${G_ENV[@]}"
[[ "$(yresolve "$TOP_BIN" "$YS")" == "$GRANT" ]] || fail 'Y4 snapshot origin record (manifest source pool) not applied'
echo 'PASS: Y4 snapshot rows resolve the origin pool from the manifest source'

# Y5: an origin pool that is not a configured pool grants nothing.
"$PY" "$LAUNCHER_DIR/bin/codex-history.py" catalog --source-root "$HARNESS" --state-home "$STATE" --legacy-state-home "$TMP/nolegacy" --native-id "$YS" \
  | "$PY" -c 'import json,sys; e=json.load(sys.stdin)["entries"]; assert e and all(r["origin_state_home"]=="" for r in e), e' \
  || fail 'Y5 a row whose origin pool is not configured must carry an empty origin_state_home'
echo 'PASS: Y5 origin pool must be a configured pool'

echo 'PASS: Codex restore fidelity'
