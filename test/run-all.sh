#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Admit only the pinned synthetic default or an explicitly equal owned source.
HARNESS_TEST_OMP_SOURCE="$(python3 -I -S -B "$ROOT/test/omp-fixture-source.py")"
export HARNESS_TEST_OMP_SOURCE
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

mode=full
group=
result=
case "$#" in
  0) ;;
  1) [[ "$1" == --ci-verify ]] || exit 2; mode=verify ;;
  4)
    [[ "$1" == --ci-group && "$3" == --ci-result ]] || exit 2
    case "$2" in headless|shell|preservation|python) group="$2" ;; *) exit 2 ;; esac
    mode=group; result="$4"
    ;;
  *) exit 2 ;;
esac

# One ordered inventory feeds both the full run and every CI group.
inventory=()
add_test() {
  local name="$1" policy="$2" credit="$3" lane=python lock=0
  if [[ "$policy" == shell ]]; then
    lane=shell
    case "$name" in test-harness-headless.sh|test-headless-target.sh) lane=headless ;; esac
    case "$name" in
      test-harness-exec-workspace-choice-tui.sh|\
      test-codex-config-preservation.sh|test-codex-global-mcp-drift.sh|\
      test-codex-home-lock.sh|test-codex-home-prepare.sh|\
      test-codex-history-gc.sh|test-codex-history-resume-integration.sh|\
      test-codex-observability-profile.sh|test-codex-migrate-to-symlinks.sh|\
      test-codex-project-trust.sh|test-session-isolation.sh|\
      test-session-isolation-concurrency.sh|test-session-transcript-retention.sh|\
      test-harness-headless.sh|test-headless-target.sh) lock=1 ;;
    esac
  fi
  case "$name" in
    test_git_policy.py|test_preservation_snapshot.py|test_trusted_preservation.py|test_preservation_adversarial.py) lane=preservation ;;
  esac
  inventory+=("$name|$lane|$policy|$credit|$lock")
}
for test_file in "$ROOT"/test/test-*.sh; do
  add_test "$(basename "$test_file")" shell 1
done
add_test test_profile_resolver.py profile 1
for python_test in test_git_policy.py test_preservation_snapshot.py test_trusted_preservation.py test_preservation_adversarial.py; do
  add_test "$python_test" resolved 0
done
add_test test_mcp_paths.py resolved 1
for python_test in test_codex_hook_trust.py test_codex_plugin_inherit.py test_slack_approval_policy.py test_claude_plugin_scope_mirror.py test_codex_app_server_guard.py test_harness_paseo.py test_herdr_harness_plugin.py test_harness_herdr_web.py test_harness_herdr_cli.py test_harness_herdr_ui_answer.py test_harness_herdr_ui_receipt.py test_harness_herdr_agent_ui_request.py test_harness_herdr_lifecycle.py test_harness_herdr_read.py test_harness_herdr_agent_wait.py test_harness_herdr_guarded.py test_harness_herdr_store.py test_harness_herdr_files.py test_harness_herdr_usage.py test_harness_herdr_launch.py test_harness_herdr_launch_receipt.py test_launch_plan.py test_launch_context.py test_claude_fork.py test_session_archive.py test_session_report.py test_session_catalog.py test_session_restore.py test_headless_retention_compat.py; do
  add_test "$python_test" resolved 1
done
add_test test_omp_fixture_source.py isolated 1
add_test test_run_all_partition.py isolated 1
# End inventory.

# Small fixed CI accounting only. This never executes a test or accepts stdout
# as proof. GitHub needs provenance, not this receipt, authenticates the job.
ci_metadata() {
  python3 -I -S -B - "$1" "$ROOT" "$group" "$result" "${initial_sha:-}" \
    "${completed_csv:-}" "${skipped_csv:-}" "${inventory[@]}" <<'PY'
import hashlib,json,os,re,stat,subprocess,sys
from pathlib import Path

def require(condition):
    if not condition:
        raise ValueError('invalid CI accounting')

def main():
    action,root,lane,result,initial,completed,skipped,*rows=sys.argv[1:]
    root=Path(root).resolve()
    groups=('headless','shell','preservation','python')
    entries=[row.split('|') for row in rows]
    require(entries and all(len(row)==5 for row in entries))
    require(all(re.fullmatch(r'test[-_A-Za-z0-9]+\.(sh|py)',row[0]) and row[1] in groups
                and row[2] in ('shell','profile','resolved','isolated') and row[3] in ('0','1')
                and row[4] in ('0','1') for row in entries))
    ids=[row[0] for row in entries]
    require(len(ids)==len(set(ids)))
    scheduled={g:[row[0] for row in entries if row[1]==g] for g in groups}
    require(len(scheduled['headless'])==2 and len(scheduled['preservation'])==4)
    allow_skip={row[0] for row in entries if row[4]=='1'}
    require(os.environ.get('GITHUB_ACTIONS')=='true')
    def number(name):
        value=os.environ.get(name,'')
        require(re.fullmatch(r'[1-9][0-9]*',value))
        return int(value)
    run_id=number('GITHUB_RUN_ID');attempt=number('GITHUB_RUN_ATTEMPT')
    execution=os.environ.get('GITHUB_SHA','')
    require(re.fullmatch(r'[0-9a-f]{40}',execution))
    actual=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],timeout=15).decode().strip()
    require(actual==execution)
    event=os.environ.get('GITHUB_EVENT_NAME','')
    pr=os.environ.get('CI_PR_HEAD','') or None
    require(event in ('pull_request','push','workflow_dispatch'))
    require((event=='pull_request' and isinstance(pr,str) and re.fullmatch(r'[0-9a-f]{40}',pr))
            or (event!='pull_request' and pr is None))
    raw=subprocess.check_output(['git','-C',str(root),'ls-files','-z'],timeout=15)
    require(len(raw)<=1048576)
    paths=set(os.fsdecode(item) for item in raw.split(b'\0') if item)
    paths.update('test/'+name for name in ids)
    source=[]
    for name in sorted(paths):
        relative=Path(name)
        require(not relative.is_absolute() and '..' not in relative.parts)
        path=root/relative;info=path.lstat()
        if stat.S_ISLNK(info.st_mode):
            data=os.fsencode(os.readlink(path))
        else:
            require(stat.S_ISREG(info.st_mode))
            data=path.read_bytes()
        source.append([name,stat.S_IMODE(info.st_mode),hashlib.sha256(data).hexdigest()])
    digest=hashlib.sha256(json.dumps([entries,source],separators=(',',':')).encode()).hexdigest()
    common=dict(schema=1,run_id=run_id,run_attempt=attempt,execution_sha=execution,
                pr_head_if_applicable=pr,inventory_sha=digest)
    def owned_target():
        target=Path(result);temp=Path(os.environ['RUNNER_TEMP'])
        require(target.is_absolute() and target.name not in ('','.','..'))
        require(not target.parent.is_symlink() and target.parent.resolve()==temp.resolve())
        info=temp.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid() and info.st_mode&0o022==0)
        require(not target.exists() and not target.is_symlink())
        return target
    if action=='start':
        owned_target()
        require(lane in groups)
        print(digest)
        return
    def check(value,g):
        fields=set(common)|{'group','scheduled_ids','completed_ids','skipped_ids_with_original_reason','exit'}
        require(type(value) is dict and set(value)==fields)
        for key,want in common.items():
            require(type(value[key]) is type(want) and value[key]==want)
        require(type(value['exit']) is int and value['exit']==0 and value['group']==g)
        expected=scheduled[g]
        require(type(value['scheduled_ids']) is list and value['scheduled_ids']==expected)
        done=value['completed_ids'];omitted=value['skipped_ids_with_original_reason']
        require(type(done) is list and all(type(item) is str for item in done))
        require(type(omitted) is list)
        skip=[]
        for item in omitted:
            require(type(item) is dict and set(item)=={'id','reason'} and type(item['id']) is str
                    and item['id'] in allow_skip and item['reason']=='lockf-unavailable')
            skip.append(item['id'])
        require(len(done)==len(set(done)) and len(skip)==len(set(skip)) and not set(done)&set(skip))
        require(set(done)|set(skip)==set(expected))
        require(done==[name for name in expected if name not in skip])
        require(skip==[name for name in expected if name in skip])
    if action=='finish':
        require(digest==initial)
        value=dict(common,group=lane,scheduled_ids=scheduled[lane],completed_ids=completed.split(',') if completed else [],
                   skipped_ids_with_original_reason=[dict(id=name,reason='lockf-unavailable') for name in skipped.split(',') if name],exit=0)
        check(value,lane)
        encoded=(json.dumps(value,separators=(',',':'))+'\n').encode()
        require(len(encoded)<=32768)
        target=owned_target()
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as out:
            out.write(encoded);out.flush();os.fsync(out.fileno())
        return
    require(action=='verify')
    union=[]
    for g in groups:
        encoded=os.environ.get('CI_RESULT_'+g.upper(),'')
        require(0<len(encoded.encode())<=32768)
        value=json.loads(encoded)
        check(value,g)
        union.extend(value['scheduled_ids'])
    require(len(union)==len(set(union)) and set(union)==set(ids))
    print('CI_VERIFY_COMPLETE')

try:
    main()
except (ValueError,TypeError,KeyError,OSError,UnicodeError,subprocess.SubprocessError):
    print('CI_VERIFICATION_REFUSED',file=sys.stderr)
    sys.exit(2)
PY
}

if [[ "$mode" == verify ]]; then
  ci_metadata verify
  exit
fi
initial_sha=
completed_csv=
skipped_csv=
if [[ "$mode" == group ]]; then initial_sha="$(ci_metadata start)"; fi
PYTHON_BIN=
preservation_header=0
for entry in "${inventory[@]}"; do
  IFS='|' read -r test_name lane policy credit needs_lockf <<< "$entry"
  if [[ "$mode" == group && "$lane" != "$group" ]]; then continue; fi
  test_file="$ROOT/test/$test_name"
  if [[ "$has_lockf" -eq 0 && "$needs_lockf" -eq 1 ]]; then
    printf '==> %s (SKIP: /usr/bin/lockf unavailable)\n' "$test_name"
    skipped=$((skipped + 1))
    skipped_csv="${skipped_csv}${skipped_csv:+,}$test_name"
    continue
  fi
  case "$policy" in
    shell)
      IFS= read -r first_line < "$test_file" || true
      if [[ "$first_line" == *zsh* ]]; then interpreter="$ZSH_BIN"; else interpreter="$BASH_BIN"; fi
      printf '==> %s (%s)\n' "$test_name" "$interpreter"
      command_argv=("$interpreter" "$test_file")
      ;;
    profile)
      printf '==> test_profile_resolver.py (python3)\n'
      command_argv=(python3 -m unittest discover -s "$ROOT/test" -p test_profile_resolver.py)
      ;;
    resolved|isolated)
      if [[ -z "$PYTHON_BIN" ]]; then
        source "$ROOT/bin/harness-common.sh"
        PYTHON_BIN="$(harness_python3_resolve)"
      fi
      if [[ "$lane" == preservation ]]; then
        if [[ "$preservation_header" -eq 0 ]]; then
          printf '==> preservation executor suites (%s)\n' "$PYTHON_BIN"
          preservation_header=1
        fi
      elif [[ "$test_name" == test_omp_fixture_source.py ]]; then
        printf '==> portable OMP fixture source tests (%s)\n' "$PYTHON_BIN"
      else
        printf '==> %s (%s)\n' "$test_name" "$PYTHON_BIN"
      fi
      command_argv=("$PYTHON_BIN")
      if [[ "$policy" == isolated ]]; then command_argv+=(-I -S -B); fi
      command_argv+=("$test_file")
      ;;
  esac
  if "${command_argv[@]}"; then
    passed=$((passed + credit))
    completed_csv="${completed_csv}${completed_csv:+,}$test_name"
  else
    child_status=$?
    if [[ "$policy" == shell ]]; then
      printf 'REGRESSION_CHILD_FAILED suite=%s interpreter=%s exit=%d\n' "$test_name" "${interpreter##*/}" "$child_status" >&2 || true
    fi
    exit "$child_status"
  fi
done
printf '\n%d test scripts passed; %d skipped.\n' "$passed" "$skipped"
if [[ "$mode" == group ]]; then ci_metadata finish; fi
