"""Opt-in exact-file scheduling, with no parent creation or repository access.

Plans detect observed races, not atomicity against a hostile local writer. SVN
has no compare-and-add primitive. Never retry, revert, or claim rollback after
execution starts. The legacy client's add() is deliberately NOT used: it adds
parents. Auto-props are disabled so user configuration and inherited auto-props
cannot expand the intended property changes. SVN may still detect MIME types.
"""
from __future__ import annotations

import os
import secrets
import time

from changelist_ops import (FingerprintBudget, MutationUncertain, content_fingerprint,
                            identity_fields, status_rows, MAX_SELECTION, PLAN_SECONDS)
from file_reads import StaleRead, strict_relative_path

COMMON = {'workspaceId', 'selectionVersion', 'snapshotToken'}
FLAGS = ('tree_conflicted', 'copied', 'switched', 'wc_locked', 'file_external',
         'moved_from', 'moved_to', 'changelist')


def _xml_identity(node):
    """Bind all parsed info content, ignoring only attribute insertion order.

    Windows SVN can emit equivalent attributes in a different order. Preserve
    every value, tag, text, tail and ordered child rather than serializing XML
    bytes or dropping fields through a semantic allowlist.
    """
    return [node.tag, sorted(node.attrib.items()), node.text, node.tail,
            [_xml_identity(child) for child in node]]


class AddUnavailable(ValueError):
    pass


def validate_request(kind, data):
    identity_fields(data)
    if kind == 'preflight':
        if set(data) != COMMON | {'ids'}:
            raise ValueError('Unexpected add preflight fields.')
        ids = data['ids']
        if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_SELECTION
                or any(not isinstance(value, str) for value in ids)
                or len({value.casefold() for value in ids}) != len(ids)):
            raise ValueError('Select 1–100 distinct exact unversioned files.')
        for value in ids:
            strict_relative_path(value)
            value.encode('utf-8', errors='strict')
        if sum(len(value.encode('utf-8')) + 4 for value in ids) > 24000:
            raise ValueError('Exact selection command length limit exceeded.')
    elif kind == 'apply':
        if (set(data) != COMMON | {'planToken', 'confirmed'} or data['confirmed'] is not True
                or not isinstance(data['planToken'], str)
                or not 1 <= len(data['planToken']) <= 64
                or any(ord(c) < 32 or ord(c) == 127 for c in data['planToken'])):
            raise ValueError('Explicit confirmation of one reviewed add plan is required.')
    else:
        raise ValueError('Unsupported add operation.')


class AddService:
    def __init__(self, client, history_manager, check_workspace):
        if (any(not callable(getattr(client, name, None)) for name in ('command', 'status', '_local_targets'))
                or any(not callable(getattr(history_manager, name, None)) for name in ('_target', '_check_target', '_info'))):
            raise AddUnavailable('The paired local SVN backend is required.')
        self.client = client
        self.manager = history_manager
        self.check_workspace = check_workspace
        self.plan = None

    def invalidate(self):
        self.plan = None

    @staticmethod
    def _safe(row, items):
        if (not row or row.get('item') not in items
                or row.get('props') not in {'none', 'normal', 'modified'}
                or any(row.get(key) for key in FLAGS)):
            raise ValueError('Unsafe, locked, external, or structurally changed status.')

    def _info(self, path, kind, schedule):
        _, target = self.manager._target(path)
        self.manager._check_target(target)
        info, _ = self.manager._info(target)
        actual = info.findtext('wc-info/wcroot-abspath', '')
        if (info.get('kind') != kind or not actual or not os.path.isabs(actual)
                or os.path.normcase(os.path.abspath(actual)) != os.path.normcase(os.path.abspath(self.client.working_copy))
                or info.findtext('wc-info/schedule', '') != schedule
                or info.find('lock') is not None or info.find('conflict') is not None
                or info.find('tree-conflict') is not None):
            raise ValueError('Target must have the expected local working-copy identity.')
        return _xml_identity(info)

    def _parents(self, expected, current):
        parents = {}
        for identifier in expected:
            for parent in reversed(strict_relative_path(identifier).parents):
                path = str(parent)
                if path in parents:
                    continue
                if sum(key.casefold() == path.casefold() for key in current) != 1:
                    raise ValueError('Parent status identity is missing or ambiguous.')
                self._safe(current.get(path), {'normal'})
                parents[path] = (dict(current[path]), self._info(path, 'dir', 'normal'))
        return parents

    def _verify(self, expected, *, reviewed=None):
        current = status_rows(self.client)
        # Reject aliases even if only one spelling was selected. SVN status must
        # identify one exact row, never a case-insensitive alternative.
        folded = {}
        for key in current:
            folded.setdefault(key.casefold(), []).append(key)
        for identifier, row in expected.items():
            if current.get(identifier) != row or len(folded.get(identifier.casefold(), [])) != 1:
                raise StaleRead('Selected status changed or has an ambiguous alias.')
            self._safe(row, {'unversioned'})
            if row.get('props') not in {'none', 'normal'}:
                raise ValueError('Unversioned file has unexpected properties.')
        budget = FingerprintBudget()
        fingerprints = {key: content_fingerprint(self.client.working_copy, strict_relative_path(key), budget)
                        for key in expected}
        if any(value[1] is None for value in fingerprints.values()):
            raise ValueError('Only existing ordinary files can be added.')
        parents = self._parents(expected, current)
        observed = (fingerprints, parents)
        if reviewed is not None and observed != reviewed:
            raise StaleRead('File content or parent identity changed after review.')
        targets = self.client._local_targets(list(expected))
        if targets != [key + '@' if '@' in key else key for key in expected]:
            raise ValueError('Backend changed the exact selection.')
        self.check_workspace()
        again = status_rows(self.client)
        if (any(again.get(key) != row for key, row in expected.items())
                or self._parents(expected, again) != parents):
            raise StaleRead('Selected status or parent changed during validation.')
        budget = FingerprintBudget()
        for key, fingerprint in fingerprints.items():
            if content_fingerprint(self.client.working_copy, strict_relative_path(key), budget) != fingerprint:
                raise StaleRead('File changed during validation.')
        return observed, targets, again

    def preflight(self, data, snapshot):
        validate_request('preflight', data)
        self.invalidate()
        if snapshot is None or data['snapshotToken'] != snapshot[0]:
            raise StaleRead('Status snapshot expired.')
        try:
            expected = {key: dict(snapshot[1][key]) for key in data['ids']}
        except KeyError:
            raise StaleRead('Selection is outside the status snapshot.') from None
        with self.client._lock:
            observed, _, _ = self._verify(expected)
        scope = {key: data[key] for key in COMMON}
        token = secrets.token_urlsafe(24)
        self.plan = dict(scope, token=token, expires=time.monotonic() + PLAN_SECONDS,
                         expected=expected, observed=observed)
        return dict(scope, mode='live', planToken=token, expiresIn=PLAN_SECONDS,
                    localOnly=True, autoProps=False,
                    files=[{'id': row['id'], 'path': row['path']} for row in expected.values()])

    def apply(self, data, snapshot, invalidate_snapshot):
        validate_request('apply', data)
        plan = self.plan
        self.invalidate()
        if (plan is None or data['planToken'] != plan['token']
                or time.monotonic() >= plan['expires']
                or any(data[key] != plan[key] for key in COMMON)
                or snapshot is None or snapshot[0] != plan['snapshotToken']):
            raise StaleRead('Reviewed add plan expired or was already used.')
        with self.client._lock:
            _, targets, before = self._verify(plan['expected'], reviewed=plan['observed'])
            if time.monotonic() >= plan['expires']:
                raise StaleRead('Plan expired during validation.')
            self.check_workspace()
            invalidate_snapshot()
            try:
                result = self.client.command(['add', '--depth', 'empty', '--no-auto-props', '--', *targets])
                if result.returncode != 0 or result.stderr:
                    raise MutationUncertain('Add returned diagnostics.')
                current = status_rows(self.client)
                if {k: v for k, v in current.items() if k not in plan['expected']} != {k: v for k, v in before.items() if k not in plan['expected']}:
                    raise MutationUncertain('Unselected status changed during add.')
                if self._parents(plan['expected'], current) != plan['observed'][1]:
                    raise MutationUncertain('Parent identity changed during add.')
                budget = FingerprintBudget()
                for key in plan['expected']:
                    self._safe(current.get(key), {'added'})
                    self._info(key, 'file', 'add')
                    if content_fingerprint(self.client.working_copy, strict_relative_path(key), budget) != plan['observed'][0][key]:
                        raise MutationUncertain('File changed during add.')
                self.check_workspace()
                # Metadata checks can take time. Do not return an older status
                # observation as a verified post-write snapshot.
                if status_rows(self.client) != current:
                    raise MutationUncertain('Status changed during post-add verification.')
                return dict(action='add', files=[row['path'] for row in plan['expected'].values()],
                            outcome='verified', verified=True, autoProps=False), current
            except Exception as error:
                raise MutationUncertain('Local add outcome is uncertain; refresh and review. No rollback was attempted.') from error
