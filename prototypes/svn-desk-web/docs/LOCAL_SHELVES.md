# Local Shelf create / restore vertical workflow

This is an opt-in modern-UI bridge, not a release or a server shelf implementation.
It reuses the original `svn_client.shelves.ShelfManager` at the reviewed source
SHA-256 `c02c04799a08c1b44a14f6ddd0582386130dd176f8d297f2b8ac252569bd795f`
(Git blob `05c3b5606d082aa350ff4c60bcc405241929c78d`, integrated `5a8bc9c`).
The original source is unchanged. Its private adapter hooks are version-pinned;
a different source disables the web Shelf service rather than weakening checks.

## Launch and scope

```sh
python3 server.py --workspace /path/to/disposable-wc \
  --backend-source /path/to/paired-original --enable-shelves
```

`--enable-shelves` is a separate opt-in from `--enable-changelists`. Default
startup is unchanged and has no Shelf write capability. The browser cannot enable
it or change workspace/storage/executable authorization. Optional
`--shelf-storage /path/to/local-shelves` uses that exact original-format directory;
otherwise the original per-working-copy application-data directory is used.
Storage must be outside every working copy and all authorized scan/workspace
roots. No upload, shelf deletion, arbitrary file/command, commit, revert, merge,
remote shelf, automatic cleanup, migration or destructive rollback API is added.

The web slice supports **1–100 exact ordinary files** only. Creation accepts
supported modified, property-modified, simply added, scheduled-deleted or
unversioned files. It never expands a directory, silently drops clean rows or
selects siblings. It does not save/restore local changelist labels. Unsupported
parents, copied/moved/replaced/conflicted/switched/external/locked paths, links,
reparse points, hardlinks, cross-device targets and nested working copies fail
closed. Ancestor checks remain in the original manager.

## Create

Select files, including any hidden by filtering/collapse, then choose Create
Shelf. Enter a name, inspect the complete explicit path list, run preflight, then
separately confirm. Preflight creates no Shelf directory or lock and performs no
working-copy mutation. Original BASE verification may create and clean temporary
local files. Creation preserves every source file and property, and never reverts
or commits. New shelves use the original verified compressed **format 2**.

## Browse and restore

The local list loads only on an explicit click. It initially reads bounded
metadata, not verified blob content; unreadable, oversized or unsupported entries
are clearly unavailable. Select a Shelf and review the entire file list. Restore
always covers the whole reviewed Shelf, with no partial selection or expanded
scope. Preflight checks every blob, repository UUID/URL, original BASE revisions,
translated pristine bytes, properties and destination boundaries. Versioned
files must be clean at the recorded BASE; added/unversioned destinations must be
absent. Existing identical new files are still collisions.

The reviewed plan binds the manifest, target identity/content/mode, exact scope,
workspace ID/generation and status snapshot. Apply rechecks these and the original
manager repeats its own safety checks. Source bytes/properties/BASE/modes are
bound for creation too; compression metadata is excluded only from the create
source comparison because preflight has not selected actual compressed storage.
The stored restore manifest, including compression metadata, remains fully bound.

Original **format 1 and format 2** shelves remain readable/restorable without
rewriting them, when within this web slice's file-only limits. Directory/large
legacy shelves are not damaged or migrated; use the original application for
those. Every successful restore retains its Shelf.

## Plan lifetime and failures

Plans expire after 120 seconds, are server-side, and are consumed once before
execution-time checks. Refresh, another review, workspace selection, editing the
name or closing the modal requires a new UI preflight. A changed file or Shelf
cannot use the old confirmation. The server validates request identity before
SVN calls, and serializes service/catalog operations. Cross-window refreshes
invalidate previous status capabilities. It still cannot lock out another editor
or SVN process. Do not edit, update, commit or use other SVN tools concurrently.

Closing an executing dialog is not cancellation. The UI clears old status and
shows an unknown outcome until a verified result is received or an explicit
refresh is made. Failed/timed-out restore commands can have partially changed
file bytes, permissions, properties or SVN scheduling. A create may also have
published a backup before a later disk/status failure. Errors do not trigger
retry, rollback, Shelf deletion or implicit revert. Inspect status and the stored
Shelf before deciding what to do next.

## Bounds and limits

- 32 MiB per live file / decoded Shelf entry; 64 MiB total selected file content.
  Legacy format 1 is capped here too before blob validation.
- 1 MiB manifest read/serialized manifest and 1 MiB list response, 100 entries,
  200 listed shelves, at most 800 directory
  entries examined. List overflow returns no partial list.
- 4 MiB per SVN metadata/diagnostic-success capture; BASE `cat` is at most
  32 MiB. Aggregate stdout budget is 128 MiB per service operation. Any stderr,
  including zero-exit warnings, is refused using the paired strict process reader.
- Native SVN subprocess timeout is at most 25 seconds and cooperatively bounded
  by a 90-second operation budget checked at guarded steps. This is not a hard
  deadline for blocking filesystem I/O. Selected-file fingerprints also retain
  the 10-second cooperative budget from the label workflow.
- File limits are checked before/after original capture. The original stable-file
  verification detects observed changes, but bridge admission limits do not make
  external file growth atomic: original capture retains its own 16 GiB/file and
  64 GiB/Shelf hard streaming limits. No claim of adversarial-process isolation
  or universally bounded filesystem/disk usage is made.
- Compression, decompression, SHA-256 verification, atomic Shelf publication and
  retained-on-failure restore behavior are the original manager's implementation.
  Free space and later write permissions are not reserved by preflight.

## Validation checkpoint

Tests use disposable directories with the actual guarded service and original
Shelf algorithms/parsers, substituting only the SVN transport. They cover
backup-only create, v1/v2 restore, stale plans/bytes/properties, collisions,
manifest/blob corruption, limits, boundaries, one-use/expiry and uncertain errors.
The new UI source includes synthetic browser cases for create/restore, hidden
selection, invalidation, stale responses, repeated clicks and close during apply.

**This checkpoint has not run Windows real-SVN acceptance.** Original-manager
acceptance does not count as acceptance of the new web adapter. Browser execution
was attempted but Chromium aborted with `socket() failed: Operation not permitted`
in this environment, including the approved escalated command. No browser pass,
new screenshot, native Tauri build or Windows installer is claimed. Exact final
unit/model counts and review findings are recorded in the checkpoint report.
