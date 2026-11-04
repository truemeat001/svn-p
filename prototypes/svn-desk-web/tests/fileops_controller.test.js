// Actual controller functions in a DOM adapter; not real browser or SVN acceptance.
import test from 'node:test';import assert from 'node:assert/strict';import vm from 'node:vm';import {readFileSync} from 'node:fs';import * as model from '../web/model.js';
const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const controller=source.slice(source.indexOf('let fileOpGeneration='),source.indexOf('let addGeneration='));
function harness(){
 const elements=new Map(),calls=[],timers=[],row={id:'src/a',path:'src/<img>',item:'modified',props:'modified'};
 const state={mode:'live',fileOpsEnabled:true,busy:false,failed:false,rows:[row],selected:new Set([row.id]),workspaceId:'w',selectionVersion:1,snapshotToken:'s'};
 function $(id){if(!elements.has(id))elements.set(id,{disabled:false,open:false,value:'',textContent:'',innerHTML:'',classList:{add(){},remove(){},toggle(){}}});return elements.get(id);}
 const scope=()=>({workspaceId:state.workspaceId,selectionVersion:state.selectionVersion,snapshotToken:state.snapshotToken});
 const plan=b=>({...b,mode:'live',localOnly:true,commitRequired:true,expiresIn:120,planToken:'p',source:row,files:[row],affectedPaths:[row.path,b.destination]});
 const result=b=>({...scope(),mode:'live',fileOpsEnabled:true,statusRefreshed:true,snapshotToken:'s2',rows:[{...row,item:'deleted'}],fileOpResult:{action:'move',files:[row.path],destination:'dest/new@x',affectedPaths:[row.path,'dest/new@x'],outcome:'verified',verified:true,commitRequired:true,warnings:[]}});
 let response=async(path,b)=>path.endsWith('preflight')?plan(b):result(b);
 const context=vm.createContext({...model,Date,state,$,esc:model.safeText,modalView:'',selectionKey:()=>JSON.stringify([...state.selected]),shelfScope:scope,setTimeout:fn=>timers.push(fn),modal:(t,c)=>{$('modal').open=true;$('modal-content').innerHTML=c;context.modalView='generic';vm.runInContext('invalidateFileOp()',context);},notify:m=>calls.push(['notify',m]),log:m=>calls.push(['log',m]),invalidateReads(){},renderPreview(){},render(){},setBusy:b=>state.busy=b,failSnapshot:m=>{state.failed=true;state.rows=[];state.snapshotToken=null;vm.runInContext('invalidateFileOp()',context);calls.push(['failed',m]);},applySnapshot:p=>{Object.assign(state,p);vm.runInContext('invalidateFileOp()',context);},api:async(path,body)=>{calls.push([path,body]);return await response(path,body);}});
 vm.runInContext(controller,context);return {state,$,calls,timers,context,plan,result,run:s=>vm.runInContext(s,context),respond:fn=>response=fn,open:()=>{vm.runInContext("openFileOp('move')",context);$('file-op-destination').value='dest/new@x';}};
}
test('opening has no requests; escaped source and destination review; confirmation dispatches exactly once',async()=>{
 const h=harness();h.open();assert.equal(h.calls.length,0);assert.match(h.$('modal-content').innerHTML,/&lt;img&gt;/);assert.match(h.$('modal-content').innerHTML,/原版可信 SVN 客户端/);await h.run('preflightFileOp()');assert.equal(h.calls[0][1].id,'src/a');assert.equal(h.calls[0][1].destination,'dest/new@x');assert.equal(h.calls.filter(c=>c[0].includes('apply')).length,0);await h.run('applyFileOp()');await h.run('applyFileOp()');assert.equal(h.calls.filter(c=>c[0].includes('apply')).length,1);assert.match(h.$('file-op-last-result').textContent,/已核实本地移动/);assert.match(h.$('file-op-result').textContent,/已核实本地移动/);
});
test('demo, disabled capability, multiple/hidden/unsafe source cannot issue requests',async()=>{
 for(const edit of [h=>h.state.mode='demo',h=>h.state.fileOpsEnabled=false,h=>h.state.selected.add('hidden'),h=>h.state.rows[0].kind='dir']){const h=harness();edit(h);h.open();await h.run('preflightFileOp()');assert(!h.calls.some(c=>c[0].startsWith('/api/')));}
});
test('editing destination, scope, selected source, expiry and dismissal invalidate confirmation',async()=>{
 for(const change of [h=>{h.$('file-op-destination').value='new';h.$('file-op-destination').oninput();},h=>h.state.snapshotToken='new',h=>h.state.selected.add('hidden'),h=>h.timers[0](),h=>{h.$('modal').open=false;h.run('invalidateFileOp()');}]){const h=harness();h.open();await h.run('preflightFileOp()');change(h);await h.run('applyFileOp()');assert.equal(h.calls.filter(c=>c[0].includes('apply')).length,0);}
});
test('late preflight after edit or dismissal cannot reenable confirmation',async()=>{
 for(const close of [true,false]){const h=harness();let resolve;h.respond(()=>new Promise(r=>resolve=r));h.open();const pending=h.run('preflightFileOp()');if(close)h.$('modal').open=false;else h.$('file-op-destination').value='later';h.run('invalidateFileOp()');resolve({});await pending;assert.equal(h.$('file-op-confirm').disabled,true);}
});
test('network or malformed write result clears snapshot, never retries',async()=>{
 for(const type of ['network','malformed']){const h=harness();h.open();await h.run('preflightFileOp()');h.respond(()=>{if(type==='network')throw Error('network');return {mode:'demo'};});await h.run('applyFileOp()');assert.equal(h.state.rows.length,0);assert.match(h.$('file-op-last-result').textContent,/结果未知/);await h.run('applyFileOp()');assert.equal(h.calls.filter(c=>c[0].includes('apply')).length,1);}
});
test('rejected request is not claimed as an attempted native mutation',async()=>{const h=harness();h.open();await h.run('preflightFileOp()');h.respond(()=>{throw Object.assign(Error('reject'),{httpStatus:409});});await h.run('applyFileOp()');assert.match(h.$('file-op-last-result').textContent,/执行前被拒绝/);});
test('double confirm is one dispatch and completed result survives close/failed snapshot',async()=>{
 const h=harness();h.open();await h.run('preflightFileOp()');let resolve;h.respond(()=>new Promise(r=>resolve=r));const pending=h.run('applyFileOp()');await h.run('applyFileOp()');h.$('modal').open=false;h.run('invalidateFileOp()');const r=h.result();r.statusRefreshed=false;r.snapshotToken=null;r.rows=[];r.fileOpResult.warnings=['status-refresh-failed'];resolve(r);await pending;assert.equal(h.calls.filter(c=>c[0].includes('apply')).length,1);assert.match(h.$('file-op-last-result').textContent,/已核实本地移动/);assert.equal(h.$('modal').open,false);assert.equal(h.state.rows.length,0);
});
test('verified result survives subsequent render error',async()=>{const h=harness();h.open();await h.run('preflightFileOp()');h.context.applySnapshot=()=>{throw Error('render');};await h.run('applyFileOp()');assert.match(h.$('file-op-last-result').textContent,/已核实本地移动/);assert.doesNotMatch(h.$('file-op-last-result').textContent,/结果未知/);});
test('lifecycle invalidates plans and capability is sourced only from status',()=>{
 for(const marker of ['function resetWorkspaceScope()', 'function failSnapshot(message)', "$('modal').addEventListener('cancel'", "$('modal').addEventListener('close'", 'function modal(title,content)']){const line=source.split('\n').find(l=>l.startsWith(marker));assert.match(line,/invalidateFileOp\(\)/);}
 assert.match(source,/fileOpsEnabled:payload.fileOpsEnabled===true/);assert.match(source,/state.fileOpsEnabled=false/);
});
