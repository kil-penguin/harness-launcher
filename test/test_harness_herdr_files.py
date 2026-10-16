import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "bin"))
import harness_herdr_files as files


class PrivateFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(os.path.realpath(self.tmp.name)) / "private"
        self.parent.mkdir(mode=0o700)
        self.parent.chmod(0o700)

    def write(self, name, content, mode=0o600):
        path = self.parent / name
        path.write_bytes(content)
        path.chmod(mode)
        return path

    def test_reads_private_text_and_json(self):
        text = self.write("target", b'{"id":"safe"}')
        self.assertEqual(files.read_private_text(str(text), 16), '{"id":"safe"}')
        self.assertEqual(files.read_private_json_object(str(text), 16), {"id": "safe"})

    def test_rejects_relative_dot_and_invalid_limits(self):
        valid = self.write("valid", b"safe")
        literal_dot = str(self.parent) + "/./valid"
        literal_empty = str(self.parent) + "//valid"
        literal_parent = str(self.parent) + "/../private/valid"
        for path in ("relative", literal_dot, literal_empty, literal_parent):
            with self.assertRaises(files.FileError):
                files.read_private_text(path, 16)
        for limit in (True, 0, -1, 65537):
            with self.assertRaises(files.FileError):
                files.read_private_text(str(self.parent / "x"), limit)

    def test_rejects_symlink_ancestor_parent_file_hardlink_and_fifo(self):
        source = self.write("source", b"safe")
        ancestor = self.parent.parent / "ancestor"
        os.symlink(self.parent.parent, ancestor)
        with self.assertRaises(files.FileError):
            files.read_private_text(str(ancestor / self.parent.name / "source"), 16)
        linked_parent = self.parent.parent / "linked-parent"
        os.symlink(self.parent, linked_parent)
        with self.assertRaises(files.FileError):
            files.read_private_text(str(linked_parent / "source"), 16)
        file_link = self.parent / "file-link"
        os.symlink(source, file_link)
        with self.assertRaises(files.FileError):
            files.read_private_text(str(file_link), 16)
        self.parent.chmod(0o755)
        with self.assertRaises(files.FileError):
            files.read_private_text(str(source), 16)
        self.parent.chmod(0o700)
        source.chmod(0o644)
        with self.assertRaises(files.FileError):
            files.read_private_text(str(source), 16)
        source.chmod(0o600)
        os.link(source, self.parent / "hard")
        with self.assertRaises(files.FileError):
            files.read_private_text(str(source), 16)
        source.unlink()
        os.mkfifo(source)
        with self.assertRaises(files.FileError):
            files.read_private_text(str(source), 16)

    @unittest.skipUnless(os.path.islink("/tmp"), "platform has no /tmp symlink")
    def test_rejects_tmp_spelling_when_private_tmp_is_canonical(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            parent = Path(temporary) / "private"
            parent.mkdir(mode=0o700)
            parent.chmod(0o700)
            target = parent / "target"
            target.write_bytes(b"safe")
            target.chmod(0o600)
            self.assertEqual(files.read_private_text(str(target), 16), "safe")
            via_tmp = Path("/tmp") / Path(temporary).name / "private" / "target"
            with self.assertRaises(files.FileError):
                files.read_private_text(str(via_tmp), 16)

    def test_rejects_directory_foreign_owner(self):
        directory = self.parent / "directory"
        directory.mkdir(mode=0o600)
        directory.chmod(0o600)
        with self.assertRaises(files.FileError):
            files.read_private_text(str(directory), 16)
        path = self.write("owned", b"safe")
        real_fstat = files.os.fstat
        def foreign(fd):
            result = real_fstat(fd)
            if stat.S_ISREG(result.st_mode):
                raw = list(result)
                raw[4] = result.st_uid + 1
                return os.stat_result(raw)
            return result
        with patch.object(files.os, "fstat", foreign):
            with self.assertRaises(files.FileError):
                files.read_private_text(str(path), 16)

    def test_os_errors_are_normalized_without_path_or_body(self):
        path = self.write("secret-name", b"secret-body")
        with patch.object(files.os, "fstat", side_effect=OSError("raw failure")):
            with self.assertRaises(files.FileError) as error:
                files.read_private_text(str(path), 16)
        self.assertEqual(str(error.exception), "private_file_invalid")
        self.assertNotIn("secret", str(error.exception))

    def test_foreign_immediate_parent_is_rejected(self):
        path = self.write("owned-parent", b"safe")
        parent_inode = self.parent.stat().st_ino
        real_fstat = files.os.fstat
        def foreign_parent(fd):
            result = real_fstat(fd)
            if result.st_ino == parent_inode:
                raw = list(result)
                raw[4] = result.st_uid + 1
                return os.stat_result(raw)
            return result
        with patch.object(files.os, "fstat", foreign_parent):
            with self.assertRaises(files.FileError):
                files.read_private_text(str(path), 16)

    def test_directory_fstat_failure_closes_opened_descriptor(self):
        root = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        opened, closed = [], []
        real_open, real_close, real_fstat = files.os.open, files.os.close, files.os.fstat
        def capture_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd
        def capture_close(fd):
            closed.append(fd)
            return real_close(fd)
        def fail_opened(fd):
            if opened and fd == opened[-1]:
                raise OSError("fstat")
            return real_fstat(fd)
        try:
            with patch.object(files.os, "open", capture_open), patch.object(files.os, "close", capture_close), patch.object(files.os, "fstat", fail_opened):
                with self.assertRaises(files.FileError):
                    files._open_directory(root, "private")
        finally:
            real_close(root)
        self.assertEqual(closed, opened)

    def test_rejects_overlimit_multibyte_nul_and_invalid_utf8(self):
        self.assertEqual(files.read_private_text(str(self.write("multi", "가".encode())), 3), "가")
        for name, content, limit in (("large", b"1234", 3), ("nul", b"a\0", 16), ("utf8", b"\xff", 16)):
            with self.assertRaises(files.FileError):
                files.read_private_text(str(self.write(name, content)), limit)

    def test_json_rejects_duplicates_nested_duplicates_nonobject_and_deep_nesting(self):
        cases = (b'{"a":1,"a":2}', b'{"a":{"b":1,"b":2}}', b'[]', b'{"n":NaN}',
                 b'{"n":Infinity}', b'{"n":-Infinity}', b'{"nul":"\\u0000"}',
                 b'{"bad":"\\ud800"}', b'{"\\ud800":"safe"}',
                 b'{"nested":{"n":1e999}}', b'{"nested":{"n":-1e999}}',
                 ('{"a":' * 1100 + "0" + "}" * 1100).encode())
        for index, value in enumerate(cases):
            with self.assertRaises(files.FileError):
                files.read_private_json_object(str(self.write(str(index), value)), 65536)

    def test_json_accepts_finite_float(self):
        path = self.write("finite", b'{"nested":{"n":1.25e2}}')
        self.assertEqual(files.read_private_json_object(str(path), 65536), {"nested": {"n": 125.0}})

    def test_surrogate_path_is_normalized_to_file_error(self):
        with self.assertRaises(files.FileError) as error:
            files.read_private_text(str(self.parent) + "/\ud800", 16)
        self.assertEqual(str(error.exception), "private_file_invalid")

    def test_open_file_fd_is_retained_after_path_replacement(self):
        path = self.write("target", b"original")
        old_parent = self.parent.with_name("displaced")
        real_read = files.os.read
        called = False
        def replace_after_open(fd, count):
            nonlocal called
            if not called:
                called = True
                os.rename(self.parent, old_parent)
                self.parent.mkdir(mode=0o700)
                self.parent.chmod(0o700)
                (self.parent / "target").write_bytes(b"attacker")
                (self.parent / "target").chmod(0o600)
            return real_read(fd, count)
        with patch.object(files.os, "read", replace_after_open):
            self.assertEqual(files.read_private_text(str(path), 16), "original")


if __name__ == "__main__":
    unittest.main()
