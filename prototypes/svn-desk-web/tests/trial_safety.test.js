// Release-blocker checks execute the actual app module and actual dialog listeners.
// Synthetic DOM only: no browser layout, native Windows, or SVN acceptance claim.
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import * as model from '../web/model.js';
import {demoRows,demoDiff,demoHistory} from '../web/demo-data.js';

const app=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const executable=app.replace(/^import .*;\n/gm,'').replace(/log\('启动 Web 原型[^\n]+await refresh\(true\);\s*$/,'');

function harness(){
 const elements=new Map(),other=new Map(),calls=[],timers=[];
 function removeTree(node){for(const child of node.children??[])removeTree(child);if(node.id)elements.delete(node.id);}
 class Element{
  constructor(id=''){this.id=id;this.children=[];this.events={};this.dataset={};this.style={};this.disabled=false;this.open=false;this.checked=false;this.value='';this.textContent='';this._html='';const classes=new Set();this.classList={add:c=>classes.add(c),remove:c=>classes.delete(c),contains:c=>classes.has(c),toggle:(c,on)=>{on??=!classes.has(c);on?classes.add(c):classes.delete(c);return on;}};}
  set innerHTML(value){for(const child of this.children)removeTree(child);this.children=[];this._html=value;for(const m of value.matchAll(/<([a-z][\w-]*)\b([^>]*)>/gi)){const id=m[2].match(/\bid="([^"]+)"/);const node=new Element(id?.[1]||'');node.tagName=m[1];for(const attr of m[2].matchAll(/data-([\w-]+)="([^"]*)"/g))node.dataset[attr[1].replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=attr[2];if(id)elements.set(id[1],node);this.children.push(node);} }
  get innerHTML(){return this._html;}
  addEventListener(name,fn){(this.events[name]??=[]).push(fn);}
  emit(name,event={}){for(const fn of this.events[name]??[])fn(event);}
  querySelectorAll(selector){const attr=selector.match(/^\[data-([\w-]+)(?:="([^"]*)")?\]$/);if(!attr)return [];const key=attr[1].replace(/-([a-z])/g,(_,c)=>c.toUpperCase());return this.children.filter(node=>Object.hasOwn(node.dataset,key)&&(attr[2]===undefined||node.dataset[key]===attr[2]));}
  querySelector(selector){const match=this.querySelectorAll(selector)[0];if(match)return match;const key=`${this.id}:${selector}`;if(!other.has(key))other.set(key,new Element(key));return other.get(key);}
  replaceChildren(){this.innerHTML='';}
  removeAttribute(){} setAttribute(name,value){this[name]=value;}
  focus(){document.activeElement=this;} setSelectionRange(){}
  showModal(){this.open=true;}
  close(){if(!this.open)return;this.open=false;this.emit('close');}
  contains(node){return node===this;}
  getBoundingClientRect(){return {left:0,right:100,top:0,bottom:100,height:20};}
 }
 for(const m of html.matchAll(/\bid="([^"]+)"/g))elements.set(m[1],new Element(m[1]));
 const document={activeElement:null,getElementById:id=>elements.get(id)||null,querySelectorAll:selector=>[...elements.values()].flatMap(node=>node.querySelectorAll(selector)),addEventListener(){},querySelector(selector){if(selector==='meta[name="svn-session"]')return {content:'test-session'};if(!other.has(selector))other.set(selector,new Element(selector));return other.get(selector);}};
 const context=vm.createContext({...model,esc:model.safeText,demoRows,demoDiff,demoHistory,document,window:{addEventListener(){}},innerWidth:1400,innerHeight:900,TextEncoder,structuredClone,Date,setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},clearTimeout(){},fetch(){throw Error('Unexpected real fetch');}});
 vm.runInContext(executable,context);
 const run=code=>vm.runInContext(code,context);
 const state=run('state');
 const rows=[{id:'a',path:'visible/a.txt',item:'modified',props:'none',kind:'file',changelist:'work',revision:'1'},{id:'b',path:'hidden/b.txt',item:'modified',props:'none',kind:'file',changelist:'work',revision:'1'}];
 Object.assign(state,{mode:'live',rows,query:'visible',selected:new Set(['a','b']),focused:'a',workspaceId:'w',selectionVersion:1,snapshotToken:'s',workspace:'Trial',root:'C:\\Trial',fileReadsEnabled:true,propertyReadsEnabled:true,changelistWritesEnabled:true,shelfWritesEnabled:true,commitsEnabled:true,locksEnabled:true,addsEnabled:true,fileOpsEnabled:true});
 let responder=async()=>{throw Error('Missing response fixture');};
 context.respond=async(path,body)=>{calls.push({path,body:structuredClone(body)});return responder(path,body);};
 run('api=respond;render();');
 const $=id=>elements.get(id)||null;
 const snapshot=()=>({mode:'live',workspaceId:'w',selectionVersion:1,snapshotToken:'s2',rows:structuredClone(rows),statusRefreshed:true,fileReadsEnabled:true,propertyReadsEnabled:true,changelistWritesEnabled:true,shelfWritesEnabled:true,commitsEnabled:true,locksEnabled:true,addsEnabled:true,fileOpsEnabled:true});
 const plan=(kind,request)=>{
  const files=rows.filter(row=>request.operation==='restore'||request.ids?.includes(row.id)).map(row=>({...row,fromChangelist:row.changelist||null}));
  const base={...request,mode:'live',planToken:'plan',expiresIn:120,files};
  if(kind==='shelf')return {...base,name:request.name||'backup',localOnly:true,backupOnly:request.operation==='create',totalBytes:files.length,files:files.map(row=>({...row,state:row.item,size:1}))};
  if(kind==='changelist')return {...base,localOnly:true};
  if(kind==='lock')return {...base,contactsRepository:true,files:files.map(row=>({...row,locked:request.action==='unlock',ownedByWorkingCopy:request.action==='unlock',localTokenPresent:request.action==='unlock',staleLocalToken:false,workingRevision:'1',serverLastChangedRevision:'1'}))};
  if(kind==='add')return {...base,localOnly:true,autoProps:false};
  if(kind==='file-op'){const row=rows.find(row=>row.id===request.id);return {...base,localOnly:true,commitRequired:true,source:row,files:[row],affectedPaths:[row.path,request.destination]};}
  return base;
 };
 const result=(kind,request)=>{
  const base=snapshot(),files=rows.filter(row=>request.operation==='restore'||request.ids?.includes(row.id)).map(row=>row.path);
  if(kind==='shelf')return {...base,shelfResult:{operation:request.operation,name:request.name||'backup',shelfId:request.shelfId||'shelf-id',shelfRetained:true,outcome:'verified',verified:true,warnings:[],files}};
  if(kind==='changelist')return {...base,changelistResult:{action:request.action,name:request.name,count:request.ids.length}};
  if(kind==='commit')return {...base,commitResult:{operation:request.operation,name:request.name,keepChangelist:request.keepChangelist,commandCompleted:true,automaticRetryAllowed:false,outcome:'committed',reportedRevision:'42',files,warnings:[]}};
  if(kind==='lock')return {...base,lockResult:{action:request.action,outcome:'verified',verified:true,files,warnings:[]}};
  if(kind==='add')return {...base,addResult:{action:'add',outcome:'verified',verified:true,autoProps:false,files,warnings:[]}};
  const row=rows.find(row=>row.id===request.id);return {...base,fileOpResult:{action:request.action,outcome:'verified',verified:true,commitRequired:true,files:[row.path],destination:request.destination,affectedPaths:[row.path,request.destination],warnings:[]}};
 };
 return {run,state,rows,$,calls,timers,context,snapshot,plan,result,respond:fn=>responder=fn,logs:()=>state.logs.map(x=>x.message).join('\n')};
}
const flows={
 changelist:{open:h=>{h.run('openChangelists()');h.$('changelist-action').value='assign';h.$('changelist-name').value='review';},preflight:'preflightChangelists()',apply:'applyChangelists()',confirmed:/本地标签修改完成/},
 shelf:{open:h=>{h.run('openCreateShelf()');h.$('shelf-name').value='backup';},preflight:'preflightShelf()',apply:'applyShelf()',confirmed:/本地 Shelf 已创建/},
 commit:{open:h=>{h.run('openCommit()');h.$('commit-message').value='Trial change';},preflight:'preflightCommit()',apply:'applyCommit()',confirmed:/SVN报告已提交r42/},
 lock:{open:h=>h.run("openLock('lock')"),preflight:'preflightLock()',apply:'applyLock()',confirmed:/已核实锁定完成/},
 add:{open:h=>{for(const row of h.rows){row.item='unversioned';row.changelist=null;}h.run('render();openAdd()');},preflight:'preflightAdd()',apply:'applyAdd()',confirmed:/已核实添加 2 个文件/},
 'file-op':{open:h=>{h.state.selected=new Set(['a']);h.run("render();openFileOp('move')");h.$('file-op-destination').value='visible/new.txt';},preflight:'preflightFileOp()',apply:'applyFileOp()',confirmed:/已核实本地移动/},
};
async function ready(kind){
 const h=harness(),flow=flows[kind];let request;
 h.respond(async(path,body)=>{request=body;return h.plan(kind,body);});
 flow.open(h);await h.run(flow.preflight);assert.equal(h.$(`${kind}-confirm`).disabled,false,`${kind} fixture must pass real preflight`);
 return {...h,flow,request};
}

for(const [kind,flow] of Object.entries(flows)){
 test(`${kind}: exact hidden selection, expired review, and double dispatch are guarded`,async()=>{
  const h=await ready(kind);
  if(kind!=='file-op')assert.deepEqual(h.calls[0].body.ids,['a','b']);
  h.respond(async()=>h.result(kind,h.request));
  const pending=h.run(flow.apply);await h.run(flow.apply);await pending;
  assert.equal(h.calls.filter(c=>c.path.endsWith('-apply')).length,1);
  assert.match(h.logs(),flow.confirmed);
  const expired=await ready(kind);expired.timers.find(t=>t.delay===120000).fn();await expired.run(flow.apply);
  assert.equal(expired.calls.filter(c=>c.path.endsWith('-apply')).length,0);
 });
 test(`${kind}: late preflight after actual dialog close cannot enable confirmation`,async()=>{
  const h=harness();let resolve;h.respond(()=>new Promise(r=>resolve=r));flow.open(h);const pending=h.run(flow.preflight);h.$('modal').close();resolve(h.plan(kind,h.calls[0].body));await pending;
  assert.equal(h.$(`${kind}-confirm`).disabled,true);assert.equal(h.$('modal').open,false);assert.equal(h.state.busy,false);
  await h.run(flow.apply);assert.equal(h.calls.length,1);
 });
 test(`${kind}: confirmed completion survives actual dialog close`,async()=>{
  const h=await ready(kind);let resolve;h.respond(()=>new Promise(r=>resolve=r));const pending=h.run(flow.apply);h.$('modal').close();resolve(h.result(kind,h.request));await pending;
  assert.equal(h.$('modal').open,false);assert.equal(h.calls.filter(c=>c.path.endsWith('-apply')).length,1);assert.match(h.logs(),flow.confirmed);
  if(kind!=='changelist'){assert.equal(h.state.snapshotToken,null);assert.match(h.$(`${kind}-last-result`).textContent,flow.confirmed);assert.equal(h.$(`${kind}-last-result`).classList.contains('hidden'),false);}
 });
}
for(const kind of ['commit','lock','add','shelf','file-op'])test(`${kind}: validated completion is not downgraded by later snapshot adoption failure`,async()=>{
 const h=await ready(kind);h.respond(async()=>h.result(kind,h.request));
 h.run("applySnapshot=()=>{throw Error('synthetic snapshot rendering failure');}");
 await h.run(h.flow.apply);assert.match(h.logs(),h.flow.confirmed);assert.doesNotMatch(h.logs(),/未收到可验证/);assert.match(h.$(`${kind}-last-result`).textContent,h.flow.confirmed);if(kind==='lock')assert.equal(h.run('lockInspectionRequired'),false);
});

test('refresh adopts write capabilities only from the matching status, not older config',async()=>{
 const h=harness();h.respond(async path=>path==='/api/config'?h.snapshot():{...h.snapshot(),shelfWritesEnabled:undefined,changelistWritesEnabled:undefined,commitsEnabled:undefined,locksEnabled:undefined,addsEnabled:undefined,fileOpsEnabled:undefined});
 await h.run('refresh()');for(const key of ['shelfWritesEnabled','changelistWritesEnabled','commitsEnabled','locksEnabled','addsEnabled','fileOpsEnabled'])assert.equal(h.state[key],false);
});
test('refresh with changed identity or failed status clears rows, write capabilities, and all preflight plans',async()=>{
 for(const failure of ['changed','network']){const h=await ready('commit');h.respond(async path=>{if(path==='/api/config')return h.snapshot();if(failure==='network')throw Error('server unavailable');return {...h.snapshot(),workspaceId:'new'};});await h.run('refresh()');assert.equal(h.state.failed,true);assert.equal(h.state.rows.length,0);assert.equal(h.state.snapshotToken,null);await h.run('applyCommit()');assert.equal(h.calls.filter(c=>c.path.endsWith('-apply')).length,0);}
});

for(const kind of ['commit','lock','add','shelf','file-op'])test(`${kind}: server-verified completion remains visible when post-write status is unavailable`,async()=>{
 const h=await ready(kind),payload=h.result(kind,h.request);payload.statusRefreshed=false;payload.snapshotToken=null;payload.rows=[];payload[`${kind==='file-op'?'fileOp':kind}Result`].warnings=['status-refresh-failed'];h.respond(async()=>payload);
 await h.run(h.flow.apply);assert.match(h.$(`${kind}-last-result`).textContent,h.flow.confirmed);assert.equal(h.state.snapshotToken,null);assert.equal(h.state.rows.length,0);h.$('modal').close();h.run('render()');assert.match(h.$(`${kind}-last-result`).textContent,h.flow.confirmed);
});
for(const kind of Object.keys(flows))test(`${kind}: malformed response or connection loss clears status and never auto-retries`,async()=>{
 for(const failure of ['identity','connection']){const h=await ready(kind);h.respond(async()=>{if(failure==='connection')throw Error('connection lost');return {...h.result(kind,h.request),workspaceId:'wrong'};});await h.run(h.flow.apply);assert.equal(h.state.snapshotToken,null);assert.equal(h.state.rows.length,0);assert.equal(h.state.busy,false);await h.run(h.flow.apply);assert.equal(h.calls.filter(c=>c.path.endsWith('-apply')).length,1);assert.doesNotMatch(h.logs(),h.flow.confirmed);}
});
test('actual row click and select-visible handlers retain hidden selections and never substitute focus for multi-selection',()=>{
 const h=harness();h.state.selected.clear();h.state.query='';h.run('render()');
 h.$('file-body').querySelector('[data-row-index="0"]').onclick({target:{type:'td'}});
 h.$('file-body').querySelector('[data-row-index="1"]').onclick({target:{type:'td'},ctrlKey:true});
 assert.deepEqual([...h.state.selected],['a','b']);
 h.$('search').emit('input',{target:{value:'visible'}});assert.equal(h.$('file-body').querySelectorAll('[data-row-index]').length,1);
 h.$('select-visible').onchange({target:{checked:false}});assert.deepEqual([...h.state.selected],['b']);
 h.$('select-visible').onchange({target:{checked:true}});assert.deepEqual([...h.state.selected].sort(),['a','b']);
 h.run("openFileOp('move')");assert.equal(h.$('modal').open,false);assert.equal(h.calls.length,0);assert.match(h.$('toast').textContent,/一个/);
 h.run('openCommit()');assert.match(h.$('modal-content').innerHTML,/visible\/a.txt/);assert.match(h.$('modal-content').innerHTML,/hidden\/b.txt/);
});
test('complete changelist commit includes hidden clean members rather than truncating to selected rows',async()=>{
 const h=harness();h.rows[1].item='normal';h.state.selected=new Set(['a']);h.run('render();openCommit("changelist","work")');h.$('commit-message').value='Commit complete changelist';h.respond(async(_,request)=>h.plan('commit',request));await h.run('preflightCommit()');assert.deepEqual(h.calls[0].body.ids,['a','b']);assert.equal(h.calls[0].body.operation,'changelist');
});
test('unlock and copy use their reviewed action, and closing their pending write keeps confirmed completion',async()=>{
 for(const [kind,action,expected] of [['lock','unlock',/已核实解锁完成/],['file-op','copy',/已核实本地带历史复制/]]){const h=harness();if(kind==='file-op'){h.state.selected=new Set(['a']);h.run("render();openFileOp('copy')");h.$('file-op-destination').value='visible/copy.txt';}else h.run("openLock('unlock')");let request;h.respond(async(_,body)=>{request=body;return h.plan(kind,body);});await h.run(flows[kind].preflight);assert.equal(h.$(`${kind}-confirm`).disabled,false);assert.equal(request.action,action);let resolve;h.respond(()=>new Promise(r=>resolve=r));const pending=h.run(flows[kind].apply);h.$('modal').close();resolve(h.result(kind,request));await pending;assert.match(h.$(`${kind}-last-result`).textContent,expected);assert.equal(h.state.snapshotToken,null);}
});
test('Shelf browser restores the full reviewed backup and retains verified completion after dismissal',async()=>{
 const h=harness();const files=h.rows.map(row=>({path:row.path,state:'modified',kind:'file',size:1}));h.respond(async()=>({mode:'live',workspaceId:'w',selectionVersion:1,snapshotToken:'s',shelves:[{id:'shelf-id',name:'backup',createdAt:'2026-11-04T00:00:00Z',format:2,files,totalBytes:2,entryCount:2}]}));await h.run('openShelfBrowser()');
 const choices=h.$('modal-content').querySelectorAll('[data-shelf-index]');assert.equal(choices.length,1);choices[0].onclick();let request;h.respond(async(_,body)=>{request=body;return h.plan('shelf',body);});await h.run('preflightShelf()');assert.equal(h.$('shelf-confirm').disabled,false);assert.equal(request.operation,'restore');assert.equal(request.shelfId,'shelf-id');assert.equal(request.ids,undefined);
 let resolve;h.respond(()=>new Promise(r=>resolve=r));const pending=h.run('applyShelf()');h.$('modal').close();resolve(h.result('shelf',request));await pending;assert.match(h.$('shelf-last-result').textContent,/整个 Shelf 已恢复/);assert.equal(h.state.snapshotToken,null);assert.equal(h.$('modal').open,false);
});
