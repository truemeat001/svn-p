// Actual property and read lifecycle controller source, DOM adapter only.
import test from 'node:test';import assert from 'node:assert/strict';import vm from 'node:vm';import {readFileSync} from 'node:fs';import * as model from '../web/model.js';
const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const lifecycle=source.slice(source.indexOf("let diffFrom='BASE'"),source.indexOf('function readsAvailable('));
const controller=source.slice(source.indexOf('function propertyReadsAvailable('),source.indexOf('function editReadRevision('));
function harness(){
 const elements=new Map(),calls=[];
 const state={mode:'live',busy:false,failed:false,propertyReadsEnabled:true,rows:[{id:'a',path:'src/<img>.txt',item:'modified',props:'modified'}],focused:'a',preview:'properties',workspaceId:'w',selectionVersion:1,snapshotToken:'s'};
 const $=id=>{if(!elements.has(id))elements.set(id,{open:false,innerHTML:'',disabled:false,classList:{remove(){},add(){}},close(){this.open=false;}});return elements.get(id);};
 let respond=async(path,body)=>({...body,path:'src/<img>.txt',mode:'live',localOnly:true,displayOnly:true,baseAvailable:true,baseUnavailableReason:null,properties:[{name:'test:<b>',change:'added',base:null,working:{kind:'text',text:'<script>private-value</script>',byteLength:30,sha256:'a'.repeat(64),textTruncated:false,redacted:false,escapedControls:0}}]});
 const context=vm.createContext({...model,TextEncoder,state,$,esc:model.safeText,modalView:'',renderPreview:()=>calls.push(['render']),log:m=>calls.push(['log',m]),api:async(path,body)=>{calls.push([path,body]);return respond(path,body);}});
 vm.runInContext(lifecycle+controller,context);
 return {state,$,calls,context,run:s=>vm.runInContext(s,context),respond:fn=>respond=fn};
}
test('opening is request-free; one explicit local request and escaped property values',async()=>{
 const h=harness();h.run('showProperties()');assert(!h.calls.some(c=>c[0].startsWith('/api/')));await h.run('requestProperties()');assert.deepEqual(Object.keys(h.calls.find(c=>c[0]==='/api/file-properties')[1]).sort(),['id','selectionVersion','snapshotToken','workspaceId']);h.run('renderProperties()');assert.match(h.$('diff-content').innerHTML,/&lt;script&gt;private-value&lt;\/script&gt;/);assert.match(h.$('diff-content').innerHTML,/test:&lt;b&gt;/);assert(!h.calls.filter(c=>c[0]==='log').some(c=>c[1].includes('private-value')));
});
test('demo, missing capability, unsupported rows and invalid state refuse request',async()=>{
 for(const edit of [h=>h.state.mode='demo',h=>h.state.propertyReadsEnabled=false,h=>h.state.rows[0].item='unversioned',h=>h.state.rows[0].wc_locked=true,h=>h.state.busy=true,h=>h.state.failed=true,h=>h.state.snapshotToken=null,h=>h.state.preview='diff']){const h=harness();edit(h);await h.run('requestProperties()');assert.equal(h.calls.length,0);}
});
test('duplicate clicks share one pending request',async()=>{
 const h=harness();let resolve;h.respond(()=>new Promise(r=>resolve=r));const first=h.run('requestProperties()');await h.run('requestProperties()');assert.equal(h.calls.filter(c=>c[0].startsWith('/api/')).length,1);resolve({});await first;
});
test('focus, scope, preview and lifecycle invalidation discard late responses',async()=>{
 for(const edit of [h=>h.state.focused='missing',h=>h.state.snapshotToken='new',h=>h.state.workspaceId='other',h=>h.state.preview='diff',h=>h.run('invalidateReads()')]){
  const h=harness();let resolve;h.respond(()=>new Promise(r=>resolve=r));const pending=h.run('requestProperties()');edit(h);resolve({});await pending;assert.equal(h.run('propertyResult'),null);assert.equal(h.run('propertyError'),'');
 }
});
test('failed or malformed responses show unknown properties and clear earlier result',async()=>{
 for(const value of ['error','bad']){const h=harness();await h.run('requestProperties()');assert(h.run('propertyResult'));h.respond(()=>{if(value==='error')throw Error('Read failed');return {mode:'demo'};});await h.run('requestProperties()');h.run('renderProperties()');assert.equal(h.run('propertyResult'),null);assert.match(h.$('diff-content').innerHTML,/失败不代表没有属性/);assert(!h.$('diff-content').innerHTML.includes('private-value'));}
});
test('shared invalidation clears property state and opening another preview resets it',async()=>{
 const h=harness();await h.run('requestProperties()');h.run('invalidateReads()');assert.equal(h.run('propertyResult'),null);assert.equal(h.run('propertyError'),'');assert.equal(h.run('readPending'),false);
 for(const pattern of [/data-preview.*invalidateReads\(\)/,/toggle-preview.*invalidateReads\(\)/,/function resetWorkspaceScope\(\).*invalidateReads\(\)/,/function failSnapshot\(message\).*invalidateReads\(\)/,/if\(lastFocus!==state.focused\)\{invalidateReads\(\)/])assert.match(source,pattern);
});
test('binary display omits content and marks display-only preview limitations',()=>{
 const h=harness();const html=h.run(`propertyValueMarkup({kind:'binary',text:null,byteLength:8,sha256:'a'.repeat(64),textTruncated:false,redacted:false,escapedControls:0})`);assert.match(html,/二进制值，不显示负载/);assert.match(html,/SHA-256/);assert(!html.includes('property-value-text'));
});

test('modified status cannot be accepted as a new file with missing BASE',async()=>{
 const h=harness();h.respond(async(path,body)=>({...body,path:'src/<img>.txt',mode:'live',localOnly:true,displayOnly:true,baseAvailable:false,baseUnavailableReason:'added-file',properties:[]}));await h.run('requestProperties()');assert.equal(h.run('propertyResult'),null);assert.match(h.run('propertyError'),/属性响应/);
});
