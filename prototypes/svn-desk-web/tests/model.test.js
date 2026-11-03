import test from 'node:test';
import assert from 'node:assert/strict';
import {stateFor,STATES,filteredRows,groupsFor,summary,reviewSelection,safeText,fileName} from '../web/model.js';
import {demoRows} from '../web/demo-data.js';
test('fixture is explicitly scoped and has stable IDs',()=>{assert.equal(demoRows.length,12);assert.equal(new Set(demoRows.map(r=>r.id)).size,12);assert.deepEqual(summary(demoRows),{total:12,modified:6,conflicted:1,unversioned:2});});
test('groups retain SVN local labels and default group first',()=>{const g=groupsFor(demoRows);assert.equal(g.length,3);assert.equal(g[0][0],'');assert.equal(g[0][1].length,4);});
test('search matches case-insensitive paths and labels',()=>{assert.equal(filteredRows(demoRows,{query:'PLAYERCONTROLLER'}).length,1);assert.equal(filteredRows(demoRows,{query:'界面细节'}).length,3);assert.equal(filteredRows(demoRows,{query:'不存在'}).length,0);});
test('folder filter is segment bound, never selects prefix siblings',()=>{const rows=[{path:'Assets/Player/a.cs'},{path:'Assets/PlayerBackup/a.cs'},{path:'Assets/Player'}];assert.equal(filteredRows(rows,{folder:'Assets/Player'}).length,2);});
test('conflict filter includes tree and property conflicts',()=>{const rows=[{path:'a',item:'normal',tree_conflicted:true},{path:'b',item:'normal',props:'conflicted'},{path:'c',item:'modified'}];assert.equal(filteredRows(rows,{filter:'conflicted'}).length,2);});
test('modified properties never labelled clean',()=>assert.equal(stateFor({item:'normal',props:'modified'}),STATES.none));
test('review contains only explicit selection including filtered-hidden files',()=>{const selected=new Set([demoRows[0].id,demoRows[8].id]);assert.equal(filteredRows(demoRows,{query:'Forest'}).length,1);assert.deepEqual(reviewSelection(demoRows,selected).map(r=>r.id),[demoRows[0].id,demoRows[8].id]);});
test('no arbitrary stale selections included in review',()=>assert.equal(reviewSelection(demoRows,new Set(['../../private'])).length,0));
test('HTML-sensitive filenames are escaped',()=>assert.equal(safeText('<img src=x onerror="x">&\''),'&lt;img src=x onerror=&quot;x&quot;&gt;&amp;&#39;'));
test('file names handle Windows display separators',()=>assert.equal(fileName('C:\\Work\\a.cs'),'a.cs'));

test('modified filter includes property-only changes in normal/none states',()=>{const rows=[{path:'a',item:'normal',props:'modified'},{path:'b',item:'none',props:'modified'},{path:'c',item:'normal',props:'none'}];assert.equal(filteredRows(rows,{filter:'modified'}).length,2);});


import {isStaleWorkspaceCatalog} from '../web/model.js';
test('older workspace catalogs cannot roll back a newer selected generation',()=>{
 assert.equal(isStaleWorkspaceCatalog({current:{selectionVersion:2}},3),true);
 assert.equal(isStaleWorkspaceCatalog({current:{selectionVersion:3}},3),false);
 assert.equal(isStaleWorkspaceCatalog({current:{selectionVersion:4}},3),false);
 assert.equal(isStaleWorkspaceCatalog({enabled:false},3),false);
});

import {parseRevision,parseComparisonPair,canReadFile,sameReadScope,validateFileResponse} from '../web/model.js';
test('revision grammar disallows ranges, arbitrary args, unsafe numbers and wrong endpoints',()=>{
 assert.equal(parseRevision(' base '),'BASE');assert.equal(parseRevision('9223372036854775807'),'9223372036854775807');
 assert.equal(parseRevision('WORKING',{working:true}),'WORKING');assert.equal(parseRevision('0',{history:true}),'0');
 for(const value of ['WORKING','-1','1:2','01','--help','1.5','9223372036854775808','1e2',''])assert.throws(()=>parseRevision(value));
 assert.throws(()=>parseRevision('BASE',{history:true}));assert.throws(()=>parseRevision(1));
});
const readScope={workspaceId:'a',selectionVersion:2,snapshotToken:'snapshot-a',id:'same-id',path:'a.txt'};
const diffResponse={...readScope,mode:'live',fromRevision:'BASE',toRevision:'WORKING',text:'汉',truncated:false,sourceUtf8Bytes:3,returnedUtf8Bytes:3,binaryPayloadsIncluded:false,textEncoding:'utf-8',undecodableBytes:0,escapedControls:0};
test('read scope binds identical native IDs to workspace, selection, snapshot and path',()=>{
 assert.equal(sameReadScope(readScope,{...readScope}),true);
 for(const field of ['workspaceId','selectionVersion','snapshotToken','id','path'])assert.equal(sameReadScope(readScope,{...readScope,[field]:'other'}),false);
});
test('diff validates identities, revisions and UTF-8 bounds before display',()=>{
 const revisions={fromRevision:'BASE',toRevision:'WORKING'};
 assert.equal(validateFileResponse(diffResponse,readScope,'diff',revisions),diffResponse);
 for(const change of [{workspaceId:'b'},{snapshotToken:'older'},{path:'b.txt'},{fromRevision:'HEAD'},{returnedUtf8Bytes:1},{binaryPayloadsIncluded:true},{text:'a'.repeat(262145),returnedUtf8Bytes:262145,sourceUtf8Bytes:262145}])assert.throws(()=>validateFileResponse({...diffResponse,...change},readScope,'diff',revisions));
});
test('history validates pagination, ordered revisions and changed paths',()=>{
 const entry={revision:'5',author:'A',date:'date',message:'message',changedPaths:[{path:'/a.txt',action:'M',kind:'file',copyfrom_path:null,copyfrom_rev:null}]};
 const data={...readScope,mode:'live',startRevision:'HEAD',entries:[entry],hasMore:true,nextStartRevision:'4'};
 assert.equal(validateFileResponse(data,readScope,'history',{startRevision:'HEAD'}),data);
 for(const change of [{startRevision:'5'},{entries:[entry,entry]},{nextStartRevision:'5'},{entries:[]},{entries:[{...entry,revision:5}]}])assert.throws(()=>validateFileResponse({...data,...change},readScope,'history',{startRevision:'HEAD'}));
 assert.doesNotThrow(()=>validateFileResponse({...data,entries:[],hasMore:false,nextStartRevision:null},readScope,'history',{startRevision:'HEAD'}));
});
test('unsupported file rows never enable read actions',()=>{
 for(const row of [null,{item:'unversioned'},{item:'ignored'},{item:'external'},{item:'modified',switched:true},{item:'modified',file_external:true}])assert.equal(canReadFile(row),false);
 assert.equal(canReadFile({item:'modified'}),true);
});
test('history rejects noncanonical, excessive, or skipped pagination revisions',()=>{
 const entry={revision:'5',author:'A',date:'date',message:'message',changedPaths:[{path:'/a',action:'A',kind:'file',copyfrom_path:'/b',copyfrom_rev:'4'}]};
 const data={...readScope,mode:'live',startRevision:'HEAD',entries:[entry],hasMore:true,nextStartRevision:'4'};
 const check=value=>validateFileResponse(value,readScope,'history',{startRevision:'HEAD'});
 for(const value of ['05','-1','9223372036854775808','1.0','HEAD']){
  assert.throws(()=>check({...data,entries:[{...entry,revision:value}]}));
  assert.throws(()=>check({...data,nextStartRevision:value}));
  assert.throws(()=>check({...data,entries:[{...entry,changedPaths:[{...entry.changedPaths[0],copyfrom_rev:value}]}]}));
 }
 assert.throws(()=>check({...data,nextStartRevision:'3'}));
 assert.doesNotThrow(()=>check({...data,entries:[{...entry,revision:'9223372036854775807'}],nextStartRevision:'9223372036854775806'}));
});
test('diff truncation flag matches source and returned byte counts',()=>{
 const check=data=>validateFileResponse(data,readScope,'diff',{fromRevision:'BASE',toRevision:'WORKING'});
 assert.throws(()=>check({...diffResponse,truncated:true}));
 assert.throws(()=>check({...diffResponse,sourceUtf8Bytes:4}));
 assert.doesNotThrow(()=>check({...diffResponse,sourceUtf8Bytes:4,truncated:true}));
});
test('obstructed and incomplete entries disable file reads',()=>{
 assert.equal(canReadFile({item:'obstructed'}),false);assert.equal(canReadFile({item:'incomplete'}),false);
});

test('comparison pairs reject BASE to BASE but preserve other supported comparisons',()=>{
 assert.throws(()=>parseComparisonPair(' base ','BASE'),/BASE → WORKING/);
 for(const [from,to] of [['BASE','WORKING'],['BASE','HEAD'],['HEAD','BASE'],['HEAD','HEAD'],['7','7'],['0','8'],['7','WORKING']])assert.deepEqual(parseComparisonPair(from,to),{fromRevision:from,toRevision:to});
 for(const [from,to] of [['WORKING','BASE'],['HEAD','-1'],['1:2','8']])assert.throws(()=>parseComparisonPair(from,to));
 assert.throws(()=>validateFileResponse({...diffResponse,toRevision:'BASE'},readScope,'diff',{fromRevision:'BASE',toRevision:'BASE'}));
});
test('diff requires bounded consistent encoding metadata, also for truncated previews',()=>{
 const check=data=>validateFileResponse(data,readScope,'diff',{fromRevision:'BASE',toRevision:'WORKING'});
 for(const field of ['textEncoding','undecodableBytes','escapedControls']){const data={...diffResponse};delete data[field];assert.throws(()=>check(data));}
 for(const field of ['undecodableBytes','escapedControls'])for(const bad of [-1,0.5,NaN,Infinity,8388609,'0',null,true,undefined])assert.throws(()=>check({...diffResponse,[field]:bad}));
 for(const textEncoding of ['cp936','utf8','utf-8-visible-escapes',null,undefined])assert.throws(()=>check({...diffResponse,textEncoding}));
 for(const field of ['undecodableBytes','escapedControls']){
  assert.throws(()=>check({...diffResponse,[field]:1}));
  for(const count of [1,8388608])for(const truncated of [false,true])assert.doesNotThrow(()=>check({...diffResponse,[field]:count,textEncoding:'utf-8-visible-escapes',truncated,sourceUtf8Bytes:truncated?4:3}));
 }
});

import {parseChangelistTarget,validateChangelistPlan} from '../web/model.js';
test('changelist names trim only, preserve exact Unicode and case, and reject controls/oversize',()=>{
 assert.equal(parseChangelistTarget('assign','  用户 UI <标签>  '),'用户 UI <标签>');
 assert.equal(parseChangelistTarget('assign','😀'.repeat(128)),'😀'.repeat(128));
 for(const name of ['', '  ', 'a\nb','a\x00b','a\x7fb','a\x85b','\ud800','\udc00','😀'.repeat(129)])assert.throws(()=>parseChangelistTarget('assign',name));
 assert.equal(parseChangelistTarget('remove','ignored'),null);assert.throws(()=>parseChangelistTarget('delete','x'));
});
test('changelist preflight binds exact IDs, full paths, source labels, action, target, scope and expiry',()=>{
 const rows=[{id:'one',path:'folder/a.txt',changelist:'old'},{id:'two',path:'hidden/b.txt',changelist:null}];
 const request={workspaceId:'a',selectionVersion:1,snapshotToken:'snapshot',ids:['one','two'],action:'assign',name:'new'};
 const plan={...request,mode:'live',planToken:'opaque',expiresIn:120,localOnly:true,files:rows.map(r=>({id:r.id,path:r.path,fromChangelist:r.changelist}))};
 assert.equal(validateChangelistPlan(plan,request,rows),plan);
 for(const patch of [{workspaceId:'b'},{selectionVersion:2},{snapshotToken:'stale'},{action:'remove'},{name:'New'},{mode:'demo'},{planToken:''},{planToken:'x'.repeat(65)},{planToken:'x\ny'},{expiresIn:121},{localOnly:false},{files:plan.files.slice(1)},{files:[plan.files[0],plan.files[0]]},{files:[{...plan.files[0],path:'other'},plan.files[1]]},{files:[{...plan.files[0],fromChangelist:'unknown'},plan.files[1]]}])assert.throws(()=>validateChangelistPlan({...plan,...patch},request,rows));
 assert.throws(()=>validateChangelistPlan(plan,{...request,ids:['one','one']},rows));
});
test('moving final file removes named group; removal uses default group without expanding paths',()=>{
 const rows=[{id:'a',path:'dir/a',changelist:'old'},{id:'b',path:'dir/b',changelist:null}];
 const selected=new Set(['a']);const moved=rows.map(r=>selected.has(r.id)?{...r,changelist:'new'}:r);
 assert.deepEqual(groupsFor(moved).map(([n])=>n),['','new']);assert.equal(moved[1],rows[1]);
 assert.deepEqual(groupsFor(moved.map(r=>({...r,changelist:null}))).map(([n])=>n),['']);
});
import {canRelabelFile} from '../web/model.js';
test('known unsupported changelist rows are disabled before preflight; unknown kind requires server validation',()=>{
 const row={item:'modified',props:'none',kind:'unknown'};assert.equal(canRelabelFile(row),true);
 for(const item of ['conflicted','missing','obstructed','unversioned','ignored','external','incomplete'])assert.equal(canRelabelFile({...row,item}),false);
 for(const key of ['switched','file_external','tree_conflicted','wc_locked'])assert.equal(canRelabelFile({...row,[key]:true}),false);
 assert.equal(canRelabelFile({...row,props:'conflicted'}),false);assert.equal(canRelabelFile({...row,kind:'dir'}),false);
});
