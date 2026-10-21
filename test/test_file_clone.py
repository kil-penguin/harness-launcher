"""Copy-on-write must retain private, independently writable Native artifacts."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CloneTests(unittest.TestCase):
    def run_case(self, program):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, '-I', '-B', '-c',
                'import sys;sys.path.insert(0,sys.argv[1]);\n'+program,
                str(ROOT/'bin'), str(Path(tmp).resolve())], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_clone_has_a_separate_inode_and_independent_bytes(self):
        self.run_case('''
from pathlib import Path
import os
from harness_file_clone import clone_from_fd
p=Path(sys.argv[2]);source=p/'source';target=p/'target';source.write_bytes(b'original')
with source.open('rb') as incoming:
    cloned=clone_from_fd(incoming.fileno(),target)
if sys.platform=='darwin':
    assert cloned, 'owned APFS clone was not used'
    assert source.stat().st_ino!=target.stat().st_ino
    assert target.stat().st_nlink==1
    source.write_bytes(b'new source')
    assert target.read_bytes()==b'original'
    target.write_bytes(b'new target')
    assert source.read_bytes()==b'new source'
else:
    assert not cloned and not target.exists()
''')

    def test_unsupported_clone_leaves_destination_for_safe_copy_but_other_errors_refuse(self):
        self.run_case('''
from pathlib import Path
from unittest import mock
import ctypes,errno
import harness_file_clone as c
p=Path(sys.argv[2]);source=p/'source';source.write_bytes(b'original')
def failed(*args):
    ctypes.set_errno(errno.EXDEV);return -1
with source.open('rb') as incoming, mock.patch.object(c,'_clone',failed):
    assert not c.clone_from_fd(incoming.fileno(),p/'unsupported')
assert not (p/'unsupported').exists()
def denied(*args):
    ctypes.set_errno(errno.EACCES);return -1
with source.open('rb') as incoming, mock.patch.object(c,'_clone',denied):
    try:c.clone_from_fd(incoming.fileno(),p/'denied')
    except PermissionError:pass
    else:raise AssertionError('permission failure must not become fallback')
''')

    def test_existing_destination_is_never_replaced(self):
        self.run_case('''
from pathlib import Path
from harness_file_clone import clone_from_fd
p=Path(sys.argv[2]);source=p/'source';source.write_bytes(b'original');target=p/'target';target.write_bytes(b'existing')
with source.open('rb') as incoming:
    try:clone_from_fd(incoming.fileno(),target)
    except FileExistsError:pass
    else:raise AssertionError('existing target accepted')
assert target.read_bytes()==b'existing'
''')


if __name__ == '__main__':
    unittest.main()
