"""Verified SVN repository file locks, distinct from working-copy admin locks.

Only ordinary committed local files are supported. No lock stealing, breaking,
forced tokens, credential arguments, automatic retries, or rollback are offered.
Public data never contains a raw server or working-copy lock token.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Sequence
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from .backend import SvnClient, SvnError, redact_credentials


class LockError(RuntimeError):
    """Read-only validation or metadata retrieval failed before mutation."""


class LockOperationError(LockError):
    """A write was attempted. Its result may be partial or already complete."""
    def __init__(self, message: str, observed: Sequence["FileLockInfo"] = ()):
        super().__init__(message)
        self.outcome = "uncertain"
        self.automatic_retry_safe = False
        self.observed = tuple(observed)


@dataclass(frozen=True)
class FileLockInfo:
    path: str
    url: str
    repository_uuid: str
    working_revision: int
    server_last_changed_revision: int
    locked: bool
    owner: str = ""
    comment: str = ""
    created: str = ""
    expires: str = ""
    local_token_present: bool = False
    owned_by_working_copy: bool = False
    stale_local_token: bool = False


@dataclass(frozen=True)
class LockPlan:
    action: str
    files: tuple[FileLockInfo, ...]
    state_sha256: str
    comment: str = ""
    warnings: tuple[str, ...] = (
        "Repository locks can block other users' commits but do not prevent local editing.",
        "SVN may change local write permissions when svn:needs-lock is set.",
        "Server state can change after review; multi-file lock operations are not atomic.",
    )

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(info.path for info in self.files)


@dataclass(frozen=True)
class LockResult:
    action: str
    files: tuple[FileLockInfo, ...]
    verified: bool = True


@dataclass(frozen=True)
class _Snapshot:
    info: FileLockInfo
    local_token: str = field(repr=False)
    server_token: str = field(repr=False)


_SAFE_ITEMS = {"normal", "modified"}
MAX_LOCK_FILES = 128
_TOKEN_TEXT = re.compile(r"(?:opaquelocktoken:|urn:uuid:)[^\s\"'<>]+")
_ERROR_CODE = re.compile(r"\b[EW][0-9]{6}\b")


def _safe_text(value: str) -> str:
    return _TOKEN_TEXT.sub("<redacted-lock-token>", redact_credentials(value))


def _revision(value: str | None) -> int:
    try:
        result = int(value or "")
    except ValueError:
        raise LockError("SVN did not return a usable file revision.") from None
    if result < 0:
        raise LockError("Commit the file before using repository locks.")
    return result


class LockManager:
    def __init__(self, client: SvnClient):
        self.client = client

    @staticmethod
    def _comment(comment: str) -> str:
        if not isinstance(comment, str):
            raise LockError("The lock comment must be text.")
        try:
            size = len(comment.encode("utf-8"))
        except UnicodeError:
            raise LockError("The lock comment must be valid Unicode text.") from None
        if size > 4096 or any(ord(c) < 32 and c not in "\n\r\t" for c in comment):
            raise LockError("Lock comments must be at most 4096 UTF-8 bytes and contain no unsupported control characters.")
        if redact_credentials(comment) != comment or _TOKEN_TEXT.search(comment):
            raise LockError("Do not include passwords or lock tokens in lock comments.")
        return comment

    def _xml_info(self, target: str) -> ET.Element:
        try:
            result = self.client.command(["info", "--xml", "--", target])
            root = ET.fromstring(result.stdout)
        except (SvnError, ValueError, ET.ParseError):
            raise LockError("Could not read SVN file/lock metadata. Check access and connection; no lock operation was started.") from None
        entries = root.findall("entry")
        if len(entries) != 1 or entries[0].get("kind") != "file":
            raise LockError("Repository locks require exactly one ordinary versioned file per target.")
        return entries[0]

    @staticmethod
    def _token(entry: ET.Element) -> str:
        lock = entry.find("lock")
        if lock is None:
            return ""
        token = lock.findtext("token")
        if not token:
            raise LockError("SVN returned incomplete lock metadata; refusing to infer ownership.")
        return token

    def _snapshots(self, paths: Sequence[str | os.PathLike[str]]) -> tuple[_Snapshot, ...]:
        if isinstance(paths, (str, bytes, os.PathLike)) or not isinstance(paths, Sequence):
            raise LockError("Pass an explicit list of local file paths.")
        if len(paths) > MAX_LOCK_FILES:
            raise LockError(f"Select at most {MAX_LOCK_FILES} files per operation; server lock checks are performed one file at a time.")
        try:
            normalized = self.client.validate_changelist_paths(paths)
        except (SvnError, ValueError, OSError, TypeError):
            raise LockError("Select explicit versioned files inside this working copy; directories, links, externals, and nested working copies are unsupported.") from None
        if not normalized:
            raise LockError("Select at least one file; an empty selection never means the entire workspace.")
        try:
            statuses = {os.path.normcase(os.path.abspath(self.client.working_copy / entry.path)): entry
                        for entry in self.client.status(verbose=True)}
        except (SvnError, ValueError, OSError):
            raise LockError("Could not inspect local file status; no lock operation was started.") from None
        local_entries = []
        # Complete local validation before contacting any repository.
        for path in normalized:
            absolute = self.client.working_copy / path
            try:
                metadata = absolute.lstat()
            except OSError:
                raise LockError("Selected lock targets must exist as ordinary local files.") from None
            if not stat.S_ISREG(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise LockError("Only ordinary local files are supported for lock operations.")
            if metadata.st_nlink > 1:
                raise LockError("Hardlinked files are unsupported because SVN lock permissions could affect another path.")
            status = statuses.get(os.path.normcase(str(absolute)))
            if status is None or status.item not in _SAFE_ITEMS or status.conflicted or status.copied or status.moved_from or status.moved_to or status.file_external or status.wc_locked or status.switched:
                raise LockError("Commit or resolve added, deleted, moved, copied, conflicted, switched, or incomplete targets before using repository locks.")
            parent = absolute.parent
            while parent == self.client.working_copy or self.client.working_copy in parent.parents:
                ancestor = statuses.get(os.path.normcase(str(parent)))
                if ancestor and (ancestor.switched or ancestor.conflicted or ancestor.copied or ancestor.moved_from or ancestor.moved_to or ancestor.item not in _SAFE_ITEMS):
                    raise LockError("Resolve switched, conflicted, or structurally changed parent directories before using repository locks.")
                if parent == self.client.working_copy:
                    break
                parent = parent.parent
            local = self._xml_info(self.client._local_targets([path])[0])
            if local.findtext("wc-info/schedule") != "normal":
                raise LockError("Commit scheduled structural changes before using repository locks.")
            url = local.findtext("url", "")
            try:
                parsed = urlsplit(url)
                safe_url = ("://" in url and url == url.strip() and not any(ord(c) < 32 for c in url)
                            and parsed.password is None and parsed.scheme.lower() in {"http", "https", "svn", "file"}
                            and bool(parsed.path if parsed.scheme.lower() == "file" else parsed.hostname)
                            and not parsed.query and not parsed.fragment)
            except ValueError:
                safe_url = False
            if not safe_url:
                raise LockError("Repository locks require an http, https, svn, or file URL without embedded passwords. External SSH transports are not supported here.")
            uuid = local.findtext("repository/uuid", "")
            if not uuid:
                raise LockError("SVN did not identify the repository for a selected file.")
            local_entries.append((path, url, uuid, local, _revision(local.get("revision")), self._token(local)))
        snapshots = []
        for path, url, uuid, local, revision, local_token in local_entries:
            # info's stored URL is literal, not user-supplied peg syntax. An
            # explicit final @HEAD preserves a literal '@' in the filename.
            remote = self._xml_info(url + "@HEAD")
            if remote.findtext("repository/uuid", "") != uuid:
                raise LockError("Repository identity changed while checking lock ownership.")
            token = self._token(remote)
            last_change = remote.find("commit")
            changed_revision = _revision(last_change.get("revision") if last_change is not None else None)
            lock = remote.find("lock")
            def text(name):
                value = lock.findtext(name, "") if lock is not None else ""
                # Lock comments can contain arbitrary text. Never echo either
                # token, even if it appears there under a custom token scheme.
                for secret in (local_token, token):
                    if secret:
                        value = value.replace(secret, "<redacted-lock-token>")
                return _safe_text(value)
            info = FileLockInfo(path, _safe_text(url), uuid, revision, changed_revision,
                                bool(token), text("owner"), text("comment"), text("created"), text("expires"),
                                bool(local_token), bool(token and local_token == token),
                                bool(local_token and local_token != token))
            snapshots.append(_Snapshot(info, local_token, token))
        return tuple(snapshots)

    def inspect(self, paths: Sequence[str | os.PathLike[str]]) -> list[FileLockInfo]:
        """Read current server owners and compare local tokens; contacts server."""
        with self.client._lock:
            return [snapshot.info for snapshot in self._snapshots(paths)]

    @staticmethod
    def _plan(action: str, snapshots: Sequence[_Snapshot], comment: str = "") -> LockPlan:
        for snapshot in snapshots:
            info = snapshot.info
            if action == "lock":
                if info.locked:
                    raise LockError("At least one selected file is already locked. Existing locks are never stolen, even from another working copy of the same user.")
                if info.stale_local_token:
                    raise LockError("The working copy has a stale lock token. Refresh or repair it with a trusted SVN client before acquiring a new lock.")
                if info.working_revision < info.server_last_changed_revision:
                    raise LockError("A selected file is out of date. Update and review it before acquiring its lock.")
            elif not info.owned_by_working_copy:
                raise LockError("Unlock requires the current server lock token in this exact working copy. Missing, stolen, or other-working-copy locks are never broken.")
        state = {"action": action, "comment": comment, "files": [
            {"info": asdict(item.info), "local_token": item.local_token, "server_token": item.server_token}
            for item in sorted(snapshots, key=lambda item: item.info.path)
        ]}
        digest = hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()
        return LockPlan(action, tuple(item.info for item in snapshots), digest, comment)

    def preflight_lock(self, paths: Sequence[str | os.PathLike[str]], comment: str = "") -> LockPlan:
        comment = self._comment(comment)
        with self.client._lock:
            return self._plan("lock", self._snapshots(paths), comment)

    def preflight_unlock(self, paths: Sequence[str | os.PathLike[str]]) -> LockPlan:
        with self.client._lock:
            return self._plan("unlock", self._snapshots(paths))

    def _operate(self, action: str, paths: Sequence[str | os.PathLike[str]], comment: str,
                 confirmed: bool, expected_state: str | None) -> LockResult:
        if confirmed is not True:
            raise LockError("Explicit confirmation is required before changing repository file locks. Use preflight first.")
        if expected_state is not None and (not isinstance(expected_state, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_state)):
            raise LockError("expected_state must be the SHA-256 value from the reviewed lock plan.")
        with self.client._lock:
            plan = self.preflight_lock(paths, comment) if action == "lock" else self.preflight_unlock(paths)
            if expected_state is not None and plan.state_sha256 != expected_state:
                raise LockError("The reviewed lock state changed. Refresh and confirm a new plan; no lock operation was started.")
            options = [action]
            if action == "lock":
                options.extend(["--message=" + comment, "--encoding", "UTF-8"])
            attempted_error = None
            try:
                # Exact local targets retain native token semantics. Never use
                # URL-only mutation, --force, or supplied lock tokens.
                self.client._command_with_targets(options, self.client._local_targets(plan.paths))
            except (SvnError, ValueError, OSError, KeyboardInterrupt) as error:
                attempted_error = ", ".join(dict.fromkeys(_ERROR_CODE.findall(str(error))))
            try:
                observed = tuple(self.inspect(plan.paths))
            except (LockError, SvnError, ValueError, OSError, KeyboardInterrupt):
                raise LockOperationError("A lock operation was attempted but the server result could not be verified. Some files may already be changed. Inspect locks before retrying; no automatic retry or rollback occurred.") from None
            achieved = all(info.owned_by_working_copy if action == "lock" else not info.locked and not info.local_token_present for info in observed)
            if attempted_error is not None or not achieved:
                codes = f" SVN codes: {attempted_error}." if attempted_error else ""
                raise LockOperationError("The lock operation may be partial or already complete. The refreshed server state is attached; inspect it before retrying. No automatic retry or rollback occurred." + codes, observed) from None
            return LockResult(action, observed)

    def lock(self, paths: Sequence[str | os.PathLike[str]], comment: str = "", *,
             confirmed: bool = False, expected_state: str | None = None) -> LockResult:
        """Acquire reviewed files' server locks, without stealing existing locks."""
        comment = self._comment(comment)
        return self._operate("lock", paths, comment, confirmed, expected_state)

    def unlock(self, paths: Sequence[str | os.PathLike[str]], *, confirmed: bool = False,
               expected_state: str | None = None) -> LockResult:
        """Release locks owned by this exact working copy, without breaking any."""
        return self._operate("unlock", paths, "", confirmed, expected_state)
