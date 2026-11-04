"""The oversized-body exception must not hide other transport/product errors."""
import errno
import http.client
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from http_body_test_helpers import assert_oversized_body_rejected


def windows_abort(code=10053):
    error = ConnectionAbortedError('synthetic Windows connection abort')
    error.winerror = code
    return error


class OversizedBodyAssertionTests(unittest.TestCase):
    def setUp(self):
        self.connection = Mock()
        self.response = self.connection.getresponse.return_value
        self.response.status = 413
        self.verify = Mock()
        self.factory = patch('http_body_test_helpers.http.client.HTTPConnection', return_value=self.connection)
        self.factory.start()
        self.addCleanup(self.factory.stop)

    def check(self, *, body=b'x' * 5, limit=4):
        assert_oversized_body_rejected(self, SimpleNamespace(server_port=8765, token='test'),
                                      '/api/test', body=body, limit=limit,
                                      assert_no_dispatch=self.verify)

    def test_accepts_413_and_always_checks_no_dispatch(self):
        self.check()
        self.connection.request.assert_called_once()
        self.response.read.assert_called_once_with()
        self.connection.close.assert_called_once_with()
        self.verify.assert_called_once_with()

    def test_accepts_only_observed_windows_10053_abort_and_checks_no_dispatch(self):
        self.connection.getresponse.side_effect = windows_abort()
        self.check()
        self.connection.request.assert_called_once()
        self.connection.close.assert_called_once_with()
        self.verify.assert_called_once_with()

    def test_allowed_body_abort_requires_already_observed_413(self):
        self.response.read.side_effect = windows_abort()
        self.check()
        self.response.status = 200
        self.response.read.reset_mock()
        with self.assertRaises(AssertionError):
            self.check()
        self.response.read.assert_not_called()

    def test_connect_or_send_abort_is_not_suppressed(self):
        error = windows_abort()
        self.connection.request.side_effect = error
        with self.assertRaises(ConnectionAbortedError) as caught:
            self.check()
        self.assertIs(caught.exception, error)
        self.connection.getresponse.assert_not_called()
        self.connection.close.assert_called_once_with()
        self.verify.assert_called_once_with()

    def test_other_os_and_http_errors_are_not_suppressed(self):
        ordinary = OSError('not the observed connection-abort class')
        ordinary.winerror = 10053
        errors = (windows_abort(10054), ConnectionAbortedError(errno.ECONNABORTED, 'POSIX abort'),
                  ConnectionResetError(errno.ECONNRESET, 'POSIX reset'),
                  BrokenPipeError(errno.EPIPE, 'broken pipe'), ConnectionRefusedError(),
                  TimeoutError(), PermissionError(), ordinary,
                  http.client.RemoteDisconnected(), http.client.BadStatusLine('invalid'),
                  http.client.IncompleteRead(b'partial'), RuntimeError('unexpected'))
        for error in errors:
            with self.subTest(error=type(error).__name__, code=getattr(error, 'winerror', None)):
                self.verify.reset_mock()
                self.connection.getresponse.side_effect = error
                with self.assertRaises(type(error)) as caught:
                    self.check()
                self.assertIs(caught.exception, error)
                self.verify.assert_called_once_with()

    def test_no_other_status_is_accepted_and_dispatch_assertion_still_runs(self):
        for status in (200, 400, 403, 409, 500, 503):
            with self.subTest(status=status):
                self.verify.reset_mock()
                self.response.status = status
                with self.assertRaises(AssertionError):
                    self.check()
                self.verify.assert_called_once_with()

    def test_normal_or_empty_body_cannot_use_the_exception(self):
        for body in (b'', b'x', b'xxxx'):
            with self.subTest(body=body):
                with self.assertRaises(AssertionError):
                    self.check(body=body)
                self.connection.request.assert_not_called()

    def test_any_backend_dispatch_fails_even_with_413_or_allowed_abort(self):
        self.verify.side_effect = AssertionError('backend was dispatched')
        for error in (None, windows_abort()):
            self.connection.getresponse.side_effect = error
            with self.assertRaisesRegex(AssertionError, 'backend was dispatched'):
                self.check()

    def test_utf8_byte_length_not_character_count_controls_limit(self):
        self.check(body='中文', limit=4)
        self.assertEqual(self.connection.request.call_args.kwargs['body'], '中文'.encode('utf-8'))


if __name__ == '__main__':
    unittest.main()
