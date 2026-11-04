import test from 'node:test';
import assert from 'node:assert/strict';
import {commitScopeRows,parseCommitMessage,validateCommitPlan} from '../web/model.js';
const rows=[{id:'a',path:'visible/a',item:'modified',props:'none',changelist:'work'},{id:'b',path:'hidden/b',item:'normal',props:'none',changelist:'work'}];
const request={workspaceId:'w',selectionVersion:1,snapshotToken:'s',operation:'selected',ids:['a'],name:null,message:'fix',keepChangelist:false};
const result={...request,mode:'live',planToken:'p',expiresIn:120,files:[{...rows[0],fromChangelist:'work'}]};
test('selected exact versus named complete scope includes clean hidden members',()=>{assert.deepEqual(commitScopeRows(rows,new Set(['a'])),[rows[0]]);assert.deepEqual(commitScopeRows(rows,new Set(['a']),'changelist','work'),rows);assert.throws(()=>commitScopeRows(Array.from({length:101},(_,i)=>({...rows[0],id:String(i)})),new Set(),'changelist','work'));assert.throws(()=>commitScopeRows(rows,new Set(['unknown'])));});
test('message UTF-8 bound and invalid Unicode',()=>{assert.equal(parseCommitMessage('中'.repeat(2730)).length,2730);for(const m of ['', '  ', '中'.repeat(2731), '\u0000', '\u0001', '\u007f', '\u0085', '\ud800'])assert.throws(()=>parseCommitMessage(m));assert.equal(parseCommitMessage('😀'),'😀');});
test('plan exact identity and immutable scope',()=>{assert.equal(validateCommitPlan(result,request,rows),result);for(const patch of [{keepChangelist:true},{operation:'changelist'},{name:'work'},{snapshotToken:'wrong'},{expiresIn:999},{files:[...result.files,...result.files]},{files:[{...result.files[0],props:'modified'}]}])assert.throws(()=>validateCommitPlan({...result,...patch},request,rows));assert.throws(()=>validateCommitPlan({...result,operation:'changelist',name:'work'},{...request,operation:'changelist',name:'work'},rows));});

import {validateCommitResult,commitResultSummary} from '../web/model.js';
const completed={mode:'live',workspaceId:'w',selectionVersion:1,statusRefreshed:true,snapshotToken:'s2',rows:[],commitResult:{operation:'selected',name:null,keepChangelist:false,commandCompleted:true,files:['visible/a'],outcome:'committed',reportedRevision:'3',warnings:[],automaticRetryAllowed:false}};
const withResult=patch=>({...completed,commitResult:{...completed.commitResult,...patch}});
test('commit result validates immutable scope and reports revision without claiming independent verification',()=>{
 assert.equal(validateCommitResult(completed,request,result.files),completed.commitResult);
 assert.match(commitResultSummary(completed.commitResult,{statusRefreshed:true}),/SVN报告已提交r3/);
 assert.match(commitResultSummary(completed.commitResult),/尚未独立核实仓库历史/);
 for(const patch of [{workspaceId:'other'},{selectionVersion:2},{mode:'demo'},{statusRefreshed:undefined},{snapshotToken:'s'},{rows:null},{rows:[{}]}])assert.throws(()=>validateCommitResult({...completed,...patch},request,result.files));
 for(const patch of [{operation:'changelist'},{name:'work'},{keepChangelist:true},{commandCompleted:false},{automaticRetryAllowed:true},{automaticRetryAllowed:undefined},{files:['hidden/b']},{files:['visible/a','visible/a']},{warnings:['raw server text']},{warnings:['toString']},{warnings:['post-commit-hook','post-commit-hook']},{outcome:'success'},{reportedRevision:null},{outcome:'completed-unverified'}])assert.throws(()=>validateCommitResult(withResult(patch),request,result.files));
});
test('revision is strict positive ASCII signed-64 range and remains a string',()=>{
 for(const value of ['0','03','-1','1.0','1\n','３','9223372036854775808',3,undefined,'','<img>'])assert.throws(()=>validateCommitResult(withResult({reportedRevision:value}),request,result.files));
 for(const value of ['1','9223372036854775807'])assert.equal(validateCommitResult(withResult({reportedRevision:value}),request,result.files).reportedRevision,value);
});
test('post-commit hook warning is prominent and never invites resubmission',()=>{
 const payload=withResult({warnings:['post-commit-hook','additional-output']});validateCommitResult(payload,request,result.files);
 assert.match(commitResultSummary(payload.commitResult,{statusRefreshed:true}),/^SVN报告已提交r3，但提交后钩子执行失败，勿重复提交。/);
 assert.match(commitResultSummary(payload.commitResult),/额外诊断信息/);
});
test('localized or unknown completion requires history check and no automatic retry',()=>{
 const payload=withResult({outcome:'completed-unverified',reportedRevision:null,warnings:['unrecognized-output']});validateCommitResult(payload,request,result.files);
 const summary=commitResultSummary(payload.commitResult,{statusRefreshed:true});assert.match(summary,/提交命令已完成/);assert.match(summary,/检查仓库历史确认结果，勿重复提交/);assert.match(summary,/不会自动重试/);assert.doesNotMatch(summary,/报告已提交r/);
});
test('refresh failure keeps reported completion and requires empty invalidated state',()=>{
 const payload={...withResult({warnings:['post-commit-hook','status-refresh-failed']}),statusRefreshed:false,snapshotToken:null};
 validateCommitResult(payload,request,result.files);const summary=commitResultSummary(payload.commitResult);assert.match(summary,/SVN报告已提交r3，但提交后钩子执行失败/);assert.match(summary,/显式刷新/);assert.doesNotMatch(summary,/提交结果未知/);
 for(const patch of [{snapshotToken:'new'},{rows:[rows[0]]},{statusRefreshed:true}])assert.throws(()=>validateCommitResult({...payload,...patch},request,result.files));
 assert.match(commitResultSummary(completed.commitResult,{statusRefreshed:true,stale:true}),/未采用返回状态；请显式刷新/);
});
