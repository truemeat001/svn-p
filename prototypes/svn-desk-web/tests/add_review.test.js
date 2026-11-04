// Independent review regressions: synthetic controller only, no real SVN.
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import * as model from '../web/model.js';
const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const controller=source.slice(source.indexOf('let addGeneration='),source.indexOf('async function api('));
test('verified add survives subsequent local snapshot application failure',async()=>{
 const nodes=new Map();
 const $=id=>{if(!nodes.has(id))nodes.set(id,{disabled:false,open:true,textContent:'',innerHTML:'',classList:{add(){},remove(){}}});return nodes.get(id);};
 const row={id:'new.txt',path:'new.txt',item:'unversioned',props:'none'};
 const state={mode:'live',addsEnabled:true,busy:false,failed:false,rows:[row],selected:new Set([row.id]),workspaceId:'w',selectionVersion:1,snapshotToken:'s'};
 const scope=()=>({workspaceId:state.workspaceId,selectionVersion:state.selectionVersion,snapshotToken:state.snapshotToken});
 let applies=0;
 const context=vm.createContext({...model,Date,state,$,esc:model.safeText,modalView:'add',selectionKey:()=>row.id,shelfScope:scope,setTimeout(){},setBusy:b=>state.busy=b,notify(){},log(){},invalidateReads(){},renderPreview(){},render(){},
  failSnapshot:()=>{state.failed=true;state.rows=[];state.snapshotToken=null;},
  applySnapshot:()=>{throw Error('synthetic rendering failure after verified response');},
  api:async(path,body)=>{
   if(path.endsWith('preflight'))return {...body,mode:'live',localOnly:true,autoProps:false,expiresIn:120,planToken:'p',files:[{id:row.id,path:row.path}]};
   applies++;return {...scope(),mode:'live',statusRefreshed:true,snapshotToken:'new',rows:[{...row,item:'added'}],addResult:{action:'add',files:[row.path],outcome:'verified',verified:true,autoProps:false,warnings:[]}};
  }});
 vm.runInContext(controller,context);
 await vm.runInContext('preflightAdd()',context);
 await vm.runInContext('applyAdd()',context);
 assert.equal(applies,1);
 assert.match($('add-last-result').textContent,/已核实添加 1/);
 assert.doesNotMatch($('add-last-result').textContent,/添加结果未知/);
});
