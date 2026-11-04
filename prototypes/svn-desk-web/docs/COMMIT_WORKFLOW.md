# Confirmed exact-scope repository commits

Base: `ca75b71d9a4f9a1a17470407b69452423fb64eeb`. This is another opt-in
modern web slice, not a released client or complete migration. The original
`svn_client` implementation is unchanged.

## Enable and review

```sh
python3 server.py --workspace "/path/to/disposable-test-wc" \
  --backend-source "/path/to/paired-original" --enable-commits
```

The default remains read-only. The flag is independent of `--enable-changelists`
and `--enable-shelves`. The page cannot enable it, change the authorized roots,
choose a repository URL, supply credentials, or run arbitrary commands. Compatible
strict status/history modules and the reviewed backend/history source digests
are required. Unknown source disables commits while retaining available safe reads.
CRLF is normalized to LF for the commit compatibility check; other changes fail
closed. This source check is version compatibility, not protection from a malicious
local process that can change the trusted Python program.

Two explicit actions:

- **Submit selected files** uses exactly the selected status IDs, including selected
  rows hidden by filters. It never adds other members of the same changelist.
- **Submit complete changelist** is a separate action. Its dialog shows every member
  of that named native changelist, including clean and hidden members. Missing or
  newly added membership aborts; over 100 members refuses instead of truncating.

Both require a nonblank message of at most 8192 UTF-8 bytes and an explicit Boolean
keep-label choice. Newlines and tabs are allowed; NUL, other control characters,
invalid Unicode, and oversized messages are rejected. The message remains in the
short-lived in-memory plan and is sent to SVN only after confirmation. It is not
included in HTTP error text, action logs, saved preferences, or the plan response.
Original Windows message-file handling remains in use.

The first button runs local preflight only. A separate final **Confirm repository
commit** button is enabled only after a matching response. Changing message,
keep-label choice, selection, workspace or snapshot, closing the dialog, refreshing,
or expiry invalidates the UI plan. The apply request contains only the reviewed
scope, a one-use token, and literal `confirmed: true`; it cannot change the plan.

## Safety boundary

- 1–100 exact ordinary versioned file targets; no directory scope or recursion.
  Dirty properties alone can be committed. Clean-only requests refuse.
- Native IDs stay native for transport; display/result paths use forward slashes.
  Case-fold aliases and duplicate IDs refuse.
- Missing, unversioned, ignored, obstructed, incomplete, conflicting, switched,
  external, locked, symlink, hardlink, reparse-point, cross-device and nested-WC
  targets refuse. A scheduled-deleted file may be absent if SVN identifies it as
  an exact versioned file in this working-copy root.
- All moved-file rows refuse in this web slice, even with both endpoints selected.
  Added/deleted/replaced/conflicting or otherwise unsafe parent-directory
  dependencies refuse. There is no automatic add, directory inclusion or repair.
- The service reuses the original strict changelist file verification and content
  fingerprint code. Each content pass allows at most 32 MiB per file and 64 MiB
  per selection, in 64 KiB chunks with a 10-second cooperative hash budget.
- Working properties are read by one fixed local `proplist --xml --verbose
  --depth empty` command per target via the original bounded process transport.
  Each XML is limited to 256 KiB; one metadata pass totals at most 4 MiB. DTDs,
  entities, unexpected scope, duplicate/invalid property encodings and `svn:special`
  refuse. File identity is checked before and after these reads.
- Canonical property values and local `info` bind BASE revision, repository UUID/URL,
  schedule and copy metadata. This catches property edits even when status remains
  `props=modified`, and repository/base replacement with otherwise identical rows.
  Values and digests remain local; neither is exposed as repository browsing.
- Exact files, state, membership, file identity/content and metadata are checked
  again during apply. A changed plan is consumed and rejected. Plans expire after
  120 seconds, including expiry during revalidation.
- Named submissions reuse original `preflight_changelist_commit` and
  `commit_changelist` with the full expected membership. Selected-file submissions
  reuse the original peg-safe, Windows-limit-aware target transport with fixed
  `commit`, `--message`, `--encoding UTF-8` and `--depth empty` options. Both retain
  original message redaction and optional `--keep-changelists` behavior.
- Catalog/adapter locks prevent simultaneous in-app selection, read and mutation.
  Beginning a write invalidates all status, changelist, Shelf and commit capabilities.
  Successful dispatch is followed by fresh status; any post-check failure is unknown.

Preflight does not contact the repository, establish authorization/remote locks,
prove the server is up-to-date, or run server hooks. Actual confirmation uses the
installed trusted SVN executable and its existing configuration/credentials.
Per-process timeouts still apply. Content hashing has cooperative timing; blocked
filesystem calls cannot be interrupted by that timing. This is not an atomic
transaction with external editors or another SVN process. Avoid editing these
files or running another client between review and completion.

## Failure semantics

Zero exit with any stderr is not treated as confirmed success. Once dispatch may
have started, timeout, errors, lost responses and post-status failure are **unknown**:
the repository may already have accepted the commit. There is no automatic retry,
rollback, revert or compensating write. Check repository history with a trusted
client before deciding to make another attempt.

Closing an active dialog does not cancel SVN. The UI clears old state, blocks
refresh until the in-flight request settles, discards late success, and then asks
for explicit refresh/history inspection. Repeated clicks cannot dispatch the same
plan twice. Successful UI text says only that the command completed and local
status was reread; it does not parse localized output or invent a revision number.

## Validation and remaining acceptance

All fixtures use synthesized temporary working-copy layouts, mock SVN XML/process
results, and loopback HTTP. No user project, real repository or credential is used.
`test_commit_bridge.py` additionally exercises actual HTTP -> catalog -> adapter ->
commit service -> original command construction, with the SVN process mocked.
Independent review adds adversarial metadata, token and command-result regressions.
Final counts are recorded in `VALIDATION.md`.

Run:

```sh
npm test
npm run test:commits:ui
```

The browser test is synthetic route-contract coverage, not real SVN. In this Linux
execution environment Chromium cannot create its process-singleton socket
(`Operation not permitted`), so no browser assertions or screenshots were produced.
`svn` and `svnadmin` are absent. Windows execution, real SVN commit behavior, UI
layout/interaction acceptance and native Tauri packaging remain **not run**.

Before acceptance, use only a self-created disposable repository and working copy:

1. Exercise nested paths, Unicode, spaces and `@` filenames on Windows. Confirm the
   fresh result uses display paths without falsely reporting an unknown outcome.
2. Commit one selected file while another file in the same changelist is dirty;
   verify only the exact file changes in the repository.
3. Review a complete named group with clean/filtered-hidden members; check full
   inclusion and rejection when any member is added or removed before confirmation.
4. Change same-size contents and restore mtime, edit a property while its status
   remains modified, and alter BASE/repository identity; each old confirmation must
   refuse without sending `commit`.
5. Cover property-only changes, scheduled deletes, keep-label choices, empty-message
   rejection, clean-only selection, moves, directories, added ancestors, externals,
   symlinks/reparse points, hardlinks and nested working copies.
6. Exercise cancel, Escape, duplicate confirmation, expiry, refresh, workspace switch,
   delayed preflight, close during apply, failed hooks, out-of-date files and lost
   responses. Verify no implicit retry, scope expansion or false rollback claim.
7. Repeat existing read-only, local-label, Shelf and delayed-body HTTP regressions.

Do not conduct these checks against a real user project or remote production repository.
