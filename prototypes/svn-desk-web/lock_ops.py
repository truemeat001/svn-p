"""Opt-in exact-file repository locks over the reviewed original LockManager.

Server checks are explicit, plans are single-use, and no force/token/URL mutation
API exists. A failed dispatch is never retried or reported as rolled back.
"""
from __future__ import annotations

import hashlib
import importlib
import math
import os
from pathlib import Path
import secrets
import time

from changelist_ops import identity_fields, status_rows
from commit_ops import CommitService
from file_reads import StaleRead, strict_relative_path
from workspace_bridge import trusted_modules

COMMON = {'workspaceId', 'selectionVersion', 'snapshotToken'}
PLAN_SECONDS = 120
OPERATION_SECONDS = 90
PAIRED_LOCKS_SHA256 = '65c8291c1055e951bfde79c4da1ce3f8bb69ee3b2bdd7b9e0a5ecc439e27d7ba'


class LockUnavailable(ValueError):
    pass


def validate_request(kind, data):
    identity_fields(data)
    for key in COMMON - {'selectionVersion'}:
        data[key].encode('utf-8', errors='strict')
    if kind == 'apply':
        if (set(data) != COMMON | {'planToken', 'confirmed'}
                or data['confirmed'] is not True
                or not isinstance(data['planToken'], str)
                or not 1 <= len(data['planToken']) <= 64
                or any(ord(c) < 33 or ord(c) > 126 for c in data['planToken'])):
            raise ValueError('Explicit one-use lock confirmation required.')
        return
    if kind != 'preflight' or set(data) != COMMON | {'action', 'ids', 'comment'}:
        raise ValueError('Unexpected lock fields.')
    ids = data['ids']
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
            or any(not isinstance(p, str) for p in ids)
            or len({p.casefold() for p in ids}) != len(ids)):
        raise ValueError('Select 1–100 distinct exact files.')
    for identifier in ids:
        strict_relative_path(identifier)
        identifier.encode('utf-8', errors='strict')
    comment = data['comment']
    if (data['action'] not in {'lock', 'unlock'} or not isinstance(comment, str)
            or len(comment) > 4096 or len(comment.encode('utf-8', errors='strict')) > 4096
            or any((ord(c) < 32 and c not in '\n\r\t') or 127 <= ord(c) <= 159 for c in comment)
            or data['action'] == 'unlock' and comment != ''):
        raise ValueError('Unsupported lock action or bounded comment.')


def lock_module(source):
    trusted_modules(source)
    path = Path(source).resolve(strict=True) / 'svn_client' / 'locks.py'
    if (not path.is_file() or path.stat().st_size > 1024 * 1024
            or hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest() != PAIRED_LOCKS_SHA256):
        return None
    module = importlib.import_module('svn_client.locks')
    trusted_modules(source)
    return module


def guarded_manager(module, history, client):
    """Keep original lock semantics; bound all info reads and observe dispatch.

    No global monkeypatches: the private client shares only this WC's lock and
    original command transport. The original application is unchanged.
    """
    backend = importlib.import_module('svn_client.backend')

    class GuardedClient(backend.SvnClient):
        def __init__(self):
            self.working_copy, self.executable, self._lock = client.working_copy, client.executable, client._lock
            self.deadline = None
            self.before_write = None
            self.write_attempted = False
            self.command_diagnostics = False
            self.reader = history.HistoryManager(self)

        @property
        def timeout(self):
            value = client.timeout
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('Invalid bounded SVN timeout.')
            if self.deadline is not None:
                value = min(value, self.deadline - time.monotonic())
            if value <= 0:
                raise ValueError('Lock operation deadline exceeded.')
            return value

        def status(self, *args, **kwargs):
            self.timeout  # Cooperative total deadline; original status is bounded.
            result = client.status(*args, **kwargs)
            self.timeout
            return result

        def _xml(self, args):
            if args[0] != 'info':
                raise ValueError('Only bounded info reads are allowed here.')
            root = self.reader._xml(args, 256 * 1024)
            if root.tag != 'info' or not len(root) or any(node.tag != 'entry' for node in root):
                raise ValueError('Invalid info scope.')
            return root

        def command(self, args, **kwargs):
            if args[0] not in {'lock', 'unlock'} or '--force' in args or not callable(self.before_write) or self.write_attempted:
                raise ValueError('Only one reviewed non-forced lock command is permitted.')
            duration = self.timeout
            self.before_write()
            self.timeout
            self.write_attempted = True
            result = client.command(args, timeout=min(duration, self.timeout), **kwargs)
            if type(getattr(result, 'returncode', None)) is not int or result.returncode != 0:
                raise backend.SvnError('Lock command did not report success.')
            if not isinstance(result.stdout, str) or not isinstance(result.stderr, str):
                raise backend.SvnError('Lock command diagnostics invalid.')
            self.command_diagnostics = bool(result.stderr)
            return result

    guarded = GuardedClient()

    class ExactLockManager(module.LockManager):
        def _xml_info(self, target):
            root = guarded._xml(['info', '--xml', '--depth', 'empty', '--', target])
            if len(root) != 1 or root[0].get('kind') != 'file':
                raise module.LockError('One ordinary versioned file is required.')
            entry = root[0]
            # Duplicate identity/lock fields must not silently select the first.
            for parent, tags in [(entry, ('url', 'repository', 'wc-info', 'commit', 'lock')),
                                 (entry.find('repository'), ('uuid', 'root')),
                                 (entry.find('wc-info'), ('wcroot-abspath', 'schedule')),
                                 (entry.find('lock'), ('token', 'owner', 'comment', 'created', 'expires'))]:
                if parent is not None and any(len(parent.findall(tag)) > 1 for tag in tags):
                    raise module.LockError('Ambiguous lock metadata.')
            url = entry.findtext('url', '')
            if '://' in target:
                if not target.endswith('@HEAD') or url != target[:-5]:
                    raise module.LockError('Server file identity changed.')
            else:
                actual = entry.get('path', '')
                raw = target[:-1] if '@' in target else target
                if (not actual or os.path.normcase(os.path.abspath(guarded.working_copy / actual))
                        != os.path.normcase(os.path.abspath(guarded.working_copy / raw))):
                    raise module.LockError('Local file identity changed.')
            return entry

    return ExactLockManager(guarded)


def public_files(files, expected):
    if len(files) != len(expected) or {f.path for f in files} != set(expected):
        raise ValueError('Lock manager changed the exact selection.')
    result = []
    for info in files:
        row = expected[info.path]
        revisions = (info.working_revision, info.server_last_changed_revision)
        if any(type(v) is not int or not 0 <= v <= 2**63 - 1 for v in revisions):
            raise ValueError('Invalid lock revision.')
        booleans = (info.locked, info.owned_by_working_copy, info.local_token_present, info.stale_local_token)
        if any(type(v) is not bool for v in booleans):
            raise ValueError('Invalid lock ownership.')
        result.append(dict(id=info.path, path=row['path'], item=row['item'], props=row['props'],
            fromChangelist=row.get('changelist'), locked=info.locked,
            ownedByWorkingCopy=info.owned_by_working_copy, localTokenPresent=info.local_token_present,
            staleLocalToken=info.stale_local_token, workingRevision=str(revisions[0]),
            serverLastChangedRevision=str(revisions[1])))
    return result


class LockService:
    def __init__(self, manager, check_workspace):
        self.manager = manager
        self.client = manager.client
        self.check_workspace = check_workspace
        self.local = CommitService(self.client, self.client.reader, check_workspace)
        self.plan = None

    def invalidate(self):
        self.plan = None

    def _verify(self, expected, identities=None, metadata=None):
        current = status_rows(self.client)
        if any(current.get(p) != row for p, row in expected.items()):
            raise StaleRead('Selected status changed.')
        for row in expected.values():
            if (row['item'] not in {'normal', 'modified'} or row['props'] not in {'none', 'normal', 'modified'}
                    or any(row.get(k) for k in ('copied', 'moved_from', 'moved_to', 'tree_conflicted',
                                               'switched', 'file_external', 'wc_locked'))):
                raise ValueError('Only ordinary committed files may use locks.')
        root = Path(os.path.abspath(self.client.working_copy))
        statuses = {os.path.normcase(os.path.abspath(root / p)): row for p, row in current.items()}
        for identifier in expected:
            parent = (root / strict_relative_path(identifier)).parent
            while parent == root or root in parent.parents:
                ancestor = statuses.get(os.path.normcase(str(parent)))
                if ancestor and (ancestor['item'] not in {'normal', 'modified', 'none'}
                        or ancestor['props'] not in {'normal', 'modified', 'none'}
                        or any(ancestor.get(k) for k in ('copied', 'moved_from', 'moved_to',
                            'tree_conflicted', 'switched', 'file_external', 'wc_locked'))):
                    raise ValueError('Unsafe parent directory; lock scope will not expand.')
                if parent == root:
                    break
                parent = parent.parent
        observed = self.local.files._verify(expected, fingerprints=identities)
        if any(value[1] is None for value in observed.values()):
            raise ValueError('Lock targets must exist.')
        actual = self.local._metadata(expected)
        if metadata is not None and actual != metadata:
            raise StaleRead('Local properties or file identity changed.')
        self.check_workspace()
        return observed, actual

    def preflight(self, data, snapshot):
        validate_request('preflight', data)
        self.invalidate()
        if snapshot is None or snapshot[0] != data['snapshotToken']:
            raise StaleRead('Status snapshot expired.')
        try:
            expected = {p: dict(snapshot[1][p]) for p in data['ids']}
        except KeyError:
            raise StaleRead('Selection outside the current status.') from None
        self.client.deadline = time.monotonic() + OPERATION_SECONDS
        try:
            with self.client._lock:
                identities, metadata = self._verify(expected)
                plan = (self.manager.preflight_lock(list(expected), data['comment']) if data['action'] == 'lock'
                        else self.manager.preflight_unlock(list(expected)))
                files = public_files(plan.files, expected)
                self._verify(expected, identities, metadata)
            scope = {key: data[key] for key in COMMON}
            token = secrets.token_urlsafe(24)
            self.plan = dict(scope, token=token, expires=time.monotonic() + PLAN_SECONDS,
                expected=expected, identities=identities, metadata=metadata, backend_plan=plan)
            return dict(scope, mode='live', planToken=token, expiresIn=PLAN_SECONDS,
                        action=data['action'], comment=data['comment'], contactsRepository=True, files=files)
        finally:
            self.client.deadline = None

    def apply(self, data, snapshot, invalidate_snapshot):
        validate_request('apply', data)
        plan = self.plan
        self.invalidate()
        if (plan is None or data['planToken'] != plan['token'] or time.monotonic() >= plan['expires']
                or any(data[key] != plan[key] for key in COMMON)
                or snapshot is None or snapshot[0] != plan['snapshotToken']):
            raise StaleRead('Lock plan expired or used.')
        native = plan['backend_plan']
        result = dict(action=native.action, files=[row['path'] for row in plan['expected'].values()],
                      outcome='uncertain', verified=False, warnings=['operation-outcome-unknown'])
        self.client.deadline = time.monotonic() + OPERATION_SECONDS
        self.client.write_attempted = False
        self.client.command_diagnostics = False
        def before_write():
            self._verify(plan['expected'], plan['identities'], plan['metadata'])
            invalidate_snapshot()
        self.client.before_write = before_write
        try:
            with self.client._lock:
                self._verify(plan['expected'], plan['identities'], plan['metadata'])
                try:
                    observed = (self.manager.lock(list(plan['expected']), native.comment, confirmed=True,
                                                 expected_state=native.state_sha256) if native.action == 'lock'
                                else self.manager.unlock(list(plan['expected']), confirmed=True,
                                                         expected_state=native.state_sha256))
                    public_files(observed.files, plan['expected'])
                    if observed.action != native.action or observed.verified is not True or not self.client.write_attempted:
                        raise ValueError('Invalid verified lock result.')
                    result.update(outcome='verified', verified=True, warnings=[])
                except Exception:
                    if not self.client.write_attempted:
                        raise StaleRead('Lock ownership or local review changed; no command was dispatched.') from None
                if self.client.command_diagnostics:
                    result['warnings'].append('command-diagnostics')
                return result
        finally:
            self.client.before_write = None
            self.client.deadline = None
