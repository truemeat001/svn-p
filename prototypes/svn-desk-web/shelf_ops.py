"""Explicit, one-use local Shelf plans over the paired original ShelfManager.

The web contract intentionally supports only exact ordinary files. Directories
and oversized legacy shelves remain usable through the original application.
No revert, commit, shelf deletion, upload, remote URL or arbitrary command API.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

from changelist_ops import (FingerprintBudget, content_fingerprint, status_rows,
                            MutationUncertain)
from file_reads import StaleRead, file_identity, strict_relative_path
from workspace_bridge import directory_identity, trusted_modules

MAX_FILES = 100
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_SHELVES = 200
MAX_LIST_BYTES = 1024 * 1024
PLAN_SECONDS = 120
OPERATION_SECONDS = 90
PAIRED_SHELVES_SHA256 = 'c02c04799a08c1b44a14f6ddd0582386130dd176f8d297f2b8ac252569bd795f'
COMMON = {'workspaceId', 'selectionVersion', 'snapshotToken'}
SHELF_ID = re.compile(r'[0-9a-f]{32}')


class ShelfUnavailable(ValueError):
    pass


def _text(value, limit):
    return (isinstance(value, str) and 1 <= len(value) <= limit
            and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
            and not any(0xd800 <= ord(c) <= 0xdfff for c in value))


def validate_request(kind, data):
    if (not isinstance(data, dict) or not COMMON <= set(data)
            or not _text(data['workspaceId'], 64) or not _text(data['snapshotToken'], 64)
            or type(data['selectionVersion']) is not int
            or not 1 <= data['selectionVersion'] <= 2**53 - 1):
        raise ValueError('Invalid Shelf workspace scope.')
    if kind == 'list':
        valid = set(data) == COMMON
    elif kind == 'preflight':
        if data.get('operation') == 'create':
            ids, name = data.get('ids'), data.get('name')
            valid = (set(data) == COMMON | {'operation', 'ids', 'name'}
                     and _text(name, 128) and name == name.strip()
                     and isinstance(ids, list) and 1 <= len(ids) <= MAX_FILES
                     and all(_text(p, 2048) for p in ids) and len(set(ids)) == len(ids))
            if valid:
                for identifier in ids:
                    strict_relative_path(identifier)
        else:
            valid = (set(data) == COMMON | {'operation', 'shelfId'}
                     and data.get('operation') == 'restore'
                     and isinstance(data.get('shelfId'), str)
                     and SHELF_ID.fullmatch(data['shelfId']))
    elif kind == 'apply':
        valid = (set(data) == COMMON | {'planToken', 'confirmed'}
                 and data.get('confirmed') is True and _text(data.get('planToken'), 64))
    else:
        valid = False
    if not valid:
        raise ValueError('Only a bounded, explicit local Shelf operation is allowed.')


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _logical_entries(entries):
    # Compression is chosen only when blobs are written. Bind source semantics,
    # not preflight's raw placeholder versus creation's final zlib representation.
    return [{key: value for key, value in entry.items()
             if key not in {'blob_encoding', 'stored_size'}} for entry in entries]


def _same_plan(value):
    return _digest({'repository': value['repository'], 'entries': value['entries']})


def _files(entries):
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
        raise ValueError('Shelf entry limit exceeded.')
    result, total, seen = [], 0, set()
    for entry in entries:
        if not isinstance(entry, dict) or not {'path', 'kind', 'state'} <= set(entry):
            raise ValueError('Invalid Shelf entry.')
        path = strict_relative_path(entry['path']).as_posix()
        size = entry.get('size', 0)
        if (entry['kind'] != 'file' or path.casefold() in seen
                or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES):
            raise ValueError('Only bounded exact regular-file shelves are supported.')
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError('Shelf exceeds the web content limit.')
        seen.add(path.casefold())
        result.append(dict(path=path, state=entry['state'], kind='file', size=size))
    return result, total


def shelf_module(source):
    trusted_modules(source)
    path = Path(source).resolve(strict=True) / 'svn_client' / 'shelves.py'
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != PAIRED_SHELVES_SHA256:
        return None  # Private adapter hooks require this reviewed source version.
    module = importlib.import_module('svn_client.shelves')
    trusted_modules(source)
    return module


def guarded_manager(module, history, client, check_workspace, storage_dir=None):
    """Keep original format/restore semantics, narrowing transport and web scope."""
    class WebShelfManager(module.ShelfManager):
        def __init__(self):
            super().__init__(client, storage_dir)
            self.expected = None
            self.deadline = None
            self.output_remaining = 128 * 1024 * 1024
            # Never put backups inside any other working copy either.
            if any(os.path.lexists(p / '.svn') for p in (self.storage_dir, *self.storage_dir.parents)):
                raise ValueError('Shelf storage must be outside every working copy.')
            self._check_storage()

        def _tick(self):
            check_workspace()
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise ValueError('Shelf operation time budget exceeded.')

        @contextmanager
        def bounded(self):
            self.deadline = time.monotonic() + OPERATION_SECONDS
            self.output_remaining = 128 * 1024 * 1024
            try:
                yield
            finally:
                self.deadline = None
                self.expected = None

        def _svn(self, *args):
            self._tick()
            if args[0] not in {'info', 'status', 'proplist', 'cat', 'export', 'add', 'delete', 'propset', 'propdel'}:
                raise ValueError('Unsupported Shelf command.')
            if args[0] in {'add', 'delete', 'propset', 'propdel'} and (not self.expected or self.expected['request']['operation'] != 'restore'):
                raise ValueError('Shelf mutation requires an executing reviewed restore.')
            limit = min(MAX_FILE_BYTES if args[0] == 'cat' else MAX_METADATA_BYTES,
                        self.output_remaining)
            if limit <= 0:
                raise ValueError('Shelf output budget exceeded.')
            try:
                value = history._bounded_process(
                    [str(self.executable), '--non-interactive', '--no-auth-cache', *args],
                    cwd=self.working_copy,
                    timeout=min(self.timeout, max(.01, self.deadline - time.monotonic())
                                if self.deadline is not None else self.timeout),
                    stdout_limit=limit)
            except Exception as exc:
                # The shared reader's diagnostics say "read" even for mutation.
                # Do not expose them as proof that a write was not attempted.
                raise module.ShelfError('Bounded Shelf SVN operation failed; outcome requires inspection.') from exc
            self.output_remaining -= len(value)
            self._tick()
            return value

        def _path(self, relative):
            self._tick()
            path = super()._path(relative)
            if relative != '.':
                rel = strict_relative_path(relative)
                if path.is_dir():
                    # Original metadata batching validates parent directories.
                    # They are never selected/captured/restored web entries.
                    directory_identity(path)
                    device = self.working_copy.lstat().st_dev
                    cursor = self.working_copy
                    for part in rel.parts:
                        cursor /= part
                        if cursor.lstat().st_dev != device:
                            raise ValueError('Cross-device metadata ancestor is unsupported.')
                else:
                    file_identity(self.working_copy, rel)
            return path

        def _select_create(self, paths):
            if not paths or len(paths) > MAX_FILES:
                raise ValueError('An exact nonempty selection is required.')
            budget = FingerprintBudget()
            for path in paths:
                content_fingerprint(self.working_copy, strict_relative_path(path), budget)
            value = super()._select_create(paths)
            if set(value[2]) != {strict_relative_path(p).as_posix() for p in paths}:
                raise ValueError('Shelf backend expanded or dropped selected paths.')
            if self.expected and _digest(value[0]) != self.expected['repositoryDigest']:
                raise StaleRead('Repository identity changed after review.')
            return value

        def _capture_create_entries(self, selected, blobs=None):
            budget = FingerprintBudget()
            for path in selected:
                content_fingerprint(self.working_copy, strict_relative_path(path), budget)
            entries = super()._capture_create_entries(selected, blobs)
            _files(entries)
            return entries

        def _verify_create_snapshot(self, paths, status_entries, selected, entries, context):
            super()._verify_create_snapshot(paths, status_entries, selected, entries, context)
            self._tick()
            if self.expected and _digest(_logical_entries(entries)) != self.expected['entriesDigest']:
                raise StaleRead('Source bytes, properties or BASE changed after confirmation.')

        def _validate_manifest(self, manifest, folder, verify_blobs=False):
            if not isinstance(manifest, dict):
                raise ValueError('Invalid Shelf manifest root.')
            if len(json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')) > MAX_MANIFEST_BYTES:
                raise ValueError('Serialized Shelf manifest exceeds web limit.')
            _files(manifest.get('entries', []))  # Cap legacy v1 as well, before decoding.
            return super()._validate_manifest(manifest, folder, verify_blobs)

        def _load(self, shelf_id, verify_blobs=False):
            self._tick()
            self._check_storage()
            folder = self._shelf_path(shelf_id)
            path = folder / 'manifest.json'
            with module._open_regular(path) as stream:
                if os.fstat(stream.fileno()).st_nlink != 1:
                    raise ValueError('Hardlinked manifest is unsupported.')
                raw = stream.read(MAX_MANIFEST_BYTES + 1)
                if len(raw) > MAX_MANIFEST_BYTES:
                    raise ValueError('Shelf manifest exceeds web limit.')
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('Duplicate manifest field.')
                    result[key] = value
                return result
            manifest = json.loads(raw, object_pairs_hook=unique)
            self._validate_manifest(manifest, folder, verify_blobs=verify_blobs)
            if manifest['id'] != shelf_id:
                raise ValueError('Shelf ID mismatch.')
            if self.expected and self.expected.get('manifestDigest') != _digest(manifest):
                raise StaleRead('Shelf changed after review.')
            return manifest

        def _preflight(self, manifest):
            # Inspect all targets before original BASE/content validation or writes.
            for entry in manifest['entries']:
                file_identity(self.working_copy, strict_relative_path(entry['path']))
            return super()._preflight(manifest)

    return WebShelfManager()


class ShelfService:
    def __init__(self, manager, check_workspace):
        self.manager = manager
        self.client = manager  # Manager reuses the SvnClient lock.
        self.check_workspace = check_workspace
        self.plan = None

    def invalidate(self):
        self.plan = None

    def _scope(self, kind, data, snapshot):
        validate_request(kind, data)
        if snapshot is None or data['snapshotToken'] != snapshot[0]:
            raise StaleRead('Shelf status snapshot expired.')
        self.check_workspace()
        return {key: data[key] for key in COMMON}

    def _public(self, manifest):
        files, total = _files(manifest['entries'])
        if not _text(manifest['name'], 200) or not _text(manifest['created_at'], 128):
            raise ValueError('Invalid display metadata.')
        return dict(id=manifest['id'], name=manifest['name'], createdAt=manifest['created_at'],
                    entryCount=len(files), format=manifest['format'], files=files, totalBytes=total)

    def listing(self, data, snapshot):
        scope = self._scope('list', data, snapshot)
        self.invalidate()
        manager = self.manager
        with manager._client_lock, manager.bounded():
            manager._check_storage()
            shelves = []
            response_size = 4096  # Leave room for envelope identity and JSON syntax.
            if manager.storage_dir.exists():
                with os.scandir(manager.storage_dir) as scan:
                    examined = 0
                    for entry in scan:
                        examined += 1
                        if examined > MAX_SHELVES * 4:
                            raise ValueError('Shelf storage directory enumeration limit exceeded.')
                        if SHELF_ID.fullmatch(entry.name):
                            if len(shelves) >= MAX_SHELVES:
                                raise ValueError('Shelf list limit exceeded; no partial list returned.')
                            try:
                                shelves.append(self._public(manager._load(entry.name)))
                            except (OSError, ValueError, RuntimeError, TypeError, KeyError, RecursionError):
                                shelves.append(dict(id=entry.name, name='不可用 Shelf', error='损坏、超出 Web 上限或包含不支持的目录；未验证内容。'))
                            response_size += len(json.dumps(shelves[-1], ensure_ascii=False).encode('utf-8')) + 2
                            if response_size > MAX_LIST_BYTES:
                                raise ValueError('Shelf list response limit exceeded; no partial list returned.')
            self.check_workspace()
            return dict(scope, mode='live', shelves=sorted(shelves, key=lambda x: x.get('createdAt', ''), reverse=True))

    def _observe_create(self, data, snapshot):
        expected = {}
        for identifier in data['ids']:
            row = snapshot[1].get(identifier)
            if row is None:
                raise StaleRead('Selected row is outside status snapshot.')
            if (row.get('item') not in {'modified', 'normal', 'none', 'added', 'deleted', 'unversioned'}
                    or row.get('props') == 'conflicted'
                    or any(row.get(k) for k in ('switched', 'file_external', 'tree_conflicted', 'wc_locked'))):
                raise ValueError('Unsupported selected status.')
            expected[identifier] = row
        current = status_rows(self.status_client)
        if any(current.get(key) != row for key, row in expected.items()):
            raise StaleRead('Selected status changed after refresh.')
        return self.manager.preflight_create(data['ids'])

    def _restore_targets(self, entries):
        budget = FingerprintBudget()
        result = {}
        for entry in entries:
            path = strict_relative_path(entry['path'])
            fingerprint = content_fingerprint(self.manager.working_copy, path, budget)
            target = self.manager.working_copy / path
            mode = stat.S_IMODE(target.lstat().st_mode) if os.path.lexists(target) else None
            result[entry['path']] = (fingerprint, mode)
        return result

    def preflight(self, data, snapshot):
        scope = self._scope('preflight', data, snapshot)
        self.invalidate()
        with self.manager._client_lock, self.manager.bounded():
            if data['operation'] == 'create':
                observed = self._observe_create(data, snapshot)
                name = data['name']
                manifest_digest = None
            else:
                manifest = self.manager._load(data['shelfId'], verify_blobs=True)
                name = manifest['name']
                manifest_digest = _digest(manifest)
                observed = self.manager.preflight_restore(data['shelfId'])
                if _same_plan(manifest) != _same_plan(observed):
                    raise StaleRead('Shelf changed during review.')
            files, total = _files(observed['entries'])
            self.check_workspace()
            plan = dict(scope, token=secrets.token_urlsafe(24), expires=time.monotonic()+PLAN_SECONDS,
                        request=dict(data), planDigest=_same_plan(observed), manifestDigest=manifest_digest,
                        repositoryDigest=_digest(observed['repository']), entriesDigest=_digest(_logical_entries(observed['entries'])),
                        files=files, name=name, targets=self._restore_targets(observed['entries']) if data['operation']=='restore' else None)
            # _select_create returns a larger info dictionary; bind its exact snapshot too.
            if data['operation'] == 'create':
                plan['repositoryDigest'] = _digest(self.manager._info())
            self.plan = plan
            return dict(scope, mode='live', operation=data['operation'], planToken=plan['token'],
                        expiresIn=PLAN_SECONDS, name=name, shelfId=data.get('shelfId'),
                        files=files, totalBytes=total, localOnly=True, backupOnly=data['operation']=='create')

    def apply(self, data, snapshot, invalidate_snapshot):
        self._scope('apply', data, snapshot)
        plan = self.plan
        self.invalidate()  # Consume before any execution-time checks or failures.
        if (not plan or plan['token'] != data['planToken'] or time.monotonic() >= plan['expires']
                or any(plan[key] != data[key] for key in COMMON)):
            raise StaleRead('Shelf confirmation expired.')
        request, manager = plan['request'], self.manager
        with manager._client_lock, manager.bounded():
            if request['operation'] == 'create':
                observed = self._observe_create(request, snapshot)
            else:
                manifest = manager._load(request['shelfId'], verify_blobs=True)
                if _digest(manifest) != plan['manifestDigest']:
                    raise StaleRead('Shelf changed after preview.')
                observed = manager.preflight_restore(request['shelfId'])
                if self._restore_targets(observed['entries']) != plan['targets']:
                    raise StaleRead('Restore target identity, bytes or mode changed after review.')
            if _same_plan(observed) != plan['planDigest']:
                raise StaleRead('Reviewed source or Shelf changed.')
            self.check_workspace()
            manager.expected = plan
            invalidate_snapshot()
            try:
                result = (manager.create(plan['name'], paths=request['ids']) if request['operation']=='create'
                          else manager.restore(request['shelfId']))
                self.check_workspace()
                if result['paths'] != [f['path'] for f in plan['files']]:
                    raise ValueError('Unexpected result scope.')
                return dict(operation=request['operation'], shelfId=result['id'], name=plan['name'],
                            files=result['paths'], shelfRetained=True)
            except Exception as exc:
                raise MutationUncertain('Shelf execution outcome is uncertain; inspect storage and working copy.') from exc
