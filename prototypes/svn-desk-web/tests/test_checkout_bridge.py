"""Fixed-scope checkout tests. Only temporary synthetic directories/processes."""
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

WEB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WEB))
from checkout_ops import CheckoutService, checkout_requested, validate_request
from checkout_paths import local_destination, pinned_directory
from file_reads import StaleRead, history_module
from server import PrototypeServer, checkout_for
from workspace_bridge import WorkspaceCatalog, trusted_modules, directory_identity
import desktop_bridge

BACKEND = WEB.parents[1]
backend, _, discovery, settings = trusted_modules(BACKEND)
history = history_module(BACKEND)


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dest = self.root / '中文 @ checkout'
        self.factory = Mock()
        self.catalog = WorkspaceCatalog(discovery=discovery, settings=settings, adapter_factory=self.factory)
        self.service = self.make()
        self.discovery = patch.object(backend, 'discover_svn', return_value='/trusted/svn')
        self.discovery.start(); self.addCleanup(self.discovery.stop)

    def make(self, **values):
        return CheckoutService(**dict(url='https://example.test/repo', destination=self.dest,
            backend=backend, history=history, catalog=self.catalog, **values))

    def plan(self):
        result = self.service.operation('preflight', {})
        return {'planToken': result['planToken'], 'confirmed': True}

    def xml(self, url=None, root=None):
        import xml.etree.ElementTree as ET
        node = ET.Element('info'); entry = ET.SubElement(node, 'entry', kind='dir', path='.', revision='3')
        ET.SubElement(entry, 'url').text = url or self.service.url
        ET.SubElement(ET.SubElement(entry, 'wc-info'), 'wcroot-abspath').text = str(root or self.dest)
        return ET.tostring(node)

    def runner(self, argv, **kwargs):
        if argv[1] == 'checkout':
            (self.dest / '.svn').mkdir(); (self.dest / 'retained.txt').write_text('fixture')
            return b''
        return self.xml()

    def test_startup_describe_preflight_are_local_and_nonmutating(self):
        with patch.object(history, '_bounded_process') as run:
            self.assertEqual(self.service.describe()['state'], 'ready')
            plan = self.plan()
        run.assert_not_called(); self.factory.assert_not_called()
        self.assertFalse(self.dest.exists()); self.assertIs(plan['confirmed'], True)

    def test_exact_confirmed_command_bounded_output_and_explicit_registration(self):
        with patch.object(history, '_bounded_process', side_effect=self.runner) as run:
            result = self.service.operation('apply', self.plan())
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], ['/trusted/svn','checkout','--non-interactive','--no-auth-cache','--ignore-externals','--quiet','--','https://example.test/repo','.'])
        self.assertEqual(run.call_args_list[0].kwargs['cwd'], self.dest) if os.name == 'nt' else self.assertIn('/fd/', str(run.call_args_list[0].kwargs['cwd']))
        self.assertLessEqual(run.call_args_list[0].kwargs['timeout'], 300)
        self.assertEqual(run.call_args_list[0].kwargs['stdout_limit'], 262144)
        self.assertTrue(result['verified']); self.assertIsNone(self.catalog.selected)
        self.assertEqual(self.catalog.candidates[result['workspaceId']].path, self.dest)
        self.assertEqual(self.catalog.discover()['candidates'][0]['id'], result['workspaceId'])
        self.factory.assert_not_called()
        self.assertEqual(self.service.describe()['state'], 'completed')

    def test_existing_empty_directory_supported(self):
        self.dest.mkdir(); self.service = self.make()
        with patch.object(history, '_bounded_process', side_effect=self.runner):
            self.service.operation('apply', self.plan())
        self.assertEqual((self.dest / 'retained.txt').read_text(), 'fixture')

    def test_strict_fields_no_url_path_command_or_false_confirmation(self):
        for data in [{}, {'confirmed':True}, {'planToken':'x','confirmed':False},
                     {'planToken':'x','confirmed':1}, {'planToken':'x','confirmed':True,'url':'https://evil.test'},
                     {'planToken':'x','confirmed':True,'destination':'/other'}, [], {'planToken':'\n','confirmed':True}]:
            with self.subTest(data=data), self.assertRaises(ValueError): validate_request('apply',data)
        for data in [{'url':'https://evil.test'}, {'destination':'/other'}, {'command':'checkout'}, []]:
            with self.assertRaises(ValueError):validate_request('preflight',data)

    def test_stale_forged_expired_and_repeated_confirmations_never_execute(self):
        for mode in ['missing','forged','expired','replaced-plan']:
            self.service = self.make(); data=self.plan()
            if mode=='missing':self.service.plan=None
            if mode=='forged':data['planToken']='forged'
            if mode=='expired':self.service.plan=(data['planToken'],0)
            if mode=='replaced-plan':self.plan()
            with patch.object(history,'_bounded_process') as run, self.assertRaises(StaleRead):
                self.service.operation('apply',data)
            run.assert_not_called();self.assertFalse(self.dest.exists())

    def test_new_destination_appearing_after_review_is_not_reused(self):
        data=self.plan();self.dest.mkdir()
        with patch.object(history,'_bounded_process') as run,self.assertRaises(StaleRead):self.service.operation('apply',data)
        run.assert_not_called();self.assertEqual(list(self.dest.iterdir()),[])

    def test_existing_destination_replacement_rejected(self):
        self.dest.mkdir();self.service=self.make();data=self.plan()
        self.dest.rename(self.root/'original');self.dest.mkdir()
        with patch.object(history,'_bounded_process') as run,self.assertRaises(StaleRead):self.service.operation('apply',data)
        run.assert_not_called()

    def test_parent_replacement_rejected(self):
        parent=self.root/'parent';parent.mkdir();self.dest=parent/'checkout';self.service=self.make();data=self.plan()
        parent.rename(self.root/'original');parent.mkdir()
        with patch.object(history,'_bounded_process') as run,self.assertRaises(StaleRead):self.service.operation('apply',data)
        run.assert_not_called()

    def test_nonempty_nested_missing_parent_and_administrative_targets_rejected(self):
        self.dest.mkdir();(self.dest/'keep').write_text('keep')
        with self.assertRaises(ValueError):self.make()
        self.assertEqual((self.dest/'keep').read_text(),'keep')
        for target in [self.root/'missing'/'checkout',self.root/'.svn']:
            self.dest=target
            with self.assertRaises(ValueError):self.make()
        (self.root/'.svn').mkdir();self.dest=self.root/'nested'
        with self.assertRaises(ValueError):self.make()

    def test_unsafe_credential_urls_are_rejected_without_echo(self):
        for url in ['https://u:SECRET@example.test/repo','https://USER@example.test/repo',
                    'svn+ssh://example.test/repo','https://example.test/repo@17',
                    'file://remote/repo','https://example.test/repo?secret=x','--password','https://example.test/\n']:
            with self.subTest(url=url),self.assertRaises(ValueError) as caught:
                CheckoutService(url=url,destination=self.dest,backend=backend,history=history,catalog=self.catalog)
            self.assertNotIn('SECRET',str(caught.exception));self.assertNotIn('USER',str(caught.exception))

    def test_link_parent_rejected(self):
        real=self.root/'real';real.mkdir();link=self.root/'link'
        if os.name=='nt':
            # No symlink privilege dependency: real directory junction fixture.
            subprocess.run(['cmd','/c','mklink','/J',str(link),str(real)],check=True,capture_output=True)
        else:link.symlink_to(real,target_is_directory=True)
        self.dest=link/'checkout'
        with self.assertRaises(ValueError):self.make()
        self.assertFalse((real/'checkout').exists())

    def test_zero_stable_identity_rejected(self):
        with patch('checkout_ops.directory_identity',return_value=((1,0,str(self.root)),)),self.assertRaises(ValueError):self.make()

    def test_timeout_failure_retains_partial_files_and_disarms_entire_session(self):
        data=self.plan()
        def failure(*args,**kwargs):
            (self.dest/'partial').write_text('keep');raise TimeoutError('raw SECRET; No write was requested')
        with patch.object(history,'_bounded_process',side_effect=failure) as run:
            with self.assertRaises(TimeoutError):self.service.operation('apply',data)
            for kind,body in [('apply',data),('preflight',{})]:
                with self.assertRaises(StaleRead):self.service.operation(kind,body)
        self.assertEqual(run.call_count,1);self.assertEqual((self.dest/'partial').read_text(),'keep')
        self.assertEqual(self.service.describe()['state'],'uncertain');self.assertEqual(self.catalog.candidates,{})

    def test_success_double_apply_never_retries(self):
        data=self.plan()
        with patch.object(history,'_bounded_process',side_effect=self.runner) as run:
            self.service.operation('apply',data)
            with self.assertRaises(StaleRead):self.service.operation('apply',data)
        self.assertEqual(run.call_count,2)

    def test_concurrent_catalog_or_checkout_operations_rejected(self):
        for lock in [self.catalog._busy,self.service._busy]:
            lock.acquire()
            try:
                with self.assertRaises(BlockingIOError):self.service.operation('preflight',{})
            finally:lock.release()

    def test_malformed_ambiguous_wrong_source_or_root_xml_never_claims_success(self):
        for raw in [b'not xml',b'<!DOCTYPE info><info/>',b'\0',b'<info/>',b'<info><entry/><entry/></info>',
                    self.xml(url='https://other.test/repo'),self.xml(root=self.root/'elsewhere')]:
            self.dest=self.root/('checkout-'+str(len(list(self.root.iterdir()))));self.service=self.make()
            def runner(argv,**kw):
                if argv[1]=='checkout':(self.dest/'.svn').mkdir();return b''
                return raw
            with patch.object(history,'_bounded_process',side_effect=runner),self.assertRaises(Exception):
                self.service.operation('apply',self.plan())
            self.assertEqual(self.service.state,'uncertain');self.assertEqual(self.catalog.candidates,{})
            self.assertTrue((self.dest/'.svn').exists())

    def test_unexpected_quiet_output_is_uncertain(self):
        with patch.object(history,'_bounded_process',return_value=b'warning') as run,self.assertRaises(ValueError):
            self.service.operation('apply',self.plan())
        self.assertEqual(run.call_count,1);self.assertEqual(self.service.state,'uncertain')

    def test_settings_file_cannot_be_inside_target(self):
        self.dest.mkdir();self.catalog.settings_file=self.dest/'prefs.json'
        with self.assertRaises(ValueError):self.make()

    def test_replaced_directory_after_command_is_retained_unregistered(self):
        def runner(argv,**kw):
            self.runner(argv,**kw);self.dest.rename(self.root/'original');self.dest.mkdir();return b''
        with patch.object(history,'_bounded_process',side_effect=runner) as run,self.assertRaises((ValueError,OSError)):
            self.service.operation('apply',self.plan())
        retained=self.dest if os.name=='nt' else self.root/'original'
        self.assertEqual(run.call_count,1);self.assertTrue((retained/'retained.txt').exists())
        self.assertEqual(self.catalog.candidates,{})

    def test_full_http_contract_auth_token_and_redacted_uncertain_result(self):
        server=PrototypeServer(0,catalog=self.catalog,checkout=self.service)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join(2)))
        def request(path,data=None,token=True):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            headers={'Content-Type':'application/json'}
            if token:headers['X-SVN-Desk-Token']=server.token
            c.request('POST' if data is not None else 'GET',path,body=json.dumps(data) if data is not None else None,headers=headers)
            r=c.getresponse();answer=(r.status,json.loads(r.read()));c.close();return answer
        self.assertEqual(request('/api/checkout',token=False)[0],403)
        self.assertEqual(request('/api/checkout')[1]['state'],'ready')
        self.assertEqual(request('/api/checkout-preflight',{'url':'evil'})[0],400)
        status,plan=request('/api/checkout-preflight',{});self.assertEqual(status,200)
        def failure(*a,**k):raise RuntimeError('SECRET: No write was requested')
        with patch.object(history,'_bounded_process',side_effect=failure) as run:
            status,result=request('/api/checkout-apply',{'planToken':plan['planToken'],'confirmed':True})
        self.assertEqual(status,503);self.assertNotIn('SECRET',str(result));self.assertNotIn('No write',str(result))
        self.assertIn('保留',result['error']);self.assertEqual(run.call_count,1)
        self.assertEqual(request('/api/checkout')[1]['state'],'uncertain')
        self.assertEqual(request('/api/checkout-preflight',{})[0],409)

    def test_desktop_requires_paired_arguments_and_no_self_check_side_effects(self):
        parse=desktop_bridge.parser().parse_args
        for args in [['--checkout-url','https://example.test/repo'],['--checkout-destination',str(self.dest)]]:
            with self.assertRaises(ValueError):desktop_bridge.catalog_for(parse(args),BACKEND)
        args=parse(['--checkout-url','https://example.test/repo','--checkout-destination',str(self.dest)])
        self.assertTrue(checkout_requested(args))
        catalog=desktop_bridge.catalog_for(args,BACKEND);self.assertEqual(catalog.candidates,{})
        service=checkout_for(args,BACKEND,catalog);self.assertEqual(service.state,'ready');self.assertFalse(self.dest.exists())
        with patch.object(desktop_bridge,'verified_backend') as verify:
            self.assertEqual(desktop_bridge.main(['--self-check','--checkout-url','https://example.test/repo','--checkout-destination',str(self.dest)]),1)
        verify.assert_not_called()

    def test_destination_syntax_rejects_relative_unc_drive_relative_and_ads(self):
        for raw in ['relative', 'C:relative', r'\\server\share', r'C:\work\..\elsewhere', r'C:\work:stream', 'C:\\trailing.']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):local_destination(raw, platform='nt')
        with self.assertRaises(ValueError):local_destination('relative', platform='posix')
        self.assertEqual(str(local_destination(r'C:\work\new',platform='nt')),r'C:\work\new')

    @unittest.skipIf(os.name == 'nt', 'POSIX descriptor race; Windows handles are tested separately')
    def test_race_at_process_launch_cannot_redirect_writes_to_replacement(self):
        outside=self.root/'outside';outside.mkdir();original=subprocess.Popen
        def fixture(argv,**kw):
            self.dest.rename(self.root/'original');self.dest.symlink_to(outside,target_is_directory=True)
            return original([sys.executable,'-c','from pathlib import Path; Path("MARKER").write_text("fixture")'],**kw)
        with patch('svn_client.history.subprocess.Popen',side_effect=fixture),self.assertRaises(ValueError):
            self.service.operation('apply',self.plan())
        self.assertFalse((outside/'MARKER').exists())
        self.assertTrue((self.root/'original'/'MARKER').exists())
        self.assertEqual(self.service.state,'uncertain');self.assertEqual(self.catalog.candidates,{})

    @unittest.skipIf(os.name == 'nt', 'POSIX directory descriptor race; native Windows handles deny rename')
    def test_parent_swap_before_mkdir_cannot_create_outside_directory(self):
        from contextlib import contextmanager
        parent=self.root/'parent';parent.mkdir();outside=self.root/'outside';outside.mkdir()
        self.dest=parent/'checkout';self.service=self.make()
        @contextmanager
        def swapped(path,expected):
            with pinned_directory(path,expected) as cwd:
                if path==parent:
                    parent.rename(self.root/'original-parent');parent.symlink_to(outside,target_is_directory=True)
                yield cwd
        with patch('checkout_ops.pinned_directory',side_effect=swapped),self.assertRaises(ValueError):
            self.service.operation('apply',self.plan())
        self.assertFalse((outside/'checkout').exists())
        self.assertTrue((self.root/'original-parent'/'checkout').is_dir())

    def test_destination_replaced_before_registration_is_never_registered(self):
        original=__import__('workspace_bridge').working_copy_identity
        def swapped(path):
            if path==self.dest:
                self.dest.rename(self.root/'original');(self.dest/'.svn').mkdir(parents=True)
            return original(path)
        with patch.object(history,'_bounded_process',side_effect=self.runner),patch('checkout_ops.working_copy_identity',side_effect=swapped),self.assertRaises((ValueError,OSError)):
            self.service.operation('apply',self.plan())
        self.assertEqual(self.catalog.candidates,{})
        retained=self.dest if os.name=='nt' else self.root/'original'
        self.assertTrue((retained/'retained.txt').exists())

    @unittest.skipUnless(os.name == 'nt', 'Native Windows directory sharing requires Windows')
    def test_native_windows_directory_handle_rejects_rename_until_release(self):
        self.dest.mkdir();other=self.root/'renamed'
        with pinned_directory(self.dest,directory_identity(self.dest)):
            with self.assertRaises(PermissionError):self.dest.rename(other)
            self.assertTrue(self.dest.is_dir());self.assertFalse(other.exists())
        self.dest.rename(other);self.assertTrue(other.is_dir())

    def test_windows_directory_handles_deny_write_delete_and_close_every_handle(self):
        import ctypes
        from checkout_paths import _windows_directory
        create=Mock(side_effect=range(1,100));close=Mock(return_value=True)
        kernel=Mock(CreateFileW=create,CloseHandle=close)
        self.dest.mkdir();expected=directory_identity(self.dest)
        with patch.object(ctypes,'WinDLL',return_value=kernel,create=True):
            with _windows_directory(self.dest,expected) as cwd:self.assertEqual(cwd,self.dest)
        self.assertEqual(create.call_count,len(expected));self.assertEqual(close.call_count,len(expected))
        for call in create.call_args_list:
            self.assertEqual(call.args[1:3],(0x81,0x1));self.assertEqual(call.args[5],0x02200000)

    def test_windows_directory_open_failure_never_falls_back_or_leaks_handles(self):
        import ctypes
        from checkout_paths import _windows_directory
        self.dest.mkdir();expected=directory_identity(self.dest)
        for failure in [None,ctypes.c_void_p(-1).value]:
            create=Mock(side_effect=[1,failure]);close=Mock(return_value=True)
            kernel=Mock(CreateFileW=create,CloseHandle=close)
            with patch.object(ctypes,'WinDLL',return_value=kernel,create=True),self.assertRaises(OSError):
                with _windows_directory(self.dest,expected):self.fail('Unsafe guard admitted execution')
            self.assertEqual(create.call_count,2);close.assert_called_once_with(1)
            for call in create.call_args_list:self.assertEqual(call.args[1:3],(0x81,0x1))

    def test_failed_directory_anchor_never_launches_and_preserves_target(self):
        with patch('checkout_ops.pinned_directory',side_effect=OSError('unsupported')):
            with patch.object(history,'_bounded_process') as run,self.assertRaises(OSError):
                self.service.operation('apply',self.plan())
        run.assert_not_called();self.assertFalse(self.dest.exists());self.assertEqual(self.service.state,'uncertain')

    def test_synthetic_real_pipe_runner_is_bounded_and_uses_no_shell(self):
        original=subprocess.Popen;observed=[]
        def fixture(argv,**kw):
            observed.append(argv)
            script=('from pathlib import Path; Path(".svn").mkdir(); Path("fixture.txt").write_text("synthetic")' if argv[1]=='checkout'
                    else 'import sys; sys.stdout.buffer.write('+repr(self.xml())+')')
            return original([sys.executable,'-c',script],**kw)
        with patch('svn_client.history.subprocess.Popen',side_effect=fixture):
            result=self.service.operation('apply',self.plan())
        self.assertTrue(result['verified']);self.assertEqual(len(observed),2)
        self.assertEqual((self.dest/'fixture.txt').read_text(),'synthetic')


if __name__=='__main__':unittest.main()
