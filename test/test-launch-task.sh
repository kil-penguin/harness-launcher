#!/usr/bin/env zsh
# test-launch-task.sh — the launcher `--task <uuid>` keyword and a fork's parent
# native session reach the launch record hook (Claude: hook arguments in the
# launcher --settings; Codex: HARNESS_LAUNCH_TASK_ID / _PARENT_NATIVE), and
# nothing after `--passthrough` is interpreted.
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCHER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
trap '[[ -n "${TEST_TEMP:-}" && -d "$TEST_TEMP" ]] && rm -rf "$TEST_TEMP"' EXIT
TEST_TEMP="$(mktemp -d)"; TEST_TEMP="${TEST_TEMP:A}"
TEST_HARNESS="$TEST_TEMP/fake-harness"; TEST_BIN="$TEST_TEMP/bin"
TEST_PROFILE_HOME="$TEST_TEMP/profile-home"
mkdir -p "$TEST_HARNESS/config" "$TEST_BIN" "$TEST_PROFILE_HOME/profiles" "$TEST_HARNESS/.claude"
printf 'HARNESS_NAME="t"\nHARNESS_PREFIX="test"\n' > "$TEST_HARNESS/config/launcher.env"
print -r -- "$TEST_HARNESS" > "$TEST_PROFILE_HOME/profiles/test"
cat > "$TEST_BIN/claude" <<'SH'
#!/usr/bin/env bash
for arg in "$@"; do printf 'ARG:%s\n' "$arg"; done >> "$TEST_STUB_FILE"
exit 0
SH
cat > "$TEST_BIN/codex" <<'SH'
#!/usr/bin/env bash
for arg in "$@"; do printf 'ARG:%s\n' "$arg"; done >> "$TEST_STUB_FILE"
printf 'TASK:%s\nPARENT:%s\n' "${HARNESS_LAUNCH_TASK_ID-unset}" "${HARNESS_LAUNCH_PARENT_NATIVE-unset}" >> "$TEST_STUB_FILE"
SH
cat > "$TEST_BIN/codex-home-prepare.sh" <<'SH'
#!/usr/bin/env bash
mkdir -p "$1/.harness/codex"
SH
chmod +x "$TEST_BIN/claude" "$TEST_BIN/codex" "$TEST_BIN/codex-home-prepare.sh"
fail() { echo "FAIL: $1" >&2; [[ -f "${2:-}" ]] && sed 's/^/  /' "$2" >&2; [[ -f "${2:-}.err" ]] && sed 's/^/  err: /' "$2.err" >&2; exit 1; }
T=cccccccc-dddd-4eee-8fff-000000000b02; P=dddddddd-eeee-4fff-8000-000000000c03

run() {  # <stub> <runtime-prefix-or-empty> args...
  local stub="$1" rt="$2"; shift 2
  : > "$stub"
  RC=0
  (
    export TEST_STUB_FILE="$stub" PATH="$TEST_BIN:$PATH" HARNESS_CODEX_BIN="$TEST_BIN/codex" HARNESS_PROFILE_HOME="$TEST_PROFILE_HOME"
    unset HARNESS_CODEX_MCP_PROFILE HARNESS_SESSION_ISOLATION
    source "$LAUNCHER_DIR/bin/aliases.zsh"
    _HARNESS_LAUNCHER_BIN="$LAUNCHER_DIR/bin"
    [[ "$rt" != codex ]] || _HARNESS_LAUNCHER_BIN="$TEST_BIN"
    _harness_launcher_run "$TEST_HARNESS" ${=rt} "$@"
  ) </dev/null >/dev/null 2>"$stub.err" || RC=$?
}
settings_hook() { # prints the SessionStart hook command from the --settings JSON in the stub
  python3 - "$1" <<'PY'
import json, sys
lines = [l[4:] for l in open(sys.argv[1]).read().split('\n') if l.startswith('ARG:')]
for i, a in enumerate(lines):
    if a == '--settings' and lines[i + 1].startswith('{'):
        d = json.loads(lines[i + 1])
        if 'hooks' in d:
            print(d['hooks']['SessionStart'][0]['hooks'][0]['command']); break
PY
}

# Claude: --task is consumed by the launcher, recorded by the hook, never given to claude.
run "$TEST_TEMP/c1" "" base --task $T
[[ $RC == 0 ]] || fail 'claude --task failed' "$TEST_TEMP/c1"
grep -Fxq -- 'ARG:--task' "$TEST_TEMP/c1" && fail '--task leaked to claude' "$TEST_TEMP/c1"
case "$(settings_hook "$TEST_TEMP/c1")" in *" --task-id $T"*) ;; *) fail 'hook lacks --task-id' "$TEST_TEMP/c1" ;; esac
case "$(settings_hook "$TEST_TEMP/c1")" in *--parent-native*) fail 'no fork, no parent' "$TEST_TEMP/c1" ;; esac
# a bad value is refused before claude starts
run "$TEST_TEMP/c2" "" base --task not-a-uuid
[[ $RC == 2 && ! -s "$TEST_TEMP/c2" ]] || fail "bad --task must refuse (rc=$RC)" "$TEST_TEMP/c2"
run "$TEST_TEMP/c3" "" base --task
[[ $RC == 2 && ! -s "$TEST_TEMP/c3" ]] || fail "missing --task value must refuse (rc=$RC)" "$TEST_TEMP/c3"
# a fork of an explicit parent records parent_native
run "$TEST_TEMP/c4" "" base --passthrough --resume $P --fork-session
case "$(settings_hook "$TEST_TEMP/c4")" in *" --parent-native $P"*) ;; *) fail 'fork parent not recorded' "$TEST_TEMP/c4" ;; esac
# #114 rewrites a verified fork to `--resume <transcript>/<native-uuid>.jsonl`; the parent is that exact native id
TP=/home/u/.claude/projects/-proj/$P.jsonl
run "$TEST_TEMP/c7" "" base --passthrough --permission-mode default --resume $TP --fork-session
case "$(settings_hook "$TEST_TEMP/c7")" in *" --parent-native $P"*) ;; *) fail 'rewritten fork parent not recorded' "$TEST_TEMP/c7" ;; esac
for bad in "projects/-proj/$P.jsonl" "/home/u/.claude/projects/../$P.jsonl" "/home/u/.claude/projects/-proj/not-a-uuid.jsonl" "/home/u/.claude/other/$P.jsonl" "/home/u/.claude/projects/-proj/$P.txt"; do
  run "$TEST_TEMP/c8" "" base --passthrough --resume "$bad" --fork-session
  case "$(settings_hook "$TEST_TEMP/c8")" in *--parent-native*) fail "unverifiable transcript path gave a parent: $bad" "$TEST_TEMP/c8" ;; esac
done
run "$TEST_TEMP/c9" "" base --passthrough --resume $TP
case "$(settings_hook "$TEST_TEMP/c9")" in *--parent-native*) fail 'a transcript resume without --fork-session is not a fork' "$TEST_TEMP/c9" ;; esac
run "$TEST_TEMP/c5" "" base --passthrough --resume $P
case "$(settings_hook "$TEST_TEMP/c5")" in *--parent-native*) fail 'resume is not a fork' "$TEST_TEMP/c5" ;; esac
# after --passthrough nothing is a launcher keyword
run "$TEST_TEMP/c6" "" base --passthrough --task $T
grep -Fxq -- 'ARG:--task' "$TEST_TEMP/c6" || fail '--task after --passthrough must stay verbatim' "$TEST_TEMP/c6"
case "$(settings_hook "$TEST_TEMP/c6")" in *--task-id*) fail 'passthrough --task recorded' "$TEST_TEMP/c6" ;; esac

# Codex
run "$TEST_TEMP/x1" codex base --task $T
[[ $RC == 0 ]] || fail 'codex --task failed' "$TEST_TEMP/x1"
grep -Fxq -- "TASK:$T" "$TEST_TEMP/x1" || fail 'codex task not exported' "$TEST_TEMP/x1"
grep -Fxq -- 'ARG:--task' "$TEST_TEMP/x1" && fail '--task leaked to codex' "$TEST_TEMP/x1"
grep -Fxq -- 'PARENT:unset' "$TEST_TEMP/x1" || fail 'no fork, no parent' "$TEST_TEMP/x1"
run "$TEST_TEMP/x2" codex base --task bad
[[ $RC == 2 && ! -s "$TEST_TEMP/x2" ]] || fail "codex bad --task must refuse (rc=$RC)" "$TEST_TEMP/x2"
run "$TEST_TEMP/x3" codex base --passthrough fork $P
grep -Fxq -- "PARENT:$P" "$TEST_TEMP/x3" || fail 'codex fork parent not exported' "$TEST_TEMP/x3"
run "$TEST_TEMP/x6" codex base --passthrough -m some-model fork $P
grep -Fxq -- "PARENT:$P" "$TEST_TEMP/x6" || fail 'codex fork after option/value pairs not found' "$TEST_TEMP/x6"
run "$TEST_TEMP/x7" codex base --passthrough -i a.png fork $P
grep -Fxq -- 'PARENT:unset' "$TEST_TEMP/x7" || fail 'separated -i takes several values: no parent' "$TEST_TEMP/x7"
run "$TEST_TEMP/x8" codex base --passthrough --image a.png fork $P
grep -Fxq -- 'PARENT:unset' "$TEST_TEMP/x8" || fail 'separated --image takes several values: no parent' "$TEST_TEMP/x8"
run "$TEST_TEMP/x9" codex base --passthrough --image=a.png fork $P
grep -Fxq -- "PARENT:$P" "$TEST_TEMP/x9" || fail '--image=x keeps the parent' "$TEST_TEMP/x9"
run "$TEST_TEMP/x10" codex base --passthrough -ia.png fork $P
grep -Fxq -- "PARENT:$P" "$TEST_TEMP/x10" || fail '-ix keeps the parent' "$TEST_TEMP/x10"
run "$TEST_TEMP/x4" codex base --passthrough resume $P
grep -Fxq -- 'PARENT:unset' "$TEST_TEMP/x4" || fail 'codex resume is not a fork' "$TEST_TEMP/x4"
HARNESS_LAUNCH_TASK_ID=stale run "$TEST_TEMP/x5" codex base
grep -Fxq -- 'TASK:unset' "$TEST_TEMP/x5" || fail 'inherited task id must be dropped' "$TEST_TEMP/x5"
echo 'PASS: launcher --task and fork parent reach the launch record'
