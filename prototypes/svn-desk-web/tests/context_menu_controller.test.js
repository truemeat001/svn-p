// DOM-adapter unit tests of the actual controller source. Not browser acceptance.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import * as model from '../web/model.js';
const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const controller=source.slice(source.indexOf('let fileMenuScope='),source.indexOf("let diffFrom='BASE'"));
function harness(){
 const calls=[],elements=new Map(),state={mode:'live',rows:[{id:'a',path:'dir/<img>.txt'},{id:'b',path:'b.txt'},{id:'c',path:'c.txt'}],selected:new Set(['a','b']),focused:'a',workspaceId:'wc',selectionVersion:1,snapshotToken:'snap',busy:false,failed:false};
 function element(id){if(!elements.has(id))elements.set(id,{open:false,disabled:false,classList:{add(){},remove(){}},replaceChildren(){this.innerHTML='';},innerHTML:'',offsetWidth:330,offsetHeight:420,style:{},querySelector(){return {focus(){calls.push(['focus',id]);},getBoundingClientRect(){return {left:10,top:20,height:30};}};}});return elements.get(id);}
 const context=vm.createContext({state,$:element,innerWidth:1440,innerHeight:960,esc:model.safeText,fileName:model.fileName,...model,
  render:()=>calls.push(['render']),displayed:()=>[state.rows[0]],readsAvailable:()=>true,propertyReadsAvailable:()=>true,showProperties:()=>calls.push(['properties',state.focused]),changelistsAvailable:()=>true,shelfCreateAvailable:()=>true,
  showDiff:()=>calls.push(['diff',state.focused]),showHistory:()=>calls.push(['history',state.focused]),review:()=>calls.push(['review',[...state.selected]]),
  openAdd:()=>calls.push(['add',[...state.selected]]),openChangelists:()=>calls.push(['changelist',[...state.selected]]),openCreateShelf:()=>calls.push(['shelf',[...state.selected]]),openCommit:operation=>calls.push(['commit',operation,[...state.selected]])});
 vm.runInContext(controller,context);
 return {state,calls,element,run:code=>vm.runInContext(code,context),open:(index=0)=>{context.event={preventDefault(){calls.push(['prevent']);},clientX:1420,clientY:940};context.row=state.rows[index];vm.runInContext('openFileMenu(event,row)',context);}};
}
test('opening renders escaped target, full/hidden count, and dispatches no action',()=>{
 const h=harness();h.open();assert.equal(h.calls.some(call=>['diff','history','review','changelist','shelf','commit'].includes(call[0])),false);
 assert.match(h.element('file-context-menu').innerHTML,/&lt;img&gt;/);assert.match(h.element('file-context-menu').innerHTML,/完整选区 2 项 · 含 1 项不可见/);
 assert.equal(h.element('file-context-menu').style.left,'1102px');assert.deepEqual([...h.state.selected],['a','b']);
});
test('each menu action reuses explicit existing workflow and commits selected files only',()=>{
 for(const action of ['diff','history','review','changelist','shelf','commit']){
  const h=harness();h.open();h.run(`runFileMenuAction('${action}')`);
  const actionCalls=h.calls.filter(call=>['diff','history','review','changelist','shelf','commit'].includes(call[0]));assert.equal(actionCalls.length,1);
  if(action==='commit')assert.deepEqual(actionCalls[0],['commit','selected',['a','b']]);
  if(action==='diff'||action==='history')assert.deepEqual(actionCalls[0],[action,'a']);
  h.run(`runFileMenuAction('${action}')`);assert.equal(h.calls.filter(call=>call[0]===action).length,1,'closed menu cannot replay');
 }
});
test('stale menu, disabled action and unknown action never dispatch',()=>{
 for(const change of [h=>h.state.selected=new Set(['c']),h=>h.state.snapshotToken='new',h=>h.state.rows=[...h.state.rows],h=>h.state.busy=true,h=>h.state.failed=true,h=>h.element('modal').open=true]){
  const h=harness();h.open();change(h);h.run("runFileMenuAction('commit')");assert.equal(h.calls.some(call=>call[0]==='commit'),false);
 }
 const h=harness();h.open();h.element('commit-selected').disabled=true;h.run("runFileMenuAction('commit')");h.run("runFileMenuAction('anything')");assert.equal(h.calls.some(call=>call[0]==='commit'),false);
});
test('unselected row switches to one file and modal/busy state blocks opening',()=>{
 const h=harness();h.open(2);assert.deepEqual([...h.state.selected],['c']);assert.equal(h.state.focused,'c');
 for(const field of ['busy','failed']){const h=harness();h.state[field]=true;h.open();assert.equal(h.calls.some(call=>call[0]==='render'),false);}
 const modal=harness();modal.element('modal').open=true;modal.open();assert.equal(modal.calls.some(call=>call[0]==='render'),false);
});
test('Escape restoration finds a current row; external closure never steals focus',()=>{
 const h=harness();h.open();h.calls.length=0;h.run('closeFileMenu(true)');assert.deepEqual(h.calls,[['focus','file-body']]);
 h.open();h.calls.length=0;h.run('closeFileMenu()');assert.deepEqual(h.calls,[]);
});
