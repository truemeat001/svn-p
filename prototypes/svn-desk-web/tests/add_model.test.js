import test from 'node:test';
import assert from 'node:assert/strict';
import {addScopeRows,validateAddPlan,validateAddResult,addResultSummary} from '../web/model.js';
const rows=[{id:'a',path:'shown/a',item:'unversioned',props:'none'},{id:'b',path:'hidden/b',item:'unversioned',props:'none'}];
const request={workspaceId:'w',selectionVersion:1,snapshotToken:'s',ids:['a','b']};
const plan={...request,mode:'live',planToken:'p',expiresIn:120,localOnly:true,autoProps:false,files:rows.map(({id,path})=>({id,path}))};
const payload={mode:'live',workspaceId:'w',selectionVersion:1,statusRefreshed:true,snapshotToken:'s2',rows:rows.map(r=>({...r,item:'added'})),addResult:{action:'add',files:rows.map(r=>r.path),outcome:'verified',verified:true,autoProps:false,warnings:[]}};
test('add exact selection preserves hidden files and rejects unsafe or mixed selections',()=>{
 assert.deepEqual(addScopeRows(rows,new Set(['a','b'])),rows);
 for(const patch of [{item:'added'},{item:'normal'},{item:'ignored'},{props:'conflicted'},{changelist:'x'},{kind:'dir'},{wc_locked:true},{file_external:true},{switched:true},{copied:true},{moved_from:'old'},{moved_to:'new'},{tree_conflicted:true}])assert.throws(()=>addScopeRows([{...rows[0],...patch}],new Set(['a'])));
 assert.throws(()=>addScopeRows(rows,new Set(['a','unknown'])));assert.throws(()=>addScopeRows(rows,new Set()));
 assert.throws(()=>addScopeRows([rows[0],{...rows[1],id:'A'}],new Set(['a','A'])));
 const many=Array.from({length:101},(_,i)=>({...rows[0],id:String(i)}));assert.throws(()=>addScopeRows(many,new Set(many.map(r=>r.id))));
});
test('plan must bind local-only operation, disabled auto-props, exact identity and TTL',()=>{
 assert.equal(validateAddPlan(plan,request,rows),plan);
 for(const patch of [{mode:'demo'},{localOnly:false},{autoProps:true},{expiresIn:119},{snapshotToken:'new'},{workspaceId:'other'},{selectionVersion:2},{planToken:'a'.repeat(65)},{planToken:'\n'},{files:[plan.files[0],plan.files[0]]},{files:[plan.files[0]]},{files:[{id:'a',path:'other'},plan.files[1]]}])assert.throws(()=>validateAddPlan({...plan,...patch},request,rows));
 assert.throws(()=>validateAddPlan(plan,{...request,ids:['a','a']},rows));
});
test('verified result requires exact paths and known warning codes',()=>{
 assert.equal(validateAddResult(payload,request,plan.files),payload.addResult);
 for(const patch of [{action:'commit'},{outcome:'success'},{verified:false},{autoProps:true},{files:['shown/a']},{files:['shown/a','shown/a']},{warnings:['toString']},{warnings:['raw diagnostic']}])assert.throws(()=>validateAddResult({...payload,addResult:{...payload.addResult,...patch}},request,plan.files));
 for(const patch of [{workspaceId:'x'},{selectionVersion:2},{snapshotToken:'s'},{statusRefreshed:false},{rows:null}])assert.throws(()=>validateAddResult({...payload,...patch},request,plan.files));
});
test('verified add survives failed refresh but does not claim repository submission',()=>{
 const failed={...payload,statusRefreshed:false,snapshotToken:null,rows:[],addResult:{...payload.addResult,warnings:['status-refresh-failed']}};
 const result=validateAddResult(failed,request,plan.files),summary=addResultSummary(result);
 assert.match(summary,/已核实添加 2 个文件/);assert.match(summary,/尚未提交/);assert.match(summary,/显式刷新/);assert.match(summary,/auto-props/);
 assert.throws(()=>validateAddResult({...failed,addResult:{...failed.addResult,warnings:[]}},request,plan.files));
});
