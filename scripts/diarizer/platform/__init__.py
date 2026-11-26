"""Filesystem and signal operations shared by the supported platforms."""

from contextlib import contextmanager
import os
import signal
import stat

from . import windows


IS_WINDOWS = os.name == "nt"
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM) + ((signal.SIGHUP,) if hasattr(signal, "SIGHUP") else ())


# Treat Windows directory junctions like symbolic links when scanning an archive.
def is_link_or_junction(path):
    if path.is_symlink():
        return True
    if IS_WINDOWS:
        try:
            return path.lstat().st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
        except FileNotFoundError:
            pass
    return False


# Update a file's modification time while preserving its access time and link target.
def set_mtime(path, timestamp_ns):
    if IS_WINDOWS:
        windows.set_mtime(path, timestamp_ns)
        return
    info = os.stat(path, follow_symlinks=False)
    os.utime(path, ns=(info.st_atime_ns, timestamp_ns), follow_symlinks=False)


# Serialize access to a path, yielding its checked open inode on Unix.
@contextmanager
def locked_path(path, *, create=False, blocking=True):
    if IS_WINDOWS:
        with windows.path_mutex(path, blocking=blocking):
            yield None
        return

    import fcntl

    with path.open("a+b" if create else "rb") as file:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(file, flags)
        yield file
