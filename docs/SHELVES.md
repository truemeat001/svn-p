# Local shelf backups

A shelf is a local, verified backup. **Creating one does not revert, remove, commit,
or upload anything in the working copy.** Shelf storage defaults to the user's
application-data directory, outside the working copy. It is not SVN's experimental
native `svn shelve` format and is not automatically synchronized to another machine.

## Supported changes

- Modified regular files, including binary data and exact line endings.
- Complete SVN file and directory properties, including binary property values.
- Simple scheduled file/directory additions and scheduled regular-file deletions.
- Unversioned files and directories, recursively, including empty directories.
- Ordinary file/directory permission bits. Ownership, ACLs, timestamps, extended
  attributes, alternate streams, and hard-link relationships are not archived.

Backups verify SHA-256 checksums, binary-safe properties, source stability, and
manifest validity before publishing a shelf. File data is flushed to disk. POSIX
also flushes directory metadata. An interrupted operation may leave a temporary
folder or operation lock; no working-copy data is automatically reverted.

## Deliberately refused

Copies and moves with history, replacements, deleted directories, missing paths,
conflicts, obstructions, switched nodes, externals, working-copy locks, sparse
selected directories, symbolic links/reparse points, special filesystem objects,
and nested working copies are refused instead of silently losing their semantics.
A scheduled deletion whose file still contains local data is also refused.

Select an added/unversioned parent together with its children. An incomplete child
selection is rejected. Paths that cannot be represented safely on Windows, paths
with case-insensitive collisions, and paths containing `.svn` are rejected.
Ignored items are not selected automatically. An explicitly selected unversioned
directory is backed up completely, including files ignored by a parent rule.

## Restoring safely

A restore does not merge. Before making any change it verifies:

1. The repository UUID and working-copy URL match the shelf.
2. Every affected versioned node is clean at the recorded BASE revision, URL,
   content, and properties.
3. Actual file bytes match SVN's translated pristine BASE, not merely the cached
   status result. `svn export -r BASE` to a temporary local file handles native
   line endings and keyword expansion correctly. This does not contact the server.
4. Every new/unversioned destination is absent, including identical existing files.
5. All shelf data checksums and all destination paths are safe.

An added directory left behind after `svn revert` is a collision. Move or remove
it deliberately before restoring. Keep unrelated local work safe: never use
recursive revert or directory deletion just to bypass a collision warning.

The shelf is retained after success. All predicted conflicts abort before writes.
Unexpected disk or SVN failures can leave a **partial restoration**; the error names
changed or attempted paths. There is no automatic destructive rollback. Inspect
status and preserve remaining work before retrying.

Do not edit, update, commit, or run another SVN client concurrently with restoration.
The UI serializes its jobs, and managers constructed with the same `SvnClient`
reuse its lock, but another process or editor cannot be locked out reliably.

## Python API

```python
from svn_client.backend import SvnClient
from svn_client.shelves import ShelfManager, ShelfError, ShelfConflict

client = SvnClient("/path/to/working-copy", executable="svn")
shelves = ShelfManager(client)  # Reuses configured executable and operation lock.
shelf = shelves.create("Before refactor", paths=["src"])
summary = shelves.list()
verified_details = shelves.details(shelf["id"])
# Once affected paths have been deliberately made clean at their original BASE:
restored = shelves.restore(shelf["id"])
# Deletes the backup only. Applications should confirm this separately.
shelves.delete(shelf["id"])
```

`ShelfManager(path, storage_dir=...)` also works and discovers the trusted installed
SVN executable lazily. A manager cannot store shelves inside its working copy.
Returned summaries have `id`, `name`, `created_at`, `paths`, `files`, `entry_count`,
`backup_only`, and `repository_url`. `ShelfConflict` is a `ShelfError` subclass.

## Read-only planning API

`preflight_create(paths=None)` performs the same selection, unsupported-state,
BASE/property, file-integrity, hierarchy, and source-stability checks as creation,
but does not create a shelf. It returns `operation: "create"`, `dry_run: true`,
`can_create: true`, `paths`, `files`, `entry_count`, `entries`, repository identity,
and `warnings`. A shelf name is not an argument to this method; clients should
validate their requested name separately (nonblank, at most 200 characters).

`preflight_restore(shelf_id)` verifies the shelf hashes and performs the same
repository, BASE, clean-content, collision, and path checks as restoration. It
returns the shelf summary, `operation: "restore"`, `dry_run: true`,
`can_restore: true`, `shelf_retained: true`, entries, repository identity, and
warnings. Unsupported states raise `ShelfError`; conflicts raise `ShelfConflict`.

`list()`, `details(shelf_id)`, and both preflight methods never create a shelf
storage directory or an operation lock. `details()` verifies the stored hashes,
so it is also suitable for inspecting a proposed shelf deletion. BASE verification
may create short-lived files in the system temporary directory, which must be
outside both the working copy and shelf storage. These files are cleaned up. SVN
read commands may use their own local cache metadata internally; the methods do
not change working-copy content, scheduling, or properties.

Preflight is an advisory snapshot, **not a reservation or authorization**. It holds
an in-process client lock only, and refuses an already-running shelf operation.
Another process can change the working copy or shelf after checking. `create()`
and `restore()` always repeat validation rather than trusting an earlier plan.
Successful preflight does not guarantee available disk space, write permissions,
or freedom from later I/O/SVN failures. Execute only with the caller's separately
obtained permission.

## Verification status

The test suite contains deterministic filesystem/command tests plus real local-SVN
integration tests. The real tests automatically skip when `svn` or `svnadmin` is
missing. A skipped test is not evidence of runtime compatibility with an installed
SVN release; run the suite on the target Windows/SVN installation before trusting
important work to a new installation.
