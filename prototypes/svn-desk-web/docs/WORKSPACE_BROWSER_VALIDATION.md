# Workspace browser verification · 2026-11-03

This is a follow-on verification of the workspace source checkpoint. It does not replace the explicit limits on real SVN, Windows or packaged builds.

## Environment and data

- Actual Linux Chromium, approximately 1180 × 757 CSS viewport.
- Running the checked-in `tests/serve_workspace_fixture.py`: real HTTP handler, real workspace catalog, unchanged original discovery/settings modules, temporary candidate directories, **synthetic status adapter**.
- All candidate names and status filenames are visibly labeled `SYNTHETIC`. The fixture never invokes SVN. The app's live-mode badge is being exercised with synthetic responses; it is not evidence of a real SVN working copy.
- The ordinary automated Playwright launch remained unavailable because the execution environment blocked Chromium's local socket setup. These are observed interactive browser checks, not a claimed pass of `npm run test:workspaces:ui`.

## Observed checks

1. Initial state is “真实模式 · 待选工作区,” with no rows and no fallback demonstration data.
2. Opening the picker discovers exactly three explicitly authorized synthetic candidates. The UI says candidates do not prove repository identity.
3. Selecting alpha returns its one synthetic row and reports that local preferences were saved. Alpha gets the current marker.
4. Select alpha's file, then search for a nonmatching term. The selected file is accurately counted as hidden.
5. Switch to beta. The workspace heading/path/rows switch together, the search becomes empty, file selection becomes zero despite the same row ID, and the current marker moves from alpha to beta.
6. Select the deliberately broken candidate. The UI reports that the local status validation failed, shows zero rows and “状态未知,” and does not show demonstration or cached rows. Raw fixture diagnostic text is not exposed.
7. Close the picker and explicitly refresh. The previous valid beta selection is restored in the UI with its one row.
8. Select alpha and close the picker while its request is in progress. The completed selection updates the main workspace, clears selection, and does not reopen the dialog.
9. Reload the page. The current alpha workspace and its synthetic row return with zero selected files. This checks page reload, not process restart; settings restart/authorization behavior is covered by Python tests.
10. Reopen the picker. Alpha remains marked current. The captured console warning/error entries were browser-extension metadata errors, not an observed application exception.

## Evidence

- `evidence/web-workspace-selected-synthetic.jpg`: selected alpha workspace, one synthetic status row, no selected files, no real diff/history, disabled write controls.
- `evidence/web-workspace-chooser-synthetic.jpg`: authorized candidate list and the correct current-workspace marker.

Screenshots are unmodified captures of the running browser and intentionally retain visible `SYNTHETIC` labels.

## Final source correction

After these interactive checks, the catalog response guard was tightened to reject a response with an older selection generation instead of applying it. The new model regression passed, bringing the model total to 12. The delayed-response race itself was not manually reproduced in Chromium; the supplied optional automation still requires a full run. No other functional source changed after the observed browser checks.

## Still not verified in a browser

Automated duplicate-click and intentionally delayed discovery-response cases are supplied in the optional browser script but have not run to completion here. Removed-current-workspace rescans, incomplete scan retention, startup settings restore, malformed HTTP requests and concurrency boundaries are covered by Python tests, not claimed as manually exercised browser cases. No native Windows, Tauri, real SVN or remote repository behavior has been tested.
