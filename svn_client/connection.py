"""Read-only, opt-in SVN connection diagnostics with no raw diagnostic output.

Only version and nonrecursive info commands are issued. Remote probes require
an explicit URL and flag; a working copy's stored URL is never followed by
default. Existing SVN authentication configuration is used without saving new
credentials, accepting certificates, retrying, or changing repository state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit
import uuid
import xml.etree.ElementTree as ET

from .backend import SvnError, discover_svn


MAX_XML_BYTES = 2_097_152
_CODES = re.compile(rb"^[ \t]*(?:svn:[ \t]*(?:warning:[ \t]*)?)?[EW]([0-9]{6}):", re.MULTILINE)
_WARNINGS = (
    "A successful info probe establishes read access at that moment, not commit, lock, checkout, or hook permissions.",
    "The timeout applies to each SVN process, not the entire report or process startup. No automatic retry is performed.",
    "Remote requests may use existing SVN credentials and appear in server logs. No password or certificate setup is performed.",
)


@dataclass(frozen=True)
class ProbeStep:
    stage: str
    status: str  # ok, failed, or skipped
    category: str
    message: str
    error_codes: tuple[str, ...] = ()
    returncode: int | None = None


@dataclass(frozen=True)
class RepositoryIdentity:
    url: str
    repository_root: str
    repository_uuid: str
    revision: int
    kind: str


@dataclass(frozen=True)
class WorkingCopyIdentity:
    path: str
    working_copy_root: str
    repository: RepositoryIdentity


@dataclass(frozen=True)
class ConnectionReport:
    ok: bool
    steps: tuple[ProbeStep, ...]
    executable: str = ""
    version: str = ""
    working_copy: WorkingCopyIdentity | None = None
    remote: RepositoryIdentity | None = None
    remote_requested: bool = False
    remote_attempted: bool = False
    repository_identity_matches: bool | None = None
    warnings: tuple[str, ...] = _WARNINGS


@dataclass(frozen=True)
class _Process:
    stdout: bytes = field(default=b"", repr=False)
    failure: ProbeStep | None = None


def validate_probe_url(url: str) -> str:
    """Validate a literal repository URL locally; do not contact or echo it.

    URL paths are literal, including '@'. The caller adds an explicit @HEAD
    peg suffix, rather than interpreting user input as a peg revision.
    """
    return _validate_url(url)


def _validate_url(url: str, *, local_metadata: bool = False) -> str:
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise ValueError("Enter a repository URL of at most 8192 characters.")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise ValueError("Repository URLs must encode whitespace and cannot contain control characters or backslashes.")
    try:
        url.encode("utf-8", errors="strict")
        parsed = urlsplit(url)
        _ = parsed.port  # Validate malformed/out-of-range ports, even when unused.
        if parsed.password is not None or (parsed.username is not None and not local_metadata):
            raise ValueError
        schemes = {"http", "https", "svn", "file", "svn+ssh"} if local_metadata else {"http", "https", "svn", "file"}
        if parsed.scheme.lower() not in schemes or "://" not in url:
            raise ValueError
        if "?" in url or "#" in url or "%" in parsed.netloc:
            raise ValueError
        if parsed.scheme.lower() == "file":
            if parsed.netloc not in {"", "localhost"} or not parsed.path.startswith("/"):
                raise ValueError
        elif not parsed.hostname or not parsed.netloc or (":" in parsed.netloc and parsed.netloc.endswith(":")):
            raise ValueError
        if re.search(r"%(?![0-9a-fA-F]{2})", url):
            raise ValueError
        decoded = unquote(url, encoding="utf-8", errors="strict")
        if any(ord(c) < 32 or ord(c) == 127 for c in decoded) or "\\" in decoded:
            raise ValueError
        if parsed.scheme.lower() == "file" and unquote(parsed.path, encoding="utf-8", errors="strict").startswith("//"):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("Use an http, https, svn, or local file URL without userinfo, query, fragment, malformed escapes, or encoded controls. SSH and remote file hosts are unsupported.") from None
    # A local-only read may inspect SSH metadata without launching SSH. Remove
    # username-only userinfo from the display identity; never return passwords
    # or silently turn that metadata into a remote request.
    return parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[-1]).geturl() if local_metadata and parsed.username is not None else url


def _failure(stage: str, output: bytes, returncode: int) -> ProbeStep:
    # Numeric error codes are stable across localized diagnostics. Do not emit
    # raw stderr/stdout, URLs from error text, certificate data, or lock tokens.
    codes = tuple(dict.fromkeys(match.decode("ascii") for match in _CODES.findall(output[:MAX_XML_BYTES])))[:16]
    numbers = {int(code) for code in codes}
    if numbers & {120170, 120171, 120172, 120174, 120175, 120176, 230001} or any(240000 <= n <= 240019 for n in numbers):
        category, message = "tls", "TLS or certificate validation failed. Check the server certificate and trusted SVN configuration; no bypass or certificate acceptance was attempted."
    elif any(215000 <= n <= 215004 for n in numbers) or numbers & {120190, 120191, 120192, 120193, 120194, 170002, 210007}:
        category, message = "authentication", "SVN could not authenticate using its existing configuration. Configure the account with a trusted SVN client, then request another probe."
    elif numbers & {170001, 175013, 195023, 220000, 220001, 220002, 220003, 220004}:
        category, message = "authorization", "SVN reported an access or authorization failure. Confirm the account and repository permissions with its administrator."
    elif numbers & {175012, 120112}:
        category, message = "timeout", "SVN reported a connection timeout. Check reachability before requesting another probe."
    elif numbers & {120101, 120102, 120107, 120108, 175000, 210002, 210003}:
        category, message = "network", "SVN reported a network or transport failure. Check the address, connection, and proxy configuration."
    elif numbers & {155007, 155010, 155021, 155032, 155033, 155036}:
        category, message = "working_copy", "SVN could not read the working copy or its format. Verify the selected folder using a trusted SVN client."
    elif numbers & {160013, 175007, 180000, 210005, 200009}:
        category, message = "not_found", "The requested SVN path or repository could not be found. Access restrictions may also hide repository paths."
    elif numbers & {170013, 175002, 175003}:
        category, message = "connection", "SVN could not establish a usable repository session. The returned codes do not reliably distinguish network, proxy, authentication, and server failures."
    else:
        category, message = "svn_error", "SVN could not complete this read-only check. The cause is not recognized; inspect it separately in a trusted SVN client."
    return ProbeStep(stage, "failed", category, message, codes, returncode)


def _run(executable: str, stage: str, args: list[str], cwd: Path | None, timeout: float) -> _Process:
    env = os.environ.copy()
    if os.name != "nt":
        env.setdefault("LC_CTYPE", "C.UTF-8")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        result = subprocess.run([executable, *args], cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                timeout=timeout, env=env, **options)
    except subprocess.TimeoutExpired:
        return _Process(failure=ProbeStep(stage, "failed", "timeout", "The read-only SVN check exceeded its process timeout. No automatic retry was performed."))
    except OSError:
        return _Process(failure=ProbeStep(stage, "failed", "process_error", "The selected SVN executable could not be started or its working directory became unavailable."))
    if result.returncode:
        return _Process(failure=_failure(stage, result.stderr, result.returncode))
    if len(result.stdout) > MAX_XML_BYTES:
        return _Process(failure=ProbeStep(stage, "failed", "invalid_response", "SVN returned more metadata than this diagnostic accepts."))
    return _Process(result.stdout)


def _identity(data: bytes, *, local_metadata: bool = False) -> tuple[RepositoryIdentity, str]:
    text = data.decode("utf-8-sig", errors="strict")
    if "\0" in text or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("Unsupported XML declaration")
    if text.lstrip().startswith("<?xml"):
        declaration = text.split("?>", 1)[0]
        encoding = re.search(r"\bencoding\s*=\s*(['\"])(.*?)\1", declaration, flags=re.IGNORECASE)
        if encoding and encoding.group(2).lower() not in {"utf-8", "utf8"}:
            raise ValueError("SVN metadata must use UTF-8")
    root = ET.fromstring(text)
    entries = root.findall("entry")
    if root.tag != "info" or len(entries) != 1:
        raise ValueError("Expected one info entry")
    entry = entries[0]
    kind = entry.get("kind", "")
    revision_text = entry.get("revision", "")
    if kind not in {"file", "dir"} or not re.fullmatch(r"[0-9]{1,20}", revision_text):
        raise ValueError("Invalid entry identity")
    revision = int(revision_text)
    identity = RepositoryIdentity(
        _validate_url(entry.findtext("url", ""), local_metadata=local_metadata),
        _validate_url(entry.findtext("repository/root", ""), local_metadata=local_metadata),
        str(uuid.UUID(entry.findtext("repository/uuid", ""))), revision, kind,
    )
    # Never copy commit author, arbitrary properties, lock tokens, certificate
    # details, or raw XML into a public result.
    return identity, entry.findtext("wc-info/wcroot-abspath", "")


class ConnectionDiagnostics:
    def __init__(self, working_copy: str | os.PathLike[str] | None = None, *,
                 executable: str | os.PathLike[str] | None = None, timeout: float = 15):
        try:
            valid = not isinstance(timeout, bool) and isinstance(timeout, (int, float)) and math.isfinite(timeout) and 0 < timeout <= 120
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("Diagnostic timeout must be a finite number greater than zero and at most 120 seconds.")
        self.working_copy, self.executable, self.timeout = working_copy, executable, float(timeout)

    def probe(self, *, url: str | None = None, check_remote: bool = False) -> ConnectionReport:
        """Check requested stages once. Invalid requests raise safe ValueError.

        Without check_remote=True, no stored or supplied URL is contacted.
        check_remote requires an explicit URL, treated as a literal HEAD path.
        Operational failures are returned as steps, not raw SVN exceptions.
        """
        if type(check_remote) is not bool or (url is not None and not check_remote) or (check_remote and url is None):
            raise ValueError("A remote probe requires both check_remote=True and an explicit repository URL.")
        target = validate_probe_url(url) if check_remote else None
        steps: list[ProbeStep] = []
        local: WorkingCopyIdentity | None = None
        remote: RepositoryIdentity | None = None
        selected: Path | None = None
        if self.working_copy is not None:
            try:
                raw = os.fspath(self.working_copy)
                if not isinstance(raw, str) or not raw or "://" in raw or any(ord(c) < 32 or ord(c) == 127 for c in raw):
                    raise ValueError
                selected = Path(raw).expanduser().resolve()
                if not selected.is_dir():
                    raise ValueError
            except (TypeError, ValueError, OSError, RuntimeError):
                steps.append(ProbeStep("working_copy", "failed", "working_copy", "Select an existing local working-copy directory."))
                selected = None
        try:
            executable = discover_svn(self.executable)
        except (SvnError, ValueError, TypeError, OSError):
            steps.insert(0, ProbeStep("executable", "failed", "dependency_missing", "The trusted SVN command-line executable was not found. Install or select it separately."))
            if self.working_copy is not None and selected is not None:
                steps.append(ProbeStep("working_copy", "skipped", "dependency_missing", "Working-copy inspection requires the SVN executable."))
            if check_remote:
                steps.append(ProbeStep("remote", "skipped", "dependency_missing", "No repository request was made because SVN is unavailable."))
            return ConnectionReport(False, tuple(steps), remote_requested=check_remote)
        version_result = _run(executable, "executable", ["--version", "--quiet"], None, self.timeout)
        version = ""
        if version_result.failure is None:
            try:
                version = version_result.stdout.decode("ascii").strip()
                if len(version) > 100 or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version):
                    raise ValueError
            except (UnicodeError, ValueError):
                version_result = _Process(failure=ProbeStep("executable", "failed", "invalid_response", "The selected executable did not return a recognizable SVN version."))
                version = ""
        steps.insert(0, version_result.failure or ProbeStep("executable", "ok", "ok", "The installed SVN executable returned its version."))
        if version_result.failure:
            if self.working_copy is not None and selected is not None:
                steps.append(ProbeStep("working_copy", "skipped", "dependency_failed", "Working-copy inspection was not started after the executable check failed."))
            if check_remote:
                steps.append(ProbeStep("remote", "skipped", "dependency_failed", "No repository request was made after the executable check failed."))
            return ConnectionReport(False, tuple(steps), executable, remote_requested=check_remote)
        options = ["info", "--non-interactive", "--no-auth-cache", "--xml", "--depth", "empty", "--"]
        if selected is not None:
            result = _run(executable, "working_copy", [*options, "."], selected, self.timeout)
            if result.failure:
                steps.append(result.failure)
            else:
                try:
                    identity, wcroot = _identity(result.stdout, local_metadata=True)
                    if not wcroot or not Path(wcroot).is_absolute() or any(ord(c) < 32 or ord(c) == 127 for c in wcroot):
                        raise ValueError
                    actual_root = Path(wcroot).resolve()
                    if selected != actual_root and actual_root not in selected.parents:
                        raise ValueError
                    if identity.kind != "dir":
                        raise ValueError
                    local = WorkingCopyIdentity(str(selected), str(actual_root), identity)
                    steps.append(ProbeStep("working_copy", "ok", "ok", "Local working-copy identity was read without contacting its repository."))
                except (ValueError, OSError, RuntimeError, ET.ParseError):
                    steps.append(ProbeStep("working_copy", "failed", "invalid_response", "SVN returned incomplete or unsafe working-copy identity metadata."))
        attempted = False
        if check_remote:
            attempted = True
            result = _run(executable, "remote", [*options, target + "@HEAD"], None, self.timeout)
            if result.failure:
                steps.append(result.failure)
            else:
                try:
                    remote, _ = _identity(result.stdout)
                    steps.append(ProbeStep("remote", "ok", "ok", "SVN read the explicitly selected repository URL at HEAD."))
                except (ValueError, ET.ParseError):
                    steps.append(ProbeStep("remote", "failed", "invalid_response", "SVN returned incomplete or unsafe repository identity metadata."))
        match = None
        if local is not None and remote is not None:
            match = local.repository.repository_uuid == remote.repository_uuid
            steps.append(ProbeStep("identity", "ok" if match else "failed", "ok" if match else "identity_mismatch",
                                   "Local and remote repository UUIDs match; this does not prove identical paths or current content." if match else "The explicitly probed URL belongs to a different repository UUID. No switch or relocation was attempted."))
        return ConnectionReport(all(step.status == "ok" for step in steps), tuple(steps), executable,
                                version, local, remote, check_remote, attempted, match)
