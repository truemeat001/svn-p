"""Conservative, explicitly confirmed SVN conflict handling.

This module never edits conflict markers, launches a merge program, or resolves
tree/property conflicts. It checks local SVN metadata and hashes the manually
reviewed working file, then delegates one exact, nonrecursive "working"
resolution to the trusted SVN executable after a fresh preflight. Conflict
sidecar contents are not fingerprinted; no sidecar-driven choice is enabled.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Sequence
import xml.etree.ElementTree as ET

from .backend import CommandResult, StatusEntry, SvnClient, SvnError
from .file_identity import identity_time_ns


ACCEPT_CHOICES = ("working",)
MAX_RESOLVE_PATHS = 128
MAX_CONFLICT_FILE_BYTES = 32 * 1024 * 1024
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE)


class ConflictError(ValueError):
    """A conflict operation was refused before any resolve command."""


@dataclass(frozen=True)
class ConflictInspection:
    path: str
    kind: str
    text_conflicted: bool
    property_conflicted: bool
    tree_conflicted: bool
    status: StatusEntry
    info: dict[str, object]
    allowed_accepts: tuple[str, ...]
    blocked_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    file_fingerprint: tuple[int, ...] | None = None
    ancestor_statuses: tuple[StatusEntry, ...] = ()
    content_sha256: str | None = None


@dataclass(frozen=True)
class ConflictPlan:
    paths: tuple[str, ...]
    accept: str
    inspections: tuple[ConflictInspection, ...]
    state_sha256: str
    warnings: tuple[str, ...]


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) &
                                           getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, identity_time_ns(info))


def _file_digest(path: Path, expected: tuple[int, ...]) -> str:
    """Hash one bounded regular file, refusing links and observable changes.

    POSIX opens each ancestor with O_NOFOLLOW. Other platforms use the caller's
    ancestor checks and compare the opened handle before reading any content.
    Neither mechanism locks an editor out of the file after this function.
    """
    if expected[2] > MAX_CONFLICT_FILE_BYTES:
        raise ConflictError("The working file exceeds the 32 MiB verified-resolution limit.")
    if expected[1] == 0:
        raise ConflictError("The filesystem did not supply a stable file identity for verified resolution.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = None
    descriptor = None
    try:
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent_fd = os.open(path.anchor, directory_flags)
            for part in path.parts[1:-1]:
                child_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = child_fd
            descriptor = os.open(path.name, flags, dir_fd=parent_fd)
        else:
            descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if _linked(before) or not stat.S_ISREG(before.st_mode) or _fingerprint(before) != expected:
            raise ConflictError("The working file changed before its content could be checked.")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_CONFLICT_FILE_BYTES - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CONFLICT_FILE_BYTES:
                raise ConflictError("The working file exceeds the 32 MiB verified-resolution limit.")
            digest.update(chunk)
        after = os.fstat(descriptor)
        current = path.lstat()
        if (_linked(current) or not stat.S_ISREG(current.st_mode) or total != expected[2]
                or before.st_ctime_ns != after.st_ctime_ns
                or _fingerprint(after) != expected or _fingerprint(current) != expected):
            raise ConflictError("The working file changed while its content was being checked.")
        return digest.hexdigest()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_fd is not None:
            os.close(parent_fd)


class ConflictManager:
    """One selected client/workspace only. No cross-working-copy resolution."""

    def __init__(self, client: SvnClient):
        self.client = client
        self.working_copy = client.working_copy

    def _path(self, value: str | os.PathLike[str]) -> str:
        try:
            raw = os.fspath(value)
        except TypeError as exc:
            raise ConflictError("Select an explicit local conflict path.") from exc
        if (not isinstance(raw, str) or not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw)
                or "://" in raw or raw.lower().startswith("file:")
                or (ntpath.splitdrive(raw)[0] and not ntpath.isabs(raw))
                or (os.name != "nt" and (ntpath.splitdrive(raw)[0] or "\\" in raw))):
            raise ConflictError("Select a local path inside this working copy.")
        # Validate before Windows abspath strips trailing dots/spaces and can
        # silently map a rejected spelling onto another file.
        supplied = Path(raw)
        if any(part not in {supplied.anchor, ".", ".."} and (
                part.casefold() == ".svn" or ":" in part
                or part.endswith((" ", ".")) or _RESERVED.match(part))
               for part in supplied.parts):
            raise ConflictError("SVN administrative or nonportable paths cannot be conflict targets.")
        absolute = Path(os.path.abspath(self.working_copy / raw))
        try:
            relative = absolute.relative_to(self.working_copy)
        except ValueError as exc:
            raise ConflictError("Conflict targets cannot leave the selected working copy.") from exc
        if any(part.lower() == ".svn" or ":" in part or part.endswith((" ", "."))
               or _RESERVED.match(part) for part in relative.parts):
            raise ConflictError("SVN administrative or nonportable paths cannot be conflict targets.")
        # No resolve() call: even in-workspace links/reparse points are refused.
        cursor = Path(self.working_copy.anchor)
        for part in (*self.working_copy.parts[1:], *relative.parts):
            cursor /= part
            try:
                metadata = cursor.lstat()
            except FileNotFoundError:
                continue  # Missing tree-conflict victims remain inspectable.
            if _linked(metadata):
                raise ConflictError("Conflict targets cannot traverse symbolic links or reparse points.")
        return str(relative)

    def _paths(self, values: Sequence[str | os.PathLike[str]]) -> list[str]:
        if values is None or isinstance(values, (str, bytes, os.PathLike)) or not values:
            raise ConflictError("Select explicit conflict paths; an empty selection never means all.")
        if len(values) > MAX_RESOLVE_PATHS:
            raise ConflictError(f"Select at most {MAX_RESOLVE_PATHS} paths per conflict operation.")
        return list(dict.fromkeys(self._path(value) for value in values))

    @staticmethod
    def _target(path: str) -> str:
        return path + "@" if "@" in path else path

    def list(self, paths: Sequence[str | os.PathLike[str]] | None = None) -> list[StatusEntry]:
        """Read conflicts under the selected scope; no automatic resolution."""
        selected = None if paths is None else self._paths(paths)
        entries = self.client.status(selected, verbose=True)
        conflicts = []
        for entry in entries:
            if entry.conflicted:
                # Do not allow an unexpected outside metadata path to become
                # a later UI/CLI mutation target.
                self._path(entry.path)
                conflicts.append(entry)
        return conflicts

    def _status_one(self, path: str) -> StatusEntry:
        result = self.client.command(["status", "--xml", "--ignore-externals", "--verbose",
                                      "--depth", "empty", "--", self._target(path)])
        try:
            root = ET.fromstring(result.stdout)
        except ET.ParseError as exc:
            raise SvnError("SVN returned invalid conflict status XML.") from exc
        wanted = _key(self.working_copy / path)
        matches = [entry for entry in root.findall(".//entry")
                   if entry.get("path") and _key(self.working_copy / entry.get("path", "")) == wanted]
        if len(matches) != 1 or matches[0].find("wc-status") is None:
            raise ConflictError("SVN did not return one exact status for this target. Refresh before resolving.")
        node = matches[0].find("wc-status")
        assert node is not None
        revision = node.get("revision", "")
        return StatusEntry(path, node.get("item", "none"), node.get("props", "none"),
                           revision=int(revision) if revision.isdigit() else None,
                           tree_conflicted=node.get("tree-conflicted") == "true",
                           copied=node.get("copied") == "true", switched=node.get("switched") == "true",
                           wc_locked=node.get("wc-locked") == "true", moved_from=node.get("moved-from"),
                           moved_to=node.get("moved-to"), file_external=node.get("file-external") == "true")

    def inspect(self, path: str | os.PathLike[str]) -> ConflictInspection:
        """Inspect one exact node, including unsupported conflicts, read-only."""
        with self.client._lock:
            relative = self._path(path)
            status = self._status_one(relative)
            blocked: list[str] = []
            warnings: list[str] = []
            root_info = self.client.info()
            try:
                info = dict(self.client.info(relative))
            except SvnError:
                info = {}
                blocked.append("metadata_unavailable")
            expected_root = str(root_info.get("wc_root") or "")
            actual_root = str(info.get("wc_root") or "")
            if not expected_root or not actual_root or _key(Path(expected_root)) != _key(Path(actual_root)):
                blocked.append("different_or_unknown_working_copy")
            if expected_root and _key(Path(expected_root)) != _key(self.working_copy):
                blocked.append("select_working_copy_root")
            expected_uuid = str(root_info.get("repository_uuid") or "")
            actual_uuid = str(info.get("repository_uuid") or "")
            if not expected_uuid or not actual_uuid or expected_uuid != actual_uuid:
                blocked.append("different_or_unknown_repository")
            if not status.conflicted:
                blocked.append("not_conflicted")
            if status.tree_conflicted:
                blocked.append("tree_conflict_requires_external_resolution")
            if status.file_external or status.item == "external":
                blocked.append("external_target")
            if status.copied or status.moved_from or status.moved_to:
                blocked.append("copy_or_move_target")
            if status.switched:
                blocked.append("switched_target")
            if status.wc_locked:
                blocked.append("working_copy_locked")
            ancestors: list[StatusEntry] = []
            cursor = Path(relative)
            while str(cursor) != ".":
                cursor = cursor.parent
                ancestor = self._status_one(str(cursor))
                ancestors.append(ancestor)
                if (ancestor.tree_conflicted or ancestor.copied or ancestor.switched
                        or ancestor.file_external or ancestor.moved_from or ancestor.moved_to
                        or ancestor.wc_locked or ancestor.item not in {"normal", "modified", "none"}):
                    blocked.append("unsafe_ancestor")
            if status.item not in {"normal", "modified", "conflicted"}:
                blocked.append("unsupported_node_status")
            if status.props not in {"none", "normal", "modified", "conflicted"}:
                blocked.append("unsupported_property_status")
            if info.get("schedule") != "normal":
                blocked.append("unsupported_schedule")
            kind = str(info.get("kind") or "unknown")
            fingerprint = None
            try:
                metadata = (self.working_copy / relative).lstat()
                if _linked(metadata) or not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                    blocked.append("unsupported_filesystem_node")
                if ((kind == "file" and not stat.S_ISREG(metadata.st_mode)) or
                        (kind == "dir" and not stat.S_ISDIR(metadata.st_mode))):
                    blocked.append("node_kind_changed")
                fingerprint = _fingerprint(metadata)
            except OSError:
                blocked.append("filesystem_node_unavailable")
            if kind not in {"file", "dir"} or kind == "dir" and status.item == "conflicted":
                blocked.append("unsupported_node_kind")
            if status.props == "conflicted":
                blocked.append("property_conflict_requires_external_resolution")
                warnings.append("Property and mixed conflicts require a trusted external SVN client; this version does not fingerprint their actual conflict inputs.")
            if kind != "file":
                blocked.append("ordinary_file_required")
            content_digest = None
            if not blocked and fingerprint is not None:
                try:
                    content_digest = _file_digest(self.working_copy / relative, fingerprint)
                except (ConflictError, OSError) as exc:
                    blocked.append("content_fingerprint_unavailable")
                    warnings.append(str(exc))
            allowed = () if blocked else ACCEPT_CHOICES
            if allowed:
                warnings.append("Resolution removes SVN conflict artifacts, whose contents are not protected by this token. Back up the working file and all conflict sidecar files first. No concurrent edits are safe.")
            return ConflictInspection(relative, kind, status.item == "conflicted", status.props == "conflicted",
                                      status.tree_conflicted, status, info, tuple(allowed),
                                      tuple(dict.fromkeys(blocked)), tuple(warnings), fingerprint, tuple(ancestors), content_digest)

    def preflight_resolve(self, paths: Sequence[str | os.PathLike[str]], accept: str) -> ConflictPlan:
        """Validate exact content-conflict victims and bind their working bytes.

        The token does not cover SVN's sidecar contents or conflict properties.
        Whole-version choices and property conflicts are therefore unsupported.
        """
        if accept not in ACCEPT_CHOICES:
            raise ConflictError("Only 'working' for manually repaired ordinary-file content conflicts is supported. Other choices require a trusted external SVN client.")
        with self.client._lock:
            selected = self._paths(paths)
            inspections = tuple(self.inspect(path) for path in selected)
            for inspected in inspections:
                if accept not in inspected.allowed_accepts:
                    detail = ", ".join(inspected.blocked_reasons) or "unsupported_resolution"
                    raise ConflictError(f"Cannot resolve {inspected.path}: {detail}.")
            # Never allow platform argv truncation, or silently split a reviewed
            # batch into multiple potentially partial mutation commands.
            argv = ["svn", "--non-interactive", "--no-auth-cache", "resolve", "--accept", accept,
                    "--depth", "empty", "--", *(self._target(path) for path in selected)]
            if len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 > 24_000:
                raise ConflictError("This resolution selection is too long. Select fewer paths.")
            state = {"workspace": str(self.working_copy), "accept": accept,
                     "inspections": [asdict(item) for item in inspections]}
            digest = hashlib.sha256(json.dumps(state, ensure_ascii=True, sort_keys=True,
                                               separators=(",", ":")).encode("ascii")).hexdigest()
            warnings = ["Only the listed nodes are resolved with depth=empty. No commit is performed.",
                        "The token binds current victim-file bytes and metadata, not conflict sidecar contents or property values. Back up all conflict files and do not edit or run other SVN tools concurrently.",
                        "A failed/interrupted batch may have resolved some nodes. Inspect status before any retry."]
            warnings.append("You must have manually repaired and reviewed the current working file; SVN does not repair its contents for you. Conflict sidecar files will be removed, even if you edited them separately.")
            return ConflictPlan(tuple(selected), accept, inspections, digest, tuple(warnings))

    def resolve(self, paths: Sequence[str | os.PathLike[str]], accept: str, *, confirmed: bool = False,
                expected_state_sha256: str | None = None) -> CommandResult:
        """Recheck and resolve once. The caller owns the actual user confirmation."""
        if confirmed is not True:
            raise ConflictError("Conflict resolution requires explicit confirmation of the paths and --accept choice.")
        if expected_state_sha256 is not None and (not isinstance(expected_state_sha256, str)
                                                  or not re.fullmatch(r"[0-9a-f]{64}", expected_state_sha256)):
            raise ConflictError("The expected conflict state must be a lowercase SHA-256 value.")
        with self.client._lock:
            plan = self.preflight_resolve(paths, accept)
            if expected_state_sha256 is not None and plan.state_sha256 != expected_state_sha256:
                raise ConflictError("Working-file bytes or checked metadata changed after review. Inspect and confirm again before resolving.")
            result = self.client.command(["resolve", "--accept", accept, "--depth", "empty", "--",
                                          *(self._target(path) for path in plan.paths)])
            for path in plan.paths:
                try:
                    self._path(path)
                    remaining = self._status_one(path)
                except (ConflictError, SvnError, OSError) as exc:
                    raise SvnError("Resolution returned successfully, but its exact target status could not be verified. Inspect this working copy before retrying; changes may already be complete.") from exc
                if remaining.conflicted:
                    raise SvnError("Resolution returned successfully, but an exact target still reports conflicts. Inspect this working copy before retrying; changes may be partial.")
            return result
