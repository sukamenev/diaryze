"""Windows process trees, mutexes, and console modes."""

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import errno
from functools import lru_cache
import hashlib
import os
import subprocess


ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
FILETIME_EPOCH_OFFSET = 116_444_736_000_000_000


# Load only the Windows APIs used by the platform adapters.
@lru_cache(maxsize=1)
def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    filetime_pointer = ctypes.POINTER(wintypes.FILETIME)
    signatures = {
        "OpenProcess": (wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD),
        "CreateJobObjectW": (wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCWSTR),
        "AssignProcessToJobObject": (wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE),
        "TerminateJobObject": (wintypes.BOOL, wintypes.HANDLE, wintypes.UINT),
        "CloseHandle": (wintypes.BOOL, wintypes.HANDLE),
        "CreateMutexW": (wintypes.HANDLE, wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR),
        "WaitForSingleObject": (wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD),
        "ReleaseMutex": (wintypes.BOOL, wintypes.HANDLE),
        "CreateFileW": (wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE),
        "SetFileTime": (wintypes.BOOL, wintypes.HANDLE, filetime_pointer, filetime_pointer, filetime_pointer),
        "GetConsoleMode": (wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)),
        "SetConsoleMode": (wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD),
    }
    for name, (result, *args) in signatures.items():
        call = getattr(kernel, name)
        call.restype, call.argtypes = result, args
    return kernel


# Keep descendants reachable after the process exits, as in BookTrans winproc.py.
def attach_job(proc):
    kernel = _kernel()
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        return
    process = kernel.OpenProcess(0x0101, False, proc.pid)  # SET_QUOTA | TERMINATE
    try:
        if process and kernel.AssignProcessToJobObject(job, process):
            proc.diaryze_job = job
            job = None
    finally:
        if process:
            kernel.CloseHandle(process)
        if job:
            kernel.CloseHandle(job)


# Stop the job, falling back to taskkill when Windows refuses job assignment.
def stop_tree(proc, grace):
    kernel = _kernel()
    job = getattr(proc, "diaryze_job", None)
    stopped = False
    if job:
        try:
            stopped = bool(kernel.TerminateJobObject(job, 1))
        finally:
            kernel.CloseHandle(job)
            proc.diaryze_job = None
    if not stopped and proc.poll() is None:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=grace)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if proc.poll() is None:
            proc.kill()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


# Lock a stable path name so the protected file can be closed and replaced.
@contextmanager
def path_mutex(path, *, blocking):
    kernel = _kernel()
    canonical = os.path.normcase(os.path.realpath(path))
    key = hashlib.sha256(os.fsencode(canonical)).hexdigest()
    mutex = kernel.CreateMutexW(None, False, "Global\\Diaryze-" + key)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = kernel.WaitForSingleObject(mutex, 0xFFFFFFFF if blocking else 0)
        if result == 258:  # WAIT_TIMEOUT
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable", path)
        if result not in (0, 128):  # WAIT_OBJECT_0 or an abandoned mutex
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            kernel.ReleaseMutex(mutex)
    finally:
        kernel.CloseHandle(mutex)


# Set only the last-write time, opening a reparse point itself rather than following it.
def set_mtime(path, timestamp_ns):
    kernel = _kernel()
    write_attributes = 0x0100
    share_all = 0x0007
    open_existing = 3
    open_reparse_point = 0x00200000
    handle = kernel.CreateFileW(os.fsdecode(path), write_attributes, share_all, None,
                                open_existing, open_reparse_point, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        ticks = timestamp_ns // 100 + FILETIME_EPOCH_OFFSET
        timestamp = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
        if not kernel.SetFileTime(handle, None, None, ctypes.byref(timestamp)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


# Retrieve the mode of a native console stream, if present.
def console_mode(stream):
    import msvcrt

    try:
        handle = msvcrt.get_osfhandle(stream.fileno())
    except (AttributeError, ValueError, OSError):
        return None
    mode = wintypes.DWORD()
    return (handle, mode.value) if _kernel().GetConsoleMode(handle, ctypes.byref(mode)) else None


# Enable ANSI output for the duration of the run and restore the original modes.
@contextmanager
def preserve_console(stdin, *outputs):
    kernel = _kernel()
    saved = {}
    for stream in (stdin, *outputs):
        if state := console_mode(stream):
            saved.setdefault(*state)
    for stream in outputs:
        if state := console_mode(stream):
            handle, mode = state
            kernel.SetConsoleMode(handle, mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    try:
        yield
    finally:
        for handle, mode in saved.items():
            kernel.SetConsoleMode(handle, mode)
