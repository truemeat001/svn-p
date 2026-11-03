# Exact-file read-only workflows · 2026-11-03

This source checkpoint extends integrated `5dbaff4` with real backend wiring for file comparison and history. It is not a release, a replacement for the native client, or a claim of real-SVN/Windows acceptance. The earlier status-warning fix has separate Windows acceptance. Later Windows tests of `9864460` confirmed history pagination and identified mixed diff encoding and BASE → BASE issues; see [DIFF_ENCODING_FIX.md](DIFF_ENCODING_FIX.md) for the corrective checkpoint and its pending real-SVN retest.

## What is implemented

- Explicit one-file BASE → WORKING diff, explicit revision comparisons, and paginated path history (50 entries/page). Viewing a row does not start a diff or remote request.
- History opens a disclosure first. The user clicks to read; numeric/HEAD revision comparison and history may access the repository using existing SVN configuration/credentials. No authentication or credential-saving flow is added.
- History details show the complete commit's changed-path metadata, which can include paths outside the selected file. Those paths cannot be used as navigation/read targets. Comparing a revision reads only the current file's r(N−1) → rN range.
- Canonical revision strings preserve integers through 2^63−1 without JS number rounding. One page is retained, and the bounded back stack keeps at most 100 start tokens.
- Read failure clears previous data and is never represented as no differences/no history/clean state. Empty textual diff does not establish binary equality. Truncated text is clearly a preview, not a complete patch.
- On short laptops the live diff panel has compact network disclosure and expandable limitations. The demo's earlier dimensions remain unchanged.

## Security and reuse

`FileReadService` delegates parsing, repository identity checks, credential redaction, noninteractive SVN execution, and bounded pipes to the existing `svn_client.history.HistoryManager`. It never uses the unbounded generic `client.diff()` or `client.log()` capture path. The bridge's private subclass adds fixed `--depth empty` to diff so a read never recursively expands into descendants; the original directory-capable native UI retains its existing scope behavior.

Requests require the session token, exact origin/host/fetch policy, strict bounded JSON, current workspace ID, selection generation, a fresh status snapshot token, and an ID from that snapshot. A refresh invalidates the old token even if the refresh fails. Catalog and adapter locks reject concurrent operations rather than queueing a read against a changing workspace. No arbitrary filesystem path, URL, executable, command, page size, output limit, or revision range is accepted from the browser.

Path checks reject absolute/drive/UNC paths, parent or dot components, empty components, `.svn`, control characters, ambiguous separators, Windows stream syntax, trailing-dot/space components, links/reparse points, hardlinks, device crossings, nested working copies, directory targets, external/switched rows, and unsupported status types. SVN info must confirm that the one target is a file owned by the selected root; HistoryManager verifies repository UUID and unswitched URL mapping. Path identity is checked before and after reading. A missing/deleted leaf may be read only when its existing parent scope is safe and SVN reports a file; missing parent chains are rejected.

This remains a local desktop bridge, **not a sandbox against malicious local processes**. Path-based checks reduce observable replacement races; they do not make SVN filesystem operations atomic. A network-mounted local working copy may access its backing filesystem.

No write endpoint, arbitrary file-content API, upload, checkout, update, commit, revert, grouping mutation, Shelf change, shell invocation, trust-server-cert option, external diff tool, auth cache write, or automatic retry was added.

## Output and failure policy

- Diff capture: 8 MiB hard bound. Display: 256 KiB UTF-8, with original/returned byte counts and truncation disclosure.
- History capture: 4 MiB hard bound. Display page: 512 KiB before the small response identity envelope; the client also caps the full response at 512 KiB, so a borderline envelope is conservatively rejected.
- Diagnostic capture: existing 64 KiB cap, never echoed to the Web UI. A failing or oversized command returns no partial result.
- Each backend subprocess uses the existing 25-second adapter timeout plus bounded cleanup. A multi-command read can take longer than one subprocess timeout. Closing/navigating invalidates presentation, not the already running SVN process; it may continue until its bounded completion. There is no claim of immediate cancellation.
- `HISTORY_DIAGNOSTICS_POLICY=1` requires exit zero **and no stderr bytes** for every bounded history/info/diff read. This deliberately rejects even harmless localized notices, wrapper banners, or whitespace. It does not guess from English text or error numbers. Fix the emitting client's configuration with a trusted SVN tool rather than silently treating warnings as a complete/empty result.
- File reads also require `DIFF_TEXT_POLICY=1`: UTF-8 interpretation with explicit visible byte/control escapes and warnings, never whole-stream locale fallback. BASE → BASE is rejected before SVN. See [DIFF_ENCODING_FIX.md](DIFF_ENCODING_FIX.md).
- If the trusted history module lacks either capability, status can still work but file reads stay disabled. Existing strict status checks in both `backend.py` and `status_stream.py` remain unchanged.

## Original checkpoint verification

Source/API tests on this Linux environment:

- JavaScript model: **20/20 passed**.
- Web Python: **79 run, 77 passed, 2 real-SVN skips**.
- Paired backend aggregate: **961 run, 915 passed, 46 skipped**.
- Backend history-focused suite: **70 run, 68 passed, 2 skips**.
- Backend benchmarks: **13/13 passed**.
- Python compilation, JavaScript syntax checks, and rebuilding the offline HTML passed.
- Independent source review found no confirmed blocking defect. It independently ran the Web and history suites and rechecked the final non-recursion hardening with 23 focused file-read tests. Two later tests additionally cover duplicate status IDs and disabling only file reads for a missing history capability.

The first full history-focused run exposed an old test that intentionally accepted stderr while draining both pipes. Its expectation was changed to successful bounded draining **followed by rejection**, consistent with the new stated policy; the subsequent run passed. No failed run is counted as a pass. One benchmark invocation was made from the wrong directory and did not execute tests; the corrected invocation from the backend root passed all 13.

### Actual Chromium observations

Linux Chromium was used against a running real HTTP/catalog/adapter fixture, with the real HistoryManager parser/scope semantics and **synthetic SVN transport only** (`tests/serve_readonly_fixture.py`). Observations at 1180 × 757 CSS viewport:

1. Initial selection did not read a diff. Explicit BASE → WORKING displayed the expected synthetic text, including literal `<script>` text without creating a script element.
2. Editing the revision field immediately removed the previous diff and retained the entered revision.
3. Opening history displayed the repository-access disclosure without entries; clicking Read returned 50 rows.
4. Older/newer page navigation replaced the one page, rather than appending. Editing the start revision removed the previous history.
5. Selecting r100 showed escaped message text and another path labeled as whole-commit metadata. Explicit r99 → r100 comparison returned the current file's synthetic text.
6. A failing file returned the generic sanitized error, with no previous diff and no synthetic secret diagnostic.
7. A delayed diff followed by focus change never displayed its late result in the newly focused file.
8. Closing during delayed history left the dialog closed and empty after the command completed.
9. Switching from SYNTHETIC-alpha to SYNTHETIC-beta cleared prior results despite identical filenames; the next explicit diff contained beta's distinct fixture marker.
10. The short-laptop live preview was visually rechecked after compacting disclosure and increasing only the live panel's height. Returned diff text is visible; the demo layout is unchanged by this live-only rule.

The browser's captured console included extension metadata errors; these are not application exceptions. The deliberately failing read also generates an expected failed HTTP request.

`tests/readonly.browser.test.cjs` adds synthetic automated regressions for delayed reads, duplicate clicks, form edits, identity mismatch, focus/preview changes, pagination, close, failure, and same-ID workspace changes. **This Playwright script did not execute**: launching a new Chromium process was blocked by the runtime's local socket restrictions. The manual browser checks above are separate evidence, not a claimed automated-script pass.

## Remaining acceptance

No `svn`/`svnadmin` is installed in this environment. New real SVN network/history/diff behavior, historical rename/copy/deletion edge cases, native Windows path/encoding/diagnostics behavior, and Windows WebView2/Tauri still need acceptance. No Rust build, installer, release, main merge, or deployment occurred. The original checkpoint changed strict `history.py` completion and its tests; the subsequent encoding fix also updates native comparison warnings. Shelf was not changed.

Suggested Windows read-only acceptance on an existing explicit test working copy: ordinary text edit, property-only diff, binary notice, Chinese/space/`@` filename, deleted file, directory and external/switched refusal, old snapshot rejection, history pagination, revision compare, output-limit rejection, zero-exit stderr rejection, and close/navigation during delayed repository reads. Use a trusted installed SVN client with preconfigured credentials; do not add auth prompts or write operations to the Web bridge.
