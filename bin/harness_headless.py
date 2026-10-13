"""harness-headless: one non-interactive isolated Claude or Codex run with broker delivery.

The caller (an external bridge) gets exactly one JSON result file per run; see
docs/architecture.md "Headless isolated runs" for the contract.
"""
import argparse
import contextlib
import errno
import fcntl
import hmac
import http.client
import json
import math
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.dont_write_bytecode = True
BIN = Path(__file__).resolve().parent
# `-I` keeps the script directory off sys.path; the launcher's own modules
# sit beside this file.
sys.path.insert(0, str(BIN))
import harness_target  # noqa: E402
from harness_target import Refused  # noqa: E402
UUID = r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}'
SESSION_LINE = re.compile(r'^harness-launcher: isolated session (' + UUID + r');', re.M)
# Caller environment kept for the launcher and Claude. Everything else
# (API keys, bot tokens, ...) is dropped before anything is launched.
ENV_ALLOW = ('HOME', 'PATH', 'USER', 'LOGNAME', 'SHELL', 'LANG')
# Launcher state location only, so `harness-session` sees the same sessions.
ENV_LAUNCHER = ('HARNESS_SESSION_STATE_HOME', 'XDG_STATE_HOME', 'HARNESS_SESSION_RETENTION_SECONDS')
HOME_DENY = ('.hermes', 'buzz', '.ssh', '.config/gh', '.aws', '.claude')
# Edit-tool denies only (persistence vectors that are not secrets).
HOME_WRITE_DENY = ('Library/LaunchAgents',)
BASH_DENY = ('harness-session', 'session-isolation.sh', 'auto-deliver', 'git push', 'sudo', 'launchctl')
# Prefix form only: a wildcard would also block paths such as docs/*buzz*/.
BASH_PREFIX_DENY = ('hermes', 'buzz', 'rm -rf')
EDIT_TOOLS = ('Edit', 'Write', 'NotebookEdit')
# Broker integration and session GC serialize on this macOS lock tool and fail
# closed without it.
LOCKF = '/usr/bin/lockf'
# The broker runs the repository verifier (candidate tests) under Seatbelt.
SANDBOX_EXEC = '/usr/bin/sandbox-exec'
SUMMARY_MAX = 3000
# The agent's message for the delivery commit (see agent_commit_message).
COMMIT_MESSAGE = 'commit-message'
MESSAGE_MAX_BYTES = 8192
MESSAGE_MAX_LINES = 200
SUBJECT_MAX = 100
# Trailer keys dropped from the message (see trailer_key): the broker appends
# Harness-Session itself, and skip-checks would switch off the checks.
DROPPED_TRAILERS = ('harnesssession', 'skipchecks')
# CI-skip directives (GitHub Actions and most CI systems): the delivered
# commit must not switch off the repository's checks.
CI_SKIP_TOKEN = re.compile(r'\[\s*(?:skip\s+ci|ci\s+skip|no\s+ci|skip\s+actions|actions\s+skip)\s*\]', re.I)
CI_SKIP_LEADING = re.compile(r'^[ \t]*(?:' + CI_SKIP_TOKEN.pattern + r')[ \t]*', re.I | re.M)
DELIVERY_NOTE = (
    '---\n'
    'Delivery note from the launcher: when you finish, the launcher commits your changes to the repository '
    'as one commit. Your own git commits are not kept as commits. Write the message for that commit with Bash '
    'to the file named by $HARNESS_COMMIT_MESSAGE_FILE: a subject line of at most 72 characters, a blank line, '
    'then the body. Follow the repository\'s commit conventions. If you do not write it, a generic message is used.')
EXIT_TIMEOUT = 124
EXIT_REFUSED = 2
# --effort per agent: Claude's effortLevel setting (max is session-only) and
# codex model_reasoning_effort.
EFFORTS = {'claude': ('low', 'medium', 'high', 'xhigh'), 'codex': ('minimal', 'low', 'medium', 'high', 'xhigh')}
CODEX_ONLY = ('model_endpoint', 'endpoint_key_file', 'max_model_requests')
MAX_MODEL_REQUESTS = 400
# The only model endpoint form: plain HTTP on IPv4 loopback, path /v1.
ENDPOINT = re.compile(r'http://127\.0\.0\.1:([0-9]{1,5})/v1')
# A codex model name goes into config.toml; keep it to a plain identifier.
CODEX_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}')
KEY_MAX_BYTES = 4096
CODEX_HOME = 'codex-home'
LAST_MESSAGE = 'last-message.md'
CODE_MODE_HOST = 'codex-code-mode-host'
HEARTBEAT_SECONDS = 30
# Pinned in config.toml; the forwarder waits longer than codex for a stream chunk.
STREAM_IDLE_MS = 300000
FORWARD_UPSTREAM_TIMEOUT = STREAM_IDLE_MS / 1000 + 30
FORWARD_BODY_MAX = 32 * 1024 * 1024
FORWARD_ROUTES = (('POST', '/v1/responses'), ('GET', '/v1/models'))
HOP_BY_HOP = ('connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
              'trailers', 'transfer-encoding', 'upgrade', 'proxy-connection')
# The only request headers forwarded upstream (plus x-codex-*): what codex
# 0.160 sends (the real-binary test checks it) and the OpenAI client headers.
# The forwarder sets Host, Content-Length and Authorization itself.
FORWARD_HEADERS = ('accept', 'content-type', 'user-agent', 'openai-beta', 'originator', 'version',
                   'session_id', 'session-id', 'conversation_id', 'conversation-id', 'thread-id',
                   'x-client-request-id', 'x-openai-internal-codex-responses-lite')
FORWARD_TOKEN_HEADER = 'X-Harness-Forwarder-Token'
FORWARD_CONCURRENCY = 8


class Outcome(Exception):
    """A final result other than refused, with its summary."""

    def __init__(self, status, summary):
        super().__init__(summary)
        self.status = status


class Terminated(Exception):
    pass


def _terminate(signum, frame):
    raise Terminated(signum)


def parse_args(argv):
    parser = argparse.ArgumentParser(prog='harness-headless', allow_abbrev=False)
    parser.add_argument('harness')
    for name in ('--prompt-file', '--result-file', '--lock-file', '--budget-usd', '--timeout-min'):
        parser.add_argument(name, required=True)
    parser.add_argument('--settings-file')
    parser.add_argument('--model')
    # Validated in validate_agent_args, so a bad value still writes a result.
    for name in ('--agent', '--effort', '--model-endpoint', '--endpoint-key-file', '--max-model-requests',
                 '--target', '--target-digest', '--task-id', '--approval-sha',
                 '--journal-board', '--journal-task'):
        parser.add_argument(name)
    return parser.parse_args(argv)


JOURNAL_HOOK = '.local/share/harness-service/bin/harness-session-hook'
JOURNAL_BOARD = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')
JOURNAL_TASK = re.compile(r't_[0-9a-f]{8}')
JOURNAL_TIMEOUT = 3


def validate_journal_args(args):
    """The Kanban board and task of this run (P3-L auto-link): both or neither."""
    board, task = args.journal_board, args.journal_task
    if board is None and task is None:
        return
    if board is None or task is None:
        raise Refused('--journal-board and --journal-task go together')
    if not JOURNAL_BOARD.fullmatch(board) or not JOURNAL_TASK.fullmatch(task):
        raise Refused('--journal-board/--journal-task have an invalid format')


def journal_run_start(args, home):
    """Tell the optional host collector callback that a Kanban-bound run starts.

    Without both journal arguments nothing happens, so Buzz capture stays off
    until the caller passes them (and the collector enables the runtime). The
    payload holds ids only. Fail-open: output is discarded and nothing here can
    change the run.
    """
    if args.journal_board is None or args.journal_task is None:
        return
    script = os.path.join(home, JOURNAL_HOOK)
    if not (os.path.isfile(script) and os.access(script, os.X_OK)):
        return
    payload = json.dumps({'hook_event_name': 'RunStart', 'run_id': str(uuid.uuid4()),
                          'board': args.journal_board, 'task': args.journal_task})
    try:
        subprocess.run(['/bin/sh', script, '--runtime', 'buzz'], input=payload.encode(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=JOURNAL_TIMEOUT, check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass


def validate_agent_args(args):
    """Refuse agent options that do not belong to --agent; returns the codex
    endpoint port and request cap (None for claude)."""
    agent = args.agent or 'claude'
    if agent not in EFFORTS:
        raise Refused('--agent must be claude or codex')
    if args.effort is not None and args.effort not in EFFORTS[agent]:
        raise Refused(f'--effort for {agent} must be one of {", ".join(EFFORTS[agent])}')
    if agent == 'claude':
        given = [name for name in CODEX_ONLY if getattr(args, name) is not None]
        if given:
            raise Refused(f'--{given[0].replace("_", "-")} is for --agent codex only')
        return None
    if args.settings_file:
        raise Refused('--settings-file holds Claude settings; it cannot apply to --agent codex')
    if not (args.model and CODEX_MODEL.fullmatch(args.model)):
        raise Refused('--agent codex needs --model, a plain model name')
    match = ENDPOINT.fullmatch(args.model_endpoint or '')
    if not (match and 0 < int(match.group(1)) < 65536):
        raise Refused('--agent codex needs --model-endpoint http://127.0.0.1:<port>/v1')
    if not args.endpoint_key_file:
        raise Refused('--agent codex needs --endpoint-key-file')
    cap = args.max_model_requests if args.max_model_requests is not None else str(MAX_MODEL_REQUESTS)
    if not (re.fullmatch(r'[0-9]{1,9}', cap) and int(cap) > 0):
        raise Refused('--max-model-requests must be a positive integer')
    return int(match.group(1)), int(cap)


def positive(value, name):
    try:
        number = float(value)
    except ValueError:
        number = math.nan
    if not (math.isfinite(number) and number > 0):
        raise Refused(f'{name} must be a positive number')
    return number


def harness_dir(name):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', name):
        raise Refused(f'invalid harness name: {name}')
    home = os.environ.get('HARNESS_PROFILE_HOME') or os.path.join(
        os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'harness-launcher')
    record = Path(home, 'profiles', name)
    if not record.is_file() or record.is_symlink():
        raise Refused(f'harness is not registered: {name}')
    lines = record.read_text().splitlines()
    root = lines[0] if lines else ''
    if not root or not Path(root, 'config', 'launcher.env').is_file():
        raise Refused(f'registered harness is unavailable: {name}')
    return root


def child_env(run_tmp):
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOW + ENV_LAUNCHER or k.startswith('LC_')}
    # One private temp base per run: Claude keeps its per-uid dir (Bash cwd
    # tracking) under CLAUDE_CODE_TMPDIR, which must stay short for AF_UNIX
    # sockets, so the shared /private/tmp/claude-<uid> can stay write-denied.
    env.update(TMPDIR=run_tmp, CLAUDE_CODE_TMPDIR=run_tmp)
    # HARNESS_HEADLESS selects the headless clone and skips harness-local
    # secrets; the interpreter is the one the launcher shim already resolved.
    env.update(HARNESS_HEADLESS='1', HARNESS_PYTHON_BIN=sys.executable, GIT_TERMINAL_PROMPT='0')
    # The only place the agent can hand the launcher a commit message: the
    # sandbox may write the run's temp base, never the session record.
    env['HARNESS_COMMIT_MESSAGE_FILE'] = os.path.join(run_tmp, COMMIT_MESSAGE)
    return env


def trailer_key(line):
    """The trailer key of a line compared loosely, so no spoof of a dropped
    trailer survives: compatibility forms (full-width letters and colons)
    folded by NFKC, case folded, and every non-alphanumeric character in the
    key (spaces, hyphens, underscores) removed. None without a colon."""
    norm = unicodedata.normalize('NFKC', line).casefold().replace('\u2236', ':')
    key, colon, _ = norm.partition(':')
    return re.sub(r'[\W_]', '', key) if colon else None


def read_agent_file(path, limit, what):
    """(bytes of at most limit + 1, None) from a file the sandboxed agent could
    write, or (None, reason): no symlink, special file or hard link (another
    file's content). Never raises."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, f'no {what}'
    except OSError as exc:
        return None, f'unreadable {what} ({exc.strerror})'
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, f'{what} is not a regular file'
        # Defence in depth: the sandbox cannot link read-denied files.
        if info.st_nlink != 1:
            return None, f'{what} has more than one link'
        return os.read(fd, limit + 1), None
    except OSError as exc:
        return None, f'unreadable {what} ({exc.strerror})'
    finally:
        os.close(fd)


def agent_commit_message(run_tmp):
    """(message, None) from the agent's message file, or (None, reason). The
    file is agent-written: no symlink or special file, bounded, strict UTF-8,
    no control characters but LF and TAB, no reserved trailer. Never raises."""
    data, reason = read_agent_file(os.path.join(run_tmp, COMMIT_MESSAGE), MESSAGE_MAX_BYTES, 'message file')
    if data is None:
        return None, reason
    if len(data) > MESSAGE_MAX_BYTES:
        return None, f'message file is larger than {MESSAGE_MAX_BYTES} bytes'
    try:
        text = data.decode('utf-8', errors='strict')
    except UnicodeError:
        return None, 'message file is not valid UTF-8'
    # Line and paragraph separators split lines for viewers, so they are lines.
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\u2028', '\n').replace('\u2029', '\n')
    text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]', '', text)
    # Format characters (bidi overrides, zero-width) could hide or reorder text.
    text = ''.join(c for c in text if unicodedata.category(c) != 'Cf')
    while CI_SKIP_TOKEN.search(text):
        # A token that starts a line takes its following blanks with it.
        text = CI_SKIP_LEADING.sub('', text)
        text = CI_SKIP_TOKEN.sub('', text)
    lines = [line.rstrip() for line in text.split('\n')
             if trailer_key(line) not in DROPPED_TRAILERS]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if not lines or len(lines[0]) > SUBJECT_MAX or len(lines) > MESSAGE_MAX_LINES:
        return None, 'message has no subject, a subject over 100 characters or over 200 lines'
    return '\n'.join(lines) + '\n', None


def record_commit_message(state, sid, message):
    """Atomically write the message to the launcher-owned session record,
    where the broker reads it. Returns None, or why it was not written."""
    record = state / 'sessions' / sid
    target = record / COMMIT_MESSAGE
    try:
        if target.is_symlink():
            return 'the session record has a symlink at commit-message'
        fd, tmp = tempfile.mkstemp(dir=record, prefix='.commit-message.')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                out.write(message)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, target)
        except BaseException:
            os.unlink(tmp)
            raise
    except OSError as exc:
        return f'could not write the session record ({exc.strerror})'
    return None


def broker_env(env):
    """The run environment without the run's temp base: broker git and the
    repository verifier write and execute their own temp files, which must
    never sit in a directory the sandboxed agent could write."""
    env = {k: v for k, v in env.items() if k != 'CLAUDE_CODE_TMPDIR'}
    env['TMPDIR'] = tempfile.gettempdir()
    return env


def state_home(env):
    if env.get('HARNESS_SESSION_STATE_HOME'):
        return Path(env['HARNESS_SESSION_STATE_HOME'])
    base = env.get('XDG_STATE_HOME') or os.path.join(env['HOME'], '.local', 'state')
    return Path(base, 'harness-launcher')


def mandatory_settings(source, home, caller_deny_read, run_tmp=None, effort=None):
    """Launcher-owned containment. Passed last, so the launcher's settings
    merge (dicts deep-merged, lists unioned, scalars last-wins) keeps every
    value here over any earlier --settings. Every sandbox read-denied path is
    also denied to the Read and edit tools, which the sandbox does not cover.
    --effort is effortLevel here; xhigh needs thinking on (as the launcher
    forces for its own xhigh), whatever the user settings say."""
    settings = _mandatory_settings(source, home, caller_deny_read, run_tmp)
    if effort:
        settings['effortLevel'] = effort
        if effort == 'xhigh':
            settings['alwaysThinkingEnabled'] = True
    return settings


def _mandatory_settings(source, home, caller_deny_read, run_tmp):
    uid = os.getuid()
    homes = [os.path.join(home, d) for d in HOME_DENY]
    sources = [source, os.path.realpath(source)]
    # The session clone has its own Git directory (no hardlinks or alternates)
    # and copied local files, so the canonical source root can be read-denied.
    deny_read = list(dict.fromkeys(homes + sources + list(caller_deny_read)))
    # Launcher records (profiles, target records and policy, the endpoint key)
    # are written only by the owner, never by a run.
    deny_write = [f'/private/tmp/claude-{uid}', f'/tmp/claude-{uid}', harness_target.profile_home()]
    write_paths = deny_read + [os.path.join(home, d) for d in HOME_WRITE_DENY] + deny_write
    deny = [f'Read(/{path}/**)' for path in deny_read]
    deny += [f'{tool}(/{path}/**)' for path in write_paths for tool in EDIT_TOOLS]
    deny += ['WebFetch', 'WebSearch']
    for word in BASH_DENY:
        deny += [f'Bash({word}:*)', f'Bash(*{word}*)']
    deny += [f'Bash({word}:*)' for word in BASH_PREFIX_DENY]
    return {'permissions': {'deny': deny},
            'disableAllHooks': True,
            'disableBypassPermissionsMode': 'disable',
            'sandbox': {'enabled': True, 'failIfUnavailable': True, 'allowUnsandboxedCommands': False,
                        'autoAllowBashIfSandboxed': True,
                        'network': {'strictAllowlist': True, 'allowedDomains': []},
                        'filesystem': {'denyRead': deny_read, 'denyWrite': deny_write,
                                       'allowWrite': [run_tmp] if run_tmp else []}}}


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


# The only caller settings accepted: additions that restrict further, plus the
# network allowlist. Any other key could weaken containment.
CALLER_SHAPE = {'_note': str, 'permissions': {'deny': _strings},
                'sandbox': {'filesystem': {'denyRead': _strings}, 'network': {'allowedDomains': _strings}}}


def validate_caller(value, shape=CALLER_SHAPE, where='settings'):
    if not isinstance(value, dict):
        raise Refused(f'--settings-file {where} must be a JSON object')
    for key, item in value.items():
        rule = shape.get(key)
        if rule is None:
            raise Refused(f'--settings-file key is not allowed: {where}.{key}')
        if isinstance(rule, dict):
            validate_caller(item, rule, f'{where}.{key}')
        elif not (isinstance(item, rule) if isinstance(rule, type) else rule(item)):
            raise Refused(f'--settings-file value has the wrong type: {where}.{key}')


def claude_result(stdout):
    for line in reversed(stdout.splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and ('is_error' in data or data.get('type') == 'result'):
            return data
    return None


def journal_state(state, sid):
    try:
        for line in (state / 'sessions' / sid / 'journal').read_text().splitlines():
            if line.startswith('state='):
                return line[len('state='):]
    except OSError:
        pass
    return ''


def launched_session(log_text, state, source):
    """The launcher-announced session, accepted only when it is genuine."""
    match = SESSION_LINE.search(log_text)
    if not match:
        return None
    sid = match.group(1)
    try:
        recorded = (state / 'sessions' / sid / 'source-root').read_text().strip()
    except OSError:
        return None
    return sid if os.path.realpath(recorded) == os.path.realpath(source) else None


def transcript_path(home, session_root, claude_sid):
    if not (claude_sid and re.fullmatch(UUID, claude_sid) and session_root):
        return None
    path = Path(home, '.claude', 'projects', re.sub(r'[^A-Za-z0-9]', '-', session_root), claude_sid + '.jsonl')
    return str(path) if path.is_file() else None


def kill_group(pgid):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        if sig == signal.SIGTERM:
            time.sleep(2)


def kill_lingering(*roots):
    """SIGKILL every process of this user whose cwd or an open file is inside
    one of roots (the session root and the run's temp base), so nothing changes
    the work tree or broker inputs while the broker reads them.
    ponytail: one lsof snapshot; a process with no cwd or open file under the
    root at that instant, or one started after it, escapes. Upgrade path: run
    the agent as a dedicated macOS user and kill all of that user's processes."""
    lsof = '/usr/sbin/lsof'
    if not os.path.exists(lsof):
        return []
    out = subprocess.run([lsof, '-nP', '-w', '-u', str(os.getuid()), '-Fpn'], stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout
    roots = [r.rstrip('/') for r in roots if r]
    pid, victims = None, set()
    for line in out.splitlines():
        if line.startswith('p'):
            pid = int(line[1:])
        elif line.startswith('n') and pid not in (None, os.getpid()):
            name = line[1:]
            if any(name == r or name.startswith(r + '/') for r in roots):
                victims.add(pid)
    for victim in victims:
        try:
            os.kill(victim, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return sorted(victims)


NO_TRUSTED_GIT = 'headless session record has no trusted git directory; delivery refused, session kept'
SOURCE_MISSING = ('the source checkout this session was cloned from could not be found (moved or deleted); '
                  'delivery refused, session kept')
ROOT_REPLACED = ('the session root was replaced during the run (a symlink or another directory); nothing staged '
                 'or delivered, session kept')
VERIFIER_REJECTED = ('the repository verifier rejected the session; it runs sandboxed (no network, not even '
                     'loopback TCP; no credentials; writes only to its candidate); see the run log; session kept')


def deliver(sid, state, env, cwd, log, message=None):
    """Broker delivery after a successful run: (status, commit, reason).
    The agent's commit message is recorded for the broker only while `close`
    runs; every outcome other than DELIVERED removes it again."""
    record = state / 'sessions' / sid / COMMIT_MESSAGE
    outcome = None
    try:
        outcome = _deliver(sid, state, env, cwd, log, message)
        return outcome
    finally:
        if outcome is None or outcome[0] != 'delivered':
            try:
                record.unlink()
            except FileNotFoundError:
                pass


def _deliver(sid, state, env, cwd, log, message):
    iso = str(BIN / 'session-isolation.sh')

    def session(*args):
        # A new session: the repository verifier the broker runs has no
        # controlling terminal, so /dev/tty never reaches the operator's.
        return subprocess.run([iso, *args, sid], env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                              stdout=log, stderr=log, start_new_session=True).returncode

    def delivered():
        sha = (state / 'sessions' / sid / 'delivered-sha').read_text().strip()
        return 'delivered', sha, None

    current = journal_state(state, sid)
    if current == 'CLOSED':
        return 'no_changes', None, None
    if current != 'ABANDONED' or session('recover') != 0:
        return 'failed', None, None
    # The session has changes and is about to be closed through the broker.
    reason = record_commit_message(state, sid, message) if message else 'no usable message file'
    print(f'harness-headless: commit message: {"agent" if not reason else f"generic ({reason})"}', file=log, flush=True)
    rc = session('close')
    after = journal_state(state, sid)
    if after == 'INTEGRATING':
        # Indeterminate integration (readback outage, exit 4 or a git 128):
        # prove the pending commit once.
        session('recover')
        after = journal_state(state, sid)
    if after == 'DELIVERED':
        return delivered()
    if rc == 0 and after == 'CLOSED':
        return 'no_changes', None, None
    if rc == 6:
        return 'refused', None, NO_TRUSTED_GIT
    if rc == 8:
        return 'refused', None, SOURCE_MISSING
    if rc == 10:
        return 'refused', None, ROOT_REPLACED
    if rc == 9:
        return 'failed', None, VERIFIER_REJECTED
    if rc in (3, 5):
        return 'conflict', None, None
    return 'failed', None, None


def endpoint_key(path):
    """The model endpoint key: a regular file (no symlink), mode 0600, owned
    by this user, one printable token. The key never appears in a message."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise Refused(f'--endpoint-key-file is unusable: {exc.strerror} (a symlink is refused)')
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused('--endpoint-key-file must be a regular file')
        if info.st_uid != os.getuid():
            raise Refused('--endpoint-key-file must be owned by the current user')
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise Refused('--endpoint-key-file must have mode 0600')
        if info.st_nlink != 1:
            raise Refused('--endpoint-key-file must have exactly one link')
        data = os.read(fd, KEY_MAX_BYTES + 1)
    except OSError as exc:
        raise Refused(f'--endpoint-key-file is unreadable: {exc.strerror}')
    finally:
        os.close(fd)
    key = data.strip()
    if not re.fullmatch(rb'[\x21-\x7e]{1,%d}' % KEY_MAX_BYTES, key):
        raise Refused('--endpoint-key-file must hold one printable token')
    return key.decode('ascii')


def unique_keys(pairs):
    """json object_pairs_hook: a duplicate key is an error, since a parser
    upstream may read the other value (a second `model`)."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate key')
    return dict(pairs)


def forwarded_header(name, listed):
    name = name.lower()
    return name not in listed and (name in FORWARD_HEADERS or name.startswith('x-codex-'))


class ForwardHandler(BaseHTTPRequestHandler):
    """One forwarder request. Never logs: headers and bodies carry the key
    and the conversation."""

    # Seconds a client may stay silent (request line, headers, body).
    timeout = 60

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def reject(self, code, reason):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Connection', 'close')
        body = json.dumps({'error': {'message': f'harness-headless forwarder: {reason}'}}).encode()
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def admit(self, fw):
        """(body, None) for a request to forward, or (None, (code, reason))."""
        # DNS rebinding and browser requests: only the exact Host, no Origin.
        if self.headers.get_all('Host') != [f'127.0.0.1:{fw.port}'] or 'Origin' in self.headers:
            return None, (403, 'host or origin not allowed')
        # The per-run capability from config.toml: other local processes
        # cannot use the forwarder.
        tokens = self.headers.get_all(FORWARD_TOKEN_HEADER) or []
        if len(tokens) != 1 or not hmac.compare_digest(tokens[0].encode(), fw.token.encode()):
            return None, (403, 'forwarder token missing or wrong')
        if (self.command, self.path) not in FORWARD_ROUTES:
            return None, (404, 'only POST /v1/responses and GET /v1/models')
        if self.command == 'GET':
            return b'', None
        lengths = self.headers.get_all('Content-Length') or []
        if 'Transfer-Encoding' in self.headers or len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,10}', lengths[0]):
            return None, (411, 'one Content-Length is required')
        if int(lengths[0]) > FORWARD_BODY_MAX:
            return None, (413, 'request body too large')
        body = self.rfile.read(int(lengths[0]))
        try:
            model = json.loads(body, object_pairs_hook=unique_keys).get('model')
        except (ValueError, AttributeError, RecursionError):
            return None, (400, 'request body is not a JSON object with unique keys')
        if model != fw.model:
            return None, (403, 'model not allowed')
        with fw.lock:
            if fw.requests >= fw.max_requests:
                fw.exhausted = True
                return None, (403, 'model request cap reached')
            fw.requests += 1
        return body, None

    def forward(self):
        fw = self.server.forwarder
        try:
            if not fw.slots.acquire(blocking=False):
                self.reject(503, 'too many requests at once')
                return
            try:
                self.relay(fw)
            finally:
                fw.slots.release()
        except Exception:
            pass  # never a traceback: the connection just closes

    def relay(self, fw):
        body, rejection = self.admit(fw)
        if rejection:
            self.reject(*rejection)
            return
        listed = {h.strip().lower() for v in self.headers.get_all('Connection') or [] for h in v.split(',')}
        headers = {k: v for k, v in self.headers.items() if forwarded_header(k, listed)}
        headers['Authorization'] = f'Bearer {fw.key}'
        if self.command == 'POST':
            headers['Content-Length'] = str(len(body))
        upstream = http.client.HTTPConnection('127.0.0.1', fw.upstream_port, timeout=FORWARD_UPSTREAM_TIMEOUT)
        try:
            try:
                upstream.request(self.command, self.path, body=body or None, headers=headers)
                resp = upstream.getresponse()
            except OSError:
                self.reject(502, 'model endpoint unreachable')
                return
            if 300 <= resp.status < 400:
                self.reject(502, 'model endpoint redirected')
                return
            self.send_response(resp.status)
            for name, value in resp.getheaders():
                if name.lower() not in HOP_BY_HOP + ('content-length', 'set-cookie'):
                    self.send_header(name, value)
            # HTTP/1.0 framing: the body ends when the connection closes.
            self.send_header('Connection', 'close')
            self.end_headers()
            # Unbuffered relay (wfile has no buffer): each SSE chunk as it arrives.
            while chunk := resp.read1(65536):
                self.wfile.write(chunk)
        finally:
            # Also when codex went away mid-stream: the upstream closes too.
            upstream.close()


class ForwardServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # no tracebacks: they would name the request


class Forwarder:
    """Per-run model forwarder, a thread of the launcher outside the sandbox
    and the only holder of the key. Listens on 127.0.0.1, random port, and
    also owns ::1 at that port (bound, never listening) so nothing else can
    serve there: Seatbelt names loopback only as `localhost`."""

    def __init__(self, upstream_port, key, model, max_requests):
        self.upstream_port, self.key, self.model, self.max_requests = upstream_port, key, model, max_requests
        self.requests, self.exhausted, self.lock = 0, False, threading.Lock()
        self.slots = threading.BoundedSemaphore(FORWARD_CONCURRENCY)
        self.token = secrets.token_hex(16)
        self.server = self.v6 = None
        self.port = None

    def start(self):
        for _ in range(20):
            server = ForwardServer(('127.0.0.1', 0), ForwardHandler)
            v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            try:
                v6.bind(('::1', server.server_address[1]))
            except OSError as exc:
                v6.close()
                if exc.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
                    v6 = None  # no ::1 on this host: nothing to own
                else:
                    server.server_close()
                    continue
            break
        else:
            raise OSError(errno.EADDRINUSE, 'no port free on both 127.0.0.1 and ::1')
        self.server, self.v6 = server, v6
        server.forwarder = self
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def models_ok(self):
        try:
            conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
            conn.request('GET', '/v1/models', headers={FORWARD_TOKEN_HEADER: self.token})
            return conn.getresponse().status == 200
        except OSError:
            return False

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.v6:
            self.v6.close()


def codex_binary():
    """(codex, code-mode host): the codex the launcher's own resolution picks
    (HARNESS_CODEX_BIN, then PATH), symlinks followed, and the
    codex-code-mode-host beside it, which code-mode models (gpt-6.1-sol)
    need. The sandbox allows exactly these two files."""
    env = {k: v for k, v in os.environ.items()
           if k in ('PATH', 'HOME', 'HARNESS_CODEX_BIN', 'HARNESS_CODEX_ALLOW_APP_FALLBACK')}
    found = subprocess.run(['/bin/zsh', '-f', '-c', 'source "$1" && harness_codex_bin_resolve', 'zsh',
                            str(BIN / 'harness-common.sh')], env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
    path = found.stdout.strip()
    if found.returncode != 0 or not path:
        raise Refused('codex binary not found (PATH or HARNESS_CODEX_BIN)')
    real = os.path.realpath(path)
    if not (os.path.isfile(real) and os.access(real, os.X_OK)):
        raise Refused(f'codex binary is not an executable file: {real}')
    host = os.path.join(os.path.dirname(real), CODE_MODE_HOST)
    if os.path.islink(host) or not (os.path.isfile(host) and os.access(host, os.X_OK)):
        raise Refused(f'no executable {CODE_MODE_HOST} beside {real}; code-mode models need it. Set '
                      'HARNESS_CODEX_BIN to a codex install that ships it (the npm vendor build does)')
    for file in (real, host):
        st = os.stat(file)
        if st.st_uid not in (os.getuid(), 0) or st.st_mode & 0o022:
            raise Refused(f'{file} is writable by others: it must be owned by this user or root, '
                          'with no group or other write')
    return real, host


def codex_agents_md(root):
    """CODEX_HOME/AGENTS.md: the session clone's .claude/rules/*.md but
    _index.md, concatenated as codex-home-prepare.sh falls back to. Read
    outside the sandbox before codex starts, so a symlink is refused."""
    rules = Path(root, '.claude', 'rules')
    parts = ['# Generated by harness-headless — do not edit manually.\n',
             f'# Source-of-truth: {rules}/*.md\n', '\n']
    if Path(root, '.claude').is_symlink() or rules.is_symlink():
        raise Refused('session .claude/rules is a symlink')
    for path in sorted(rules.glob('*.md')) if rules.is_dir() else []:
        if path.name == '_index.md':
            continue
        data, reason = read_agent_file(str(path), 1024 * 1024, f'rule file {path.name}')
        if data is None:
            raise Refused(f'session rules: {reason}')
        parts += [f'## {path.stem}\n', '\n', data.decode('utf-8', errors='replace'), '\n']
    return ''.join(parts)


def codex_config(model, effort, forwarder):
    lines = [f'model = {json.dumps(model)}', 'model_provider = "loop"']
    if effort:
        lines.append(f'model_reasoning_effort = "{effort}"')
    lines += ['approval_policy = "never"', 'sandbox_mode = "danger-full-access"',
              'check_for_update_on_startup = false', '',
              '[model_providers.loop]', 'name = "loop"',
              f'base_url = "http://127.0.0.1:{forwarder.port}/v1"', 'wire_api = "responses"',
              'supports_websockets = false', 'requires_openai_auth = false',
              f'http_headers = {{ "{FORWARD_TOKEN_HEADER}" = "{forwarder.token}" }}',
              f'stream_idle_timeout_ms = {STREAM_IDLE_MS}', '', '[history]', 'persistence = "none"']
    return '\n'.join(lines) + '\n'


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(text)


def codex_events(stdout):
    """(num_turns, usage) from `codex exec --json`: turn.completed events."""
    turns, usage = 0, {'input_tokens': 0, 'output_tokens': 0}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'turn.completed':
            turns += 1
            counts = event.get('usage') if isinstance(event.get('usage'), dict) else {}
            for name in usage:
                if isinstance(counts.get(name), int):
                    usage[name] += counts[name]
    return (turns, usage) if turns else (None, None)


class CodexSession:
    """An isolated session for a codex run, the launcher's way: GC, create
    (headless clone), lease, announcement, heartbeat, and exit at the end."""

    def __init__(self, hdir, env, log):
        self.iso, self.env, self.log = str(BIN / 'session-isolation.sh'), env, log
        if self.call('gc', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
            print('harness-launcher: warning: isolated-session GC failed; workspaces retained', file=log, flush=True)
        made = subprocess.run([self.iso, 'create', hdir], env=env, cwd=hdir, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True)
        log.write(made.stderr)
        log.flush()
        if made.returncode != 0:
            if 'headless clone refused' in made.stderr:
                raise Refused('isolated session refused: symlinked machine-local path in the harness tree')
            raise RuntimeError(f'isolated session could not be created (exit {made.returncode}); see the run log')
        fields = dict(line.split('=', 1) for line in made.stdout.splitlines() if '=' in line)
        self.sid, self.root = fields['HARNESS_SESSION_ID'], fields['HARNESS_SESSION_ROOT']
        self.lease = None
        try:
            lease = os.open(Path(state_home(env), 'sessions', self.sid, 'runtime.lock'), os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.lockf(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                os.close(lease)
                raise
        except BaseException:
            # No unleased OPEN session is left behind (it closes clean).
            self.call('exit', self.sid, stdout=log, stderr=log)
            raise
        self.lease = lease
        print(f'harness-launcher: isolated session {self.sid}; headless codex run', file=log, flush=True)
        self.stop = threading.Event()
        self.beat = threading.Thread(target=self.heartbeat, daemon=True)
        self.beat.start()

    extra_env = {}

    def call(self, *args, **kw):
        return subprocess.run([self.iso, *args], env=self.env, stdin=subprocess.DEVNULL, **kw).returncode

    def agents_md(self):
        return codex_agents_md(self.root)

    def profile_suffix(self, base):
        return ''

    def heartbeat(self):
        while not self.stop.wait(HEARTBEAT_SECONDS):
            self.call('heartbeat', self.sid, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def finish(self):
        """Once: OPEN goes to ABANDONED (changes) or CLOSED; the lease goes."""
        if self.lease is None:
            return
        self.stop.set()
        self.beat.join()
        if self.call('exit', self.sid, stdout=self.log, stderr=self.log):
            print(f'harness-launcher: warning: failed to finalize isolated session {self.sid}; workspace retained',
                  file=self.log, flush=True)
        os.close(self.lease)
        self.lease = None


def codex_sandbox_profile(session, hdir, run_tmp, exe, host, port, codex_home):
    made = subprocess.run([session.iso, 'codex-sandbox-profile', session.root, run_tmp, session.env['HOME'],
                           hdir, exe, str(port), codex_home, host], env=session.env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True)
    if made.returncode != 0:
        raise Refused(f'the codex sandbox profile could not be generated: {made.stderr.strip()[-300:]}')
    path = os.path.join(run_tmp, 'codex.sb')
    write_private(path, made.stdout + session.profile_suffix(made.stdout))
    return path


def codex_preflight(profile, exe, host, key_file, env, root):
    """Before any model request: codex and its code-mode host start under the
    profile, and the key file is not readable there."""
    def sandboxed(*command, stdout=subprocess.PIPE):
        try:
            return subprocess.run([SANDBOX_EXEC, '-f', profile, *command], env=env, cwd=root, stdin=subprocess.DEVNULL,
                                  stdout=stdout, stderr=subprocess.PIPE, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(command, 124, '', 'timed out')
    version = sandboxed(exe, '--version')
    if version.returncode != 0:
        raise Refused(f'codex does not start under the agent sandbox (exit {version.returncode}): '
                      f'{version.stderr.strip()[-300:]}')
    helped = sandboxed(host, '--help', stdout=subprocess.DEVNULL)
    if helped.returncode != 0:
        raise Refused(f'{CODE_MODE_HOST} does not start under the agent sandbox (exit {helped.returncode}): '
                      f'{helped.stderr.strip()[-300:]}')
    if sandboxed('/bin/cat', os.path.realpath(key_file), stdout=subprocess.DEVNULL).returncode == 0:
        raise Refused('--endpoint-key-file is readable inside the agent sandbox; keep it under ~/.config/harness-launcher')


def sandbox_preflight():
    """Refuse before launch unless the verifier sandbox profile loads here."""
    if not os.access(SANDBOX_EXEC, os.X_OK):
        raise Refused(f'{SANDBOX_EXEC} is required to sandbox the repository verifier and is unavailable')
    env = {'HOME': os.environ.get('HOME', ''), 'PATH': '/usr/bin:/bin', 'TMPDIR': tempfile.gettempdir()}
    check = subprocess.run([str(BIN / 'session-isolation.sh'), 'sandbox-check', SANDBOX_EXEC], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if check.returncode != 0:
        detail = check.stderr.strip()[-300:]
        raise Refused(f'the repository verifier sandbox profile failed to load ({SANDBOX_EXEC}): {detail}')


def run(args, result, run_tmp):
    hdir = harness_dir(args.harness)
    target = validate_target_args(args)
    if target:
        result.update(target=target['name'], branch=target['branch'], base_sha=None, pr_url=None)
    codex = validate_agent_args(args)
    validate_journal_args(args)
    if target:
        load_target(target, os.environ['HOME'])
    if not os.access(LOCKF, os.X_OK):
        # Delivery could never succeed; do not spend the budget first.
        raise Refused(f'{LOCKF} is required for session delivery and is unavailable')
    sandbox_preflight()
    try:
        prompt = Path(args.prompt_file).read_text()
    except OSError as exc:
        raise Refused(f'prompt file is unreadable: {exc.strerror}')
    if not prompt.strip():
        raise Refused('prompt file is empty; a headless run cannot ask for input')
    positive(args.budget_usd, '--budget-usd')
    timeout = positive(args.timeout_min, '--timeout-min') * 60
    caller = None
    if args.settings_file:
        try:
            caller = json.loads(Path(args.settings_file).read_text())
        except (OSError, ValueError):
            raise Refused('--settings-file must contain a JSON object')
        validate_caller(caller)
    env = child_env(run_tmp)
    state = state_home(env)
    journal_run_start(args, os.environ['HOME'])
    log_path = args.result_file + '.log'
    with open(log_path, 'w') as log, tempfile.TemporaryFile('w+') as stdin:
        stdin.write(prompt.rstrip('\n') + '\n\n' + DELIVERY_NOTE + (target_note(target) if target else ''))
        stdin.flush()
        stdin.seek(0)
        if target:
            run_target(args, codex, hdir, env, run_tmp, stdin, timeout, log, result, target)
            return
        if codex:
            outcome = run_codex(args, codex, hdir, env, run_tmp, stdin, timeout, log, result)
        else:
            outcome = run_claude(args, caller, hdir, env, state, run_tmp, stdin, timeout, log, log_path, result)
        if outcome is None:
            return
        sid, message, message_reason = outcome
        if not sid:
            result['status'] = 'failed'
            result['summary'] = 'isolated session id was not announced by the launcher'
            return
        if not message:
            print(f'harness-headless: agent commit message unusable: {message_reason}', file=log, flush=True)
        result['status'], result['commit'], reason = deliver(sid, state, broker_env(env), str(state), log, message)
        if reason:
            result['summary'] = reason


def wait_agent(command, cwd, env, stdin, out, log, timeout):
    """Run the agent in its own process group; (returncode, timed_out). The
    whole group goes on return, timeout or a terminating signal."""
    proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=stdin, stdout=out, stderr=log, start_new_session=True)
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        kill_group(proc.pid)
    proc.wait()
    log.flush()
    return proc.returncode, timed_out


def collect(run_tmp, root):
    """After the agent: kill what lingers under the session root and the temp
    base, read the agent's commit message and last message (codex -o), then
    remove the temp base. (message, reason, last message bytes or None)."""
    kill_lingering(root, run_tmp)
    message, reason = agent_commit_message(run_tmp)
    last, _ = read_agent_file(os.path.join(run_tmp, LAST_MESSAGE), SUMMARY_MAX * 4, 'last message')
    shutil.rmtree(run_tmp, ignore_errors=True)
    return message, reason, last


def run_claude(args, caller, hdir, env, state, run_tmp, stdin, timeout, log, log_path, result):
    """Claude through the launcher; (sid, message, reason) to deliver, or None
    when the result is final."""
    # The launcher merges every --settings (its launch-record hook, the
    # caller's, these denies) into the single --settings Claude receives.
    # acceptEdits, not bypass: writes outside the session root are denied and
    # sandboxed Bash auto-runs (autoAllowBashIfSandboxed). The prompt is stdin.
    command = [str(BIN / 'harness-exec'), hdir, '--isolated', '--passthrough', '-p',
               '--output-format', 'json', '--max-budget-usd', args.budget_usd,
               '--permission-mode', 'acceptEdits', '--strict-mcp-config']
    if args.model:
        command += ['--model', args.model]
    if caller is not None:
        command += ['--settings', json.dumps(caller)]
    caller_deny_read = ((caller or {}).get('sandbox') or {}).get('filesystem', {}).get('denyRead', [])
    settings = mandatory_settings(hdir, env['HOME'], caller_deny_read, run_tmp, args.effort)
    command += ['--settings', json.dumps(settings)]
    with tempfile.TemporaryFile('w+') as out:
        try:
            returncode, timed_out = wait_agent(command, hdir, env, stdin, out, log, timeout)
        except BaseException:
            # Interrupted (a signal): what left the process group goes too.
            sid = launched_session(Path(log_path).read_text(errors='replace'), state, hdir)
            kill_lingering(str(state / 'worktrees' / sid) if sid else None, run_tmp)
            raise
        out.seek(0)
        data = claude_result(out.read())
    log_text = Path(log_path).read_text(errors='replace')
    sid = launched_session(log_text, state, hdir)
    result['session_id'] = sid
    # Before any broker step: nothing from the run survives near the work
    # tree or the temp base, and the temp base is gone.
    message, message_reason, _ = collect(run_tmp, str(state / 'worktrees' / sid) if sid else None)
    # A clone refusal can only precede the launcher's session announcement.
    announced = SESSION_LINE.search(log_text)
    launcher_text = log_text[:announced.start()] if announced else log_text
    result['exit_code'] = EXIT_TIMEOUT if timed_out else returncode
    if data:
        text = data.get('result')
        result['summary'] = (text if isinstance(text, str) else str(data.get('subtype') or ''))[:SUMMARY_MAX]
        result['cost_usd'] = data.get('total_cost_usd')
        result['num_turns'] = data.get('num_turns')
        session_root = str(state / 'worktrees' / sid) if sid else None
        result['transcript'] = transcript_path(env['HOME'], session_root, data.get('session_id'))
    if timed_out:
        result['status'] = 'timeout'
    elif 'headless clone refused' in launcher_text:
        result['status'] = 'refused'
        result['summary'] = 'isolated session refused: symlinked machine-local path in the harness tree'
    elif data is None:
        result['status'] = 'failed'
        result['summary'] = f'launcher exited {returncode} without a Claude result; see {log_path}'
    elif data.get('subtype') == 'error_max_budget_usd':
        result['status'] = 'budget'
    elif data.get('is_error') or returncode != 0:
        result['status'] = 'failed'
    else:
        return sid, message, message_reason
    return None


def run_codex(args, codex, hdir, env, run_tmp, stdin, timeout, log, result, make_session=None):
    """codex exec under the launcher's Seatbelt profile, its model requests
    through the forwarder; (sid, message, reason) to deliver, or None when the
    result is final. Every preflight refusal comes before the first model
    request; a refusal after the session exists finishes it (CLOSED)."""
    upstream_port, max_requests = codex
    exe, host = codex_binary()
    forwarder = Forwarder(upstream_port, endpoint_key(args.endpoint_key_file), args.model, max_requests)
    try:
        forwarder.start()
    except OSError as exc:
        raise Refused(f'the model forwarder could not start: {exc.strerror}')
    session = None
    try:
        if not forwarder.models_ok():
            raise Refused('GET /v1/models through the forwarder did not return 200; check --model-endpoint and the key')
        session = make_session() if make_session else CodexSession(hdir, env, log)
        codex_home = os.path.join(run_tmp, CODEX_HOME)
        os.mkdir(codex_home, 0o700)
        write_private(os.path.join(codex_home, 'config.toml'), codex_config(args.model, args.effort, forwarder))
        write_private(os.path.join(codex_home, 'AGENTS.md'), session.agents_md())
        profile = codex_sandbox_profile(session, hdir, run_tmp, exe, host, forwarder.port, codex_home)
        codex_env = {k: v for k, v in env.items() if k != 'CLAUDE_CODE_TMPDIR'}
        codex_env['CODEX_HOME'] = codex_home
        codex_env.update(session.extra_env)
        codex_preflight(profile, exe, host, args.endpoint_key_file, codex_env, session.root)
        last = os.path.join(run_tmp, LAST_MESSAGE)
        command = [SANDBOX_EXEC, '-f', profile, exe, 'exec', '--json', '-o', last, '-C', session.root, '-']
        result['session_id'] = session.sid
        with tempfile.TemporaryFile('w+') as out:
            returncode, timed_out = wait_agent(command, session.root, codex_env, stdin, out, log, timeout)
            out.seek(0)
            result['num_turns'], usage = codex_events(out.read())
        if usage:
            result['usage'] = usage
        message, message_reason, summary = collect(run_tmp, session.root)
        session.finish()
    finally:
        forwarder.close()
        if session and session.lease is not None:
            # Interrupted (a signal): what left the process group goes too.
            kill_lingering(session.root, run_tmp)
            session.finish()
    result['summary'] = (summary or b'').decode('utf-8', errors='replace')[:SUMMARY_MAX]
    result['exit_code'] = EXIT_TIMEOUT if timed_out else returncode
    if timed_out:
        result['status'] = 'timeout'
    elif forwarder.exhausted:
        result['status'] = 'budget'
        result['summary'] = f'the model request cap ({max_requests}) was reached; see the run log'
    elif returncode != 0:
        result['status'] = 'failed'
        result['summary'] = result['summary'] or f'codex exited {returncode}; see the run log'
    else:
        return session.sid, message, message_reason
    return None


# === --target: a registered personal code repository, delivered as a draft PR =======
# See docs/architecture.md "Target mode". The agent works in a launcher-owned
# clone of the target's base branch under the 0.47 codex profile plus explicit
# denies; the broker moves its work into a launcher-owned trusted.git through
# a patch and manifest, checks and verifies it, and pushes a new loop/ branch
# and opens a draft PR with the owner's gh keyring token, after every agent
# process is gone.

TARGET_COMPANIONS = ('target_digest', 'task_id', 'approval_sha')
TASK_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
APPROVAL_SHA = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')
TARGET_RULES = ('work-scale.md', 'outcome-control.md', 'performance-observation.md')
# Agent-sandbox denies under HOME on top of the 0.47 profile and the owner's
# policy home_deny.
TARGET_HOME_DENY = ('Library/Keychains', '.config/harness-launcher')
BASE_DENY = '(deny file-read* file-write* process-exec*'
DEPENDENCY_FILES = re.compile(r'uv\.lock|uv\.toml|\.python-version|requirements[^/]*\.txt')
UV_TIMEOUT = 1200
DEPENDENCY_REASON = '의존성 변경은 오프라인 검증 불가 (dependency changes cannot be verified offline); nothing pushed'
BRANCH_NOTE = ('Branch note: `loop/` is the harness loop\'s branch prefix and may not follow this repository\'s '
               'branching strategy.')
# auto-deliver's secret_patterns (the first is case-sensitive, the rest are
# not), then GitHub tokens and every PEM private key header.
SECRET_PATTERNS = [re.compile(r'[A-Za-z0-9_]*_(API_KEY|SECRET|TOKEN|PASSWORD)[A-Za-z0-9_]*\s*=')] + [
    re.compile(p, re.I) for p in (
        r'-----BEGIN (PRIVATE KEY|RSA|OPENSSH)-----', r'ghp_[A-Za-z0-9]{36,}', r'sk-[A-Za-z0-9]{32,}',
        r'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}')] + [
    re.compile(r'gh[opsur]_[A-Za-z0-9]{36,}'), re.compile(r'github_pat_[A-Za-z0-9_]{80,}'),
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')]
JOURNAL = re.compile(r'(PENDING|PUSHED|DELIVERED)\(([0-9a-f]{40}|[0-9a-f]{64})\)')


def validate_target_args(args):
    """None without --target; otherwise the run's target, branch and
    approval. --target is codex-only and needs every companion option."""
    if args.target is None:
        given = [n for n in TARGET_COMPANIONS if getattr(args, n) is not None]
        if given:
            raise Refused(f'--{given[0].replace("_", "-")} needs --target')
        return None
    if (args.agent or 'claude') != 'codex':
        raise Refused('--target runs with --agent codex only: code repositories need the deny-default codex sandbox')
    harness_target.check_name(args.target)
    if not re.fullmatch(r'[0-9a-f]{64}', args.target_digest or ''):
        raise Refused('--target needs --target-digest <sha256 of the host record>')
    if not TASK_ID.fullmatch(args.task_id or ''):
        raise Refused('--target needs --task-id [A-Za-z0-9][A-Za-z0-9_-]{0,63}')
    if not APPROVAL_SHA.fullmatch(args.approval_sha or ''):
        raise Refused('--target needs --approval-sha, a lowercase hex commit SHA')
    slug = f'{args.task_id}-{args.approval_sha[:8]}'
    return {'name': args.target, 'digest': args.target_digest, 'task': args.task_id,
            'approval': args.approval_sha, 'slug': slug, 'branch': f'loop/{slug}'}


def load_target(target, home):
    """The owner's policy, the host record (digest-checked) and the static
    boundary rules."""
    policy = harness_target.read_policy()
    entry, path = harness_target.read_record(target['name'], policy, target['digest'])
    target.update(entry=entry, policy=policy, path=harness_target.check_live_path(path, home, policy))
    return target


def sbpl(path):
    if '"' in path or '\n' in path:
        raise Refused(f'path not usable in a sandbox profile: {path!r}')
    return '"' + path.replace('\\', '\\\\') + '"'


def subpaths(paths):
    return ' '.join(f'(subpath {sbpl(p)})' for p in dict.fromkeys(paths))


def target_profile_rules(base, home, hdir, live, env_dir, base_prefix, home_deny, state):
    """SBPL appended to a generated agent or verifier profile (base): the
    prepared environment read and exec only; then every path deny of base
    again (so the base_prefix allow cannot reopen one), except the state-home
    lines, which would close the env; then the target denies."""
    env_paths = [env_dir, os.path.realpath(env_dir), base_prefix]
    states = {sbpl(p) for p in (state, os.path.realpath(state))}
    repeat = [line for line in base.splitlines() if line.startswith(BASE_DENY) and 'require-all' not in line
              and not any(f'(subpath {s})' in line for s in states)]
    deny = [os.path.join(home, d) for d in TARGET_HOME_DENY] + [harness_target.profile_home(), hdir]
    deny += [os.path.join(home, d) for d in home_deny if not d.endswith('*')]
    if live:
        deny.append(live)
    deny += [os.path.realpath(p) for p in deny]
    homes = dict.fromkeys([home, os.path.realpath(home)])
    regex = ' '.join('(regex #"^%s")' % re.sub(r'([^A-Za-z0-9/_-])', r'\\\1', f'{h}/{d[:-1]}')
                     for h in homes for d in home_deny if d.endswith('*'))
    return '\n'.join([f'(allow file-read* process-exec* {subpaths(env_paths)})',
                      f'(deny file-write* {subpaths(env_paths)})', *repeat,
                      f'{BASE_DENY} {subpaths(deny)} {regex})']) + '\n'


def harness_rules_md(hdir):
    """CODEX_HOME/AGENTS.md for a target run: only the harness rules that hold
    in any repository, read from the harness's origin/main (never its work tree)."""
    parts = ['# Generated by harness-headless for a code repository target — do not edit manually.\n',
             '# Source-of-truth: harness origin/main .claude/rules/{%s}\n' % ','.join(TARGET_RULES), '\n']
    for name in TARGET_RULES:
        shown = subprocess.run(['git', '-C', hdir, '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
                                'cat-file', 'blob', f'origin/main:.claude/rules/{name}'],
                               stdin=subprocess.DEVNULL, capture_output=True)
        if shown.returncode != 0:
            raise Refused(f'the harness origin/main has no .claude/rules/{name}')
        parts += [f'## {name[:-3]}\n', '\n', shown.stdout.decode('utf-8', errors='replace'), '\n']
    return ''.join(parts)


def git_env(network=False, index=None):
    """Broker git: no system or global config, no prompts; the ssh agent
    socket only for network calls."""
    env = {k: v for k, v in os.environ.items() if k in ('HOME', 'PATH', 'USER', 'LOGNAME', 'LANG')}
    env.update(GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0',
               TMPDIR=tempfile.gettempdir())
    if network and os.environ.get('SSH_AUTH_SOCK'):
        env['SSH_AUTH_SOCK'] = os.environ['SSH_AUTH_SOCK']
    if index:
        env['GIT_INDEX_FILE'] = str(index)
    return env


HARDENED = ('-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'gc.auto=0',
            '-c', 'submodule.recurse=false', '-c', 'diff.external=', '-c', 'core.untrackedCache=false')


def git(*args, cwd=None, network=False, index=None, input=None, check=True, timeout=600):
    done = subprocess.run(['git', *HARDENED, *args], cwd=cwd, env=git_env(network, index), input=input,
                          stdin=None if input is not None else subprocess.DEVNULL, capture_output=True,
                          timeout=timeout)
    if check and done.returncode != 0:
        raise Outcome('failed', f'broker git {args[0] if not args[0].startswith("--") else args[1]} failed '
                                f'(exit {done.returncode}): {done.stderr.decode(errors="replace").strip()[-300:]}')
    return done


class Delivery:
    """Launcher-owned delivery record of one branch (target, task, approval):
    trusted.git (base and candidate commits), the journal, manifest, message,
    PR body and pr-url. Never inside a session root or a run's temp base."""

    def __init__(self, state, target):
        self.parent = Path(state, 'target-delivery', target['name'])
        self.dir = self.parent / target['slug']
        self.trusted = self.dir / 'trusted.git'
        self.lock_file = self.parent / f'{target["slug"]}.lock'

    def lock(self):
        self.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise Refused('another run is delivering this branch')
        return fd

    def journal(self):
        try:
            match = JOURNAL.fullmatch(self.read('journal'))
        except FileNotFoundError:
            return None
        if not match:
            raise Outcome('failed', f'unreadable delivery journal in {self.dir}')
        return match.group(1), match.group(2)

    def read(self, name):
        return (self.dir / name).read_text().strip()

    def read_optional(self, name):
        try:
            return self.read(name)
        except FileNotFoundError:
            return ''

    def write(self, name, text):
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=f'.{name}.')
        with os.fdopen(fd, 'w') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, self.dir / name)

    def fresh(self):
        """A new record; an earlier one that never reached PENDING is moved
        aside (kept, never deleted)."""
        if os.path.lexists(self.dir):
            os.rename(self.dir, self.parent / f'.retired-{self.dir.name}-{time.time_ns()}')
        self.dir.mkdir(mode=0o700)

    def git(self, *args, **kw):
        return git(f'--git-dir={self.trusted}', *args, **kw)


class TargetSession:
    """The agent's work tree for a target run, run_codex's session: a
    launcher-owned clone of the base branch detached at its SHA, the
    delivery record's trusted.git with the base and a matching index, and
    the prepared test environment, all before the agent starts."""

    def __init__(self, hdir, env, log, target, delivery):
        self.iso, self.env, self.log, self.hdir, self.target = str(BIN / 'session-isolation.sh'), env, log, hdir, target
        self.sid = str(uuid.uuid4())
        runs = Path(state_home(env), 'target-runs')
        runs.mkdir(parents=True, exist_ok=True, mode=0o700)
        if runs.is_symlink():
            raise Refused(f'{runs} is a symlink')
        self.run_dir = runs / self.sid
        self.run_dir.mkdir(mode=0o700)
        self.root, self.env_dir = str(self.run_dir / 'root'), str(self.run_dir / 'env')
        self.lease = True
        try:
            self.setup(env, log, target, delivery)
        except BaseException:
            # Nothing the agent never saw is worth keeping: the clone, the
            # env and its source, and the not yet journaled trusted.git.
            shutil.rmtree(self.run_dir, ignore_errors=True)
            shutil.rmtree(delivery.trusted, ignore_errors=True)
            raise

    def setup(self, env, log, target, delivery):
        entry = target['entry']
        os.mkdir(self.root, 0o700)
        git('init', '-q', '--template=', self.root)
        print(f'harness-launcher: target run {self.sid}: fetching {entry["base"]} of {entry["github"]}', file=log, flush=True)
        git('-C', self.root, 'fetch', '-q', '--no-tags', entry['remote'],
            f'+refs/heads/{entry["base"]}:refs/remotes/origin/{entry["base"]}', network=True)
        self.base = git('-C', self.root, 'rev-parse', '--verify', f'refs/remotes/origin/{entry["base"]}^{{commit}}'
                        ).stdout.decode().strip()
        git('-C', self.root, 'checkout', '-q', '--detach', self.base)
        info = os.lstat(self.root)
        self.root_id = (info.st_dev, info.st_ino)
        delivery.trusted.mkdir(mode=0o700)
        delivery.git('init', '-q', '--bare', '--template=')
        for key in ('core.ignorecase', 'core.precomposeunicode'):
            value = git('-C', self.root, 'config', '--bool', '--default', 'false', key).stdout.decode().strip()
            delivery.git('config', key, value)
        delivery.git('fetch', '-q', '--no-tags', self.root, f'+refs/remotes/origin/{entry["base"]}:refs/heads/base')
        delivery.git(f'--work-tree={self.root}', 'read-tree', self.base)
        delivery.git(f'--work-tree={self.root}', 'update-index', '-q', '--refresh', check=False)
        self.python, self.base_prefix = prepare_test_env(delivery, self.base, self.run_dir, self.env_dir, entry, log)
        self.extra_env = {'HARNESS_TARGET_PYTHON': self.python}
        print(f'harness-launcher: isolated session {self.sid}; headless codex run on target {target["name"]} '
              f'({entry["base"]} at {self.base})', file=log, flush=True)

    def agents_md(self):
        return harness_rules_md(self.hdir)

    def profile_suffix(self, base):
        return target_profile_rules(base, self.env['HOME'], self.hdir, self.target['path'], self.env_dir,
                                    self.base_prefix, self.target['policy']['home_deny'], str(state_home(self.env)))

    def freeze(self):
        """After every agent process is gone (H1): move the root where the
        agent never could write, and require the directory made for it there;
        a swapped root (a symlink or another directory) stages nothing."""
        frozen = str(self.run_dir / 'root.frozen')
        try:
            os.rename(self.root, frozen)
            info = os.lstat(frozen)
        except OSError:
            info = None
        if info is None or not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self.root_id:
            raise Outcome('failed', 'the session root was replaced during the run (not the directory the launcher '
                                    'made); nothing staged or pushed')
        self.root = frozen

    def finish(self):
        self.lease = None


def extract_tree(delivery, sha, dest, index):
    """sha checked out by git (its own path safety, no .git) into a new
    broker-owned dir, with index matching it."""
    os.mkdir(dest, 0o700)
    delivery.git(f'--work-tree={dest}', 'read-tree', sha, index=index)
    delivery.git(f'--work-tree={dest}', 'checkout-index', '-a', '-f', '-u', cwd=dest, index=index)


def prepare_test_env(delivery, base, run_dir, env_dir, entry, log):
    """uv sync of the test group on a base clone, outside the sandbox, with
    network, into a broker-owned venv outside run_tmp and the session root.
    (venv python, realpath of its sys.base_prefix)."""
    source = str(run_dir / 'env-source')
    extract_tree(delivery, base, source, run_dir / 'env-source.index')
    env = harness_target.base_env()
    env['UV_PROJECT_ENVIRONMENT'] = env_dir
    uv = harness_target.tool('uv', env)
    print(f'harness-launcher: preparing the test environment (uv sync, group {entry["test"]["group"]})', file=log, flush=True)
    # --no-build: an sdist build would run package code outside every sandbox.
    try:
        done = subprocess.run([uv, 'sync', '--frozen', '--no-build', '--no-install-project', '--group',
                               entry['test']['group']], cwd=source, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=UV_TIMEOUT)
        rc, output = done.returncode, done.stdout
    except subprocess.TimeoutExpired as exc:
        rc, output = 'timeout', exc.stdout or ''
        output = output.decode(errors='replace') if isinstance(output, bytes) else output
    log.write(output)
    log.flush()
    if rc != 0 and 'no-build' in output:
        raise Refused('the test group needs a source (sdist) build, and uv sync runs with --no-build: a build would '
                      'run package code outside the sandbox. Use wheels-only dependencies for the test group')
    python = os.path.join(env_dir, 'bin', 'python')
    if rc != 0 or not os.access(python, os.X_OK):
        raise Outcome('failed', f'the test environment could not be prepared (uv sync: {rc}); see the run log')
    found = subprocess.run([python, '-I', '-S', '-c', 'import os, sys; print(os.path.realpath(sys.base_prefix))'],
                           env={'PATH': '/usr/bin:/bin', 'HOME': env['HOME']}, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
    prefix = found.stdout.strip()
    if found.returncode != 0 or not usable_base_prefix(prefix, os.path.realpath(env['HOME']),
                                                        os.path.realpath(run_dir.parent.parent)):  # <state>/target-runs/<id>
        raise Outcome('failed', f'the test environment python has an unusable base prefix: {prefix!r}')
    return python, prefix


def usable_base_prefix(prefix, home, state):
    """A python base prefix the profiles may allow read and exec: absolute,
    not /, not HOME or the state home or an ancestor of either, and not at or
    under the state home (other runs, records and trusted git dirs)."""
    return (prefix.startswith('/') and prefix != '/'
            and not any((p + '/').startswith(prefix + '/') for p in (home, state))
            and not (prefix + '/').startswith(state + '/'))


def stage_manifest(delivery, index, work_tree, base):
    """(kind, mode, oid, path) for every path the index changes against base."""
    listed = delivery.git(f'--work-tree={work_tree}', 'diff', '--cached', '--name-status', '--no-renames', '-z', base,
                          index=index).stdout.decode('utf-8', 'surrogateescape').split('\0')
    manifest = []
    for status, path in zip(listed[0::2], listed[1::2]):
        if status.startswith('D'):
            manifest.append(('D', '-', '-', path))
            continue
        entry = delivery.git(f'--work-tree={work_tree}', 'ls-files', '-s', '--', f':(literal){path}',
                             index=index).stdout.decode('utf-8', 'surrogateescape')
        mode, oid = entry.split()[:2]
        manifest.append(('F', mode, oid, path))
    return manifest


def build_candidate(delivery, session):
    """The agent's work tree as a patch and manifest through trusted.git
    (the agent's own .git is never read), applied onto the base in a fresh
    broker-owned tree. (manifest, candidate dir, candidate index) or None."""
    root, base, run_dir = session.root, session.base, session.run_dir
    delivery.git(f'--work-tree={root}', 'add', '-A', '--', '.', cwd=root)
    manifest = stage_manifest(delivery, None, root, base)
    if not manifest:
        return None
    check_paths(manifest)
    patch = delivery.git(f'--work-tree={root}', 'diff', '--cached', '--binary', '--no-ext-diff', '--no-textconv',
                         '--no-renames', base).stdout
    (run_dir / 'submission.patch').write_bytes(patch)
    candidate, index = str(run_dir / 'candidate'), run_dir / 'candidate.index'
    extract_tree(delivery, base, candidate, index)
    applied = delivery.git(f'--work-tree={candidate}', 'apply', '--index', '--binary', str(run_dir / 'submission.patch'),
                           cwd=candidate, index=index, check=False)
    if applied.returncode != 0 or stage_manifest(delivery, index, candidate, base) != manifest:
        raise Outcome('failed', 'the submission patch does not reproduce the agent work tree; nothing pushed')
    return manifest, candidate, index


def check_paths(manifest):
    if any(github_path(path) for _, _, _, path in manifest):
        raise Refused('the change touches .github/ (workflows run before human review); nothing pushed')
    if any(mode == '160000' or path.rsplit('/', 1)[-1].casefold() == '.gitmodules' for _, mode, _, path in manifest):
        raise Refused('the change adds or edits a submodule (gitlink or .gitmodules); nothing pushed')


def github_path(path):
    parts = unicodedata.normalize('NFKC', path).casefold().split('/')
    return any(part == '.github' for part in parts)


def dependency_keys(data):
    import tomllib  # 3.11+, as the launcher requires; imported late for older test interpreters
    doc = tomllib.loads(data.decode('utf-8'))
    project = doc.get('project') if isinstance(doc.get('project'), dict) else {}
    tool = doc.get('tool') if isinstance(doc.get('tool'), dict) else {}
    return (project.get('dependencies'), project.get('optional-dependencies'), project.get('requires-python'),
            doc.get('dependency-groups'), doc.get('build-system'), tool.get('uv'))


def dependency_changed(delivery, base, manifest):
    """Any change (any depth) to uv.lock, uv.toml, .python-version or
    requirements*.txt, or to [project] dependencies, optional-dependencies or
    requires-python, [dependency-groups], [build-system] or [tool.uv] of a
    pyproject.toml (parsed TOML; a candidate that does not parse counts)."""
    for kind, _, oid, path in manifest:
        name = path.rsplit('/', 1)[-1]
        if DEPENDENCY_FILES.fullmatch(name):
            return True
        if name != 'pyproject.toml':
            continue
        old = delivery.git('cat-file', 'blob', f'{base}:{path}', check=False)
        new = delivery.git('cat-file', 'blob', oid).stdout if kind == 'F' else None
        try:
            before = dependency_keys(old.stdout) if old.returncode == 0 else None
            after = dependency_keys(new) if new is not None else None
        except (UnicodeError, ValueError):  # tomllib.TOMLDecodeError is a ValueError
            return True
        if before != after:
            return True
    return False


def placeholder(line):
    """auto-deliver's KEY=VALUE placeholder test: <...>, $..., empty or masks."""
    value = line[line.find('=') + 1:].strip().strip('"\'`').strip()
    return not value or value[0] in '<$' or bool(re.fullmatch(r'[*x.\-_…\s]+', value, re.I))


def secret_hits(texts):
    """Names ('diff: pattern_7') of secret patterns found; never the match."""
    hits = []
    for where, text in texts:
        for i, rx in enumerate(SECRET_PATTERNS):
            if any(rx.search(line) and not (i == 0 and placeholder(line)) for line in text.splitlines()):
                hits.append(f'{where}: pattern_{i}')
    return hits


def added_lines(delivery, candidate, index, base):
    diff = delivery.git(f'--work-tree={candidate}', 'diff', '--cached', '--no-ext-diff', '--no-textconv', '--text',
                        '-U0', '--no-renames', base, index=index).stdout.decode('utf-8', 'replace')
    return '\n'.join(line for line in diff.splitlines() if line.startswith('+'))


def verifier_profile(session, vcopy, vtmp, env):
    made = subprocess.run([session.iso, 'verifier-sandbox-profile', vcopy, vtmp, env['HOME'], session.hdir,
                           session.env_dir], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if made.returncode != 0:
        raise Outcome('failed', f'the verifier sandbox profile could not be generated: {made.stderr.strip()[-300:]}')
    return made.stdout + session.profile_suffix(made.stdout)


def run_target_verifier(session, candidate, log, env):
    """test.run (argv[0] is the env python) on a copy of the candidate under
    the verifier profile plus the read-only env; True when it exits 0."""
    test = session.target['entry']['test']
    vcopy = tempfile.mkdtemp(prefix='verifier-copy.', dir=session.run_dir)
    vtmp = os.path.realpath(tempfile.mkdtemp(prefix='verifier-tmp.', dir='/private/tmp'))
    try:
        shutil.copytree(candidate, vcopy, symlinks=True, dirs_exist_ok=True)
        profile = verifier_profile(session, vcopy, vtmp, env)
        venv = {k: v for k, v in env.items() if k in ('HOME', 'LANG') or k.startswith('LC_')}
        venv.update(PATH='/usr/bin:/bin', TMPDIR=vtmp)
        print(f'harness-headless: verifier: {shlex.join(test["run"])} (timeout {test["timeout_min"]} min)', file=log, flush=True)
        proc = subprocess.Popen([SANDBOX_EXEC, '-p', profile, session.python, *test['run'][1:]], cwd=vcopy, env=venv,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        try:
            proc.wait(timeout=test['timeout_min'] * 60)
        except subprocess.TimeoutExpired:
            print('harness-headless: verifier timed out', file=log, flush=True)
        finally:
            kill_group(proc.pid)
        proc.wait()
        return proc.returncode == 0
    finally:
        kill_lingering(vcopy, vtmp)
        shutil.rmtree(vcopy, ignore_errors=True)
        shutil.rmtree(vtmp, ignore_errors=True)


def pr_body(target, base, message):
    entry = target['entry']
    body = message.split('\n', 1)[1].strip() if '\n' in message else ''
    lines = [BRANCH_NOTE, '']
    if body:
        # A fence longer than any backtick run in the text: shown, never rendered.
        fence = '`' * max(3, 1 + max((len(run) for run in re.findall(r'`+', body)), default=0))
        lines += [f'{fence}text', body, fence, '']
    lines += ['---', f'Task: {target["task"]}', f'Approval: {target["approval"]}',
              f'Base: {entry["base"]} at {base}',
              'Opened as a draft by harness-launcher (harness-headless --target); review before merging.']
    return '\n'.join(lines) + '\n'


def commit_candidate(delivery, session, target, manifest, candidate, index, message, log):
    """Checks, verifier and the commit in trusted.git, then the delivery
    record up to PENDING; returns the commit SHA."""
    base = session.base
    message = message or f'harness session {session.sid}\n'
    body = pr_body(target, base, message)
    hits = secret_hits([('diff', added_lines(delivery, candidate, index, base)), ('message', message), ('body', body)])
    if hits:
        raise Refused(f'secret pattern matched ({", ".join(hits)}); nothing pushed')
    if dependency_changed(delivery, base, manifest):
        raise Outcome('failed', DEPENDENCY_REASON)
    if not run_target_verifier(session, candidate, log, broker_env(session.env)):
        raise Outcome('failed', VERIFIER_REJECTED)
    tree = delivery.git(f'--work-tree={candidate}', 'write-tree', index=index).stdout.decode().strip()
    text = f'{message.rstrip()}\n\nHarness-Session: {session.sid}\n'
    ident = {'GIT_AUTHOR_NAME': 'harness-broker', 'GIT_AUTHOR_EMAIL': 'broker@invalid',
             'GIT_COMMITTER_NAME': 'harness-broker', 'GIT_COMMITTER_EMAIL': 'broker@invalid'}
    done = subprocess.run(['git', *HARDENED, f'--git-dir={delivery.trusted}', 'commit-tree', tree, '-p', base],
                          env={**git_env(), **ident}, input=text.encode(), capture_output=True)
    if done.returncode != 0:
        raise Outcome('failed', 'broker git commit-tree failed')
    sha = done.stdout.decode().strip()
    delivery.git('update-ref', 'refs/heads/delivery', sha)
    if manifest_mismatch(delivery.trusted, sha, manifest):
        raise Outcome('failed', 'the candidate commit does not match the manifest; nothing pushed')
    for name, value in (('manifest', json.dumps(manifest)), ('message', message), ('body', body), ('base-sha', base),
                        ('run-id', session.sid), ('branch', target['branch']), ('digest', target['digest'])):
        delivery.write(name, value)
    delivery.write('journal', f'PENDING({sha})')
    return sha


def manifest_mismatch(git_dir, sha, manifest):
    """True unless every manifest entry is in sha's tree as recorded."""
    for kind, mode, oid, path in manifest:
        listed = git(f'--git-dir={git_dir}', 'ls-tree', sha, '--', f':(literal){path}').stdout
        entry = listed.decode('utf-8', 'surrogateescape').split()
        if (kind == 'D') != (not entry) or (entry and entry[:3] != [mode, 'blob' if mode != '160000' else 'commit', oid]):
            return True
    return False


def remote_branch(delivery, entry, branch):
    out = delivery.git('ls-remote', entry['remote'], f'refs/heads/{branch}', network=True).stdout.decode()
    for line in out.splitlines():
        sha, _, ref = line.partition('\t')
        if ref == f'refs/heads/{branch}':
            return sha
    return None


def find_pr(remote, github, branch):
    rc, out, _, timed_out = remote.gh('pr', 'list', '--repo', github, '--head', branch, '--state', 'all', '--json', 'url')
    if rc != 0 or timed_out:
        raise Outcome('failed', f'gh pr list failed ({"timed out" if timed_out else f"exit {rc}"}); the branch is pushed; '
                                'rerun the same task and approval to open the PR')
    try:
        urls = [item['url'] for item in json.loads(out)]
    except (ValueError, TypeError, KeyError):
        raise Outcome('failed', 'gh pr list did not return JSON')
    for url in urls:
        if re.fullmatch(rf'https://github\.com/{re.escape(github)}/pull/[0-9]+', url):
            return url
    return None


def deliver_target(delivery, target, log, result):
    """Broker delivery from the journal: preflight, push (no force) or
    resume, readback, list-before-create draft PR, DELIVERED."""
    entry, branch = target['entry'], target['branch']
    _, sha = delivery.journal()
    manifest = [tuple(item) for item in json.loads(delivery.read('manifest'))]
    result.update(commit=sha, base_sha=delivery.read('base-sha'))
    workdir = tempfile.mkdtemp(prefix='gh.', dir=delivery.dir)
    try:
        remote = harness_target.Remote(entry, workdir, target['policy'])
        remote.check()
        current = remote_branch(delivery, entry, branch)
        if current == sha:
            print(f'harness-headless: {branch} already at {sha}; resuming delivery', file=log, flush=True)
        elif current is not None:
            raise Outcome('conflict', f'{branch} exists at another commit; nothing pushed (a new approval starts a new branch)')
        elif delivery.journal()[0] == 'PUSHED':
            raise Outcome('failed', f'the journal says {branch} was pushed but the remote has no such branch; not pushed again')
        else:
            pushed = delivery.git('push', '-q', entry['remote'], f'{sha}:refs/heads/{branch}', network=True, check=False)
            if pushed.returncode != 0:
                after = remote_branch(delivery, entry, branch)
                if after != sha:
                    raise Outcome('conflict' if after else 'failed', f'push of {branch} failed; see the run log')
            delivery.write('journal', f'PUSHED({sha})')
        readback(delivery, entry, branch, sha, manifest)
        url = find_pr(remote, entry['github'], branch) or create_pr(remote, delivery, workdir, entry, branch)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    delivery.write('pr-url', url)
    delivery.write('journal', f'DELIVERED({sha})')
    result.update(status='pr_opened', pr_url=url)


def readback(delivery, entry, branch, sha, manifest):
    check = tempfile.mkdtemp(prefix='readback.', dir=delivery.dir)
    try:
        git('init', '-q', '--bare', '--template=', check)
        git(f'--git-dir={check}', 'fetch', '-q', '--no-tags', entry['remote'], f'+refs/heads/{branch}:refs/heads/readback',
            network=True)
        got = git(f'--git-dir={check}', 'rev-parse', 'refs/heads/readback').stdout.decode().strip()
        if got != sha or manifest_mismatch(check, sha, manifest):
            raise Outcome('failed', f'readback of {branch} does not match the delivered commit and manifest')
    finally:
        shutil.rmtree(check, ignore_errors=True)


def create_pr(remote, delivery, workdir, entry, branch):
    """gh pr create from the empty broker dir; after a failure or timeout
    the PR list is read again before giving up."""
    title = delivery.read('message').split('\n', 1)[0]
    body = os.path.join(workdir, 'pr-body.md')
    write_private(body, delivery.read('body') + '\n')
    try:
        rc, out, _, timed_out = remote.gh('pr', 'create', '--repo', entry['github'], '--base', entry['base'],
                                          '--head', branch, '--draft', '--title', title, '--body-file', body)
    finally:
        os.unlink(body)
    url = out.strip().splitlines()[-1] if rc == 0 and not timed_out and out.strip() else None
    if url and re.fullmatch(rf'https://github\.com/{re.escape(entry["github"])}/pull/[0-9]+', url):
        return url
    url = find_pr(remote, entry['github'], branch)
    if not url:
        raise Outcome('failed', f'gh pr create {"timed out" if timed_out else f"failed (exit {rc})"} and no PR is '
                                f'listed for {branch}; rerun the same task and approval to retry')
    return url


def target_note(target):
    entry = target['entry']
    return ('\n\nCode repository note from the launcher: this run works on the repository '
            f'{entry["github"]} (base branch {entry["base"]}). The launcher, not you, pushes your change to the '
            f'branch {target["branch"]} and opens a draft pull request; do not run git push or gh (there is no '
            'network). Run the repository tests with: $HARNESS_TARGET_PYTHON '
            f'{shlex.join(entry["test"]["run"][1:])} (a prepared environment; installing packages is not possible). '
            'Do not change anything under .github/, uv.lock, or the dependencies in pyproject.toml: such a change '
            'is not delivered.')


def run_target(args, codex, hdir, env, run_tmp, stdin, timeout, log, result, target):
    """A --target run: resume a journaled delivery, or preflight, the agent,
    the broker's checks and verifier, and PR delivery."""
    delivery = Delivery(state_home(env), target)
    lock = delivery.lock()
    sessions = []
    try:
        journal = delivery.journal()
        if journal and not hmac.compare_digest(delivery.read_optional('digest'), target['digest']):
            raise Outcome('failed', f'{target["branch"]} was started under another target record digest; '
                                    'not resumed (a changed record needs a new approval)')
        if journal and journal[0] == 'DELIVERED':
            result.update(status='pr_opened', commit=journal[1], pr_url=delivery.read('pr-url'),
                          base_sha=delivery.read('base-sha'), session_id=delivery.read('run-id'), exit_code=0)
            return
        if journal:
            print(f'harness-headless: {target["branch"]} has a {journal[0]} delivery; resuming it, no agent run',
                  file=log, flush=True)
            result.update(session_id=delivery.read('run-id'), exit_code=0)
            deliver_target(delivery, target, log, result)
            return
        workdir = tempfile.mkdtemp(prefix='gh-preflight.')
        try:
            harness_target.Remote(target['entry'], workdir, target['policy']).check()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        delivery.fresh()

        def make_session():
            sessions.append(TargetSession(hdir, env, log, target, delivery))
            result['base_sha'] = sessions[0].base
            return sessions[0]
        outcome = run_codex(args, codex, hdir, env, run_tmp, stdin, timeout, log, result, make_session)
        if outcome is None:
            return
        _, message, message_reason = outcome
        if not message:
            print(f'harness-headless: agent commit message unusable: {message_reason}', file=log, flush=True)
        sessions[0].freeze()
        built = build_candidate(delivery, sessions[0])
        if built is None:
            result['status'] = 'no_changes'
            return
        commit_candidate(delivery, sessions[0], target, *built, message, log)
        deliver_target(delivery, target, log, result)
    finally:
        os.close(lock)
        # Resume needs only the delivery record, never these broker copies.
        for session in sessions:
            for name in ('env-source', 'candidate'):
                shutil.rmtree(session.run_dir / name, ignore_errors=True)
            for name in ('env-source.index', 'candidate.index'):
                with contextlib.suppress(FileNotFoundError):
                    (session.run_dir / name).unlink()


def write_result(path, result):
    directory = os.path.dirname(os.path.abspath(path))
    with tempfile.NamedTemporaryFile('w', dir=directory, prefix='.harness-headless-', delete=False) as tmp:
        json.dump(result, tmp)
        tmp.flush()
        os.fsync(tmp.fileno())
    os.replace(tmp.name, path)


def main(argv):
    args = parse_args(argv)
    result = {'version': 1, 'status': 'failed', 'session_id': None, 'commit': None, 'cost_usd': None,
              'num_turns': None, 'summary': '', 'transcript': None, 'exit_code': 1,
              'started_at': int(time.time()), 'ended_at': None}
    try:
        lock = open(args.lock_file, 'a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Another run owns the lock and its result file: touch nothing.
        print(f'harness-headless: lock is held: {args.lock_file}', file=sys.stderr)
        return 75
    except OSError as exc:
        print(f'harness-headless: lock unavailable: {args.lock_file}: {exc.strerror}', file=sys.stderr)
        result.update(status='refused', summary=f'lock unavailable: {exc.strerror}',
                      exit_code=EXIT_REFUSED, ended_at=int(time.time()))
        try:
            write_result(args.result_file, result)
        except OSError:
            pass
        return 2
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _terminate)
    run_tmp = None
    try:
        # Short, user-owned, 0700, not a symlink, outside the shared claude-<uid>.
        run_tmp = tempfile.mkdtemp(prefix='hh-', dir='/private/tmp')
        run(args, result, run_tmp)
    except Refused as exc:
        result.update(status='refused', summary=str(exc)[:SUMMARY_MAX], exit_code=EXIT_REFUSED)
    except Outcome as exc:
        result.update(status=exc.status, summary=str(exc)[:SUMMARY_MAX])
    except Terminated as exc:
        signum = exc.args[0]
        result.update(status='failed', exit_code=128 + signum,
                      summary=f'harness-headless terminated by signal {signum}')
    except Exception as exc:  # the result file is the contract; never exit silently
        result.update(status='failed', summary=f'harness-headless error: {exc}'[:SUMMARY_MAX])
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    if run_tmp:
        shutil.rmtree(run_tmp, ignore_errors=True)
    result['ended_at'] = int(time.time())
    try:
        write_result(args.result_file, result)
    except OSError as exc:
        print(f'harness-headless: cannot write result: {exc}', file=sys.stderr)
        return 1
    # The lock is released only after the result is in place.
    lock.close()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
