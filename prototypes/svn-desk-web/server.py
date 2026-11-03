#!/usr/bin/env python3
"""Loopback-only, read-only SVN Desk prototype bridge. No write APIs."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import urlsplit
from workspace_bridge import WorkspaceCatalog, local_path, working_copy_identity, trusted_modules
from file_reads import FileReadService, StaleRead, history_module, revision

ROOT = Path(__file__).resolve().parent
ASSETS = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js",
          "/model.js": "model.js", "/demo-data.js": "demo-data.js", "/styles.css": "styles.css", "/favicon.svg": "favicon.svg"}
MIMES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
MAX_ROWS = 50000

class ReadOnlyAdapter:
    """Imports trusted bounded services. Never exposes a generic SVN command."""
    def __init__(self, workspace: Path, backend_source: Path, executable: str | None = None):
        self.workspace = local_path(workspace)
        self._identity = working_copy_identity(self.workspace)
        backend, status_stream, _, _ = trusted_modules(backend_source)
        if (getattr(backend, 'STATUS_DIAGNOSTICS_POLICY', None) != 1
                or getattr(status_stream, 'STATUS_DIAGNOSTICS_POLICY', None) != 1):
            raise ValueError("Update the trusted backend: strict status diagnostics policy is required.")
        self.client = backend.SvnClient(self.workspace, executable=executable, timeout=25)
        history = history_module(backend_source)
        self._file_service = FileReadService(self.client, history) if history else None
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
            self._read_snapshot = None  # Refresh invalidates old file capabilities even on failure.
            self._check_workspace()
            # No --show-updates, remote info/list/log, shell, diff or writes.
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
            token = secrets.token_urlsafe(24)
            self._read_snapshot = (token, {row['id']: dict(row) for row in rows})
            return {"mode": "live", "workspace": self.workspace.name,
                    "root": str(self.workspace), "rows": rows,
                    "snapshotToken": token, "fileReadsEnabled": getattr(self, '_file_service', None) is not None,
                    "readAt": datetime.now(timezone.utc).isoformat(),
                    "scope": "Local status only; repository connectivity not tested."}
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
    def log_message(self, *_args):
        pass  # Never log tokens, working-copy paths or raw SVN diagnostics.

    def respond(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
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
        if (url.path != "/api/workspace-selection" and file_kind is None) or self.server.catalog is None:
            return self.respond(405, {"error": "SVN writes are not implemented."})
        if (self.headers.get_all("Content-Length") is None
                or len(self.headers.get_all("Content-Length")) != 1
                or self.headers.get("Transfer-Encoding") is not None
                or self.headers.get("Content-Type", "").lower() != "application/json"):
            return self.respond(400, {"error": "A bounded JSON request is required."})
        try:
            size = int(self.headers['Content-Length'])
            if not 1 <= size <= 4096:
                return self.respond(413, {"error": "Selection request is too large or empty."})
            self.connection.settimeout(5)
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError('Truncated request.')
            def strict_object(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj:
                        raise ValueError('Duplicate field.')
                    obj[key] = value
                return obj
            data = json.loads(body, object_pairs_hook=strict_object)
            if file_kind is None:
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
                    revision(data['fromRevision']); revision(data['toRevision'], working=True)
                else:
                    revision(data['startRevision'])
        except (ValueError, OSError, RecursionError):
            return self.respond(400, {"error": "只接受明确工作区、当前状态行与受限修订；不接受任意路径、命令或重复字段。"})
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
    parser.add_argument("--svn", help="Trusted installed native SVN executable")
    args = parser.parse_args()
    authorized = bool(args.workspace or args.scan_parent)
    if authorized and not args.backend_source:
        parser.error("--workspace/--scan-parent requires --backend-source; demo is never used as a fallback.")
    if (args.backend_source or args.svn or args.settings_file) and not authorized:
        parser.error("Backend, SVN and preferences require --workspace or --scan-parent.")
    try:
        catalog = None
        if authorized:
            _, _, discovery, settings = trusted_modules(args.backend_source)
            catalog = WorkspaceCatalog(roots=args.workspace, parents=args.scan_parent,
                discovery=discovery, settings=settings, settings_file=args.settings_file,
                adapter_factory=lambda path: ReadOnlyAdapter(path, args.backend_source, args.svn))
        server = PrototypeServer(args.port, catalog=catalog)
    except (ValueError, OSError, RuntimeError, ImportError):
        parser.exit(1, "Startup failed. Verify authorized directories, preferences path and trusted backend.\n")
    print(f"SVN Desk {'READ-ONLY WORKSPACES' if catalog else 'DEMO'}: {server.origin}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
