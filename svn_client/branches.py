"""Conservative whole-working-copy switch and explicit forward range merge.

Branch/tag creation is a READ-ONLY plan, never a repository write. SVN's URL
copy fallback into an existing directory cannot preserve our exact destination
contract across a server race. Switch/merge never commit, force, retry or undo.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import quote, unquote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from .backend import StatusEntry, SvnClient, SvnError, redact_credentials
from .fileops import FileOperationError, _snapshot_bytes

MAX_TREE_ENTRIES = 10_000
MAX_TREE_BYTES = 256 * 1024 * 1024
MAX_PREVIEW_BYTES = 1024 * 1024
MAX_MERGE_REVISIONS = 64
_MAX_REVISION = 2**63 - 1


class BranchError(ValueError):
    """Read-only preflight rejected an unsupported, stale or unsafe request."""


class BranchOperationError(RuntimeError):
    """A local mutation was attempted. Never infer rollback or retry safety."""
    def __init__(self, message: str):
        super().__init__(message)
        self.outcome = "uncertain"
        self.automatic_retry_safe = False


@dataclass(frozen=True)
class BranchPlan:
    operation: str
    workspace: str
    source_url: str
    source_revision: int
    target_url: str
    working_revision: int
    repository_uuid: str
    repository_root: str
    start_revision: int | None
    state_sha256: str
    preview_text: str
    preview_has_conflicts: bool
    svn_dry_run: bool
    warnings: tuple[str, ...]
    scope: str = "whole_working_copy_root"
    commit_performed: bool = False


@dataclass(frozen=True)
class CopyPlan:
    kind: str
    source_url: str
    source_revision: int
    destination_url: str
    repository_uuid: str
    destination_checked_revision: int
    message: str
    state_sha256: str
    warnings: tuple[str, ...]
    execution_supported: bool = False
    would_commit_immediately: bool = True
    local_changes_included: bool = False


@dataclass(frozen=True)
class BranchResult:
    operation: str
    outcome: str
    workspace: str
    url: str
    working_revision: int
    output: str
    changes: tuple[StatusEntry, ...]
    conflicts: tuple[str, ...]
    commit_performed: bool = False
    automatic_retry_safe: bool = False


@dataclass(frozen=True)
class _TreeState:
    url: str
    repository_root: str
    repository_uuid: str
    revision: int
    state_sha256: str


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                     separators=(",", ":")).encode("ascii")).hexdigest()


def _revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise BranchError("Use an explicit nonnegative numeric revision, not HEAD, BASE, a range or a date.")
    text = str(value)
    if not re.fullmatch(r"[0-9]{1,19}", text) or int(text) > _MAX_REVISION:
        raise BranchError("Use an explicit nonnegative numeric revision, not HEAD, BASE, a range or a date.")
    return int(text)


def _url(value: str) -> str:
    """Canonicalize a literal URL, with peg revisions supplied separately."""
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or "\\" in value or re.search(r"%(?![0-9a-fA-F]{2})", value)):
        raise BranchError("Use a literal absolute repository URL without control characters or peg suffixes.")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https", "svn", "file"} or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or (parsed.scheme != "file" and not parsed.hostname)):
            raise ValueError
        parsed.port  # Validate malformed ports, without echoing the URL.
        path = unquote(parsed.path, encoding="utf-8", errors="strict")
        if (not path.startswith("/") or "\\" in path
                or re.search(r"%(?:2f|5c)", parsed.path, re.IGNORECASE)
                or any(ord(c) < 32 or ord(c) == 127 for c in path)
                or any(part in {".", ".."} for part in path.split("/"))
                or "//" in path):
            raise ValueError
    except (ValueError, UnicodeError):
        raise BranchError("Use a credential-free http, https, svn or file URL; provide revision separately (a URL @ is treated literally). svn+ssh is not supported here.") from None
    return urlunsplit((parsed.scheme, parsed.netloc, quote(path.rstrip("/") or "/", safe="/!$&'()*+,;=:-._~"), "", ""))


def validate_branch_url(value: str) -> str:
    """Validate/canonicalize a literal URL locally; never reads or transmits it."""
    return _url(value)


def validate_branch_revision(value: str | int) -> int:
    """Validate one fixed numeric revision locally, without contacting SVN."""
    return _revision(value)


def _under(url: str, root: str) -> bool:
    return url == root or url.startswith(root.rstrip("/") + "/")


def _safe_output(text: str) -> str:
    safe = redact_credentials(text)
    if len(safe.encode("utf-8", errors="replace")) > MAX_PREVIEW_BYTES:
        raise BranchError("SVN output exceeded the supported preview limit. Use a smaller separate working copy.")
    return safe


def _codes(error: BaseException) -> str:
    values = sorted(set(re.findall(r"\b[EW][0-9]{6}\b", str(error))))
    return " (" + ", ".join(values) + ")" if values else ""


class BranchManager:
    """Preview, review, then explicitly apply to one complete clean WC root.

    State hashes are stale-review guards, not permission tokens or protection
    against another process changing the working copy during the SVN command.
    """
    def __init__(self, client: SvnClient):
        self.client = client

    def _xml(self, args: list[str], tag: str) -> ET.Element:
        try:
            result = self.client.command(args)
            root = ET.fromstring(result.stdout)
            if root.tag != tag:
                raise ValueError
            return root
        except (SvnError, ValueError, ET.ParseError) as error:
            raise BranchError("Could not read complete SVN metadata; no mutation was started." + _codes(error)) from None

    @staticmethod
    def _info(entry: ET.Element) -> dict[str, object]:
        data = {"kind": entry.get("kind", ""), "revision": _revision(entry.get("revision", "")),
                "url": _url(entry.findtext("url", "")),
                "repository_root": _url(entry.findtext("repository/root", "")),
                "repository_uuid": entry.findtext("repository/uuid", ""),
                "wc_root": entry.findtext("wc-info/wcroot-abspath", ""),
                "schedule": entry.findtext("wc-info/schedule", ""),
                "depth": entry.findtext("wc-info/depth", "")}
        if not data["repository_uuid"] or not _under(str(data["url"]), str(data["repository_root"])):
            raise BranchError("SVN returned an incomplete or inconsistent repository identity.")
        return data

    def _remote(self, url: str, revision: int | str) -> dict[str, object]:
        root = self._xml(["info", "--xml", "--revision", str(revision), "--", f"{url}@{revision}"], "info")
        entries = root.findall("entry")
        if len(entries) != 1:
            raise BranchError("SVN did not identify exactly one repository directory.")
        data = self._info(entries[0])
        if data["kind"] != "dir" or data["url"] != url or (revision != "HEAD" and data["revision"] != revision):
            raise BranchError("The exact directory URL and requested revision could not be verified.")
        return data

    def _path(self, value: str) -> Path:
        root = self.client.working_copy
        if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise BranchError("SVN returned an empty or unsupported local path.")
        path = Path(os.path.abspath(root / value))
        try:
            relative = path.relative_to(root)
        except ValueError:
            raise BranchError("SVN returned a path outside the selected working copy.") from None
        if any(part.casefold() == ".svn" for part in relative.parts):
            raise BranchError("Administrative paths cannot be branch operation targets.")
        cursor = root
        for part in ("", *relative.parts):
            cursor = cursor / part
            try:
                node = cursor.lstat()
            except OSError:
                raise BranchError("A working path cannot be inspected safely.") from None
            if stat.S_ISLNK(node.st_mode) or getattr(node, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise BranchError("Symbolic links and reparse points are unsupported.")
            if cursor != root and (cursor / ".svn").exists():
                raise BranchError("Nested working copies are unsupported.")
        return path

    def _properties(self, target: str, revision: int | str | None = None) -> str:
        args = ["proplist", "--xml", "--verbose", "--depth", "infinity"]
        if revision is not None:
            args += ["--revision", str(revision)]
        root = self._xml([*args, "--", target], "properties")
        values = []
        for item in root.findall("target"):
            for prop in item.findall("property"):
                name = prop.get("name", "")
                if name in {"svn:externals", "svn:special"}:
                    raise BranchError("Trees with svn:externals or svn:special are unsupported; external workspaces remain separate.")
                values.append((item.get("path", ""), name, prop.get("encoding", ""), prop.text or ""))
        return _hash(sorted(values))

    def _statuses(self) -> list[StatusEntry]:
        try:
            entries = self.client.status(include_ignored=True, verbose=True)
        except (SvnError, ValueError) as error:
            raise BranchError("Could not read complete working-copy status." + _codes(error)) from None
        if not entries or len(entries) > MAX_TREE_ENTRIES:
            raise BranchError("Working-copy status is empty or exceeds the supported 10,000-entry limit.")
        return entries

    def _verify_base_tree(self, expected: dict[str, tuple[str, str, int]]) -> None:
        """Compare with SVN-translated local BASE, not its size/mtime cache.

        A single isolated export preserves EOL/keyword translation and binary
        bytes without writing in the working copy or issuing per-file commands.
        """
        try:
            temporary_root = Path(tempfile.gettempdir()).resolve()
            if temporary_root == self.client.working_copy or self.client.working_copy in temporary_root.parents:
                raise BranchError("BASE verification requires temporary storage outside this working copy.")
            with tempfile.TemporaryDirectory(prefix="svn-branches-base-", dir=temporary_root) as temporary:
                destination = Path(temporary) / "base-tree"
                self.client.command(["export", "--quiet", "--ignore-externals", "--revision", "BASE",
                                     "--", ".", str(destination)])
                pending, seen = [destination], set()
                while pending:
                    path = pending.pop()
                    relative = path.relative_to(destination).as_posix()
                    if relative not in expected or relative in seen:
                        raise BranchError("The working tree does not match the complete translated BASE tree.")
                    seen.add(relative)
                    kind, digest, size = expected[relative]
                    node = path.lstat()
                    if (stat.S_ISLNK(node.st_mode) or getattr(node, "st_file_attributes", 0)
                            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                        raise BranchError("Translated BASE contains an unsupported symbolic link or reparse point.")
                    if kind == "dir":
                        if not stat.S_ISDIR(node.st_mode):
                            raise BranchError("A working directory does not match translated BASE.")
                        with os.scandir(path) as children:
                            for child in children:
                                pending.append(Path(child.path))
                                if len(seen) + len(pending) > MAX_TREE_ENTRIES:
                                    raise BranchError("Translated BASE exceeds the supported 10,000-entry limit.")
                    elif (not stat.S_ISREG(node.st_mode) or node.st_size != size
                          or _snapshot_bytes(path)[:2] != (digest, size)):
                        raise BranchError("File bytes differ from translated BASE; the whole working copy must be clean.")
                if seen != set(expected):
                    raise BranchError("The working tree does not match the complete translated BASE tree.")
        except (OSError, FileOperationError, SvnError) as error:
            raise BranchError("Could not verify translated BASE safely; no mutation was started." + _codes(error)) from None

    def _working_metadata(self) -> dict[Path, dict[str, object]]:
        self._path(".")
        root = self._xml(["info", "--xml", "--depth", "infinity", "--", "."], "info")
        entries = root.findall("entry")
        if not entries or len(entries) > MAX_TREE_ENTRIES:
            raise BranchError("Use a complete working copy with at most 10,000 versioned entries.")
        metadata = {}
        for entry in entries:
            path = self._path(entry.get("path", ""))
            if path in metadata:
                raise BranchError("SVN returned duplicate working-copy metadata.")
            metadata[path] = self._info(entry)
        return metadata

    def _clean_tree(self) -> _TreeState:
        root_path = self.client.working_copy
        metadata = self._working_metadata()
        info = metadata.get(root_path)
        if (info is None or info["kind"] != "dir" or not info["wc_root"]
                or Path(str(info["wc_root"])).resolve() != root_path):
            raise BranchError("Open the actual complete SVN working-copy root, not a subdirectory or external.")
        statuses = self._statuses()
        observed = {}
        for status in statuses:
            path = self._path(status.path)
            if path in observed:
                raise BranchError("SVN returned duplicate working-copy status.")
            if (status.item != "normal" or status.props not in {"none", "normal"}
                    or status.conflicted or status.copied or status.switched or status.wc_locked
                    or status.file_external or status.moved_from or status.moved_to):
                raise BranchError("The whole working copy must be clean: no edits, ignored/unversioned files, conflicts, locks, switches or externals.")
            if status.revision != info["revision"]:
                raise BranchError("Mixed-revision working copies are unsupported. Update separately and review again.")
            observed[path] = status
        if set(observed) != set(metadata):
            raise BranchError("Status and metadata do not describe the same complete working tree.")
        files, total, expected_base = [], 0, {}
        for path, item in sorted(metadata.items(), key=lambda pair: str(pair[0])):
            relative = path.relative_to(root_path).as_posix()
            expected = str(info["url"]).rstrip("/") + ("/" + quote(relative, safe="/") if relative != "." else "")
            if (item["repository_uuid"] != info["repository_uuid"]
                    or item["repository_root"] != info["repository_root"] or item["url"] != _url(expected)
                    or not item["wc_root"] or Path(str(item["wc_root"])).resolve() != root_path
                    or item["revision"] != info["revision"] or item["schedule"] != "normal"
                    or item["kind"] not in {"file", "dir"}
                    or (item["kind"] == "dir" and item["depth"] != "infinity")):
                raise BranchError("Mixed revisions, sparse trees, switched paths and nested or foreign working copies are unsupported.")
            node = path.lstat()
            if item["kind"] == "dir":
                if not stat.S_ISDIR(node.st_mode):
                    raise BranchError("A versioned directory is obstructed.")
                expected_base[relative] = ("dir", "", 0)
                continue
            if node.st_nlink != 1:
                raise BranchError("Hard-linked versioned files are unsupported because edits can affect another path.")
            if total + node.st_size > MAX_TREE_BYTES:
                raise BranchError("The working tree exceeds the supported 256 MiB content-inspection limit.")
            try:
                digest, size, fingerprint = _snapshot_bytes(path)
            except (OSError, FileOperationError):
                raise BranchError("A file changed or could not be inspected safely during preflight.") from None
            total += size
            files.append((relative, digest, size, fingerprint))
            expected_base[relative] = ("file", digest, size)
        properties = self._properties(".")
        if properties != self._properties(".", "BASE"):
            raise BranchError("Working properties differ from BASE; the whole working copy must be clean.")
        # SVN status can report normal for different bytes with the recorded
        # size and mtime. Hashes alone bind review state, not cleanliness.
        self._verify_base_tree(expected_base)
        # Export can take time. Do not return the pre-export review hash if an
        # editor (including one restoring mtime) or SVN changed the WC during
        # that work. This narrows the race; it is not an interprocess lock.
        for relative, digest, size, fingerprint in files:
            try:
                current = _snapshot_bytes(self._path(relative))
            except (OSError, FileOperationError):
                raise BranchError("A file changed or could not be inspected safely after BASE verification.") from None
            if current != (digest, size, fingerprint):
                raise BranchError("The working-copy state changed during BASE verification. Review again.")
        if (self._working_metadata() != metadata
                or sorted(self._statuses(), key=lambda item: item.path) != sorted(statuses, key=lambda item: item.path)
                or self._properties(".") != properties):
            raise BranchError("The working-copy state changed during BASE verification. Review again.")
        state = {"workspace": str(root_path), "metadata": [(str(path), item) for path, item in sorted(metadata.items(), key=lambda pair: str(pair[0]))],
                 "status": [asdict(item) for item in sorted(statuses, key=lambda item: item.path)],
                 "files": files, "properties": properties}
        return _TreeState(str(info["url"]), str(info["repository_root"]), str(info["repository_uuid"]),
                          int(info["revision"]), _hash(state))

    @staticmethod
    def _same_repository(remote: dict[str, object], tree: _TreeState) -> None:
        if remote["repository_uuid"] != tree.repository_uuid or remote["repository_root"] != tree.repository_root:
            raise BranchError("The source and target must belong to this exact same repository and endpoint.")

    def _remote_tree(self, url: str, revision: int, *, peg_revision: int | None = None) -> str:
        """Reject known unsupported incoming trees before changing local files."""
        root = self._xml(["list", "--xml", "--recursive", "--revision", str(revision),
                          "--", f"{url}@{revision if peg_revision is None else peg_revision}"], "lists")
        lists = root.findall("list")
        if len(lists) != 1 or _url(lists[0].get("path", "")) != url:
            raise BranchError("Could not verify the exact incoming tree.")
        entries = lists[0].findall("entry")
        if len(entries) + 1 > MAX_TREE_ENTRIES:
            raise BranchError("The incoming tree exceeds the supported 10,000-entry limit.")
        values, names, total = [], set(), 0
        for entry in entries:
            name, kind = entry.findtext("name", ""), entry.get("kind", "")
            if (not name or name.startswith("/") or "\\" in name
                    or any(ord(c) < 32 or ord(c) == 127 for c in name)
                    or any(part in {"", ".", ".."} or part.casefold() == ".svn" for part in name.split("/"))
                    or kind not in {"file", "dir"} or name in names):
                raise BranchError("The incoming tree contains unsupported paths or incomplete metadata.")
            names.add(name)
            size = 0
            if kind == "file":
                value = entry.findtext("size", "")
                if not re.fullmatch(r"[0-9]{1,19}", value):
                    raise BranchError("SVN did not report a usable incoming file size.")
                size = int(value)
                total += size
            values.append((name, kind, size))
        kinds = {name: kind for name, kind, _ in values}
        for name in names:
            parent = name.rpartition("/")[0]
            if parent and kinds.get(parent) != "dir":
                raise BranchError("The incoming tree has missing or non-directory parents.")
        if total > MAX_TREE_BYTES:
            raise BranchError("The incoming tree exceeds the supported 256 MiB repository-content limit.")
        return _hash(sorted(values))

    @staticmethod
    def _merge_args(url: str, start: int, end: int, *, dry_run: bool) -> list[str]:
        args = ["merge", "--revision", f"{start}:{end}", "--depth", "infinity", "--accept", "postpone"]
        if dry_run:
            args.append("--dry-run")
        # merge does NOT support --ignore-externals. Reject external trees in
        # preflight instead of passing an invented switch to the real CLI.
        return [*args, "--", f"{url}@{end}", "."]

    def _preflight(self, operation: str, url: str, revision: int, start: int | None) -> BranchPlan:
        url, revision = _url(url), _revision(revision)
        if start is not None:
            start = _revision(start)
            if start >= revision:
                raise BranchError("Only a forward, nonempty revision range is supported (start < end).")
            if revision - start > MAX_MERGE_REVISIONS:
                raise BranchError("This conservative workflow supports at most 64 revisions per merge range.")
        with self.client._lock:
            tree = self._clean_tree()
            remote = self._remote(url, revision)
            self._same_repository(remote, tree)
            if url == tree.url:
                raise BranchError("Choose a different branch URL; self-merge and update-via-switch are unsupported.")
            # Merge tracking can split a requested range around already-merged
            # revisions. A forbidden property/path added then removed before
            # the endpoint can still be applied through such a hole. Inspect
            # every bounded historical state, retaining the SAME end peg.
            source_revisions = range(start, revision + 1) if start is not None else (revision,)
            incoming, source_properties = [], []
            for source_revision in source_revisions:
                incoming.append((source_revision, self._remote_tree(url, source_revision, peg_revision=revision)))
                source_properties.append((source_revision, self._properties(f"{url}@{revision}", source_revision)))
            preview, conflicts = "", False
            warnings = ["This affects the entire selected working-copy root. No commit is performed.",
                        "Preflight is a snapshot, not a reservation. Do not edit or run other SVN tools concurrently.",
                        "Failure or interruption can leave partial changes. Inspect status before any further action."]
            if operation == "merge":
                assert start is not None
                try:
                    result = self.client.command(self._merge_args(url, start, revision, dry_run=True))
                except SvnError as error:
                    raise BranchError("SVN merge dry-run failed; no apply command was started." + _codes(error)) from None
                preview = _safe_output(result.stdout + ("\n" + result.stderr if result.stderr else ""))
                conflicts = any(len(line) > 4 and line[4] == " " and "C" in line[:4]
                                and all(c in " UADGCRE!?~" for c in line[:4]) for line in preview.splitlines())
                warnings += ["The range applies changes after start through end, subject to SVN merge tracking.",
                             "A dry-run is not proof that the real merge will be conflict-free or complete. Review status, diff and mergeinfo afterward.",
                             "Merge results remain local edits, including svn:mergeinfo. Commit only after a separate review."]
            else:
                warnings += ["SVN switch has no dry-run. This is metadata validation only; ancestry and final success are checked by SVN when applying."]
            # Catch edits during network lookups and merge preview, not only
            # changes between the user's review and a later execution request.
            if self._clean_tree() != tree:
                raise BranchError("The working-copy state changed during preflight. Review again.")
            state = _hash({"operation": operation, "tree": asdict(tree), "source": remote,
                           "start": start, "preview": preview, "incoming": incoming, "source_properties": source_properties})
            return BranchPlan(operation, str(self.client.working_copy), url, revision, tree.url,
                              tree.revision, tree.repository_uuid, tree.repository_root, start, state, preview, conflicts, operation == "merge", tuple(warnings))

    def preflight_switch(self, url: str, *, revision: int) -> BranchPlan:
        return self._preflight("switch", url, revision, None)

    def preflight_merge(self, url: str, *, start_revision: int, end_revision: int) -> BranchPlan:
        return self._preflight("merge", url, end_revision, start_revision)

    def _apply(self, operation: str, url: str, revision: int, start: int | None,
               *, confirmed: bool, expected_state_sha256: str | None) -> BranchResult:
        if confirmed is not True:
            raise BranchError("Explicit confirmation of the whole-working-copy operation is required.")
        if not isinstance(expected_state_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_state_sha256):
            raise BranchError("A reviewed preflight state SHA-256 is required before applying.")
        with self.client._lock:
            plan = self._preflight(operation, url, revision, start)
            if plan.state_sha256 != expected_state_sha256:
                raise BranchError("The reviewed branch operation state changed. Review and confirm a fresh preflight.")
            if plan.preview_has_conflicts:
                raise BranchError("The dry-run predicts conflicts. This conservative workflow will not apply it.")
            args = (["switch", "--revision", str(plan.source_revision), "--depth", "infinity",
                     "--ignore-externals", "--accept", "postpone", "--", f"{plan.source_url}@{plan.source_revision}", "."]
                    if operation == "switch" else self._merge_args(plan.source_url, plan.start_revision, plan.source_revision, dry_run=False))
            try:
                result = self.client.command(args)
                output = _safe_output(result.stdout + ("\n" + result.stderr if result.stderr else ""))
                if operation == "switch":
                    current = self._clean_tree()
                    if (current.url != plan.source_url or current.revision != plan.source_revision
                            or current.repository_uuid != plan.repository_uuid or current.repository_root != plan.repository_root):
                        raise BranchError("Switch postconditions did not match the reviewed target.")
                    return BranchResult(operation, "switched", plan.workspace, current.url, current.revision, output, (), ())
                info = self.client.info()
                if (_url(str(info.get("url", ""))) != plan.target_url or info.get("revision") != plan.working_revision
                        or info.get("repository_uuid") != plan.repository_uuid
                        or _url(str(info.get("repository_root", ""))) != plan.repository_root
                        or not info.get("wc_root") or Path(str(info["wc_root"])).resolve() != self.client.working_copy):
                    raise BranchError("The target working-copy mapping changed during the merge.")
                self._properties(".")
                statuses = self._statuses()
                for status in statuses:
                    # Deleted/conflict-artifact paths need not exist. Validate
                    # their lexical scope, but never read or resolve them here.
                    Path(os.path.abspath(self.client.working_copy / status.path)).relative_to(self.client.working_copy)
                    if status.file_external or status.item == "external" or status.switched or status.wc_locked:
                        raise BranchError("Unexpected external, switched or locked state after merge.")
                conflicts = tuple(item.path for item in statuses if item.conflicted)
                changes = tuple(item for item in statuses if item.item != "normal" or item.props not in {"none", "normal"})
                return BranchResult(operation, "conflicted" if conflicts else "needs_review", plan.workspace,
                                    plan.target_url, plan.working_revision, output, changes, conflicts)
            except (Exception, KeyboardInterrupt) as error:
                raise BranchOperationError("The SVN operation was attempted, but completion could not be verified. "
                                           "The outcome is uncertain; changes were retained. Inspect status and history before retrying."
                                           + _codes(error)) from None

    def switch(self, url: str, *, revision: int, confirmed: bool = False,
               expected_state_sha256: str | None = None) -> BranchResult:
        return self._apply("switch", url, revision, None, confirmed=confirmed, expected_state_sha256=expected_state_sha256)

    def merge(self, url: str, *, start_revision: int, end_revision: int, confirmed: bool = False,
              expected_state_sha256: str | None = None) -> BranchResult:
        return self._apply("merge", url, end_revision, start_revision, confirmed=confirmed, expected_state_sha256=expected_state_sha256)

    def preflight_copy(self, source_url: str, destination_url: str, *, revision: int,
                       kind: str = "branch", message: str) -> CopyPlan:
        """Inspect a proposed remote branch/tag. There is no execute method."""
        source_url, destination_url, revision = _url(source_url), _url(destination_url), _revision(revision)
        if kind not in {"branch", "tag"}:
            raise BranchError("Copy kind must be branch or tag.")
        try:
            message_size = len(message.encode("utf-8")) if isinstance(message, str) else 8193
        except UnicodeError:
            raise BranchError("The commit message must be valid Unicode text.") from None
        if (not isinstance(message, str) or not message.strip() or message_size > 8192
                or any(ord(c) < 32 and c not in "\n\r\t" for c in message)
                or redact_credentials(message) != message):
            raise BranchError("Use a nonempty commit message of at most 8192 UTF-8 bytes without credentials or control characters.")
        if source_url == destination_url or _under(destination_url, source_url):
            raise BranchError("The exact new destination must be outside the source directory tree.")
        with self.client._lock:
            tree = self._clean_tree()
            source = self._remote(source_url, revision)
            self._same_repository(source, tree)
            if not _under(destination_url, tree.repository_root) or destination_url == tree.repository_root:
                raise BranchError("The destination must be a new directory in this exact repository.")
            parent, name = destination_url.rsplit("/", 1)
            parent_info = self._remote(parent, "HEAD")
            self._same_repository(parent_info, tree)
            checked = int(parent_info["revision"])
            listing = self._xml(["list", "--xml", "--revision", str(checked), "--", f"{parent}@{checked}"], "lists")
            lists = listing.findall("list")
            if len(lists) != 1 or _url(lists[0].get("path", "")) != parent:
                raise BranchError("Could not verify the exact existing destination parent.")
            names = [entry.findtext("name", "") for entry in lists[0].findall("entry")]
            if any(not value or "/" in value or "\\" in value for value in names):
                raise BranchError("SVN returned unsupported destination child names.")
            if unquote(name).casefold() in {value.casefold() for value in names}:
                raise BranchError("The exact destination already exists or has a case-insensitive collision.")
            state = _hash({"kind": kind, "source": source, "destination": destination_url,
                           "parent": parent_info, "names": sorted(names), "message": message})
            return CopyPlan(kind, source_url, revision, destination_url, tree.repository_uuid, checked,
                            message, state, (
                                "This is a read-only plan. Branch/tag creation is not executable in this preview.",
                                "A URL-to-URL SVN copy commits immediately and includes committed source content only.",
                                "A tag is an ordinary SVN directory, not intrinsically immutable; repository policy must protect it.",
                                "Destination absence is only a snapshot. SVN copy can copy into a newly created destination directory, so exact-destination execution is withheld.",
                                "This plan does not freeze or follow externals, check server write permission or run commit hooks.",
                            ))
