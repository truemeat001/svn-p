import test from 'node:test';
import assert from 'node:assert/strict';
import {contextMenuSelection,captureContextMenuScope,sameContextMenuScope,contextMenuPosition} from '../web/model.js';
const rows=[{id:'a',path:'A/a.txt',changelist:'work'},{id:'b',path:'B/b.txt',changelist:'work'},{id:'c',path:'C/c.txt',changelist:''}];
function state(){return {rows,mode:'live',workspaceId:'wc',selectionVersion:1,snapshotToken:'snapshot',focused:'a',selected:new Set(['a','b']),busy:false,failed:false};}
test('right-click selected target preserves exact multi-selection without group expansion',()=>{
 const selected=new Set(['a','c']);const result=contextMenuSelection(rows,selected,'a');
 assert.deepEqual([...result],['a','c']);assert.notEqual(result,selected);assert.deepEqual([...selected],['a','c']);
});
test('right-click unselected target explicitly replaces selection with that file only',()=>{
 assert.deepEqual([...contextMenuSelection(rows,new Set(['a','c']),'b')],['b']);
});
test('context selection prunes absent ids and rejects missing targets without mutation',()=>{
 const selected=new Set(['a','gone']);assert.deepEqual([...contextMenuSelection(rows,selected,'a')],['a']);
 assert.throws(()=>contextMenuSelection(rows,selected,'gone'));assert.deepEqual([...selected],['a','gone']);
});
test('menu scope matches equivalent selection ordering',()=>{
 const current=state(),scope=captureContextMenuScope(current);current.selected=new Set(['b','a']);assert.equal(sameContextMenuScope(scope,current),true);
});
test('menu scope rejects changed identity, status, focus, selection, refreshed rows, failures and busy state',()=>{
 for(const [key,value] of [['rows',[...rows]],['mode','demo'],['workspaceId','other'],['selectionVersion',2],['snapshotToken','new'],['focused','b'],['selected',new Set(['a'])],['busy',true],['failed',true]]){
  const current=state(),scope=captureContextMenuScope(current);current[key]=value;assert.equal(sameContextMenuScope(scope,current),false,key);
 }
 assert.equal(sameContextMenuScope(null,state()),false);
});
test('context menu coordinates stay inside viewport for corners and keyboard anchors',()=>{
 assert.deepEqual(contextMenuPosition(1400,950,330,420,1440,960),{left:1102,top:532});
 assert.deepEqual(contextMenuPosition(-12,-1,330,420,520,520),{left:8,top:8});
 assert.deepEqual(contextMenuPosition(40,100,330,420,1440,960),{left:40,top:100});
 assert.deepEqual(contextMenuPosition(NaN,Infinity,330,420,520,520),{left:8,top:8});
});
