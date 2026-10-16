#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASH_BIN="${BASH_BIN:-$(command -v bash)}"
ZSH_BIN="${ZSH_BIN:-$(command -v zsh)}"
passed=0
skipped=0
has_lockf=0
[[ -x /usr/bin/lockf ]] && has_lockf=1
[[ "${HARNESS_TEST_FORCE_NO_LOCKF:-0}" == "1" ]] && has_lockf=0
# Isolated Claude launches mirror plugin install records into Claude Code's
# user registry. Suites that do not set their own config directory must not
# read the contributor's; test-launcher-isolated.sh re-enables the mirror on a
# fixture CLAUDE_CONFIG_DIR. (A suite-wide CLAUDE_CONFIG_DIR would break tests
# that derive ~/.claude from their fixture HOME.)
export HARNESS_CLAUDE_PLUGIN_MIRROR=0

for test_file in "$ROOT"/test/test-*.sh; do
  test_name="$(basename "$test_file")"
  # These integration tests verify the macOS /usr/bin/lockf kernel-lock contract.
  # Hosted images without that system binary cannot exercise them safely;
  # production preparation and session integration remain fail-closed when
  # serialization needs it.
  if [[ "$has_lockf" -eq 0 ]]; then
    case "$test_name" in
      test-harness-exec-workspace-choice-tui.sh|\
      test-codex-config-preservation.sh|test-codex-global-mcp-drift.sh|\
      test-codex-home-lock.sh|test-codex-home-prepare.sh|\
        test-codex-history-gc.sh|test-codex-history-resume-integration.sh|\
        test-codex-observability-profile.sh|test-codex-migrate-to-symlinks.sh|\
        test-codex-project-trust.sh|\
        test-session-isolation.sh|test-session-isolation-concurrency.sh|\
        test-session-transcript-retention.sh|\
        test-harness-headless.sh|test-headless-target.sh)
        printf '==> %s (SKIP: /usr/bin/lockf unavailable)\n' "$test_name"
        skipped=$((skipped + 1))
        continue
        ;;
    esac
  fi
  IFS= read -r first_line < "$test_file" || true
  if [[ "$first_line" == *zsh* ]]; then
    interpreter="$ZSH_BIN"
  else
    interpreter="$BASH_BIN"
  fi

  printf '==> %s (%s)\n' "$test_name" "$interpreter"
  "$interpreter" "$test_file"
  passed=$((passed + 1))
done


printf '==> test_profile_resolver.py (python3)\n'
python3 -m unittest discover -s "$ROOT/test" -p 'test_profile_resolver.py'
passed=$((passed + 1))

source "$ROOT/bin/harness-common.sh"
PYTHON_BIN="$(harness_python3_resolve)"
printf '==> preservation executor suites (%s)\n' "$PYTHON_BIN"
for python_test in test_git_policy.py test_preservation_snapshot.py test_trusted_preservation.py test_preservation_adversarial.py; do
  "$PYTHON_BIN" "$ROOT/test/$python_test"
done
printf '==> test_mcp_paths.py (%s)\n' "$PYTHON_BIN"
"$PYTHON_BIN" "$ROOT/test/test_mcp_paths.py"
passed=$((passed + 1))

for python_test in test_codex_hook_trust.py test_codex_plugin_inherit.py test_slack_approval_policy.py test_claude_plugin_scope_mirror.py test_codex_app_server_guard.py test_harness_paseo.py test_herdr_harness_plugin.py test_harness_herdr_web.py test_harness_herdr_usage.py test_launch_plan.py test_claude_fork.py test_session_archive.py test_session_report.py test_session_catalog.py test_session_restore.py test_headless_retention_compat.py; do
  printf '==> %s (%s)\n' "$python_test" "$PYTHON_BIN"
  "$PYTHON_BIN" "$ROOT/test/$python_test"
  passed=$((passed + 1))
done

printf '\n%d test scripts passed; %d skipped.\n' "$passed" "$skipped"
