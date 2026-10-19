#!/usr/bin/env bash
# test-launch-record-common.sh — the launch-record helpers in harness-common.sh
# (shared by aliases.zsh and launcher.sh) behave the same under macOS
# /bin/bash 3.2 and zsh: shell quoting, JSON escaping, the Codex environment
# export, and the Claude --settings hook, run end to end through /bin/sh.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PY="$(bash -c ". '$ROOT/bin/harness-common.sh'; harness_python3_resolve")"

cat > "$TMP/ps-clean" <<'PS'
1000 999 /bin/sh
998 997 claude
999 998 /bin/sh
997 996 -zsh
996 995 zsh
995 1 herdr
1 0 launchd
PS
WEIRD="$TMP/we ird'root"
mkdir -p "$WEIRD"

# The body runs under each shell; it prints `FAIL: ...` lines and exits 1 on the first failure.
cat > "$TMP/body.sh" <<'BODY'
. "$ROOT/bin/harness-common.sh"
fail() { printf 'FAIL(%s): %s\n' "$SHELL_NAME" "$1"; exit 1; }
unset HARNESS_SESSION_ROOT HARNESS_SESSION_ID HARNESS_SOURCE_ROOT

# harness_shell_word
[ "$(harness_shell_word /opt/x/python3.13@a:b=c,d+e%f-1)" = "/opt/x/python3.13@a:b=c,d+e%f-1" ] || fail 'safe word must stay unquoted'
[ "$(harness_shell_word '')" = "''" ] || fail "empty word must print ''"
[ "$(harness_shell_word 'a b')" = "'a b'" ] || fail 'space must be quoted'
[ "$(harness_shell_word "it's")" = "'it'\\''s'" ] || fail "single quote must use '\\''"
for w in "a b" "it's" "x'y'z" '$HOME;`id`' '*' "$WEIRD" "tab	x"; do
  [ "$(/bin/sh -c "printf '%s' $(harness_shell_word "$w")")" = "$w" ] || fail "round trip through /bin/sh: $w"
done

# harness_json_escape
e="$(harness_json_escape 'a\b"c')"
[ "$e" = 'a\\b\"c' ] || fail "json escape: $e"
[ "$("$PY" -c 'import json,sys; print(json.loads("\"" + sys.argv[1] + "\""))' "$e")" = 'a\b"c' ] || fail 'json escape does not round-trip'

# harness_launch_record_export_codex
export HARNESS_LAUNCH_PERMISSION=bypassPermissions HARNESS_LAUNCH_APPROVAL=never HARNESS_LAUNCH_SANDBOX=read-only HARNESS_LAUNCH_NETWORK=enabled
HARNESS_CODEX_CONTEXT=500k
harness_launch_record_export_codex /r "" "" 1 sol
[ "${HARNESS_LAUNCH_BYPASS-}" = 1 ] && [ "${HARNESS_LAUNCH_PROFILE-}" = sol ] && [ "$HARNESS_LAUNCH_SOURCE_ROOT" = /r ] \
  && [ "$HARNESS_LAUNCH_ISOLATED" = 0 ] && [ "$HARNESS_LAUNCH_CONTEXT" = 500k ] || fail 'codex export values'
[ -z "${HARNESS_LAUNCH_PERMISSION+x}${HARNESS_LAUNCH_APPROVAL+x}${HARNESS_LAUNCH_SANDBOX+x}${HARNESS_LAUNCH_NETWORK+x}" ] || fail 'inherited grant not dropped'
[ "$(/usr/bin/env | grep -c '^HARNESS_LAUNCH_')" = 5 ] || fail 'codex export must reach child processes'
HARNESS_SESSION_ROOT=/s harness_launch_record_export_codex /r on-request workspace-write "" ""
[ "$HARNESS_LAUNCH_ISOLATED" = 1 ] && [ "$HARNESS_LAUNCH_APPROVAL" = on-request ] && [ -z "${HARNESS_LAUNCH_BYPASS+x}" ] || fail 'isolated export'
harness_launch_record_export_codex /r never workspace-write "" base disabled
[ "$HARNESS_LAUNCH_NETWORK" = disabled ] && [ "$HARNESS_LAUNCH_CONTEXT" = 500k ] && [ "$HARNESS_LAUNCH_APPROVAL" = never ] && [ "$HARNESS_LAUNCH_SANDBOX" = workspace-write ] && [ "$HARNESS_LAUNCH_PROFILE" = base ] || fail 'explicit network, context and grants must reach the hook'
HARNESS_CODEX_CONTEXT=auto harness_launch_record_export_codex /r "" "" "" base
[ "$HARNESS_LAUNCH_CONTEXT" = auto ] || fail 'auto context must reach the hook'

# harness_claude_launch_settings
hook_of() { "$PY" -c 'import json,sys; print(json.loads(sys.argv[1])["hooks"]["SessionStart"][0]["hooks"][0]["command"])' "$1"; }
run_hook() { # <command> <session-id>
  printf '{"hook_event_name":"SessionStart","source":"startup","session_id":"%s"}' "$2" \
    | HARNESS_SESSION_STATE_HOME="$TMP/state-$SHELL_NAME" /bin/sh -c "$1 --pstable $TMP/ps-clean --start-pid 1000"
}
record_of() { HARNESS_SESSION_STATE_HOME="$TMP/state-$SHELL_NAME" "$PY" "$ROOT/bin/harness-launch-record" read claude "$1"; }
s="$(harness_claude_launch_settings "$ROOT/bin" "$WEIRD" true bypassPermissions 1m)"
"$PY" -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["alwaysThinkingEnabled"] is True' "$s" || fail "settings JSON: $s"
id=0d5d1f3e-0000-4000-8000-0000000000c1
run_hook "$(hook_of "$s")" $id
[ "$(record_of $id)" = "$(printf 'permission=bypassPermissions\nsource_root=%s\nisolated=0\ncontext=1m' "$WEIRD")" ] \
  || fail "record through /bin/sh with a quoted root: $(record_of $id)"
s="$(harness_claude_launch_settings "$ROOT/bin" /r false 'bypassPermissions;id' "")"
case "$(hook_of "$s")" in *--permission*|*--context*) fail 'unknown permission must record no grant' ;; esac
case "$s" in *alwaysThinking*) fail 'thinking must not be forced' ;; esac
s="$(HARNESS_SESSION_ROOT=/s HARNESS_SESSION_ID=0D5D1F3E-0000-4000-8000-0000000000C2 harness_claude_launch_settings "$ROOT/bin" /r false plan "")"
case "$(hook_of "$s")" in *"--isolated 1"*"--harness-session-id 0D5D1F3E-0000-4000-8000-0000000000C2") ;; *) fail "isolated hook: $(hook_of "$s")" ;; esac
s="$(HARNESS_SESSION_ROOT=/s HARNESS_SESSION_ID='x;id' harness_claude_launch_settings "$ROOT/bin" /r false plan "")"
case "$(hook_of "$s")" in *--harness-session-id*) fail 'a non-UUID session id must not be passed' ;; esac
[ -z "$(harness_claude_launch_settings "$ROOT/bin" "$(printf '/r\033x')" false plan "")" ] || fail 'a control character must drop the hook'
[ -z "$(harness_claude_launch_settings "$TMP/no-bin" /r false plan "")" ] || fail 'no hook script must print nothing'
# The grant binds the canonical source, while opt-in follows the session snapshot.
mkdir -p "$TMP/session/config"
printf '{"enabled":true}\n' > "$TMP/session/config/ssot-session-hooks.json"
s="$(harness_claude_launch_settings "$ROOT/bin" "$WEIRD" false plan "" "$TMP/session")"
"$PY" -c 'import json,sys; h=json.loads(sys.argv[1])["hooks"]; assert "Stop" in h and "UserPromptSubmit" in h' "$s" || fail 'session opt-in lost with stale canonical source'
id=0d5d1f3e-0000-4000-8000-0000000000c3
run_hook "$(hook_of "$s")" $id
case "$(record_of $id)" in *"source_root=$WEIRD"*) ;; *) fail 'config root displaced source grant' ;; esac
s="$(harness_claude_launch_settings "$ROOT/bin" "$TMP/session" false plan "" "$WEIRD")"
"$PY" -c 'import json,sys; assert "Stop" not in json.loads(sys.argv[1])["hooks"]' "$s" || fail 'canonical opt-in overrode disabled session'
echo "PASS($SHELL_NAME)"
BODY

# aliases.zsh is sourced into interactive zsh, so also run under options users set.
printf 'setopt KSH_ARRAYS EXTENDED_GLOB KSH_GLOB SH_GLOB\n. "$TMP/body.sh"\n' > "$TMP/body-zsh-opts.sh"
for run in "/bin/bash bash $TMP/body.sh" "zsh zsh $TMP/body.sh" "zsh zsh+opts $TMP/body-zsh-opts.sh"; do
  set -- $run
  out="$(env ROOT="$ROOT" TMP="$TMP" PY="$PY" WEIRD="$WEIRD" SHELL_NAME="$2" "$1" "$3" 2>&1)" \
    || { printf '%s\n' "$out"; exit 1; }
  printf '%s\n' "$out"
done
echo 'PASS: launch-record helpers agree under bash 3.2, zsh and zsh with common options'
