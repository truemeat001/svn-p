import test from 'node:test';import assert from 'node:assert/strict';
import {fileOpSource,parseFileDestination,validateFileOpPlan,validateFileOpResult,fileOpResultSummary} from '../web/model.js';
const rows=[{id:'dir/a@x',path:'dir/a@x',item:'modified',props:'modified'},{id:'hidden',path:'hidden',item:'normal',props:'none'}];
const request={workspaceId:'w',selectionVersion:1,snapshotToken:'s',action:'move',id:rows[0].id,destination:'dir/new@name'};
const source=rows[0],plan={...request,mode:'live',source,files:[source],affectedPaths:[source.path,request.destination],localOnly:true,commitRequired:true,planToken:'p',expiresIn:120};
const payload={mode:'live',workspaceId:'w',selectionVersion:1,statusRefreshed:true,snapshotToken:'new',rows:[],fileOpResult:{action:'move',files:[source.path],destination:request.destination,affectedPaths:plan.affectedPaths,outcome:'verified',verified:true,commitRequired:true,warnings:[]}};
test('exact one source selection never reduces hidden multi-selection or permits structural changes',()=>{
 assert.equal(fileOpSource(rows,new Set([source.id])),source);
 for(const selection of [new Set(),new Set([source.id,'hidden']),new Set(['unknown'])])assert.throws(()=>fileOpSource(rows,selection));
 for(const patch of [{item:'added'},{item:'deleted'},{item:'none'},{item:'unversioned'},{props:'conflicted'},{kind:'dir'},{copied:true},{moved_from:'old'},{moved_to:'new'},{wc_locked:true},{switched:true},{tree_conflicted:true},{file_external:true}])assert.throws(()=>fileOpSource([{...source,...patch}],new Set([source.id])));
 assert.throws(()=>fileOpSource([source,{...source}],new Set([source.id])));
});
test('destination is explicit portable relative file path without silently rewriting input',()=>{
 for(const value of ['dir/new@name','-leading/你好.txt','space folder/new file.txt','é.txt'])assert.equal(parseFileDestination(value,source.path),value);
 for(const value of ['', '/abs', 'C:/abs','a\\b','a/../b','./a','a//b','a/','a/.svn/b','CON.txt','a/Lpt9.foo','a/last.','a/last ','a\n.txt','dir/A@X',null,'x'.repeat(2049),'a\ud800'])assert.throws(()=>parseFileDestination(value,source.path));
});
test('preflight is bound to action, source metadata, destination, affected paths and scope',()=>{
 assert.equal(validateFileOpPlan(plan,request,rows),plan);
 for(const patch of [{action:'copy'},{destination:'other'},{source:{...source,path:'other'}},{files:[source,source]},{affectedPaths:[request.destination,source.path]},{expiresIn:119},{planToken:'p\n'},{localOnly:false},{commitRequired:false},{workspaceId:'x'},{selectionVersion:2},{snapshotToken:'x'}])assert.throws(()=>validateFileOpPlan({...plan,...patch},request,rows));
 assert.throws(()=>validateFileOpPlan(plan,{...request,action:'delete'},rows));
});
test('verified result preserves exact source/destination and refuses forged completion',()=>{
 assert.equal(validateFileOpResult(payload,request,plan),payload.fileOpResult);
 for(const patch of [{action:'copy'},{destination:'other'},{files:['other']},{affectedPaths:[source.path,'other']},{verified:false},{outcome:'completed'},{commitRequired:false},{warnings:['toString']},{warnings:['private diagnostic']}])assert.throws(()=>validateFileOpResult({...payload,fileOpResult:{...payload.fileOpResult,...patch}},request,plan));
 for(const patch of [{workspaceId:'other'},{selectionVersion:2},{snapshotToken:'s'},{rows:null},{statusRefreshed:false}])assert.throws(()=>validateFileOpResult({...payload,...patch},request,plan));
});
test('verified completion survives unavailable status and explains original-client commit boundary',()=>{
 const failed={...payload,statusRefreshed:false,snapshotToken:null,rows:[],fileOpResult:{...payload.fileOpResult,warnings:['status-refresh-failed']}};
 const r=validateFileOpResult(failed,request,plan),text=fileOpResultSummary(r);
 assert.match(text,/已核实本地移动／重命名完成/);assert.match(text,/尚未提交/);assert.match(text,/原版可信 SVN 客户端/);assert.match(text,/移动两端/);assert.match(text,/显式刷新/);
 assert.throws(()=>validateFileOpResult({...failed,fileOpResult:{...r,warnings:[]}},request,plan));
 assert.match(fileOpResultSummary({...r,action:'copy'}),/复制目标/);
});
