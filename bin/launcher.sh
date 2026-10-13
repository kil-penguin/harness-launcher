#!/usr/bin/env bash
# harness-launcher — TUI for picking runtime/provider/mode/effort.
# Invoked by bin/aliases.zsh when user runs `<prefix>` with no shortcut args.
# Required env: HARNESS_DIR, HARNESS_NAME. Native Codex also receives the
# HARNESS_PREFIX forwarded by aliases.zsh.
#
# Design (v3, launchpad):
#   - The top screen is a launchpad: recent complete launch configs (history)
#     plus one "New …" composer entry per installed runtime, fuzzy-searchable
#     under gum. Picking a history row replays it through the same assembly
#     path as a fresh config, so every launch revalidates gateways/homes/MCP.
#   - Choice collection and command assembly are separate: menus only fill
#     CHOICE_* variables; assembly/exec reads them in one place per runtime.
#   - Model/effort/labels come from harness-common.sh (harness_mode_*), the
#     same table the shortcut path uses — labels cannot drift from behavior.
#   - Esc/cancel = one step back everywhere; at the launchpad it exits.
#     Ctrl-C exits anywhere.

HARNESS_DIR="${HARNESS_DIR:?HARNESS_DIR required}"
HARNESS_NAME="${HARNESS_NAME:?HARNESS_NAME required}"

# Bind the config owner before evaluating any isolated launcher configuration.
LAUNCHER_BIN_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -n "${HARNESS_SOURCE_ROOT:-}" ] && [ -n "${HARNESS_SESSION_ID:-}" ]; then
  bound_root="$("$LAUNCHER_BIN_DIR/session-isolation.sh" resolve-run-dir "$HARNESS_SOURCE_ROOT" "$HARNESS_SESSION_ID" "$HARNESS_SOURCE_ROOT")" || exit $?
  [ "$(cd "$HARNESS_DIR" 2>/dev/null && pwd -P)" = "$bound_root" ] || exit 2
fi

# The launcher is a native Codex entrypoint as well as a TUI. Load only its
# trusted harness configuration so global MCP selection cannot inherit from the
# caller when the config omits it.
unset HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST HARNESS_CODEX_APPS_ALLOWLIST HARNESS_CODEX_SLACK_APPS HARNESS_MCP_SURFACE_POLICY
# shellcheck source=/dev/null
. "$HARNESS_DIR/config/launcher.env"

LAUNCHER_BIN_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=harness-common.sh
. "$LAUNCHER_BIN_DIR/harness-common.sh"
LAUNCHER_BIN_DIR="$(harness_launcher_stable_dir "$LAUNCHER_BIN_DIR")"

MCP_SURFACE_POLICY="$(harness_mcp_surface_policy_resolve "${HARNESS_MCP_SURFACE_POLICY:-}")" || exit $?

prepare_codex_global_mcp_allowlist() {
  local raw="${HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST:-}" normalized
  [ -n "$raw" ] || { unset HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST; return 0; }
  normalized="$(harness_codex_global_mcp_allowlist_normalize "$raw")" || return $?
  [ -n "$normalized" ] && export HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="$normalized" \
    || unset HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST
}

prepare_codex_apps_allowlist() {
  export HARNESS_CODEX_SLACK_APPS="${HARNESS_CODEX_SLACK_APPS:-}"
  local raw="${HARNESS_CODEX_APPS_ALLOWLIST:-}" normalized
  [ -n "$raw" ] || { unset HARNESS_CODEX_APPS_ALLOWLIST; return 0; }
  normalized="$(harness_codex_apps_allowlist_normalize "$raw")" || return $?
  [ -n "$normalized" ] && export HARNESS_CODEX_APPS_ALLOWLIST="$normalized" \
    || unset HARNESS_CODEX_APPS_ALLOWLIST
}

if [ -n "${HARNESS_RUN_DIR:-}" ]; then
  if [ -n "${HARNESS_SOURCE_ROOT:-}" ] && [ -n "${HARNESS_SESSION_ID:-}" ]; then
    HARNESS_RUN_DIR="$("$LAUNCHER_BIN_DIR/session-isolation.sh" resolve-run-dir "$HARNESS_SOURCE_ROOT" "$HARNESS_SESSION_ID" "$HARNESS_RUN_DIR")" || exit $?
  else
    HARNESS_RUN_DIR="$(harness_resolve_run_dir "$HARNESS_DIR" "$HARNESS_RUN_DIR")" || exit $?
  fi
else
  harness_resolve_run_dir "$HARNESS_DIR" "$HARNESS_DIR" >/dev/null || exit $?
  HARNESS_RUN_DIR="$HARNESS_DIR"
fi
cd "$HARNESS_RUN_DIR" || exit 1

# Launch history (launchpad rows): one tab-separated KEY=VALUE line per entry,
# newest first, deduped by config identity (TS/SUMMARY excluded).
HISTORY_FILE="$HARNESS_DIR/.harness/launcher-history"
HISTORY_MAX=8
# Pre-0.12 single-entry plan file — migrated into the history on startup.
LEGACY_PLAN_FILE="$HARNESS_DIR/.harness/launcher-last"
PROBE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/harness-launcher-probe.XXXXXX")"
trap 'rm -rf "$PROBE_DIR"' EXIT

# ---------------------------------------------------------------------------
# menu <header> <option>...
#   gum: Esc (rc 1) → return 1 ("back"), Ctrl-C (rc 130) → exit.
#   fallback: numbered list, reprompts until valid; 'q' or EOF → return 1.
MENU_RESULT=""
BREADCRUMB=""
menu() {
  local header="$1"; shift
  local options=("$@")
  MENU_RESULT=""
  [ -n "$BREADCRUMB" ] && header="$BREADCRUMB ▸ $header"

  if command -v gum >/dev/null 2>&1 && : </dev/tty 2>/dev/null; then
    local result rc
    result=$(gum choose \
      --header "$header" \
      --cursor "❯ " \
      --select-if-one \
      --height $(( ${#options[@]} + 1 )) \
      "${options[@]}")
    rc=$?
    [ $rc -eq 130 ] && exit 0
    [ $rc -ne 0 ] && return 1
    MENU_RESULT="$result"
    return 0
  fi

  local i choice
  while true; do
    echo ""
    echo "$header"
    for i in "${!options[@]}"; do
      printf "  %d) %s\n" "$((i + 1))" "${options[$i]}"
    done
    if ! read -rp "> " choice; then
      return 1  # EOF → back
    fi
    [ "$choice" = "q" ] && return 1
    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#options[@]}" ]; then
      MENU_RESULT="${options[$((choice - 1))]}"
      return 0
    fi
    echo "  (1-${#options[@]} 또는 q=뒤로)"
  done
}

# menu_filter <header> <option>...
#   Launchpad top screen: fuzzy-searchable under gum (type to filter, Enter to
#   launch). No-gum fallback is the same numbered menu as everywhere else.
menu_filter() {
  local header="$1"; shift
  local options=("$@")
  MENU_RESULT=""
  if command -v gum >/dev/null 2>&1 && : </dev/tty 2>/dev/null; then
    local result rc
    result=$(printf '%s\n' "${options[@]}" | gum filter \
      --header "$header" \
      --placeholder "타이핑해서 검색 · Enter 실행" \
      --indicator "❯" \
      --height $(( ${#options[@]} + 4 )))
    rc=$?
    [ $rc -eq 130 ] && exit 0
    [ $rc -ne 0 ] && return 1
    MENU_RESULT="$result"
    return 0
  fi
  menu "$header" "${options[@]}"
}

# ---------------------------------------------------------------------------
# Gateway probes — fired in the background at startup so the provider menu
# never blocks on them; results are read from status files when needed.
KIRO_GATEWAY_CONFIGURED=false
CODEX_GATEWAY_CONFIGURED=false
prewarm_probes() {
  if [ -f "$HARNESS_DIR/config/.local/kiro-gateway.env" ]; then
    KIRO_GATEWAY_CONFIGURED=true
    (
      . "$HARNESS_DIR/config/.local/kiro-gateway.env"
      if harness_probe_health "${KIRO_GATEWAY_URL:-}"; then echo up; else echo down; fi > "$PROBE_DIR/kiro"
    ) &
  fi
  if [ -f "$HARNESS_DIR/config/.local/codex-gateway.env" ]; then
    CODEX_GATEWAY_CONFIGURED=true
    (
      . "$HARNESS_DIR/config/.local/codex-gateway.env"
      if harness_probe_health "${CODEX_GATEWAY_URL:-}"; then echo up; else echo down; fi > "$PROBE_DIR/codex"
    ) &
  fi
}

# probe_status <kiro|codex> → up | down | ? (probe still running)
probe_status() {
  local f="$PROBE_DIR/$1"
  if [ -s "$f" ]; then cat "$f"; else echo "?"; fi
}

# probe_wait <kiro|codex> — bounded wait (probe itself times out in 2s).
probe_wait() {
  local f="$PROBE_DIR/$1" n=0
  while [ ! -s "$f" ] && [ $n -lt 30 ]; do sleep 0.1; n=$((n + 1)); done
  probe_status "$1"
}

probe_mark() {
  case "$(probe_status "$1")" in
    up) echo "🟢" ;; down) echo "🔴" ;; *) echo "⚪" ;;
  esac
}

# ---------------------------------------------------------------------------
# Launch-history persistence (launchpad rows).

# plan_apply_field <key> <value> — one field into its CHOICE_* variable.
plan_apply_field() {
  case "$1" in
    RUNTIME)       CHOICE_RUNTIME="$2" ;;
    SUMMARY)       PLAN_SUMMARY="$2" ;;
    PROVIDER)      CHOICE_PROVIDER="$2" ;;
    SESSION)       CHOICE_SESSION="$2" ;;
    MODE)          CHOICE_MODE="$2" ;;
    C_MODEL)       CHOICE_C_MODEL="$2" ;;
    C_EFFORT)      CHOICE_C_EFFORT="$2" ;;
    PERM)          CHOICE_PERM="$2" ;;
    MCP_SURFACE)   CHOICE_MCP_SURFACE="$2" ;;
    CHROME)        CHOICE_CHROME="$2" ;;
    HAPPY)         CHOICE_HAPPY="$2" ;;
    CODEX_PROFILE) CHOICE_CODEX_PROFILE="$2" ;;
    CODEX_SURFACE) CHOICE_CODEX_SURFACE="$2" ;;
    CODEX_SAFETY)  CHOICE_CODEX_SAFETY="$2" ;;
    CODEX_CONTEXT) CHOICE_CODEX_CONTEXT="$2" ;;
    KIRO_TRUST)    CHOICE_KIRO_TRUST="$2" ;;
  esac
}

# history_ident <line> — config identity: every field except TS/SUMMARY.
history_ident() {
  printf '%s\n' "$1" | awk -F'\t' \
    '{out=""; for (i=1; i<=NF; i++) if ($i !~ /^TS=/ && $i !~ /^SUMMARY=/) out = out $i "\t"; print out}'
}

# history_current_line <ts> — canonical serializer for current CHOICE_* state.
history_current_line() {
  local ts="$1" line
  local fields=(
    "TS=$ts" "SUMMARY=$PLAN_SUMMARY" "RUNTIME=$CHOICE_RUNTIME"
    "PROVIDER=$CHOICE_PROVIDER" "SESSION=$CHOICE_SESSION" "MODE=$CHOICE_MODE"
    "C_MODEL=$CHOICE_C_MODEL" "C_EFFORT=$CHOICE_C_EFFORT" "PERM=$CHOICE_PERM"
    "MCP_SURFACE=$CHOICE_MCP_SURFACE" "CHROME=$CHOICE_CHROME" "HAPPY=$CHOICE_HAPPY"
    "CODEX_PROFILE=$CHOICE_CODEX_PROFILE" "CODEX_SURFACE=$CHOICE_CODEX_SURFACE"
    "CODEX_SAFETY=$CHOICE_CODEX_SAFETY" "CODEX_CONTEXT=$CHOICE_CODEX_CONTEXT"
    "KIRO_TRUST=$CHOICE_KIRO_TRUST"
  )
  line=$(printf '%s\t' "${fields[@]}"); line="${line%$'\t'}"
  printf '%s\n' "$line"
}

# history_save [ts] — prepend the current CHOICE_* config; an existing entry
# with the same identity moves to the top instead of duplicating.
history_save() {
  mkdir -p "$HARNESS_DIR/.harness"
  local ts="${1:-$(date +%s)}" line ident old tmp
  line="$(history_current_line "$ts")" || return $?
  ident=$(history_ident "$line")
  tmp="$HISTORY_FILE.tmp.$$"
  {
    printf '%s\n' "$line"
    if [ -f "$HISTORY_FILE" ]; then
      while IFS= read -r old; do
        [ -n "$old" ] || continue
        [ "$(history_ident "$old")" = "$ident" ] && continue
        printf '%s\n' "$old"
      done < "$HISTORY_FILE"
    fi
  } | head -n "$HISTORY_MAX" > "$tmp"
  mv "$tmp" "$HISTORY_FILE"
}

# history_field <line> <key> — extract one KEY=VALUE field's value.
history_field() {
  printf '%s\n' "$1" | tr '\t' '\n' | sed -n "s/^$2=//p" | head -1
}

# history_load_line <line> — fill CHOICE_* from one history entry. The line is
# captured when the launchpad menu is built, so a concurrent launcher rewriting
# the file while the menu is open cannot shift which config gets replayed.
history_load_line() {
  local line="$1" field key
  [ -n "$line" ] || return 1
  local fields=()
  IFS=$'\t' read -ra fields <<< "$line"
  for field in "${fields[@]}"; do
    key="${field%%=*}"
    plan_apply_field "$key" "${field#*=}"
  done
  [ -n "$CHOICE_RUNTIME" ]
}

plan_reset() {
  CHOICE_RUNTIME=""; CHOICE_PROVIDER="direct"; CHOICE_SESSION="new"
  CHOICE_MODE=""; CHOICE_C_MODEL=""; CHOICE_C_EFFORT=""
  CHOICE_PERM="default"; CHOICE_CHROME=0; CHOICE_HAPPY=0; CHOICE_MCP_SURFACE="full"
  CHOICE_CODEX_PROFILE="base"
  if harness_mcp_surface_policy_is_single_full "$MCP_SURFACE_POLICY"; then
    CHOICE_CODEX_SURFACE="full"
  else
    CHOICE_CODEX_SURFACE="default"
  fi
  CHOICE_CODEX_SAFETY="default"
  CHOICE_CODEX_CONTEXT="272k"
  CHOICE_KIRO_TRUST=0
  PLAN_SUMMARY=""
}

# Pre-0.12 `launcher-last` (multi-line KEY=VALUE) → one history entry, stamped
# with the old file's mtime so its relative age stays honest.
migrate_legacy_plan() {
  [ -f "$LEGACY_PLAN_FILE" ] || return 0
  if [ ! -f "$HISTORY_FILE" ]; then
    plan_reset
    local line ts
    while IFS= read -r line; do
      plan_apply_field "${line%%=*}" "${line#*=}"
    done < "$LEGACY_PLAN_FILE"
    if [ -n "$CHOICE_RUNTIME" ]; then
      ts=$(stat -f %m "$LEGACY_PLAN_FILE" 2>/dev/null || stat -c %Y "$LEGACY_PLAN_FILE" 2>/dev/null || date +%s)
      history_save "$ts"
    fi
    plan_reset
  fi
  rm -f "$LEGACY_PLAN_FILE"
}

# Canonicalize opt-in history before any launchpad labels are built. Validation
# and serialization finish in same-directory scratch files before atomic mv, so
# malformed input or replacement failure cannot alter the original bytes.
history_migration_fail() {
  local cause="$1" raw="$2" sorted="$3" tmp="$4"
  rm -f -- "$raw" "$sorted" "$tmp"
  echo "harness-launcher: history migration failed: $cause" >&2
  return 1
}

# Migration runs before provider collection loads gateway settings. Resolve a
# Codex-provider summary in a subshell so only its rendered label crosses back;
# secrets, unrelated variables, and any output from the env file stay isolated.
history_claude_summary() {
  if [ "$CHOICE_PROVIDER" != "codex" ]; then
    claude_summary
    return 0
  fi

  local summary
  summary="$(
    unset CODEX_CONTEXT_SUFFIX
    if [ -f "$HARNESS_DIR/config/.local/codex-gateway.env" ]; then
      . "$HARNESS_DIR/config/.local/codex-gateway.env" >/dev/null 2>&1 || exit $?
    fi
    claude_summary
    printf '%s\n' "$PLAN_SUMMARY"
  )" || return $?
  PLAN_SUMMARY="$summary"
}

history_migrate_single_full() {
  harness_mcp_surface_policy_is_single_full "$MCP_SURFACE_POLICY" || return 0
  [ -f "$HISTORY_FILE" ] || return 0

  local raw="$HISTORY_FILE.migrate.raw.$$"
  local sorted="$HISTORY_FILE.migrate.sorted.$$"
  local tmp="$HISTORY_FILE.migrate.$$"
  local line field key ts runtime canonical ident prior count=0 duplicate
  local read_rc
  local fields=() idents=()

  if ! : > "$raw"; then
    history_migration_fail "scratch setup failed" "$raw" "$sorted" "$tmp"
    return 1
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    plan_reset
    fields=()
    IFS=$'\t' read -ra fields <<< "$line"
    for field in "${fields[@]}"; do
      case "$field" in
        ?*=*) key="${field%%=*}"; plan_apply_field "$key" "${field#*=}" ;;
        *)
          history_migration_fail "malformed TSV field" "$raw" "$sorted" "$tmp"
          return 1 ;;
      esac
    done
    ts="$(history_field "$line" TS)"
    if ! [[ "$ts" =~ ^[0-9]+$ ]]; then
      history_migration_fail "invalid TS" "$raw" "$sorted" "$tmp"
      return 1
    fi
    runtime="$CHOICE_RUNTIME"
    case "$runtime" in
      claude)
        CHOICE_MCP_SURFACE="full"; CHOICE_CODEX_SURFACE="full"
        if ! history_claude_summary; then
          history_migration_fail "summary generation failed" "$raw" "$sorted" "$tmp"
          return 1
        fi ;;
      codex)
        CHOICE_MCP_SURFACE="full"; CHOICE_CODEX_SURFACE="full"
        codex_summary ;;
      kiro)
        CHOICE_CODEX_SURFACE="full"
        if [ "$CHOICE_MCP_SURFACE" != "light" ]; then
          CHOICE_MCP_SURFACE="full"
          kiro_summary
        fi ;;
      *)
        history_migration_fail "invalid runtime" "$raw" "$sorted" "$tmp"
        return 1 ;;
    esac
    if ! canonical="$(history_current_line "$ts")"; then
      history_migration_fail "row serialization failed" "$raw" "$sorted" "$tmp"
      return 1
    fi
    if ! printf '%s\n' "$canonical" >> "$raw"; then
      history_migration_fail "scratch write failed" "$raw" "$sorted" "$tmp"
      return 1
    fi
  done < "$HISTORY_FILE"
  read_rc=$?
  if [ "$read_rc" -ne 0 ]; then
    history_migration_fail "history read failed" "$raw" "$sorted" "$tmp"
    return 1
  fi

  if ! LC_ALL=C sort -t $'\t' -k1.4,1nr "$raw" > "$sorted"; then
    history_migration_fail "history sort failed" "$raw" "$sorted" "$tmp"
    return 1
  fi
  if ! : > "$tmp"; then
    history_migration_fail "target setup failed" "$raw" "$sorted" "$tmp"
    return 1
  fi
  while IFS= read -r line; do
    if ! ident="$(history_ident "$line")"; then
      history_migration_fail "identity generation failed" "$raw" "$sorted" "$tmp"
      return 1
    fi
    duplicate=false
    for prior in "${idents[@]}"; do
      [ "$prior" = "$ident" ] && { duplicate=true; break; }
    done
    $duplicate && continue
    if ! printf '%s\n' "$line" >> "$tmp"; then
      history_migration_fail "target write failed" "$raw" "$sorted" "$tmp"
      return 1
    fi
    idents+=("$ident")
    count=$((count + 1))
    if [ "$count" -ge "$HISTORY_MAX" ]; then
      break
    fi
  done < "$sorted"
  read_rc=$?
  if [ "$read_rc" -ne 0 ]; then
    history_migration_fail "sorted history read failed" "$raw" "$sorted" "$tmp"
    return 1
  fi

  if ! mv "$tmp" "$HISTORY_FILE"; then
    history_migration_fail "atomic replace failed" "$raw" "$sorted" "$tmp"
    return 1
  fi
  rm -f "$raw" "$sorted"
  plan_reset
}

# rel_time <epoch> — compact relative age for launchpad rows.
rel_time() {
  local diff=$(( $(date +%s) - ${1:-0} ))
  if [ "$diff" -lt 60 ]; then echo "방금 전"
  elif [ "$diff" -lt 3600 ]; then echo "$(( diff / 60 ))분 전"
  elif [ "$diff" -lt 86400 ]; then echo "$(( diff / 3600 ))시간 전"
  else echo "$(( diff / 86400 ))일 전"; fi
}

# ---------------------------------------------------------------------------
launch_banner() {
  # launch_banner <summary> <cmd>...
  # Also the last stop before exec: the EXIT trap dies with exec, so the
  # probe scratch dir must be removed here.
  local summary="$1"; shift
  rm -rf "$PROBE_DIR"
  stty sane 2>/dev/null
  echo ""
  echo "▶ $HARNESS_NAME · $summary"
  echo "  $*"
  echo ""
}

# codex_profile_intent <profile> — operational intent shown in the profile menu.
codex_profile_intent() {
  case "$1" in
    base) printf '%s\n' 'Everyday · Recommended' ;;
    sol)  printf '%s\n' 'Stronger · slower' ;;
    astra) printf '%s\n' 'Frontier · opt-in' ;;
    rich) printf '%s\n' 'Deep · slowest' ;;
    fast) printf '%s\n' 'Quick · shallow' ;;
    plan) printf '%s\n' 'Planning · deep' ;;
    *)    printf '%s\n' 'Custom' ;;
  esac
}

# codex_profile_label <profile> — label derived from the generated profile
# config when available (drift-proof), profile name and intent otherwise.
codex_profile_label() {
  local profile="$1" cfg="$HARNESS_DIR/.harness/codex/$1.config.toml" model="" effort=""
  local intent
  intent="$(codex_profile_intent "$profile")"
  if [ -f "$cfg" ]; then
    model=$(sed -n 's/^model = "\(.*\)"/\1/p' "$cfg" | head -1)
    effort=$(sed -n 's/^model_reasoning_effort = "\(.*\)"/\1/p' "$cfg" | head -1)
  fi
  if [ -n "$model" ]; then
    printf '%s — %s — %s · %s\n' "$profile" "$intent" "$model" "${effort:-default}"
  else
    printf '%s — %s\n' "$profile" "$intent"
  fi
}

# ---------------------------------------------------------------------------
# Runtime availability.
HAS_CLAUDE=false; HAS_CODEX=false; HAS_KIRO=false; HAS_HAPPY=false
command -v claude >/dev/null 2>&1 && HAS_CLAUDE=true
command -v happy >/dev/null 2>&1 && HAS_HAPPY=true
CODEX_BIN=""
if CODEX_BIN="$(harness_codex_bin_resolve)"; then HAS_CODEX=true; fi
KIRO_BIN=""
if KIRO_BIN="$(harness_kiro_bin_resolve)"; then HAS_KIRO=true; fi

prewarm_probes
migrate_legacy_plan
plan_reset
REPLAY=false

# Original credential state — a failed gateway attempt unsets/overrides these,
# and the retry loop must hand the next attempt a clean slate.
ORIG_ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY-__HARNESS_UNSET__}"

# ===========================================================================
# Choice collection (interactive) — fills CHOICE_*; history_load fills the
# same variables when a launchpad history row is picked.
# ===========================================================================

AUTO_RUNTIME=false
collect_launchpad() {
  local opts=() labels=() hist_lines=() i line ts summary runtime
  history_migrate_single_full || return 2
  AUTO_RUNTIME=false
  if ! $HAS_CLAUDE && ! $HAS_CODEX && ! $HAS_KIRO; then
    echo "Error: no runtime found (claude / codex / kiro-cli not in PATH)" >&2
    echo "  claude 설치 여부 또는 HARNESS_CODEX_BIN / HARNESS_KIRO_BIN 설정을 확인하세요." >&2
    exit 1
  fi
  # Composer entries first — the primary action stays at the top; recent
  # configs follow below them.
  $HAS_CLAUDE && { opts+=("claude"); labels+=("☁️  New — Claude Code 구성…"); }
  $HAS_CODEX && { opts+=("codex"); labels+=("⚡ New — Codex CLI 구성…"); }
  $HAS_KIRO && { opts+=("kiro"); labels+=("🦜 New — Kiro CLI 구성…"); }
  # History rows — complete configs, newest first. Rows whose runtime is no
  # longer installed are hidden (the entry stays in the file). The full line is
  # captured per row so replay does not depend on the file staying unchanged
  # while the menu is open.
  if [ -f "$HISTORY_FILE" ]; then
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      summary=$(history_field "$line" SUMMARY)
      runtime=$(history_field "$line" RUNTIME)
      [ -n "$summary" ] || continue
      case "$runtime" in
        claude) $HAS_CLAUDE || continue ;;
        codex)  $HAS_CODEX || continue ;;
        kiro)   $HAS_KIRO || continue ;;
        *) continue ;;
      esac
      ts=$(history_field "$line" TS)
      opts+=("hist:${#hist_lines[@]}"); labels+=("↩ $summary · $(rel_time "$ts")")
      hist_lines+=("$line")
    done < "$HISTORY_FILE"
  fi

  if [ ${#opts[@]} -eq 1 ]; then
    CHOICE_RUNTIME="${opts[0]}"
    AUTO_RUNTIME=true
    return 0
  fi

  BREADCRUMB=""
  menu_filter "$HARNESS_NAME" "${labels[@]}" || return 1
  for i in "${!labels[@]}"; do
    if [ "$MENU_RESULT" = "${labels[$i]}" ]; then
      CHOICE_RUNTIME="${opts[$i]}"
      break
    fi
  done
  case "$CHOICE_RUNTIME" in
    hist:*)
      history_load_line "${hist_lines[${CHOICE_RUNTIME#hist:}]}" || return 1
      REPLAY=true ;;
  esac
  return 0
}

# --- Claude flow -----------------------------------------------------------
claude_summary() {
  local model effort
  if [ "$CHOICE_MODE" = "custom" ]; then
    model="$CHOICE_C_MODEL"; effort="$CHOICE_C_EFFORT"
  else
    harness_mode_resolve "$CHOICE_MODE" "$CHOICE_PROVIDER"
    model="$HARNESS_MODE_MODEL"; effort="$HARNESS_MODE_EFFORT"
  fi
  PLAN_SUMMARY="Claude · $CHOICE_PROVIDER · $CHOICE_SESSION · $model · $effort"
  [ "$CHOICE_PERM" != "default" ] && PLAN_SUMMARY="$PLAN_SUMMARY · $CHOICE_PERM"
  [ "$CHOICE_MCP_SURFACE" = "light" ] && PLAN_SUMMARY="$PLAN_SUMMARY · mcp-light"
  [ "$CHOICE_CHROME" = 1 ] && PLAN_SUMMARY="$PLAN_SUMMARY · chrome"
  [ "$CHOICE_HAPPY" = 1 ] && PLAN_SUMMARY="$PLAN_SUMMARY · happy"
}

collect_claude() {
  local step=provider i
  # Skip the provider menu entirely when no gateway is configured.
  if ! $KIRO_GATEWAY_CONFIGURED && ! $CODEX_GATEWAY_CONFIGURED; then
    CHOICE_PROVIDER="direct"
    step=session
  fi

  while true; do
    case "$step" in
      provider)
        BREADCRUMB="$HARNESS_NAME ▸ Claude"
        local popts=("☁️  Anthropic — direct")
        $KIRO_GATEWAY_CONFIGURED && popts+=("$(probe_mark kiro) Kiro gateway")
        $CODEX_GATEWAY_CONFIGURED && popts+=("$(probe_mark codex) Codex gateway")
        menu "Provider" "${popts[@]}" || return 1
        case "$MENU_RESULT" in
          *Kiro*)  CHOICE_PROVIDER="kiro" ;;
          *Codex*) CHOICE_PROVIDER="codex" ;;
          *)       CHOICE_PROVIDER="direct" ;;
        esac
        # Load the gateway env now so mode labels/summary see the same
        # CODEX_CONTEXT_SUFFIX / model overrides the launch will use.
        case "$CHOICE_PROVIDER" in
          kiro)  [ -f "$HARNESS_DIR/config/.local/kiro-gateway.env" ] && . "$HARNESS_DIR/config/.local/kiro-gateway.env" ;;
          codex) [ -f "$HARNESS_DIR/config/.local/codex-gateway.env" ] && . "$HARNESS_DIR/config/.local/codex-gateway.env" ;;
        esac
        step=session ;;

      session)
        BREADCRUMB="$HARNESS_NAME ▸ Claude"
        menu "Session" \
          "🆕 New session" \
          "⏩ Continue last session" \
          "📋 Resume — pick from list" || {
            if $KIRO_GATEWAY_CONFIGURED || $CODEX_GATEWAY_CONFIGURED; then step=provider; continue; fi
            return 1
          }
        case "$MENU_RESULT" in
          *Continue*) CHOICE_SESSION="continue" ;;
          *Resume*)   CHOICE_SESSION="resume" ;;
          *)          CHOICE_SESSION="new" ;;
        esac
        step=mode ;;

      mode)
        BREADCRUMB="$HARNESS_NAME ▸ Claude ▸ $CHOICE_SESSION"
        local mopts=() modes=() m label
        for m in fast base plan opus rich ultracode; do
          label=$(harness_mode_label "$m" "$CHOICE_PROVIDER") || continue
          mopts+=("$label"); modes+=("$m")
        done
        mopts+=("🔧 Custom — model/effort 직접 선택"); modes+=("custom")
        # Append after Custom to keep existing numbered selections stable.
        if label=$(harness_mode_label fable "$CHOICE_PROVIDER"); then
          mopts+=("$label"); modes+=("fable")
        fi
        menu "Mode" "${mopts[@]}" || { step=session; continue; }
        for i in "${!mopts[@]}"; do
          [ "$MENU_RESULT" = "${mopts[$i]}" ] && { CHOICE_MODE="${modes[$i]}"; break; }
        done
        if [ "$CHOICE_MODE" = "custom" ]; then step=custom_model; else step=final; fi ;;

      custom_model)
        BREADCRUMB="$HARNESS_NAME ▸ Claude ▸ custom"
        if [ "$CHOICE_PROVIDER" = "kiro" ]; then
          menu "Model" "sonnet" "opus" "opusplan" "haiku" || { step=mode; continue; }
        elif [ "$CHOICE_PROVIDER" = "direct" ]; then
          menu "Model" "sonnet" "opus" "opus-1m" "opusplan" "haiku" "fable" || { step=mode; continue; }
        else
          menu "Model" "sonnet" "opus" "opus-1m" "opusplan" "haiku" || { step=mode; continue; }
        fi
        CHOICE_C_MODEL="$MENU_RESULT"
        step=custom_effort ;;

      custom_effort)
        BREADCRUMB="$HARNESS_NAME ▸ Claude ▸ custom ▸ $CHOICE_C_MODEL"
        case "$CHOICE_C_MODEL" in
          haiku)    menu "Effort" "low ← Recommended" "medium" || { step=custom_model; continue; } ;;
          sonnet)   menu "Effort" "low" "medium ← Recommended" "high" || { step=custom_model; continue; } ;;
          opus-1m)  menu "Effort" "medium" "high" "max ← Recommended" || { step=custom_model; continue; } ;;
          opus)     menu "Effort" "medium" "high ← Recommended" "max" || { step=custom_model; continue; } ;;
          fable)    menu "Effort" "medium" "high ← Recommended" "xhigh" "max" || { step=custom_model; continue; } ;;
          opusplan) menu "Effort" "medium" "high ← Recommended" || { step=custom_model; continue; } ;;
          *)        menu "Effort" "low" "medium ← Recommended" "high" || { step=custom_model; continue; } ;;
        esac
        CHOICE_C_EFFORT="${MENU_RESULT%% *}"
        step=final ;;

      final)
        claude_summary
        BREADCRUMB=""
        local fopts=("🚀 Start now" "🔐 Permission: $CHOICE_PERM")
        fopts+=("🌐 Chrome: $( [ "$CHOICE_CHROME" = 1 ] && echo on || echo off )")
        if ! harness_mcp_surface_policy_is_single_full "$MCP_SURFACE_POLICY"; then
          fopts+=("🔌 MCP surface: $CHOICE_MCP_SURFACE")
        fi
        $HAS_HAPPY && fopts+=("📱 Happy wrapper: $( [ "$CHOICE_HAPPY" = 1 ] && echo on || echo off )")
        fopts+=("↩ Back")
        menu "$PLAN_SUMMARY" "${fopts[@]}" || {
          if [ "$CHOICE_MODE" = "custom" ]; then step=custom_effort; else step=mode; fi
          continue
        }
        case "$MENU_RESULT" in
          *Start*) return 0 ;;
          *Permission*)
            menu "Permission mode" \
              "default — 매번 확인" \
              "acceptEdits — 편집만 자동 승인" \
              "dontAsk — 대부분 자동 승인" \
              "bypassPermissions — 모든 확인 건너뛰기 (주의)" || continue
            CHOICE_PERM="${MENU_RESULT%% *}" ;;
          *Chrome*)
            if [ "$CHOICE_CHROME" = 1 ]; then CHOICE_CHROME=0; else CHOICE_CHROME=1; fi ;;
          *"MCP surface"*)
            # light rides on --mcp-config, which the happy wrapper does not
            # accept — the two toggles are mutually exclusive (last one wins).
            if [ "$CHOICE_MCP_SURFACE" = "light" ]; then
              CHOICE_MCP_SURFACE="full"
            else
              CHOICE_MCP_SURFACE="light"
              [ "$CHOICE_HAPPY" = 1 ] && { CHOICE_HAPPY=0; echo "ℹ️  light MCP surface는 Happy와 함께 쓸 수 없어 Happy를 껐습니다." >&2; }
            fi ;;
          *Happy*)
            if [ "$CHOICE_HAPPY" = 1 ]; then
              CHOICE_HAPPY=0
            else
              CHOICE_HAPPY=1
              [ "$CHOICE_MCP_SURFACE" = "light" ] && { CHOICE_MCP_SURFACE="full"; echo "ℹ️  Happy는 light MCP surface를 지원하지 않아 full로 되돌렸습니다." >&2; }
            fi ;;
          *Back*)
            if [ "$CHOICE_MODE" = "custom" ]; then step=custom_effort; else step=mode; fi ;;
        esac ;;
    esac
  done
}

# --- Codex flow ------------------------------------------------------------
codex_happy_compatible() {
  # Same constraint as the shortcut path: Happy cannot map subcommands,
  # explicit profiles, work surface, or safety overrides.
  local expected_surface="default"
  harness_mcp_surface_policy_is_single_full "$MCP_SURFACE_POLICY" && expected_surface="full"
  [ "$CHOICE_SESSION" = "new" ] && \
  [ "$CHOICE_CODEX_PROFILE" = "base" ] && \
  [ "$CHOICE_CODEX_SURFACE" = "$expected_surface" ] && \
  [ "$CHOICE_CODEX_SAFETY" = "default" ]
}

codex_summary() {
  local context_label="272K"
  [ "$CHOICE_CODEX_CONTEXT" = "1m" ] && context_label="1M"
  PLAN_SUMMARY="Codex · $CHOICE_SESSION · $CHOICE_CODEX_PROFILE · $context_label"
  [ "$CHOICE_CODEX_SURFACE" = "work" ] && PLAN_SUMMARY="$PLAN_SUMMARY · work-MCP"
  [ "$CHOICE_CODEX_SAFETY" != "default" ] && PLAN_SUMMARY="$PLAN_SUMMARY · $CHOICE_CODEX_SAFETY"
  [ "$CHOICE_HAPPY" = 1 ] && PLAN_SUMMARY="$PLAN_SUMMARY · happy"
}

collect_codex() {
  local step=session
  while true; do
    case "$step" in
      session)
        BREADCRUMB="$HARNESS_NAME ▸ Codex"
        menu "Session" \
          "🆕 New session" \
          "⏩ Continue last session" \
          "📋 Resume — pick from list" \
          "🔱 Fork last session" || return 1
        case "$MENU_RESULT" in
          *Continue*) CHOICE_SESSION="continue" ;;
          *Resume*)   CHOICE_SESSION="resume" ;;
          *Fork*)     CHOICE_SESSION="fork" ;;
          *)          CHOICE_SESSION="new" ;;
        esac
        step=mode ;;

      mode)
        BREADCRUMB="$HARNESS_NAME ▸ Codex ▸ $CHOICE_SESSION"
        menu "Profile" \
          "⚡ $(codex_profile_label fast)" \
          "⚖️  $(codex_profile_label base)" \
          "🌞 $(codex_profile_label sol)" \
          "🗺️  $(codex_profile_label plan)" \
          "🧠 $(codex_profile_label rich)" \
          "✨ $(codex_profile_label astra)" || { step=session; continue; }
        case "$MENU_RESULT" in
          "⚡"*) CHOICE_CODEX_PROFILE="fast" ;;
          "🌞"*) CHOICE_CODEX_PROFILE="sol" ;;
          "✨"*) CHOICE_CODEX_PROFILE="astra" ;;
          "🗺"*) CHOICE_CODEX_PROFILE="plan" ;;
          "🧠"*) CHOICE_CODEX_PROFILE="rich" ;;
          *)     CHOICE_CODEX_PROFILE="base" ;;
        esac
        step=safety ;;

      safety)
        BREADCRUMB="$HARNESS_NAME ▸ Codex ▸ $CHOICE_CODEX_PROFILE"
        menu "Safety" \
          "🛡  Default — sandboxed, ask on request" \
          "🤖 Full auto (--full-auto)" \
          "🙈 Never ask (-a never)" \
          "⚠️  Bypass — 샌드박스·승인 전부 해제 (위험)" || { step=mode; continue; }
        case "$MENU_RESULT" in
          *Full*)   CHOICE_CODEX_SAFETY="full-auto" ;;
          *Never*)  CHOICE_CODEX_SAFETY="never" ;;
          *Bypass*) CHOICE_CODEX_SAFETY="bypass" ;;
          *)        CHOICE_CODEX_SAFETY="default" ;;
        esac
        step=final ;;

      final)
        # Happy only works for a base-profile new session with the default
        # surface; if back-navigation broke compatibility, the toggle silently
        # staying on would make Start fail with no way to turn it off (the
        # toggle is hidden).
        codex_happy_compatible || CHOICE_HAPPY=0
        codex_summary
        BREADCRUMB=""
        local fopts=("🚀 Start now")
        if ! harness_mcp_surface_policy_is_single_full "$MCP_SURFACE_POLICY"; then
          fopts+=("🔌 MCP surface: $CHOICE_CODEX_SURFACE")
        fi
        if $HAS_HAPPY && codex_happy_compatible; then
          fopts+=("📱 Happy wrapper: $( [ "$CHOICE_HAPPY" = 1 ] && echo on || echo off )")
        fi
        fopts+=("🧠 Context: $( [ "$CHOICE_CODEX_CONTEXT" = "1m" ] && echo "1M" || echo "272K (Recommended)" )")
        fopts+=("↩ Back")
        menu "$PLAN_SUMMARY" "${fopts[@]}" || { step=safety; continue; }
        case "$MENU_RESULT" in
          *Start*) return 0 ;;
          *"MCP surface"*)
            # work rides on the generated work profile config; the happy
            # wrapper cannot use it — same exclusivity as claude light+happy.
            if [ "$CHOICE_CODEX_SURFACE" = "work" ]; then
              CHOICE_CODEX_SURFACE="default"
            else
              CHOICE_CODEX_SURFACE="work"
              [ "$CHOICE_HAPPY" = 1 ] && { CHOICE_HAPPY=0; echo "ℹ️  work MCP surface는 Happy와 함께 쓸 수 없어 Happy를 껐습니다." >&2; }
            fi ;;
          *Happy*)
            if [ "$CHOICE_HAPPY" = 1 ]; then CHOICE_HAPPY=0; else CHOICE_HAPPY=1; fi ;;
          *Context*)
            if [ "$CHOICE_CODEX_CONTEXT" = "1m" ]; then CHOICE_CODEX_CONTEXT="272k"; else CHOICE_CODEX_CONTEXT="1m"; fi ;;
          *Back*) step=safety ;;
        esac ;;
    esac
  done
}

# --- Kiro flow -------------------------------------------------------------
kiro_summary() {
  if harness_kiro_selection_resolve "${CHOICE_MODE:-base}" "$CHOICE_C_MODEL" "$CHOICE_C_EFFORT" 2>/dev/null; then
    PLAN_SUMMARY="Kiro · $CHOICE_SESSION · $HARNESS_KIRO_MODEL · $HARNESS_KIRO_EFFORT"
    [ "$CHOICE_MODE" = "custom" ] && PLAN_SUMMARY="$PLAN_SUMMARY · custom"
  else
    # Unresolvable pair (e.g. a replayed row from a newer/older version): show
    # what is stored rather than silently substituting a different launch.
    # launch_kiro fails on the same input, so display and launch agree.
    PLAN_SUMMARY="Kiro · $CHOICE_SESSION · ${CHOICE_C_MODEL:-${CHOICE_MODE:-base}} · ${CHOICE_C_EFFORT:-?} · ⚠ 확인 필요"
  fi
  [ "$CHOICE_MCP_SURFACE" = "light" ] && PLAN_SUMMARY="$PLAN_SUMMARY · mcp-light"
  [ "$CHOICE_KIRO_TRUST" = 1 ] && PLAN_SUMMARY="$PLAN_SUMMARY · trust-all"
}

collect_kiro() {
  local step=session i
  while true; do
    case "$step" in
      session)
        BREADCRUMB="$HARNESS_NAME ▸ Kiro"
        menu "Session" \
          "🆕 New session" \
          "⏩ Continue last session" \
          "📋 Resume — pick from list" || return 1
        case "$MENU_RESULT" in
          *Continue*) CHOICE_SESSION="continue" ;;
          *Resume*)   CHOICE_SESSION="resume" ;;
          *)          CHOICE_SESSION="new" ;;
        esac
        step=mode ;;

      mode)
        BREADCRUMB="$HARNESS_NAME ▸ Kiro ▸ $CHOICE_SESSION"
        local kopts=() modes=() m label
        for m in fast base plan rich; do
          label=$(harness_kiro_mode_label "$m") || continue
          kopts+=("$label"); modes+=("$m")
        done
        kopts+=("🔧 Custom — model/effort 직접 선택"); modes+=("custom")
        menu "Mode" "${kopts[@]}" || { step=session; continue; }
        for i in "${!kopts[@]}"; do
          [ "$MENU_RESULT" = "${kopts[$i]}" ] && { CHOICE_MODE="${modes[$i]}"; break; }
        done
        if [ "$CHOICE_MODE" = "custom" ]; then
          step=custom_model
        else
          # A preset launch must not carry a leftover custom pair into history —
          # two rows would otherwise share a label but hash differently.
          CHOICE_C_MODEL=""; CHOICE_C_EFFORT=""
          step=final
        fi ;;

      custom_model)
        BREADCRUMB="$HARNESS_NAME ▸ Kiro ▸ custom"
        local mopts=() models=() row model_id model_mult
        while IFS= read -r row; do
          [ -n "$row" ] || continue
          model_id="${row%%	*}"
          if [ "$model_id" = "$row" ]; then model_mult=""; else model_mult="${row#*	}"; fi
          [ -n "$model_id" ] || continue
          models+=("$model_id")
          mopts+=("$(harness_kiro_catalog_label "$model_id" "$model_mult")")
        done <<< "$(harness_kiro_catalog_rows "$KIRO_BIN")"
        [ ${#models[@]} -gt 0 ] || { echo "⚠️  모델 목록을 가져오지 못했습니다" >&2; step=mode; continue; }
        menu "Model" "${mopts[@]}" || { step=mode; continue; }
        for i in "${!mopts[@]}"; do
          [ "$MENU_RESULT" = "${mopts[$i]}" ] && { CHOICE_C_MODEL="${models[$i]}"; break; }
        done
        step=custom_effort ;;

      custom_effort)
        BREADCRUMB="$HARNESS_NAME ▸ Kiro ▸ custom ▸ $CHOICE_C_MODEL"
        local eopts=() level
        while IFS= read -r level; do
          [ -n "$level" ] || continue
          eopts+=("$level")
        done <<< "$(harness_kiro_efforts_for_model "$CHOICE_C_MODEL")"
        menu "Effort" "${eopts[@]}" || { step=custom_model; continue; }
        CHOICE_C_EFFORT="${MENU_RESULT%% *}"
        step=final ;;

      final)
        kiro_summary
        BREADCRUMB=""
        menu "$PLAN_SUMMARY" \
          "🚀 Start now" \
          "🔓 Trust all tools: $( [ "$CHOICE_KIRO_TRUST" = 1 ] && echo on || echo off )" \
          "🔌 MCP surface: $CHOICE_MCP_SURFACE" \
          "↩ Back" || {
            if [ "$CHOICE_MODE" = "custom" ]; then step=custom_effort; else step=mode; fi
            continue
          }
        case "$MENU_RESULT" in
          *Start*) return 0 ;;
          *Trust*)
            if [ "$CHOICE_KIRO_TRUST" = 1 ]; then CHOICE_KIRO_TRUST=0; else CHOICE_KIRO_TRUST=1; fi ;;
          *"MCP surface"*)
            if [ "$CHOICE_MCP_SURFACE" = "light" ]; then CHOICE_MCP_SURFACE="full"; else CHOICE_MCP_SURFACE="light"; fi ;;
          *Back*)
            if [ "$CHOICE_MODE" = "custom" ]; then step=custom_effort; else step=mode; fi ;;
        esac ;;
    esac
  done
}

# ===========================================================================
# Assembly + exec — one place per runtime; replay enters here directly.
# ===========================================================================

launch_claude() {
  local provider_url="" gateway_api_key=""
  harness_export_local_env "$HARNESS_DIR" || return $?
  local HARNESS_OBSERVABILITY_ACTIVE HARNESS_OBSERVABILITY_ENABLED
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
    [ "$obs_rc" -eq 1 ] || return "$obs_rc"
  fi
  case "$CHOICE_PROVIDER" in
    kiro)
      [ -f "$HARNESS_DIR/config/.local/kiro-gateway.env" ] && . "$HARNESS_DIR/config/.local/kiro-gateway.env"
      provider_url="${KIRO_GATEWAY_URL:-}"
      gateway_api_key="${KIRO_GATEWAY_API_KEY:-}"
      [ -z "$provider_url" ] && { echo "❌ KIRO_GATEWAY_URL이 설정되지 않았습니다" >&2; return 1; }
      [ "$(probe_wait kiro)" = "up" ] || harness_probe_health "$provider_url" || {
        echo "❌ kiro-gateway에 연결할 수 없습니다 ($provider_url)" >&2; return 1; } ;;
    codex)
      [ -f "$HARNESS_DIR/config/.local/codex-gateway.env" ] && . "$HARNESS_DIR/config/.local/codex-gateway.env"
      provider_url="${CODEX_GATEWAY_URL:-}"
      gateway_api_key="${CODEX_GATEWAY_API_KEY:-}"
      [ -z "$provider_url" ] && { echo "❌ CODEX_GATEWAY_URL이 설정되지 않았습니다" >&2; return 1; }
      [ "$(probe_wait codex)" = "up" ] || harness_probe_health "$provider_url" || {
        echo "❌ codex-gateway에 연결할 수 없습니다 ($provider_url)" >&2; return 1; } ;;
  esac

  local model effort
  if [ "$CHOICE_MODE" = "custom" ]; then
    model="$CHOICE_C_MODEL"; effort="$CHOICE_C_EFFORT"
    if [ "$model" = "fable" ] && [ "$CHOICE_PROVIDER" != "direct" ]; then
      echo "❌ fable는 Anthropic direct 전용입니다 (codex/kiro 미지원)" >&2
      return 1
    fi
    if [ "$model" = "opus-1m" ]; then
      case "$CHOICE_PROVIDER" in
        codex) model="opus${CODEX_CONTEXT_SUFFIX:-[1m]}" ;;
        *)     model="claude-opus-4-6[1m]" ;;
      esac
    fi
  else
    harness_mode_resolve "$CHOICE_MODE" "$CHOICE_PROVIDER" || {
      echo "❌ mode '$CHOICE_MODE'는 provider '$CHOICE_PROVIDER'에서 지원되지 않습니다" >&2; return 1; }
    model="$HARNESS_MODE_MODEL"; effort="$HARNESS_MODE_EFFORT"
  fi

  local args=()
  case "$CHOICE_SESSION" in
    continue) args+=(--continue) ;;
    resume)   args+=(--resume) ;;
  esac
  args+=(--model "$model")
  [ "$CHOICE_PERM" != "default" ] && args+=(--permission-mode "$CHOICE_PERM")
  [ "$CHOICE_CHROME" = 1 ] && args+=(--chrome)
  [ -n "$effort" ] && args+=(--effort "$effort")
  args+=(--exclude-dynamic-system-prompt-sections)

  if [ -n "$provider_url" ]; then
    export ANTHROPIC_BASE_URL="$provider_url"
    if [ -n "$gateway_api_key" ]; then
      export ANTHROPIC_AUTH_TOKEN="$gateway_api_key"
      unset ANTHROPIC_API_KEY
    fi
    unset ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL ANTHROPIC_CUSTOM_HEADERS
    if [ "$CHOICE_PROVIDER" = "codex" ]; then
      [ -n "${CODEX_OPUS_MODEL:-}" ]   && export ANTHROPIC_DEFAULT_OPUS_MODEL="$CODEX_OPUS_MODEL"
      [ -n "${CODEX_SONNET_MODEL:-}" ] && export ANTHROPIC_DEFAULT_SONNET_MODEL="$CODEX_SONNET_MODEL"
      [ -n "${CODEX_HAIKU_MODEL:-}" ]  && export ANTHROPIC_DEFAULT_HAIKU_MODEL="$CODEX_HAIKU_MODEL"
    fi
  fi
  harness_autocompact_pct "$CHOICE_PROVIDER" "${args[@]}"

  local exe="claude"
  [ "$CHOICE_HAPPY" = 1 ] && exe="happy"
  [ "$exe" != "happy" ] || harness_slack_happy_guard || return $?
  command -v "$exe" >/dev/null 2>&1 || { echo "Error: $exe not found in PATH" >&2; return 1; }

  # The launch-record hook, as on the shortcut path (harness_claude_launch_settings).
  # happy's flag passthrough is unverified (as for --mcp-config), so it gets none.
  if [ "$exe" = "claude" ]; then
    local launch_grant="" launch_context="" launch_settings
    [ "$CHOICE_PERM" = "default" ] || launch_grant="$CHOICE_PERM"
    case "$model" in *"[1m]") launch_context=1m ;; esac
    launch_settings="$(harness_claude_launch_settings "$LAUNCHER_BIN_DIR" "$(harness_launch_source_root)" \
      false "$launch_grant" "$launch_context" "$HARNESS_DIR")"
    [ -z "$launch_settings" ] || args+=(--settings "$launch_settings")
  fi

  # Replay-path guard: history rows predating the toggle exclusivity (or a
  # hand-edited history file) must not silently launch the full surface.
  if [ "$CHOICE_MCP_SURFACE" = "light" ] && [ "$CHOICE_HAPPY" = 1 ]; then
    echo "❌ Happy 래퍼는 light MCP surface를 지원하지 않습니다 (--mcp-config 미지원)" >&2
    return 1
  fi

  if [ "$CHOICE_MCP_SURFACE" = "light" ]; then
    # Light surface: SSH-backed servers filtered out. The generated file is the
    # complete surface (its merge step already validates duplicates).
    local light_file
    light_file=$(harness_claude_light_mcp_config "$HARNESS_DIR" "$LAUNCHER_BIN_DIR") || return 1
    if [ "$exe" = "claude" ]; then
      args+=(--strict-mcp-config --mcp-config "$light_file")
    fi
  else
    # --mcp-config is a claude flag; happy does not accept it.
    if [ "$exe" = "claude" ]; then
      local full_file
      full_file=$(harness_claude_mcp_runtime_config "$HARNESS_DIR" "$LAUNCHER_BIN_DIR") || return 1
      args+=(--mcp-config "$full_file")
    else
      harness_validate_mcp_local_configs "$HARNESS_DIR" || return 1
    fi
  fi

  local HARNESS_CLAUDE_TITLE_BOOTSTRAP_ID="" HARNESS_CLAUDE_TITLE_BOOTSTRAP_VALUE=""
  local claude_interactive=0
  harness_claude_stdio_is_tty && claude_interactive=1
  if harness_claude_bootstrap_eligible "$exe" "$CHOICE_PROVIDER" "$claude_interactive" "${args[@]}"; then
    IFS=$'\t' read -r HARNESS_CLAUDE_TITLE_BOOTSTRAP_ID HARNESS_CLAUDE_TITLE_BOOTSTRAP_VALUE \
      < <(harness_claude_bootstrap_values) || true
    if [ -n "$HARNESS_CLAUDE_TITLE_BOOTSTRAP_ID" ]; then
      export HARNESS_CLAUDE_TITLE_BOOTSTRAP_ID HARNESS_CLAUDE_TITLE_BOOTSTRAP_VALUE
      args+=(--name "$HARNESS_CLAUDE_TITLE_BOOTSTRAP_VALUE")
    fi
  fi

  [ "$CHOICE_MODE" = "ultracode" ] && harness_ultracode_hint

  history_save
  # The banner names the launcher settings instead of printing the JSON.
  local -a banner_args=()
  local banner_arg banner_prev=""
  for banner_arg in "${args[@]}"; do
    if [ "$banner_prev" = "--settings" ]; then banner_args+=("<launch settings>"); else banner_args+=("$banner_arg"); fi
    banner_prev="$banner_arg"
  done
  launch_banner "$PLAN_SUMMARY" "$exe" "${banner_args[@]}"
  harness_claude_cmux_broker_start "$LAUNCHER_BIN_DIR/codex-cmux-title-sync.py" "$HARNESS_DIR"
  local HARNESS_SLACK_ARGV=()
  if [ "$exe" = "claude" ]; then
    harness_slack_claude_argv "$LAUNCHER_BIN_DIR/slack-approval-policy.py" "${args[@]}" || return $?
    args=("${HARNESS_SLACK_ARGV[@]}")
  fi
  if [ -n "${HARNESS_SLACK_SETTINGS_FILE:-}" ]; then
    trap 'harness_slack_settings_cleanup; rm -rf "$PROBE_DIR"' EXIT
    "$exe" "${args[@]}"
    exit $?
  fi
  exec "$exe" "${args[@]}"
}

launch_codex() {
  [ "$CHOICE_HAPPY" != 1 ] || harness_slack_happy_guard || return $?
  export HARNESS_CODEX_CONTEXT="$CHOICE_CODEX_CONTEXT"
  if harness_mcp_surface_policy_is_single_full "$MCP_SURFACE_POLICY"; then
    unset HARNESS_CODEX_MCP_PROFILE
  elif [ "$CHOICE_CODEX_SURFACE" = "work" ]; then
    export HARNESS_CODEX_MCP_PROFILE="work"
  else
    unset HARNESS_CODEX_MCP_PROFILE
  fi
  prepare_codex_global_mcp_allowlist || return $?
  prepare_codex_apps_allowlist || return $?
  if [ -x "$LAUNCHER_BIN_DIR/codex-home-prepare.sh" ]; then
    echo "⏳ Codex 홈 준비 중…" >&2
    "$LAUNCHER_BIN_DIR/codex-home-prepare.sh" "$HARNESS_DIR" || return $?
  fi
  export CODEX_HOME="$HARNESS_DIR/.harness/codex"
  harness_export_local_env "$HARNESS_DIR"

  # The launch record's source root, resolved before any cd.
  local source_root
  source_root="$(harness_launch_source_root)"

  if [ "$CHOICE_HAPPY" = 1 ]; then
    command -v happy >/dev/null 2>&1 || { echo "❌ happy not found in PATH" >&2; return 1; }
    codex_happy_compatible || { echo "❌ Happy는 base 프로필 새 세션에서만 사용할 수 있습니다" >&2; return 1; }
    history_save
    launch_banner "$PLAN_SUMMARY" happy codex
    harness_codex_cmux_broker_start "$LAUNCHER_BIN_DIR/codex-cmux-title-sync.py"
    # Happy runs codex with no -p and the default safety: no profile, no grant.
    harness_launch_record_export_codex "$source_root" "" "" "" ""
    cd "$HARNESS_RUN_DIR" && exec happy codex
    local rc=$?
    harness_codex_cmux_broker_stop
    return $rc
  fi

  [ -n "$CODEX_BIN" ] || { echo "❌ codex not found in PATH" >&2; return 1; }
  local cmd=("$CODEX_BIN")
  case "$CHOICE_SESSION" in
    continue) cmd+=(resume --last) ;;
    resume)   cmd+=(resume) ;;
    fork)     cmd+=(fork --last) ;;
  esac
  cmd+=(--cd "$HARNESS_RUN_DIR" -p "$CHOICE_CODEX_PROFILE")
  # One mapping for the flag and the grant the launch record keeps; an unknown
  # value adds neither.
  local rec_approval="" rec_sandbox="" rec_bypass="" rec_profile=""
  case "$CHOICE_CODEX_SAFETY" in
    full-auto) cmd+=(--full-auto); rec_approval=on-request; rec_sandbox=workspace-write ;;
    never)     cmd+=(-a never); rec_approval=never ;;
    bypass)    cmd+=(--dangerously-bypass-approvals-and-sandbox); rec_bypass=1 ;;
  esac
  case "$CHOICE_CODEX_PROFILE" in
    fast|base|sol|astra|plan|rich) rec_profile="$CHOICE_CODEX_PROFILE" ;;
  esac

  local HARNESS_SLACK_ARGV=()
  harness_slack_codex_argv "$LAUNCHER_BIN_DIR/slack-approval-policy.py" "${cmd[@]}" || return $?
  cmd=("${HARNESS_SLACK_ARGV[@]}")
  if [ -n "${HARNESS_CODEX_SLACK_APPS:-}" ]; then
    rec_approval=on-request
    rec_bypass=""
    [ "$CHOICE_CODEX_SAFETY" != bypass ] || rec_sandbox=danger-full-access
  fi
  harness_launch_record_export_codex "$source_root" "$rec_approval" "$rec_sandbox" "$rec_bypass" "$rec_profile"
  history_save
  launch_banner "$PLAN_SUMMARY" "${cmd[@]}"
  harness_codex_cmux_broker_start "$LAUNCHER_BIN_DIR/codex-cmux-title-sync.py"
  exec "${cmd[@]}"
  local rc=$?
  harness_codex_cmux_broker_stop
  return $rc
}

launch_kiro() {
  if [ "$CHOICE_MCP_SURFACE" = "light" ]; then
    export HARNESS_KIRO_MCP_PROFILE="light"
  else
    unset HARNESS_KIRO_MCP_PROFILE
  fi
  if [ -x "$LAUNCHER_BIN_DIR/kiro-home-prepare.sh" ]; then
    "$LAUNCHER_BIN_DIR/kiro-home-prepare.sh" "$HARNESS_DIR" || return $?
  fi
  export KIRO_HOME="$HARNESS_DIR/.harness/kiro"
  harness_export_local_env "$HARNESS_DIR"

  [ -n "$KIRO_BIN" ] || { echo "❌ kiro-cli not found in PATH" >&2; return 1; }
  harness_kiro_selection_resolve "${CHOICE_MODE:-base}" "$CHOICE_C_MODEL" "$CHOICE_C_EFFORT" || return 1
  local cmd=("$KIRO_BIN" chat)
  case "$CHOICE_SESSION" in
    continue) cmd+=(-r) ;;
    resume)   cmd+=(--resume-picker) ;;
  esac
  cmd+=(--model "$HARNESS_KIRO_MODEL" --effort "$HARNESS_KIRO_EFFORT" --agent harness)
  [ "$CHOICE_KIRO_TRUST" = 1 ] && cmd+=(-a)

  history_save
  launch_banner "$PLAN_SUMMARY" "${cmd[@]}"
  exec "${cmd[@]}"
}

# ===========================================================================
# Main loop — collect (or replay), then assemble/exec. Backing out of a
# flow's first menu returns here; backing out of the runtime menu exits.
# ===========================================================================
while true; do
  REPLAY=false
  plan_reset
  # A failed previous attempt must not leak provider env into the next one.
  unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN CLAUDE_AUTOCOMPACT_PCT_OVERRIDE HARNESS_CODEX_CONTEXT HARNESS_CODEX_MCP_PROFILE HARNESS_KIRO_MCP_PROFILE
  unset ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL
  if [ "$ORIG_ANTHROPIC_API_KEY" = "__HARNESS_UNSET__" ]; then
    unset ANTHROPIC_API_KEY
  else
    export ANTHROPIC_API_KEY="$ORIG_ANTHROPIC_API_KEY"
  fi
  collect_launchpad
  collect_rc=$?
  if [ "$collect_rc" -ne 0 ]; then
    [ "$collect_rc" -eq 1 ] && exit 0
    exit "$collect_rc"
  fi

  if ! $REPLAY; then
    case "$CHOICE_RUNTIME" in
      # When the runtime was auto-selected (single option), backing out of the
      # flow's first menu means exit, not an infinite reselect loop.
      claude) collect_claude || { $AUTO_RUNTIME && exit 0; continue; } ;;
      codex)  collect_codex || { $AUTO_RUNTIME && exit 0; continue; } ;;
      kiro)   collect_kiro || { $AUTO_RUNTIME && exit 0; continue; } ;;
      *) echo "Error: unknown runtime '$CHOICE_RUNTIME'" >&2; exit 1 ;;
    esac
  fi

  case "$CHOICE_RUNTIME" in
    claude) launch_claude ;;
    codex)  launch_codex ;;
    kiro)   launch_kiro ;;
  esac
  # launch_* only returns on failure — surface it and let the user retry.
  echo "" >&2
  echo "⚠️  시작 실패 — 메뉴로 돌아갑니다." >&2
done
