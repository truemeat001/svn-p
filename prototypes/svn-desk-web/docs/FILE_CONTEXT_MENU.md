# Exact-selection file context menu · 2026-11-04

Base: `d9f3a342f81ba7195e803d1b4b84a9311a02a584` on
`codex/svn-integrated-warning-fix-20261103`.

This closes a concrete P4V-style interaction gap without adding SVN operations:
the previous Web file rows offered only click/checkbox/arrow/space navigation.
The menu reuses established workflows. It is not evidence that server locks,
conflict resolution, checkout, repository browsing or drag/drop are implemented.

## Selection and operation contract

- Right-click an already selected row: preserve the complete current selection.
- Right-click an unselected row: select only that row. No parent directory or
  changelist membership is added. Existing selected IDs absent from current rows
  cannot enter the menu scope.
- Display the focused relative path, selection count, and hidden selection count.
- Diff and history target the focused file. They open their existing UI, without
  automatically reading local content or contacting the repository.
- Review, changelist, local Shelf creation and commit use the complete exact
  selection. Commit always enters `openCommit('selected')`, not full-group commit.
- Existing write capabilities and validity checks determine enabled actions and
  are checked again at dispatch. All existing preflight and separate confirmation
  stages remain. No apply request is sent by opening the menu or its dialogs.
- Default/demo modes keep their prior capabilities. Demo grouping remains
  explicitly simulated; Shelf and repository commit stay unavailable there.
- No new HTTP endpoint, arbitrary command, clipboard/export, path input, backend
  change, Shelf format change or repository side effect was introduced.

## Keyboard and invalidation

Shift+F10 or the ContextMenu key opens the menu for a focused file row. Up/Down
wrap among enabled actions; Home/End move to the first/last enabled action.
Native buttons provide Enter/Space activation. Esc closes and focuses the current
row found by stable ID. Tab closes and returns the tab order to that row before
allowing normal browser traversal; external clicks and focus changes do not steal
focus back. Repeated activation after closure is ignored.

Opening captures row-array identity, mode, workspace ID, workspace selection
version, snapshot token, focused ID and exact selected IDs. Dispatch rejects stale
scope. Every full render, busy transition, modal opening, external scroll/pointer/
focus event, window resize or blur closes the menu. A stale/removed row is never
used for focus restoration.

The menu is fixed-positioned with an eight-pixel viewport inset and bounded
scrollable height. Dynamic paths are HTML-escaped. Positioning uses individual
CSSOM properties rather than assigning a style-attribute string or relaxing CSP.

## Checks actually run

- 55 JavaScript tests passed, including 16 new context-menu tests: 6 model,
  5 controller DOM-adapter, and 5 independent listener-review tests.
- 261 Python tests ran: 259 passed, 2 existing opt-in real-SVN tests skipped.
- JavaScript syntax checks and standalone HTML regeneration passed.
- Independent code review found no blocking scope/confirmation issue; it added
  event listener regressions. The old review-dialog claim that commit was absent
  was corrected to describe independently enabled preflight/confirmation.
- Existing backend/Shelf core and every Python service source remain byte-for-byte
  unchanged from the baseline. Tests use self-created temporary/synthetic data.

DOM-adapter tests execute the actual controller/listener source with small fake
DOM objects. They verify code branches and dispatch contracts, **not actual browser
layout, native key defaults, accessibility behavior or CSP enforcement**.

## Not run successfully / acceptance still needed

The installed Linux Chromium could not start: process-singleton `socket()` returned
`Operation not permitted`. No new browser assertion, screenshot or real-browser
pass is claimed. This environment has no `svn`/`svnadmin`; no real SVN, user's
working copy, credentials or repository were touched. Windows native key behavior,
WebView2, DPI scaling, Tauri/EXE/MSI and packaging remain untested.

Run in a permitted browser environment using installed Playwright/Chromium:

```sh
CHROMIUM_PATH=/path/to/chromium npm run test:context-menu:ui
```

The browser script uses the loopback demo service and synthetic HTTP responses.
It covers hidden selections, single-file replacement, action targets, no automatic
API requests, disabled write actions, keyboard navigation/focus, resize/blur,
small-viewport position, refresh and status failure. It does not constitute real
SVN acceptance. Native Windows acceptance must additionally verify the menu key,
Shift+F10, Tab/Shift+Tab, Enter/Space, high DPI, long names, and pointer placement.
Review all complete path lists and preflight/confirmation stages in a disposable
working copy before enabling writes against meaningful data.
