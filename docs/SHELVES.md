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

## Storage and compression

New shelves use manifest format **2**. Each file records its uncompressed SHA-256
`blob`, exact `size`, `blob_encoding` (`raw` or `zlib`), and `stored_size`. Data is
compressed with standard-library zlib, level 6, only if it saves at least 5% and
256 bytes. Tiny files, empty files, and incompressible/already-compressed data use
raw bytes, so compression never increases the final blob size. Exact duplicate
file content shares one blob **within that shelf**, as before. Properties remain
binary-safe base64 in the manifest and are not converted or compressed.

Existing format-1 shelves remain readable, verifiable, restorable and deletable.
They are not rewritten or migrated automatically. Older application versions
that only understand format 1 cannot read new format-2 shelves. Back up the entire
shelf directory, including `manifest.json` and `blobs/`, together.

File capture, checksums and blob decoding use 1 MiB chunks instead of loading an
entire file into memory. New shelves allow at most **16 GiB per file and 64 GiB of
total file content per shelf**, counting duplicate destinations toward the total.
These limits are checked before decoding. Decoding refuses output beyond the
declared size, invalid checksums, truncated streams, extra bytes and concatenated
compressed streams. No unbounded decompressor flush is used. Legacy raw shelves
have no compression expansion, so these new size caps do not block old shelves.
SVN command output and the manifest/properties are still held in memory; this is
not a claim of bounded memory for every SVN or metadata operation.

To preserve a single source snapshot, creation stages raw and compressed
candidates for one file at a time, then keeps only the smaller eligible candidate.
Allow temporary free space for roughly **two copies of the current file**, plus
already-staged blobs and metadata. A restore needs temporary free space for the
decoded destination file. Insufficient space aborts without publishing a new shelf
or replacing that destination; earlier restored entries may already be changed.
Only verified, flushed data is published via the existing atomic directory rename.
Restore rechecks streamed data into a private temporary file before replacing the
destination. It retains the shelf on success and failure.

There is no cross-shelf shared blob pool, automatic migration, eviction, garbage
collection, or removal of abandoned crash folders/locks. Deleting one shelf cannot
invalidate another shelf. The operation lock continues to serialize cooperating
writers. After a crash, inspect leftover files and verify no operation is active
before manually removing a stale lock. External file edits are still not locked
out; never modify the storage or working copy while restoring.

### Reproducible synthetic measurements

The `test_representative_synthetic_storage_savings` filesystem test creates and
round-trips four deterministic fixtures. Measured blob sizes in this development
environment (Python 3.12.14, zlib 1.3.2):

| Fixture | Original bytes | Stored blob bytes | Saving |
| --- | ---: | ---: | ---: |
| Repeated JSON-style text, 4 MiB | 4,194,304 | 12,282 | 99.707% |
| Repeated 0–255 binary pattern, 4 MiB | 4,194,304 | 16,601 | 99.604% |
| Seeded pseudo-random binary, 4 MiB | 4,194,304 | 4,194,304 | 0% (raw) |
| That random data already zlib-compressed | 4,195,590 | 4,195,590 | 0% (raw) |

These are synthetic **blob-byte** savings, excluding manifest/directory overhead
and filesystem allocation. They are not measurements of a real project, overall
disk usage, SVN performance, or Windows compatibility. Actual compression varies
with content and zlib version. The test checks conservative savings and byte-exact
restoration rather than requiring implementation-specific compressed sizes.

## Deliberately refused

Copies and moves with history, replacements, deleted directories, missing paths,
conflicts, obstructions, switched nodes, externals, working-copy locks, sparse
selected directories, symbolic links/reparse points, special filesystem objects,
and nested working copies are refused instead of silently losing their semantics.
A scheduled deletion whose file still contains local data is also refused.
Creation and restore also validate every selected path's ancestors, including
the working-copy root. Selecting a child alone cannot bypass a switched, copied,
conflicted, or scheduled-deleted parent. Creation rechecks ancestor status before
publishing, so a changed ancestor invalidates the backup snapshot.

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
6. Parent directories are supported versioned nodes or new directories included
   in the shelf. A directory scheduled for deletion is rejected even when
   `svn delete --keep-local` left it on disk; unrelated added/unversioned parents
   cannot replace the original hierarchy.

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

## Bounded metadata batches

Creation and creation preflight read selected targets' `svn info`, working
properties and BASE properties in bounded batches instead of launching one
metadata command per file. Each command uses **explicit targets and `--depth
empty`**, at most 128 targets and a conservative 12,000-byte target-argument
budget. Longer selections are split without dropping entries or broadening to a
recursive query. Paths are checked for traversal, redirected ancestors and nested
working copies before the command. Every target still has an individual trailing
`@` peg-escape, after `--`.

Explicit child selections additionally read any omitted ancestors with bounded
`svn status --depth empty` batches. These reads do not select parent property
edits or scan unrelated siblings. Both creation and its final snapshot check
perform these reads; whole-working-copy selections already include the ancestors
and need no extra status commands.

XML entries are associated by normalized local path, never by response order.
Info must return every requested target exactly once. Property output rejects
unexpected targets and duplicate entries/names. SVN deliberately omits targets
with no properties, so missing property entries mean an empty map **only after
the complete command exits successfully**. Even well-formed partial XML from a
nonzero exit is rejected. These semantics were checked against the
[Apache SVN 1.14 proplist CLI](https://github.com/apache/subversion/blob/1.14.x/subversion/svn/proplist-cmd.c#L236-L306)
and [local BASE-property implementation](https://github.com/apache/subversion/blob/1.14.x/subversion/libsvn_client/prop_commands.c#L1257-L1411).

No metadata persists across operations. A fresh working-property batch is read
after source hashing and compared with the capture snapshot, even if SVN status
still says `modified`. Existing repeated status, source-byte, mode, unversioned
tree, per-file pristine `cat`, and translated `export -r BASE` checks remain. The
restoration algorithm still performs its own per-path checks; it does not trust a
creation snapshot or preflight result.

The deterministic 100-file regression compares the previous one-target-per-command
strategy with batching while checking identical manifests and restored bytes and
binary properties. Creation uses **207 simulated SVN calls instead of 603**:
info 101 → 2, properties 300 → 3, status 2 → 2, pristine cat 100 → 100, translated
export 100 → 100. This is 65.7% fewer command invocations, **not a measured 65.7%
runtime improvement**. Long paths or more than 128 targets require more batches.
Preflight and actual creation both reread everything; doing both still incurs two
complete checks. Native SVN/Windows timings remain unverified in this environment.

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
Method signatures and summary fields are unchanged by storage format 2. Verified
file entries returned by `details()` additionally carry the encoding and stored
size when the shelf uses format 2.

## Read-only planning API

`preflight_create(paths=None)` performs the same selection, unsupported-state,
BASE/property, file-integrity, hierarchy, and source-stability checks as creation,
but does not create a shelf. It returns `operation: "create"`, `dry_run: true`,
`can_create: true`, `paths`, `files`, `entry_count`, `entries`, repository identity,
and `warnings`. A shelf name is not an argument to this method; clients should
validate their requested name separately (nonblank, at most 200 characters).
Creation preflight hashes sources but does not compress or stage them; its file
entries use raw sizes as a conservative storage plan. Actual creation may choose
compression. The preflight is not a free-disk-space check.

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
