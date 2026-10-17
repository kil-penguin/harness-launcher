#!/usr/bin/env zsh
# harness-launcher — generic zsh launcher function + tab completion
# Usage:
#   source /path/to/harness-launcher/bin/aliases.zsh
#   harness_register /path/to/some-harness

_HARNESS_LAUNCHER_BIN="$(cd "$(dirname "${(%):-%x}")" 2>/dev/null && pwd)"
_HARNESS_SLACK_POLICY="$_HARNESS_LAUNCHER_BIN/slack-approval-policy.py"
typeset -ga _HARNESS_LAUNCHER_REGISTERED_DIRS=()
if (( ${+parameters[_HARNESS_LAUNCHER_SHELL_CLAUDE_OWNED]} )) && \
    [[ "${(t)_HARNESS_LAUNCHER_SHELL_CLAUDE_OWNED}" == *-export* ]]; then
  unset _HARNESS_LAUNCHER_SHELL_CLAUDE_OWNED
fi
typeset -gi _HARNESS_LAUNCHER_SHELL_CLAUDE_OWNED="${_HARNESS_LAUNCHER_SHELL_CLAUDE_OWNED:-0}"
typeset -g +x _HARNESS_LAUNCHER_SHELL_CLAUDE_OWNED
if (( _HARNESS_LAUNCHER_SHELL_CLAUDE_OWNED )); then
  typeset -gi _HARNESS_LAUNCHER_SHELL_AUTO_ENABLED="${_HARNESS_LAUNCHER_SHELL_AUTO_ENABLED:-1}"
else
  typeset -gi _HARNESS_LAUNCHER_SHELL_AUTO_ENABLED=0
fi
typeset -g +x _HARNESS_LAUNCHER_SHELL_AUTO_ENABLED
typeset -g _HARNESS_LAUNCHER_CODEX_WRAPPER_BODY=""

# Single source of truth for mode tables, bin resolution, probes, MCP config
# validation, secrets export, and autocompact PCT — shared with launcher.sh.
source "$_HARNESS_LAUNCHER_BIN/harness-common.sh"
# Hook commands and the Claude launch-record hook name files here; keep them on
# the upgrade-stable `opt` spelling even when entered through a Cellar path.
_HARNESS_LAUNCHER_BIN="$(harness_launcher_stable_dir "$_HARNESS_LAUNCHER_BIN")"

_harness_launcher_codex_bin() { harness_codex_bin_resolve "$@"; }

_harness_launcher_probe_provider_health() { harness_probe_health "$@"; }

_harness_launcher_mcp_local_configs() { harness_mcp_local_configs "$@"; }

_harness_launcher_validate_mcp_local_configs() { harness_validate_mcp_local_configs "$@"; }

_harness_launcher_isolated_session_create() {
  local source_root="$1" session_id="${2:-}" creation="${3:-}" output key value
  if [[ -n "$session_id" ]]; then
    output="$("$_HARNESS_LAUNCHER_BIN/session-isolation.sh" resume "$source_root" "$session_id")" || return $?
  elif [[ "$creation" == local ]]; then
    output="$("$_HARNESS_LAUNCHER_BIN/session-isolation.sh" create --local "$source_root")" || return $?
  else
    output="$("$_HARNESS_LAUNCHER_BIN/session-isolation.sh" create "$source_root")" || return $?
  fi
  while IFS='=' read -r key value; do
    case "$key" in
      HARNESS_SESSION_ID|HARNESS_SOURCE_ROOT|HARNESS_SESSION_ROOT) export "$key=$value" ;;
    esac
  done <<< "$output"
}

# _harness_launcher_session_is_current <session-dir> <source-root>
#   True for a UUID-named, non-symlink session directory whose source-root is a
#   regular non-symlink file that resolves to the current harness.
_harness_launcher_session_is_current() {
  local dir="$1" source_root="$2" recorded
  local uuid_re='^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'
  [[ "${dir:t}" =~ $uuid_re && -d "$dir" && ! -L "$dir" && -f "$dir/source-root" && ! -L "$dir/source-root" ]] || return 1
  recorded="$(<"$dir/source-root")"
  [[ -d "$recorded" && "${recorded:A}" == "$source_root" ]]
}

# _harness_launcher_session_records_claude <provider-sessions file> <lowercase id>
#   True when the regular non-symlink file has a line exactly `claude <id>`
#   (id compared case-insensitively); every other line is ignored.
_harness_launcher_session_records_claude() {
  local file="$1" id="$2" line
  [[ -f "$file" && ! -L "$file" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "${(L)line}" == "claude $id" && "$line" == "claude "* ]] && return 0
  done < "$file"
  return 1
}

# A restored parent is lookup material for an explicit fork, not a native
# session owned by the new workspace. The private ledger binds that import.
_harness_launcher_session_imported_codex() {
  local python
  [[ -e "$1/restored-native-sessions" || -L "$1/restored-native-sessions" ]] || return 1
  python="$(harness_python3_resolve)" || return 2
  "$python" -B -c 'import sys; from pathlib import Path
sys.path.insert(0,sys.argv[1])
from harness_session_catalog import imported_ids
try: imports=imported_ids(Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4],strict=True)
except ValueError: raise SystemExit(2)
raise SystemExit(0 if sys.argv[5].lower() in imports else 1)' \
    "$_HARNESS_LAUNCHER_BIN" "$1" "$2" "$3" "$4"
}

# _harness_launcher_claude_transcript_exists <state-home> <session name> <lowercase id>
#   Claude stores transcripts at <config>/projects/<cwd with non-alnum -> ->/<id>.jsonl.
#   The cwd is the session root or one of the run directories the launcher
#   recorded in the session's run-dirs. run-dirs counts only as a regular
#   non-symlink file; a line counts only when it is canonical (an absolute path
#   without control characters that equals its own resolved form, as the
#   launcher writes it) and lies inside the session's recorded source root or
#   its session root. The session itself has already passed
#   _harness_launcher_session_is_current.
_harness_launcher_claude_transcript_exists() {
  local state_home="$1" name="$2" id="$3"
  local dir="$state_home/sessions/$name" root candidate enc line resolved base source_root
  local config="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  local -a candidates=() bases=()
  root="$state_home/worktrees/$name"
  [[ -f "$dir/session-root" && ! -L "$dir/session-root" ]] && root="$(<"$dir/session-root")"
  candidates=("$root" "${root:A}")
  if [[ -f "$dir/run-dirs" && ! -L "$dir/run-dirs" && -f "$dir/source-root" && ! -L "$dir/source-root" ]]; then
    source_root="$(<"$dir/source-root")"
    bases=("${source_root:A}" "${root:A}")
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ "$line" == /* && "$line" != *[[:cntrl:]]* ]] || continue
      resolved="${line:A}"
      [[ "$line" == "$resolved" ]] || continue
      for base in "${bases[@]}"; do
        if [[ "$base" == /?* && ( "$resolved" == "$base" || "$resolved" == "$base"/* ) ]]; then
          candidates+=("$resolved")
          break
        fi
      done
    done < "$dir/run-dirs"
  fi
  for candidate in "${(u)candidates[@]}"; do
    enc="${candidate//[^A-Za-z0-9]/-}"
    [[ -f "$config/projects/$enc/$id.jsonl" && ! -L "$config/projects/$enc/$id.jsonl" ]] && return 0
  done
  return 1
}

# _harness_launcher_resolve_restore <source-root> [launcher argv...]
#   Orca restores an agent as `<override> <default args> --resume <id>` (Claude)
#   or `<override> codex resume <id>` (Codex). Map that single UUID back to the
#   isolated session that owns it. Prints the session directory name (uppercase
#   UUID) and returns 0 on a unique owner; 1 when there is no owner (caller keeps
#   its reject message); 3 when several sessions own the id (ambiguous); 4 when
#   there is no isolated owner and the session lives in the canonical
#   (non-isolated) harness root: a Codex rollout only in <source-root>'s own
#   CODEX_HOME, or a Claude launch record with isolated=0 for this root.
#   Only sessions recorded for this harness (source-root) count; symlinks are
#   never followed. For Claude, a session also owns ids listed as exactly
#   `claude <uuid>` in its provider-sessions (written by
#   harness-session-provider-record, e.g. after /clear); such a record-only
#   owner maps only when the id's transcript exists under its Claude project dir.
_harness_launcher_resolve_restore() {
  local source_root="$1"; shift
  local uuid_re='^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'
  local -a candidates=()
  local id="" is_codex=false arg prev=""
  if [[ "${1:-}" == codex ]]; then
    is_codex=true; shift
    for arg in "$@"; do
      [[ "$arg" == -- ]] && break
      [[ "$prev" == resume ]] && candidates+=("$arg")
      prev="$arg"
    done
  else
    for arg in "$@"; do
      [[ "$arg" == -- ]] && break
      case "$arg" in
        --resume=*) candidates+=("${arg#--resume=}") ;;
        -r?*) candidates+=("${arg#-r}") ;;
        *) [[ "$prev" == (--resume|-r) ]] && candidates+=("$arg") ;;
      esac
      prev="$arg"
    done
  fi
  (( ${#candidates} == 1 )) || return 1
  [[ "${candidates[1]}" =~ $uuid_re ]] || return 1
  id="${(L)candidates[1]}"

  local state_home="${HARNESS_SESSION_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/harness-launcher}"
  local sessions="$state_home/sessions" dir name recorded
  local -a owners=() hits=() recorders=() record_only=()
  if $is_codex; then
    local rollout import_status
    for rollout in "$state_home"/worktrees/*/.harness/codex/sessions/*/*/*/rollout-*-$id.jsonl(N); do
      [[ -f "$rollout" && ! -L "$rollout" ]] || continue
      name="${${rollout#$state_home/worktrees/}%%/*}"
      if _harness_launcher_session_imported_codex "$sessions/$name" "$source_root" "$name" "$id"; then
        continue
      else
        import_status=$?; (( import_status == 1 )) || return 3
      fi
      hits+=("$name")
    done
  else
    for dir in "$sessions"/*(/N); do
      name="${dir:t}"
      [[ "${(L)name}" == "$id" ]] && hits+=("$name")
      _harness_launcher_session_records_claude "$dir/provider-sessions" "$id" && recorders+=("$name")
    done
  fi
  for name in "${hits[@]}"; do
    _harness_launcher_session_is_current "$sessions/$name" "$source_root" && owners+=("$name")
  done
  for name in "${recorders[@]}"; do
    (( ${owners[(Ie)$name]} )) && continue
    _harness_launcher_session_is_current "$sessions/$name" "$source_root" || continue
    owners+=("$name"); record_only+=("$name")
  done
  owners=(${(u)owners})
  (( ${#owners} <= 1 )) || return 3
  if (( ${#owners} == 0 )); then
    # No isolated owner: 4 when the session belongs to the canonical harness
    # root itself. Codex: its rollout exists only in the source CODEX_HOME.
    # Claude: a launch record says isolated=0 for this harness (the transcript
    # alone is no proof, isolated sessions file theirs in the same project dir).
    local restore_permission restore_approval restore_sandbox restore_bypass restore_isolated
    if $is_codex; then
      local -a source_rollouts=("$source_root"/.harness/codex/sessions/*/*/*/rollout-*-$id.jsonl(N.))
      (( ${#source_rollouts} )) && return 4
    elif _harness_launcher_restore_launch_record claude "$id" "$source_root" && [[ "$restore_isolated" == 0 ]]; then
      return 4
    fi
    return 1
  fi
  if (( ${record_only[(Ie)${owners[1]}]} )); then
    _harness_launcher_claude_transcript_exists "$state_home" "${owners[1]}" "$id" || return 1
  fi
  print -r -- "${owners[1]}"
}

_harness_launcher_resolve_orca_resume() { _harness_launcher_resolve_restore "$@"; }

# --- Restore fidelity ---------------------------------------------------------
# A host restores an agent by re-typing `claude --resume <id>` / `codex resume
# <id>`. The typed argv carries no model, effort or grant, so the launcher would
# apply its defaults. For a pure single-UUID resume it instead restores the
# session's model and effort (harness-restore-probe: read from the transcript or
# rollout) and its permission/sandbox grant (only from the launcher-owned launch
# record harness-launch-record wrote at that session's start). A grant is never
# taken from a transcript or rollout, and every value read is re-validated here.
typeset -g _HARNESS_LAUNCHER_UUID_RE='^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'
typeset -g _HARNESS_LAUNCHER_MODEL_RE='^[A-Za-z0-9][A-Za-z0-9._-]*(\[1m\])?$'

# _harness_launcher_restore_resume_id <claude|codex> [argv...]
#   Prints the lowercase id when argv is exactly one resume of one UUID:
#   Claude `--resume <id>`, `--resume=<id>`, `-r <id>`; Codex `resume <id>`.
_harness_launcher_restore_resume_id() {
  local kind="$1" id=""; shift
  if [[ "$kind" == codex ]]; then
    [[ $# -eq 2 && "$1" == resume ]] && id="$2"
  else
    case $# in
      1) [[ "$1" == --resume=* ]] && id="${1#--resume=}" ;;
      2) [[ "$1" == (--resume|-r) ]] && id="$2" ;;
    esac
  fi
  [[ "$id" =~ $_HARNESS_LAUNCHER_UUID_RE ]] || return 1
  print -r -- "${(L)id}"
}

# _harness_launcher_codex_history_select <source-root> <list|continue|native-uuid>
# Reads only the source-bound private catalog.  `list` returns the catalog JSON;
# selections return a narrow key/value record that callers validate again before
# touching an isolated home.  A noninteractive continuation must name a UUID
# when more than one local history is available.
_harness_launcher_codex_history_select() {
  local source_root="$1" mode="$2" helper py state output interactive=0
  helper="$_HARNESS_LAUNCHER_BIN/codex-history.py"
  [[ -f "$helper" && ! -L "$helper" ]] || return 1
  py="$(harness_python3_resolve 2>/dev/null)" || return 1
  state="${HARNESS_SESSION_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/harness-launcher}"
  output="$("$py" -I "$helper" catalog --source-root "$source_root" --state-home "$state" \
    --legacy-state-home "${XDG_STATE_HOME:-$HOME/.local/state}/harness-launcher")" || return $?
  if [[ "$mode" == list ]]; then
    print -r -- "$output"
    return 0
  fi
  [[ -t 0 && -t 1 ]] && interactive=1
  "$py" -c '
import json, re, sys
mode, source, interactive = sys.argv[1:]
uuid = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
try:
    entries = json.load(sys.stdin)["entries"]
except (json.JSONDecodeError, KeyError, TypeError):
    raise SystemExit(2)
entries = [row for row in entries if isinstance(row, dict) and row.get("source_local") is True and row.get("source_root") == source and row.get("status") in {"canonical", "live", "snapshot"} and row.get("reason") is None and uuid.fullmatch(str(row.get("native_id", "")))]
if mode == "continue":
    if interactive != "1" and len(entries) != 1:
        raise SystemExit(2)
    entries.sort(key=lambda row: str(row.get("updated_at", "")), reverse=True)
    if not entries:
        raise SystemExit(2)
elif uuid.fullmatch(mode):
    entries = [row for row in entries if row["native_id"].lower() == mode.lower()]
    if len(entries) != 1:
        raise SystemExit(2)
else:
    raise SystemExit(2)
row = entries[0]
for key in ("native_id", "home", "source_root", "isolation_id", "origin_runtime", "snapshot_id", "updated_at", "status", "reason"):
    value = row.get(key)
    if value is not None and (not isinstance(value, str) or "\n" in value or "\r" in value):
        raise SystemExit(2)
    print("{}={}".format(key, value or str()))
' "$mode" "$source_root" "$interactive" <<< "$output"
}

# _harness_launcher_codex_history_request [launcher argv...]
# Recognizes only the public, unambiguous history shortcuts before Codex's
# normal argument parser can start a native process.
_harness_launcher_codex_history_request() {
  [[ "${1:-}" == codex ]] || return 1
  case "${2:-}" in
    continue) [[ $# -eq 2 ]] && print -r -- continue ;;
    resume)
      case "${3:-}" in
        --list) [[ $# -eq 3 ]] && print -r -- list ;;
        '') [[ $# -eq 2 ]] && print -r -- list ;;
        *) [[ "$3" =~ $_HARNESS_LAUNCHER_UUID_RE && $# -eq 3 ]] && print -r -- "${(L)3}" ;;
      esac
      ;;
  esac
}

# _harness_launcher_codex_history_prepare <source-root> <native-uuid>
# Converts a trusted, snapshotless canonical catalog row into immutable private
# history before a fresh isolation is created.  The helper refuses any origin
# it cannot bind safely (including active or nonterminal isolated writers).
_harness_launcher_codex_history_prepare() {
  local source_root="$1" native_id="$2" helper py state
  [[ "$native_id" =~ $_HARNESS_LAUNCHER_UUID_RE ]] || return 2
  helper="$_HARNESS_LAUNCHER_BIN/codex-history.py"
  [[ -f "$helper" && ! -L "$helper" ]] || return 2
  py="$(harness_python3_resolve 2>/dev/null)" || return 2
  state="${HARNESS_SESSION_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/harness-launcher}"
  "$py" -I "$helper" prepare --source-root "$source_root" --state-home "$state" --native-id "$native_id" \
    --legacy-state-home "${XDG_STATE_HOME:-$HOME/.local/state}/harness-launcher"
}

_harness_launcher_codex_history_lease_acquire() {
  local native_id="$1" state history leases lock
  [[ "$native_id" =~ $_HARNESS_LAUNCHER_UUID_RE ]] || return 2
  state="${HARNESS_SESSION_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/harness-launcher}"
  history="$state/native-history"; leases="$history/leases"; lock="$leases/${(L)native_id}.lock"
  [[ ! -e "$history" && ! -L "$history" || -d "$history" && ! -L "$history" ]] || return 2
  mkdir -p "$leases" && chmod 700 "$history" "$leases" || return 2
  [[ -d "$leases" && ! -L "$leases" ]] || return 2
  [[ -e "$lock" || -L "$lock" ]] || umask 077 > "$lock"
  [[ -f "$lock" && ! -L "$lock" ]] || return 2
  chmod 600 "$lock" || return 2
  zmodload zsh/system || return 2
  zsystem flock -t 0 -f HARNESS_NATIVE_HISTORY_LEASE_FD "$lock" 2>/dev/null || return 2
}

_harness_launcher_codex_history_lease_release() {
  [[ -n "${HARNESS_NATIVE_HISTORY_LEASE_FD:-}" ]] || return 0
  if [[ "${HARNESS_NATIVE_HISTORY_LEASE_ADOPTED:-0}" != 1 ]]; then
    zsystem flock -u "$HARNESS_NATIVE_HISTORY_LEASE_FD" 2>/dev/null || true
  else
    exec {HARNESS_NATIVE_HISTORY_LEASE_FD}>&- 2>/dev/null || true
  fi
  HARNESS_NATIVE_HISTORY_LEASE_FD=""
  HARNESS_NATIVE_HISTORY_LEASE_ADOPTED=0
}

_harness_launcher_codex_history_lease_adopt() {
  local native_id="$1" fd="${HARNESS_CODEX_HISTORY_LEASE_FD:-}" state lock actual expected
  local -A held metadata
  [[ "$native_id" =~ $_HARNESS_LAUNCHER_UUID_RE && "$fd" =~ '^[1-9][0-9]+$' ]] || return 2
  state="${HARNESS_SESSION_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/harness-launcher}"
  lock="$state/native-history/leases/${(L)native_id}.lock"
  [[ -f "$lock" && ! -L "$lock" ]] || return 2
  zmodload zsh/stat || return 2
  zstat -H held -f "$fd" 2>/dev/null || return 2
  zstat -H metadata "$lock" 2>/dev/null || return 2
  [[ "$held[device]:$held[inode]" == "$metadata[device]:$metadata[inode]" ]] || return 2
  (( (held[mode] & 8#777) == 8#600 && held[uid] == EUID && held[nlink] == 1 )) || return 2
  HARNESS_NATIVE_HISTORY_LEASE_FD="$fd"
  HARNESS_NATIVE_HISTORY_LEASE_ADOPTED=1
}

# _harness_launcher_codex_history_restore <source-root> <generated-codex-home>
# Hydrates only an Entry-selected immutable snapshot after home preparation.
# The catalog is consulted again so inherited environment never chooses another
# profile's archive or substitutes origin provenance.
_harness_launcher_codex_history_restore() {
  local source_root="$1" destination="$2" native_id="${HARNESS_CODEX_HISTORY_NATIVE_ID:-}"
  local snapshot_id="${HARNESS_CODEX_HISTORY_SNAPSHOT_ID:-}" origin_runtime="${HARNESS_CODEX_HISTORY_ORIGIN_RUNTIME:-}"
  local state helper py selection key value selected_snapshot="" selected_origin="" selected_source="" selected_home="" selected_isolation="" receipt session
  local expected_home="${HARNESS_CODEX_HISTORY_HOME:-}" expected_isolation="${HARNESS_CODEX_HISTORY_ORIGIN_ISOLATION:-}"
  [[ -n "$native_id$snapshot_id$origin_runtime" ]] || return 0
  [[ "$native_id" =~ $_HARNESS_LAUNCHER_UUID_RE && "$snapshot_id" =~ '^[0-9a-f]{64}$' ]] || return 2
  [[ "$origin_runtime" == legacy-unknown || "$origin_runtime" =~ '^[0-9a-f]{40}$' ]] || return 2
  selection="$(_harness_launcher_codex_history_select "$source_root" "$native_id")" || return 2
  while IFS='=' read -r key value; do
    case "$key" in
      source_root) selected_source="$value" ;;
      snapshot_id) selected_snapshot="$value" ;;
      origin_runtime) selected_origin="$value" ;;
      home) selected_home="$value" ;;
      isolation_id) selected_isolation="$value" ;;
    esac
  done <<< "$selection"
  [[ "$selected_source" == "$source_root" && "$selected_snapshot" == "$snapshot_id" && "$selected_origin" == "$origin_runtime" ]] || return 2
  [[ -z "$expected_home" || "$expected_home" == "$selected_home" ]] || return 2
  [[ -z "$expected_isolation" || "$expected_isolation" == "$selected_isolation" ]] || return 2
  [[ "$HARNESS_SESSION_ID" =~ $_HARNESS_LAUNCHER_UUID_RE ]] || return 2
  state="${HARNESS_SESSION_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/harness-launcher}"
  session="$state/sessions/$HARNESS_SESSION_ID"
  [[ -d "$session" && ! -L "$session" && -d "$destination" && ! -L "$destination" ]] || return 2
  receipt="$session/native-history-restore.json"
  [[ ! -e "$receipt" && ! -L "$receipt" ]] || return 2
  helper="$_HARNESS_LAUNCHER_BIN/codex-history.py"
  [[ -f "$helper" && ! -L "$helper" ]] || return 2
  py="$(harness_python3_resolve 2>/dev/null)" || return 2
  "$py" -I "$helper" restore --catalog "$state/native-history" --snapshot "$snapshot_id" \
    --destination "$destination" --receipt "$receipt" --native-id "$native_id" --origin-runtime "$origin_runtime" >/dev/null
}

# _harness_launcher_restore_probe <claude|codex> <lowercase id>
#   Fills the caller's restore_model, restore_effort and restore_context from
#   the session file. Output of the helper is data: each line is matched, never
#   evaluated or word-split, and a value outside its vocabulary is dropped.
_harness_launcher_restore_probe() {
  local agent="$1" id="$2" file="" py out line key value
  local helper="$_HARNESS_LAUNCHER_BIN/harness-restore-probe"
  local -a hits=()
  restore_model="" restore_effort="" restore_context=""
  if [[ "$agent" == codex ]]; then
    hits=("$HARNESS_DIR"/.harness/codex/sessions/*/*/*/rollout-*-$id.jsonl(N.om[1]))
    # A durable-history resume starts in a fresh home that is hydrated only after
    # this probe: read the model and effort from the selected immutable snapshot.
    if (( ${#hits} == 0 )) && [[ "${HARNESS_CODEX_HISTORY_SNAPSHOT_ID:-}" =~ '^[0-9a-f]{64}$' ]]; then
      local history_state="${HARNESS_SESSION_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/harness-launcher}"
      hits=("$history_state"/native-history/snapshots/$HARNESS_CODEX_HISTORY_SNAPSHOT_ID/data/sessions/*/*/*/rollout-*-$id.jsonl(N.om[1]))
    fi
  else
    hits=("${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/projects/*/$id.jsonl(N.om[1]))
  fi
  file="${hits[1]-}"
  [[ -n "$file" && -f "$helper" ]] || return 1
  py="$(harness_python3_resolve 2>/dev/null)" || return 1
  out="$("$py" "$helper" "$agent" "$file" 2>/dev/null)" || return 1
  for line in "${(@f)out}"; do
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"; value="${line#*=}"
    case "$key" in
      model)
        [[ "$value" =~ $_HARNESS_LAUNCHER_MODEL_RE ]] || continue
        [[ "$agent" == codex || "$value" == claude-* ]] && restore_model="$value" ;;
      effort)
        case "$value" in
          low|medium|high|xhigh|max) restore_effort="$value" ;;
          minimal) [[ "$agent" == codex ]] && restore_effort="$value" ;;
        esac ;;
      context)
        [[ "$agent" == codex && "$value" == (272k|1m) ]] && restore_context="$value" ;;
    esac
  done
  return 0
}

# _harness_launcher_restore_launch_record <claude|codex> <lowercase id> <source-root>
#   Fills the caller's restore_permission, restore_approval, restore_sandbox,
#   restore_bypass, restore_profile, restore_window (Claude context `1m`, not a
#   grant) and restore_isolated from the launch record, only when it
#   names this harness root. Returns 1 (all empty) without a usable record.
_harness_launcher_restore_launch_record() {
  local agent="$1" id="$2" source_root="$3" py out line key value recorded=""
  local helper="$_HARNESS_LAUNCHER_BIN/harness-launch-record"
  local state_home="${HARNESS_SESSION_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/harness-launcher}"
  restore_permission="" restore_approval="" restore_sandbox="" restore_network="" restore_bypass="" restore_isolated="" restore_profile="" restore_window=""
  [[ -f "$helper" ]] || return 1
  py="$(harness_python3_resolve 2>/dev/null)" || return 1
  out="$(HARNESS_SESSION_STATE_HOME="$state_home" "$py" "$helper" read "$agent" "$id" 2>/dev/null)" || return 1
  for line in "${(@f)out}"; do
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"; value="${line#*=}"
    case "$key" in
      permission) [[ "$agent" == claude && "$value" == (default|acceptEdits|plan|auto|dontAsk|bypassPermissions) ]] && restore_permission="$value" ;;
      approval) [[ "$agent" == codex && "$value" == (untrusted|on-failure|on-request|never) ]] && restore_approval="$value" ;;
      sandbox) [[ "$agent" == codex && "$value" == (read-only|workspace-write|danger-full-access) ]] && restore_sandbox="$value" ;;
      network) [[ "$agent" == codex && "$value" == (enabled|disabled) ]] && restore_network="$value" ;;
      bypass) [[ "$agent" == codex && "$value" == 1 ]] && restore_bypass=1 ;;
      profile) [[ "$agent" == codex && "$value" == (fast|base|sol|astra|plan|rich) ]] && restore_profile="$value" ;;
      context) [[ "$agent" == claude && "$value" == 1m ]] && restore_window=1m ;;
      isolated) [[ "$value" == (0|1) ]] && restore_isolated="$value" ;;
      source_root) [[ "$value" == /* && "$value" != *[[:cntrl:]]* ]] && recorded="$value" ;;
    esac
  done
  if [[ -z "$recorded" || "${recorded:A}" != "${source_root:A}" ]]; then
    restore_permission="" restore_approval="" restore_sandbox="" restore_network="" restore_bypass="" restore_isolated="" restore_profile="" restore_window=""
    return 1
  fi
  return 0
}

# _harness_launcher_restore_hint <claude|codex> <id> <model>
#   One stderr line with the exact command that relaunches this session with
#   bypass, printed when the session has no launch record for this harness (a
#   session that predates records, or one started outside the launcher). The
#   launcher never escalates on its own.
_harness_launcher_restore_hint() {
  local agent="$1" id="$2" model="$3" keyword=""
  if [[ "$agent" == codex ]]; then
    case "$model" in *sol*) keyword=sol ;; *terra*) keyword=base ;; *luna*) keyword=fast ;; esac
    echo "harness-launcher: restored with default permissions (no launch record for this session); to relaunch with bypass: ${HARNESS_PREFIX} codex ${keyword:+$keyword }bypass --passthrough resume $id" >&2
  else
    case "$model" in *opus*) keyword=rich ;; *sonnet*) keyword=base ;; *haiku*) keyword=fast ;; esac
    echo "harness-launcher: restored with default permissions (no launch record for this session); to relaunch with bypass: ${HARNESS_PREFIX} ${keyword:+$keyword }bypass --passthrough --resume $id" >&2
  fi
}

# _harness_launcher_claude_restore_apply
#   Detects a Claude restore and injects the restored --model/--effort/
#   --permission-mode. Runs before _harness_launcher_passthrough_reconcile,
#   which already implements caller-wins (a restored --model replaces a host
#   default `base` model) and forces thinking for xhigh/max. Reads and updates
#   the caller's locals (zsh dynamic scope).
#   A restore is one resume of one UUID and nothing else, and no launcher
#   keyword other than the host-default `base` marked by shell routing.
_harness_launcher_claude_restore_apply() {
  [[ -z "$provider_name" && -z "$session_flag" ]] || return 0
  local -a argv=()
  if $passthrough; then
    if $mode_applied; then
      $host_default_base || return 0
    else
      (( ${#claude_args} == 0 )) || return 0
    fi
    argv=("${claude_passthrough_args[@]}")
  else
    $mode_applied && return 0
    (( ${#claude_prompt_args} == 0 )) || return 0
    argv=("${claude_args[@]}")
  fi
  local restore_id restore_model restore_effort restore_context
  local restore_permission restore_approval restore_sandbox restore_bypass restore_isolated restore_window
  restore_id="$(_harness_launcher_restore_resume_id claude "${argv[@]}")" || return 0
  skip_tui=true
  _harness_launcher_restore_probe claude "$restore_id" || true
  # Only a session with no record at all gets the hint; a record without a grant
  # means the user chose the default mode.
  _harness_launcher_restore_launch_record claude "$restore_id" "${HARNESS_SOURCE_ROOT:-${HARNESS_DIR:A}}" \
    || _harness_launcher_restore_hint claude "$restore_id" "$restore_model"
  # A long 1M session may leave no evidence in the transcript window; the launch
  # record remembers that the launcher started it with a [1m] model. This only
  # sizes the context window, it grants nothing.
  if [[ "$restore_window" == 1m && -n "$restore_model" && "$restore_model" != *"[1m]" \
        && "${restore_model}[1m]" =~ $_HARNESS_LAUNCHER_MODEL_RE ]]; then
    restore_model="${restore_model}[1m]"
  fi
  if $passthrough; then
    [[ -z "$restore_model" ]] || claude_passthrough_args+=(--model "$restore_model")
    [[ -z "$restore_effort" ]] || claude_passthrough_args+=(--effort "$restore_effort")
    [[ -z "$restore_permission" ]] || claude_passthrough_args+=(--permission-mode "$restore_permission")
  else
    [[ -z "$restore_model" ]] || claude_args+=(--model "$restore_model")
    [[ -z "$restore_effort" ]] || env_effort="$restore_effort"
    [[ -z "$restore_permission" ]] || claude_args+=(--permission-mode "$restore_permission")
  fi
  return 0
}

# _harness_launcher_claude_launch_grant
#   Prints the permission mode Claude is launched with (the last one in the
#   final launcher and passthrough argv), or nothing. Recorded by the launch
#   record hook so a later restore can reapply it.
_harness_launcher_claude_launch_grant() {
  local arg prev="" grant=""
  for arg in "${claude_args[@]}" "${claude_passthrough_opts[@]}"; do
    case "$arg" in
      --permission-mode=*) grant="${arg#--permission-mode=}" ;;
      --dangerously-skip-permissions) grant=bypassPermissions ;;
      *) [[ "$prev" == --permission-mode ]] && grant="$arg" ;;
    esac
    prev="$arg"
  done
  case "$grant" in
    default|acceptEdits|plan|auto|dontAsk|bypassPermissions) print -r -- "$grant" ;;
  esac
}

# _harness_launcher_export_launch_env <approval> <sandbox> <bypass> <profile>
#   Codex only; run inside the agent's subshell. harness_launch_record_export_codex
#   (harness-common.sh, shared with launcher.sh) with this launch's source root.
_harness_launcher_export_launch_env() {
  harness_launch_record_export_codex "${HARNESS_SOURCE_ROOT:-${HARNESS_DIR:A}}" "$@"
}

# _harness_launcher_claude_launch_context
#   Prints `1m` when the final --model (launcher argv, then passthrough argv; the
#   last one wins) ends in [1m].
_harness_launcher_claude_launch_context() {
  local arg prev="" model=""
  for arg in "${claude_args[@]}" "${claude_passthrough_opts[@]}"; do
    case "$arg" in
      --model=*) model="${arg#--model=}" ;;
      *) [[ "$prev" == --model ]] && model="$arg" ;;
    esac
    prev="$arg"
  done
  [[ "$model" == *"[1m]" ]] && print -r -- 1m
}

# _harness_launcher_claude_launch_settings <force-thinking:true|false> <permission> <context>
#   harness_claude_launch_settings (harness-common.sh, shared with launcher.sh)
#   with this launch's source root.
_harness_launcher_claude_launch_settings() {
  harness_claude_launch_settings "$_HARNESS_LAUNCHER_BIN" "${HARNESS_SOURCE_ROOT:-${HARNESS_DIR:A}}" "$@" "${HARNESS_DIR:A}"
}

# _harness_launcher_isolated_record_run_dir
#   Appends the isolated session's resolved run directory (HARNESS_RUN_DIR) to
#   <state>/sessions/<HARNESS_SESSION_ID>/run-dirs, one path per line, so the
#   restore resolver can find Claude transcripts filed under that directory.
#   Only a directory inside the session's source root or session root is
#   recorded; an identical line is not repeated; the file is opened without
#   following a symlink and must be regular. Silent, and never fails a launch.
_harness_launcher_isolated_record_run_dir() {
  local uuid_re='^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'
  local state_home="${HARNESS_SESSION_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/harness-launcher}"
  local id="${HARNESS_SESSION_ID:-}" run="${HARNESS_RUN_DIR:-}" dir file resolved base line fd
  local -a bases=()
  [[ "$id" =~ $uuid_re && "$run" == /* && -d "$run" ]] || return 0
  dir="$state_home/sessions/$id"
  [[ -d "$dir" && ! -L "$dir" ]] || return 0
  resolved="${run:A}"
  [[ -n "${HARNESS_SOURCE_ROOT:-}" ]] && bases+=("${HARNESS_SOURCE_ROOT:A}")
  [[ -n "${HARNESS_SESSION_ROOT:-}" ]] && bases+=("${HARNESS_SESSION_ROOT:A}")
  for base in "${bases[@]}"; do
    [[ "$resolved" == "$base" || "$resolved" == "$base"/* ]] && break
    base=""
  done
  [[ -n "$base" ]] || return 0
  file="$dir/run-dirs"
  [[ -L "$file" ]] && return 0
  {
    zmodload zsh/system || return 0
    sysopen -r -w -a -o nofollow,creat,nonblock,cloexec -m 600 -u fd "$file" || return 0
    if [[ -f /dev/fd/$fd ]]; then
      while IFS= read -r -u $fd line || [[ -n "$line" ]]; do
        [[ "$line" == "$resolved" ]] && { exec {fd}>&-; return 0; }
      done
      print -r -u $fd -- "$resolved"
    fi
    exec {fd}>&-
  } 2>/dev/null
  return 0
}

_harness_launcher_isolated_heartbeat() {
  local session_id="$1" interval="${HARNESS_SESSION_HEARTBEAT_SECONDS:-30}"
  while "$_HARNESS_LAUNCHER_BIN/session-isolation.sh" heartbeat "$session_id" 2>/dev/null; do
    sleep "$interval"
  done
}

# _harness_launcher_isolated_claude_plugins
#   Before an isolated Claude launch, mirrors the canonical root's project- and
#   local-scope Claude plugin install records into the session root: Claude Code
#   applies those records per project path, and the session is a separate clone.
#   Reads the caller's isolated_session_id (zsh dynamic scope). Skipped for
#   headless runs, which must not gain plugins the canonical secrets policy
#   withholds, and when HARNESS_CLAUDE_PLUGIN_MIRROR=0 is set in the launching
#   environment (settings.local.json env is not consulted). The helper runs after the
#   same local env export Claude gets, so both resolve one registry. Never fails
#   the launch.
_harness_launcher_isolated_claude_plugins() {
  [[ -n "${isolated_session_id:-}" && "${HARNESS_HEADLESS:-0}" != 1 && "${HARNESS_CLAUDE_PLUGIN_MIRROR:-1}" != 0 ]] || return 0
  local state_home="${HARNESS_SESSION_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/harness-launcher}"
  (
    harness_export_local_env "$HARNESS_SOURCE_ROOT" || exit 1
    local py
    py="$(harness_python3_resolve 2>/dev/null)" || exit 1
    "$py" -I "$_HARNESS_LAUNCHER_BIN/claude-plugin-scope-mirror.py" ensure \
      --source-root "$HARNESS_SOURCE_ROOT" --session-root "$HARNESS_SESSION_ROOT" \
      --worktrees-dir "$state_home/worktrees"
  ) || echo 'harness-launcher: warning: Claude plugin install records were not mirrored into the isolated session' >&2
  return 0
}

_harness_launcher_isolated_finish() {
  local session_id="$1" heartbeat_pid="${2:-}" lease_fd="${3:-}"
  if [[ -n "$heartbeat_pid" ]]; then
    kill "$heartbeat_pid" 2>/dev/null || true
    wait "$heartbeat_pid" 2>/dev/null || true
  fi
  if ! "$_HARNESS_LAUNCHER_BIN/session-isolation.sh" exit "$session_id"; then
    echo "harness-launcher: warning: failed to finalize isolated session $session_id; workspace retained" >&2
  fi
  if [[ -n "$lease_fd" ]]; then
    zsystem flock -u "$lease_fd" 2>/dev/null || true
  fi
}

_harness_launcher_isolated_lease_acquire() {
  local session_id="$1" state_home record marker lock
  [[ "$session_id" =~ '^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$' ]] || {
    echo "harness-launcher: invalid isolated session UUID: $session_id" >&2
    return 2
  }
  state_home="${HARNESS_SESSION_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/harness-launcher}"
  record="$state_home/sessions/$session_id"; marker="$record/lease-v1"; lock="$record/runtime.lock"
  [[ -d "$record" && ! -L "$record" && -f "$marker" && ! -L "$marker" && "$(wc -c < "$marker" | tr -d ' ')" == 2 && "$(<"$marker")" == 1 && -f "$lock" && ! -L "$lock" ]] || {
    echo "harness-launcher: invalid runtime lease record for $session_id" >&2
    return 2
  }
  zmodload zsh/system || {
    echo 'harness-launcher: zsh/system is required for isolated session leases' >&2
    return 2
  }
  if ! zsystem flock -t 0 -f HARNESS_SESSION_LEASE_FD "$lock" 2>/dev/null; then
    HARNESS_SESSION_LEASE_FD=""
    echo "harness-launcher: isolated session $session_id is already active" >&2
    return 2
  fi
}

_harness_launcher_prepare_codex_global_mcp_allowlist() {
  local raw="${HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST:-}" normalized
  [[ -n "$raw" ]] || { unset HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST; return 0; }
  normalized="$(harness_codex_global_mcp_allowlist_normalize "$raw")" || return $?
  [[ -n "$normalized" ]] && export HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="$normalized" \
    || unset HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST
}

_harness_launcher_prepare_codex_apps_allowlist() {
  export HARNESS_CODEX_SLACK_APPS="${HARNESS_CODEX_SLACK_APPS:-}"
  local raw="${HARNESS_CODEX_APPS_ALLOWLIST:-}" normalized
  [[ -n "$raw" ]] || { unset HARNESS_CODEX_APPS_ALLOWLIST; return 0; }
  normalized="$(harness_codex_apps_allowlist_normalize "$raw")" || return $?
  [[ -n "$normalized" ]] && export HARNESS_CODEX_APPS_ALLOWLIST="$normalized" \
    || unset HARNESS_CODEX_APPS_ALLOWLIST
}

# _harness_launcher_claude_mcp_local_args <harness-dir>
#   Sets reply to the harness MCP flags (`--mcp-config <rendered>`), or to an
#   empty array when the harness has no local MCP config.
_harness_launcher_claude_mcp_local_args() {
  local rendered
  reply=()
  # harness-headless passes --strict-mcp-config with no MCP servers.
  [[ "${HARNESS_HEADLESS:-0}" != 1 ]] || return 0
  rendered="$(harness_claude_mcp_runtime_config "$1" "$_HARNESS_LAUNCHER_BIN")" || return $?
  [[ -z "$rendered" ]] || reply=(--mcp-config "$rendered")
}

# _harness_launcher_argv_has_option <option> [args...]
#   True when args contain `<option>` or `<option>=value`.
_harness_launcher_argv_has_option() {
  local option="$1" arg; shift
  for arg in "$@"; do
    [[ "$arg" == "$option" || "$arg" == "$option="* ]] && return 0
  done
  return 1
}

# _harness_launcher_argv_disables_thinking [args...]
#   True for an explicit thinking disable: `--thinking disabled`,
#   `--max-thinking-tokens 0`, or inline JSON `--settings` whose
#   alwaysThinkingEnabled is false. A settings file path is not read.
_harness_launcher_argv_disables_thinking() {
  local -a args=("$@")
  local i arg value
  for (( i = 1; i <= ${#args}; i++ )); do
    arg="${args[i]}"
    case "$arg" in
      --thinking|--max-thinking-tokens|--settings) value="${args[i+1]-}"; (( i++ )) ;;
      --thinking=*|--max-thinking-tokens=*|--settings=*) value="${arg#*=}"; arg="${arg%%=*}" ;;
      *) continue ;;
    esac
    case "$arg" in
      --thinking) [[ "$value" == disabled ]] && return 0 ;;
      --max-thinking-tokens) [[ "$value" == 0 ]] && return 0 ;;
      --settings)
        [[ "$value" == '{'* && "$value" =~ '"alwaysThinkingEnabled"[[:space:]]*:[[:space:]]*false' ]] && return 0 ;;
    esac
  done
  return 1
}

# _harness_launcher_passthrough_reconcile
#   Caller-wins rules for `--passthrough`: an explicit caller --model,
#   --permission-mode or --effort replaces the launcher default instead of
#   duplicating or overriding it, and an explicit caller thinking disable
#   drops the launcher's xhigh/max effort (which requires thinking). Scans
#   claude_passthrough_opts only (the caller argv before its own `--`).
#   Updates the caller's claude_args, env_effort and passthrough_force_thinking
#   (zsh dynamic scope).
_harness_launcher_passthrough_reconcile() {
  local arg caller_effort="" i
  local -a drop=() kept=()
  if [[ -n "$session_flag" ]]; then
    for arg in "${claude_passthrough_opts[@]}"; do
      case "$arg" in
        -c|--continue|-r|-r?*|--resume|--resume=*|--session-id|--session-id=*|--fork-session|--fork-session=*)
          echo "harness-launcher: launcher '${session_flag}' conflicts with '$arg' after --passthrough" >&2
          return 2 ;;
      esac
    done
  fi
  _harness_launcher_argv_has_option --model "${claude_passthrough_opts[@]}" && drop+=(--model)
  _harness_launcher_argv_has_option --permission-mode "${claude_passthrough_opts[@]}" && drop+=(--permission-mode)
  if (( ${#drop} )); then
    for (( i = 1; i <= ${#claude_args}; i++ )); do
      if (( ${drop[(Ie)${claude_args[i]}]} )); then
        (( i++ ))  # skip the option value as well
        continue
      fi
      kept+=("${claude_args[i]}")
    done
    claude_args=("${kept[@]}")
  fi
  if ! _harness_launcher_argv_has_option --effort "${claude_passthrough_opts[@]}"; then
    if [[ "$env_effort" == (xhigh|max) ]] && _harness_launcher_argv_disables_thinking "${claude_passthrough_opts[@]}"; then
      env_effort=""
    fi
    return 0
  fi
  env_effort=""
  for (( i = 1; i <= ${#claude_passthrough_opts}; i++ )); do
    case "${claude_passthrough_opts[i]}" in
      --effort) caller_effort="${claude_passthrough_opts[i+1]-}" ;;
      --effort=*) caller_effort="${claude_passthrough_opts[i]#--effort=}" ;;
    esac
  done
  # Same API constraint as the launcher's own xhigh/max efforts, unless the
  # caller sets thinking itself.
  if [[ "$caller_effort" == (xhigh|max) ]] \
    && ! _harness_launcher_argv_has_option --thinking "${claude_passthrough_opts[@]}" \
    && ! _harness_launcher_argv_has_option --max-thinking-tokens "${claude_passthrough_opts[@]}" \
    && ! _harness_launcher_argv_disables_thinking "${claude_passthrough_opts[@]}"; then
    passthrough_force_thinking=true
  fi
  return 0
}

_harness_launcher_export_codex_runtime_env() {
  local HARNESS_DIR="$1"
  local prepare="$_HARNESS_LAUNCHER_BIN/codex-home-prepare.sh"
  _harness_launcher_prepare_codex_global_mcp_allowlist || return $?
  _harness_launcher_prepare_codex_apps_allowlist || return $?
  if [[ -x "$prepare" ]]; then
    "$prepare" "$HARNESS_DIR" || return $?
  fi

  export CODEX_HOME="$HARNESS_DIR/.harness/codex"

  # MCP secrets from settings.local.json env so codex streamable_http
  # bearer_token_env_var resolves (native codex inherits no other harness env).
  harness_export_local_env "$HARNESS_DIR"
}

_harness_launcher_codex_synthetic_smoke() {
  local HARNESS_DIR="$1" obs_rc
  if harness_observability_load "$HARNESS_DIR"; then
    "$_HARNESS_LAUNCHER_BIN/codex-synthetic-smoke.py" \
      "$HARNESS_OBSERVABILITY_PROFILE" "$HARNESS_OTLP_HTTP_ENDPOINT"
    return $?
  else
    obs_rc=$?
  fi
  [[ "$obs_rc" -eq 1 ]] || return "$obs_rc"
  echo "harness-launcher: observability is not enabled for this profile" >&2
  return 2
}

_harness_launcher_kiro_bin() { harness_kiro_bin_resolve "$@"; }

_harness_launcher_export_kiro_runtime_env() {
  local HARNESS_DIR="$1"
  local prepare="$_HARNESS_LAUNCHER_BIN/kiro-home-prepare.sh"
  if [[ -x "$prepare" ]]; then
    "$prepare" "$HARNESS_DIR" || return $?
  fi
  export KIRO_HOME="$HARNESS_DIR/.harness/kiro"
  harness_export_local_env "$HARNESS_DIR"
}

_harness_launcher_codex_cd_arg() {
  local -a args=("$@")
  local i arg
  for (( i = 1; i <= ${#args[@]}; i++ )); do
    arg="${args[$i]}"
    case "$arg" in
      --cd|-C)
        (( i < ${#args[@]} )) && print -r -- "${args[$((i + 1))]}"
        return 0
        ;;
      --cd=*)
        print -r -- "${arg#--cd=}"
        return 0
        ;;
    esac
  done
  return 1
}

_harness_launcher_codex_harness_for_args() {
  local cd_arg cd_abs registered
  cd_arg="$(_harness_launcher_codex_cd_arg "$@")" || return 1
  [[ -n "$cd_arg" && -d "$cd_arg" ]] || return 1
  cd_abs="${cd_arg:A}"

  for registered in "${_HARNESS_LAUNCHER_REGISTERED_DIRS[@]}"; do
    [[ "$cd_abs" == "$registered" ]] && {
      print -r -- "$registered"
      return 0
    }
  done

  if [[ -f "$cd_abs/config/launcher.env" ]]; then
    print -r -- "$cd_abs"
    return 0
  fi

  return 1
}

_harness_launcher_auto_runtime() {
  local runtime="$1"; shift
  local harness_auto="$_HARNESS_LAUNCHER_BIN/harness-auto"
  [[ -x "$harness_auto" ]] || {
    echo "harness-launcher: missing executable: $harness_auto" >&2
    return 2
  }
  # Plain commands keep native argv: everything goes after --passthrough, so
  # `claude rich` is a prompt and `codex -a never` is a Codex option. Use
  # `<prefix> <keyword>` for launcher presets.
  if [[ "$runtime" == claude ]]; then
    if _harness_launcher_is_claude_management_command "${1:-}"; then
      "$harness_auto" claude-management "$@"
    else
      # `base` is the host default, not a user choice: mark it so a pure
      # resume can restore the session's own model and effort instead.
      HARNESS_HOST_DEFAULT_MODE=base "$harness_auto" claude base --passthrough "$@"
    fi
  elif [[ "$runtime" == codex && "${1:-}" == (--version|-V|--help|-h) ]]; then
    local codex_bin
    codex_bin="$(_harness_launcher_codex_bin)" || {
      echo "❌ codex not found in PATH" >&2
      return 1
    }
    "$codex_bin" "$@"
  else
    HARNESS_HOST_DEFAULT_MODE=base "$harness_auto" "$runtime" --passthrough "$@"
  fi
}

_harness_launcher_is_claude_management_command() {
  harness_claude_is_management_command "$@"
}

_harness_launcher_auto_claude() {
  _harness_launcher_auto_runtime claude "$@"
}

codex() {
  if (( _HARNESS_LAUNCHER_SHELL_AUTO_ENABLED )); then
    _harness_launcher_auto_runtime codex "$@"
    return $?
  fi
  local codex_bin harness_dir broker_started=false
  local HARNESS_OBSERVABILITY_ACTIVE HARNESS_OBSERVABILITY_ENABLED HARNESS_OBSERVABILITY_PROFILE HARNESS_OTLP_HTTP_ENDPOINT
  local OTEL_RESOURCE_ATTRIBUTES obs_rc
  local inherited_codex_mcp_profile="${HARNESS_CODEX_MCP_PROFILE-}"
  local inherited_codex_mcp_profile_type="${(t)HARNESS_CODEX_MCP_PROFILE}"
  local HARNESS_CODEX_MCP_PROFILE="$inherited_codex_mcp_profile"
  [[ "$inherited_codex_mcp_profile_type" == *-export* ]] && export HARNESS_CODEX_MCP_PROFILE
  codex_bin="$(_harness_launcher_codex_bin)" || {
    echo "❌ codex not found in PATH" >&2
    return 1
  }

  if [[ "${HARNESS_LAUNCHER_DISABLE_CODEX_WRAPPER:-}" != "1" ]]; then
    if harness_dir="$(_harness_launcher_codex_harness_for_args "$@")"; then
      local HARNESS_NAME HARNESS_PREFIX HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST HARNESS_CODEX_APPS_ALLOWLIST HARNESS_CODEX_SLACK_APPS HARNESS_MCP_SURFACE_POLICY="" mcp_surface_policy
      unset HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST HARNESS_CODEX_APPS_ALLOWLIST HARNESS_CODEX_SLACK_APPS
      source "$harness_dir/config/launcher.env"
      mcp_surface_policy="$(harness_mcp_surface_policy_resolve "$HARNESS_MCP_SURFACE_POLICY")" || return $?
      export HARNESS_PREFIX
      # Per-harness GitHub identity: fail-open, never overrides with an empty token.
      local GH_TOKEN HARNESS_GH_USER
      harness_gh_token_load "$harness_dir" || true
      if harness_observability_load "$harness_dir"; then
        OTEL_RESOURCE_ATTRIBUTES="service.name=codex_exec,obs.runtime=codex,obs.profile=$HARNESS_OBSERVABILITY_PROFILE"
        export OTEL_RESOURCE_ATTRIBUTES
      else
        obs_rc=$?
        [[ "$obs_rc" -eq 1 ]] || return "$obs_rc"
      fi
      harness_mcp_surface_policy_is_single_full "$mcp_surface_policy" && unset HARNESS_CODEX_MCP_PROFILE
      _harness_launcher_export_codex_runtime_env "$harness_dir" || return $?
      # This wrapper runs in the user's interactive shell and skips
      # _harness_launcher_run, so it applies the same scrub, but only in the
      # subshell that runs Codex: the shell keeps its own markers. The broker
      # (started here, in the shell, still holding them) reads the launch
      # runtime through this function-local variable.
      local HARNESS_TERMINAL_RUNTIME
      harness_terminal_launch_runtime HARNESS_TERMINAL_RUNTIME
      harness_codex_cmux_broker_start "$_HARNESS_LAUNCHER_BIN/codex-cmux-title-sync.py"
      broker_started=true
    fi
  fi

  if $broker_started; then
    (
      harness_terminal_scrub_env "$HARNESS_TERMINAL_RUNTIME"
      harness_terminal_announce_cwd "$harness_dir"
      "$codex_bin" "$@"
    )
  else
    "$codex_bin" "$@"
  fi
  local rc=$?
  # herdr keeps the last reported cwd: hand the shell's directory back.
  $broker_started && harness_terminal_announce_cwd "$PWD"
  $broker_started && harness_codex_cmux_broker_stop
  return $rc
}

_HARNESS_LAUNCHER_CODEX_WRAPPER_BODY="$functions[codex]"

harness_shell_enable() {
  local harness_auto="$_HARNESS_LAUNCHER_BIN/harness-auto"
  [[ -x "$harness_auto" ]] || {
    echo "harness_shell_enable: missing executable: $harness_auto" >&2
    return 2
  }
  if alias claude >/dev/null 2>&1; then
    echo 'harness_shell_enable: claude alias already exists' >&2
    return 2
  fi
  if alias codex >/dev/null 2>&1; then
    echo 'harness_shell_enable: codex alias already exists' >&2
    return 2
  fi
  if [[ "$functions[codex]" != "$_HARNESS_LAUNCHER_CODEX_WRAPPER_BODY" ]]; then
    echo 'harness_shell_enable: codex function is not launcher-owned' >&2
    return 2
  fi
  if (( _HARNESS_LAUNCHER_SHELL_CLAUDE_OWNED )); then
    if (( $+functions[claude] )) && \
        [[ "$functions[claude]" != "$functions[_harness_launcher_auto_claude]" ]]; then
      echo 'harness_shell_enable: launcher-owned claude function was replaced' >&2
      return 2
    fi
  elif (( $+functions[claude] )); then
    echo 'harness_shell_enable: claude function already exists' >&2
    return 2
  fi

  if (( ! $+functions[claude] )); then
    functions -c _harness_launcher_auto_claude claude || return 2
  fi
  typeset -g _HARNESS_LAUNCHER_SHELL_CLAUDE_OWNED=1
  typeset -g +x _HARNESS_LAUNCHER_SHELL_CLAUDE_OWNED
  typeset -g _HARNESS_LAUNCHER_SHELL_AUTO_ENABLED=1
  typeset -g +x _HARNESS_LAUNCHER_SHELL_AUTO_ENABLED
}

harness_shell_disable() {
  typeset -g _HARNESS_LAUNCHER_SHELL_AUTO_ENABLED=0
  typeset -g +x _HARNESS_LAUNCHER_SHELL_AUTO_ENABLED
  (( _HARNESS_LAUNCHER_SHELL_CLAUDE_OWNED )) || return 0
  if (( $+functions[claude] )); then
    if [[ "$functions[claude]" == "$functions[_harness_launcher_auto_claude]" ]]; then
      unfunction claude
    else
      echo 'harness_shell_disable: claude function is no longer launcher-owned; preserved it' >&2
      return 2
    fi
  fi
  typeset -g _HARNESS_LAUNCHER_SHELL_CLAUDE_OWNED=0
  typeset -g +x _HARNESS_LAUNCHER_SHELL_CLAUDE_OWNED
}

# harness_register <harness-dir>
#   Reads <dir>/config/launcher.env (HARNESS_NAME, HARNESS_PREFIX),
#   defines the prefix function, wires tab completion.
harness_register() {
  local dir="$1"
  [[ -z "$dir" || ! -d "$dir" ]] && { echo "harness_register: invalid dir: $dir" >&2; return 1; }
  dir="${dir:A}"
  local env_file="$dir/config/launcher.env"
  [[ ! -f "$env_file" ]] && { echo "harness_register: missing $env_file" >&2; return 1; }

  local HARNESS_NAME HARNESS_PREFIX HARNESS_MCP_SURFACE_POLICY="" mcp_surface_policy
  source "$env_file"
  mcp_surface_policy="$(harness_mcp_surface_policy_resolve "$HARNESS_MCP_SURFACE_POLICY")" || return $?
  [[ -z "$HARNESS_PREFIX" ]] && { echo "harness_register: HARNESS_PREFIX required in $env_file" >&2; return 1; }
  [[ "$HARNESS_PREFIX" =~ '^[A-Za-z_][A-Za-z0-9_-]*$' ]] || {
    echo "harness_register: invalid HARNESS_PREFIX in $env_file: $HARNESS_PREFIX" >&2
    return 1
  }
  [[ -z "$HARNESS_NAME" ]] && { echo "harness_register: HARNESS_NAME required in $env_file" >&2; return 1; }

  # Remove any pre-existing alias that shadows the configured prefix.
  unalias "$HARNESS_PREFIX" 2>/dev/null || true

  # Define <prefix>() as a thin wrapper around the same executable contract used
  # by external workspace managers. This keeps interactive shells, Orca, and
  # non-interactive automation on one cwd-resolution and policy path.
  local harness_exec="$_HARNESS_LAUNCHER_BIN/harness-exec"
  [[ -x "$harness_exec" ]] || {
    echo "harness_register: missing executable: $harness_exec" >&2
    return 1
  }
  local quoted_dir="${(q)dir}"
  local quoted_harness_exec="${(q)harness_exec}"
  eval "${HARNESS_PREFIX}() { ${quoted_harness_exec} ${quoted_dir} \"\$@\"; }" || return 1
  # Define _<prefix>_complete() — delegates to generic completion
  eval "_${HARNESS_PREFIX}_complete() { _harness_launcher_complete ${quoted_dir} \"\$@\"; }" || return 1
  if (( $+functions[compdef] )); then
    compdef "_${HARNESS_PREFIX}_complete" "$HARNESS_PREFIX"
  fi

  local registered exists=false
  for registered in "${_HARNESS_LAUNCHER_REGISTERED_DIRS[@]}"; do
    [[ "$registered" == "$dir" ]] && { exists=true; break; }
  done
  $exists || _HARNESS_LAUNCHER_REGISTERED_DIRS+=("$dir")
}

# _harness_launcher_announce_run_dir <dir>
#   Announces the agent's run directory to herdr (OSC 7) and marks it, so
#   _harness_launcher_run re-announces the caller's directory once the agent
#   (or the TUI) returns: herdr keeps the last reported cwd.
_harness_launcher_announce_run_dir() {
  _harness_launcher_cwd_announced=1
  harness_terminal_announce_cwd "$1"
}

# _harness_launcher_codex_history_begin <continue|native-uuid>
# Selects the durable history row, takes its lease and prepares a snapshot for a
# canonical-only rollout.  It fills history_* variables of the calling
# _harness_launcher_run (zsh dynamic scope); the caller applies the result.
_harness_launcher_codex_history_begin() {
  local history_request="$1"
  history_selection="$(_harness_launcher_codex_history_select "${HARNESS_DIR:A}" "$history_request")" || {
    echo 'harness-launcher: durable Codex history selection requires an exact local native UUID' >&2
    return 2
  }
  while IFS='=' read -r history_key history_value; do
    case "$history_key" in
      native_id) history_native_id="$history_value" ;;
      snapshot_id) history_snapshot_id="$history_value" ;;
      origin_runtime) history_origin_runtime="$history_value" ;;
    esac
  done <<< "$history_selection"
  _harness_launcher_codex_history_lease_acquire "$history_native_id" || {
    echo 'harness-launcher: selected Codex history is already active' >&2
    return 2
  }
  if [[ -z "$history_snapshot_id" ]]; then
    _harness_launcher_codex_history_prepare "${HARNESS_DIR:A}" "$history_native_id" >/dev/null || {
      _harness_launcher_codex_history_lease_release
      echo 'harness-launcher: selected Codex history cannot be prepared safely' >&2
      return 2
    }
    history_selection="$(_harness_launcher_codex_history_select "${HARNESS_DIR:A}" "$history_native_id")" || return 2
    history_snapshot_id="" history_origin_runtime=""
    while IFS='=' read -r history_key history_value; do
      case "$history_key" in
        snapshot_id) history_snapshot_id="$history_value" ;;
        origin_runtime) history_origin_runtime="$history_value" ;;
      esac
    done <<< "$history_selection"
  fi
  return 0
}

# _harness_launcher_run <harness-dir> [args...]
#   Shared implementation for every registered profile function.
_harness_launcher_run() {
  # The invoking shell's directory (harness-exec inherits the caller's $PWD);
  # announced again to herdr after the agent returns.
  local _harness_launcher_caller_dir="$PWD" _harness_launcher_cwd_announced=0
  # Every launched agent sees one terminal runtime: the launch runtime (`plain`
  # without a TTY, else the marker runtime) is exported as
  # HARNESS_TERMINAL_RUNTIME and every other runtime's variables are removed.
  # Its first step is the Orca CODEX_HOME rule: drop CODEX_HOME and
  # ORCA_CODEX_HOME when CODEX_HOME is exactly Orca's value, so launcher-owned
  # Claude/Codex paths never inherit it; a differing user value is left alone.
  # This runs in the launcher process (harness-exec), not the user's shell.
  local _terminal_runtime
  harness_terminal_launch_runtime _terminal_runtime
  harness_terminal_scrub_env "$_terminal_runtime"
  # Production entry points run without errexit; a caller's errexit would also
  # skip the `always` block that finishes an isolated session.
  setopt localoptions noerrexit
  local HARNESS_DIR="$1"; shift
  # Shell routing marks its host-default `base` for a restore; consume it here
  # so it never reaches the agent or a nested launch.
  local host_default_mode="${HARNESS_HOST_DEFAULT_MODE-}"
  unset HARNESS_HOST_DEFAULT_MODE
  local HARNESS_NAME HARNESS_PREFIX HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST HARNESS_CODEX_APPS_ALLOWLIST HARNESS_CODEX_SLACK_APPS HARNESS_MCP_SURFACE_POLICY="" mcp_surface_policy
  local HARNESS_SESSION_ISOLATION_DEFAULT="0"
  local HARNESS_SESSION_ID="" HARNESS_SOURCE_ROOT="" HARNESS_SESSION_ROOT=""
  local HARNESS_NATIVE_HISTORY_LEASE_FD=""
  local HARNESS_NATIVE_HISTORY_LEASE_ADOPTED=0
  {
  if [[ -n "${HARNESS_CODEX_HISTORY_LEASE_FD:-}" ]]; then
    _harness_launcher_codex_history_lease_adopt "${HARNESS_CODEX_HISTORY_NATIVE_ID:-}" || {
      echo 'harness-launcher: inherited Codex history lease is invalid' >&2
      return 2
    }
  fi
  local config_root="$HARNESS_DIR"
  local HARNESS_RUN_DIR=""
  case "${1:-}" in
    --cwd)
      [[ $# -ge 2 ]] || { echo "harness-launcher: --cwd requires a directory" >&2; return 2; }
      HARNESS_RUN_DIR="$(harness_resolve_run_dir "$HARNESS_DIR" "$2")" || return $?
      shift 2
      ;;
  esac
  local isolated_source_run_dir="$HARNESS_RUN_DIR"

  # This entry accepts raw argv. Consume one discriminator at the selector
  # position; the session runner and isolation classifier receive canonical argv.
  local selector_i=1
  case "${1:-}" in
    --isolated|--no-isolated) selector_i=2 ;;
    --isolated-session) selector_i=3 ;;
  esac
  if [[ "${@[selector_i]-}" == claude ]]; then
    local -a canonical_args=("$@")
    canonical_args[selector_i]=()
    set -- "${canonical_args[@]}"
    case "${@[selector_i]-}" in
      codex|codex-smoke|checkup|kiro|kiro-cli|codex-gateway|claude-management)
        echo 'harness-launcher: explicit claude cannot select another runtime prefix' >&2
        return 2 ;;
    esac
  fi

  # A bare interactive entrypoint chooses its workspace before any runtime
  # preparation, session allocation or foreground collection. Older installed
  # runtimes can omit the helper and retain the historical TUI path.
  if [[ $# -eq 0 && -x "$_HARNESS_LAUNCHER_BIN/harness-workspace-choice" ]]; then
    local workspace_choice workspace_rc
    workspace_choice="$("$_HARNESS_LAUNCHER_BIN/harness-workspace-choice")"
    workspace_rc=$?
    case "$workspace_rc:$workspace_choice" in
      0:isolated) set -- --isolated ;;
      0:shared) set -- --no-isolated ;;
      1:*) return 0 ;;
      130:*) return 130 ;;
      *) return "$workspace_rc" ;;
    esac
  fi

  unset HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST HARNESS_CODEX_APPS_ALLOWLIST HARNESS_CODEX_SLACK_APPS
  source "$HARNESS_DIR/config/launcher.env"
  mcp_surface_policy="$(harness_mcp_surface_policy_resolve "$HARNESS_MCP_SURFACE_POLICY")" || return $?
  export HARNESS_PREFIX

  # SDK hosts run diagnostics such as `auth status` through the same argv
  # prefix (`base --passthrough auth status`); a management subcommand right
  # after the marker runs natively. Only for direct Claude: other runtimes and
  # gateway prefixes keep their own path.
  local mgmt_i=1
  case "${1:-}" in
    --isolated|--no-isolated) mgmt_i=2 ;;
    --isolated-session) mgmt_i=3 ;;
  esac
  if harness_claude_is_management_command "${@[mgmt_i]-}"; then
    set -- claude-management "${@[mgmt_i,-1]}"
    mgmt_i=1
  fi
  if [[ "${@[mgmt_i]-}" != (codex|codex-smoke|checkup|kiro|kiro-cli|codex-gateway|claude-management) ]]; then
    for (( ; mgmt_i < $#; mgmt_i++ )); do
      [[ "${@[mgmt_i]}" == -- ]] && break
      if [[ "${@[mgmt_i]}" == --passthrough ]]; then
        if harness_claude_is_management_command "${@[mgmt_i+1]}"; then
          set -- claude-management "${@[mgmt_i+1,-1]}"
        fi
        break
      fi
    done
  fi

  if [[ "${1:-}" == claude-management ]]; then
    shift
    [[ $# -gt 0 ]] || {
      echo 'harness-launcher: claude-management requires a native Claude subcommand' >&2
      return 2
    }
    local claude_management_bin="${commands[claude]:-}"
    [[ -n "$claude_management_bin" && -x "$claude_management_bin" ]] || {
      echo 'harness-launcher: claude not found in PATH' >&2
      return 1
    }
    (
      [[ -z "$HARNESS_RUN_DIR" ]] || cd "$HARNESS_RUN_DIR" || exit $?
      harness_export_local_env "$HARNESS_DIR" || exit $?
      "$claude_management_bin" "$@"
    )
    return $?
  fi

  case "$HARNESS_SESSION_ISOLATION_DEFAULT" in 0|1) ;; *) echo 'harness-launcher: HARNESS_SESSION_ISOLATION_DEFAULT must be 0 or 1' >&2; return 2;; esac
  case "${HARNESS_SESSION_ISOLATION-}" in ''|0|1) ;; *) echo 'harness-launcher: HARNESS_SESSION_ISOLATION must be 0 or 1' >&2; return 2;; esac
  local isolated=false explicit_isolation_control=false created_isolated_session=false history_local_create=false
  local requested_session_id="" isolation_route="" orca_resume=false orca_resume_id=""
  local history_request="" history_selection="" history_native_id="" history_snapshot_id="" history_origin_runtime="" history_key history_value
  if [[ -f "$_HARNESS_LAUNCHER_BIN/codex-history.py" && ! -L "$_HARNESS_LAUNCHER_BIN/codex-history.py" ]]; then
    history_request="$(_harness_launcher_codex_history_request "$@")" || true
  fi
  if [[ "$history_request" == list ]]; then
    _harness_launcher_codex_history_select "${HARNESS_DIR:A}" list
    return $?
  fi
  if [[ -n "$history_request" ]]; then
    _harness_launcher_codex_history_begin "$history_request" || return $?
    if [[ -n "$history_snapshot_id" ]]; then
      export HARNESS_CODEX_HISTORY_NATIVE_ID="$history_native_id"
      export HARNESS_CODEX_HISTORY_SNAPSHOT_ID="$history_snapshot_id"
      export HARNESS_CODEX_HISTORY_ORIGIN_RUNTIME="$history_origin_runtime"
      set -- codex resume "$history_native_id"
      isolated=true
      history_local_create=true
    fi
  fi
  if [[ "${1:-}" == "--isolated" ]]; then
    isolated=true; explicit_isolation_control=true
    shift
  elif [[ "${1:-}" == "--isolated-session" ]]; then
    [[ $# -ge 2 ]] || { echo 'harness-launcher: --isolated-session requires a UUID' >&2; return 2; }
    isolated=true; explicit_isolation_control=true
    requested_session_id="$2"
    shift 2
  elif [[ "${1:-}" == "--no-isolated" ]]; then
    explicit_isolation_control=true
    shift
  elif [[ "${HARNESS_SESSION_ISOLATION-}" == 1 ]]; then
    isolated=true
  elif [[ "$HARNESS_SESSION_ISOLATION_DEFAULT" == 1 ]]; then
    local isolation_interactive=0
    harness_claude_stdio_is_tty && isolation_interactive=1
    isolation_route="$(harness_session_isolation_default_route "$isolation_interactive" "$@")" || return $?
    case "$isolation_route" in
      isolate) isolated=true ;;
      legacy) ;;
      reject)
        orca_resume_id="$(_harness_launcher_resolve_restore "${HARNESS_DIR:A}" "$@")"
        case $? in
          0) isolated=true; requested_session_id="$orca_resume_id"; orca_resume=true ;;
          3) echo "harness-launcher: resume id is ambiguous (several isolated sessions own it); use '${HARNESS_PREFIX} --isolated-session <uuid> $*'" >&2
             return 2 ;;
          4)
             # The session lives in the canonical checkout.  Never continue it
             # there silently.  Only the pure forms `codex [preset...] [--passthrough]
             # resume <id>` become a fresh isolated session through durable history
             # (presets are kept, so the caller's profile wins over the recorded
             # grant); any other Codex form (-m, -s, -a, -c, context words, prompt
             # text, planner argv) would lose its flags in the rewrite and let the
             # recorded grant be re-applied, so it is refused, as is Claude.
             local canonical_arg canonical_prev="" canonical_codex_id="" canonical_pure=false
             local canonical_cwd="" canonical_alt="" canonical_prepare_failed=false
             local -a canonical_presets=()
             if [[ "${1:-}" == codex ]]; then
               for canonical_arg in "${@:2}"; do
                 [[ "$canonical_arg" == -- ]] && break
                 [[ "$canonical_prev" == resume && "$canonical_arg" =~ $_HARNESS_LAUNCHER_UUID_RE ]] && canonical_codex_id="${(L)canonical_arg}"
                 canonical_prev="$canonical_arg"
               done
               # Pure forms: [profile presets...] [--passthrough] resume <id>, nothing else.
               # Presets stay in the rewrite, so profile_explicit keeps the recorded
               # grant from being re-applied.  Native flags, context words, prompt text
               # and anything after --passthrough but `resume <id>` are refused.
               local -a canonical_rest=("${@:2}")
               while (( ${#canonical_rest} )) && [[ "${canonical_rest[1]}" == (fast|base|sol|plan|rich|astra) ]]; do
                 canonical_presets+=("${canonical_rest[1]}")
                 canonical_rest=("${(@)canonical_rest[2,-1]}")
               done
               [[ "${canonical_rest[1]-}" != --passthrough ]] || canonical_rest=("${(@)canonical_rest[2,-1]}")
               if (( ${#canonical_rest} == 2 )) && [[ "${canonical_rest[1]}" == resume && "${canonical_rest[2]}" =~ $_HARNESS_LAUNCHER_UUID_RE ]]; then
                 canonical_pure=true
               fi
             fi
             if $canonical_pure && _harness_launcher_codex_history_begin "$canonical_codex_id" && [[ -n "$history_snapshot_id" ]]; then
               export HARNESS_CODEX_HISTORY_NATIVE_ID="$history_native_id"
               export HARNESS_CODEX_HISTORY_SNAPSHOT_ID="$history_snapshot_id"
               export HARNESS_CODEX_HISTORY_ORIGIN_RUNTIME="$history_origin_runtime"
               set -- codex "${canonical_presets[@]}" resume "$history_native_id"
               isolated=true
               history_local_create=true
             else
               ! $canonical_pure || canonical_prepare_failed=true
               [[ -z "$isolated_source_run_dir" ]] || canonical_cwd="--cwd ${(q-)isolated_source_run_dir} "
               canonical_alt="start a fresh isolated session ('${HARNESS_PREFIX}'), or for maintenance run '${HARNESS_PREFIX} ${canonical_cwd}--no-isolated ${(j: :)${(q-)@}}'"
               # The pure alternative would fail for the same reason after a failed prepare.
               [[ -z "$canonical_codex_id" ]] || $canonical_prepare_failed || canonical_alt+=", or resume it as a fresh isolated session with '${HARNESS_PREFIX} ${canonical_cwd}codex resume ${canonical_codex_id}'"
               echo "harness-launcher: this session lives in the shared canonical checkout and would keep writing there; $canonical_alt" >&2
               return 2
             fi
             ;;
          *) echo "harness-launcher: this profile isolates fresh sessions; use '${HARNESS_PREFIX} --isolated-session <uuid> $*' or '${HARNESS_PREFIX} --no-isolated $*'" >&2
             return 2 ;;
        esac
        ;;
      *) echo 'harness-launcher: invalid or conflicting isolation controls' >&2; return 2 ;;
    esac
  fi
  # checkup audits the harness root itself; reject before any clone exists.
  if $isolated && [[ "${1:-}" == checkup ]]; then
    echo "harness-launcher: checkup audits the harness root and cannot run in an isolated session; use '${HARNESS_PREFIX} --no-isolated checkup prompt-audit'" >&2
    return 2
  fi
  if $isolated; then
    isolation_route="$(harness_session_isolation_default_route 1 "$@")" || return $?
    [[ "$isolation_route" != invalid ]] || { echo 'harness-launcher: invalid or conflicting isolation controls' >&2; return 2; }
    if ! $history_local_create && [[ -z "$requested_session_id" && "$isolation_route" == reject ]]; then
      orca_resume_id="$(_harness_launcher_resolve_restore "${HARNESS_DIR:A}" "$@")"
      case $? in
        0) requested_session_id="$orca_resume_id"; orca_resume=true ;;
        3) echo "harness-launcher: resume id is ambiguous (several isolated sessions own it); use '${HARNESS_PREFIX} --isolated-session <uuid> $*'" >&2
           return 2 ;;
        *) echo "harness-launcher: continuation requires '${HARNESS_PREFIX} --isolated-session <uuid> $*' or intentional '${HARNESS_PREFIX} --no-isolated $*'" >&2
           return 2 ;;
      esac
    fi
  elif $explicit_isolation_control; then
    isolation_route="$(harness_session_isolation_default_route 1 "$@")" || return $?
    [[ "$isolation_route" != invalid ]] || { echo 'harness-launcher: invalid or conflicting isolation controls' >&2; return 2; }
  fi
  if $isolated; then
    local source_root="${HARNESS_DIR:A}"
    local HARNESS_SESSION_LEASE_FD=""
    if [[ -n "$requested_session_id" ]]; then
      _harness_launcher_isolated_lease_acquire "$requested_session_id" || {
        local resume_rc=$?
        $orca_resume && echo "harness-launcher: could not restore isolated session $requested_session_id" >&2
        return "$resume_rc"
      }
      if ! harness_claude_stdio_is_tty; then
        HARNESS_HISTORY_SOURCE_ROOT="$source_root" "$_HARNESS_LAUNCHER_BIN/session-isolation.sh" gc >/dev/null 2>&1 || echo 'harness-launcher: warning: isolated-session GC failed; workspaces retained' >&2
      fi
      _harness_launcher_isolated_session_create "$source_root" "$requested_session_id" || {
        local resume_rc=$?
        zsystem flock -u "$HARNESS_SESSION_LEASE_FD" 2>/dev/null || true
        $orca_resume && echo "harness-launcher: could not restore isolated session $requested_session_id" >&2
        return "$resume_rc"
      }
    else
      # Do not make a new interactive start wait on retention collection.
      # Noninteractive launches retain the legacy best-effort maintenance.
      if ! harness_claude_stdio_is_tty; then
        HARNESS_HISTORY_SOURCE_ROOT="$source_root" "$_HARNESS_LAUNCHER_BIN/session-isolation.sh" gc >/dev/null 2>&1 || echo 'harness-launcher: warning: isolated-session GC failed; workspaces retained' >&2
      fi
      if $history_local_create; then
        _harness_launcher_isolated_session_create "$source_root" "" local || return $?
      else
        _harness_launcher_isolated_session_create "$source_root" || return $?
      fi
      _harness_launcher_isolated_lease_acquire "$HARNESS_SESSION_ID" || return $?
      created_isolated_session=true
    fi
    HARNESS_DIR="$HARNESS_SESSION_ROOT"
    if [[ -n "$isolated_source_run_dir" ]]; then
      HARNESS_RUN_DIR="$("$_HARNESS_LAUNCHER_BIN/session-isolation.sh" resolve-run-dir "$source_root" "$HARNESS_SESSION_ID" "$isolated_source_run_dir")" || {
        echo 'harness-launcher: isolated session could not bind the requested working directory' >&2
        _harness_launcher_isolated_finish "$HARNESS_SESSION_ID" "" "${HARNESS_SESSION_LEASE_FD:-}"
        return 2
      }
    else
      HARNESS_RUN_DIR="$HARNESS_SESSION_ROOT"
    fi
    export HARNESS_RUN_DIR
    # Claude files transcripts under its cwd's project directory, which is the
    # caller's run directory (harness-exec passes --cwd), not the session root.
    _harness_launcher_isolated_record_run_dir
    if $created_isolated_session; then
      if [[ "${1:-}" == codex ]]; then
        echo "harness-launcher: isolated session $HARNESS_SESSION_ID; continue: ${HARNESS_PREFIX} --isolated-session $HARNESS_SESSION_ID codex resume" >&2
      else
        echo "harness-launcher: isolated session $HARNESS_SESSION_ID; continue: ${HARNESS_PREFIX} --isolated-session $HARNESS_SESSION_ID resume" >&2
      fi
    fi
  fi
  local isolated_session_id="${HARNESS_SESSION_ID:-}" isolated_heartbeat_pid="" native_history_lease_id=""
  native_history_lease_id="${HARNESS_CODEX_HISTORY_NATIVE_ID:-}"
  [[ "$native_history_lease_id" =~ $_HARNESS_LAUNCHER_UUID_RE ]] || native_history_lease_id="$(_harness_launcher_codex_history_request "$@" 2>/dev/null || true)"
  if [[ "$native_history_lease_id" =~ $_HARNESS_LAUNCHER_UUID_RE ]]; then
    if [[ -n "${HARNESS_NATIVE_HISTORY_LEASE_FD:-}" ]]; then
      :
    elif [[ -n "${HARNESS_CODEX_HISTORY_LEASE_FD:-}" ]]; then
      _harness_launcher_codex_history_lease_adopt "$native_history_lease_id" || {
        echo 'harness-launcher: inherited Codex history lease is invalid' >&2
        return 2
      }
    else
      _harness_launcher_codex_history_lease_acquire "$native_history_lease_id" || {
      echo 'harness-launcher: selected Codex history is already active' >&2
      return 2
      }
    fi
  fi
  # Every exit after the isolated session is acquired, including error
  # returns, stops a started heartbeat and finishes the session once.
  {
    if [[ -n "$isolated_session_id" && -n "$isolated_source_run_dir" ]]; then
      HARNESS_RUN_DIR="$("$_HARNESS_LAUNCHER_BIN/session-isolation.sh" resolve-run-dir "$HARNESS_SOURCE_ROOT" "$isolated_session_id" "$isolated_source_run_dir")" || {
        echo 'harness-launcher: isolated session working-directory binding changed before launch' >&2
        return 2
      }
    fi
    _harness_launcher_run_session "$@"
  } always {
    (( _harness_launcher_cwd_announced )) && harness_terminal_announce_cwd "$_harness_launcher_caller_dir"
    if [[ -n "$isolated_session_id" ]]; then
      _harness_launcher_isolated_finish "$isolated_session_id" "$isolated_heartbeat_pid" "${HARNESS_SESSION_LEASE_FD:-}"
    fi
  }
  } always {
    _harness_launcher_codex_history_lease_release
  }
}

# _harness_launcher_run_session [args...]
#   Launch body of _harness_launcher_run after isolation is settled. Reads the
#   caller's locals (zsh dynamic scope) and sets its isolated_heartbeat_pid.
_harness_launcher_run_session() {
  local -a claude_args=() claude_passthrough_args=() claude_passthrough_opts=() claude_prompt_args=()
  local session_flag="" skip_tui=false env_effort="" provider_url="" gateway_api_key="" provider_name=""
  local mode_applied=false mcp_surface="full" passthrough=false passthrough_force_thinking=false
  local host_default_base=false
  [[ "${host_default_mode-}" == base && "${1:-}" == base && "${2:-}" == --passthrough ]] && host_default_base=true

  # Optional provider prefix (must be first arg)
  case "${1:-}" in
    kiro)
      provider_name="kiro"
      local _env_file="$config_root/config/.local/kiro-gateway.env"
      if [[ -f "$_env_file" ]]; then
        source "$_env_file"
        [[ -n "${KIRO_GATEWAY_URL:-}" ]] && provider_url="$KIRO_GATEWAY_URL"
        [[ -n "${KIRO_GATEWAY_API_KEY:-}" ]] && gateway_api_key="$KIRO_GATEWAY_API_KEY"
      fi
      [[ -z "$provider_url" ]] && echo "❌ KIRO_GATEWAY_URL이 설정되지 않았습니다" && return 1
      _harness_launcher_probe_provider_health "$provider_url" \
        || { echo "❌ kiro-gateway에 연결할 수 없습니다 ($provider_url)"; return 1; }
      skip_tui=true; shift ;;
    codex)
      shift
      if [[ -n "$isolated_session_id" ]]; then _harness_launcher_isolated_heartbeat "$isolated_session_id" & isolated_heartbeat_pid=$!; fi
      _harness_launcher_run_codex_cli "$HARNESS_DIR" "$mcp_surface_policy" "$@"
      return $?
      ;;
    checkup)
      shift
      _harness_launcher_run_checkup "$HARNESS_DIR" "$@"
      return $?
      ;;
    codex-smoke)
      shift
      [[ $# -eq 0 ]] || { echo "harness-launcher: codex-smoke takes no arguments" >&2; return 2; }
      if [[ -n "$isolated_session_id" ]]; then _harness_launcher_isolated_heartbeat "$isolated_session_id" & isolated_heartbeat_pid=$!; fi
      _harness_launcher_codex_synthetic_smoke "$HARNESS_DIR"
      return $?
      ;;
    kiro-cli)
      shift
      if [[ -n "$isolated_session_id" ]]; then _harness_launcher_isolated_heartbeat "$isolated_session_id" & isolated_heartbeat_pid=$!; fi
      _harness_launcher_run_kiro_cli "$HARNESS_DIR" "$@"
      return $?
      ;;
    codex-gateway)
      provider_name="codex"
      local _env_file="$config_root/config/.local/codex-gateway.env"
      if [[ -f "$_env_file" ]]; then
        source "$_env_file"
        [[ -n "${CODEX_GATEWAY_URL:-}" ]] && provider_url="$CODEX_GATEWAY_URL"
        [[ -n "${CODEX_GATEWAY_API_KEY:-}" ]] && gateway_api_key="$CODEX_GATEWAY_API_KEY"
      fi
      [[ -z "$provider_url" ]] && echo "❌ CODEX_GATEWAY_URL이 설정되지 않았습니다" && return 1
      _harness_launcher_probe_provider_health "$provider_url" \
        || { echo "❌ codex-gateway에 연결할 수 없습니다 ($provider_url)"; return 1; }
      skip_tui=true; shift ;;
  esac
  # Codex, Kiro CLI and checkup have returned; the routes still here launch
  # Claude Code (direct or gateway) or the TUI, which can also pick Claude.
  _harness_launcher_isolated_claude_plugins

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --passthrough)
        # SDK hosts (e.g. Paseo) append their own Claude argv after this
        # marker. Forward it verbatim: option values such as
        # `--permission-mode plan` or `--effort high` are not launcher keywords.
        shift
        claude_passthrough_args=("$@")
        passthrough=true; skip_tui=true
        break ;;
      --)
        # Everything after a launcher `--` is prompt text, forwarded last.
        claude_prompt_args=("$@")
        skip_tui=true
        break ;;
      fast|base|plan|opus|rich)
        harness_mode_resolve "$1" "${provider_name:-direct}"
        claude_args+=(--model "$HARNESS_MODE_MODEL")
        env_effort="$HARNESS_MODE_EFFORT"
        skip_tui=true; mode_applied=true; shift ;;
      fable)
        if ! harness_mode_resolve fable "${provider_name:-direct}"; then
          echo "❌ fable는 Anthropic direct 전용입니다 (codex/kiro 미지원)" >&2
          return 1
        fi
        claude_args+=(--model "$HARNESS_MODE_MODEL"); env_effort="$HARNESS_MODE_EFFORT"
        skip_tui=true; mode_applied=true; shift ;;
      ultracode)
        # ultracode = xhigh + dynamic workflow orchestration. The orchestration
        # half is a SESSION-ONLY Claude Code preset: the CLI rejects 'ultracode'
        # as an --effort / env / settings value (allowed: low|medium|high|xhigh|
        # max), so it cannot be set at launch. Launch as rich (opus[1m] + xhigh)
        # and remind the user to flip it on in-session via /effort.
        # Anthropic direct only — kiro/codex gateways don't support it at all.
        if ! harness_mode_resolve ultracode "${provider_name:-direct}"; then
          echo "❌ ultracode는 Anthropic direct 전용입니다 (codex/kiro 미지원)" >&2
          return 1
        fi
        claude_args+=(--model "$HARNESS_MODE_MODEL"); env_effort="$HARNESS_MODE_EFFORT"
        harness_ultracode_hint
        skip_tui=true; mode_applied=true; shift ;;
      low|medium|high|xhigh|max)
        env_effort="$1"
        if ! $mode_applied; then
          case "$provider_name" in
            kiro)  ;;
            codex) claude_args+=(--model "sonnet${CODEX_CONTEXT_SUFFIX:-}") ;;
            *)     claude_args+=(--model sonnet) ;;
          esac
        fi
        skip_tui=true; shift ;;
      light)
        if [[ "$provider_name" == kiro ]]; then
          mcp_surface="light"
        else
          case "$mcp_surface_policy" in
            legacy) mcp_surface="light" ;;
            single-full-compat)
              echo "⚠️  light MCP surface is deprecated for this project; using full" >&2
              mcp_surface="full"
              ;;
            single-full)
              echo "harness-launcher: light MCP surface is retired for this project" >&2
              return 2
              ;;
          esac
        fi
        skip_tui=true; shift ;;
      continue) session_flag="--continue"; skip_tui=true; shift ;;
      resume)   session_flag="--resume"; skip_tui=true; shift ;;
      bypass)       claude_args+=(--permission-mode bypassPermissions); skip_tui=true; shift ;;
      acceptEdits)  claude_args+=(--permission-mode acceptEdits); skip_tui=true; shift ;;
      dontAsk)      claude_args+=(--permission-mode dontAsk); skip_tui=true; shift ;;
      --chrome|--no-chrome)
                    claude_args+=("$1"); skip_tui=true; shift ;;
      *)            claude_args+=("$1"); shift ;;
    esac
  done

  _harness_launcher_claude_restore_apply

  if $passthrough; then
    # Caller options end at its own `--`; later tokens are prompt text and
    # are never scanned for options.
    local _pt_i=${claude_passthrough_args[(ie)--]}
    claude_passthrough_opts=("${(@)claude_passthrough_args[1,_pt_i-1]}")
    _harness_launcher_passthrough_reconcile || return $?
  fi

  if $skip_tui; then
    # Shortcut/gateway paths below launch Claude directly. Native Codex/Kiro
    # returned above; no-argument TUI selection is handled inside launcher.sh.
    local HARNESS_OBSERVABILITY_ACTIVE HARNESS_OBSERVABILITY_ENABLED HARNESS_OBSERVABILITY_PROFILE
    local HARNESS_OTLP_HTTP_ENDPOINT obs_rc
    # Per-harness GitHub identity: fail-open, never overrides with an empty token.
    local GH_TOKEN HARNESS_GH_USER
    harness_gh_token_load "$HARNESS_DIR" || true
    if harness_observability_load "$HARNESS_DIR"; then
      local CLAUDE_CODE_ENABLE_TELEMETRY=1
      local OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp OTEL_TRACES_EXPORTER=none
      local OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
      local OTEL_EXPORTER_OTLP_ENDPOINT="$HARNESS_OTLP_HTTP_ENDPOINT"
      local OTEL_EXPORTER_OTLP_LOGS_ENDPOINT="$HARNESS_OTLP_HTTP_ENDPOINT/v1/logs"
      local OTEL_EXPORTER_OTLP_METRICS_ENDPOINT="$HARNESS_OTLP_HTTP_ENDPOINT/v1/metrics"
      local OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="$HARNESS_OTLP_HTTP_ENDPOINT/v1/traces"
      local OTEL_EXPORTER_OTLP_LOGS_PROTOCOL=http/protobuf
      local OTEL_EXPORTER_OTLP_METRICS_PROTOCOL=http/protobuf
      local OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
      local OTEL_EXPORTER_OTLP_HEADERS="" OTEL_EXPORTER_OTLP_LOGS_HEADERS=""
      local OTEL_EXPORTER_OTLP_METRICS_HEADERS="" OTEL_EXPORTER_OTLP_TRACES_HEADERS=""
      local OTEL_RESOURCE_ATTRIBUTES="service.name=harness-agent,obs.runtime=claude_code,obs.profile=$HARNESS_OBSERVABILITY_PROFILE"
      local OTEL_LOG_USER_PROMPTS=0 OTEL_LOG_ASSISTANT_RESPONSES=0
      local OTEL_LOG_TOOL_DETAILS=0 OTEL_LOG_TOOL_CONTENT=0 OTEL_LOG_RAW_API_BODIES=0
      export CLAUDE_CODE_ENABLE_TELEMETRY OTEL_METRICS_EXPORTER OTEL_LOGS_EXPORTER OTEL_TRACES_EXPORTER
      export OTEL_EXPORTER_OTLP_PROTOCOL OTEL_EXPORTER_OTLP_ENDPOINT OTEL_RESOURCE_ATTRIBUTES
      export OTEL_EXPORTER_OTLP_LOGS_ENDPOINT OTEL_EXPORTER_OTLP_METRICS_ENDPOINT OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
      export OTEL_EXPORTER_OTLP_LOGS_PROTOCOL OTEL_EXPORTER_OTLP_METRICS_PROTOCOL OTEL_EXPORTER_OTLP_TRACES_PROTOCOL
      export OTEL_EXPORTER_OTLP_HEADERS OTEL_EXPORTER_OTLP_LOGS_HEADERS OTEL_EXPORTER_OTLP_METRICS_HEADERS OTEL_EXPORTER_OTLP_TRACES_HEADERS
      export OTEL_LOG_USER_PROMPTS OTEL_LOG_ASSISTANT_RESPONSES
      export OTEL_LOG_TOOL_DETAILS OTEL_LOG_TOOL_CONTENT OTEL_LOG_RAW_API_BODIES
    else
      obs_rc=$?
      [[ "$obs_rc" -eq 1 ]] || return "$obs_rc"
    fi

    [[ -n "$session_flag" ]] && claude_args=("$session_flag" "${claude_args[@]}")
    if [[ -n "$provider_url" ]]; then
      export ANTHROPIC_BASE_URL="$provider_url"
      if [[ -n "$gateway_api_key" ]]; then
        export ANTHROPIC_AUTH_TOKEN="$gateway_api_key"
        unset ANTHROPIC_API_KEY
      fi
      unset ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL ANTHROPIC_CUSTOM_HEADERS
      if [[ "$provider_name" == "codex" ]]; then
        # DRIFT FIX: no dead fallbacks — only export what user configured
        [[ -n "${CODEX_OPUS_MODEL:-}" ]]   && export ANTHROPIC_DEFAULT_OPUS_MODEL="$CODEX_OPUS_MODEL"
        [[ -n "${CODEX_SONNET_MODEL:-}" ]] && export ANTHROPIC_DEFAULT_SONNET_MODEL="$CODEX_SONNET_MODEL"
        [[ -n "${CODEX_HAIKU_MODEL:-}" ]]  && export ANTHROPIC_DEFAULT_HAIKU_MODEL="$CODEX_HAIKU_MODEL"
      fi
    fi
    local force_thinking=false
    if [[ -n "$env_effort" ]]; then
      claude_args+=(--effort "$env_effort")
      # The API rejects xhigh/max while thinking is disabled ("effort 'xhigh' is
      # not supported when thinking is disabled on this model"). rich/ultracode
      # both resolve to xhigh, so a user with alwaysThinkingEnabled=false in
      # settings.json got a 400 at first prompt. Force it on for those efforts
      # instead of depending on per-machine settings; high and below are
      # unaffected and keep the user's own choice.
      [[ "$env_effort" == (xhigh|max) ]] && force_thinking=true
    elif $passthrough_force_thinking; then
      force_thinking=true
    fi
    # One launcher --settings: forced thinking merged with the SessionStart
    # hook that records the launch grant for a later restore.
    local launch_grant launch_settings launch_context
    launch_grant="$(_harness_launcher_claude_launch_grant)"
    launch_context="$(_harness_launcher_claude_launch_context)"
    launch_settings="$(_harness_launcher_claude_launch_settings "$force_thinking" "$launch_grant" "$launch_context")" || return $?
    [[ -z "$launch_settings" ]] || claude_args+=(--settings "$launch_settings")
    if [[ "${HARNESS_HEADLESS:-0}" == 1 && -n "${HARNESS_SESSION_ROOT:-}" ]]; then
      claude_args+=(--settings "$(harness_headless_session_settings "${HARNESS_SESSION_ROOT:A}")")
    fi
    # Passthrough argv goes last, after the launcher-owned flags. The boolean
    # --exclude-dynamic-system-prompt-sections then closes the variadic
    # --mcp-config value list, so neither a caller prompt nor a caller `--`
    # can swallow or displace launcher flags.
    local -a claude_launch_tail=()
    if $passthrough; then
      claude_launch_tail=(--exclude-dynamic-system-prompt-sections "${claude_passthrough_args[@]}")
    else
      claude_args+=(--exclude-dynamic-system-prompt-sections)
    fi
    claude_launch_tail+=("${claude_prompt_args[@]}")
    local HARNESS_CLAUDE_TITLE_BOOTSTRAP_ID="" HARNESS_CLAUDE_TITLE_BOOTSTRAP_VALUE=""
    local _claude_interactive=0
    harness_claude_stdio_is_tty && _claude_interactive=1
    if harness_claude_bootstrap_eligible claude "${provider_name:-direct}" "$_claude_interactive" "${claude_args[@]}" "${claude_passthrough_opts[@]}"; then
      IFS=$'\t' read -r HARNESS_CLAUDE_TITLE_BOOTSTRAP_ID HARNESS_CLAUDE_TITLE_BOOTSTRAP_VALUE \
        < <(harness_claude_bootstrap_values) || true
      if [[ -n "$HARNESS_CLAUDE_TITLE_BOOTSTRAP_ID" ]]; then
        export HARNESS_CLAUDE_TITLE_BOOTSTRAP_ID HARNESS_CLAUDE_TITLE_BOOTSTRAP_VALUE
        claude_args+=(--name "$HARNESS_CLAUDE_TITLE_BOOTSTRAP_VALUE")
      fi
    fi
    # A fresh isolated Claude launch names its session after the isolated
    # session so an Orca restore (`--resume <id>`) can be mapped back to it.
    # Appended after the bootstrap decision: bootstrap eligibility rejects
    # --session-id, and this keeps --name.
    if $created_isolated_session && [[ "$isolation_route" == isolate ]]; then
      local _sid_arg _sid_ok=true
      for _sid_arg in "${claude_args[@]}" "${claude_passthrough_opts[@]}"; do
        case "$_sid_arg" in
          --session-id|--session-id=*|-c|--continue|-r|-r?*|--resume|--resume=*|--fork-session|--fork-session=*) _sid_ok=false ;;
        esac
      done
      $_sid_ok && claude_args+=(--session-id "${(L)HARNESS_SESSION_ID}")
    fi
    harness_autocompact_pct "${provider_name:-direct}" "${claude_args[@]}" "${claude_passthrough_opts[@]}"
    # Shared-table globals must not linger in the interactive shell.
    unset HARNESS_MODE_MODEL HARNESS_MODE_EFFORT
    # Plain invocation (not exec) so the user's interactive shell survives
    # the launched process — Ctrl+C returns to the prompt instead of closing
    # the terminal window.
    local claude_broker_started=false
    if [[ "$mcp_surface" == "light" ]]; then
      local _light_file
      _light_file="$(harness_claude_light_mcp_config "$HARNESS_DIR" "$_HARNESS_LAUNCHER_BIN")" || return $?
      if [[ -n "$isolated_session_id" ]]; then _harness_launcher_isolated_heartbeat "$isolated_session_id" & isolated_heartbeat_pid=$!; fi
      harness_claude_cmux_broker_start "$_HARNESS_LAUNCHER_BIN/codex-cmux-title-sync.py" "$HARNESS_DIR"
      claude_broker_started=true
      _harness_launcher_announce_run_dir "${HARNESS_RUN_DIR:-$PWD}"
      (
        [[ -z "$HARNESS_RUN_DIR" ]] || cd "$HARNESS_RUN_DIR" || exit $?
        harness_export_local_env "${HARNESS_SOURCE_ROOT:-$HARNESS_DIR}" || exit $?
        local -a HARNESS_SLACK_ARGV=()
        harness_slack_claude_argv "$_HARNESS_SLACK_POLICY" --strict-mcp-config --mcp-config "$_light_file" "${claude_args[@]}" "${claude_launch_tail[@]}" || exit $?
        trap 'harness_slack_settings_cleanup' EXIT
        claude "${HARNESS_SLACK_ARGV[@]}"
      )
    else
      if [[ -n "$isolated_session_id" ]]; then _harness_launcher_isolated_heartbeat "$isolated_session_id" & isolated_heartbeat_pid=$!; fi
      harness_claude_cmux_broker_start "$_HARNESS_LAUNCHER_BIN/codex-cmux-title-sync.py" "$HARNESS_DIR"
      claude_broker_started=true
      _harness_launcher_announce_run_dir "${HARNESS_RUN_DIR:-$PWD}"
      (
        [[ -z "$HARNESS_RUN_DIR" ]] || cd "$HARNESS_RUN_DIR" || exit $?
        harness_export_local_env "${HARNESS_SOURCE_ROOT:-$HARNESS_DIR}" || exit $?
        _harness_launcher_claude_mcp_local_args "$HARNESS_DIR" || exit $?
        local -a HARNESS_SLACK_ARGV=()
        harness_slack_claude_argv "$_HARNESS_SLACK_POLICY" "${claude_args[@]}" "${reply[@]}" "${claude_launch_tail[@]}" || exit $?
        trap 'harness_slack_settings_cleanup' EXIT
        claude "${HARNESS_SLACK_ARGV[@]}"
      )
    fi
    local rc=$?
    $claude_broker_started && harness_claude_cmux_broker_stop
    return $rc
  else
    if [[ -n "$isolated_session_id" ]]; then _harness_launcher_isolated_heartbeat "$isolated_session_id" & isolated_heartbeat_pid=$!; fi
    _harness_launcher_announce_run_dir "${HARNESS_RUN_DIR:-$HARNESS_DIR}"
    HARNESS_DIR="$HARNESS_DIR" HARNESS_NAME="$HARNESS_NAME" HARNESS_PREFIX="$HARNESS_PREFIX" \
      HARNESS_RUN_DIR="${HARNESS_RUN_DIR:-}" \
      "$_HARNESS_LAUNCHER_BIN/launcher.sh"
    return $?
  fi
}

# _harness_launcher_run_checkup <harness-dir> prompt-audit [preset] [--max-budget-usd N]
#   Runs Claude Code's `/checkup prompt-audit` headless at the harness root.
#   --restricted drops user/project/local settings (hooks, allow rules, default
#   modes) and MCP. Under dontAsk the allow list adds only two git commands that
#   neither write files, run commands, nor print file contents; other Bash
#   commands run only when Claude Code's read-only check accepts them. An allow
#   rule for git blame/log/show would also accept --contents/--output. Secrets
#   and earlier reports are denied even though Glob could list them. The session is not persisted, so `<prefix> continue` never
#   resumes the audit. The raw JSON, stderr, and report stay under
#   .harness/reports/checkup/; stdout gets one status line without report text,
#   so a caller running several profiles never sees another profile's content.
_harness_launcher_run_checkup() {
  local HARNESS_DIR="$1"; shift
  local usage="usage: ${HARNESS_PREFIX:-<prefix>} checkup prompt-audit [fast|base|opus|rich|fable] [--max-budget-usd N]"
  if [[ "${1:-}" != prompt-audit ]]; then
    echo "harness-launcher: $usage" >&2
    return 2
  fi
  shift
  # `plan` (opusplan) is left out: without plan mode it runs as Sonnet.
  local preset="opus" preset_set=false budget="${HARNESS_CHECKUP_MAX_BUDGET_USD:-20}"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      fast|base|opus|rich|fable)
        $preset_set && { echo "harness-launcher: checkup accepts one preset; $usage" >&2; return 2; }
        preset="$1"; preset_set=true; shift ;;
      --max-budget-usd)
        [[ $# -ge 2 ]] || { echo "harness-launcher: --max-budget-usd requires a value" >&2; return 2; }
        budget="$2"; shift 2 ;;
      --max-budget-usd=*) budget="${1#*=}"; shift ;;
      *) echo "harness-launcher: checkup does not accept '$1'; $usage" >&2; return 2 ;;
    esac
  done
  if [[ ! "$budget" =~ '^[0-9]+(\.[0-9]+)?$' ]] || (( budget <= 0 )); then
    echo "harness-launcher: --max-budget-usd must be a positive number (got '$budget')" >&2
    return 2
  fi

  harness_mode_resolve "$preset" direct || return 2
  local model="$HARNESS_MODE_MODEL" effort="$HARNESS_MODE_EFFORT"
  unset HARNESS_MODE_MODEL HARNESS_MODE_EFFORT
  local py settings
  py="$(harness_python3_resolve)" || return 1
  settings="$("$py" - "$HARNESS_DIR" "$effort" <<'PY'
import json, sys
root, effort = sys.argv[1].rstrip("/"), sys.argv[2]
private = [".claude/settings*.json", ".mcp*.json", "mcp*.local.json",
           "config/.local/**", ".harness/**"]
settings = {"permissions": {
    "allow": ["Bash(git ls-files:*)", "Bash(git check-ignore:*)"],
    # `//` anchors a rule at the filesystem root; `**/` covers nested
    # worktrees and projects.
    "deny": ["Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch"]
            + [f"Read(/{root}/{prefix}{path})" for path in private for prefix in ("", "**/")],
}}
if effort in ("xhigh", "max"):
    settings["alwaysThinkingEnabled"] = True
print(json.dumps(settings))
PY
)" || return 1
  # User-level configuration the audit covers; the ~/.claude root (settings,
  # credentials) is deliberately not added.
  local -a add_dirs=()
  local sub
  for sub in skills commands agents output-styles rules plugins; do
    [[ -d "$HOME/.claude/$sub" ]] && add_dirs+=(--add-dir "$HOME/.claude/$sub")
  done

  local report_dir="$HARNESS_DIR/.harness/reports/checkup"
  local stem="$report_dir/prompt-audit-$(date -u +%Y%m%dT%H%M%SZ)-$$" n=0
  local base="$stem"
  while [[ -e "$base.json" ]]; do n=$(( n + 1 )); base="$stem-$n"; done
  ( umask 077; mkdir -p "$report_dir" && chmod 700 "$report_dir" ) || return 1

  # Best-effort signal, not a boundary: concurrent sessions on the same tree
  # also change it.
  local guard=false tree_before="" tree_after=""
  if git -C "$HARNESS_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    guard=true
    tree_before="$(git -C "$HARNESS_DIR" status --porcelain=v1 --untracked-files=all -- . ':(exclude).harness/reports/checkup' 2>/dev/null)"
  fi

  echo "checkup prompt-audit: running at $HARNESS_DIR (model=$model effort=$effort budget=\$$budget); this can take several minutes" >&2
  # Claude's duration_ms leaves out time spent waiting on subagents; report the
  # launcher's own wall-clock time.
  local rc=0 started=$SECONDS
  (
    cd "$HARNESS_DIR" || exit $?
    # A caller launched through a gateway profile must not route this harness's
    # audit through its provider or hand it its GitHub token; the harness's own
    # local env below may set them again.
    unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_CUSTOM_HEADERS \
      ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL \
      GH_TOKEN
    harness_export_local_env "$HARNESS_DIR" || exit $?
    # A caller that is itself a Claude session must not link this run to its
    # session, messaging socket, terminal, or telemetry profile.
    unset CLAUDECODE CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_ENTRYPOINT \
      CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_CODE_SESSION_ATTENDED \
      CLAUDE_CODE_EXECPATH CLAUDE_CODE_ENABLE_TELEMETRY \
      HARNESS_CLAUDE_TITLE_BOOTSTRAP_ID HARNESS_CLAUDE_TITLE_BOOTSTRAP_VALUE \
      HARNESS_SESSION_ID HARNESS_SESSION_ROOT HARNESS_SOURCE_ROOT HARNESS_RUN_DIR
    unset -m 'OTEL_*' 'CMUX_*' 'ORCA_*' 'HERDR_*'
    # --restricted skips project settings env; keep Glob honoring .gitignore.
    export CLAUDE_CODE_GLOB_NO_IGNORE=false CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
    umask 077
    claude -p "/checkup prompt-audit" --restricted \
      --tools "Read,Grep,Glob,Bash,Agent" --permission-mode dontAsk \
      --settings "$settings" --strict-mcp-config "${add_dirs[@]}" \
      --model "$model" --effort "$effort" \
      --output-format json --max-budget-usd "$budget" --no-session-persistence \
      < /dev/null > "$base.json" 2> "$base.stderr"
  ) || rc=$?
  local duration=$(( SECONDS - started ))
  [[ -s "$base.stderr" ]] || rm -f "$base.stderr"

  local extra=""
  if $guard; then
    tree_after="$(git -C "$HARNESS_DIR" status --porcelain=v1 --untracked-files=all -- . ':(exclude).harness/reports/checkup' 2>/dev/null)"
    local -a before_lines=(${(f)tree_before}) after_lines=(${(f)tree_after})
    local -a gone=(${before_lines:|after_lines}) added=(${after_lines:|before_lines})
    local changed=$(( ${#gone} + ${#added} ))
    if (( changed )); then
      extra+=" tree_changed=$changed"
      echo "harness-launcher: working tree changed during checkup ($changed git status entries); if no other session was writing, review git status" >&2
    fi
  fi
  [[ -f "$base.stderr" ]] && extra=" stderr=$base.stderr$extra"

  local state subtype cost turns denials
  IFS=$'\t' read -r state subtype cost turns denials < <("$py" - "$base.json" "$base.md" <<'PY'
import json, os, sys
raw, md = sys.argv[1], sys.argv[2]
try:
    with open(raw, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict):
        raise ValueError("not an object")
except Exception:
    print("nojson")
    sys.exit(0)
res = d.get("result")
wrote = isinstance(res, str) and bool(res.strip())
if wrote:
    fd = os.open(md, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(res if res.endswith("\n") else res + "\n")
subtype = str(d.get("subtype") or "unknown")
ok = d.get("is_error") is False and subtype == "success" and wrote
if not ok and subtype == "success":
    subtype = "error" if d.get("is_error") else "empty result"
num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
cost, turns = d.get("total_cost_usd"), d.get("num_turns")
print("\t".join([
    "ok" if ok else "fail", subtype,
    f"{cost:.2f}" if num(cost) else "-",
    str(turns) if num(turns) else "-",
    str(len(d.get("permission_denials") or [])),
]))
PY
)

  local exit_code=0
  case "$state" in
    ok)
      echo "checkup prompt-audit: ok report=$base.md cost_usd=$cost duration_s=$duration turns=$turns denials=$denials$extra"
      ;;
    fail)
      echo "checkup prompt-audit: failed ($subtype) raw=$base.json cost_usd=$cost denials=$denials$extra"
      exit_code=$(( rc ? rc : 1 ))
      ;;
    *)
      echo "checkup prompt-audit: failed (exit $rc) raw=$base.json$extra"
      exit_code=$(( rc ? rc : 1 ))
      ;;
  esac
  return $exit_code
}

# _harness_launcher_codex_restore_apply
#   Detects a Codex restore (one `resume <uuid>` and no launcher profile) and
#   fills the caller's codex_restore_args with the restored `-m`, `-c
#   model_reasoning_effort` and the recorded grant flags, and HARNESS_CODEX_CONTEXT
#   unless a context keyword chose one. Caller flags make the argv impure, so
#   they always win. Reads and updates the caller's locals.
_harness_launcher_codex_restore_apply() {
  $profile_explicit && return 0
  $use_happy && return 0
  local -a argv=()
  if $passthrough; then
    [[ -z "$subcmd" ]] && (( ${#codex_args} == 0 )) || return 0
    argv=("${codex_passthrough_args[@]}")
  else
    [[ "$subcmd" == resume ]] && (( ${#codex_args} == 1 )) || return 0
    argv=(resume "${codex_args[1]}")
  fi
  local restore_id restore_model restore_effort restore_context
  local restore_permission restore_approval restore_sandbox restore_network restore_bypass restore_isolated restore_profile
  local hint=false
  restore_id="$(_harness_launcher_restore_resume_id codex "${argv[@]}")" || return 0
  _harness_launcher_restore_probe codex "$restore_id" || true
  _harness_launcher_restore_launch_record codex "$restore_id" "${HARNESS_SOURCE_ROOT:-${HARNESS_DIR:A}}" || hint=true
  # The launched profile carries grants of its own (plan is read-only), so it is
  # reapplied, but only while the generated registry still has it; otherwise
  # keep base and say so.
  if [[ -n "$restore_profile" ]]; then
    if [[ -f "$HARNESS_DIR/.harness/codex/$restore_profile.config.toml" && ! -L "$HARNESS_DIR/.harness/codex/$restore_profile.config.toml" ]]; then
      profile="$restore_profile"
    else
      hint=true
    fi
  fi
  ! $hint || _harness_launcher_restore_hint codex "$restore_id" "$restore_model"
  [[ -z "$restore_model" ]] || codex_restore_args+=(-m "$restore_model")
  [[ -z "$restore_effort" ]] || codex_restore_args+=(-c "model_reasoning_effort=\"$restore_effort\"")
  [[ -z "$restore_context" ]] || $context_explicit || HARNESS_CODEX_CONTEXT="$restore_context"
  if [[ -n "$restore_bypass" ]]; then
    codex_restore_args+=(--dangerously-bypass-approvals-and-sandbox)
  else
    [[ -z "$restore_approval" ]] || codex_restore_args+=(-a "$restore_approval")
    [[ -z "$restore_sandbox" ]] || codex_restore_args+=(-s "$restore_sandbox")
    if [[ "$restore_sandbox" == workspace-write && -n "$restore_network" ]]; then
      local network_bool=false
      [[ "$restore_network" != enabled ]] || network_bool=true
      codex_restore_args+=(-c "sandbox_workspace_write.network_access=$network_bool")
    fi
  fi
  return 0
}

# _harness_launcher_codex_launch_grant [codex args...]
#   Sets the caller's launch_approval, launch_sandbox and launch_bypass to the
#   grant the final Codex flags carry (later flags win; bypass replaces both).
#   Only launcher-visible flags and the exact workspace network boolean count;
#   profile defaults and other config overrides never grant authority.
_harness_launcher_codex_launch_grant() {
  local -a argv=("$@")
  local i arg py
  local -a network_overrides=()
  launch_approval="" launch_sandbox="" launch_network="" launch_bypass=""
  for (( i = 1; i <= ${#argv}; i++ )); do
    arg="${argv[i]}"
    case "$arg" in
      --) break ;;
      --dangerously-bypass-approvals-and-sandbox) launch_bypass=1 ;;
      --full-auto) launch_approval=on-request; launch_sandbox=workspace-write ;;
      -a|--ask-for-approval) launch_approval="${argv[i+1]-}"; (( i++ )) ;;
      --ask-for-approval=*) launch_approval="${arg#--ask-for-approval=}" ;;
      -a?*) launch_approval="${arg#-a}" ;;
      -s|--sandbox) launch_sandbox="${argv[i+1]-}"; (( i++ )) ;;
      --sandbox=*) launch_sandbox="${arg#--sandbox=}" ;;
      -s?*) launch_sandbox="${arg#-s}" ;;
      -c|--config) network_overrides+=("${argv[i+1]-}"); (( i++ )) ;;
      --config=*) network_overrides+=("${arg#--config=}") ;;
      -c?*) network_overrides+=("${arg#-c}") ;;
      -*) if harness_codex_option_takes_value "$arg"; then (( i++ )); fi ;;
    esac
  done
  [[ "$launch_approval" == (untrusted|on-failure|on-request|never) ]] || launch_approval=""
  [[ "$launch_sandbox" == (read-only|workspace-write|danger-full-access) ]] || launch_sandbox=""
  if [[ -n "$launch_bypass" ]]; then
    launch_approval="" launch_sandbox=""
    launch_network=enabled
  elif [[ "$launch_sandbox" == danger-full-access ]]; then
    launch_network=enabled
  elif [[ "$launch_sandbox" == workspace-write && ${#network_overrides} -gt 0 ]]; then
    py="$(harness_python3_resolve 2>/dev/null)" || return 0
    launch_network="$("$py" "$_HARNESS_LAUNCHER_BIN/harness_launch_plan.py" network-grant "${network_overrides[@]}" 2>/dev/null)" || launch_network=""
  fi
  return 0
}

# _harness_launcher_run_codex_cli <harness-dir> <resolved-policy> [args...]
#   Launches Codex CLI natively against a per-harness CODEX_HOME.
#   Modes:    fast | base | sol | plan | rich | astra → -p <profile>
#   Surface:  work → work MCP surface (combinable with any profile)
#   Apps:     --app <asdk_app_id> → add one app for this launch only
#   Wrapper:  happy → `happy codex ...`
#   Sessions: resume → `codex resume`,  continue → `codex resume --last`,
#             fork   → `codex fork`
#   SDK:      --passthrough → every later token is a native Codex argument;
#             a caller -p/--profile or -C/--cd replaces the launcher's.
#   Codex rejects -p for non-runtime subcommands, so the launcher omits it
#   there; plain `app-server` runs behind codex-app-server-guard.py.
_harness_launcher_run_codex_cli() {
  local HARNESS_DIR="$1" mcp_surface_policy="$2"; shift 2
  local run_dir="${HARNESS_RUN_DIR:-$HARNESS_DIR}"
  export HARNESS_PREFIX
  local HARNESS_OBSERVABILITY_ACTIVE HARNESS_OBSERVABILITY_ENABLED HARNESS_OBSERVABILITY_PROFILE HARNESS_OTLP_HTTP_ENDPOINT
  local OTEL_RESOURCE_ATTRIBUTES obs_rc
  # Per-harness GitHub identity: fail-open, never overrides with an empty token.
  local GH_TOKEN HARNESS_GH_USER
  harness_gh_token_load "$HARNESS_DIR" || true
  if harness_observability_load "$HARNESS_DIR"; then
    OTEL_RESOURCE_ATTRIBUTES="service.name=codex_exec,obs.runtime=codex,obs.profile=$HARNESS_OBSERVABILITY_PROFILE"
    export OTEL_RESOURCE_ATTRIBUTES
  else
    obs_rc=$?
    [[ "$obs_rc" -eq 1 ]] || return "$obs_rc"
  fi
  local profile=""
  local profile_explicit=false
  local mcp_profile=""
  local HARNESS_CODEX_APPS_ALLOWLIST="${HARNESS_CODEX_APPS_ALLOWLIST:-}"
  local HARNESS_CODEX_CONTEXT="272k"
  local subcmd=""
  local use_happy=false
  local freeform=false
  local passthrough=false context_explicit=false
  local -a codex_args=() codex_passthrough_args=() codex_restore_args=()
  local launch_approval="" launch_sandbox="" launch_network="" launch_bypass="" launch_profile=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --passthrough)
        shift; passthrough=true; codex_passthrough_args=("$@"); break ;;
      fast|base|sol|plan|rich)
        profile="$1"; profile_explicit=true; shift ;;
      astra)
        if $freeform; then
          codex_args+=("$1")
        else
          profile="$1"; profile_explicit=true
        fi
        shift ;;
      work)
        # Surface keyword, combinable with any model profile and any launcher
        # keyword in any order (same UX as the claude `light` keyword). Only a
        # genuinely free-form token before it (e.g. `exec work`) demotes it to
        # prompt text — launcher keywords like full-auto/continue must not.
        if $freeform; then
          codex_args+=("$1"); shift
        else
          case "$mcp_surface_policy" in
            legacy) mcp_profile="work" ;;
            single-full-compat)
              echo "⚠️  work MCP surface is deprecated for this project; using full" >&2
              ;;
            single-full)
              echo "harness-launcher: work MCP surface is retired for this project" >&2
              return 2
              ;;
          esac
          shift
        fi
        ;;
      --app)
        if $freeform; then
          codex_args+=("$1")
          shift
          [[ $# -gt 0 ]] && { codex_args+=("$1"); shift; }
        else
          [[ $# -ge 2 ]] || {
            echo "harness-launcher: --app requires an asdk_app_* id" >&2
            return 2
          }
          if [[ -n "$HARNESS_CODEX_APPS_ALLOWLIST" ]]; then
            HARNESS_CODEX_APPS_ALLOWLIST="$HARNESS_CODEX_APPS_ALLOWLIST,$2"
          else
            HARNESS_CODEX_APPS_ALLOWLIST="$2"
          fi
          shift 2
        fi
        ;;
      272k|1m)
        if $freeform; then
          codex_args+=("$1")
        else
          HARNESS_CODEX_CONTEXT="$1"; context_explicit=true
        fi
        shift ;;
      resume)              subcmd="resume"; shift ;;
      continue)            subcmd="resume"; codex_args+=(--last); shift ;;
      fork)                subcmd="fork"; codex_args+=(--last); shift ;;
      happy)               use_happy=true; shift ;;
      full-auto)           codex_args+=(--full-auto); shift ;;
      never)               codex_args+=(-a never); shift ;;
      bypass)              codex_args+=(--dangerously-bypass-approvals-and-sandbox); shift ;;
      *)                   freeform=true; codex_args+=("$1"); shift ;;
    esac
  done

  HARNESS_CODEX_APPS_ALLOWLIST="$(harness_codex_apps_allowlist_normalize "$HARNESS_CODEX_APPS_ALLOWLIST")" || return $?
  export HARNESS_CODEX_APPS_ALLOWLIST HARNESS_CODEX_SLACK_APPS

  [[ -z "$profile" ]] && profile="base"
  # A pure `resume <id>` restores the session's model, effort, context and
  # recorded grant instead of the launcher's profile defaults.
  _harness_launcher_codex_restore_apply
  _harness_launcher_codex_launch_grant "${codex_restore_args[@]}" "${codex_args[@]}" "${codex_passthrough_args[@]}"
  export HARNESS_CODEX_CONTEXT

  # Caller wins after the marker. A caller -C/--cd must stay inside the
  # harness; a relative one resolves against the directory Codex starts in.
  local caller_profile=false caller_cd_set=false caller_cd="" caller_run_dir=""
  if $passthrough; then
    local i arg
    for (( i = 1; i <= ${#codex_passthrough_args}; i++ )); do
      arg="${codex_passthrough_args[i]}"
      case "$arg" in
        --) break ;;
        -p|--profile) caller_profile=true; (( i++ )) ;;
        -p?*|--profile=*) caller_profile=true ;;
        -C|--cd) caller_cd_set=true; caller_cd="${codex_passthrough_args[i+1]-}"; (( i++ )) ;;
        -C=*|--cd=*) caller_cd_set=true; caller_cd="${arg#*=}" ;;
        -C?*) caller_cd_set=true; caller_cd="${arg#-C}" ;;
        -*) if harness_codex_option_takes_value "$arg"; then (( i++ )); fi ;;
      esac
    done
  fi
  if $caller_cd_set; then
    caller_run_dir="$(cd "$run_dir" 2>/dev/null && harness_resolve_run_dir "$HARNESS_DIR" "$caller_cd" 2>/dev/null)" || {
      echo "harness-launcher: codex -C must name a directory inside the registered harness: $caller_cd" >&2
      return 2
    }
  fi

  local native_subcmd="" profile_arg=true guard_app_server=false
  if [[ -z "$subcmd" ]] && ! $use_happy; then
    native_subcmd="$(harness_codex_subcommand "${codex_args[@]}" "${codex_passthrough_args[@]}")"
    if harness_codex_subcommand_rejects_profile "$native_subcmd"; then
      if $profile_explicit; then
        echo "harness-launcher: codex profile '$profile' applies only to runtime commands; '${native_subcmd% }' rejects --profile" >&2
        return 2
      fi
      profile_arg=false
    fi
    case "$native_subcmd" in
      remote-control|exec-server|mcp-server)
        echo "harness-launcher: codex $native_subcmd serves clients outside the harness boundary guard; run 'command codex $native_subcmd' deliberately" >&2
        return 2
        ;;
    esac
    if [[ "$native_subcmd" == app-server ]]; then
      local app_server_route
      app_server_route="$(_harness_launcher_codex_app_server_route "${codex_args[@]}" "${codex_passthrough_args[@]}")"
      case "$app_server_route" in
        guard) guard_app_server=true ;;
        tool) ;;
        *)
          echo "harness-launcher: codex app-server '$app_server_route' is not supported; only the stdio app-server runs behind the harness boundary guard" >&2
          return 2
          ;;
      esac
    fi
  fi
  $caller_profile && profile_arg=false
  # The launcher-owned profile (not a caller -p) is part of what was launched.
  if [[ "$profile" == (fast|base|sol|astra|plan|rich) ]] && ! $use_happy && ! $caller_profile; then
    launch_profile="$profile"
  fi
  local guard_python=""
  if $guard_app_server; then
    guard_python="$(harness_python3_resolve)" || return 1
  fi

  # Validate incompatible combinations BEFORE preparing the runtime home, so a
  # rejected launch leaves no work-surface residue in the generated config.
  if $use_happy; then
    harness_slack_happy_guard || return $?
    command -v happy >/dev/null 2>&1 || {
      echo "❌ happy not found in PATH" >&2
      return 1
    }
    if [[ -n "$subcmd" ]]; then
      echo "❌ Happy Codex cannot map Codex CLI resume/continue/fork. Use 'happy resume <happy-session-id>' or 'happy codex --resume <codex-thread-id>'." >&2
      return 1
    fi
    if $profile_explicit; then
      echo "❌ Happy Codex does not support launcher profiles. Use '${HARNESS_PREFIX:-harness} codex happy' for Happy mode, or '${HARNESS_PREFIX:-harness} codex $profile' for native Codex CLI." >&2
      return 1
    fi
    if [[ -n "$mcp_profile" ]]; then
      echo "❌ Happy Codex는 work MCP surface와 함께 사용할 수 없습니다." >&2
      return 1
    fi
  else
    local codex_bin
    codex_bin="$(_harness_launcher_codex_bin)" || {
      echo "❌ codex not found in PATH" >&2
      return 1
    }
  fi

  harness_mcp_surface_policy_is_single_full "$mcp_surface_policy" && unset HARNESS_CODEX_MCP_PROFILE
  if [[ -n "$mcp_profile" ]]; then
    local HARNESS_CODEX_MCP_PROFILE="$mcp_profile"
    export HARNESS_CODEX_MCP_PROFILE
    _harness_launcher_export_codex_runtime_env "$HARNESS_DIR" || return $?
  else
    _harness_launcher_export_codex_runtime_env "$HARNESS_DIR" || return $?
  fi
  _harness_launcher_codex_history_restore "${HARNESS_SOURCE_ROOT:-${HARNESS_DIR:A}}" "$CODEX_HOME" || {
    echo 'harness-launcher: durable Codex history restore was refused; no model was launched' >&2
    return 2
  }

  local -a HARNESS_SLACK_ARGV=()
  harness_slack_codex_argv "$_HARNESS_SLACK_POLICY" "${codex_restore_args[@]}" "${codex_args[@]}" "${codex_passthrough_args[@]}" || return $?
  codex_restore_args=() codex_passthrough_args=()
  codex_args=("${HARNESS_SLACK_ARGV[@]}")
  _harness_launcher_codex_launch_grant "${codex_args[@]}"

  # Plain invocation (not exec) so the user's interactive shell survives
  # codex exit — Ctrl+C returns to the prompt instead of closing the terminal.
  local -a launch_cmd=() codex_head=()
  if $use_happy; then
    launch_cmd=(happy codex)
  else
    launch_cmd=("$codex_bin")
  fi
  $caller_cd_set || codex_head+=(--cd "$run_dir")
  $profile_arg && codex_head+=(-p "$profile")
  codex_head+=("${codex_restore_args[@]}")
  if $guard_app_server; then
    local registry="${HARNESS_PROFILE_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/harness-launcher}/profiles"
    launch_cmd=("$guard_python" "$_HARNESS_LAUNCHER_BIN/codex-app-server-guard.py"
      --root "$HARNESS_DIR" --server-cwd "${caller_run_dir:-$run_dir}" --prefix "$HARNESS_PREFIX"
      --registry "$registry" -- "${launch_cmd[@]}")
  fi
  # The directory Codex starts in: a caller -C wins over the run directory.
  _harness_launcher_announce_run_dir "${caller_run_dir:-$run_dir}"
  if [[ -n "$subcmd" ]]; then
    harness_codex_cmux_broker_start "$_HARNESS_LAUNCHER_BIN/codex-cmux-title-sync.py"
    (cd "$run_dir" && _harness_launcher_export_launch_env "$launch_approval" "$launch_sandbox" "$launch_bypass" "$launch_profile" "$launch_network" \
      && "${launch_cmd[@]}" "$subcmd" "${codex_head[@]}" "${codex_args[@]}" "${codex_passthrough_args[@]}")
  elif $use_happy; then
    harness_codex_cmux_broker_start "$_HARNESS_LAUNCHER_BIN/codex-cmux-title-sync.py"
    (cd "$run_dir" && _harness_launcher_export_launch_env "$launch_approval" "$launch_sandbox" "$launch_bypass" "$launch_profile" "$launch_network" \
      && "${launch_cmd[@]}" "${codex_args[@]}" "${codex_passthrough_args[@]}")
  else
    harness_codex_cmux_broker_start "$_HARNESS_LAUNCHER_BIN/codex-cmux-title-sync.py"
    (cd "$run_dir" && _harness_launcher_export_launch_env "$launch_approval" "$launch_sandbox" "$launch_bypass" "$launch_profile" "$launch_network" \
      && "${launch_cmd[@]}" "${codex_head[@]}" "${codex_args[@]}" "${codex_passthrough_args[@]}")
  fi
  local rc=$?
  harness_codex_cmux_broker_stop
  return $rc
}

# _harness_launcher_codex_app_server_route [codex args...]
#   Classifies the argv around `app-server`: `guard` for the stdio server,
#   `tool` for schema/binding generators and help, otherwise the shape that
#   would bypass the relay (a nested daemon/proxy or a non-stdio listener).
_harness_launcher_codex_app_server_route() {
  while (( $# )); do
    if [[ "$1" == app-server ]]; then
      shift
      break
    fi
    if [[ "$1" == -* ]] && harness_codex_option_takes_value "$1" && (( $# > 1 )); then
      shift
    fi
    shift
  done
  local arg
  while (( $# )); do
    arg="$1"; shift
    case "$arg" in
      --) break ;;
      --listen)
        [[ "${1-}" == stdio:// ]] || { print -r -- "--listen ${1-}"; return 0; }
        shift ;;
      --listen=*)
        [[ "${arg#--listen=}" == stdio:// ]] || { print -r -- "$arg"; return 0; } ;;
      -c|--config|--enable|--disable|--code-mode-host|--ws-auth|--ws-token-file|--ws-token-sha256|--ws-shared-secret-file|--ws-issuer|--ws-audience|--ws-max-clock-skew-seconds)
        if (( $# )); then shift; fi ;;
      -h|--help|generate-ts|generate-json-schema|help) print -r -- tool; return 0 ;;
      -*) ;;
      *) print -r -- "$arg"; return 0 ;;
    esac
  done
  print -r -- guard
}

# _harness_launcher_run_kiro_cli <harness-dir> [args...]
#   Launches Kiro CLI natively against a per-harness KIRO_HOME.
#   Modes:    fast | base | plan | rich → --model + --effort
#   Granular: model=<id> and/or effort=<low|medium|high|xhigh|max> override the
#             mode pair, so a one-off combination needs no new preset. A bare
#             model ID (claude-*, gpt-*, auto) is accepted as model=<id>.
#   Sessions: resume → --resume-picker, continue → -r
_harness_launcher_run_kiro_cli() {
  local HARNESS_DIR="$1"; shift
  local run_dir="${HARNESS_RUN_DIR:-$HARNESS_DIR}"
  local model="" effort="" agent="harness" mcp_surface="full"
  local model_set=0 effort_set=0
  local -a kiro_args=()
  local session_flag=""

  # Explicit model=/effort= win over a preset regardless of argument order:
  # `effort=medium plan` and `plan effort=medium` must behave identically.
  while [[ $# -gt 0 ]]; do
    case "$1" in
      fast|base|plan|rich)
        harness_kiro_mode_resolve "$1"
        [[ $model_set -eq 1 ]] || model="$HARNESS_KIRO_MODEL"
        [[ $effort_set -eq 1 ]] || effort="$HARNESS_KIRO_EFFORT"
        shift ;;
      model=*)  model="${1#model=}"; model_set=1; shift ;;
      effort=*) effort="${1#effort=}"; effort_set=1; shift ;;
      claude-*|gpt-*|glm-*|qwen*|minimax-*|deepseek-*|auto)
        model="$1"; model_set=1; shift ;;
      resume)   session_flag="--resume-picker"; shift ;;
      continue) session_flag="-r"; shift ;;
      bypass)   kiro_args+=(-a); shift ;;
      light)    mcp_surface="light"; shift ;;
      *)        kiro_args+=("$1"); shift ;;
    esac
  done

  if [[ -z "$model" ]]; then
    harness_kiro_mode_resolve base
    model="$HARNESS_KIRO_MODEL"
    if [[ -z "$effort" ]]; then
      effort="$HARNESS_KIRO_EFFORT"
    fi
  fi
  if [[ -z "$effort" ]]; then
    effort="$(harness_kiro_effort_recommended "$model")"
  fi
  # The Kiro CLI validates --model (it errors with the live catalog) but NOT
  # --effort: an unknown level is silently swallowed, so guard it here.
  if ! harness_kiro_effort_is_valid "$effort"; then
    echo "❌ 잘못된 effort: '$effort' (가능한 값: $HARNESS_KIRO_EFFORT_LEVELS)" >&2
    return 1
  fi

  # Resolve the binary before exporting the surface profile: an early failure
  # return here must not leave HARNESS_KIRO_MCP_PROFILE in the user's shell.
  local kiro_bin
  kiro_bin="$(_harness_launcher_kiro_bin)" || {
    echo "❌ kiro-cli not found in PATH" >&2
    return 1
  }

  if [[ "$mcp_surface" == "light" ]]; then
    export HARNESS_KIRO_MCP_PROFILE="light"
  else
    unset HARNESS_KIRO_MCP_PROFILE
  fi
  _harness_launcher_export_kiro_runtime_env "$HARNESS_DIR" || {
    local rc=$?
    unset HARNESS_KIRO_MCP_PROFILE
    return $rc
  }

  local -a launch_cmd=("$kiro_bin" chat)
  [[ -n "$session_flag" ]] && launch_cmd+=("$session_flag")
  launch_cmd+=(--model "$model" --effort "$effort" --agent "$agent")
  [[ ${#kiro_args[@]} -gt 0 ]] && launch_cmd+=("${kiro_args[@]}")
  unset HARNESS_KIRO_MODEL HARNESS_KIRO_EFFORT

  _harness_launcher_announce_run_dir "$run_dir"
  (cd "$run_dir" && "${launch_cmd[@]}")
  local rc=$?
  unset HARNESS_KIRO_MCP_PROFILE
  return $rc
}

# _harness_launcher_complete <harness-dir>
_harness_launcher_complete() {
  local dir="$1"
  local -a shortcuts
  local m desc codex_surface_desc="" kiro_surface_desc=""
  local HARNESS_NAME HARNESS_PREFIX HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST
  local HARNESS_MCP_SURFACE_POLICY="" mcp_surface_policy
  if [[ -f "$dir/config/launcher.env" ]]; then
    source "$dir/config/launcher.env"
  fi
  mcp_surface_policy="$(harness_mcp_surface_policy_resolve "$HARNESS_MCP_SURFACE_POLICY")" || return $?
  # Mode descriptions come from the shared table so completion text cannot
  # drift from the launched model/effort. Resolution runs in subshells so the
  # HARNESS_MODE_* globals never touch the interactive shell.
  shortcuts=()
  for m in fast base plan opus rich fable; do
    desc="$( harness_mode_resolve "$m" direct; printf '%s · %s' "$HARNESS_MODE_MODEL" "$HARNESS_MODE_EFFORT" )"
    shortcuts+=("$m:$desc")
  done
  desc="$( harness_mode_resolve ultracode direct; printf '%s · %s' "$HARNESS_MODE_MODEL" "$HARNESS_MODE_EFFORT" )"
  if ! harness_mcp_surface_policy_is_single_full "$mcp_surface_policy"; then
    shortcuts+=('light:Light MCP surface — SSH-backed servers excluded (claude/kiro-cli)')
    codex_surface_desc=' · work surface'
  else
    kiro_surface_desc=' (optional light surface)'
  fi
  shortcuts+=(
    "ultracode:$desc now · /effort→ultracode for workflows (direct only)"
    'continue:Continue last session'
    'resume:Resume from list'
    'bypass:Skip all permission prompts'
    'acceptEdits:Auto-approve edits only'
    'dontAsk:Auto-approve most actions'
    '--chrome:Enable Claude in Chrome integration'
    '--no-chrome:Disable Claude in Chrome integration'
    'claude:Claude Code native (explicit selector; existing presets supported)'
    "codex:Codex CLI native (272K default · 1m opt-in · profiles${codex_surface_desc}/fork/safety)"
    'codex-smoke:Send one bounded metadata-only Codex verification event'
    'checkup:Headless read-only /checkup prompt-audit at the harness root'
    "kiro-cli:Kiro CLI native${kiro_surface_desc}"
    'happy:Use Happy mobile wrapper for Codex CLI'
  )
  # Gateway env files are sourced in subshells so API keys never leak into
  # the interactive shell's variables.
  local _kiro_url _codex_url
  _kiro_url="${KIRO_GATEWAY_URL:-}"
  [[ -z "$_kiro_url" && -f "$dir/config/.local/kiro-gateway.env" ]] && \
    _kiro_url="$( source "$dir/config/.local/kiro-gateway.env" 2>/dev/null; printf '%s' "${KIRO_GATEWAY_URL:-}" )"
  _codex_url="${CODEX_GATEWAY_URL:-}"
  [[ -z "$_codex_url" && -f "$dir/config/.local/codex-gateway.env" ]] && \
    _codex_url="$( source "$dir/config/.local/codex-gateway.env" 2>/dev/null; printf '%s' "${CODEX_GATEWAY_URL:-}" )"
  [[ -n "$_kiro_url" ]]  && shortcuts+=('kiro:Kiro via gateway')
  [[ -n "$_codex_url" ]] && shortcuts+=('codex-gateway:Claude Code via Codex gateway (legacy)')
  _describe 'harness shortcuts' shortcuts
}
