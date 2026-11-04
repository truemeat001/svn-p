"""Keep checkout's process working directory bound to the reviewed directory.

POSIX uses an open descriptor through procfs, never the mutable target pathname.
Windows holds non-write/non-delete-sharing directory handles for every ancestor
until both commands finish. Unsupported systems fail closed before SVN launch.
This is not a general filesystem transaction or protection for all child files.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat

from workspace_bridge import directory_identity


def local_destination(value, *, platform=os.name):
    raw = os.fspath(value)
    if (not isinstance(raw, str) or not raw or any(ord(c) < 32 or ord(c) == 127 for c in raw)
            or len(raw.encode('utf-8')) > 4096):
        raise ValueError('An absolute local checkout directory is required.')
    if platform == 'nt':
        if (not re.match(r'^[A-Za-z]:[\\/]', raw) or ':' in raw[2:]
                or any(part in {'.', '..'} or part.endswith((' ', '.'))
                       for part in re.split(r'[\\/]', raw[3:]) if part)):
            raise ValueError('A canonical local drive path is required.')
    elif not Path(raw).is_absolute():
        raise ValueError('An absolute local checkout directory is required.')
    return Path(raw)


@contextmanager
def pinned_directory(path, expected):
    if os.name == 'nt':
        with _windows_directory(path, expected) as cwd:
            yield cwd
    else:
        with _posix_directory(path, expected) as cwd:
            yield cwd


@contextmanager
def _posix_directory(path, expected):
    if not all(hasattr(os, key) for key in ('O_DIRECTORY', 'O_NOFOLLOW')):
        raise ValueError('Safe checkout directory anchoring is unavailable.')
    descriptors = []
    try:
        for index, (_, _, component) in enumerate(expected):
            name = component if index == 0 else Path(component).name
            options = {'dir_fd': descriptors[-1]} if descriptors else {}
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, **options)
            descriptors.append(fd)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != expected[index][:2] or not stat.S_ISDIR(info.st_mode):
                raise ValueError('Checkout path identity changed while anchoring.')
        cwd = Path('/proc') / str(os.getpid()) / 'fd' / str(descriptors[-1])
        info = cwd.stat()
        if (info.st_dev, info.st_ino) != expected[-1][:2] or directory_identity(path) != expected:
            raise ValueError('Checkout working-directory anchor could not be verified.')
        yield cwd
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


@contextmanager
def _windows_directory(path, expected):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]; close.restype = wintypes.BOOL
    handles = []
    try:
        for _, _, component in expected:
            # FILE_READ_ATTRIBUTES; FILE_SHARE_READ only. In particular neither
            # delete/rename nor reparse-point modification may race child chdir.
            handle = create(component, 0x80, 0x1, None, 3, 0x02000000 | 0x00200000, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                raise OSError('Could not retain safe checkout directory handles.')
            handles.append(handle)
        if directory_identity(path) != expected:
            raise ValueError('Checkout directory changed while retaining handles.')
        yield path
    finally:
        failed = False
        for handle in reversed(handles):
            if not close(handle):
                failed = True
        if failed:
            raise OSError('Checkout directory handle cleanup could not be verified.')
