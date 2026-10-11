#!/usr/bin/env bash
# harness-common.sh — single source of truth shared by launcher.sh (bash) and
# aliases.zsh (zsh). Everything here must stay in the bash-3.2 ∩ zsh subset:
# no associative arrays, no ${var,,}, no bash-only ${!arr[@]}, no zsh-only print.
#
# Contents:
#   harness_resolve_run_dir <harness-dir> <requested-dir>
#   harness_mode_resolve <mode> <provider>   → HARNESS_MODE_MODEL / HARNESS_MODE_EFFORT
#   harness_mode_label   <mode> <provider>   → display label derived from resolve
#   harness_codex_bin_resolve / harness_kiro_bin_resolve
#   harness_probe_health <url>
#   harness_export_local_env <harness-dir>
#   harness_autocompact_pct <provider> <arg>...
#   harness_mcp_local_configs / harness_validate_mcp_local_configs <harness-dir>
#   harness_ultracode_hint
#   harness_terminal_runtime / harness_terminal_launch_runtime
#   harness_terminal_scrub_env [runtime] / harness_terminal_announce_cwd <dir>

HARNESS_CODEX_APP_BIN_DEFAULT="/Applications/Codex.app/Contents/Resources/codex"

harness_mcp_surface_policy_resolve() {
  case "${1:-}" in
    "") printf '%s\n' legacy ;;
    single-full-compat|single-full) printf '%s\n' "$1" ;;
    *)
      echo "harness-launcher: invalid HARNESS_MCP_SURFACE_POLICY '$1'; supported: empty, single-full-compat, single-full" >&2
      return 2
      ;;
  esac
}

harness_mcp_surface_policy_is_single_full() {
  [ "$1" = single-full-compat ] || [ "$1" = single-full ]
}

harness_resolve_run_dir() {
  local harness_dir="$1" requested="$2" harness_abs requested_abs
  [ -d "$requested" ] || {
    echo "harness-launcher: --cwd is not a directory: $requested" >&2
    return 2
  }
  harness_abs="$(cd -P "$harness_dir" 2>/dev/null && pwd -P)" || return 2
  requested_abs="$(cd -P "$requested" 2>/dev/null && pwd -P)" || return 2
  case "$requested_abs" in
    "$harness_abs"|"$harness_abs"/*) printf '%s\n' "$requested_abs" ;;
    *)
      echo "harness-launcher: --cwd must stay inside the registered harness: $harness_abs" >&2
      return 2
      ;;
  esac
}

harness_python3_resolve() {
  local candidate
  if [ -n "${HARNESS_PYTHON_BIN:-}" ]; then
    candidate="$HARNESS_PYTHON_BIN"
    [ -x "$candidate" ] && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null && {
      printf '%s\n' "$candidate"
      return 0
    }
  else
    for candidate in \
      /opt/homebrew/opt/python@3.13/libexec/bin/python3 \
      /usr/local/opt/python@3.13/libexec/bin/python3 \
      /opt/homebrew/bin/python3 \
      /usr/local/bin/python3 \
      "$(command -v python3 2>/dev/null || true)"; do
      [ -n "$candidate" ] && [ -x "$candidate" ] || continue
      "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null || continue
      printf '%s\n' "$candidate"
      return 0
    done
  fi
  echo "ERROR: harness-launcher requires Python 3.11 or newer (set HARNESS_PYTHON_BIN)" >&2
  return 1
}

# harness_claude_bootstrap_eligible <exe> <provider> <interactive> [final argv...]
# Claude Code generates an auxiliary Haiku session title unless a new session
# already has --name.  Only opt in when the harness title hook can safely adopt
# and replace the reserved bootstrap title.  Existing-session, noninteractive,
# hook-free, remote, and user-named paths retain Claude's native behavior.
harness_claude_bootstrap_eligible() {
  local exe="${1:-}" provider="${2:-}" interactive="${3:-0}" arg
  shift 3 || return 1

  [ "${HARNESS_CLAUDE_BOOTSTRAP_NAME:-1}" != "0" ] || return 1
  [ "$exe" = "claude" ] || return 1
  [ "$provider" = "direct" ] || return 1
  [ "$interactive" = "1" ] || return 1

  for arg in "$@"; do
    case "$arg" in
      -c|--continue|-r|-r?*|--resume|--resume=*|--fork-session|--fork-session=*|\
      --from-pr|--from-pr=*|-p|-p?*|--print|--print=*|--background|--bg|\
      --cloud|--cloud=*|--remote-control|--remote-control=*|--teleport|--teleport=*|attach|respawn|--bare|\
      --safe-mode|--restricted|--setting-sources|--setting-sources=*|\
      --session-id|--session-id=*|-w|-w?*|--worktree|--worktree=*|--tmux|\
      --tmux=*|--environment|--environment=*|-n|-n?*|--name|--name=*)
        return 1
        ;;
    esac
  done
  return 0
}

# harness_claude_is_management_command <word>: native Claude subcommands and
# informational flags that must run without launcher model/MCP flags.
harness_claude_is_management_command() {
  case "${1:-}" in
    agents|attach|auth|auto-mode|doctor|gateway|help|import|install|logs|mcp|plugin|plugins|project|respawn|rm|setup-token|stop|kill|ultrareview|update|upgrade|-h|--help|-v|-V|--version)
      return 0 ;;
  esac
  return 1
}

harness_claude_stdio_is_tty() {
  [ -t 0 ] && [ -t 1 ]
}

# harness_launcher_stable_dir <dir>
#   The Homebrew `opt` spelling of a launcher package directory reached through
#   the compat share link or a versioned Cellar path, when both name the same
#   directory (-ef); any other directory prints unchanged. Generated hook
#   commands use this spelling: Codex asks to trust every hook whose command
#   changed, so a Cellar path re-asked after each upgrade, and a long-running
#   session's hooks broke once `brew cleanup` removed the old keg. A different
#   active keg is not an alias and never wins.
harness_launcher_stable_dir() {
  local dir="$1" candidate=""
  case "$dir" in
    */opt/harness-launcher/share/harness-launcher) ;;
    */Cellar/harness-launcher/*/share/harness-launcher)
      candidate="${dir%%/Cellar/harness-launcher/*}/opt/harness-launcher/share/harness-launcher" ;;
    */share/harness-launcher)
      candidate="${dir%/share/harness-launcher}/opt/harness-launcher/share/harness-launcher" ;;
  esac
  if [ -n "$candidate" ] && [ -d "$candidate" ] && [ "$dir" -ef "$candidate" ]; then
    printf '%s\n' "$candidate"
  else
    printf '%s\n' "$dir"
  fi
}

# Launch record (harness-launch-record, a SessionStart hook) — shared by the
# shortcut path (aliases.zsh) and the interactive picker (launcher.sh), so both
# record the same facts. Portable across bash 3.2 and zsh.

# harness_shell_word <s>
#   Prints <s> as one word for a command line /bin/sh parses: unchanged when it
#   holds only [A-Za-z0-9_./:@%+=,-], otherwise single-quoted. Empty prints ''.
harness_shell_word() {
  case "$1" in
    ''|*[!A-Za-z0-9_./:@%+=,-]*) ;;
    *) printf '%s' "$1"; return 0 ;;
  esac
  local rest="$1" out="'"
  while :; do
    case "$rest" in
      *\'*) out="$out${rest%%\'*}'\\''"; rest="${rest#*\'}" ;;
      *) out="$out$rest'"; break ;;
    esac
  done
  printf '%s' "$out"
}

# harness_json_escape <s>: <s> with \ and " escaped for a JSON string body.
harness_json_escape() {
  local s="$1" out="" c i=0 n=${#1}
  while [ "$i" -lt "$n" ]; do
    c="${s:$i:1}"
    case "$c" in
      \\) out="$out\\\\" ;;
      \") out="$out\\\"" ;;
      *) out="$out$c" ;;
    esac
    i=$((i + 1))
  done
  printf '%s' "$out"
}

# harness_launch_source_root
#   bash callers only: HARNESS_SOURCE_ROOT when set, else the physical
#   HARNESS_DIR (zsh callers pass ${HARNESS_DIR:A}; the restore side resolves
#   both before comparing). Falls back to HARNESS_DIR as given.
harness_launch_source_root() {
  if [ -n "${HARNESS_SOURCE_ROOT:-}" ]; then
    printf '%s\n' "$HARNESS_SOURCE_ROOT"
    return 0
  fi
  (cd -P -- "$HARNESS_DIR" 2>/dev/null && pwd -P) || printf '%s\n' "$HARNESS_DIR"
}

# harness_launch_record_export_codex <source-root> <approval> <sandbox> <bypass> <profile> [network]
#   Codex only; run in the process that execs the agent. Exports the grant and
#   profile it is launched with, its source root and isolation, for the
#   launch-record hook (its hooks.json row is static, so it reads the
#   environment). Anything inherited from a parent launch is dropped first.
#   Claude does not use this: its hook takes the same facts as arguments,
#   because Claude applies an agent-writable `env` block from settings.
harness_launch_record_export_codex() {
  unset HARNESS_LAUNCH_PERMISSION HARNESS_LAUNCH_APPROVAL HARNESS_LAUNCH_SANDBOX HARNESS_LAUNCH_NETWORK HARNESS_LAUNCH_BYPASS HARNESS_LAUNCH_PROFILE
  [ -z "$2" ] || export HARNESS_LAUNCH_APPROVAL="$2"
  [ -z "$3" ] || export HARNESS_LAUNCH_SANDBOX="$3"
  [ -z "$4" ] || export HARNESS_LAUNCH_BYPASS="$4"
  [ -z "$5" ] || export HARNESS_LAUNCH_PROFILE="$5"
  [ -z "${6:-}" ] || export HARNESS_LAUNCH_NETWORK="$6"
  export HARNESS_LAUNCH_SOURCE_ROOT="$1"
  export HARNESS_LAUNCH_ISOLATED=0
  [ -z "${HARNESS_SESSION_ROOT:-}" ] || HARNESS_LAUNCH_ISOLATED=1
  return 0
}

# harness_claude_launch_settings <bin-dir> <source-root> <force-thinking:true|false> <permission> <context> [config-root]
#   Prints the one launcher-owned --settings JSON: the forced-thinking setting
#   (xhigh/max) merged with the SessionStart hook that writes the launch record.
#   The grant, source root and isolation are ARGUMENTS of the hook command, not
#   environment, so a forged `env` block in settings.local.json cannot change
#   them. An unknown permission records no grant. The hook prints nothing (its
#   stdout would become session context). Prints nothing when there is nothing
#   to set.
harness_claude_launch_settings() {
  local hook="$1/harness-launch-record" py cmd out="" isolated=0 perm="" hooks="" optins="" config_root="${6:-$2}"
  local uuid_re='^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'
  [ -z "${HARNESS_SESSION_ROOT:-}" ] || isolated=1
  [ "$3" = true ] && out='"alwaysThinkingEnabled":true'
  case "$4" in default|acceptEdits|plan|auto|dontAsk|bypassPermissions) perm="$4" ;; esac
  if [ -f "$hook" ] && py="$(harness_python3_resolve 2>/dev/null)"; then
    cmd="$(harness_shell_word "$py") $(harness_shell_word "$hook") claude --source-root $(harness_shell_word "$2") --isolated $isolated"
    [ -z "$perm" ] || cmd="$cmd --permission $perm"
    [ "$5" != 1m ] || cmd="$cmd --context 1m"
    if [ "$isolated" = 1 ] && [[ "${HARNESS_SESSION_ID:-}" =~ $uuid_re ]]; then
      cmd="$cmd --harness-session-id $HARNESS_SESSION_ID"
    fi
    # A control character would break the hand-built JSON; record nothing instead.
    case "$cmd" in
      *[[:cntrl:]]*) ;;
      *) hooks="\"SessionStart\":[{\"hooks\":[{\"type\":\"command\",\"command\":\"$(harness_json_escape "$cmd")\",\"timeout\":5}]}]" ;;
    esac
    if [ -f "$1/runtime_hooks_optin.py" ]; then
      optins="$("$py" "$1/runtime_hooks_optin.py" "$config_root" 2>/dev/null)"
      case " $optins " in
        *" ssot=1 "*)
          cmd="/bin/sh -c 's=\"\$HOME/.local/share/harness-service/bin/harness-session-hook\"; [ -x \"\$s\" ] && { /bin/sh \"\$s\" --runtime claude >/dev/null 2>&1; exit 0; }; cat >/dev/null'"
          local event entry
          entry="[{\"hooks\":[{\"type\":\"command\",\"command\":\"$(harness_json_escape "$cmd")\",\"timeout\":5}]}]"
          for event in UserPromptSubmit Stop; do hooks="$hooks${hooks:+,}\"$event\":$entry"; done
          ;;
      esac
    fi
  fi
  [ -z "$hooks" ] || out="$out${out:+,}\"hooks\":{$hooks}"
  [ -z "$out" ] || printf '%s\n' "{$out}"
}

# harness_headless_session_settings <session-root>
#   harness-headless only: settings that need the session path, merged into the
#   single Claude --settings. The session git config, hooks and info dir are
#   write-denied.
harness_headless_session_settings() {
  local git tool rules=""
  git="$(harness_json_escape "$1/.git")"
  for tool in Edit Write NotebookEdit; do
    rules="$rules${rules:+,}\"$tool(/$git/config)\",\"$tool(/$git/hooks/**)\",\"$tool(/$git/info/**)\""
  done
  printf '{"permissions":{"deny":[%s]},"sandbox":{"filesystem":{"denyWrite":["%s/config","%s/hooks","%s/info"]}}}\n' \
    "$rules" "$git" "$git" "$git"
}

# harness_codex_option_takes_value <option>: top-level Codex options whose
# value is the next argument.
harness_codex_option_takes_value() {
  case "${1:-}" in
    -c|--config|--enable|--disable|--remote|--remote-auth-token-env|-i|--image|-m|--model|--local-provider|-p|--profile|-s|--sandbox|-C|--cd|--add-dir|-a|--ask-for-approval)
      return 0 ;;
  esac
  return 1
}

# harness_codex_subcommand [codex args...]: print the first subcommand word
# ("debug <next word>" for debug), or nothing for an interactive launch.
harness_codex_subcommand() {
  local arg
  while [ "$#" -gt 0 ]; do
    arg="$1"; shift
    case "$arg" in
      --) return 0 ;;
      -*) if harness_codex_option_takes_value "$arg" && [ "$#" -gt 0 ]; then shift; fi ;;
      debug) printf 'debug %s\n' "${1:-}"; return 0 ;;
      *) printf '%s\n' "$arg"; return 0 ;;
    esac
  done
  return 0
}

# harness_codex_subcommand_rejects_profile <subcommand>: Codex accepts
# --profile only for runtime commands, `mcp`, and `debug prompt-input`.
harness_codex_subcommand_rejects_profile() {
  case "${1:-}" in
    'debug prompt-input') return 1 ;;
    agents|login|logout|plugin|app-server|remote-control|app|completion|update|doctor|apply|a|migrate-rollouts|cloud|exec-server|features|help|debug|debug\ *)
      return 0 ;;
  esac
  return 1
}

# harness_session_isolation_default_route <interactive:0|1> [launcher argv...]
# Prints isolate, legacy, reject, or invalid without mutating session state.
harness_session_isolation_default_route() {
  local interactive="${1:-0}" arg passthrough=0
  shift || return 2
  [ "$interactive" = 1 ] || { printf '%s\n' legacy; return 0; }
  [ "$#" -gt 0 ] || { printf '%s\n' legacy; return 0; }

  case "$1" in
    --isolated|--no-isolated|--isolated-session)
      printf '%s\n' invalid
      return 0
      ;;
    codex-smoke|checkup|kiro|kiro-cli|codex-gateway)
      printf '%s\n' legacy
      return 0
      ;;
    codex)
      shift
      while [ "$#" -gt 0 ]; do
        arg="$1"; shift
        case "$arg" in
          --) break ;;
          --passthrough)
            [ "$passthrough" = 0 ] || { printf '%s\n' invalid; return 0; }
            passthrough=1 ;;
          --isolated|--no-isolated|--isolated-session) printf '%s\n' invalid; return 0 ;;
          resume|fork) printf '%s\n' reject; return 0 ;;
          continue)
            [ "$passthrough" = 0 ] || break
            printf '%s\n' reject; return 0 ;;
          agents|exec|e|review|login|logout|mcp|plugin|app-server|remote-control|app|completion|update|doctor|sandbox|debug|apply|a|queue|archive|delete|migrate-rollouts|unarchive|cloud|exec-server|features|help|-h|--help|-V|--version)
            printf '%s\n' legacy; return 0 ;;
          fast|base|sol|plan|rich|astra|work|272k|1m|happy|full-auto|never|bypass)
            [ "$passthrough" = 0 ] || break ;;
          -c|--config|--enable|--disable|--remote|--remote-auth-token-env|-i|--image|-m|--model|--local-provider|-p|--profile|-s|--sandbox|-C|--cd|--add-dir|-a|--ask-for-approval|--app)
            [ "$#" -gt 0 ] && shift || { printf '%s\n' invalid; return 0; }
            ;;
          --config=*|--enable=*|--disable=*|--remote=*|--remote-auth-token-env=*|--image=*|--model=*|--local-provider=*|--profile=*|--sandbox=*|--cd=*|--add-dir=*|--ask-for-approval=*|--app=*) ;;
          -c?*|-i?*|-m?*|-p?*|-s?*|-C?*|-a?*) ;;
          --strict-config|--oss|--approve-for-me|--dangerously-bypass-approvals-and-sandbox|--dangerously-bypass-hook-trust|--worktree|--search|--no-alt-screen) ;;
          -*) printf '%s\n' invalid; return 0 ;;
          *) break ;;
        esac
      done
      printf '%s\n' isolate
      return 0
      ;;
  esac

  # After --passthrough, bare launcher keywords are Claude arguments (prompt
  # text), while Claude options keep their classification.
  while [ "$#" -gt 0 ]; do
    arg="$1"; shift
    case "$arg" in
      --) break ;;
      --passthrough)
        [ "$passthrough" = 0 ] || { printf '%s\n' invalid; return 0; }
        passthrough=1
        # A management subcommand right after the marker runs natively.
        if harness_claude_is_management_command "${1:-}"; then printf '%s\n' legacy; return 0; fi ;;
      --isolated|--no-isolated|--isolated-session) printf '%s\n' invalid; return 0 ;;
      continue|resume)
        [ "$passthrough" = 0 ] || break
        printf '%s\n' reject; return 0 ;;
      -c|--continue|-r|-r?*|--resume|--resume=*|--fork-session|--fork-session=*)
        printf '%s\n' reject; return 0 ;;
      -h|--help|-V|--version|-p|-p?*|--print|--print=*|--background|--bg|--cloud|--cloud=*|--remote-control|--remote-control=*|--teleport|--teleport=*|attach|respawn|--bare|--safe-mode|--restricted|--from-pr|--from-pr=*|--session-id|--session-id=*|-w|-w?*|--worktree|--worktree=*|--tmux|--tmux=*|--environment|--environment=*)
        printf '%s\n' legacy; return 0 ;;
      fast|base|plan|opus|rich|fable|ultracode|low|medium|high|xhigh|max|light|bypass|acceptEdits|dontAsk)
        [ "$passthrough" = 0 ] || break ;;
      --chrome|--no-chrome) ;;
      --agent|--agents|--append-system-prompt|--autocompact|--debug-file|--effort|--fallback-model|--input-format|--json-schema|--max-budget-usd|--model|-n|--name|--output-format|--permission-mode|--permission-prompts|--plugin-dir|--plugin-url|--remote-control-session-name-prefix|--setting-sources|--settings|--system-prompt|--system-prompt-snapshot)
        [ "$#" -gt 0 ] && shift || { printf '%s\n' invalid; return 0; }
        ;;
      --agent=*|--agents=*|--append-system-prompt=*|--autocompact=*|--debug-file=*|--effort=*|--fallback-model=*|--input-format=*|--json-schema=*|--max-budget-usd=*|--model=*|--name=*|--output-format=*|--permission-mode=*|--permission-prompts=*|--plugin-dir=*|--plugin-url=*|--remote-control-session-name-prefix=*|--setting-sources=*|--settings=*|--system-prompt=*|--system-prompt-snapshot=*) ;;
      --add-dir|--allowedTools|--allowed-tools|--betas|--disallowedTools|--disallowed-tools|--file|--mcp-config|--tools)
        printf '%s\n' invalid; return 0
        ;;
      --allow-dangerously-skip-permissions|--ax-screen-reader|--brief|--dangerously-skip-permissions|--disable-slash-commands|--exclude-dynamic-system-prompt-sections|--forward-subagent-text|--ide|--include-hook-events|--include-partial-messages|--no-chrome|--no-session-persistence|--replay-user-messages|--strict-mcp-config)
        ;;
      -*) printf '%s\n' invalid; return 0 ;;
      *) break ;;
    esac
  done
  printf '%s\n' isolate
}

harness_claude_bootstrap_values() {
  local launch_id="" harness_python
  if command -v uuidgen >/dev/null 2>&1; then
    launch_id="$(uuidgen | tr '[:upper:]' '[:lower:]')"
  else
    harness_python="$(harness_python3_resolve)" || return 1
    launch_id="$("$harness_python" -c 'import uuid; print(uuid.uuid4())')" || return 1
  fi
  printf '%s\t%s\n' "$launch_id" 'Harness startup'
}

# harness_codex_global_mcp_allowlist_normalize <raw> → normalized CSV.
# The caller owns export/unset so each native entrypoint can keep its dynamic
# launcher.env scope isolated before it starts codex-home-prepare.sh.
harness_codex_global_mcp_allowlist_normalize() {
  local raw="${1:-}" label="${2:-global MCP}" harness_python
  [ -n "$raw" ] || return 0
  harness_python="$(harness_python3_resolve)" || return 1
  "$harness_python" - "$raw" "$label" <<'PY'
import re
import sys

label = sys.argv[2]
names = []
for item in sys.argv[1].split(","):
    name = item.strip()
    if not name:
        continue
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise SystemExit(f"invalid {label} allowlist name: {name!r}")
    if name not in names:
        names.append(name)
print(",".join(names))
PY
}

# harness_codex_apps_allowlist_normalize <raw> → normalized CSV.
# App ids become TOML table keys, so only Codex's app-id namespace is accepted.
# This keeps reserved names and punctuation from producing malformed or
# duplicate [apps.*] sections in the generated home.
harness_codex_apps_allowlist_normalize() {
  local raw="${1:-}" harness_python
  [ -n "$raw" ] || return 0
  harness_python="$(harness_python3_resolve)" || return 1
  "$harness_python" - "$raw" <<'PY'
import re
import sys

apps = []
for item in sys.argv[1].split(","):
    app_id = item.strip()
    if not app_id:
        continue
    if not re.fullmatch(r"(?:asdk_app_|connector_|templated_apps_)[A-Za-z0-9][A-Za-z0-9_-]*", app_id):
        raise SystemExit(f"invalid Codex app allowlist id: {app_id!r}")
    if app_id not in apps:
        apps.append(app_id)
print(",".join(apps))
PY
}

# --- mode → model/effort table -------------------------------------------------
# The ONLY place mode/provider → model/effort lives. Labels are derived from
# this resolution so a label can never disagree with the launched model again.
# provider: direct | kiro | codex   (claude-runtime providers)
harness_mode_resolve() {
  local mode="$1" provider="${2:-direct}"
  HARNESS_MODE_MODEL=""
  HARNESS_MODE_EFFORT=""
  case "$mode" in
    fast)
      HARNESS_MODE_MODEL="haiku"; HARNESS_MODE_EFFORT="low" ;;
    base)
      case "$provider" in
        kiro)  HARNESS_MODE_MODEL="sonnet[1m]" ;;
        codex) HARNESS_MODE_MODEL="sonnet${CODEX_CONTEXT_SUFFIX:-}" ;;
        *)     HARNESS_MODE_MODEL="sonnet" ;;
      esac
      HARNESS_MODE_EFFORT="high" ;;
    plan)
      case "$provider" in
        kiro)  HARNESS_MODE_MODEL="opusplan[1m]" ;;
        codex) HARNESS_MODE_MODEL="opusplan${CODEX_CONTEXT_SUFFIX:-}" ;;
        *)     HARNESS_MODE_MODEL="opusplan" ;;
      esac
      HARNESS_MODE_EFFORT="high" ;;
    opus)
      # rich's model at one effort step down: strong main model without the
      # xhigh cost (and without the forced-thinking override xhigh/max need).
      case "$provider" in
        kiro)  HARNESS_MODE_MODEL="claude-opus-4-6[1m]" ;;
        codex) HARNESS_MODE_MODEL="opus${CODEX_CONTEXT_SUFFIX:-}" ;;
        *)     HARNESS_MODE_MODEL="opus[1m]" ;;
      esac
      HARNESS_MODE_EFFORT="high" ;;
    rich)
      case "$provider" in
        kiro)  HARNESS_MODE_MODEL="claude-opus-4-6[1m]"; HARNESS_MODE_EFFORT="max" ;;
        codex) HARNESS_MODE_MODEL="opus${CODEX_CONTEXT_SUFFIX:-}"; HARNESS_MODE_EFFORT="high" ;;
        *)     HARNESS_MODE_MODEL="opus[1m]"; HARNESS_MODE_EFFORT="xhigh" ;;
      esac ;;
    fable)
      [ "$provider" = "direct" ] || return 1
      HARNESS_MODE_MODEL="fable"; HARNESS_MODE_EFFORT="high" ;;
    ultracode)
      # Anthropic direct only; orchestration half is session-only (/effort).
      [ "$provider" = "direct" ] || return 1
      HARNESS_MODE_MODEL="opus[1m]"; HARNESS_MODE_EFFORT="xhigh" ;;
    *) return 1 ;;
  esac
  return 0
}

# Display label derived from the live resolution (drift-proof by construction).
harness_mode_label() {
  local mode="$1" provider="${2:-direct}" icon=""
  harness_mode_resolve "$mode" "$provider" || return 1
  case "$mode" in
    fast)      icon="⚡ Fast" ;;
    base)      icon="⚖️  Base" ;;
    plan)      icon="🗺️  Plan" ;;
    opus)      icon="🎼 Opus" ;;
    rich)      icon="🧠 Rich" ;;
    fable)     icon="📖 Fable" ;;
    ultracode) icon="🌀 Ultracode" ;;
  esac
  printf '%s — %s · %s\n' "$icon" "$HARNESS_MODE_MODEL" "$HARNESS_MODE_EFFORT"
}

# --- kiro-native mode table -------------------------------------------------
# Kiro CLI takes literal model IDs; shared by the TUI menu and `<prefix> kiro-cli`.
# Tier IDs live here only for the SESSION model. Subagent tiers resolve through
# bin/subagent-model-map.tsv — bump both together (see the cascading matrix).
HARNESS_KIRO_MODEL_FAST="claude-haiku-4.5"
HARNESS_KIRO_MODEL_BASE="claude-sonnet-5"
HARNESS_KIRO_MODEL_DEEP="claude-opus-5"

# Effort enum the Kiro runtime accepts (verified against the live session
# schema: enum [low medium high xhigh max], default high). The CLI does NOT
# reject an unknown value — it silently falls back — so the launcher validates.
# Kept as a display string plus an emitter: zsh does not word-split unquoted
# parameters, so `for x in $VAR` is not portable between bash and zsh.
HARNESS_KIRO_EFFORT_LEVELS="low medium high xhigh max"
harness_kiro_effort_each() { printf '%s\n' "$HARNESS_KIRO_EFFORT_LEVELS" | tr ' ' '\n' | grep -v '^$'; }

# Static fallback catalog: used when the live listing is unavailable (offline,
# stub binary, not logged in). `id<TAB>rate_multiplier`, in the same order the
# runtime returns — including `auto` first — so a keystroke picks the same row
# online and offline. Captured 2026-09-18 from `kiro-cli chat --list-models`.
HARNESS_KIRO_MODEL_FALLBACK_CATALOG="auto	1.00
claude-opus-5	2.20
claude-sonnet-5	1.30
claude-opus-4.8	2.20
gpt-5.6-sol	4.40
gpt-5.6-terra	2.20
gpt-5.6-luna	1.10
claude-opus-4.7	2.20
claude-opus-4.6	2.20
claude-sonnet-4.6	1.30
claude-opus-4.5	2.20
claude-sonnet-4.5	1.30
claude-sonnet-4	1.30
claude-haiku-4.5	0.40
deepseek-3.2	0.25
minimax-m2.5	0.25
minimax-m2.1	0.15
glm-5	0.50
qwen3-coder-next	0.05"

# harness_kiro_catalog_fallback — static list plus one notice, so a frozen list
# (e.g. the runtime changed its listing format) can never look like live data.
harness_kiro_catalog_fallback() {
  # Once per process: the TUI can re-enter the model menu on Back navigation.
  if [ -z "${_HARNESS_KIRO_FALLBACK_NOTICED:-}" ]; then
    _HARNESS_KIRO_FALLBACK_NOTICED=1
    echo "ℹ️  Kiro 모델 목록을 조회하지 못해 내장 목록을 사용합니다 (kiro-cli chat --list-models 확인)" >&2
  fi
  printf '%s\n' "$HARNESS_KIRO_MODEL_FALLBACK_CATALOG"
}

harness_kiro_mode_resolve() {
  local mode="$1"
  HARNESS_KIRO_MODEL=""
  HARNESS_KIRO_EFFORT=""
  case "$mode" in
    fast) HARNESS_KIRO_MODEL="$HARNESS_KIRO_MODEL_FAST"; HARNESS_KIRO_EFFORT="low" ;;
    base) HARNESS_KIRO_MODEL="$HARNESS_KIRO_MODEL_BASE"; HARNESS_KIRO_EFFORT="high" ;;
    plan) HARNESS_KIRO_MODEL="$HARNESS_KIRO_MODEL_DEEP"; HARNESS_KIRO_EFFORT="high" ;;
    rich) HARNESS_KIRO_MODEL="$HARNESS_KIRO_MODEL_DEEP"; HARNESS_KIRO_EFFORT="max" ;;
    *) return 1 ;;
  esac
  return 0
}

# Operational intent per preset — same idea as codex_profile_intent, so the menu
# says what a mode is FOR, not just which model it picks.
harness_kiro_mode_intent() {
  case "$1" in
    fast) printf '%s\n' "로그·요약·단순 확인" ;;
    base) printf '%s\n' "일반 구현·탐색" ;;
    plan) printf '%s\n' "설계·계획 수립" ;;
    rich) printf '%s\n' "리뷰·복잡 디버깅" ;;
    *) return 1 ;;
  esac
}

harness_kiro_mode_label() {
  local mode="$1" icon="" intent=""
  harness_kiro_mode_resolve "$mode" || return 1
  intent="$(harness_kiro_mode_intent "$mode")" || intent=""
  case "$mode" in
    fast) icon="⚡ Fast" ;;
    base) icon="⚖️  Base" ;;
    plan) icon="🗺️  Plan" ;;
    rich) icon="🧠 Rich" ;;
  esac
  if [ -n "$intent" ]; then
    printf '%s — %s — %s · %s\n' "$icon" "$intent" "$HARNESS_KIRO_MODEL" "$HARNESS_KIRO_EFFORT"
  else
    printf '%s — %s · %s\n' "$icon" "$HARNESS_KIRO_MODEL" "$HARNESS_KIRO_EFFORT"
  fi
}

# harness_kiro_effort_is_valid <effort> — enum guard for --effort.
harness_kiro_effort_is_valid() {
  local candidate="$1"
  [ -n "$candidate" ] || return 1
  case " $HARNESS_KIRO_EFFORT_LEVELS " in
    *" $candidate "*) return 0 ;;
  esac
  return 1
}

# harness_kiro_effort_recommended <model> — the effort a model is tuned for.
# This is the mode-neutral default (it matches the runtime's own `high` default
# for the Claude tiers); the `rich` preset deliberately escalates to `max`, which
# stays an explicit choice rather than a recommendation.
harness_kiro_effort_recommended() {
  case "$1" in
    *haiku*) printf 'low\n' ;;
    *sonnet*) printf 'high\n' ;;
    *) printf 'high\n' ;;
  esac
}

# harness_kiro_efforts_for_model <model> — ordered enum with the recommended
# level marked, mirroring the Claude custom-effort menu.
harness_kiro_efforts_for_model() {
  local model="$1" rec level
  rec="$(harness_kiro_effort_recommended "$model")"
  harness_kiro_effort_each | while IFS= read -r level; do
    if [ "$level" = "$rec" ]; then
      printf '%s ← Recommended\n' "$level"
    else
      printf '%s\n' "$level"
    fi
  done
}

# harness_kiro_catalog_cache_path — per-user cache for the probed model list.
harness_kiro_catalog_cache_path() {
  local state="${XDG_STATE_HOME:-$HOME/.local/state}/harness-launcher"
  printf '%s/kiro-models.tsv\n' "$state"
}

# harness_kiro_catalog_probe <bin> — `kiro-cli chat --list-models` is a local,
# documented listing (no API call, no credits). stdin is redirected because the
# TUI's terminal must not be consumed by the child process, and stdout goes to a
# file rather than a pipe: capturing through `$( )` would wait for stdout EOF, so
# a lingering grandchild holding the pipe could outlast the timeout.
# Output: `id<TAB>rate_multiplier` per line.
harness_kiro_catalog_probe() {
  local bin="$1" tmp rc
  [ -n "$bin" ] || return 1
  tmp="$(mktemp "${TMPDIR:-/tmp}/harness-kiro-models.XXXXXX")" || return 1
  harness_kiro_bounded_run 8 "$bin" chat --list-models --format plain \
    </dev/null >"$tmp" 2>/dev/null
  rc=$?
  if [ "$rc" -ne 0 ]; then rm -f "$tmp"; return 1; fi
  awk '
    { line = $0
      sub(/^[[:space:]]*\*?[[:space:]]*/, "", line)
      if (split(line, f, /[[:space:]]+/) < 2) next
      if (f[2] !~ /^[0-9]+(\.[0-9]+)?x$/) next
      sub(/x$/, "", f[2])
      printf "%s\t%s\n", f[1], f[2] }' "$tmp"
  rc=$?
  rm -f "$tmp"
  return "$rc"
}

# harness_kiro_bounded_run <seconds> <cmd...> — `timeout` is not on macOS by
# default; perl is. The child runs in its own process group and the supervisor
# SIGKILLs the whole group on expiry: a plain `alarm`+`exec` only signals the
# direct child, which a shell defers while waiting on its own child (measured
# 12s overshoot) and which leaves descendants running. Falls back to an
# unbounded run if perl is missing.
harness_kiro_bounded_run() {
  local secs="$1"; shift
  if harness_path_lookup perl >/dev/null 2>&1; then
    perl -e '
      use POSIX ();
      my $secs = shift;
      my $pid = fork();
      exit 127 unless defined $pid;
      if ($pid == 0) {
        eval { POSIX::setpgid(0, 0) };
        exec @ARGV;
        POSIX::_exit(127);
      }
      eval { POSIX::setpgid($pid, $pid) };
      my $rc = 0;
      my $timed_out = 0;
      eval {
        local $SIG{ALRM} = sub { $timed_out = 1; kill("KILL", -$pid); die "timeout\n" };
        alarm $secs;
        waitpid($pid, 0);
        alarm 0;
        $rc = $? >> 8;
        $rc = 128 + ($? & 127) if $rc == 0 && ($? & 127);
        1;
      };
      if ($timed_out) { waitpid($pid, 0); exit 124 }
      exit $rc;
    ' "$secs" "$@"
  else
    "$@"
  fi
}

# harness_kiro_catalog_rows [bin] — `id<TAB>multiplier` rows for the model menu.
# Precedence: explicit override → fresh cache → live probe → stale cache →
# static fallback. A failed probe is stamped so an offline session does not pay
# the timeout again on every menu entry.
harness_kiro_catalog_rows() {
  local bin="${1:-}" cache stamp ttl probed
  if [ -n "${HARNESS_KIRO_MODEL_CATALOG:-}" ]; then
    printf '%s\n' "$HARNESS_KIRO_MODEL_CATALOG" | tr ' ,' '\n\n' | grep -v '^$'
    return 0
  fi
  cache="$(harness_kiro_catalog_cache_path)"
  stamp="$cache.failed"
  ttl="${HARNESS_KIRO_CATALOG_TTL:-86400}"
  if [ -s "$cache" ] && harness_file_age_below "$cache" "$ttl"; then
    cat "$cache"
    return 0
  fi
  if harness_file_age_below "$stamp" "${HARNESS_KIRO_CATALOG_FAIL_TTL:-300}"; then
    [ -s "$cache" ] && { cat "$cache"; return 0; }
    harness_kiro_catalog_fallback
    return 0
  fi
  probed="$(harness_kiro_catalog_probe "$bin")" || probed=""
  if [ -n "$probed" ]; then
    mkdir -p "$(dirname "$cache")" 2>/dev/null || true
    # $$ is the launcher PID (command substitution does not change it); the
    # temp name only needs to be unique per process, and the mv is atomic.
    if printf '%s\n' "$probed" > "$cache.tmp.$$" 2>/dev/null; then
      mv "$cache.tmp.$$" "$cache" 2>/dev/null || rm -f "$cache.tmp.$$" 2>/dev/null
    else
      rm -f "$cache.tmp.$$" 2>/dev/null
    fi
    rm -f "$stamp" 2>/dev/null
    printf '%s\n' "$probed"
    return 0
  fi
  mkdir -p "$(dirname "$stamp")" 2>/dev/null && : > "$stamp" 2>/dev/null || true
  [ -s "$cache" ] && { cat "$cache"; return 0; }
  harness_kiro_catalog_fallback
}

# harness_kiro_catalog_models [bin] — just the model IDs, menu order preserved.
harness_kiro_catalog_models() {
  harness_kiro_catalog_rows "${1:-}" | cut -f1
}

# harness_kiro_catalog_label <id> <multiplier> — menu row: cost is the decision
# input here, the same way the Codex profile labels expose model · effort.
harness_kiro_catalog_label() {
  local id="$1" mult="$2" label="$1"
  [ -n "$mult" ] && label="$label · ${mult}x credits"
  harness_kiro_model_is_recommended "$id" && label="$label ← Recommended"
  printf '%s\n' "$label"
}

# harness_file_age_below <path> <seconds> — true when the file exists and its
# mtime is within the window. Guards against a non-numeric stat fallback.
# NOTE: the local must not be named `path` — zsh ties $path to $PATH, so a
# `local path=...` here would make stat/date unresolvable inside the function.
harness_file_age_below() {
  local target="$1" window="$2" mtime now
  [ -e "$target" ] || return 1
  mtime="$(harness_file_mtime "$target")"
  case "$mtime" in
    ''|*[!0-9]*) return 1 ;;
  esac
  now="$(date +%s)"
  [ "$((now - mtime))" -lt "$window" ]
}

# harness_file_mtime <path> — epoch mtime (BSD stat first, GNU second).
harness_file_mtime() {
  stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null
}

# harness_kiro_model_is_recommended <model> — one of the three session tiers.
harness_kiro_model_is_recommended() {
  case "$1" in
    "$HARNESS_KIRO_MODEL_FAST"|"$HARNESS_KIRO_MODEL_BASE"|"$HARNESS_KIRO_MODEL_DEEP") return 0 ;;
    *) return 1 ;;
  esac
}

# harness_kiro_selection_resolve <mode> [model] [effort] — single entry point
# for "what does this launch actually run". `custom` takes the explicit pair;
# every preset ignores it. Rejects an out-of-enum effort instead of letting the
# runtime swallow it.
harness_kiro_selection_resolve() {
  local mode="${1:-base}" model="${2:-}" effort="${3:-}"
  if [ "$mode" = "custom" ]; then
    [ -n "$model" ] || { echo "❌ custom 모드에는 model이 필요합니다" >&2; return 1; }
    [ -n "$effort" ] || effort="$(harness_kiro_effort_recommended "$model")"
    if ! harness_kiro_effort_is_valid "$effort"; then
      echo "❌ 잘못된 effort: '$effort' (가능한 값: $HARNESS_KIRO_EFFORT_LEVELS)" >&2
      return 1
    fi
    HARNESS_KIRO_MODEL="$model"
    HARNESS_KIRO_EFFORT="$effort"
    return 0
  fi
  harness_kiro_mode_resolve "$mode" || return 1
  harness_kiro_effort_is_valid "$HARNESS_KIRO_EFFORT" || {
    echo "❌ 모드 '$mode'의 effort가 유효하지 않습니다: '$HARNESS_KIRO_EFFORT'" >&2
    return 1
  }
  return 0
}

# --- binary resolution ----------------------------------------------------------
# Path-only lookup: `command -v` in zsh reports shell functions too, and
# aliases.zsh defines a codex() wrapper — resolving through it would recurse
# forever. whence -p (zsh) / type -P (bash) only ever return filesystem paths.
harness_path_lookup() {
  if [ -n "${ZSH_VERSION:-}" ]; then
    whence -p "$1" 2>/dev/null
  else
    type -P "$1" 2>/dev/null
  fi
}

harness_codex_bin_resolve() {
  local configured="${HARNESS_CODEX_BIN:-}"
  if [ -n "$configured" ]; then
    if [ -x "$configured" ]; then
      printf '%s\n' "$configured"
      return 0
    fi
    harness_path_lookup "$configured"
    return $?
  fi
  if harness_path_lookup codex; then
    return 0
  fi
  local app_bin="${_HARNESS_CODEX_APP_BIN:-$HARNESS_CODEX_APP_BIN_DEFAULT}"
  if [ "${HARNESS_CODEX_ALLOW_APP_FALLBACK:-0}" = "1" ] && [ -x "$app_bin" ]; then
    printf '%s\n' "$app_bin"
    return 0
  fi
  return 1
}

# Which terminal runtime hosts this launch: herdr, orca, cmux or plain. Prints
# one value and reads only the environment plus one socket test, so it is safe
# in any shell and changes nothing. First match wins. herdr ranks first because
# a herdr pane inherits the variables of whatever started its server (Orca or
# cmux markers), yet the pane belongs to herdr; its socket test rejects
# HERDR_* left exported after the pane is gone. orca outranks cmux because
# cmux variables linger inside Orca terminals.
harness_terminal_runtime() {
  if [ "${HERDR_ENV:-}" = "1" ] && [ -n "${HERDR_PANE_ID:-}" ] \
     && [ -n "${HERDR_SOCKET_PATH:-}" ] && [ -S "$HERDR_SOCKET_PATH" ]; then
    printf 'herdr\n'
  elif [ -n "${ORCA_TERMINAL_HANDLE:-}" ] || [ "${TERM_PROGRAM:-}" = "Orca" ]; then
    printf 'orca\n'
  elif [ -n "${CMUX_WORKSPACE_ID:-}" ] && [ -n "${CMUX_TAB_ID:-}" ] \
       && [ -n "${CMUX_SURFACE_ID:-}" ]; then
    printf 'cmux\n'
  else
    printf 'plain\n'
  fi
}

# The runtime an agent launch belongs to: `plain` when the launch has no
# terminal (stdin or stdout is not a TTY; SDK hosts such as Paseo inherit their
# daemon's terminal markers but host no terminal), otherwise the marker runtime.
# It tests this process's own stdin and stdout, so capturing it with
# `x="$(harness_terminal_launch_runtime)"` always reports `plain`: the capture
# pipe is stdout. A caller that has a terminal passes a variable name instead
# (`harness_terminal_launch_runtime rt`) and gets the value assigned to it.
harness_terminal_launch_runtime() {
  local _htlr_rt
  if harness_claude_stdio_is_tty; then
    _htlr_rt="$(harness_terminal_runtime)"
  else
    _htlr_rt="plain"
  fi
  case "${1:-}" in
    "") printf '%s\n' "$_htlr_rt" ;;
    [0-9]*|*[!A-Za-z0-9_]*|_htlr_rt) return 2 ;;
    *) eval "$1=\$_htlr_rt" ;;
  esac
}

# Strip every foreign runtime's variables from the current shell so an agent
# sees exactly one terminal runtime. Run it in the agent's subshell or process,
# never the user's interactive shell. The runtime is computed once up front, so
# each step judges the original environment, not one it already trimmed. An
# optional argument (herdr, orca, cmux or plain) is used as the runtime instead
# of detecting one; any other value, or none, detects from the markers.
harness_terminal_scrub_env() {
  local rt line name
  case "${1:-}" in
    herdr|orca|cmux|plain) rt="$1" ;;
    *) rt="$(harness_terminal_runtime)" ;;
  esac
  # L1: Orca points CODEX_HOME at its own managed home; drop the pair when it
  # is only Orca's value.
  if [ -n "${ORCA_CODEX_HOME:-}" ] && [ "${CODEX_HOME:-}" = "$ORCA_CODEX_HOME" ]; then
    unset CODEX_HOME ORCA_CODEX_HOME
  fi
  if [ "$rt" != "orca" ] && [ "${TERM_PROGRAM:-}" = "Orca" ]; then
    unset TERM_PROGRAM
  fi
  # Names come from `env` lines. A value that spans lines contributes lines
  # that are not NAME=..., so only lines whose text before the first "=" is a
  # bare identifier count. The heredoc keeps the loop, and its unsets, in this
  # shell (a pipe would run them in a subshell).
  while IFS= read -r line; do
    name="${line%%=*}"
    [ "$name" != "$line" ] || continue
    case "$name" in
      *[!A-Za-z0-9_]*) continue ;;
      CMUX_*) [ "$rt" != "cmux" ] || continue ;;
      ORCA_*) [ "$rt" != "orca" ] || continue ;;
      HERDR_*) [ "$rt" != "herdr" ] || continue ;;
      *) continue ;;
    esac
    unset "$name" 2>/dev/null || true
  done <<EOF_HARNESS_ENV
$(env)
EOF_HARNESS_ENV
  export HARNESS_TERMINAL_RUNTIME="$rt"
  return 0
}

# Tell herdr the launcher's run directory as an OSC 7 cwd report so a pane
# restore is typed there. herdr accepts a file:// URI only with an empty host
# (or localhost) and an existing absolute directory, hence file:///... with the
# path percent-encoded byte by byte (everything but A-Za-z0-9 - . _ ~ / becomes
# %XX). It goes to the controlling terminal because stdout may be captured;
# HARNESS_TERMINAL_TTY redirects it for tests. Appends one sequence, prints
# nothing, and returns 0 even when the target cannot be opened.
harness_terminal_announce_cwd() {
  local dir="${1:-}" target="${HARNESS_TERMINAL_TTY:-/dev/tty}" hex pair enc=""
  [ "${HARNESS_TERMINAL_RUNTIME:-}" = "herdr" ] || return 0
  case "$dir" in /*) ;; *) return 0 ;; esac
  [ -d "$dir" ] || return 0
  # Hex bytes (uppercase) keep multibyte paths and every locale on one code path.
  hex="$(printf '%s' "$dir" | LC_ALL=C od -An -v -tx1 | LC_ALL=C tr -d ' \n' | LC_ALL=C tr 'a-f' 'A-F')" || return 0
  [ -n "$hex" ] || return 0
  while [ -n "$hex" ]; do
    pair="${hex%"${hex#??}"}"
    hex="${hex#??}"
    case "$pair" in
      2[DEF]|3[0-9]|4[1-9A-F]|5[0-9AF]|6[1-9A-F]|7[0-9AE]) enc="$enc\\x$pair" ;;
      *) enc="$enc%$pair" ;;
    esac
  done
  { printf '\033]7;file://%b\007' "$enc" >>"$target"; } 2>/dev/null || true
  return 0
}

# Keep the title watcher below a live launcher/Codex ancestor. cmux authorizes
# terminal callers through process ancestry, so a watcher orphaned by a
# short-lived SessionStart hook cannot rename the target tab.
harness_codex_cmux_broker_start() {
  local helper="$1" state_dir request_file
  harness_codex_cmux_broker_stop
  [ -x "$helper" ] || return 0
  [ -n "${CODEX_HOME:-}" ] || return 0
  [ -n "${CMUX_WORKSPACE_ID:-}" ] || return 0
  [ -n "${CMUX_TAB_ID:-}" ] || return 0
  [ -n "${CMUX_SURFACE_ID:-}" ] || return 0
  # Stale CMUX_* inside another runtime's terminal (Orca, herdr) or without a
  # terminal must not start a cmux title broker. The launcher exports its
  # verdict; without one, detect from the markers.
  [ "${HARNESS_TERMINAL_RUNTIME:-$(harness_terminal_runtime)}" = "cmux" ] || return 0
  case "${HARNESS_PREFIX:-}" in
    ''|[0-9-]*|*[!A-Za-z0-9_-]*) return 0 ;;
  esac

  state_dir="${CODEX_CMUX_TITLE_STATE_DIR:-$CODEX_HOME/.cmux-title-sync}"
  mkdir -p "$state_dir" 2>/dev/null || return 0
  chmod 700 "$state_dir" 2>/dev/null || return 0
  request_file="$(mktemp "$state_dir/request.XXXXXX")" || return 0
  chmod 600 "$request_file" 2>/dev/null || {
    rm -f "$request_file"
    return 0
  }

  export CODEX_CMUX_TITLE_REQUEST_FILE="$request_file"
  HARNESS_CODEX_CMUX_BROKER_REQUEST="$request_file"
  "$helper" --broker "$request_file" "$CMUX_SURFACE_ID" "$HARNESS_PREFIX" "$CODEX_HOME" "$$" </dev/null >/dev/null 2>&1 &
  HARNESS_CODEX_CMUX_BROKER_PID=$!
  return 0
}

harness_codex_cmux_broker_stop() {
  if [ -n "${HARNESS_CODEX_CMUX_BROKER_PID:-}" ]; then
    kill "$HARNESS_CODEX_CMUX_BROKER_PID" 2>/dev/null || true
    wait "$HARNESS_CODEX_CMUX_BROKER_PID" 2>/dev/null || true
  fi
  if [ -n "${HARNESS_CODEX_CMUX_BROKER_REQUEST:-}" ]; then
    rm -f "$HARNESS_CODEX_CMUX_BROKER_REQUEST"
  fi
  unset HARNESS_CODEX_CMUX_BROKER_PID
  unset HARNESS_CODEX_CMUX_BROKER_REQUEST
  unset CODEX_CMUX_TITLE_REQUEST_FILE
}

# Claude writes its title events to its transcript.  A launcher-owned broker
# stays in the cmux-authorized ancestry and accepts every later SessionStart
# handoff (/clear, resume, and fork), rather than one hook owning a tab.
harness_claude_cmux_broker_start() {
  local helper="$1" harness_dir="$2" state_root state_dir request_file
  harness_claude_cmux_broker_stop
  [ -x "$helper" ] || return 0
  [ -n "$harness_dir" ] || return 0
  [ -n "${CMUX_WORKSPACE_ID:-}" ] || return 0
  [ -n "${CMUX_TAB_ID:-}" ] || return 0
  [ -n "${CMUX_SURFACE_ID:-}" ] || return 0
  # Stale CMUX_* inside another runtime's terminal (Orca, herdr) or without a
  # terminal must not start a cmux title broker. The launcher exports its
  # verdict; without one, detect from the markers.
  [ "${HARNESS_TERMINAL_RUNTIME:-$(harness_terminal_runtime)}" = "cmux" ] || return 0
  case "${HARNESS_PREFIX:-}" in
    ''|[0-9-]*|*[!A-Za-z0-9_-]*) return 0 ;;
  esac
  state_root="${CLAUDE_CMUX_TITLE_STATE_ROOT:-$harness_dir/.harness/claude/.cmux-title-sync}"
  if [ -e "$state_root" ] || [ -L "$state_root" ]; then
    [ -d "$state_root" ] && [ ! -L "$state_root" ] && [ "$(stat -f '%u' "$state_root" 2>/dev/null)" = "$(id -u)" ] && [ "$(stat -f '%Lp' "$state_root" 2>/dev/null)" = "700" ] || return 0
  fi
  mkdir -p "$state_root" 2>/dev/null || return 0
  chmod 700 "$state_root" 2>/dev/null || return 0
  state_dir="$(mktemp -d "$state_root/launch.XXXXXX")" || return 0
  chmod 700 "$state_dir" 2>/dev/null || { rm -rf "$state_dir"; return 0; }
  request_file="$state_dir/request.json"
  : > "$request_file" || { rm -rf "$state_dir"; return 0; }
  chmod 600 "$request_file" 2>/dev/null || { rm -rf "$state_dir"; return 0; }
  export CLAUDE_CMUX_TITLE_STATE_DIR="$state_dir"
  export CLAUDE_CMUX_TITLE_REQUEST_FILE="$request_file"
  export CLAUDE_CMUX_TITLE_HELPER="$helper"
  HARNESS_CLAUDE_CMUX_BROKER_STATE="$state_dir"
  HARNESS_CLAUDE_CMUX_BROKER_ROOT="$state_root"
  "$helper" --claude-broker "$request_file" "$CMUX_SURFACE_ID" "$HARNESS_PREFIX" "$harness_dir/.harness/claude" "$$" </dev/null >/dev/null 2>&1 &
  HARNESS_CLAUDE_CMUX_BROKER_PID=$!
  printf '%s\n%s\n' "$$" "$HARNESS_CLAUDE_CMUX_BROKER_PID" > "$state_dir/owner" 2>/dev/null || true
  chmod 600 "$state_dir/owner" 2>/dev/null || true
  return 0
}

harness_claude_cmux_broker_stop() {
  local state="${HARNESS_CLAUDE_CMUX_BROKER_STATE:-}" root="${HARNESS_CLAUDE_CMUX_BROKER_ROOT:-${CLAUDE_CMUX_TITLE_STATE_ROOT:-}}" owner launcher_pid broker_pid
  if [ -z "$root" ] && [ -n "${HARNESS_DIR:-}" ]; then root="$HARNESS_DIR/.harness/claude/.cmux-title-sync"; fi
  case "$state" in "$root"/launch.*) ;; *) state="" ;; esac
  if [ -n "$state" ] && [ -d "$state" ] && [ ! -L "$state" ] && [ "$(stat -f '%u' "$state" 2>/dev/null)" = "$(id -u)" ] && [ "$(stat -f '%Lp' "$state" 2>/dev/null)" = "700" ] && [ -f "$state/owner" ] && [ ! -L "$state/owner" ] && [ "$(stat -f '%u' "$state/owner" 2>/dev/null)" = "$(id -u)" ]; then
    IFS= read -r launcher_pid < "$state/owner" || launcher_pid=""
    IFS= read -r broker_pid < <(sed -n '2p' "$state/owner") || broker_pid=""
  else
    state=""
  fi
  if [ -n "$state" ] && [ "$launcher_pid" = "$$" ] && [ "$broker_pid" = "${HARNESS_CLAUDE_CMUX_BROKER_PID:-}" ]; then
    kill "$HARNESS_CLAUDE_CMUX_BROKER_PID" 2>/dev/null || true
    wait "$HARNESS_CLAUDE_CMUX_BROKER_PID" 2>/dev/null || true
    rm -f "$state/request.json" "$state/active.json" "$state/owner" "$state/claude.status.json" 2>/dev/null || true
    rmdir "$state" 2>/dev/null || true
  fi
  unset HARNESS_CLAUDE_CMUX_BROKER_PID HARNESS_CLAUDE_CMUX_BROKER_STATE HARNESS_CLAUDE_CMUX_BROKER_ROOT
  unset CLAUDE_CMUX_TITLE_STATE_DIR CLAUDE_CMUX_TITLE_REQUEST_FILE
  unset CLAUDE_CMUX_TITLE_HELPER CLAUDE_CMUX_TITLE_OWNER_PID
}

harness_kiro_bin_resolve() {
  local configured="${HARNESS_KIRO_BIN:-}"
  if [ -n "$configured" ]; then
    if [ -x "$configured" ]; then
      printf '%s\n' "$configured"
      return 0
    fi
    harness_path_lookup "$configured"
    return $?
  fi
  harness_path_lookup kiro-cli
}

# --- gateway health -------------------------------------------------------------
harness_probe_health() {
  local provider_url="$1"
  [ -z "$provider_url" ] && return 1
  PROBE_PROVIDER_URL="$provider_url" \
    node -e 'const baseUrl = process.env.PROBE_PROVIDER_URL; const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 2000); fetch(`${baseUrl}/health`, { signal: controller.signal }).then(() => { clearTimeout(timer); process.exit(0); }).catch(() => { clearTimeout(timer); process.exit(1); });' >/dev/null 2>&1
}

# --- local observability --------------------------------------------------------
# Strict parser: local observability config is data, never sourced as shell code.
# Return 0=enabled, 1=disabled/not configured/invalid (optional telemetry is fail-open).
harness_observability_load() {
  local harness_dir="$1" launcher_file config_file line key value prefix="" seen_keys="" HARNESS_OBSERVABILITY_ENABLED=""
  HARNESS_OBSERVABILITY_ACTIVE=0
  HARNESS_OBSERVABILITY_PROFILE=""
  HARNESS_OTLP_HTTP_ENDPOINT=""

  launcher_file="$harness_dir/config/launcher.env"
  if [ -f "$launcher_file" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        HARNESS_PREFIX=*)
          value="${line#HARNESS_PREFIX=}"
          value="${value#\"}"; value="${value%\"}"
          value="${value#\'}"; value="${value%\'}"
          prefix="$value"
          ;;
      esac
    done < "$launcher_file"
  fi

  case "$prefix" in
    ''|[0-9-]*|*[!A-Za-z0-9_-]*) return 1 ;;
  esac
  config_file="$harness_dir/config/.local/observability.env"
  [ -f "$config_file" ] || return 1

  HARNESS_OBSERVABILITY_ENABLED=""
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*) continue ;;
      *=*) key="${line%%=*}"; value="${line#*=}" ;;
      *) echo "WARN: invalid observability config line; telemetry disabled" >&2; return 1 ;;
    esac
    case " $seen_keys " in
      *" $key "*) echo "WARN: duplicate observability config key: $key; telemetry disabled" >&2; return 1 ;;
    esac
    seen_keys="$seen_keys $key"
    case "$key" in
      HARNESS_OBSERVABILITY_ENABLED) HARNESS_OBSERVABILITY_ENABLED="$value" ;;
      HARNESS_OTLP_HTTP_ENDPOINT) HARNESS_OTLP_HTTP_ENDPOINT="$value" ;;
      *) echo "WARN: unknown observability config key: $key; telemetry disabled" >&2; return 1 ;;
    esac
  done < "$config_file"

  [ "$HARNESS_OBSERVABILITY_ENABLED" = "1" ] || {
    [ -z "$HARNESS_OBSERVABILITY_ENABLED" ] || [ "$HARNESS_OBSERVABILITY_ENABLED" = "0" ] || {
      echo "WARN: HARNESS_OBSERVABILITY_ENABLED must be 0 or 1; telemetry disabled" >&2
      return 1
    }
    return 1
  }
  [ "$HARNESS_OTLP_HTTP_ENDPOINT" = "http://127.0.0.1:4318" ] || {
    echo "WARN: observability HTTP endpoint must be loopback http://127.0.0.1:4318; telemetry disabled" >&2
    return 1
  }

  HARNESS_OBSERVABILITY_ACTIVE=1
  HARNESS_OBSERVABILITY_PROFILE="$prefix"
  return 0
}

# --- per-harness GitHub identity ------------------------------------------------
# gh keeps ONE global active account, but the harnesses expect different GitHub
# users (personal and team profiles), so whichever harness switched last
# decides whether the others' gh commands succeed. GH_TOKEN takes precedence over
# gh's stored credentials, so deriving it per harness removes that arbitration.
# Optional and fail-open: on any doubt export nothing and leave the pre-bash
# gh-auth hook as the backstop — an empty GH_TOKEN would override the stored
# credentials with nothing. The token value is never printed.
harness_gh_token_load() {
  local harness_dir="$1" cfg user="" token=""
  HARNESS_GH_USER=""
  # harness-headless runs get no derived credentials.
  [ "${HARNESS_HEADLESS:-0}" != 1 ] || return 1
  # Same resolution order as the harness gh-auth hook: team profiles keep
  # github_user in gitignored per-machine config; personal profiles may commit it.
  for cfg in "$harness_dir/config/.local/config.yaml" "$harness_dir/config/config.yaml"; do
    [ -f "$cfg" ] || continue
    user="$(sed -n 's/^github_user:[[:space:]]*//p' "$cfg" 2>/dev/null | head -1 | tr -d '[:space:]')"
    [ -n "$user" ] && break
  done
  [ -n "$user" ] || return 1
  # GitHub logins are alphanumeric with internal hyphens, at most 39 characters.
  # Reject anything else instead of passing it to gh.
  case "$user" in
    -*|*-|*[!A-Za-z0-9-]*) return 1 ;;
  esac
  [ "${#user}" -le 39 ] || return 1
  command -v gh >/dev/null 2>&1 || return 1
  token="$(gh auth token --user "$user" 2>/dev/null)" || return 1
  [ -n "$token" ] || return 1
  export GH_TOKEN="$token"
  HARNESS_GH_USER="$user"
  return 0
}

# --- per-harness env ------------------------------------------------------------
# Export MCP secrets from .claude/settings.local.json env so gateway/native
# runtimes resolve bearer_token_env_var etc. (they inherit no other harness env).
harness_export_local_env() {
  local harness_dir="$1"
  local harness_python
  # harness-headless runs never inherit harness-local secrets.
  [ "${HARNESS_HEADLESS:-0}" != 1 ] || return 0
  [ -f "$harness_dir/.claude/settings.local.json" ] || return 0
  harness_python="$(harness_python3_resolve)" || return 1
  local _mk _mv _restore_xtrace=0
  case $- in *x*) _restore_xtrace=1; set +x ;; esac
  while IFS=$'\t' read -r _mk _mv; do
    [ -n "$_mk" ] && export "$_mk=$_mv"
  done < <("$harness_python" - "$harness_dir/.claude/settings.local.json" 2>/dev/null <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    env = (json.load(f).get("env") or {})
for k, v in env.items():
    print(k + "\t" + str(v))
PY
)
  if [ "$_restore_xtrace" = 1 ]; then set -x; fi
  return 0
}

# --- auto-compact PCT -----------------------------------------------------------
# [1m] + codex gateway + GPT-5.5 mapping → PCT=35 (real 400K limit; 35% of the
# fake 1M window = 350K fits). [1m] otherwise → PCT=50. No [1m] → leave unset
# (settings.json fallback applies).
harness_autocompact_pct() {
  local provider="$1" _arg _gpt55=0; shift
  case "${CODEX_OPUS_MODEL:-}" in *5.5*) _gpt55=1 ;; esac
  case "${CODEX_SONNET_MODEL:-}" in *5.5*) _gpt55=1 ;; esac
  for _arg in "$@"; do
    case "$_arg" in
      *"[1m]"*)
        if [ "$provider" = "codex" ] && [ "$_gpt55" = "1" ]; then
          export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=35
        else
          export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50
        fi
        return 0 ;;
    esac
  done
  return 0
}

# --- local MCP configs ----------------------------------------------------------
harness_mcp_local_configs() {
  local harness_dir="$1"
  [ -f "$harness_dir/.mcp.local.json" ] && printf '%s\n' "$harness_dir/.mcp.local.json"
  [ -f "$harness_dir/mcp.local.json" ] && printf '%s\n' "$harness_dir/mcp.local.json"
}

harness_validate_mcp_local_configs() {
  local harness_dir="$1"
  local harness_python
  set --
  [ -f "$harness_dir/.mcp.json" ] && set -- "$@" "$harness_dir/.mcp.json"
  [ -f "$harness_dir/.mcp.local.json" ] && set -- "$@" "$harness_dir/.mcp.local.json"
  [ -f "$harness_dir/mcp.local.json" ] && set -- "$@" "$harness_dir/mcp.local.json"
  [ "$#" -gt 1 ] || return 0
  harness_python="$(harness_python3_resolve)" || return 1
  "$harness_python" - "$@" <<'PY'
import json, sys

seen = {}
for path in sys.argv[1:]:
    try:
        with open(path, encoding="utf-8") as f:
            servers = (json.load(f).get("mcpServers") or {})
    except FileNotFoundError:
        continue
    for name in servers:
        if name in seen:
            print(
                f"ERROR: duplicate MCP server '{name}' in {seen[name]} and {path}; "
                "rename the local server instead of overriding committed .mcp.json",
                file=sys.stderr,
            )
            sys.exit(1)
        seen[name] = path
PY
}

# Isolated runtime artifacts must never enter the session Git worktree: they
# can contain host-local MCP credentials and would contaminate submission.
harness_claude_mcp_output_path() {
  local harness_dir="$1" filename="$2" harness_python
  harness_python="$(harness_python3_resolve)" || return 1
  "$harness_python" - "$harness_dir" "$filename" <<'PY'
import os, re, sys
from pathlib import Path

root, filename = sys.argv[1:]
if filename not in ("mcp-full.json", "mcp-light.json"):
    raise SystemExit("invalid Claude MCP runtime filename")
session_id = os.environ.get("HARNESS_SESSION_ID", "")
if session_id:
    if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", session_id):
        raise SystemExit("invalid isolated session ID")
    if Path(root).resolve() != Path(os.environ.get("HARNESS_SESSION_ROOT", "")).resolve():
        raise SystemExit("isolated MCP root mismatch")
    state_home = Path(os.environ.get("HARNESS_SESSION_STATE_HOME") or
                      Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "harness-launcher")
    session_dir = state_home / "sessions" / session_id
    if not session_dir.is_dir() or session_dir.is_symlink():
        raise SystemExit("isolated MCP state directory missing or unsafe")
    print(session_dir / filename)
else:
    print(Path(root) / ".harness/claude" / filename)
PY
}

# Render the definition SSOT once for Claude, independent of its launch CWD.
# Runtime files are derived state, never an MCP definition authority.
harness_claude_mcp_runtime_config() {
  local harness_dir="$1" launcher_bin="$2" out harness_python
  harness_python="$(harness_python3_resolve)" || return 1
  out="$(harness_claude_mcp_output_path "$harness_dir" mcp-full.json)" || return 1
  mkdir -p "$(dirname "$out")" || return 1
  "$harness_python" - "$harness_dir" "$launcher_bin" "$out" <<'PY'
import json, os, sys, tempfile
root, launcher_bin, out = sys.argv[1:]
sys.path.insert(0, launcher_bin)
from mcp_paths import normalize_servers

merged, seen = {}, {}
for name in (".mcp.json", ".mcp.local.json", "mcp.local.json"):
    source = os.path.join(root, name)
    if not os.path.isfile(source):
        continue
    try:
        with open(source, encoding="utf-8") as stream:
            servers = json.load(stream).get("mcpServers")
        if not isinstance(servers, dict):
            raise ValueError("mcpServers must be an object")
        for server, spec in servers.items():
            if server in seen:
                raise ValueError(f"duplicate MCP server '{server}' in {seen[server]} and {source}")
            seen[server] = source
            merged.update(normalize_servers({server: spec}, root))
    except (OSError, ValueError) as error:
        print(f"ERROR: {source}: {error}", file=sys.stderr)
        raise SystemExit(1)
if not merged:
    raise SystemExit(0)
fd, tmp = tempfile.mkstemp(prefix=".mcp-full.", dir=os.path.dirname(out))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"mcpServers": merged}, stream, indent=2)
        stream.write("\n")
    os.replace(tmp, out)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
print(out)
PY
}

# --- MCP surface (light) ----------------------------------------------------------
# The "light" surface drops SSH-backed MCP servers — the heavy class that
# opens remote connections per session — and keeps everything else. Two shapes
# identify that class:
#   1. stdio wrappers: command "bash" + args[0] containing start-ssh-mcp.sh
#   2. loopback HTTP on the SSH-tunnel port band 38200–38299 (RAG/KG tunnels;
#      documented in domains/knowledge/tools/mcp/ssh-backed-mcp.md)
# Local non-SSH services (e.g. 381xx) and remote https servers stay.
# Used by Claude (--strict-mcp-config) and Kiro (HARNESS_KIRO_MCP_PROFILE=light
# in kiro-home-prepare.sh).
#
# harness_claude_light_mcp_config <harness-dir> → prints generated file path.
# Merges .mcp.json + local overlays, filters the SSH class, writes the result
# under .harness/claude/ (isolated sessions use external session state).
# Fails (rc 1) on duplicate server names.
harness_claude_light_mcp_config() {
  local harness_dir="$1"
  local launcher_bin="$2"
  local out
  local harness_python
  harness_python="$(harness_python3_resolve)" || return 1
  out="$(harness_claude_mcp_output_path "$harness_dir" mcp-light.json)" || return 1
  mkdir -p "$(dirname "$out")" || return 1
  "$harness_python" - "$harness_dir" "$out" "$launcher_bin" <<'PY' || return 1
import json, os, re, sys, tempfile

harness_dir, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, sys.argv[3])
from mcp_paths import normalize_servers

def is_ssh_backed(spec):
    args = spec.get("args") or []
    if spec.get("command") == "bash" and args and "start-ssh-mcp.sh" in str(args[0]):
        return True
    m = re.match(r"https?://(127\.0\.0\.1|localhost):(\d+)(/|$)", str(spec.get("url") or ""))
    return bool(m) and 38200 <= int(m.group(2)) <= 38299

merged, seen = {}, {}
for name in (".mcp.json", ".mcp.local.json", "mcp.local.json"):
    path = os.path.join(harness_dir, name)
    if not os.path.isfile(path):
        continue
    with open(path, encoding="utf-8") as f:
        servers = (json.load(f).get("mcpServers") or {})
    for srv, spec in servers.items():
        if srv in seen:
            print(
                f"ERROR: duplicate MCP server '{srv}' in {seen[srv]} and {path}; "
                "rename the local server instead of overriding committed .mcp.json",
                file=sys.stderr,
            )
            sys.exit(1)
        seen[srv] = path
        if is_ssh_backed(spec):
            continue  # SSH-backed heavy class — excluded from the light surface
        try:
            merged[srv] = normalize_servers({srv: spec}, harness_dir)[srv]
        except ValueError as error:
            print(f"ERROR: {path}: {error}", file=sys.stderr)
            sys.exit(1)

# --strict-mcp-config also drops user-scope servers (~/.claude.json), so the
# generated file must carry every non-SSH server the session would otherwise
# have. Harness scope wins on name collisions (mirrors claude's precedence).
user_cfg = os.path.expanduser("~/.claude.json")
if os.path.isfile(user_cfg):
    try:
        with open(user_cfg, encoding="utf-8") as f:
            user_servers = json.load(f).get("mcpServers") or {}
    except (OSError, ValueError):
        user_servers = {}
    for srv, spec in user_servers.items():
        if srv not in seen and not is_ssh_backed(spec):
            merged[srv] = spec

fd, tmp = tempfile.mkstemp(prefix=".mcp-light.", dir=os.path.dirname(out))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": merged}, f, indent=2)
        f.write("\n")
    os.replace(tmp, out)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
print(out)
PY
}

# --- shared user-facing strings ---------------------------------------------------
harness_ultracode_hint() {
  printf '💡 ultracode는 세션 전용입니다 — 시작 후 /effort 에서 ultracode를 선택하면 워크플로우 오케스트레이션이 켜집니다 (지금은 opus[1m] + xhigh로 시작).\n' >&2
}

# Happy may replace settings or approval policy when starting its own runtime.
# Fail before launching until that boundary can preserve exact user approvals.
harness_slack_happy_guard() {
  if [ -n "${HARNESS_CODEX_SLACK_APPS:-}" ]; then
    echo "❌ Happy cannot enforce Slack user approval; use the native Claude/Codex runtime." >&2
    return 1
  fi
}

# Both launch surfaces use one policy; shell quoting preserves multiline prompts.
harness_slack_codex_argv() {
  local policy="$1" py output
  shift
  if [ -z "${HARNESS_CODEX_SLACK_APPS:-}" ]; then
    HARNESS_SLACK_ARGV=("$@")
    return 0
  fi
  py="$(harness_python3_resolve)" || return $?
  output="$("$py" "$policy" argv "$@")" || return $?
  eval "HARNESS_SLACK_ARGV=($output)"
}

harness_slack_claude_settings() {
  local policy="$1" settings="${2:-}" py
  py="$(harness_python3_resolve)" || return $?
  "$py" "$policy" claude "$settings"
}

harness_slack_claude_argv() {
  local policy="$1" py output
  shift
  py="$(harness_python3_resolve)" || return $?
  output="$("$py" "$policy" claude-argv "$@")" || return $?
  eval "$output"
}

harness_slack_settings_cleanup() {
  if [ -n "${HARNESS_SLACK_SETTINGS_FILE:-}" ]; then
    rm -f -- "$HARNESS_SLACK_SETTINGS_FILE"
    unset HARNESS_SLACK_SETTINGS_FILE
  fi
}
