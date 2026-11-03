"""TEST ONLY: real bridge/HistoryManager, synthetic SVN transport. Never runs SVN."""
from pathlib import Path
import os
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.parse import quote
import xml.etree.ElementTree as ET
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import PrototypeServer, ReadOnlyAdapter
from workspace_bridge import WorkspaceCatalog, trusted_modules

source = Path(os.environ.get('SVN_DESK_BACKEND_SOURCE', Path(__file__).resolve().parents[2] / 'svn-release-review-9bbc406'))
backend, _, discovery, settings = trusted_modules(source)


class SyntheticReadAdapter(ReadOnlyAdapter):
    def __init__(self, path):
        with patch.object(backend, 'discover_svn', return_value='/SYNTHETIC-NOT-EXECUTED/svn'):
            super().__init__(path, source)
        self.client.status = lambda **kwargs: [backend.StatusEntry(name, 'modified', revision=100, changelist='SYNTHETIC') for name in ('one.txt', 'two.txt', 'slow.txt', 'fail.txt')]
        self._file_service.manager._run = self.synthetic

    def synthetic(self, args, limit):
        if args[0] == 'info':
            target = args[-1]; name = target[:-1] if '@' in target else target
            root = ET.Element('info'); entry = ET.SubElement(root, 'entry', kind='dir' if target == '.' else 'file')
            ET.SubElement(entry, 'url').text = 'https://synthetic.invalid/repo' + ('' if target == '.' else '/' + quote(name))
            repo = ET.SubElement(entry, 'repository'); ET.SubElement(repo, 'uuid').text = 'SYNTHETIC'
            wc = ET.SubElement(entry, 'wc-info'); ET.SubElement(wc, 'wcroot-abspath').text = str(self.workspace)
            return ET.tostring(root)
        if 'slow.txt' in ' '.join(args): time.sleep(3)
        if 'fail.txt' in ' '.join(args): raise RuntimeError('SYNTHETIC secret diagnostic must not leak')
        if args[0] == 'diff':
            return f'Index: SYNTHETIC {self.workspace.name}\n--- old\n+++ working\n@@ -1 +1 @@\n-old content\n+<script>escaped synthetic content</script>\n'.encode()
        if args[0] == 'log':
            start = args[args.index('--revision') + 1].split(':')[0]
            maximum = 100 if start in {'HEAD', 'BASE'} else int(start)
            root = ET.Element('log')
            for number in range(maximum, max(maximum - 51, 0), -1):
                entry = ET.SubElement(root, 'logentry', revision=str(number))
                ET.SubElement(entry, 'author').text = 'SYNTHETIC reviewer'
                ET.SubElement(entry, 'date').text = '2026-11-03T00:00:00Z'
                ET.SubElement(entry, 'msg').text = f'SYNTHETIC revision {number} <b>escaped</b>'
                paths = ET.SubElement(entry, 'paths'); ET.SubElement(paths, 'path', action='M', kind='file').text = '/other-synthetic-file.txt'
            return ET.tostring(root)
        raise AssertionError('No other commands supported by fixture.')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='svn-readonly-SYNTHETIC-') as temp:
        home = Path(temp)
        for name in ('SYNTHETIC-alpha', 'SYNTHETIC-beta'):
            root = home / name; (root / '.svn').mkdir(parents=True)
            for filename in ('one.txt', 'two.txt', 'slow.txt', 'fail.txt'): (root / filename).write_text('SYNTHETIC only')
        catalog = WorkspaceCatalog(roots=[home / 'SYNTHETIC-alpha', home / 'SYNTHETIC-beta'], discovery=discovery,
            settings=settings, adapter_factory=SyntheticReadAdapter)
        server = PrototypeServer(0, catalog=catalog)
        print('SYNTHETIC READ TEST ONLY ' + server.origin, flush=True)
        try: server.serve_forever()
        finally: server.server_close()
