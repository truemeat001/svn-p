// Independent controller review: synthetic DOM + API only, no browser or SVN.
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import * as model from '../web/model.js';
const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const controller=source.slice(source.indexOf('let fileOpGeneration='),source.indexOf('let addGeneration='));

function harness(){
 const nodes=new Map(),calls=[],row={id:'src/original@x',path:'src/original@x',item:'normal',props:'none'};
 const state={mode:'live',fileOpsEnabled:true,busy:false,failed:false,rows:[row],selected:new Set([row.id]),workspaceId:'w',selectionVersion:1,snapshotToken:'s'};
 const $=id=>{if(!nodes.has(id))nodes.set(id,{disabled:false,open:false,value:'',textContent:'',innerHTML:'',classList:{add(){},remove(){},toggle(){}}});return nodes.get(id);};
 const scope=()=>({workspaceId:state.workspaceId,selectionVersion:state.selectionVersion,snapshotToken:state.snapshotToken});
 const plan=b=>({...b,mode:'live',localOnly:true,commitRequired:true,expiresIn:120,planToken:'p',source:row,files:[row],affectedPaths:[row.path,b.destination]});
 const result=()=>({...scope(),mode:'live',fileOpsEnabled:true,statusRefreshed:true,snapshotToken:'fresh',rows:[row,{...row,id:'dest/copied@x',path:'dest/copied@x',item:'added',copied:true}],fileOpResult:{action:'copy',files:[row.path],destination:'dest/copied@x',affectedPaths:[row.path,'dest/copied@x'],outcome:'verified',verified:true,commitRequired:true,warnings:[]}});
 let respond=async(path,b)=>path.endsWith('preflight')?plan(b):result();
 const context=vm.createContext({...model,Date,state,$,esc:model.safeText,modalView:'',selectionKey:()=>JSON.stringify([...state.selected]),shelfScope:scope,setTimeout(){},
  modal:(title,content)=>{$('modal').open=true;$('modal-content').innerHTML=content;context.modalView='generic';vm.runInContext('invalidateFileOp()',context);},
  notify:m=>calls.push(['notify',m]),log:m=>calls.push(['log',m]),invalidateReads(){},renderPreview(){},render(){},setBusy:b=>{state.busy=b;},
  failSnapshot:()=>{state.failed=true;state.rows=[];state.snapshotToken=null;vm.runInContext('invalidateFileOp()',context);},
  applySnapshot:p=>{Object.assign(state,p);vm.runInContext('invalidateFileOp()',context);},
  api:async(path,b)=>{calls.push([path,b]);return respond(path,b);}
 });
 vm.runInContext(controller,context);
 const run=s=>vm.runInContext(s,context);
 return {state,$,calls,row,context,plan,result,run,respond:fn=>respond=fn,open:()=>{run("openFileOp('copy')");$('file-op-destination').value='dest/copied@x';}};
}

test('copy controller reviews explicit copy, preserves source, and commits only copy destination',async()=>{
 const h=harness();h.open();assert.match(h.$('modal-content').innerHTML,/源文件保持原状/);
 await h.run('preflightFileOp()');
 const sent=h.calls.find(c=>c[0]==='/api/file-op-preflight')[1];
 assert.equal(sent.action,'copy');assert.equal(sent.id,h.row.id);assert.equal(sent.destination,'dest/copied@x');
 await h.run('applyFileOp()');
 assert.equal(h.state.rows.find(r=>r.id===h.row.id).item,'normal');
 assert.equal(h.state.rows.find(r=>r.id==='dest/copied@x').copied,true);
 assert.match(h.$('file-op-last-result').textContent,/已核实本地带历史复制完成/);
 assert.match(h.$('file-op-last-result').textContent,/提交复制目标/);
 assert.doesNotMatch(h.$('file-op-last-result').textContent,/提交移动两端/);
});

test('late copy result is retained without applying rows from a prior workspace',async()=>{
 const h=harness();h.open();await h.run('preflightFileOp()');
 let resolve;h.respond(()=>new Promise(r=>resolve=r));
 const payload=h.result(),pending=h.run('applyFileOp()');
 h.state.workspaceId='other';h.state.selectionVersion=2;h.state.snapshotToken='elsewhere';
 resolve(payload);await pending;
 assert.equal(h.state.workspaceId,'other');assert.equal(h.state.selectionVersion,2);
 assert.equal(h.state.rows.length,0);
 assert.match(h.$('file-op-last-result').textContent,/已核实本地带历史复制完成/);
 assert.match(h.$('file-op-last-result').textContent,/显式刷新/);
 assert.equal(h.calls.filter(c=>c[0]==='/api/file-op-apply').length,1);
});

test('verified copy survives errors in both snapshot adoption and failure rendering',async()=>{
 const h=harness();h.open();await h.run('preflightFileOp()');
 h.context.applySnapshot=()=>{throw Error('snapshot adoption render failed');};
 h.context.failSnapshot=()=>{h.state.rows=[];h.state.snapshotToken=null;throw Error('failure-state rendering also failed');};
 await h.run('applyFileOp()').catch(()=>{});
 assert.match(h.$('file-op-last-result').textContent,/已核实本地带历史复制完成/);
 assert.doesNotMatch(h.$('file-op-last-result').textContent,/结果未知/);
 assert.equal(h.calls.filter(c=>c[0]==='/api/file-op-apply').length,1);
});

test('late copy preflight cannot confirm a newer move dialog',async()=>{
 const h=harness();h.open();let resolve,request;
 h.respond((path,b)=>{request=b;return new Promise(r=>resolve=r);});
 const pending=h.run('preflightFileOp()');
 h.$('modal').open=false;h.run('invalidateFileOp()');
 h.state.busy=false;h.run("openFileOp('move')");h.$('file-op-destination').value='dest/moved@x';
 resolve(h.plan(request));await pending;
 assert.equal(h.$('file-op-confirm').disabled,true);
 await h.run('applyFileOp()');
 assert.equal(h.calls.filter(c=>c[0]==='/api/file-op-apply').length,0);
});
