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

## Verification status

The test suite contains deterministic filesystem/command tests plus real local-SVN
integration tests. The real tests automatically skip when `svn` or `svnadmin` is
missing. A skipped test is not evidence of runtime compatibility with an installed
SVN release; run the suite on the target Windows/SVN installation before trusting
important work to a new installation.
