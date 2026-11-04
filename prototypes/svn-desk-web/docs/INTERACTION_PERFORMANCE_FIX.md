# Keyboard focus and directory-count correction

This patch keeps file selection and directory filtering semantics unchanged. In the actual row keyboard handler, Space first assigns the keyboard row as the focused file before toggling it. Previously, Tab could move keyboard focus without updating the logical focus, so Space selected one file and then moved focus/preview back to an older file. Hidden selected files remain selected; no range or folder expansion was added.

Directory navigation counts now accumulate ancestors in one path pass, followed by a second pass for status rows naming a directory itself. Counts preserve exact segment boundaries, root totals, Unicode display paths and directory-self rows. Sorting and markup generation remain, but there is no full-row scan for each directory. The model does not change native IDs, rows, changelists or selected IDs.

## Verification

- `node --test tests/folder_counts.test.js tests/row_keyboard.test.js` runs actual-controller DOM-adapter tests, exact-count edge cases, 500 deterministic randomized comparisons with the prior algorithm, and a path-access bound for 10,000 sibling folders.
- `node benchmarks/folder-counts.mjs` compares the old and new actual directory renderers on 2,000, 4,000 and 8,000 synthetic rows, requires identical generated markup and reports the median of three runs. It includes sorting/string construction but excludes browser DOM creation/layout, Windows rendering and SVN.
- `npm test` includes the new regressions with the existing Node and Python suite.
- `python3 build-standalone.py` regenerates the offline demonstration from the same source.

This is not a complete large-project optimization: the main table still rebuilds rows and uses per-row index lookup. There is no row virtualization, incremental selection paint or browser/Windows acceptance claim. Native browser verification remains required before shipping a new Windows package. No real working copy or repository is required by these new checks.
