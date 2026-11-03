# Confirmed local changelist workflow · 2026-11-03

This checkpoint extends the modern Web UI, based on integration commit `86a3237ee26746545605490895a54ceaca4d96a3`. It does not replace the existing application, publish a release, or change the original SVN backend or Shelf implementation.

## Scope and operator consent

The default bridge remains read-only. `--enable-changelists` opts into exactly two native local label operations: assign selected versioned files to a named changelist, and remove their changelist labels. The browser cannot turn the option on, choose arbitrary filesystem paths, change executable/configuration, call generic commands, add unversioned files, commit, revert, or invoke Shelf.

The UI shows every selected file, including items hidden by filters or collapsed groups, with source labels. A local preflight is followed by a separate explicit confirmation. Only the exact selected file IDs from the successful current snapshot are accepted. A group is a local working-copy label, never a numbered server changelist. Files can belong to one native changelist; empty groups have no independent persisted identity. See [Subversion's changelist documentation](https://svnbook.red-bean.com/en/1.7/svn.advanced.changelists.html).

Demo mode explicitly simulates label changes in page memory. It never posts a mutation request, writes files, or implies real SVN success. Reload restores demo fixtures.

## Request and execution safety

- Existing loopback Host/Origin/Fetch Metadata/session-token checks, exact routes, CSP and no CORS remain enforced.
- Preflight body is strict JSON with only workspace ID, selection version, snapshot token, 1–100 unique exact IDs, action and name. Maximum body is 256 KiB; other endpoints retain their previous limits. Name is 1–128 Unicode characters, trimmed, with control characters forbidden. Remove requires `name: null`.
- Apply accepts only the same workspace/snapshot identity, an opaque reviewed plan token and literal `confirmed: true`. Browser-supplied new names, paths or IDs cannot alter a reviewed plan. Each adapter stores only one plan, valid for 120 seconds. Starting a new preflight replaces it; apply consumes it before revalidation. Refresh discards it. Old snapshot capabilities also fail after workspace replacement/selection.
- Catalog and adapter operation locks serialize local operations and reads across windows. No background queue, automatic retries, recursive directory selection, or fallback to whole workspace exists.
- Local verbose status includes clean named members. The complete collected status is rejected above 50,000 entries, then normal ungrouped files are hidden. This may reject a large mostly-clean working copy. It is not a streaming UI or total-memory bound.
- Reuse the trusted original backend's `validate_changelist_paths`, `assign_changelist`, `remove_changelist`, exact-target/peg handling and `--depth empty`. No direct SQL edits or new SVN command implementation.
- Strict bounded HistoryManager local info verifies file kind, exact WC root, repository UUID and URL ancestry without remote access. Physical checks reject symlinks, reparse points, hardlinks, cross-device paths, nested WC markers and unsafe path components. Conflicted, missing, obstructed, external, switched and locked rows are not eligible; scheduled deletions still require SVN file metadata and a safe existing parent.
- Revalidate selected row metadata/labels, file identities, SHA-256 content digests and WC metadata before execution. Recheck status and content after lengthy backend validation and expiry just before invoking the native operation. No plan reserves a file against other local programs.
- Before a native operation begins, discard old snapshot/plan capabilities. Validate the resulting exact labels, unchanged selected status fields and physical file identities before returning a new snapshot.
- Any execution failure, timeout, diagnostics (even exit-zero stderr), or postcheck mismatch is an uncertain result. No rollback is claimed and no automatic retry occurs. Raw command output is never returned to the web page. Refresh, review and a new confirmation are required.

## Content binding correction after Windows acceptance

Real Windows SVN acceptance of checkpoint `c797d99` exposed a stale-plan defect: an equal-length content edit followed by restoring mtime could pass metadata-only identity checks. The plan now also retains SHA-256 for each selected existing file. Content and digests are never returned to the browser, logged, uploaded, or sent to a repository. Scheduled-deleted absent files have a distinct absence marker, not the empty-file digest.

Fingerprinting retains every existing path-chain refusal, then opens one read-only descriptor. Before reading, its device, inode, size, mtime and link count must match the checked leaf; it must be a regular, non-reparse, single-link file, and the complete path chain is checked again. Path and descriptor checks repeat after reading. Windows path ctime and handle ctime are deliberately not compared across APIs because Python versions differ on creation/change-time semantics; raw handle ctime must remain unchanged across the read.

Limits are 32 MiB per file, 64 MiB of selected content per fingerprint pass, 64 KiB read chunks, and 10 cumulative seconds spent fingerprinting per pass. An EOF growth probe may read one extra byte per file before refusing a changed file. A preflight uses two passes (before and after backend validation); successful apply uses three (two validation passes plus postwrite comparison), bounded to 128 MiB and 192 MiB respectively, plus the EOF probes. Over-limit, short, growing, replaced, changed, or unreadable files fail closed with no metadata-only fallback. The elapsed checks are cooperative, not a hard deadline for blocked filesystem open/read calls, and do not change existing SVN subprocess limits.

This fixes binding to content observed during review; it does not reserve or lock files against other applications. Content can still change after the final check or during the original backend's own validation/native operation. A detected postwrite change is an uncertain outcome, not a rollback. The original no-concurrent-edit guidance still applies.

Closing the dialog during preflight discards late confirmation. Closing during apply does not cancel the request: the old UI snapshot is cleared and the eventual result is shown without reopening the dialog. Selection/target edits, refresh and workspace changes invalidate confirmation. Repeated clicks cannot submit the same UI plan twice.

## Remaining limits

The existing native backend's generic `info` calls inside changelist validation and final command capture retain their original subprocess timeout/capture behavior. The bridge adds bounded identity prechecks and selected-count limits but does not claim a new hard memory bound for those original transports. A preflight on many files can require many local info calls. Mounted network filesystems may incur filesystem network I/O even though no repository command is requested.

Local file/WC checks and per-process locks cannot make path-based SVN operations atomic against a hostile local process. The original assign/remove methods also perform their own target validation after the bridge checks, so another SVN tool can change files or labels in that interval. Do not modify the selected files concurrently with another SVN tool. Other applications can still race the final native command; postchecks report uncertainty rather than assert rollback. Do not expose this prototype server to a LAN or the Internet.

## Validation

Content-binding correction: 126 Python tests ran, 124 passed and 2 real-SVN tests skipped; all 26 JavaScript model tests passed. The 40 focused changelist tests include restored mtime with unchanged synthetic Windows path metadata, same-length edits during backend validation and native execution, descriptor mismatch before content reads, handle changes during reads, byte/time limits, absence versus empty files, and native Windows fixture ID/argv separators. Fixture display paths and repository URLs keep forward slashes and peg suffixes are preserved. The symlink fixture is independent, skips only WinError 1314, and cannot skip directory/hardlink/nested-WC tests. Compilation/syntax and offline rebuild checks passed. These are synthetic/Linux regressions; Windows and real-SVN acceptance of this correction is pending.

Recovery/checkpoint verification on 2026-11-03 reran all 110 Python tests (108 passed, 2 real-SVN tests skipped) and all 26 JavaScript model tests successfully. The unchanged original backend suite also reran: 975 tests, 929 passed and 46 skipped. Python compilation and JavaScript syntax checks passed. The offline HTML was regenerated from the final source; a second build produced the same SHA-256. The paired original backend files were individually matched against integration commit `86a3237ee26746545605490895a54ceaca4d96a3`, with no backend differences. A fresh read-only code review found no checkpoint-blocking issue.

The recovery environment's Chromium smoke rerun failed before any browser assertion at `socket() failed: Operation not permitted`, including the supported elevated retry. Therefore the five earlier browser passes below remain historical verification, not newly rerun acceptance. The later browser suites were not started after the smoke launch failed. Real SVN, Windows, native Python 3.10 and packaged acceptance are still pending. This is a development checkpoint, not a release.

Source/API verification: 110 Python tests ran, 108 passed and 2 real-SVN tests were explicitly skipped; this includes 24 new dedicated changelist tests. 26 JavaScript model tests passed. An independent read-only review reran the source/API suites and found three issues that were corrected: selected labels changing during lengthy validation, replaced `.svn` metadata during validation, and a plan expiring while validation runs. The new regressions cover all three. Python compilation, JS syntax and the offline rebuild pass.

The changelist browser contract suite passed through a cloud Linux desktop terminal using installed Chromium. Direct executor Chromium launch had failed at restricted socket setup before assertions; that failure is not reported as a pass. The desktop browser run covers opt-in, full hidden selections, assign/remove, malformed plan, input/selection/refresh/Esc invalidation, expiration, repeated clicks, closing during apply, uncertain outcomes and demo isolation. Five actual Chromium suites now pass through the cloud desktop: existing smoke, workspace, readonly-file tests; new changelist contract tests; and new real-HTTP changelist integration. The integration fixture runs the actual bridge/catalog/one-use-plan service and original backend argument generation, with synthetic SVN transport. It verifies assignment, move-back of a clean named member, final return to default, refresh and Chinese/HTML-like names without markup execution. This is not real SVN acceptance.

Running the earlier browser suites also exposed and fixed two UI issues: startup configuration failure followed by live recovery could leave the default group folded, and Esc's queued close cleanup could briefly retain read results. Initial collapse now applies only to the demo, and cancel invalidates reads/confirmation synchronously. Existing assertions were preserved.

Unchanged original backend regression: 975 tests ran, 929 passed and 46 skipped. No original backend, history, status or Shelf source was changed in this feature. No real repository revision or working-copy metadata was modified by these tests. All automated new mutation tests use actual backend validation/argument generation with synthetic SVN transport; none operate a real working copy. This environment has no `svn` or `svnadmin`. Real Windows SVN, native Python 3.10, Tauri/WebView2 and packaged application checks remain distinct and unverified for this feature.

Run source/API regressions:

```sh
npm test
SVN_DESK_BACKEND_SOURCE=/path/to/paired/backend npm test
python3 build-standalone.py
CHROMIUM_PATH=/path/to/installed/chromium npm run test:changelists:ui
```

For real acceptance use a disposable local working copy. Verify default startup has no label capability, opt-in assignment from default, move between existing groups, removal to default, clean named members, Chinese/space/at-sign names, hidden selections, expiry and repeat clicks. Change a selected file/label or switch workspace during review and confirm refusal. Check file bytes and local Shelf remain unchanged, no repository revision is created, and no unrelated file receives a label. Never use a valuable working copy to test uncertain-error recovery.
