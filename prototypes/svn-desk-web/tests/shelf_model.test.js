import test from 'node:test';
import assert from 'node:assert/strict';
import {canShelfFile,parseShelfName,sameShelfScope,validateShelfFiles,validateShelfPlan} from '../web/model.js';
const rows=[{id:'one',path:'visible/a <x>.txt',item:'modified',kind:'file'},{id:'two',path:'hidden/b.txt',item:'unversioned',kind:'file'}];
const request={workspaceId:'w',selectionVersion:1,snapshotToken:'s',operation:'create',ids:['one','two'],name:'backup'};
const files=rows.map(r=>({path:r.path,state:r.item,kind:'file',size:2}));
const plan={...request,mode:'live',planToken:'p',expiresIn:120,localOnly:true,backupOnly:true,files,totalBytes:4};
test('Shelf is limited to eligible explicit regular-file changes',()=>{
 assert(rows.every(canShelfFile));for(const item of ['added','deleted'])assert(canShelfFile({...rows[0],item}));
 assert(canShelfFile({...rows[0],item:'normal',props:'modified'}));
 for(const delta of [{kind:'dir'},{kind:'symlink'},{tree_conflicted:true},{file_external:true},{wc_locked:true},{switched:true},{props:'conflicted'},{item:'replaced'},{item:'normal'}])assert(!canShelfFile({...rows[0],...delta}));
});
test('Shelf name is explicit, Unicode-safe and bounded',()=>{assert.equal(parseShelfName('  临时备份  '),'临时备份');for(const value of ['', ' ', '\0', 'x'.repeat(129)])assert.throws(()=>parseShelfName(value));});
test('Shelf create binds snapshot, exact full paths, flags, token and TTL',()=>{
 validateShelfPlan(plan,request,rows);assert(sameShelfScope(plan,request));
 for(const delta of [{mode:'demo'},{workspaceId:'other'},{selectionVersion:2},{snapshotToken:'old'},{operation:'restore'},{localOnly:false},{backupOnly:false},{name:'other'},{expiresIn:119},{planToken:''},{totalBytes:5},{files:files.slice(1),totalBytes:2},{files:[files[0],files[0]]}])assert.throws(()=>validateShelfPlan({...plan,...delta},request,rows));
 assert.throws(()=>validateShelfPlan(plan,{...request,ids:['one','one']},rows));
});
test('Shelf paths and sizes reject unsafe or incomplete scope',()=>{
 for(const path of ['../escape','/absolute','a/../b','a\\b','a//b','a\0b'])assert.throws(()=>validateShelfFiles([{...files[0],path}],2));
 for(const delta of [{kind:'dir'},{size:-1},{size:1.5},{state:null}])assert.throws(()=>validateShelfFiles([{...files[0],...delta}],2));
 assert.throws(()=>validateShelfFiles([],0));
});
test('Restore requires whole unchanged shelf metadata and matching identity',()=>{
 const req={workspaceId:'w',selectionVersion:1,snapshotToken:'s',operation:'restore',shelfId:'backup-id'};
 const restore={...plan,...req,backupOnly:false};validateShelfPlan(restore,req,files);
 for(const delta of [{shelfId:'other'},{files:[{...files[0],state:'deleted'},files[1]]},{files:[{...files[0],size:3},files[1]],totalBytes:5}])assert.throws(()=>validateShelfPlan({...restore,...delta},req,files));
});
