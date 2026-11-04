# Confirmed local changelist checkpoint · 2026-11-03

Current opt-in local label workflow, safety boundaries and new verification are in [LOCAL_CHANGELISTS.md](LOCAL_CHANGELISTS.md). This supersedes the older "no SVN write endpoints" statements below only when `--enable-changelists` is explicitly enabled. Default startup remains read-only; no commit/revert/Shelf APIs exist. Earlier counts describe their historical checkpoints.

---

# Mixed diff encoding correction · 2026-11-03

The latest source/API checks and outstanding Windows retest are in [DIFF_ENCODING_FIX.md](DIFF_ENCODING_FIX.md). Earlier counts below describe their own historical checkpoints.

---

# Read-only file workflow checkpoint · 2026-11-03

Current diff/history implementation and verification are documented in [READONLY_FILE_WORKFLOWS.md](READONLY_FILE_WORKFLOWS.md). The sections below retain historical checkpoints. Subsequent separately reported Windows real-SVN acceptance of `5dbaff4` supersedes the status-only pending-retest statements below; it does not validate the newer file workflows.

---

# Status-warning correction · 2026-11-03

The Windows real-SVN check of `94d85a2` exposed a fail-open empty-working-copy result. That checkpoint is superseded for real workspace use by the strict stderr completion check described in [STATUS_DIAGNOSTICS_FIX.md](STATUS_DIAGNOSTICS_FIX.md). Matching backend modules and a service restart are required. Windows retesting is pending; no pass is claimed from synthetic tests.

Current checks: original backend 956 run (910 passed, 46 skipped), 13 benchmark tests passed, and 45 focused backend status tests passed; 12 Web model tests passed; Web Python 54 run, 52 passed and 2 real-SVN tests skipped. Earlier counts and unchanged-backend statements below describe historical checkpoints only.

---

# Workspace checkpoint · 2026-11-03

Subsequent interactive browser verification with synthetic status is documented in [WORKSPACE_BROWSER_VALIDATION.md](WORKSPACE_BROWSER_VALIDATION.md).

This section supersedes earlier checkpoint counts below. It is a source/API checkpoint, not a real-SVN or Windows release.

- Node model tests: **12 passed**.
- Python: **49 run, 48 passed, 1 explicitly skipped**. Tests use the unchanged original discovery/settings modules with temporary `.svn` candidate markers and synthetic status adapters. The skipped test requires both installed SVN and an explicitly provided working copy.
- `node --check web/app.js`, Python compilation and the offline rebuild passed.
- The original backend, discovery, workspaces and status_stream sources were reused without edits. New code is contained in the web prototype.
- New coverage: authorized discovery, nested candidates, ID-only selection, selection generation, persistence/restore, tampered and corrupt preferences, save failures, stale IDs, symlink/root/ancestor replacement, change during read, concurrent operation rejection, partial scan disclosure, settings path exclusions, missing SVN, HTTP origin/token/schema/body-size/recursion checks, and selection invalidation after rescan.
- One initial combined run encountered a loopback HTTP timeout. A targeted rerun passed (26 passed, 1 skipped), and the subsequent final combined Python run passed (48 passed, 1 skipped). The earlier timeout is not represented as a pass.
- Source review found and corrected stale picker “current” markers, removed-selection inconsistency, nonatomic config metadata and nested-JSON error handling. A final source review also found that delayed discovery could overwrite a newer selection generation; older catalogs are now rejected and a model regression covers the version rule. Original safety modules were not changed.
- **Automated Chromium tests did not run successfully:** launching installed Chromium was blocked at its local Unix socket setup. `tests/workspaces.browser.test.cjs` and its synthetic fixture server are supplied but remain unverified. They do not constitute real SVN tests.
- At that earlier source/API freeze, the new workspace UI had not yet been browser-verified; the subsequent synthetic browser checks are documented separately. Older manual browser observations below apply to the preceding demo checkpoint only.
- No SVN or svnadmin installed. No real working-copy, repository/network, native Windows or Tauri verification. No SVN write operation was executed.
- Persistence writes only explicitly enabled local app preferences. There are no SVN write endpoints. Discovery time limits remain cooperative; row caps are post-collection checks, not total-memory bounds.

Optional workspace browser test: `CHROMIUM_PATH=/usr/bin/chromium SVN_DESK_BACKEND_SOURCE=/path/to/backend npm run test:workspaces:ui`. All status records in that test are synthetic and labeled SYNTHETIC.

---

## Previous demo checkpoint (historical)

# Verification report · 2026-11-03

## What was actually run

- Node 24.22.1: **11 model tests passed**.
- Python 3.12.14: **20 HTTP / adapter tests passed**.
- `node --check web/app.js` passed. Offline bundle regenerated from final source.
- Python local demo service actually started, and the app was opened and interacted with in **Linux Chromium on a Linux desktop**. This is a running application, not an image mockup.
- Browser screenshots are original captures of the running page at **1180 × 757 CSS viewport**, demo mode. They do not show Windows, Tauri or real SVN.

## Browser interactions observed

1. Initial state: 12 fixture rows in 3 changelist groups, default group folded, 8 rendered rows, `PlayerController.cs` selected. Demo identity and disabled write actions visible.
2. Search `Forest`: one match; selected PlayerController remains selected but is reported as hidden. Read-only review contains only PlayerController, not the search result.
3. Check Forest: review now contains exactly those two paths. Closing and reopening the dialog preserves selection. Closing via button and Esc both worked.
4. Collapse all → zero file rows; expand all → 12 file rows. No data lost.
5. Search an absent name → explicit empty state; reset restores results.
6. Select `Assets/Scripts/Player` directory → 3 matching files; selection is not expanded to the folder.
7. Select PlayerController → labeled fictional history; Esc closes it. Other files do not receive its fake history or diff.
8. ArrowDown moves focus to InteractionSystem without expanding selection; Space adds the focused file to selection.
9. Conflict navigation shows one conflict. Its details identify the conflict; no content is fabricated and no solve action exists.
10. Repository tab explicitly says it is not connected. Switching back restores the directory view.
11. Difference panel collapse works independently from the operation log.
12. The app's captured console has no application exception. A browser-extension metadata warning was present and is not an app test failure.

## Independent source review and corrections

A separate read-only review found three correctness defects, all corrected before the final checkpoint:

- Normalize Windows native path separators at the read-only adapter's display boundary while keeping the native row identity. Added regression.
- Include property-only modifications in “已修改” filtering, consistent with counts. Added regression.
- Fetch configuration on every refresh and apply snapshot identity, so retry after an initial configuration failure cannot keep the demonstration workspace name. The header name now uses the same identity.

The initial fixture-count assertion expected 7 modified files, but the actual fixture contains 6; the test expectation was corrected after inspecting all 12 entries. No failed checks are represented as passes.

## Source / API regression coverage

Host, Origin, Fetch Metadata and session-token checks; non-ASCII invalid token rejection; no CORS; strict CSP; static-asset allowlist; query rejection; mutation methods rejected; unknown APIs rejected; sanitized live failure without demo fallback; in-flight read rejection; fixed working-copy requirement; bounded row count; lock release on failure; status-only adapter contract; path/label escaping; exact selections and directory segment boundaries.

The adapter tests use mocks. They do **not** constitute real SVN integration testing.

## Not run / remaining gaps

- No `svn`/`svnadmin` in the current test environment: real working-copy integration and all real repository/network behavior remain unverified.
- No Rust/Cargo/WebKitGTK 4.1: Tauri source shell **not compiled or run**, no packaged desktop binary or installer.
- No native Windows / WebView2 / 100–200% DPI / cross-monitor verification.
- Optional `npm run test:ui` script is supplied but **not executed here**. It needs Playwright/browser installation. Manual browser interactions above are the browser evidence, not a claimed automated script pass.
- Synthetic config-recovery route in that optional browser script is not a real live SVN test; it remains unexecuted here.
- No large-list performance claim: full snapshot + DOM row rendering are still O(n); the 50,000-return-row guard does not bound the existing backend's total state memory or make the UI virtualized.
- No panel splitter dragging, persistent layout, changelist drag/drop, commits, reverts, grouping writes, Shelf, remote history, remote tree, diff reads, or arbitrary file reads.
- Read-only server is a local prototype, not a production multi-user service. It protects against cross-origin web access, not hostile local processes.

## Browser verification scope

Manual interaction with the running local demo passed in Linux Chromium. The optional automated browser script was not executed in the current test environment. These checks establish only the browser behavior listed above, not Windows, Tauri, real SVN, or packaged desktop behavior.

## Optional local automation

After deliberately installing development dependencies and Playwright Chromium:

```sh
npm run test:ui
# Or point to an already installed trusted browser:
CHROMIUM_PATH=/usr/bin/chromium npm run test:ui
```

This exercises demo behavior and mocked failure/recovery. It does not perform SVN writes.

## Final checkpoint review

Independent reviewer rechecked the fixes and independently reran **11 JS + 20 Python** tests successfully. No remaining checkpoint-blocking issue was found within the reviewed source/API scope. Final manual browser reload + refresh preserved the demonstration identity and one selected file. Four screenshot files are provided: default diff layout, list-focused overview, conflict filter and exact-selection review dialog.

## Local Shelf checkpoint · 2026-11-03

The opt-in exact-file local Shelf vertical workflow has 31 passing model tests
and 156 Python tests (154 passed, 2 real-SVN tests skipped), including 30 new
Shelf service/HTTP tests with synthetic SVN transport. Standalone generation and
Python/JavaScript syntax checks passed. Independent review fixes were rerun.

The new browser test source is present but browser execution was blocked by the
installed Chromium sandbox socket restriction. No new browser screenshot or
native Windows/SVN acceptance is claimed. See
[SHELF_CHECKPOINT.md](SHELF_CHECKPOINT.md) and [LOCAL_SHELVES.md](LOCAL_SHELVES.md).

## HTTP rejection / Windows fixture follow-up · 2026-11-03

Base `ad382ca9bab4a098a5c09b9e48507f6e0278ccb4`: the subsequent Windows report
passed the Shelf core workflow, but found delayed-body HTTP rejection and test
portability defects. This repair is not accepted on Windows until retested.

Final Linux repair checks:

- 31 JavaScript model tests passed.
- 175 Python tests ran: 173 passed and 2 explicit real-SVN tests skipped.
- 13 focused HTTP tests passed: actual loopback TCP delayed/coalesced/fragmented
  bodies, authentication/Origin/Host/content-type rejection, unsupported methods,
  malformed/oversized/truncated input and body timeout; deterministic clock/read1
  coverage additionally verifies the absolute deadline without TCP-close races.
- Shelf fixtures: 35 passed. File-read fixtures: 33 passed.
- Forced synthetic WinError 1314 over those 68 fixture tests: exactly 2
  symlink-specific skips, all other tests passed. No general error or hardlink,
  nested-WC, inode assertion was skipped by a symlink privilege failure.
- Independent review passed, including rerunning the final full suite and
  replacing the timing-racy trickle test. Original Shelf core and safety checks
  were unchanged; no browser or real project was touched.

Details and limits: [HTTP_REJECTION_FIX.md](HTTP_REJECTION_FIX.md). New Windows
real-SVN/HTTP rerun remains required; the Linux results are not that acceptance.


## Exact-scope commit slice · 2026-11-03

Base `ca75b71d9a4f9a1a17470407b69452423fb64eeb`. Original backend/history
and Shelf implementation remain unchanged. This is a development checkpoint,
not real SVN or Windows acceptance.

- 34 JavaScript model tests passed.
- 226 Python tests ran: 224 passed and 2 existing opt-in real-SVN tests skipped.
- 51 commit-specific Python tests passed: 23 core, 23 HTTP/adapter/catalog,
  and 5 independent adversarial-review regressions. The HTTP coverage includes
  actual loopback routing through the service and original target-command
  construction, with only SVN process/XML responses mocked.
- Python compilation and JavaScript syntax checks passed; offline demo rebuilt
  from the same web sources. Offline mode cannot dispatch repository commits.
- Independent review found and verified fixes for Windows native-ID versus
  display-path result mismatch and property-read ordering before path guards.
  Review also checked same-status property/BASE/UUID changes, one-use replay,
  incomplete membership, exact targets, expiry and uncertain-result semantics.
- Commit source compatibility accepts CRLF-to-LF equivalence only, rejects
  unknown changes, and leaves safe status/file reads available when disabled.

All data was synthetic or self-created temporary filesystem content. No user
project, actual SVN repository or credential was touched. `svn` and `svnadmin`
are not installed here. The new browser test source could not execute because
Chromium's process-singleton socket creation was denied by the environment
(`Operation not permitted`). No browser assertions, screenshot, Windows real-SVN
results, native EXE/MSI or Tauri packaging are claimed.

See [COMMIT_WORKFLOW.md](COMMIT_WORKFLOW.md) for limits, failure semantics and the
required disposable-repository Windows/browser acceptance checklist.


## Commit XML attribute-order repair · 2026-11-04

Base `68bae524950675ccdd21353b778456363022a322`. Subsequent Windows real-SVN
testing found a false 409 on unchanged reviewed files: repeated semantically equal
`info` XML had differing attribute order, producing three hashes in eight reads.
This is a reproducible metadata-serialization defect, not changed file contents.

- Fingerprinting now uses deterministic structured JSON, sorting only XML
  attribute entries recursively and retaining every other parsed XML component.
- 11 new XML regressions passed (8 implementation tests and 3 independent tests).
  They cover reordered attributes, repeated reads, selected/full-group apply,
  added/removed/changed attributes and elements, BASE/UUID/schedule/copy fields,
  text/tail/child-order changes and real same-status property edits.
- Full Linux suite: 34 JavaScript tests passed; 237 Python tests ran, with 235
  passed and 2 existing explicit real-SVN skips. Compilation checks passed.
- Independent review confirmed the order regressions fail before the fix and
  pass after it. Original SVN backend, exact targets, one-use confirmation,
  content hashing and workspace isolation are unchanged.

Only synthetic temporary fixtures were used for this repair. The reported Windows
run diagnosed the base commit; it does not validate the new repair. Windows real-SVN
retest, browser execution and native packaging remain pending. Restart the bridge
on the new full checkpoint, then repeat unchanged-file and real-change rejection
checks in a self-created disposable repository before acceptance.

## Commit diagnostic preservation · 2026-11-04

Base `b22f584bc81903076e3320d2b3a0c0e0925e389c`. The supplied Windows real-SVN
report verified the XML-order fix on Python 3.10 and 3.14, with unchanged metadata
now stable. Its real commit matrix found a separate warning-reporting defect:
a revision was created, but a post-commit hook failure in stdout was hidden because
SVN exited 0 with empty stderr. The repair retains completion and prominently
qualifies diagnostics instead of implying rollback or inviting duplicate submission.

- 39 JavaScript model tests passed.
- 261 Python tests ran: 259 passed and 2 existing explicit real-SVN tests skipped.
- 20 diagnostic-focused tests passed (8 implementation and 12 independent review
  tests), plus new service/HTTP tests for completed hook warnings, failed status
  refresh and workspace replacement after dispatch.
- Independent review found and verified a fix for catalog configuration overriding
  disabled read/write capabilities after a failed follow-up status check.
- Output caps, ASCII bounded revision parsing, localized/unknown output, raw secret
  suppression, nonzero/exception uncertainty, consumed tokens, and fixed frontend
  warning messages are covered. Python/JavaScript syntax checks passed and the
  standalone demo was rebuilt from the same frontend sources.

Only synthetic temporary fixtures, mock native output and loopback HTTP were used
for this repair. The supplied Windows run concerns the base, not acceptance of this
new fix. Browser contracts are updated but browser execution remains unperformed
because of the existing Chromium socket restriction. Windows real-SVN and native
packaging retests remain pending. No user project or credentials were accessed.
See [COMMIT_DIAGNOSTICS.md](COMMIT_DIAGNOSTICS.md) for the result contract and the
required disposable-repository retest sequence.

## Exact-selection file menu · 2026-11-04

Base `d9f3a342f81ba7195e803d1b4b84a9311a02a584`. The right-click and keyboard
menu reuses existing exact-scope workflows; no Python service, SVN backend or
Shelf format changed. It adds no API endpoint or implicit repository request.

- 55 JavaScript tests passed, including 16 new model/controller/listener tests.
- 261 Python tests ran: 259 passed and 2 existing real-SVN tests skipped.
- Independent source review and 5 separately written listener regressions passed.
- JavaScript syntax checks and standalone rebuild passed.

These new controller/listener tests use DOM adapters, not a browser. Actual Linux
Chromium launch was blocked by the environment's process-singleton socket
restriction. The browser acceptance script is provided but did not execute its
assertions. No new screenshot, real SVN, Windows, WebView2 or native packaging
result is claimed. Details and outstanding checks: [FILE_CONTEXT_MENU.md](FILE_CONTEXT_MENU.md).

## Exact-file repository locks · 2026-11-04

Base `069d454a118fd60641bcc5b172e50033d6c3e146`. Optional `--enable-locks`
adds explicit remote preflight and separately confirmed single-use lock/unlock
plans over the unchanged original LockManager. It never forces, steals or breaks
locks. Default status is still local; lock-enabled status includes clean rows.

- 71 JavaScript model/controller tests passed, including independent regression
  of existing commit input callbacks after lock integration.
- 294 Python tests ran: 292 passed and 2 existing real-SVN tests skipped.
- 21 lock service tests, 7 HTTP/catalog tests and 5 independent adversarial tests
  cover exact paths, ownership, mutation races, parent administrative locks,
  malformed remote metadata, partial/error outcomes and preserved completion.
- Independent review found a commit input callback regression; fixed and verified.
  An additional parent-directory administrative-lock check was independently
  reviewed and tested before any server request or write.
- JavaScript/Python syntax checks and rebuilt standalone demo passed.

All executed lock tests use synthetic temporary files and mock SVN, plus loopback
HTTP. The browser contract is available as `npm run test:locks:ui`; bundled
Playwright Chromium is unavailable and the installed Chromium launch was denied
at process-singleton socket creation. No bypass, real SVN/`svnadmin` operation,
Windows test, new screenshot or native package is claimed. Read
[FILE_LOCKS.md](FILE_LOCKS.md) for the disposable-repository acceptance checklist
and remaining safety/atomicity limitations.

## Exact-file local additions · 2026-11-04

Base `4554f2fbe7c602ec2e8c13897df60f5874504634`. Independent default-off
`--enable-adds` registers only 1–100 selected unversioned ordinary files. It
never adds parents, recurses, forces ignored targets, or commits. Existing SVN
backend source is unchanged; the fixed argv explicitly disables auto-props.

- 84 JavaScript tests passed, including model/controller and independent add
  result-preservation regression.
- 334 Python tests ran: 332 passed and 2 existing explicit real-SVN tests skipped.
- 40 add service/HTTP/catalog/independent tests cover exact scope, root/ancestor
  safety, content changes with restored mtime, links and alias refusal, strict
  input, TTL expiry, cross-operation invalidation and partial/unknown outcomes.
- Self-review fixed equivalent XML attribute order changing parent fingerprints.
  Independent review found and verified the fix for a frontend snapshot error
  overwriting an already validated successful add with an unknown outcome.
- Syntax checks and standalone rebuild passed. No original backend source changed.

All executed tests use synthetic temporary directories, mock SVN and loopback
HTTP, plus DOM adapters for the actual controller code. `npm run test:adds:ui`
was attempted with installed Chromium, but the browser aborted at startup with
`socket() failed: Operation not permitted`; no browser assertions ran and no
bypass was attempted. No real SVN/`svnadmin`, Windows/native package or new
screenshot acceptance is claimed. See [LOCAL_ADDS.md](LOCAL_ADDS.md) for limits
and the disposable-repository Windows acceptance sequence.

## d01bf15 Windows add follow-up · 2026-11-04

Subsequent isolated Windows real-SVN verification of `d01bf15` reported two Python
versions with 324 passed and 10 skipped each, 57 real-SVN behavior checks passed,
one additional command-audit recheck passed, and 84 Node tests passed. GUI was
not accepted. This evidence applies to the exact add checkpoint, not to later
property-view changes.

## Local single-file property comparison · 2026-11-04

Base `d01bf15cdf1e65d617716fde77f6d5d954c59235`. The property tab, file details
and context menu now offer an explicit local WORKING/BASE property read. The
original backend and its command allowlist remain unchanged. No property write,
remote revision, inherited-property or generic command route was added.

- 104 JavaScript model/controller/independent tests passed.
- 380 Python tests ran: 378 passed, 2 existing explicit real-SVN tests skipped.
- New backend and HTTP tests cover exact snapshot scope, repeated observations,
  fixed local argv, default read-only capability, revocation on mutation failure,
  malformed XML/base64, bounds, binary metadata and explicit unavailable BASE.
- Independent tests feed actual Python display serialization into the frontend
  validator, including astral Unicode, controls, empty/absent and binary values.
- Review added binding between returned BASE availability and the status captured
  when the user requested the read. Both mismatch directions are rejected.
- Python/JavaScript syntax checks and standalone rebuild passed. All tests use
  synthetic temporary files, mock SVN, local HTTP or DOM adapters.

The browser script is `npm run test:properties:ui`. It was not executed because
the immediately preceding checkpoint confirmed this environment blocks Chromium
startup at socket creation. The denied launch was not retried or bypassed. Real
SVN, Windows/native and browser acceptance remain pending for this slice; the
prior Windows add result above does not validate these new property reads.
See [FILE_PROPERTIES.md](FILE_PROPERTIES.md) for exact limits and acceptance steps.

## Property Windows test compatibility repair · 2026-11-04

Base `3a4cb076eb270fa99f5ef02d3b55d7974da78466`. The original Windows
aggregate reported 2 failures and 2 errors in property test fixtures: native
path expectations, unsupported concrete PosixPath construction, and missing
symbolic-link privilege. This is not recorded as a passing full suite.

The patch isolates platform simulation and the symlink test, preserves directory,
hardlink and nested-WC coverage, and skips only explicit Windows error 1314.
Production property/backend/UI code is unchanged. The previously reported
corrected targeted Windows check (4 passed, 1 skipped), 78 real-SVN behaviors
and one XML audit recheck do not replace a full rerun of this exact patch.

Isolated Linux validation against the original checkpoint plus this test patch:
104 JavaScript passed; 381 Python ran, 379 passed and 2 existing explicit real-SVN
tests skipped. Focused property tests: 23 passed. Independent review confirmed
only Windows error 1314 is skipped; all other simulated link errors propagate.
Windows full aggregate, GUI and native packaging acceptance remain pending.
See [PROPERTY_WINDOWS_TEST_FIX.md](PROPERTY_WINDOWS_TEST_FIX.md).

## Exact single-file move/rename and history-preserving copy · 2026-11-04

Base `59a762b4d697dbbe3b658123b0b36031f9ad3456`. Independent default-off
`--enable-file-ops` adds one exact source and one absent relative destination,
with local preflight and separately confirmed 120-second one-use plans. The
original FileOperationManager and all original backend files remain unchanged.
The wrapper bounds original metadata/content reads and the one native dispatch.

- 122 JavaScript model/controller/review tests passed.
- 464 Python tests ran: 462 passed and 2 existing real-SVN tests skipped.
- New file-operation service, HTTP/catalog and independent suites: 83 Python
  tests. New model/controller/review suites: 18 JavaScript tests.
- Independent review reproduced and fixed complete info-XML binding, destination
  URL/copy provenance, unsafe move-source post-state, expiry in the last dispatch
  callback, unexpected stdout under deterministic `--quiet`, malformed leaf XML
  diagnostics, and preserved verified results even when both snapshot adoption
  and error-state rendering fail. Each has a regression test.
- Native destination peg handling differs correctly for copy and move; no
  implicit parent creation, directory operation, forced overwrite, delete or
  commit route was added. Existing Web commit still rejects structural changes;
  users must review/commit the two move sides or copy target in a trusted client.
- Final syntax checks, standalone rebuild and an independent frozen audit are
  required before this source checkpoint is saved.

Executed checks use synthetic temporary files, mock SVN, local HTTP and DOM
adapters only. The browser contract is `npm run test:file-ops:ui`; it was syntax
checked, not executed. Prior Chromium socket denial was not retried or bypassed.
No real SVN, Windows filesystem, GUI, WebView2, native package, performance or
new screenshot acceptance is claimed. The separate property-test compatibility
checkpoint above does not validate this new workflow. Full limits, failure
semantics and disposable-Windows acceptance checklist:
[LOCAL_FILE_OPERATIONS.md](LOCAL_FILE_OPERATIONS.md).
