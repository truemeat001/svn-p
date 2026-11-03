"""Small, dependency-free interface to the installed Subversion CLI.

The CLI is always invoked as an argument vector, never through a shell. SVN's
own prompts are disabled and existing credentials are reused without accepting
password arguments. External transports can have their own prompt behavior;
the dedicated checkout helper therefore excludes svn+ssh. Authentication must
be configured with a trusted SVN client.
This module is a local desktop utility, not a sandbox for untrusted commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import locale
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
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
_DIRECT_TARGET_LIMIT = 24_000  # Leave headroom below Windows' 32,767 UTF-16 units.
_CREDENTIAL_AUTHORITY_RE = re.compile(r"(://[^/\s:?#]*:)[^/\s?#]*@")


def redact_credentials(text: str) -> str:
    """Redact URL authority passwords without quadratic scanning of prose.

    Start with the fixed literal '://' rather than a variable-length scheme.
    Each candidate's authority stops before '/' (also query, fragment, or
    whitespace), so it cannot scan past another '://' marker. Username parsing
    stops at the first colon; a greedy password span ends at the last '@', as
    URL userinfo parsing does. Empty usernames and unescaped email usernames
    are treated conservatively, even for a malformed or unknown URL scheme.
    """
    return _CREDENTIAL_AUTHORITY_RE.sub(r"\1***@", text)


# Keep the existing internal name for error/command formatting and callers that
# imported it before the public machine-interface helper was introduced.
_redact_credentials = redact_credentials


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
               *, include_ignored: bool = False, verbose: bool = False) -> list[StatusEntry]:
        args = ["status", "--xml", "--ignore-externals"]
        if include_ignored:
            args.append("--no-ignore")
        if verbose:
            args.append("--verbose")
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

    @staticmethod
    def _changelist_name(name: str) -> str:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("A nonempty changelist name is required.")
        if any(ord(character) < 32 or ord(character) == 127 for character in name):
            raise ValueError("Changelist names cannot contain control characters.")
        return name

    def changelists(self) -> dict[str, list[StatusEntry]]:
        """List native, local SVN file assignments, including clean members.

        A native changelist has no independent persisted identity: after its last
        file is removed, it no longer exists. Directories cannot be members.
        """
        result: dict[str, list[StatusEntry]] = {}
        for entry in self.status(verbose=True):
            if entry.changelist and not entry.file_external and entry.item != "external":
                # Never turn an unexpected absolute/outside status entry into a
                # target for a later changelist commit. Listing only reads SVN
                # metadata, so do not follow an unrelated symlink here; every
                # mutating operation separately validates the chosen targets.
                try:
                    Path(os.path.abspath(self.working_copy / entry.path)).relative_to(self.working_copy)
                except ValueError as error:
                    raise SvnError("SVN returned a changelist path outside the selected working copy.") from error
                result.setdefault(entry.changelist, []).append(entry)
        for entries in result.values():
            entries.sort(key=lambda entry: entry.path)
        return dict(sorted(result.items()))

    def _changelist_file_targets(self, paths: Sequence[str | os.PathLike[str]]) -> list[str]:
        if paths is None:
            raise ValueError("Select individual versioned files for the changelist.")
        targets = self._local_targets(paths)
        root_info = self.info()
        wc_root = str(root_info.get("wc_root") or "")
        if not wc_root:
            raise SvnError("SVN did not identify a working-copy root for changelist operations.")
        expected_root = Path(wc_root).resolve()
        for original in paths:
            # Reject symlinks and Windows reparse points, even when their
            # destination happens to remain within the selected working copy.
            absolute = Path(os.path.abspath(self.working_copy / os.fspath(original)))
            cursor = self.working_copy
            for part in absolute.relative_to(self.working_copy).parts:
                cursor /= part
                try:
                    metadata = cursor.lstat()
                except FileNotFoundError:
                    # Scheduled deletions need not exist on disk; SVN info is
                    # still authoritative for their kind and WC membership.
                    continue
                if stat.S_ISLNK(metadata.st_mode) or (
                    getattr(metadata, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                ):
                    raise ValueError("Changelist operations do not support symbolic links or junctions.")
            info = self.info(original)
            if info.get("kind") != "file":
                raise ValueError("SVN changelists support individual versioned files only, not directories.")
            file_root = str(info.get("wc_root") or "")
            if not file_root or Path(file_root).resolve() != expected_root:
                raise ValueError("Files in externals or nested working copies cannot join this changelist.")
        selected = {os.path.normcase(os.path.abspath(self.working_copy / os.fspath(path))) for path in paths}
        # Status does not support --targets on all supported SVN versions. Scan
        # the selected WC once instead of producing a huge status argument list.
        for entry in self.status(verbose=True):
            if entry.file_external or entry.item == "external":
                if os.path.normcase(os.path.abspath(self.working_copy / entry.path)) in selected:
                    raise ValueError("External files cannot join a changelist in this working copy.")
        return list(dict.fromkeys(targets))

    def _command_size(self, args: Sequence[str]) -> int:
        text = subprocess.list2cmdline([self.executable, "--non-interactive", "--no-auth-cache", *args])
        return len(text.encode("utf-16-le")) // 2

    def _command_with_targets(self, options: Sequence[str], targets: Sequence[str],
                              positionals: Sequence[str] = ()) -> CommandResult:
        """Keep large exact selections below the Windows argument limit.

        SVN reads --targets using the native locale and trims line whitespace.
        Encoding is strict, with a read-only XML roundtrip before any mutation.
        Names that cannot be represented as target-file lines stay on argv.
        No passwords, commit messages, or file contents are written to disk.
        """
        targets = list(dict.fromkeys(targets))
        direct = [*options, "--", *positionals, *targets]
        if self._command_size(direct) <= _DIRECT_TARGET_LIMIT:
            return self.command(direct)
        encoding = locale.getencoding() if hasattr(locale, "getencoding") else locale.getpreferredencoding(False)
        buffered: list[str] = []
        lines: list[str] = []
        inline: list[str] = []
        for target in targets:
            if "://" in target and urlsplit(target).password is not None:
                raise ValueError("Passwords in target filenames or URLs cannot be written to a targets file.")
            # Prefix protects leading whitespace; an empty peg suffix protects
            # trailing whitespace from SVN's line trimming. Existing targets
            # containing '@' already have the suffix from _local_targets().
            line = "./" + target + ("@" if "@" not in target else "")
            try:
                encoded = line.encode(encoding, errors="strict")
                if encoded.decode(encoding, errors="strict") != line:
                    raise UnicodeError("Target does not roundtrip through the native encoding")
            except (UnicodeError, LookupError):
                inline.append(target)
                continue
            if "\n" in line or "\r" in line:
                inline.append(target)
            else:
                buffered.append(target)
                lines.append(line)
        if not buffered:
            raise ValueError("This selection is too large for one command and its filenames cannot safely use an SVN targets file. Select fewer files.")
        with tempfile.TemporaryDirectory(prefix="svn-client-targets-") as directory:
            filename = Path(directory) / "targets.txt"
            # Use a private temporary directory and restrictive POSIX mode where
            # supported. Close the handle before SVN opens it on Windows.
            descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(("\n".join(lines) + "\n").encode(encoding, errors="strict"))
            args = [*options, "--targets", str(filename), "--", *positionals, *inline]
            if self._command_size(args) > _DIRECT_TARGET_LIMIT:
                raise ValueError("The commit message, changelist name, or exceptional filenames are too long for one command. Select fewer files or shorten the message.")
            # A misdetected legacy Windows codepage must never redirect a write
            # to a different existing (mojibake) filename. Verify the exact set
            # through the same SVN executable before using this targets file.
            try:
                root = self._xml(["info", "--xml", "--depth", "empty", "--targets", str(filename)])
                observed = [self._local_targets([entry.get("path", "")])[0] for entry in root.findall("entry")]
            except (SvnError, ValueError) as error:
                raise SvnError("SVN could not safely read the exact target filenames. No changelist operation was started; select fewer files or check the SVN locale.") from error
            if sorted(observed) != sorted(buffered):
                raise SvnError("SVN target-file decoding changed the filename selection. No changelist operation was started.")
            return self.command(args)

    def assign_changelist(self, name: str, paths: Sequence[str | os.PathLike[str]]) -> CommandResult:
        """Move selected versioned files into one native SVN changelist.

        Every target is checked before changing any assignment. This does not add
        files to version control and does not create empty placeholder lists.
        """
        name = self._changelist_name(name)
        with self._lock:
            targets = self._changelist_file_targets(paths)
            return self._command_with_targets(["changelist", "--depth", "empty"], targets, [name])

    def validate_changelist_paths(self, paths: Sequence[str | os.PathLike[str]]) -> list[str]:
        """Read-only validation; return unique raw relative file paths.

        The result is for display or later high-level calls, not literal CLI
        arguments. It excludes the empty peg suffix needed only on SVN argv.
        Validation does not reserve files against changes by other processes.
        """
        with self._lock:
            targets = self._changelist_file_targets(paths)
            return [target[:-1] if "@" in target else target for target in targets]

    def remove_changelist(self, paths: Sequence[str | os.PathLike[str]]) -> CommandResult:
        """Remove only the selected files' labels; keep their content and edits."""
        with self._lock:
            targets = self._changelist_file_targets(paths)
            return self._command_with_targets(["changelist", "--remove", "--depth", "empty"], targets)

    def preflight_changelist_commit(self, name: str,
                                   expected_paths: Sequence[str | os.PathLike[str]] | None = None) -> list[StatusEntry]:
        """Validate local commit eligibility without modifying the repository.

        Returns all members, including clean ones, and checks known local
        dependencies. This is not an SVN server dry-run: remote out-of-date
        checks, authorization, locks, and hook acceptance are not established.
        Another application can still change files or assignments afterward.
        """
        name = self._changelist_name(name)
        with self._lock:
            members = self.changelists().get(name, [])
            if not members:
                raise ValueError("The changelist has no members in this working copy. Refresh before committing.")
            paths = [entry.path for entry in members]
            targets = set(self._local_targets(paths))
            if expected_paths is not None and set(self._local_targets(expected_paths)) != targets:
                raise ValueError("Changelist membership changed after review. Refresh and review it again before committing.")
            if not any(entry.item not in {"normal", "none"} or entry.props in {"modified", "conflicted"} for entry in members):
                raise ValueError("The changelist has no modified files to commit.")
            if any(entry.conflicted or entry.item in {"missing", "obstructed", "incomplete", "unversioned", "ignored", "external"} for entry in members):
                raise ValueError("Resolve conflicting, missing, obstructed, or unversioned changelist members before committing.")
            self._changelist_file_targets(paths)
            # Build lexical keys only. An unrelated symlink elsewhere in the
            # working copy must not fail validation of the reviewed members.
            statuses = {os.path.normcase(os.path.abspath(self.working_copy / entry.path)): entry
                        for entry in self.status(verbose=True)}
            for entry in members:
                for counterpart in (entry.moved_from, entry.moved_to):
                    if counterpart and self._local_targets([counterpart])[0] not in targets:
                        raise ValueError("A moved file requires both source and destination in the reviewed changelist.")
                parent = Path(os.path.abspath(self.working_copy / entry.path)).parent
                while parent == self.working_copy or self.working_copy in parent.parents:
                    parent_status = statuses.get(os.path.normcase(str(parent)))
                    if parent_status and (parent_status.conflicted or parent_status.item in {"added", "deleted", "replaced", "missing", "obstructed", "incomplete"}):
                        raise ValueError("A changelist member depends on an added, deleted, replaced, or conflicted directory. Commit or resolve the directory change separately; this operation will not expand its scope.")
                    if parent == self.working_copy:
                        break
                    parent = parent.parent
            return members

    def commit_changelist(self, name: str, message: str, *, keep_changelist: bool = False,
                         expected_paths: Sequence[str | os.PathLike[str]] | None = None) -> CommandResult:
        """Commit one native changelist without broadening its file scope.

        ``expected_paths`` is the full membership displayed during confirmation,
        including clean members. A changed membership aborts before the commit.
        Explicit file targets plus SVN's changelist filter exclude unrelated
        paths even if another process adds a member after this preflight.
        File contents can still be changed by an external editor during commit.
        SVN removes committed assignments unless ``keep_changelist`` is true.
        Added/deleted directory dependencies are never automatically included.
        """
        name = self._changelist_name(name)
        if not isinstance(message, str) or not message.strip():
            raise ValueError("A nonempty commit message is required.")
        if not isinstance(keep_changelist, bool):
            raise ValueError("keep_changelist must be a boolean.")
        with self._lock:
            members = self.preflight_changelist_commit(name, expected_paths)
            paths = [entry.path for entry in members]
            targets = self._local_targets(paths)
            args = ["commit", "--changelist=" + name, "--message=" + message,
                    "--encoding", "UTF-8", "--depth", "empty"]
            if keep_changelist:
                args.append("--keep-changelists")
            return self._command_with_targets(args, targets)

    def add(self, paths: Sequence[str | os.PathLike[str]]) -> CommandResult:
        return self.command(["add", "--parents", "--", *self._local_targets(paths)])

    def revert(self, paths: Sequence[str | os.PathLike[str]], recursive: bool = False) -> CommandResult:
        return self.command(["revert", "--depth", "infinity" if recursive else "empty", "--", *self._local_targets(paths)])

    def version(self) -> str:
        return self.command(["version", "--quiet"]).stdout.strip()


def _check_checkout_path_components(destination: Path) -> None:
    for component in reversed((destination, *destination.parents)):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ValueError("The checkout destination cannot be inspected safely.") from error
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ValueError("Checkout destinations cannot contain symbolic links or junctions.")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("The checkout destination and its existing parents must be directories.")


def validate_checkout_destination(destination: str | os.PathLike[str]) -> Path:
    """Read-only validation for an explicit new or empty checkout directory.

    Its immediate parent must already exist. This does not create directories,
    delete anything, or reserve the path against another process changing it.
    """
    value = os.fspath(destination)
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("Choose a nonempty local checkout destination.")
    path = Path(os.path.abspath(Path(value).expanduser()))
    if path.name.lower() == ".svn":
        raise ValueError("The SVN administrative directory name cannot be used as a checkout destination.")
    _check_checkout_path_components(path)
    if not path.parent.is_dir():
        raise ValueError("The checkout destination's parent directory must already exist.")
    for ancestor in (path, *path.parents):
        if os.path.lexists(ancestor / ".svn"):
            raise ValueError("Checkout cannot be nested inside an existing SVN working copy.")
    if path.exists():
        try:
            if any(path.iterdir()):
                raise ValueError("Checkout requires a new or empty directory; existing contents will not be overwritten.")
        except OSError as error:
            raise ValueError("The checkout destination cannot be inspected safely.") from error
    return path


def validate_checkout_url(url: str) -> str:
    """Validate a repository URL locally, without logging it or contacting it."""
    if not isinstance(url, str) or not url or url != url.strip() or any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError("Enter a repository URL without surrounding whitespace or control characters.")
    try:
        parsed = urlsplit(url)
        password, hostname = parsed.password, parsed.hostname
    except ValueError as error:
        # Do not include the supplied URL or urllib's input-bearing diagnostics.
        raise ValueError("The repository URL is invalid.") from error
    if password is not None:
        raise ValueError("Passwords in repository URLs are not allowed. Use SVN's existing credential cache.")
    if parsed.scheme.lower() == "svn+ssh":
        raise ValueError("Built-in checkout does not run svn+ssh because an external SSH helper can prompt or outlive the SVN timeout. Check out with your trusted SVN/SSH client, then add that existing working copy.")
    if "://" not in url or parsed.scheme.lower() not in {"http", "https", "svn", "file"} or (parsed.scheme.lower() != "file" and not hostname):
        raise ValueError("Use an http, https, svn, or file repository URL.")
    if parsed.fragment or parsed.query or (parsed.scheme.lower() == "file" and not parsed.path):
        raise ValueError("Repository URLs cannot contain a query or fragment; encode literal filename characters in the URL.")
    return url


def checkout_working_copy(url: str, destination: str | os.PathLike[str], *,
                          executable: str | os.PathLike[str] | None = None,
                          timeout: float = 300) -> CommandResult:
    """Perform an explicitly authorized checkout, retaining all partial output.

    Callers must obtain confirmation of URL and destination before invoking this
    mutating helper. Externals are excluded. Credentials and certificates must
    be configured in a trusted SVN client, not supplied in the URL. No automatic
    retry, force-overwrite, authentication caching, or failure cleanup is done.
    A timeout or failed verification is not evidence that nothing was created.
    svn+ssh is deliberately unsupported here because external SSH helpers can
    prompt independently and survive termination of the SVN parent process.
    """
    url = validate_checkout_url(url)
    try:
        valid_timeout = not isinstance(timeout, bool) and isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout > 0
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise ValueError("Checkout timeout must be a finite positive number.")
    path = validate_checkout_destination(destination)
    client = SvnClient(path.parent, executable=executable, timeout=timeout)
    with client._lock:
        validate_checkout_destination(path)
        try:
            # Unlike normal SVN targets, checkout's explicit local destination
            # is not peg-parsed. Preserve every literal '@' without a suffix.
            result = client.command(["checkout", "--ignore-externals", "--", url, str(path)])
        except SvnError as error:
            raise SvnError(
                "Checkout did not finish successfully. Any files created at the destination were retained; "
                "inspect that directory before retrying. " + str(error), error.result
            ) from error
        try:
            _check_checkout_path_components(path)
            info = SvnClient(path, executable=client.executable, timeout=timeout).info()
            actual_root = str(info.get("wc_root") or "")
            if not actual_root or os.path.normcase(os.path.abspath(actual_root)) != os.path.normcase(str(path)):
                raise ValueError("SVN did not report the selected destination as the working-copy root.")
        except (SvnError, ValueError, OSError) as error:
            raise SvnError(
                "The checkout command completed, but working-copy verification failed. "
                "The destination was retained; inspect it before retrying. " + str(error)
            ) from error
        return result
