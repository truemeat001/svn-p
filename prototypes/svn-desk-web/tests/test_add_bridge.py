"""Add endpoint transport and catalog identity, with synthetic adapters only."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from server import PrototypeServer, ReadOnlyAdapter
from add_ops import AddUnavailable
from file_reads import StaleRead
from workspace_bridge import WorkspaceCatalog, Candidate, working_copy_identity


def scope(**extra):
    return dict(workspaceId='w',selectionVersion=1,snapshotToken='s',**extra)

def preview():
    return scope(ids=['a.txt'])


class AddHTTPTests(unittest.TestCase):
    def setUp(self):
        self.catalog=Mock(); self.catalog.add_operation.return_value={'fixture':True}
        self.server=PrototypeServer(0,catalog=self.catalog)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def request(self,route='/api/add-preflight',data=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            connection.request('POST',route,body=json.dumps(preview() if data is None else data),
                headers=headers or {'Content-Type':'application/json','X-SVN-Desk-Token':self.server.token})
            result=connection.getresponse(); return result.status,result.read()
        finally: connection.close()

    def test_exact_schema_and_separate_confirmation(self):
        self.assertEqual(self.request()[0],200)
        self.catalog.add_operation.assert_called_once_with('preflight',preview())
        self.catalog.add_operation.reset_mock()
        for confirmed in [False,1,'true',None]:
            self.assertEqual(self.request('/api/add-apply',scope(planToken='p',confirmed=confirmed))[0],400)
        for field in ['force','ids','action','comment','url','token']:
            self.assertEqual(self.request('/api/add-apply',scope(planToken='p',confirmed=True,**{field:'x'}))[0],400)
        self.catalog.add_operation.assert_not_called()
        self.assertEqual(self.request('/api/add-apply',scope(planToken='p',confirmed=True))[0],200)
        self.catalog.add_operation.assert_called_once_with('apply',scope(planToken='p',confirmed=True))

    def test_missing_auth_and_foreign_origin(self):
        for headers in [{'Content-Type':'application/json'}, {'Content-Type':'application/json',
                'X-SVN-Desk-Token':self.server.token,'Origin':'https://outside.test'}]:
            self.assertEqual(self.request(headers=headers)[0],403)
        self.catalog.add_operation.assert_not_called()

    def test_rejections_and_uncertainty_sanitized(self):
        for error,status in [(AddUnavailable('SECRET'),403),(StaleRead('SECRET'),409),
                             (BlockingIOError('SECRET'),409),(RuntimeError('SECRET'),503)]:
            self.catalog.add_operation.side_effect=error
            code,body=self.request('/api/add-apply',scope(planToken='p',confirmed=True))
            self.assertEqual(code,status); self.assertNotIn(b'SECRET',body)
            if status==503: self.assertIn('没有自动重试或回滚',body.decode())

    def test_demo_no_add_api(self):
        self.server.catalog=None
        self.assertEqual(self.request()[0],405)
        self.assertEqual(self.request('/api/add-apply',scope(planToken='p',confirmed=True))[0],405)
        self.catalog.add_operation.assert_not_called()

    def test_host_bounds_and_no_arbitrary_inputs(self):
        headers = {'Content-Type':'application/json','X-SVN-Desk-Token':self.server.token,
                   'Host':'outside.test'}
        self.assertEqual(self.request(headers=headers)[0],403)
        for change in [{'ids':[]},{'ids':['a.txt']*2},{'ids':['../secret']},
                       {'ids':[str(i)+'.txt' for i in range(101)]},{'path':'a.txt'},
                       {'command':'add'},{'recursive':True},{'autoProps':True},
                       {'selectionVersion':True},{'snapshotToken':''}]:
            self.assertEqual(self.request(data={**preview(),**change})[0],400)
        self.catalog.add_operation.assert_not_called()

    def test_duplicate_json_fields_and_transfer_encoding_rejected(self):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            connection.request('POST','/api/add-preflight',
                body='{"workspaceId":"w","workspaceId":"w","selectionVersion":1,"snapshotToken":"s","ids":["a.txt"]}',
                headers={'Content-Type':'application/json','X-SVN-Desk-Token':self.server.token})
            response=connection.getresponse()
            self.assertEqual(response.status,400); response.read()
        finally: connection.close()
        headers={'Content-Type':'application/json','X-SVN-Desk-Token':self.server.token,
                 'Transfer-Encoding':'chunked'}
        self.assertEqual(self.request(headers=headers)[0],400)
        self.catalog.add_operation.assert_not_called()


class AddCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.wc=Path(self.tmp.name)/'synthetic'; (self.wc/'.svn').mkdir(parents=True)
        self.candidate=Candidate('w',self.wc,working_copy_identity(self.wc),'explicit')
        self.adapter=Mock(); self.adapter.add_operation.return_value={'fixture':True}
        self.catalog=WorkspaceCatalog.__new__(WorkspaceCatalog)
        self.catalog._selection=(self.candidate,self.adapter,1)
        self.catalog._busy=threading.Lock(); self.catalog.parent_identities={}
        self.catalog.settings_file=None; self.catalog.settings_warning=None

    def test_scope_and_replaced_working_copy_rejected(self):
        for change in [{'workspaceId':'other'},{'selectionVersion':2}]:
            with self.assertRaises(StaleRead): self.catalog.add_operation('preflight',{**preview(),**change})
        (self.wc/'.svn').rename(self.wc/'old'); (self.wc/'.svn').mkdir()
        with self.assertRaises(ValueError): self.catalog.add_operation('preflight',preview())
        self.adapter.add_operation.assert_not_called()

    def test_revoked_capabilities_win_over_configuration(self):
        self.adapter.add_operation.return_value={'statusRefreshed':False,'addsEnabled':False,'commitsEnabled':False}
        result=self.catalog.add_operation('apply',scope(planToken='p',confirmed=True))
        self.assertFalse(result['addsEnabled']); self.assertFalse(result['commitsEnabled'])

    def test_workspace_replaced_after_verified_result_preserves_fact(self):
        result={'action':'add','verified':True,'outcome':'verified','files':['a.txt'],'autoProps':False,'warnings':[]}
        def change(kind,data):
            (self.wc/'.svn').rename(self.wc/'old'); (self.wc/'.svn').mkdir()
            return {'addResult':result}
        self.adapter.add_operation.side_effect=change
        self.adapter._add_status_unknown.return_value={'addResult':result,'statusRefreshed':False,'addsEnabled':False}
        payload=self.catalog.add_operation('apply',scope(planToken='p',confirmed=True))
        self.assertTrue(payload['addResult']['verified']); self.assertFalse(payload['addsEnabled'])
        self.adapter._add_status_unknown.assert_called_once_with(result,'workspace-changed')



    def test_shared_catalog_lock_blocks_selection_races(self):
        self.catalog._busy.acquire()
        try:
            with self.assertRaises(BlockingIOError):
                self.catalog.add_operation('preflight',preview())
        finally:
            self.catalog._busy.release()
        self.adapter.add_operation.assert_not_called()

    def test_add_capability_participates_in_config_independently(self):
        for name in ('_changelist_service','_shelf_service','_commit_service','_lock_service'):
            setattr(self.adapter,name,None)
        self.adapter._add_service = None
        self.assertFalse(self.catalog.config()['addsEnabled'])
        self.assertFalse(self.catalog.config()['writesEnabled'])
        self.adapter._add_service = Mock()
        self.assertTrue(self.catalog.config()['addsEnabled'])
        self.assertTrue(self.catalog.config()['writesEnabled'])
        self.assertFalse(self.catalog.config()['commitsEnabled'])


class AddAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ReadOnlyAdapter.__new__(ReadOnlyAdapter)
        self.adapter.workspace = Path('/SYNTHETIC/wc')
        self.adapter.client = Mock()
        self.adapter.client.status.return_value = []
        self.adapter._check_workspace = Mock()
        self.adapter._busy = threading.Lock()
        self.adapter._read_snapshot = ('s', {})
        for name in ('_changelist_service', '_shelf_service', '_commit_service', '_lock_service'):
            setattr(self.adapter, name, None)
        self.adapter._add_service = Mock()
        self.result = {'action':'add','files':['a.txt'],'outcome':'verified','verified':True,'autoProps':False}
        self.adapter._add_service.apply.return_value = (self.result, {})

    def test_constructor_disabled_by_default_and_reviewed_gate(self):
        backend = SimpleNamespace(STATUS_DIAGNOSTICS_POLICY=1, SvnClient=Mock())
        stream = SimpleNamespace(STATUS_DIAGNOSTICS_POLICY=1)
        with patch('server.local_path', return_value=Path('/synthetic')), patch('server.working_copy_identity', return_value=()), \
             patch('server.trusted_modules', return_value=(backend,stream,None,None)), \
             patch('server.history_module', return_value=object()), patch('server.FileReadService'), \
             patch('server.reviewed_commit_backend', return_value=False) as reviewed, patch('server.AddService') as service:
            adapter = ReadOnlyAdapter(Path('/synthetic'),Path('/backend'))
            self.assertIsNone(adapter._add_service)
            adapter = ReadOnlyAdapter(Path('/synthetic'),Path('/backend'),enable_adds=True)
            self.assertIsNone(adapter._add_service)
            service.assert_not_called()
            reviewed.return_value = True
            adapter = ReadOnlyAdapter(Path('/synthetic'),Path('/backend'),enable_adds=True)
            self.assertIs(adapter._add_service, service.return_value)
            self.assertIsNone(adapter._commit_service)
            self.assertIsNone(adapter._changelist_service)
            self.assertIsNone(adapter._lock_service)
            self.assertIsNone(adapter._shelf_service)

    def test_snapshot_invalidates_add_even_on_read_failure(self):
        self.adapter.client.status.side_effect = ValueError('fixture')
        with self.assertRaises(ValueError): self.adapter.snapshot()
        self.adapter._add_service.invalidate.assert_called_once()
        self.assertIsNone(self.adapter._read_snapshot)

    def test_add_only_uses_strict_shared_status(self):
        payload = self.adapter.snapshot()
        self.adapter.client.status.assert_called_once_with(verbose=True)
        self.assertTrue(payload['addsEnabled'])
        self.assertFalse(payload['commitsEnabled'])

    def test_disabled_and_busy_never_queue(self):
        service = self.adapter._add_service
        self.adapter._add_service = None
        with self.assertRaises(AddUnavailable): self.adapter.add_operation('preflight',preview())
        self.adapter._add_service = service
        self.adapter._busy.acquire()
        with self.assertRaises(BlockingIOError): self.adapter.add_operation('preflight',preview())
        service.preflight.assert_not_called()
        self.adapter._busy.release()

    def test_preflight_postcheck_invalidates(self):
        self.adapter._check_workspace.side_effect = [None, ValueError('changed')]
        with self.assertRaises(ValueError): self.adapter.add_operation('preflight',preview())
        self.adapter._add_service.invalidate.assert_called_once()
        self.assertFalse(self.adapter._busy.locked())

    def test_add_invalidates_all_other_plans_before_execution(self):
        services = [self.adapter._add_service]
        for name in ('_changelist_service','_shelf_service','_commit_service','_lock_service'):
            setattr(self.adapter,name,Mock())
            services.append(getattr(self.adapter,name))
        def apply(data,snapshot,invalidate):
            self.assertEqual(snapshot,('s',{}))
            invalidate()
            self.assertIsNone(self.adapter._read_snapshot)
            for service in services: service.invalidate.assert_called_once()
            return self.result, {}
        self.adapter._add_service.apply.side_effect = apply
        payload = self.adapter.add_operation('apply',scope(planToken='p',confirmed=True))
        self.assertTrue(payload['statusRefreshed'])
        self.assertEqual(payload['addResult'],{**self.result,'warnings':[]})

    def test_other_mutations_invalidate_add(self):
        for kind in ['changelist','shelf','commit','lock']:
            with self.subTest(kind=kind):
                self.adapter._read_snapshot = ('s',{})
                self.adapter._add_service.invalidate.reset_mock()
                service = Mock()
                setattr(self.adapter,'_'+kind+'_service',service)
                def apply(data,snapshot,invalidate):
                    invalidate()
                    self.adapter._add_service.invalidate.assert_called_once()
                    return (self.result,{}) if kind == 'changelist' else self.result
                service.apply.side_effect = apply
                getattr(self.adapter,kind+'_operation')('apply',scope(planToken='p',confirmed=True))
                setattr(self.adapter,'_'+kind+'_service',None)

    def test_verified_add_survives_post_write_validation_failure(self):
        self.adapter._check_workspace.side_effect = [None,ValueError('changed')]
        payload = self.adapter.add_operation('apply',scope(planToken='p',confirmed=True))
        self.assertFalse(payload['statusRefreshed'])
        self.assertTrue(payload['addResult']['verified'])
        self.assertEqual(payload['addResult']['warnings'],['status-refresh-failed'])
        for field in ('addsEnabled','commitsEnabled','locksEnabled','shelfWritesEnabled','changelistWritesEnabled','writesEnabled','fileReadsEnabled'):
            self.assertFalse(payload[field])
        self.assertEqual(payload['rows'],[])
        self.assertIsNone(payload['snapshotToken'])
        self.assertIsNone(self.adapter._read_snapshot)

    def test_verified_add_survives_publication_failure(self):
        with patch.object(self.adapter,'_publish_snapshot',side_effect=ValueError('failure')):
            payload = self.adapter.add_operation('apply',scope(planToken='p',confirmed=True))
        self.assertFalse(payload['statusRefreshed'])
        self.assertEqual(payload['addResult']['outcome'],'verified')


if __name__ == '__main__': unittest.main()
