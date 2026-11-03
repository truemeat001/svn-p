"""Small, dependency-free interface to the installed Subversion CLI.

The CLI is always invoked as an argument vector, never through a shell. The
client uses SVN's existing credential cache, never prompts, and never accepts a
password argument. Authentication must be configured with a trusted SVN client.
This module is a local desktop utility, not a sandbox for untrusted commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import locale
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from typing import Sequence
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class SvnError(RuntimeError):
    """An SVN failure. A timeout/error does NOT mean that changes rolled back."""

    def __init__(self, message: str, result: CommandResult | None = None):
        super().__init__(_redact_credentials(message))
        self.result = result


@dataclass(frozen=True)
class StatusEntry:
    path: str
    item: str
    props: str = "none"
    revision: int | None = None
    tree_conflicted: bool = False
    copied: bool = False
    switched: bool = False
    wc_locked: bool = False
    moved_from: str | None = None
    moved_to: str | None = None
    changelist: str | None = None
    file_external: bool = False
    last_changed_revision: int | None = None
    last_changed_author: str = ""

    @property
    def conflicted(self) -> bool:
        return self.tree_conflicted or self.item == "conflicted" or self.props == "conflicted"


@dataclass(frozen=True)
class LogEntry:
    revision: int
    author: str = ""
    date: str = ""
    message: str = ""
    changed_paths: list[dict[str, str | int | None]] = field(default_factory=list)


@dataclass(frozen=True)
class RepoEntry:
    name: str
    kind: str
    size: int | None = None
    revision: int | None = None
    author: str = ""
    date: str = ""


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_URL_SCHEMES = {"http", "https", "svn", "svn+ssh", "file"}
_BLOCKED_OPTIONS = {
    "--password", "--password-from-stdin", "--force-interactive",
    "--trust-server-cert", "--trust-server-cert-failures", "--config-option",
    "--editor-cmd", "--diff-cmd", "--diff3-cmd",
}
_BLOCKED_COMMANDS = {"propedit", "pedit", "pe"}


def _redact_credentials(text: str) -> str:
    return re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:)[^/\s@]*@", r"\1***@", text)


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _is_url(value: str) -> bool:
    return urlsplit(value).scheme.lower() in _URL_SCHEMES


def discover_svn(executable: str | os.PathLike[str] | None = None) -> str:
    """Find SVN on PATH or in common Windows installations.

    An explicitly selected executable is respected; it is never replaced by a
    different executable after an invalid selection.
    """
    if executable is not None:
        requested = os.fspath(executable)
        if not requested or "\0" in requested:
            raise SvnError("Invalid SVN executable path.")
        found = shutil.which(requested)
        if found:
            return _validate_executable(found)
        candidate = Path(requested).expanduser()
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return _validate_executable(str(candidate.resolve()))
        raise SvnError("SVN executable was not found. Select the installed svn executable in settings.")
    found = shutil.which("svn")
    if found:
        return _validate_executable(found)
    if os.name == "nt":
        roots = {os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")}
        for root in sorted(roots):
            for suffix in ("TortoiseSVN/bin/svn.exe", "SlikSvn/bin/svn.exe",
                           "VisualSVN Server/bin/svn.exe", "CollabNet/Subversion Client/svn.exe"):
                candidate = Path(root) / suffix
                if candidate.is_file():
                    return str(candidate)
    raise SvnError("Subversion CLI was not found. Install SVN command-line tools and select svn in settings.")


def _validate_executable(path: str) -> str:
    # Windows may launch .bat/.cmd through a shell even with shell=False.
    # Require a native executable instead of allowing a PATH wrapper script.
    if os.name == "nt" and os.path.splitext(path)[1].lower() != ".exe":
        raise SvnError("Select the native svn.exe executable; Windows batch wrappers are not supported.")
    return path


class SvnClient:
    """Thread-safe per-directory facade over a trusted installed svn executable."""

    def __init__(self, working_copy: str | os.PathLike[str],
                 executable: str | os.PathLike[str] | None = None, timeout: float = 120):
        self.working_copy = Path(working_copy).expanduser().resolve()
        if not self.working_copy.is_dir():
            raise SvnError("Working-copy directory does not exist.")
        if timeout <= 0:
            raise ValueError("Timeout must be positive.")
        self.timeout = timeout
        self.executable = discover_svn(executable)
        # Real .svn metadata is at the WC root, not necessarily the selected
        # subdirectory. Serialize clients for every directory in that WC.
        lock_root = self.working_copy
        for candidate in (self.working_copy, *self.working_copy.parents):
            if (candidate / ".svn").is_dir():
                lock_root = candidate
                break
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(os.path.normcase(str(lock_root)), threading.RLock())

    def command(self, args: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        """Run an explicit SVN argument list and return captured Unicode output.

        This is an advanced escape hatch. It does not shell-split a string or
        infer permission for destructive commands. The caller must confirm them.
        Arguments after ``--`` are passed literally, including peg revisions.
        """
        if isinstance(args, (str, bytes)) or not args:
            raise ValueError("SVN arguments must be a nonempty sequence of strings.")
        values = list(args)
        if any(not isinstance(arg, str) or "\0" in arg for arg in values):
            raise ValueError("SVN arguments must be strings without NUL characters.")
        command = values[0]
        if command.startswith("-") or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]*", command):
            raise ValueError("The first argument must be an SVN subcommand.")
        if command in _BLOCKED_COMMANDS:
            raise ValueError("Interactive property editors are not supported. Use propset or propdel.")
        options = values[1:values.index("--")] if "--" in values else values[1:]
        for index, arg in enumerate(options):
            option = arg.split("=", 1)[0]
            if option in _BLOCKED_OPTIONS:
                raise ValueError(f"{option} is disabled. Use your trusted SVN client for authentication or configuration.")
            if arg.startswith("--accept=") and arg.split("=", 1)[1] in {"edit", "launch"}:
                raise ValueError("External conflict editors are not supported.")
            if arg == "--accept" and index + 1 < len(options) and options[index + 1] in {"edit", "launch"}:
                raise ValueError("External conflict editors are not supported.")
        for arg in values:
            if "://" in arg:
                try:
                    parsed = urlsplit(arg)
                except ValueError as error:
                    raise ValueError("Invalid repository URL.") from error
                if parsed.password is not None:
                    raise ValueError("Passwords in repository URLs are not allowed. Use SVN's credential cache.")
        duration = self.timeout if timeout is None else timeout
        if duration <= 0:
            raise ValueError("Timeout must be positive.")
        argv = ([self.executable, "--version", *values[1:]] if command == "version" else
                [self.executable, command, "--non-interactive", "--no-auth-cache", *values[1:]])
        if command in {"diff", "di"}:
            argv.insert(4, "--internal-diff")
        # SVN uses UTF-8 for XML. A UTF-8 locale also supports Chinese filenames
        # on Unix; Windows argv is Unicode independently of the console page.
        environment = os.environ.copy()
        if os.name != "nt":
            environment.setdefault("LC_CTYPE", "C.UTF-8")
        kwargs: dict[str, object] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        display_args = self._display_args(argv)
        with self._lock:
            try:
                completed = subprocess.run(
                    argv, cwd=self.working_copy, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    shell=False, timeout=duration, env=environment, **kwargs,
                )
            except subprocess.TimeoutExpired as error:
                # Never automatically retry: a commit may already have reached
                # the server, and a local operation may have partially finished.
                raise SvnError(
                    f"SVN timed out after {duration:g} seconds. The result is uncertain; "
                    "refresh status and history before retrying. Changes were not rolled back."
                ) from error
            except OSError as error:
                raise SvnError(f"Could not start SVN: {error}") from error
        result = CommandResult(display_args, completed.returncode,
                               self._decode(completed.stdout), _redact_credentials(self._decode(completed.stderr)))
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "No diagnostic output."
            raise SvnError(f"SVN exited with code {result.returncode}: {detail}", result)
        return result

    @staticmethod
    def _decode(value: bytes | str) -> str:
        if isinstance(value, str):
            return value
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            # XML is always UTF-8, but non-XML diagnostics may use the native
            # Windows code page. Prefer that over discarding non-ASCII names.
            encoding = locale.getencoding() if hasattr(locale, "getencoding") else locale.getpreferredencoding(False)
            return value.decode(encoding, errors="replace")

    @staticmethod
    def _display_args(args: Sequence[str]) -> tuple[str, ...]:
        result: list[str] = []
        hide_next = False
        for arg in args:
            if hide_next:
                result.append("<message>")
                hide_next = False
            elif arg in {"--message", "-m"}:
                result.append(arg)
                hide_next = True
            elif arg.startswith("--message="):
                result.append("--message=<message>")
            else:
                result.append(_redact_credentials(arg))
        return tuple(result)

    def _xml(self, args: Sequence[str]) -> ET.Element:
        result = self.command(args)
        try:
            return ET.fromstring(result.stdout)
        except ET.ParseError as error:
            raise SvnError("SVN returned invalid XML; verify that the selected executable is Subversion.") from error

    def _local_targets(self, paths: Sequence[str | os.PathLike[str]] | None) -> list[str]:
        if paths is None:
            paths = ["."]
        elif isinstance(paths, (str, bytes, os.PathLike)):
            raise ValueError("Pass paths as a list, not a single string.")
        elif not paths:
            raise ValueError("Select at least one path; an empty selection never means the whole working copy.")
        result = []
        for path in paths:
            value = os.fspath(path)
            if not value or "\0" in value or _is_url(value):
                raise ValueError("Expected a local path inside the selected working copy.")
            absolute = Path(os.path.abspath(self.working_copy / value))
            try:
                relative = absolute.relative_to(self.working_copy)
            except ValueError as error:
                raise ValueError("Paths outside the selected working copy are not allowed.") from error
            try:
                absolute.resolve().relative_to(self.working_copy)
            except (ValueError, RuntimeError, OSError) as error:
                raise ValueError("Paths through symbolic links or junctions outside the selected working copy are not allowed.") from error
            value = str(relative)
            # Appending an empty peg revision escapes every literal '@', even
            # a filename ending in '@'. It is needed for local paths only.
            result.append(value + "@" if "@" in value else value)
        return result

    def _target(self, target: str | os.PathLike[str] | None) -> str:
        if target is not None and _is_url(os.fspath(target)):
            return os.fspath(target)
        return self._local_targets(None if target is None else [target])[0]

    def _relative(self, value: str) -> str:
        path = Path(value)
        if path.is_absolute():
            try:
                return str(path.relative_to(self.working_copy))
            except ValueError:
                return value
        return str(path)

    def info(self, target: str | os.PathLike[str] | None = None) -> dict[str, object]:
        root = self._xml(["info", "--xml", "--", self._target(target)])
        entry = root.find("entry")
        if entry is None:
            raise SvnError("SVN returned no information for the selected target.")
        return {
            "path": self._relative(entry.get("path", "")), "kind": entry.get("kind", ""),
            "revision": _int(entry.get("revision")), "url": entry.findtext("url", ""),
            "relative_url": entry.findtext("relative-url", ""),
            "repository_root": entry.findtext("repository/root", ""),
            "repository_uuid": entry.findtext("repository/uuid", ""),
            "wc_root": entry.findtext("wc-info/wcroot-abspath", ""),
            "schedule": entry.findtext("wc-info/schedule", ""),
            "depth": entry.findtext("wc-info/depth", ""),
            "last_changed_revision": _int(entry.find("commit").get("revision")) if entry.find("commit") is not None else None,
            "last_changed_author": entry.findtext("commit/author", ""),
            "last_changed_date": entry.findtext("commit/date", ""),
        }

    def status(self, paths: Sequence[str | os.PathLike[str]] | None = None,
               *, include_ignored: bool = False) -> list[StatusEntry]:
        args = ["status", "--xml", "--ignore-externals"]
        if include_ignored:
            args.append("--no-ignore")
        root = self._xml([*args, "--", *self._local_targets(paths)])
        changelists = {id(entry): group.get("name") for group in root.findall(".//changelist")
                      for entry in group.findall("entry")}
        results = []
        for entry in root.findall(".//entry"):
            status = entry.find("wc-status")
            if status is None:
                continue
            commit = status.find("commit")
            results.append(StatusEntry(
                path=self._relative(entry.get("path", "")), item=status.get("item", "none"),
                props=status.get("props", "none"), revision=_int(status.get("revision")),
                tree_conflicted=status.get("tree-conflicted") == "true",
                copied=status.get("copied") == "true", switched=status.get("switched") == "true",
                wc_locked=status.get("wc-locked") == "true", moved_from=status.get("moved-from"),
                moved_to=status.get("moved-to"), changelist=changelists.get(id(entry)),
                file_external=status.get("file-external") == "true",
                last_changed_revision=_int(commit.get("revision")) if commit is not None else None,
                last_changed_author=status.findtext("commit/author", ""),
            ))
        return results

    def log(self, limit: int = 100, target: str | os.PathLike[str] | None = None,
            *, revision: str | None = None) -> list[LogEntry]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("Log limit must be a positive integer.")
        args = ["log", "--xml", "--verbose", "--limit", str(limit)]
        if revision:
            args.extend(["--revision", revision])
        root = self._xml([*args, "--", self._target(target)])
        return [LogEntry(
            revision=int(entry.get("revision", "0")), author=entry.findtext("author", ""),
            date=entry.findtext("date", ""), message=entry.findtext("msg", ""),
            changed_paths=[{"path": path.text or "", "action": path.get("action", ""),
                            "kind": path.get("kind", ""), "copyfrom_path": path.get("copyfrom-path"),
                            "copyfrom_rev": _int(path.get("copyfrom-rev"))}
                           for path in entry.findall("paths/path")],
        ) for entry in root.findall("logentry")]

    def list_entries(self, target: str | os.PathLike[str] | None = None,
                     revision: str | None = None) -> list[RepoEntry]:
        args = ["list", "--xml"]
        if revision:
            args.extend(["--revision", revision])
        root = self._xml([*args, "--", self._target(target)])
        return [RepoEntry(
            name=entry.findtext("name", ""), kind=entry.get("kind", ""),
            size=_int(entry.findtext("size")),
            revision=_int(entry.find("commit").get("revision")) if entry.find("commit") is not None else None,
            author=entry.findtext("commit/author", ""), date=entry.findtext("commit/date", ""),
        ) for entry in root.findall(".//entry")]

    def diff(self, paths: Sequence[str | os.PathLike[str]] | None = None,
             *, revision: str | None = None) -> str:
        args = ["diff"]
        if revision:
            args.extend(["--revision", revision])
        return self.command([*args, "--", *self._local_targets(paths)]).stdout

    def update(self, paths: Sequence[str | os.PathLike[str]] | None = None) -> CommandResult:
        return self.command(["update", "--ignore-externals", "--accept", "postpone", "--", *self._local_targets(paths)])

    def commit(self, message: str, paths: Sequence[str | os.PathLike[str]] | None = None) -> CommandResult:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("A nonempty commit message is required.")
        return self.command(["commit", "--message", message, "--encoding", "UTF-8", "--", *self._local_targets(paths)])

    def add(self, paths: Sequence[str | os.PathLike[str]]) -> CommandResult:
        return self.command(["add", "--parents", "--", *self._local_targets(paths)])

    def revert(self, paths: Sequence[str | os.PathLike[str]], recursive: bool = False) -> CommandResult:
        return self.command(["revert", "--depth", "infinity" if recursive else "empty", "--", *self._local_targets(paths)])

    def version(self) -> str:
        return self.command(["version", "--quiet"]).stdout.strip()
