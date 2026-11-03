"""Opt-in, exact-file native SVN label changes. No commit, revert or Shelf API.

Plans are short-lived, one-use capabilities, not durable jobs or transactions.
A failed command may have changed some labels; callers must invalidate status.
"""
from __future__ import annotations

from dataclasses import asdict
import os
import secrets
import time

from file_reads import StaleRead, file_identity, strict_relative_path

MAX_SELECTION = 100
MAX_ROWS = 50000
PLAN_SECONDS = 120


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
        for identifier, row in expected.items():
            if (row.get('item') not in {'normal', 'none', 'modified', 'added', 'deleted', 'replaced'}
                    or row.get('props') == 'conflicted'
                    or any(row.get(key) for key in ('switched', 'file_external', 'tree_conflicted', 'wc_locked'))):
                raise ValueError('Only safe versioned files may change labels.')
            relative = strict_relative_path(identifier)
            before = file_identity(self.client.working_copy, relative)
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
            if file_identity(self.client.working_copy, relative) != before:
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
        # Recheck physical identities last, including absent/deleted targets.
        for identifier, before in identities.items():
            if file_identity(self.client.working_copy, strict_relative_path(identifier)) != before:
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
                for identifier, original in plan['expected'].items():
                    expected = {**original, 'changelist': plan['name']}
                    if current.get(identifier) != expected:
                        raise MutationUncertain('Post-write membership or status did not match.')
                    if file_identity(self.client.working_copy, strict_relative_path(identifier)) != plan['identities'][identifier]:
                        raise MutationUncertain('File changed during label operation.')
                return {'action': plan['action'], 'name': plan['name'], 'count': len(paths)}, current
            except Exception as error:
                # Never expose raw SVN diagnostics or claim a failed write rolled
                # back. Retrying always requires fresh status, preflight, consent.
                raise MutationUncertain('Local label outcome is uncertain; refresh and review.') from error
