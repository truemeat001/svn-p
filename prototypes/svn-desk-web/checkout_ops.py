"""One explicitly configured checkout, with local review and one-use confirmation.

The page cannot supply URLs, paths, credentials or commands. The original paired
validators and bounded pipe runner are reused without changing their globals.
A failed/ambiguous attempt keeps all files and permanently disarms this session.
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from checkout_paths import local_destination, pinned_directory
from file_reads import StaleRead
from workspace_bridge import directory_identity, local_path, working_copy_identity

PLAN_SECONDS = 120
OPERATION_SECONDS = 300
OUTPUT_BYTES = 256 * 1024


def validate_request(kind, data):
    if not isinstance(data, dict):
        raise ValueError('A strict checkout request is required.')
    if kind == 'preflight' and not data:
        return
    if (kind != 'apply' or set(data) != {'planToken', 'confirmed'}
            or data['confirmed'] is not True
            or not isinstance(data['planToken'], str)
            or not 1 <= len(data['planToken']) <= 64
            or any(ord(c) < 33 or ord(c) > 126 for c in data['planToken'])):
        raise ValueError('Explicit confirmation of the fixed checkout is required.')


def checkout_arguments(parser):
    parser.add_argument('--checkout-url', help='One fixed repository URL; UI confirmation is still required')
    parser.add_argument('--checkout-destination', type=Path, help='One fixed new/empty local directory with existing parent')


def checkout_requested(args):
    url, destination = args.checkout_url, args.checkout_destination
    if bool(url) != bool(destination):
        raise ValueError('Checkout URL and destination must be provided together.')
    return bool(url and destination)


def _stable_identity(path):
    identity = directory_identity(path)
    if any(not item[1] for item in identity):
        raise ValueError('Stable directory identity is required.')
    return identity


class CheckoutService:
    def __init__(self, *, url, destination, backend, history, catalog, executable=None):
        self.url = backend.validate_checkout_url(url)
        parsed = urlsplit(self.url)
        # Do not accept even username-only URL credentials. Peg revisions and
        # SSH helpers are outside this first checkout flow; encode literal @.
        if (len(self.url.encode('utf-8')) > 4096 or parsed.username is not None
                or '@' in parsed.path or (parsed.scheme == 'file' and parsed.netloc)):
            raise ValueError('Credential-free URL without a peg revision is required.')
        self.destination = local_path(local_destination(destination))
        if len(str(self.destination).encode('utf-8')) > 4096:
            raise ValueError('Checkout destination is too long.')
        self.backend, self.history, self.catalog = backend, history, catalog
        self.executable = executable
        self.plan = None
        self.state, self.result = 'ready', None
        self._busy = threading.Lock()
        self.initial_identity = self._observe()
        if catalog.settings_file and (catalog.settings_file == self.destination
                or self.destination in catalog.settings_file.parents):
            raise ValueError('Checkout must not contain the preferences file.')

    def _observe(self):
        path = self.backend.validate_checkout_destination(self.destination)
        parent = _stable_identity(path.parent)
        existing = _stable_identity(path) if os.path.lexists(path) else None
        return parent, existing

    def describe(self):
        return {'enabled': True, 'url': self.url, 'destination': str(self.destination),
                'state': self.state, 'result': self.result}

    def operation(self, kind, data):
        validate_request(kind, data)
        if not self._busy.acquire(blocking=False):
            raise BlockingIOError('A checkout operation is running.')
        try:
            with self.catalog.operation():
                if self.state != 'ready':
                    raise StaleRead('This checkout was already attempted; inspect its retained result.')
                if kind == 'preflight':
                    self.plan = None
                    if self._observe() != self.initial_identity:
                        raise StaleRead('Checkout destination changed since startup.')
                    self.plan = (secrets.token_urlsafe(24), time.monotonic() + PLAN_SECONDS)
                    return {'planToken': self.plan[0], 'url': self.url,
                            'destination': str(self.destination), 'expiresIn': PLAN_SECONDS,
                            'ignoreExternals': True, 'remoteAccess': False}
                plan, self.plan = self.plan, None
                if (plan is None or not secrets.compare_digest(data['planToken'], plan[0])
                        or time.monotonic() >= plan[1]):
                    raise StaleRead('Checkout confirmation expired.')
                if self._observe() != self.initial_identity:
                    raise StaleRead('Checkout destination changed after review.')
                # Discovering an executable is local-only and happens before
                # disarming. No subprocess, credentials or remote probe here.
                executable = self.backend.discover_svn(self.executable)
                if time.monotonic() >= plan[1] or self._observe() != self.initial_identity:
                    raise StaleRead('Checkout confirmation changed before dispatch.')
                self.state = 'uncertain'  # One attempt, including mkdir failures.
                return self._execute(executable)
        finally:
            self._busy.release()

    def _execute(self, executable):
        deadline = time.monotonic() + OPERATION_SECONDS
        path = self.destination
        with pinned_directory(path.parent, self.initial_identity[0]) as parent_anchor:
            if self.initial_identity[1] is None:
                (parent_anchor / path.name).mkdir(exist_ok=False)  # Never silently reuse a raced destination.
            self.backend.validate_checkout_destination(path)
            identity = _stable_identity(path)
            if identity[:-1] != self.initial_identity[0]:
                raise ValueError('Checkout parent changed before dispatch.')
            if self.initial_identity[1] is not None and identity != self.initial_identity[1]:
                raise ValueError('Checkout destination changed before dispatch.')

            def run(arguments):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Checkout verification deadline expired.')
                # Always wrap failures at HTTP boundary: the reused read runner's
                # text may say "No write was requested", which is false here.
                return self.history._bounded_process([executable, *arguments], cwd=pinned_cwd,
                    timeout=remaining, stdout_limit=OUTPUT_BYTES)

            with pinned_directory(path, identity) as pinned_cwd:
                raw = run(['checkout', '--non-interactive', '--no-auth-cache', '--ignore-externals',
                           '--quiet', '--', self.url, '.'])
                if raw:
                    raise ValueError('Quiet checkout returned unexpected diagnostics.')
                if _stable_identity(path) != identity:
                    raise ValueError('Checkout destination changed during execution.')
                raw = run(['info', '--non-interactive', '--no-auth-cache', '--xml', '--depth', 'empty', '--', '.'])
                if b'\0' in raw or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                    raise ValueError('Unsupported checkout verification XML.')
                root = ET.fromstring(raw)
                if root.tag != 'info' or len(root) != 1 or root[0].tag != 'entry':
                    raise ValueError('Ambiguous working-copy verification.')
                entry = root[0]
                roots, urls = entry.findall('wc-info/wcroot-abspath'), entry.findall('url')
                if (entry.get('kind') != 'dir' or not (entry.get('revision') or '').isdigit()
                        or len(roots) != 1 or len(urls) != 1 or len(entry.findall('wc-info')) != 1):
                    raise ValueError('Incomplete working-copy verification.')
                actual = roots[0].text or ''
                if os.path.altsep:
                    actual = actual.replace(os.path.altsep, os.path.sep)
                if actual != str(path) or (urls[0].text or '').rstrip('/') != self.url.rstrip('/'):
                    raise ValueError('Checkout root or source URL did not match the confirmed scope.')
                if _stable_identity(path) != identity:
                    raise ValueError('Checkout destination changed during verification.')
                _stable_identity(path / '.svn')
                wc_identity = working_copy_identity(path)
                if wc_identity[0] != identity:
                    raise ValueError('Checkout changed before candidate registration.')
                candidate = self.catalog._register_checkout(path, wc_identity)
                self.result = {'verified': True, 'url': self.url, 'destination': str(path),
                               'workspaceId': candidate.id, 'ignoreExternals': True}
        self.state = 'completed'
        return dict(self.result)
