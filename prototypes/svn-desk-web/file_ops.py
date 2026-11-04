"""Opt-in, one-file local move/copy over the pinned original SVN manager.

No delete, overwrite, recursive operation, parent creation, force or commit.
Observations are not an atomic reservation against other local processes. After
one attempted dispatch, failures are uncertain and never retried or rolled back.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import secrets
import time
import xml.etree.ElementTree as ET

from add_ops import _xml_identity
from changelist_ops import (FingerprintBudget, MutationUncertain, content_fingerprint,
                            identity_fields, status_rows)
from file_reads import StaleRead, file_identity, strict_relative_path
from property_reads import _properties
from workspace_bridge import trusted_modules

COMMON = {'workspaceId', 'selectionVersion', 'snapshotToken'}
PLAN_SECONDS = 120
OPERATION_SECONDS = 90
XML_BYTES = 256 * 1024
TOTAL_XML_BYTES = 8 * 1024 * 1024
OUTPUT_BYTES = 256 * 1024
MAX_DIRECTORY_ENTRIES = 50000
PAIRED_FILEOPS_SHA256 = '2527be940c52d60901e4620e76b4a345f2e40266ae665e5c8b2f5a49f48a604b'
FLAGS = ('tree_conflicted', 'switched', 'wc_locked', 'file_external')


class FileOperationUnavailable(ValueError):
    pass


def validate_request(kind, data):
    identity_fields(data)
    for key in COMMON - {'selectionVersion'}:
        data[key].encode('utf-8', errors='strict')
    if kind == 'apply':
        if (set(data) != COMMON | {'planToken', 'confirmed'} or data['confirmed'] is not True
                or not isinstance(data['planToken'], str) or not 1 <= len(data['planToken']) <= 64
                or any(ord(c) < 33 or ord(c) > 126 for c in data['planToken'])):
            raise ValueError('Explicit confirmation of one reviewed file operation is required.')
        return
    if kind != 'preflight' or set(data) != COMMON | {'action', 'id', 'destination'}:
        raise ValueError('Unexpected file-operation fields.')
    if data['action'] not in ('move', 'copy'):
        raise ValueError('Only exact single-file move or copy is supported.')
    for key in ('id', 'destination'):
        strict_relative_path(data[key])
        data[key].encode('utf-8', errors='strict')
    if str(strict_relative_path(data['id'])).casefold() == str(strict_relative_path(data['destination'])).casefold():
        raise ValueError('Source and destination must differ, including letter case.')


def fileops_module(source):
    trusted_modules(source)
    path = Path(source).expanduser().resolve(strict=True) / 'svn_client' / 'fileops.py'
    if (not path.is_file() or path.stat().st_size > 1024 * 1024
            or hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest() != PAIRED_FILEOPS_SHA256):
        return None
    module = importlib.import_module('svn_client.fileops')
    trusted_modules(source)
    return module


def _xml_structure(root):
    for node in root.iter():
        if (not isinstance(node.tag, str) or (node.tail or '').strip()
                or len(node) and (node.text or '').strip()):
            raise ValueError('Unexpected XML structure, comments or processing instructions.')
    if (root.text or '').strip():
        raise ValueError('Unexpected XML root text.')


def _exact_status(root, path, workspace):
    if (root.tag != 'status' or root.attrib or len(root.findall('target')) != 1
            or len(root.findall('changelist')) > 1
            or any(node.tag not in {'target', 'changelist'} for node in root)):
        raise ValueError('Invalid exact status scope.')
    for group in root:
        key = 'path' if group.tag == 'target' else 'name'
        if set(group.attrib) != {key} or not group.get(key) or (group.text or '').strip():
            raise ValueError('Invalid status group.')
        if group.tag == 'target' and os.path.normcase(os.path.abspath(workspace / group.get('path'))) != os.path.normcase(os.path.abspath(workspace / path)):
            raise ValueError('Exact status target changed.')
        for entry in group:
            if (entry.tag != 'entry' or set(entry.attrib) != {'path'} or not entry.get('path')
                    or len(entry) != 1 or entry[0].tag != 'wc-status'):
                raise ValueError('Ambiguous exact status entry.')
            node = entry[0]
            booleans = {'copied', 'switched', 'wc-locked', 'file-external', 'tree-conflicted'}
            if ((node.text or '').strip() or set(node.attrib) - (booleans | {'item', 'props', 'revision', 'moved-from', 'moved-to'})
                    or any(node.get(key) not in {'true', 'false'} for key in booleans if key in node.attrib)
                    or len(node) > 1 or any(child.tag != 'commit' for child in node)):
                raise ValueError('Unsafe or ambiguous exact working status.')
            for commit in node:
                if (commit.text or '').strip() or set(commit.attrib) != {'revision'} or len(commit) > 2:
                    raise ValueError('Invalid status commit metadata.')
                if any(child.tag not in {'author', 'date'} or child.attrib or len(child) for child in commit):
                    raise ValueError('Invalid status commit metadata.')
                if len({child.tag for child in commit}) != len(commit):
                    raise ValueError('Duplicate status commit metadata.')
    if len(root.findall('.//entry')) > 1:
        raise ValueError('Status expanded beyond one file.')


def _argv(action, source, destination):
    # SVN copy parses the destination peg; move treats it literally.
    return [action, '--', source + '@' if '@' in source else source,
            destination + '@' if action == 'copy' and '@' in destination else destination]


def guarded_manager(module, history, client):
    """Reuse original execute semantics with bounded local reads and dispatch.

    The original manager's unbounded module-level byte reader cannot be safely
    replaced per instance. Its narrow preflight/postcondition overrides below
    retain native plans, state hashing, metadata validation and original execute
    flow, replacing hashing with the Web's already bounded ordinary-file reader.
    """
    backend = importlib.import_module('svn_client.backend')

    class GuardedClient(backend.SvnClient):
        def __init__(self):
            self.working_copy, self.executable, self._lock = client.working_copy, client.executable, client._lock
            self.deadline = None
            self.before_write = None
            self.expected_argv = None
            self.write_attempted = False
            self.metadata_bytes = 0
            self.reader = history.HistoryManager(self)

        @property
        def timeout(self):
            value = client.timeout
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('Invalid bounded SVN timeout.')
            if self.deadline is not None:
                value = min(value, self.deadline - time.monotonic())
            if value <= 0:
                raise ValueError('File operation deadline exceeded.')
            return value

        def status(self, *args, **kwargs):
            self.timeout
            # Keep explicit synthetic transports injectable. For the production
            # paired client, use its same bounded streaming implementation with
            # the remaining operation budget, never its original fixed timeout.
            if (getattr(client.status, '__func__', None) is backend.SvnClient.status
                    and getattr(client.command, '__func__', None) is backend.SvnClient.command):
                if args or kwargs != {'verbose': True}:
                    raise ValueError('Only a complete verbose local status is permitted.')
                stream = importlib.import_module('svn_client.status_stream')
                argv = [self.executable, 'status', '--non-interactive', '--no-auth-cache',
                        '--xml', '--ignore-externals', '--verbose', '--', '.']
                with stream.status_lock(self._lock, self.timeout, None) as duration:
                    result = stream.run_status(argv, cwd=self.working_copy, timeout=duration,
                        relative=self._relative, display_args=self._display_args(argv),
                        decode=self._decode, cancel_event=None, max_bytes=8 * 1024 * 1024)
            else:
                result = client.status(*args, **kwargs)
            self.timeout
            return result

        def info(self, target=None):
            result = super().info(target)
            # Bind every XML field and text value, canonicalizing only attribute
            # insertion order. The backend's ordinary projection is insufficient
            # for a browser capability whose plan must detect metadata changes.
            result['_web_xml_identity'] = self._last_info_identity
            return result

        def _xml(self, args):
            # All original-manager metadata reads are exact depth-empty locals.
            if (not isinstance(args, list) or not args or args[0] not in {'info', 'status', 'proplist'}
                    or '--' not in args or args.index('--') != len(args) - 2):
                raise ValueError('Only one exact bounded local metadata read is allowed.')
            target = args[-1]
            path = target[:-1] if '@' in target else target
            if path != '.':
                strict_relative_path(path)
            allowed = {
                'info': ['info', '--xml', '--', target],
                'status': ['status', '--xml', '--ignore-externals', '--verbose', '--no-ignore', '--depth', 'empty', '--', target],
                'proplist': ['proplist', '--xml', '--verbose', '--depth', 'empty', '--', target],
            }
            if args != allowed[args[0]]:
                raise ValueError('Metadata scope changed.')
            options = args[1:] if args[0] != 'info' else ['--xml', '--depth', 'empty', '--', target]
            raw = history._bounded_process([self.executable, args[0], '--non-interactive', '--no-auth-cache', *options],
                cwd=self.working_copy, timeout=self.timeout, stdout_limit=XML_BYTES)
            self.timeout
            if not isinstance(raw, bytes):
                raise ValueError('Invalid metadata output.')
            self.metadata_bytes += len(raw)
            if (len(raw) > XML_BYTES or self.metadata_bytes > TOTAL_XML_BYTES or b'\0' in raw
                    or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper()):
                raise ValueError('Oversized or unsafe metadata.')
            try:
                parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
                root = ET.fromstring(raw, parser=parser)
            except (ET.ParseError, ValueError):
                raise ValueError('Invalid local metadata XML.') from None
            _xml_structure(root)
            if args[0] == 'info':
                self._last_info_identity = _xml_identity(root)
                if root.tag != 'info' or root.attrib or len(root) != 1 or root[0].tag != 'entry':
                    raise ValueError('Ambiguous info scope.')
                entry = root[0]
                for node in root.iter():
                    if node.tag in {'entry', 'repository', 'wc-info', 'commit'} and (node.text or '').strip():
                        raise ValueError('Unexpected local info container text.')
                    if len({child.tag for child in node}) != len(node):
                        raise ValueError('Duplicate local info metadata.')
                if (any(node.tag in {'entry', 'info', 'status', 'wc-status', 'target', 'changelist'} for node in entry.iter() if node is not entry)
                        or any(child.tag not in {'url', 'relative-url', 'repository', 'wc-info', 'commit', 'lock', 'conflict', 'tree-conflict'} for child in entry)):
                    raise ValueError('Unexpected nested identity metadata.')
                actual = entry.get('path', '')
                if not actual or os.path.normcase(os.path.abspath(self.working_copy / actual)) != os.path.normcase(os.path.abspath(self.working_copy / path)):
                    raise ValueError('Local info identity changed.')
                for parent, tags in [(entry, ('url', 'relative-url', 'repository', 'wc-info', 'commit', 'lock')),
                                     (entry.find('repository'), ('root', 'uuid')),
                                     (entry.find('wc-info'), ('wcroot-abspath', 'schedule', 'copy-from-url', 'copy-from-rev'))]:
                    if parent is not None and any(len(parent.findall(tag)) > 1 for tag in tags):
                        raise ValueError('Ambiguous local identity metadata.')
                if any(entry.find('.//' + tag) is not None for tag in ('lock', 'conflict', 'tree-conflict')):
                    raise ValueError('Locked or conflicted local node.')
                if entry.findtext('wc-info/schedule') == 'normal' and any(
                        entry.find('wc-info/' + tag) is not None for tag in ('copy-from-url', 'copy-from-rev', 'moved-from', 'moved-to')):
                    raise ValueError('Source or parent has unexpected copy metadata.')
            elif args[0] == 'proplist':
                values = _properties(raw, strict_relative_path(path))
                if 'svn:special' in values or sum(map(len, values.values())) > XML_BYTES:
                    raise ValueError('Special or oversized local properties are unsupported.')
            else:
                _exact_status(root, path, self.working_copy)
            return root

        def command(self, args, **kwargs):
            if (kwargs or not isinstance(args, list) or args != self.expected_argv
                    or args[0] not in {'move', 'copy'} or not callable(self.before_write) or self.write_attempted):
                raise ValueError('Only the one reviewed exact move/copy command is permitted.')
            self.timeout
            self.before_write()
            duration = self.timeout
            self.write_attempted = True
            raw = history._bounded_process([self.executable, args[0], '--non-interactive', '--no-auth-cache', '--quiet', *args[1:]],
                cwd=self.working_copy, timeout=duration, stdout_limit=OUTPUT_BYTES)
            self.timeout
            if not isinstance(raw, bytes) or raw:
                raise ValueError('Quiet file operation returned unexpected command diagnostics.')
            return backend.CommandResult(tuple(args), 0, backend.SvnClient._decode(raw), '')

    guarded = GuardedClient()

    class ExactFileManager(module.FileOperationManager):
        _web_guarded_fileops = True

        def _snapshot(self, path):
            budget = FingerprintBudget()
            budget.seconds = min(budget.seconds, guarded.timeout)
            observed = content_fingerprint(self.working_copy, strict_relative_path(path), budget)
            if observed[1] is None:
                raise ValueError('An existing ordinary file is required.')
            node = (self.working_copy / path).lstat()
            if file_identity(self.working_copy, strict_relative_path(path)) != observed[0]:
                raise StaleRead('File changed during bounded fingerprinting.')
            guarded.timeout
            return observed[1], node.st_size, module._fingerprint(node)

        def _path(self, value, *, allow_root=False):
            path = super()._path(value, allow_root=allow_root)
            guarded.timeout
            if path != '.':
                relative = strict_relative_path(path)
                # Validate every component, not merely the destination basename.
                cursor = self.working_copy
                for part in relative.parts:
                    matches = []
                    with os.scandir(cursor) as entries:
                        for count, entry in enumerate(entries, 1):
                            if count > MAX_DIRECTORY_ENTRIES:
                                raise ValueError('Directory inspection limit exceeded.')
                            guarded.timeout
                            if entry.name.casefold() == part.casefold():
                                matches.append(entry.name)
                    if matches and matches != [part]:
                        raise ValueError('Ambiguous case alias in file path.')
                    cursor /= part
                    if not os.path.lexists(cursor):
                        break
            return path

        def _destination_absent(self, destination):
            # _path performs a bounded case-alias scan of every component.
            self._path(destination)
            absolute = self.working_copy / destination
            if os.path.lexists(absolute) or not absolute.parent.is_dir():
                raise ValueError('Destination must be absent under an existing parent.')
            if self._status(destination, absent_ok=True) is not None:
                raise ValueError('Destination already has SVN metadata.')

        def _source(self, path, root, operation):
            if operation not in {'move', 'copy'}:
                raise ValueError('Deletion is unavailable.')
            info = self.client.info(path)
            self._check_identity(path, info, root, 'file')
            if type(info.get('revision')) is not int or not 0 <= info['revision'] <= 2**63 - 1:
                raise ValueError('Source BASE revision is unavailable.')
            status = self._status(path)
            if status is None:
                raise ValueError('Source has no exact status.')
            self._check_status(status)
            digest, size, fingerprint = self._snapshot(path)
            return module.FileSnapshot(path, info, status, digest, size, self._properties_hash(path), fingerprint)

        def _preflight(self, operation, paths, destination=None):
            with self.client._lock:
                if operation not in {'move', 'copy'} or len(paths) != 1 or destination is None:
                    raise ValueError('One source and one destination are required.')
                selected = self._paths(paths)
                target = self._path(destination)
                if selected[0].casefold() == target.casefold():
                    raise ValueError('Case-only rename or same-path copy is forbidden.')
                # Inspect full paths and size before metadata or ordinary manager reads.
                self._snapshot(selected[0])
                destination_identity = file_identity(self.working_copy, strict_relative_path(target))
                if destination_identity[1][-1][0] != 'absent':
                    raise ValueError('Destination must not exist.')
                root = self._root_info()
                parent_paths = {'.'}
                for path in [*selected, target]:
                    parent_paths.update(str(parent) for parent in Path(path).parents)
                ancestors = tuple(self._ancestor(path, root) for path in sorted(parent_paths))
                files = (self._source(selected[0], root, operation),)
                self._destination_absent(target)
                source = files[0]
                if (self._snapshot(source.path) != (source.content_sha256, source.size_bytes, source.fingerprint)
                        or self._status(source.path) != source.status
                        or self._properties_hash(source.path) != source.properties_sha256):
                    raise StaleRead('Source changed during review.')
                argv = self._argv(operation, selected, target)
                if argv != _argv(operation, selected[0], target) or self.client._command_size(argv) > 24000:
                    raise ValueError('Exact command scope changed or is too large.')
                state = dict(operation=operation, workspace=str(self.working_copy), paths=selected,
                    destination=target, files=[asdict(item) for item in files], ancestors=[asdict(item) for item in ancestors])
                digest = hashlib.sha256(json.dumps(state, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()
                guarded.timeout
                return module.FileOperationPlan(operation, str(self.working_copy), tuple(selected), target,
                    tuple([*selected, target]), files, ancestors, digest, ())

        def _verify_result(self, plan):
            source = plan.files[0]
            if plan.operation == 'move':
                status = self._status(source.path)
                if status is None or status.item != 'deleted' or os.path.lexists(self.working_copy / source.path):
                    raise ValueError('Source removal could not be verified.')
                if ((status.moved_to and str(Path(status.moved_to)) != plan.destination)
                        or status.moved_from or status.copied or status.conflicted or status.switched
                        or status.file_external or status.wc_locked or status.props not in {'none', 'normal', 'modified'}):
                    raise ValueError('Source move metadata differs.')
            else:
                self._path(source.path)
                if (self._status(source.path) != source.status or self.client.info(source.path) != source.info
                        or self._snapshot(source.path) != (source.content_sha256, source.size_bytes, source.fingerprint)
                        or self._properties_hash(source.path) != source.properties_sha256):
                    raise ValueError('Copy source changed during execution.')
            self._path(plan.destination)
            status = self._status(plan.destination)
            if (status is None or status.item != 'added' or not status.copied or status.conflicted
                    or status.switched or status.file_external or status.wc_locked or status.moved_to
                    or status.props not in {'none', 'normal', 'modified'}
                    or status.moved_from and (plan.operation != 'move' or str(Path(status.moved_from)) != source.path)):
                raise ValueError('History-preserving destination could not be verified.')
            digest, size, _ = self._snapshot(plan.destination)
            if (digest != source.content_sha256 or size != source.size_bytes
                    or self._properties_hash(plan.destination) != source.properties_sha256):
                raise ValueError('Destination bytes or properties differ from the reviewed source.')
            # Verify same-root provenance, not merely an arbitrary copied node.
            root = self.client._xml(['info', '--xml', '--', self._target(plan.destination)])
            entry = root[0]
            if (entry.get('kind') != 'file' or entry.findtext('wc-info/wcroot-abspath') != str(self.working_copy)
                    or entry.findtext('wc-info/schedule') != 'add'
                    or entry.findtext('repository/uuid') != source.info['repository_uuid']
                    or entry.findtext('wc-info/copy-from-url') != source.info['url']
                    or entry.findtext('wc-info/copy-from-rev') != str(source.info['revision'])):
                raise ValueError('Destination copy provenance changed.')
            root_info = self._root_info()
            expected_url = str(root_info['url']).rstrip('/') + '/' + module.quote(Path(plan.destination).as_posix(), safe='/')
            if module.unquote(entry.findtext('url', '')).rstrip('/') != module.unquote(expected_url).rstrip('/'):
                raise ValueError('Destination repository mapping changed.')
            if tuple(self._ancestor(item.path, root_info) for item in plan.ancestors) != plan.ancestors:
                raise ValueError('Parent identity changed during execution.')
            guarded.timeout

    return ExactFileManager(guarded)


class FileOperationService:
    def __init__(self, manager, check_workspace):
        if (not getattr(manager, '_web_guarded_fileops', False)
                or any(not callable(getattr(manager, name, None)) for name in ('preflight_move', 'preflight_copy', 'move', 'copy'))):
            raise FileOperationUnavailable('The bounded paired file-operation manager is required.')
        self.manager, self.client, self.check_workspace = manager, manager.client, check_workspace
        self.plan = None

    def invalidate(self):
        self.plan = None

    def _verify(self, identifier, expected, destination):
        self.check_workspace()
        current = status_rows(self.client)
        if current.get(identifier) != expected:
            raise StaleRead('Selected status changed.')
        if (expected['item'] not in {'normal', 'modified'} or expected['props'] not in {'none', 'normal', 'modified'}
                or any(expected.get(key) for key in (*FLAGS, 'copied', 'moved_from', 'moved_to'))):
            raise ValueError('Only ordinary committed source files are supported.')
        for path in (identifier, destination):
            for relative in (strict_relative_path(path), *strict_relative_path(path).parents):
                raw = str(relative)
                aliases = [key for key in current if key.casefold() == raw.casefold()]
                if len(aliases) > 1 or aliases and aliases != [raw]:
                    raise StaleRead('Status has an ambiguous path alias.')
                if raw == destination:
                    if aliases:
                        raise ValueError('Destination already has working-copy metadata.')
                elif raw != identifier:
                    parent = current.get(raw)
                    if (not parent or parent['item'] not in {'normal', 'modified'}
                            or parent['props'] not in {'none', 'normal', 'modified'}
                            or any(parent.get(key) for key in (*FLAGS, 'copied', 'moved_from', 'moved_to'))):
                        raise ValueError('An ordinary same-root destination/source parent is required.')
        self.manager._path(identifier)
        self.manager._path(destination)
        self.manager._snapshot(identifier)
        absent = file_identity(self.client.working_copy, strict_relative_path(destination))
        if absent[1][-1][0] != 'absent':
            raise ValueError('Destination must not exist.')
        self.check_workspace()
        return current

    def _start(self):
        self.client.deadline = time.monotonic() + OPERATION_SECONDS
        self.client.metadata_bytes = 0
        self.client.write_attempted = False
        self.client.expected_argv = None
        self.client.before_write = None

    def _finish(self):
        self.client.deadline = None
        self.client.expected_argv = None
        self.client.before_write = None

    def preflight(self, data, snapshot):
        validate_request('preflight', data)
        self.invalidate()
        if snapshot is None or snapshot[0] != data['snapshotToken'] or data['id'] not in snapshot[1]:
            raise StaleRead('Selection is outside the current status snapshot.')
        identifier, destination = data['id'], str(strict_relative_path(data['destination']))
        expected = dict(snapshot[1][identifier])
        self._start()
        try:
            with self.client._lock:
                self._verify(identifier, expected, destination)
                native = getattr(self.manager, 'preflight_' + data['action'])(identifier, destination)
                if (native.operation != data['action'] or native.paths != (identifier,) or native.destination != destination
                        or native.affected_paths != (identifier, destination) or len(native.files) != 1):
                    raise ValueError('Backend changed the exact operation scope.')
                self._verify(identifier, expected, destination)
                # Bind a second full metadata/content observation, not only status.
                if getattr(self.manager, 'preflight_' + data['action'])(identifier, destination) != native:
                    raise StaleRead('Local file or parent metadata changed during review.')
                self.check_workspace()
                self.client.timeout
            scope = {key: data[key] for key in COMMON}
            token = secrets.token_urlsafe(24)
            self.plan = dict(scope, token=token, expires=time.monotonic() + PLAN_SECONDS,
                             expected=expected, native=native)
            source = {key: expected[key] for key in ('id', 'path', 'item', 'props')}
            return dict(scope, mode='live', planToken=token, expiresIn=PLAN_SECONDS, action=native.operation,
                localOnly=True, commitRequired=True, source=source, files=[source],
                destination=Path(destination).as_posix(), affectedPaths=[source['path'], Path(destination).as_posix()])
        finally:
            self._finish()

    def apply(self, data, snapshot, invalidate_snapshot):
        validate_request('apply', data)
        plan = self.plan
        self.invalidate()
        if (plan is None or data['planToken'] != plan['token'] or time.monotonic() >= plan['expires']
                or any(data[key] != plan[key] for key in COMMON)
                or snapshot is None or snapshot[0] != plan['snapshotToken']):
            raise StaleRead('Reviewed file-operation plan expired or was already used.')
        native = plan['native']
        identifier, destination = native.paths[0], native.destination
        self._start()
        try:
            with self.client._lock:
                before = self._verify(identifier, plan['expected'], destination)
                self.client.expected_argv = _argv(native.operation, identifier, destination)
                def before_write():
                    self._verify(identifier, plan['expected'], destination)
                    # Last dispatch check includes bytes/properties/root/parents.
                    if getattr(self.manager, 'preflight_' + native.operation)(identifier, destination) != native:
                        raise StaleRead('Reviewed file state changed before dispatch.')
                    if time.monotonic() >= plan['expires']:
                        raise StaleRead('Plan expired during validation.')
                    self.check_workspace()
                    invalidate_snapshot()
                    if time.monotonic() >= plan['expires']:
                        raise StaleRead('Plan expired at dispatch; no command was attempted.')
                self.client.before_write = before_write
                try:
                    getattr(self.manager, native.operation)(identifier, destination, confirmed=True,
                        expected_state_sha256=native.state_sha256)
                    if not self.client.write_attempted:
                        raise ValueError('No exact file operation was dispatched.')
                    self.check_workspace()
                    current = status_rows(self.client)
                    if ({key: row for key, row in current.items() if key not in {identifier, destination}}
                            != {key: row for key, row in before.items() if key not in {identifier, destination}}):
                        raise ValueError('Unselected status changed during file operation.')
                    # Recheck postconditions after the complete status observation.
                    self.manager._verify_result(native)
                    self.check_workspace()
                    self.client.timeout
                except Exception as error:
                    if not self.client.write_attempted:
                        raise
                    raise MutationUncertain('File operation outcome is uncertain; refresh and inspect both paths. No rollback was attempted.') from error
                result = dict(action=native.operation, files=[plan['expected']['path']], destination=Path(destination).as_posix(),
                    affectedPaths=[plan['expected']['path'], Path(destination).as_posix()],
                    outcome='verified', verified=True, commitRequired=True, warnings=[])
                # The verified command fact survives a later snapshot read failure.
                try:
                    rows = status_rows(self.client)
                    self.check_workspace()
                    if rows != current:
                        raise StaleRead('Status changed after verified file operation.')
                except Exception:
                    rows = None
                    result['warnings'].append('status-refresh-failed')
                return result, rows
        finally:
            self._finish()
