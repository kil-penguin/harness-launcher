#!/usr/bin/env python3
"""Fixed CI-only prerequisite, exclusively for the selected ephemeral hosted job.

Environment consistency is not host authentication or local-machine permission.
No CLI targets: only /opt/homebrew and /opt/homebrew/Cellar can be changed.
"""
import json
import os
import stat
import sys

TARGETS = ('/opt/homebrew', '/opt/homebrew/Cellar')
CONTEXT = {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_OS': 'macOS'}

class Refused(Exception):
    def __init__(self, label, target=None, observed=None):
        super().__init__(label)
        self.target = target
        self.observed = observed

class PosixFS:
    """Descriptor operations; synthetic tests inject a separately rooted backend."""
    def uid(self):
        return os.getuid()
    def open_root(self):
        return os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    def open_dir(self, name, parent):
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    def named(self, name, parent):
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    def stat(self, fd):
        return os.fstat(fd)
    def chmod(self, fd, mode):
        os.fchmod(fd, mode)
    def close(self, fd):
        os.close(fd)

def snapshot(info):
    return {'dev': info.st_dev, 'ino': info.st_ino, 'uid': info.st_uid,
            'gid': info.st_gid, 'mode': stat.S_IMODE(info.st_mode)}

def require_binding(named, opened, expected):
    if not stat.S_ISDIR(named.st_mode) or not stat.S_ISDIR(opened.st_mode):
        raise Refused('DIRECTORY_REQUIRED')
    if snapshot(named) != expected or snapshot(opened) != expected:
        raise Refused('BINDING_CHANGED')

def check_chain(fs, nodes):
    for i, node in enumerate(nodes):
        observed = None
        try:
            named = fs.named(node['name'], node['parent'])
            opened = fs.stat(node['fd'])
            observed = snapshot(opened)
            require_binding(named, opened, node['expected'])
        except (Refused, OSError, NotImplementedError, AttributeError) as error:
            label = str(error) if isinstance(error, Refused) else 'FILESYSTEM_UNAVAILABLE'
            raise Refused(label, i - 2 if i >= 2 else None, observed) from None

def prepare(context, fs):
    """Return nonzero plus bounded partial-state metadata on every admitted failure.

    fs is an injectable syscall boundary, never a command-line target selector.
    All admission decisions for both targets precede the first possible fchmod.
    """
    records = [{'path': p, 'before': None, 'after': None, 'changed': False,
                'status': 'unattempted'} for p in TARGETS]
    report = {'schema': 'w07.ci-owned-homebrew/v1', 'status': 'refused',
              'error': None, 'targets': records}
    if any(context.get(k) != v for k, v in CONTEXT.items()):
        report['error'] = 'CONTEXT_REFUSED'
        return 2, report
    nodes = []
    held = []
    active = None
    stage = 'preflight'
    code = 2
    try:
        uid = fs.uid()
        # Keep every binding in the fixed / -> opt -> homebrew -> Cellar chain.
        parent = None
        for name in ('/', 'opt', 'homebrew', 'Cellar'):
            fd = fs.open_root() if parent is None else fs.open_dir(name, parent)
            held.append(fd)
            info = fs.stat(fd)
            expected = snapshot(info)
            require_binding(fs.named(name, parent), info, expected)
            nodes.append({'name': name, 'parent': parent, 'fd': fd, 'expected': expected})
            parent = fd
        for i, node in enumerate(nodes[2:]):
            records[i]['before'] = dict(node['expected'])
        for i, node in enumerate(nodes[2:]):
            active = i
            before = records[i]['before']
            if before['uid'] == 0:
                if before['mode'] & 0o022:
                    raise Refused('ROOT_UNSAFE')
            elif before['uid'] != uid or uid == 0:
                raise Refused('FOREIGN_OWNER')
        for i, node in enumerate(nodes[2:]):
            active = i
            record = records[i]
            stage = 'precheck'
            check_chain(fs, nodes)
            before = record['before']
            wanted = before['mode'] & ~0o022
            if wanted != before['mode']:
                # Root-safe is a no-op; root-unsafe was refused in ALL-target preflight.
                if uid == 0 or before['uid'] != uid:
                    raise Refused('OWNER_CHANGED')
                stage = 'chmod'
                record['status'] = 'attempted'
                fs.chmod(node['fd'], wanted)
                node['expected'] = {**node['expected'], 'mode': wanted}
            stage = 'postcheck'
            record['after'] = None
            record['changed'] = None
            observed = snapshot(fs.stat(node['fd']))
            record['after'] = observed
            record['changed'] = observed != before
            check_chain(fs, nodes)
            record['status'] = 'changed' if wanted != before['mode'] else 'unchanged'
        # Final all-target binding checks are required for success, including earlier targets.
        stage = 'finalcheck'
        check_chain(fs, nodes)
        report['status'] = 'prepared'
        code = 0
    except (Refused, OSError, NotImplementedError, AttributeError) as error:
        report['error'] = str(error) if isinstance(error, Refused) else 'FILESYSTEM_UNAVAILABLE'
        if isinstance(error, Refused) and error.target is not None:
            active = error.target
            records[active]['after'] = error.observed
            records[active]['changed'] = (error.observed != records[active]['before']
                                          if error.observed is not None else None)
        if active is not None:
            record = records[active]
            if stage == 'chmod':
                # A failed syscall may have applied: observe the held fd once, no retry.
                record['after'] = None
                record['changed'] = None
                try:
                    info = snapshot(fs.stat(nodes[active + 2]['fd']))
                    record['after'] = info
                    record['changed'] = info != record['before']
                except (OSError, NotImplementedError, AttributeError):
                    pass
            record['status'] = 'unavailable' if record['changed'] is None else 'failed'
        code = 2
    finally:
        for fd in reversed(held):
            try:
                fs.close(fd)
            except (OSError, NotImplementedError, AttributeError):
                code = 2
                report['status'] = 'refused'
                report['error'] = report['error'] or 'CLOSE_FAILED'
    return code, report

def main():
    # No caller-controlled path, privilege fallback or local maintenance command.
    if len(sys.argv) != 1:
        code, report = prepare({}, PosixFS())
        report['error'] = 'ARGUMENTS_REFUSED'
    else:
        context = {key: os.environ.get(key) for key in CONTEXT}
        if sys.platform != 'darwin':
            context = {}
        code, report = prepare(context, PosixFS())
    print(json.dumps(report, separators=(',', ':')))
    return code

if __name__ == '__main__':
    sys.exit(main())
