import http.client
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

WEB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WEB))
import desktop_bridge as desktop


class DesktopConfigurationTests(unittest.TestCase):
    def args(self, *args):
        return desktop.parser().parse_args(list(args))

    def test_demo_default_has_no_catalog(self):
        with patch.object(desktop, 'verified_backend') as backend:
            self.assertIsNone(desktop.catalog_for(self.args(), Path('/unused')))
        backend.assert_not_called()

    def test_all_writes_are_disabled_by_default(self):
        args = self.args('--workspace', 'fixture')
        self.assertTrue(all(not getattr(args, 'enable_' + name) for name in desktop.WRITE_FLAGS))

    def test_svn_or_write_opt_in_without_roots_is_rejected(self):
        for flag in ['--svn', *['--enable-' + name.replace('_', '-') for name in desktop.WRITE_FLAGS]]:
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                desktop.catalog_for(self.args(*([flag, 'svn'] if flag == '--svn' else [flag])), Path('/unused'))

    def test_no_backend_port_or_settings_override(self):
        for flag in ['--backend-source', '--port', '--settings-file', '--shelf-storage', '--work']:
            with self.subTest(flag=flag), patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                self.args(flag, 'anything')

    def test_parser_accepts_exact_multiple_roots(self):
        args = self.args('--workspace', 'a b', '--workspace', '中文', '--scan-parent', 'third')
        self.assertEqual(args.workspace, [Path('a b'), Path('中文')])
        self.assertEqual(args.scan_parent, [Path('third')])

    def test_catalog_only_passes_explicit_capabilities(self):
        args = self.args('--workspace', 'fixture', '--enable-adds', '--enable-commits')
        with patch.object(desktop, 'verified_backend', return_value=('d', 's')), patch.object(desktop, 'WorkspaceCatalog') as factory, patch.object(desktop, 'ReadOnlyAdapter') as adapter:
            desktop.catalog_for(args, Path('/paired'))
            kw = factory.call_args.kwargs
            self.assertNotIn('settings_file', kw)
            kw['adapter_factory'](Path('/fixture'))
            self.assertTrue(adapter.call_args.kwargs['enable_adds'])
            self.assertTrue(adapter.call_args.kwargs['enable_commits'])
            self.assertFalse(adapter.call_args.kwargs['enable_locks'])

    def test_source_backend_is_fixed_to_repository(self):
        self.assertEqual(desktop.backend_source(), WEB.parents[1])

    def test_frozen_backend_is_fixed_to_internal_resource(self):
        with patch.object(desktop.sys, 'frozen', True, create=True):
            self.assertEqual(desktop.backend_source(), WEB / 'backend-source')

    def test_control_eof_shutdown_invalid_control_also_fails_closed(self):
        for line in ['', 'shutdown\n', 'not-a-command\n', 'x' * 1000]:
            with self.subTest(line=line[:20]):
                server = Mock()
                desktop.control_shutdown(io.StringIO(line), server)
                server.begin_drain.assert_called_once()
                server.shutdown.assert_called_once()

    def test_control_read_is_bounded(self):
        stream = Mock()
        desktop.control_shutdown(stream, Mock())
        stream.readline.assert_called_once_with(desktop.MAX_CONTROL_LINE + 1)

    def test_control_error_still_stops_admission(self):
        stream, server = Mock(), Mock()
        stream.readline.side_effect = OSError('closed')
        with self.assertRaises(OSError):
            desktop.control_shutdown(stream, server)
        server.begin_drain.assert_called_once()
        server.shutdown.assert_called_once()

    def test_self_check_rejects_workspaces_without_reading_them(self):
        with patch.object(desktop, 'verified_backend') as verify, patch('sys.stderr', io.StringIO()):
            self.assertEqual(desktop.main(['--self-check', '--workspace', '/never-touch']), 1)
        verify.assert_not_called()

    def test_startup_error_redacts_diagnostics(self):
        stream = io.StringIO()
        with patch.object(desktop, 'catalog_for', side_effect=ValueError('password=private')), patch('sys.stderr', stream):
            self.assertEqual(desktop.main([]), 1)
        self.assertNotIn('private', stream.getvalue())

    def test_source_runtime_does_not_change_dll_search(self):
        with patch.object(desktop.sys, 'frozen', False, create=True), patch.object(desktop, 'verified_backend') as verify:
            desktop.prepare_external_runtime(Path('/unused'))
        verify.assert_not_called()

    def test_frozen_windows_resets_dll_search_after_importing_backend(self):
        import ctypes
        reset = Mock(return_value=1); kernel = Mock(SetDllDirectoryW=reset)
        events = []
        with patch.object(desktop.sys, 'frozen', True, create=True), patch.object(desktop.sys, 'platform', 'win32'), patch.object(ctypes, 'WinDLL', return_value=kernel, create=True) as loader, patch.object(desktop, 'verified_backend', side_effect=lambda source: events.append('verified')):
            reset.side_effect = lambda value: events.append('reset') or 1
            desktop.prepare_external_runtime(Path('/paired'))
        self.assertEqual(events, ['verified', 'reset'])
        loader.assert_called_once_with('kernel32', use_last_error=True)
        reset.assert_called_once_with(None)

    def test_frozen_windows_dll_reset_failure_is_fatal(self):
        import ctypes
        kernel = Mock(SetDllDirectoryW=Mock(return_value=0))
        with patch.object(desktop.sys, 'frozen', True, create=True), patch.object(desktop.sys, 'platform', 'win32'), patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), patch.object(desktop, 'verified_backend'), self.assertRaises(OSError):
            desktop.prepare_external_runtime(Path('/paired'))

    def test_backend_pairing_fails_closed(self):
        with patch.object(desktop, 'trusted_modules', return_value=(Mock(), Mock(), None, None)), patch.object(desktop, 'history_module'), self.assertRaises(ValueError):
            desktop.verified_backend(Path('/fixture'))


class DesktopProcessTests(unittest.TestCase):
    def start(self):
        proc = subprocess.Popen([sys.executable, str(WEB / 'desktop_bridge.py')], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.cleanup, proc)
        result = queue.Queue()
        threading.Thread(target=lambda: result.put(proc.stdout.readline()), daemon=True).start()
        line = result.get(timeout=5)
        self.assertLess(len(line), 4096)
        ready = json.loads(line)
        self.assertEqual(ready['protocol'], desktop.PROTOCOL)
        self.assertEqual(ready['pid'], proc.pid)
        self.assertRegex(ready['origin'], r'^http://127\.0\.0\.1:[1-9][0-9]{0,4}$')
        return proc, ready

    def cleanup(self, proc):
        if proc.poll() is None:
            try:
                proc.communicate('shutdown\n', timeout=5)
            except (subprocess.TimeoutExpired, ValueError):
                # Demo process only. Never used for real working-copy operations.
                proc.kill(); proc.communicate(timeout=5)
        for stream in [proc.stdin, proc.stdout, proc.stderr]:
            if stream and not stream.closed:
                stream.close()

    def test_handshake_owned_port_demo_and_graceful_exit(self):
        proc, ready = self.start()
        port = int(ready['origin'].rsplit(':', 1)[1])
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        connection.request('GET', '/')
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn(b'SVN Desk', response.read())
        connection.close()
        stdout, stderr = proc.communicate('shutdown\n', timeout=5)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertEqual(stdout, '')
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
        with self.assertRaises(OSError):
            connection.connect()
        connection.close()

    def test_parent_pipe_eof_drains_and_exits(self):
        proc, _ = self.start()
        proc.stdin.close(); proc.stdin = None
        stdout, stderr = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertEqual(stdout, '')

    def test_two_instances_get_distinct_ports(self):
        first, a = self.start(); second, b = self.start()
        self.assertNotEqual(a['origin'], b['origin'])
        self.assertEqual(first.communicate('shutdown\n', timeout=5), ('', ''))
        self.assertEqual(second.communicate('shutdown\n', timeout=5), ('', ''))

    def test_self_check_is_source_only_no_listener_or_svn(self):
        completed = subprocess.run([sys.executable, str(WEB / 'desktop_bridge.py'), '--self-check'],
                                   capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {'protocol': desktop.PROTOCOL, 'status': 'ok', 'backendVerified': True})


class DesktopDrainTests(unittest.TestCase):
    def test_close_waits_for_accepted_request_instead_of_abandoning_it(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        class Adapter:
            def snapshot(self):
                started.set()
                if not release.wait(5):
                    raise TimeoutError('test release not received')
                finished.set()
                return {'mode': 'live', 'rows': []}
        server = desktop.DesktopServer(0, adapter=Adapter())
        control_read, control_write = os.pipe()
        reader = os.fdopen(control_read, 'r')
        output = io.StringIO()
        service = threading.Thread(target=desktop.serve, args=(server, reader, output))
        service.start()
        response = []
        def request():
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                connection.request('GET', '/api/status', headers={'X-SVN-Desk-Token': server.token})
                result = connection.getresponse(); response.append((result.status, result.read()))
            finally:
                connection.close()
        http_thread = threading.Thread(target=request)
        http_thread.start()
        try:
            self.assertTrue(started.wait(3))
            os.write(control_write, b'shutdown\n'); os.close(control_write); control_write = None
            deadline = time.monotonic() + 2
            while not server._draining and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(server._draining)
            service.join(0.2)
            self.assertTrue(service.is_alive(), 'Close must wait for the accepted operation.')
            self.assertFalse(finished.is_set())
            release.set()
            http_thread.join(3); service.join(3)
            self.assertFalse(service.is_alive())
            self.assertTrue(finished.is_set())
            self.assertEqual(response[0][0], 200)
        finally:
            release.set()
            if control_write is not None:
                os.close(control_write)
            service.join(5); http_thread.join(5); reader.close()


if __name__ == '__main__':
    unittest.main()
