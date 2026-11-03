"""One-shot, noninteractive JSON interface for trusted local SVN workspaces.

Run ``python -m svn_client.cli --workspace /path/to/wc status``.  This is an
explicit tool interface, not an authorization system or a sandbox.  Mutations
require --yes; --dry-run performs read-only eligibility checks instead.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Sequence, TextIO

from .backend import CommandResult, SvnClient, SvnError, redact_credentials
from .shelves import ShelfConflict, ShelfError, ShelfManager

SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 1_048_576
MAX_MESSAGE_BYTES = 65_536
DEFAULT_TEXT_BYTES = 262_144
DEFAULT_OUTPUT_BYTES = 4_194_304
EXIT_CODES = {
    "ok": 0, "usage_error": 2, "confirmation_required": 3,
    "validation_failed": 4, "dependency_missing": 5, "svn_failed": 6,
    "shelf_failed": 7, "operation_uncertain": 8, "output_limit": 9,
    "internal_error": 70, "io_error": 74, "interrupted": 130,
}


class CliError(Exception):
    def __init__(self, code: str, message: str, *, details: Any = None,
                 outcome: str = "refused"):
        super().__init__(message)
        self.code, self.details, self.outcome = code, details, outcome


class HelpRequested(Exception):
    def __init__(self, text: str):
        self.text = text


class JsonParser(argparse.ArgumentParser):
    """Keep argparse's diagnostics/help inside the same JSON protocol."""
    def error(self, message: str) -> None:
        # Unknown options may include a mistakenly supplied password or token.
        # Never echo raw argv back into logs or machine responses.
        raise CliError("usage_error", "Invalid arguments. Use --help for the command syntax.")

    def print_help(self, file: TextIO | None = None) -> None:
        raise HelpRequested(self.format_help())


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def _timeout(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite positive number") from exc
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return result


def _paths(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--path", action="append", dest="paths",
                       help="Workspace-relative or absolute local path; repeat for each path")
    group.add_argument("--paths-json", metavar="FILE|-",
                       help="UTF-8 JSON array of paths; '-' explicitly reads stdin")


def _mutation(parser: argparse.ArgumentParser) -> None:
    flags = parser.add_mutually_exclusive_group()
    flags.add_argument("--yes", action="store_true", help="Explicitly authorize this mutation")
    flags.add_argument("--dry-run", action="store_true", help="Validate and show scope without mutation")
    parser.set_defaults(mutating=True)


def build_parser() -> JsonParser:
    parser = JsonParser(prog="python -m svn_client.cli", allow_abbrev=False,
                        description="SVN Desk JSON tool. Global options precede the command; no prompts.")
    parser.add_argument("--workspace", required=True, help="Existing local SVN working-copy directory")
    parser.add_argument("--timeout", type=_timeout, default=120.0,
                        help="Per SVN process timeout in seconds (default: 120)")
    parser.add_argument("--shelf-store", help="Optional shelf directory outside the workspace")
    parser.add_argument("--max-text-bytes", type=_positive_int, default=DEFAULT_TEXT_BYTES,
                        help="UTF-8 byte limit per diff/diagnostic text field")
    parser.add_argument("--max-output-bytes", type=_positive_int, default=DEFAULT_OUTPUT_BYTES,
                        help="Serialized JSON output limit (minimum 2048 bytes)")
    parser.set_defaults(mutating=False, dry_run=False, yes=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=JsonParser)
    status = commands.add_parser("status", allow_abbrev=False, help="Read local status")
    _paths(status)
    status.add_argument("--all", action="store_true", help="Include clean versioned entries")
    info = commands.add_parser("info", allow_abbrev=False, help="Read local SVN metadata")
    info.add_argument("--path", default=".")
    log = commands.add_parser("log", allow_abbrev=False, help="Read repository history (may use network)")
    log.add_argument("--path", default=".")
    log.add_argument("--limit", type=_positive_int, default=100)
    log.add_argument("--revision", help="SVN revision or range: numbers, HEAD, BASE, COMMITTED, PREV")
    diff = commands.add_parser("diff", allow_abbrev=False, help="Read SVN text diff; binary payloads are not included")
    _paths(diff)
    diff.add_argument("--revision", help="SVN revision or range: numbers, HEAD, BASE, COMMITTED, PREV")
    lists = commands.add_parser("changelist", allow_abbrev=False, help="Native local SVN file labels")
    actions = lists.add_subparsers(dest="action", required=True, parser_class=JsonParser)
    listing = actions.add_parser("list", allow_abbrev=False)
    listing.add_argument("--name", help="Return only this changelist")
    assign = actions.add_parser("assign", allow_abbrev=False)
    assign.add_argument("--name", required=True)
    _paths(assign, required=True)
    _mutation(assign)
    remove = actions.add_parser("remove", allow_abbrev=False)
    _paths(remove, required=True)
    _mutation(remove)
    commit = actions.add_parser("commit", allow_abbrev=False)
    commit.add_argument("--name", required=True)
    message = commit.add_mutually_exclusive_group(required=True)
    message.add_argument("--message")
    message.add_argument("--message-file", metavar="FILE|-", help="UTF-8 message, '-' explicitly reads stdin")
    commit.add_argument("--keep-changelist", action="store_true")
    commit.add_argument("--expect-members-sha256", help="Refuse if full membership differs from a reviewed dry-run")
    _mutation(commit)
    shelf = commands.add_parser("shelf", allow_abbrev=False, help="Verified local backups; not server-side shelves")
    actions = shelf.add_subparsers(dest="action", required=True, parser_class=JsonParser)
    actions.add_parser("list", allow_abbrev=False)
    inspect = actions.add_parser("inspect", allow_abbrev=False)
    inspect.add_argument("shelf_id")
    create = actions.add_parser("create", allow_abbrev=False)
    create.add_argument("--name", required=True)
    _paths(create, required=True)
    _mutation(create)
    for action in ("restore", "delete"):
        sub = actions.add_parser(action, allow_abbrev=False)
        sub.add_argument("shelf_id")
        _mutation(sub)
    return parser


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        # Silently redacting keys could merge distinct changelist names and
        # misrepresent scope. Fail closed rather than emit or rename such keys.
        if any(_redact(str(key)) != str(key) for key in value):
            raise CliError("validation_failed", "Metadata contains a credential-bearing name. Inspect it with a trusted SVN client before using this JSON interface.")
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _redact(value)
    return value


def _redact(value: str) -> str:
    # Share the backend's bounded linear-time URL-password scrubber. A long
    # ordinary diff line must not stall before our output limits are applied.
    return redact_credentials(value)


def _text(value: str, limit: int) -> dict[str, Any]:
    value = _redact(value)
    data = value.encode("utf-8", errors="replace")
    prefix = data[:limit].decode("utf-8", errors="ignore")
    return {"text": prefix, "truncated": len(data) > limit,
            "source_utf8_bytes": len(data), "returned_utf8_bytes": len(prefix.encode("utf-8"))}


def _result(result: CommandResult, limit: int) -> dict[str, Any]:
    # Do not expose argv: it may include commit messages, private temp paths, or
    # authentication-related details from a future backend implementation.
    return {"returncode": result.returncode, "stdout": _text(result.stdout, limit),
            "stderr": _text(result.stderr, limit)}


def _read_input(filename: str, stdin: TextIO, limit: int) -> str:
    if filename == "-":
        stream = getattr(stdin, "buffer", stdin)
        data = stream.read(limit + 1)
    else:
        if not stat.S_ISREG(os.stat(filename).st_mode):
            raise CliError("validation_failed", "Input files must be regular files, not devices, pipes, or directories.")
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(filename, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CliError("validation_failed", "Input file changed to a nonregular object before reading.")
            data = stream.read(limit + 1)
    raw = data.encode("utf-8", errors="strict") if isinstance(data, str) else data
    if len(raw) > limit:
        raise CliError("validation_failed", f"Input exceeds {limit} bytes.")
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise CliError("validation_failed", "Input must be valid UTF-8.") from exc
    if "\0" in value:
        raise CliError("validation_failed", "NUL characters are not supported in input.")
    return value


def _normalized_paths(workspace: Path, paths: Sequence[str]) -> list[str]:
    if not paths:
        raise CliError("validation_failed", "An empty path selection never means the whole workspace.")
    result = []
    for value in paths:
        if not isinstance(value, str) or not value or "\0" in value:
            raise CliError("validation_failed", "Paths must be nonempty strings without NUL characters.")
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value) or value.lower().startswith("file:"):
            raise CliError("validation_failed", "Only local paths inside --workspace are accepted.")
        absolute = Path(os.path.abspath(workspace / value))
        try:
            relative = absolute.relative_to(workspace)
            absolute.resolve().relative_to(workspace)
        except (ValueError, OSError, RuntimeError) as exc:
            raise CliError("validation_failed", "A selected path escapes --workspace.") from exc
        result.append(str(relative))
    return list(dict.fromkeys(result))


def _selected(args: argparse.Namespace, workspace: Path, stdin: TextIO) -> list[str] | None:
    if getattr(args, "paths_json", None) is not None:
        try:
            paths = json.loads(_read_input(args.paths_json, stdin, MAX_INPUT_BYTES))
        except json.JSONDecodeError as exc:
            raise CliError("validation_failed", "--paths-json must contain a JSON array of path strings.") from exc
        if not isinstance(paths, list) or len(paths) > 100_000:
            raise CliError("validation_failed", "--paths-json must be an array of at most 100000 path strings.")
        return _normalized_paths(workspace, paths)
    paths = getattr(args, "paths", None)
    return None if paths is None else _normalized_paths(workspace, paths)


def _name(value: str) -> str:
    if not value.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise CliError("validation_failed", "Names must be nonempty and contain no control characters.")
    if _redact(value) != value:
        raise CliError("validation_failed", "Names must not contain credential-bearing URLs.")
    return value


def _membership_hash(workspace: Path, name: str, paths: Sequence[str]) -> str:
    canonical = json.dumps({"workspace": str(workspace), "changelist": name,
                            "members": sorted(paths)}, ensure_ascii=True, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


class RunContext:
    def __init__(self) -> None:
        self.attempted = False
        self.command: str | None = None
        self.workspace: str | None = None
        self.dry_run = False
        self.warnings: list[str] = []

    def mutate(self, callback, *args, **kwargs):
        self.attempted = True
        return callback(*args, **kwargs)


def _execute(args: argparse.Namespace, stdin: TextIO, context: RunContext) -> Any:
    if args.max_output_bytes < 2048 or args.max_output_bytes > 67_108_864:
        raise CliError("usage_error", "--max-output-bytes must be between 2048 and 67108864.")
    if args.max_text_bytes > 67_108_864:
        raise CliError("usage_error", "--max-text-bytes must not exceed 67108864.")
    if args.mutating and not (args.yes or args.dry_run):
        raise CliError("confirmation_required", "Mutation requires --yes. Use --dry-run to inspect and validate first.")
    if not args.workspace or "\0" in args.workspace:
        raise CliError("validation_failed", "--workspace must identify an existing local directory.")
    workspace = Path(args.workspace).expanduser().resolve()
    context.workspace = str(workspace)
    if not workspace.is_dir():
        raise CliError("validation_failed", "--workspace must identify an existing local directory.")
    paths = _selected(args, workspace, stdin)
    revision = getattr(args, "revision", None)
    if revision is not None and not re.fullmatch(r"(?:[0-9]+|HEAD|BASE|COMMITTED|PREV)(?::(?:[0-9]+|HEAD|BASE|COMMITTED|PREV))?", revision):
        raise CliError("validation_failed", "Unsupported revision. Use a number, HEAD, BASE, COMMITTED, PREV, or a range of two such values.")
    if args.command == "shelf" and args.action in {"list", "inspect", "delete"}:
        # Reading/verifying/deleting local backups does not require an installed
        # SVN executable and must not accidentally create a new shelf store.
        manager = ShelfManager(workspace, storage_dir=args.shelf_store)
        if args.action == "list":
            return {"shelves": manager.list()}
        details = manager.details(args.shelf_id)
        if args.action == "inspect":
            return {"shelf": details, "blob_integrity_verified": True,
                    "restore_eligibility_checked": False}
        plan = {"shelf": details, "permanent_local_deletion": True,
                "working_copy_unchanged": True}
        if args.dry_run:
            return {"preflight": plan}
        context.mutate(manager.delete, args.shelf_id)
        return {"deleted_shelf_id": args.shelf_id, "working_copy_unchanged": True}
    try:
        client = SvnClient(workspace, timeout=args.timeout)
    except SvnError as exc:
        raise CliError("dependency_missing", str(exc)) from exc
    if args.command == "status":
        return {"entries": _plain(client.status(paths, verbose=args.all))}
    if args.command in {"info", "log"}:
        target = _normalized_paths(workspace, [args.path])[0]
        if args.command == "info":
            return {"info": client.info(target)}
        if args.limit > 10_000:
            raise CliError("validation_failed", "--limit must not exceed 10000.")
        return {"entries": _plain(client.log(args.limit, target, revision=revision))}
    if args.command == "diff":
        context.warnings.append("SVN text diff does not contain binary file payloads. Text uses the backend's decoded output; it is not a byte-for-byte file export.")
        return {"diff": _text(client.diff(paths, revision=revision), args.max_text_bytes),
                "binary_payloads_included": False}
    if args.command == "changelist":
        if args.action == "list":
            lists = client.changelists()
            if args.name is not None:
                _name(args.name)
                lists = {args.name: lists[args.name]} if args.name in lists else {}
            return {"changelists": _plain(lists)}
        if args.action in {"assign", "remove"}:
            if args.action == "assign":
                _name(args.name)
            validated = client.validate_changelist_paths(paths)
            plan = {"paths": validated, "content_unchanged": True,
                    "name": args.name if args.action == "assign" else None}
            if args.dry_run:
                return {"preflight": plan}
            result = (context.mutate(client.assign_changelist, args.name, validated)
                      if args.action == "assign" else context.mutate(client.remove_changelist, validated))
            return {**plan, "svn": _result(result, args.max_text_bytes)}
        name = _name(args.name)
        message = args.message if args.message is not None else _read_input(args.message_file, stdin, MAX_MESSAGE_BYTES)
        if not message.strip() or "\0" in message or len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise CliError("validation_failed", "Commit message must be nonempty, contain no NUL, and be at most 65536 UTF-8 bytes.")
        members = client.preflight_changelist_commit(name)
        validated = _normalized_paths(workspace, [member.path for member in members])
        digest = _membership_hash(workspace, name, validated)
        if args.expect_members_sha256 is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", args.expect_members_sha256):
                raise CliError("validation_failed", "--expect-members-sha256 must be a lowercase SHA-256 hex string.")
            if args.expect_members_sha256 != digest:
                raise CliError("validation_failed", "Changelist membership differs from the reviewed dry-run.",
                               details={"current_members_sha256": digest})
        context.warnings.append("Preflight checks local membership and eligibility, not server permissions, hooks, out-of-date state, or every possible dependency. It does not freeze file contents. Do not edit this workspace concurrently.")
        plan = {"name": name, "paths": validated, "members": _plain(members),
                "members_sha256": digest, "keep_changelist": args.keep_changelist,
                "message_utf8_bytes": len(message.encode("utf-8")),
                "server_checked": False}
        if args.dry_run:
            return {"preflight": plan}
        result = context.mutate(client.commit_changelist, name, message,
                                keep_changelist=args.keep_changelist, expected_paths=validated)
        return {**plan, "svn": _result(result, args.max_text_bytes),
                "verification": "Read status and log before deciding whether another commit is needed."}
    manager = ShelfManager(client, storage_dir=args.shelf_store)
    if args.action == "create":
        name = _name(args.name)
        if len(name) > 200:
            raise CliError("validation_failed", "Shelf names must contain at most 200 characters.")
        plan = manager.preflight_create(paths)
        if args.dry_run:
            return {"preflight": plan, "name": name, "working_copy_unchanged": True}
        return {"shelf": context.mutate(manager.create, name, paths), "working_copy_unchanged": True}
    plan = manager.preflight_restore(args.shelf_id)
    if args.dry_run:
        return {"preflight": plan}
    return {"restore": context.mutate(manager.restore, args.shelf_id)}


def _envelope(context: RunContext, *, ok: bool, outcome: str, data: Any = None,
              error: Any = None) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "ok": ok, "command": context.command,
            "workspace": context.workspace, "dry_run": context.dry_run,
            "outcome": outcome, "data": _plain(data), "error": error,
            "warnings": context.warnings}


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None,
         stdin: TextIO | None = None) -> int:
    """Write exactly one ASCII-safe JSON object and return a documented exit code.

    No stdin is read unless explicitly selected by an input-file flag. Diagnostic
    and diff text may be truncated with metadata; oversized JSON is an error, not
    a silently incomplete success. Failed/interrupted mutations are never retried.
    """
    stdout = sys.stdout if stdout is None else stdout
    stdin = sys.stdin if stdin is None else stdin
    context = RunContext()
    limit = DEFAULT_OUTPUT_BYTES
    text_limit = DEFAULT_TEXT_BYTES
    try:
        args = build_parser().parse_args(argv)
        context.command = args.command + ("." + args.action if hasattr(args, "action") else "")
        context.dry_run = args.dry_run
        # Clamp reporting configuration before validating it so invalid limits
        # cannot prevent a useful structured error from being emitted.
        limit = args.max_output_bytes if 2048 <= args.max_output_bytes <= 67_108_864 else DEFAULT_OUTPUT_BYTES
        text_limit = min(args.max_text_bytes, 67_108_864)
        data = _execute(args, stdin, context)
        payload = _envelope(context, ok=True, outcome="planned" if args.dry_run else "completed" if args.mutating else "read", data=data)
        code = 0
    except HelpRequested as exc:
        payload = _envelope(context, ok=True, outcome="read", data={"help": exc.text, "exit_codes": EXIT_CODES})
        code = 0
    except (CliError, ValueError, ShelfError, SvnError, OSError, UnicodeError, KeyboardInterrupt) as exc:
        if context.attempted:
            fault = CliError("operation_uncertain", "A mutation was started and did not finish successfully. Changes may be partial or already complete; inspect status, history, and shelves before retrying.",
                             outcome="uncertain", details={"cause": _text(str(exc) or "Interrupted", text_limit),
                                                           "automatic_retry_safe": False})
        elif isinstance(exc, CliError):
            fault = exc
        elif isinstance(exc, ShelfConflict):
            fault = CliError("validation_failed", str(exc))
        elif isinstance(exc, ShelfError):
            fault = CliError("shelf_failed", str(exc), outcome="failed")
        elif isinstance(exc, SvnError):
            fault = CliError("svn_failed", str(exc), outcome="failed")
        elif isinstance(exc, KeyboardInterrupt):
            fault = CliError("interrupted", "Interrupted before mutation.", outcome="failed")
        elif isinstance(exc, OSError):
            fault = CliError("io_error", str(exc), outcome="failed")
        else:
            fault = CliError("validation_failed", str(exc))
        code = EXIT_CODES[fault.code]
        payload = _envelope(context, ok=False, outcome=fault.outcome,
                            error={"code": fault.code, "message": _text(str(fault), text_limit)["text"],
                                   "message_truncated": _text(str(fault), text_limit)["truncated"],
                                   "details": fault.details})
    except Exception:
        # Do not leak arbitrary exception reprs, stack locals, or file contents.
        code = EXIT_CODES["operation_uncertain"] if context.attempted else EXIT_CODES["internal_error"]
        payload = _envelope(context, ok=False, outcome="uncertain" if context.attempted else "failed",
                            error={"code": "operation_uncertain" if context.attempted else "internal_error",
                                   "message": "Unexpected internal error. No automatic retry was attempted; inspect state before retrying any mutation.",
                                   "message_truncated": False, "details": {"automatic_retry_safe": False}})
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n"
    if len(serialized.encode("ascii")) > limit:
        original_outcome = payload["outcome"]
        original_error = payload["error"]["code"] if payload["error"] else None
        payload = _envelope(context, ok=False, outcome=original_outcome,
                            error={"code": "output_limit", "message": "Result exceeded --max-output-bytes. Narrow the read query or increase the limit. Do not repeat a mutation just to retrieve its output.",
                                   "message_truncated": False, "details": {"required_bytes": len(serialized.encode("ascii")), "limit_bytes": limit,
                                               "original_error_code": original_error, "original_outcome": original_outcome,
                                               "mutation_attempted": context.attempted, "automatic_retry_safe": False}})
        payload["warnings"] = []
        # Workspace and command lengths cannot make the fallback exceed its
        # own limit. Exact workspace was available in the original invocation.
        if payload["workspace"] is not None and len(payload["workspace"]) > 256:
            payload["workspace"] = None
        serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n"
        if len(serialized.encode("ascii")) > limit:
            payload["workspace"] = None
            serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n"
        code = EXIT_CODES["output_limit"]
    try:
        stdout.write(serialized)
        stdout.flush()
    except (BrokenPipeError, OSError):
        # The receiver cannot receive a replacement envelope. Never rerun work.
        return EXIT_CODES["io_error"]
    return code


if __name__ == "__main__":
    raise SystemExit(main())
