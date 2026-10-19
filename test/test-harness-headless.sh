#!/usr/bin/env bash
# harness-headless: non-interactive isolated run, delivery, and result contract.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
TMP="$(cd "$TMP" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT
PREFIX="$TMP/prefix"
STUB="$TMP/stub"
SOURCE="$TMP/harness"
REMOTE="$TMP/remote.git"
STATE="$TMP/state"
FAKE_HOME="$TMP/home"
PROFILES="$TMP/profiles"
RESULT="$TMP/out/result.json"
LOCK="$TMP/run.lock"
PROMPT="$TMP/prompt.txt"
mkdir -p "$STUB" "$FAKE_HOME" "$PROFILES/profiles" "$TMP/out" "$SOURCE/config" "$SOURCE/core/bin" "$SOURCE/projects/product" "$SOURCE/.claude"

bash "$ROOT/test/lib/install-runtime-fixture.sh" "$ROOT" "$PREFIX"

# The clean verifier PATH already includes STUB. Bind its python3 to the
# launcher's supported runtime instead of macOS's xcrun-backed system shim.
# This adds no PATH directory or sandbox permission and copies no executable.
FIXTURE_PYTHON="$(source "$ROOT/bin/harness-common.sh"; harness_python3_resolve)"
[[ "$FIXTURE_PYTHON" == /* && -x "$FIXTURE_PYTHON" ]] || {
  echo 'FAIL: headless fixture requires an absolute supported Python executable' >&2
  exit 1
}
ln -s "$FIXTURE_PYTHON" "$STUB/python3"

git init -q --bare -b main "$REMOTE"
git -C "$SOURCE" init -q -b main
git -C "$SOURCE" config user.email test@example.invalid
git -C "$SOURCE" config user.name test
printf '%s\n' 'HARNESS_NAME="headless"' 'HARNESS_PREFIX="hh"' > "$SOURCE/config/launcher.env"
printf '%s\n' 'github_user: tester' > "$SOURCE/config/config.yaml"
printf '%s\n' tracked > "$SOURCE/tracked.txt"
printf '%s\n' '.claude/settings.local.json' 'mcp.local.json' > "$SOURCE/.gitignore"
printf '%s\n' base > "$SOURCE/projects/keep.txt"
printf '%s\n' base > "$SOURCE/projects/drop.txt"
cat > "$SOURCE/core/bin/auto-deliver.sh" <<EOF
#!/usr/bin/env bash
# Reports the environment the repository verifier runs with on stdout (the
# broker log): headless verifiers are sandboxed and can only write their
# candidate and temp dir. Like a harness auto-deliver, it runs the candidate's tests.
printf 'VERIFIER-ENV TMPDIR=%s CLAUDE_CODE_TMPDIR=%s\n' "\${TMPDIR:-}" "\${CLAUDE_CODE_TMPDIR:-}"
[[ ! -s "$TMP/run-tmp" || ! -e "\$(cat "$TMP/run-tmp")" ]] || echo 'VERIFIER-ENV RUN_TMP_PRESENT'
env | grep -E '^(SSH_AUTH_SOCK|GH_|GITHUB_|[A-Z_]*_TOKEN=)' | sed 's/^/VERIFIER-LEAK /'
{ git remote -v; git -C "\$(dirname "\$0")/../.." remote -v; } 2>/dev/null | sed 's/^/VERIFIER-REMOTE /'
[[ ! -e "$TMP/verify-fail" ]] || exit 92
# The broker process that started the sandboxed verifier, for the breach probes.
export VERIFIER_BROKER_PID=\$PPID
for t in "\$TEST_HARNESS_DIR"/verify-tests/*.sh; do
  [[ -e "\$t" ]] || continue
  bash "\$t" || exit 93
done
EOF
chmod +x "$SOURCE/core/bin/auto-deliver.sh"
git -C "$SOURCE" add . && git -C "$SOURCE" commit -qm initial
git -C "$SOURCE" remote add origin "$REMOTE"
git -C "$SOURCE" push -q origin main
printf '%s\n' '{"env":{"LOCAL_SETTINGS_SECRET":"leak-local"}}' > "$SOURCE/.claude/settings.local.json"
printf '%s\n' '{"mcpServers":{"docs":{"command":"echo"}}}' > "$SOURCE/.mcp.local.json"
printf '%s\n' '{"mcpServers":{"local":{"command":"echo"}}}' > "$SOURCE/mcp.local.json"
SOURCE_REAL="$(cd "$SOURCE" && pwd -P)"
printf '%s\n' "$SOURCE" > "$PROFILES/profiles/hh"

cat > "$STUB/gh" <<'EOF'
#!/usr/bin/env bash
[[ "$*" == "auth token --user tester" ]] && echo gh-token-leak
EOF
# Test control travels through files: harness-headless clears the environment.
cat > "$STUB/claude" <<EOF
#!/usr/bin/env bash
printf '%s\0' "\$@" > "$TMP/claude-argv"
env > "$TMP/claude-env"
printf '%s\n' "\${CLAUDE_CODE_TMPDIR:-}" > "$TMP/run-tmp"
stat -f '%Lp %u' "\${CLAUDE_CODE_TMPDIR:-/nonexistent}" > "$TMP/claude-tmpdir-stat" 2>/dev/null
printf '%s\n' "\$PWD" > "$TMP/claude-pwd"
cat > "$TMP/claude-stdin"
mode="\$(cat "$TMP/mode")"
sid=11111111-2222-4333-8444-555555555555
ok='{"type":"result","subtype":"success","is_error":false,"total_cost_usd":0.12,"num_turns":3,"result":"done: summary","session_id":"'\$sid'"}'
case "\$mode" in
  message)
    printf 'message %s\n' "\$\$" > message-change.txt
    bash "$TMP/write-message.sh" "\$HARNESS_COMMIT_MESSAGE_FILE"
    echo "\$ok" ;;
  nonemessage)
    bash "$TMP/write-message.sh" "\$HARNESS_COMMIT_MESSAGE_FILE"
    echo "\$ok" ;;
  failmessage)
    printf 'failmessage %s\n' "\$\$" > failmessage-change.txt
    bash "$TMP/write-message.sh" "\$HARNESS_COMMIT_MESSAGE_FILE"
    echo '{"type":"result","subtype":"error_during_execution","is_error":true,"result":"boom","session_id":"'\$sid'"}'; exit 1 ;;
  change)
    printf 'headless %s\n' "\$\$" > headless-change.txt
    project="\$HOME/.claude/projects/\$(printf '%s' "\$PWD" | sed 's/[^A-Za-z0-9]/-/g')"
    mkdir -p "\$project" && : > "\$project/\$sid.jsonl"
    echo "\$ok" ;;
  none)
    # Claude output that mimics a launcher refusal must not change the status.
    echo 'harness-session: headless clone refused: symlink at x' >&2
    echo "\$ok" ;;
  commit)
    printf 'committed %s\n' "\$\$" > committed.txt
    git add committed.txt && git -c user.name=t -c user.email=t@example.invalid commit -qm agent
    printf 'uncommitted %s\n' "\$\$" > uncommitted.txt
    echo "\$ok" ;;
  excluded)
    printf '{"env":{"PLANTED":"x"}}\n' > .claude/settings.local.json
    mkdir -p config/.local projects && printf 'x\n' > config/.local/planted && printf 'x\n' > projects/planted
    printf 'normal %s\n' "\$\$" > normal.txt
    git add -f .claude/settings.local.json config/.local/planted projects/planted normal.txt
    git -c user.name=t -c user.email=t@example.invalid commit -qm planted
    echo "\$ok" ;;
  variants)
    # One table row: <expect>|<kind>|<path>. The canonical excluded entries are
    # removed first so the variant is created under its own spelling, and the
    # path is negated in .gitignore so only the launcher's fence stands.
    rm -rf projects config/.local mcp.local.json .mcp.local.json .claude/settings.local.json
    while IFS='|' read -r expect kind path; do
      [[ -n "\$kind" ]] || continue
      printf '!/%s\n' "\$path" >> .gitignore
      case "\$kind" in
        file) mkdir -p "\$(dirname "\$path")"; printf 'variant\n' > "\$path" ;;
        dir) mkdir -p "\$path"; printf 'variant\n' > "\$path/a" ;;
        link) mkdir -p projects; ln -s projects "\$path"; printf 'through-link\n' > "\$path/x" ;;
      esac
    done < "$TMP/variants"
    printf 'normal %s\n' "\$\$" > variants-normal.txt
    echo "\$ok" ;;
  casevariants)
    # Negations override info/exclude; case variants dodge case-sensitive
    # pathspecs. None of these may be delivered.
    printf '%s\n' '!/Projects' '!/PROJECTS' '!/config/.LOCAL' '!/.claude/Settings.Local.json' '!/MCP.local.json' '!/.MCP.Local.json' >> .gitignore
    mkdir -p PROJECTS config/.LOCAL .claude
    printf 'v\n' > PROJECTS/variant.txt
    printf 'v\n' > config/.LOCAL/variant.txt
    printf '{"v":1}\n' > .claude/Settings.Local.json
    printf '{"v":1}\n' > MCP.local.json
    printf '{"v":1}\n' > .MCP.Local.json
    printf 'normal %s\n' "\$\$" > casevariants-normal.txt
    echo "\$ok" ;;
  trackedexcluded)
    printf 'agent edit\n' > projects/keep.txt
    rm projects/drop.txt
    printf 'normal %s\n' "\$\$" > trackedexcluded-normal.txt
    echo "\$ok" ;;
  nested)
    printf 'edit %s\n' "\$\$" > nested-edit.txt
    git init -q sub
    echo "\$ok" ;;
  unreadable)
    printf 'edit %s\n' "\$\$" > unreadable-edit.txt
    printf 'x\n' > locked.txt && chmod 000 locked.txt
    echo "\$ok" ;;
  linger)
    printf 'linger %s\n' "\$\$" > linger.txt
    # Outside claude's process group, cwd in the session root.
    perl -e 'use POSIX; POSIX::setsid(); exec @ARGV' sleep 300 > /dev/null 2>&1 &
    echo \$! > "$TMP/linger.pid"
    # Another one parked in the run's private temp base.
    (cd "\$CLAUDE_CODE_TMPDIR" && exec perl -e 'use POSIX; POSIX::setsid(); exec @ARGV' sleep 300 > /dev/null 2>&1) &
    echo \$! > "$TMP/linger-tmp.pid"
    echo "\$ok" ;;
  breach)
    printf 'breach %s\n' "\$\$" > breach-normal.txt
    mkdir -p verify-tests && cp "$TMP/breach.sh" verify-tests/breach.sh && cp "$TMP/breach.env" verify-tests/breach.env
    echo "\$ok" ;;
  payload)
    printf 'payload %s\n' "\$\$" > payload-normal.txt
    mkdir -p verify-tests && cp "$TMP/payload.sh" verify-tests/payload.sh
    echo "\$ok" ;;
  okverify)
    printf 'okverify %s\n' "\$\$" > okverify.txt
    mkdir -p verify-tests && cp "$TMP/okverify.sh" verify-tests/ok.sh
    echo "\$ok" ;;
  conflict)
    printf 'session %s\n' "\$\$" > tracked.txt
    # Meanwhile main changes the same line.
    rm -rf "$TMP/conflict-clone" && git clone -q "$REMOTE" "$TMP/conflict-clone"
    printf 'remote %s\n' "\$\$" > "$TMP/conflict-clone/tracked.txt"
    git -C "$TMP/conflict-clone" -c user.name=t -c user.email=t@example.invalid commit -qam remote
    git -C "$TMP/conflict-clone" push -q origin HEAD:main
    echo "\$ok" ;;
  gitcfgonly)
    printf '[filter "evil"]\n\tclean = touch %s\n' "$TMP/filter-ran" >> .git/config
    echo "\$ok" ;;
  gitcfg)
    printf 'tamper %s\n' "\$\$" > tamper.txt
    printf '* filter=evil\n' > .gitattributes
    printf '[core]\n\tfsmonitor = touch %s\n[filter "evil"]\n\tclean = touch %s\n' "$TMP/fsmonitor-ran" "$TMP/filter-ran" >> .git/config
    echo "\$ok" ;;
  budget) echo '{"type":"result","subtype":"error_max_budget_usd","is_error":true,"total_cost_usd":2.01,"num_turns":9,"session_id":"'\$sid'"}'; exit 1 ;;
  error) echo '{"type":"result","subtype":"error_during_execution","is_error":true,"total_cost_usd":0.5,"num_turns":2,"result":"boom","session_id":"'\$sid'"}'; exit 1 ;;
  hang)
    sleep 300 & echo \$! > "$TMP/grandchild.pid"
    # Its own session (kill_group misses it); cwd in the session root.
    perl -e 'use POSIX; POSIX::setsid(); exec @ARGV' sleep 300 > /dev/null 2>&1 &
    echo \$! > "$TMP/setsid.pid"
    echo \$\$ > "$TMP/child.pid"
    wait ;;
  swaproot)
    # The agent replaces its session root with a symlink to another tree.
    printf 'swap %s\n' "\$\$" > swap-normal.txt
    root="\$PWD"; cd /
    mv "\$root" "$TMP/swapped-root" && ln -s "$TMP/swap-target" "\$root"
    echo "\$ok" ;;
  wait)
    : > "$TMP/started"
    while [[ ! -e "$TMP/release" ]]; do sleep 0.05; done
    echo "\$ok" ;;
esac
EOF
chmod +x "$STUB/gh" "$STUB/claude"
printf '%s\n' 'Fix the typo.' > "$PROMPT"
printf '%s\n' '{"_note":"bridge policy","permissions":{"deny":["Read(//secret/**)"]},"sandbox":{"filesystem":{"denyRead":["/secret"]},"network":{"allowedDomains":["example.com"]}}}' > "$TMP/caller-settings.json"

headless() {
  rm -f "$TMP/claude-argv" "$TMP/claude-env" "$TMP/claude-pwd"
  ${HEADLESS_EXEC:+exec} ${HEADLESS_TTY:+python3} ${HEADLESS_TTY:+"$TMP/ctty.py"} env -i PATH="${EXTRA_PATH:+$EXTRA_PATH:}$TMP/failgit:$STUB:/usr/bin:/bin:/usr/sbin:/sbin" HOME="$FAKE_HOME" TMPDIR="$TMP" \
    HARNESS_PROFILE_HOME="$PROFILES" HARNESS_SESSION_STATE_HOME="$STATE" \
    ANTHROPIC_API_KEY=leak-api BUZZ_TOKEN=leak-buzz TELEGRAM_BOT_TOKEN=leak-telegram \
    ${HARNESS_CODEX_BIN:+"HARNESS_CODEX_BIN=$HARNESS_CODEX_BIN"} \
    "$PREFIX/bin/harness-headless" hh --prompt-file "$PROMPT" --result-file "$RESULT" \
    --lock-file "$LOCK" --budget-usd 2 --timeout-min "${TIMEOUT_MIN:-1}" "$@"
}
# ctty.py <command...>: run the command in a new session whose controlling
# terminal is a fresh PTY, as from an operator's terminal.
cat > "$TMP/ctty.py" <<'PY'
import os, pty, sys
saved = [os.dup(fd) for fd in (0, 1, 2)]
pid, master = pty.fork()
if pid == 0:
    # The test's own stdio again, with one slave descriptor kept open: macOS
    # drops the controlling terminal on the slave's last close.
    os.set_inheritable(os.dup(0), True)
    for fd, orig in enumerate(saved):
        os.dup2(orig, fd)
    os.execvp(sys.argv[1], sys.argv[1:])
raise SystemExit(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
PY
field() { python3 -c 'import json,sys; v=json.load(open(sys.argv[1]))[sys.argv[2]]; print("null" if v is None else v)' "$RESULT" "$1"; }
# On failure, print the result and the launcher/Claude/broker log next to it.
fail() {
  echo "FAIL: $*" >&2
  [[ -f "$RESULT" ]] && { sed 's/^/  /' "$RESULT"; echo; } >&2
  [[ -f "$RESULT.log" ]] && { echo "  --- $RESULT.log" >&2; sed 's/^/  | /' "$RESULT.log" >&2; }
  exit 1
}
expect_status() { [[ "$(field status)" == "$1" && "$(field version)" == 1 ]] || fail "expected status $1 (version 1)"; }

# --- delivered -----------------------------------------------------------------
echo change > "$TMP/mode"
headless --settings-file "$TMP/caller-settings.json" --model sonnet || fail 'delivered run must exit 0'
expect_status delivered
sid="$(field session_id)"
[[ "$sid" =~ ^[0-9A-Fa-f-]{36}$ ]] || fail 'delivered result must name the launcher session'
[[ "$(field commit)" == "$(git --git-dir="$REMOTE" rev-parse main)" ]] || fail 'commit must be the delivered remote SHA'
git --git-dir="$REMOTE" show main:headless-change.txt >/dev/null || fail 'change must reach the remote'
grep -qx state=DELIVERED "$STATE/sessions/$sid/journal" || fail 'session must be DELIVERED'
[[ "$(field cost_usd)" == 0.12 && "$(field num_turns)" == 3 && "$(field summary)" == 'done: summary' && "$(field exit_code)" == 0 ]] || fail 'claude metadata must be copied'
session_root="$STATE/worktrees/$sid"
[[ "$(cat "$TMP/claude-pwd")" == "$session_root" ]] || fail "claude must run in the session root, got $(cat "$TMP/claude-pwd")"
[[ "$(field transcript)" == "$FAKE_HOME/.claude/projects/"*"/11111111-2222-4333-8444-555555555555.jsonl" ]] || fail 'transcript path must be reported'
[[ ! -L "$session_root/projects" && ! -L "$session_root/.claude/settings.local.json" && ! -L "$session_root/mcp.local.json" ]] || fail 'headless session must not link back to the source'
cat > "$TMP/expected-stdin" <<'EOF'
Fix the typo.

---
Delivery note from the launcher: when you finish, the launcher commits your changes to the repository as one commit. Your own git commits are not kept as commits. Write the message for that commit with Bash to the file named by $HARNESS_COMMIT_MESSAGE_FILE: a subject line of at most 72 characters, a blank line, then the body. Follow the repository's commit conventions. If you do not write it, a generic message is used.
EOF
[[ "$(cat "$TMP/claude-stdin")" == "$(cat "$TMP/expected-stdin")" ]] || fail 'the prompt and then the delivery note must reach claude on stdin'
[[ "$(git --git-dir="$REMOTE" log -1 --format=%B main)" == "harness session $sid" ]] || fail 'no message file must give the generic commit message'
grep -q '^harness-headless: commit message: generic' "$RESULT.log" || fail 'the run log must name the generic message source'
run_tmp="$(sed -n 's/^CLAUDE_CODE_TMPDIR=//p' "$TMP/claude-env")"
grep -qx "HARNESS_COMMIT_MESSAGE_FILE=$run_tmp/commit-message" "$TMP/claude-env" || fail 'claude must be told where to write the commit message'
# The message file is in the sandbox's writable temp base; the session record
# that keeps the launcher's copy is outside every path the agent may write.
record="$STATE/sessions/$sid"
python3 - "$TMP/claude-argv" "$record" "$session_root" "$run_tmp" <<'PY' || fail 'the session record must not be writable from the agent sandbox'
import json, sys
argv = open(sys.argv[1]).read().split('\0')[:-1]
record, root, tmp = sys.argv[2:]
fs = json.loads(argv[argv.index('--settings') + 1])['sandbox']['filesystem']
writable = [root] + fs['allowWrite']
inside = lambda path, base: path == base or path.startswith(base.rstrip('/') + '/')
assert inside(tmp + '/commit-message', tmp) and tmp in fs['allowWrite'], fs
assert not any(inside(record, w) or inside(w, record) for w in writable), (record, writable)
PY
[[ "$run_tmp" == /private/tmp/hh-* && "$run_tmp" != /private/tmp/claude-* ]] || fail "claude must get a private short temp base, got '$run_tmp'"
grep -qx "TMPDIR=$run_tmp" "$TMP/claude-env" || fail 'TMPDIR must be the same private temp base'
[[ "$(cat "$TMP/claude-tmpdir-stat")" == "700 $(id -u)" ]] || fail 'the temp base must be 0700 and owned by the user'
[[ ! -e "$run_tmp" ]] || fail 'the temp base must be removed after the run'
python3 - "$TMP/claude-argv" "$SOURCE_REAL" "$FAKE_HOME" "$session_root" "$run_tmp" "$(id -u)" <<'PY' || fail 'claude argv contract'
import json, sys
argv = open(sys.argv[1]).read().split('\0')[:-1]
source, home, root, tmp, uid = sys.argv[2:]
assert argv.count('--settings') == 1, argv
assert '--strict-mcp-config' in argv and '--mcp-config' not in argv, argv
for a, b in (('--output-format', 'json'), ('--max-budget-usd', '2'), ('--permission-mode', 'acceptEdits'), ('--model', 'sonnet')):
    assert argv[argv.index(a) + 1] == b, (a, argv)
assert 'bypassPermissions' not in argv and '-p' in argv and 'Fix the typo.\n' not in argv and '--' not in argv, argv
settings = json.loads(argv[argv.index('--settings') + 1])
assert settings['disableAllHooks'] is True and settings['disableBypassPermissionsMode'] == 'disable', settings
deny = settings['permissions']['deny']
homes = ['%s/%s' % (home, d) for d in ('.hermes', 'buzz', '.ssh', '.config/gh', '.aws', '.claude')]
sandbox = settings['sandbox']
fs = sandbox['filesystem']
assert sandbox['enabled'] is True and sandbox['failIfUnavailable'] is True, sandbox
assert sandbox['allowUnsandboxedCommands'] is False and sandbox['autoAllowBashIfSandboxed'] is True, sandbox
assert sandbox['network'] == {'strictAllowlist': True, 'allowedDomains': ['example.com']}, sandbox
assert set(fs['denyRead']) == set(homes + [source, '/secret']), fs
assert fs['allowWrite'] == [tmp], fs
git = root + '/.git'
assert set(fs['denyWrite']) >= {git + '/config', git + '/hooks', git + '/info', '/private/tmp/claude-' + uid, '/tmp/claude-' + uid}, fs
assert set(sandbox) == {'enabled', 'failIfUnavailable', 'allowUnsandboxedCommands', 'autoAllowBashIfSandboxed', 'network', 'filesystem'}, sandbox
# Gate parity: every sandbox read deny is a Read deny, and both read- and
# write-denied paths are closed to the edit tools.
for path in fs['denyRead']:
    assert 'Read(/%s/**)' % path in deny, ('Read', path)
for path in fs['denyRead'] + [home + '/Library/LaunchAgents']:
    for tool in ('Edit', 'Write', 'NotebookEdit'):
        assert '%s(/%s/**)' % (tool, path) in deny, (tool, path)
for tool in ('Edit', 'Write', 'NotebookEdit'):
    for rule in ('%s(/%s/config)', '%s(/%s/hooks/**)', '%s(/%s/info/**)'):
        assert rule % (tool, git) in deny, (rule, tool)
rules = ['WebFetch', 'WebSearch', 'Bash(hermes:*)', 'Bash(buzz:*)', 'Bash(rm -rf:*)']
rules += ['Bash(*%s*)' % w for w in ('harness-session', 'session-isolation.sh', 'auto-deliver', 'git push', 'sudo', 'launchctl')]
for rule in rules:
    assert rule in deny, (rule, deny)
assert 'Bash(*buzz*)' not in deny and 'Bash(*hermes*)' not in deny, deny
PY
# Merge order: the launcher's settings merge is last-wins for scalars, so the
# mandatory block (passed last) beats any weaker earlier --settings.
python3 - "$ROOT/bin" "$SOURCE" "$FAKE_HOME" <<'PY' || fail 'mandatory settings must win the launcher merge'
import importlib.util, json, sys
bin_dir, source, home = sys.argv[1:]
sys.path.insert(0, bin_dir)
import harness_headless
spec = importlib.util.spec_from_file_location('policy', bin_dir + '/slack-approval-policy.py')
policy = importlib.util.module_from_spec(spec); spec.loader.exec_module(policy)
weak = {'sandbox': {'enabled': False, 'failIfUnavailable': False, 'allowUnsandboxedCommands': True,
                    'network': {'strictAllowlist': False}}, 'permissions': {'deny': []},
        'disableAllHooks': False, 'disableBypassPermissionsMode': 'enable'}
mandatory = harness_headless.mandatory_settings(source, home, [])
argv = policy.claude_argv(['--settings', json.dumps(weak), '--settings', json.dumps(mandatory), '-p'])
merged = json.loads(argv[argv.index('--settings') + 1])
sandbox = merged['sandbox']
assert sandbox['enabled'] is True and sandbox['failIfUnavailable'] is True, sandbox
assert sandbox['allowUnsandboxedCommands'] is False and sandbox['network']['strictAllowlist'] is True, sandbox
assert merged['disableAllHooks'] is True and merged['disableBypassPermissionsMode'] == 'disable', merged
assert set(mandatory['permissions']['deny']) <= set(merged['permissions']['deny'])
PY
for leak in leak-api leak-buzz leak-telegram leak-local gh-token-leak; do
  ! grep -q "$leak" "$TMP/claude-env" || fail "secret $leak reached the claude environment"
done
! grep -q '^GH_TOKEN=' "$TMP/claude-env" || fail 'GH_TOKEN must not be exported in headless runs'
echo 'PASS: harness-headless delivers through the broker with merged denies and a clean environment'

# --- no_changes -----------------------------------------------------------------
echo none > "$TMP/mode"
headless || fail 'no_changes run must exit 0'
expect_status no_changes
grep -qx state=CLOSED "$STATE/sessions/$(field session_id)/journal" || fail 'clean session must close'
[[ "$(field commit)" == null ]] || fail 'no_changes has no commit'
echo 'PASS: harness-headless reports no_changes for a clean session'

# --- the agent's commit message file becomes the delivered commit message ---------
# write-message.sh <path> writes the agent's message: CRLF, an ESC colour
# sequence, trailing blanks, surrounding blank lines and a forged trailer, all
# normalized away. The fallback cases are unit tests below.
cat > "$TMP/write-message.sh" <<'EOF'
#!/bin/bash
printf '\r\n\r\nfeat: agent subject  \r\n\r\nBody with \033[31mred\033[0m text.\t\r\nharness-session: forged\r\nSecond line.\r\n\r\n' > "$1"
EOF
echo message > "$TMP/mode"
headless || fail 'message run must exit 0'
expect_status delivered
msid="$(field session_id)"
body="$(git --git-dir="$REMOTE" log -1 --format=%B main)"
expected="$(printf 'feat: agent subject\n\nBody with [31mred[0m text.\nSecond line.\n\nHarness-Session: %s' "$msid")"
[[ "$body" == "$expected" ]] || fail "the agent message must be delivered sanitized with one trailer, got: $(printf '%q' "$body")"
[[ "$(stat -f '%Lp' "$STATE/sessions/$msid/commit-message")" == 600 ]] || fail 'the record copy of the message must be 0600'
grep -q '^harness-headless: commit message: agent' "$RESULT.log" || fail 'the run log must name the agent message source'
# A run that is not delivered never puts the agent's message in the record:
# an operator who later resumes and closes the session gets the generic one.
echo failmessage > "$TMP/mode"
headless || fail 'failed message run must exit 0'
expect_status failed
[[ ! -e "$STATE/sessions/$(field session_id)/commit-message" ]] || fail 'a failed run must not record the agent message'
echo nonemessage > "$TMP/mode"
headless || fail 'no-changes message run must exit 0'
expect_status no_changes
grep -qx state=CLOSED "$STATE/sessions/$(field session_id)/journal" && [[ ! -e "$STATE/sessions/$(field session_id)/commit-message" ]] \
  || fail 'a no-changes run must leave a CLOSED record without the agent message'
python3 - "$ROOT/bin" <<'PY' || fail 'agent_commit_message unit cases'
import os, sys, tempfile
sys.path.insert(0, sys.argv[1])
import harness_headless as h
def read(content=None, setup=None):
    d = tempfile.mkdtemp()
    path = os.path.join(d, 'commit-message')
    if content is not None:
        open(path, 'wb').write(content if isinstance(content, bytes) else content.encode())
    if setup:
        setup(d, path)
    return h.agent_commit_message(d)
def fallback(name, **kw):
    message, reason = read(**kw)
    assert message is None and reason, (name, message, reason)
def valid(content, expected):
    message, reason = read(content)
    assert message == expected and reason is None, (content, message, reason)
fallback('missing')
fallback('symlink', setup=lambda d, p: (open(d + '/t', 'w').write('feat: x\n'), os.symlink(d + '/t', p)))
fallback('fifo', setup=lambda d, p: os.mkfifo(p))
fallback('oversized', content='feat: big\n\n' + 'a' * 8200)
fallback('utf8', content=b'feat: bad \xff\xfe bytes\n')
fallback('empty', content=' \n\t\n\n')
fallback('long', content='x' * 101 + '\n')
fallback('lines', content='feat: many\n\n' + '\n'.join(str(i) for i in range(199)) + '\n')
fallback('hardlink', setup=lambda d, p: (open(d + '/t', 'w').write('feat: x\n'), os.link(d + '/t', p)))
fallback('only a ci token', content='[skip ci]\n')
valid('x' * 100 + '\n', 'x' * 100 + '\n')
valid('feat: lines\n\n' + '\n'.join(str(i) for i in range(198)) + '\n', 'feat: lines\n\n' + '\n'.join(str(i) for i in range(198)) + '\n')
# CI-skip directives are removed wherever they are; GitHub keywords pass.
valid('fix: thing [skip ci]\n\nbody [CI Skip] [ no ci ] [skip actions] [Actions Skip]\nx [skip [skip ci]ci] y\n'
      'Skip-Checks: true\n  skip-checks : yes\nFixes #12\n',
      'fix: thing\n\nbody\nx  y\nFixes #12\n')
# Format (Cf) characters, C0/C1 controls; trailer variants.
valid('feat: a\u200bb\u202ec\u2066d\x9b31m\n\n  Harness-Session : x\nHARNESS-SESSION:y\n\u200bHarness-Session: z\nNot-Harness-Session: k\n',
      'feat: abcd31m\n\nNot-Harness-Session: k\n')
# The validator's input space, one case per class.
fallback('directory', setup=lambda d, p: os.mkdir(p))
fallback('encoded lone surrogate', content=b'feat: \xed\xa0\x80\n')
fallback('overlong UTF-8', content=b'feat: \xc0\xaf\n')
fallback('only controls and format characters', content='\x00\x1b\x7f\x9b​‮\n')
fallback('over 200 lines through U+2028', content='feat: s\n\n' + ' '.join('x' * 199) + '\n')
valid('feat: n\x00u\x1b[2Jl\x7fl\x85\x9b\n', 'feat: nu[2Jll\n')
valid('\x1b​\n 　\nfeat: real subject\n', 'feat: real subject\n')
valid('feat: s second third\n', 'feat: s\nsecond\nthird\n')
valid('feat: s\n\n' + '\n'.join([
    'Harness-Session: a', 'harness-session:b', '  HARNESS-SESSION : c', '\tHarness-Session\t:d',
    'Harness-Session： e', 'Harness Session: f', 'Harness_Session: g', 'Harness‐Session: h',
    'Ｈａｒｎｅｓｓ-Ｓｅｓｓｉｏｎ: i',
    'Harness-Session﹕ j', 'Harness-Session∶ k', 'Harness​-Session: l', 'Skip-Checks： true',
    'Harness-Sessions: kept']) + '\n', 'feat: s\n\nHarness-Sessions: kept\n')
valid('feat: s [SKIP CI] [Skip Actions][ci  skip]\n', 'feat: s\n')
valid('[skip ci] feat: lead\n\n[no ci]  body\n', 'feat: lead\n\nbody\n')
PY
echo 'PASS: harness-headless delivers a sanitized agent commit message and falls back safely'

# --- committed work, session git payloads, excluded paths, lingering processes ------
echo commit > "$TMP/mode"
headless || fail 'commit run must exit 0'
expect_status delivered
git --git-dir="$REMOTE" show main:committed.txt >/dev/null || fail 'committed agent work must be delivered'
git --git-dir="$REMOTE" show main:uncommitted.txt >/dev/null || fail 'uncommitted agent work must be delivered with it'
echo gitcfg > "$TMP/mode"
headless || fail 'git payload run must exit 0'
expect_status delivered
[[ ! -e "$TMP/fsmonitor-ran" && ! -e "$TMP/filter-ran" ]] || fail 'session git config and attribute payloads must never run'
git --git-dir="$REMOTE" show main:tamper.txt >/dev/null || fail 'work-tree content is still delivered'
echo gitcfgonly > "$TMP/mode"
headless || fail 'config-only run must exit 0'
expect_status no_changes
[[ ! -e "$TMP/filter-ran" ]] || fail 'a repo-config filter must not run'
echo excluded > "$TMP/mode"
headless || fail 'excluded-path run must exit 0'
expect_status delivered
git --git-dir="$REMOTE" show main:normal.txt >/dev/null || fail 'normal committed paths must be delivered'
for excluded in .claude/settings.local.json config/.local/planted projects/planted; do
  ! git --git-dir="$REMOTE" cat-file -e "main:$excluded" 2>/dev/null || fail "excluded $excluded was delivered"
done
echo linger > "$TMP/mode"
rm -f "$TMP/linger.pid"
headless || fail 'linger run must exit 0'
expect_status delivered
[[ -s "$TMP/linger.pid" ]] || fail 'linger process did not start'
! kill -0 "$(cat "$TMP/linger.pid")" 2>/dev/null || fail 'a process left in the session root must be killed before delivery'
[[ -s "$TMP/linger-tmp.pid" ]] && ! kill -0 "$(cat "$TMP/linger-tmp.pid")" 2>/dev/null || fail 'a process left in the run temp base must be killed before delivery'
grep -q '^VERIFIER-ENV TMPDIR=' "$RESULT.log" || fail 'the repository verifier did not run'
! grep -q '^VERIFIER-ENV TMPDIR=/private/tmp/hh-' "$RESULT.log" || fail 'broker git and the verifier must not use the run temp base'
! grep -q '^VERIFIER-ENV .*CLAUDE_CODE_TMPDIR=[^ ]' "$RESULT.log" || fail 'the broker env must not carry CLAUDE_CODE_TMPDIR'
! grep -q '^VERIFIER-ENV RUN_TMP_PRESENT' "$RESULT.log" || fail 'the run temp base must be removed before delivery starts'
verifier_tmp="$(sed -n 's/^VERIFIER-ENV TMPDIR=\([^ ]*\) .*/\1/p' "$RESULT.log" | tail -n 1)"
[[ "$verifier_tmp" == */verifier-tmp.* && ! -e "$verifier_tmp" ]] || fail "the verifier must get its own temp dir, removed afterwards (got '$verifier_tmp')"
echo trackedexcluded > "$TMP/mode"
headless || fail 'tracked excluded run must exit 0'
expect_status delivered
git --git-dir="$REMOTE" show main:trackedexcluded-normal.txt >/dev/null || fail 'normal paths beside an excluded edit must be delivered'
[[ "$(git --git-dir="$REMOTE" show main:projects/keep.txt)" == base ]] || fail 'an edit to a tracked file under an excluded path must not be delivered'
[[ "$(git --git-dir="$REMOTE" show main:projects/drop.txt)" == base ]] || fail 'a deletion under an excluded path must not be delivered'
for local_file in .claude/settings.local.json mcp.local.json .mcp.local.json; do
  ! git --git-dir="$REMOTE" cat-file -e "main:$local_file" 2>/dev/null || fail "gitignored local file $local_file reached the remote"
done
echo casevariants > "$TMP/mode"
headless || fail 'case-variant run must exit 0'
expect_status delivered
git --git-dir="$REMOTE" show main:casevariants-normal.txt >/dev/null || fail 'normal paths beside case variants must be delivered'
leaked="$(git --git-dir="$REMOTE" ls-tree -r --name-only main | grep -iE '^(config/\.local/|\.claude/settings\.local\.json$|\.?mcp\.local\.json$|projects/)' | grep -vx -e projects/keep.txt -e projects/drop.txt || true)"
[[ -z "$leaked" ]] || fail "case variants of excluded paths were delivered: $leaked"
for kept in keep.txt drop.txt; do
  [[ "$(git --git-dir="$REMOTE" show "main:projects/$kept")" == base ]] || fail "projects/$kept changed through a case variant"
done
echo 'PASS: harness-headless delivers the work tree through a launcher-owned git dir'

# --- every exclusion layer agrees: table of path variants -----------------------
# excluded: aliases an excluded entry on this filesystem (case, Unicode case
# folding) and must not reach the remote. distinct: a different name on APFS
# and in git, delivered as its own path. link: a symlink into projects/ is
# delivered as the link itself, never as content under projects/.
{
  printf '%s\n' 'excluded|dir|Projects' 'excluded|file|PROJECTS' 'excluded|dir|config/.LOCAL' \
    'excluded|file|config/.local' 'excluded|file|.claude/SETTINGS.local.json' 'excluded|file|MCP.local.json' \
    'excluded|file|.Mcp.Local.json' 'distinct|dir|projects.' 'distinct|dir|projects ' 'link|link|plink'
  printf 'excluded|dir|project\xc5\xbf\n'                      # projectſ (U+017F folds to s)
  printf 'excluded|file|mcp.local.j\xc5\xbfon\n'                # mcp.local.jſon
  printf 'excluded|file|.claude/\xc5\xbfettings.local.json\n'   # .claude/ſettings.local.json
} > "$TMP/variant-table"
echo variants > "$TMP/mode"
while IFS= read -r row; do
  printf '%s\n' "$row" > "$TMP/variants"
  headless || fail "variant run must exit 0: $row"
  expect_status delivered
  git --git-dir="$REMOTE" show main:variants-normal.txt >/dev/null || fail "normal paths beside $row must be delivered"
  git --git-dir="$REMOTE" ls-tree -r --name-only -z main > "$TMP/main-paths"
  python3 - "$TMP/variants" "$TMP/main-paths" "$TMP/probe" <<'PY' || fail "an exclusion layer disagreed on: $row"
import os, sys
rows = [l.split(b'|', 2) for l in open(sys.argv[1], 'rb').read().split(b'\n') if l]
main = set(open(sys.argv[2], 'rb').read().split(b'\0')) - {b''}
# Filesystem truth: does a path alias an excluded entry on this volume?
probe = sys.argv[3].encode()
for d in (b'projects', b'config/.local', b'.claude'):
    os.makedirs(os.path.join(probe, d), exist_ok=True)
for f in (b'mcp.local.json', b'.mcp.local.json', b'.claude/settings.local.json'):
    open(os.path.join(probe, f), 'a').close()
leaves = [os.path.join(probe, e) for e in (b'projects', b'config/.local', b'mcp.local.json', b'.mcp.local.json', b'.claude/settings.local.json')]
def aliases(path):
    parts = path.split(b'/')
    for depth in (1, 2):
        if len(parts) < depth:
            break
        candidate = os.path.join(probe, *parts[:depth])
        if os.path.lexists(candidate) and any(os.path.samefile(candidate, l) for l in leaves):
            return True
    return False
base = {b'projects/keep.txt', b'projects/drop.txt'}
leaked = sorted(p for p in main if aliases(p) and p not in base)
assert not leaked, ('excluded content delivered', leaked)
for expect, kind, path in rows:
    if expect == b'excluded':
        assert path not in main and path + b'/a' not in main, ('excluded variant delivered', path)
    if expect == b'distinct':
        assert path + b'/a' in main, ('distinct name must be delivered', path)
    if expect == b'link':
        assert path in main and path + b'/x' not in main, ('symlink must be delivered as itself', path)
PY
  for kept in keep.txt drop.txt; do
    [[ "$(git --git-dir="$REMOTE" show "main:projects/$kept")" == base ]] || fail "projects/$kept changed through $row"
  done
done < "$TMP/variant-table"
[[ "$(git --git-dir="$REMOTE" ls-tree main plink | awk '{print $1}')" == 120000 ]] || fail 'plink must be delivered as a symlink'
! git --git-dir="$REMOTE" cat-file -e main:projects/x 2>/dev/null || fail 'content written through a symlink into projects/ was delivered'
echo 'PASS: every exclusion layer agrees on case, Unicode folding, file/dir forms, trailing dots and symlinks'

# --- a failed stage is never reported as no_changes -------------------------------
for mode in nested unreadable; do
  echo "$mode" > "$TMP/mode"
  remote_before="$(git --git-dir="$REMOTE" rev-parse main)"
  headless || fail "$mode run must exit 0"
  expect_status failed
  [[ "$(git --git-dir="$REMOTE" rev-parse main)" == "$remote_before" ]] || fail "$mode run must deliver nothing"
  [[ "$(field session_id)" != null ]] && grep -qv state=CLOSED "$STATE/sessions/$(field session_id)/journal" || fail "$mode session must be kept"
done
echo 'PASS: harness-headless fails closed when the work tree cannot be staged'

# --- a broker git error is never read as "nothing to exclude" or "no changes" -----
# The git on PATH fails calls matching the pattern in $TMP/fail-git (the
# launcher's environment allowlist drops anything else).
mkdir -p "$TMP/failgit"
cat > "$TMP/failgit/git" <<EOF
#!/bin/bash
pattern="\$(cat "$TMP/fail-git" 2>/dev/null)"
if [[ -n "\$pattern" && " \$* " == *"\$pattern"* ]]; then echo "injected git failure: \$pattern" >&2; exit 128; fi
exec "$(command -v git)" "\$@"
EOF
chmod +x "$TMP/failgit/git"
echo variants > "$TMP/mode"
printf 'excluded|dir|project\xc5\xbf\n' > "$TMP/variants"
for pattern in '--name-only --no-renames -z' '--cached --quiet' '--name-status' 'ls-tree'; do
  printf '%s' "$pattern" > "$TMP/fail-git"
  remote_before="$(git --git-dir="$REMOTE" rev-parse main)"
  headless || fail "run with failing git '$pattern' must exit 0"
  expect_status failed
  [[ "$(git --git-dir="$REMOTE" rev-parse main)" == "$remote_before" ]] || fail "run with failing git '$pattern' must deliver nothing"
  ! grep -qx state=CLOSED "$STATE/sessions/$(field session_id)/journal" || fail "session with failing git '$pattern' must be kept"
done
rm -f "$TMP/fail-git"
echo 'PASS: harness-headless fails closed when broker git fails'

# --- an INTEGRATING journal after close is recovered once, whatever the exit code --
python3 - "$ROOT/bin" "$TMP/m1" <<'PY' || fail 'INTEGRATING after close must be recovered'
import os, sys, types
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import harness_headless as h
state, sid = Path(sys.argv[2]), '11111111-2222-4333-8444-555555555555'
d = state / 'sessions' / sid
d.mkdir(parents=True)
journal = lambda s: (d / 'journal').write_text('state=%s\nidentity=\nheartbeat=x\n' % s)
journal('ABANDONED')
calls = []
def run(cmd, **kw):
    calls.append(cmd[1])
    if cmd[1] == 'close':
        journal('INTEGRATING'); return types.SimpleNamespace(returncode=128)
    if cmd[1] == 'recover' and len(calls) > 1:
        (d / 'delivered-sha').write_text('a' * 40 + '\n'); journal('DELIVERED')
    return types.SimpleNamespace(returncode=0)
h.subprocess.run = run
assert h.deliver(sid, state, {}, '/', None) == ('delivered', 'a' * 40, None), calls
assert calls == ['recover', 'close', 'recover'], calls
PY
echo 'PASS: harness-headless recovers an INTEGRATING session once after close'

# --- budget / failed --------------------------------------------------------------
echo budget > "$TMP/mode"
headless || fail 'budget run must exit 0'
expect_status budget
[[ "$(field cost_usd)" == 2.01 && "$(field session_id)" != null ]] || fail 'budget result must keep cost and session'
echo error > "$TMP/mode"
headless || fail 'failed run must exit 0'
expect_status failed
[[ "$(field summary)" == boom && "$(field exit_code)" == 1 ]] || fail 'failed result must keep the claude result and exit code'
echo 'PASS: harness-headless maps budget and error results'

# --- conflict -----------------------------------------------------------------------
echo conflict > "$TMP/mode"
headless || fail 'conflict run must exit 0'
expect_status conflict
grep -qx state=CONFLICT "$STATE/sessions/$(field session_id)/journal" || fail 'conflict session must be left for the owner'
echo 'PASS: harness-headless reports conflict and leaves the session'

# --- a verifier rejection is a failure, not a conflict ------------------------------
echo change > "$TMP/mode"
: > "$TMP/verify-fail"
remote_before="$(git --git-dir="$REMOTE" rev-parse main)"
headless || fail 'verifier-rejected run must exit 0'
rm -f "$TMP/verify-fail"
expect_status failed
[[ "$(git --git-dir="$REMOTE" rev-parse main)" == "$remote_before" ]] || fail 'a rejected candidate must not be delivered'
grep -q 'verifier' "$RESULT" || fail 'a verifier rejection must say so'
! grep -qx -e state=CLOSED -e state=DELIVERED "$STATE/sessions/$(field session_id)/journal" || fail 'a rejected session must be kept'
echo 'PASS: harness-headless reports a verifier rejection as failed and keeps the session'

# --- the verifier runs agent-written tests in a sandbox -----------------------------
# Fixtures under the fake HOME: secrets, history, browser data, another
# session's record, and a Docker-style socket served from outside the sandbox.
mkdir -p "$FAKE_HOME/.ssh" "$FAKE_HOME/Library/Cookies" "$FAKE_HOME/.local/state/harness-launcher/sessions/other" "$FAKE_HOME/.orbstack/run"
printf 'fixture-private-key\n' > "$FAKE_HOME/.ssh/id_fixture"
printf 'echo secret-history\n' > "$FAKE_HOME/.zsh_history"
printf 'cookie\n' > "$FAKE_HOME/Library/Cookies/Cookies.binarycookies"
printf 'state=OPEN\n' > "$FAKE_HOME/.local/state/harness-launcher/sessions/other/journal"
mkdir -p "$FAKE_HOME/.config/git"
# Fake credentials, assembled at run time (no literal credential in the file).
fake="fixture-token"
printf 'https://%s:%s@%s\n' user "$fake" example.invalid > "$FAKE_HOME/.git-credentials"
cp "$FAKE_HOME/.git-credentials" "$FAKE_HOME/.config/git/credentials"
printf '%s %s %s %s %s %s\n' machine example.invalid login user password "$fake" > "$FAKE_HOME/.netrc"
printf '[user]\n\tname = Fixture User\n\temail = fixture@example.invalid\n[http "https://example.invalid/"]\n\textraHeader = %s %s\n' 'Authorization: Bearer' "$fake" > "$FAKE_HOME/.gitconfig"
python3 - "$FAKE_HOME/.orbstack/run/docker.sock" "$TMP/listen-port" "$TMP/breach-tty" <<'PY' &
import os, pty, socket, sys, time
# A terminal the user has open: a PTY made outside the sandbox.
m, s = pty.openpty()
open(sys.argv[3], 'w').write(os.ttyname(s))
unix = socket.socket(socket.AF_UNIX); unix.bind(sys.argv[1]); unix.listen()
tcp = socket.socket(); tcp.bind(('127.0.0.1', 0)); tcp.listen()
open(sys.argv[2], 'w').write(str(tcp.getsockname()[1]))
time.sleep(600)
PY
listener=$!
for _ in $(seq 50); do [[ -s "$TMP/listen-port" ]] && break; sleep 0.1; done
# Outside HOME, in no allowed tree: a token file and a copied binary.
mkdir -p "$TMP/outside"; printf 'outside-%s\n' token > "$TMP/outside/token"; printf '#!/bin/sh\necho "$@"\n' > "$TMP/outside/echo-copy"; chmod +x "$TMP/outside/echo-copy"
# The breach run's state home sits inside a PATH directory, beside another
# session's record and trusted git dir.
BREACH_STATE="$TMP/xdg/state"
mkdir -p "$BREACH_STATE/sessions/other/trusted.git" "$TMP/xdg/bin"
printf 'other-session-%s\n' record > "$BREACH_STATE/sessions/other/journal"
printf '[remote "origin"]\n\turl = https://example.invalid/other.git\n' > "$BREACH_STATE/sessions/other/trusted.git/config"
printf 'LOOP_PORT=%s\nOUTSIDE_PID=%s\nLAUNCHCTL_MARKER=%s\nLAUNCHCTL_LABEL=%s\nOUTSIDE_TOKEN=%s\nOUTSIDE_BIN=%s\nOTHER_SESSION=%s\nOUTSIDE_TTY=%s\n' \
  "$(cat "$TMP/listen-port")" "$listener" "$TMP/launchctl-ran" "harness-breach-$$" "$TMP/outside/token" "$TMP/outside/echo-copy" \
  "$BREACH_STATE/sessions/other" "$(cat "$TMP/breach-tty")" > "$TMP/breach.env"
cat > "$TMP/breach.sh" <<'EOF'
#!/bin/bash
# An agent-written test that reaches for the network, local services, HOME,
# secrets, the keychain, git credentials, launchd, LaunchServices, AppleEvents
# and processes outside the sandbox. It "passes" only if it got everything.
source "$(dirname "$0")/breach.env"
LOOP_PORT="$LOOP_PORT" OUTSIDE_PID="$OUTSIDE_PID" OUTSIDE_TOKEN="$OUTSIDE_TOKEN" OTHER_SESSION="$OTHER_SESSION" \
  OUTSIDE_TTY="$OUTSIDE_TTY" python3 - <<'PY'
import ctypes, ctypes.util, errno, os, signal, socket, subprocess
home = os.environ['HOME']
def probe(name, f):
    try:
        f()
        print(f'PROBE {name} ALLOWED')
    except OSError as e:
        print(f'PROBE {name} {errno.errorcode.get(e.errno, e.errno)}')
def unix_connect(path):
    s = socket.socket(socket.AF_UNIX)
    s.connect(path)
    s.close()
probe('network', lambda: socket.create_connection(('1.1.1.1', 443), timeout=5).close())
probe('loopback', lambda: socket.create_connection(('127.0.0.1', int(os.environ['LOOP_PORT'])), timeout=5).close())
probe('docker-socket', lambda: unix_connect(os.path.join(home, '.orbstack/run/docker.sock')))
probe('write-home', lambda: open(os.path.join(home, 'breach-wrote-home'), 'w').close())
for name, path in (('read-ssh', '.ssh/id_fixture'), ('read-history', '.zsh_history'),
                   ('read-git-credentials', '.git-credentials'), ('read-xdg-git-credentials', '.config/git/credentials'),
                   ('read-netrc', '.netrc'), ('read-gitconfig', '.gitconfig'),
                   ('read-cookies', 'Library/Cookies/Cookies.binarycookies'),
                   ('read-session-record', '.local/state/harness-launcher/sessions/other/journal')):
    probe(name, lambda path=path: open(os.path.join(home, path)).read())
probe('signal-outside', lambda: os.kill(int(os.environ['OUTSIDE_PID']), 0))
probe('read-outside-token', lambda: open(os.environ['OUTSIDE_TOKEN']).read())
probe('read-other-session', lambda: open(os.path.join(os.environ['OTHER_SESSION'], 'journal')).read())
probe('read-other-trusted-git', lambda: open(os.path.join(os.environ['OTHER_SESSION'], 'trusted.git/config')).read())
probe('list-state-home', lambda: os.listdir(os.path.dirname(os.environ['OTHER_SESSION'])))
probe('list-home', lambda: os.listdir(home))
probe('read-library-prefs', lambda: open('/Library/Preferences/SystemConfiguration/preferences.plist', 'rb').read())
probe('pf-system-socket', lambda: socket.socket(socket.PF_SYSTEM, socket.SOCK_DGRAM, socket.SYSPROTO_CONTROL).close())
def process_table():
    # sysctl({CTL_KERN, KERN_PROC, KERN_PROC_ALL}): the process list ps reads.
    libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
    mib, size = (ctypes.c_int * 3)(1, 14, 0), ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value == 0:
        raise OSError(ctypes.get_errno() or errno.EPERM, 'kern.proc')
probe('process-table', process_table)
def outside_process(mib_tail):
    # KERN_PROCARGS2 (argv and environment) and KERN_PROC_PID of the broker.
    def f():
        libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
        mib = (ctypes.c_int * (1 + len(mib_tail)))(1, *mib_tail)
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, len(mib), None, ctypes.byref(size), None, 0) != 0:
            raise OSError(ctypes.get_errno() or errno.EPERM, 'sysctl size')
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, len(mib), buf, ctypes.byref(size), None, 0) != 0:
            raise OSError(ctypes.get_errno() or errno.EPERM, 'sysctl')
    return f
broker = int(os.environ['VERIFIER_BROKER_PID'])
probe('procargs-broker', outside_process((49, broker)))
probe('procpid-broker', outside_process((14, 1, broker)))
# Control: the same reads of the probe's own child, inside the sandbox.
own = subprocess.Popen(['/bin/sleep', '30'])
probe('procargs-own-child', outside_process((49, own.pid)))
probe('procpid-own-child', outside_process((14, 1, own.pid)))
own.kill()
probe('outside-pty', lambda: os.close(os.open(os.environ['OUTSIDE_TTY'], os.O_RDONLY | os.O_NOCTTY)))
# The broker runs with the operator's terminal as its controlling terminal.
probe('dev-tty', lambda: os.close(os.open('/dev/tty', os.O_RDWR)))
PY
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then echo "PROBE $name ALLOWED"; else echo "PROBE $name blocked"; fi; }
check security security find-generic-password -s harness-breach-probe -w
# Credentials in the environment, in a remote URL of the copy or of the
# trusted verifier clone, or in git config the verifier can see.
check env-credentials sh -c "env | grep -qE '^(SSH_AUTH_SOCK|GIT_ASKPASS|GH_|GITHUB_|ANTHROPIC_|OPENAI_|CLAUDE_)|_TOKEN='"
check copy-remote sh -c '[ -n "$(git -C "$TEST_HARNESS_DIR" remote)" ]'
check git-config-token sh -c 'git config --list 2>/dev/null | grep -q fixture-token'
check git-credential sh -c "printf 'protocol=https\nhost=example.invalid\n\n' | GIT_TERMINAL_PROMPT=0 git credential fill"
check nested-sandbox /usr/bin/sandbox-exec -p '(version 1)(allow default)' /usr/bin/true
check osascript /usr/bin/osascript -e 'tell application "Finder" to get version'
check open /usr/bin/open -g -j -a Calculator
check launchctl /bin/launchctl submit -l "$LAUNCHCTL_LABEL" -- /usr/bin/touch "$LAUNCHCTL_MARKER"
check exec-outside "$OUTSIDE_BIN" hi
exit 1
EOF
# Control: outside the sandbox the same reads and writes succeed.
control="$("$TMP/outside/echo-copy" EXEC-OK; python3 - "$FAKE_HOME" "$(cat "$TMP/listen-port")" "$TMP/outside/token" <<'PY' 2>&1 || true
import ctypes, ctypes.util, os, socket, sys
home, port = sys.argv[1], int(sys.argv[2])
open(sys.argv[3]).read()
libc = ctypes.CDLL(ctypes.util.find_library('c'))
mib, size = (ctypes.c_int * 3)(1, 14, 0), ctypes.c_size_t(0)
assert libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) == 0 and size.value > 0
open(os.path.join(home, 'breach-wrote-home'), 'w').close()
open(os.path.join(home, '.ssh/id_fixture')).read()
socket.create_connection(('127.0.0.1', port), timeout=5).close()
s = socket.socket(socket.AF_UNIX); s.connect(os.path.join(home, '.orbstack/run/docker.sock')); s.close()
print('CONTROL-OK')
PY
)"
grep -q EXEC-OK <<< "$control" && grep -q CONTROL-OK <<< "$control" || fail "breach control must reach HOME and the listeners unsandboxed: $control"
rm -f "$FAKE_HOME/breach-wrote-home"
echo breach > "$TMP/mode"
remote_before="$(git --git-dir="$REMOTE" rev-parse main)"
STATE_MAIN="$STATE"; STATE="$BREACH_STATE"; EXTRA_PATH="$TMP/xdg"
HEADLESS_TTY=1 headless || fail 'breach run must exit 0'
STATE="$STATE_MAIN"; unset EXTRA_PATH
sleep 1
/bin/launchctl remove "harness-breach-$$" 2>/dev/null || true
pkill -x Calculator 2>/dev/null || true
kill "$listener" 2>/dev/null || true
expect_status failed
[[ "$(git --git-dir="$REMOTE" rev-parse main)" == "$remote_before" ]] || fail 'a candidate whose tests broke out must not be delivered'
for blocked in 'network EPERM' 'loopback EPERM' 'docker-socket EPERM' 'write-home EPERM' 'read-ssh EPERM' \
    'read-history EPERM' 'read-cookies EPERM' 'read-session-record EPERM' 'signal-outside EPERM' \
    'read-git-credentials EPERM' 'read-xdg-git-credentials EPERM' 'read-netrc EPERM' 'read-gitconfig EPERM' \
    'read-outside-token EPERM' 'process-table EPERM' 'exec-outside blocked' \
    'read-other-session EPERM' 'read-other-trusted-git EPERM' 'list-state-home EPERM' 'list-home EPERM' \
    'read-library-prefs EPERM' 'pf-system-socket EPERM' 'procargs-broker EPERM' 'procpid-broker EPERM' \
    'outside-pty EPERM' 'dev-tty ENXIO' \
    'env-credentials blocked' 'copy-remote blocked' 'git-config-token blocked' \
    'security blocked' 'git-credential blocked' 'nested-sandbox blocked' 'osascript blocked' 'open blocked' 'launchctl blocked'; do
  grep -q "^PROBE $blocked\$" "$RESULT.log" || fail "the verifier sandbox must block: $blocked"
done
for allowed in procargs-own-child procpid-own-child; do
  grep -q "^PROBE $allowed ALLOWED\$" "$RESULT.log" || fail "the verifier sandbox must allow the control: $allowed"
done
! grep -v -e '^PROBE procargs-own-child ALLOWED$' -e '^PROBE procpid-own-child ALLOWED$' "$RESULT.log" | grep -q 'ALLOWED' \
  || fail 'a sandboxed probe was allowed'
[[ ! -e "$FAKE_HOME/breach-wrote-home" && ! -e "$TMP/launchctl-ran" ]] || fail 'the verifier wrote outside the sandbox'
! grep -q '^VERIFIER-LEAK' "$RESULT.log" || fail 'the verifier environment carried credentials'
! grep -q '^VERIFIER-REMOTE' "$RESULT.log" || fail 'a clone the verifier can read kept its remote URL'
echo okverify > "$TMP/mode"
cat > "$TMP/okverify.sh" <<'EOF'
#!/bin/bash
# A normal test: its temp dir, the candidate copy, its own unix socket and git.
set -e
: > "$TMPDIR/ok-tmp"
: > "$TEST_HARNESS_DIR/ok-candidate-write"
python3 -c 'import os, socket; p = os.path.join(os.environ["TMPDIR"], "s"); s = socket.socket(socket.AF_UNIX); s.bind(p); s.listen(); c = socket.socket(socket.AF_UNIX); c.connect(p)'
git -C "$TEST_HARNESS_DIR" status --porcelain >/dev/null
[ "$(git config --global user.name)" = 'Fixture User' ]
echo OK-VERIFY-RAN
EOF
headless || fail 'okverify run must exit 0'
expect_status delivered
grep -q '^OK-VERIFY-RAN$' "$RESULT.log" || fail 'a normal candidate test must run in the sandbox'
git --git-dir="$REMOTE" show main:okverify.txt >/dev/null || fail 'a normal candidate must be delivered'
! git --git-dir="$REMOTE" cat-file -e main:ok-candidate-write 2>/dev/null || fail 'a file the verifier wrote must not be delivered'
# A test that plants git payloads and edits a tracked file in its copy: none
# of it runs during the broker's commit or push, and only the reviewed patch
# is delivered.
git init -q --bare "$TMP/evil-push.git"
cat > "$TMP/payload.sh" <<EOF
#!/bin/bash
cd "\$TEST_HARNESS_DIR"
for hook in pre-commit commit-msg post-commit pre-push reference-transaction; do
  printf '#!/bin/sh\ntouch "$TMP/payload-ran"\n' > ".git/hooks/\$hook"; chmod +x ".git/hooks/\$hook"
done
git config core.fsmonitor 'touch $TMP/payload-ran #'
git config filter.evil.clean 'touch $TMP/payload-ran; cat'
git config credential.helper '!touch $TMP/payload-ran #'
git config remote.origin.pushurl "$TMP/evil-push.git"
printf '* filter=evil\n' > .git/info/attributes
printf 'tampered by the verifier\n' > tracked.txt
git add tracked.txt
exit 0
EOF
echo payload > "$TMP/mode"
headless || fail 'payload run must exit 0'
expect_status delivered
[[ ! -e "$TMP/payload-ran" ]] || fail 'a git payload written by a verifier test ran outside the sandbox'
[[ "$(git --git-dir="$REMOTE" show main:tracked.txt)" != 'tampered by the verifier' ]] || fail 'content the verifier changed was delivered'
git --git-dir="$REMOTE" show main:payload-normal.txt >/dev/null || fail 'the reviewed patch must be delivered'
[[ -z "$(git --git-dir="$TMP/evil-push.git" for-each-ref)" ]] || fail 'the push was redirected by the verifier copy'
echo 'PASS: harness-headless runs candidate tests in a sandbox with no network, services, secrets or write-back'

# --- a moved source checkout is refused with a plain reason -----------------------
mv_out="$(env -i PATH="/usr/bin:/bin:/usr/sbin:/sbin" HOME="$FAKE_HOME" HARNESS_HEADLESS=1 HARNESS_PYTHON_BIN="$(command -v python3)" \
  HARNESS_SESSION_STATE_HOME="$STATE" "$ROOT/bin/session-isolation.sh" create "$SOURCE")"
mv_id="$(printf '%s\n' "$mv_out" | sed -n 's/^HARNESS_SESSION_ID=//p')"; mv_root="$(printf '%s\n' "$mv_out" | sed -n 's/^HARNESS_SESSION_ROOT=//p')"
printf 'moved\n' > "$mv_root/moved.txt"
mv "$SOURCE" "$SOURCE.moved"
moved_rc=0
env -i PATH="/usr/bin:/bin:/usr/sbin:/sbin" HOME="$FAKE_HOME" HARNESS_SESSION_STATE_HOME="$STATE" \
  python3 - "$ROOT/bin" "$STATE" "$mv_id" <<'PY' || moved_rc=$?
import os, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import harness_headless as h
state, sid = Path(sys.argv[2]), sys.argv[3]
subprocess.run([str(h.BIN / 'session-isolation.sh'), 'exit', sid], env=os.environ, stderr=subprocess.DEVNULL)
with open(os.devnull, 'w') as log:
    status, commit, reason = h.deliver(sid, state, dict(os.environ), str(state), log, 'feat: moved\n')
assert status == 'refused' and commit is None and 'source checkout' in (reason or ''), (status, reason)
# Recorded for close, then removed: only a delivered session keeps it.
assert not (state / 'sessions' / sid / 'commit-message').exists(), 'a refused delivery must not keep the agent message'
PY
mv "$SOURCE.moved" "$SOURCE"
[[ "$moved_rc" == 0 ]] || fail 'a moved source checkout must be refused with its own reason'
! grep -qx -e state=CLOSED -e state=DELIVERED "$STATE/sessions/$mv_id/journal" || fail 'a session whose source moved must be kept'
echo 'PASS: harness-headless names a moved source checkout'

# --- H1b: a session root swapped for a symlink is never staged or delivered --------
mkdir -p "$TMP/swap-target"
git -C "$TMP/swap-target" init -q -b main
printf 'stolen\n' > "$TMP/swap-target/stolen.txt"
echo swaproot > "$TMP/mode"
remote_before="$(git --git-dir="$REMOTE" rev-parse main)"
headless || fail 'swapped-root run must exit 0'
expect_status refused
grep -q 'session root' "$RESULT" || fail 'a swapped session root must be named'
[[ "$(git --git-dir="$REMOTE" rev-parse main)" == "$remote_before" ]] || fail 'a swapped session root must deliver nothing'
[[ -z "$(git -C "$TMP/swap-target" status --porcelain --untracked-files=no)" ]] || fail 'the symlink target must not be staged'
echo 'PASS: harness-headless refuses a session whose root was replaced by a symlink'

# --- timeout ------------------------------------------------------------------------
echo hang > "$TMP/mode"
rm -f "$TMP/child.pid" "$TMP/grandchild.pid"
start=$SECONDS
TIMEOUT_MIN=0.1 headless || fail 'timeout run must exit 0'
expect_status timeout
timeout_tmp="$(sed -n 's/^CLAUDE_CODE_TMPDIR=//p' "$TMP/claude-env")"
[[ -n "$timeout_tmp" && ! -e "$timeout_tmp" ]] || fail 'the temp base must be removed after a timeout'
(( SECONDS - start < 30 )) || fail 'timeout must stop the run promptly'
for pidfile in "$TMP/child.pid" "$TMP/grandchild.pid"; do
  [[ -s "$pidfile" ]] || fail "missing $pidfile"
  ! kill -0 "$(cat "$pidfile")" 2>/dev/null || fail 'timeout must kill the whole process group'
done
echo 'PASS: harness-headless kills the process group on timeout'

# --- lock ---------------------------------------------------------------------------
echo wait > "$TMP/mode"
rm -f "$TMP/started" "$TMP/release"
headless > /dev/null 2>&1 &
first=$!
for _ in $(seq 1 200); do [[ -e "$TMP/started" ]] && break; sleep 0.05; done
[[ -e "$TMP/started" ]] || fail 'locked run did not start'
python3 - "$LOCK" <<'PY' || fail 'harness-headless must hold an exclusive flock while running'
import fcntl, sys
with open(sys.argv[1], 'a') as f:
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(0)
raise SystemExit(1)
PY
before="$(shasum "$RESULT")"
rc=0; headless > /dev/null 2>&1 || rc=$?
[[ "$rc" == 75 && "$(shasum "$RESULT")" == "$before" ]] || fail "a busy lock must exit 75 without touching the result (rc=$rc)"
: > "$TMP/release"
wait "$first" || fail 'locked run must finish'
expect_status no_changes
rc=0
env -i PATH="$STUB:/usr/bin:/bin" HOME="$FAKE_HOME" HARNESS_PROFILE_HOME="$PROFILES" \
  "$PREFIX/bin/harness-headless" hh --prompt-file "$PROMPT" --result-file "$RESULT" \
  --lock-file "$TMP/missing-dir/run.lock" --budget-usd 1 --timeout-min 1 2>/dev/null || rc=$?
[[ "$rc" == 2 ]] || fail "an unusable lock path must exit 2 (rc=$rc)"
expect_status refused
echo 'PASS: harness-headless holds the lock file for its whole run'

# --- SIGTERM: kill the group, write the result, then release the lock -------------
echo hang > "$TMP/mode"
rm -f "$TMP/child.pid" "$TMP/grandchild.pid" "$RESULT"
# exec: the background job's pid is harness-headless itself.
HEADLESS_EXEC=1 headless > /dev/null 2>&1 &
runner=$!
for _ in $(seq 1 200); do [[ -s "$TMP/grandchild.pid" && -s "$TMP/child.pid" ]] && break; sleep 0.05; done
[[ -s "$TMP/grandchild.pid" ]] || fail 'hang run did not start'
python3 - "$LOCK" "$RESULT" "$runner" <<'PY' || fail 'the lock must be released only after the result is written'
import fcntl, os, signal, sys, time
lock, result, pid = sys.argv[1], sys.argv[2], int(sys.argv[3])
os.kill(pid, signal.SIGTERM)
with open(lock, 'a') as f:
    for _ in range(400):
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            time.sleep(0.05)
            continue
        raise SystemExit(0 if os.path.exists(result) else 1)
raise SystemExit(1)
PY
wait "$runner" || true
expect_status failed
term_tmp="$(sed -n 's/^CLAUDE_CODE_TMPDIR=//p' "$TMP/claude-env")"
[[ -n "$term_tmp" && ! -e "$term_tmp" ]] || fail 'the temp base must be removed after SIGTERM'
for pidfile in "$TMP/child.pid" "$TMP/grandchild.pid"; do
  ! kill -0 "$(cat "$pidfile")" 2>/dev/null || fail 'SIGTERM must kill the whole process group'
done
! kill -0 "$(cat "$TMP/setsid.pid")" 2>/dev/null || fail 'SIGTERM must kill a process that left the process group'
echo 'PASS: harness-headless handles SIGTERM by killing the group and writing a result'

# --- refused ------------------------------------------------------------------------
rm -f "$TMP/claude-argv"
: > "$PROMPT"
headless || fail 'refused run must exit 0'
expect_status refused
[[ "$(field session_id)" == null && ! -e "$TMP/claude-argv" ]] || fail 'refused run must not launch claude'
printf '%s\n' 'Fix the typo.' > "$PROMPT"
env -i PATH="/usr/bin:/bin" HOME="$FAKE_HOME" HARNESS_PROFILE_HOME="$PROFILES" \
  "$PREFIX/bin/harness-headless" missing --prompt-file "$PROMPT" --result-file "$RESULT" \
  --lock-file "$LOCK" --budget-usd 1 --timeout-min 1 || fail 'unknown harness must still write a result'
expect_status refused
# A tracked symlinked .claude makes the headless clone refuse itself.
LINKED="$TMP/linked"
mkdir -p "$LINKED/config" "$TMP/linked-outside"
git -C "$LINKED" init -q -b main
printf '%s\n' 'HARNESS_NAME="linked"' 'HARNESS_PREFIX="lh"' > "$LINKED/config/launcher.env"
ln -s "$TMP/linked-outside" "$LINKED/.claude"
git -C "$LINKED" add -A && git -C "$LINKED" -c user.email=t@example.invalid -c user.name=t commit -qm initial
rm "$LINKED/.claude" && mkdir "$LINKED/.claude" && printf '%s\n' '{}' > "$LINKED/.claude/settings.local.json"
printf '%s\n' "$LINKED" > "$PROFILES/profiles/lh"
rm -f "$TMP/claude-argv"
env -i PATH="$STUB:/usr/bin:/bin" HOME="$FAKE_HOME" HARNESS_PROFILE_HOME="$PROFILES" HARNESS_SESSION_STATE_HOME="$STATE" \
  "$PREFIX/bin/harness-headless" lh --prompt-file "$PROMPT" --result-file "$RESULT" \
  --lock-file "$LOCK" --budget-usd 1 --timeout-min 1 || fail 'refused clone must still write a result'
expect_status refused
[[ ! -e "$TMP/claude-argv" && ! -e "$TMP/linked-outside/settings.local.json" ]] || fail 'refused clone must not launch claude or write outside'
# Caller settings may only add restrictions; anything else is refused.
for weak in '{"permissions":{"allow":["Bash"]}}' '{"permissions":{"ask":["Bash"]}}' \
  '{"permissions":{"defaultMode":"bypassPermissions"}}' '{"permissions":{"additionalDirectories":["/"]}}' \
  '{"hooks":{}}' '{"env":{"A":"b"}}' '{"apiKeyHelper":"x"}' '{"enableAllProjectMcpServers":true}' \
  '{"sandbox":{"enabled":false}}' '{"sandbox":{"allowUnsandboxedCommands":true}}' \
  '{"sandbox":{"excludedCommands":["git"]}}' '{"sandbox":{"autoAllowBashIfSandboxed":true}}' \
  '{"sandbox":{"network":{"strictAllowlist":false}}}' '{"sandbox":{"filesystem":{"allowWrite":["/"]}}}' \
  '{"sandbox":{"filesystem":{"allowRead":["/"]}}}' '{"sandbox":{"enableWeakerNestedSandbox":true}}' \
  '{"permissions":{"deny":"Bash"}}' '{"sandbox":{"network":{"allowedDomains":[1]}}}' '[]'; do
  printf '%s\n' "$weak" > "$TMP/weak.json"
  rm -f "$TMP/claude-argv"
  headless --settings-file "$TMP/weak.json" || fail "weak settings run must write a result: $weak"
  [[ "$(field status)" == refused && ! -e "$TMP/claude-argv" ]] || fail "caller settings must be refused: $weak"
done
if "$PREFIX/bin/harness-headless" hh --prompt-file "$PROMPT" 2>/dev/null; then fail 'missing required options must exit nonzero'; fi
echo 'PASS: harness-headless refuses runs that would need interactive input'

# --- a temp-base failure still writes a result -------------------------------------
rm -f "$RESULT"
python3 - "$ROOT/bin" hh --prompt-file "$PROMPT" --result-file "$RESULT" --lock-file "$LOCK" \
  --budget-usd 1 --timeout-min 1 <<'PY' || fail 'a mkdtemp failure must still exit 0 with a result'
import sys, tempfile
sys.path.insert(0, sys.argv[1])
import harness_headless
def boom(*a, **k):
    raise OSError(28, 'No space left on device')
tempfile.mkdtemp = boom
raise SystemExit(harness_headless.main(sys.argv[2:]))
PY
expect_status failed
echo 'PASS: harness-headless writes a result when the temp base cannot be created'

# --- no /usr/bin/lockf: refuse before spending anything ---------------------------
rm -f "$RESULT" "$TMP/claude-argv"
env -i PATH="$STUB:/usr/bin:/bin:/usr/sbin:/sbin" HOME="$FAKE_HOME" TMPDIR="$TMP" \
  HARNESS_PROFILE_HOME="$PROFILES" HARNESS_SESSION_STATE_HOME="$STATE" \
  python3 - "$ROOT/bin" hh --prompt-file "$PROMPT" --result-file "$RESULT" --lock-file "$LOCK" \
  --budget-usd 1 --timeout-min 1 <<'PY' || fail 'a missing lockf must still exit 0 with a result'
import sys
sys.path.insert(0, sys.argv[1])
import harness_headless
harness_headless.LOCKF = '/nonexistent/lockf'
raise SystemExit(harness_headless.main(sys.argv[2:]))
PY
expect_status refused
[[ ! -e "$TMP/claude-argv" ]] && grep -q lockf "$RESULT" || fail 'a missing lockf must refuse before launching claude'
echo 'PASS: harness-headless refuses before launch when session delivery cannot lock'

# --- no verifier sandbox: refuse before spending anything --------------------------
printf '#!/bin/sh\necho "sandbox-exec: profile failed to load" >&2\nexit 65\n' > "$TMP/bad-sandbox-exec"; chmod +x "$TMP/bad-sandbox-exec"
for sandbox in /nonexistent/sandbox-exec "$TMP/bad-sandbox-exec"; do
  rm -f "$RESULT" "$TMP/claude-argv"
  env -i PATH="$STUB:/usr/bin:/bin:/usr/sbin:/sbin" HOME="$FAKE_HOME" TMPDIR="$TMP" \
    HARNESS_PROFILE_HOME="$PROFILES" HARNESS_SESSION_STATE_HOME="$STATE" \
    python3 - "$ROOT/bin" "$sandbox" hh --prompt-file "$PROMPT" --result-file "$RESULT" --lock-file "$LOCK" \
    --budget-usd 1 --timeout-min 1 <<'PY' || fail "an unusable sandbox ($sandbox) must still exit 0 with a result"
import sys
sys.path.insert(0, sys.argv[1])
import harness_headless
harness_headless.SANDBOX_EXEC = sys.argv[2]
raise SystemExit(harness_headless.main(sys.argv[3:]))
PY
  expect_status refused
  [[ ! -e "$TMP/claude-argv" ]] && grep -q sandbox "$RESULT" || fail "an unusable sandbox ($sandbox) must refuse before launching claude"
done
echo 'PASS: harness-headless refuses before launch when the verifier sandbox is unavailable'

# === --agent codex ===================================================================
# Same isolated session, delivery and result contract; only the agent process
# differs. The fake codex runs under the generated Seatbelt profile, so it can
# read only what the sandbox allows: it reports on stderr (the run log), and
# test control travels in the prompt (MODE=<mode>). The model endpoint is the
# Responses stub; the launcher's forwarder adds the key.
UP="$TMP/upstream"
mkdir -p "$UP" "$TMP/outside"
printf 'stub-model\n' > "$UP/model"
python3 "$ROOT/test/fixtures/responses_stub.py" "$UP" &
upstream=$!
trap 'kill "$upstream" 2>/dev/null; rm -rf "$TMP"' EXIT
for _ in $(seq 50); do [[ -s "$UP/port" ]] && break; sleep 0.1; done
UP_PORT="$(cat "$UP/port")"
ENDPOINT="http://127.0.0.1:$UP_PORT/v1"
# The production location of the endpoint key, under the fake HOME.
KEY_FILE="$FAKE_HOME/.config/harness-launcher/cliproxy-headless.key"
mkdir -p "$(dirname "$KEY_FILE")"
KEY="sk-fixture-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
(umask 077 && printf '%s\n' "$KEY" > "$KEY_FILE")
CODEX_ARGS=(--agent codex --model stub-model --model-endpoint "$ENDPOINT" --endpoint-key-file "$KEY_FILE")
# Rules the launcher concatenates into CODEX_HOME/AGENTS.md, on origin/main.
git clone -q "$REMOTE" "$TMP/rules-clone"
mkdir -p "$TMP/rules-clone/.claude/rules"
printf '# Fixture rule\n\nRULE-FIXTURE-BODY\n' > "$TMP/rules-clone/.claude/rules/fixture.md"
printf 'INDEX-NOT-COPIED\n' > "$TMP/rules-clone/.claude/rules/_index.md"
git -C "$TMP/rules-clone" add .claude/rules
git -C "$TMP/rules-clone" -c user.name=t -c user.email=t@example.invalid commit -qm rules
git -C "$TMP/rules-clone" push -q origin HEAD:main
codex_prompt() { printf 'Fix the typo.\nMODE=%s\n' "$1" > "$PROMPT"; }

# The fake codex, its code-mode host and a sibling sit in a directory not on
# PATH: only the profile's exact allows let the first two run.
mkdir -p "$TMP/vendor"
HARNESS_CODEX_BIN="$TMP/vendor/codex"
cat > "$TMP/vendor/codex" <<'EOF'
#!/bin/bash
# Fake codex: answers --version; for `exec`, reports what it was given on
# stderr (the run log) and then acts on MODE=<mode> from the prompt.
[[ "${1:-}" != --version ]] || { echo 'codex-cli 0.0.0-fake'; exit 0; }
[[ "${1:-}" == exec ]] || exit 64
args=("$@") out=""
for ((i = 0; i < ${#args[@]}; i++)); do [[ "${args[i]}" != -o ]] || out="${args[i+1]}"; done
{
  printf 'FAKE-CODEX-ARGV'; printf ' [%s]' "$@"; printf '\n'
  printf 'FAKE-CODEX-PWD %s\n' "$PWD"
  env | sed 's/^/FAKE-CODEX-ENV /'
  ls -A "$CODEX_HOME" | sed 's/^/FAKE-CODEX-HOME /'
  stat -f 'FAKE-CODEX-HOME-MODE %Lp' "$CODEX_HOME"
  sed 's/^/FAKE-CODEX-CONFIG /' "$CODEX_HOME/config.toml"
  sed 's/^/FAKE-CODEX-AGENTS /' "$CODEX_HOME/AGENTS.md"
} >&2
prompt="$(cat)"
printf '%s\n' "$prompt" | sed 's/^/FAKE-CODEX-STDIN /' >&2
mode="$(printf '%s\n' "$prompt" | sed -n 's/^MODE=//p' | head -n 1)"
post() {  # post <n>: n model requests through the forwarder named in config.toml
  python3 - "$CODEX_HOME/config.toml" "$1" >&2 <<'PY'
import http.client, json, re, sys
config = open(sys.argv[1]).read()
port = int(re.search(r'^base_url = "http://127\.0\.0\.1:(\d+)/v1"$', config, re.M).group(1))
model = re.search(r'^model = "(.*)"$', config, re.M).group(1)
token = re.search(r'"X-Harness-Forwarder-Token" = "([0-9a-f]+)"', config).group(1)
for _ in range(int(sys.argv[2])):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    c.request('POST', '/v1/responses', json.dumps({'model': model, 'input': []}),
              {'Content-Type': 'application/json', 'Authorization': 'Bearer forged-by-agent', 'Cookie': 'c=1',
               'X-Harness-Forwarder-Token': token})
    r = c.getresponse()
    print('FAKE-CODEX-POST', r.status, b'response.completed' in r.read())
PY
}
ok() {  # ok <summary>: the --json events and -o file of two finished turns
  echo '{"type":"thread.started","thread_id":"t"}'
  echo '{"type":"turn.completed","usage":{"input_tokens":11,"cached_input_tokens":0,"output_tokens":7}}'
  echo 'not json'
  echo '{"type":"turn.completed","usage":{"input_tokens":5,"cached_input_tokens":0,"output_tokens":2}}'
  printf '%s' "$1" > "$out"
}
case "$mode" in
  change)
    post 1
    printf 'codex %s\n' "$$" > codex-change.txt
    printf 'feat: codex change\n\nWritten by the fake codex.\n' > "$HARNESS_COMMIT_MESSAGE_FILE"
    ok 'codex: summary' ;;
  none) post 1; ok 'codex: nothing to do' ;;
  cap) printf 'cap %s\n' "$$" > cap-change.txt; post 3; ok 'codex: capped'; exit 1 ;;
  fail) printf 'fail %s\n' "$$" > fail-change.txt; printf 'boom' > "$out"; exit 3 ;;
  hang)
    sleep 300 & echo "FAKE-CODEX-GRANDCHILD $!" >&2
    # Its own session: kill_group misses it; its cwd is the session root.
    perl -e 'use POSIX; POSIX::setsid(); exec @ARGV' sleep 300 > /dev/null 2>&1 &
    echo "FAKE-CODEX-SETSID $!" >&2
    wait ;;
  probe) LAUNCHER_PID=$PPID CODEX_DIR="$(dirname "$0")" bash @STUB@/codex-probe.sh >&2; ok 'codex: probed' ;;
esac
EOF
sed -i '' "s|@STUB@|$STUB|" "$TMP/vendor/codex"
printf '#!/bin/sh\n[ "$1" != --version ] || exit 70\nexit 0\n' > "$TMP/vendor/codex-badversion"
# Code mode's host, which codex runs from beside its own binary.
printf '#!/bin/sh\n[ "$1" = --help ] || exit 64\necho FAKE-HOST-HELP\n' > "$TMP/vendor/codex-code-mode-host"
chmod +x "$TMP/vendor/codex" "$TMP/vendor/codex-badversion" "$TMP/vendor/codex-code-mode-host"

# --- arguments: per-agent validation, all refused before any agent starts ------------
refuse() {  # refuse <why> <harness-headless args...>
  local why="$1"; shift
  rm -f "$RESULT" "$RESULT.log"
  headless "$@" || fail "a refused run must write a result: $why"
  [[ "$(field status)" == refused && "$(field exit_code)" == 2 && "$(field session_id)" == null ]] || fail "must be refused: $why"
  [[ ! -e "$TMP/claude-argv" ]] && ! grep -q '^FAKE-CODEX-ARGV' "$RESULT.log" 2>/dev/null || fail "a refused run started an agent: $why"
  ! grep -qF "$KEY" "$RESULT" || fail "a refusal printed the key: $why"
}
codex_prompt none
refuse 'unknown agent' --agent gpt
refuse 'claude effort outside low..xhigh' --effort max
refuse 'claude effort minimal' --effort minimal
refuse 'claude with an endpoint' --model-endpoint "$ENDPOINT"
refuse 'claude with a key file' --endpoint-key-file "$KEY_FILE"
refuse 'claude with a request cap' --max-model-requests 5
refuse 'codex effort max' "${CODEX_ARGS[@]}" --effort max
refuse 'codex without an endpoint' --agent codex --model stub-model --endpoint-key-file "$KEY_FILE"
refuse 'codex without a key file' --agent codex --model stub-model --model-endpoint "$ENDPOINT"
refuse 'codex without a model' --agent codex --model-endpoint "$ENDPOINT" --endpoint-key-file "$KEY_FILE"
refuse 'codex model outside the safe set' --agent codex --model 'a"b' --model-endpoint "$ENDPOINT" --endpoint-key-file "$KEY_FILE"
refuse 'codex with caller settings' "${CODEX_ARGS[@]}" --settings-file "$TMP/caller-settings.json"
for endpoint in "http://localhost:$UP_PORT/v1" "https://127.0.0.1:$UP_PORT/v1" "http://127.0.0.1:$UP_PORT/v2" \
    "http://127.0.0.1:$UP_PORT/v1/" "http://127.0.0.1:0/v1" "http://127.0.0.1:99999/v1" "http://10.0.0.1:$UP_PORT/v1" \
    "http://user@127.0.0.1:$UP_PORT/v1" "http://127.0.0.1:$UP_PORT/v1?x=1" "http://[::1]:$UP_PORT/v1"; do
  refuse "endpoint $endpoint" --agent codex --model stub-model --model-endpoint "$endpoint" --endpoint-key-file "$KEY_FILE"
done
for cap in 0 -1 1.5 x ''; do
  refuse "request cap '$cap'" "${CODEX_ARGS[@]}" --max-model-requests "$cap"
done
mkdir -p "$TMP/keys"
ln -s "$KEY_FILE" "$TMP/keys/link.key"
(umask 022 && printf 'open-key\n' > "$TMP/keys/open.key")
(umask 077 && : > "$TMP/keys/empty.key" && printf 'two words\n' > "$TMP/keys/space.key" && mkdir "$TMP/keys/dir.key")
(umask 077 && printf '%s\n' "$KEY" > "$TMP/keys/base.key") && ln "$TMP/keys/base.key" "$TMP/keys/hard.key"
for key in link open empty space dir missing hard; do
  refuse "key file $key" --agent codex --model stub-model --model-endpoint "$ENDPOINT" --endpoint-key-file "$TMP/keys/$key.key"
done
python3 - "$ROOT/bin" "$KEY_FILE" <<'PY' || fail 'a key file owned by another user must be refused'
import os, sys
sys.path.insert(0, sys.argv[1])
import harness_headless as h
uid = os.getuid()
h.os.getuid = lambda: uid + 1
try:
    h.endpoint_key(sys.argv[2])
except h.Refused as exc:
    assert 'owned' in str(exc), exc
else:
    raise SystemExit('accepted')
PY
echo 'PASS: harness-headless validates --agent, --effort, the endpoint, the key file and the request cap per agent'

# --- claude: --effort is launcher-owned settings, absent unless given ----------------
printf '%s\n' 'Fix the typo.' > "$PROMPT"
echo none > "$TMP/mode"
headless --effort xhigh || fail 'claude effort run must exit 0'
expect_status no_changes
python3 - "$TMP/claude-argv" <<'PY' || fail 'claude --effort must be the mandatory effortLevel'
import json, sys
argv = open(sys.argv[1]).read().split('\0')[:-1]
assert '--effort' not in argv, argv
settings = json.loads(argv[argv.index('--settings') + 1])
assert settings['effortLevel'] == 'xhigh' and settings['alwaysThinkingEnabled'] is True, settings
PY
headless --effort low || fail 'claude low effort run must exit 0'
python3 - "$TMP/claude-argv" <<'PY' || fail 'claude low effort must not force thinking'
import json, sys
argv = open(sys.argv[1]).read().split('\0')[:-1]
settings = json.loads(argv[argv.index('--settings') + 1])
assert settings['effortLevel'] == 'low' and 'alwaysThinkingEnabled' not in settings, settings
PY
headless || fail 'claude run without effort must exit 0'
python3 - "$TMP/claude-argv" <<'PY' || fail 'claude without --effort must not set effortLevel'
import json, sys
argv = open(sys.argv[1]).read().split('\0')[:-1]
settings = json.loads(argv[argv.index('--settings') + 1])
assert 'effortLevel' not in settings and 'alwaysThinkingEnabled' not in settings, settings
PY
echo 'PASS: harness-headless passes the claude effort only as launcher-owned settings'

# --- forwarder: loopback-only, fixed paths and model, key added, request cap ---------
python3 - "$ROOT/bin" "$UP_PORT" "$KEY" "$UP" <<'PY' || fail 'forwarder contract'
import contextlib, http.client, io, json, os, socket, sys, time
sys.path.insert(0, sys.argv[1])
import harness_headless as h
up_port, key, up = int(sys.argv[2]), sys.argv[3], sys.argv[4]
err = io.StringIO()
def upstream_posts():
    lines = open(os.path.join(up, 'requests.jsonl')).read().splitlines()
    return [json.loads(l) for l in lines if json.loads(l)['method'] == 'POST']
def req(fw, method, path, body=None, headers=None, host=None, raw=None, token=True):
    c = http.client.HTTPConnection('127.0.0.1', fw.port, timeout=10)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    if host != '':
        c.putheader('Host', host or '127.0.0.1:%d' % fw.port)
    if token:
        c.putheader('X-Harness-Forwarder-Token', fw.token if token is True else token)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b'')
    for k, v in (headers or {}).items():
        c.putheader(k, v)
    if method == 'POST' and not {'Transfer-Encoding', 'Content-Length'} & set(headers or {}):
        c.putheader('Content-Length', str(len(data)))
    c.endheaders(data or None)
    r = c.getresponse()
    return r.status, r.read()
ok_body = {'model': 'stub-model', 'input': []}
with contextlib.redirect_stderr(err):
    fw = h.Forwarder(up_port, key, 'stub-model', 2)
    fw.start()
    try:
        assert req(fw, 'GET', '/v1/models')[0] == 200
        assert h.ForwardHandler.timeout == 60
        # The per-run token from config.toml is required on every request.
        assert len(fw.token) == 32 and req(fw, 'GET', '/v1/models', token=False)[0] == 403
        assert req(fw, 'GET', '/v1/models', token='0' * 32)[0] == 403
        assert req(fw, 'POST', '/v1/responses', body=ok_body, token=False)[0] == 403
        # The forwarder also owns ::1 at its port: nothing else can listen there.
        v6 = socket.socket(socket.AF_INET6)
        try:
            v6.bind(('::1', fw.port))
        except OSError:
            pass
        else:
            raise AssertionError('::1 at the forwarder port is free')
        finally:
            v6.close()
        for host in ('localhost:%d' % fw.port, '127.0.0.1', '127.0.0.1:%d' % up_port, 'evil.example:%d' % fw.port, ''):
            assert req(fw, 'GET', '/v1/models', host=host)[0] == 403, host
        assert req(fw, 'GET', '/v1/models', headers={'Origin': 'http://127.0.0.1:%d' % fw.port})[0] == 403
        for method, path in (('GET', '/v1/responses'), ('POST', '/v1/models'), ('POST', '/v1/chat/completions'),
                             ('GET', '/v1/models?x=1'), ('GET', '/v2/models'), ('GET', '/v1/../v1/models'),
                             ('GET', '/'), ('GET', 'http://127.0.0.1:%d/v1/models' % up_port), ('PUT', '/v1/responses'),
                             ('DELETE', '/v1/responses')):
            assert req(fw, method, path, body=ok_body if method in ('POST', 'PUT') else None)[0] in (404, 405, 501), (method, path)
        for body in ({'model': 'other-model', 'input': []}, {'input': []}, {'model': ['stub-model']}):
            assert req(fw, 'POST', '/v1/responses', body=body)[0] == 403, body
        # Duplicate keys: a parser upstream may read the other one.
        for raw in (b'{not json', b'["stub-model"]', b'', b'{"model":"stub-model","model":"other-model"}',
                    b'{"model":"other-model","model":"stub-model"}', b'{"model":"stub-model","input":[],"input":[]}',
                    b'{"model":"stub-model","input":[{"a":1,"a":2}]}'):
            assert req(fw, 'POST', '/v1/responses', raw=raw)[0] == 400, raw
        # Refused from the declared length, before the body is read.
        assert req(fw, 'POST', '/v1/responses', headers={'Content-Length': str(h.FORWARD_BODY_MAX + 1)})[0] == 413
        assert req(fw, 'POST', '/v1/responses', headers={'Transfer-Encoding': 'chunked'}, raw=b'0\r\n\r\n')[0] in (411, 413)
        assert fw.requests == 0 and not fw.exhausted, 'rejected requests must not count'
        before = len(upstream_posts())
        status, data = req(fw, 'POST', '/v1/responses', body=ok_body,
                           headers={'Authorization': 'Bearer caller', 'Cookie': 'a=b', 'Proxy-Authorization': 'x',
                                    'Connection': 'X-Drop', 'X-Drop': '1', 'X-Unknown': '1', 'OpenAI-Beta': 'b',
                                    'X-Codex-Turn-Metadata': 'm', 'Session-Id': 's', 'Accept': 'text/event-stream'})
        assert status == 200 and b'response.completed' in data, (status, data)
        sent = upstream_posts()[-1]
        assert sent['authorization'] == 'Bearer ' + key and sent['cookie'] is None, 'key added, caller credentials dropped'
        dropped = {'proxy-authorization', 'x-drop', 'x-unknown', 'cookie', 'x-harness-forwarder-token', 'connection'}
        assert not dropped & set(sent['headers']), sent['headers']
        assert {'openai-beta', 'x-codex-turn-metadata', 'session-id', 'accept'} <= set(sent['headers']), sent['headers']
        assert req(fw, 'POST', '/v1/responses', body=ok_body)[0] == 200
        assert req(fw, 'POST', '/v1/responses', body=ok_body)[0] == 403 and fw.exhausted, 'request cap'
        assert len(upstream_posts()) == before + 2, 'the cap stops requests before upstream'
        assert fw.requests == 2
        open(os.path.join(up, 'redirect'), 'w').close()
        try:
            assert req(fw, 'GET', '/v1/models')[0] == 502, 'redirects are not followed or passed on'
        finally:
            os.unlink(os.path.join(up, 'redirect'))
    finally:
        fw.close()
    # SSE is relayed as it arrives.
    fw = h.Forwarder(up_port, key, 'stub-model', 5)
    fw.start()
    with open(os.path.join(up, 'delay'), 'w') as f:
        f.write('1.5')
    try:
        c = http.client.HTTPConnection('127.0.0.1', fw.port, timeout=10)
        c.request('POST', '/v1/responses', json.dumps(ok_body),
                  {'Content-Type': 'application/json', 'X-Harness-Forwarder-Token': fw.token})
        start = time.monotonic()
        r = c.getresponse()
        first = r.read1(65536)
        first_at = time.monotonic() - start
        rest = r.read()
        total = time.monotonic() - start
        assert b'response.created' in first and b'response.completed' not in first, first
        assert first_at < 1.0 and total >= 2.5 and b'response.completed' in rest, (first_at, total)
    finally:
        os.unlink(os.path.join(up, 'delay'))
        fw.close()
    # At most 8 requests at once; the 9th is answered 503 at once.
    import threading
    fw = h.Forwarder(up_port, key, 'stub-model', 50)
    fw.start()
    with open(os.path.join(up, 'delay'), 'w') as f:
        f.write('1')
    try:
        results = []
        workers = [threading.Thread(target=lambda: results.append(req(fw, 'POST', '/v1/responses', body=ok_body)[0]))
                   for _ in range(8)]
        for w in workers:
            w.start()
        time.sleep(0.7)
        assert req(fw, 'GET', '/v1/models')[0] == 503
        for w in workers:
            w.join()
        assert results == [200] * 8, results
        assert req(fw, 'GET', '/v1/models')[0] == 200
    finally:
        os.unlink(os.path.join(up, 'delay'))
        fw.close()
    # An idle client is dropped after the handler timeout.
    h.ForwardHandler.timeout = 1
    fw = h.Forwarder(up_port, key, 'stub-model', 5)
    fw.start()
    try:
        idle = socket.create_connection(('127.0.0.1', fw.port), timeout=5)
        start = time.monotonic()
        assert idle.recv(1) == b'' and time.monotonic() - start < 4
        idle.close()
    finally:
        h.ForwardHandler.timeout = 60
        fw.close()
    # A handler error closes the connection and prints nothing.
    fw = h.Forwarder(up_port, key, 'stub-model', 5)
    fw.start()
    admit = h.ForwardHandler.admit
    h.ForwardHandler.admit = lambda self, fw: (_ for _ in ()).throw(RuntimeError('boom ' + key))
    try:
        try:
            status = req(fw, 'GET', '/v1/models')[0]
        except (http.client.HTTPException, OSError):
            status = None
        assert status in (None, 500), status
    finally:
        h.ForwardHandler.admit = admit
        fw.close()
    # The upstream is down: a gateway error, no hang.
    s = socket.socket(); s.bind(('127.0.0.1', 0)); dead = s.getsockname()[1]; s.close()
    fw = h.Forwarder(dead, key, 'stub-model', 5)
    fw.start()
    try:
        assert req(fw, 'GET', '/v1/models')[0] == 502
    finally:
        fw.close()
assert err.getvalue() == '', 'the forwarder must not log: %r' % err.getvalue()[:200]
PY
echo 'PASS: the forwarder admits only its own Host and paths, adds the key, streams SSE and caps requests'

# --- preflight: every check refuses before the agent starts --------------------------
codex_prompt none
HARNESS_CODEX_BIN=/nonexistent/codex refuse 'no codex binary' "${CODEX_ARGS[@]}"
grep -q codex "$RESULT" || fail 'a missing codex must be named'
dead_port="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
refuse 'models unreachable' --agent codex --model stub-model --model-endpoint "http://127.0.0.1:$dead_port/v1" --endpoint-key-file "$KEY_FILE"
grep -q 'models' "$RESULT" || fail 'an unreachable endpoint must be named'
: > "$UP/redirect"
refuse 'models redirected' "${CODEX_ARGS[@]}"
rm -f "$UP/redirect"
open_sessions() { { grep -l '^state=OPEN$' "$STATE"/sessions/*/journal 2>/dev/null || true; } | wc -l | tr -d ' '; }
open_before="$(open_sessions)"
HARNESS_CODEX_BIN="$TMP/vendor/codex-badversion" refuse 'codex --version fails under the profile' "${CODEX_ARGS[@]}"
grep -q 'sandbox' "$RESULT" || fail 'a sandbox start failure must say so'
# No code-mode host beside codex, or one that does not run under the profile.
mkdir -p "$TMP/nohost" "$TMP/badhost"
cp "$TMP/vendor/codex" "$TMP/nohost/codex" && cp "$TMP/vendor/codex" "$TMP/badhost/codex"
printf '#!/bin/sh\nexit 1\n' > "$TMP/badhost/codex-code-mode-host" && chmod +x "$TMP/badhost/codex-code-mode-host"
HARNESS_CODEX_BIN="$TMP/nohost/codex" refuse 'no code-mode host beside codex' "${CODEX_ARGS[@]}"
grep -q 'codex-code-mode-host' "$RESULT" || fail 'a missing code-mode host must be named'
HARNESS_CODEX_BIN="$TMP/badhost/codex" refuse 'code-mode host fails under the profile' "${CODEX_ARGS[@]}"
grep -q 'codex-code-mode-host' "$RESULT" || fail 'a failing code-mode host must be named'
# A codex or host others could rewrite: group or other write.
for which in codex codex-code-mode-host; do
  rm -rf "$TMP/writable-$which"; mkdir "$TMP/writable-$which"
  cp -p "$TMP/vendor/codex" "$TMP/vendor/codex-code-mode-host" "$TMP/writable-$which/"
  for bit in g+w o+w; do
    chmod 755 "$TMP/writable-$which/$which" && chmod "$bit" "$TMP/writable-$which/$which"
    HARNESS_CODEX_BIN="$TMP/writable-$which/codex" refuse "$which $bit" "${CODEX_ARGS[@]}"
    grep -q 'writable' "$RESULT" || fail "a $bit $which must be named"
  done
done
[[ "$(open_sessions)" == "$open_before" ]] || fail 'a refused codex preflight must not leave an OPEN session'
# A key the agent sandbox could read (a PATH directory is readable) is refused.
(umask 077 && printf '%s\n' "$KEY" > "$STUB/readable.key")
refuse 'key readable inside the sandbox' --agent codex --model stub-model --model-endpoint "$ENDPOINT" --endpoint-key-file "$STUB/readable.key"
grep -q 'readable' "$RESULT" || fail 'a sandbox-readable key must be named'
rm -f "$STUB/readable.key"
rm -f "$RESULT"
env -i PATH="$STUB:/usr/bin:/bin:/usr/sbin:/sbin" HOME="$FAKE_HOME" TMPDIR="$TMP" \
  HARNESS_PROFILE_HOME="$PROFILES" HARNESS_SESSION_STATE_HOME="$STATE" HARNESS_CODEX_BIN="$HARNESS_CODEX_BIN" \
  python3 - "$ROOT/bin" hh --prompt-file "$PROMPT" --result-file "$RESULT" --lock-file "$LOCK" \
  --budget-usd 1 --timeout-min 1 "${CODEX_ARGS[@]}" <<'PY' || fail 'a forwarder that cannot start must still exit 0'
import sys
sys.path.insert(0, sys.argv[1])
import harness_headless as h
def boom(*a, **k):
    raise OSError(48, 'Address already in use')
h.Forwarder.start = boom
raise SystemExit(h.main(sys.argv[2:]))
PY
expect_status refused
grep -q forwarder "$RESULT" || fail 'a forwarder start failure must be named'
open_before="$(open_sessions)"
rm -f "$RESULT"
env -i PATH="$STUB:/usr/bin:/bin:/usr/sbin:/sbin" HOME="$FAKE_HOME" TMPDIR="$TMP" \
  HARNESS_PROFILE_HOME="$PROFILES" HARNESS_SESSION_STATE_HOME="$STATE" HARNESS_CODEX_BIN="$HARNESS_CODEX_BIN" \
  python3 - "$ROOT/bin" hh --prompt-file "$PROMPT" --result-file "$RESULT" --lock-file "$LOCK" \
  --budget-usd 1 --timeout-min 1 "${CODEX_ARGS[@]}" <<'PY' || fail 'a lease failure must still exit 0'
import sys
sys.path.insert(0, sys.argv[1])
import harness_headless as h
def busy(*a, **k):
    raise BlockingIOError(35, 'Resource temporarily unavailable')
h.fcntl.lockf = busy
raise SystemExit(h.main(sys.argv[2:]))
PY
[[ "$(field status)" == failed && "$(open_sessions)" == "$open_before" ]] || fail 'a lease failure after create must finish the session'
echo 'PASS: harness-headless refuses a codex run before it starts when any preflight check fails'

# --- delivered: CODEX_HOME, argv, env, key isolation, result and commit message ------
codex_prompt change
up_before="$(wc -l < "$UP/requests.jsonl" | tr -d ' ')"
headless "${CODEX_ARGS[@]}" --effort high || fail 'codex delivered run must exit 0'
expect_status delivered
sid="$(field session_id)"
session_root="$STATE/worktrees/$sid"
[[ "$(field commit)" == "$(git --git-dir="$REMOTE" rev-parse main)" ]] || fail 'codex commit must be the delivered remote SHA'
git --git-dir="$REMOTE" show main:codex-change.txt >/dev/null || fail 'the codex change must reach the remote'
[[ "$(git --git-dir="$REMOTE" log -1 --format=%B main)" == "$(printf 'feat: codex change\n\nWritten by the fake codex.\n\nHarness-Session: %s' "$sid")" ]] \
  || fail 'the codex commit message must be delivered'
[[ "$(field summary)" == 'codex: summary' && "$(field cost_usd)" == null && "$(field num_turns)" == 2 \
   && "$(field transcript)" == null && "$(field exit_code)" == 0 ]] || fail 'codex result fields'
python3 -c 'import json,sys; r = json.load(open(sys.argv[1])); assert r["usage"] == {"input_tokens": 16, "output_tokens": 9}, r' "$RESULT" \
  || fail 'usage must sum the turn.completed events'
log="$RESULT.log"
run_tmp="$(sed -n 's/^FAKE-CODEX-ENV TMPDIR=//p' "$log")"
[[ "$run_tmp" == /private/tmp/hh-* && ! -e "$run_tmp" ]] || fail "the codex temp base must be private and removed, got '$run_tmp'"
expected_argv="FAKE-CODEX-ARGV [exec] [--json] [-o] [$run_tmp/last-message.md] [-C] [$session_root] [-]"
grep -qxF "$expected_argv" "$log" || fail "codex argv, expected: $expected_argv"
grep -qxF "FAKE-CODEX-PWD $session_root" "$log" || fail 'codex must run in the session root'
grep -qxF "FAKE-CODEX-ENV CODEX_HOME=$run_tmp/codex-home" "$log" || fail 'CODEX_HOME must be under the run temp base'
grep -qxF "FAKE-CODEX-ENV HARNESS_COMMIT_MESSAGE_FILE=$run_tmp/commit-message" "$log" || fail 'codex must be told where to write the commit message'
! grep -q '^FAKE-CODEX-ENV CLAUDE_CODE_TMPDIR=' "$log" || fail 'codex gets no Claude temp variable'
[[ "$(sed -n 's/^FAKE-CODEX-HOME //p' "$log" | sort | tr '\n' ' ')" == 'AGENTS.md config.toml ' ]] || fail 'CODEX_HOME must hold exactly config.toml and AGENTS.md'
grep -qx 'FAKE-CODEX-HOME-MODE 700' "$log" || fail 'CODEX_HOME must be 0700'
sed -n 's/^FAKE-CODEX-CONFIG //p' "$log" > "$TMP/codex-config.toml"
python3 - "$TMP/codex-config.toml" "$UP_PORT" <<'PY' || fail 'codex config.toml contract'
import re, sys, tomllib
text = open(sys.argv[1]).read()
c = tomllib.loads(text)
loop = c['model_providers']['loop']
port = re.fullmatch(r'http://127\.0\.0\.1:(\d+)/v1', loop['base_url'])
assert port and int(port.group(1)) != int(sys.argv[2]), 'base_url must be the forwarder, not the endpoint'
assert c['model'] == 'stub-model' and c['model_provider'] == 'loop' and c['model_reasoning_effort'] == 'high', c
assert c['approval_policy'] == 'never' and c['sandbox_mode'] == 'danger-full-access', c
assert loop['wire_api'] == 'responses' and loop['supports_websockets'] is False and loop['requires_openai_auth'] is False, loop
assert 'env_key' not in loop and 'env_http_headers' not in loop, loop
assert list(loop['http_headers']) == ['X-Harness-Forwarder-Token'], loop
assert re.fullmatch(r'[0-9a-f]{32}', loop['http_headers']['X-Harness-Forwarder-Token']), loop
assert c['history'] == {'persistence': 'none'}, c
# Code mode stays on: code-mode models need it (the host is allowed instead).
assert 'features' not in c, c
for absent in ('mcp_servers', 'hooks', 'notify', 'projects', 'profiles'):
    assert absent not in c, absent
PY
grep -qx 'FAKE-CODEX-AGENTS RULE-FIXTURE-BODY' "$log" && ! grep -q 'INDEX-NOT-COPIED' "$log" \
  || fail 'CODEX_HOME/AGENTS.md must concatenate the session .claude/rules/*.md without _index.md'
grep -qF 'FAKE-CODEX-STDIN Delivery note from the launcher:' "$log" && grep -qx 'FAKE-CODEX-STDIN MODE=change' "$log" \
  || fail 'the prompt and the delivery note must reach codex on stdin'
grep -qx 'FAKE-CODEX-POST 200 True' "$log" || fail 'codex must reach the model through the forwarder'
python3 - "$UP/requests.jsonl" "$up_before" "$KEY" <<'PY' || fail 'the forwarder must replace the agent credentials with the key'
import json, sys
lines = open(sys.argv[1]).read().splitlines()[int(sys.argv[2]):]
posts = [json.loads(l) for l in lines if json.loads(l)['method'] == 'POST']
assert len(posts) == 1, len(posts)
assert posts[0]['authorization'] == 'Bearer ' + sys.argv[3] and posts[0]['cookie'] is None, 'credentials'
PY
for leak in "$KEY" leak-api leak-buzz leak-telegram; do
  ! grep -qF -- "$leak" "$log" "$RESULT" || fail "secret $leak reached the codex env, argv, config, AGENTS.md, log or result"
done
echo 'PASS: harness-headless --agent codex delivers through the broker with a launcher-built CODEX_HOME and no key in reach'

# --- no_changes, failed, request cap ---------------------------------------------------
codex_prompt none
headless "${CODEX_ARGS[@]}" || fail 'codex no_changes run must exit 0'
expect_status no_changes
grep -qx state=CLOSED "$STATE/sessions/$(field session_id)/journal" || fail 'a clean codex session must close'
[[ "$(field summary)" == 'codex: nothing to do' ]] || fail 'no_changes keeps the codex summary'
codex_prompt fail
remote_before="$(git --git-dir="$REMOTE" rev-parse main)"
headless "${CODEX_ARGS[@]}" || fail 'codex failed run must exit 0'
expect_status failed
[[ "$(field summary)" == boom && "$(field exit_code)" == 3 && "$(git --git-dir="$REMOTE" rev-parse main)" == "$remote_before" ]] \
  || fail 'a failed codex run keeps its summary and exit code and delivers nothing'
grep -qx state=ABANDONED "$STATE/sessions/$(field session_id)/journal" || fail 'a failed codex session must be kept'
codex_prompt cap
headless "${CODEX_ARGS[@]}" --max-model-requests 2 || fail 'codex capped run must exit 0'
expect_status budget
[[ "$(grep -c '^FAKE-CODEX-POST 200 True$' "$RESULT.log")" == 2 && "$(grep -c '^FAKE-CODEX-POST 403' "$RESULT.log")" == 1 ]] \
  || fail 'the forwarder must refuse the request over the cap'
[[ "$(git --git-dir="$REMOTE" rev-parse main)" == "$remote_before" ]] || fail 'a capped codex run delivers nothing'
grep -q 'request' "$RESULT" || fail 'a capped run must say so'
echo 'PASS: harness-headless maps codex no_changes, failure and the request cap'

# --- Seatbelt: what the codex process tree can and cannot reach ------------------------
mkdir -p "$FAKE_HOME/.ssh" "$FAKE_HOME/.hermes" "$FAKE_HOME/buzz" "$FAKE_HOME/.codex" "$FAKE_HOME/.claude" \
  "$FAKE_HOME/.config/gh" "$FAKE_HOME/Library/Keychains"
for f in .ssh/id_fixture .hermes/state.json buzz/token .codex/auth.json .claude/.credentials.json .config/gh/hosts.yml \
    Library/Keychains/login.keychain-db .zsh_history; do
  printf 'secret-%s\n' fixture > "$FAKE_HOME/$f"
done
other="$(ls "$STATE/sessions" | grep -v "^$sid\$" | head -n 1)"
# A terminal the user has open: a PTY made outside the sandbox.
python3 -c 'import os, pty, time
m, s = pty.openpty()
print(os.ttyname(s), flush=True); time.sleep(600)' > "$TMP/outside-tty" &
tty_holder=$!
for _ in $(seq 50); do [[ -s "$TMP/outside-tty" ]] && break; sleep 0.1; done
python3 -c 'import socket, time
s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen()
print(s.getsockname()[1], flush=True); time.sleep(600)' > "$TMP/other-port" &
other_listener=$!
for _ in $(seq 50); do [[ -s "$TMP/other-port" ]] && break; sleep 0.1; done
printf 'OTHER_RECORD=%s\nOTHER_ROOT=%s\nSOURCE_FILE=%s\nUP_PORT=%s\nOTHER_PORT=%s\nOUTSIDE_PID=%s\nOUTSIDE_DIR=%s\nKEY_FILE=%s\nSTATE_SESSIONS=%s\nOUTSIDE_TTY=%s\n' \
  "$STATE/sessions/$other" "$STATE/worktrees/$other" "$SOURCE/tracked.txt" "$UP_PORT" "$(cat "$TMP/other-port")" \
  "$other_listener" "$TMP/outside" "$KEY_FILE" "$STATE/sessions" "$(cat "$TMP/outside-tty")" > "$STUB/codex-probe.env"
cat > "$STUB/codex-probe.sh" <<'EOF'
#!/bin/bash
# Probes from inside the agent sandbox, one `PROBE <name> <ALLOWED|errno>` each.
set -a; source "$(dirname "$0")/codex-probe.env"; set +a
python3 - <<'PY'
import errno, http.client, os, re, socket, subprocess
E, home, root = os.environ, os.environ['HOME'], os.getcwd()
def probe(name, f):
    try:
        f()
        print('PROBE', name, 'ALLOWED')
    except socket.timeout:
        print('PROBE', name, 'TIMEOUT')
    except OSError as e:
        print('PROBE', name, errno.errorcode.get(e.errno, e.errno))
def read(path):
    return lambda: open(path, 'rb').read()
def write(path):
    def f():
        open(path, 'w').write('x')
        os.unlink(path)
    return f
def child(command):
    def f():
        if subprocess.run(['/bin/bash', '-c', command], stderr=subprocess.DEVNULL).returncode:
            raise OSError(errno.EPERM, command)
    return f
def connect(port, host='127.0.0.1', timeout=5):
    return lambda: socket.create_connection((host, port), timeout=timeout).close()
config = open(E['CODEX_HOME'] + '/config.toml').read()
FORWARDER = int(re.search(r'127\.0\.0\.1:(\d+)/v1', config).group(1))
def models():
    token = re.search(r'"X-Harness-Forwarder-Token" = "([0-9a-f]+)"', config).group(1)
    c = http.client.HTTPConnection('127.0.0.1', FORWARDER, timeout=10)
    c.request('GET', '/v1/models', headers={'X-Harness-Forwarder-Token': token})
    if c.getresponse().status != 200:
        raise OSError(errno.EACCES, 'models')
def own_pty():
    import pty
    m, s = pty.openpty()
    os.ttyname(s)
    os.write(s, b'x\n')
    if os.read(m, 8) != b'x\r\n':
        raise OSError(errno.EIO, 'pty')
def procargs(pid):
    def f():
        import ctypes, ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
        mib, size = (ctypes.c_int * 3)(1, 49, pid), ctypes.c_size_t(65536)
        buf = ctypes.create_string_buffer(65536)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            raise OSError(ctypes.get_errno() or errno.EPERM, 'kern.procargs2')
    return f
def procpid(pid):
    def f():
        import ctypes, ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
        mib, size = (ctypes.c_int * 4)(1, 14, 1, pid), ctypes.c_size_t(4096)
        buf = ctypes.create_string_buffer(4096)
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value == 0:
            raise OSError(ctypes.get_errno() or errno.EPERM, 'kern.proc.pid')
    return f
def replace_config():
    tmp = os.path.join(E['CODEX_HOME'], 'config.new')
    open(tmp, 'w').write('x')
    try:
        os.replace(tmp, os.path.join(E['CODEX_HOME'], 'config.toml'))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
probe('read-root', read(os.path.join(root, 'tracked.txt')))
probe('read-system', read('/usr/bin/true'))
probe('read-codex-home', read(E['CODEX_HOME'] + '/AGENTS.md'))
# names and fixture paths kept apart: a (name, credential path) literal reads as a secret to scanners
names = 'ssh hermes buzz codex claude gh keychain history'.split()
paths = ['.ssh/id_fixture', '.hermes/state.json', 'buzz/token', '.codex/auth.json', '.claude/.credentials.json',
         '.config/gh/hosts.yml', 'Library/Keychains/login.keychain-db', '.zsh_history']
assert len(names) == len(paths)
for name, path in zip(names, paths):
    probe('read-' + name, read(os.path.join(home, path)))
probe('read-key', read(E['KEY_FILE']))
probe('read-other-record', read(os.path.join(E['OTHER_RECORD'], 'journal')))
probe('read-other-root', read(os.path.join(E['OTHER_ROOT'], 'tracked.txt')))
probe('list-sessions', lambda: os.listdir(E['STATE_SESSIONS']))
probe('read-source', read(E['SOURCE_FILE']))
probe('write-root', write(os.path.join(root, 'probe-write.txt')))
probe('write-tmp', write(os.path.join(E['TMPDIR'], 'probe-write.txt')))
probe('write-outside', write(os.path.join(E['OUTSIDE_DIR'], 'probe-write.txt')))
probe('write-home', write(os.path.join(home, 'probe-write.txt')))
probe('write-codex-config', lambda: open(E['CODEX_HOME'] + '/config.toml', 'a').close())
probe('write-codex-agents', lambda: open(E['CODEX_HOME'] + '/AGENTS.md', 'a').close())
probe('child-write-root', child('printf x > child-write.txt && rm child-write.txt'))
probe('child-write-outside', child('printf x > "$OUTSIDE_DIR/child-write.txt"'))
probe('child-read-ssh', child('cat "$HOME/.ssh/id_fixture" > /dev/null'))
probe('net-external', connect(443, '1.1.1.1'))
probe('net-upstream-direct', connect(int(E['UP_PORT'])))
probe('net-other-loopback', connect(int(E['OTHER_PORT'])))
probe('net-forwarder', models)
probe('signal-outside', lambda: os.kill(int(E['OUTSIDE_PID']), 0))
probe('own-pty', own_pty)
probe('outside-pty', lambda: os.close(os.open(E['OUTSIDE_TTY'], os.O_RDONLY | os.O_NOCTTY)))
probe('read-dev-disk', read('/dev/disk0'))
probe('net-forwarder-v6', connect(FORWARDER, '::1', 2))
probe('rename-codex-home', lambda: os.rename(E['CODEX_HOME'], E['CODEX_HOME'] + '.x'))
probe('rename-codex-config', lambda: os.rename(E['CODEX_HOME'] + '/config.toml', E['CODEX_HOME'] + '/x.toml'))
probe('replace-codex-config', replace_config)
probe('procargs-launcher', procargs(int(E['LAUNCHER_PID'])))
probe('procpid-launcher', procpid(int(E['LAUNCHER_PID'])))
# Control: the same reads of the probe's own child, inside the sandbox.
own = subprocess.Popen(['/bin/sleep', '30'])
probe('procargs-own-child', procargs(own.pid))
probe('procpid-own-child', procpid(own.pid))
own.kill()
probe('exec-code-mode-host', child('"$CODEX_DIR/codex-code-mode-host" --help > /dev/null'))
probe('exec-codex-sibling', child('"$CODEX_DIR/codex-badversion" --help'))
probe('read-codex-sibling', read(os.path.join(E['CODEX_DIR'], 'codex-badversion')))
PY
EOF
codex_prompt probe
# PATH directories are readable in the profile; these sit inside denied trees,
# so only the denies placed after every allow keep them closed.
EXTRA_PATH="$FAKE_HOME/.hermes:$FAKE_HOME/.config/harness-launcher:$STATE/sessions:$FAKE_HOME/.ssh:$FAKE_HOME/.codex:$FAKE_HOME/.claude:$FAKE_HOME/.config/gh" \
  headless "${CODEX_ARGS[@]}" || fail 'codex probe run must exit 0'
kill "$other_listener" "$tty_holder" 2>/dev/null || true
expect_status no_changes
for allowed in read-root read-system read-codex-home write-root write-tmp child-write-root net-forwarder own-pty exec-code-mode-host \
    procargs-own-child procpid-own-child; do
  grep -qx "PROBE $allowed ALLOWED" "$RESULT.log" || fail "the agent sandbox must allow: $allowed"
done
for denied in read-ssh read-hermes read-buzz read-codex read-claude read-gh read-keychain read-history read-key \
    read-other-record read-other-root list-sessions read-source write-outside write-home write-codex-config \
    write-codex-agents child-write-outside child-read-ssh net-external net-upstream-direct net-other-loopback signal-outside \
    outside-pty read-dev-disk rename-codex-home rename-codex-config replace-codex-config procargs-launcher procpid-launcher \
    exec-codex-sibling read-codex-sibling; do
  grep -qx "PROBE $denied EPERM" "$RESULT.log" || fail "the agent sandbox must deny: $denied"
done
# ::1 at the forwarder port: the forwarder's own bound, never-listening socket
# (a connect times out or is refused), or a sandbox deny.
grep -qx -e 'PROBE net-forwarder-v6 EPERM' -e 'PROBE net-forwarder-v6 ECONNREFUSED' -e 'PROBE net-forwarder-v6 TIMEOUT' "$RESULT.log" \
  || fail 'the agent must not reach ::1 at the forwarder port'
[[ ! -e "$TMP/outside/probe-write.txt" && ! -e "$TMP/outside/child-write.txt" && ! -e "$FAKE_HOME/probe-write.txt" ]] \
  || fail 'the agent wrote outside the session root'
! grep -q 'secret-fixture' "$RESULT.log" || fail 'a secret reached the run log'
echo 'PASS: the codex sandbox denies secrets, other sessions, outside writes and every network but the forwarder'

# --- timeout ---------------------------------------------------------------------------
codex_prompt hang
TIMEOUT_MIN=0.1 headless "${CODEX_ARGS[@]}" || fail 'codex timeout run must exit 0'
expect_status timeout
grandchild="$(sed -n 's/^FAKE-CODEX-GRANDCHILD //p' "$RESULT.log")"
[[ -n "$grandchild" ]] && ! kill -0 "$grandchild" 2>/dev/null || fail 'a codex timeout must kill the process tree'
! grep -qx state=OPEN "$STATE/sessions/$(field session_id)/journal" || fail 'a timed-out codex session must be finished'
python3 - "$STATE/sessions/$(field session_id)/runtime.lock" <<'PY' || fail 'the session lease must be released'
import fcntl, sys
with open(sys.argv[1], 'r+') as f:
    fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
PY
codex_prompt hang
rm -f "$RESULT" "$RESULT.log"
HEADLESS_EXEC=1 headless "${CODEX_ARGS[@]}" > /dev/null 2>&1 &
runner=$!
for _ in $(seq 1 200); do grep -q '^FAKE-CODEX-GRANDCHILD' "$RESULT.log" 2>/dev/null && break; sleep 0.05; done
for _ in $(seq 1 200); do grep -q '^FAKE-CODEX-SETSID' "$RESULT.log" 2>/dev/null && break; sleep 0.05; done
grandchild="$(sed -n 's/^FAKE-CODEX-GRANDCHILD //p' "$RESULT.log")"
setsid_child="$(sed -n 's/^FAKE-CODEX-SETSID //p' "$RESULT.log")"
[[ -n "$grandchild" && -n "$setsid_child" ]] || fail 'codex hang run did not start'
term_sid="$(sed -n 's/^harness-launcher: isolated session \([0-9A-F-]*\);.*/\1/p' "$RESULT.log")"
python3 - "$STATE/sessions/$term_sid/runtime.lock" <<'PY' || fail 'the session lease must be held while codex runs'
import fcntl, sys
with open(sys.argv[1], 'r+') as f:
    try:
        fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(0)
raise SystemExit(1)
PY
kill -TERM "$runner"
wait "$runner" || true
expect_status failed
! kill -0 "$grandchild" 2>/dev/null || fail 'SIGTERM must kill the codex process tree'
! kill -0 "$setsid_child" 2>/dev/null || fail 'SIGTERM must kill a process that left the codex process group'
[[ -n "$term_sid" ]] && ! grep -qx state=OPEN "$STATE/sessions/$term_sid/journal" || fail 'SIGTERM must finish the codex session'
echo 'PASS: harness-headless --agent codex stops the process tree on timeout or SIGTERM and finishes the session'

# --- rules: a symlinked rule is refused, never read outside the sandbox ----------------
python3 - "$ROOT/bin" "$TMP/rules-root" "$FAKE_HOME/.ssh/id_fixture" <<'PY' || fail 'codex AGENTS.md rules'
import os, sys
sys.path.insert(0, sys.argv[1])
import harness_headless as h
root, secret = sys.argv[2], sys.argv[3]
rules = os.path.join(root, '.claude', 'rules')
os.makedirs(rules)
open(os.path.join(rules, 'b.md'), 'w').write('B\n')
open(os.path.join(rules, 'a.md'), 'w').write('A\n')
open(os.path.join(rules, '_index.md'), 'w').write('I\n')
text = h.codex_agents_md(root)
assert text.index('## a\n\nA\n') < text.index('## b\n\nB\n') and 'I\n' not in text.split('\n', 3)[3], text
os.symlink(secret, os.path.join(rules, 'c.md'))
try:
    h.codex_agents_md(root)
except h.Refused:
    pass
else:
    raise SystemExit('a symlinked rule was read')
PY
echo 'PASS: CODEX_HOME/AGENTS.md concatenates regular rule files and refuses a symlink'

# --- R4: the real codex binary under the generated profile (no model cost) ------------
# A codex install with codex-code-mode-host beside it (the npm vendor build
# ships one; a standalone ~/.local/bin/codex may not).
# HARNESS_TEST_REAL_CODEX wins, then the newest such install: an older
# catalog does not know gpt-6.1-sol.
REAL_CODEX="$(python3 - "${HARNESS_TEST_REAL_CODEX:-}" \
  "$HOME"/.local/share/mise/installs/node/*/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex \
  "$HOME/.local/bin/codex" <<'PY'
import os, re, subprocess, sys
found = []
for path in sys.argv[1:]:
    if not (path and os.path.isfile(path) and os.access(path, os.X_OK)
            and os.access(os.path.join(os.path.dirname(path), 'codex-code-mode-host'), os.X_OK)):
        continue
    out = subprocess.run([path, '--version'], capture_output=True, text=True).stdout
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', out)
    if match:
        found.append((path == sys.argv[1], tuple(map(int, match.groups())), path))
print(max(found)[2] if found else '')
PY
)"
if [[ -z "$REAL_CODEX" ]]; then
  echo 'SKIP: no real codex with codex-code-mode-host beside it (set HARNESS_TEST_REAL_CODEX); the R4 real-binary sandbox test did not run'
else
  # Login-shell dotfiles exist; inside the sandbox they are unreadable and the
  # shell still runs the command.
  for f in .zshenv .zprofile .zshrc .bash_profile; do printf 'export DOTFILE_SEEN=%s\n' "$f" > "$FAKE_HOME/$f"; done
  printf '%s\n' "printf 'real %s\n' ok > real-codex.txt && echo WRITE-OK; cat '$FAKE_HOME/.ssh/id_fixture' && echo READ-ALLOWED || echo READ-DENIED; printf x > '$TMP/outside/real-codex' && echo OUT-ALLOWED || echo OUT-DENIED; echo \"DOTFILE=\${DOTFILE_SEEN:-none}\"" > "$UP/command"
  printf 'real codex done\n' > "$UP/final"
  printf '*** Begin Patch\n*** Add File: patched.txt\n+patched by apply_patch\n*** End Patch\n' > "$UP/patch"
  printf '%s\n' 'Run the command.' > "$PROMPT"
  up_before="$(wc -l < "$UP/requests.jsonl" | tr -d ' ')"
  # The production model name: codex uses its real tool set (apply_patch,
  # exec_command) and request headers for it.
  printf 'gpt-6.1-sol\n' > "$UP/model"
  HARNESS_CODEX_BIN="$REAL_CODEX" TIMEOUT_MIN=3 headless --agent codex --model gpt-6.1-sol --model-endpoint "$ENDPOINT" \
    --endpoint-key-file "$KEY_FILE" --effort low || fail 'real codex run must exit 0'
  expect_status delivered
  git --git-dir="$REMOTE" show main:real-codex.txt >/dev/null || fail 'the real codex write inside the session root must be delivered'
  [[ ! -e "$TMP/outside/real-codex" ]] || fail 'the real codex wrote outside the session root'
  [[ "$(field summary)" == 'real codex done' ]] || fail 'the summary must be the real codex -o file'
  python3 - "$UP/requests.jsonl" "$up_before" "$KEY" "$RESULT" <<'PY' || fail 'real codex tool output'
import json, sys
lines = [json.loads(l) for l in open(sys.argv[1]).read().splitlines()[int(sys.argv[2]):]]
posts = [l for l in lines if l['method'] == 'POST']
assert posts and all(p['authorization'] == 'Bearer ' + sys.argv[3] and p['model'] == 'gpt-6.1-sol' for p in posts), 'forwarded'
# Everything codex 0.160 sends passes the header allowlist; nothing else does.
sent = set().union(*(p['headers'] for p in posts))
assert {'originator', 'session-id', 'x-codex-turn-metadata', 'x-openai-internal-codex-responses-lite'} <= sent, sent
assert 'x-harness-forwarder-token' not in sent, sent
out = '\n'.join(o for p in posts for o in p['outputs'])
assert 'Success. Updated the following files' in out, out
assert 'WRITE-OK' in out and 'READ-DENIED' in out and 'OUT-DENIED' in out and 'DOTFILE=none' in out, out
assert 'READ-ALLOWED' not in out and 'secret-fixture' not in out, out
result = json.load(open(sys.argv[4]))
assert result['usage']['input_tokens'] > 0 and result['usage']['output_tokens'] > 0 and result['num_turns'] == 1, result
PY
  [[ "$(git --git-dir="$REMOTE" show main:patched.txt)" == 'patched by apply_patch' ]] || fail 'the real codex apply_patch edit must be delivered'
  echo "PASS: the real codex ($("$REAL_CODEX" --version 2>/dev/null)) runs under the generated profile: apply_patch and in-root write allowed, secret read and outside write denied"
fi
