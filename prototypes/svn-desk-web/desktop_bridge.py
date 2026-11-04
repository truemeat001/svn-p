#!/usr/bin/env python3
"""Fixed backend process for the native Windows preview. No generic RPC or shell."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import threading

# Frozen source resources remain read-only in the installation directory.
sys.dont_write_bytecode = True

from server import PrototypeServer, ReadOnlyAdapter, reviewed_commit_backend, checkout_for
from checkout_ops import checkout_arguments, checkout_requested
from workspace_bridge import WorkspaceCatalog, trusted_modules
from file_reads import history_module
from shelf_ops import shelf_module
from lock_ops import lock_module
from file_ops import fileops_module

PROTOCOL = 'svn-desk-desktop/1'
MAX_CONTROL_LINE = 32
WRITE_FLAGS = ('changelists', 'shelves', 'commits', 'locks', 'adds', 'file_ops')


def backend_source():
    here = Path(__file__).resolve().parent
    if getattr(sys, 'frozen', False):
        return here / 'backend-source'
    # The repository root is fixed by the entry point, never page/CLI input.
    return here.parents[1]


def verified_backend(source):
    backend, status_stream, discovery, settings = trusted_modules(source)
    history = history_module(source)
    if (getattr(backend, 'STATUS_DIAGNOSTICS_POLICY', None) != 1
            or getattr(status_stream, 'STATUS_DIAGNOSTICS_POLICY', None) != 1
            or not history or not reviewed_commit_backend(backend, history)
            or not shelf_module(source) or not lock_module(source) or not fileops_module(source)):
        raise ValueError('Bundled backend does not match the reviewed version.')
    return discovery, settings


def prepare_external_runtime(source):
    """Do not pass PyInstaller's bundled DLL search directory to installed SVN.

    All bundled Python/backend modules load first. This changes this process only,
    not the user's system settings. No third-party runtime hooks change PATH.
    https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#launching-external-programs-from-the-frozen-application
    """
    if getattr(sys, 'frozen', False) and sys.platform == 'win32':
        verified_backend(source)
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        reset = kernel.SetDllDirectoryW
        reset.argtypes = [ctypes.c_wchar_p]
        reset.restype = ctypes.c_int
        if not reset(None):
            raise OSError('Could not restore external-program DLL search paths.')


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    result.add_argument('--workspace', type=Path, action='append', default=[])
    result.add_argument('--scan-parent', type=Path, action='append', default=[])
    result.add_argument('--svn', help='Explicit trusted installed SVN executable')
    for flag in WRITE_FLAGS:
        result.add_argument('--enable-' + flag.replace('_', '-'), action='store_true')
    checkout_arguments(result)
    result.add_argument('--self-check', action='store_true', help='Check bundled source/resources without SVN or a server')
    return result


def catalog_for(args, source):
    authorized = bool(args.workspace or args.scan_parent or checkout_requested(args))
    if not authorized:
        if args.svn or any(getattr(args, 'enable_' + name) for name in WRITE_FLAGS):
            raise ValueError('Capabilities require an explicitly authorized working copy or scan parent.')
        return None
    discovery, settings = verified_backend(source)
    options = {'enable_' + name: getattr(args, 'enable_' + name) for name in WRITE_FLAGS}
    return WorkspaceCatalog(roots=args.workspace, parents=args.scan_parent,
                            discovery=discovery, settings=settings,
                            adapter_factory=lambda path: ReadOnlyAdapter(path, source, args.svn, **options))


class DesktopServer(PrototypeServer):
    """Drain accepted HTTP handlers before process exit, including SVN operations.

    No daemon request handlers: closing a window must not silently abandon an
    in-flight write. Existing socket/header/body and SVN limits still apply.
    A blocked local filesystem is not cancellable; the parent must show waiting,
    rather than falsely claiming that closing cancelled the operation.
    """
    daemon_threads = False
    block_on_close = True

    def __init__(self, *args, **kwargs):
        self._admission = threading.Lock()
        self._draining = False
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        with self._admission:
            if self._draining:
                self.shutdown_request(request)
                return
            super().process_request(request, client_address)

    def begin_drain(self):
        with self._admission:
            self._draining = True


def control_shutdown(stream, server):
    """The inherited pipe is the only shutdown authority; EOF also drains.

    No workspace paths, commands, credentials or status information cross this
    pipe. An invalid or oversized control line fails closed by starting shutdown.
    """
    try:
        stream.readline(MAX_CONTROL_LINE + 1)
    finally:
        server.begin_drain()
        server.shutdown()


def serve(server, input_stream, output_stream):
    # The server already owns its ephemeral port, so there is no free-port race.
    # Serve on a separate thread before the control thread can call shutdown.
    serving = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.1}, name='svn-desk-http')
    serving.start()
    control = threading.Thread(target=control_shutdown, args=(input_stream, server),
                               name='svn-desk-control', daemon=True)
    try:
        output_stream.write(json.dumps({'protocol': PROTOCOL, 'origin': server.origin, 'pid': os.getpid()},
                                       separators=(',', ':')) + '\n')
        output_stream.flush()
        control.start()
        serving.join()
    except BaseException:
        server.begin_drain()
        server.shutdown()
        serving.join()
        raise
    finally:
        # ThreadingMixIn joins all non-daemon handlers here, after admission stops.
        server.begin_drain()
        server.server_close()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        source = backend_source()
        if args.self_check:
            if args.workspace or args.scan_parent or args.checkout_url or args.checkout_destination or args.svn or any(getattr(args, 'enable_' + name) for name in WRITE_FLAGS):
                raise ValueError('Self-check never accepts working-copy arguments.')
            verified_backend(source)
            prepare_external_runtime(source)
            import server as bridge
            for asset in bridge.ASSETS.values():
                if not (bridge.ROOT / 'web' / asset).is_file():
                    raise ValueError('Bundled Web asset is missing.')
            print(json.dumps({'protocol': PROTOCOL, 'status': 'ok', 'backendVerified': True}), flush=True)
            return 0
        prepare_external_runtime(source)
        catalog = catalog_for(args, source)
        checkout = checkout_for(args, source, catalog) if checkout_requested(args) else None
        service = DesktopServer(0, catalog=catalog, checkout=checkout)
        serve(service, sys.stdin, sys.stdout)
        return 0
    except (ValueError, OSError, RuntimeError, ImportError):
        # Do not send raw SVN, local paths or auth diagnostics to the native host.
        print('SVN Desk backend failed. Verify the paired installation, authorized directories and SVN.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
