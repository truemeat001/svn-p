"""Bounded transport and incremental parser for read-only ``svn status --xml``.

Only the final StatusEntry list grows with the working copy. XML tree nodes,
pipe buffering and diagnostics do not accumulate with the complete output.
The caller receives no result until XML, EOF, zero exit and empty stderr are verified.
"""
from __future__ import annotations

from contextlib import contextmanager
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from typing import Callable, Iterable, Sequence
import xml.etree.ElementTree as ET

from .backend import CommandResult, StatusEntry, SvnError, _int, redact_credentials

CHUNK_BYTES = 64 * 1024
PIPE_QUEUE_CHUNKS = 4
DIAGNOSTIC_BYTES = 64 * 1024
MAX_XML_BYTES = 512 * 1024 * 1024
MAX_ENTRY_BYTES = 4 * 1024 * 1024
MAX_XML_DEPTH = 64
# Checked by the web bridge along with backend.STATUS_DIAGNOSTICS_POLICY.
STATUS_DIAGNOSTICS_POLICY = 1


class StatusXmlParser:
    """Incrementally retain entries, removing completed XML from its parent.

    The complete result necessarily uses O(number of entries) memory. The
    parser rejects excessive output, oversized records, deep nesting, and DTDs
    rather than silently returning a prefix of an incomplete status snapshot.
    """

    def __init__(self, relative: Callable[[str], str], *,
                 max_bytes: int = MAX_XML_BYTES, max_entry_bytes: int = MAX_ENTRY_BYTES):
        self.relative = relative
        self.max_bytes = max_bytes
        self.max_entry_bytes = max_entry_bytes
        self._parser = ET.XMLPullParser(events=("start", "end"))
        self._stack: list[ET.Element] = []
        self._results: list[StatusEntry] = []
        self._bytes = 0
        self._pending_bytes = 0
        self._markup_tail = b""
        self._seen_root = False
        self._in_entry = False
        self._closed = False

    def feed(self, chunk: bytes | str) -> None:
        if self._closed:
            raise ValueError("Status XML parser is already closed.")
        # The legacy injected-command adapter may supply Unicode. Encode only
        # this bounded chunk, never a second copy of the complete XML document.
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        self._bytes += len(chunk)
        self._pending_bytes += len(chunk)
        if self._bytes > self.max_bytes:
            raise SvnError("SVN status XML exceeded the output limit; no partial status was returned.")
        if self._pending_bytes > self.max_entry_bytes:
            raise SvnError("SVN status XML contains an oversized record; no partial status was returned.")
        # SVN XML is UTF-8. NUL rejection also prevents an alternative UTF-16
        # encoding from hiding a DTD from this bounded lexical guard.
        markup = self._markup_tail + chunk
        if b"\0" in chunk or b"<!DOCTYPE" in markup or b"<!ENTITY" in markup:
            raise SvnError("SVN returned unsupported XML declarations or encoding; no status was returned.")
        self._markup_tail = markup[-8:]
        try:
            self._parser.feed(chunk)
            self._drain()
        except ET.ParseError as error:
            raise SvnError("SVN returned invalid XML; verify that the selected executable is Subversion.") from error

    def _drain(self) -> None:
        for event, element in self._parser.read_events():
            if event == "start":
                if not self._seen_root:
                    if element.tag != "status":
                        raise SvnError("SVN returned invalid status XML; expected a status document.")
                    self._seen_root = True
                if element.tag == "entry":
                    if self._in_entry:
                        raise SvnError("SVN returned invalid status XML with nested entries.")
                    self._in_entry = True
                self._stack.append(element)
                if len(self._stack) > MAX_XML_DEPTH:
                    raise SvnError("SVN status XML nesting exceeded the safe limit.")
                continue
            parent = self._stack[-2] if len(self._stack) > 1 else None
            if element.tag == "entry":
                self._in_entry = False
                self._append(element, parent)
                self._pending_bytes = 0
                if parent is not None:
                    parent.remove(element)
                element.clear()
            elif not self._in_entry:
                if parent is not None:
                    parent.remove(element)
                element.clear()
            self._stack.pop()

    def _append(self, entry: ET.Element, parent: ET.Element | None) -> None:
        status = entry.find("wc-status")
        if status is None:
            return
        commit = status.find("commit")
        self._results.append(StatusEntry(
            path=self.relative(entry.get("path", "")), item=status.get("item", "none"),
            props=status.get("props", "none"), revision=_int(status.get("revision")),
            tree_conflicted=status.get("tree-conflicted") == "true",
            copied=status.get("copied") == "true", switched=status.get("switched") == "true",
            wc_locked=status.get("wc-locked") == "true", moved_from=status.get("moved-from"),
            moved_to=status.get("moved-to"),
            changelist=parent.get("name") if parent is not None and parent.tag == "changelist" else None,
            file_external=status.get("file-external") == "true",
            last_changed_revision=_int(commit.get("revision")) if commit is not None else None,
            last_changed_author=status.findtext("commit/author", ""),
        ))

    def finish(self) -> list[StatusEntry]:
        if self._closed:
            raise ValueError("Status XML parser is already closed.")
        self._closed = True
        try:
            self._parser.close()
            self._drain()
        except ET.ParseError as error:
            raise SvnError("SVN returned invalid XML; verify that the selected executable is Subversion.") from error
        if not self._seen_root or self._stack:
            raise SvnError("SVN returned incomplete status XML; no partial status was returned.")
        return self._results


def parse_status_chunks(chunks: Iterable[bytes | str], relative: Callable[[str], str],
                        *, cancel_event: threading.Event | None = None) -> list[StatusEntry]:
    parser = StatusXmlParser(relative)
    for chunk in chunks:
        _check_cancel(cancel_event)
        parser.feed(chunk)
    _check_cancel(cancel_event)
    return parser.finish()


def text_chunks(text: str) -> Iterable[str]:
    """Compatibility with custom command transports that already return text."""
    for offset in range(0, len(text), CHUNK_BYTES):
        yield text[offset:offset + CHUNK_BYTES]


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise SvnError("SVN status was cancelled; no partial status was returned.")


def _validate_timeout(timeout: float) -> None:
    try:
        valid = (not isinstance(timeout, bool) and isinstance(timeout, (int, float))
                 and math.isfinite(timeout) and timeout > 0)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Timeout must be a finite positive number.")


@contextmanager
def status_lock(lock, timeout: float, cancel_event: threading.Event | None):
    """Bound queue wait as well as execution, without losing RLock reentrancy."""
    _validate_timeout(timeout)
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while not acquired:
            _check_cancel(cancel_event)
            left = deadline - time.monotonic()
            if left <= 0:
                raise SvnError("SVN status timed out waiting for the working-copy lock; no command was started.")
            acquired = lock.acquire(timeout=min(left, 0.05))
        _check_cancel(cancel_event)
        left = deadline - time.monotonic()
        if left <= 0:
            raise SvnError("SVN status timed out waiting for the working-copy lock; no command was started.")
        yield left
    finally:
        if acquired:
            lock.release()


def _diagnostic(value: bytes, truncated: bool, decode: Callable[[bytes], str]) -> str:
    text = redact_credentials(decode(value))
    if truncated:
        # A bounded prefix may end inside user:password@host. Redact unfinished
        # userinfo too, before attaching the explicit truncation marker.
        text = re.sub(r"(://[^/\s:?#]*:)[^/\s?#]*$", r"\1***", text)
        text += "\n[SVN diagnostic output truncated]"
    return text


def require_status_success(returncode: int, stderr: str, display_args: tuple[str, ...],
                           *, had_stderr: bool = False) -> None:
    """Accept only a diagnostic-free status process, never just exit code zero.

    W155007 (not a working copy) and other warnings can coexist with valid XML
    and exit 0. Any diagnostic makes completeness unknown. Callers must provide
    bounded, redacted stderr and never attach the full stdout XML to an error.
    had_stderr also preserves a byte-level diagnostic that decoding may erase.
    """
    result = CommandResult(display_args, returncode, "", stderr)
    if returncode:
        detail = stderr.strip() or "No diagnostic output."
        raise SvnError(f"SVN exited with code {returncode}: {detail}", result)
    if had_stderr or stderr:
        detail = stderr.strip() or "SVN wrote diagnostic bytes to stderr."
        raise SvnError(
            "SVN status emitted diagnostic output despite exit code 0; "
            f"no complete status was returned: {detail}", result)


def check_status_command_result(result: CommandResult, display_args: tuple[str, ...]) -> None:
    """Apply the same failure rule to an already-buffered custom transport."""
    # Bound before encoding: a custom transport may already hold a very large
    # string, but error construction must not duplicate all of it.
    prefix = result.stderr[:DIAGNOSTIC_BYTES].encode("utf-8", errors="replace")
    truncated = len(result.stderr) > DIAGNOSTIC_BYTES or len(prefix) > DIAGNOSTIC_BYTES
    diagnostic = _diagnostic(prefix[:DIAGNOSTIC_BYTES], truncated,
                             lambda value: value.decode("utf-8", errors="replace"))
    require_status_success(result.returncode, diagnostic, display_args,
                           had_stderr=bool(result.stderr))


def run_status(argv: Sequence[str], *, cwd: Path, timeout: float,
               relative: Callable[[str], str], display_args: tuple[str, ...],
               decode: Callable[[bytes], str], cancel_event: threading.Event | None = None,
               max_bytes: int = MAX_XML_BYTES) -> list[StatusEntry]:
    """Run a trusted SVN command without buffering its complete XML/diagnostics.

    Dedicated pipe readers avoid Windows select limitations and stderr/stdout
    deadlocks. The stdout queue has fixed capacity and applies backpressure.
    All exceptional exits kill/reap the process; readers are daemonized only as
    a final safeguard against an unrelated descendant retaining a pipe handle.
    """
    _validate_timeout(timeout)
    _check_cancel(cancel_event)
    environment = os.environ.copy()
    if os.name != "nt":
        environment.setdefault("LC_CTYPE", "C.UTF-8")
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        process = subprocess.Popen(
            list(argv), cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, shell=False, env=environment, bufsize=0, **kwargs,
        )
    except OSError as error:
        raise SvnError(f"Could not start SVN: {error}") from error
    deadline = time.monotonic() + timeout
    stop = threading.Event()
    output: queue.Queue[bytes | None] = queue.Queue(maxsize=PIPE_QUEUE_CHUNKS)
    diagnostics = bytearray()
    stderr_truncated = False
    errors: list[Exception] = []
    stderr_done = threading.Event()

    def offer(chunk: bytes | None) -> None:
        while not stop.is_set():
            try:
                output.put(chunk, timeout=0.05)
                return
            except queue.Full:
                continue

    def read_stdout() -> None:
        try:
            while not stop.is_set():
                chunk = process.stdout.read(CHUNK_BYTES)
                if not chunk:
                    break
                offer(chunk)
        except (OSError, ValueError) as error:
            if not stop.is_set():
                errors.append(error)
        finally:
            offer(None)

    def read_stderr() -> None:
        nonlocal stderr_truncated
        try:
            while not stop.is_set():
                chunk = process.stderr.read(CHUNK_BYTES)
                if not chunk:
                    break
                available = DIAGNOSTIC_BYTES - len(diagnostics)
                diagnostics.extend(chunk[:available])
                stderr_truncated |= len(chunk) > available
        except (OSError, ValueError) as error:
            if not stop.is_set():
                errors.append(error)
        finally:
            stderr_done.set()

    readers = [threading.Thread(target=read_stdout, name="svn-status-stdout", daemon=True),
               threading.Thread(target=read_stderr, name="svn-status-stderr", daemon=True)]
    parser = StatusXmlParser(relative, max_bytes=max_bytes)
    parse_error: SvnError | None = None
    output_bytes = 0

    def remaining() -> float:
        _check_cancel(cancel_event)
        left = deadline - time.monotonic()
        if left <= 0:
            raise SvnError(
                f"SVN status read timed out after {timeout:g} seconds; no complete status was returned. "
                "No write operation was requested."
            )
        return min(left, 0.05)

    try:
        for reader in readers:
            reader.start()
        while True:
            try:
                chunk = output.get(timeout=remaining())
            except queue.Empty:
                continue
            if chunk is None:
                break
            output_bytes += len(chunk)
            if output_bytes > max_bytes:
                raise SvnError("SVN status XML exceeded the output limit; no partial status was returned.")
            if parse_error is None:
                try:
                    parser.feed(chunk)
                except SvnError as error:
                    # Malformed XML often accompanies an SVN error. Drain only
                    # to collect the bounded diagnostic and exit status. Limits
                    # still terminate immediately rather than drain forever.
                    parse_error = error
                    if "limit" in str(error) or "oversized" in str(error):
                        raise
        while process.poll() is None:
            try:
                process.wait(timeout=remaining())
            except subprocess.TimeoutExpired:
                continue
        while not stderr_done.is_set():
            stderr_done.wait(remaining())
        _check_cancel(cancel_event)
        if errors:
            raise SvnError(f"Could not read SVN status output: {errors[0]}")
        stderr = _diagnostic(bytes(diagnostics), stderr_truncated, decode)
        require_status_success(process.returncode, stderr, display_args,
                               had_stderr=bool(diagnostics) or stderr_truncated)
        if parse_error is not None:
            raise parse_error
        return parser.finish()
    finally:
        stop.set()
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # Do not turn timeout/cancellation into an unbounded cleanup wait.
            pass
        for reader in readers:
            if reader.ident is not None:
                reader.join(timeout=0.2)
        process.stdout.close()
        process.stderr.close()
