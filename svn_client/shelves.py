"""Verified local SVN backups, deliberately separate from destructive revert.

A shelf saves raw regular-file bytes, complete binary-safe SVN properties, directory
entries and executable modes. It never clears the working copy. Restore is strict:
all affected versioned paths must be clean at the original BASE and new paths must
be absent. Copies, moves, replacements, missing/conflicted/switched/external nodes,
symlinks/reparse points, and deleted directories are rejected explicitly.

This is not SVN's experimental shelve format. No network, checkout, commit, or merge
is performed. Do not edit/update the working copy concurrently with a restore.
"""
from __future__ import annotations

import base64
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
import xml.etree.ElementTree as ET
import zlib

from .file_identity import identity_time_ns


class ShelfError(RuntimeError):
    """An unsupported state, invalid shelf, or failed SVN operation."""


class ShelfConflict(ShelfError):
    """A restore was refused without changing the working copy."""


_FORMAT = 2
_CHUNK_SIZE = 1024 * 1024
_MIN_COMPRESSION_SAVING = 256
# Format 2 limits bound even a malicious manifest's decompression request.
# Legacy raw shelves do not need an expansion limit and remain readable.
_MAX_FILE_BYTES = 16 * 1024 ** 3
_MAX_SHELF_BYTES = 64 * 1024 ** 3
# Keep target vectors comfortably below Windows command-line limits as well as
# common POSIX limits. These are batching bounds, not a total selection cap.
_SVN_BATCH_TARGETS = 128
_SVN_BATCH_BYTES = 12000
_ID = re.compile(r"^[0-9a-f]{32}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?$", re.I)
_UNSAFE_STATUS = {"conflicted", "obstructed", "incomplete", "missing", "replaced", "external"}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_signature(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, identity_time_ns(st))


@contextlib.contextmanager
def _open_regular(path):
    """Read one stable regular file, refusing final-component link swaps."""
    before = path.lstat()
    if _special(before) or not stat.S_ISREG(before.st_mode):
        raise ShelfError(f"Unsafe or non-regular file: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if _special(opened) or not stat.S_ISREG(opened.st_mode) or _file_signature(before) != _file_signature(opened):
            raise ShelfError(f"File changed while being read; retry: {path.name}")
        yield stream
        after = os.fstat(stream.fileno())
        if (opened.st_ctime_ns != after.st_ctime_ns
                or _file_signature(opened) != _file_signature(after)
                or _file_signature(opened) != _file_signature(path.lstat())):
            raise ShelfError(f"File changed while being read; retry: {path.name}")


def _file_digest(path):
    checksum = hashlib.sha256()
    with _open_regular(path) as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            checksum.update(chunk)
    return checksum.hexdigest()


def _encoded(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _decoded(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ShelfError("Shelf contains an invalid binary property value") from exc


def _safe_relative(value: str) -> str:
    """Use a deliberately portable, traversal-proof on-disk manifest syntax."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ShelfError("Shelf path is empty or invalid")
    if value == ".":
        return value
    if "\\" in value or ":" in value or value.startswith("/"):
        raise ShelfError(f"Unsafe shelf path: {value!r}")
    parts = value.split("/")
    if any(p in ("", ".", "..") or p.lower() == ".svn" or p.endswith((".", " "))
           or _RESERVED.match(p) or any(ord(c) < 32 or c in '<>\"|?*' for c in p)
           for p in parts):
        raise ShelfError(f"Unsafe or non-portable shelf path: {value!r}")
    if PurePosixPath(value).is_absolute():
        raise ShelfError(f"Absolute shelf path: {value!r}")
    return value


def _special(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _sync_directory(path: Path):
    # Windows does not expose portable directory fsync. File contents are always
    # fsynced; POSIX additionally makes new directory entries durable.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


class ShelfManager:
    """Create/list/restore/delete verified local backups for one working copy.

    ``workspace_path`` can be a path or a backend SvnClient. Returned dictionaries
    are JSON-serializable. Creation has no working-copy side effects. Restore
    always retains the shelf, including after success or an SVN command failure.
    """

    def __init__(self, workspace_path, storage_dir=None):
        client = workspace_path if hasattr(workspace_path, "working_copy") else None
        source = client.working_copy if client is not None else workspace_path
        self.working_copy = Path(source).expanduser().resolve(strict=True)
        if not self.working_copy.is_dir():
            raise ShelfError("Working copy must be a directory")
        self.executable = getattr(client, "executable", "svn")
        self._executable_checked = client is not None
        self.timeout = getattr(client, "timeout", 120)
        self._client_lock = getattr(client, "_lock", threading.RLock())
        if storage_dir is None:
            if os.name == "nt":
                app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            else:
                app_data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
            key = _digest(os.fsencode(str(self.working_copy)))[:24]
            storage_dir = app_data / "svn-workbench" / "shelves" / key
        self.storage_dir = Path(storage_dir).expanduser().absolute()
        if _is_within(self.storage_dir.resolve(), self.working_copy):
            raise ShelfError("Shelf storage must be outside the working copy")

    def _svn(self, *args: str) -> bytes:
        if not self._executable_checked:
            from .backend import discover_svn, SvnError
            try:
                self.executable = discover_svn(self.executable)
            except SvnError as exc:
                raise ShelfError(str(exc)) from exc
            self._executable_checked = True
        environment = os.environ.copy()
        if os.name != "nt":
            environment.setdefault("LC_CTYPE", "C.UTF-8")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            result = subprocess.run(
                [str(self.executable), "--non-interactive", "--no-auth-cache", *args],
                cwd=self.working_copy, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self.timeout, check=False, shell=False, env=environment, **options,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ShelfError(f"Unable to finish SVN operation; it may have partially completed: {exc}") from exc
        if result.returncode:
            reason = result.stderr.decode("utf-8", errors="replace").strip()
            raise ShelfError(f"SVN operation failed: {reason or result.returncode}")
        return result.stdout

    def _xml(self, *args: str) -> ET.Element:
        try:
            return ET.fromstring(self._svn(*args))
        except ET.ParseError as exc:
            raise ShelfError("SVN returned invalid XML") from exc

    def _path(self, relative: str) -> Path:
        relative = _safe_relative(relative)
        path = self.working_copy if relative == "." else self.working_copy.joinpath(*relative.split("/"))
        current = self.working_copy
        for part in (() if relative == "." else relative.split("/")):
            current /= part
            try:
                st = current.lstat()
            except FileNotFoundError:
                continue
            if _special(st):
                raise ShelfError(f"Symlink or reparse-point paths are unsupported: {relative}")
            if stat.S_ISDIR(st.st_mode) and (current / ".svn").exists():
                raise ShelfError(f"Nested working copy is unsupported: {relative}")
            if current != path and not stat.S_ISDIR(st.st_mode):
                raise ShelfError(f"Path ancestor is not a directory: {relative}")
        if not _is_within(path.resolve(strict=False), self.working_copy):
            raise ShelfError(f"Path escapes working copy: {relative}")
        return path

    def _relative(self, supplied) -> str:
        raw = os.fspath(supplied)
        if os.path.isabs(raw):
            try:
                raw = str(Path(raw).relative_to(self.working_copy))
            except ValueError as exc:
                raise ShelfError("Selected path is outside the working copy") from exc
        if os.name == "nt":
            raw = raw.replace("\\", "/")
        return _safe_relative(raw)

    @staticmethod
    def _target(path: str) -> str:
        # Trailing @ disables SVN's peg revision interpretation, including names
        # containing @. The preceding -- separately prevents option injection.
        return path + "@"

    def _info(self, path=".") -> dict:
        root = self._xml("info", "--xml", "--depth", "empty", "--", self._target(path))
        entry = root.find("entry")
        if entry is None:
            raise ShelfError(f"No SVN metadata for {path}")
        return self._parse_info(entry, path)

    @staticmethod
    def _parse_info(entry, path):
        info = {
            "kind": entry.get("kind"), "revision": entry.get("revision"),
            "url": entry.findtext("url"), "uuid": entry.findtext("repository/uuid"),
            "schedule": entry.findtext("wc-info/schedule", "normal"),
            "depth": entry.findtext("wc-info/depth", "infinity"),
        }
        if entry.find("wc-info/copy-from-url") is not None:
            raise ShelfError(f"Copied or moved paths are unsupported: {path}")
        if info["kind"] not in ("file", "dir"):
            raise ShelfError(f"Unsupported SVN node kind: {path}")
        return info

    def _target_batches(self, paths):
        batch, cost, seen = [], 0, set()
        for supplied in paths:
            path = self._relative(supplied)
            self._path(path)  # Reject redirected/nested targets before any SVN read.
            if path in seen:
                raise ShelfError(f"Duplicate SVN metadata target: {path}")
            seen.add(path)
            # UTF-8 bytes bound POSIX argv; they also conservatively cover UTF-16
            # code units and quoting (portable paths cannot contain quotes).
            path_cost = len(self._target(path).encode("utf-8")) + 3
            if path_cost > _SVN_BATCH_BYTES:
                raise ShelfError(f"SVN metadata target exceeds safe command-line length: {path}")
            if batch and (len(batch) >= _SVN_BATCH_TARGETS or cost + path_cost > _SVN_BATCH_BYTES):
                yield batch
                batch, cost = [], 0
            batch.append(path)
            cost += path_cost
        if batch:
            yield batch

    def _infos(self, paths):
        """Read exact local targets, matching XML by path rather than order."""
        result = {}
        for batch in self._target_batches(paths):
            root = self._xml("info", "--xml", "--depth", "empty", "--",
                             *(self._target(path) for path in batch))
            if root.tag != "info" or any(child.tag != "entry" for child in root):
                raise ShelfError("SVN returned an invalid info batch")
            expected, observed = set(batch), set()
            for entry in root:
                path = self._relative(entry.get("path", ""))
                if path not in expected or path in observed:
                    raise ShelfError(f"Unexpected or duplicate SVN info target: {path}")
                observed.add(path)
                result[path] = self._parse_info(entry, path)
            if observed != expected:
                raise ShelfError("SVN info batch omitted required targets")
        return result

    @staticmethod
    def _parse_properties(target, path):
        properties = {}
        for node in target:
            if node.tag != "property":
                raise ShelfError(f"Unexpected SVN property element on {path}")
            name, value = node.get("name"), node.text or ""
            if not name or "\x00" in name or name in properties or len(node):
                raise ShelfError(f"Invalid SVN property on {path}")
            if node.get("encoding") == "base64":
                data = _decoded("".join(value.split()))
            elif node.get("encoding") is None:
                data = value.encode("utf-8")
            else:
                raise ShelfError(f"Unknown SVN property encoding on {path}")
            properties[name] = _encoded(data)
        if "svn:special" in properties:
            raise ShelfError(f"SVN special files (including symlinks) are unsupported: {path}")
        return properties

    def _properties_many(self, paths, base=False):
        """Empty properties are omitted by SVN, but failed batches are never used.

        _xml calls _svn, which rejects nonzero exit codes before parsing even a
        well-formed partial XML result. Do not add a partial-result fallback here.
        """
        result = {}
        for batch in self._target_batches(paths):
            args = ["proplist", "--xml", "--verbose", "--depth", "empty"]
            if base:
                args.extend(["-r", "BASE"])
            root = self._xml(*args, "--", *(self._target(path) for path in batch))
            if root.tag != "properties" or any(child.tag != "target" for child in root):
                raise ShelfError("SVN returned an invalid property batch")
            expected, observed = set(batch), set()
            result.update((path, {}) for path in batch)
            for target in root:
                path = self._relative(target.get("path", ""))
                if path not in expected or path in observed:
                    raise ShelfError(f"Unexpected or duplicate SVN property target: {path}")
                observed.add(path)
                result[path] = self._parse_properties(target, path)
        return result

    def _properties(self, path: str, base=False) -> dict[str, str]:
        path = self._relative(path)
        return self._properties_many([path], base=base)[path]

    def _status(self, paths=None, *, depth=None, include_paths=None) -> list[dict]:
        targets = [self._target(self._relative(p)) for p in (paths or ["."])]
        options = ["--depth", depth] if depth is not None else []
        root = self._xml("status", "--xml", "--verbose", "--no-ignore", "--ignore-externals", *options, "--", *targets)
        entries = {}
        for node in root.findall(".//entry"):
            try:
                path = self._relative(node.get("path", ""))
            except ShelfError:
                if include_paths is not None:
                    continue  # An unrelated sibling is not a shelf target.
                raise
            if include_paths is not None and path not in include_paths:
                continue
            status = node.find("wc-status")
            if status is None:
                continue
            item = status.get("item", "none")
            props = status.get("props", "none")
            flags = {key: status.get(key) for key in ("copied", "switched", "tree-conflicted", "moved-from", "moved-to", "file-external", "wc-locked")}
            entry = {"path": path, "item": item, "props": props, "revision": status.get("revision"), **flags}
            previous = entries.get(path)
            if previous is not None:
                if any(previous[key] != entry[key] for key in ("item", "props", "revision")):
                    raise ShelfError(f"SVN status changed between overlapping targets: {path}")
                # SVN omits switched on an explicit status target, but reports
                # it when visiting that node from its parent. Never erase that
                # evidence when overlapping target outputs repeat the node.
                for key in flags:
                    if previous.get(key) not in (None, "false", ""):
                        entry[key] = previous[key]
            entries[path] = entry
        return list(entries.values())

    @staticmethod
    def _check_status(entry: dict):
        if entry["item"] in _UNSAFE_STATUS or entry["props"] == "conflicted":
            raise ShelfError(f"Unsupported working-copy state {entry['item']}/{entry['props']}: {entry['path']}")
        if any(entry.get(key) not in (None, "false", "") for key in
               ("copied", "switched", "tree-conflicted", "moved-from", "moved-to", "file-external", "wc-locked")):
            raise ShelfError(f"Copied, moved, switched or conflicted path is unsupported: {entry['path']}")

    @staticmethod
    def _ancestor_paths(paths):
        return {str(parent) for path in paths for parent in PurePosixPath(path).parents}

    def _create_context_status(self, selected, status_entries):
        # Explicit status targets can omit their own switched flag. Observe
        # missing ancestors and selection boundaries from their parents, not
        # with depth=empty on the nodes themselves. Only relevant rows survive;
        # immediate siblings never expand the selection or block its backup.
        required = self._ancestor_paths(selected) | selected.keys()
        known = {entry["path"]: entry for entry in status_entries}
        for path, entry in selected.items():
            known.setdefault(path, entry)  # Recursively expanded unversioned dirs.
        parents = {str(PurePosixPath(path).parent) for path in required
                   if path not in known or str(PurePosixPath(path).parent) not in known}
        for batch in self._target_batches(sorted(parents)):
            for entry in self._status(batch, depth="immediates", include_paths=required):
                previous = known.get(entry["path"])
                if previous is not None:
                    for key in ("copied", "switched", "tree-conflicted", "moved-from", "moved-to", "file-external", "wc-locked"):
                        if previous.get(key) not in (None, "false", ""):
                            entry[key] = previous[key]
                known[entry["path"]] = entry
        return {path: known.get(path) for path in required}

    def _check_ancestors(self, paths, by_path, planned_dirs):
        for parent in sorted(self._ancestor_paths(paths)):
            entry = by_path.get(parent)
            if entry is None:
                if parent not in planned_dirs:
                    raise ShelfError(f"Parent directory is not versioned or included in the shelf: {parent}")
                continue
            self._check_status(entry)
            if entry["item"] == "deleted":
                raise ShelfError(f"Deleted parent directory is unsupported: {parent}")
            if entry["item"] in ("added", "unversioned", "ignored") and parent not in planned_dirs:
                raise ShelfError(f"Select the added/unversioned parent directory as well: {parent}")

    def _walk_unversioned(self, relative: str) -> list[dict]:
        result = []
        def visit(rel):
            path = self._path(rel)
            st = path.lstat()
            if _special(st):
                raise ShelfError(f"Symlink or reparse point is unsupported: {rel}")
            if stat.S_ISDIR(st.st_mode):
                if (path / ".svn").exists():
                    raise ShelfError(f"Nested working copy is unsupported: {rel}")
                result.append({"path": rel, "item": "unversioned", "props": "none"})
                for child in sorted(path.iterdir()):
                    visit(rel + "/" + child.name)
            elif stat.S_ISREG(st.st_mode):
                result.append({"path": rel, "item": "unversioned", "props": "none"})
            else:
                raise ShelfError(f"Only regular files and directories are supported: {rel}")
        visit(relative)
        return result

    def _check_storage(self):
        """Validate existing storage paths without creating or modifying anything."""
        for ancestor in (self.storage_dir, *self.storage_dir.parents):
            try:
                existing = ancestor.lstat()
            except FileNotFoundError:
                continue
            if _special(existing):
                raise ShelfError("Shelf storage cannot contain symlink/reparse-point ancestors")
            if not stat.S_ISDIR(existing.st_mode):
                raise ShelfError("Shelf storage or an ancestor is not a directory")

    def _ensure_storage(self):
        self._check_storage()
        self.storage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Refuse symlink/junction ancestors, rather than trusting manifest paths
        # in a directory redirected by a different process.
        current = self.storage_dir
        while current != current.parent:
            if _special(current.lstat()):
                raise ShelfError("Shelf storage cannot contain symlink/reparse-point ancestors")
            current = current.parent

    @contextlib.contextmanager
    def _locked(self):
        with self._client_lock:
            with self._storage_lock():
                yield

    @contextlib.contextmanager
    def _storage_lock(self):
        self._ensure_storage()
        lock = self.storage_dir / ".operation.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ShelfError("Another shelf operation is running. If it crashed, remove the .operation.lock file from shelf storage after checking no operation is active.") from exc
        try:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(str(os.getpid()))
            yield
        finally:
            lock.unlink(missing_ok=True)

    def _exported_base_hash(self, path: str) -> str:
        # Status alone is insufficient: SVN may use cached size/mtime and miss a
        # same-size edit with a restored timestamp. Export BASE uses the pristine
        # bytes with SVN's native EOL and keyword translation, unlike a raw
        # repository-content comparison that would reject valid CRLF/keywords.
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if _is_within(temporary_root, self.working_copy) or _is_within(temporary_root, self.storage_dir.resolve()):
            raise ShelfError("System temporary storage must be outside the working copy and shelf storage")
        with tempfile.TemporaryDirectory(prefix="svn-shelf-base-", dir=temporary_root) as temporary:
            destination = Path(temporary) / "base-file"
            self._svn("export", "--quiet", "--ignore-externals", "-r", "BASE", "--",
                      self._target(path), str(destination))
            if not destination.is_file() or _special(destination.lstat()):
                raise ShelfError(f"Cannot verify translated BASE content: {path}")
            return _file_digest(destination)

    def _base(self, path: str, info=None, properties=None) -> dict:
        info = info or self._info(path)
        props = self._properties(path, base=True) if properties is None else properties
        base = {"revision": info["revision"], "kind": info["kind"],
                "url": info["url"], "uuid": info["uuid"], "properties": props}
        if info["kind"] == "file":
            base["sha256"] = _digest(self._svn("cat", "-r", "BASE", "--", self._target(path)))
            base["working_sha256"] = self._exported_base_hash(path)
        return base

    @staticmethod
    def _summary(manifest):
        paths = [entry["path"] for entry in manifest["entries"]]
        return {"id": manifest["id"], "name": manifest["name"],
                "created_at": manifest["created_at"], "paths": paths, "files": paths,
                "entry_count": len(paths), "backup_only": True,
                "repository_url": manifest["repository"]["url"]}

    def _select_create(self, paths):
        if isinstance(paths, (str, bytes)) or (paths is not None and not paths):
            raise ShelfError("Select at least one path using a sequence of paths")
        repository = self._info()
        status_entries = self._status(paths)
        selected = {}
        for status_entry in status_entries:
            self._check_status(status_entry)
            if status_entry["item"] == "unversioned":
                for expanded in self._walk_unversioned(status_entry["path"]):
                    selected[expanded["path"]] = expanded
            elif status_entry["item"] in ("modified", "added", "deleted") or status_entry["props"] == "modified":
                selected[status_entry["path"]] = status_entry
            elif status_entry["item"] not in ("normal", "none", "ignored"):
                raise ShelfError(f"Unsupported status: {status_entry['item']}")
        if not selected:
            raise ShelfError("No supported changes selected for backup")
        context = self._create_context_status(selected, status_entries)
        for path in selected:
            self._check_status(context[path])
        self._check_ancestors(selected, context, selected)
        return repository, status_entries, selected, context

    @staticmethod
    def _capture_file(path, blobs=None):
        """Stream a stable source; keep compression only when it saves space.

        Raw and compressed candidates are private to the unpublished shelf. The
        raw candidate is also the fallback, so no second, potentially different
        read of the working file is used to choose its storage representation.
        """
        checksum, size = hashlib.sha256(), 0
        with contextlib.ExitStack() as stack:
            source = stack.enter_context(_open_regular(path))
            if os.fstat(source.fileno()).st_size > _MAX_FILE_BYTES:
                raise ShelfError(f"File exceeds the 16 GiB shelf limit: {path.name}")
            if blobs is not None:
                candidates = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=".packing-", dir=blobs)))
                raw_path, packed_path = candidates / "raw", candidates / "zlib"
                raw = stack.enter_context(open(raw_path, "xb"))
                packed = stack.enter_context(open(packed_path, "xb"))
                compressor = zlib.compressobj(level=6)
            while chunk := source.read(_CHUNK_SIZE):
                size += len(chunk)
                if size > _MAX_FILE_BYTES:
                    raise ShelfError(f"File exceeds the 16 GiB shelf limit: {path.name}")
                checksum.update(chunk)
                if blobs is not None:
                    raw.write(chunk)
                    packed.write(compressor.compress(chunk))
            result = {"blob": checksum.hexdigest(), "size": size,
                      "blob_encoding": "raw", "stored_size": size}
            if blobs is not None:
                packed.write(compressor.flush())
                packed_size = packed.tell()
                use_packed = packed_size <= size * 95 // 100 and size - packed_size >= _MIN_COMPRESSION_SAVING
                chosen = packed if use_packed else raw
                chosen.flush()
                os.fsync(chosen.fileno())
                raw.close()
                packed.close()
                if use_packed:
                    result.update(blob_encoding="zlib", stored_size=packed_size)
                destination = blobs / result["blob"]
                if destination.exists():
                    # Duplicate content within this shelf shares one immutable
                    # blob. Verify rather than overwriting a pre-existing blob.
                    ShelfManager._copy_verified_blob(result, blobs.parent, _FORMAT)
                else:
                    os.rename(packed_path if use_packed else raw_path, destination)
        return result

    @staticmethod
    def _copy_verified_blob(entry, folder, shelf_format, output=None):
        """Verify/decode in bounded chunks, optionally into a private temp file.

        No output beyond the declared, capped size is written. A single complete
        zlib stream is required: truncation, trailing bytes and concatenated
        streams are errors. Never call an unbounded decompressor flush().
        """
        blob_dir = folder / "blobs"
        if _special(blob_dir.lstat()) or not stat.S_ISDIR(blob_dir.lstat().st_mode):
            raise ShelfError("Unsafe shelf blob directory")
        encoding = entry["blob_encoding"] if shelf_format == 2 else "raw"
        expected = entry["size"]
        stored_size = entry["stored_size"] if shelf_format == 2 else expected
        checksum, size = hashlib.sha256(), 0

        def consume(chunk):
            nonlocal size
            size += len(chunk)
            if size > expected:
                raise ShelfError("Shelf data is damaged: expanded size exceeds its declared limit")
            checksum.update(chunk)
            if output is not None:
                output.write(chunk)

        try:
            with _open_regular(blob_dir / entry["blob"]) as source:
                if os.fstat(source.fileno()).st_size != stored_size:
                    raise ShelfError("Shelf data is damaged: stored size mismatch")
                decoder = zlib.decompressobj() if encoding == "zlib" else None
                while chunk := source.read(_CHUNK_SIZE):
                    if decoder is None:
                        consume(chunk)
                        continue
                    pending = chunk
                    while True:
                        limit = min(_CHUNK_SIZE, expected - size + 1)
                        decoded = decoder.decompress(pending, limit)
                        consume(decoded)
                        if decoder.unused_data:
                            raise ShelfError("Shelf data is damaged: trailing compressed data")
                        pending = decoder.unconsumed_tail
                        if not pending and len(decoded) < limit:
                            break
                if decoder is not None and not decoder.eof:
                    raise ShelfError("Shelf data is damaged: truncated compressed stream")
        except zlib.error as exc:
            raise ShelfError("Shelf data is damaged: invalid compressed stream") from exc
        if size != expected or checksum.hexdigest() != entry["blob"]:
            raise ShelfError("Shelf data is damaged: checksum or decoded size mismatch")

    def _capture_create_entries(self, selected, blobs=None):
        entries = []
        total_size = 0
        ordered = sorted(selected, key=lambda path: (path.count("/"), path))
        versioned = [path for path in ordered if selected[path]["item"] != "unversioned"]
        live = [path for path in versioned if selected[path]["item"] != "deleted"]
        based = [path for path in versioned if selected[path]["item"] != "added"]
        # These are operation-local snapshots, not persistent caches. Reads are
        # depth=empty on explicit targets only; never broaden to an entire tree.
        infos = self._infos(versioned)
        current_properties = self._properties_many(live)
        base_properties = self._properties_many(based, base=True)
        for relative, status_entry in sorted(selected.items(), key=lambda item: (item[0].count("/"), item[0])):
            path = self._path(relative)
            item = status_entry["item"]
            info = infos.get(relative)
            if info and info["kind"] == "dir" and info["depth"] != "infinity":
                raise ShelfError(f"Sparse directories are unsupported: {relative}")
            kind = info["kind"] if info else ("dir" if path.is_dir() else "file")
            if item == "deleted" and path.exists():
                raise ShelfError(f"Deleted path still contains local data; back it up separately first: {relative}")
            if item == "deleted" and kind == "dir":
                raise ShelfError(f"Deleted directories are not yet supported; back up before deleting: {relative}")
            if item != "deleted" and kind == "file":
                size = path.lstat().st_size
                if size > _MAX_FILE_BYTES or total_size + size > _MAX_SHELF_BYTES:
                    raise ShelfError(f"File exceeds the shelf content size limits: {relative}")
            entry = {"path": relative, "state": item, "kind": kind,
                     "properties": {} if info is None or item == "deleted" else current_properties[relative]}
            if item not in ("added", "unversioned"):
                entry["base"] = self._base(relative, info, properties=base_properties[relative])
            if item != "deleted":
                st = path.lstat()
                if _special(st) or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                    raise ShelfError(f"Unsupported filesystem object: {relative}")
                if kind == "dir" and relative != "." and (path / ".svn").exists():
                    raise ShelfError(f"Nested working copy is unsupported: {relative}")
                entry["mode"] = stat.S_IMODE(st.st_mode) & 0o777
                if kind == "file":
                    if total_size + st.st_size > _MAX_SHELF_BYTES:
                        raise ShelfError("Shelf exceeds the 64 GiB total file-content limit")
                    entry.update(self._capture_file(path, blobs))
                    total_size += entry["size"]
                    if total_size > _MAX_SHELF_BYTES:
                        raise ShelfError("Shelf exceeds the 64 GiB total file-content limit")
            entries.append(entry)
        return entries

    def _verify_create_snapshot(self, paths, status_entries, selected, entries, context):
        # Status changes during the snapshot invalidate it. Byte checks above
        # additionally detect in-place edits that SVN status cannot distinguish.
        latest_status = self._status(paths)
        if latest_status != status_entries:
            raise ShelfError("Working-copy status changed while backing up; retry")
        if self._create_context_status(selected, latest_status) != context:
            raise ShelfError("Working-copy ancestor status changed while backing up; retry")
        for entry in entries:
            relative, item = entry["path"], entry["state"]
            source = self._path(relative)
            if item == "deleted":
                if source.exists():
                    raise ShelfError(f"Deleted path appeared during backup: {relative}")
                continue
            if entry["kind"] == "file" and _file_digest(source) != entry["blob"]:
                raise ShelfError(f"File changed while being backed up; retry: {relative}")
            if stat.S_IMODE(source.stat().st_mode) & 0o777 != entry["mode"]:
                raise ShelfError(f"Permissions changed during backup; retry: {relative}")
        # Always make a new read after source hashing. Never reuse the capture
        # snapshot, even if status still reports the same "modified" flag.
        latest_properties = self._properties_many(
            [entry["path"] for entry in entries if entry["state"] not in ("unversioned", "deleted")])
        for entry in entries:
            relative, item = entry["path"], entry["state"]
            if item not in ("unversioned", "deleted") and latest_properties[relative] != entry["properties"]:
                raise ShelfError(f"Properties changed during backup; retry: {relative}")
        for status_entry in status_entries:
            if status_entry["item"] == "unversioned":
                initial = {p for p, item in selected.items() if item["item"] == "unversioned" and (p == status_entry["path"] or p.startswith(status_entry["path"] + "/"))}
                final = {e["path"] for e in self._walk_unversioned(status_entry["path"])}
                if initial != final:
                    raise ShelfError("Unversioned directory changed during backup; retry")

    @staticmethod
    def _preflight_warnings():
        return [
            "Preflight is an advisory snapshot, not a reservation or authorization. Execution reruns all checks.",
            "Other processes and editors may change files, properties, BASE revisions, or shelf data after this check.",
            "Disk space, write permissions, and later OS/SVN failures are not guaranteed by a successful preflight.",
            "Translated BASE verification may use temporary files outside the working copy; these are cleaned up.",
        ]

    def preflight_create(self, paths=None) -> dict:
        """Inspect a backup plan without creating a shelf, storage directory, or lock.

        No working-copy data or SVN scheduling/properties is changed. Local SVN
        reads may use cache metadata internally. Pristine exports use cleaned-up
        system temporary files. The result is advisory; create() repeats checks.
        Shelf-name validation remains create()'s responsibility.
        """
        with self._client_lock:
            self._check_storage()
            if (self.storage_dir / ".operation.lock").exists():
                raise ShelfError("Another shelf operation is running; retry preflight when it finishes")
            repository, status_entries, selected, context = self._select_create(paths)
            entries = self._capture_create_entries(selected)
            self._verify_create_snapshot(paths, status_entries, selected, entries, context)
            # The normal manifest validator enforces portable path collisions and
            # property/base schemas even though no manifest or blobs are written.
            manifest = {"format": _FORMAT, "id": "0" * 32, "name": "Preflight",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "repository": {"uuid": repository["uuid"], "url": repository["url"]},
                        "entries": entries}
            self._validate_manifest(manifest, self.storage_dir, verify_blobs=False)
            planned_paths = [entry["path"] for entry in entries]
            return {"operation": "create", "dry_run": True, "can_create": True,
                    "backup_only": True, "paths": planned_paths, "files": planned_paths,
                    "entry_count": len(entries), "repository": manifest["repository"],
                    "repository_url": repository["url"], "entries": entries,
                    "warnings": self._preflight_warnings()}

    def create(self, name: str, paths=None) -> dict:
        """Back up selected changes (all changes if paths is None), without revert."""
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ShelfError("Shelf name must contain 1–200 characters")
        with self._locked():
            repository, status_entries, selected, context = self._select_create(paths)
            shelf_id = uuid.uuid4().hex
            temporary = Path(tempfile.mkdtemp(prefix=".creating-", dir=self.storage_dir))
            try:
                blobs = temporary / "blobs"
                blobs.mkdir(mode=0o700)
                entries = self._capture_create_entries(selected, blobs)
                self._verify_create_snapshot(paths, status_entries, selected, entries, context)
                manifest = {"format": _FORMAT, "id": shelf_id, "name": name.strip(),
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "repository": {"uuid": repository["uuid"], "url": repository["url"]},
                            "working_copy": str(self.working_copy), "entries": entries}
                with open(temporary / "manifest.json", "x", encoding="utf-8") as stream:
                    json.dump(manifest, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._validate_manifest(manifest, temporary, verify_blobs=True)
                _sync_directory(blobs)
                _sync_directory(temporary)
                os.replace(temporary, self.storage_dir / shelf_id)
                _sync_directory(self.storage_dir)
                return self._summary(manifest)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)

    def _shelf_path(self, shelf_id):
        if not isinstance(shelf_id, str) or not _ID.fullmatch(shelf_id):
            raise ShelfError("Invalid shelf ID")
        path = self.storage_dir / shelf_id
        if path.is_symlink() or (path.exists() and _special(path.lstat())):
            raise ShelfError("Shelf directory cannot be a symlink or reparse point")
        return path

    def _validate_manifest(self, manifest, folder, verify_blobs=False):
        if not isinstance(manifest, dict) or type(manifest.get("format")) is not int or manifest["format"] not in (1, 2):
            raise ShelfError("Unsupported shelf format")
        shelf_format = manifest["format"]
        if not isinstance(manifest.get("id"), str) or not _ID.fullmatch(manifest["id"]):
            raise ShelfError("Invalid manifest shelf ID")
        if not isinstance(manifest.get("name"), str) or not isinstance(manifest.get("created_at"), str):
            raise ShelfError("Invalid shelf metadata")
        repository = manifest.get("repository")
        if not isinstance(repository, dict) or not all(isinstance(repository.get(k), str) and repository[k] for k in ("uuid", "url")):
            raise ShelfError("Invalid shelf repository identity")
        entries = manifest.get("entries")
        if not isinstance(entries, list) or not entries or len(entries) > 100000:
            raise ShelfError("Invalid shelf entries")
        seen, blobs = set(), {}
        total_size = 0
        for entry in entries:
            if not isinstance(entry, dict):
                raise ShelfError("Invalid shelf entry")
            path = _safe_relative(entry.get("path"))
            if path.casefold() in seen:
                raise ShelfError(f"Duplicate or case-colliding shelf path: {path}")
            seen.add(path.casefold())
            state, kind = entry.get("state"), entry.get("kind")
            if state not in ("normal", "modified", "added", "deleted", "unversioned") or kind not in ("file", "dir"):
                raise ShelfError(f"Unsupported shelf entry: {path}")
            if state == "deleted" and kind == "dir":
                raise ShelfError("Deleted directory shelves are unsupported")
            if path == "." and (kind != "dir" or state not in ("normal", "modified")):
                raise ShelfError("Invalid working-copy root entry")
            prop_sets = [entry.get("properties")]
            if state not in ("added", "unversioned"):
                base = entry.get("base")
                if not isinstance(base, dict) or base.get("kind") != kind or not all(isinstance(base.get(k), str) and base[k] for k in ("url", "uuid", "revision")):
                    raise ShelfError(f"Missing BASE identity: {path}")
                if kind == "file" and (not _HASH.fullmatch(base.get("sha256", "")) or not _HASH.fullmatch(base.get("working_sha256", ""))):
                    raise ShelfError(f"Invalid BASE checksum: {path}")
                prop_sets.append(base.get("properties"))
            for props in prop_sets:
                if not isinstance(props, dict) or "svn:special" in props:
                    raise ShelfError(f"Invalid or unsupported properties: {path}")
                for key, value in props.items():
                    if not isinstance(key, str) or not key or key.startswith("-") or "\x00" in key or not isinstance(value, str):
                        raise ShelfError(f"Invalid property: {path}")
                    _decoded(value)
            if state != "deleted":
                if type(entry.get("mode")) is not int or not 0 <= entry["mode"] <= 0o777:
                    raise ShelfError(f"Invalid file mode: {path}")
                if kind == "file":
                    checksum = entry.get("blob", "")
                    if not isinstance(checksum, str) or not _HASH.fullmatch(checksum) or type(entry.get("size")) is not int or entry["size"] < 0:
                        raise ShelfError(f"Invalid blob metadata: {path}")
                    total_size += entry["size"]
                    if shelf_format == 2:
                        encoding, stored = entry.get("blob_encoding"), entry.get("stored_size")
                        if encoding not in ("raw", "zlib") or type(stored) is not int or not 0 <= stored <= entry["size"]:
                            raise ShelfError(f"Invalid encoded blob metadata: {path}")
                        if (encoding == "raw" and stored != entry["size"]) or (encoding == "zlib" and stored == 0):
                            raise ShelfError(f"Invalid encoded blob size: {path}")
                        if entry["size"] > _MAX_FILE_BYTES or total_size > _MAX_SHELF_BYTES:
                            raise ShelfError("Shelf exceeds the supported decompression size limits (16 GiB/file, 64 GiB/shelf)")
                    previous = blobs.get(checksum)
                    if previous is not None and any(entry.get(k) != previous.get(k) for k in
                                                    ("size", "blob_encoding", "stored_size")):
                        raise ShelfError(f"Inconsistent duplicate blob metadata: {path}")
                    blobs[checksum] = entry
        # Validate all metadata and total limits before reading/decompressing any
        # content. Duplicate file contents need verification only once per pass.
        if verify_blobs:
            for entry in blobs.values():
                try:
                    self._copy_verified_blob(entry, folder, shelf_format)
                except ShelfError as exc:
                    raise ShelfError(f"{exc}: {entry['path']}") from exc

    def _load(self, shelf_id, verify_blobs=False):
        folder = self._shelf_path(shelf_id)
        try:
            manifest_path = folder / "manifest.json"
            if _special(manifest_path.lstat()) or not stat.S_ISREG(manifest_path.lstat().st_mode):
                raise ShelfError("Unsafe shelf manifest")
            with open(manifest_path, encoding="utf-8") as stream:
                manifest = json.load(stream)
            self._validate_manifest(manifest, folder, verify_blobs)
            if manifest["id"] != shelf_id:
                raise ShelfError("Shelf ID does not match manifest")
            return manifest
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ShelfError(f"Cannot read shelf {shelf_id}: {exc}") from exc

    def list(self) -> list[dict]:
        self._check_storage()
        if not self.storage_dir.exists():
            return []
        results = []
        for path in self.storage_dir.iterdir():
            if _ID.fullmatch(path.name):
                try:
                    results.append(self._summary(self._load(path.name)))
                except ShelfError as exc:
                    results.append({"id": path.name, "name": "Unreadable shelf", "created_at": "",
                                    "paths": [], "files": [], "entry_count": 0, "error": str(exc)})
        return sorted(results, key=lambda shelf: shelf["created_at"], reverse=True)

    def details(self, shelf_id) -> dict:
        """Read and verify a shelf without creating storage directories or locks."""
        with self._client_lock:
            self._check_storage()
            manifest = self._load(shelf_id, verify_blobs=True)
            return {**self._summary(manifest), "entries": manifest["entries"]}

    def preflight_restore(self, shelf_id) -> dict:
        """Verify current restore eligibility without changing the working copy.

        The shelf and storage are read-only. BASE verification uses temporary
        local files, not persistent shelf files. No lock file is created, so a
        successful result cannot reserve the observed state for a later restore.
        """
        with self._client_lock:
            self._check_storage()
            if (self.storage_dir / ".operation.lock").exists():
                raise ShelfError("Another shelf operation is running; retry preflight when it finishes")
            manifest = self._load(shelf_id, verify_blobs=True)
            observations = self._preflight(manifest)
            for entry in manifest["entries"]:
                self._check_observation(entry, observations[entry["path"]])
            return {**self._summary(manifest), "operation": "restore", "dry_run": True,
                    "can_restore": True, "shelf_retained": True,
                    "repository": manifest["repository"], "entries": manifest["entries"],
                    "warnings": self._preflight_warnings()}

    def _preflight(self, manifest):
        repository = self._info()
        if any(repository[key] != manifest["repository"][key] for key in ("uuid", "url")):
            raise ShelfConflict("This shelf belongs to a different repository or working-copy URL")
        entries = manifest["entries"]
        statuses = self._status()
        by_path = {s["path"]: s for s in statuses}
        observations = {}
        planned_dirs = {e["path"] for e in entries if e["kind"] == "dir" and e["state"] in ("added", "unversioned")}
        try:
            self._check_ancestors((entry["path"] for entry in entries), by_path, planned_dirs)
        except ShelfError as exc:
            raise ShelfConflict(str(exc)) from exc
        for entry in entries:
            rel = entry["path"]
            target = self._path(rel)
            if rel in by_path:
                try:
                    self._check_status(by_path[rel])
                except ShelfError as exc:
                    raise ShelfConflict(str(exc)) from exc
            if entry["state"] in ("added", "unversioned"):
                if target.exists() or target.is_symlink() or rel in by_path:
                    raise ShelfConflict(f"Restore would overwrite an existing or scheduled path: {rel}")
                parent_rel = str(PurePosixPath(rel).parent)
                if parent_rel not in planned_dirs and not target.parent.is_dir():
                    raise ShelfConflict(f"Restore parent directory is missing: {rel}")
                observations[rel] = None
            else:
                status_entry = by_path.get(rel)
                if status_entry is None or status_entry["item"] != "normal" or status_entry["props"] not in ("none", "normal"):
                    raise ShelfConflict(f"Restore requires a clean versioned path: {rel}")
                if not target.exists():
                    raise ShelfConflict(f"Versioned restore target is missing: {rel}")
                info = self._info(rel)
                if self._base(rel, info) != entry["base"]:
                    raise ShelfConflict(f"BASE revision, content or properties changed: {rel}")
                if self._properties(rel) != entry["base"]["properties"]:
                    raise ShelfConflict(f"Local properties changed: {rel}")
                st = target.lstat()
                if (entry["kind"] == "file") != stat.S_ISREG(st.st_mode):
                    raise ShelfConflict(f"Restore target kind changed: {rel}")
                actual_hash = _file_digest(target) if entry["kind"] == "file" else None
                if actual_hash is not None and actual_hash != entry["base"]["working_sha256"]:
                    raise ShelfConflict(f"Local file bytes differ from translated BASE: {rel}")
                observations[rel] = {"mode": stat.S_IMODE(st.st_mode), "inode": st.st_ino,
                                     "sha256": actual_hash}
        return observations

    def _check_observation(self, entry, observation):
        path = self._path(entry["path"])
        if observation is None:
            if path.exists() or path.is_symlink():
                raise ShelfConflict(f"Target appeared during restore: {entry['path']}")
        else:
            st = path.lstat()
            if st.st_ino != observation["inode"] or stat.S_IMODE(st.st_mode) != observation["mode"] or (
                observation["sha256"] is not None and _file_digest(path) != observation["sha256"]
            ):
                raise ShelfConflict(f"Target changed during restore: {entry['path']}")

    def _set_properties(self, relative, wanted):
        actual = self._properties(relative)
        for name in sorted(set(actual) - set(wanted)):
            self._svn("propdel", "--", name, self._target(relative))
        for name, encoded in sorted(wanted.items()):
            if actual.get(name) == encoded:
                continue
            fd, temporary = tempfile.mkstemp(prefix=".property-", dir=self.storage_dir)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(_decoded(encoded))
                self._svn("propset", "--file", temporary, "--", name, self._target(relative))
            finally:
                Path(temporary).unlink(missing_ok=True)

    def restore(self, shelf_id) -> dict:
        """Restore only onto clean, identical BASE targets; never merge/overwrite edits.

        All collisions and blob integrity are checked before the first write. An
        unexpected OS/SVN failure can leave a partial restore; its error lists the
        affected paths and the verified shelf is kept for recovery. No destructive
        automatic rollback is attempted, since that could erase concurrent edits.
        """
        with self._locked():
            manifest = self._load(shelf_id, verify_blobs=True)
            observations = self._preflight(manifest)
            folder = self._shelf_path(shelf_id)
            changed = []
            try:
                entries = sorted(manifest["entries"], key=lambda entry: (entry["path"].count("/"), entry["path"]))
                for entry in entries:
                    rel, state = entry["path"], entry["state"]
                    path = self._path(rel)
                    self._check_observation(entry, observations[rel])
                    if state == "deleted":
                        changed.append(rel)  # A failed/timed-out command may still mutate SVN.
                        self._svn("delete", "--", self._target(rel))
                        continue
                    if entry["kind"] == "dir":
                        if state in ("added", "unversioned"):
                            path.mkdir(mode=0o700)
                            changed.append(rel)
                    else:
                        fd, temporary = tempfile.mkstemp(prefix=".svn-restore-", dir=path.parent)
                        try:
                            with os.fdopen(fd, "wb") as stream:
                                self._copy_verified_blob(entry, folder, manifest["format"], stream)
                                stream.flush()
                                os.fsync(stream.fileno())
                            self._check_observation(entry, observations[rel])
                            os.replace(temporary, path)
                            changed.append(rel)
                        finally:
                            Path(temporary).unlink(missing_ok=True)
                    if state == "added":
                        self._svn("add", "--no-auto-props", "--depth", "empty", "--", self._target(rel))
                    if state != "unversioned":
                        if rel not in changed:
                            changed.append(rel)
                        self._set_properties(rel, entry["properties"])
                # Apply directory modes last so a read-only saved directory does
                # not prevent restoring its children.
                for entry in reversed(entries):
                    if entry["state"] != "deleted":
                        os.chmod(self._path(entry["path"]), entry["mode"])
                return {**self._summary(manifest), "restored_paths": changed, "shelf_retained": True}
            except (ShelfError, OSError) as exc:
                if not changed:
                    raise
                raise ShelfError(f"Restore stopped after changing or attempting {', '.join(changed)}. Shelf retained; inspect the working copy before retrying. Cause: {exc}") from exc

    def delete(self, shelf_id) -> None:
        """Remove a local shelf only; the working copy is never touched."""
        with self._locked():
            folder = self._shelf_path(shelf_id)
            if not folder.is_dir():
                raise ShelfError("Shelf does not exist")
            shutil.rmtree(folder)
