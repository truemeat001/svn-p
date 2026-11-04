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
