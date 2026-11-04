# Windows file-operation postcondition fix · 2026-11-04

This is a narrow follow-up to the Windows native SVN 1.15.0 validation of
`2a25d94eee7fa918e8db9b04947ea8ada5bbe925`, following the completed-result
retention and desktop-source checkpoints. It does not enable new write flows.

## Observed defects and exact fix

The native command succeeded once, but the wrapper rejected the postcondition
and returned an uncertain result. The original 52-check diagnostic had 49 passes
and three product failures, not a passing release gate.

- SVN Windows info XML spells `wcroot-abspath` with forward slashes. The wrapper
  compared it directly with the Windows backslash spelling. The fix allows only
  the native separator variant. It does not fold case, resolve aliases, collapse
  dot components or accept another working-copy root.
- For `svn status EXACT_FILE`, `moved-to` and `moved-from` are relative to the
  exact target itself, including its file name. For example, `src/other.txt` to
  `src/renamed.txt` reports `../renamed.txt`; `src/clean.txt` to
  `moved-clean.txt` reports `../../moved-clean.txt`. The wrapper now constructs
  the one expected relative counterpart and compares it exactly, with only
  Windows separator variants allowed. Both directions are checked.
- XML fixtures now model the native command instead of incorrectly returning
  root-relative move metadata. Copy history, source bytes, properties, exact
  repository URL, parent identities, nesting/link guards and single-use dispatch
  remain required. Unknown results are not retried or rolled back.

## Oversized HTTP test compatibility

The server intentionally does not drain an oversized submitted request body,
so Windows can abort the connection before the already-issued 413 arrives. This
is a test-policy correction, not a production transport relaxation. Five tests
now accept only an actual 413 or the specifically observed
`ConnectionAbortedError` with `winerror == 10053`, only while reading the response.
They independently assert no backend dispatch. Known non-413 status, errors
while connecting/sending, other socket/HTTP errors and within-limit requests
still fail. Tests that advertise but do not submit an oversized body still
require a complete rejection response.

## Validation boundaries

Cross-platform unit coverage exercises the real XML spellings with `ntpath`
and `posixpath`, wrong counterparts, wrong bases, absolute/foreign roots, case
aliases, extra dot components and junction/.svn detours. The old code fails the
corrected move reproductions; the fix passes. The original paired backend is
unchanged.

This checkpoint is locally tested with synthetic metadata, disposable files,
loopback HTTP and controller adapters. It must still pass a fresh Windows
real-SVN run using the exact final commit, followed by packaged executable and
WebView2 GUI acceptance. No new Windows, native build, browser or GUI acceptance
is claimed by these local tests.
