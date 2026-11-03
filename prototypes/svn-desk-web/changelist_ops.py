"""Opt-in, exact-file native SVN label changes. No commit, revert or Shelf API.

Plans are short-lived, one-use capabilities, not durable jobs or transactions.
A failed command may have changed some labels; callers must invalidate status.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import os
import secrets
import stat
import time

from file_reads import StaleRead, file_identity, strict_relative_path

MAX_SELECTION = 100
MAX_ROWS = 50000
PLAN_SECONDS = 120
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_FINGERPRINT_BYTES = 64 * 1024 * 1024
HASH_CHUNK_BYTES = 64 * 1024
HASH_SECONDS = 10.0


class FingerprintBudget:
    """Per selection pass; elapsed checks cannot interrupt blocked filesystem I/O."""
    def __init__(self):
        self.remaining = MAX_FINGERPRINT_BYTES
        self.seconds = HASH_SECONDS


def _descriptor_fields(node):
    # Do not compare path ctime to fstat ctime: Windows Python releases differ
    # on creation-time versus change-time semantics. Raw fstat ctime is checked
    # against itself before/after the read instead.
    return node.st_dev, node.st_ino, node.st_size, node.st_mtime_ns, node.st_nlink


def content_fingerprint(workspace, relative, budget):
    """Hash only the already-validated exact regular file, through one handle.

    Path checks reject links/reparse points/hardlinks/nested WCs as before. The
    opened descriptor must match that checked leaf before any bytes are read.
    Content stays local: only its SHA-256 is retained in the short-lived plan.
    This detects observed races, not atomicity against a hostile local writer.
    """
    started = time.monotonic()
    def check_time():
        if time.monotonic() - started >= budget.seconds:
            raise ValueError('Selected file fingerprint time limit exceeded.')
    try:
        check_time()
        before = file_identity(workspace, relative)
        leaf = before[1][-1]
        if leaf[0] == 'absent':
            if file_identity(workspace, relative) != before:
                raise StaleRead('Deleted target changed while checking identity.')
            check_time()
            return before, None
        size = leaf[2]
        if size > MAX_FILE_BYTES or size > budget.remaining:
            raise ValueError('Selected file fingerprint byte limit exceeded.')
        flags = (os.O_RDONLY | getattr(os, 'O_BINARY', 0)
                 | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        fd = os.open(workspace / relative, flags)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or getattr(opened, 'st_file_attributes', 0) & 0x400
                    or _descriptor_fields(opened) != (*leaf[:4], leaf[5])
                    or file_identity(workspace, relative) != before):
                raise StaleRead('Selected file was replaced before fingerprinting.')
            digest = hashlib.sha256()
            remaining = size
            while remaining:
                check_time()
                block = os.read(fd, min(HASH_CHUNK_BYTES, remaining))
                check_time()
                if not block:
                    raise StaleRead('Selected file shrank during fingerprinting.')
                remaining -= len(block)
                budget.remaining -= len(block)
                digest.update(block)
            check_time()
            if os.read(fd, 1):
                raise StaleRead('Selected file grew during fingerprinting.')
            check_time()
            after = os.fstat(fd)
            if (not stat.S_ISREG(after.st_mode)
                    or _descriptor_fields(after) != _descriptor_fields(opened)
                    or after.st_ctime_ns != opened.st_ctime_ns
                    or getattr(after, 'st_file_attributes', 0) & 0x400
                    or file_identity(workspace, relative) != before):
                raise StaleRead('Selected file changed during fingerprinting.')
            check_time()
            return before, digest.hexdigest()
        finally:
            os.close(fd)
    finally:
        budget.seconds -= max(0, time.monotonic() - started)


class ChangelistUnavailable(ValueError):
    pass


class MutationUncertain(RuntimeError):
    """Execution started or post-check failed; never retry or imply rollback."""


def identity_fields(data):
    if not isinstance(data, dict):
        raise ValueError('Expected an object.')
    if (type(data.get('selectionVersion')) is not int
            or not 1 <= data['selectionVersion'] <= 2**53 - 1):
        raise ValueError('Invalid generation.')
    for key in ('workspaceId', 'snapshotToken'):
        value = data.get(key)
        if (not isinstance(value, str) or not 1 <= len(value) <= 64
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError('Invalid scope.')


def validate_request(kind, data):
    identity_fields(data)
    common = {'workspaceId', 'selectionVersion', 'snapshotToken'}
    if kind == 'preflight':
        if set(data) != common | {'ids', 'action', 'name'}:
            raise ValueError('Unexpected preflight fields.')
        ids = data['ids']
        if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_SELECTION
                or any(not isinstance(value, str) for value in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('Select 1–100 distinct exact status rows.')
        for value in ids:
            strict_relative_path(value)
        name = data['name']
        if data['action'] == 'assign':
            if (not isinstance(name, str) or not 1 <= len(name) <= 128
                    or name != name.strip() or not name
                    or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in name)):
                raise ValueError('Invalid local changelist name.')
            name.encode('utf-8', errors='strict')
        elif data['action'] != 'remove' or name is not None:
            raise ValueError('Invalid label action.')
    elif kind == 'apply':
        if (set(data) != common | {'planToken', 'confirmed'} or data['confirmed'] is not True
                or not isinstance(data['planToken'], str)
                or not 1 <= len(data['planToken']) <= 64
                or any(ord(c) < 32 or ord(c) == 127 for c in data['planToken'])):
            raise ValueError('An explicit confirmation for one reviewed plan is required.')
    else:
        raise ValueError('Unsupported operation.')


def status_rows(client):
    entries = client.status(verbose=True)  # Includes clean named members.
    if len(entries) > MAX_ROWS:
        raise ValueError('Status row limit exceeded.')
    rows = {}
    for entry in entries:
        row = asdict(entry)
        identifier = row['path']
        if identifier in rows:
            raise ValueError('Duplicate status identity.')
        row.update(id=identifier, path=identifier.replace(os.sep, '/'), kind='unknown')
        rows[identifier] = row
    return rows


def visible_rows(rows):
    return [row for row in rows.values() if row.get('changelist')
            or row.get('item') not in {'normal', 'none'}
            or row.get('props') not in {'normal', 'none'}
            or any(row.get(k) for k in ('tree_conflicted', 'switched', 'file_external', 'wc_locked'))]


class ChangelistService:
    def __init__(self, client, history_manager, check_workspace):
        if any(not callable(getattr(client, method, None)) for method in
               ('validate_changelist_paths', 'assign_changelist', 'remove_changelist')):
            raise ChangelistUnavailable('The paired changelist backend is required.')
        self.client = client
        self.manager = history_manager
        self.check_workspace = check_workspace
        self.plan = None

    def invalidate(self):
        self.plan = None

    def _verify(self, expected, *, fingerprints=None):
        current = status_rows(self.client)
        for identifier, row in expected.items():
            if current.get(identifier) != row:
                raise StaleRead('Selected file status or label changed; review again.')
        identities = {}
        budget = FingerprintBudget()
        for identifier, row in expected.items():
            if (row.get('item') not in {'normal', 'none', 'modified', 'added', 'deleted', 'replaced'}
                    or row.get('props') == 'conflicted'
                    or any(row.get(key) for key in ('switched', 'file_external', 'tree_conflicted', 'wc_locked'))):
                raise ValueError('Only safe versioned files may change labels.')
            relative = strict_relative_path(identifier)
            before = content_fingerprint(self.client.working_copy, relative, budget)
            if fingerprints is not None and before != fingerprints[identifier]:
                raise StaleRead('Selected file changed after review.')
            # Reuse strict bounded local info; no repository calls or URLs from
            # the browser. This additionally rejects switched parent paths.
            _, target = self.manager._target(identifier)
            self.manager._check_target(target)
            info, _ = self.manager._info(target)
            actual = info.findtext('wc-info/wcroot-abspath', '')
            expected_root = os.path.normcase(os.path.abspath(self.client.working_copy))
            if (info.get('kind') != 'file' or not actual
                    or os.path.normcase(os.path.abspath(actual)) != expected_root):
                raise ValueError('Target must belong to this exact working-copy root.')
            if file_identity(self.client.working_copy, relative) != before[0]:
                raise StaleRead('Selected file changed during validation.')
            identities[identifier] = before
        validated = self.client.validate_changelist_paths(list(expected))
        if validated != list(expected):
            raise ValueError('Backend changed the exact selection.')
        # Original backend validation also performs reads. Check status again
        # after these potentially lengthy calls, rather than overwriting a label
        # changed by another process during preflight.
        current = status_rows(self.client)
        if any(current.get(identifier) != row for identifier, row in expected.items()):
            raise StaleRead('Selected status changed during validation.')
        self.check_workspace()
        # Rehash after potentially lengthy SVN validation, so a same-length edit
        # with restored mtime cannot use the earlier content observation.
        budget = FingerprintBudget()
        for identifier, before in identities.items():
            if content_fingerprint(self.client.working_copy, strict_relative_path(identifier), budget) != before:
                raise StaleRead('Selected file changed during backend validation.')
        return identities

    def preflight(self, data, snapshot):
        validate_request('preflight', data)
        self.invalidate()  # A newer review replaces the previous capability.
        if snapshot is None or data['snapshotToken'] != snapshot[0]:
            raise StaleRead('Status snapshot expired.')
        try:
            expected = {identifier: dict(snapshot[1][identifier]) for identifier in data['ids']}
        except KeyError:
            raise StaleRead('Selection is outside the current status snapshot.') from None
        with self.client._lock:
            identities = self._verify(expected)
        plan_token = secrets.token_urlsafe(24)
        scope = {key: data[key] for key in ('workspaceId', 'selectionVersion', 'snapshotToken')}
        self.plan = dict(scope, token=plan_token, expires=time.monotonic() + PLAN_SECONDS,
                         expected=expected, identities=identities, action=data['action'], name=data['name'])
        return dict(scope, mode='live', planToken=plan_token, expiresIn=PLAN_SECONDS,
                    action=data['action'], name=data['name'], localOnly=True,
                    files=[{'id': row['id'], 'path': row['path'],
                            'fromChangelist': row.get('changelist')} for row in expected.values()])

    def apply(self, data, snapshot, invalidate_snapshot):
        validate_request('apply', data)
        plan = self.plan
        self.invalidate()  # Consume before revalidation, including failed tries.
        if (plan is None or data['planToken'] != plan['token']
                or time.monotonic() >= plan['expires']
                or any(data[key] != plan[key] for key in ('workspaceId', 'selectionVersion', 'snapshotToken'))
                or snapshot is None or snapshot[0] != plan['snapshotToken']):
            raise StaleRead('Reviewed plan expired or was already used.')
        with self.client._lock:
            self._verify(plan['expected'], fingerprints=plan['identities'])
            if time.monotonic() >= plan['expires']:
                raise StaleRead('Plan expired during validation.')
            self.check_workspace()
            # No old read/write capability remains valid once execution starts.
            invalidate_snapshot()
            try:
                paths = list(plan['expected'])
                result = (self.client.assign_changelist(plan['name'], paths)
                          if plan['action'] == 'assign' else self.client.remove_changelist(paths))
                if result.returncode != 0 or result.stderr:
                    raise MutationUncertain('Mutation returned diagnostics.')
                current = status_rows(self.client)
                budget = FingerprintBudget()
                for identifier, original in plan['expected'].items():
                    expected = {**original, 'changelist': plan['name']}
                    if current.get(identifier) != expected:
                        raise MutationUncertain('Post-write membership or status did not match.')
                    if content_fingerprint(self.client.working_copy, strict_relative_path(identifier), budget) != plan['identities'][identifier]:
                        raise MutationUncertain('File changed during label operation.')
                return {'action': plan['action'], 'name': plan['name'], 'count': len(paths)}, current
            except Exception as error:
                # Never expose raw SVN diagnostics or claim a failed write rolled
                # back. Retrying always requires fresh status, preflight, consent.
                raise MutationUncertain('Local label outcome is uncertain; refresh and review.') from error
