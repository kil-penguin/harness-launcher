#!/usr/bin/env bash
# Verify that Codex composite exec input is resolved before command-sensitive
# PreToolUse hooks run.

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ADAPTER="$ROOT/bin/codex-pretool-adapter.py"
PASS=0
FAIL=0
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

assert_eq() {
  local label="$1" actual="$2" expected="$3"
  if [[ "$actual" == "$expected" ]]; then
    echo "PASS: $label"
    PASS=$((PASS + 1))
  else
    echo "FAIL: $label (expected '$expected', got '$actual')"
    FAIL=$((FAIL + 1))
  fi
}

if [[ ! -x "$ADAPTER" ]]; then
  echo "FAIL: missing executable adapter: $ADAPTER"
  exit 1
fi

CAPTURE="$TMP_ROOT/calls.jsonl"
export CAPTURE
mkdir -p "$TMP_ROOT/core/hooks"
HOOK="$TMP_ROOT/core/hooks/pre-bash-pr-gate.sh"
cat > "$HOOK" <<'EOF'
#!/usr/bin/env bash
payload="$(cat)"
command="$(printf '%s' "$payload" | jq -r '.tool_input.command')"
jq -cn --arg command "$command" --arg cwd "$(pwd -P)" \
  '{command:$command,cwd:$cwd}' >> "$CAPTURE"
case "$command" in
  gh\ pr\ create*|gh\ pr\ edit*)
    if [[ "${HARNESS_ALLOW_PR:-}" != "1" ]]; then
      echo "probe denied resolved gh mutation" >&2
      exit 2
    fi
    ;;
  ADVISE*)
    jq -n --arg ctx "$command" '{additionalContext:$ctx}'
    ;;
esac
EOF
chmod +x "$HOOK"

WORKDIR="$TMP_ROOT/project worktree"
mkdir -p "$WORKDIR"
WORKDIR_REAL="$(cd "$WORKDIR" && pwd -P)"

payload() {
  local source="$1" tool_name="${2:-code_mode_exec}" cwd="${3:-$TMP_ROOT}"
  jq -cn --arg source "$source" --arg cwd "$cwd" --arg tool_name "$tool_name" '{
    session_id:"session", turn_id:"turn", cwd:$cwd,
    hook_event_name:"PreToolUse", model:"gpt", permission_mode:"default",
    tool_name:$tool_name, tool_input:{command:$source}, tool_use_id:"call"
  }'
}

# A native Bash PreToolUse payload is already a shell command. It must reach
# the canonical guard once, without being interpreted as JavaScript source.
: > "$CAPTURE"
raw_command='printf raw-shell && rg -n "/tools/" fixture'
printf '%s' "$(payload "$raw_command" Bash "$WORKDIR")" | python3 "$ADAPTER" "$HOOK" >/dev/null
assert_eq "raw Bash forwards command exactly once" \
  "$(jq -r '.command' "$CAPTURE")" "$raw_command"
assert_eq "raw Bash runs hook in payload cwd" \
  "$(jq -r '.cwd' "$CAPTURE")" "$WORKDIR_REAL"

: > "$CAPTURE"
set +e
printf '%s' "$(payload 'gh pr create --title blocked' Bash "$WORKDIR")" | python3 "$ADAPTER" "$HOOK" >/dev/null 2>"$TMP_ROOT/raw-pr.err"
rc=$?
set -e
assert_eq "raw Bash gh PR mutation is blocked" "$rc" "2"
assert_eq "raw Bash gh PR reaches canonical hook once" \
  "$(wc -l < "$CAPTURE" | tr -d ' ')" "1"

# HARNESS_ALLOW_PR is a canonical-hook override. The adapter must preserve it
# for native Bash payloads and normalized composite children alike.
: > "$CAPTURE"
set +e
printf '%s' "$(payload 'gh pr create --title allowed' Bash "$WORKDIR")" | HARNESS_ALLOW_PR=1 python3 "$ADAPTER" "$HOOK" >/dev/null 2>"$TMP_ROOT/raw-pr-allow.err"
rc=$?
set -e
assert_eq "raw Bash preserves HARNESS_ALLOW_PR" "$rc" "0"

: > "$CAPTURE"
set +e
printf '%s' "$(payload 'text(await tools.exec_command({cmd:"gh pr create --title composite",workdir:"/tmp"}));' code_mode_exec)" | HARNESS_ALLOW_PR=1 python3 "$ADAPTER" "$HOOK" >/dev/null 2>"$TMP_ROOT/composite-pr-allow.err"
rc=$?
set -e
assert_eq "composite child preserves HARNESS_ALLOW_PR" "$rc" "0"

: > "$CAPTURE"
raw_command='rg -n "tools.exec_command" transcript.jsonl'
printf '%s' "$(payload "$raw_command" Bash "$WORKDIR")" | python3 "$ADAPTER" "$HOOK" >/dev/null
assert_eq "raw Bash tools text is not parsed as composite" \
  "$(jq -r '.command' "$CAPTURE")" "$raw_command"
: > "$CAPTURE"

: > "$CAPTURE"
set +e
printf '%s' "$(payload 'gh pr create --title must-not-downgrade' code_mode_exec "$WORKDIR")" | python3 "$ADAPTER" "$HOOK" >/dev/null 2>"$TMP_ROOT/composite-raw.err"
rc=$?
set -e
assert_eq "composite identity never downgrades raw shell" "$rc" "0"
assert_eq "composite raw shell reaches no canonical hook" \
  "$(wc -l < "$CAPTURE" | tr -d ' ')" "0"

# Transcript-shaped static JSON object: pass the real command and nested cwd.
source="const r = await tools.exec_command({\"cmd\":\"printf harmless\",\"workdir\":$(jq -Rn --arg v "$WORKDIR" '$v'),\"yield_time_ms\":10000}); text(r.output);"
printf '%s' "$(payload "$source")" | python3 "$ADAPTER" "$HOOK" >/dev/null
assert_eq "static call forwards resolved command" \
  "$(jq -r '.command' "$CAPTURE")" "printf harmless"
assert_eq "static call runs hook in resolved workdir" \
  "$(jq -r '.cwd' "$CAPTURE")" "$WORKDIR_REAL"

# Missing workdir inherits the caller-provided payload cwd, never the adapter's
# process cwd.
: > "$CAPTURE"
source='await tools.exec_command({cmd:"printf inherited"});'
printf '%s' "$(payload "$source")" | (cd "$WORKDIR" && python3 "$ADAPTER" "$HOOK") >/dev/null
assert_eq "absent workdir inherits payload cwd" \
  "$(jq -r '.cwd' "$CAPTURE")" "$(cd "$TMP_ROOT" && pwd -P)"

# Both composite identities use strict parsing, while unrecognized/missing
# identities and invalid payload cwd values fail before the canonical hook.
for identity_case in exec unknown missing missing_command relative_cwd missing_cwd missing_cwd_path; do
  : > "$CAPTURE"
  case "$identity_case" in
    exec) input="$(payload 'await tools.exec_command({cmd:"printf exec",workdir:"/tmp"});' exec)"; expected=0 ;;
    unknown) input="$(payload 'printf unknown' Shell)"; expected=2 ;;
    missing) input="$(payload 'printf missing' Bash | jq 'del(.tool_name)')"; expected=2 ;;
    missing_command) input="$(payload 'printf missing-command' Bash | jq 'del(.tool_input.command)')"; expected=2 ;;
    relative_cwd) input="$(payload 'printf cwd' Bash relative)"; expected=2 ;;
    missing_cwd) input="$(payload 'printf cwd' Bash | jq 'del(.cwd)')"; expected=2 ;;
    missing_cwd_path) input="$(payload 'printf cwd' Bash "$TMP_ROOT/does-not-exist")"; expected=2 ;;
  esac
  set +e
  printf '%s' "$input" | python3 "$ADAPTER" "$HOOK" >/dev/null 2>"$TMP_ROOT/$identity_case.err"
  rc=$?
  set -e
  assert_eq "$identity_case identity/cwd contract" "$rc" "$expected"
  if [[ "$expected" == 2 ]]; then
    assert_eq "$identity_case fails before canonical hook" "$(wc -l < "$CAPTURE" | tr -d ' ')" "0"
  else
    assert_eq "exec identity normalizes child tool name" "$(jq -r '.command' "$CAPTURE")" "printf exec"
  fi
done

# `text(await tools.exec_command(...))` is a valid composite shape, not a
# reason to reinterpret its source as raw Bash. Its nested mutation must be
# seen and denied by the canonical hook.
: > "$CAPTURE"
set +e
printf '%s' "$(payload 'text(await tools.exec_command({cmd:"gh pr create --title nested",workdir:"/tmp"}));' code_mode_exec)" | python3 "$ADAPTER" "$HOOK" >/dev/null 2>"$TMP_ROOT/text-await-deny.err"
rc=$?
set -e
assert_eq "text await composite gh PR is blocked" "$rc" "2"
assert_eq "text await composite reaches canonical hook once" \
  "$(wc -l < "$CAPTURE" | tr -d ' ')" "1"

# Each composite identity keeps the same strict static, multi-call, dynamic,
# and deny behavior; neither may fall through to the raw Bash route.
for identity in code_mode_exec exec; do
  : > "$CAPTURE"
  printf '%s' "$(payload 'await tools.exec_command({cmd:"printf static",workdir:"/tmp"});' "$identity")" | python3 "$ADAPTER" "$HOOK" >/dev/null
  assert_eq "$identity static composite reaches hook" "$(jq -r '.command' "$CAPTURE")" "printf static"

  : > "$CAPTURE"
  set +e
  printf '%s' "$(payload 'const args={cmd:"printf dynamic",workdir:"/tmp"}; await tools.exec_command(args);' "$identity")" | python3 "$ADAPTER" "$HOOK" >/dev/null 2>"$TMP_ROOT/$identity-dynamic.err"
  rc=$?
  set -e
  assert_eq "$identity dynamic composite fails closed" "$rc" "2"
  assert_eq "$identity dynamic composite runs no hook" "$(wc -l < "$CAPTURE" | tr -d ' ')" "0"

  : > "$CAPTURE"
  set +e
  printf '%s' "$(payload 'await tools.exec_command({cmd:"gh pr create --title deny",workdir:"/tmp"});' "$identity")" | python3 "$ADAPTER" "$HOOK" >/dev/null 2>"$TMP_ROOT/$identity-deny.err"
  rc=$?
  set -e
  assert_eq "$identity denied composite blocks" "$rc" "2"
  assert_eq "$identity denied composite reaches hook once" "$(wc -l < "$CAPTURE" | tr -d ' ')" "1"

  : > "$CAPTURE"
  printf '%s' "$(payload 'await tools.exec_command({cmd:"printf first",workdir:"/tmp"}); await tools.exec_command({cmd:"printf second",workdir:"/tmp"});' "$identity")" | python3 "$ADAPTER" "$HOOK" >/dev/null
  assert_eq "$identity multi composite preserves every call" \
    "$(jq -r '.command' "$CAPTURE" | paste -sd '|' -)" "printf first|printf second"
done

# functions.exec operations without a nested shell call are irrelevant to Bash
# guards and must not invoke the canonical hook.
: > "$CAPTURE"
source='const patch = "*** Begin Patch"; text(await tools.apply_patch(patch));'
printf '%s' "$(payload "$source")" | python3 "$ADAPTER" "$HOOK" >/dev/null
assert_eq "no exec_command call is a no-op" "$(wc -l < "$CAPTURE" | tr -d ' ')" "0"

# Computed access to a different tool is still not a shell call. Reject only
# computed exec_command access, whose arguments cannot be statically proven.
: > "$CAPTURE"
source='const result = await tools["apply_patch"]("patch"); text(result);'
set +e
printf '%s' "$(payload "$source")" | python3 "$ADAPTER" "$HOOK" >/dev/null 2>"$TMP_ROOT/computed-non-exec.err"
rc=$?
set -e
assert_eq "computed non-exec tool access is allowed" "$rc" "0"
assert_eq "computed non-exec tool access is a no-op" "$(wc -l < "$CAPTURE" | tr -d ' ')" "0"

# A search command may contain a gh mutation as data; only the resolved command
# position is evaluated by the canonical hook.
: > "$CAPTURE"
source='const r = await tools.exec_command({"cmd":"rg -n \"gh pr edit\" transcript.jsonl","workdir":"/tmp"}); text(r.output);'
set +e
printf '%s' "$(payload "$source")" | python3 "$ADAPTER" "$HOOK" >/dev/null 2>"$TMP_ROOT/search.err"
rc=$?
set -e
assert_eq "gh text inside resolved search does not false-block" "$rc" "0"
assert_eq "search reaches hook as search" \
  "$(jq -r '.command' "$CAPTURE")" 'rg -n "gh pr edit" transcript.jsonl'

# Call-shaped text inside comments, templates, and regex literals is data, not
# an invocation. Only the final static call reaches the hook.
for syntax in comment template regex; do
  : > "$CAPTURE"
  case "$syntax" in
    comment) source='/* tools.exec_command({cmd:"gh pr edit 1",workdir:"/tmp"}) */ await tools.exec_command({cmd:"printf comment-safe",workdir:"/tmp"});' ;;
    template) source='const sample = `tools.exec_command({cmd:"gh pr edit 1",workdir:"/tmp"})`; await tools.exec_command({cmd:"printf template-safe",workdir:"/tmp"});' ;;
    regex) source='const sample = /tools\.exec_command\(\{cmd:"gh pr edit 1"/; await tools.exec_command({cmd:"printf regex-safe",workdir:"/tmp"});' ;;
  esac
  printf '%s' "$(payload "$source")" | python3 "$ADAPTER" "$HOOK" >/dev/null
  assert_eq "$syntax call-shaped text is ignored" \
    "$(jq -r '.command' "$CAPTURE")" "printf $syntax-safe"
done

# Dynamic arguments cannot be proven safe from the outer hook boundary.
: > "$CAPTURE"
source='const args = {cmd:"printf dynamic",workdir:"/tmp"}; await tools.exec_command(args);'
set +e
printf '%s' "$(payload "$source")" | python3 "$ADAPTER" "$HOOK" >/dev/null 2>"$TMP_ROOT/dynamic.err"
rc=$?
set -e
assert_eq "dynamic exec arguments fail closed" "$rc" "2"
assert_eq "dynamic exec is denied before canonical hook" "$(wc -l < "$CAPTURE" | tr -d ' ')" "0"
if grep -q 'inline.*literal' "$TMP_ROOT/dynamic.err"; then
  echo "PASS: dynamic retry explains inline literal contract"
  PASS=$((PASS + 1))
else
  echo "FAIL: dynamic retry diagnostic missing"
  FAIL=$((FAIL + 1))
fi

# Alternate JavaScript spellings and dynamic evaluators must never make a real
# exec_command disappear into the no-call path.
for case_name in optional_computed comment_gap_computed template_interpolation unicode_member aliased_exec eval_string function_string global_eval computed_constructor reflected_constructor descriptor_constructor; do
  : > "$CAPTURE"
  case "$case_name" in
    optional_computed) source='await tools?.["exec_command"]({cmd:"printf hidden",workdir:"/tmp"});' ;;
    comment_gap_computed) source='await tools /* gap */ ["exec_command"]({cmd:"printf hidden",workdir:"/tmp"});' ;;
    template_interpolation) source='const hidden = `${tools[method]({cmd:"printf hidden",workdir:"/tmp"})}`;' ;;
    unicode_member) source='await tools.\u0065xec_command({cmd:"printf hidden",workdir:"/tmp"});' ;;
    aliased_exec) source='const run = tools.exec_command; await run({cmd:"printf hidden",workdir:"/tmp"});' ;;
    eval_string) source='eval("tools.exec_command({cmd:\"printf hidden\",workdir:\"/tmp\"})");' ;;
    function_string) source='Function("return tools.exec_command({cmd:\"printf hidden\",workdir:\"/tmp\"})")();' ;;
    global_eval) source='globalThis["eval"]("tools.exec_command({cmd:\"printf hidden\",workdir:\"/tmp\"})");' ;;
    computed_constructor) source='[]["filter"]["constructor"]("return tools.exec_command({cmd:\"printf hidden\",workdir:\"/tmp\"})")();' ;;
    reflected_constructor) source='Reflect.get(Reflect.get([], "filter"), "constructor")("return tools.exec_command({cmd:\"printf hidden\",workdir:\"/tmp\"})")();' ;;
    descriptor_constructor) source='Object.getOwnPropertyDescriptor([].filter, "constructor").value("return tools.exec_command({cmd:\"printf hidden\",workdir:\"/tmp\"})")();' ;;
  esac
  set +e
  printf '%s' "$(payload "$source")" | python3 "$ADAPTER" "$HOOK" >/dev/null 2>"$TMP_ROOT/$case_name.err"
  rc=$?
  set -e
  assert_eq "$case_name fails closed" "$rc" "2"
  assert_eq "$case_name runs no canonical hook" "$(wc -l < "$CAPTURE" | tr -d ' ')" "0"
done

for case_name in relative_workdir duplicate_cmd extra_argument; do
  : > "$CAPTURE"
  case "$case_name" in
    relative_workdir) source='await tools.exec_command({cmd:"printf x",workdir:"relative/path"});' ;;
    duplicate_cmd) source='await tools.exec_command({cmd:"printf x",cmd:"printf y",workdir:"/tmp"});' ;;
    extra_argument) source='await tools.exec_command({cmd:"printf x",workdir:"/tmp"}, true);' ;;
  esac
  set +e
  printf '%s' "$(payload "$source")" | python3 "$ADAPTER" "$HOOK" >/dev/null 2>"$TMP_ROOT/$case_name.err"
  rc=$?
  set -e
  assert_eq "$case_name fails closed" "$rc" "2"
  assert_eq "$case_name runs no canonical hook" "$(wc -l < "$CAPTURE" | tr -d ' ')" "0"
done

# Validation covers the complete source before the first hook invocation.
: > "$CAPTURE"
source='await tools.exec_command({cmd:"printf safe",workdir:"/tmp"}); const args={cmd:"printf hidden",workdir:"/tmp"}; await tools.exec_command(args);'
set +e
printf '%s' "$(payload "$source")" | python3 "$ADAPTER" "$HOOK" >/dev/null 2>"$TMP_ROOT/preflight.err"
rc=$?
set -e
assert_eq "later unresolved call blocks during preflight" "$rc" "2"
assert_eq "preflight failure runs no canonical hook" "$(wc -l < "$CAPTURE" | tr -d ' ')" "0"

# Every static call is preflighted, and one deny blocks the composite request.
: > "$CAPTURE"
source='const a = await tools.exec_command({"cmd":"printf first","workdir":"/tmp"}); const b = await tools.exec_command({"cmd":"gh pr edit 7","workdir":"/tmp"}); text(a.output); text(b.output);'
set +e
printf '%s' "$(payload "$source")" | python3 "$ADAPTER" "$HOOK" >/dev/null 2>"$TMP_ROOT/multi.err"
rc=$?
set -e
assert_eq "one denied nested command blocks composite request" "$rc" "2"
assert_eq "all nested calls are inspected in source order" \
  "$(jq -r '.command' "$CAPTURE" | paste -sd '|' -)" "printf first|gh pr edit 7"

# Advisory output from several calls remains one valid Codex hook response.
: > "$CAPTURE"
source='await tools.exec_command({"cmd":"ADVISE one","workdir":"/tmp"}); await tools.exec_command({"cmd":"ADVISE two","workdir":"/tmp"});'
out="$(printf '%s' "$(payload "$source")" | python3 "$ADAPTER" "$HOOK")"
assert_eq "multiple advisories merge deterministically" \
  "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext')" $'ADVISE one\nADVISE two'

# Malformed hook stdout is a control-plane error, not an advisory bypass.
BAD_HOOK="$TMP_ROOT/core/hooks/pre-bash-harness-main-only-guard.sh"
printf '#!/usr/bin/env bash\nprintf "not-json"\n' > "$BAD_HOOK"
chmod +x "$BAD_HOOK"
source='await tools.exec_command({cmd:"printf harmless",workdir:"/tmp"});'
set +e
printf '%s' "$(payload "$source")" | python3 "$ADAPTER" "$BAD_HOOK" >/dev/null 2>"$TMP_ROOT/bad-hook.err"
rc=$?
set -e
assert_eq "malformed canonical hook output fails closed" "$rc" "2"

for case_name in scalar_specific unknown_decision unknown_field null_decision null_specific null_context; do
  case "$case_name" in
    scalar_specific) hook_json='{"hookSpecificOutput":"corrupt"}' ;;
    unknown_decision) hook_json='{"decision":"allow"}' ;;
    unknown_field) hook_json='{"unexpected":true}' ;;
    null_decision) hook_json='{"decision":null}' ;;
    null_specific) hook_json='{"hookSpecificOutput":null}' ;;
    null_context) hook_json='{"additionalContext":null}' ;;
  esac
  printf '#!/usr/bin/env bash\nprintf %s %s\n' "'%s'" "'$hook_json'" > "$BAD_HOOK"
  chmod +x "$BAD_HOOK"
  set +e
  printf '%s' "$(payload "$source")" | python3 "$ADAPTER" "$BAD_HOOK" >/dev/null 2>"$TMP_ROOT/$case_name-output.err"
  rc=$?
  set -e
  assert_eq "$case_name canonical hook output fails closed" "$rc" "2"
done

# The peer-checkout guard runs through the adapter too (Codex code mode). It
# reads HARNESS_HOOK_OWNER_PID as the session owner, as codex-hook-adapter.sh
# exports it: the adapter's parent (the Codex process), never the adapter itself.
PEER_HOOK="$TMP_ROOT/core/hooks/pre-tool-peer-checkout-guard.sh"
cat > "$PEER_HOOK" <<'EOF'
#!/usr/bin/env bash
payload="$(cat)"
jq -cn --arg command "$(printf '%s' "$payload" | jq -r '.tool_input.command')" \
  --arg owner "${HARNESS_HOOK_OWNER_PID:-unset}" --arg runtime "${HARNESS_HOOK_RUNTIME:-}" \
  '{command:$command,owner:$owner,runtime:$runtime}' >> "$CAPTURE"
EOF
chmod +x "$PEER_HOOK"
: > "$CAPTURE"
set +e
OWNER_FILE="$TMP_ROOT/owner.pid"
bash -c 'echo $$ > "$1"; printf "%s" "$2" | python3 "$3" "$4" >/dev/null 2>"$5"; exit $?' _ \
  "$OWNER_FILE" "$(payload 'text(await tools.exec_command({cmd:"touch x",workdir:"/tmp"}));' code_mode_exec)" "$ADAPTER" "$PEER_HOOK" "$TMP_ROOT/peer.err"
rc=$?
set -e
assert_eq "peer-checkout guard is an allowlisted adapter target" "$rc" "0"
assert_eq "peer guard reaches the canonical hook once with the resolved command" \
  "$(jq -r '.command' "$CAPTURE" | tr '\n' ' ')" "touch x "
assert_eq "peer guard receives the adapter parent pid as owner" \
  "$(jq -r '.owner' "$CAPTURE")" "$(cat "$OWNER_FILE")"
assert_eq "peer guard keeps the codex runtime marker" "$(jq -r '.runtime' "$CAPTURE")" "codex"
UNLISTED="$TMP_ROOT/core/hooks/pre-bash-unlisted.sh"
cp "$PEER_HOOK" "$UNLISTED"
set +e
printf '%s' "$(payload 'touch x' Bash)" | python3 "$ADAPTER" "$UNLISTED" >/dev/null 2>&1
rc=$?
set -e
assert_eq "unlisted hook is still refused" "$rc" "2"

echo "---"
echo "passed: $PASS, failed: $FAIL"
[[ $FAIL -eq 0 ]]
