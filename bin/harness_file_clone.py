"""Private COW copies from already-open files; never hardlink mutable state."""
import ctypes
import errno
import os
from pathlib import Path
import stat
import sys

_clone = None
if sys.platform == 'darwin':
    _clone = getattr(ctypes.CDLL(None, use_errno=True), 'fclonefileat', None)
    if _clone is not None:
        _clone.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        _clone.restype = ctypes.c_int


def clone_from_fd(source_fd, destination):
    """Return False only for unsupported COW; all other failures preserve gates.

    Callers validate their source/parent boundary, private mode, contents and
    source stability. fclonefileat binds the source inode, unlike path copying.
    """
    destination = Path(destination)
    if os.path.lexists(destination):
        raise FileExistsError(errno.EEXIST, 'clone destination exists', str(destination))
    if not stat.S_ISREG(os.fstat(source_fd).st_mode):
        raise OSError(errno.EINVAL, 'clone source is not a regular file')
    if _clone is None:
        return False
    parent = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # CLONE_NOOWNERCOPY from the macOS SDK sys/clonefile.h; destination has
        # its own inode/ownership, and later writes do not change the source.
        if _clone(source_fd, parent, os.fsencode(destination.name), 0x0002) == 0:
            return True
        error = ctypes.get_errno()
        if error in (errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL):
            return False
        raise OSError(error, os.strerror(error), str(destination))
    finally:
        os.close(parent)
