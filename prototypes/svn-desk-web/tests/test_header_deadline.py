"""Absolute pre-dispatch HTTP deadlines; only synthetic loopback fixtures."""
import http.client
import io
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as bridge
import desktop_bridge as desktop


class HeaderReaderTests(unittest.TestCase):
    def reader(self, data, seconds=5):
        source = io.BufferedReader(io.BytesIO(data), buffer_size=8)
        self.addCleanup(source.close)
        connection = Mock()
        return bridge.HeaderDeadlineReader(source, connection, seconds, 5), source, connection

    def test_lines_and_coalesced_body_remain_exact(self):
        reader, source, connection = self.reader(b'POST / HTTP/1.1\r\nX: test\r\n\r\n{"body":1}')
        self.assertEqual(reader.readline(65537), b'POST / HTTP/1.1\r\n')
        self.assertEqual(reader.readline(65537), b'X: test\r\n')
        self.assertEqual(reader.readline(65537), b'\r\n')
        reader.finish_headers()
        self.assertEqual(source.read(), b'{"body":1}')
        self.assertTrue(reader.headers_finished)
        connection.settimeout.assert_called_with(5)

    def test_line_limit_eof_and_zero_limit_match_buffered_reader(self):
        reader, source, _ = self.reader(b'abcd\nef')
        self.assertEqual(reader.readline(0), b'')
        self.assertEqual(reader.readline(3), b'abc')
        self.assertEqual(reader.readline(65537), b'd\n')
        self.assertEqual(reader.readline(65537), b'ef')
        self.assertEqual(reader.readline(65537), b'')

    def test_timeout_shrinks_across_partial_reads_instead_of_restarting(self):
        source, connection = Mock(), Mock()
        source.peek.side_effect = [b'a', b'b']
        source.read1.side_effect = [b'a', b'b']
        with patch.object(bridge.time, 'monotonic', side_effect=[10, 10, 11, 11, 12, 13, 15]):
            reader = bridge.HeaderDeadlineReader(source, connection, 5, 5)
            with self.assertRaises(TimeoutError):
                reader.readline(65537)
        self.assertEqual(connection.settimeout.call_args_list, [call(5), call(3)])
        self.assertEqual(source.peek.call_count, 2)
        self.assertEqual(source.read1.call_args_list, [call(1), call(1)])

    def test_deadline_is_shared_across_request_line_and_headers(self):
        now = [10.0]
        with patch.object(bridge.time, 'monotonic', side_effect=lambda: now[0]):
            reader, source, _ = self.reader(b'GET / HTTP/1.1\r\nHost: fixture\r\n\r\n')
            self.assertEqual(reader.readline(65537), b'GET / HTTP/1.1\r\n')
            now[0] = 14.0
            self.assertEqual(reader.readline(65537), b'Host: fixture\r\n')
            now[0] = 15.0
            with self.assertRaises(TimeoutError):
                reader.readline(65537)
            self.assertEqual(source.read(), b'\r\n')

    def test_late_successful_peek_does_not_dispatch_or_consume(self):
        source, connection = Mock(), Mock()
        source.peek.return_value = b'\r\n'
        with patch.object(bridge.time, 'monotonic', side_effect=[10, 10, 15]):
            reader = bridge.HeaderDeadlineReader(source, connection, 5, 5)
            with self.assertRaises(TimeoutError):
                reader.readline(65537)
        source.read1.assert_not_called()

    def test_completed_headers_do_not_limit_body_or_accepted_work(self):
        now = [10.0]
        with patch.object(bridge.time, 'monotonic', side_effect=lambda: now[0]):
            reader, _, connection = self.reader(b'\r\nbody\n')
            self.assertEqual(reader.readline(65537), b'\r\n')
            reader.finish_headers()
            now[0] = 100.0
            self.assertEqual(reader.read1(2), b'bo')
            self.assertEqual(reader.readline(), b'dy\n')
            connection.settimeout.assert_called_with(5)


class HeaderSocketTests(unittest.TestCase):
    def start_server(self, handler=bridge.Handler):
        catalog = Mock()
        catalog.select.return_value = {'mode': 'live', 'fixture': True}
        server = desktop.DesktopServer(0, catalog=catalog)
        server.RequestHandlerClass = handler
        serving = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        serving.start()
        self.addCleanup(self.close_server, server, serving)
        return server, catalog

    def close_server(self, server, serving):
        server.begin_drain()
        server.shutdown()
        server.server_close()
        serving.join(3)

    def connect(self, server):
        connection = socket.create_connection(('127.0.0.1', server.server_port), timeout=3)
        self.addCleanup(connection.close)
        return connection

    def read_response(self, connection):
        response = http.client.HTTPResponse(connection)
        response.begin()
        return response.status, response.read()

    def test_split_request_line_headers_and_body_dispatch_once(self):
        with patch.object(bridge, 'REQUEST_HEADER_SECONDS', 1):
            server, catalog = self.start_server()
            connection = self.connect(server)
            body = b'{"id":"candidate"}'
            fields = (f'ST /api/workspace-selection HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n'
                      f'X-SVN-Desk-Token: {server.token}\r\nContent-Type: application/json\r\n'
                      f'Content-Length: {len(body)}\r\n\r\n').encode()
            for chunk in [b'PO', fields[:15], fields[15:-1], fields[-1:] + body[:3], body[3:]]:
                connection.sendall(chunk)
                time.sleep(.01)
            status, payload = self.read_response(connection)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(payload)['fixture'])
            catalog.select.assert_called_once_with('candidate')
            self.assertEqual(len(catalog.mock_calls), 1)

    def test_stdlib_request_line_header_line_and_count_limits_remain(self):
        server, catalog = self.start_server()
        cases = [(b'GET /' + b'x' * 65536 + b' HTTP/1.1\r\n\r\n', 414),
                 (b'GET / HTTP/1.1\r\nX: ' + b'x' * 65536 + b'\r\n\r\n', 431),
                 (b'GET / HTTP/1.1\r\n' + b'X: v\r\n' * 101 + b'\r\n', 431)]
        for data, expected in cases:
            with self.subTest(expected=expected, size=len(data)):
                connection = self.connect(server)
                connection.sendall(data)
                self.assertEqual(self.read_response(connection)[0], expected)
        self.assertFalse(catalog.mock_calls)

    def test_shutdown_drops_trickled_pre_dispatch_requests_within_total_budget(self):
        for initial in [b'GET /', b'GET /api/status HTTP/1.1\r\nX-Pending: ']:
            with self.subTest(initial=initial):
                entered, stop, finished = threading.Event(), threading.Event(), threading.Event()
                class ObservedHandler(bridge.Handler):
                    timeout = .4
                    def handle_one_request(self):
                        entered.set()
                        super().handle_one_request()
                with patch.object(bridge, 'REQUEST_HEADER_SECONDS', .25):
                    server, catalog = self.start_server(ObservedHandler)
                    connection = self.connect(server)
                    connection.sendall(initial)
                    self.assertTrue(entered.wait(2))
                    def trickle():
                        while not stop.wait(.03):
                            try:
                                connection.sendall(b'x')
                            except OSError:
                                return
                    writer = threading.Thread(target=trickle)
                    writer.start()
                    def shutdown():
                        server.begin_drain()
                        server.shutdown()
                        server.server_close()
                        finished.set()
                    closer = threading.Thread(target=shutdown)
                    closer.start()
                    try:
                        # The sender stays active through this assertion. An
                        # idle-only timeout cannot pass by waiting for silence.
                        self.assertTrue(finished.wait(1.5), 'Header trickling kept desktop drain alive.')
                        self.assertFalse(catalog.mock_calls)
                    finally:
                        stop.set()
                        connection.close()
                        writer.join(3)
                        closer.join(3)
                    self.assertFalse(writer.is_alive())
                    self.assertFalse(closer.is_alive())


if __name__ == '__main__':
    unittest.main()
