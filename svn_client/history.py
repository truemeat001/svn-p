"""Bounded, read-only path history and explicit revision comparison.

Only trusted installed SVN is invoked, without a shell, prompts, auth caching,
external diff commands, credential arguments, or temporary patch files. This
module never modifies working files, repository data, or SVN configuration.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
from typing import Sequence
from urllib.parse import SplitResult, unquote, urlsplit
import xml.etree.ElementTree as ET

from .backend import LogEntry, SvnClient, SvnError, redact_credentials


# Web consumers require fail-closed completion of all read-only transports.
HISTORY_DIAGNOSTICS_POLICY = 1
DIFF_TEXT_POLICY = 1  # UTF-8 segments plus explicit visible escapes; never locale guessing.

MAX_HISTORY_ENTRIES = 1000
MAX_HISTORY_BYTES = 4 * 1024 * 1024
MAX_DIFF_BYTES = 8 * 1024 * 1024
DEFAULT_DIFF_TEXT_BYTES = 256 * 1024
MAX_DIAGNOSTIC_BYTES = 64 * 1024
_MAX_REVISION = 2**63 - 1


class HistoryError(SvnError):
    """A read failed; no working-copy or repository mutation was requested."""


@dataclass(frozen=True)
class BoundedText:
    text: str
    truncated: bool
    source_utf8_bytes: int
    returned_utf8_bytes: int
    text_encoding: str = 'utf-8'
    undecodable_bytes: int = 0
    escaped_controls: int = 0


@dataclass(frozen=True)
class HistoryResult:
    path: str
    start_revision: str
    entries: tuple[LogEntry, ...]
    has_more: bool
    next_start_revision: int | None


@dataclass(frozen=True)
class ComparisonResult:
    path: str
    from_revision: str
    to_revision: str
    diff: BoundedText
    binary_payloads_included: bool = False


def decode_diff_output(raw: bytes) -> tuple[str, int, int]:
    """Preserve valid UTF-8 even when SVN's native-encoded headers differ.

    Diff is not one guaranteed-encoding document: Windows metadata can use the
    native code page while file hunks retain their original bytes. No locale
    fallback can safely decode that whole stream (or determine file encoding).
    Undecodable bytes stay individually visible as \\xNN, with explicit counts
    for every caller to disclose. This is display-only, never an exact patch.
    """
    if b'\0' in raw or b'\xff\xfe' in raw or b'\xfe\xff' in raw:
        raise HistoryError('SVN diff contains NUL bytes or a UTF-16 byte-order marker; binary or UTF-16 data is not displayed. No partial comparison was returned.')
    value = raw.decode('utf-8', errors='surrogateescape')
    undecodable = controls = 0

    def visible(match):
        nonlocal undecodable, controls
        code = ord(match.group())
        if 0xDC80 <= code <= 0xDCFF:
            undecodable += 1
            return '\\x%02x' % (code - 0xDC00)
        controls += 1
        return '\\x%02x' % code if code < 128 else '\\u%04x' % code

    # Bound re.sub's temporary replacement list even for an all-invalid 8-MiB
    # stream. Keep CRLF together at chunk boundaries so policy is unchanged.
    pattern = re.compile(r'[\udc80-\udcff\x01-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]|\r(?!\n)')
    parts = []
    start = 0
    while start < len(value):
        end = min(start + 64 * 1024, len(value))
        if end < len(value) and value[end - 1] == '\r' and value[end] == '\n':
            end += 1
        parts.append(pattern.sub(visible, value[start:end]))
        start = end
    return ''.join(parts), undecodable, controls



def normalize_revision(value: str | int, *, working: bool = False) -> str:
    """Accept a single explicit revision, never ranges, dates, or options."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Use BASE, HEAD, or a nonnegative revision number.")
    text = str(value).strip().upper()
    if text in {"BASE", "HEAD"} or (working and text == "WORKING"):
        return text
    if re.fullmatch(r"[0-9]{1,19}", text) and int(text) <= _MAX_REVISION:
        return str(int(text))
    if text == "WORKING":
        raise ValueError("WORKING is supported as the right-hand revision only. Put BASE, HEAD, or a number on the left.")
    raise ValueError("Use BASE, HEAD, WORKING on the right, or a nonnegative revision number; ranges and dates are not supported here.")


def _bounded_process(argv: Sequence[str], *, cwd: Path, timeout: float,
                     stdout_limit: int) -> bytes:
    """Capture bounded pipes in memory; abort rather than expose cut diagnostics.

    A cut URL could expose part of a password before its closing '@', so neither
    an oversized stream nor any failure diagnostic is returned to the caller.
    Concurrent draining avoids deadlocks when both pipes contain substantial data.
    """
    environment = os.environ.copy()
    if os.name != "nt":
        environment.setdefault("LC_CTYPE", "C.UTF-8")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        process = subprocess.Popen(
            list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
            env=environment, bufsize=0, **options,
        )
    except OSError as error:
        raise HistoryError("Could not start SVN for this read. Check the installed executable.") from error
    overflow = threading.Event()
    read_failed = threading.Event()
    buffers = [bytearray(), bytearray()]

    def kill() -> None:
        try:
            process.kill()
        except OSError:
            pass

    def drain(stream, slot: int, limit: int) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                remaining = limit - len(buffers[slot])
                buffers[slot].extend(chunk[:max(remaining, 0)])
                if len(chunk) > remaining:
                    overflow.set()
                    kill()
        except (OSError, ValueError):
            read_failed.set()
            kill()
        finally:
            stream.close()

    readers = [threading.Thread(target=drain, args=(process.stdout, 0, stdout_limit), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, 1, MAX_DIAGNOSTIC_BYTES), daemon=True)]
    for reader in readers:
        reader.start()
    try:
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise HistoryError("SVN history/comparison read timed out. No write was requested; retry explicitly if needed.") from error
    finally:
        if process.poll() is None:
            kill()
        cleanup_failed = False
        try:
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            cleanup_failed = True
        for reader in readers:
            reader.join(timeout=2)
        if cleanup_failed:
            raise HistoryError("SVN process cleanup could not be verified within the deadline. No partial result is shown; check running SVN processes before retrying.")
    if any(reader.is_alive() for reader in readers) or read_failed.is_set():
        raise HistoryError("SVN output could not be read safely. No partial result is shown.")
    if overflow.is_set():
        raise HistoryError("SVN output exceeded the bounded capture limit. Narrow the path or history page; no partial result is shown.")
    if code:
        # Arbitrary errors may embed file content, auth tokens, or credentials.
        # Include SVN's stable diagnostic codes only, never raw stderr/stdout.
        codes = sorted(set(re.findall(rb"\b[EW][0-9]{6}\b", bytes(buffers[1]))))[:8]
        suffix = ", " + ", ".join(item.decode("ascii") for item in codes) if codes else ""
        raise HistoryError(f"SVN read failed (exit {code}{suffix}). Check the path, revisions, repository access, and authentication with a trusted SVN client.")
    if buffers[1]:
        # A zero exit code can still accompany W155007 or another incomplete
        # read. We cannot reliably classify localized/custom diagnostics as
        # harmless. Even whitespace is rejected; no partial/empty success or
        # raw stderr is exposed. Configure a quiet trusted SVN client instead.
        raise HistoryError("SVN emitted diagnostics despite exit code zero. No history or comparison result was accepted; verify the target with a trusted SVN client.")
    return bytes(buffers[0])


class HistoryManager:
    def __init__(self, client: SvnClient):
        self.client = client

    def _run(self, args: Sequence[str], byte_limit: int) -> bytes:
        timeout = self.client.timeout
        try:
            valid_timeout = (not isinstance(timeout, bool) and isinstance(timeout, (int, float))
                             and math.isfinite(timeout) and timeout > 0)
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ValueError("History/comparison timeout must be finite and positive.")
        if args[0] not in {"info", "log", "diff"}:
            raise ValueError("Only read-only history commands are supported.")
        argv = [self.client.executable, args[0], "--non-interactive", "--no-auth-cache"]
        if args[0] == "diff":
            argv.append("--internal-diff")
        argv.extend(args[1:])
        return _bounded_process(argv, cwd=self.client.working_copy, timeout=timeout,
                                stdout_limit=byte_limit)

    def _xml(self, args: Sequence[str], byte_limit: int) -> ET.Element:
        raw = self._run(args, byte_limit)
        # SVN emits UTF-8 XML. UTF-16/32 embeds NULs and could otherwise conceal
        # a declaration from this byte-level guard while ElementTree accepts it.
        if b"\0" in raw or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise HistoryError("SVN XML declarations are unsupported; no result was accepted.")
        try:
            return ET.fromstring(raw)
        except (ET.ParseError, ValueError) as error:
            raise HistoryError("SVN returned invalid XML for this read.") from error

    def _target(self, path: str | os.PathLike[str]) -> tuple[str, str]:
        target = self.client._local_targets([path])[0]
        relative = target[:-1] if "@" in target else target
        cursor = self.client.working_copy
        for part in Path(relative).parts:
            if part.lower() == ".svn":
                raise ValueError("SVN administrative paths cannot be inspected as project files.")
            cursor /= part
            try:
                metadata = cursor.lstat()
            except FileNotFoundError:
                continue  # SVN metadata can describe a scheduled deletion.
            if stat.S_ISLNK(metadata.st_mode) or (getattr(metadata, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                raise ValueError("History/comparison does not follow symbolic links or junctions.")
        return relative, target

    def _info(self, target: str) -> tuple[ET.Element, SplitResult]:
        root = self._xml(["info", "--xml", "--depth", "empty", "--", target], 256 * 1024)
        entries = root.findall("entry")
        if root.tag != "info" or len(entries) != 1 or entries[0].get("kind") not in {"file", "dir"}:
            raise HistoryError("SVN did not return a versioned file or directory for this path.")
        entry = entries[0]
        try:
            parsed = urlsplit(entry.findtext("url", ""))
            safe = (parsed.scheme.lower() in {"http", "https", "svn", "file"}
                    and parsed.password is None and not parsed.query and not parsed.fragment
                    and bool(parsed.path if parsed.scheme.lower() == "file" else parsed.hostname))
        except ValueError:
            safe = False
        if not safe:
            raise ValueError("History/comparison requires a credential-free http, https, svn, or file repository URL. External SSH transports are not supported here.")
        return entry, parsed

    def _check_target(self, target: str) -> None:
        entry, url = self._info(target)
        root_entry, root_url = (entry, url) if target == "." else self._info(".")
        wc_root = root_entry.findtext("wc-info/wcroot-abspath", "")
        target_root = entry.findtext("wc-info/wcroot-abspath", "")
        uuid = root_entry.findtext("repository/uuid", "")
        if not wc_root or not target_root or not uuid or not Path(wc_root).is_absolute() or not Path(target_root).is_absolute():
            raise HistoryError("SVN did not provide the working-copy identity needed to verify this path.")
        expected_root = os.path.normcase(os.path.abspath(wc_root))
        if root_entry.get("kind") != "dir" or os.path.normcase(os.path.abspath(target_root)) != expected_root:
            raise ValueError("History/comparison cannot cross into another or nested working copy. Open that workspace explicitly.")
        try:
            self.client.working_copy.relative_to(Path(wc_root).resolve())
        except (ValueError, OSError, RuntimeError):
            raise HistoryError("SVN returned an inconsistent working-copy root.") from None
        relative = target[:-1] if "@" in target else target
        suffix = "/".join(Path(relative).parts)
        expected_url_path = unquote(root_url.path).rstrip("/") + ("/" + suffix if suffix else "")
        if (entry.findtext("repository/uuid", "") != uuid
                or (url.scheme.lower(), url.netloc) != (root_url.scheme.lower(), root_url.netloc)
                or unquote(url.path).rstrip("/") != expected_url_path):
            raise ValueError("History/comparison cannot silently follow a switched or external target. Open its owning workspace explicitly.")

    def history(self, path: str | os.PathLike[str] = ".", *, limit: int = 100,
                start_revision: str | int = "HEAD") -> HistoryResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_HISTORY_ENTRIES:
            raise ValueError(f"History limit must be between 1 and {MAX_HISTORY_ENTRIES}.")
        start = normalize_revision(start_revision)
        relative, target = self._target(path)
        with self.client._lock:
            self._check_target(target)
            root = self._xml(["log", "--xml", "--verbose", "--limit", str(limit + 1),
                              "--revision", start + ":0", "--", target], MAX_HISTORY_BYTES)
        nodes = root.findall("logentry")
        if root.tag != "log" or len(nodes) > limit + 1:
            raise HistoryError("SVN returned an unexpected history response.")
        entries = []
        previous = _MAX_REVISION + 1
        for node in nodes:
            revision_text = node.get("revision", "")
            if not re.fullmatch(r"[0-9]{1,19}", revision_text):
                raise HistoryError("SVN history contains an invalid revision.")
            revision = int(revision_text)
            if revision >= previous or revision > _MAX_REVISION:
                raise HistoryError("SVN history is not in descending revision order.")
            previous = revision
            changes = []
            for changed in node.findall("paths/path"):
                copy_revision = changed.get("copyfrom-rev", "")
                changes.append({"path": redact_credentials(changed.text or ""),
                                "action": redact_credentials(changed.get("action", "")),
                                "kind": redact_credentials(changed.get("kind", "")),
                                "copyfrom_path": redact_credentials(changed.get("copyfrom-path", "")) or None,
                                "copyfrom_rev": int(copy_revision) if re.fullmatch(r"[0-9]{1,19}", copy_revision) else None})
            entries.append(LogEntry(revision, redact_credentials(node.findtext("author", "")),
                                    redact_credentials(node.findtext("date", "")),
                                    redact_credentials(node.findtext("msg", "")), changes))
        has_more = len(entries) > limit
        entries = entries[:limit]
        next_start = entries[-1].revision - 1 if has_more else None
        return HistoryResult(relative, start, tuple(entries), has_more, next_start)

    def compare(self, path: str | os.PathLike[str], *, from_revision: str | int = "BASE",
                to_revision: str | int = "WORKING",
                max_text_bytes: int = DEFAULT_DIFF_TEXT_BYTES) -> ComparisonResult:
        left = normalize_revision(from_revision)
        right = normalize_revision(to_revision, working=True)
        if left == right == 'BASE':
            raise ValueError('BASE to BASE comparison is unsupported. Use BASE to WORKING for local changes, or explicit numeric revisions; no comparison was run.')
        if isinstance(max_text_bytes, bool) or not isinstance(max_text_bytes, int) or not 1 <= max_text_bytes <= MAX_DIFF_BYTES:
            raise ValueError(f"Comparison text limit must be between 1 and {MAX_DIFF_BYTES} bytes.")
        relative, target = self._target(path)
        revision = left if right == "WORKING" else left + ":" + right
        with self.client._lock:
            self._check_target(target)
            # Explicit old/new operands always parse peg revisions. SVN's
            # BASE-to-WORKING shortcut instead treats an appended empty peg as
            # part of the filename, and its Windows canonicalizer can trim a
            # space before a literal '@'. Both operands retain the exact path.
            # Keep operative revisions separate from peg identity so a path
            # renamed/copied after the older revision still follows ancestry.
            raw = self._run(["diff", "--old=" + relative + "@",
                             "--new=" + relative + "@", "--revision", revision],
                            MAX_DIFF_BYTES)
        decoded, undecodable, controls = decode_diff_output(raw)
        text = redact_credentials(decoded)
        encoded = text.encode("utf-8", errors="replace")
        prefix = encoded[:max_text_bytes].decode("utf-8", errors="ignore")
        field = BoundedText(prefix, len(encoded) > max_text_bytes, len(encoded), len(prefix.encode("utf-8")),
                            'utf-8-visible-escapes' if undecodable or controls else 'utf-8',
                            undecodable, controls)
        return ComparisonResult(relative, left, right, field)
