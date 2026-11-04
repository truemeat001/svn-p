#!/usr/bin/env python3
"""Loopback SVN Desk bridge; optional confirmed labels, Shelf and repository commits."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import hashlib
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import sys
import threading
import time
from urllib.parse import urlsplit
from workspace_bridge import WorkspaceCatalog, local_path, working_copy_identity, trusted_modules
from file_reads import (FileReadService, StaleRead, UnsupportedComparison,
                        comparison_revisions, history_module, revision)

from changelist_ops import (ChangelistService, ChangelistUnavailable, MutationUncertain,
                            validate_request, status_rows, visible_rows)

from commit_ops import (CommitService, CommitUnavailable, validate_request as validate_commit_request)

from shelf_ops import (ShelfService, ShelfUnavailable, guarded_manager, shelf_module,
                       validate_request as validate_shelf_request)

ROOT = Path(__file__).resolve().parent
ASSETS = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js",
          "/model.js": "model.js", "/demo-data.js": "demo-data.js", "/styles.css": "styles.css", "/favicon.svg": "favicon.svg"}
MIMES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
MAX_ROWS = 50000
MAX_REQUEST_BODY_BYTES = 262144
REQUEST_BODY_SECONDS = 5.0
REQUEST_BODY_CHUNK = 64 * 1024

COMMIT_BACKEND_SHA256 = '4ba9cf1ce8973dd8473db2d7732b83152e6ce63e1ea124b601ac6cf27c55997d'
COMMIT_HISTORY_SHA256 = '6b8614308da1e978837518da9feb32f9694c57559dd368f3eca1e654abdf29fc'


def reviewed_commit_backend(backend, history):
    """Unknown paired semantics disable commits; CRLF checkouts retain Python semantics."""
    try:
        for module, expected in ((backend, COMMIT_BACKEND_SHA256), (history, COMMIT_HISTORY_SHA256)):
            source = Path(module.__file__)
            if source.stat().st_size > 1024 * 1024 or hashlib.sha256(source.read_bytes().replace(b'\r\n', b'\n')).hexdigest() != expected:
                return False
        return True
    except (AttributeError, TypeError, ValueError, OSError):
        return False


class ReadOnlyAdapter:
    """Imports trusted bounded services. Never exposes a generic SVN command."""
    def __init__(self, workspace: Path, backend_source: Path, executable: str | None = None, *, enable_changelists=False, enable_shelves=False, shelf_storage=None, enable_commits=False):
        self.workspace = local_path(workspace)
        self._identity = working_copy_identity(self.workspace)
        backend, status_stream, _, _ = trusted_modules(backend_source)
        if (getattr(backend, 'STATUS_DIAGNOSTICS_POLICY', None) != 1
                or getattr(status_stream, 'STATUS_DIAGNOSTICS_POLICY', None) != 1):
            raise ValueError("Update the trusted backend: strict status diagnostics policy is required.")
        self.client = backend.SvnClient(self.workspace, executable=executable, timeout=25)
        history = history_module(backend_source)
        self._file_service = FileReadService(self.client, history) if history else None
        self._changelist_service = (ChangelistService(self.client, self._file_service.manager, self._check_workspace)
                                   if enable_changelists and self._file_service else None)
        self._commit_service = (CommitService(self.client, self._file_service.manager, self._check_workspace)
                                if enable_commits and self._file_service and reviewed_commit_backend(backend, history) else None)
        shelves = shelf_module(backend_source) if enable_shelves and history else None
        self._shelf_service = (ShelfService(guarded_manager(shelves, history, self.client,
                                self._check_workspace, shelf_storage), self._check_workspace)
                               if shelves else None)
        if self._shelf_service:
            self._shelf_service.status_client = self.client
        self._read_snapshot = None
        self._check_workspace()
        self._busy = threading.Lock()

    def _check_workspace(self):
        if working_copy_identity(self.workspace) != self._identity:
            raise ValueError("Working copy changed since opening.")

    def snapshot(self):
        if not self._busy.acquire(blocking=False):
            raise BlockingIOError("A status read is already running.")
        try:
            self._read_snapshot = None  # Refresh invalidates old capabilities even on failure.
            commit_service = getattr(self, '_commit_service', None)
            if commit_service:
                commit_service.invalidate()
            service = getattr(self, '_changelist_service', None)
            if service:
                service.invalidate()
            shelf_service = getattr(self, '_shelf_service', None)
            if shelf_service:
                shelf_service.invalidate()
            self._check_workspace()
            # No --show-updates, remote info/list/log, shell, diff or writes.
            if service or commit_service:
                rows = visible_rows(status_rows(self.client))
                self._check_workspace()
                return self._publish_snapshot(rows)
            entries = self.client.status(verbose=False)
            self._check_workspace()
            if len(entries) > MAX_ROWS:
                raise ValueError("Status exceeds prototype row limit; no partial snapshot returned.")
            rows = []
            for entry in entries:
                row = asdict(entry)
                # UI receives metadata only. It cannot request arbitrary paths.
                row["id"] = row["path"]
                row["path"] = row["path"].replace(os.sep, "/")  # UI display only; preserve native ID.
                row["kind"] = "unknown"  # status XML does not reliably identify node kinds.
                rows.append(row)
            if len({row['id'] for row in rows}) != len(rows):
                raise ValueError("Duplicate status IDs are not safe read capabilities.")
            return self._publish_snapshot(rows)
        finally:
            self._busy.release()

    def _publish_snapshot(self, rows):
        token = secrets.token_urlsafe(24)
        self._read_snapshot = (token, {row['id']: dict(row) for row in rows})
        return {"mode": "live", "workspace": self.workspace.name,
                "root": str(self.workspace), "rows": rows,
                "snapshotToken": token, "fileReadsEnabled": getattr(self, '_file_service', None) is not None,
                "changelistWritesEnabled": getattr(self, '_changelist_service', None) is not None,
                "shelfWritesEnabled": getattr(self, '_shelf_service', None) is not None,
                "commitsEnabled": getattr(self, '_commit_service', None) is not None,
                "readAt": datetime.now(timezone.utc).isoformat(),
                "scope": "Local status only; repository connectivity not tested."}

    def changelist_operation(self, kind, data):
        if not self._busy.acquire(blocking=False):
            raise BlockingIOError('A workspace operation is already running.')
        try:
            service = getattr(self, '_changelist_service', None)
            if service is None:
                raise ChangelistUnavailable('Local changelist writes are not enabled.')
            self._check_workspace()
            if kind == 'preflight':
                payload = service.preflight(data, self._read_snapshot)
                try:
                    self._check_workspace()
                except Exception:
                    service.invalidate()
                    raise
                return payload
            def invalidate():
                self._read_snapshot = None
                if getattr(self, '_commit_service', None):
                    self._commit_service.invalidate()
                if getattr(self, '_shelf_service', None):
                    self._shelf_service.invalidate()
            result, rows = service.apply(data, self._read_snapshot, invalidate)
            try:
                self._check_workspace()
                return {**self._publish_snapshot(visible_rows(rows)), 'changelistResult': result}
            except Exception as error:
                self._read_snapshot = None
                raise MutationUncertain('Post-write workspace validation failed.') from error
        finally:
            self._busy.release()

    def shelf_operation(self, kind, data):
        if not self._busy.acquire(blocking=False):
            raise BlockingIOError('A workspace operation is already running.')
        try:
            service = getattr(self, '_shelf_service', None)
            if service is None:
                raise ShelfUnavailable('The paired local Shelf workflow is not enabled.')
            self._check_workspace()
            if kind in {'list', 'preflight'}:
                result = (service.listing(data, self._read_snapshot) if kind == 'list'
                          else service.preflight(data, self._read_snapshot))
                try:
                    self._check_workspace()
                except Exception:
                    service.invalidate()
                    raise
                return result
            def invalidate():
                self._read_snapshot = None
                if getattr(self, '_commit_service', None):
                    self._commit_service.invalidate()
                if getattr(self, '_changelist_service', None):
                    self._changelist_service.invalidate()
            result = service.apply(data, self._read_snapshot, invalidate)
            try:
                rows = visible_rows(status_rows(self.client))
                self._check_workspace()
                return {**self._publish_snapshot(rows), 'shelfResult': result}
            except Exception as error:
                self._read_snapshot = None
                raise MutationUncertain('Post-Shelf status could not be verified.') from error
        finally:
            self._busy.release()

    def _commit_status_unknown(self, result, warning):
        """A failed follow-up read must not erase an already completed command."""
        self._read_snapshot = None
        for name in ('_changelist_service', '_shelf_service', '_commit_service'):
            service = getattr(self, name, None)
            if service:
                service.invalidate()
        warnings = list(dict.fromkeys([*result.get('warnings', []), warning]))
        return {'mode': 'live', 'workspace': self.workspace.name, 'root': str(self.workspace),
                'rows': [], 'snapshotToken': None, 'statusRefreshed': False,
                'fileReadsEnabled': False, 'changelistWritesEnabled': False,
                'shelfWritesEnabled': False, 'commitsEnabled': False, 'writesEnabled': False,
                'commitResult': {**result, 'warnings': warnings},
                'scope': 'Command completed; subsequent local status could not be verified.'}

    def commit_operation(self, kind, data):
        if not self._busy.acquire(blocking=False):
            raise BlockingIOError('A workspace operation is already running.')
        try:
            service = getattr(self, '_commit_service', None)
            if service is None:
                raise CommitUnavailable('Repository commits are not enabled.')
            self._check_workspace()
            if kind == 'preflight':
                payload = service.preflight(data, self._read_snapshot)
                try:
                    self._check_workspace()
                except Exception:
                    service.invalidate()
                    raise
                return payload
            def invalidate():
                self._read_snapshot = None
                for name in ('_changelist_service', '_shelf_service', '_commit_service'):
                    current = getattr(self, name, None)
                    if current:
                        current.invalidate()
            result = service.apply(data, self._read_snapshot, invalidate)
            try:
                rows = visible_rows(status_rows(self.client))
                self._check_workspace()
                return {**self._publish_snapshot(rows), 'commitResult': result, 'statusRefreshed': True}
            except Exception:
                return self._commit_status_unknown(result, 'status-refresh-failed')
        finally:
            self._busy.release()

    def file_read(self, kind, data):
        if not self._busy.acquire(blocking=False):
            raise BlockingIOError('A read is already running.')
        try:
            self._check_workspace()
            snapshot = self._read_snapshot
            if snapshot is None or data['snapshotToken'] != snapshot[0]:
                raise StaleRead('Status capability expired.')
            row = snapshot[1].get(data['id'])
            if row is None:
                raise StaleRead('Row is not in the current status snapshot.')
            if self._file_service is None:
                raise ValueError('A strict bounded history backend is required.')
            result = self._file_service.read(kind, row, data)
            self._check_workspace()
            return {**result, 'id': data['id'], 'snapshotToken': snapshot[0]}
        finally:
            self._busy.release()

class PrototypeServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, port=8765, adapter=None, catalog=None):
        super().__init__(("127.0.0.1", port), Handler)
        self.token = secrets.token_urlsafe(32)
        self.adapter = adapter
        self.catalog = catalog
        self.origin = f"http://127.0.0.1:{self.server_port}"

class Handler(BaseHTTPRequestHandler):
    server: PrototypeServer
    timeout = 5.0  # Also bound idle request-line/header reads; body has a total deadline.

    def _request_body_size(self, *, optional=False):
        lengths = self.headers.get_all('Content-Length')
        if self.headers.get('Transfer-Encoding') is not None:
            raise ValueError('Transfer encoding is not supported.')
        if lengths is None and optional:
            return 0
        if lengths is None or len(lengths) != 1:
            raise ValueError('One Content-Length is required.')
        value = lengths[0]
        if not re.fullmatch(r'0|[1-9][0-9]{0,19}', value):
            raise ValueError('Content-Length must be one canonical decimal integer.')
        return int(value)

    def _read_request_body(self, limit, *, discard=False, optional=False):
        # Mark before inspecting framing: an invalid/partial body must never be
        # retried by respond(), or parsed later as another HTTP request.
        self._body_attempted = True
        size = self._request_body_size(optional=optional)
        if size > limit:
            raise ValueError('Request body exceeds the bounded read limit.')
        deadline = time.monotonic() + REQUEST_BODY_SECONDS
        remaining, chunks = size, []
        while remaining:
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise TimeoutError('Request body deadline exceeded.')
            self.connection.settimeout(seconds)
            # read1 performs at most one underlying buffered read. Unlike
            # read(size), slow trickles cannot restart the timeout indefinitely.
            chunk = self.rfile.read1(min(remaining, REQUEST_BODY_CHUNK))
            if not chunk:
                raise ValueError('Truncated request body.')
            remaining -= len(chunk)
            if not discard:
                chunks.append(chunk)
            if time.monotonic() >= deadline:
                raise TimeoutError('Request body deadline exceeded.')
        return b''.join(chunks)

    def _finish_rejected_body(self):
        if getattr(self, '_body_attempted', False):
            return
        # A well-framed bounded request may send headers before its body. Drain
        # it before the final rejection so Windows does not lose the response
        # when TCP closes with unread payload. Never drain ambiguous framing,
        # oversized content or beyond the absolute deadline. Those connections
        # close fail-closed; reliable delivery to a misbehaving sender is not
        # guaranteed and no backend operation is dispatched.
        try:
            self._read_request_body(MAX_REQUEST_BODY_BYTES, discard=True, optional=True)
        except (ValueError, OSError):
            pass

    def log_message(self, *_args):
        pass  # Never log tokens, working-copy paths or raw SVN diagnostics.

    def respond(self, status, body, content_type="application/json; charset=utf-8"):
        self._finish_rejected_body()
        self.close_connection = True
        self.connection.settimeout(self.timeout)
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def validated_url(self):
        if not self.valid_host():
            self.respond(403, {"error": "Invalid host."})
            return None
        if self.headers.get("Sec-Fetch-Site", "") not in {"", "same-origin", "none"}:
            self.respond(403, {"error": "Cross-origin requests are not allowed."})
            return None
        try:
            url = urlsplit(self.path)
        except ValueError:
            self.respond(400, {"error": "Invalid request target."})
            return None
        if url.scheme or url.netloc or url.query or url.fragment:
            self.respond(400, {"error": "Only local paths without query parameters are supported."})
            return None
        if url.path.startswith("/api/"):
            origin = self.headers.get("Origin")
            if (origin is not None and origin != self.server.origin) or not secrets.compare_digest(self.headers.get("X-SVN-Desk-Token", "").encode(), self.server.token.encode()):
                self.respond(403, {"error": "Session authorization required."})
                return None
        return url

    def do_GET(self):
        url = self.validated_url()
        if url is None:
            return
        if url.path.startswith("/api/"):
            if url.path == "/api/config":
                if self.server.catalog:
                    return self.respond(200, self.server.catalog.config())
                adapter = self.server.adapter
                return self.respond(200, {"mode": "live" if adapter else "demo", "workspace": adapter.workspace.name if adapter else "Project Atlas", "root": str(adapter.workspace) if adapter else "D:\\Workspace\\ProjectAtlas", "writesEnabled": False})
            if url.path == "/api/workspaces":
                if self.server.catalog is None:
                    return self.respond(200, {"enabled": False, "candidates": []})
                try:
                    return self.respond(200, self.server.catalog.discover())
                except BlockingIOError:
                    return self.respond(409, {"error": "已有工作区操作正在运行，请稍后重试。"})
                except Exception:
                    return self.respond(503, {"error": "工作区发现失败；没有扩大扫描范围。"})
            if url.path == "/api/status":
                adapter = self.server.catalog or self.server.adapter
                if adapter is None:
                    return self.respond(200, {"mode": "demo"})
                try:
                    return self.respond(200, adapter.snapshot())
                except BlockingIOError:
                    return self.respond(409, {"error": "已有状态查询正在运行，请稍后重试。"})
                except Exception:
                    # Fail closed. Never replace live failure with clean/demo rows.
                    return self.respond(503, {"error": "本地 SVN 状态读取失败。没有返回部分结果；请检查工作副本、严格状态后端版本、SVN 安装或输出上限。"})
            return self.respond(404, {"error": "Read-only API not found."})
        filename = ASSETS.get(url.path)
        if filename is None:
            return self.respond(404, {"error": "Not found."})
        path = ROOT / "web" / filename
        data = path.read_bytes()
        if filename == "index.html":
            data = data.replace(b"__SESSION_TOKEN__", self.server.token.encode())
        self.respond(200, data, MIMES[path.suffix])

    def do_POST(self):
        url = self.validated_url()
        if url is None:
            return
        file_kind = {'/api/file-diff': 'diff', '/api/file-history': 'history'}.get(url.path)
        changelist_kind = {'/api/changelist-preflight': 'preflight', '/api/changelist-apply': 'apply'}.get(url.path)
        commit_kind = {'/api/commit-preflight': 'preflight', '/api/commit-apply': 'apply'}.get(url.path)
        shelf_kind = {'/api/shelves-list': 'list', '/api/shelf-preflight': 'preflight', '/api/shelf-apply': 'apply'}.get(url.path)
        if (url.path != "/api/workspace-selection" and file_kind is None and changelist_kind is None and shelf_kind is None and commit_kind is None) or self.server.catalog is None:
            return self.respond(405, {"error": "SVN writes are not implemented."})
        if (self.headers.get_all("Content-Length") is None
                or len(self.headers.get_all("Content-Length")) != 1
                or self.headers.get("Transfer-Encoding") is not None
                or self.headers.get("Content-Type", "").lower() != "application/json"):
            return self.respond(400, {"error": "A bounded JSON request is required."})
        try:
            size = self._request_body_size()
            limit = MAX_REQUEST_BODY_BYTES if changelist_kind or shelf_kind or commit_kind else 4096
            if not 1 <= size <= limit:
                self._body_attempted = True  # Oversized/empty input is not drained.
                return self.respond(413, {"error": "Selection request is too large or empty."})
            body = self._read_request_body(limit)
            def strict_object(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj:
                        raise ValueError('Duplicate field.')
                    obj[key] = value
                return obj
            data = json.loads(body, object_pairs_hook=strict_object)
            if commit_kind:
                validate_commit_request(commit_kind, data)
            elif shelf_kind:
                validate_shelf_request(shelf_kind, data)
            elif changelist_kind:
                validate_request(changelist_kind, data)
            elif file_kind is None:
                if (not isinstance(data, dict) or set(data) != {"id"}
                        or not isinstance(data['id'], str) or not 1 <= len(data['id']) <= 64):
                    raise ValueError('Invalid selection.')
            else:
                expected = {'workspaceId', 'selectionVersion', 'snapshotToken', 'id'}
                expected |= {'fromRevision', 'toRevision'} if file_kind == 'diff' else {'startRevision'}
                if (not isinstance(data, dict) or set(data) != expected
                        or not isinstance(data['selectionVersion'], int)
                        or isinstance(data['selectionVersion'], bool)
                        or not 1 <= data['selectionVersion'] <= 2**53 - 1):
                    raise ValueError('Invalid read identity.')
                for field, maximum in [('workspaceId', 64), ('snapshotToken', 64), ('id', 2048)]:
                    if (not isinstance(data[field], str) or not 1 <= len(data[field]) <= maximum
                            or any(ord(c) < 32 or ord(c) == 127 for c in data[field])):
                        raise ValueError('Invalid read field.')
                if file_kind == 'diff':
                    comparison_revisions(data)
                else:
                    revision(data['startRevision'])
        except TimeoutError:
            return self.respond(408, {'error': '请求体未在有界时限内完整到达；未执行操作。'})
        except UnsupportedComparison:
            return self.respond(400, {'error': '不支持 BASE → BASE 比较。查看本地修改请选 BASE → WORKING，或使用明确数字修订；未运行比较。'})
        except (ValueError, OSError, RecursionError):
            return self.respond(400, {"error": "只接受明确工作区、当前状态行与受限修订；不接受任意路径、命令或重复字段。"})
        if commit_kind:
            try:
                return self.respond(200, self.server.catalog.commit_operation(commit_kind, data))
            except CommitUnavailable:
                return self.respond(403, {'error': '仓库提交未启用或配套后端不匹配。须由本机启动参数明确启用。'})
            except StaleRead:
                return self.respond(409, {'error': '提交计划、完整成员、内容或状态已改变；未执行此确认。请刷新并重新预检确认。'})
            except BlockingIOError:
                return self.respond(409, {'error': '已有工作区操作正在运行；本次未执行提交。'})
            except Exception:
                if commit_kind == 'apply':
                    return self.respond(503, {'error': '提交结果未能确认，仓库可能已经接受提交。没有自动重试或回滚；请先通过可信客户端核对仓库历史，再明确刷新状态和重新审查。'})
                return self.respond(503, {'error': '本地提交预检未通过；未运行提交。请检查完整成员、文件/目录依赖、移动/冲突、内容上限和配套安全后端。'})
        if shelf_kind:
            try:
                return self.respond(200, self.server.catalog.shelf_operation(shelf_kind, data))
            except ShelfUnavailable:
                return self.respond(403, {'error': '本地 Shelf 未启用或配套安全后端不匹配；请检查启动参数和配套版本。'})
            except StaleRead:
                return self.respond(409, {'error': 'Shelf 计划、内容或状态已改变；未执行此确认。请刷新并重新预检。'})
            except BlockingIOError:
                return self.respond(409, {'error': '已有工作区操作正在运行；本次未执行 Shelf 操作。'})
            except Exception:
                if shelf_kind == 'apply':
                    return self.respond(503, {'error': 'Shelf 结果未能确认。备份可能已保存，恢复可能已部分修改文件、属性或 SVN 调度；没有自动重试或回滚。请刷新状态并检查本地 Shelf 后重新审查。'})
                return self.respond(503, {'error': '本地 Shelf 读取或预检未通过；未运行备份或恢复。请检查文件/存储范围、大小上限、清洁 BASE、目标冲突和配套后端。'})
        if changelist_kind:
            try:
                return self.respond(200, self.server.catalog.changelist_operation(changelist_kind, data))
            except ChangelistUnavailable:
                return self.respond(403, {'error': '本地归组未启用。须由本机启动参数明确启用；不会提交或还原。'})
            except StaleRead:
                return self.respond(409, {'error': '确认计划、文件或状态已过期；本次未执行归组。请刷新状态并重新预检确认。'})
            except BlockingIOError:
                return self.respond(409, {'error': '已有工作区操作正在运行；本次未执行归组。请完成后明确重试。'})
            except Exception:
                if changelist_kind == 'apply':
                    return self.respond(503, {'error': '本地归组结果未能确认，部分标签可能已改变；没有自动重试或回滚。请明确刷新状态，再重新选择、预检和确认。'})
                return self.respond(503, {'error': '本地归组预检未通过，未运行归组写操作。请检查文件范围、状态、严格配套后端与 SVN 安装后重新审查。'})
        if file_kind is not None:
            try:
                return self.respond(200, self.server.catalog.file_read(file_kind, data))
            except StaleRead:
                return self.respond(409, {'error': '工作区或状态已改变，请刷新状态后重新选择文件。'})
            except BlockingIOError:
                return self.respond(409, {'error': '已有工作区读取正在运行，请完成后明确重试。'})
            except Exception:
                return self.respond(503, {'error': '文件只读查询失败；未返回部分结果，失败不代表无差异或无历史。请检查文件范围、配套严格后端、修订、仓库访问或输出上限。'})
        try:
            return self.respond(200, self.server.catalog.select(data['id']))
        except KeyError:
            return self.respond(404, {"error": "候选已失效，请重新扫描后选择。"})
        except BlockingIOError:
            return self.respond(409, {"error": "已有工作区操作正在运行，请稍后重试。"})
        except Exception:
            return self.respond(503, {"error": "工作区本地状态验证失败；没有切换，也没有保存。请检查目录、严格状态后端版本与 SVN 安装。"})

    def reject_mutation(self):
        self.respond(405, {"error": "SVN writes are not implemented."})
    do_PUT = do_DELETE = do_PATCH = reject_mutation
    def do_OPTIONS(self):
        self.respond(405, {"error": "Cross-origin access is not supported."})

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path, action="append", default=[], help="Explicit existing WC root; repeatable")
    parser.add_argument("--scan-parent", type=Path, action="append", default=[], help="Authorize bounded metadata discovery below this directory; repeatable")
    parser.add_argument("--settings-file", type=Path, help="Opt in to selection preferences outside all scan/WC directories; parent must exist")
    parser.add_argument("--backend-source", type=Path)
    parser.add_argument("--enable-changelists", action="store_true", help="Opt in to confirmed exact-file local SVN label assignment/removal; no commit/revert")
    parser.add_argument("--enable-commits", action="store_true", help="Opt in to exact-file or complete named-changelist repository commits after separate one-use confirmation")
    parser.add_argument("--enable-shelves", action="store_true", help="Opt in to previewed and confirmed exact-file local Shelf backup/restore; no implicit revert/commit")
    parser.add_argument("--shelf-storage", type=Path, help="Optional original-format Shelf directory outside every workspace and authorized scan parent")
    parser.add_argument("--svn", help="Trusted installed native SVN executable")
    args = parser.parse_args()
    authorized = bool(args.workspace or args.scan_parent)
    if authorized and not args.backend_source:
        parser.error("--workspace/--scan-parent requires --backend-source; demo is never used as a fallback.")
    if (args.backend_source or args.svn or args.settings_file or args.enable_changelists or args.enable_shelves or args.shelf_storage or args.enable_commits) and not authorized:
        parser.error("Backend, SVN and preferences require --workspace or --scan-parent.")
    if args.shelf_storage and not args.enable_shelves:
        parser.error("--shelf-storage requires --enable-shelves.")
    try:
        catalog = None
        if authorized:
            _, _, discovery, settings = trusted_modules(args.backend_source)
            catalog = WorkspaceCatalog(roots=args.workspace, parents=args.scan_parent,
                discovery=discovery, settings=settings, settings_file=args.settings_file,
                adapter_factory=lambda path: ReadOnlyAdapter(path, args.backend_source, args.svn, enable_changelists=args.enable_changelists, enable_shelves=args.enable_shelves,
                    shelf_storage=args.shelf_storage, enable_commits=args.enable_commits))
        server = PrototypeServer(args.port, catalog=catalog)
    except (ValueError, OSError, RuntimeError, ImportError):
        parser.exit(1, "Startup failed. Verify authorized directories, preferences path and trusted backend.\n")
    print(f"SVN Desk {('CONFIRMED REPOSITORY COMMITS' if args.enable_commits else 'CONFIRMED LOCAL SHELVES' if args.enable_shelves else 'CONFIRMED LOCAL CHANGELISTS' if args.enable_changelists else 'READ-ONLY WORKSPACES') if catalog else 'DEMO'}: {server.origin}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
