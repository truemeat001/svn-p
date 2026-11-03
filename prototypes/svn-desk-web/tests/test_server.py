import http.client
import json
from pathlib import Path
import sys
import threading
import unittest
from dataclasses import dataclass
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import PrototypeServer, ReadOnlyAdapter

class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=PrototypeServer(0)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(2)
    def request(self,path='/',method='GET',headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        c.request(method,path,headers=headers or {})
        r=c.getresponse();body=r.read();result=(r.status,dict(r.getheaders()),body);c.close();return result
    def auth(self,**extra):
        return {'X-SVN-Desk-Token':self.server.token,**extra}
    def test_index_bootstraps_session_and_strict_csp(self):
        status,headers,body=self.request()
        self.assertEqual(status,200);self.assertIn(self.server.token.encode(),body)
        self.assertNotIn(b'__SESSION_TOKEN__',body)
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertNotIn('Access-Control-Allow-Origin',headers)
    def test_api_rejects_missing_token(self):self.assertEqual(self.request('/api/status')[0],403)
    def test_api_rejects_wrong_token(self):self.assertEqual(self.request('/api/status',headers={'X-SVN-Desk-Token':'wrong'})[0],403)
    def test_non_ascii_token_rejected_without_exception(self):self.assertEqual(self.request('/api/status',headers={'X-SVN-Desk-Token':'é'})[0],403)
    def test_cross_origin_rejected_even_with_token(self):self.assertEqual(self.request('/api/status',headers=self.auth(Origin='https://example.com'))[0],403)
    def test_cross_site_bootstrap_is_rejected(self):self.assertEqual(self.request(headers={'Sec-Fetch-Site':'cross-site'})[0],403)
    def test_dns_rebinding_host_rejected(self):self.assertEqual(self.request(headers={'Host':'evil.test'})[0],403)
    def test_demo_is_explicit_never_claims_live(self):
        status,_,body=self.request('/api/status',headers=self.auth());self.assertEqual(status,200);self.assertEqual(json.loads(body),{'mode':'demo'})
    def test_configuration_declares_no_writes(self):
        status,_,body=self.request('/api/config',headers=self.auth());data=json.loads(body)
        self.assertEqual(status,200);self.assertFalse(data['writesEnabled']);self.assertEqual(data['mode'],'demo')
    def test_all_mutating_methods_rejected(self):
        for method in ['POST','PUT','PATCH','DELETE']:
            with self.subTest(method=method):self.assertEqual(self.request('/api/status',method=method,headers=self.auth())[0],405)
    def test_unknown_api_cannot_call_arbitrary_svn(self):
        for endpoint in ['/api/commit','/api/exec','/api/shelf','/api/diff']:
            self.assertEqual(self.request(endpoint,headers=self.auth())[0],404)
    def test_paths_not_served_outside_asset_allowlist(self):
        for path in ['/server.py','/../server.py','/%2e%2e/server.py','/web/index.html','/tests/test_server.py']:
            self.assertEqual(self.request(path)[0],404)
    def test_query_cannot_change_workspace_or_command(self):self.assertEqual(self.request('/api/status?workspace=/tmp',headers=self.auth())[0],400)
    def test_live_failure_never_returns_demo_or_raw_diagnostics(self):
        adapter=Mock();adapter.snapshot.side_effect=RuntimeError('password=DO_NOT_LEAK');self.server.adapter=adapter
        try:
            status,_,body=self.request('/api/status',headers=self.auth())
            self.assertEqual(status,503);self.assertNotIn(b'DO_NOT_LEAK',body);self.assertNotIn(b'"mode": "demo"',body)
        finally:self.server.adapter=None
    def test_busy_read_is_not_retried(self):
        adapter=Mock();adapter.snapshot.side_effect=BlockingIOError();self.server.adapter=adapter
        try:
            self.assertEqual(self.request('/api/status',headers=self.auth())[0],409);adapter.snapshot.assert_called_once()
        finally:self.server.adapter=None
    def test_missing_working_copy_refused_before_import(self):
        with self.assertRaises((ValueError,FileNotFoundError)):ReadOnlyAdapter(Path('/tmp/no-svn-wc-prototype'),Path('/tmp/no-backend'))

@dataclass
class Entry:
    path: str
    item: str = "modified"
    props: str = "none"
    revision: int = 23
    changelist: str = "test"

class AdapterTests(unittest.TestCase):
    def adapter(self, entries):
        adapter=ReadOnlyAdapter.__new__(ReadOnlyAdapter)
        adapter.workspace=Path('/fixture/workspace')
        adapter.client=Mock();adapter.client.status.return_value=entries
        adapter._check_workspace=Mock()
        adapter._busy=threading.Lock()
        return adapter
    def test_snapshot_calls_only_local_status_and_preserves_native_identity(self):
        adapter=self.adapter([Entry('Assets/Player/a.cs')])
        result=adapter.snapshot()
        adapter.client.status.assert_called_once_with(verbose=False)
        self.assertEqual(result['mode'],'live')
        self.assertEqual(result['rows'][0]['kind'],'unknown')
        self.assertEqual(result['rows'][0]['id'],'Assets/Player/a.cs')
    def test_windows_display_paths_normalized_without_changing_ids(self):
        native=r'Assets\Player\a.cs'
        adapter=self.adapter([Entry(native)])
        with patch('server.os.sep','\\'):
            row=adapter.snapshot()['rows'][0]
        self.assertEqual(row['path'],'Assets/Player/a.cs')
        self.assertEqual(row['id'],native)
    def test_oversized_snapshot_rejected_and_lock_released(self):
        adapter=self.adapter([Entry('a'),Entry('b')])
        with patch('server.MAX_ROWS',1),self.assertRaises(ValueError):adapter.snapshot()
        self.assertFalse(adapter._busy.locked())
    def test_second_status_call_refused_not_queued(self):
        adapter=self.adapter([]);adapter._busy.acquire()
        try:
            with self.assertRaises(BlockingIOError):adapter.snapshot()
            adapter.client.status.assert_not_called()
        finally:adapter._busy.release()

if __name__=='__main__':unittest.main()
