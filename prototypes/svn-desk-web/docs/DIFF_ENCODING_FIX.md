# Mixed diff encoding correction · 2026-11-03

This corrective checkpoint follows `9864460`. Windows real-SVN testing of that version found CP936 filenames in diff headers combined with UTF-8 file hunks. Whole-output code-page fallback changed valid Chinese body text into mojibake. It also exposed SVN E200004 for BASE → BASE. History pagination and other tested read workflows passed on that version; those results do not establish acceptance of this correction.

## Display policy

`svn_client.history` now declares `DIFF_TEXT_POLICY=1`. Both native and Web comparisons use the same decoder:

- Interpret valid UTF-8 sequences without changing them, including when adjacent header or content bytes are not UTF-8. Never fall back to the process locale for the complete stream.
- Display each undecodable byte as a visible `\xNN` escape. Escape unsafe control characters, preserving tabs, LF and CRLF. Return exact counts and the `utf-8-visible-escapes` marker when either count is nonzero.
- Show an explicit warning outside the clipped text in both native and Web previews. Counts describe the entire bounded command output, including bytes beyond the displayed prefix. The warning also remains visible when only a short preview is requested.
- This is a display policy, **not encoding detection or a reversible patch format**. Legacy bytes that happen to form valid UTF-8 are interpreted as UTF-8; no original-encoding claim is made. Original literal `\xNN` text and displayed byte escapes can look alike. Unknown legacy-encoded content is not guessed into characters. The separately selected Unicode filename remains visible above the diff.
- Reject the entire result if NUL or a UTF-16 BOM appears. This conservative rule may also reject a coincidental marker in other unknown encodings. No `--force` flag or binary payload mode is enabled. SVN's textual binary notice may be displayed, but an empty textual diff never proves binary equality.

The Web bridge requires both `HISTORY_DIAGNOSTICS_POLICY=1` and `DIFF_TEXT_POLICY=1`. Missing capabilities disable file reads without disabling otherwise valid status. Its response schema validates the three encoding fields and their counts; an old or malformed response is not silently shown.

## Scope and limits

BASE → BASE now receives a clear unsupported-pair explanation before any SVN target query or command, and before a browser POST. Use BASE → WORKING for local modifications or explicit numeric revisions for historical comparisons. No synthetic empty result substitutes for SVN's unsupported operation.

The fixture URL builder normalizes Windows paths with `Path(...).as_posix()` before URL quoting. Production still rejects encoded backslash URLs such as `/src%5Ca.txt`; strict path checks were not weakened. Regressions cover both facts.

Raw diff capture remains limited to 8 MiB; the Web display remains limited to 256 KiB UTF-8. Credential redaction occurs after visible decoding and before display truncation. Existing zero-exit stderr rejection, output caps, identity checks, exact-file `--depth empty`, snapshot/generation invalidation, sanitized errors and no-write boundaries remain intact. Shelf and status implementation files were not changed.

Replacement runs in 64 Ki-character chunks, preserving CRLF across boundaries. This bounds the temporary per-character replacement list. The final escaped intermediate text can expand up to 32 MiB before the display cap; this is not streaming total-memory containment. On one Linux Python 3.12 synthetic 8 MiB all-invalid-byte test, peak RSS decreased from 543,100 KiB to 104,152 KiB and decode time from 9.47 s to 5.84 s. These are measurements of that test, not Windows performance guarantees.

## Verification completed

- JavaScript model: **22 passed**.
- Web Python: **86 run, 84 passed, 2 real-SVN skips**.
- Paired backend aggregate: **975 run, 929 passed, 46 skips**.
- New mixed-encoding suite: **14 passed**. Covers CP936 headers/UTF-8 hunks, CP932 and CP1252 unknown bytes, partial sequences, locale independence, controls, UTF-16/NUL rejection, binary notices, redaction, truncation warnings, BASE refusal, native warnings, empty output and chunk boundaries.
- Backend benchmarks: **13 passed**.
- Independent review confirmed the policy, API/UI metadata validation and Windows fixture-only normalization. A separate reference comparison found identical results for **51** chunk-boundary and randomized samples. The memory concern found in the initial review was corrected and rechecked.
- Python compilation, JS syntax checks and offline rebuild passed.

Actual Linux Chromium interactions used the real HTTP bridge and HistoryManager with **synthetic SVN transport**, not an installed SVN client. A Chinese file showed intact `旧正文` / `新正文 ✓ UTF-8` body text with visible header byte escapes and the 12-byte warning for BASE → WORKING, explicit 1 → 2, and history r99 → r100. Editing revisions cleared prior text and warnings. BASE → BASE showed the helpful refusal without a completed-read log entry. Closing history removed both text and warnings. The fixture was stopped afterward.

The updated automated Playwright script includes encoding warning, escaped HTML, truncation, missing-metadata rejection and cleanup regressions, but **was not executed** because this environment cannot launch its Chromium test process. Manual interactions are separate evidence, not an automated-script pass. One interactive control attempt timed out and was inspected before retrying; the verified observations above were completed afterward.

## Remaining acceptance

No `svn` or `svnadmin` is installed in this Linux environment. This corrective source requires a fresh Windows real-SVN check, particularly Chinese filenames plus UTF-8 body under CP936, local and numeric revision comparison, visible undecodable bytes, native preview, binary/UTF-16 refusal, and the corrected Windows fixtures. Native WebView2/Tauri, installers, historical rename/copy/deletion edge cases and release readiness are not claimed. No SVN writes, Release, deployment or main-branch merge occurred.
