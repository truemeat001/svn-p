"""Bounded, display-only WORKING/BASE properties of one ordinary local file.

Repeated observations detect observed changes; they are not an atomic snapshot.
No property writes, inherited properties, remote revisions or raw-command API.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import unicodedata
import xml.etree.ElementTree as ET

from file_reads import StaleRead, file_identity, strict_relative_path

XML_BYTES = 1024 * 1024
PROPERTY_BYTES = 64 * 1024
TOTAL_BYTES = 256 * 1024
MAX_PROPERTIES = 128
PREVIEW_BYTES = 4096
RESPONSE_BYTES = 512 * 1024


def _blank(value):
    return value is None or not value.strip()


def _xml_identity(node):
    """Attribute ordering is incidental; text, tails and child order are not."""
    return (node.tag, tuple(sorted(node.attrib.items())), node.text, node.tail,
            tuple(_xml_identity(child) for child in node))


def _properties(raw, relative):
    if (not isinstance(raw, bytes) or len(raw) > XML_BYTES or b'\0' in raw
            or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper()):
        raise ValueError('Invalid or oversized property XML.')
    try:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
        root = ET.fromstring(raw, parser=parser)
    except (ET.ParseError, ValueError):
        raise ValueError('Invalid property XML.') from None
    if (root.tag != 'properties' or root.attrib or not _blank(root.text) or not _blank(root.tail)
            or len(root) > 1):
        raise ValueError('Invalid property scope.')
    if not len(root):
        return {}
    target = root[0]
    # SVN prints its local operand without the empty peg delimiter. Do not
    # normalize ../, absolute paths or alternative spelling into this identity.
    if (target.tag != 'target' or set(target.attrib) != {'path'}
            or strict_relative_path(target.get('path')).as_posix() != relative.as_posix()
            or not _blank(target.text)
            or not _blank(target.tail) or len(target) > MAX_PROPERTIES):
        raise ValueError('Property target is not the selected exact file.')
    result = {}
    for prop in target:
        name = prop.get('name')
        if (prop.tag != 'property' or set(prop.attrib) not in ({'name'}, {'name', 'encoding'})
                or not name or len(name.encode('utf-16-le')) // 2 > 256 or name in result or len(prop)
                or any(c.isspace() or unicodedata.category(c).startswith('C') for c in name)
                or not _blank(prop.tail)):
            raise ValueError('Invalid or duplicate property.')
        value = prop.text or ''
        if 'encoding' in prop.attrib:
            if prop.get('encoding') != 'base64':
                raise ValueError('Unsupported property encoding.')
            try:
                # SVN line-wraps base64; only XML whitespace is ignored.
                encoded = ''.join(value.split())
                if any(c.isspace() and c not in ' \t\r\n' for c in value):
                    raise ValueError('Invalid base64 whitespace.')
                decoded = base64.b64decode(encoded, validate=True)
                if base64.b64encode(decoded).decode('ascii') != encoded:
                    raise ValueError('Noncanonical base64.')
                value = decoded
            except (ValueError, binascii.Error):
                raise ValueError('Invalid base64 property.') from None
        else:
            value = value.encode('utf-8')
        if len(value) > PROPERTY_BYTES:
            raise ValueError('Property exceeds the value limit.')
        result[name] = value
    return result


def _value(raw, redact):
    result = dict(kind='binary', text=None, byteLength=len(raw),
                  sha256=hashlib.sha256(raw).hexdigest(), textTruncated=False,
                  redacted=False, escapedControls=0)
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return result
    if '\0' in text:
        return result
    safe = redact(text)
    escaped = 0
    parts = []
    for char in safe:
        if unicodedata.category(char).startswith('C'):
            escaped += 1
            parts.append('\\u%04x' % ord(char) if ord(char) <= 0xffff else '\\U%08x' % ord(char))
        else:
            parts.append(char)
    encoded = ''.join(parts).encode('utf-8')
    result.update(kind='text', text=encoded[:PREVIEW_BYTES].decode('utf-8', errors='ignore'),
                  textTruncated=len(encoded) > PREVIEW_BYTES, redacted=safe != text,
                  escapedControls=escaped)
    return result


class PropertyReadService:
    def __init__(self, client, history_module, history_manager):
        self.client = client
        self.module = history_module
        self.manager = history_manager

    def _status(self, path, expected):
        entries = self.client.status([path], verbose=True)
        if len(entries) != 1:
            raise StaleRead('Exact file status changed; refresh before reading properties.')
        row = asdict(entries[0])
        identifier = row['path']
        row.update(id=identifier, path=identifier.replace(os.sep, '/'), kind='unknown')
        if row != expected:
            raise StaleRead('File status changed; refresh before reading properties.')

    def _info(self, target, workspace):
        self.manager._check_target(target)
        info, _ = self.manager._info(target)
        root = info.findtext('wc-info/wcroot-abspath', '')
        if (info.get('kind') != 'file' or not root or not Path(root).is_absolute()
                or os.path.normcase(os.path.abspath(root)) != os.path.normcase(os.path.abspath(workspace))):
            raise ValueError('Properties require this exact working-copy root and file.')
        if info.find('wc-info/copy-from-url') is not None or info.find('wc-info/copy-from-rev') is not None:
            raise ValueError('Copied files are outside property comparison scope.')
        return _xml_identity(info)

    def _read(self, target, relative, base=False):
        timeout = self.client.timeout
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('Invalid local property timeout.')
        argv = [self.client.executable, 'proplist', '--non-interactive', '--no-auth-cache',
                '--xml', '--verbose', '--depth', 'empty']
        if base:
            argv += ['--revision', 'BASE']
        argv += ['--', target]
        try:
            raw = self.module._bounded_process(argv, cwd=self.client.working_copy,
                                               timeout=timeout, stdout_limit=XML_BYTES)
        except Exception:
            raise ValueError('Local property read failed; no result was accepted.') from None
        return _properties(raw, relative)

    def read(self, row):
        path = row['id']
        relative = strict_relative_path(path)
        if (row.get('item') not in {'normal', 'none', 'modified', 'added'}
                or row.get('props') not in {'normal', 'none', 'modified'}
                or any(row.get(key) for key in ('tree_conflicted', 'copied', 'moved_from',
                       'moved_to', 'switched', 'file_external', 'wc_locked'))):
            raise ValueError('Only safe ordinary versioned files support property reads.')
        workspace = self.client.working_copy
        with self.client._lock:
            identity = file_identity(workspace, relative)
            if not (workspace / relative).is_file():
                raise ValueError('Property comparison requires an existing ordinary file.')
            self._status(path, row)
            actual, target = self.manager._target(path)
            if Path(actual) != relative:
                raise ValueError('Property target identity mismatch.')
            info = self._info(target, workspace)
            added = row['item'] == 'added'
            working = self._read(target, relative)
            base = {} if added else self._read(target, relative, base=True)
            names = sorted(set(base) | set(working))
            if (len(names) > MAX_PROPERTIES
                    or sum(map(len, base.values())) + sum(map(len, working.values())) > TOTAL_BYTES):
                raise ValueError('Combined property limit exceeded.')
            if (working != self._read(target, relative)
                    or (not added and base != self._read(target, relative, base=True))):
                raise StaleRead('Properties changed during the read; refresh and retry.')
            if info != self._info(target, workspace):
                raise StaleRead('Local file metadata changed during the property read.')
            self._status(path, row)
            if identity != file_identity(workspace, relative):
                raise StaleRead('File identity changed during the property read.')
            properties = []
            for name in names:
                left, right = base.get(name), working.get(name)
                change = ('unbased' if added else 'added' if left is None else
                          'deleted' if right is None else 'unchanged' if left == right else 'modified')
                properties.append(dict(name=name, change=change,
                    base=_value(left, self.module.redact_credentials) if left is not None else None,
                    working=_value(right, self.module.redact_credentials) if right is not None else None))
            payload = dict(path=relative.as_posix(), localOnly=True, baseAvailable=not added,
                           baseUnavailableReason='added-file' if added else None,
                           displayOnly=True, properties=properties)
            if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > RESPONSE_BYTES:
                raise ValueError('Property display response exceeds its limit.')
            return payload
