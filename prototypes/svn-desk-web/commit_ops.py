"""Opt-in exact-file commits, with local review and one-use in-memory consent.

There is no server dry-run and no rollback/retry. Another process can still edit
files after our last observation. The adapter must pin the reviewed backend and
refresh status after dispatch, including a command with an uncertain outcome.
"""
from __future__ import annotations

from dataclasses import asdict
import os
import hashlib
import base64
import binascii
import json
import math
import importlib
import xml.etree.ElementTree as ET
from pathlib import Path
import secrets
import time

from changelist_ops import (ChangelistService, FingerprintBudget, content_fingerprint,
                            identity_fields, status_rows, MutationUncertain)
from file_reads import StaleRead, strict_relative_path, file_identity

PLAN_SECONDS = 120
MAX_MESSAGE_BYTES = 8192
COMMON = {'workspaceId', 'selectionVersion', 'snapshotToken'}


class CommitUnavailable(ValueError):
    pass


def validate_request(kind, data):
    identity_fields(data)
    if kind == 'apply':
        if (set(data) != COMMON | {'planToken', 'confirmed'}
                or data['confirmed'] is not True
                or not isinstance(data['planToken'], str)
                or not 1 <= len(data['planToken']) <= 64
                or any(ord(c) < 32 or ord(c) == 127 for c in data['planToken'])):
            raise ValueError('Explicit confirmation for one reviewed commit is required.')
        return
    if kind != 'preflight' or set(data) != COMMON | {'operation', 'ids', 'name', 'message', 'keepChangelist'}:
        raise ValueError('Unexpected commit fields.')
    ids = data['ids']
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
            or any(not isinstance(p, str) for p in ids) or len({p.casefold() for p in ids}) != len(ids)):
        raise ValueError('Select 1–100 distinct exact files.')
    for identifier in ids:
        strict_relative_path(identifier)
        identifier.encode('utf-8', errors='strict')
    name = data['name']
    if data['operation'] == 'selected':
        if name is not None:
            raise ValueError('Exact selection cannot specify a changelist filter.')
    elif data['operation'] == 'changelist':
        if (not isinstance(name, str) or not 1 <= len(name) <= 128
                or name != name.strip()
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in name)):
            raise ValueError('Invalid changelist name.')
        name.encode('utf-8', errors='strict')
    else:
        raise ValueError('Unsupported commit operation.')
    message = data['message']
    if (not isinstance(message, str) or not message.strip()
            or len(message) > MAX_MESSAGE_BYTES
            or any((ord(c) < 32 and c not in '\n\r\t') or 127 <= ord(c) <= 159 for c in message)
            or len(message.encode('utf-8', errors='strict')) > MAX_MESSAGE_BYTES
            or type(data['keepChangelist']) is not bool):
        raise ValueError('A bounded UTF-8 commit message and explicit keep-label choice are required.')


class CommitService:
    def __init__(self, client, history_manager, check_workspace):
        required = ('preflight_changelist_commit', 'commit_changelist',
                    '_local_targets', '_command_with_targets')
        if any(not callable(getattr(client, method, None)) for method in required):
            raise CommitUnavailable('The reviewed paired commit backend is required.')
        self.client = client
        self.check_workspace = check_workspace
        self.manager = history_manager
        self.files = ChangelistService(client, history_manager, check_workspace)
        self.plan = None

    def invalidate(self):
        self.plan = None

    def _property_bytes(self, target):
        # _run's original allowlist does not include proplist. Reuse its reviewed
        # bounded process implementation for exactly this one local-only read.
        timeout = self.client.timeout
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('Invalid bounded commit read timeout.')
        history = importlib.import_module('svn_client.history')
        return history._bounded_process([self.client.executable, 'proplist',
            '--non-interactive', '--no-auth-cache', '--xml', '--verbose',
            '--depth', 'empty', '--', target], cwd=self.client.working_copy,
            timeout=timeout, stdout_limit=256 * 1024)

    def _metadata(self, expected):
        result, total = {}, 0
        for identifier in expected:
            relative = strict_relative_path(identifier)
            before = file_identity(self.client.working_copy, relative)
            _, target = self.manager._target(identifier)
            raw = self._property_bytes(target)
            if not isinstance(raw, bytes):
                raise ValueError('Invalid local property data.')
            total += len(raw)
            if (len(raw) > 256 * 1024 or total > 4 * 1024 * 1024
                    or b'\0' in raw or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper()):
                raise ValueError('Unsupported or oversized local properties.')
            try:
                root = ET.fromstring(raw)
            except (ET.ParseError, ValueError):
                raise ValueError('Invalid local property XML.') from None
            if (root.tag != 'properties' or root.attrib or (root.text or '').strip()
                    or (root.tail or '').strip() or len(root) > 1
                    or any(node.tag != 'target' for node in root)):
                raise ValueError('Unexpected local property scope.')
            props = []
            for node in root:
                path = node.get('path', '')
                actual = os.path.normcase(os.path.abspath(self.client.working_copy / path))
                wanted = os.path.normcase(os.path.abspath(self.client.working_copy / identifier))
                if (actual != wanted or set(node.attrib) != {'path'}
                        or (node.text or '').strip() or (node.tail or '').strip()):
                    raise ValueError('Local property target changed.')
                for prop in node:
                    if (prop.tag != 'property' or len(prop) or not prop.get('name')
                            or (prop.tail or '').strip()
                            or set(prop.attrib) - {'name', 'encoding'}
                            or prop.get('encoding') not in {None, 'base64'}
                            or prop.get('name') == 'svn:special'):
                        raise ValueError('Unsupported local property metadata.')
                    if prop.get('encoding') == 'base64':
                        try:
                            base64.b64decode(''.join((prop.text or '').split()), validate=True)
                        except (ValueError, binascii.Error):
                            raise ValueError('Invalid encoded local property.') from None
                    props.append((prop.get('name'), prop.get('encoding', ''), prop.text or ''))
            if len({p[0] for p in props}) != len(props):
                raise ValueError('Duplicate local property metadata.')
            info, _ = self.manager._info(target)
            # Bind BASE revision, repository UUID/URL and schedule/copy metadata.
            # Values never leave this process or appear in browser diagnostics.
            payload = json.dumps(sorted(props), ensure_ascii=True).encode() + ET.tostring(info)
            if file_identity(self.client.working_copy, relative) != before:
                raise StaleRead('File changed during local metadata read.')
            result[identifier] = hashlib.sha256(payload).hexdigest()
        return result

    def _dependencies(self, expected, operation, name):
        current = status_rows(self.client)
        if any(current.get(p) != row for p, row in expected.items()):
            raise StaleRead('Reviewed commit status changed.')
        if operation == 'changelist':
            members = {p for p, row in current.items() if row.get('changelist') == name}
            if members != set(expected):
                raise StaleRead('Review every changelist member, including clean files.')
        if not any(row['item'] not in {'normal', 'none'} or row['props'] == 'modified'
                   for row in expected.values()):
            raise ValueError('The reviewed selection has no changes to commit.')
        root = Path(os.path.abspath(self.client.working_copy))
        statuses = {os.path.normcase(os.path.abspath(root / p)): row for p, row in current.items()}
        for identifier, row in expected.items():
            if (row.get('moved_from') or row.get('moved_to')
                    or row.get('props') not in {'normal', 'none', 'modified'}):
                raise ValueError('Moved files and unsafe property states require the original client.')
            # Requiring both ends is insufficient for directory moves and
            # implicit copy dependencies. Web commits reject moves entirely.
            parent = (root / strict_relative_path(identifier)).parent
            while parent == root or root in parent.parents:
                ancestor = statuses.get(os.path.normcase(str(parent)))
                if ancestor and (ancestor['item'] not in {'normal', 'none', 'modified'}
                        or ancestor.get('props') == 'conflicted'
                        or any(ancestor.get(k) for k in ('tree_conflicted', 'switched',
                            'file_external', 'wc_locked', 'moved_from', 'moved_to'))):
                    raise ValueError('A parent directory has an unsupported dependency; scope will not expand.')
                if parent == root:
                    break
                parent = parent.parent
        return current

    def _verify(self, expected, operation, name, fingerprints=None):
        self._dependencies(expected, operation, name)
        observed = self.files._verify(expected, fingerprints=fingerprints)
        metadata = self._metadata(expected)
        for identifier, value in observed.items():
            if value[1] is None and expected[identifier]['item'] != 'deleted':
                raise ValueError('Only scheduled deletions may be absent.')
        if operation == 'changelist':
            members = self.client.preflight_changelist_commit(name, expected_paths=list(expected))
            if {member.path for member in members} != set(expected):
                raise StaleRead('The backend changed the reviewed membership.')
            for member in members:
                row = asdict(member)
                if any(expected[member.path].get(key) != value for key, value in row.items() if key != 'path'):
                    raise StaleRead('The backend observed changed member status.')
        self._dependencies(expected, operation, name)
        self.check_workspace()
        budget = FingerprintBudget()
        for identifier, identity in observed.items():
            if content_fingerprint(self.client.working_copy, strict_relative_path(identifier), budget) != identity:
                raise StaleRead('Commit contents changed during local review.')
        if self._metadata(expected) != metadata:
            raise StaleRead('Local properties or base metadata changed during review.')
        return observed, metadata

    def preflight(self, data, snapshot):
        validate_request('preflight', data)
        self.invalidate()
        if snapshot is None or data['snapshotToken'] != snapshot[0]:
            raise StaleRead('Status snapshot expired.')
        try:
            expected = {p: dict(snapshot[1][p]) for p in data['ids']}
        except KeyError:
            raise StaleRead('Selection is outside the current snapshot.') from None
        with self.client._lock:
            identities, metadata = self._verify(expected, data['operation'], data['name'])
        scope = {key: data[key] for key in COMMON}
        token = secrets.token_urlsafe(24)
        self.plan = dict(scope, token=token, expires=time.monotonic() + PLAN_SECONDS,
                         expected=expected, identities=identities, metadata=metadata, operation=data['operation'],
                         name=data['name'], message=data['message'], keepChangelist=data['keepChangelist'])
        return dict(scope, mode='live', planToken=token, expiresIn=PLAN_SECONDS,
                    operation=data['operation'], name=data['name'], keepChangelist=data['keepChangelist'],
                    localOnly=True, files=[{'id': p, 'path': row['path'], 'item': row['item'],
                                           'props': row['props'], 'fromChangelist': row.get('changelist')}
                                          for p, row in expected.items()])

    def apply(self, data, snapshot, invalidate_snapshot):
        validate_request('apply', data)
        plan = self.plan
        self.invalidate()  # No retry, even after a stale review or failed recheck.
        if (plan is None or data['planToken'] != plan['token']
                or time.monotonic() >= plan['expires']
                or any(data[key] != plan[key] for key in COMMON)
                or snapshot is None or snapshot[0] != plan['snapshotToken']):
            raise StaleRead('Commit plan expired or was already used.')
        with self.client._lock:
            _, metadata = self._verify(plan['expected'], plan['operation'], plan['name'], plan['identities'])
            if metadata != plan['metadata']:
                raise StaleRead('Reviewed properties or base metadata changed.')
            if time.monotonic() >= plan['expires']:
                raise StaleRead('Commit plan expired during validation.')
            self.check_workspace()
            invalidate_snapshot()
            try:
                paths = list(plan['expected'])
                if plan['operation'] == 'changelist':
                    result = self.client.commit_changelist(plan['name'], plan['message'],
                        keep_changelist=plan['keepChangelist'], expected_paths=paths)
                else:
                    options = ['commit', '--message=' + plan['message'], '--encoding', 'UTF-8', '--depth', 'empty']
                    if plan['keepChangelist']:
                        options.append('--keep-changelists')
                    # Paired reviewed transport handles peg escaping and bounded
                    # argv/targets files; user input can never supply options.
                    targets = self.client._local_targets(paths)
                    result = self.client._command_with_targets(options, targets, _message_index=1)
                if result.returncode != 0 or result.stderr:
                    raise MutationUncertain('Commit returned diagnostics.')
                # Deliberately discard stdout, stderr, argv and commit message.
                return {'operation': plan['operation'], 'name': plan['name'],
                        'keepChangelist': plan['keepChangelist'], 'files': [row['path'] for row in plan['expected'].values()], 'commandCompleted': True}
            except Exception:
                raise MutationUncertain('Commit outcome is uncertain; refresh and inspect repository history before any new attempt.') from None
