"""Exact-file bridge over the trusted bounded HistoryManager. No write API."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat

from workspace_bridge import REPARSE, directory_identity, trusted_modules

DIFF_TEXT_BYTES = 256 * 1024
HISTORY_PAGE_SIZE = 50
HISTORY_RESPONSE_BYTES = 512 * 1024
MAX_REVISION = 2**63 - 1


class StaleRead(ValueError):
    """The UI's workspace/status capability no longer identifies this read."""


def revision(value, *, working=False):
    # Wire values are strings: JS numbers cannot represent all SVN revisions.
    if not isinstance(value, str):
        raise ValueError('Revision must be an explicit string.')
    if value in {'BASE', 'HEAD'} or (working and value == 'WORKING'):
        return value
    if re.fullmatch(r'0|[1-9][0-9]{0,18}', value) and int(value) <= MAX_REVISION:
        return value
    raise ValueError('Unsupported revision.')


def strict_relative_path(value):
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or ':' in value or PureWindowsPath(value).is_absolute()
            or PureWindowsPath(value).drive or value.startswith(('/', '\\'))):
        raise ValueError('An exact relative project file is required.')
    # Windows status IDs keep native separators. On POSIX a literal backslash
    # filename is deliberately unsupported rather than interpreted two ways.
    normalized = value.replace('\\', '/') if os.name == 'nt' else value
    if '\\' in normalized:
        raise ValueError('Ambiguous path separator.')
    parts = normalized.split('/')
    if any(part in {'', '.', '..'} or part.lower() == '.svn'
           or part.endswith((' ', '.')) for part in parts):
        raise ValueError('Administrative, parent, or ambiguous path is forbidden.')
    return Path(*parts)


def file_identity(workspace, relative):
    """Refuse links, nested WCs, device crossings, and non-files before SVN."""
    parents = directory_identity(workspace)
    root_device = os.lstat(workspace).st_dev
    cursor = workspace
    chain = []
    for index, part in enumerate(relative.parts):
        cursor /= part
        last = index == len(relative.parts) - 1
        # A deleted/missing file may still be an SVN file; parent scope may not
        # be absent because that would prevent checking its identity safely.
        try:
            info = os.lstat(cursor)
        except FileNotFoundError:
            if last:
                chain.append(('absent', str(cursor)))
                break
            raise ValueError('A parent is missing.') from None
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & REPARSE
                or info.st_dev != root_device):
            raise ValueError('Links or cross-device targets are forbidden.')
        if last:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Only one regular non-hardlinked file can be read.')
            chain.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                          info.st_ctime_ns, info.st_nlink))
        else:
            if not stat.S_ISDIR(info.st_mode) or os.path.lexists(cursor / '.svn'):
                raise ValueError('Nested working copies are outside this file scope.')
            chain.append((info.st_dev, info.st_ino, str(cursor)))
    return parents, tuple(chain)


def history_module(source):
    trusted_modules(source)
    package = Path(source).expanduser().resolve(strict=True) / 'svn_client'
    if not (package / 'history.py').is_file():
        return None
    module = importlib.import_module('svn_client.history')
    # Check the imported module AND dependencies rather than trusting sys.path.
    trusted_modules(source)
    return module if getattr(module, 'HISTORY_DIAGNOSTICS_POLICY', None) == 1 else None


class FileReadService:
    def __init__(self, client, module):
        self.client = client
        class ExactFileHistory(module.HistoryManager):
            def _run(self, args, byte_limit):
                # Do not rely on current file kind or historical peg ancestry
                # to prevent descent. Original directory-aware UI is unchanged.
                if args[0] == 'diff':
                    args = [args[0], '--depth', 'empty', *args[1:]]
                return super()._run(args, byte_limit)
        self.manager = ExactFileHistory(client)

    def read(self, kind, row, data):
        if kind == 'diff':
            left = revision(data['fromRevision'])
            right = revision(data['toRevision'], working=True)
        elif kind == 'history':
            start = revision(data['startRevision'])
        else:
            raise ValueError('Read kind is not allowed.')
        path = row['id']
        relative = strict_relative_path(path)
        if (row.get('item') in {'unversioned', 'ignored', 'external', 'obstructed', 'incomplete'}
                or row.get('file_external') or row.get('switched')):
            raise ValueError('This status row is outside versioned-file scope.')
        workspace = self.client.working_copy
        before = file_identity(workspace, relative)
        # HistoryManager permits directory scopes. This endpoint intentionally
        # narrows that contract to one file, with no root/directory fallback.
        with self.client._lock:
            _, target = self.manager._target(path)
            info, _ = self.manager._info(target)
            if info.get('kind') != 'file':
                raise ValueError('The selected status row is not a versioned file.')
            expected = os.path.normcase(os.path.abspath(workspace))
            actual = info.findtext('wc-info/wcroot-abspath', '')
            if not actual or os.path.normcase(os.path.abspath(actual)) != expected:
                raise ValueError('Target belongs to another working copy.')
            if kind == 'diff':
                result = self.manager.compare(path, from_revision=left,
                    to_revision=right, max_text_bytes=DIFF_TEXT_BYTES)
                if (Path(result.path) != relative or result.from_revision != left
                        or result.to_revision != right or result.binary_payloads_included):
                    raise ValueError('Comparison scope mismatch.')
                diff = result.diff
                if (len(diff.text.encode('utf-8')) > DIFF_TEXT_BYTES
                        or diff.returned_utf8_bytes != len(diff.text.encode('utf-8'))
                        or diff.source_utf8_bytes < diff.returned_utf8_bytes
                        or not isinstance(diff.truncated, bool)
                        or diff.truncated != (diff.source_utf8_bytes > diff.returned_utf8_bytes)):
                    raise ValueError('Comparison output limit mismatch.')
                payload = {'path': relative.as_posix(), 'fromRevision': left,
                    'toRevision': right, 'text': diff.text, 'truncated': diff.truncated,
                    'sourceUtf8Bytes': diff.source_utf8_bytes,
                    'returnedUtf8Bytes': diff.returned_utf8_bytes,
                    'binaryPayloadsIncluded': False}
            elif kind == 'history':
                result = self.manager.history(path, limit=HISTORY_PAGE_SIZE, start_revision=start)
                if Path(result.path) != relative or result.start_revision != start or len(result.entries) > HISTORY_PAGE_SIZE:
                    raise ValueError('History scope mismatch.')
                if result.has_more and (not result.entries or result.next_start_revision is None
                        or result.next_start_revision < 0
                        or result.next_start_revision != result.entries[-1].revision - 1):
                    raise ValueError('History pagination mismatch.')
                payload = {'path': relative.as_posix(), 'startRevision': start,
                    'hasMore': result.has_more,
                    'nextStartRevision': str(result.next_start_revision) if result.has_more else None,
                    'entries': [{'revision': str(e.revision), 'author': e.author,
                        'date': e.date, 'message': e.message,
                        'changedPaths': [{**p, 'copyfrom_rev': str(p['copyfrom_rev'])
                            if p.get('copyfrom_rev') is not None else None} for p in e.changed_paths]}
                        for e in result.entries]}
                if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > HISTORY_RESPONSE_BYTES:
                    raise ValueError('History response exceeds the display limit.')
            else:
                raise ValueError('Read kind is not allowed.')
        if file_identity(workspace, relative) != before:
            raise ValueError('Target changed while reading; no result accepted.')
        return payload
