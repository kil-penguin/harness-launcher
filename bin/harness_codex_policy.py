"""Read-only access to the fixed, administrator-owned Codex requirements file."""
import json
import os
from pathlib import Path
import stat
import sys

REQUIREMENTS = Path('/private/etc/codex/requirements.toml')


def policy_read_rule():
    # No environment or caller path override. Every component must remain in
    # administrator custody; never follow a redirected policy to a secret file.
    for path in (*reversed(REQUIREMENTS.parents), REQUIREMENTS):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return ''
        except OSError as exc:
            raise ValueError('cannot inspect managed Codex policy metadata') from exc
        is_policy = path == REQUIREMENTS
        regular = stat.S_ISREG(info.st_mode) if is_policy else stat.S_ISDIR(info.st_mode)
        if (not regular or info.st_uid != 0 or info.st_mode & 0o022
                or (is_policy and info.st_nlink != 1)):
            raise ValueError('managed Codex policy must be a regular root-owned file in trusted directories')
    literal = json.dumps(str(REQUIREMENTS))
    return (f'(allow file-read-data (literal {literal}))\n'
            f'(deny file-write* process-exec* (literal {literal}))\n')


if __name__ == '__main__':
    try:
        sys.stdout.write(policy_read_rule())
    except ValueError as exc:
        print('harness-session: ' + str(exc), file=sys.stderr)
        sys.exit(2)
