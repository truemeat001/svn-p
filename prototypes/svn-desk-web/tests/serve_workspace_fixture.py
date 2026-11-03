"""TEST ONLY: real catalog/HTTP, synthetic status. Never invokes SVN."""
from pathlib import Path
import os
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import PrototypeServer
from workspace_bridge import WorkspaceCatalog, trusted_modules

backend = Path(os.environ.get('SVN_DESK_BACKEND_SOURCE', Path(__file__).resolve().parents[2] / 'svn-release-review-9bbc406'))
_, _, discovery, settings = trusted_modules(backend)

class SyntheticAdapter:
    def __init__(self, path):
        self.path = path

    def snapshot(self):
        time.sleep(0.25)
        if self.path.name == 'SYNTHETIC-broken':
            raise RuntimeError('TEST ONLY: deliberately unavailable; raw secret must not leak')
        return {'mode': 'live', 'workspace': self.path.name, 'root': str(self.path),
                'rows': [{'id': 'same.txt', 'path': 'folder/SYNTHETIC.txt', 'item': 'modified',
                          'props': 'none', 'revision': 1, 'kind': 'unknown', 'changelist': ''}],
                'scope': 'TEST ONLY: synthetic status, not a real SVN result.'}

with tempfile.TemporaryDirectory(prefix='svn-desk-SYNTHETIC-') as temp:
    home = Path(temp)
    parent = home / 'authorized'
    for name in ('SYNTHETIC-alpha', 'SYNTHETIC-beta', 'SYNTHETIC-broken'):
        (parent / name / '.svn').mkdir(parents=True)
    catalog = WorkspaceCatalog(parents=[parent], discovery=discovery, settings=settings,
        adapter_factory=SyntheticAdapter, settings_file=home / 'preferences.json')
    server = PrototypeServer(0, catalog=catalog)
    print('SYNTHETIC TEST ONLY ' + server.origin, flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
