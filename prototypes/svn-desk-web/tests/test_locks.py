"""Repository lock bridge, using synthetic files and mocked SVN transport only."""
from dataclasses import replace
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lock_ops import validate_request, public_files
from server import ReadOnlyAdapter
from changelist_ops import status_rows
from file_reads import StaleRead
from workspace_bridge import trusted_modules

BACKEND = Path(os.environ.get('SVN_DESK_BACKEND_SOURCE', Path(__file__).resolve().parents[2] / 'svn-release-review-9bbc406'))

def request(**changes):
    return dict(workspaceId='w', selectionVersion=1, snapshotToken='s',
                ids=[str(Path('src/a.txt'))], action='lock', comment='Review asset', **changes)


class SchemaTests(unittest.TestCase):
    def test_exact_schema_and_bounded_comment(self):
        base = request()
        validate_request('preflight', base)
        for key, values in {'action':['force', '', True], 'ids':[[], ['a', 'A'], ['../x'], ['.svn/wc.db'], ['x']*101, [True]],
                            'comment':['x'*4097, '汉'*1400, '\x00', '\x7f', '\x85', '\ud800'],
                            'selectionVersion':[True, 0]}.items():
            for value in values:
                with self.subTest(key=key, value=repr(value)[:50]), self.assertRaises(ValueError):
                    validate_request('preflight', {**base, key:value})
        for field in ['force', 'token', 'url', 'args']:
            with self.assertRaises(ValueError): validate_request('preflight', {**base, field:True})
        with self.assertRaises(ValueError): validate_request('preflight', {**base, 'action':'unlock'})
        validate_request('preflight', {**base, 'action':'unlock', 'comment':''})

    def test_literal_confirmation_no_scope_edits(self):
        data = dict(workspaceId='w', selectionVersion=1, snapshotToken='s', planToken='p', confirmed=True)
        validate_request('apply', data)
        for value in [False, 1, 'true', None]:
            with self.assertRaises(ValueError): validate_request('apply', {**data, 'confirmed':value})
        for field in ['ids', 'action', 'comment', 'force']:
            with self.assertRaises(ValueError): validate_request('apply', {**data, field:None})


@unittest.skipUnless((BACKEND/'svn_client/locks.py').is_file(), 'Paired backend unavailable.')
class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'synthetic'; (self.root/'.svn').mkdir(parents=True)
        (self.root/'src').mkdir()
        self.names = [str(Path('src/a.txt')), str(Path('src/b @.txt'))]
        for name in self.names: (self.root/name).write_bytes(b'original contents\n')
        self.backend, _, _, _ = trusted_modules(BACKEND)
        with patch.object(self.backend, 'discover_svn', return_value='/trusted/svn'):
            self.adapter = ReadOnlyAdapter(self.root, BACKEND, enable_locks=True)
        self.service = self.adapter._lock_service
        self.assertIsNotNone(self.service)
        self.client = self.adapter.client
        self.entries = [self.backend.StatusEntry(name, 'normal') for name in self.names]
        self.client.status = Mock(side_effect=lambda **kw:list(self.entries))
        self.local = {p:'' for p in self.names}; self.remote = {p:'' for p in self.names}
        self.rev = '5'; self.changed = '5'; self.uuid = 'fixture'; self.props = {}
        self.remote_calls = []; self.mutations = []; self.mode = 'normal'; self.bad_info = None
        self.history = importlib.import_module('svn_client.history')
        p = patch.object(self.history, '_bounded_process', side_effect=self.read)
        p.start(); self.addCleanup(p.stop)
        self.client.command = Mock(side_effect=self.command)
        self.invalidated = Mock()
        self.snapshot = ('s', status_rows(self.client))

    def url(self, name):
        return 'https://example.test/repo' + ('' if name=='.' else '/'+quote(Path(name).as_posix(),safe='/@'))

    def xml(self, name, remote=False):
        root=ET.Element('info'); node=ET.SubElement(root,'entry', path=name, kind='dir' if name=='.' else 'file', revision=self.rev)
        ET.SubElement(node,'url').text=self.url(name)
        repo=ET.SubElement(node,'repository'); ET.SubElement(repo,'uuid').text=self.uuid
        ET.SubElement(repo,'root').text='https://example.test/repo'
        if not remote:
            wc=ET.SubElement(node,'wc-info'); ET.SubElement(wc,'wcroot-abspath').text=str(self.root)
            ET.SubElement(wc,'schedule').text='normal'
        ET.SubElement(node,'commit',revision=self.changed)
        token=(self.remote if remote else self.local).get(name,'')
        if token:
            lock=ET.SubElement(node,'lock'); ET.SubElement(lock,'token').text=token
            ET.SubElement(lock,'owner').text='SECRET OWNER'; ET.SubElement(lock,'comment').text='SECRET COMMENT '+token
        return ET.tostring(root)

    def read(self, argv, **kwargs):
        self.assertLessEqual(kwargs['timeout'], 25)
        target=argv[-1]
        if argv[1]=='proplist':
            target=target[:-1] if '@' in target else target
            root=ET.Element('properties'); node=ET.SubElement(root,'target',path=target)
            for name,value in self.props.items(): ET.SubElement(node,'property',name=name).text=value
            return ET.tostring(root)
        self.assertEqual(argv[1], 'info')
        self.assertIn('--non-interactive', argv); self.assertIn('--no-auth-cache', argv)
        if '://' in target:
            self.remote_calls.append(target)
            if self.mode=='postread-failed' and self.mutations: raise self.history.HistoryError('SECRET')
            name=next(p for p in self.names if self.url(p)+'@HEAD'==target)
            raw=self.xml(name, True)
            return self.bad_info(raw) if self.bad_info else raw
        return self.xml(target[:-1] if '@' in target else target)

    def command(self, args, **kwargs):
        self.mutations.append(args)
        self.assertIn(args[0], {'lock','unlock'}); self.assertNotIn('--force', args)
        for i,target in enumerate(args[args.index('--')+1:]):
            name=target[:-1] if '@' in target else target
            if self.mode=='partial' and i>0: continue
            if args[0]=='lock': self.local[name]=self.remote[name]='opaque:SECRET-'+name
            else: self.local[name]=self.remote[name]=''
        if self.mode=='error': raise self.backend.SvnError('SECRET timeout')
        return self.backend.CommandResult((), 0, 'SECRET OUTPUT', 'SECRET warning' if self.mode=='warning' else '')

    def preflight(self, **changes):
        return self.service.preflight({**request(), **changes}, self.snapshot)

    def apply_data(self, plan):
        return dict(workspaceId='w', selectionVersion=1, snapshotToken='s', planToken=plan['planToken'], confirmed=True)

    def apply(self, plan):
        return self.service.apply(self.apply_data(plan), self.snapshot, self.invalidated)

    def test_explicit_remote_preflight_no_mutation_and_private_metadata(self):
        plan=self.preflight(ids=self.names)
        self.assertEqual(len(self.mutations),0); self.assertTrue(self.remote_calls)
        text=json.dumps(plan); self.assertNotIn('SECRET',text); self.assertNotIn('https:',text)
        self.assertNotIn('repository_uuid',text); self.assertTrue(plan['contactsRepository'])
        self.assertEqual([f['id'] for f in plan['files']],self.names)
        self.assertEqual(plan['expiresIn'],120)

    def test_acquire_exact_peg_comment_and_no_force(self):
        result=self.apply(self.preflight(ids=self.names))
        self.assertTrue(result['verified']); self.assertEqual(result['outcome'],'verified')
        self.assertEqual(self.mutations,[['lock','--message=Review asset','--encoding','UTF-8','--',self.names[0],self.names[1]+'@']])
        self.assertNotIn('SECRET',repr(result)); self.invalidated.assert_called_once()
        with self.assertRaises(StaleRead): self.apply_data_again(result)

    def apply_data_again(self, _):
        return self.service.apply(dict(workspaceId='w',selectionVersion=1,snapshotToken='s',planToken='used',confirmed=True),self.snapshot,self.invalidated)

    def test_unlock_exact_owned_token(self):
        self.local[self.names[0]]=self.remote[self.names[0]]='opaque:SECRET'
        plan=self.preflight(action='unlock',comment='')
        self.assertTrue(plan['files'][0]['ownedByWorkingCopy'])
        self.assertNotIn('SECRET',repr(plan))
        self.assertTrue(self.apply(plan)['verified'])
        self.assertEqual(self.mutations,[['unlock','--',self.names[0]]])

    def test_other_working_copy_stale_and_outdated_block(self):
        for local,remote,changed in [('', 'other', '5'),('old','','5'),('old','other','5'),('','','6')]:
            self.local[self.names[0]]=local; self.remote[self.names[0]]=remote; self.changed=changed
            with self.subTest(local=local,remote=remote,changed=changed), self.assertRaises(Exception): self.preflight()
        self.assertFalse(self.mutations)

    def test_secrets_in_comment_rejected_before_remote(self):
        for comment in ['https://u:PASSWORD@host/path','opaquelocktoken:SECRET','urn:uuid:SECRET']:
            with self.assertRaises(Exception): self.preflight(comment=comment)
        self.assertFalse(self.remote_calls); self.assertFalse(self.mutations)

    def test_full_selection_no_skipping(self):
        self.remote[self.names[1]]='other'
        with self.assertRaises(Exception): self.preflight(ids=self.names)
        self.assertFalse(self.mutations)

    def test_scope_state_expiry_and_content_changed_no_dispatch(self):
        plan=self.preflight(); path=self.root/self.names[0]; old=path.stat()
        path.write_bytes(b'x'*old.st_size); os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns))
        with self.assertRaises(StaleRead): self.apply(plan)
        with self.assertRaises(StaleRead): self.apply(plan)
        self.assertFalse(self.mutations)
        plan=self.preflight(); self.service.plan['expires']=0
        with self.assertRaises(StaleRead): self.apply(plan)

    def test_remote_changed_after_preflight_does_not_dispatch(self):
        plan=self.preflight(); self.remote[self.names[0]]='other'
        with self.assertRaises(StaleRead): self.apply(plan)
        self.assertFalse(self.mutations); self.invalidated.assert_not_called()

    def test_property_or_base_change_blocks(self):
        for which in ['property','base']:
            plan=self.preflight()
            if which=='property': self.props={'custom':'new'}
            else: self.rev='6'
            with self.assertRaises(StaleRead): self.apply(plan)
        self.assertFalse(self.mutations)

    def test_change_during_final_server_check_blocks_before_dispatch(self):
        plan=self.preflight(); original=self.read
        def changed(argv,**kwargs):
            raw=original(argv,**kwargs)
            if '://' in argv[-1]: self.props={'changed':'after remote read'}
            return raw
        with patch.object(self.history,'_bounded_process',side_effect=changed):
            with self.assertRaises(StaleRead): self.apply(plan)
        self.assertFalse(self.mutations)

    def test_partial_error_and_failed_postread_never_retry_or_success(self):
        for mode in ['partial','error','postread-failed']:
            self.local={p:'' for p in self.names}; self.remote={p:'' for p in self.names}; self.mode='normal'; self.mutations=[]
            plan=self.preflight(ids=self.names); self.mode=mode
            result=self.apply(plan)
            self.assertFalse(result['verified']); self.assertEqual(result['warnings'],['operation-outcome-unknown'])
            with self.assertRaises(StaleRead): self.apply(plan)
            self.assertEqual(len(self.mutations),1)

    def test_stderr_warning_with_verified_state_preserved(self):
        plan=self.preflight(); self.mode='warning'; result=self.apply(plan)
        self.assertTrue(result['verified']); self.assertEqual(result['warnings'],['command-diagnostics'])
        self.assertNotIn('SECRET',repr(result))

    def test_unsafe_files_and_status_rejected_before_remote(self):
        for change in [{'item':v} for v in ['added','deleted','replaced','missing','conflicted','unversioned']] + [{'copied':True},{'wc_locked':True},{'file_external':True},{'props':'conflicted'}]:
            self.entries[0]=replace(self.backend.StatusEntry(self.names[0],'normal'),**change)
            self.snapshot=('s',status_rows(self.client))
            with self.assertRaises(ValueError): self.preflight()
        self.assertFalse(self.remote_calls)

    def test_unsafe_parent_admin_lock_or_structure_rejected_before_remote(self):
        for change in [{'wc_locked':True},{'copied':True},{'file_external':True},{'item':'added'},{'props':'conflicted'}]:
            self.entries=self.entries[:2]+[replace(self.backend.StatusEntry('src','normal'),**change)]
            self.snapshot=('s',status_rows(self.client))
            with self.subTest(change=change), self.assertRaises(ValueError): self.preflight()
        self.assertFalse(self.remote_calls); self.assertFalse(self.mutations)

    def test_hardlinks_and_directories_rejected(self):
        path=self.root/self.names[0]; path.unlink(); os.link(self.root/self.names[1],path)
        with self.assertRaises(ValueError): self.preflight()
        path.unlink(); path.mkdir()
        with self.assertRaises(ValueError): self.preflight()
        self.assertFalse(self.remote_calls)

    def test_server_identity_and_duplicate_token_rejected(self):
        def different(raw):
            tree=ET.fromstring(raw); tree.find('entry/url').text='https://example.test/other'; return ET.tostring(tree)
        self.bad_info=different
        with self.assertRaises(Exception): self.preflight()
        self.local[self.names[0]]=self.remote[self.names[0]]='opaque:SECRET'
        def duplicate(raw):
            tree=ET.fromstring(raw); ET.SubElement(tree.find('entry/lock'),'token').text='other'; return ET.tostring(tree)
        self.bad_info=duplicate
        with self.assertRaises(Exception): self.preflight(action='unlock',comment='')
        self.assertFalse(self.mutations)

    def test_disabled_default_and_pin_mismatch_preserve_status(self):
        with patch.object(self.backend,'discover_svn',return_value='/trusted/svn'):
            disabled=ReadOnlyAdapter(self.root,BACKEND)
            with patch('lock_ops.PAIRED_LOCKS_SHA256','0'*64): mismatch=ReadOnlyAdapter(self.root,BACKEND,enable_locks=True)
        self.assertIsNone(disabled._lock_service); self.assertIsNone(mismatch._lock_service)
        self.assertIsNotNone(mismatch._file_service)

    def test_clean_files_visible_only_when_enabled_and_refresh_consumes_plan(self):
        plan=self.preflight(); payload=self.adapter.snapshot()
        self.assertEqual(len(payload['rows']),2); self.assertTrue(payload['locksEnabled'])
        self.assertIsNone(self.service.plan)
        self.adapter._lock_service=None
        self.adapter.snapshot()
        self.client.status.assert_called_with(verbose=False)

    def test_adapter_retains_verified_result_on_status_failure(self):
        plan=self.preflight(); self.adapter._read_snapshot=self.snapshot
        with patch.object(self.adapter,'_status_rows',side_effect=ValueError('SECRET')):
            payload=self.adapter.lock_operation('apply',self.apply_data(plan))
        self.assertTrue(payload['lockResult']['verified']); self.assertFalse(payload['statusRefreshed'])
        self.assertFalse(payload['locksEnabled']); self.assertIsNone(payload['snapshotToken']); self.assertEqual(payload['rows'],[])
        self.assertEqual(payload['lockResult']['warnings'],['status-refresh-failed'])


if __name__=='__main__': unittest.main()
