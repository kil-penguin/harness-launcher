#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
TMP="$(cd "$TMP" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT
PREFIX="$TMP/prefix"
HOME_DIR="$TMP/home"
BIN_DIR="$HOME_DIR/.local/bin"
PROFILE_HOME="$HOME_DIR/.config/harness-launcher"
ALPHA="$HOME_DIR/alpha harness"
GAMMA="$HOME_DIR/gamma harness"
ALPHA_WORKTREE="$ALPHA/.worktrees/project/task"
GAMMA_PROJECT="$GAMMA/projects/service"
LOG="$TMP/auto.log"

mkdir -p "$ALPHA/config" "$GAMMA/config" "$ALPHA_WORKTREE" "$GAMMA_PROJECT" "$BIN_DIR"
cat > "$ALPHA/config/launcher.env" <<'EOF'
HARNESS_NAME="alpha test"
HARNESS_PREFIX="alpha"
EOF
cat > "$GAMMA/config/launcher.env" <<'EOF'
HARNESS_NAME="gamma test"
HARNESS_PREFIX="gamma"
EOF

bash "$ROOT/test/lib/install-runtime-fixture.sh" "$ROOT" "$PREFIX"
HOME="$HOME_DIR" HARNESS_PROFILE_BIN_DIR="$BIN_DIR" \
  "$PREFIX/bin/harness-profile" register "$ALPHA" >/dev/null
HOME="$HOME_DIR" HARNESS_PROFILE_BIN_DIR="$BIN_DIR" \
  "$PREFIX/bin/harness-profile" register "$GAMMA" >/dev/null

cat > "$PREFIX/share/harness-launcher/harness-exec" <<'EOF'
#!/usr/bin/env bash
{
  printf 'HARNESS:%s\n' "$1"
  printf 'PWD:%s\n' "$(pwd -P)"
  shift
  printf 'ARGV:'
  printf ' <%s>' "$@"
  printf '\n'
} > "$HARNESS_AUTO_TEST_LOG"
EOF
chmod 755 "$PREFIX/share/harness-launcher/harness-exec"

: > "$LOG"
(
  cd "$ALPHA_WORKTREE"
  HOME="$HOME_DIR" HARNESS_AUTO_TEST_LOG="$LOG" \
    "$PREFIX/bin/harness-auto" codex base
)
ALPHA_REAL="$(cd "$ALPHA" && pwd -P)"
ALPHA_WORKTREE_REAL="$(cd "$ALPHA_WORKTREE" && pwd -P)"
grep -Fqx "HARNESS:$ALPHA_REAL" "$LOG" || {
  echo "FAIL: harness-auto did not select alpha from the current worktree" >&2
  exit 1
}
grep -Fqx "PWD:$ALPHA_WORKTREE_REAL" "$LOG" || {
  echo "FAIL: harness-auto changed the current worktree" >&2
  exit 1
}
grep -Fqx 'ARGV: <codex> <base>' "$LOG" || {
  echo "FAIL: harness-auto changed launcher argument order" >&2
  exit 1
}

echo "PASS: harness-auto selects alpha from a nested worktree"

: > "$LOG"
(
  cd "$ALPHA_WORKTREE"
  HOME="$HOME_DIR" HARNESS_AUTO_TEST_LOG="$LOG" \
    "$PREFIX/bin/harness-auto" claude base --passthrough --permission-mode plan --effort high
)
grep -Fqx 'ARGV: <claude> <base> <--passthrough> <--permission-mode> <plan> <--effort> <high>' "$LOG" || {
  echo "FAIL: harness-auto changed SDK passthrough arguments" >&2
  cat "$LOG" >&2
  exit 1
}

echo "PASS: harness-auto forwards --passthrough arguments unchanged"

: > "$LOG"
EXPLAIN="$(
  cd "$ALPHA_WORKTREE"
  HOME="$HOME_DIR" "$PREFIX/bin/harness-auto" --explain codex
)"
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["profile"] == "alpha"; assert d["agent"] == "codex"; assert d["reason"] == "registered-ancestor"' "$EXPLAIN"
[[ ! -s "$LOG" ]] || {
  echo "FAIL: harness-auto --explain launched an agent" >&2
  exit 1
}
echo "PASS: harness-auto --explain reports the selected profile without launch"

MINIMAL_EXPLAIN="$(
  cd "$ALPHA_WORKTREE"
  PATH=/usr/bin:/bin HOME="$HOME_DIR" "$PREFIX/bin/harness-auto" --explain codex
)"
python3 -c 'import json,sys; assert json.loads(sys.argv[1])["profile"] == "alpha"' "$MINIMAL_EXPLAIN"
echo "PASS: harness-auto resolves Python with a minimal PATH"

if (
  cd "$ALPHA_WORKTREE"
  HOME="$HOME_DIR" HARNESS_AUTO_TEST_LOG="$LOG" \
    "$PREFIX/bin/harness-auto" --profile gamma codex base
) >"$TMP/mismatch.out" 2>"$TMP/mismatch.err"; then
  echo "FAIL: harness-auto accepted an explicit profile from another boundary" >&2
  exit 1
fi
grep -Fq 'does not own the current directory' "$TMP/mismatch.err" || {
  echo "FAIL: harness-auto did not explain explicit profile mismatch" >&2
  exit 1
}
[[ ! -s "$LOG" ]] || {
  echo "FAIL: harness-auto launched an agent after profile mismatch" >&2
  exit 1
}
echo "PASS: harness-auto rejects mismatched explicit profiles"

: > "$LOG"
(
  cd "$GAMMA_PROJECT"
  HOME="$HOME_DIR" HARNESS_AUTO_TEST_LOG="$LOG" \
    "$PREFIX/bin/harness-auto" kiro-cli base
)
GAMMA_REAL="$(cd "$GAMMA" && pwd -P)"
GAMMA_PROJECT_REAL="$(cd "$GAMMA_PROJECT" && pwd -P)"
grep -Fqx "HARNESS:$GAMMA_REAL" "$LOG" || {
  echo "FAIL: harness-auto did not select gamma from a source project" >&2
  exit 1
}
grep -Fqx "PWD:$GAMMA_PROJECT_REAL" "$LOG" || {
  echo "FAIL: harness-auto changed the source project directory" >&2
  exit 1
}
grep -Fqx 'ARGV: <kiro-cli> <base>' "$LOG" || {
  echo "FAIL: harness-auto changed Kiro argument order" >&2
  exit 1
}

echo "PASS: harness-auto selects gamma from a source project"

: > "$LOG"
(
  cd "$ALPHA_WORKTREE"
  HOME="$HOME_DIR" HARNESS_AUTO_TEST_LOG="$LOG" \
    "$PREFIX/bin/harness-auto" claude --resume session-123
)
grep -Fqx "HARNESS:$ALPHA_REAL" "$LOG" || {
  echo "FAIL: harness-auto did not select alpha for Claude" >&2
  exit 1
}
grep -Fqx 'ARGV: <claude> <--resume> <session-123>' "$LOG" || {
  echo "FAIL: harness-auto dropped its raw Claude selector before launcher entry" >&2
  exit 1
}

echo "PASS: harness-auto maps the Claude selector to the direct launcher path"

: > "$LOG"
if (
  cd "$TMP"
  HOME="$HOME_DIR" HARNESS_AUTO_TEST_LOG="$LOG" \
    "$PREFIX/bin/harness-auto" codex base
) >"$TMP/outside.out" 2>"$TMP/outside.err"; then
  echo "FAIL: harness-auto selected a profile outside all registered boundaries" >&2
  exit 1
fi
grep -Fq 'no registered harness contains the current directory' "$TMP/outside.err" || {
  echo "FAIL: harness-auto did not explain the unmatched workspace" >&2
  exit 1
}
[[ ! -s "$LOG" ]] || {
  echo "FAIL: harness-auto executed a profile for an unmatched workspace" >&2
  exit 1
}

echo "PASS: harness-auto fails closed outside registered boundaries"

ln -s "$TMP" "$ALPHA/.worktrees/escape"
if (
  cd "$ALPHA/.worktrees/escape"
  HOME="$HOME_DIR" HARNESS_AUTO_TEST_LOG="$LOG" \
    "$PREFIX/bin/harness-auto" codex base
) >"$TMP/escape.out" 2>"$TMP/escape.err"; then
  echo "FAIL: harness-auto followed a symlink outside the harness boundary" >&2
  exit 1
fi

echo "PASS: harness-auto rejects symlink escapes"

NESTED="$ALPHA/projects/nested-harness"
NESTED_WORKTREE="$NESTED/.worktrees/task"
mkdir -p "$NESTED/config" "$NESTED_WORKTREE"
cat > "$NESTED/config/launcher.env" <<'EOF'
HARNESS_NAME="nested test"
HARNESS_PREFIX="nested"
EOF
HOME="$HOME_DIR" HARNESS_PROFILE_BIN_DIR="$BIN_DIR" \
  "$PREFIX/bin/harness-profile" register "$NESTED" >/dev/null
: > "$LOG"
(
  cd "$NESTED_WORKTREE"
  HOME="$HOME_DIR" HARNESS_AUTO_TEST_LOG="$LOG" \
    "$PREFIX/bin/harness-auto" codex base
)
NESTED_REAL="$(cd "$NESTED" && pwd -P)"
grep -Fqx "HARNESS:$NESTED_REAL" "$LOG" || {
  echo "FAIL: harness-auto did not select the longest matching boundary" >&2
  exit 1
}

echo "PASS: harness-auto selects the most specific registered harness"

printf '%s\n' "$ALPHA_REAL" > "$PROFILE_HOME/profiles/alpha-duplicate"
chmod 600 "$PROFILE_HOME/profiles/alpha-duplicate"
if (
  cd "$ALPHA_WORKTREE"
  HOME="$HOME_DIR" HARNESS_AUTO_TEST_LOG="$LOG" \
    "$PREFIX/bin/harness-auto" codex base
) >"$TMP/ambiguous.out" 2>"$TMP/ambiguous.err"; then
  echo "FAIL: harness-auto accepted duplicate matching registrations" >&2
  exit 1
fi
grep -Fq 'ambiguous registered harness boundary' "$TMP/ambiguous.err" || {
  echo "FAIL: harness-auto did not explain ambiguous registrations" >&2
  exit 1
}
rm "$PROFILE_HOME/profiles/alpha-duplicate"

echo "PASS: harness-auto rejects ambiguous profile registrations"

UNTRUSTED="$HOME_DIR/untrusted-harness"
mkdir -p "$UNTRUSTED/config" "$UNTRUSTED/projects/app"
cat > "$UNTRUSTED/config/launcher.env" <<'EOF'
HARNESS_NAME="untrusted"
HARNESS_PREFIX="untrusted"
EOF
printf '%s\n' "$UNTRUSTED" > "$TMP/untrusted-profile"
ln -s "$TMP/untrusted-profile" "$PROFILE_HOME/profiles/untrusted"
if (
  cd "$UNTRUSTED/projects/app"
  HOME="$HOME_DIR" HARNESS_AUTO_TEST_LOG="$LOG" \
    "$PREFIX/bin/harness-auto" codex base
) >"$TMP/untrusted.out" 2>"$TMP/untrusted.err"; then
  echo "FAIL: harness-auto followed a symlinked registry entry" >&2
  exit 1
fi

echo "PASS: harness-auto ignores untrusted registry symlinks"

if (
  cd "$ALPHA_WORKTREE"
  HOME="$HOME_DIR" HARNESS_AUTO_TEST_LOG="$LOG" \
    "$PREFIX/bin/harness-auto"
) >"$TMP/no-args.out" 2>"$TMP/no-args.err"; then
  echo "FAIL: harness-auto accepted a missing agent command" >&2
  exit 1
fi
grep -Fq 'agent command is required' "$TMP/no-args.err" || {
  echo "FAIL: harness-auto did not explain the missing agent command" >&2
  exit 1
}

echo "PASS: harness-auto requires an explicit agent command"

if (
  cd "$ALPHA_WORKTREE"
  HOME="$HOME_DIR" HARNESS_AUTO_TEST_LOG="$LOG" \
    "$PREFIX/bin/harness-auto" unsupported-agent
) >"$TMP/unsupported.out" 2>"$TMP/unsupported.err"; then
  echo "FAIL: harness-auto accepted an unsupported agent selector" >&2
  exit 1
fi
grep -Fq 'unsupported agent: unsupported-agent' "$TMP/unsupported.err" || {
  echo "FAIL: harness-auto did not explain the unsupported agent" >&2
  exit 1
}

echo "PASS: harness-auto rejects unsupported agent selectors"

# Every Codex working-directory form is validated, also after --passthrough;
# tokens after `--` are prompt text.
OUTSIDE_DIR="$TMP/outside-cd"
mkdir -p "$OUTSIDE_DIR"
for form in "-C$OUTSIDE_DIR" "-C=$OUTSIDE_DIR" "--cd=$OUTSIDE_DIR" "--passthrough -C $OUTSIDE_DIR" \
  "--passthrough -C$OUTSIDE_DIR"; do
  : > "$LOG"
  rc=0
  # shellcheck disable=SC2086
  ( cd "$ALPHA_WORKTREE"; HOME="$HOME_DIR" HARNESS_AUTO_TEST_LOG="$LOG" \
      "$PREFIX/bin/harness-auto" codex $form exec x ) >/dev/null 2>"$TMP/cd-form.err" || rc=$?
  [[ "$rc" == 2 && ! -s "$LOG" ]] || { echo "FAIL: harness-auto accepted outside Codex dir: $form" >&2; exit 1; }
  grep -Fq 'outside selected harness boundary' "$TMP/cd-form.err" || {
    echo "FAIL: harness-auto did not explain outside Codex dir: $form" >&2; cat "$TMP/cd-form.err" >&2; exit 1; }
done
accept_codex_args() {
  : > "$LOG"
  ( cd "$ALPHA_WORKTREE"; HOME="$HOME_DIR" HARNESS_AUTO_TEST_LOG="$LOG" \
      "$PREFIX/bin/harness-auto" codex "$@" ) || { echo "FAIL: harness-auto rejected: $*" >&2; exit 1; }
  grep -Fq 'ARGV: <codex>' "$LOG" || { echo "FAIL: harness-auto did not launch: $*" >&2; exit 1; }
}
accept_codex_args "-C$ALPHA_WORKTREE" exec x
accept_codex_args "-C=$ALPHA_WORKTREE" exec x
accept_codex_args --passthrough exec -- -C "$OUTSIDE_DIR"

echo "PASS: harness-auto validates attached and post-marker Codex -C forms"

# A retained auto agent belongs after the recognized leading workspace controls.
: > "$LOG"
(
  cd "$ALPHA_WORKTREE"
  HOME="$HOME_DIR" HARNESS_AUTO_TEST_LOG="$LOG" \
    "$PREFIX/bin/harness-auto" claude --cwd "$ALPHA_WORKTREE" --isolated-session aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa claude -- text
)
grep -Fqx "ARGV: <--cwd> <$ALPHA_WORKTREE> <--isolated-session> <aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa> <claude> <claude> <--> <text>" "$LOG" || {
  echo 'FAIL: auto hid cwd/owner controls or consumed the caller claude' >&2
  exit 1
}
echo 'PASS: auto controls precede the retained agent and literal caller tail'
