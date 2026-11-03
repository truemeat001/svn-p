# Invalid working-copy status correction · 2026-11-03

## Failure found in Windows acceptance

The earlier `94d85a2` checkpoint accepted a directory with an empty `.svn` marker when installed SVN returned `W155007` on stderr, valid empty status XML and exit code 0. The UI then treated it as an empty live snapshot and saved that invalid selection. `.svn` presence and a zero process exit code are insufficient to establish a successful local status read.

## Correction

- Native streaming status now requires complete valid XML, process completion, exit code 0 **and no stderr diagnostic bytes**. A warning with empty XML or with valid entries rejects the entire snapshot.
- The custom `command()` transport applies the same policy, including nonzero returns. Errors retain bounded, redacted diagnostics and do not attach full XML stdout.
- The check does not rely on English warning text or a particular warning number. Whitespace prefixes, truncated diagnostics and a decoder returning an empty string cannot hide emitted stderr.
- A valid working copy with no changes still succeeds with an empty row list when SVN emits no diagnostics. Empty rows alone are not rejected.
- The Web adapter requires the strict diagnostics policy in both backend modules. Older or partially updated backends fail before constructing the SVN client. Update the matching backend and restart the local service before retesting.
- Failed selection returns HTTP 503, leaves the previous selected root/generation/preferences unchanged, and does not return live or demonstration rows.

Only the read-only status completion path changed in the original backend. No SVN write operation, remote query, credential prompt or automatic retry was added. Existing process limits, cancellation, draining, locks, argument handling and diagnostic redaction remain in place.

## Windows test fixtures

The three symlink tests now skip only when fixture creation raises `WinError 1314`, meaning the test process lacks the Windows symlink privilege/Developer Mode capability. Other errors still fail the test, and product assertions are never wrapped in that skip. Ordinary path-replacement and read-only status tests remain active.

## Validation at this checkpoint

- Original backend aggregate: 956 run, 910 passed and 46 skipped. Benchmark regressions: 13 passed.
- Native/custom status focused regressions: 45 passed using real Python child-process pipes, not SVN.
- Independent source review found no blocking issue and independently reran the 45 status tests and 34 workspace tests (32 passed, 2 real-SVN skips).
- Web model tests: 12 passed.
- Web Python: 54 run, 52 passed, 2 real-SVN tests skipped because SVN is not installed here.
- The Web regression passes through real HTTP, catalog, adapter and native status transport, substituting a Python process that emits W155007 plus exit 0. It verifies prior root, generation and preference bytes are unchanged after HTTP 503, and that refreshing the prior valid synthetic workspace succeeds.
- A new real-SVN invalid-marker test runs whenever installed SVN and the matching backend are available. It creates only a temporary directory and `.svn` marker, then invokes read-only status. No checkout/update/commit is needed.

This checkpoint still requires a fresh Windows real-SVN retest. Synthetic tests are not evidence that the Windows failure has been resolved in the installed environment.
