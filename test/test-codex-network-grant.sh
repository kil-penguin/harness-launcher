#!/usr/bin/env zsh
# Only explicit workspace booleans may become a network grant.
set -e
ROOT="${0:A:h:h}"
source "$ROOT/bin/aliases.zsh"
_HARNESS_LAUNCHER_BIN="$ROOT/bin"
local launch_approval launch_sandbox launch_network launch_bypass
check() {
  local expected="$1"; shift
  _harness_launcher_codex_launch_grant "$@"
  [[ "$launch_network" == "$expected" ]] || { print -u2 -- "FAIL: expected $expected, got $launch_network"; exit 1; }
}
check enabled -s workspace-write -c sandbox_workspace_write.network_access=true
check disabled --sandbox=workspace-write --config=sandbox_workspace_write.network_access=false
check enabled -sworkspace-write -csandbox_workspace_write.network_access=true
check disabled -s workspace-write -c sandbox_workspace_write.network_access=true -c sandbox_workspace_write.network_access=false
check '' -s workspace-write -c 'sandbox_workspace_write.network_access="true"'
check '' -s workspace-write -c sandbox_workspace_write.network_access=true -c 'sandbox_workspace_write={network_access=false}'
check '' -s workspace-write -- -c sandbox_workspace_write.network_access=true
check '' -s workspace-write -m sandbox_workspace_write.network_access=true
check '' -c sandbox_workspace_write.network_access=true
check enabled --dangerously-bypass-approvals-and-sandbox
check enabled -s danger-full-access
print -- 'PASS: exact Codex network grants, precedence and prompt/value boundaries'
