"""Narrow transport assertion for a deliberately oversized submitted body.

The bridge deliberately does not drain oversized bodies. Windows may abort the
connection while unread payload remains, losing the already-issued 413. Only
the observed ConnectionAbortedError/WinError 10053 is accepted here; unrelated
socket errors, HTTP errors, and all within-limit requests remain strict.
"""
import http.client


def _observed_windows_abort(error):
    if getattr(error, 'winerror', None) != 10053:
        raise error


def assert_oversized_body_rejected(case, server, route, *, body, limit, assert_no_dispatch):
    payload = body.encode('utf-8') if isinstance(body, str) else body
    case.assertGreater(len(payload), limit, 'This exception is only for an oversized body.')
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    try:
        # Connect/send failures are not the response-read failure observed in
        # Windows evidence, and must still fail the test.
        connection.request('POST', route, body=payload,
                           headers={'Content-Type': 'application/json',
                                    'X-SVN-Desk-Token': server.token})
        try:
            response = connection.getresponse()
        except ConnectionAbortedError as error:
            _observed_windows_abort(error)
        else:
            # A known non-413 response cannot become acceptable merely because
            # reading its body subsequently fails with the allowed abort.
            case.assertEqual(response.status, 413)
            try:
                response.read()
            except ConnectionAbortedError as error:
                _observed_windows_abort(error)
    finally:
        try:
            connection.close()
        finally:
            # Losing the response is never evidence that the request was rejected.
            # Independently prove that it did not reach the operation dispatcher.
            assert_no_dispatch()
