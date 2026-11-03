"""Real loopback TCP framing regression; no SVN or project filesystem used."""
import http.client
import json
from pathlib import Path
import select
import socket
import sys
import threading
import time
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as bridge


class RequestSocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Mock()
        cls.catalog.select.return_value = {'mode': 'live', 'fixture': True}
        cls.server = bridge.PrototypeServer(0, catalog=cls.catalog)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2)

    def setUp(self):
        self.catalog.reset_mock()

    def connect(self):
        connection = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=3)
        self.addCleanup(connection.close)
        return connection

    def headers(self, path='/api/not-implemented', method='POST', size=7, extra=(), token=None):
        fields = [('Host', f'127.0.0.1:{self.server.server_port}'),
                  ('X-SVN-Desk-Token', self.server.token if token is None else token),
                  ('Content-Type', 'application/json')]
        if size is not None:
            fields.append(('Content-Length', str(size)))
        fields += list(extra)
        return (f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{k}: {v}\r\n' for k,v in fields) + '\r\n').encode('ascii')

    def response(self, connection):
        response = http.client.HTTPResponse(connection)
        response.begin()
        payload = response.read()
        self.assertEqual(response.getheader('Connection'), 'close')
        self.assertEqual(int(response.getheader('Content-Length')), len(payload))
        return response.status, json.loads(payload)

    def delayed(self, path='/api/not-implemented', method='POST', expected=405, extra=(), token=None):
        payload = b'{"x":1}'
        connection = self.connect()
        connection.sendall(self.headers(path, method, len(payload), extra, token))
        # Explicitly cover the Windows reproducer: headers, 20ms gap, then body.
        self.assertEqual(select.select([connection], [], [], .020)[0], [],
                         'Final response/close arrived before bounded body was consumed')
        connection.sendall(payload)
        status, body = self.response(connection)
        self.assertEqual(status, expected, body)

    def test_unsupported_post_delayed_20ms_body_receives_complete_405(self):
        self.delayed()
        self.assertFalse(self.catalog.mock_calls)

    def test_unsupported_post_coalesced_body_receives_same_405(self):
        connection = self.connect()
        connection.sendall(self.headers() + b'{"x":1}')
        self.assertEqual(self.response(connection)[0], 405)
        self.assertFalse(self.catalog.mock_calls)

    def test_other_rejected_mutation_methods_consume_bounded_body(self):
        for method in ['PUT', 'PATCH', 'DELETE', 'OPTIONS']:
            with self.subTest(method=method): self.delayed(method=method)
        self.assertFalse(self.catalog.mock_calls)

    def test_authentication_and_origin_rejections_stay_closed(self):
        self.delayed(path='/api/shelves-list', token='invalid', expected=403)
        self.delayed(path='/api/shelves-list', extra=[('Origin', 'https://outside.test')], expected=403)
        self.assertFalse(self.catalog.mock_calls)

    def test_host_and_content_type_rejections_consume_bounded_body(self):
        # A wrong origin/host never reaches the backend, even with the right token.
        connection = self.connect()
        header = self.headers().replace(f'Host: 127.0.0.1:{self.server.server_port}'.encode(), b'Host: outside.test')
        connection.sendall(header)
        self.assertEqual(select.select([connection], [], [], .020)[0], [])
        connection.sendall(b'{"x":1}')
        self.assertEqual(self.response(connection)[0], 403)
        connection = self.connect()
        connection.sendall(self.headers('/api/workspace-selection').replace(b'application/json', b'text/plain'))
        self.assertEqual(select.select([connection], [], [], .020)[0], [])
        connection.sendall(b'{"x":1}')
        self.assertEqual(self.response(connection)[0], 400)
        self.assertFalse(self.catalog.mock_calls)

    def test_supported_body_split_across_packets_dispatches_exactly_once(self):
        payload = b'{"id":"candidate"}'
        connection = self.connect()
        connection.sendall(self.headers('/api/workspace-selection', size=len(payload)))
        for chunk in [payload[:2], payload[2:9], payload[9:]]:
            time.sleep(.020); connection.sendall(chunk)
        self.assertEqual(self.response(connection)[0], 200)
        self.catalog.select.assert_called_once_with('candidate')
        self.assertEqual(len(self.catalog.mock_calls), 1)

    def test_oversized_advertised_body_is_not_drained(self):
        for path, size, expected in [('/api/not-implemented', bridge.MAX_REQUEST_BODY_BYTES+1, 405),
                                      ('/api/workspace-selection', 4097, 413),
                                      ('/api/shelf-preflight', bridge.MAX_REQUEST_BODY_BYTES+1, 413)]:
            with self.subTest(path=path):
                connection = self.connect(); start = time.monotonic()
                connection.sendall(self.headers(path, size=size))
                self.assertEqual(self.response(connection)[0], expected)
                self.assertLess(time.monotonic()-start, 1.5)
        self.assertFalse(self.catalog.mock_calls)

    def test_ambiguous_framing_is_not_drained_or_dispatched(self):
        for size, extra in [(7, [('Content-Length', '7')]), (7, [('Transfer-Encoding', 'chunked')]),
                             ('-1', []), ('+7', []), ('7x', []), (None, [])]:
            with self.subTest(size=size, extra=extra):
                connection = self.connect()
                connection.sendall(self.headers('/api/workspace-selection', size=size, extra=extra))
                self.assertEqual(self.response(connection)[0], 400)
        self.assertFalse(self.catalog.mock_calls)

    def test_truncated_supported_body_is_rejected_before_dispatch(self):
        connection = self.connect()
        connection.sendall(self.headers('/api/workspace-selection', size=20)+b'{')
        connection.shutdown(socket.SHUT_WR)
        self.assertEqual(self.response(connection)[0], 400)
        self.assertFalse(self.catalog.mock_calls)

    def test_supported_body_timeout_is_408_not_dispatch(self):
        with patch.object(bridge, 'REQUEST_BODY_SECONDS', .15):
            connection = self.connect(); start = time.monotonic()
            connection.sendall(self.headers('/api/workspace-selection', size=20))
            self.assertEqual(self.response(connection)[0], 408)
            self.assertGreaterEqual(time.monotonic()-start, .10)
            self.assertLess(time.monotonic()-start, 1.5)
        self.assertFalse(self.catalog.mock_calls)

    def test_rejected_body_timeout_keeps_original_rejection_and_is_bounded(self):
        with patch.object(bridge, 'REQUEST_BODY_SECONDS', .15):
            connection = self.connect(); start = time.monotonic()
            connection.sendall(self.headers(size=20))
            self.assertEqual(self.response(connection)[0], 405)
            self.assertLess(time.monotonic()-start, 1.5)
        self.assertFalse(self.catalog.mock_calls)

    def test_partial_body_then_silence_times_out_without_dispatch(self):
        # Send finite early chunks, never race a write against the server close.
        # Late bytes can legitimately reset Windows TCP after the body deadline.
        with patch.object(bridge, 'REQUEST_BODY_SECONDS', .5):
            connection = self.connect()
            connection.sendall(self.headers('/api/workspace-selection', size=100))
            start = time.monotonic()
            connection.sendall(b' ')
            time.sleep(.020)
            connection.sendall(b' ')
            self.assertEqual(self.response(connection)[0], 408)
            self.assertLess(time.monotonic()-start, 1.5)
        self.assertFalse(self.catalog.mock_calls)

    def test_absolute_deadline_shrinks_read_timeouts_and_stops_further_reads(self):
        # A deterministic clock proves that successful partial reads cannot
        # restart the budget, without sending TCP bytes after its deadline.
        handler = object.__new__(bridge.Handler)
        handler.headers = Mock()
        handler.headers.get_all.return_value = ['3']
        handler.headers.get.return_value = None
        handler.connection = Mock()
        handler.rfile = Mock()
        handler.rfile.read1.side_effect = [b'a', b'b']
        with patch.object(bridge, 'REQUEST_BODY_SECONDS', 5.0), \
                patch.object(bridge.time, 'monotonic', side_effect=[10, 10, 12, 12, 15]):
            with self.assertRaises(TimeoutError):
                handler._read_request_body(4096)
        self.assertEqual(handler.connection.settimeout.call_args_list, [call(5.0), call(3.0)])
        self.assertEqual(handler.rfile.read1.call_args_list, [call(3), call(2)])
        self.assertTrue(handler._body_attempted)


if __name__ == '__main__': unittest.main()
