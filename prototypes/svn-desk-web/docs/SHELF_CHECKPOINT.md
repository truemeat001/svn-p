# Modern local Shelf checkpoint · 2026-11-03

Base integrated commit: `5a8bc9c70882607056aaba472b3489887c4e1f65`.
This checkpoint adds an opt-in modern-UI exact-file local Shelf workflow. It is
not a release, Windows acceptance sign-off, native installer or server shelf.

## Final executed checks

Environment: Linux cloud executor, Python 3.12.14, Node v24.22.1.

- `npm test`: **31 JavaScript model tests passed**; **156 Python tests run,
  154 passed and 2 explicit real-SVN tests skipped**.
- New `tests/test_shelf_ops.py`: **30/30 passed**, using real guarded services,
  HTTP/catalog integration and original Shelf algorithms/parsers with synthetic
  SVN transport and disposable temporary directories.
- `node --check web/app.js` and `node --check tests/shelves.browser.test.cjs`:
  passed.
- `python3 -m py_compile server.py workspace_bridge.py shelf_ops.py`: passed.
- `python3 build-standalone.py`: completed; offline demo regenerated. Shelf
  buttons remain unavailable in offline/demo mode, with no simulated backup claim.
- Independent review completed. Regression fixes verified: compression-only
  metadata no longer invalidates creation; safe parent-directory metadata does
  not reject nested selected files; oversized properties cannot publish a
  Shelf beyond the manifest read limit. Malformed shelf entries are isolated
  as unavailable list items and the list response has a byte cap.

Coverage includes exact selection, backup-only creation, v1/v2 restoration,
binary properties and added/deleted/unversioned files, source and destination
staleness, expiry/replay, workspace/snapshot scope, default-disabled and source
version gates, corruption/collisions, filesystem boundaries, serialized manifest
and response limits, genuine partial-restore error handling and retained backup.

## Unexecuted or blocked checks

- Browser test source `tests/shelves.browser.test.cjs` was added, but not run
  successfully. Bundled Playwright browser was absent and installed Chromium
  aborted on `socket() failed: Operation not permitted`, including an escalated
  attempt. No new UI screenshot or browser pass is claimed.
- No native Windows real-SVN test of this new adapter, Windows reparse-point
  fixture, Tauri compilation, EXE/MSI, production workspace or remote server
  operation was performed.
- Cross-device refusal is covered by a mocked filesystem identity. The synthetic
  transport does not establish native SVN CLI/Windows compatibility.

Next acceptance should use the frozen commit in a disposable Windows SVN
repository/working copy, verify create/restore and failure paths through the
actual browser, and retain a complete test record. Real project work is outside
this checkpoint's test scope.

See [LOCAL_SHELVES.md](LOCAL_SHELVES.md) for capability, format and limit details.
