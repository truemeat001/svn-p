# Bounded HTTP rejection and Windows fixture repair

Base checkpoint: `ad382ca9bab4a098a5c09b9e48507f6e0278ccb4`.
The subsequent Windows real-SVN report passed the Shelf core workflow but found
an HTTP transport defect and two portability errors in the tests. This follow-up
is a repair checkpoint, **not Windows acceptance** until it is retested there.

## Rejected delayed requests

Previously, an unsupported POST route returned HTTP 405 and closed the socket
before reading its request body. A client sending headers, waiting 20ms, then
sending the body could receive Windows `WinError 10053` instead of a complete
rejection. Coalescing headers/body hid the defect. No SVN operation was started.

The handler now consumes a well-framed, bounded unread body before sending a
final rejection, including authorization/Origin failures and unsupported mutation
methods. Authentication and endpoint checks still happen before any backend
dispatch; rejected data is discarded, never parsed into a command. Existing
403/400/405 meanings are retained and the response explicitly closes the connection.

Bounds:

- At most **256 KiB** for rejection draining; no ambiguous/duplicate Content-Length
  or Transfer-Encoding framing is drained. Accepted small endpoints retain their
  **4 KiB** cap; changelist/Shelf endpoints retain their **256 KiB** cap.
- Bodies are read in at most **64 KiB chunks**, with a **5-second total monotonic
  deadline**. Each `read1` gets only the time left, preventing slow fragments from
  extending the body deadline. Discarded bodies are not accumulated.
- Supported requests that time out return **408** before dispatch. Truncated or
  malformed supported bodies fail closed. Oversized advertised input is rejected
  without waiting for it. A consumed or attempted body is never read again.
- Request-line/header idle reads also have a 5-second socket timeout. This is a
  loopback prototype, not a general DoS-resistant HTTP server: thread count and
  overall header duration are not newly capped by this patch.

Graceful response handling is limited to the framed/bounded input contract;
network delivery is not guaranteed, and Windows retesting is still required.
Oversized, ambiguous, truncated or late senders may still see transport closure
or reset; the server does not drain unlimited input just to preserve an error
message. It never runs an SVN operation to compensate for a failed response.

## Test portability

- Native relative backslash paths are legal on Windows and deliberately invalid
  as ambiguous literal separators on POSIX. Tests now assert both platform
  contracts rather than applying the POSIX expectation everywhere.
- POSIX executable-mode invalidation and cross-platform file-replacement/inode
  invalidation are separate fresh-fixture tests. Windows skips only the explicitly
  POSIX mode-bit case; it still runs the inode case. Failure in one cannot restore
  a file and contaminate the other.
- Shelf symlink, hardlink, nested working-copy and FIFO guards are separate tests.
  File-read symlink and hardlink guards are separate too. Missing Windows symlink
  privilege (WinError 1314) skips only the symlink-specific assertions; hardlink,
  nested-WC and other applicable checks still run. FIFO skips only where mkfifo
  is unavailable. Other fixture errors remain failures.

## Verification scope

`tests/test_http_socket.py` uses actual loopback TCP sockets and the real HTTP
handler, with a mock catalog and no SVN. It covers the 20ms delayed-body reproducer,
coalesced/split bodies, authentication/Origin/Host/content-type rejection,
unsupported verbs, malformed/oversized framing, truncation and timeouts. A
separate deterministic read1/clock regression checks the shrinking absolute
budget without sending bytes at a TCP-close boundary.

Linux tests and independent review are recorded in the follow-up section of
`VALIDATION.md`. No production working copy, real SVN, Windows execution or browser
acceptance is claimed by this repair pass. Run the same frozen commit on Windows
before calling the HTTP/fixture follow-up accepted.
