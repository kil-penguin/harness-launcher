#!/usr/bin/env bash
# harness-headless --target: a codex run on a registered personal code
# repository, delivered as a draft PR by the broker (B1g, launcher 0.48.0).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
TMP="$(cd "$TMP" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT
PREFIX="$TMP/prefix"
STUB="$TMP/stub"
SOURCE="$TMP/harness"
HARNESS_REMOTE="$TMP/harness-remote.git"
STATE="$TMP/state"
FAKE_HOME="$TMP/home"
# The production layout: the profile home is ~/.config/harness-launcher.
PROFILES="$FAKE_HOME/.config/harness-launcher"
# Every helper below, Python ones included, must see the fake profile home and
# never the real ~/.config/harness-launcher.
export HARNESS_PROFILE_HOME="$PROFILES" XDG_CONFIG_HOME="$FAKE_HOME/.config" PYTHONDONTWRITEBYTECODE=1
REAL_HOME="$HOME"
real_targets_before="$(ls -ld "$REAL_HOME/.config/harness-launcher/targets" "$REAL_HOME/.config/harness-launcher/target-policy.json" 2>&1 || true)"
RESULT="$TMP/out/result.json"
LOCK="$TMP/run.lock"
PROMPT="$TMP/prompt.txt"
GHD="$TMP/gh"
PY3="$(command -v python3)"
# Use the fixture-selected Git inside the clean agent PATH, not Apple's shim.
GIT_BIN="$(command -v git)"
NAME=example-repo
GITHUB=example-owner/example-repo
TARGET_REMOTE="$TMP/remotes/$GITHUB.git"
LIVE="$FAKE_HOME/dev/example-repo"
# A token-shaped sentinel the fake gh hands out; it must appear nowhere.
SENTINEL="gho_SENTINEL$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
mkdir -p "$STUB" "$FAKE_HOME" "$PROFILES/profiles" "$TMP/out" "$SOURCE/config" "$SOURCE/.claude/rules" "$GHD" \
  "$TMP/remotes/example-owner"

bash "$ROOT/test/lib/install-runtime-fixture.sh" "$ROOT" "$PREFIX"
# The owner's host policy (placeholders): account, ssh aliases, deny lists.
POLICY='{"owner":"example-owner","ssh_hosts":["github.com-example","github.com-example2"],"deny_repos":["example-owner/example-harness","example-owner/harness-launcher","other-org/*"],"home_deny":[".agent-state","company-*","company-code","mirrors"]}'
put_policy() { (umask 077 && printf '%s\n' "$1" > "$PROFILES/target-policy.json"); }
field() { python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print("null" if v is None else v)' "$RESULT" "$1"; }
fail() {
  echo "FAIL: $*" >&2
  [[ -f "$RESULT" ]] && { sed 's/^/  /' "$RESULT"; echo; } >&2
  [[ -f "$RESULT.log" ]] && { echo "  --- $RESULT.log" >&2; sed 's/^/  | /' "$RESULT.log" >&2; }
  exit 1
}
expect_status() { [[ "$(field status)" == "$1" && "$(field version)" == 1 ]] || fail "expected status $1 (version 1)"; }

# --- digest vectors: canonical JSON and sha256 shared with the harness loop --------------
python3 - "$ROOT/bin" "$ROOT/test/fixtures/target-digest-vectors.json" <<'PY' || fail 'digest vectors'
import hashlib, json, subprocess, sys
sys.path.insert(0, sys.argv[1])
import harness_target as t
vectors = json.load(open(sys.argv[2]))['vectors']
assert len(vectors) >= 2
for v in vectors:
    assert t.canonical(v['entry']) == v['canonical'].encode(), v['name']
    assert t.digest(v['entry']) == v['sha256'] == hashlib.sha256(v['canonical'].encode()).hexdigest(), v['name']
    # Independent of Python: the system shasum over the same bytes.
    out = subprocess.run(['shasum', '-a', '256'], input=v['canonical'].encode(), capture_output=True).stdout
    assert out.split()[0].decode() == v['sha256'], v['name']
    if 'yaml' in v:
        assert t.entry_from_targets(t.parse_yaml_text(v['yaml']), v['name']) == v['entry'], v['name']
# `~` is never expanded and non-ASCII stays literal.
assert '~/fixtures' in vectors[1]['canonical'] and '한글' in vectors[1]['canonical']
# Without PyYAML the system Ruby's safe_load reads the same entry.
sys.modules['yaml'] = None
v = vectors[0]
assert t.entry_from_targets(t.parse_yaml_text(v['yaml']), v['name']) == v['entry'], 'ruby YAML path'
PY
echo 'PASS: target digest vectors (canonical JSON, sha256, YAML entry) match'

# --- fixtures: the harness, the target remote (default branch trunk), fakes -------
git init -q --bare -b main "$HARNESS_REMOTE"
git -C "$SOURCE" init -q -b main
printf '%s\n' 'HARNESS_NAME="headless"' 'HARNESS_PREFIX="hh"' > "$SOURCE/config/launcher.env"
for rule in work-scale outcome-control performance-observation; do
  printf '# %s\n\nRULE-%s-ORIGIN-MAIN\n' "$rule" "$rule" > "$SOURCE/.claude/rules/$rule.md"
done
printf '# other\n\nRULE-OTHER-NOT-COPIED\n' > "$SOURCE/.claude/rules/rag-integration.md"
printf 'INDEX-NOT-COPIED\n' > "$SOURCE/.claude/rules/_index.md"
printf 'harness tracked\n' > "$SOURCE/tracked.txt"
git -C "$SOURCE" add . && git -C "$SOURCE" -c user.name=t -c user.email=t@example.invalid commit -qm initial
git -C "$SOURCE" remote add origin "$HARNESS_REMOTE"
git -C "$SOURCE" push -q origin main
# The working tree differs from origin/main: AGENTS.md must come from origin/main.
printf '# work-scale\n\nRULE-WORKTREE-NOT-USED\n' > "$SOURCE/.claude/rules/work-scale.md"
printf '%s\n' "$SOURCE" > "$PROFILES/profiles/hh"

git init -q --bare -b trunk "$TARGET_REMOTE"
seed="$TMP/seed"
git init -q -b trunk "$seed"
cat > "$seed/pyproject.toml" <<'EOF'
[project]
name = "example-repo"
version = "0.1.0"
requires-python = ">=3.9"
dependencies = ["fastapi>=0.110"]

[project.optional-dependencies]
mcp = ["mcp>=1.0"]

[dependency-groups]
dev = ["pytest==9.1.1"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.uv]
package = false

[tool.ruff]
line-length = 100
EOF
printf 'version = 1\n# lock fixture\n' > "$seed/uv.lock"
printf 'def answer():\n    return 42\n' > "$seed/app.py"
# The repository's tests: fail when the candidate carries a FAIL marker, and
# report the verifier environment in the run log.
cat > "$seed/run_tests.py" <<'EOF'
import os, sys
print('VERIFIER-PYTHON', sys.executable, flush=True)
for k, v in sorted(os.environ.items()):
    print('VERIFIER-ENV', k + '=' + v, flush=True)
try:
    open(os.path.join(sys.prefix, 'verifier-write'), 'w').close()
    print('VERIFIER-VENV-WRITE ALLOWED', flush=True)
except OSError:
    print('VERIFIER-VENV-WRITE blocked', flush=True)
if os.path.exists('FAIL'):
    print('VERIFIER-TESTS FAILED', flush=True)
    sys.exit(1)
if os.path.exists('HANG'):
    import time
    time.sleep(600)
print('VERIFIER-TESTS OK', flush=True)
EOF
git -C "$seed" add . && git -C "$seed" -c user.name=t -c user.email=t@example.invalid commit -qm seed
git -C "$seed" push -q "$TARGET_REMOTE" trunk
BASE_SHA="$(git --git-dir="$TARGET_REMOTE" rev-parse trunk)"
# The owner's live checkout: never touched by a run, denied to the agent.
git clone -q "$TARGET_REMOTE" "$LIVE"
printf 'local work in progress\n' > "$LIVE/wip.txt"
live_snapshot() { (cd "$LIVE" && find . -type f -print0 | sort -z | xargs -0 shasum -a 256; git -C "$LIVE" status --porcelain=v1 --ignored) | shasum -a 256; }
LIVE_BEFORE="$(live_snapshot)"

# Fake ssh: `-T` prints GitHub's greeting for the account in $TMP/ssh-account;
# otherwise it runs git's upload/receive-pack against $TMP/remotes/<path>.
printf 'example-owner\n' > "$TMP/ssh-account"
cat > "$STUB/ssh" <<EOF
#!/bin/bash
printf '%s\n' "\$*" >> "$TMP/ssh-log"
for a in "\$@"; do
  if [[ "\$a" == -T ]]; then
    printf 'Hi %s! You'"'"'ve successfully authenticated, but GitHub does not provide shell access.\n' "\$(cat "$TMP/ssh-account")" >&2
    exit 1
  fi
done
cmd="\${@: -1}"
verb="\${cmd%% *}"; path="\${cmd#* }"; path="\${path//\\'/}"
case "\$verb" in git-upload-pack|git-receive-pack) ;; *) exit 64 ;; esac
exec git "\${verb#git-}" "$TMP/remotes/\$path"
EOF
# Fake gh: records argv and only whether GH_TOKEN was present; serves JSON
# from control files in $GHD.
cat > "$STUB/gh" <<EOF
#!$PY3
import json, os, sys, time
D = '$GHD'
a = sys.argv[1:]
def ctl(name, default=None):
    try:
        return open(os.path.join(D, name)).read().strip()
    except OSError:
        return default
with open(os.path.join(D, 'log.jsonl'), 'a') as f:
    f.write(json.dumps({'argv': a, 'token': 'GH_TOKEN' in os.environ, 'cwd': os.getcwd(),
                        'cwd_entries': sorted(os.listdir('.'))}) + '\n')
if 'GH_TOKEN' in os.environ and os.environ['GH_TOKEN'] != '$SENTINEL':
    sys.exit(90)
def out(value):
    print(json.dumps(value))
    sys.exit(0)
prs = json.loads(ctl('prs', '[]'))
if a == ['auth', 'token', '--user', 'example-owner']:
    if ctl('token-fail') is not None:
        print('no oauth token found for github.com', file=sys.stderr)
        sys.exit(1)
    print('$SENTINEL')
    sys.exit(0)
if 'GH_TOKEN' not in os.environ:
    sys.exit(91)
if a == ['api', 'user']:
    out({'login': ctl('login', 'example-owner')})
if a[:1] == ['api'] and a[1].endswith('/actions/permissions'):
    out({'enabled': ctl('actions-enabled', 'false') == 'true', 'allowed_actions': 'all'})
if a[:1] == ['api'] and a[1].endswith('/actions/permissions/workflow'):
    out({'default_workflow_permissions': ctl('workflow', 'read'), 'can_approve_pull_request_reviews': False})
if a[:1] == ['api'] and a[1].endswith('/actions/secrets'):
    out({'total_count': int(ctl('secrets', '0')), 'secrets': []})
if a[:1] == ['api'] and a[1].startswith('repos/'):
    out({'full_name': a[1][6:], 'private': ctl('private', 'true') == 'true', 'fork': ctl('fork', 'false') == 'true'})
if a[:2] == ['pr', 'list']:
    head = a[a.index('--head') + 1]
    out([{'url': p['url']} for p in prs if p['head'] == head])
if a[:2] == ['pr', 'create']:
    if ctl('create-fail') is not None:
        sys.exit(1)
    head = a[a.index('--head') + 1]
    n = len(prs) + 1
    url = 'https://github.com/' + a[a.index('--repo') + 1] + '/pull/%d' % n
    body = open(a[a.index('--body-file') + 1]).read()
    open(os.path.join(D, 'body-%d' % n), 'w').write(body)
    prs.append({'head': head, 'url': url})
    open(os.path.join(D, 'prs'), 'w').write(json.dumps(prs))
    if ctl('create-hang') is not None:
        time.sleep(30)
    print(url)
    sys.exit(0)
sys.exit(64)
EOF
# Fake uv: records its call, then makes a plain venv (no pip) at
# UV_PROJECT_ENVIRONMENT from this test's python.
cat > "$STUB/uv" <<EOF
#!/bin/bash
printf 'argv=%s\ncwd=%s\nenv=%s\nfiles=%s\n' "\$*" "\$PWD" "\${UV_PROJECT_ENVIRONMENT:-}" "\$(ls "\$PWD" | tr '\n' ' ')" > "$TMP/uv-call"
[[ ! -e "$TMP/uv-fail" ]] || exit 2
if [[ -e "$TMP/uv-needs-build" ]]; then
  echo 'error: Distribution \`legacy==1.0 @ registry+https://pypi.org/simple\` can'"'"'t be installed because it is marked as \`--no-build\` but has no binary distribution' >&2
  exit 2
fi
exec "$PY3" -m venv --without-pip "\$UV_PROJECT_ENVIRONMENT"
EOF
chmod +x "$STUB/ssh" "$STUB/gh" "$STUB/uv"

TARGETS_YAML="$TMP/targets.yaml"
cat > "$TARGETS_YAML" <<EOF
$NAME:
  github: $GITHUB
  remote: git@github.com-example:$GITHUB.git
  base: trunk
  path: $FAKE_HOME/dev/./example-repo
  about: "example repository"
  test:
    group: dev
    run: [python, run_tests.py]
    timeout_min: 1
EOF
CLEAN_ENV=(env -i PATH="$STUB:/usr/bin:/bin:/usr/sbin:/sbin" HOME="$FAKE_HOME" TMPDIR="$TMP" HARNESS_PROFILE_HOME="$PROFILES")

# --- harness-profile target add: TTY, entry and diff shown, retyped name ----------
# Without the owner's policy file nothing is accepted (fail closed).
if "${CLEAN_ENV[@]}" "$PREFIX/bin/harness-profile" target add "$NAME" --from "$TARGETS_YAML" < /dev/null > "$TMP/add.out" 2>&1; then
  fail 'target add without a TTY must fail'
fi
grep -q 'needs a terminal' "$TMP/add.out" && grep -q 'not a security boundary' "$TMP/add.out" || { cat "$TMP/add.out"; fail 'target add must name the TTY requirement and its nature'; }
[[ ! -e "$PROFILES/targets/$NAME.json" ]] || fail 'target add without a TTY wrote a record'
# ptyrun.py <answer> <command...>: run with a PTY as stdin/stdout, type answer.
cat > "$TMP/ptyrun.py" <<'PY'
import os, pty, sys, time
answer = sys.argv[1].encode() + b'\n'
pid, fd = pty.fork()
if pid == 0:
    os.execvp(sys.argv[2], sys.argv[2:])
out, sent = b'', False
while True:
    try:
        chunk = os.read(fd, 4096)
    except OSError:
        break
    if not chunk:
        break
    out += chunk
    if not sent and b'Retype the target name' in out:
        os.write(fd, answer)
        sent = True
sys.stdout.write(out.decode('utf-8', 'replace'))
raise SystemExit(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
PY
"${CLEAN_ENV[@]}" "$PY3" "$TMP/ptyrun.py" "$NAME" "$PREFIX/bin/harness-profile" target add "$NAME" --from "$TARGETS_YAML" > "$TMP/add.out" 2>&1 \
  && fail 'target add without a policy file must be refused'
grep -q 'target-policy.json' "$TMP/add.out" && [[ ! -e "$PROFILES/targets/$NAME.json" ]] || { cat "$TMP/add.out"; fail 'a missing policy must be named'; }
put_policy "$POLICY"
chmod 644 "$PROFILES/target-policy.json"
"${CLEAN_ENV[@]}" "$PY3" "$TMP/ptyrun.py" "$NAME" "$PREFIX/bin/harness-profile" target add "$NAME" --from "$TARGETS_YAML" > "$TMP/add.out" 2>&1 \
  && fail 'a group-readable policy must be refused'
chmod 600 "$PROFILES/target-policy.json"
sed 's|  path: .*|  path: dev/example-repo|' "$TARGETS_YAML" > "$TMP/relative.yaml"
"${CLEAN_ENV[@]}" "$PY3" "$TMP/ptyrun.py" "$NAME" "$PREFIX/bin/harness-profile" target add "$NAME" --from "$TMP/relative.yaml" > "$TMP/add.out" 2>&1 \
  && fail 'a relative live path must be refused'
grep -q 'absolute' "$TMP/add.out" || { cat "$TMP/add.out"; fail 'a relative live path must be named'; }
if "${CLEAN_ENV[@]}" "$PY3" "$TMP/ptyrun.py" wrong-name "$PREFIX/bin/harness-profile" target add "$NAME" --from "$TARGETS_YAML" > "$TMP/add.out" 2>&1; then
  fail 'a mistyped name must not write'
fi
grep -q 'Not written' "$TMP/add.out" && [[ ! -e "$PROFILES/targets/$NAME.json" ]] || { cat "$TMP/add.out"; fail 'a mistyped name must not write'; }
"${CLEAN_ENV[@]}" "$PY3" "$TMP/ptyrun.py" "$NAME" "$PREFIX/bin/harness-profile" target add "$NAME" --from "$TARGETS_YAML" > "$TMP/add.out" 2>&1 \
  || { cat "$TMP/add.out"; fail 'target add with a TTY and the retyped name must write'; }
grep -q '(no existing record)' "$TMP/add.out" && grep -q '"base": "trunk"' "$TMP/add.out" || { cat "$TMP/add.out"; fail 'target add must show the entry'; }
for line in 'owner: example-owner' 'ssh_hosts: github.com-example, github.com-example2' \
    'deny_repos: example-owner/example-harness, example-owner/harness-launcher, other-org/*' \
    'home_deny: .agent-state, company-*, company-code, mirrors'; do
  grep -qF "$line" "$TMP/add.out" || { cat "$TMP/add.out"; fail "target add must show the policy: $line"; }
done
# A policy without home_deny entries is refused (like ssh_hosts).
put_policy "${POLICY/\".agent-state\",\"company-*\",\"company-code\",\"mirrors\"/}"
grep -q '"home_deny":\[\]' "$PROFILES/target-policy.json" || fail 'fixture: empty home_deny'
"${CLEAN_ENV[@]}" "$PREFIX/bin/harness-profile" target list > "$TMP/add.out" 2>&1 && fail 'an empty home_deny must be refused'
grep -q 'home_deny' "$TMP/add.out" || { cat "$TMP/add.out"; fail 'an empty home_deny must be named'; }
put_policy "$POLICY"
[[ "$(stat -f '%Lp %l' "$PROFILES/targets/$NAME.json")" == '600 1' && "$(stat -f '%Lp' "$PROFILES/targets")" == 700 ]] || fail 'record mode 0600 in a 0700 dir'
DIGEST="$(shasum -a 256 < "$PROFILES/targets/$NAME.json" | cut -d' ' -f1)"
python3 - "$ROOT/bin" "$PROFILES/targets/$NAME.json" "$DIGEST" "$LIVE" <<'PY' || fail 'the record must be the canonical JSON of the entry'
import json, sys
sys.path.insert(0, sys.argv[1])
import harness_target as t
data = open(sys.argv[2], 'rb').read()
entry = json.loads(data)
assert t.canonical(entry) == data and t.digest(entry) == sys.argv[3], data
assert entry == {'name': 'example-repo', 'github': 'example-owner/example-repo', 'base': 'trunk',
                 'remote': 'git@github.com-example:example-owner/example-repo.git',
                 'test': {'group': 'dev', 'run': ['python', 'run_tests.py'], 'timeout_min': 1}}, entry
assert open(sys.argv[2][:-5] + '.path').read() == sys.argv[4] + '\n'
PY
# A second add shows the diff against the current record.
sed -i '' 's/timeout_min: 1/timeout_min: 2/' "$TARGETS_YAML"
"${CLEAN_ENV[@]}" "$PY3" "$TMP/ptyrun.py" nope "$PREFIX/bin/harness-profile" target add "$NAME" --from "$TARGETS_YAML" > "$TMP/add.out" 2>&1 || true
grep -q '^-.*"timeout_min": 1' "$TMP/add.out" && grep -q '^+.*"timeout_min": 2' "$TMP/add.out" || { cat "$TMP/add.out"; fail 'target add must show the diff'; }
sed -i '' 's/timeout_min: 2/timeout_min: 1/' "$TARGETS_YAML"
[[ "$(shasum -a 256 < "$PROFILES/targets/$NAME.json" | cut -d' ' -f1)" == "$DIGEST" ]] || fail 'a declined add must keep the record'
# The boundary is checked before anything is shown.
printf 'someone\n' > "$TMP/ssh-account"
"${CLEAN_ENV[@]}" "$PY3" "$TMP/ptyrun.py" "$NAME" "$PREFIX/bin/harness-profile" target add "$NAME" --from "$TARGETS_YAML" > "$TMP/add.out" 2>&1 && fail 'a wrong ssh account must refuse target add'
grep -q 'authenticates as someone' "$TMP/add.out" || { cat "$TMP/add.out"; fail 'target add must name the ssh account'; }
printf 'example-owner\n' > "$TMP/ssh-account"
"${CLEAN_ENV[@]}" "$PREFIX/bin/harness-profile" target list | grep -q "^$NAME	$DIGEST	$GITHUB	trunk\$" || fail 'target list'
"${CLEAN_ENV[@]}" "$PREFIX/bin/harness-profile" target show "$NAME" | grep -q "^sha256 $DIGEST\$" || fail 'target show'
! grep -rqF "$SENTINEL" "$TMP/add.out" || fail 'target add printed the token'
echo 'PASS: harness-profile target add needs a TTY, shows the entry and diff, takes the retyped name and writes 0600'

# === harness-headless --target ======================================================
UP="$TMP/upstream"
mkdir -p "$UP"
printf 'stub-model\n' > "$UP/model"
UPSTREAM_LOG="$UP/responses-stub.log"
"$PY3" "$ROOT/test/fixtures/responses_stub.py" "$UP" >"$UPSTREAM_LOG" 2>&1 &
upstream=$!
trap 'kill "$upstream" 2>/dev/null; rm -rf "$TMP"' EXIT
for _ in $(seq 50); do [[ -s "$UP/port" ]] && break; sleep 0.1; done
if [[ ! -s "$UP/port" ]]; then
  if kill -0 "$upstream" 2>/dev/null; then
    fail "responses stub did not publish a port within 5 seconds (pid $upstream is still alive)"
  fi
  wait "$upstream" || true
  { echo 'responses stub exited before publishing a port'; sed 's/^/  | /' "$UPSTREAM_LOG"; } >&2
  fail 'responses stub readiness failed'
fi
ENDPOINT="http://127.0.0.1:$(cat "$UP/port")/v1"
KEY_FILE="$PROFILES/cliproxy-headless.key"
(umask 077 && printf 'sk-fixture-%s\n' "$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')" > "$KEY_FILE")
APPROVAL=0123456789abcdef0123456789abcdef01234567
target_args() {  # target_args <task-id> [digest]
  printf '%s\0' --agent codex --model "${MODEL:-stub-model}" --model-endpoint "$ENDPOINT" --endpoint-key-file "$KEY_FILE" \
    --target "${TNAME:-$NAME}" --target-digest "${2:-$DIGEST}" --task-id "$1" --approval-sha "$APPROVAL"
}
headless() {
  rm -f "$RESULT" "$RESULT.log"
  local -a args=()
  while IFS= read -r -d '' a; do args+=("$a"); done < <(target_args "$TASK" "${TDIGEST:-$DIGEST}")
  env -i PATH="${EXTRA_PATH:+$EXTRA_PATH:}$STUB:/usr/bin:/bin:/usr/sbin:/sbin" HOME="$FAKE_HOME" TMPDIR="$TMP" \
    HARNESS_PROFILE_HOME="$PROFILES" HARNESS_SESSION_STATE_HOME="$STATE" HARNESS_CODEX_BIN="${CODEX_BIN:-$TMP/vendor/codex}" \
    GH_TOKEN=leak-caller-gh GITHUB_TOKEN=leak-caller-github HARNESS_HEADLESS_GH_TIMEOUT="${GH_TIMEOUT:-30}" \
    "$PREFIX/bin/harness-headless" hh --prompt-file "$PROMPT" --result-file "$RESULT" --lock-file "$LOCK" \
    --budget-usd 2 --timeout-min "${TIMEOUT_MIN:-1}" "${args[@]}" "$@"
}
prompt() { printf 'Fix the answer.\nMODE=%s\n' "$1" > "$PROMPT"; }
branch() { printf 'loop/%s-%s' "$1" "${APPROVAL:0:8}"; }
remote_sha() { git --git-dir="$TARGET_REMOTE" rev-parse -q --verify "refs/heads/$1" 2>/dev/null || true; }
gh_calls() { [[ -f "$GHD/log.jsonl" ]] && wc -l < "$GHD/log.jsonl" | tr -d ' ' || echo 0; }

# The fake codex runs under the generated profile: it reports on stderr (the
# run log) and acts on MODE=<mode> from the prompt.
mkdir -p "$TMP/vendor"
cat > "$TMP/vendor/codex" <<'EOF'
#!/bin/bash
[[ "${1:-}" != --version ]] || { echo 'codex-cli 0.0.0-fake'; exit 0; }
[[ "$*" != 'features list' ]] || exit 0
[[ "${1:-}" == exec ]] || exit 64
args=("$@") out=""
for ((i = 0; i < ${#args[@]}; i++)); do [[ "${args[i]}" != -o ]] || out="${args[i+1]}"; done
{
  printf 'FAKE-CODEX-ARGV'; printf ' [%s]' "$@"; printf '\n'
  printf 'FAKE-CODEX-PWD %s\n' "$PWD"
  env | sed 's/^/FAKE-CODEX-ENV /'
  sed 's/^/FAKE-CODEX-AGENTS /' "$CODEX_HOME/AGENTS.md"
} >&2
prompt="$(cat)"
printf '%s\n' "$prompt" | sed 's/^/FAKE-CODEX-STDIN /' >&2
mode="$(printf '%s\n' "$prompt" | sed -n 's/^MODE=//p' | head -n 1)"
msg() { printf '%s\n' "$@" > "$HARNESS_COMMIT_MESSAGE_FILE"; }
edit() { printf 'def answer():\n    return %s\n' "$1" > app.py; }
case "$mode" in
  change) edit 43; msg 'feat: codex change' '' 'Written by the fake codex.' ;;
  none) ;;
  github) edit 44; mkdir -p .github/workflows; printf 'on: push\n' > .github/workflows/ci.yml; msg 'ci: add' ;;
  githubcase) edit 45; mkdir -p .GitHub; printf 'x\n' > .GitHub/notes.md; msg 'docs: add' ;;
  uvlock) edit 46; printf 'version = 1\n# lock fixture changed\n' > uv.lock; msg 'deps: lock' ;;
  pyproject) edit 47; sed -i '' 's/fastapi>=0.110/fastapi>=0.111/' pyproject.toml; msg 'deps: bump' ;;
  groups) edit 48; sed -i '' 's/pytest==9.1.1/pytest==9.1.2/' pyproject.toml; msg 'deps: dev' ;;
  optional) edit 49; sed -i '' 's/mcp>=1.0/mcp>=1.1/' pyproject.toml; msg 'deps: extra' ;;
  pyprojectok) edit 50; sed -i '' 's/line-length = 100/line-length = 99/' pyproject.toml; msg 'style: ruff' ;;
  secretdiff) edit 51; printf 'TOKEN_VALUE = "%s"\n' "gho_$(printf 'A%.0s' $(seq 36))" > leaked.py; msg 'feat: leak' ;;
  secretpat) edit 52; printf 'x = "%s"\n' "github_pat_$(printf 'B%.0s' $(seq 82))" > leaked.py; msg 'feat: leak' ;;
  secretkey) edit 53; printf -- '-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n' > id_fixture; msg 'feat: key' ;;
  secretmsg) edit 54; msg 'feat: message' '' "see ghp_$(printf 'C%.0s' $(seq 36))" ;;
  verifyfail) edit 55; : > FAIL; msg 'feat: breaks tests' ;;
  hooks)
    edit 56; msg 'feat: hooks'
    mkdir -p .git/hooks
    for h in pre-commit commit-msg post-commit pre-push reference-transaction post-checkout pre-auto-gc; do
      printf '#!/bin/sh\ntouch "%s/hook-ran-%s"\n' "@MARK@" "$h" > ".git/hooks/$h"; chmod +x ".git/hooks/$h"
    done
    printf '[core]\n\tfsmonitor = touch @MARK@/fsmonitor-ran\n\thooksPath = .git/hooks\n[filter "evil"]\n\tclean = touch @MARK@/filter-ran\n\tsmudge = touch @MARK@/filter-ran\n' >> .git/config
    printf '* filter=evil\n' > .gitattributes ;;
  probe) edit 57; msg 'feat: probe'; bash @STUB@/target-probe.sh >&2 ;;
  uvtoml) edit 60; printf '[pip]\nindex-url = "https://example.invalid/simple"\n' > uv.toml; msg 'deps: uv.toml' ;;
  pyversion) edit 61; printf '3.12\n' > .python-version; msg 'deps: python version' ;;
  requirements) edit 62; mkdir -p sub; printf 'requests\n' > sub/requirements-dev.txt; msg 'deps: requirements' ;;
  buildsys) edit 63; sed -i '' 's/hatchling.build/hatchling.other/' pyproject.toml; msg 'deps: build system' ;;
  reqpython) edit 64; sed -i '' 's/>=3.9/>=3.10/' pyproject.toml; msg 'deps: requires-python' ;;
  tooluv) edit 65; sed -i '' 's/package = false/package = true/' pyproject.toml; msg 'deps: tool.uv' ;;
  gitlink)
    edit 66; msg 'feat: vendor'
    (mkdir -p vendor/sub && cd vendor/sub && GIT_CONFIG_NOSYSTEM=1 "@GIT@" init -q && printf 'x\n' > f && GIT_CONFIG_NOSYSTEM=1 "@GIT@" add f \
      && GIT_CONFIG_NOSYSTEM=1 "@GIT@" -c user.name=t -c user.email=t@example.invalid commit -qm sub) || exit "$?" ;;
  gitmodules) edit 67; printf '[submodule "x"]\n\tpath = x\n\turl = https://example.invalid/x.git\n' > .gitmodules; msg 'feat: modules' ;;
  swaproot)
    # Replace the session root with a symlink to a sandbox-denied tree.
    edit 68; msg 'feat: swap'
    root="$PWD"; cd /
    rm -rf "$root" && ln -s "@LIVE@" "$root" && echo FAKE-CODEX-SWAPPED >&2 ;;
esac
echo '{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":2}}'
printf 'codex: %s' "$mode" > "$out"
EOF
sed -i '' "s|@STUB@|$STUB|; s|@MARK@|$TMP/marks|g; s|@LIVE@|$LIVE|; s|@GIT@|$GIT_BIN|g" "$TMP/vendor/codex"
mkdir -p "$TMP/marks"
printf '#!/bin/sh\n[ "$1" = --help ] || exit 64\necho FAKE-HOST-HELP\n' > "$TMP/vendor/codex-code-mode-host"
chmod +x "$TMP/vendor/codex" "$TMP/vendor/codex-code-mode-host"

# --- arguments: --target is codex-only and needs every companion option -------------
refuse() {  # refuse <why> [extra harness-headless args...]
  local why="$1"; shift
  local remote_before calls_before
  remote_before="$(git --git-dir="$TARGET_REMOTE" for-each-ref | shasum)"
  headless "$@" || fail "a refused run must write a result: $why"
  [[ "$(field status)" == refused && "$(field exit_code)" == 2 ]] || fail "must be refused: $why"
  ! grep -q '^FAKE-CODEX-ARGV' "$RESULT.log" 2>/dev/null || fail "a refused run started the agent: $why"
  [[ "$(git --git-dir="$TARGET_REMOTE" for-each-ref | shasum)" == "$remote_before" ]] || fail "a refused run changed the remote: $why"
  ! grep -qF "$SENTINEL" "$RESULT" "$RESULT.log" 2>/dev/null || fail "a refusal printed the token: $why"
}
prompt change
TASK=t_args
# claude with --target: the last --agent wins in argparse.
refuse '--target with --agent claude' --agent claude
grep -q 'codex' "$RESULT" || fail 'the claude refusal must say codex only'
rm -f "$RESULT"
env -i PATH="$STUB:/usr/bin:/bin" HOME="$FAKE_HOME" HARNESS_PROFILE_HOME="$PROFILES" HARNESS_SESSION_STATE_HOME="$STATE" \
  "$PREFIX/bin/harness-headless" hh --prompt-file "$PROMPT" --result-file "$RESULT" --lock-file "$LOCK" --budget-usd 1 --timeout-min 1 \
  --target "$NAME" --target-digest "$DIGEST" --task-id t_x --approval-sha "$APPROVAL" || fail 'claude + target must write a result'
[[ "$(field status)" == refused ]] && grep -q 'agent codex' "$RESULT" || fail '--target without --agent codex must be refused'
for bad in 'task-id t/x' 'task-id ""' 'approval-sha 0123' 'approval-sha XYZ0456789abcdef0123456789abcdef01234567' \
    'target-digest abc' "target $(printf '%s%s' K H)" "target $(printf '%s%s' k h)"; do
  rm -f "$RESULT"
  eval "set -- --$bad"
  headless "$@" || fail "bad $bad must write a result"
  [[ "$(field status)" == refused ]] || fail "bad --$bad must be refused"
done
for missing in --task-id --approval-sha --target-digest; do
  rm -f "$RESULT"
  local_args=()
  while IFS= read -r -d '' a; do local_args+=("$a"); done < <(target_args t_missing)
  for ((i = 0; i < ${#local_args[@]}; i++)); do
    [[ "${local_args[i]}" == "$missing" ]] && { unset 'local_args[i]' 'local_args[i+1]'; break; }
  done
  env -i PATH="$STUB:/usr/bin:/bin" HOME="$FAKE_HOME" HARNESS_PROFILE_HOME="$PROFILES" HARNESS_SESSION_STATE_HOME="$STATE" \
    "$PREFIX/bin/harness-headless" hh --prompt-file "$PROMPT" --result-file "$RESULT" --lock-file "$LOCK" --budget-usd 1 --timeout-min 1 \
    "${local_args[@]}" || fail "missing $missing must write a result"
  [[ "$(field status)" == refused ]] || fail "missing $missing must be refused"
done
rm -f "$RESULT"
env -i PATH="$STUB:/usr/bin:/bin" HOME="$FAKE_HOME" HARNESS_PROFILE_HOME="$PROFILES" HARNESS_SESSION_STATE_HOME="$STATE" \
  "$PREFIX/bin/harness-headless" hh --prompt-file "$PROMPT" --result-file "$RESULT" --lock-file "$LOCK" --budget-usd 1 --timeout-min 1 \
  --task-id t_x || fail 'a companion without --target must write a result'
[[ "$(field status)" == refused ]] || fail '--task-id without --target must be refused'
echo 'PASS: --target is codex-only and needs a valid digest, task id and approval SHA'

# --- delivered as a draft PR ----------------------------------------------------------
prompt change
TASK=t_ok
BR="$(branch t_ok)"
: > "$GHD/log.jsonl"
headless || fail 'target run must exit 0'
expect_status pr_opened
[[ "$(field target)" == "$NAME" && "$(field branch)" == "$BR" && "$(field base_sha)" == "$BASE_SHA" \
   && "$(field pr_url)" == "https://github.com/$GITHUB/pull/1" && "$(field exit_code)" == 0 ]] || fail 'pr_opened result fields'
pushed="$(remote_sha "$BR")"
[[ -n "$pushed" && "$(field commit)" == "$pushed" && "$(git --git-dir="$TARGET_REMOTE" rev-parse "$pushed^")" == "$BASE_SHA" ]] \
  || fail 'the commit must be pushed to the loop branch on top of the base'
[[ "$(git --git-dir="$TARGET_REMOTE" rev-parse trunk)" == "$BASE_SHA" ]] || fail 'the base branch must not move'
[[ "$(git --git-dir="$TARGET_REMOTE" show "$pushed:app.py")" == "$(printf 'def answer():\n    return 43')" ]] || fail 'the change must be pushed'
sid="$(field session_id)"
[[ "$(git --git-dir="$TARGET_REMOTE" log -1 --format=%B "$pushed")" == "$(printf 'feat: codex change\n\nWritten by the fake codex.\n\nHarness-Session: %s' "$sid")" ]] \
  || fail 'the agent message must be the commit message'
python3 - "$GHD/log.jsonl" "$GITHUB" "$BR" <<'PY' || fail 'gh calls'
import json, os, sys
calls = [json.loads(l) for l in open(sys.argv[1])]
github, branch = sys.argv[2], sys.argv[3]
argvs = [c['argv'] for c in calls]
create = [c for c in calls if c['argv'][:2] == ['pr', 'create']]
assert len(create) == 1, argvs
body = create[0]['argv'][-1]
assert create[0]['argv'] == ['pr', 'create', '--repo', github, '--base', 'trunk', '--head', branch, '--draft',
                             '--title', 'feat: codex change', '--body-file', body], create[0]['argv']
assert os.path.dirname(body) == create[0]['cwd'] and create[0]['cwd_entries'] == [os.path.basename(body)], create[0]
listed = [i for i, a in enumerate(argvs) if a[:2] == ['pr', 'list']]
assert listed and listed[-1] < argvs.index(create[0]['argv']), argvs
assert argvs[listed[-1]] == ['pr', 'list', '--repo', github, '--head', branch, '--state', 'all', '--json', 'url'], argvs
for c in calls:
    assert c['token'] == (c['argv'][:2] != ['auth', 'token']), c
    assert not c['cwd'].startswith('/private/tmp/hh-'), c
    if c['argv'][:2] != ['pr', 'create']:
        assert c['cwd_entries'] == [], c
# Preflight before the agent and again at delivery.
assert argvs.count(['auth', 'token', '--user', 'example-owner']) == 2, argvs
for a in (['api', 'user'], ['api', 'repos/' + github], ['api', 'repos/' + github + '/actions/permissions'],
          ['api', 'repos/' + github + '/actions/permissions/workflow'],
          ['api', 'repos/' + github + '/actions/secrets']):
    assert argvs.count(a) == 2, (a, argvs)
PY
body="$(cat "$GHD/body-1")"
grep -qx '```text' <<< "$body" && grep -qx 'Written by the fake codex.' <<< "$body" || fail 'the agent text must sit in a fenced code block'
[[ ! -e "$STATE/target-runs/$sid/env-source" && ! -e "$STATE/target-runs/$sid/candidate" ]] || fail 'env-source/ and candidate/ must be removed after the run'
[[ "$(head -n 1 <<< "$body")" == *'loop/'* ]] || fail 'the PR body must open with the loop/ branch note'
grep -qF 'Written by the fake codex.' <<< "$body" && grep -qF "$BASE_SHA" <<< "$body" || fail 'the PR body must carry the message body and base'
log="$RESULT.log"
grep -q '^FAKE-CODEX-AGENTS RULE-work-scale-ORIGIN-MAIN$' "$log" && grep -q '^FAKE-CODEX-AGENTS RULE-outcome-control-ORIGIN-MAIN$' "$log" \
  && grep -q '^FAKE-CODEX-AGENTS RULE-performance-observation-ORIGIN-MAIN$' "$log" || fail 'AGENTS.md must hold the three rules from origin/main'
! grep -q -e RULE-OTHER-NOT-COPIED -e RULE-WORKTREE-NOT-USED -e INDEX-NOT-COPIED "$log" || fail 'AGENTS.md must hold only the three rules, from origin/main'
session_root="$(sed -n 's/^FAKE-CODEX-PWD //p' "$log")"
run_tmp="$(sed -n 's/^FAKE-CODEX-ENV TMPDIR=//p' "$log")"
venv="$(sed -n 's/^FAKE-CODEX-ENV HARNESS_TARGET_PYTHON=//p' "$log")"; venv="${venv%/bin/python}"
grep -qxF "argv=sync --frozen --no-build --no-install-project --group dev" "$TMP/uv-call" || fail 'uv sync argv'
uv_env="$(sed -n 's/^env=//p' "$TMP/uv-call")"; uv_cwd="$(sed -n 's/^cwd=//p' "$TMP/uv-call")"
[[ -n "$uv_env" && "$uv_env" == "$venv" && "$uv_env" != "$run_tmp"* && "$uv_env" != "$session_root"* && "$uv_cwd" != "$session_root"* ]] \
  || fail "the venv must be broker-owned outside run_tmp and the session root (env=$uv_env cwd=$uv_cwd root=$session_root)"
grep -q 'pyproject.toml' "$TMP/uv-call" || fail 'uv sync must run on a base clone'
[[ "$session_root" == "$STATE/"* && "$(git --git-dir="$session_root.frozen/.git" rev-parse HEAD)" == "$BASE_SHA" && ! -e "$session_root" ]] || fail 'the session root must be detached at the base, then frozen'
grep -qxF "VERIFIER-PYTHON $venv/bin/python" "$log" && grep -qx 'VERIFIER-TESTS OK' "$log" || fail 'the verifier must run test.run with the venv python'
grep -qx 'VERIFIER-VENV-WRITE blocked' "$log" || fail 'the verifier must not write the venv'
! grep -q -e '^VERIFIER-ENV GH_' -e '^VERIFIER-ENV GITHUB_' -e '^FAKE-CODEX-ENV GH_' -e '^FAKE-CODEX-ENV GITHUB_' -e '^FAKE-CODEX-ENV SSH_AUTH' "$log" \
  || fail 'no GitHub variable may reach the agent or the verifier'
for leak in "$SENTINEL" leak-caller-gh leak-caller-github; do
  ! grep -rqF -- "$leak" "$RESULT" "$log" "$GHD"/body-* || fail "a token reached the result, log or PR body: $leak"
done
delivery="$(dirname "$(dirname "$session_root")")"
[[ "$(cat "$STATE/target-delivery/$NAME/t_ok-${APPROVAL:0:8}/journal")" == "DELIVERED($pushed)" \
   && "$(cat "$STATE/target-delivery/$NAME/t_ok-${APPROVAL:0:8}/pr-url")" == "https://github.com/$GITHUB/pull/1" ]] || fail 'journal DELIVERED and pr-url'
[[ "$(live_snapshot)" == "$LIVE_BEFORE" ]] || fail 'the live checkout must not change'
echo 'PASS: harness-headless --target delivers a draft PR through trusted.git with the exact gh argv and no token in reach'

no_leak() {  # the sentinel token and caller tokens appear in no result, log or PR body
  ! grep -rqF -e "$SENTINEL" -e leak-caller-gh -e leak-caller-github "$RESULT" "$RESULT.log" "$GHD" 2>/dev/null \
    || fail "a token leaked: $1"
}
run_case() {  # run_case <mode> <task> <expected status>
  prompt "$1"; TASK="$2"
  headless || fail "$1 run must exit 0"
  expect_status "$3"
  no_leak "$1"
}

# --- nothing to deliver ------------------------------------------------------------------
run_case none t_none no_changes
[[ -z "$(remote_sha "$(branch t_none)")" ]] || fail 'no_changes must push nothing'

# --- the verifier rejects: failed, nothing pushed ----------------------------------------
run_case verifyfail t_verify failed
grep -qx 'VERIFIER-TESTS FAILED' "$RESULT.log" && grep -q 'verifier rejected' "$RESULT" || fail 'a verifier rejection must be failed with its reason'
[[ -z "$(remote_sha "$(branch t_verify)")" ]] || fail 'a rejected candidate must not be pushed'
echo 'PASS: --target maps no changes and a verifier rejection, pushing nothing'

# --- .github/ in any case: refused --------------------------------------------------------
for mode in github githubcase; do
  run_case "$mode" "t_$mode" refused
  grep -q '\.github' "$RESULT" || fail "$mode refusal must name .github/"
  [[ -z "$(remote_sha "$(branch "t_$mode")")" ]] || fail "$mode must push nothing"
done
echo 'PASS: --target refuses a change under .github/ (case-insensitive)'

# --- dependency files: failed with the offline reason; other pyproject edits pass ---------
for mode in uvlock pyproject groups optional uvtoml pyversion requirements buildsys reqpython tooluv; do
  run_case "$mode" "t_$mode" failed
  [[ "$(field summary)" == *'의존성 변경은 오프라인 검증 불가'* ]] || fail "$mode must report the dependency reason"
  [[ -z "$(remote_sha "$(branch "t_$mode")")" ]] || fail "$mode must push nothing"
  ! grep -q '^VERIFIER-' "$RESULT.log" || fail "$mode must fail before the verifier"
done
run_case pyprojectok t_pyprojectok pr_opened
# uv sync runs with --no-build: a test group that needs an sdist build is refused.
: > "$TMP/uv-needs-build"
runs_before="$(ls "$STATE/target-runs" | wc -l | tr -d ' ')"
run_case change t_nobuild refused
# A constructor that fails after checking out env-source leaves nothing behind.
[[ "$(ls "$STATE/target-runs" | wc -l | tr -d ' ')" == "$runs_before" \
   && ! -e "$STATE/target-delivery/$NAME/t_nobuild-${APPROVAL:0:8}/trusted.git" ]] \
  || fail 'a failed session setup must remove its run dir (env-source, env, root) and trusted.git'
rm -f "$TMP/uv-needs-build"
grep -q 'no-build' "$RESULT" && ! grep -q '^FAKE-CODEX-ARGV' "$RESULT.log" || fail 'a group that needs builds must be refused before the agent'
# Submodules: a gitlink or a .gitmodules change is refused.
for mode in gitlink gitmodules; do
  run_case "$mode" "t_$mode" refused
  grep -q 'submodule' "$RESULT" && [[ -z "$(remote_sha "$(branch "t_$mode")")" ]] || fail "$mode must be refused as a submodule change"
done
echo 'PASS: --target fails dependency changes before the verifier and delivers other pyproject edits'

# --- secrets in the diff, the message or the PR body: refused ------------------------------
for mode in secretdiff secretpat secretkey secretmsg; do
  run_case "$mode" "t_$mode" refused
  grep -q 'secret pattern matched' "$RESULT" || fail "$mode must be a secret refusal"
  [[ -z "$(remote_sha "$(branch "t_$mode")")" ]] || fail "$mode must push nothing"
  ! grep -q -e 'AAAAAAAAAAAA' -e 'BBBBBBBBBBBB' -e 'CCCCCCCCCCCC' "$RESULT" || fail "$mode printed the secret"
done
grep -q 'message: pattern' "$RESULT" || fail 'the message secret must be found in the message'
python3 - "$ROOT/bin" <<'PY' || fail 'secret scan unit'
import sys
sys.path.insert(0, sys.argv[1])
import harness_headless as h
tok = 'gho_' + 'D' * 36
assert h.secret_hits([('body', 'x\n-----BEGIN RSA PRIVATE KEY-----\n')]) == ['body: pattern_7']
assert h.secret_hits([('body', 'see ' + tok)]) == ['body: pattern_5']
assert h.secret_hits([('diff', '+x = "github_pat_' + 'E' * 80 + '"')]) == ['diff: pattern_6']
for prefix in 'gho_ ghs_ ghu_ ghr_ ghp_'.split():
    assert h.secret_hits([('diff', prefix + 'F' * 36)]), prefix
assert h.secret_hits([('diff', '+API_TOKEN=<from the keychain>'), ('diff', '+OTHER_TOKEN=${X}')]) == []
assert h.secret_hits([('diff', '+GITHUB_TOKEN=abcdef')]) == ['diff: pattern_0']
assert h.secret_hits([('body', 'nothing here')]) == []
PY
echo 'PASS: --target refuses secrets in the diff, the commit message or the PR body'

# --- push conflict: the branch exists at another commit ------------------------------------
other="$(git --git-dir="$TARGET_REMOTE" commit-tree -m other "$BASE_SHA^{tree}" -p "$BASE_SHA")"
git --git-dir="$TARGET_REMOTE" update-ref "refs/heads/$(branch t_conflict)" "$other"
run_case change t_conflict conflict
[[ "$(remote_sha "$(branch t_conflict)")" == "$other" ]] || fail 'a conflict must not move the branch'
! grep -q "\"pr\", \"create\".*$(branch t_conflict)" "$GHD/log.jsonl" || fail 'a conflict must not open a PR'
echo 'PASS: --target reports conflict when the branch exists at another commit'

# --- resume: same SHA on the remote means PR only; a lost push is failed --------------------
: > "$GHD/create-fail"
run_case change t_resume failed
resumed_sha="$(remote_sha "$(branch t_resume)")"
[[ -n "$resumed_sha" && "$(cat "$STATE/target-delivery/$NAME/t_resume-${APPROVAL:0:8}/journal")" == "PUSHED($resumed_sha)" ]] \
  || fail 'a failed PR create keeps the pushed branch and PUSHED in the journal'
rm -f "$GHD/create-fail"
pushes_before="$(grep -c 'git-receive-pack' "$TMP/ssh-log")"
creates_before="$(grep -c '"pr", "create"' "$GHD/log.jsonl")"
run_case change t_resume pr_opened
! grep -q '^FAKE-CODEX-ARGV' "$RESULT.log" || fail 'a resumed delivery must not run the agent'
[[ "$(field commit)" == "$resumed_sha" && "$(grep -c 'git-receive-pack' "$TMP/ssh-log")" == "$pushes_before" \
   && "$(grep -c '"pr", "create"' "$GHD/log.jsonl")" == $((creates_before + 1)) ]] || fail 'resume with the same SHA must only open the PR'
run_case change t_resume pr_opened
[[ "$(grep -c '"pr", "create"' "$GHD/log.jsonl")" == $((creates_before + 1)) ]] || fail 'a DELIVERED record must not open another PR'
# PUSHED in the journal but the branch is gone: failed, not pushed again.
: > "$GHD/create-fail"
run_case change t_lost failed
git --git-dir="$TARGET_REMOTE" update-ref -d "refs/heads/$(branch t_lost)"
rm -f "$GHD/create-fail"
run_case change t_lost failed
grep -q 'no such branch' "$RESULT" && [[ -z "$(remote_sha "$(branch t_lost)")" ]] || fail 'PUSHED with no branch must fail without pushing'
echo 'PASS: --target resumes a pushed SHA with the PR only, and fails a journaled push the remote lost'

# --- gh pr create times out: the PR list is read again before anything else ----------------
: > "$GHD/create-hang"
creates_before="$(grep -c '"pr", "create"' "$GHD/log.jsonl")"
GH_TIMEOUT=2 run_case change t_hang pr_opened
rm -f "$GHD/create-hang"
python3 - "$GHD/log.jsonl" "$(branch t_hang)" <<'PY' || fail 'list-before-create after a timeout'
import json, sys
argvs = [json.loads(l)['argv'] for l in open(sys.argv[1])]
mine = [a[:2] for a in argvs if sys.argv[2] in a]
assert mine == [['pr', 'list'], ['pr', 'create'], ['pr', 'list']], mine
PY
[[ "$(field pr_url)" == https://github.com/$GITHUB/pull/* ]] || fail 'the listed PR must be reported'
echo 'PASS: --target lists PRs again after a gh pr create timeout instead of creating twice'

# --- boundary: every mismatch is refused before the agent ---------------------------------
# Records written directly (no target add checks), each under its own name.
put_record() {  # put_record <name> <python dict update> [live path]; prints the digest
  python3 - "$ROOT/bin" "$1" "$2" "${3:-}" <<'PY'
import copy, json, os, sys
sys.path.insert(0, sys.argv[1])
import harness_target as t
name, update, path = sys.argv[2], json.loads(sys.argv[3]), sys.argv[4] or None
entry = {'name': name, 'github': 'example-owner/example-repo', 'base': 'trunk',
         'remote': 'git@github.com-example:example-owner/example-repo.git',
         'test': {'group': 'dev', 'run': ['python', 'run_tests.py'], 'timeout_min': 1}}
entry.update(update)
t.write_record(name, entry, path)
print(t.digest(entry))
PY
}
boundary() {  # boundary <why> <name> <digest> <expected text in the summary>
  : > "$GHD/log.jsonl"
  TNAME="$2" TDIGEST="$3" refuse "$1"
  grep -qF -- "$4" "$RESULT" || fail "refusal '$1' must say: $4"
}
prompt change
TASK=t_boundary
boundary 'plain github.com remote' b-host "$(put_record b-host '{"remote": "git@github.com:example-owner/example-repo.git"}')" 'remote must be'
boundary 'another owner' b-owner "$(put_record b-owner '{"github": "company/example-repo", "remote": "git@github.com-example:company/example-repo.git"}')" 'owner must be the policy owner example-owner'
boundary 'remote path differs' b-path "$(put_record b-path '{"remote": "git@github.com-example:example-owner/other.git"}')" 'remote path must be exactly'
boundary 'deny list' b-deny "$(put_record b-deny '{"github": "example-owner/harness-launcher", "remote": "git@github.com-example2:example-owner/harness-launcher.git"}')" 'deny list'
boundary 'the harness repo' b-harness "$(put_record b-harness '{"github": "example-owner/example-harness", "remote": "git@github.com-example:example-owner/example-harness.git"}')" 'deny list'
boundary 'test.run argv0' b-argv "$(put_record b-argv '{"test": {"group": "dev", "run": ["/usr/bin/python3", "x.py"], "timeout_min": 1}}')" 'literal `python`'
mkdir -p "$FAKE_HOME/company-web/app" "$FAKE_HOME/company-code" "$FAKE_HOME/mirrors" "$FAKE_HOME/.agent-state"
for live in "$FAKE_HOME/company-web/app" "$FAKE_HOME/company-code" "$FAKE_HOME/mirrors/x" "$FAKE_HOME/.agent-state/x"; do
  boundary "live path $live" b-live "$(put_record b-live '{}' "$live")" 'target path is under'
done
boundary 'digest mismatch' "$NAME" "$(printf '0%.0s' $(seq 64))" 'does not match --target-digest'
good="$(put_record b-good '{}')"
chmod 644 "$PROFILES/targets/b-good.json"
boundary 'record mode 0644' b-good "$good" 'mode 0600'
chmod 600 "$PROFILES/targets/b-good.json"
ln "$PROFILES/targets/b-good.json" "$TMP/hardlink.json"
boundary 'record with two links' b-good "$good" 'one link'
rm -f "$TMP/hardlink.json"
mv "$PROFILES/targets/b-good.json" "$TMP/b-good.json" && ln -s "$TMP/b-good.json" "$PROFILES/targets/b-good.json"
boundary 'symlinked record' b-good "$good" 'symlink'
rm -f "$PROFILES/targets/b-good.json" && mv "$TMP/b-good.json" "$PROFILES/targets/b-good.json"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); open(sys.argv[1],"w").write(json.dumps(d, indent=1))' "$PROFILES/targets/b-good.json"
boundary 'record not canonical' b-good "$(shasum -a 256 < "$PROFILES/targets/b-good.json" | cut -d' ' -f1)" 'not the canonical JSON'
# Network rules, on the registered record.
gh_case() {  # gh_case <why> <control file> <value> <expected text>
  printf '%s\n' "$3" > "$GHD/$2"
  boundary "$1" "$NAME" "$DIGEST" "$4"
  rm -f "$GHD/$2"
}
gh_case 'not private' private false 'is not private'
gh_case 'a fork' fork true 'is a fork'
gh_case 'workflow permissions write' workflow write 'workflow permissions are not read'
gh_case 'Actions secrets' secrets 1 'Actions secrets'
gh_case 'Actions enabled' actions-enabled true 'Actions are enabled'
mv "$PROFILES/target-policy.json" "$TMP/policy.bak"
boundary 'no policy file' "$NAME" "$DIGEST" 'target-policy.json'
mv "$TMP/policy.bak" "$PROFILES/target-policy.json"
gh_case 'gh user' login someone-else 'gh api user is not example-owner'
# A launchd/cron context whose gh cannot read the keyring token.
: > "$GHD/token-fail"
boundary 'gh auth token fails' "$NAME" "$DIGEST" 'gh auth token --user example-owner'
rm -f "$GHD/token-fail"
printf 'someone-else\n' > "$TMP/ssh-account"
boundary 'ssh account' "$NAME" "$DIGEST" 'authenticates as someone-else'
printf 'example-owner\n' > "$TMP/ssh-account"
echo 'PASS: --target refuses every boundary mismatch before the agent starts'

# --- resume binds the record digest: a changed record fails instead of resuming ------------
: > "$GHD/create-fail"
run_case change t_digest failed
rm -f "$GHD/create-fail"
changed="$(put_record "$NAME" '{"test": {"group": "dev", "run": ["python", "run_tests.py"], "timeout_min": 2}}' "$LIVE")"
TDIGEST="$changed" run_case change t_digest failed
grep -q 'digest' "$RESULT" && ! grep -q '^FAKE-CODEX-ARGV' "$RESULT.log" || fail 'a resume under another record digest must fail'
[[ "$(put_record "$NAME" '{}' "$LIVE")" == "$DIGEST" ]] || fail 'the registered record must be restored'
echo 'PASS: --target resumes only under the record digest it started with'

# --- H1: the agent swaps the session root for a symlink -------------------------------------
run_case swaproot t_swap failed
grep -qx 'FAKE-CODEX-SWAPPED' "$RESULT.log" || fail 'the fake agent must have swapped the root (sandbox allowed it)'
grep -q 'session root' "$RESULT" || fail 'a swapped root must be named'
swap_run="$STATE/target-runs/$(field session_id)"
swap_trusted="$STATE/target-delivery/$NAME/t_swap-${APPROVAL:0:8}/trusted.git"
[[ -z "$(remote_sha "$(branch t_swap)")" && ! -e "$swap_run/submission.patch" ]] || fail 'a swapped root must deliver and stage nothing'
# wip.txt exists only in the live checkout (the symlink target), never in the base.
! git --git-dir="$swap_trusted" cat-file -e "$(git hash-object "$LIVE/wip.txt")" 2>/dev/null \
  || fail 'content of the symlink target reached trusted.git'
[[ "$(live_snapshot)" == "$LIVE_BEFORE" ]] || fail 'a swapped root must not change the live checkout'
echo 'PASS: --target fails a run whose session root was replaced, staging nothing through the link'

# --- hooks, fsmonitor and filters planted in the agent clone never run in the broker -------
run_case hooks t_hooks pr_opened
[[ -z "$(ls -A "$TMP/marks")" ]] || fail "a planted hook, fsmonitor or filter ran: $(ls "$TMP/marks")"
[[ "$(git --git-dir="$TARGET_REMOTE" show "$(field commit):app.py")" == "$(printf 'def answer():\n    return 56')" ]] \
  || fail 'the planted filter must not change the delivered content'
echo 'PASS: --target never runs hooks, fsmonitor or filters planted in the agent clone'

# --- the agent sandbox: env read/exec only, launcher records, company code, the harness, live -------
mkdir -p "$FAKE_HOME/company-code" "$FAKE_HOME/company-web" "$FAKE_HOME/mirrors"
for f in company-code/secret.txt company-web/secret.txt mirrors/secret.txt; do printf 'secret-fixture\n' > "$FAKE_HOME/$f"; done
printf 'HOME_DIR=%s\nHARNESS_ROOT=%s\nLIVE=%s\nPROFILES=%s\nRECORD=%s\nDELIVERY=%s\n' "$FAKE_HOME" "$SOURCE" "$LIVE" "$PROFILES" \
  "$PROFILES/targets/$NAME.json" "$STATE/target-delivery/$NAME/t_probe-${APPROVAL:0:8}" > "$STUB/target-probe.env"
cat > "$STUB/target-probe.sh" <<'EOF'
#!/bin/bash
set -a; source "$(dirname "$0")/target-probe.env"; set +a
"$HARNESS_TARGET_PYTHON" - <<'PY'
import errno, os, subprocess
E = os.environ
venv = os.path.dirname(os.path.dirname(E['HARNESS_TARGET_PYTHON']))
def probe(name, f):
    try:
        f()
        print('PROBE', name, 'ALLOWED')
    except OSError as e:
        print('PROBE', name, errno.errorcode.get(e.errno, e.errno))
def read(path):
    return lambda: open(path, 'rb').read()
def write(path, mode='w'):
    def f():
        open(path, mode).write('x')
    return f
def run(*argv):
    def f():
        if subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            raise OSError(errno.EPERM, argv[0])
    return f
probe('read-venv', read(os.path.join(venv, 'pyvenv.cfg')))
probe('run-venv-python', run(E['HARNESS_TARGET_PYTHON'], '-c', 'import json'))
probe('write-venv-bin', write(os.path.join(venv, 'bin', 'evil')))
probe('write-venv-cfg', write(os.path.join(venv, 'pyvenv.cfg'), 'a'))
probe('write-venv-site', write(os.path.join(venv, 'sitecustomize.py')))
probe('child-write-venv', run('/bin/sh', '-c', 'printf x > "$1/bin/evil2"', 'sh', venv))
probe('write-launcher-record', write(os.path.join(E['PROFILES'], 'targets', 'evil.json')))
probe('write-launcher-home', write(os.path.join(E['PROFILES'], 'evil')))
probe('read-launcher-record', read(E['RECORD']))
probe('read-company-code', read(os.path.join(E['HOME_DIR'], 'company-code', 'secret.txt')))
probe('read-company-web', read(os.path.join(E['HOME_DIR'], 'company-web', 'secret.txt')))
probe('read-mirrors', read(os.path.join(E['HOME_DIR'], 'mirrors', 'secret.txt')))
probe('read-live', read(os.path.join(E['LIVE'], 'app.py')))
probe('read-harness-root', read(os.path.join(E['HARNESS_ROOT'], 'tracked.txt')))
probe('read-trusted-git', read(os.path.join(E['DELIVERY'], 'trusted.git', 'HEAD')))
probe('write-root', write('probe-root.txt'))
PY
EOF
# PATH directories are readable in the profile: these sit on PATH, so only the
# explicit denies keep them closed.
prompt probe
TASK=t_probe
EXTRA_PATH="$FAKE_HOME/company-code:$FAKE_HOME/company-web:$FAKE_HOME/mirrors:$LIVE:$SOURCE:$PROFILES/targets:$PROFILES" \
  headless || fail 'probe run must exit 0'
expect_status pr_opened
for allowed in read-venv run-venv-python write-root; do
  grep -qx "PROBE $allowed ALLOWED" "$RESULT.log" || fail "the target sandbox must allow: $allowed"
done
for denied in write-venv-bin write-venv-cfg write-venv-site child-write-venv write-launcher-record write-launcher-home \
    read-launcher-record read-company-code read-company-web read-mirrors read-live read-harness-root read-trusted-git; do
  grep -qx "PROBE $denied EPERM" "$RESULT.log" || fail "the target sandbox must deny: $denied"
done
[[ ! -e "$PROFILES/targets/evil.json" && ! -e "$PROFILES/evil" ]] || fail 'the agent wrote a launcher record'
! grep -q 'secret-fixture' "$RESULT.log" || fail 'a denied file reached the log'
no_leak probe
HARNESS_PROFILE_HOME="$TMP/custom-profile" python3 - "$ROOT/bin" "$FAKE_HOME" "$TMP/custom-profile" <<'PY' || fail 'the Claude lane must deny writes to the profile home'
import sys
sys.path.insert(0, sys.argv[1])
import harness_headless as h
home, path = sys.argv[2], sys.argv[3]
s = h.mandatory_settings('/src', home, [], '/private/tmp/hh-x')
assert path in s['sandbox']['filesystem']['denyWrite'], s['sandbox']['filesystem']
for tool in ('Edit', 'Write', 'NotebookEdit'):
    assert f'{tool}(/{path}/**)' in s['permissions']['deny'], tool
PY
echo 'PASS: the target sandbox keeps the env read-only and denies launcher records, company code, mirrors, the harness and the live checkout'

# --- R4: the real codex binary under the target profile (no model cost) --------------------
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
  echo 'SKIP: no real codex with codex-code-mode-host beside it (set HARNESS_TEST_REAL_CODEX); the real-binary keyring and ~/company-code tests did not run'
else
  # Output goes to /dev/null: only whether each read worked is reported.
  printf '%s\n' "/usr/bin/security find-generic-password -s gh:github.com -w >/dev/null 2>&1 && echo KEYCHAIN-ALLOWED || echo KEYCHAIN-DENIED; cat '$FAKE_HOME/company-code/secret.txt' >/dev/null 2>&1 && echo COMPANY-ALLOWED || echo COMPANY-DENIED; printf 'real\\n' > real-target.txt && echo WRITE-OK" > "$UP/command"
  printf 'real codex done\n' > "$UP/final"
  printf 'gpt-6.1-sol\n' > "$UP/model"
  up_before="$(wc -l < "$UP/requests.jsonl" | tr -d ' ')"
  printf '%s\n' 'Run the command.' > "$PROMPT"
  TASK=t_real MODEL=gpt-6.1-sol CODEX_BIN="$REAL_CODEX" TIMEOUT_MIN=3 headless --effort low || fail 'real codex target run must exit 0'
  expect_status pr_opened
  git --git-dir="$TARGET_REMOTE" show "$(field commit):real-target.txt" > /dev/null || fail 'the real codex write must be delivered'
  python3 - "$UP/requests.jsonl" "$up_before" <<'PY' || fail 'real codex: keyring and ~/company-code reads must fail'
import json, sys
lines = [json.loads(l) for l in open(sys.argv[1]).read().splitlines()[int(sys.argv[2]):]]
out = '\n'.join(o for l in lines if l['method'] == 'POST' for o in l['outputs'])
assert 'KEYCHAIN-DENIED' in out and 'COMPANY-DENIED' in out and 'WRITE-OK' in out, out
assert 'KEYCHAIN-ALLOWED' not in out and 'COMPANY-ALLOWED' not in out and 'secret-fixture' not in out, out
PY
  no_leak real-codex
  echo "PASS: the real codex ($("$REAL_CODEX" --version 2>/dev/null)) under the target profile cannot read the gh keyring item or ~/company-code"
fi

[[ "$(live_snapshot)" == "$LIVE_BEFORE" ]] || fail 'the live checkout must not change across all runs'
# The base_prefix allow cannot reopen a 0.47 deny: every base deny line is
# repeated after the env allows (state-home lines aside, which would close the env).
python3 - "$ROOT/bin" <<'PY' || fail 'profile rules must repeat the base denies after the env allows'
import sys
sys.path.insert(0, sys.argv[1])
import harness_headless as h
base = ('(version 1)\n(allow file-read* (subpath "/opt"))\n'
        '(deny file-read* file-write* process-exec* (subpath "/S") (subpath "/S"))\n'
        '(allow file-read* process-exec* (subpath "/S/copy"))\n'
        '(deny file-read* file-write* process-exec* (subpath "/H/.ssh") (subpath "/K"))\n'
        '(deny file-read* file-write* process-exec* (require-all (subpath "/S") (require-not (subpath "/S/r"))))\n'
        '(deny process-info*)\n(allow process-info* (target same-sandbox))\n')
rules = h.target_profile_rules(base, home='/H', hdir='/K', live=None, env_dir='/S/env', base_prefix='/H/.ssh/py',
                               home_deny=['company-*', 'mirrors'], state='/S').splitlines()
allow = next(i for i, l in enumerate(rules) if l.startswith('(allow file-read* process-exec*') and '/S/env' in l)
assert '(deny file-read* file-write* process-exec* (subpath "/H/.ssh") (subpath "/K"))' in rules[allow + 1:], rules
assert not any('require-all' in l or '"/S")' in l or 'process-info' in l for l in rules), rules
assert any('regex #"^/H/company-"' in l for l in rules[allow + 1:]) and any('"/H/mirrors"' in l for l in rules), rules
PY
[[ "$(ls -ld "$REAL_HOME/.config/harness-launcher/targets" "$REAL_HOME/.config/harness-launcher/target-policy.json" 2>&1 || true)" == "$real_targets_before" ]] \
  || fail 'the real ~/.config/harness-launcher target records changed'
! ls -d "$REAL_HOME"/.local/state/harness-launcher/target-* >/dev/null 2>&1 || fail 'the real state home got target runs'
python3 - "$ROOT/bin" <<'PY' || fail 'base_prefix must not be /, an ancestor of HOME or state, or at/under the state home'
import sys
sys.path.insert(0, sys.argv[1])
import harness_headless as h
ok = h.usable_base_prefix
assert ok('/opt/py/3.14', '/Users/u', '/Users/u/.local/state/hl')
for bad in ('', 'rel', '/', '/Users', '/Users/u', '/Users/u/.local/state/hl', '/Users/u/.local/state/hl/target-runs/x/py'):
    assert not ok(bad, '/Users/u', '/Users/u/.local/state/hl'), bad
# State outside HOME: at or under it is still refused.
assert not ok('/srv/state/py', '/Users/u', '/srv/state') and ok('/srv/py', '/Users/u', '/srv/state')
PY
echo 'PASS: profile rule order, base_prefix rules, and the real launcher config and state were not touched'
echo 'PASS: the live checkout is unchanged after every target run'
