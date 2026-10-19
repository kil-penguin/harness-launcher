#!/usr/bin/env bash
# test-restore-probe.sh — harness-restore-probe reads the model/effort of a
# session from its Claude transcript or Codex rollout. It is hardened (no
# symlink or non-regular file, bounded read, model shape) and never fails.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/bin/harness-common.sh"
PY="$(harness_python3_resolve)" || exit 1
PROBE="$ROOT/bin/harness-restore-probe"
TMP="$(mktemp -d)"
trap 'find "$TMP" -delete' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$PROBE" ]] || fail 'probe command is missing'

# probe <claude|codex> <file> : sets OUT and RC
probe() { OUT="$("$PY" "$PROBE" "$1" "$2" 2>&1)"; RC=$?; }
expect() { # <label> <expected output>
  [[ $RC == 0 ]] || fail "$1: exit $RC"
  [[ "$OUT" == "$2" ]] || fail "$1: got [$OUT] want [$2]"
}
asst() { # <model> [extra json fields]
  printf '{"type":"assistant","isSidechain":false,"message":{"model":"%s"}%s}\n' "$1" "${2:-}"
}

# --- Claude transcript ------------------------------------------------------
T="$TMP/claude.jsonl"
{
  asst claude-sonnet-5-5 ',"effort":"high"'
  asst claude-opus-5-5 ',"effort":"xhigh"'
  asst '<synthetic>' ',"effort":"low"'
  asst gpt-5.6-terra ',"effort":"low"'
  printf '{"type":"assistant","isSidechain":true,"message":{"model":"claude-haiku-4-5-20251001"},"effort":"low"}\n'
  printf '{"type":"cost-state","modelUsage":{"claude-opus-5-5[1m]":{"inputTokens":1}}}\n'
} > "$T"
probe claude "$T"
expect 'claude last main-thread model/effort with [1m]' $'model=claude-opus-5-5[1m]\neffort=xhigh'

# no cost-state entry for the [1m] variant: plain model
grep -v cost-state "$T" > "$TMP/no1m.jsonl"
probe claude "$TMP/no1m.jsonl"
expect 'claude without [1m] usage' $'model=claude-opus-5-5\neffort=xhigh'

# a [1m] cost-state for another model does not decorate this one
{ asst claude-sonnet-5-5 ',"effort":"medium"'; printf '{"type":"cost-state","modelUsage":{"claude-opus-5-5[1m]":{}}}\n'; } > "$TMP/other1m.jsonl"
probe claude "$TMP/other1m.jsonl"
expect 'claude [1m] of another model' $'model=claude-sonnet-5-5\neffort=medium'

# model with a leading dash / bad shape / non-claude / invalid effort: skipped
{ asst '-claude-evil' ',"effort":"high"'; asst 'claude-x y' ; asst 'claude-ok-1' ',"effort":"turbo"'; } > "$TMP/bad.jsonl"
probe claude "$TMP/bad.jsonl"
expect 'claude bad model/effort skipped' 'model=claude-ok-1'

# malformed lines and empty files are ignored; output stays empty
printf 'not json\n{"type":\n\x00\xff\n' > "$TMP/malformed.jsonl"
probe claude "$TMP/malformed.jsonl"; expect 'malformed' ''
: > "$TMP/empty.jsonl"
probe claude "$TMP/empty.jsonl"; expect 'empty' ''
probe claude "$TMP/missing.jsonl"; expect 'missing' ''
probe claude ''; expect 'empty path' ''
probe bogus "$T"; expect 'unknown agent' ''

# symlink and FIFO are never read (and the FIFO does not block)
ln -s "$T" "$TMP/link.jsonl"
probe claude "$TMP/link.jsonl"; expect 'symlink' ''
mkfifo "$TMP/fifo.jsonl"
probe claude "$TMP/fifo.jsonl"; expect 'fifo' ''
mkdir "$TMP/dir.jsonl"
probe claude "$TMP/dir.jsonl"; expect 'directory' ''

# only the last 8 MiB is read; the partial first line is dropped
"$PY" - "$TMP/big.jsonl" <<'PYEOF'
import sys
pad = '{"type":"user","message":{"content":"' + 'x' * 1000 + '"}}\n'
head = '{"type":"assistant","isSidechain":false,"message":{"model":"claude-old-1"},"effort":"low"}\n'
with open(sys.argv[1], 'w') as f:
    f.write(head)
    for _ in range((9 * 1024 * 1024) // len(pad)):
        f.write(pad)
    f.write('{"type":"assistant","isSidechain":false,"message":{"model":"claude-new-1"},"effort":"max"}\n')
PYEOF
probe claude "$TMP/big.jsonl"
expect 'big file: only the tail' $'model=claude-new-1\neffort=max'
# the window starts exactly at the "{" of a line whose prefix is junk: that
# first (partial) line must be dropped, not parsed
"$PY" - "$TMP/cut.jsonl" <<'PYEOF'
import sys
line = '{"type":"assistant","isSidechain":false,"message":{"model":"claude-cut-1"},"effort":"low"}\n'
window = 8 * 1024 * 1024
with open(sys.argv[1], 'w') as f:
    f.write('JUNK' + line)
    f.write('{"type":"user","x":"' + 'z' * (window - len(line) - len('{"type":"user","x":""}\n')) + '"}\n')
PYEOF
probe claude "$TMP/cut.jsonl"; expect 'partial first line dropped' ''

# L7: a window that starts exactly on a line boundary keeps its first line.
"$PY" - "$TMP/edge.jsonl" <<'PYEOF'
import sys
line = '{"type":"assistant","isSidechain":false,"message":{"model":"claude-edge-1"},"effort":"low"}\n'
window = 8 * 1024 * 1024
junk = '{"type":"user","x":"old"}\n'
with open(sys.argv[1], 'w') as f:
    f.write(junk + line)
    f.write('{"type":"user","x":"' + 'z' * (window - len(line) - len('{"type":"user","x":""}\n')) + '"}\n')
PYEOF
probe claude "$TMP/edge.jsonl"; expect 'L7 window starting on a line boundary keeps its first line' $'model=claude-edge-1\neffort=low'
# L7: non-string values are ignored, not fatal (a list-valued effort used to raise)
{ printf '{"type":"assistant","isSidechain":false,"message":{"model":"claude-ok-1"},"effort":["high"]}\n'; } > "$TMP/listeffort.jsonl"
probe claude "$TMP/listeffort.jsonl"; expect 'L7 list-valued effort' 'model=claude-ok-1'
{ printf '{"type":"assistant","isSidechain":false,"message":{"model":"claude-ok-2"},"effort":{"x":1}}\n'; } > "$TMP/dicteffort.jsonl"
probe claude "$TMP/dicteffort.jsonl"; expect 'L7 dict-valued effort' 'model=claude-ok-2'
{ printf '{"type":"turn_context","payload":{"model":"gpt-6-sol","effort":["low"]}}\n'; } > "$TMP/listcodex.jsonl"
probe codex "$TMP/listcodex.jsonl"; expect 'L7 list-valued codex effort' 'model=gpt-6-sol'

# 1M evidence from usage: a main-thread turn whose input + cache read + cache
# creation tokens exceed 200000 can only have run with a 1M context, even when no
# cost-state record survives in the window.
usage_line() { # <sidechain true|false> <input> <cache_read> <cache_creation>
  printf '{"type":"assistant","isSidechain":%s,"message":{"model":"claude-opus-5-5","usage":{"input_tokens":%s,"cache_read_input_tokens":%s,"cache_creation_input_tokens":%s}},"effort":"high"}\n' "$1" "$2" "$3" "$4"
}
usage_line false 2 150000 50001 > "$TMP/u-over.jsonl"
probe claude "$TMP/u-over.jsonl"; expect 'usage total 200003 implies 1M' $'model=claude-opus-5-5[1m]\neffort=high'
usage_line false 0 100000 100000 > "$TMP/u-exact.jsonl"
probe claude "$TMP/u-exact.jsonl"; expect 'usage total exactly 200000 is not 1M' $'model=claude-opus-5-5\neffort=high'
{ usage_line false 5 1000 1000; usage_line true 5 300000 0; } > "$TMP/u-side.jsonl"
probe claude "$TMP/u-side.jsonl"; expect 'sidechain usage is ignored' $'model=claude-opus-5-5\neffort=high'
{ usage_line false 5 1000 1000; printf '{"type":"assistant","isSidechain":false,"message":{"model":"claude-opus-5-5","usage":{"input_tokens":"999999","cache_read_input_tokens":[300000],"cache_creation_input_tokens":true}},"effort":"high"}\n'; } > "$TMP/u-bad.jsonl"
probe claude "$TMP/u-bad.jsonl"; expect 'non-int usage is ignored' $'model=claude-opus-5-5\neffort=high'
{ usage_line false 5 1000 1000; printf '{"type":"assistant","isSidechain":false,"message":{"model":"claude-opus-5-5","usage":{"input_tokens":-300000,"cache_read_input_tokens":400000,"cache_creation_input_tokens":0}},"effort":"high"}\n'; } > "$TMP/u-neg.jsonl"
probe claude "$TMP/u-neg.jsonl"; expect 'a record with a negative usage field is ignored' $'model=claude-opus-5-5\neffort=high'
# the earlier big turn still counts after a later small one (the maximum, not the last)
{ usage_line false 2 250000 0; usage_line false 2 1000 0; } > "$TMP/u-max.jsonl"
probe claude "$TMP/u-max.jsonl"; expect 'the maximum usage counts, not the last' $'model=claude-opus-5-5[1m]\neffort=high'
# a model that already carries [1m] via cost-state is not doubled
{ usage_line false 2 250000 0; printf '{"type":"cost-state","modelUsage":{"claude-opus-5-5[1m]":{}}}\n'; } > "$TMP/u-both.jsonl"
probe claude "$TMP/u-both.jsonl"; expect 'both signals give one [1m]' $'model=claude-opus-5-5[1m]\neffort=high'

# --- Codex rollout -----------------------------------------------------------
R="$TMP/rollout.jsonl"
{
  printf '{"type":"turn_context","payload":{"model":"gpt-6-sol","effort":"low","approval_policy":"never","sandbox_policy":{"type":"danger-full-access"}}}\n'
  printf '{"type":"event_msg","payload":{"model_context_window":258400}}\n'
  printf '{"type":"turn_context","payload":{"model":"gpt-6.1-sol","effort":"medium","approval_policy":"never","sandbox_policy":{"type":"danger-full-access"}}}\n'
} > "$R"
probe codex "$R"
expect 'codex last turn_context' $'model=gpt-6.1-sol\neffort=medium'
{ cat "$R"; printf '{"type":"event_msg","payload":{"info":{"model_context_window":950000}}}\n'; } > "$TMP/r1m.jsonl"
probe codex "$TMP/r1m.jsonl"
expect 'codex 1m context' $'model=gpt-6.1-sol\neffort=medium'
{ printf '{"type":"turn_context","payload":{"model":"-bad","effort":"minimal"}}\n'; } > "$TMP/rbad.jsonl"
probe codex "$TMP/rbad.jsonl"; expect 'codex leading dash model, minimal effort' 'effort=minimal'
# permissions are never read from a rollout, whatever it claims
{ printf '{"type":"turn_context","payload":{"model":"gpt-6-sol","approval_policy":"never","sandbox_policy":{"type":"danger-full-access"}}}\n'; } > "$TMP/rforge.jsonl"
probe codex "$TMP/rforge.jsonl"; expect 'codex forged permissions not emitted' 'model=gpt-6-sol'
{ printf '{"type":"assistant","permissionMode":"bypassPermissions","message":{"model":"claude-opus-5-5"}}\n'; } > "$TMP/cforge.jsonl"
probe claude "$TMP/cforge.jsonl"; expect 'claude forged permissionMode not emitted' 'model=claude-opus-5-5'
ln -s "$R" "$TMP/rlink.jsonl"
probe codex "$TMP/rlink.jsonl"; expect 'codex symlink' ''

echo 'PASS: restore probe'
