"""Commit transport/capability integration; synthetic adapters and temporary WCs only."""
from dataclasses import dataclass
import http.client
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import PrototypeServer, ReadOnlyAdapter, reviewed_commit_backend
from commit_ops import CommitUnavailable
from changelist_ops import MutationUncertain
from file_reads import StaleRead
from workspace_bridge import WorkspaceCatalog, Candidate, working_copy_identity


def scope(**extra):
    return dict(workspaceId='w', selectionVersion=1, snapshotToken='s', **extra)


def preview(**extra):
    return scope(operation='selected', ids=['a.txt'], name=None,
                 message='Explicit reviewed message', keepChangelist=False, **extra)


class CommitHTTPTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Mock()
        self.catalog.commit_operation.return_value = {'fixture': True}
        self.server = PrototypeServer(0, catalog=self.catalog)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def request(self, route='/api/commit-preflight', data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            payload = json.dumps(preview() if data is None else data)
            connection.request('POST', route, body=payload,
                headers=headers or {'Content-Type':'application/json', 'X-SVN-Desk-Token':self.server.token})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_preflight_exact_schema_dispatches_once(self):
        self.assertEqual(self.request()[0], 200)
        self.catalog.commit_operation.assert_called_once_with('preflight', preview())

    def test_apply_requires_literal_confirmation(self):
        for confirmed in [False, 'true', 1, None]:
            with self.subTest(confirmed=confirmed):
                self.assertEqual(self.request('/api/commit-apply', scope(planToken='p',confirmed=confirmed))[0], 400)
        self.catalog.commit_operation.assert_not_called()
        self.assertEqual(self.request('/api/commit-apply', scope(planToken='p',confirmed=True))[0], 200)
        self.catalog.commit_operation.assert_called_once_with('apply', scope(planToken='p',confirmed=True))

    def test_apply_cannot_change_message_or_files(self):
        for key, value in [('message','other'),('ids',['b.txt']),('keepChangelist',True),('operation','selected')]:
            self.assertEqual(self.request('/api/commit-apply', scope(planToken='p',confirmed=True,**{key:value}))[0],400)
        self.catalog.commit_operation.assert_not_called()

    def test_missing_auth_and_cross_origin_never_dispatch(self):
        for headers in [{'Content-Type':'application/json'},
                        {'Content-Type':'application/json','X-SVN-Desk-Token':self.server.token,'Origin':'https://outside.test'}]:
            self.assertEqual(self.request(headers=headers)[0], 403)
        self.catalog.commit_operation.assert_not_called()

    def test_unavailable_stale_busy_and_errors_are_sanitized(self):
        for error, status in [(CommitUnavailable('SECRET'),403), (StaleRead('SECRET'),409),
                              (BlockingIOError('SECRET'),409), (RuntimeError('SECRET'),503)]:
            self.catalog.commit_operation.side_effect = error
            code, body = self.request()
            self.assertEqual(code,status); self.assertNotIn(b'SECRET',body)

    def test_uncertain_apply_warns_no_automatic_retry(self):
        self.catalog.commit_operation.side_effect = MutationUncertain('SECRET message and stderr')
        code,body = self.request('/api/commit-apply',scope(planToken='p',confirmed=True))
        self.assertEqual(code,503); self.assertNotIn(b'SECRET',body)
        self.assertIn('仓库可能已经接受提交',body.decode())
        self.catalog.commit_operation.assert_called_once()

    def test_message_and_selection_limits_fail_before_dispatch(self):
        for change in [{'message':''},{'message':'中'*2731},{'ids':[]},{'ids':['a']*2},
                       {'ids':['../secret']},{'recursive':True},{'url':'https://outside.test'}]:
            self.assertEqual(self.request(data={**preview(),**change})[0],400)
        self.catalog.commit_operation.assert_not_called()

    def test_demo_catalog_absence_rejects_both_endpoints(self):
        self.server.catalog = None
        self.assertEqual(self.request()[0],405)
        self.assertEqual(self.request('/api/commit-apply',scope(planToken='p',confirmed=True))[0],405)
        self.catalog.commit_operation.assert_not_called()


@dataclass
class Row:
    path: str
    item: str = 'normal'
    props: str = 'none'
    changelist: str | None = 'Feature'


class CommitAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ReadOnlyAdapter.__new__(ReadOnlyAdapter)
        self.adapter.workspace = Path('/SYNTHETIC/wc')
        self.adapter.client = Mock()
        self.adapter.client.status.return_value = [Row('clean.txt'),Row('edited.txt','modified'),Row('irrelevant.txt',changelist=None)]
        self.adapter._check_workspace = Mock()
        self.adapter._busy = threading.Lock()
        self.adapter._commit_service = Mock()
        self.adapter._changelist_service = None
        self.adapter._shelf_service = None
        self.adapter._read_snapshot = ('s',{})

    def test_unknown_paired_sources_disable_commit_gate(self):
        self.assertFalse(reviewed_commit_backend(None, None))
        with patch('server.Path') as path:
            path.return_value.stat.return_value.st_size = 1024
            path.return_value.read_bytes.return_value = b'unknown backend'
            self.assertFalse(reviewed_commit_backend(SimpleNamespace(__file__='fixture'),SimpleNamespace(__file__='fixture')))

    def test_reviewed_source_gate_normalizes_crlf_only(self):
        import hashlib
        expected = hashlib.sha256(b'a\nb\n').hexdigest()
        with patch('server.Path') as path, patch('server.COMMIT_BACKEND_SHA256', expected), patch('server.COMMIT_HISTORY_SHA256', expected):
            path.return_value.stat.return_value.st_size = 6
            path.return_value.read_bytes.return_value = b'a\r\nb\r\n'
            self.assertTrue(reviewed_commit_backend(SimpleNamespace(__file__='fixture'),SimpleNamespace(__file__='fixture')))
            path.return_value.read_bytes.return_value = b'a\r\nc\r\n'
            self.assertFalse(reviewed_commit_backend(SimpleNamespace(__file__='fixture'),SimpleNamespace(__file__='fixture')))

    def test_commit_only_status_includes_clean_named_members(self):
        result = self.adapter.snapshot()
        self.assertEqual([row['id'] for row in result['rows']],['clean.txt','edited.txt'])
        self.adapter.client.status.assert_called_once_with(verbose=True)
        self.assertTrue(result['commitsEnabled'])
        self.assertFalse(result['changelistWritesEnabled'])
        self.adapter._commit_service.invalidate.assert_called_once()

    def test_disabled_commit_fails_before_preflight(self):
        self.adapter._commit_service = None
        with self.assertRaises(CommitUnavailable): self.adapter.commit_operation('preflight',preview())
        self.adapter.client.status.assert_not_called()
        self.assertFalse(self.adapter._busy.locked())

    def test_busy_commit_is_not_queued(self):
        self.adapter._busy.acquire()
        with self.assertRaises(BlockingIOError): self.adapter.commit_operation('preflight',preview())
        self.adapter._commit_service.preflight.assert_not_called()

    def test_preflight_post_workspace_failure_invalidates_plan(self):
        self.adapter._check_workspace.side_effect = [None,ValueError('changed')]
        with self.assertRaises(ValueError): self.adapter.commit_operation('preflight',preview())
        self.adapter._commit_service.invalidate.assert_called_once()
        self.assertFalse(self.adapter._busy.locked())

    def test_apply_invalidates_all_services_before_mutation(self):
        self.adapter._changelist_service = Mock(); self.adapter._shelf_service = Mock()
        def apply(data,snapshot,invalidate):
            self.assertEqual(snapshot,('s',{})); invalidate()
            self.assertIsNone(self.adapter._read_snapshot)
            for service in [self.adapter._commit_service,self.adapter._changelist_service,self.adapter._shelf_service]:
                service.invalidate.assert_called_once()
            return {'commandCompleted':True}
        self.adapter._commit_service.apply.side_effect = apply
        result = self.adapter.commit_operation('apply',scope(planToken='p',confirmed=True))
        self.assertEqual(result['commitResult'],{'commandCompleted':True})
        self.assertNotEqual(result['snapshotToken'],'s')

    def test_failed_post_commit_status_clears_snapshot(self):
        self.adapter.client.status.side_effect = RuntimeError('failed')
        self.adapter._commit_service.apply.return_value = {'commandCompleted':True,'outcome':'committed','reportedRevision':'3','warnings':[]}
        result = self.adapter.commit_operation('apply',scope(planToken='p',confirmed=True))
        self.assertFalse(result['statusRefreshed'])
        self.assertIsNone(result['snapshotToken'])
        self.assertEqual(result['rows'],[])
        self.assertEqual(result['commitResult']['reportedRevision'],'3')
        self.assertIn('status-refresh-failed',result['commitResult']['warnings'])
        self.assertIsNone(self.adapter._read_snapshot)
        self.adapter._commit_service.apply.assert_called_once()
        self.assertFalse(self.adapter._busy.locked())


class CommitCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.wc = Path(self.temporary.name)/'SYNTHETIC'; (self.wc/'.svn').mkdir(parents=True)
        self.candidate = Candidate('w',self.wc,working_copy_identity(self.wc),'explicit')
        self.adapter = Mock(); self.adapter.commit_operation.return_value = {'result':'fixture'}
        self.catalog = WorkspaceCatalog.__new__(WorkspaceCatalog)
        self.catalog._selection = (self.candidate,self.adapter,1)
        self.catalog._busy = threading.Lock(); self.catalog.parent_identities = {}
        self.catalog.settings_file = None; self.catalog.settings_warning = None

    def test_scope_mismatch_stops_before_adapter(self):
        for change in [{'workspaceId':'other'},{'selectionVersion':2}]:
            with self.assertRaises(StaleRead): self.catalog.commit_operation('preflight',{**preview(),**change})
        self.adapter.commit_operation.assert_not_called()

    def test_replaced_working_copy_rejected(self):
        (self.wc/'.svn').rename(self.wc/'old-metadata'); (self.wc/'.svn').mkdir()
        with self.assertRaises(ValueError): self.catalog.commit_operation('preflight',preview())
        self.adapter.commit_operation.assert_not_called()

    def test_valid_scope_preserves_identity(self):
        result = self.catalog.commit_operation('preflight',preview())
        self.assertEqual(result['workspaceId'],'w'); self.assertEqual(result['selectionVersion'],1)
        self.adapter.commit_operation.assert_called_once_with('preflight',preview())

    def test_post_apply_workspace_failure_is_uncertain(self):
        def replace(kind,data):
            (self.wc/'.svn').rename(self.wc/'old-metadata'); (self.wc/'.svn').mkdir()
            return {'result':'fixture'}
        self.adapter.commit_operation.side_effect = replace
        with self.assertRaises(MutationUncertain): self.catalog.commit_operation('apply',scope(planToken='p',confirmed=True))
        self.assertIsNone(self.adapter._read_snapshot)
        self.adapter._commit_service.invalidate.assert_called_once()
        self.assertFalse(self.catalog._busy.locked())


class CommitHTTPServiceTests(unittest.TestCase):
    """Actual HTTP -> catalog -> adapter -> service -> reviewed transport semantics."""
    def setUp(self):
        import test_commits as fixture
        from workspace_bridge import trusted_modules
        self.fixture = fixture.CommitTests(methodName='test_preflight_local_and_message_private')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.fixture.adapter._commit_service = self.fixture.service
        self.fixture.service.check_workspace = self.fixture.adapter._check_workspace
        _, _, discovery, settings = trusted_modules(fixture.BACKEND)
        self.catalog = WorkspaceCatalog(roots=[self.fixture.root],discovery=discovery,settings=settings,
            adapter_factory=lambda path:self.fixture.adapter)
        snapshot = self.catalog.snapshot()
        self.data = {key:snapshot[key] for key in ('workspaceId','selectionVersion','snapshotToken')}
        self.data.update(operation='selected',ids=[fixture.native('src/a.txt')],name=None,
                         message='HTTP reviewed message',keepChangelist=False)
        self.server = PrototypeServer(0,catalog=self.catalog)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def post(self,route,data):
        connection = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            connection.request('POST',route,json.dumps(data),headers={'Content-Type':'application/json',
                'X-SVN-Desk-Token':self.server.token})
            response = connection.getresponse(); return response.status,json.loads(response.read())
        finally: connection.close()

    def confirm(self,plan):
        return {**{key:self.data[key] for key in ('workspaceId','selectionVersion','snapshotToken')},
                'planToken':plan['planToken'],'confirmed':True}

    def test_full_roundtrip_then_replay_refused(self):
        status,plan = self.post('/api/commit-preflight',self.data)
        self.assertEqual(status,200,plan); self.fixture.client.command.assert_not_called()
        status,result = self.post('/api/commit-apply',self.confirm(plan))
        self.assertEqual(status,200,result); self.fixture.client.command.assert_called_once()
        self.assertEqual(result['commitResult']['files'],['src/a.txt'])
        self.assertNotEqual(result['snapshotToken'],self.data['snapshotToken'])
        self.assertEqual(self.post('/api/commit-apply',self.confirm(plan))[0],409)
        self.fixture.client.command.assert_called_once()
        self.assertNotIn('other.txt',self.fixture.client.command.call_args.args[0])

    def test_http_hook_warning_retains_reported_commit_and_one_use(self):
        self.fixture.client.command.return_value = self.fixture.backend.CommandResult((),0,
            'Committed revision 3.\nWarning: post-commit hook failed (exit code 1) with output:\nPRIVATE HOOK','')
        status,plan = self.post('/api/commit-preflight',self.data); self.assertEqual(status,200)
        status,result = self.post('/api/commit-apply',self.confirm(plan))
        self.assertEqual(status,200,result); self.assertTrue(result['statusRefreshed'])
        self.assertEqual(result['commitResult']['outcome'],'committed')
        self.assertEqual(result['commitResult']['reportedRevision'],'3')
        self.assertIn('post-commit-hook',result['commitResult']['warnings'])
        self.assertNotIn('PRIVATE',repr(result))
        self.assertEqual(self.post('/api/commit-apply',self.confirm(plan))[0],409)
        self.fixture.client.command.assert_called_once()

    def test_http_status_failure_keeps_known_commit_and_disables_snapshot(self):
        self.fixture.client.command.return_value = self.fixture.backend.CommandResult((),0,'Committed revision 3.','')
        def status_rows(**kwargs):
            if self.fixture.client.command.call_count: raise RuntimeError('PRIVATE STATUS ERROR')
            return list(self.fixture.entries)
        self.fixture.client.status.side_effect = status_rows
        status,plan = self.post('/api/commit-preflight',self.data); self.assertEqual(status,200)
        status,result = self.post('/api/commit-apply',self.confirm(plan))
        self.assertEqual(status,200,result); self.assertFalse(result['statusRefreshed'])
        self.assertIsNone(result['snapshotToken']); self.assertEqual(result['rows'],[])
        self.assertEqual(result['commitResult']['reportedRevision'],'3')
        self.assertIn('status-refresh-failed',result['commitResult']['warnings'])
        self.assertNotIn('PRIVATE',repr(result))
        self.assertEqual(self.post('/api/commit-apply',self.confirm(plan))[0],409)
        self.fixture.client.command.assert_called_once()

    def test_http_workspace_replacement_after_dispatch_keeps_completion(self):
        def dispatch(args,**kwargs):
            (self.fixture.root/'.svn').rename(self.fixture.root/'old-metadata')
            (self.fixture.root/'.svn').mkdir()
            return self.fixture.backend.CommandResult((),0,'Committed revision 3.','')
        self.fixture.client.command.side_effect = dispatch
        status,plan = self.post('/api/commit-preflight',self.data); self.assertEqual(status,200)
        status,result = self.post('/api/commit-apply',self.confirm(plan))
        self.assertEqual(status,200,result); self.assertFalse(result['statusRefreshed'])
        self.assertIsNone(result['snapshotToken']); self.assertEqual(result['rows'],[])
        self.assertEqual(result['commitResult']['reportedRevision'],'3')
        self.assertIn('workspace-changed',result['commitResult']['warnings'])
        self.fixture.client.command.assert_called_once()

    def test_property_change_after_http_review_never_commits(self):
        status,plan = self.post('/api/commit-preflight',self.data); self.assertEqual(status,200)
        self.fixture.props = {'custom':'changed'}
        self.assertEqual(self.post('/api/commit-apply',self.confirm(plan))[0],409)
        self.fixture.client.command.assert_not_called()

    def test_missing_clean_named_member_rejected_over_http(self):
        status,_ = self.post('/api/commit-preflight',{**self.data,'operation':'changelist','name':'Feature'})
        self.assertEqual(status,409); self.fixture.client.command.assert_not_called()


if __name__ == '__main__': unittest.main()
