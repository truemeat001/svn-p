"""Explicit, conservative SVN file deletion, movement and history-preserving copy.

Only existing regular versioned files in one working-copy root are supported.
No directories, URLs, force, implicit parents, cross-root operations or commits.
Preflight is read-only apart from isolated temporary BASE exports for deletion;
it is not a reservation against an external editor or another SVN process.
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
import tempfile
from typing import Sequence
from urllib.parse import quote, unquote

from .backend import CommandResult, StatusEntry, SvnClient, SvnError


MAX_DELETE_PATHS = 128
_CHUNK = 1_048_576
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE)


class FileOperationError(ValueError):
    """An unsupported or unsafe file operation was refused."""


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    info: dict[str, object]
    status: StatusEntry
    content_sha256: str
    size_bytes: int
    properties_sha256: str
    fingerprint: tuple[int, ...]


@dataclass(frozen=True)
class AncestorSnapshot:
    path: str
    info: dict[str, object]
    status: StatusEntry


@dataclass(frozen=True)
class FileOperationPlan:
    operation: str
    workspace: str
    paths: tuple[str, ...]
    destination: str | None
    affected_paths: tuple[str, ...]
    files: tuple[FileSnapshot, ...]
    ancestors: tuple[AncestorSnapshot, ...]
    state_sha256: str
    warnings: tuple[str, ...]
    commit_required: bool = True
    server_checked: bool = False
    scope: str = "regular_files_only"


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _linked(node: os.stat_result) -> bool:
    return stat.S_ISLNK(node.st_mode) or bool(getattr(node, "st_file_attributes", 0) &
                                           getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _fingerprint(node: os.stat_result) -> tuple[int, ...]:
    return (node.st_dev, node.st_ino, node.st_mode, node.st_size,
            node.st_mtime_ns, node.st_ctime_ns, node.st_nlink)


def _snapshot_bytes(path: Path) -> tuple[str, int, tuple[int, ...]]:
    """Stream without following the final symlink; detect replacement/edits."""
    before = path.lstat()
    if _linked(before) or not stat.S_ISREG(before.st_mode):
        raise FileOperationError("Only ordinary regular files are supported.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if _fingerprint(opened) != _fingerprint(before) or not stat.S_ISREG(opened.st_mode):
            raise FileOperationError("A file changed while it was being inspected. Refresh and retry the preflight.")
        digest, total = hashlib.sha256(), 0
        # Never chase a concurrently appended file forever. Read at most the
        # initial size plus one byte; growth is an immediate stale-state error.
        while total <= opened.st_size:
            chunk = handle.read(min(_CHUNK, opened.st_size + 1 - total))
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > opened.st_size:
                raise FileOperationError("A file grew while it was being inspected. Refresh and retry the preflight.")
        after = os.fstat(handle.fileno())
    if _fingerprint(before) != _fingerprint(after) or _fingerprint(path.lstat()) != _fingerprint(before):
        raise FileOperationError("A file changed while it was being inspected. Refresh and retry the preflight.")
    return digest.hexdigest(), total, _fingerprint(after)


class FileOperationManager:
    """Use a trusted SvnClient. Mutation confirmation belongs to the caller."""

    def __init__(self, client: SvnClient):
        self.client = client
        self.working_copy = client.working_copy

    def _path(self, value: str | os.PathLike[str], *, allow_root: bool = False) -> str:
        try:
            raw = os.fspath(value)
        except TypeError as exc:
            raise FileOperationError("Select an explicit local file path.") from exc
        if (not isinstance(raw, str) or not raw or any(ord(c) < 32 or ord(c) == 127 for c in raw)
                or "://" in raw or raw.lower().startswith("file:")
                or (os.name != "nt" and (ntpath.splitdrive(raw)[0] or "\\" in raw))):
            raise FileOperationError("Select an explicit local path inside this working copy; URLs are not allowed.")
        absolute = Path(os.path.abspath(self.working_copy / raw))
        try:
            relative = absolute.relative_to(self.working_copy)
        except ValueError as exc:
            raise FileOperationError("File operations cannot leave the selected working copy.") from exc
        if not relative.parts and not allow_root:
            raise FileOperationError("The working-copy root cannot be a file operation target.")
        for part in relative.parts:
            if (part.casefold() == ".svn" or ":" in part or part.endswith((" ", ".")) or _RESERVED.match(part)):
                raise FileOperationError("Administrative or nonportable path components are not supported.")
        cursor = Path(self.working_copy.anchor)
        for part in (*self.working_copy.parts[1:], *relative.parts):
            cursor /= part
            try:
                node = cursor.lstat()
            except FileNotFoundError:
                continue
            if _linked(node):
                raise FileOperationError("File operations cannot traverse symbolic links or reparse points.")
            if cursor != self.working_copy and cursor != absolute and cursor.is_dir() and (cursor / ".svn").exists():
                # Ancestors outside the selected root can belong to a parent WC;
                # the authoritative root check below handles that case.
                if self.working_copy in cursor.parents:
                    raise FileOperationError("Nested working copies and externals must be opened separately.")
        return str(relative)

    def _paths(self, paths: Sequence[str | os.PathLike[str]]) -> list[str]:
        if paths is None or isinstance(paths, (str, bytes, os.PathLike)) or not paths:
            raise FileOperationError("Select explicit files; an empty selection never means all.")
        if len(paths) > MAX_DELETE_PATHS:
            raise FileOperationError(f"Select at most {MAX_DELETE_PATHS} files per operation.")
        selected = [self._path(path) for path in paths]
        return sorted(set(selected), key=lambda value: (value.casefold(), value))

    @staticmethod
    def _target(path: str) -> str:
        return path + "@" if "@" in path else path

    def _status(self, path: str, *, absent_ok: bool = False) -> StatusEntry | None:
        root = self.client._xml(["status", "--xml", "--ignore-externals", "--verbose", "--no-ignore",
                                 "--depth", "empty", "--", self._target(path)])
        entries = root.findall(".//entry")
        if not entries and absent_ok:
            return None
        if len(entries) != 1 or not entries[0].get("path") or \
                _key(self.working_copy / entries[0].get("path", "")) != _key(self.working_copy / path):
            raise FileOperationError("SVN did not return one exact status for the requested path.")
        status = entries[0].find("wc-status")
        if status is None:
            raise FileOperationError("SVN did not return working-copy status for this path.")
        revision = status.get("revision", "")
        label = next((group.get("name") for group in root.findall(".//changelist")
                      if entries[0] in list(group)), None)
        return StatusEntry(path, status.get("item", "none"), status.get("props", "none"),
                           revision=int(revision) if revision.isdigit() else None,
                           tree_conflicted=status.get("tree-conflicted") == "true",
                           copied=status.get("copied") == "true", switched=status.get("switched") == "true",
                           wc_locked=status.get("wc-locked") == "true", moved_from=status.get("moved-from"),
                           moved_to=status.get("moved-to"), changelist=label,
                           file_external=status.get("file-external") == "true")

    def _properties_hash(self, path: str) -> str:
        root = self.client._xml(["proplist", "--xml", "--verbose", "--depth", "empty", "--", self._target(path)])
        targets = root.findall("target")
        # SVN can omit the target element when the file has no properties.
        # command() already rejects nonzero exit codes, and status/info verify
        # the exact versioned node independently.
        if root.tag == "properties" and not list(root):
            return hashlib.sha256(b"[]").hexdigest()
        if len(targets) != 1 or not targets[0].get("path") or \
                _key(self.working_copy / targets[0].get("path", "")) != _key(self.working_copy / path):
            raise FileOperationError("SVN did not return exact property metadata for this file.")
        values = sorted((p.get("name", ""), p.get("encoding", ""), p.text or "")
                        for p in targets[0].findall("property"))
        return hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()

    def _root_info(self) -> dict[str, object]:
        self._path(".", allow_root=True)
        info = self.client.info()
        if (info.get("kind") != "dir" or not info.get("wc_root") or
                _key(Path(str(info["wc_root"]))) != _key(self.working_copy)):
            raise FileOperationError("Open the actual SVN working-copy root for file operations.")
        if not info.get("repository_uuid") or not info.get("url") or info.get("schedule") != "normal":
            raise FileOperationError("The working-copy repository identity or schedule is unavailable or unsafe.")
        return info

    def _check_identity(self, path: str, info: dict[str, object], root: dict[str, object], kind: str) -> None:
        if (info.get("kind") != kind or not info.get("wc_root") or
                _key(Path(str(info["wc_root"]))) != _key(self.working_copy)
                or info.get("repository_uuid") != root["repository_uuid"]):
            raise FileOperationError("Only versioned regular files and parents in this same working-copy root are supported.")
        expected_url = str(root["url"]).rstrip("/") + ("/" + quote(Path(path).as_posix(), safe="/") if path != "." else "")
        if unquote(str(info.get("url", ""))).rstrip("/") != unquote(expected_url).rstrip("/"):
            raise FileOperationError("Switched paths or a different repository mapping are not supported.")
        if info.get("schedule") != "normal":
            raise FileOperationError("Paths already scheduled for addition, deletion or replacement are not supported.")

    @staticmethod
    def _check_status(status: StatusEntry, *, clean: bool = False) -> None:
        if (status.conflicted or status.copied or status.switched or status.file_external or status.wc_locked
                or status.moved_from or status.moved_to or status.item not in {"normal", "modified"}
                or status.props not in {"none", "normal", "modified"}):
            raise FileOperationError("Conflicted, copied, moved, switched, external, locked or unsupported paths are refused.")
        if clean and (status.item != "normal" or status.props == "modified"):
            raise FileOperationError("Deletion requires clean content and properties; back up and deliberately handle local edits first.")

    def _ancestor(self, path: str, root: dict[str, object]) -> AncestorSnapshot:
        path = self._path(path, allow_root=True)
        if not (self.working_copy / path).is_dir():
            raise FileOperationError("The destination parent must already exist as a versioned directory.")
        info = self.client.info(path)
        self._check_identity(path, info, root, "dir")
        status = self._status(path)
        assert status is not None
        self._check_status(status)
        return AncestorSnapshot(path, info, status)

    def _base_hash(self, path: str) -> str:
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if temporary_root == self.working_copy or self.working_copy in temporary_root.parents:
            raise FileOperationError("BASE verification requires temporary storage outside this working copy.")
        with tempfile.TemporaryDirectory(prefix="svn-fileops-base-", dir=temporary_root) as temporary:
            destination = Path(temporary) / "base-file"
            # Export local BASE, translating EOLs and keywords just as SVN does.
            # Never write in the user's WC and never use --force.
            self.client.command(["export", "--quiet", "--ignore-externals", "--revision", "BASE", "--",
                                 self._target(path), str(destination)])
            return _snapshot_bytes(destination)[0]

    def _source(self, path: str, root: dict[str, object], operation: str) -> FileSnapshot:
        info = self.client.info(path)
        self._check_identity(path, info, root, "file")
        status = self._status(path)
        assert status is not None
        self._check_status(status, clean=operation == "delete")
        digest, size, fingerprint = _snapshot_bytes(self.working_copy / path)
        properties = self._properties_hash(path)
        if operation == "delete" and digest != self._base_hash(path):
            raise FileOperationError("File bytes differ from translated BASE; deletion would discard local changes.")
        return FileSnapshot(path, info, status, digest, size, properties, fingerprint)

    def _destination_absent(self, destination: str) -> None:
        absolute = self.working_copy / destination
        if os.path.lexists(absolute):
            raise FileOperationError("The exact destination must not exist; no overwrite or move-into-directory fallback is allowed.")
        if not absolute.parent.is_dir():
            raise FileOperationError("The destination parent must already exist; parents are never created implicitly.")
        for sibling in absolute.parent.iterdir():
            if sibling.name.casefold() == absolute.name.casefold():
                raise FileOperationError("Case-only renames and case-insensitive destination collisions are not supported.")
        if self._status(destination, absent_ok=True) is not None:
            raise FileOperationError("The destination already has SVN metadata or a pending action.")

    def _preflight(self, operation: str, paths: Sequence[str | os.PathLike[str]],
                   destination: str | os.PathLike[str] | None = None) -> FileOperationPlan:
        with self.client._lock:
            selected = self._paths(paths)
            target = self._path(destination) if destination is not None else None
            if operation != "delete" and (len(selected) != 1 or target is None):
                raise FileOperationError("Copy and move require one source file and one exact new destination.")
            root = self._root_info()
            parent_paths = {"."}
            for path in [*selected, *([target] if target is not None else [])]:
                parent_paths.update(str(parent) for parent in Path(path).parents)
            ancestors = tuple(self._ancestor(path, root) for path in sorted(parent_paths))
            files = tuple(self._source(path, root, operation) for path in selected)
            if target is not None:
                if selected[0].casefold() == target.casefold():
                    raise FileOperationError("Source and destination must be different, including letter case.")
                self._destination_absent(target)
            # A preflight itself may take time; verify sources/metadata once more.
            for snapshot in files:
                self._path(snapshot.path)
                if (_snapshot_bytes(self.working_copy / snapshot.path) !=
                        (snapshot.content_sha256, snapshot.size_bytes, snapshot.fingerprint)
                        or self._status(snapshot.path) != snapshot.status
                        or self._properties_hash(snapshot.path) != snapshot.properties_sha256):
                    raise FileOperationError("File state changed during preflight. Inspect it again before continuing.")
            argv = self._argv(operation, selected, target)
            if self.client._command_size(argv) > 24_000:
                raise FileOperationError("The exact selection is too long for one command. Select fewer or shorter paths.")
            state = {"operation": operation, "workspace": str(self.working_copy), "paths": selected,
                     "destination": target, "files": [asdict(item) for item in files],
                     "ancestors": [asdict(item) for item in ancestors]}
            digest = hashlib.sha256(json.dumps(state, ensure_ascii=True, sort_keys=True,
                                               separators=(",", ":")).encode("ascii")).hexdigest()
            warnings = ["Only the exact listed regular files are changed. No repository commit is performed.",
                        "The preflight is a snapshot, not a reservation. Do not edit or run other SVN tools concurrently.",
                        "An interrupted or failed SVN command may have partly completed; inspect status before retrying."]
            if operation == "delete":
                warnings.append("Deletion removes the local file and schedules repository deletion for a later explicit commit. Keep an independent backup.")
            elif operation == "move":
                warnings.append("The source path is removed and the destination is added with history. Review and commit both sides together; changelist labels are not guaranteed to transfer.")
            else:
                warnings.append("The destination is added with copy history using working-copy content/properties. Changelist labels are not guaranteed to transfer.")
            return FileOperationPlan(operation, str(self.working_copy), tuple(selected), target,
                                     tuple([*selected, *([target] if target is not None else [])]),
                                     files, ancestors, digest, tuple(warnings))

    def preflight_delete(self, paths: Sequence[str | os.PathLike[str]]) -> FileOperationPlan:
        return self._preflight("delete", paths)

    def preflight_move(self, source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> FileOperationPlan:
        return self._preflight("move", [source], destination)

    def preflight_copy(self, source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> FileOperationPlan:
        return self._preflight("copy", [source], destination)

    def _argv(self, operation: str, paths: Sequence[str], destination: str | None) -> list[str]:
        if operation == "delete":
            return ["delete", "--", *(self._target(path) for path in paths)]
        assert destination is not None
        # SVN 1.14 copy parses an empty peg on DST, while move treats DST
        # literally (move-cmd.c removes SRC pegs only). Do not append '@' to a
        # move destination, or the actual new filename would gain that suffix.
        target = self._target(destination) if operation == "copy" else destination
        return [operation, "--", self._target(paths[0]), target]

    def _verify_result(self, plan: FileOperationPlan) -> None:
        """Check exact local postconditions; never silently roll back or retry."""
        if plan.operation in {"delete", "move"}:
            for path in plan.paths:
                status = self._status(path)
                if status is None or status.item != "deleted" or os.path.lexists(self.working_copy / path):
                    raise SvnError("SVN returned success but the exact source removal could not be verified. Inspect status; do not automatically retry.")
        if plan.destination is not None:
            status = self._status(plan.destination)
            self._path(plan.destination)
            if status is None or status.item != "added" or not status.copied:
                raise SvnError("SVN returned success but the exact history-preserving destination could not be verified. Inspect status; do not automatically retry.")
            source = plan.files[0]
            digest, size, _ = _snapshot_bytes(self.working_copy / plan.destination)
            if (digest != source.content_sha256 or size != source.size_bytes
                    or self._properties_hash(plan.destination) != source.properties_sha256):
                raise SvnError("SVN completed but destination content/properties differ from the reviewed source. Inspect both paths before any further action.")

    def _execute(self, operation: str, paths: Sequence[str | os.PathLike[str]], destination=None,
                 *, confirmed: bool = False, expected_state_sha256: str | None = None) -> CommandResult:
        if confirmed is not True:
            raise FileOperationError("File changes require explicit confirmation of the exact operation and paths.")
        if expected_state_sha256 is not None and (not isinstance(expected_state_sha256, str) or
                not re.fullmatch(r"[0-9a-f]{64}", expected_state_sha256)):
            raise FileOperationError("The expected file state must be a lowercase SHA-256 value.")
        with self.client._lock:
            plan = self._preflight(operation, paths, destination)
            if expected_state_sha256 is not None and plan.state_sha256 != expected_state_sha256:
                raise FileOperationError("File state changed after review. Review and confirm the new state before continuing.")
            # Recheck the namespace immediately before launching the trusted CLI.
            for path in plan.paths:
                self._path(path)
            if plan.destination is not None:
                self._path(plan.destination)
                self._destination_absent(plan.destination)
            result = self.client.command(self._argv(operation, plan.paths, plan.destination))
            try:
                self._verify_result(plan)
            except SvnError:
                raise
            except Exception as exc:
                # Path/status/IO validation errors here happen AFTER the SVN
                # mutation. Never mislabel them as a harmless preflight refusal.
                raise SvnError("SVN returned success, but local postcondition checks failed. "
                               "The outcome is uncertain; inspect both paths before retrying. " + str(exc)) from exc
            return result

    def delete(self, paths: Sequence[str | os.PathLike[str]], *, confirmed: bool = False,
               expected_state_sha256: str | None = None) -> CommandResult:
        return self._execute("delete", paths, confirmed=confirmed, expected_state_sha256=expected_state_sha256)

    def move(self, source: str | os.PathLike[str], destination: str | os.PathLike[str], *, confirmed: bool = False,
             expected_state_sha256: str | None = None) -> CommandResult:
        return self._execute("move", [source], destination, confirmed=confirmed, expected_state_sha256=expected_state_sha256)

    def copy(self, source: str | os.PathLike[str], destination: str | os.PathLike[str], *, confirmed: bool = False,
             expected_state_sha256: str | None = None) -> CommandResult:
        return self._execute("copy", [source], destination, confirmed=confirmed, expected_state_sha256=expected_state_sha256)
