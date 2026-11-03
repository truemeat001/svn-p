"""Comparable timestamps for path and descriptor file identity checks."""
from __future__ import annotations

import os


_WINDOWS = os.name == "nt"


def identity_time_ns(node: os.stat_result) -> int:
    """Keep Windows creation time and POSIX metadata-change time comparable.

    Windows Python 3.12+ exposes creation time explicitly. Some releases return
    creation time from stat/lstat.st_ctime_ns but ChangeTime from fstat. Using
    that field across the two APIs rejects an unchanged file after any write.
    Older Windows Python has no birthtime field and uses creation time for both.
    Callers must still compare raw fstat ctime before/after a read to detect
    descriptor metadata changes during that read.
    """
    if _WINDOWS:
        birthtime = getattr(node, "st_birthtime_ns", None)
        return node.st_ctime_ns if birthtime is None else birthtime
    return node.st_ctime_ns
