#!/usr/bin/env bash
set -euo pipefail

# Break protected: a bare interactive launcher choice must select an isolated
# workspace by default, allow an intentional shared choice, and cancel before
# any launcher allocation can begin. This drives the real terminal UI.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHOOSER="$ROOT/bin/harness-workspace-choice"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

choose() {
  local input="$1" output="$2"
  expect <<EXPECT >"$output" 2>&1
set timeout 5
log_user 0
spawn "$CHOOSER"
expect {
  "Workspace" {}
  timeout { exit 97 }
  eof { exit 98 }
}
send -- "$input"
expect eof
puts -nonewline \$expect_out(buffer)
catch wait result
exit [lindex \$result 3]
EXPECT
}

choose '1\r' "$TMP/isolated.out"
tr -d '\r' < "$TMP/isolated.out" | grep -qx 'isolated' || {
  echo 'FAIL: choice 1 must return the isolated workspace enum' >&2
  cat "$TMP/isolated.out" >&2
  exit 1
}

choose '2\r' "$TMP/shared.out"
tr -d '\r' < "$TMP/shared.out" | grep -qx 'shared' || {
  echo 'FAIL: choice 2 must return the shared workspace enum' >&2
  cat "$TMP/shared.out" >&2
  exit 1
}

if choose 'q\r' "$TMP/cancel.out"; then
  echo 'FAIL: q must cancel without selecting a workspace' >&2
  exit 1
fi
if tr -d '\r' < "$TMP/cancel.out" | grep -qxE 'isolated|shared'; then
  echo 'FAIL: cancel must not emit a workspace enum' >&2
  cat "$TMP/cancel.out" >&2
  exit 1
fi

if choose '\004' "$TMP/eof.out"; then
  echo 'FAIL: EOF must cancel without selecting a workspace' >&2
  exit 1
fi
if tr -d '\r' < "$TMP/eof.out" | grep -qxE 'isolated|shared'; then
  echo 'FAIL: EOF must not emit a workspace enum' >&2
  cat "$TMP/eof.out" >&2
  exit 1
fi

if choose '\003' "$TMP/sigint.out"; then
  echo 'FAIL: Ctrl-C must preserve its interrupt exit status' >&2
  exit 1
else
  sigint_status=$?
fi
[[ "$sigint_status" -eq 130 ]] || {
  echo "FAIL: Ctrl-C must return 130, got $sigint_status" >&2
  exit 1
}

if "$CHOOSER" </dev/null >"$TMP/non-tty.out" 2>"$TMP/non-tty.err"; then
  echo 'FAIL: a non-interactive bare launch must require an explicit selector' >&2
  exit 1
fi
grep -Fqx 'harness-workspace-choice: interactive terminal required' "$TMP/non-tty.err" || {
  echo 'FAIL: non-interactive rejection must explain the explicit-selector requirement' >&2
  cat "$TMP/non-tty.err" >&2
  exit 1
}

echo 'PASS: workspace choice returns a pure early isolation selector'
