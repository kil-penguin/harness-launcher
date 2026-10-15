#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
bash "$ROOT/test/lib/install-runtime-fixture.sh" "$ROOT" "$TMP/prefix"
CMD="$TMP/prefix/bin/harness-preserve"
[[ -x "$CMD" ]]
"$CMD" --help >/dev/null
marker="$TMP/replaced"
fake="$TMP/fake"; mkdir "$fake"
printf '#!/bin/sh\n/usr/bin/touch "%s"\n' "$marker" > "$fake/python3"; chmod 755 "$fake/python3"
printf '/usr/bin/touch "%s"\n' "$marker" > "$fake/.zshenv"
printf 'import pathlib\npathlib.Path("%s").touch()\n' "$marker" > "$fake/startup"
mkdir "$fake/pathlib"; printf 'raise SystemExit(99)\n' > "$fake/pathlib/__init__.py"
"$fake/python3"
rm "$marker"
set +e
out="$(printf '{}' | PATH="$fake" HARNESS_PYTHON_BIN="$fake/python3" ZDOTDIR="$fake" PYTHONPATH="$fake" PYTHONHOME="$fake" PYTHONSTARTUP="$fake/startup" "$CMD" 2>"$TMP/stderr")"; status=$?
set -e
[[ $status -eq 2 && "$out" == '{"status": "rejected"}' && ! -s "$TMP/stderr" && ! -e "$marker" ]]
rm "$TMP/prefix/share/harness-launcher/git_policy.py"
set +e
printf '{}' | "$CMD" >/dev/null 2>"$TMP/broken"; broken=$?
set -e
[[ $broken -ne 0 ]]
