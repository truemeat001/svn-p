#!/usr/bin/env python3
"""Loopback-only, read-only SVN Desk prototype bridge. No write APIs."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import importlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
ASSETS = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js",
          "/model.js": "model.js", "/demo-data.js": "demo-data.js", "/styles.css": "styles.css", "/favicon.svg": "favicon.svg"}
MIMES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
MAX_ROWS = 50000

class ReadOnlyAdapter:
    """Imports the unchanged trusted backend, exposing only local status()."""
    def __init__(self, workspace: Path, backend_source: Path, executable: str | None = None):
        self.workspace = workspace.expanduser().resolve(strict=True)
        if not self.workspace.is_dir() or not (self.workspace / ".svn").is_dir():
            raise ValueError("Choose an existing working-copy root containing .svn.")
        backend_source = backend_source.expanduser().resolve(strict=True)
        if not (backend_source / "svn_client" / "status_stream.py").is_file():
            raise ValueError("Trusted SVN Desk backend source is required.")
        sys.path.insert(0, str(backend_source))
        backend = importlib.import_module("svn_client.backend")
        expected = backend_source / "svn_client" / "backend.py"
        if Path(backend.__file__).resolve() != expected:
            raise ValueError("Unexpected backend module was already loaded.")
        self.client = backend.SvnClient(self.workspace, executable=executable, timeout=25)
        self._busy = threading.Lock()

    def snapshot(self):
        if not self._busy.acquire(blocking=False):
            raise BlockingIOError("A status read is already running.")
        try:
            # No --show-updates, remote info/list/log, shell, diff or writes.
            entries = self.client.status(verbose=False)
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
            return {"mode": "live", "workspace": self.workspace.name,
                    "root": str(self.workspace), "rows": rows,
                    "readAt": datetime.now(timezone.utc).isoformat(),
                    "scope": "Local status only; repository connectivity not tested."}
        finally:
            self._busy.release()

class PrototypeServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, port=8765, adapter=None):
        super().__init__(("127.0.0.1", port), Handler)
        self.token = secrets.token_urlsafe(32)
        self.adapter = adapter
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

    def do_GET(self):
        if not self.valid_host():
            return self.respond(403, {"error": "Invalid host."})
        fetch_site = self.headers.get("Sec-Fetch-Site", "")
        if fetch_site not in {"", "same-origin", "none"}:
            return self.respond(403, {"error": "Cross-origin requests are not allowed."})
        url = urlsplit(self.path)
        if url.query or url.fragment:
            return self.respond(400, {"error": "Query parameters are not supported."})
        if url.path.startswith("/api/"):
            origin = self.headers.get("Origin")
            if (origin is not None and origin != self.server.origin) or not secrets.compare_digest(self.headers.get("X-SVN-Desk-Token", "").encode(), self.server.token.encode()):
                return self.respond(403, {"error": "Session authorization required."})
            if url.path == "/api/config":
                adapter = self.server.adapter
                return self.respond(200, {"mode": "live" if adapter else "demo", "workspace": adapter.workspace.name if adapter else "Project Atlas", "root": str(adapter.workspace) if adapter else "D:\\Workspace\\ProjectAtlas", "writesEnabled": False})
            if url.path == "/api/status":
                if self.server.adapter is None:
                    return self.respond(200, {"mode": "demo"})
                try:
                    return self.respond(200, self.server.adapter.snapshot())
                except BlockingIOError:
                    return self.respond(409, {"error": "已有状态查询正在运行，请稍后重试。"})
                except Exception:
                    # Fail closed. Never replace live failure with clean/demo rows.
                    return self.respond(503, {"error": "本地 SVN 状态读取失败。没有返回部分结果；请检查工作副本、SVN 安装或输出上限。"})
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
        self.respond(405, {"error": "Read-only prototype: writes are not implemented."})
    do_PUT = do_DELETE = do_PATCH = do_POST
    def do_OPTIONS(self):
        self.respond(405, {"error": "Cross-origin access is not supported."})

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--backend-source", type=Path)
    parser.add_argument("--svn", help="Trusted installed native SVN executable")
    args = parser.parse_args()
    if args.workspace and not args.backend_source:
        parser.error("--workspace requires --backend-source; demo is never used as a fallback.")
    if (args.backend_source or args.svn) and not args.workspace:
        parser.error("--backend-source and --svn require --workspace.")
    try:
        adapter = ReadOnlyAdapter(args.workspace, args.backend_source, args.svn) if args.workspace else None
        server = PrototypeServer(args.port, adapter)
    except (ValueError, OSError, RuntimeError) as error:
        parser.exit(1, "Startup failed. Verify working-copy root, backend source and installed SVN.\n")
    print(f"SVN Desk {'READ-ONLY LIVE' if adapter else 'DEMO'}: {server.origin}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
