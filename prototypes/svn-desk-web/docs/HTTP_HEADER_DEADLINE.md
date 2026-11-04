# Total HTTP header deadline and native graceful close

Base checkpoint: `81af9ce4180074fcc53883e3dc21a6d108069a83`.
This is a source-level lifecycle repair, not a new packaged Windows preview or
acceptance of the remaining P4V workflows.

## Reproduced defect

The request body already had a five-second total deadline, but the request line
and headers used only a five-second socket idle timeout. Python's buffered
`readline` can receive another byte before each idle timeout and continue waiting.
Its line-length and header-count limits eventually stop the request, but allow
an unfinished request to occupy a handler for hours or days.

The native bridge intentionally joins its non-daemon HTTP handlers when closing,
so it could remain at the waiting screen even when no SVN operation had started.
One unauthenticated loopback connection was enough to reproduce the stall with
shortened test timeouts. No real repository or working copy was involved.

## Repair and boundaries

- One **five-second monotonic deadline** spans acquisition of both the request
  line and all headers. Receiving bytes and starting a new header never reset it.
- The wrapper uses the existing buffered stream's `peek` and `read1`, consumes
  only the current line, and checks time before and after reads. Coalesced body
  bytes remain in the same stream for the existing body reader.
- Python's HTTP parser still enforces request-line size, header-line size and
  header-count limits. Header timeout closes the connection before API dispatch;
  an incomplete request is not guaranteed a formatted HTTP error response.
- After parsing, the socket's normal idle timeout is restored. Request bodies
  retain their independent total deadline and limits. Rejected bodies retain
  their bounded drain behavior.
- The header deadline does **not** cancel an already-dispatched command. Native
  close still stops admission and waits for accepted operations, with no forced
  process termination, automatic retry, rollback claim or daemon request thread.

This is still a loopback prototype, not a general denial-of-service-resistant
HTTP server. Connection/thread counts are not capped by this repair. An admitted
SVN operation or blocked filesystem call can legitimately keep shutdown waiting;
that separate lifecycle limitation is not masked by killing a process.

## Verification

`tests/test_header_deadline.py` covers shrinking deadlines, a shared budget across
lines, a successful read arriving after the deadline, exact line/EOF behavior,
preservation of coalesced bodies, ordinary split headers, stdlib framing caps, and
live loopback desktop drain while a sender keeps trickling request-line/header
bytes. The test sender remains active until the drain assertion, so silence cannot
make the old idle-only implementation pass.

The existing accepted-operation drain test uses a shorter header budget than the
mock backend operation, and still waits for the operation to finish. All fixtures
are synthetic and local. Current results are recorded in `VALIDATION.md`.

Windows socket behavior, a rebuilt frozen bridge, the actual WebView2 window,
close/crash/restart GUI interactions and real SVN must be validated on the exact
new commit before claiming Windows acceptance. This patch does not implement
modern-UI update, revert, checkout, conflict resolution or full P4V parity.
