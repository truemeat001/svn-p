"""CLI-bounded workspace catalog and local selection preferences.

Endpoints never accept filesystem paths; optional label mutations are separately gated. Discovery
candidates are observations, not proof of repository identity. Persistence is
explicitly opt-in and stores preferences outside the working copies.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
import importlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import threading

MAX_SETTINGS_BYTES = 1024 * 1024
REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


def local_path(value):
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw.strip() or '://' in raw or any(ord(c) < 32 for c in raw):
        raise ValueError('A local directory is required.')
    # Do not resolve: it would silently follow links before checking them.
    return Path(os.path.abspath(os.path.expanduser(raw)))


def directory_identity(path):
    """Record every directory component without following symbolic links."""
    path = local_path(path)
    current = Path(path.anchor)
    identity = []
    for part in (None, *path.parts[1:]):
        if part is not None:
            current /= part
        info = os.lstat(current)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & REPARSE):
            raise ValueError('Links and reparse points are outside the workspace scope.')
        identity.append((info.st_dev, info.st_ino, str(current)))
    return tuple(identity)


def working_copy_identity(path):
    path = local_path(path)
    if any(part.lower() == '.svn' for part in path.parts):
        raise ValueError('Administrative directories cannot be selected.')
    return directory_identity(path), directory_identity(path / '.svn')


def trusted_modules(source):
    """Import the trusted backend and verify the whole loaded namespace."""
    source = Path(source).expanduser().resolve(strict=True)
    package = source / 'svn_client'
    for name in ('backend', 'status_stream', 'discovery', 'workspaces'):
        if not (package / (name + '.py')).is_file():
            raise ValueError('The trusted backend must include workspace/discovery support.')
    for name, module in list(sys.modules.items()):
        if name.startswith('svn_client.') and getattr(module, '__file__', None):
            if Path(module.__file__).resolve().parent != package:
                raise ValueError('Unexpected backend module was already loaded.')
    sys.path.insert(0, str(source))
    try:
        modules = [importlib.import_module('svn_client.' + name)
                   for name in ('backend', 'status_stream', 'discovery', 'workspaces')]
        namespace = importlib.import_module('svn_client')
        if any(Path(p).resolve() != package for p in namespace.__path__):
            raise ValueError('Unexpected backend namespace.')
        for name, module in list(sys.modules.items()):
            if name.startswith('svn_client.') and getattr(module, '__file__', None):
                if Path(module.__file__).resolve().parent != package:
                    raise ValueError('Unexpected backend module.')
        return tuple(modules)
    finally:
        sys.path.remove(str(source))


@dataclass(frozen=True)
class Candidate:
    id: str
    path: Path
    identity: tuple
    relation: str

    def public(self):
        return {'id': self.id, 'name': self.path.name or str(self.path),
                'path': str(self.path), 'relation': self.relation,
                'verified': False}  # Successful status is not repository info validation.


class WorkspaceCatalog:
    def __init__(self, *, roots=(), parents=(), discovery, settings, adapter_factory,
                 settings_file=None):
        self.roots = tuple(dict.fromkeys(local_path(p) for p in roots))
        self.parents = tuple(dict.fromkeys(local_path(p) for p in parents))
        self.parent_identities = {p: directory_identity(p) for p in self.parents}
        self.root_identities = {p: working_copy_identity(p) for p in self.roots}
        self.discovery, self.settings = discovery, settings
        self.adapter_factory = adapter_factory
        self.settings_file = local_path(settings_file) if settings_file else None
        self.settings_parent_identity = None
        self.settings_warning = None
        self.saved = {}
        self._busy = threading.Lock()
        self.candidates = {}
        self._selection = (None, None, 0)  # Atomic identity, cached adapter, generation.
        self.scans = []
        self.discover()
        if self.settings_file:
            self._validate_settings_path(initial=True)
            try:
                if self.settings_file.exists() and self.settings_file.stat().st_size > MAX_SETTINGS_BYTES:
                    raise ValueError('Settings exceed limit.')
                self.saved = self.settings.load_settings(self.settings_file)
            except (OSError, ValueError):
                # Corruption is not permission to overwrite the existing file.
                self.settings_warning = '本地偏好读取失败，已禁用本次保存；原文件未修改。'
        # Restoring does not execute SVN. The first status must still succeed.
        by_path = {str(c.path): c for c in self.candidates.values()}
        chosen = by_path.get(str(self.roots[0])) if self.roots else None
        if not chosen and self.saved.get('restore_last_workspace', True):
            saved_path = self.saved.get('last_workspace')
            chosen = by_path.get(saved_path) if isinstance(saved_path, str) else None
        if chosen:
            self._selection = (chosen, None, 1)

    @property
    def selected(self):
        return self._selection[0]

    @property
    def adapter(self):
        return self._selection[1]

    @property
    def generation(self):
        return self._selection[2]

    @contextmanager
    def operation(self):
        if not self._busy.acquire(blocking=False):
            raise BlockingIOError('A workspace operation is already running.')
        try:
            yield
        finally:
            self._busy.release()

    def _check(self, candidate):
        if working_copy_identity(candidate.path) != candidate.identity:
            raise ValueError('Working copy changed since discovery.')
        for parent, identity in self.parent_identities.items():
            if candidate.path == parent or parent in candidate.path.parents:
                if directory_identity(parent) != identity:
                    raise ValueError('Authorized scan parent changed.')

    def _validate_settings_path(self, initial=False):
        path = self.settings_file
        if any(part.lower() == '.svn' for part in path.parts):
            raise ValueError('Preferences cannot be stored in SVN metadata.')
        # Also inspect ancestors above the declared roots, including an enclosing WC.
        for parent in path.parents:
            if os.path.lexists(parent / '.svn'):
                raise ValueError('Preferences must be outside all working copies.')
        if any(path == p or p in path.parents for p in (*self.roots, *self.parents)):
            raise ValueError('Preferences must be outside authorized scan/workspace directories.')
        identity = directory_identity(path.parent)  # Parent must already exist.
        if initial:
            self.settings_parent_identity = identity
        elif identity != self.settings_parent_identity:
            raise ValueError('Preferences directory changed.')
        if os.path.lexists(path):
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, 'st_file_attributes', 0) & REPARSE:
                raise ValueError('Unsafe preferences file.')

    def discover(self):
        with self.operation():
            old = {str(c.path): c for c in self.candidates.values()}
            found, scans = {}, []

            def add(path, relation, expected=None):
                path = local_path(path)
                identity = working_copy_identity(path)
                if expected is not None and identity != expected:
                    raise ValueError('Explicit working copy changed.')
                previous = old.get(str(path))
                identifier = previous.id if previous and previous.identity == identity else secrets.token_urlsafe(18)
                found[str(path)] = Candidate(identifier, path, identity, relation)

            for path in self.roots:
                try:
                    add(path, 'explicit', self.root_identities[path])
                except (OSError, ValueError):
                    scans.append({'name': path.name, 'complete': False, 'truncated': False,
                                  'reasons': ['explicit_root_unavailable'], 'count': 0})
            for parent in self.parents:
                try:
                    if directory_identity(parent) != self.parent_identities[parent]:
                        raise ValueError('Scan parent changed.')
                    result = self.discovery.discover_working_copies(parent)
                    rejected = 0
                    for item in result.working_copies:
                        path = local_path(item.path)
                        if path != parent and parent not in path.parents:
                            rejected += 1
                            continue
                        try:
                            add(path, item.relation)
                        except (OSError, ValueError):
                            rejected += 1
                    scans.append({'name': parent.name or str(parent), 'complete': result.complete and not rejected,
                                  'truncated': result.truncated,
                                  'reasons': list(result.reasons) + (['candidate_changed'] if rejected else []),
                                  'count': len(result.working_copies) - rejected,
                                  'directoriesScanned': result.directories_scanned,
                                  'skippedLinks': result.skipped_links})
                except (OSError, ValueError):
                    scans.append({'name': parent.name, 'complete': False, 'truncated': False,
                                  'reasons': ['parent_unavailable'], 'count': 0})
            self.candidates = {c.id: c for c in found.values()}
            selected = self.selected
            if selected and selected.id not in self.candidates:
                try:
                    self._check(selected)
                    # Incomplete scans must not silently discard an unchanged,
                    # previously authorized selection that the scan omitted.
                    self.candidates[selected.id] = selected
                except (OSError, ValueError):
                    self._selection = (None, None, self.generation + 1)
            self.scans = scans
            return self.describe()

    def describe(self):
        current = self.config()
        return {'enabled': True, 'candidates': [c.public() for c in self.candidates.values()],
                'selectedId': current['workspaceId'], 'current': current,
                'selectionVersion': current['selectionVersion'], 'scans': self.scans,
                'persistenceEnabled': bool(self.settings_file) and not self.settings_warning,
                'warning': self.settings_warning,
                'scope': '只扫描启动参数授权的父目录；候选尚未验证仓库身份。'}

    def config(self):
        selected, adapter, generation = self._selection
        changelists = bool(adapter is not None and getattr(adapter, '_changelist_service', None) is not None)
        shelves = bool(adapter is not None and getattr(adapter, '_shelf_service', None) is not None)
        commits = bool(adapter is not None and getattr(adapter, '_commit_service', None) is not None)
        return {'mode': 'live' if selected else 'select',
                'workspace': selected.path.name if selected else '尚未选择工作区',
                'root': str(selected.path) if selected else '请从已授权候选中选择',
                'workspaceId': selected.id if selected else None,
                'selectionVersion': generation, 'writesEnabled': changelists or shelves or commits,
                'commitsEnabled': commits,
                'shelfWritesEnabled': shelves,
                'changelistWritesEnabled': changelists,
                'workspaceSelectionEnabled': True,
                'persistenceEnabled': bool(self.settings_file) and not self.settings_warning}

    def _snapshot(self, candidate, adapter):
        self._check(candidate)
        payload = adapter.snapshot()
        self._check(candidate)
        if payload.get('mode') != 'live' or not isinstance(payload.get('rows'), list):
            raise ValueError('Invalid status snapshot.')
        return payload

    def snapshot(self):
        with self.operation():
            if self.selected is None:
                return {**self.config(), 'rows': []}
            self._check(self.selected)
            adapter = self.adapter or self.adapter_factory(self.selected.path)
            payload = self._snapshot(self.selected, adapter)
            self._selection = (self.selected, adapter, self.generation)
            return {**payload, **self.config()}

    def file_read(self, kind, data):
        # The catalog lock prevents a selection/refresh from racing a read in
        # another browser window. Request identity is checked before any SVN.
        from file_reads import StaleRead
        with self.operation():
            candidate, adapter, generation = self._selection
            if (candidate is None or adapter is None or data['workspaceId'] != candidate.id
                    or data['selectionVersion'] != generation):
                raise StaleRead('Workspace selection changed.')
            self._check(candidate)
            payload = adapter.file_read(kind, data)
            self._check(candidate)
            return {**payload, **self.config()}

    def changelist_operation(self, kind, data):
        from file_reads import StaleRead
        from changelist_ops import MutationUncertain
        with self.operation():
            candidate, adapter, generation = self._selection
            if (candidate is None or adapter is None or data['workspaceId'] != candidate.id
                    or data['selectionVersion'] != generation):
                raise StaleRead('Workspace selection changed.')
            self._check(candidate)
            payload = adapter.changelist_operation(kind, data)
            try:
                self._check(candidate)
            except Exception as error:
                adapter._read_snapshot = None
                if getattr(adapter, '_changelist_service', None):
                    adapter._changelist_service.invalidate()
                if kind == 'apply':
                    raise MutationUncertain('Workspace changed after label execution.') from error
                raise
            return {**payload, **self.config()}

    def shelf_operation(self, kind, data):
        from file_reads import StaleRead
        from changelist_ops import MutationUncertain
        with self.operation():
            candidate, adapter, generation = self._selection
            if (candidate is None or adapter is None or data['workspaceId'] != candidate.id
                    or data['selectionVersion'] != generation):
                raise StaleRead('Workspace selection changed.')
            self._check(candidate)
            service = getattr(adapter, '_shelf_service', None)
            if service:
                storage = local_path(service.manager.storage_dir)
                if any(storage == p or p in storage.parents for p in (*self.roots, *self.parents)):
                    raise ValueError('Shelf storage overlaps the authorized project scope.')
            payload = adapter.shelf_operation(kind, data)
            try:
                self._check(candidate)
            except Exception as error:
                adapter._read_snapshot = None
                if service:
                    service.invalidate()
                if kind == 'apply':
                    raise MutationUncertain('Workspace changed after Shelf execution.') from error
                raise
            return {**payload, **self.config()}

    def commit_operation(self, kind, data):
        from file_reads import StaleRead
        from changelist_ops import MutationUncertain
        with self.operation():
            candidate, adapter, generation = self._selection
            if (candidate is None or adapter is None or data['workspaceId'] != candidate.id
                    or data['selectionVersion'] != generation):
                raise StaleRead('Workspace selection changed.')
            self._check(candidate)
            payload = adapter.commit_operation(kind, data)
            try:
                self._check(candidate)
            except Exception as error:
                adapter._read_snapshot = None
                for name in ('_changelist_service', '_shelf_service', '_commit_service'):
                    service = getattr(adapter, name, None)
                    if service:
                        service.invalidate()
                if kind == 'apply':
                    result = payload.get('commitResult')
                    if isinstance(result, dict) and result.get('commandCompleted') is True:
                        return {**self.config(), **adapter._commit_status_unknown(result, 'workspace-changed')}
                    raise MutationUncertain('Workspace changed after commit execution.') from error
                raise
            if payload.get('statusRefreshed') is False:
                return {**self.config(), **payload}  # Revoked capabilities win over configured services.
            return {**payload, **self.config()}

    def select(self, identifier):
        with self.operation():
            candidate = self.candidates.get(identifier)
            if candidate is None:
                raise KeyError('Candidate is stale or outside the authorized catalog.')
            self._check(candidate)
            adapter = self.adapter_factory(candidate.path)
            payload = self._snapshot(candidate, adapter)
            saved = False
            warning = self.settings_warning
            if self.settings_file and not warning:
                try:
                    self._validate_settings_path()
                    # Build a fresh allowlisted record, never reuse loaded executable/paths.
                    preferences = {'format': 1, 'workspaces': [], 'restore_last_workspace': True}
                    self.settings.remember_workspace(preferences, str(candidate.path))
                    self.settings.save_settings(self.settings_file, preferences)
                    self.saved = preferences
                    saved = True
                except (OSError, ValueError):
                    warning = '状态已读取，但偏好保存失败；下次启动可能无法恢复本次选择。'
            self._selection = (candidate, adapter, self.generation + 1)
            return {**payload, **self.config(), 'selectionSaved': saved, 'warning': warning}
