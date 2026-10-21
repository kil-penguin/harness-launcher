#!/usr/bin/env bash
# Durable, opt-in isolated harness session repositories.
set -euo pipefail
# Inherited git variables would change what every pathspec, index and git
# dir below means (GIT_LITERAL_PATHSPECS turns the icase fences into literals).
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES \
  GIT_COMMON_DIR GIT_NAMESPACE GIT_LITERAL_PATHSPECS GIT_GLOB_PATHSPECS GIT_NOGLOB_PATHSPECS GIT_ICASE_PATHSPECS

state_home() { printf '%s\n' "${HARNESS_SESSION_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/harness-launcher}"; }
session_dir() { printf '%s/sessions/%s\n' "$(state_home)" "$1"; }
integration_queue_enabled() { [[ -e "$(state_home)/integration-queue.json" || -L "$(state_home)/integration-queue.json" ]]; }
# Homebrew exposes this script through a bin symlink; helpers live beside its target.
integration_queue() {
  local script="${BASH_SOURCE[0]}" directory hop
  for hop in {1..40}; do
    directory="$(cd -P -- "$(dirname -- "$script")" && pwd -P)" || return 2
    script="$directory/$(basename -- "$script")"
    [[ -L "$script" ]] || break
    script="$(readlink "$script")" || return 2
    [[ "$script" == /* ]] || script="$directory/$script"
  done
  [[ -f "$script" && ! -L "$script" && -f "$directory/harness_integration_queue.py" ]] || return 2
  # A guardian must replace its background shell: that shell waiting for its
  # child would otherwise keep the execution group alive forever after a crash.
  if [[ "${1:-}" == guardian ]]; then
    exec python3 -I -B "$directory/harness_integration_queue.py" --state "$(state_home)" --engine "$script" "$@"
  fi
  python3 -I -B "$directory/harness_integration_queue.py" --state "$(state_home)" --engine "$script" "$@"
}
integration_headless() { [[ -e "${INTEGRATION_INPUT_DIR:-$(session_dir "$1")}/headless" ]]; }
integration_phase() {
  [[ -n "${INTEGRATION_INPUT_DIR:-}" ]] || return 0
  integration_queue phase "$1" --home "${HARNESS_INTEGRATION_HOME:?}"
}
verifier_exec() {
  if [[ -n "${INTEGRATION_INPUT_DIR:-}" ]]; then
    integration_queue verifier-exec --home "${HARNESS_INTEGRATION_HOME:?}" -- "$@"
  else "$@"; fi
}
new_id() { uuidgen 2>/dev/null || { date +%s%N; } | shasum -a 256 | cut -c1-32; }
write_journal() {
  local dir="$1" state="$2" identity="$3" tmp
  tmp="$(mktemp "$dir/.journal.XXXXXX")"
  { printf 'state=%s\nidentity=%s\nheartbeat=%s\n' "$state" "$identity" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"; } > "$tmp"
  mv -f "$tmp" "$dir/journal"
}
write_heartbeat() {
  local dir="$1" tmp
  tmp="$(mktemp "$dir/.heartbeat.XXXXXX")"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$tmp"
  mv -f "$tmp" "$dir/heartbeat"
}
# agent_usage_sync <session-id> <source-root>: refresh this session's company
# usage-collection manifest (created, retired). Fail-open and silent on stdout:
# create's stdout is a key=value contract.
agent_usage_sync() {
  local bin
  bin="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  [[ -f "$bin/harness-common.sh" ]] || return 0
  ( source "$bin/harness-common.sh"; harness_agent_usage_sync_session "$bin" "$1" "$2" ) >/dev/null || true
}
field() { sed -n "s/^$2=//p" "$1" | head -n 1; }
# Broker git on a session root: nothing named by the session's own config may
# run (fsmonitor, hooks, external diff, submodule recursion).
session_git() {
  git -c core.fsmonitor=false -c core.hooksPath=/dev/null -c submodule.recurse=false \
    -c diff.external= -c core.untrackedCache=false "$@"
}
EXCLUDED_PATHS=(config/.local projects .mcp.local.json mcp.local.json .claude/settings.local.json)
# Case-insensitive: the agent can negate the ignore rules in .gitignore, and
# a case variant (Projects/, config/.LOCAL/) must not slip past the fence.
EXCLUDED_PATHSPEC=() EXCLUDED_ICASE=()
for _excluded in "${EXCLUDED_PATHS[@]}"; do
  EXCLUDED_PATHSPEC+=(":(exclude,icase)$_excluded"); EXCLUDED_ICASE+=(":(icase)$_excluded")
done
unset _excluded
# stage_worktree <root> <base>: stage the session work tree. `add` never names
# the excluded paths: an exclude pathspec that names a gitignored file (the
# copied or linked machine-local files are usually ignored) makes `add -A`
# exit 1. Excluded paths are then reset; headless sessions reset them to the
# base so a tracked file there keeps its base content (their trusted.git also
# ignores them through info/exclude), interactive ones to HEAD so submit can
# still refuse excluded paths the session committed.
stage_worktree() {
  local list path
  local -a aliases=()
  root_git "$1" add -A -- . || return 1
  if [[ -n "$ROOT_GIT_DIR" ]]; then
    root_git "$1" reset -q "$2" -- "${EXCLUDED_ICASE[@]}" || return 1
  else
    session_git -C "$1" reset -q -- "${EXCLUDED_ICASE[@]}" || return 1
  fi
  # Names git's ASCII-only icase misses but the filesystem folds onto an
  # excluded entry (projectſ is projects on APFS) are reset the same way.
  list="$(mktemp "$(state_home)/aliases.XXXXXX")" || return 1
  excluded_aliases "$1" "$2" > "$list" || { rm -f "$list"; return 1; }
  while IFS= read -r -d '' path; do aliases+=(":(literal)$path"); done < "$list"
  rm -f "$list"
  (( ${#aliases[@]} )) || return 0
  if [[ -n "$ROOT_GIT_DIR" ]]; then
    root_git "$1" reset -q "$2" -- "${aliases[@]}"
  else
    session_git -C "$1" reset -q -- "${aliases[@]}"
  fi
}
# excluded_aliases <root> <base>: NUL-separated staged paths (changed from
# base) that a filesystem resolves to an excluded entry: case, Unicode case
# folding and normalization are decided by probe trees of the excluded
# entries, one on the session volume (state home) and one on the volume of
# the source checkout the work lands in (its git dir), so every layer agrees
# with what a checkout on either would actually write.
excluded_aliases() {
  local root="$1" base="$2" staged probe path first second hit
  local -a probes=()
  [[ -n "$SOURCE_GIT_DIR" ]] || { echo 'harness-session: no source git dir to probe' >&2; return 1; }
  staged="$(mktemp "$(state_home)/staged.XXXXXX")" || return 1
  # Callers test the status, so errexit is off here: every step is checked,
  # and the staged list is a file, not a process substitution whose failure
  # would read as "no paths".
  for probe in "$(state_home)" "$SOURCE_GIT_DIR"; do
    probe="$(mktemp -d "$probe/harness-probe.XXXXXX")" || { rm -rf "$staged" "${probes[@]}"; return 1; }
    probes+=("$probe")
    { mkdir -p "$probe/projects" "$probe/config/.local" "$probe/.claude" \
        && : > "$probe/mcp.local.json" && : > "$probe/.mcp.local.json" && : > "$probe/.claude/settings.local.json"
    } || { rm -rf "$staged" "${probes[@]}"; return 1; }
  done
  root_git "$root" diff --no-ext-diff --ignore-submodules=all --cached --name-only --no-renames -z "$base" > "$staged" \
    || { rm -rf "$staged" "${probes[@]}"; return 1; }
  while IFS= read -r -d '' path; do
    first="${path%%/*}"; second=""
    if [[ "$path" == */* ]]; then second="${path#*/}"; second="$first/${second%%/*}"; fi
    hit=0
    for probe in "${probes[@]}"; do
      if [[ -e "$probe/$first" ]] && { [[ "$probe/$first" -ef "$probe/projects" ]] \
          || [[ "$probe/$first" -ef "$probe/mcp.local.json" ]] || [[ "$probe/$first" -ef "$probe/.mcp.local.json" ]]; }; then
        hit=1; break
      fi
      if [[ -n "$second" && -e "$probe/$second" ]] && { [[ "$probe/$second" -ef "$probe/config/.local" ]] \
          || [[ "$probe/$second" -ef "$probe/.claude/settings.local.json" ]]; }; then
        hit=1; break
      fi
    done
    [[ "$hit" == 0 ]] || printf '%s\0' "$path"
  done < "$staged"
  rm -rf "$staged" "${probes[@]}"
}
# Headless sessions (record marker `headless`, written at clone time) never
# let broker git read the session's own .git: it runs on a launcher-owned git
# dir in the record (`trusted.git`, base commit plus its own index) with the
# session root as work tree and no system or global config. Anything the agent
# put in the session .git (config, hooks, modules, alternates) is never read,
# and work-tree attributes name filter or diff drivers that are not defined.
# ROOT_GIT_DIR is set by use_session_git; empty means an interactive session.
ROOT_GIT_DIR="" SOURCE_GIT_DIR=""
# root_identity <path>: device and inode of the path itself (never followed).
root_identity() { stat -f '%d %i' "$1" 2>/dev/null || stat -c '%d %i' "$1"; }
# root_identity_matches <recorded> <root>: the record "<dev> <ino>" still names
# <root>. The inode must match. The device must match too, except that a
# whole-volume device renumbering (reboot) is accepted when the root sits on
# the device of its worktrees directory; a root replaced by a mount of another
# filesystem has a different device from its parent and is refused.
root_identity_matches() {
  local recorded="$1" root="$2" current parent rdev rino cdev cino extra
  [[ "$recorded" != *$'\n'* ]] || return 1
  while [[ "$root" == */?* && "$root" == */ ]]; do root="${root%/}"; done
  current="$(root_identity "$root")" || return 1
  read -r rdev rino extra <<<"$recorded"
  read -r cdev cino <<<"$current"
  [[ -z "$extra" && "$rdev" =~ ^[0-9]+$ && "$rino" =~ ^[0-9]+$ && "$rino" == "$cino" ]] || return 1
  [[ "$rdev" == "$cdev" ]] && return 0
  parent="$(root_identity "${root%/*}")" || return 1
  [[ "${parent%% *}" == "$cdev" ]]
}
# root_intact <dir> <root>: the session root is still the directory create
# made: a real directory (not a symlink) with the inode recorded at create;
# a record from an older launcher without it falls back to the physical
# path. Anything else (the agent removed the root and put a symlink or
# another directory there) must never be staged: git would follow it.
root_intact() {
  local dir="$1" root="$2"
  [[ -d "$root" && ! -L "$root" ]] || return 1
  if [[ -f "$dir/root-inode" && ! -L "$dir/root-inode" ]]; then
    root_identity_matches "$(<"$dir/root-inode")" "$root"
  else
    [[ "$(cd "${root%/*}" && pwd -P)/${root##*/}" == "$(cd "$root" && pwd -P)" ]]
  fi
}
use_session_git() {
  local dir; dir="$(session_dir "$1")"; ROOT_GIT_DIR=""
  # Exit 10: the session root was replaced; the record is kept, nothing staged.
  root_intact "$dir" "$(<"$dir/session-root")" || {
    echo "harness-session: refused: the session root of $1 was replaced (a symlink or another directory); nothing staged" >&2
    return 10
  }
  # The canonical checkout's git dir: excluded_aliases probes its volume.
  # Exit 8: the source checkout is gone (moved or deleted); the work is kept.
  local source=""
  { source="$(<"$dir/source-root")" && [[ -d "$source" ]] \
      && SOURCE_GIT_DIR="$(git -C "$source" rev-parse --path-format=absolute --git-common-dir)"; } || {
    echo "harness-session: refused: the source checkout ${source:-of session $1} could not be found; the session is kept" >&2
    return 8
  }
  [[ -e "$dir/headless" ]] || return 0
  if [[ ! -d "$dir/trusted.git" || -L "$dir/trusted.git" ]]; then
    echo "harness-session: refused: headless session $1 has no trusted git directory" >&2
    return 6
  fi
  ROOT_GIT_DIR="$dir/trusted.git"
}
# root_git <root> <git args...>: broker git on a session work tree.
root_git() {
  local root="$1"; shift
  if [[ -n "$ROOT_GIT_DIR" ]]; then
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
      session_git -C "$root" --git-dir="$ROOT_GIT_DIR" --work-tree="$root" "$@"
  else
    session_git -C "$root" "$@"
  fi
}
reopen_session() {
  local id="$1" dir
  dir="$(session_dir "$id")"
  chmod u+w "$dir/submission.patch" "$dir/manifest" "$dir/remote-url" 2>/dev/null || true
  # Stale discard evidence (a failed discard) must not outlive the reopen.
  rm -f "$dir/pending-sha" "$dir/push-ack" "$dir/.candidate-manifest" "$dir/discarded.patch" "$dir/discarded-at"
  transition "$id" OPEN
  write_heartbeat "$dir"
}
# headless_local_files <source> <root>
#   HARNESS_HEADLESS=1 (set only by harness-headless): no write path back to the
#   canonical root. projects/ is never a link, local files are copied (the
#   settings env block of MCP secrets is dropped), and a symlink on any path
#   component under <root> refuses the clone instead of being followed.
headless_local_files() {
  local source="$1" root="$2" local_file part path tmp py="${HARNESS_PYTHON_BIN:-}"
  [[ ! -L "$root/projects" ]] || rm -f "$root/projects" || return 2
  for local_file in .mcp.local.json mcp.local.json .claude/settings.local.json; do
    [[ -f "$source/$local_file" ]] || continue
    path="$root"
    for part in ${local_file//\// }; do
      path="$path/$part"
      [[ ! -L "$path" ]] || { echo "harness-session: headless clone refused: symlink at $local_file" >&2; return 2; }
    done
    [[ ! -e "$root/$local_file" ]] || continue
    mkdir -p "$(dirname "$root/$local_file")" || return 2
    tmp="$(mktemp "$(dirname "$root/$local_file")/.headless-local.XXXXXX")" || return 2
    if [[ "$local_file" == .claude/settings.local.json ]]; then
      # Absolute interpreter (resolved by the launcher) in isolated mode: no
      # cwd, PYTHON* or user-site module can replace the stdlib json.
      [[ "$py" == /* && -x "$py" ]] || { echo 'harness-session: headless clone refused: HARNESS_PYTHON_BIN must be an absolute interpreter' >&2; rm -f "$tmp"; return 2; }
      "$py" -I -c 'import json,sys; s=json.load(open(sys.argv[1])); s.pop("env",None); json.dump(s,open(sys.argv[2],"w"))' \
        "$source/$local_file" "$tmp" || { rm -f "$tmp"; return 2; }
    else
      cat "$source/$local_file" > "$tmp" || { rm -f "$tmp"; return 2; }
    fi
    # rename replaces, never follows, whatever is at the target.
    mv -f "$tmp" "$root/$local_file" || { rm -f "$tmp"; return 2; }
  done
}
create() {
  local source="$1" local_only="${2:-0}" reserved_id="${3:-}" root state id base sha dir local_file has_origin=0 helper
  source="$(cd "$source" && pwd -P)"
  git -C "$source" rev-parse --is-inside-work-tree >/dev/null
  state="$(state_home)"
  helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/harness_session_creation.py"
  if [[ -n "$reserved_id" ]]; then
    valid_id "$reserved_id" || { echo 'harness-session: invalid reserved UUID' >&2; return 2; }
    dir="$state/sessions/$reserved_id"; root="$state/worktrees/$reserved_id"
    [[ -n "${HARNESS_CREATION_LEASE_FD:-}" && -f "$helper" && ! -L "$helper" ]] || {
      echo 'harness-session: reserved create lacks its creation lease' >&2; return 2;
    }
    python3 -I "$helper" verify-reserved --record "$dir" --source "$source" --engine "$0" \
      --lease-fd "$HARNESS_CREATION_LEASE_FD" || return 2
    [[ -d "$dir" && ! -L "$dir" && -d "$state/worktrees" && ! -L "$state/worktrees" \
        && -d "$root" && ! -L "$root" && -z "$(find "$root" -mindepth 1 -maxdepth 1 -print -quit)" ]] || {
      echo 'harness-session: reserved paths are no longer owned' >&2; return 2;
    }
    for local_file in source-root session-root base-sha root-inode lease-v1 runtime.lock journal; do
      [[ ! -e "$dir/$local_file" && ! -L "$dir/$local_file" ]] || {
        echo 'harness-session: reserved record is no longer empty' >&2; return 2;
      }
    done
    id="$reserved_id"
  else
    mkdir -p "$state/sessions" "$state/worktrees"
  fi
  [[ -f "$helper" && ! -L "$helper" ]] || { echo 'harness-session: creation helper unavailable' >&2; return 2; }
  if [[ "$local_only" == 1 ]]; then
    sha="$(python3 -I "$helper" source-base --source "$source" --local)" || {
      echo 'harness-session: offline restore requires a cached origin/main' >&2; return 2;
    }
  else
    sha="$(python3 -I "$helper" source-base --source "$source")" || return 2
  fi
  if [[ -z "$reserved_id" ]]; then
    id="$(new_id)"; dir="$state/sessions/$id"; root="$state/worktrees/$id"
    mkdir -p "$dir"
  fi
  local -a clone_flags=(-q --no-checkout)
  [[ "${HARNESS_HEADLESS:-0}" != 1 ]] || clone_flags+=(--no-hardlinks)
  git clone "${clone_flags[@]}" "$source" "$root"
  git -C "$root" remote remove origin
  git -C "$root" checkout -q --detach "$sha"
  rm -rf "$root/config/.local"
  if [[ "${HARNESS_HEADLESS:-0}" == 1 ]]; then
    headless_local_files "$source" "$root" || {
      # Reserved cleanup belongs to the lease/identity-checked parent. A fixed
      # internal status preserves the public refusal outcome without raw stderr.
      [[ -z "$reserved_id" ]] || return 78
      rm -rf "$dir"
      rm -rf "$root"
      return 2
    }
  else
    if [[ -d "$source/projects" ]]; then rm -rf "$root/projects"; ln -s "$source/projects" "$root/projects"; fi
    for local_file in .mcp.local.json mcp.local.json .claude/settings.local.json; do
      if [[ -f "$source/$local_file" && ! -e "$root/$local_file" ]]; then
        mkdir -p "$(dirname "$root/$local_file")"
        ln -s "$source/$local_file" "$root/$local_file"
      fi
    done
    # Staging adds the whole work tree; keep it out of the excluded paths even
    # when the harness .gitignore does not cover them.
    mkdir -p "$root/.git/info"
    printf '/%s\n' "${EXCLUDED_PATHS[@]}" >> "$root/.git/info/exclude"
  fi
  printf '%s\n' "$source" > "$dir/source-root"
  printf '%s\n' "$root" > "$dir/session-root"
  printf '%s\n' "$sha" > "$dir/base-sha"
  root_identity "$root" > "$dir/root-inode"
  if [[ "${HARNESS_HEADLESS:-0}" == 1 ]]; then
    # Before the agent runs: the base commit and a matching index, owned by
    # the launcher record and outside the sandbox's writable paths.
    # No user template or config: a hooks-only init.templateDir would leave no
    # info/, and user hooks or config must not reach the trusted dir.
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git init -q --bare --template= "$dir/trusted.git"
    mkdir -p "$dir/trusted.git/info"
    # Match the clone's filesystem view (git sets these per volume at clone).
    git --git-dir="$dir/trusted.git" config core.ignorecase "$(git -C "$root" config --bool --default false core.ignorecase)"
    git --git-dir="$dir/trusted.git" config core.precomposeunicode "$(git -C "$root" config --bool --default false core.precomposeunicode)"
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git --git-dir="$dir/trusted.git" fetch -q --no-tags "$root" "+HEAD:refs/heads/base"
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git --git-dir="$dir/trusted.git" --work-tree="$root" read-tree "$sha"
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git --git-dir="$dir/trusted.git" --work-tree="$root" update-index -q --refresh
    printf '/%s\n' "${EXCLUDED_PATHS[@]}" >> "$dir/trusted.git/info/exclude"
    : > "$dir/headless"
  fi
  printf '1\n' > "$dir/lease-v1"
  : > "$dir/runtime.lock"
  [[ -n "$reserved_id" ]] || write_journal "$dir" OPEN ""
  [[ -n "$reserved_id" ]] || write_heartbeat "$dir"
  agent_usage_sync "$id" "$source"
  printf 'HARNESS_SESSION_ID=%s\nHARNESS_SOURCE_ROOT=%s\nHARNESS_SESSION_ROOT=%s\nHARNESS_RUN_DIR=%s\n' "$id" "$source" "$root" "$root"
}
valid_id() { [[ "$1" =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]; }
journal_valid() {
  local journal="$1"
  [[ -f "$journal" && ! -L "$journal" ]] || return 1
  awk '
    NR == 1 && $0 ~ /^state=(OPEN|ABANDONED|CONFLICT|CLOSED|SUBMITTED|INTEGRATING|DELIVERED|DISCARDED)$/ { state = 1; next }
    NR == 2 && $0 ~ /^identity=[^[:cntrl:]]*$/ { identity = 1; next }
    NR == 3 && $0 ~ /^heartbeat=[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/ { heartbeat = 1; next }
    { bad = 1 }
    END { exit !(NR == 3 && state && identity && heartbeat && !bad) }
  ' "$journal"
}
lease_marker_valid() {
  local marker="$1"
  [[ -f "$marker" && ! -L "$marker" && "$(wc -c < "$marker" | tr -d ' ')" == 2 ]] && grep -qx 1 "$marker"
}
git_object_id_file_valid() {
  local path="$1" oid bytes
  [[ -f "$path" && ! -L "$path" ]] || return 1
  oid="$(<"$path")"; bytes="$(wc -c < "$path" | tr -d ' ')"
  [[ "$oid" =~ ^([0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})$ ]] || return 1
  [[ "$bytes" == 41 || "$bytes" == 65 ]]
}
delivered_manifest_valid() {
  local manifest="$1" kind mode oid path count=0
  [[ -s "$manifest" && ! -L "$manifest" ]] || return 1
  exec 7< "$manifest" || return 1
  while :; do
    kind=""
    if ! IFS= read -r -d '' kind <&7; then
      exec 7<&-
      [[ -z "$kind" && "$count" -gt 0 ]]
      return
    fi
    IFS= read -r -d '' mode <&7 && IFS= read -r -d '' oid <&7 && IFS= read -r -d '' path <&7 || { exec 7<&-; return 1; }
    case "$kind" in
      F)
        [[ "$mode" == 100644 || "$mode" == 100755 || "$mode" == 120000 || "$mode" == 160000 ]] || { exec 7<&-; return 1; }
        [[ "$oid" =~ ^([0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})$ ]] || { exec 7<&-; return 1; }
        ;;
      D) [[ "$mode" == - && "$oid" == - ]] || { exec 7<&-; return 1; } ;;
      *) exec 7<&-; return 1 ;;
    esac
    case "$path" in ''|/*|.|..|./*|../*|*/.|*/..|*/./*|*/../*|*//*) exec 7<&-; return 1 ;; esac
    count=$((count + 1))
  done
}
terminal_record_valid() {
  local dir="$1" journal="$dir/journal" state identity
  journal_valid "$journal" || return 1
  state="$(field "$journal" state)"; identity="$(field "$journal" identity)"
  case "$state" in
    CLOSED) [[ -z "$identity" ]] ;;
    DELIVERED)
      [[ "$identity" =~ ^[0-9A-Fa-f]{64}$ ]] && git_object_id_file_valid "$dir/delivered-sha" && delivered_manifest_valid "$dir/delivered-manifest"
      ;;
    # Identity: empty (ABANDONED) or the submission digest CONFLICT carried.
    DISCARDED)
      [[ -z "$identity" || "$identity" =~ ^[0-9A-Fa-f]{64}$ ]] && [[ -f "$dir/discarded.patch" && ! -L "$dir/discarded.patch" ]]
      ;;
    *) return 1 ;;
  esac
}
terminal_epoch() {
  local journal="$1" value
  value="$(field "$journal" heartbeat)"
  date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$value" +%s 2>/dev/null \
    || date -u -d "$value" +%s 2>/dev/null
}
# Called only under the stock per-root lease. Native verification must see the
# original pathname disappear; failures restore the same directory/inode.
work_content_verified() {
  local dir="$1" root="$2" helper tmp
  helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/harness_session_work_proof.py"
  [[ -f "$helper" && ! -L "$helper" ]] || return 1
  tmp="$(mktemp "$dir/.work-proof.XXXXXX")" || return 1
  if python3 -I "$helper" --state "$(cd "$dir/../.." && pwd -P)" --owner "${dir##*/}" --retained-root "$root" > "$tmp"; then
    mv -f "$tmp" "$dir/work-proof.json"
  else
    rm -f "$tmp"; return 1
  fi
}
retire_native_root() {
  local root="$1" tomb="$2" dir="$3" source="$4" id="$5" staged="${6:-0}"
  local helper py native catalog receipt output snapshot revision='legacy-unknown'
  helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/codex-history.py"
  [[ -f "$helper" && ! -L "$helper" ]] || return 1
  source "$(dirname "$helper")/harness-common.sh"
  py="$(harness_python3_resolve)" && native="$(harness_codex_bin_resolve)" || return 1
  catalog="$(state_home)/native-history"
  receipt="$dir/native-history-snapshot-${BASHPID:-$$}-$(date -u +%s).json"
  # Binding provenance is checked by the managed entry; legacy origin is
  # explicitly unknown rather than borrowing the currently selected pin.
  output="$("$py" -I "$helper" snapshot --source "$root/.harness/codex" --catalog "$catalog" \
    --receipt "$receipt" --source-root "$source" --isolation-id "$id" --runtime-revision "$revision")" || return 1
  snapshot="$(printf '%s' "$output" | "$py" -I -c 'import json,sys; print(json.load(sys.stdin)["snapshot_id"])')" || return 1
  # Strict Native provenance/ledger validation precedes transcript copying.
  # Preserve the managed conversation catalog as well as the full Native store.
  archive_session_transcripts "$(state_home)" "$id" "$source" "$root" || return 1
  if [[ "$staged" != 1 ]]; then
    [[ ! -e "$tomb" && ! -L "$tomb" ]] && mv "$root" "$tomb" || return 1
  fi
  if ! work_content_verified "$dir" "$tomb"; then
    if [[ "$staged" != 1 && ! -e "$root" && ! -L "$root" ]]; then mv "$tomb" "$root" || return 1; fi
    return 1
  fi
  if ! "$py" -I "$helper" verify --catalog "$catalog" --snapshot "$snapshot" \
      --source "$tomb/.harness/codex" --codex-bin "$native" >/dev/null; then
    if [[ "$staged" != 1 && ! -e "$root" && ! -L "$root" ]]; then mv "$tomb" "$root" || return 1; fi
    return 1
  fi
  if ! work_content_verified "$dir" "$tomb"; then
    if [[ "$staged" != 1 && ! -e "$root" && ! -L "$root" ]]; then mv "$tomb" "$root" || return 1; fi
    return 1
  fi
  rm -rf -- "$tomb"
}
archive_verified() {
  local state="$1" id="$2" source="$3" script
  script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/harness_session_archive.py"
  [[ -f "$script" && ! -L "$script" ]] || return 1
  python3 "$script" verify --state "$state" --owner "$id" --source "$source" >/dev/null 2>&1
}
archive_session_transcripts() {
  local state="$1" id="$2" source="$3" root="$4" script
  script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/harness_session_archive.py"
  [[ -f "$script" && ! -L "$script" ]] || return 1
  python3 "$script" archive --state "$state" --owner "$id" --source "$source" --root "$root" >/dev/null 2>&1 \
    && archive_verified "$state" "$id" "$source"
}
resume_hold_preserves_root() {
  local dir="$1" id="$2" source="$3" hold pid start held_source held_id request actual
  for hold in "$dir"/resume-hold-*.json; do
    [[ -e "$hold" || -L "$hold" ]] || continue
    [[ -f "$hold" && ! -L "$hold" && "$(stat -f '%u %l' "$hold" 2>/dev/null || stat -c '%u %h' "$hold")" == "$(id -u) 1" ]] || return 0
    {
      IFS= read -r pid
      IFS= read -r start
      IFS= read -r held_source
      IFS= read -r held_id
      IFS= read -r request
    } < <(python3 - "$hold" <<'PY'
import json,sys
try:
    value=json.load(open(sys.argv[1]))
    keys=('pid','start','source_root','session_id','request_id')
    if set(value) != set(keys) or not isinstance(value['pid'],int) or any(not isinstance(value[k],str) or not value[k] for k in keys[1:]): raise ValueError
    if any('\n' in value[k] or '\r' in value[k] for k in keys[1:]): raise ValueError
    for key in keys: print(value[key])
except Exception: pass
PY
)
    [[ -n "$pid" && "$held_source" == "$source" && "$held_id" == "$id" && "$request" =~ ^[A-Za-z0-9._-]+$ ]] || return 0
    start="$(printf '%s\n' "$start" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    actual="$(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    if [[ -z "$actual" || "$actual" != "$start" ]]; then rm -f -- "$hold"; continue; fi
    return 0
  done
  return 1
}
reserve_hold() {
  local id="$1" request="$2" pid="$3" started="$4" source="$5" dir recorded actual hold tmp
  valid_id "$id" && [[ "$request" =~ ^[A-Za-z0-9._-]+$ && "$pid" =~ ^[1-9][0-9]*$ && -n "$started" ]] || return 2
  dir="$(session_dir "$id")"
  [[ -d "$dir" && ! -L "$dir" && -f "$dir/source-root" && ! -L "$dir/source-root" ]] || return 2
  IFS= read -r recorded < "$dir/source-root"
  [[ "$recorded" == "$source" ]] || return 2
  started="$(printf '%s\n' "$started" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  actual="$(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  [[ -n "$actual" && "$actual" == "$started" ]] || return 2
  hold="$dir/resume-hold-$request.json"; [[ ! -e "$hold" && ! -L "$hold" ]] || return 2
  tmp="$(mktemp "$dir/.resume-hold.XXXXXX")" || return 2
  python3 - "$tmp" "$pid" "$started" "$source" "$id" "$request" <<'PY'
import json,sys
with open(sys.argv[1], 'w') as out:
    json.dump(dict(zip(('pid','start','source_root','session_id','request_id'),
                       (int(sys.argv[2]),*sys.argv[3:]))), out, separators=(',',':'))
    out.write('\n')
PY
  chmod 600 "$tmp" && mv -f "$tmp" "$hold" || { rm -f "$tmp"; return 2; }
}
release_hold() {
  local id="$1" request="$2" hold
  valid_id "$id" && [[ "$request" =~ ^[A-Za-z0-9._-]+$ ]] || return 2
  hold="$(session_dir "$id")/resume-hold-$request.json"
  [[ -e "$hold" || -L "$hold" ]] || return 0
  [[ -f "$hold" && ! -L "$hold" && "$(stat -f '%u %l' "$hold" 2>/dev/null || stat -c '%u %h' "$hold")" == "$(id -u) 1" ]] || return 2
  rm -f -- "$hold"
}
gc_unlocked() {
  local state sessions worktrees retention now started scanned=0 retired=0 retained=0
  local dir id journal status epoch age marker lock root source expected tomb tomb_name tomb_id tomb_nonce
  local history_helper history_python presence
  state="$(state_home)"; sessions="$state/sessions"; worktrees="$state/worktrees"
  retention="${HARNESS_SESSION_RETENTION_SECONDS:-86400}"
  [[ "$retention" == archive ]] && retention=86400
  [[ "$retention" =~ ^[0-9]+$ ]] && [[ "$retention" -le 604800 ]] || {
    echo 'ERROR: HARNESS_SESSION_RETENTION_SECONDS must be 0..604800' >&2
    return 2
  }
  mkdir -p "$sessions" "$worktrees"
  [[ ! -L "$state" && ! -L "$sessions" && ! -L "$worktrees" ]] || return 2
  state="$(cd "$state" && pwd -P)" || return 2
  sessions="$(cd "$sessions" && pwd -P)"; worktrees="$(cd "$worktrees" && pwd -P)"
  history_helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/codex-history.py"
  [[ -f "$history_helper" && ! -L "$history_helper" ]] || return 2
  source "$(dirname "$history_helper")/harness-common.sh"
  history_python="$(harness_python3_resolve)" || return 2
  now="$(date -u +%s)"; started="$now"

  for tomb in "$worktrees"/.retired-*; do
    [[ -d "$tomb" && ! -L "$tomb" ]] || continue
    tomb_name="${tomb##*/}"; tomb_id="${tomb_name#.retired-}"; tomb_id="${tomb_id%-*}"; tomb_nonce="${tomb_name##*-}"
    valid_id "$tomb_id" && [[ "$tomb_nonce" =~ ^[0-9]+$ ]] || continue
    # A transcript archive alone cannot prove the work content of an old
    # crash tomb. Preserve it for explicit identity/lease/work reconstruction.
    retained=$((retained + 1))
  done
  for tomb in "$worktrees"/.archiving-*; do
    [[ -d "$tomb" && ! -L "$tomb" ]] || continue
    tomb_name="${tomb##*/}"; tomb_id="${tomb_name#.archiving-}"; tomb_id="${tomb_id%-*}"; tomb_nonce="${tomb_name##*-}"
    valid_id "$tomb_id" && [[ "$tomb_nonce" =~ ^[0-9]+$ ]] || continue
    [[ -z "${HARNESS_GC_OWNER_FILTER:-}" || "$tomb_id" == "$HARNESS_GC_OWNER_FILTER" ]] || continue
    dir="$sessions/$tomb_id"; root="$worktrees/$tomb_id"
    if [[ -n "${HARNESS_GC_SOURCE_FILTER:-}" ]]; then
      [[ -f "$dir/source-root" && ! -L "$dir/source-root" && "$(<"$dir/source-root")" == "$HARNESS_GC_SOURCE_FILTER" ]] || continue
    fi
    [[ ! -e "$root" && ! -L "$root" && -f "$dir/runtime.lock" && ! -L "$dir/runtime.lock" ]] || { retained=$((retained + 1)); continue; }
    (
      exec 8>"$dir/runtime.lock"; /usr/bin/lockf -s -t 0 8 || exit 11
      terminal_record_valid "$dir" && root_intact "$dir" "$tomb" || exit 12
      [[ "$(<"$dir/session-root")" == "$root" ]] || exit 12
      mv "$tomb" "$root"
    ) || true
    retained=$((retained + 1))
  done

  for dir in "$sessions"/*; do
    [[ -d "$dir" && ! -L "$dir" ]] || continue
    id="${dir##*/}"; valid_id "$id" || continue
    [[ -z "${HARNESS_GC_OWNER_FILTER:-}" || "$id" == "$HARNESS_GC_OWNER_FILTER" ]] || continue
    if [[ -n "${HARNESS_GC_SOURCE_FILTER:-}" ]]; then
      [[ -f "$dir/source-root" && ! -L "$dir/source-root" && "$(<"$dir/source-root")" == "$HARNESS_GC_SOURCE_FILTER" ]] || continue
    fi
    scanned=$((scanned + 1)); journal="$dir/journal"; marker="$dir/lease-v1"; lock="$dir/runtime.lock"
    [[ -f "$lock" && ! -L "$lock" ]] && lease_marker_valid "$marker" && terminal_record_valid "$dir" || { retained=$((retained + 1)); continue; }
    status="$(field "$journal" state)"
    epoch="$(terminal_epoch "$journal")" || { retained=$((retained + 1)); continue; }
    age=$((now - epoch)); [[ "$age" -ge 0 && "$age" -ge "$retention" ]] || { retained=$((retained + 1)); continue; }
    (
      exec 8>"$lock"
      /usr/bin/lockf -s -t 0 8 || exit 11
      lease_marker_valid "$marker" && terminal_record_valid "$dir" || exit 12
      status="$(field "$journal" state)"
      epoch="$(terminal_epoch "$journal")" || exit 13
      age=$(( $(date -u +%s) - epoch )); [[ "$age" -ge 0 && "$age" -ge "$retention" ]] || exit 13
      [[ -f "$dir/session-root" && ! -L "$dir/session-root" && -f "$dir/source-root" && ! -L "$dir/source-root" ]] || exit 14
      IFS= read -r root < "$dir/session-root"; IFS= read -r source < "$dir/source-root"
      expected="$worktrees/$id"
      [[ "${root##*/}" == "$id" && -d "$root" && ! -L "$root" ]] || exit 15
      [[ "$(cd "${root%/*}" && pwd -P)/${root##*/}" == "$expected" ]] || exit 16
      [[ "$(cd "$root" && pwd -P)" == "$expected" && -d "$source" && "$(cd "$source" && pwd -P)" != "$expected" ]] || exit 17
      root_intact "$dir" "$root" || exit 17
      resume_hold_preserves_root "$dir" "$id" "$source" && exit 21
      work_content_verified "$dir" "$root" || exit 23
      # Binding checks above establish this physical path; keep the recorded
      # spelling for provenance even when system temp paths have aliases.
      presence="$("$history_python" -I "$history_helper" content-kind --source "$expected/.harness/codex" --anchor "$state")" || exit 21
      if [[ "$presence" == native ]]; then
        [[ -z "${HARNESS_HISTORY_SOURCE_ROOT:-}" || "$source" == "$HARNESS_HISTORY_SOURCE_ROOT" ]] || exit 21
        tomb="$worktrees/.archiving-$id-${BASHPID:-$$}"
        retire_native_root "$root" "$tomb" "$dir" "$source" "$id" || exit 21
        agent_usage_sync "$id" "$source"
        exit 0
      fi
      [[ "$presence" == absent || "$presence" == generated ]] || exit 21
      archive_session_transcripts "$state" "$id" "$source" "$root" || exit 22
      tomb="$worktrees/.retired-$id-${BASHPID:-$$}"
      [[ ! -e "$tomb" && ! -L "$tomb" ]] || exit 18
      mv "$root" "$tomb" || exit 19
      if ! work_content_verified "$dir" "$tomb"; then
        [[ ! -e "$root" && ! -L "$root" ]] && mv "$tomb" "$root"
        exit 23
      fi
      rm -rf -- "$tomb" || exit 20
      agent_usage_sync "$id" "$source"
    ) && retired=$((retired + 1)) || retained=$((retained + 1))
  done
  printf 'harness-session gc: scanned=%s retired=%s retained=%s elapsed_seconds=%s\n' "$scanned" "$retired" "$retained" "$(( $(date -u +%s) - started ))" >&2
}
gc_sessions() {
  if integration_queue_enabled; then
    local retention="${HARNESS_SESSION_RETENTION_SECONDS:-86400}"
    [[ "$retention" != archive ]] || retention=86400
    integration_queue control gc --wait --retention "$retention" --filter-owner "${HARNESS_GC_OWNER_FILTER:-}" --filter-source "${HARNESS_GC_SOURCE_FILTER:-}"
  else with_lock gc_unlocked; fi
}
resume_session() {
  local source="$1" id="$2" dir recorded root state lock worktrees expected
  valid_id "$id" || { echo "cannot resume $id: invalid session UUID" >&2; return 2; }
  source="$(cd "$source" && pwd -P)"; dir="$(session_dir "$id")"
  [[ -d "$dir" && ! -L "$dir" && -f "$dir/source-root" && ! -L "$dir/source-root" && -f "$dir/session-root" && ! -L "$dir/session-root" && -f "$dir/journal" && ! -L "$dir/journal" ]] || {
    echo "cannot resume $id: session record is missing or invalid" >&2
    return 2
  }
  lease_marker_valid "$dir/lease-v1" && [[ -f "$dir/runtime.lock" && ! -L "$dir/runtime.lock" ]] || {
    echo "cannot resume $id: invalid runtime lease record" >&2
    return 2
  }
  recorded="$(cd "$(<"$dir/source-root")" && pwd -P)"; [[ "$recorded" == "$source" ]] || { echo "cannot resume $id: source root does not match" >&2; return 2; }
  root="$(<"$dir/session-root")"; [[ -d "$root" && ! -L "$root" ]] || { echo "cannot resume $id: workspace is missing or retired; start a fresh isolated session" >&2; return 2; }
  worktrees="$(state_home)/worktrees"
  [[ -d "$worktrees" && ! -L "$worktrees" ]] || { echo "cannot resume $id: invalid workspace state root" >&2; return 2; }
  worktrees="$(cd "$worktrees" && pwd -P)"; expected="$worktrees/$id"
  [[ "${root##*/}" == "$id" && "$(cd "${root%/*}" && pwd -P)/${root##*/}" == "$expected" && "$(cd "$root" && pwd -P)" == "$expected" ]] || {
    echo "cannot resume $id: workspace is outside the isolated session root" >&2
    return 2
  }
  journal_valid "$dir/journal" || { echo "cannot resume $id: invalid session journal" >&2; return 2; }
  state="$(field "$dir/journal" state)"
  case "$state" in
    OPEN)
      lock="$dir/runtime.lock"
      [[ -f "$lock" && ! -L "$lock" ]] || { echo "cannot resume OPEN session: runtime lease is missing" >&2; return 2; }
      if ( exec 8>"$lock"; /usr/bin/lockf -s -t 0 8 ); then
        echo 'cannot resume OPEN session without an active launcher lease' >&2
        return 2
      fi
      ;;
    ABANDONED|CONFLICT|CLOSED) reopen_session "$id" ;;
    SUBMITTED) echo 'cannot resume SUBMITTED session; run harness-session integrate or recover' >&2; return 2 ;;
    INTEGRATING) echo 'cannot resume INTEGRATING session; run harness-session recover' >&2; return 2 ;;
    DELIVERED|DISCARDED) echo "cannot resume $state session; start a fresh isolated session" >&2; return 2 ;;
    *) echo "cannot resume $state session: invalid journal state" >&2; return 2 ;;
  esac
  write_heartbeat "$dir"
  printf 'HARNESS_SESSION_ID=%s\nHARNESS_SOURCE_ROOT=%s\nHARNESS_SESSION_ROOT=%s\nHARNESS_RUN_DIR=%s\n' "$id" "$source" "$root" "$root"
}

# Strict mapping trust check; legacy GC/root_intact compatibility is unchanged.
run_dir_record_safe() {
  local path="$1" kind="$2" meta owner links mode
  [[ ! -L "$path" ]] || return 1
  if [[ "$kind" == directory ]]; then [[ -d "$path" ]] || return 1
  else [[ -f "$path" ]] || return 1; fi
  meta="$(stat -f '%u %l %Lp' "$path" 2>/dev/null || stat -c '%u %h %a' "$path" 2>/dev/null)" || return 1
  read -r owner links mode <<< "$meta"
  [[ "$owner" == "$EUID" && "$mode" =~ ^[0-7]{3,4}$ ]] || return 1
  [[ "$kind" == directory || "$links" == 1 ]] || return 1
  (( (8#$mode & 022) == 0 ))
}

# resolve_run_dir <source-root> <session-id> <source-run-dir>
# Bind a caller-selected source-relative directory to the session clone. The
# record, journal and root inode make this a broker decision, never an ambient
# environment trust decision. `projects` is the sole external exception: an
# interactive clone deliberately links its canonical source projects directory.
resolve_run_dir() {
  local source="$1" id="$2" requested="$3" dir recorded root root_abs source_abs requested_abs relative mapped projects worktrees expected record state
  valid_id "$id" || return 2
  dir="$(session_dir "$id")"
  state="$(state_home)"
  for record in "$state" "$state/sessions" "$dir" "$state/worktrees"; do
    run_dir_record_safe "$record" directory || return 2
  done
  for record in source-root session-root root-inode journal; do
    run_dir_record_safe "$dir/$record" file || return 2
  done
  journal_valid "$dir/journal" || return 2
  source_abs="$(cd "$source" 2>/dev/null && pwd -P)" || return 2
  recorded="$(<"$dir/source-root")"
  [[ -d "$recorded" && "$(cd "$recorded" 2>/dev/null && pwd -P)" == "$source_abs" ]] || return 2
  root="$(<"$dir/session-root")"
  root_intact "$dir" "$root" || return 2
  root_abs="$(cd "$root" 2>/dev/null && pwd -P)" || return 2
  worktrees="$(state_home)/worktrees"
  [[ -d "$worktrees" && ! -L "$worktrees" ]] || return 2
  expected="$(cd "$worktrees" 2>/dev/null && pwd -P)/$id"
  [[ "$root_abs" == "$expected" ]] || return 2
  requested_abs="$(cd "$requested" 2>/dev/null && pwd -P)" || return 2
  case "$requested_abs" in
    "$root_abs"|"$root_abs"/*) printf '%s\n' "$requested_abs"; return 0 ;;
    "$source_abs"|"$source_abs"/*) ;;
    *) return 2 ;;
  esac
  relative="${requested_abs#"$source_abs"}"
  mapped="$root$relative"
  [[ -d "$mapped" ]] || return 2
  mapped="$(cd "$mapped" 2>/dev/null && pwd -P)" || return 2
  case "$mapped" in "$root_abs"|"$root_abs"/*) printf '%s\n' "$mapped"; return 0 ;; esac
  projects="$root/projects"
  [[ "$relative" == /projects || "$relative" == /projects/* ]] || return 2
  [[ -L "$projects" && "$(readlink "$projects")" == "$source_abs/projects" ]] || return 2
  case "$mapped" in "$source_abs/projects"|"$source_abs/projects"/*) printf '%s\n' "$mapped" ;; *) return 2 ;; esac
}
transition() {
  local id="$1" next="$2" identity="${3:-}" dir old old_identity
  dir="$(session_dir "$id")"; [[ -f "$dir/journal" ]] || { echo "unknown session: $id" >&2; return 2; }
  old="$(field "$dir/journal" state)"; old_identity="$(field "$dir/journal" identity)"
  [[ "$old" == "$next" && "$old_identity" == "$identity" ]] && return 0
  case "$old:$next" in OPEN:SUBMITTED|OPEN:CLOSED|SUBMITTED:INTEGRATING|INTEGRATING:SUBMITTED|INTEGRATING:DELIVERED|INTEGRATING:CONFLICT|OPEN:ABANDONED|ABANDONED:OPEN|CONFLICT:OPEN|CLOSED:OPEN|ABANDONED:DISCARDED|CONFLICT:DISCARDED) ;; *) echo "invalid transition: $old -> $next" >&2; return 2;; esac
  write_journal "$dir" "$next" "$identity"
}
# session_has_changes <root> [base-sha]: 0 when the work tree changed or the
# session committed (HEAD moved off the base), 1 when it did not, 2 when git
# could not tell. Callers test the status, so errexit is off: every git call
# is checked and an error is never read as "no changes". Call use_session_git first.
session_has_changes() {
  local rc=0 head out
  [[ -n "${2:-}" ]] || return 2
  if [[ -n "$ROOT_GIT_DIR" ]]; then
    # The work tree holds the final content, committed or not.
    stage_worktree "$1" "$2" || return 2
    root_git "$1" diff --no-ext-diff --ignore-submodules=all --cached --quiet "$2" -- . "${EXCLUDED_PATHSPEC[@]}" || rc=$?
    case "$rc" in 0) return 1 ;; 1) return 0 ;; *) return 2 ;; esac
  fi
  if [[ -n "${2:-}" ]]; then
    head="$(session_git -C "$1" rev-parse HEAD)" || return 2
    [[ "$head" == "$2" ]] || return 0
  fi
  out="$(session_git -C "$1" status --porcelain --ignore-submodules=all --untracked-files=all -- . "${EXCLUDED_PATHSPEC[@]}")" || return 2
  [[ -n "$out" ]] || return 1
}
write_exit_reason() {
  local dir="$1" reason="$2" code="$3" tmp
  tmp="$(mktemp "$dir/.exit-reason.XXXXXX")" || return 1
  printf 'reason=%s\nexit_code=%s\nobserved_at=%s\n' "$reason" "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp"
  mv -f "$tmp" "$dir/exit-reason"
}
exit_session() {
  local id="$1" dir root state
  dir="$(session_dir "$id")"; root="$(<"$dir/session-root")"; state="$(field "$dir/journal" state)"
  if [[ "$state" == OPEN ]]; then
    local refused=0
    use_session_git "$id" || refused=$?
    [[ "$refused" == 0 ]] || { write_exit_reason "$dir" git_preflight_refused "$refused"; transition "$id" ABANDONED; return "$refused"; }
    # Only a clean answer closes; changes or a git error keep the work.
    local rc=0
    session_has_changes "$root" "$(<"$dir/base-sha")" || rc=$?
    if [[ "$rc" == 1 ]]; then write_exit_reason "$dir" clean "$rc"; transition "$id" CLOSED
    else
      if [[ "$rc" == 0 ]]; then write_exit_reason "$dir" work_changes "$rc"; else write_exit_reason "$dir" git_read_error "$rc"; fi
      transition "$id" ABANDONED
    fi
  fi
}
recover_unlocked() {
  local id="$1" dir state source remote pending readback kind mode hash path actual_mode actual_type actual_oid actual_path entry
  dir="$(session_dir "$id")"; state="$(field "$dir/journal" state)"
  if [[ "$state" == INTEGRATING ]]; then
    source="$(<"$dir/source-root")"; remote="$(git -C "$source" remote get-url origin)"
    if [[ "$remote" == "$(<"$dir/remote-url")" && -f "$dir/pending-sha" && -f "$dir/.candidate-manifest" ]]; then
      pending="$(<"$dir/pending-sha")"
      if [[ ! -f "$dir/push-ack" || "$(<"$dir/push-ack")" == "$pending" ]]; then
        readback="$(mktemp -d "$(state_home)/recover-readback.XXXXXX")"
        git clone -q "$remote" "$readback"
        git -C "$readback" merge-base --is-ancestor "$pending" HEAD || { rm -rf "$readback"; transition "$id" SUBMITTED "$(field "$dir/journal" identity)"; printf 'HARNESS_SESSION_ROOT=%s\nstate=SUBMITTED\n' "$(<"$dir/session-root")"; return 0; }
        while IFS= read -r -d '' kind && IFS= read -r -d '' mode && IFS= read -r -d '' hash && IFS= read -r -d '' path; do
          if [[ "$kind" == D ]]; then git -C "$readback" cat-file -e "$pending:$path" 2>/dev/null && { rm -rf "$readback"; transition "$id" CONFLICT "$(field "$dir/journal" identity)"; return 4; }; continue; fi
          actual_mode=""; actual_oid=""; entry="$(git -C "$readback" ls-tree "$pending" -- ":(literal)$path")"; [[ -z "$entry" ]] || read -r actual_mode actual_type actual_oid actual_path <<< "$entry"
          [[ "$actual_mode" == "$mode" && "$actual_oid" == "$hash" ]] || { rm -rf "$readback"; transition "$id" CONFLICT "$(field "$dir/journal" identity)"; return 4; }
        done < "$dir/.candidate-manifest"
        printf '%s\n' "$pending" > "$dir/delivered-sha"
        cp -p "$dir/.candidate-manifest" "$dir/delivered-manifest"
        fsync_paths "$dir/delivered-sha" "$dir/delivered-manifest" "$dir" || return 4
        rm -rf "$readback"; transition "$id" DELIVERED "$(field "$dir/journal" identity)"
        fsync_paths "$dir/journal" "$dir" || return 4
        printf 'HARNESS_SESSION_ROOT=%s\nstate=DELIVERED\n' "$(<"$dir/session-root")"
        return 0
      fi
    fi
    transition "$id" SUBMITTED "$(field "$dir/journal" identity)"
    printf 'HARNESS_SESSION_ROOT=%s\nstate=SUBMITTED\n' "$(<"$dir/session-root")"
    return 0
  fi
  [[ "$state" != DISCARDED ]] || { echo 'cannot recover DISCARDED session; start a fresh isolated session' >&2; return 2; }
  [[ "$state" == ABANDONED || "$state" == CONFLICT ]] || return 2
  reopen_session "$id"
  printf 'HARNESS_SESSION_ROOT=%s\nstate=OPEN\n' "$(<"$dir/session-root")"
}
recover() {
  if integration_queue_enabled; then integration_queue control recover "$1" --wait
  else with_lock recover_unlocked "$1"; fi
}
heartbeat_epoch() { date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null || printf '0\n'; }
heartbeat_session() {
  local dir="$(session_dir "$1")" state
  [[ -f "$dir/journal" ]] || return 2
  state="$(field "$dir/journal" state)"
  [[ "$state" == OPEN ]] || return 0
  write_heartbeat "$dir"
}
list() {
  local dir id state heartbeat now age limit
  now="$(date -u +%s)"; limit="${HARNESS_SESSION_STALE_SECONDS:-300}"
  for dir in "$(state_home)"/sessions/*; do
    [[ -d "$dir" && -f "$dir/journal" ]] || continue
    id="${dir##*/}"; state="$(field "$dir/journal" state)"
    if [[ -f "$dir/heartbeat" ]]; then heartbeat="$(heartbeat_epoch "$(<"$dir/heartbeat")")"; else heartbeat="$(heartbeat_epoch "$(field "$dir/journal" heartbeat)")"; fi
    age=$((now - heartbeat))
    if [[ "$state" == OPEN && "$age" -gt "$limit" ]]; then
      # Stale heartbeats do not prove a runtime stopped. Only lease-v1 owners
      # with an available runtime lease can be observed as abandoned.
      if [[ -f "$dir/lease-v1" && ! -L "$dir/lease-v1" && "$(<"$dir/lease-v1")" == 1 && -f "$dir/runtime.lock" && ! -L "$dir/runtime.lock" ]]; then
        (
          exec 8>"$dir/runtime.lock"; /usr/bin/lockf -s -t 0 8 || exit 1
          [[ "$(field "$dir/journal" state)" == OPEN ]] || exit 1
          local latest
          if [[ -f "$dir/heartbeat" ]]; then latest="$(heartbeat_epoch "$(<"$dir/heartbeat")")"; else latest="$(heartbeat_epoch "$(field "$dir/journal" heartbeat)")"; fi
          [[ $((now - latest)) -gt "$limit" ]] || exit 1
          local reason_tmp
          reason_tmp="$(mktemp "$dir/.exit-reason.XXXXXX")" || exit 1
          printf 'reason=stale_without_lease\nexit_code=unknown\nobserved_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$reason_tmp"
          mv "$reason_tmp" "$dir/exit-reason" && transition "$id" ABANDONED
        ) || true
        state="$(field "$dir/journal" state)"
      fi
    fi
    printf '%s %s\n' "$id" "$state"
  done
}
write_index_manifest() {
  local root="$1" base="$2" output="$3" status path mode oid stage path2 entry
  root_git "$root" diff --no-ext-diff --ignore-submodules=all --cached --name-status --no-renames -z "$base" -- . "${EXCLUDED_PATHSPEC[@]}" > "$output.paths" || return 1
  : > "$output"
  while IFS= read -r -d '' status && IFS= read -r -d '' path; do
    if [[ "$status" == D* ]]; then printf 'D\0-\0-\0%s\0' "$path" >> "$output"; continue; fi
    entry="$(root_git "$root" ls-files -s -- ":(literal)$path")" && [[ -n "$entry" ]] || return 1
    read -r mode oid stage path2 <<< "$entry"
    printf 'F\0%s\0%s\0%s\0' "$mode" "$oid" "$path" >> "$output"
  done < "$output.paths"
  rm -f "$output.paths"
}
submission_identity() {
  local dir="$1"
  { shasum -a 256 "$dir/base-sha" "$dir/source-root" "$dir/remote-url" "$dir/submission.patch" "$dir/manifest"; } | shasum -a 256 | awk '{print $1}'
}
submit() {
  local id="$1" dir root base state identity
  dir="$(session_dir "$id")"; root="$(<"$dir/session-root")"; base="$(<"$dir/base-sha")"; state="$(field "$dir/journal" state)"
  [[ "$state" == SUBMITTED ]] && return 0
  [[ "$state" == OPEN ]] || { echo "cannot submit $state session" >&2; return 2; }
  use_session_git "$id" || return $?
  stage_worktree "$root" "$base"
  if [[ -z "$ROOT_GIT_DIR" ]]; then
    # Interactive: committed work is in the index already, so staged excluded
    # paths are only unstaged back to HEAD; committed ones refuse instead of
    # being silently dropped. A headless index is reset to the base there.
    local touched aliases
    touched="$(session_git -C "$root" diff --no-ext-diff --ignore-submodules=all --cached --name-only "$base" -- "${EXCLUDED_ICASE[@]}")" \
      && aliases="$(excluded_aliases "$root" "$base" | tr '\0' '\n')" \
      || { echo 'harness-session: refused: could not check the submission for excluded paths' >&2; return 2; }
    touched="$touched${aliases:+$'\n'$aliases}"
    [[ -z "$touched" ]] || { printf 'harness-session: refused: submission touches excluded path(s):\n%s\n' "$touched" >&2; return 7; }
  fi
  root_git "$root" diff --no-ext-diff --no-textconv --ignore-submodules=all --cached --binary "$base" -- . "${EXCLUDED_PATHSPEC[@]}" > "$dir/submission.patch"
  [[ -s "$dir/submission.patch" ]] || { echo 'empty submission' >&2; return 2; }
  write_index_manifest "$root" "$base" "$dir/.manifest"
  mv -f "$dir/.manifest" "$dir/manifest"
  git -C "$(<"$dir/source-root")" remote get-url origin > "$dir/remote-url"
  identity="$(submission_identity "$dir")"
  chmod 0444 "$dir/base-sha" "$dir/source-root" "$dir/remote-url" "$dir/submission.patch" "$dir/manifest"
  transition "$id" SUBMITTED "$identity"
}
with_lock() {
  local lock="$(state_home)/integration.lock" timeout="${HARNESS_SESSION_LOCK_TIMEOUT:-600}"
  [[ -x /usr/bin/lockf ]] || { echo 'ERROR: /usr/bin/lockf is required for safe session integration' >&2; return 1; }
  mkdir -p "$(state_home)"
  (
    /usr/bin/lockf -s -t "$timeout" 9 || { echo 'ERROR: timed out waiting for session integration lock' >&2; return 1; }
    "$@"
  ) 9>"$lock"
}
write_candidate_manifest() {
  local repo="$1" tree="$2" submitted="$3" output="$4" kind old_mode old_oid path entry meta mode type oid
  : > "$output"
  while IFS= read -r -d '' kind && IFS= read -r -d '' old_mode && IFS= read -r -d '' old_oid && IFS= read -r -d '' path; do
    # Empty output is a deletion; an ls-tree error is not.
    entry="$(git -C "$repo" ls-tree "$tree" -- ":(literal)$path")" || return 1
    if [[ -z "$entry" ]]; then
      printf 'D\0-\0-\0%s\0' "$path" >> "$output"
      continue
    fi
    meta="${entry%%$'\t'*}"
    read -r mode type oid <<< "$meta"
    printf 'F\0%s\0%s\0%s\0' "$mode" "$oid" "$path" >> "$output"
  done < "$submitted"
}
verify_submission_identity() {
  local dir="$1" expected
  if [[ -n "${INTEGRATION_INPUT_DIR:-}" ]]; then
    integration_queue authorize integrate "${dir##*/}" --home "${HARNESS_INTEGRATION_HOME:?}" >/dev/null
    return
  fi
  expected="$(field "$dir/journal" identity)"
  [[ -n "$expected" && "$(submission_identity "$dir")" == "$expected" ]]
}
# The headless verifier runs candidate content (repository tests the agent may
# have written), so it runs under Seatbelt, the mechanism Claude Code itself
# uses on macOS, on a throwaway copy of the candidate the broker never reads
# back. Fixed path: nothing in the environment chooses it.
SANDBOX_EXEC=/usr/bin/sandbox-exec
# Mach services a sandboxed bash, git or python needs: user and group lookup
# and logging. Everything else (launchd job submission, LaunchServices,
# AppleEvents, XPC services, the keychain) is unreachable.
SANDBOX_MACH_ALLOW=(com.apple.system.opendirectoryd.libinfo
  com.apple.system.logger com.apple.logd com.apple.diagnosticd com.apple.system.notification_center)
# phys <path>: the physical path of an existing directory (Seatbelt matches
# resolved paths), otherwise the path itself.
phys() { if [[ -d "$1" ]]; then (cd -P "$1" && pwd); else printf '%s\n' "$1"; fi; }
sbpl_str() { local value="${1//\\/\\\\}"; printf '"%s"' "${value//\"/\\\"}"; }
# verifier_sandbox_profile <copy> <tmp> <home> <source> <trusted>: the SBPL
# profile, deny by default. Allowed: fork, and exec of binaries under system
# and toolchain prefixes or the copy, temp dir and trusted verifier; reads of
# system and toolchain trees (Homebrew without etc/var except its OpenSSL and
# CA config, mise, PATH directories other than / or ones holding HOME or the
# state home), a
# few /private/etc files, the copy, the temp dir and the
# trusted verifier; metadata reads anywhere (path resolution: stat, not
# directory listings); writes only to the copy and the temp dir; network
# only for unix sockets inside the temp dir; sysctl reads except
# the process table; mach services in SANDBOX_MACH_ALLOW; signals and process
# inspection within the sandbox. The state home (other sessions' worktrees,
# records and trusted git dirs) is denied even if a PATH directory holds it,
# then the copy and the trusted verifier inside it are allowed again.
# Credential stores and the source checkout are denied even inside an allowed
# tree. No user git config is readable (the verifier gets a generated one).
# /Library: only the toolchain parts, not Application Support, Preferences,
# Managed Preferences, Logs or Keychains.
SANDBOX_SYSTEM_READ=(/usr /bin /sbin /System /Library/Developer /Library/Frameworks /Library/Apple /Library/Perl
  /private/var/db/timezone /private/var/select)
SANDBOX_SYSTEM_EXEC=(/usr /bin /sbin /System /Library/Developer /private/var/select)
SANDBOX_ETC_READ=(/private/etc/hosts /private/etc/passwd /private/etc/group /private/etc/localtime
  /private/etc/services /private/etc/protocols /private/etc/shells /private/etc/ssl)
SANDBOX_TOOL_PREFIXES=(/opt/homebrew /usr/local)
# Credential stores under HOME, denied even inside an allowed tree.
SANDBOX_HOME_DENY=(.ssh .hermes buzz .config/gh .aws .claude Library/Keychains .git-credentials .netrc .config/git)
verifier_sandbox_profile() {
  local copy tmp home source="$4" trusted state path entry name prefix
  copy="$(phys "$1")"; tmp="$(phys "$2")"; home="$(phys "$3")"; trusted="$(phys "$5")"; state="$(phys "$(state_home)")"
  local -a read=("${SANDBOX_SYSTEM_READ[@]}" "${SANDBOX_ETC_READ[@]}" "$copy" "$tmp" "$trusted"
    "$home/.config/mise/config.toml" "$home/.local/share/mise" "$home/.cache/mise" "$home/.CFUserTextEncoding")
  local -a exec=("${SANDBOX_SYSTEM_EXEC[@]}" "$copy" "$tmp" "$trusted" "$home/.local/share/mise") deny=() etc_allow=()
  for prefix in "${SANDBOX_TOOL_PREFIXES[@]}"; do
    [[ -d "$prefix" ]] || continue
    read+=("$prefix"); exec+=("$prefix"); deny+=("$prefix/etc" "$prefix/var")
    # TLS trust and OpenSSL config (no secrets) under the denied etc.
    for path in "$prefix/etc/openssl@3" "$prefix/etc/ca-certificates"; do [[ ! -d "$path" ]] || etc_allow+=("$path"); done
  done
  local -a path_entries=()
  IFS=: read -r -a path_entries <<< "${PATH:-}"
  for entry in "${path_entries[@]}"; do
    [[ -d "$entry" ]] || continue
    entry="$(phys "$entry")"
    # A PATH directory is a toolchain, unless it is / or holds HOME or the
    # state home.
    [[ "$entry" != / && "$home/" != "$entry/"* && "$state/" != "$entry/"* ]] || continue
    read+=("$entry"); exec+=("$entry")
  done
  for name in "${SANDBOX_HOME_DENY[@]}"; do deny+=("$home/$name"); done
  deny+=(/Library/Keychains "$source")
  for path in "${deny[@]}"; do [[ ! -e "$path" ]] || deny+=("$(phys "$path")"); done
  printf '(version 1)\n(deny default)\n'
  printf '(allow process-fork)\n(allow process-exec*'
  for path in "${exec[@]}"; do printf ' (subpath %s)' "$(sbpl_str "$path")"; done
  printf ')\n(allow file-read-metadata)\n(allow file-read*'
  for path in "${read[@]}"; do printf ' (subpath %s)' "$(sbpl_str "$path")"; done
  printf ' (literal "/"))\n'
  # Not all of /dev (the user's terminals are /dev/ttys*): the listing itself
  # (ttyname) and the devices a process needs.
  printf '(allow file-read* (literal "/dev") (literal "/dev/null") (literal "/dev/zero") (literal "/dev/random")'
  printf ' (literal "/dev/urandom") (literal "/dev/tty") (literal "/dev/dtracehelper") (subpath "/dev/fd"))\n'
  printf '(allow file-write* (subpath %s) (subpath %s)' "$(sbpl_str "$copy")" "$(sbpl_str "$tmp")"
  printf ' (literal "/dev/null") (literal "/dev/tty") (literal "/dev/dtracehelper") (subpath "/dev/fd"))\n'
  printf '(allow file-ioctl (literal "/dev/null") (literal "/dev/tty") (literal "/dev/dtracehelper") (subpath "/dev/fd"))\n'
  printf '(allow ipc-posix-shm-read* (ipc-posix-name "apple.shm.notification_center"))\n'
  # Python multiprocessing locks and events: POSIX semaphores named /mp-<random>
  # (create, open, post, wait and unlink; unlink is how the resource tracker
  # cleans up). The codex agent profile inherits this rule. Known limit: named
  # semaphores outlive the run, so a hostile test could exhaust the system-wide
  # table (kern.posix.sem.max) until reboot; Seatbelt has no count limit.
  printf '(allow ipc-posix-sem (ipc-posix-name-prefix "/mp-"))\n'
  # The state home, then the verifier's own dirs inside it again.
  printf '(deny file-read* file-write* process-exec* (subpath %s) (subpath %s))\n' \
    "$(sbpl_str "$(state_home)")" "$(sbpl_str "$state")"
  printf '(allow file-read* process-exec* (subpath %s) (subpath %s) (subpath %s))\n' \
    "$(sbpl_str "$copy")" "$(sbpl_str "$tmp")" "$(sbpl_str "$trusted")"
  printf '(allow file-write* (subpath %s) (subpath %s))\n' "$(sbpl_str "$copy")" "$(sbpl_str "$tmp")"
  printf '(deny file-read* file-write* process-exec*'
  for path in "${deny[@]}"; do printf ' (subpath %s)' "$(sbpl_str "$path")"; done
  printf ')\n'
  if (( ${#etc_allow[@]} )); then
    printf '(allow file-read*'
    for path in "${etc_allow[@]}"; do printf ' (subpath %s)' "$(sbpl_str "$path")"; done
    printf ')\n'
  fi
  # No system-socket: it gates PF_SYSTEM kernel-control sockets, which no
  # verifier needs. Ordinary socket() calls are not gated; every network*
  # operation on them is denied except AF_UNIX inside the temp dir.
  printf '(allow network* (local unix-socket (subpath %s)))\n' "$(sbpl_str "$tmp")"
  printf '(allow network* (remote unix-socket (subpath %s)))\n' "$(sbpl_str "$tmp")"
  printf '(allow sysctl-read)\n(deny sysctl-read (sysctl-name-prefix "kern.proc"))\n'
  printf '(allow mach-lookup'
  for name in "${SANDBOX_MACH_ALLOW[@]}"; do printf ' (global-name "%s")' "$name"; done
  printf ')\n(allow signal (target same-sandbox))\n'
  # The same-sandbox allow alone does not stop KERN_PROCARGS2 or KERN_PROC_PID
  # on outside processes (measured on macOS 26: argv and environment of the
  # broker and launcher were readable); an explicit deny does, with the
  # sandbox's own processes allowed again.
  printf '(deny process-info*)\n(allow process-info* (target same-sandbox))\n'
}
# sandbox_check <sandbox-exec>: does the profile load and run here?
sandbox_check() {
  local exe="$1" dir rc=0
  dir="$(mktemp -d "${TMPDIR:-/tmp}/harness-sandbox-check.XXXXXX")" || return 1
  mkdir "$dir/candidate" "$dir/tmp" "$dir/trusted" || { rm -rf "$dir"; return 1; }
  "$exe" -p "$(verifier_sandbox_profile "$dir/candidate" "$dir/tmp" "${HOME:?}" "$dir/source" "$dir/trusted")" /usr/bin/true || rc=$?
  rm -rf "$dir"
  return "$rc"
}
# codex_sandbox_profile <root> <tmp> <home> <source> <codex> <port> <codex-home> <host>:
# the headless Codex agent's profile, the only boundary around the whole codex
# process tree. The verifier profile with the session root and the run's temp
# dir as its copy and temp dir and the resolved codex binary as its trusted
# path, plus read and exec of exactly the code-mode host beside it, PTYs
# (codex runs commands in one) and TCP to exactly the
# launcher's forwarder port. Seatbelt names loopback only as `localhost`, so
# that is `localhost:<port>`, never a wildcard port. After every allow: the
# verifier's credential stores, ~/.codex, ~/.config/harness-launcher (the
# endpoint key), the source checkout, the state home except the session root
# (other sessions' worktrees and records), and writes to the launcher's
# CODEX_HOME files.
codex_sandbox_profile() {
  local root tmp home source="$4" exe="$5" port="$6" host="$8" codex_home state name path
  [[ "$port" =~ ^[0-9]{1,5}$ ]] || { echo 'harness-session: invalid forwarder port' >&2; return 2; }
  root="$(phys "$1")"; tmp="$(phys "$2")"; home="$(phys "$3")"; codex_home="$(phys "$7")"
  verifier_sandbox_profile "$1" "$2" "$3" "$source" "$exe" || return
  # PTYs as Codex's own Seatbelt policy grants them: openpty(), and a slave
  # tty only with the sandbox extension the kernel issues for a PTY opened
  # inside the sandbox, never a terminal opened outside it.
  printf '(allow pseudo-tty)\n(allow file-read* file-write* file-ioctl (literal "/dev/ptmx"))\n'
  printf '(allow file-read* file-write* file-ioctl (require-all (regex #"^/dev/ttys[0-9]+$") (extension "com.apple.sandbox.pty")))\n'
  printf '(allow file-read* process-exec* (literal %s))\n' "$(sbpl_str "$host")"
  printf '(allow network-outbound (remote tcp "localhost:%s"))\n' "$port"
  local -a deny=(/Library/Keychains "$source")
  for name in "${SANDBOX_HOME_DENY[@]}" .codex .config/harness-launcher; do deny+=("$home/$name"); done
  for path in "${deny[@]}"; do [[ ! -e "$path" ]] || deny+=("$(phys "$path")"); done
  printf '(deny file-read* file-write* process-exec*'
  for path in "${deny[@]}"; do printf ' (subpath %s)' "$(sbpl_str "$path")"; done
  printf ')\n'
  for state in "$(state_home)" "$(phys "$(state_home)")"; do
    printf '(deny file-read* file-write* process-exec* (require-all (subpath %s) (require-not (subpath %s))))\n' \
      "$(sbpl_str "$state")" "$(sbpl_str "$root")"
  done
  # Neither file can be rewritten, replaced or renamed, nor CODEX_HOME moved.
  printf '(deny file-write* (literal %s) (literal %s) (literal %s))\n' "$(sbpl_str "$codex_home")" \
    "$(sbpl_str "$codex_home/config.toml")" "$(sbpl_str "$codex_home/AGENTS.md")"
}
run_repository_verifier() {
  local candidate="$1" id="$2" remote="$3" baseline="$4" trusted verifier help_output
  local -a verifier_args
  trusted="$(mktemp -d "$(state_home)/verifier.XXXXXX")" || return 2
  { git clone -q "$remote" "$trusted" && git -C "$trusted" checkout -q --detach "$baseline"; } || { rm -rf "$trusted"; return 2; }
  verifier="$trusted/core/bin/auto-deliver.sh"
  [[ -f "$verifier" ]] || { echo 'ERROR: repository-owned core/bin/auto-deliver.sh verifier is required' >&2; rm -rf "$trusted"; return 2; }
  verifier_args=("verify isolated session $id" --staged-only --dry-run --no-push)
  if integration_headless "$id"; then
    run_sandboxed_verifier "$candidate" "$id" "$trusted" "$verifier" "${verifier_args[@]}"
    return
  fi
  help_output="$(verifier_exec bash "$verifier" --help 2>/dev/null || true)"
  if grep -q -- '--contract-checked' <<< "$help_output"; then
    verifier_args+=(--contract-checked 'broker candidate cascade verified')
  fi
  local rc=0
  (if [[ -z "${INTEGRATION_INPUT_DIR:-}" ]]; then unset HARNESS_SESSION_ID HARNESS_SOURCE_ROOT HARNESS_SESSION_ROOT HARNESS_RUN_DIR HARNESS_SESSION_STATE_HOME; fi
    cd "$candidate" && TEST_HARNESS_DIR="$candidate" HARNESS_POST_COMMIT_PUSH=0 HARNESS_POST_COMMIT_CODEX_SYNC=0 HARNESS_RAG_ENABLED=0 HARNESS_SESSION_BACKLINK=0 \
    verifier_exec bash "$verifier" "${verifier_args[@]}") || rc=$?
  rm -rf "$trusted"
  return "$rc"
}
# run_sandboxed_verifier <candidate> <id> <trusted> <verifier> <args...>:
# headless sessions only. The verifier gets a copy of the candidate (index
# included, no hard links), a minimal environment (no agent sockets, tokens or
# GitHub variables), a fresh temp dir outside HOME and a generated global git
# config (identity only); neither the copy nor the trusted clone keeps a
# remote, so no URL with credentials is readable. The copy and the temp dir
# are removed afterwards and nothing is read back. The broker commits and
# pushes from the candidate itself, which the sandbox never touched.
# scrub_remotes <repo>: drop every remote (and url rewrites) from a clone the
# sandbox can read.
scrub_remotes() {
  local remote
  for remote in $(session_git -C "$1" remote); do session_git -C "$1" remote remove "$remote" || return 1; done
  session_git -C "$1" config --remove-section credential 2>/dev/null || true
  [[ -z "$(session_git -C "$1" config --local --get-regexp '^(url|remote|credential|http)\.' || true)" ]]
}
# write_verifier_gitconfig <path>: identity only, no user excludes or
# attributes files (their default XDG paths are not readable).
write_verifier_gitconfig() {
  local name email
  name="$(git config --global --get user.name | tr -d '\n\\"' || true)"
  email="$(git config --global --get user.email | tr -d '\n\\"' || true)"
  { printf '[user]\n\tname = %s\n\temail = %s\n' "${name:-harness-verifier}" "${email:-verifier@invalid}"
    printf '[core]\n\texcludesFile = /dev/null\n\tattributesFile = /dev/null\n'; } > "$1"
}
run_sandboxed_verifier() {
  local candidate="$1" id="$2" trusted="$3" verifier="$4" source vtmp vcopy profile help_output name rc=0
  shift 4
  local -a args=("$@") venv=()
  [[ -x "$SANDBOX_EXEC" ]] || { echo "harness-session: refused: $SANDBOX_EXEC is required to verify a headless session" >&2; rm -rf "$trusted"; return 2; }
  source="$(<"$(session_dir "$id")/source-root")" || { rm -rf "$trusted"; return 2; }
  vtmp="$(mktemp -d /private/tmp/verifier-tmp.XXXXXX)" || { rm -rf "$trusted"; return 2; }
  vcopy="$(mktemp -d "$(state_home)/verifier-copy.XXXXXX")" || { rm -rf "$trusted" "$vtmp"; return 2; }
  { vtmp="$(phys "$vtmp")" && vcopy="$(phys "$vcopy")" && cp -R "$candidate/." "$vcopy/" \
      && scrub_remotes "$vcopy" && scrub_remotes "$trusted" && write_verifier_gitconfig "$vtmp/gitconfig" \
      && profile="$(verifier_sandbox_profile "$vcopy" "$vtmp" "${HOME:?}" "$source" "$trusted")"; } \
    || { rm -rf "$trusted" "$vtmp" "$vcopy"; return 2; }
  venv=(HOME="$HOME" PATH="${PATH:-/usr/bin:/bin}" TMPDIR="$vtmp" GIT_CONFIG_GLOBAL="$vtmp/gitconfig")
  if [[ -n "${INTEGRATION_INPUT_DIR:-}" ]]; then
    [[ "${HARNESS_DELIVER_TEST_JOBS:-}" =~ ^([1-9]|1[0-6])$ ]] || return 2
    venv+=(HARNESS_DELIVER_TEST_JOBS="$HARNESS_DELIVER_TEST_JOBS" HARNESS_TEST_CASE_JOBS_LIMIT="$HARNESS_DELIVER_TEST_JOBS")
  fi
  [[ -z "${LANG:-}" ]] || venv+=(LANG="$LANG")
  for name in $(compgen -e); do [[ "$name" != LC_* ]] || venv+=("$name=${!name}"); done
  help_output="$(cd "$vcopy" && verifier_exec env -i "${venv[@]}" "$SANDBOX_EXEC" -p "$profile" bash "$verifier" --help 2>/dev/null || true)"
  if grep -q -- '--contract-checked' <<< "$help_output"; then
    args+=(--contract-checked 'broker candidate cascade verified')
  fi
  (cd "$vcopy" && verifier_exec env -i "${venv[@]}" TEST_HARNESS_DIR="$vcopy" HARNESS_POST_COMMIT_PUSH=0 HARNESS_POST_COMMIT_CODEX_SYNC=0 \
    HARNESS_RAG_ENABLED=0 HARNESS_SESSION_BACKLINK=0 "$SANDBOX_EXEC" -p "$profile" bash "$verifier" "${args[@]}") || rc=$?
  rm -rf "$trusted" "$vtmp" "$vcopy"
  return "$rc"
}
write_manifest_pathset() {
  local manifest="$1" output="$2" kind mode oid path
  : > "$output"
  while IFS= read -r -d '' kind && IFS= read -r -d '' mode && IFS= read -r -d '' oid && IFS= read -r -d '' path; do
    printf '%s\0%s\0' "$kind" "$path" >> "$output"
  done < "$manifest"
}
write_index_pathset() {
  local repo="$1" base="$2" output="$3" status path kind
  : > "$output"
  while IFS= read -r -d '' status && IFS= read -r -d '' path; do
    kind=F; [[ "$status" == D* ]] && kind=D
    printf '%s\0%s\0' "$kind" "$path" >> "$output"
  done < <(git -C "$repo" diff --cached --name-status --no-renames -z "$base")
}
integrate_unlocked() {
  local id="$1" dir input source remote attempt candidate current delivered readback kind mode hash path actual_mode actual_type actual_oid actual_path entry
  dir="$(session_dir "$id")"; source="$(<"$dir/source-root")"
  input="${INTEGRATION_INPUT_DIR:-$dir}"
  remote="$(git -C "$source" remote get-url origin)"
  [[ "$remote" == "$(<"$dir/remote-url")" ]] || { transition "$id" INTEGRATING "$(field "$dir/journal" identity)"; transition "$id" CONFLICT "$(field "$dir/journal" identity)"; return 2; }
  transition "$id" INTEGRATING "$(field "$dir/journal" identity)"
  verify_submission_identity "$dir" || { transition "$id" CONFLICT "$(field "$dir/journal" identity)"; return 2; }
  for attempt in 1 2; do
    integration_phase preparing || return $?
    candidate="$(mktemp -d "$(state_home)/candidate.XXXXXX")"
    git clone -q "$remote" "$candidate"
    git -C "$candidate" checkout -q main
    current="$(git -C "$candidate" rev-parse HEAD)"
    if ! git -C "$candidate" apply --3way --index "$input/submission.patch"; then rm -rf "$candidate"; transition "$id" CONFLICT; return 3; fi
    write_manifest_pathset "$input/manifest" "$dir/.submitted-pathset"
    write_index_pathset "$candidate" "$current" "$dir/.candidate-pathset"
    cmp -s "$dir/.submitted-pathset" "$dir/.candidate-pathset" || { rm -rf "$candidate"; transition "$id" CONFLICT "$(field "$dir/journal" identity)"; return 3; }
    integration_phase verifying || return $?
    run_repository_verifier "$candidate" "$id" "$remote" "$current" || {
      rm -rf "$candidate"; transition "$id" CONFLICT "$(field "$dir/journal" identity)"
      # Headless: exit 9, the verifier rejected the candidate (not a merge conflict).
      if integration_headless "$id"; then return 9; fi
      return 3
    }
    verify_submission_identity "$dir" || { rm -rf "$candidate"; transition "$id" CONFLICT "$(field "$dir/journal" identity)"; return 2; }
    [[ "${HARNESS_SESSION_TEST_CRASH_BEFORE_PUSH:-0}" == 1 ]] && kill -9 "$BASHPID"
    # Headless: no hooks, fsmonitor or external drivers on the broker's own
    # commit and push (the verifier never touched this dir; belt and braces).
    local -a cgit=(git)
    if integration_headless "$id"; then cgit=(session_git); fi
    local message="" text="" rc=0
    local -a cmsg=(-m "harness session $id")
    # A launcher-written message (headless runs) plus the session trailer.
    if [[ -f "$input/commit-message" && ! -L "$input/commit-message" ]] && text="$(<"$input/commit-message")" && [[ -n "$text" ]] \
        && message="$(mktemp "$(state_home)/commit-message.XXXXXX")"; then
      printf '%s\n\nHarness-Session: %s\n' "$text" "$id" > "$message" && cmsg=(--cleanup=whitespace -F "$message")
    fi
    "${cgit[@]}" -C "$candidate" -c user.name=harness-broker -c user.email=broker@invalid commit -q "${cmsg[@]}" || rc=$?
    [[ -z "$message" ]] || rm -f "$message"
    [[ "$rc" == 0 ]] || { rm -rf "$candidate"; return "$rc"; }
    delivered="$(git -C "$candidate" rev-parse HEAD)"
    write_candidate_manifest "$candidate" "$delivered" "$input/manifest" "$dir/.candidate-manifest" || { rm -rf "$candidate"; transition "$id" CONFLICT "$(field "$dir/journal" identity)"; return 2; }
    printf '%s\n' "$delivered" > "$dir/pending-sha"
    # Durable write-ahead proof must precede any remote side effect. A queue
    # publishing receipt without this evidence remains indeterminate on reboot.
    fsync_paths "$dir/.candidate-manifest" "$dir/pending-sha" "$dir/journal" "$dir" || return 4
    git -C "$candidate" fetch -q origin main
    [[ "$(git -C "$candidate" rev-parse origin/main)" == "$current" ]] || { rm -rf "$candidate"; continue; }
    integration_phase publishing || return $?
    if "${cgit[@]}" -C "$candidate" push -q origin HEAD:refs/heads/main; then
      [[ "${HARNESS_SESSION_TEST_CRASH_BEFORE_ACK:-0}" == 1 ]] && kill -9 "$BASHPID"
      printf '%s\n' "$delivered" > "$dir/push-ack"
      [[ "${HARNESS_SESSION_TEST_CRASH_AFTER_PUSH:-0}" == 1 ]] && kill -9 "$BASHPID"
      if [[ "${HARNESS_SESSION_TEST_POST_PUSH_DELAY:-}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then sleep "$HARNESS_SESSION_TEST_POST_PUSH_DELAY"; fi
      # A transport/readback outage after a successful push is indeterminate,
      # not a content conflict. Keep INTEGRATING so recover can prove the
      # pending commit from fresh remote history without resubmitting it.
      [[ "${HARNESS_SESSION_TEST_READBACK_FAIL:-0}" == 1 ]] && { rm -rf "$candidate"; return 4; }
      readback="$(mktemp -d "$(state_home)/readback.XXXXXX")"
      integration_phase readback || return $?
      git clone -q "$remote" "$readback"
      git -C "$readback" merge-base --is-ancestor "$delivered" HEAD || { rm -rf "$candidate" "$readback"; transition "$id" CONFLICT "$(field "$dir/journal" identity)"; return 4; }
      while IFS= read -r -d '' kind && IFS= read -r -d '' mode && IFS= read -r -d '' hash && IFS= read -r -d '' path; do
        if [[ "$kind" == D ]]; then git -C "$readback" cat-file -e "$delivered:$path" 2>/dev/null && { rm -rf "$candidate" "$readback"; transition "$id" CONFLICT "$(field "$dir/journal" identity)"; return 4; }; continue; fi
        actual_mode=""; actual_oid=""; entry="$(git -C "$readback" ls-tree "$delivered" -- ":(literal)$path")"; [[ -z "$entry" ]] || read -r actual_mode actual_type actual_oid actual_path <<< "$entry"
        [[ "$actual_mode" == "$mode" && "$actual_oid" == "$hash" ]] || { rm -rf "$candidate" "$readback"; transition "$id" CONFLICT "$(field "$dir/journal" identity)"; return 4; }
      done < "$dir/.candidate-manifest"
      printf '%s\n' "$delivered" > "$dir/delivered-sha"
      cp -p "$dir/.candidate-manifest" "$dir/delivered-manifest"
      fsync_paths "$dir/delivered-sha" "$dir/delivered-manifest" "$dir" || return 4
      rm -rf "$candidate" "$readback"; transition "$id" DELIVERED "$(field "$dir/journal" identity)"
      fsync_paths "$dir/journal" "$dir" || return 4
      return 0
    fi
    rm -rf "$candidate"
  done
  transition "$id" CONFLICT "$(field "$dir/journal" identity)"
  return 5
}
integrate() {
  if integration_queue_enabled; then
    local accepted job
    accepted="$(integration_queue enqueue "$1")" || return $?
    job="$(printf '%s' "$accepted" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')" || return 2
    integration_queue wait "$job"
  else
    with_lock integrate_unlocked "$@"
  fi
}
close_session() {
  local id="$1" mode="${2:-wait}" dir state root
  [[ "$mode" != enqueue ]] || integration_queue_enabled || { echo 'harness-session: queue is not enabled for this pool' >&2; return 2; }
  dir="$(session_dir "$id")"; state="$(field "$dir/journal" state)"; root="$(<"$dir/session-root")"
  if integration_queue_enabled && [[ "$state" == SUBMITTED || "$state" == INTEGRATING || "$state" == DELIVERED ]]; then
    if [[ "$mode" == enqueue ]]; then integration_queue enqueue "$id"
    else integrate "$id"; fi
    return
  fi
  use_session_git "$id" || return $?
  if [[ "$state" == OPEN ]]; then
    local rc=0
    session_has_changes "$root" "$(<"$dir/base-sha")" || rc=$?
    [[ "$rc" != 2 ]] || { echo 'harness-session: refused: could not read the session changes' >&2; return 2; }
    [[ "$rc" != 1 ]] || { transition "$id" CLOSED; return 0; }
  fi
  [[ "$state" == OPEN ]] && submit "$id"
  [[ "$(field "$dir/journal" state)" == SUBMITTED ]] || { echo "cannot close $state session" >&2; return 2; }
  if [[ "$mode" == enqueue ]]; then integration_queue enqueue "$id"; return; fi
  integrate "$id"
}
# write_discard_patch <id> <dir>: the session work tree against its base as a
# binary patch in <dir>/discarded.patch, fsynced. It stages through a copy of
# the index submit would stage (the session index, or the trusted index for
# headless sessions), so force-added ignored files and `rm --cached` removals
# come out as submit would deliver them, untracked files are added, and
# neither the session index nor the session .git of a headless session is
# written. Completeness rests on every git step's exit status and on the
# rename that publishes the file only after git wrote it.
write_discard_patch() {
  local id="$1" dir="$2" root base tmp patch index rc=0
  root="$(<"$dir/session-root")" && base="$(<"$dir/base-sha")" || return 1
  [[ -d "$root" && ! -L "$root" ]] || return 1
  ROOT_GIT_DIR=""
  if [[ -e "$dir/headless" ]]; then
    [[ -d "$dir/trusted.git" && ! -L "$dir/trusted.git" ]] || return 1
    ROOT_GIT_DIR="$dir/trusted.git"; index="$ROOT_GIT_DIR/index"
  else
    index="$(session_git -C "$root" rev-parse --path-format=absolute --git-path index)" || return 1
  fi
  [[ -f "$index" && ! -L "$index" ]] || return 1
  # Runs in discard's lease subshell: an interrupt still removes the temps
  # (globals, so the trap sees them; a renamed patch is no longer there).
  DISCARD_TEMPS=()
  trap 'rm -rf ${DISCARD_TEMPS[@]+"${DISCARD_TEMPS[@]}"}' EXIT
  trap 'exit 130' INT TERM HUP
  tmp="$(mktemp -d "$(state_home)/discard.XXXXXX")" || return 1
  DISCARD_TEMPS+=("$tmp")
  patch="$(mktemp "$dir/.discarded.patch.XXXXXX")" || { rm -rf "$tmp"; return 1; }
  DISCARD_TEMPS+=("$patch")
  export GIT_INDEX_FILE="$tmp/index"
  # Fixed output format: no user color, prefix or external diff setting
  # changes the patch.
  { cp "$index" "$GIT_INDEX_FILE" && root_git "$root" add -A -- . \
      && root_git "$root" diff --no-color --no-ext-diff --no-textconv --src-prefix=a/ --dst-prefix=b/ \
        --ignore-submodules=all --cached --binary "$base" -- . "${EXCLUDED_PATHSPEC[@]}" > "$patch"
  } || rc=1
  unset GIT_INDEX_FILE
  rm -rf "$tmp"
  [[ "$rc" == 0 ]] && mv -f "$patch" "$dir/discarded.patch" || { rm -f "$patch"; return 1; }
}
# fsync_paths <path>...: flush files and directories to stable storage:
# F_FULLFSYNC (51, macOS: also the drive cache), else plain fsync.
fsync_paths() {
  /usr/bin/perl -MIO::Handle -e 'for (@ARGV) { open(my $f, "<", $_) or die "$_: $!\n"; fcntl($f, 51, 0) or $f->sync or die "$_: $!\n"; }' "$@"
}
# discard <id>: retire an ABANDONED or CONFLICT session whose work will not be
# delivered. Its work is kept first as discarded.patch; then DISCARDED, which
# gc retires like CLOSED and DELIVERED.
discard_unlocked() {
  local id="$1" dir state
  valid_id "$id" || { echo "harness-session: refused: invalid session UUID: $id" >&2; return 2; }
  dir="$(session_dir "$id")"
  [[ -d "$dir" && ! -L "$dir" ]] && journal_valid "$dir/journal" || { echo "harness-session: refused: session $id has no valid record" >&2; return 2; }
  state="$(field "$dir/journal" state)"
  case "$state" in
    ABANDONED|CONFLICT) ;;
    OPEN) echo "harness-session: refused: session $id is OPEN; use exit or close" >&2; return 2 ;;
    SUBMITTED|INTEGRATING) echo "harness-session: refused: session $id is $state and may be indeterminate; run harness-session recover" >&2; return 2 ;;
    *) echo "harness-session: refused: session $id is $state; only ABANDONED or CONFLICT sessions can be discarded" >&2; return 2 ;;
  esac
  [[ -f "$dir/runtime.lock" && ! -L "$dir/runtime.lock" ]] || { echo "harness-session: refused: session $id has no runtime lease record" >&2; return 2; }
  (
    exec 8>"$dir/runtime.lock"
    /usr/bin/lockf -s -t 0 8 || { echo "harness-session: refused: session $id is in use (its runtime lease is held)" >&2; exit 2; }
    # Reread under the lease: a launcher may have reopened it meanwhile.
    state="$(field "$dir/journal" state)"
    [[ "$state" == ABANDONED || "$state" == CONFLICT ]] || { echo "harness-session: refused: session $id is now $state" >&2; exit 2; }
    { write_discard_patch "$id" "$dir" && date -u +%Y-%m-%dT%H:%M:%SZ > "$dir/discarded-at" \
        && fsync_paths "$dir/discarded.patch" "$dir/discarded-at" "$dir"; } || {
      rm -f "$dir/discarded.patch" "$dir/discarded-at"
      echo "harness-session: refused: could not write the discard patch for session $id; nothing changed" >&2
      exit 2
    }
    transition "$id" DISCARDED "$(field "$dir/journal" identity)"
  )
}
case "${1:-}" in
  queue) shift; integration_queue "$@" ;;
  wait) shift; integration_queue wait "$@" ;;
  _queue-execute)
    shift; [[ $# -eq 2 ]] || exit 2
    # Never trust inherited INPUT_DIR. Only the job's current inherited leases
    # and pinned runtime can authorize entry past the old compatibility gate.
    unset INTEGRATION_INPUT_DIR
    INTEGRATION_INPUT_DIR="$(integration_queue authorize "$1" "$2" --home "${HARNESS_INTEGRATION_HOME:?}")" || exit $?
    export INTEGRATION_INPUT_DIR
    integration_queue guardian --home "$HARNESS_INTEGRATION_HOME" >/dev/null &
    integration_queue await-guardian --home "$HARNESS_INTEGRATION_HOME" || exit $?
    case "$1" in
      gc) gc_unlocked; exit $? ;;
      recover) recover_unlocked "$2"; exit $? ;;
      discard) discard_unlocked "$2"; exit $? ;;
      integrate) ;;
      *) exit 2 ;;
    esac
    queued_state="$(field "$(session_dir "$2")/journal" state)"
    if [[ "$queued_state" == INTEGRATING ]]; then
      integration_queue recovery-check --home "$HARNESS_INTEGRATION_HOME" || exit $?
      recover_unlocked "$2"; queued_state="$(field "$(session_dir "$2")/journal" state)"
    fi
    if [[ "$queued_state" == DELIVERED ]]; then exit 0; fi
    [[ "$queued_state" == SUBMITTED ]] || exit 2
    integrate_unlocked "$2"
    ;;
  create)
    shift
    if [[ "${1:-}" == --reserved ]]; then
      reserved_id="${2:-}"
      shift 2
      if [[ "${1:-}" == --local ]]; then shift; [[ $# -eq 1 ]] || exit 2; create "$1" 1 "$reserved_id"; else [[ $# -eq 1 ]] || exit 2; create "$1" 0 "$reserved_id"; fi
    else
      creation_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/harness_session_creation.py"
      [[ -f "$creation_script" && ! -L "$creation_script" ]] || { echo 'harness-session: creation helper unavailable' >&2; exit 2; }
      if [[ "${1:-}" == --local ]]; then
        shift; [[ $# -eq 1 ]] || exit 2
        exec python3 -I "$creation_script" legacy-create --engine "$0" --state "$(state_home)" --local "$1"
      fi
      [[ $# -eq 1 ]] || exit 2
      exec python3 -I "$creation_script" legacy-create --engine "$0" --state "$(state_home)" "$1"
    fi
    ;;
  discard)
    shift; [[ $# -eq 1 ]] || exit 2
    if integration_queue_enabled; then integration_queue control discard "$1" --wait
    else with_lock discard_unlocked "$1"; fi ;;
  resolve-run-dir) shift; [[ $# -eq 3 ]] || exit 2; resolve_run_dir "$@" ;;
  resume) shift; [[ $# -eq 2 ]] || exit 2; resume_session "$1" "$2" ;;
  transition)
    shift
    # DISCARDED needs the lease and the evidence that only discard provides.
    [[ "${2:-}" != DISCARDED ]] || { echo 'harness-session: refused: use harness-session discard to discard a session' >&2; exit 2; }
    transition "$@" ;;
  exit) shift; exit_session "$1" ;;
  recover) shift; recover "$1" ;;
  list) list ;;
  heartbeat) shift; heartbeat_session "$1" ;;
  report)
    shift
    # Homebrew exposes this script through a bin symlink; modules live beside
    # its target. Bound link traversal so a concurrent broken install fails.
    report_script="${BASH_SOURCE[0]}"
    for report_hop in {1..40}; do
      report_dir="$(cd -P -- "$(dirname -- "$report_script")" && pwd -P)" || exit 2
      report_script="$report_dir/$(basename -- "$report_script")"
      [[ -L "$report_script" ]] || break
      report_script="$(readlink "$report_script")" || exit 2
      case "$report_script" in
        /*) ;;
        *) report_script="$report_dir/$report_script" ;;
      esac
    done
    [[ -f "$report_script" && ! -L "$report_script" ]] || {
      echo 'harness-session: cannot resolve report entry' >&2; exit 2;
    }
    report_dir="$(cd -P -- "$(dirname -- "$report_script")" && pwd -P)" || exit 2
    [[ -f "$report_dir/harness-common.sh" && -r "$report_dir/harness-common.sh" \
        && -f "$report_dir/harness_session_report.py" && -r "$report_dir/harness_session_report.py" ]] || {
      echo 'harness-session: report modules unavailable' >&2; exit 2;
    }
    source "$report_dir/harness-common.sh"
    report_python="$(harness_python3_resolve)" || exit $?
    exec "$report_python" -I "$report_dir/harness_session_report.py" "$@" ;;
  gc)
    shift
    HARNESS_GC_OWNER_FILTER="" HARNESS_GC_SOURCE_FILTER=""
    if [[ $# -gt 0 ]]; then
      [[ $# -eq 4 && "$1" == --owner && "$3" == --source-root ]] && valid_id "$2" || exit 2
      HARNESS_GC_OWNER_FILTER="$2"; HARNESS_GC_SOURCE_FILTER="$4"
      [[ -d "$4" && ! -L "$4" ]] || exit 2
    fi
    gc_sessions ;;
  reserve-hold) shift; [[ $# -eq 5 ]] || exit 2; reserve_hold "$@" ;;
  release-hold) shift; [[ $# -eq 2 ]] || exit 2; release_hold "$@" ;;
  sandbox-check) shift; [[ $# -eq 1 ]] || exit 2; sandbox_check "$1" ;;
  codex-sandbox-profile) shift; [[ $# -eq 8 ]] || exit 2; codex_sandbox_profile "$@" ;;
  verifier-sandbox-profile) shift; [[ $# -eq 5 ]] || exit 2; verifier_sandbox_profile "$@" ;;
  submit) shift; submit "$1" ;;
  integrate) shift; [[ $# -eq 1 ]] || exit 2; integrate "$1" ;;
  close)
    shift; close_mode=wait
    if [[ "${1:-}" == --enqueue || "${1:-}" == --wait ]]; then close_mode="${1#--}"; shift; fi
    [[ $# -eq 1 ]] || exit 2; close_session "$1" "$close_mode" ;;
  *) echo 'usage: harness-session {create|resolve-run-dir|resume|transition|exit|recover|list|heartbeat|report|gc|reserve-hold|release-hold|submit|integrate|close [--enqueue|--wait]|queue|wait|discard|sandbox-check}' >&2; exit 2 ;;
esac
