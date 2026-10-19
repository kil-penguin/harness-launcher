#!/bin/bash
# pre-bash-curl-block.sh
# Block command-position `curl` invocations that hit the network so the agent
# pivots to `node -e "fetch(...)"`. Quoted curl (in ssh/SSM args, commit
# messages, heredoc bodies) is stripped before matching so it stays allowed.

set -u

INPUT=$(cat /dev/stdin 2>/dev/null || echo '{}')

command -v jq >/dev/null 2>&1 || exit 0

COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
[ -z "$COMMAND" ] && exit 0

case "$COMMAND" in
  *c*u*r*l*) ;;
  *) exit 0 ;;
esac

HARNESS_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

# Escape hatch: explicit env override
case "$COMMAND" in
  HARNESS_ALLOW_CURL=1*) exit 0 ;;
esac

# Strip quoted strings and heredoc bodies. curl inside these usually never executes
# in the local shell (it's an argument to another tool, a commit message, or a remote
# command). Only what remains is real shell text.
#
# The exception, and the reason for the INTERP_CURL line below: when the consumer of
# that body is a LOCAL interpreter (python/node/ruby/…), the body is a script that runs
# here and can shell out to curl through a subprocess. Stripping it would hide a real
# local invocation. Observed 2026-08-28: `python3 - <<'PY' … subprocess.run(["curl", …])`
# passed this hook, while a top-level curl in the same session was blocked.
#
# The analyzer is read from a plain heredoc and passed with -c. Stock macOS bash 3.2 cannot
# parse a heredoc with unbalanced quotes inside $(...): the syntax error exits 2, which
# reads as "blocked" for every command that gets this far.
# Keep the analyzer body ASCII: it travels as an argv entry, which a non-UTF-8 locale
# would fail to decode. `read` returns 1 at end of input by design.
IFS= read -r -d '' ANALYZER <<'PYEOF' || true
import os, re

t = os.environ.get('CMD', '')

# Interpreters that execute an inline script we hand them. `ssh`, `git`, `cat`, `jq`
# and friends are deliberately absent: their quoted bodies run elsewhere or nowhere.
INTERP = r'(?:python[0-9.]*|node|nodejs|ruby|perl|php|deno|bun|bash|sh|zsh)'
# A curl that is being invoked, not merely mentioned: an argv entry, or a flag/URL after it.
CURL_CALL = re.compile(r"""['"]curl['"]|\bcurl\s+(?:-|https?://)""")
# One quoted shell word. Double quotes honour backslash escapes, single quotes do not.
SPAN = r"'[^']*'|\"(?:\\.|[^\"\\])*\""

def command_positions(text):
    """Start of each command in the shell text: line start or after ; & | ( )."""
    yield 0
    for m in re.finditer(r'[;&|()\n]', text):
        yield m.end()

def leads_with_interpreter(text, pos):
    # Skip whitespace and VAR=value prefixes, then require an interpreter word.
    return re.match(r'\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*' + INTERP + r'(?:\s|$)', text[pos:])

interp_curl = False

# Heredocs first: the tag may itself sit inside outer quotes.
def strip_heredoc(m):
    global interp_curl
    before = t[:m.start()]
    head = before[before.rfind('\n') + 1:]
    # The body's consumer is the last command before `<<`, not any command on that line:
    # `bash run-tests.sh && git commit -F - <<EOF` hands the body to git. Separators inside
    # a quoted argument or an fd redirection (`2>&1`) do not start a command, so mask them.
    masked = re.sub(SPAN + r'|\d*[<>]&\d*-?', lambda q: 'x' * len(q.group(0)), head)
    consumer = list(command_positions(masked))[-1]
    if CURL_CALL.search(m.group(0)) and leads_with_interpreter(head, consumer):
        interp_curl = True
    return ''

t = re.sub(r"<<-?['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?[^\n]*\n.*?\n\1\b",
           strip_heredoc, t, flags=re.DOTALL)

# Inline scripts passed with -c / -e to a local interpreter. The interpreter must sit at
# command position: in `ssh host bash -c "curl ..."` the curl runs on the remote, not here.
INLINE = (r'(?:^|[;&|()\n])\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*'
          + INTERP + r'\s+(?:-\S+\s+)*-[ce]\s+(\'[^\']*\'|"[^"]*")')
# An interpreter that starts inside a top-level quoted span is an argument to another
# command (`ssh host 'cd /app && bash -c "curl ..."'`) and does not run here either.
QUOTED = [q.span() for q in re.finditer(SPAN, t, flags=re.DOTALL)]
for m in re.finditer(INLINE, t, flags=re.DOTALL):
    if CURL_CALL.search(m.group(1)) and not any(a < m.start() < b for a, b in QUOTED):
        interp_curl = True

t = re.sub(r'"[^"]*"', '', t, flags=re.DOTALL)
t = re.sub(r"'[^']*'", '', t, flags=re.DOTALL)

print('INTERP_CURL=1' if interp_curl else 'INTERP_CURL=0')
print(t)
PYEOF
ANALYSIS=$(CMD="$COMMAND" "$HARNESS_DIR/core/bin/harness-python" -- -c "$ANALYZER")
# First line is the interpreter verdict, the rest is the stripped shell text. Parameter
# expansion keeps this free of PATH lookups: a missing `tail` once emptied CLEAN and let
# a top-level curl through.
INTERP_CURL=${ANALYSIS%%$'\n'*}
CLEAN=${ANALYSIS#*$'\n'}
[ "$CLEAN" = "$ANALYSIS" ] && CLEAN=

# Match command-position curl (start-of-line OR after ; && || |) + space.
# A local interpreter carrying curl in its inline script counts as command position too.
if [ "$INTERP_CURL" != "INTERP_CURL=1" ] \
   && ! echo "$CLEAN" | grep -qE '(^|[;&|])[[:space:]]*curl[[:space:]]'; then
  exit 0
fi

# Escape hatch: `command curl ...` (explicit bypass)
if echo "$CLEAN" | grep -qE '(^|[;&|])[[:space:]]*command[[:space:]]+curl[[:space:]]'; then
  exit 0
fi

# Allowlist non-network curl invocations (version/help only)
SEGMENT=$(echo "$CLEAN" | grep -oE '(^|[;&|])[[:space:]]*curl[[:space:]][^;&|]*' | head -1)
case "$SEGMENT" in
  *" --version"*|*" -V "*|*" -V"|*" --help"*|*" -h "*|*" -h"|*" -h ;"*)
    if ! echo "$SEGMENT" | grep -qE '(https?://|[[:space:]]-[a-zA-Z]*[dXopuFGI])'; then
      exit 0
    fi
    ;;
esac

case "$(echo "$CLEAN" | sed -E 's/^[[:space:]]+//')" in
  "curl --version"|"curl --help"|"curl -V"|"curl -h") exit 0 ;;
esac

cat >&2 <<'EOF'
⚠️  curl 차단 — 이 환경에서는 Node 내장 fetch 사용.

복붙용 (GET):
  node -e "fetch('URL').then(async r => { console.log('HTTP ' + r.status); console.log((await r.text()).slice(0, 2000)); }).catch(e => { console.error('ERR ' + e.message); process.exit(1); });"

POST/headers/timeout:
  node -e "fetch('URL', { method: 'POST', headers: { 'content-type':'application/json', authorization:'Bearer KEY' }, body: JSON.stringify({a:1}), signal: AbortSignal.timeout(5000) }).then(async r => { console.log('HTTP ' + r.status, await r.text()); });"

원격 실행이라면 (ssh/SSM args 안의 curl은 차단되지 않음): 따옴표 안에서 그대로 사용.
파이썬·노드 인라인 스크립트 안에서 subprocess 로 curl 을 부르는 것도 로컬 실행이므로 차단된다.
긴급 우회: HARNESS_ALLOW_CURL=1 curl ...  또는  command curl ...
차단 범위와 예외는 core/hooks/test/test-pre-bash-curl-block.sh 의 케이스가 정본이다.
EOF

exit 2
