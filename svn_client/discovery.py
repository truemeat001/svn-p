"""Read-only, bounded discovery of possible SVN working-copy roots.

Only directory names and filesystem metadata are inspected. A real directory
named ``.svn`` is a *candidate marker*, not evidence of a usable working copy,
repository identity, or an ``svn:externals`` relationship. The caller must use
SVN info to validate each independently before offering repository operations.

The scan includes nested candidates, never reads inside .svn, never reads user
file contents, and excludes symbolic links and Windows reparse points. Time
and cancellation limits are cooperative: a blocked filesystem system call
cannot be interrupted by this synchronous standard-library function. Run it
off the UI thread. Results are observations, not a stable filesystem snapshot.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import math
import os
from pathlib import Path
import stat
import time
from typing import Callable, Iterator


class DiscoveryError(ValueError):
    """The selected parent or scan bounds are invalid or unsafe."""


@dataclass(frozen=True)
class WorkingCopyCandidate:
    path: str
    relation: str  # "root" or "nested"; neither asserts an external.
    containing_root: str | None = None
    verified: bool = False


@dataclass(frozen=True)
class DiscoveryIssue:
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class DiscoveryResult:
    parent: str
    working_copies: tuple[WorkingCopyCandidate, ...]
    complete: bool
    truncated: bool
    reasons: tuple[str, ...]
    issues: tuple[DiscoveryIssue, ...]
    directories_scanned: int
    entries_scanned: int
    elapsed_seconds: float
    skipped_links: int = 0
    skipped_mounts: int = 0
    skipped_duplicates: int = 0
    issues_truncated: bool = False


_CAN_OPEN_NOFOLLOW = (os.open in os.supports_dir_fd and os.scandir in os.supports_fd
                      and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"))
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_MAX_ISSUES = 100
_LIMIT_REASONS = {"depth_limit", "directory_limit", "entry_limit", "working_copy_limit",
                  "time_limit", "cancelled"}


def _unsafe(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _identity(info: os.stat_result, path: Path) -> tuple[object, ...]:
    # Some filesystems provide zero file IDs. Do not merge all such directories.
    return ((info.st_dev, info.st_ino) if info.st_ino
            else ("path", os.path.normcase(os.path.abspath(path))))


def _checked_path(path: Path) -> os.stat_result:
    """Check every component without resolve(), which would follow links."""
    current = Path(path.anchor)
    info = os.lstat(current)
    for part in path.parts[1:]:
        if _unsafe(info) or not stat.S_ISDIR(info.st_mode):
            raise DiscoveryError("The parent path must not contain links or reparse points.")
        current /= part
        info = os.lstat(current)
    if _unsafe(info) or not stat.S_ISDIR(info.st_mode):
        raise DiscoveryError("The parent must be a real directory, not a link or reparse point.")
    return info


def _open_components(path: Path, base_fd: int | None = None) -> int:
    """Open each component relative to its predecessor, never following links."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path.anchor, flags) if base_fd is None else os.dup(base_fd)
    parts = path.parts[1:] if base_fd is None else path.parts
    try:
        for part in parts:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _directory(root: Path, relative: Path, root_fd: int | None,
               root_identity: tuple[object, ...], expected: tuple[object, ...]
               ) -> Iterator[tuple[Path | int, os.stat_result]]:
    path = root / relative
    # Refuse a renamed/replaced parent rather than returning paths for an old
    # root still held open by its descriptor. Windows also checks every child
    # component because scandir there does not accept a directory descriptor.
    current_root = _checked_path(root)
    if _identity(current_root, root) != root_identity:
        raise DiscoveryError("The scan parent changed during discovery.")
    if root_fd is None:
        info = _checked_path(path)
        if _identity(info, path) != expected:
            raise DiscoveryError("A directory changed during discovery.")
        yield path, info
        after = _checked_path(path)
        if _identity(after, path) != expected:
            raise DiscoveryError("A directory changed during discovery.")
    else:
        descriptor = _open_components(relative, root_fd)
        try:
            info = os.fstat(descriptor)
            if _unsafe(info) or _identity(info, path) != expected:
                raise DiscoveryError("A directory changed during discovery.")
            yield descriptor, info
        finally:
            os.close(descriptor)


def discover_working_copies(parent: str | os.PathLike[str], *, max_depth: int = 8,
                           max_directories: int = 10_000, max_entries: int = 50_000,
                           max_working_copies: int = 200, timeout: float = 5.0,
                           cancelled: Callable[[], bool] | None = None) -> DiscoveryResult:
    """Find sibling/nested .svn-directory candidates below an explicit parent.

    The parent has depth zero. Limits include the parent and all enumerated
    entries (including regular files, without opening them). Link/reparse
    exclusions are part of the scope, so ``complete`` may be true with
    ``skipped_links`` > 0. Mount/device boundaries, access failures, changed
    directories, and resource limits make it false. ``truncated`` specifically
    means a resource bound or cancellation stopped some part of the scan.

    Bounds may be lowered or raised explicitly, but must be finite positive
    values (depth may be zero). Cancellation should be a cheap callback, e.g.
    ``threading.Event.is_set``. There are no writes, subprocesses or SVN calls.
    """
    for name, value in (("max_depth", max_depth), ("max_directories", max_directories),
                        ("max_entries", max_entries), ("max_working_copies", max_working_copies)):
        if isinstance(value, bool) or not isinstance(value, int) or value < (0 if name == "max_depth" else 1):
            raise DiscoveryError(f"{name} must be an integer {'at least zero' if name == 'max_depth' else 'greater than zero'}.")
    try:
        valid_timeout = (not isinstance(timeout, bool) and isinstance(timeout, (int, float))
                         and math.isfinite(timeout) and timeout > 0)
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise DiscoveryError("timeout must be a finite positive number.")
    if cancelled is not None and not callable(cancelled):
        raise DiscoveryError("cancelled must be a callable or None.")
    raw = os.fspath(parent)
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        raise DiscoveryError("Select a local parent directory.")
    root = Path(os.path.abspath(os.path.expanduser(raw)))
    if any(part.lower() == ".svn" for part in root.parts):
        raise DiscoveryError("Discovery cannot scan SVN administrative directories.")

    start = time.monotonic()
    deadline = start + timeout
    reasons: list[str] = []
    issues: list[DiscoveryIssue] = []
    candidates: list[WorkingCopyCandidate] = []
    counters = {"directories": 0, "entries": 0, "links": 0, "mounts": 0, "duplicates": 0}
    issues_truncated = False

    def reason(code: str) -> None:
        if code not in reasons:
            reasons.append(code)

    def issue(path: Path, code: str, message: str) -> None:
        nonlocal issues_truncated
        reason(code)
        if len(issues) < _MAX_ISSUES:
            issues.append(DiscoveryIssue(str(path), code, message))
        else:
            issues_truncated = True

    def stopped() -> bool:
        if cancelled is not None and cancelled():
            reason("cancelled")
            return True
        if time.monotonic() >= deadline:
            reason("time_limit")
            return True
        return False

    def result() -> DiscoveryResult:
        ordered = tuple(sorted(candidates, key=lambda item: (item.path.casefold(), item.path)))
        return DiscoveryResult(str(root), ordered, not reasons, bool(_LIMIT_REASONS.intersection(reasons)),
                               tuple(reasons), tuple(issues), counters["directories"], counters["entries"],
                               max(0.0, time.monotonic() - start), counters["links"], counters["mounts"],
                               counters["duplicates"], issues_truncated)

    if stopped():
        return result()
    try:
        root_info = _checked_path(root)
    except OSError as exc:
        raise DiscoveryError("The parent directory does not exist or is not accessible.") from exc
    root_identity = _identity(root_info, root)
    root_fd: int | None = None
    try:
        if _CAN_OPEN_NOFOLLOW:
            root_fd = _open_components(root)
            if _identity(os.fstat(root_fd), root) != root_identity:
                raise DiscoveryError("The scan parent changed before discovery.")
        # Breadth first: sibling working copies are found before deep contents.
        pending = deque([(Path(), 0, None, root_identity)])
        seen = {root_identity}
        scheduled = 1
        while pending and not stopped():
            relative, depth, containing_root, expected = pending.popleft()
            path = root / relative
            try:
                with _directory(root, relative, root_fd, root_identity, expected) as (scan_target, info):
                    if stopped():
                        break
                    counters["directories"] += 1
                    # Probe only the marker itself. Never enter/read .svn.
                    try:
                        marker = (os.stat(".svn", dir_fd=scan_target, follow_symlinks=False)
                                  if isinstance(scan_target, int) else os.lstat(path / ".svn"))
                    except FileNotFoundError:
                        marker = None
                    if stopped():
                        break
                    if marker is not None:
                        if _unsafe(marker):
                            counters["links"] += 1
                        elif stat.S_ISDIR(marker.st_mode):
                            if len(candidates) >= max_working_copies:
                                reason("working_copy_limit")
                                break
                            candidates.append(WorkingCopyCandidate(str(path), "nested" if containing_root else "root", containing_root))
                            containing_root = str(path)
                    with os.scandir(scan_target) as entries:
                        while not stopped():
                            # Stop before another iterator read. When exactly
                            # the budget is consumed, completeness is unknown
                            # until a caller explicitly reruns with a larger
                            # budget, so conservatively report truncation.
                            if counters["entries"] >= max_entries:
                                reason("entry_limit")
                                break
                            try:
                                entry = next(entries)
                            except StopIteration:
                                break
                            counters["entries"] += 1
                            if entry.name.lower() == ".svn":
                                continue
                            if stopped():
                                break
                            try:
                                if entry.is_symlink():
                                    counters["links"] += 1
                                    continue
                                # Directory entries usually contain the type,
                                # avoiding even a stat call for ordinary files.
                                if not entry.is_dir(follow_symlinks=False):
                                    continue
                                entry_info = entry.stat(follow_symlinks=False)
                                # Windows' cached DirEntry metadata sets device
                                # and inode IDs to zero. Refresh incomplete IDs
                                # without following links before using them for
                                # device boundaries or queued-directory identity.
                                if (not _unsafe(entry_info) and stat.S_ISDIR(entry_info.st_mode)
                                        and (not entry_info.st_dev or not entry_info.st_ino)):
                                    entry_info = (os.stat(entry.name, dir_fd=scan_target, follow_symlinks=False)
                                                  if isinstance(scan_target, int) else os.lstat(path / entry.name))
                            except OSError:
                                issue(path / entry.name, "entry_unavailable", "A directory entry could not be inspected.")
                                continue
                            if _unsafe(entry_info):
                                counters["links"] += 1
                                continue
                            if not stat.S_ISDIR(entry_info.st_mode):
                                continue
                            if entry_info.st_dev != root_info.st_dev:
                                counters["mounts"] += 1
                                reason("mount_boundary")
                                continue
                            child_relative = relative / entry.name
                            identity = _identity(entry_info, root / child_relative)
                            if identity in seen:
                                counters["duplicates"] += 1
                                continue
                            if depth >= max_depth:
                                reason("depth_limit")
                                continue
                            if scheduled >= max_directories:
                                reason("directory_limit")
                                continue
                            seen.add(identity)
                            scheduled += 1
                            pending.append((child_relative, depth + 1, containing_root, identity))
                    if "entry_limit" in reasons or "time_limit" in reasons or "cancelled" in reasons:
                        break
            except DiscoveryError:
                issue(path, "directory_changed", "A directory changed or became a link during discovery; it was skipped.")
            except OSError:
                issue(path, "directory_unavailable", "A directory or its SVN marker could not be inspected.")
        stopped()
    except OSError as exc:
        raise DiscoveryError("The parent directory could not be opened safely.") from exc
    finally:
        if root_fd is not None:
            os.close(root_fd)
    return result()
