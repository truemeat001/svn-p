"""Lock endpoint transport and catalog identity, with synthetic adapters only."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from server import PrototypeServer, ReadOnlyAdapter
from lock_ops import LockUnavailable
from file_reads import StaleRead
from workspace_bridge import WorkspaceCatalog, Candidate, working_copy_identity


def scope(**extra):
    return dict(workspaceId='w',selectionVersion=1,snapshotToken='s',**extra)

def preview():
    return scope(action='lock',ids=['a.txt'],comment='reviewed')


class LockHTTPTests(unittest.TestCase):
    def setUp(self):
        self.catalog=Mock(); self.catalog.lock_operation.return_value={'fixture':True}
        self.server=PrototypeServer(0,catalog=self.catalog)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def request(self,route='/api/lock-preflight',data=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            connection.request('POST',route,body=json.dumps(preview() if data is None else data),
                headers=headers or {'Content-Type':'application/json','X-SVN-Desk-Token':self.server.token})
            result=connection.getresponse(); return result.status,result.read()
        finally: connection.close()

    def test_exact_schema_and_separate_confirmation(self):
        self.assertEqual(self.request()[0],200)
        self.catalog.lock_operation.assert_called_once_with('preflight',preview())
        self.catalog.lock_operation.reset_mock()
        for confirmed in [False,1,'true',None]:
            self.assertEqual(self.request('/api/lock-apply',scope(planToken='p',confirmed=confirmed))[0],400)
        for field in ['force','ids','action','comment','url','token']:
            self.assertEqual(self.request('/api/lock-apply',scope(planToken='p',confirmed=True,**{field:'x'}))[0],400)
        self.catalog.lock_operation.assert_not_called()
        self.assertEqual(self.request('/api/lock-apply',scope(planToken='p',confirmed=True))[0],200)
        self.catalog.lock_operation.assert_called_once_with('apply',scope(planToken='p',confirmed=True))

    def test_missing_auth_and_foreign_origin(self):
        for headers in [{'Content-Type':'application/json'}, {'Content-Type':'application/json',
                'X-SVN-Desk-Token':self.server.token,'Origin':'https://outside.test'}]:
            self.assertEqual(self.request(headers=headers)[0],403)
        self.catalog.lock_operation.assert_not_called()

    def test_rejections_and_uncertainty_sanitized(self):
        for error,status in [(LockUnavailable('SECRET'),403),(StaleRead('SECRET'),409),
                             (BlockingIOError('SECRET'),409),(RuntimeError('SECRET'),503)]:
            self.catalog.lock_operation.side_effect=error
            code,body=self.request('/api/lock-apply',scope(planToken='p',confirmed=True))
            self.assertEqual(code,status); self.assertNotIn(b'SECRET',body)
            if status==503: self.assertIn('没有自动重试或回滚',body.decode())

    def test_demo_no_lock_api(self):
        self.server.catalog=None
        self.assertEqual(self.request()[0],405)
        self.assertEqual(self.request('/api/lock-apply',scope(planToken='p',confirmed=True))[0],405)
        self.catalog.lock_operation.assert_not_called()


class LockCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.wc=Path(self.tmp.name)/'synthetic'; (self.wc/'.svn').mkdir(parents=True)
        self.candidate=Candidate('w',self.wc,working_copy_identity(self.wc),'explicit')
        self.adapter=Mock(); self.adapter.lock_operation.return_value={'fixture':True}
        self.catalog=WorkspaceCatalog.__new__(WorkspaceCatalog)
        self.catalog._selection=(self.candidate,self.adapter,1)
        self.catalog._busy=threading.Lock(); self.catalog.parent_identities={}
        self.catalog.settings_file=None; self.catalog.settings_warning=None

    def test_scope_and_replaced_working_copy_rejected(self):
        for change in [{'workspaceId':'other'},{'selectionVersion':2}]:
            with self.assertRaises(StaleRead): self.catalog.lock_operation('preflight',{**preview(),**change})
        (self.wc/'.svn').rename(self.wc/'old'); (self.wc/'.svn').mkdir()
        with self.assertRaises(ValueError): self.catalog.lock_operation('preflight',preview())
        self.adapter.lock_operation.assert_not_called()

    def test_revoked_capabilities_win_over_configuration(self):
        self.adapter.lock_operation.return_value={'statusRefreshed':False,'locksEnabled':False,'commitsEnabled':False}
        result=self.catalog.lock_operation('apply',scope(planToken='p',confirmed=True))
        self.assertFalse(result['locksEnabled']); self.assertFalse(result['commitsEnabled'])

    def test_workspace_replaced_after_verified_result_preserves_fact(self):
        result={'action':'lock','verified':True,'outcome':'verified','files':['a.txt'],'warnings':[]}
        def change(kind,data):
            (self.wc/'.svn').rename(self.wc/'old'); (self.wc/'.svn').mkdir()
            return {'lockResult':result}
        self.adapter.lock_operation.side_effect=change
        self.adapter._lock_status_unknown.return_value={'lockResult':result,'statusRefreshed':False,'locksEnabled':False}
        payload=self.catalog.lock_operation('apply',scope(planToken='p',confirmed=True))
        self.assertTrue(payload['lockResult']['verified']); self.assertFalse(payload['locksEnabled'])
        self.adapter._lock_status_unknown.assert_called_once_with(result,'workspace-changed')


if __name__=='__main__': unittest.main()
