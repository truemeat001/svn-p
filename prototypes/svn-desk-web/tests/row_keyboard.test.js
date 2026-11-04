// Actual event-handler regression tests with a DOM adapter, not browser acceptance.
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const fragment=source.split('\n').find(line=>line.includes('tr.onkeydown=e=>'));
function harness({focused='a',selected=['a','hidden'],target='c',visible=['a','b','c']}={}){
 const rows=['a','b','c','hidden'].map(id=>({id,path:`${id}.txt`}));
 const state={rows,focused,selected:new Set(selected)};
 const tr={},focusCalls=[],renders=[];let prevented=0;
 vm.runInNewContext(fragment,{tr,row:rows.find(row=>row.id===target),state,visible:visible.map(id=>rows.find(row=>row.id===id)),
  render(){renders.push({focused:state.focused,selected:[...state.selected]});},
  $:()=>({querySelector(selector){return {focus(){focusCalls.push(selector);}};}})
 });
 return {state,focusCalls,renders,key(key){tr.onkeydown({key,preventDefault(){prevented++;}});},prevented:()=>prevented};
}
test('Tab to another row then Space keeps focus and preview target on that row',()=>{
 const h=harness();h.key(' ');assert.equal(h.state.focused,'c');assert.deepEqual([...h.state.selected],['a','hidden','c']);
 assert.deepEqual(h.focusCalls,['[data-row-index="2"]']);assert.equal(h.renders[0].focused,'c');assert.equal(h.prevented(),1);
});
test('Space from initially null logical focus selects the actual keyboard row',()=>{
 const h=harness({focused:null,selected:[],target:'b'});h.key(' ');assert.equal(h.state.focused,'b');assert.deepEqual([...h.state.selected],['b']);assert.deepEqual(h.focusCalls,['[data-row-index="1"]']);
});
test('Space deselects only the focused row and preserves hidden selected files',()=>{
 const h=harness({selected:['a','c','hidden']});h.key(' ');assert.equal(h.state.focused,'c');assert.deepEqual([...h.state.selected],['a','hidden']);assert.equal(h.prevented(),1);
});
test('arrow navigation follows visible rows without changing selected scope',()=>{
 const h=harness({target:'a',visible:['a','c']});h.key('ArrowDown');assert.equal(h.state.focused,'c');assert.deepEqual([...h.state.selected],['a','hidden']);assert.deepEqual(h.focusCalls,['[data-row-index="2"]']);
});
test('arrow boundary remains at the actual keyboard row, not stale logical focus',()=>{
 for(const [target,key,index] of [['a','ArrowUp',0],['c','ArrowDown',2]]){
  const h=harness({focused:'b',target});h.key(key);assert.equal(h.state.focused,target);assert.deepEqual(h.focusCalls,[`[data-row-index="${index}"]`]);assert.deepEqual([...h.state.selected],['a','hidden']);
 }
});
test('unhandled keys leave focus and selection unchanged for their own handlers',()=>{
 for(const key of ['Tab','Enter','F5','F10','ContextMenu','Escape']){
  const h=harness();h.key(key);assert.equal(h.state.focused,'a');assert.deepEqual([...h.state.selected],['a','hidden']);assert.deepEqual(h.focusCalls,[]);assert.deepEqual(h.renders,[]);assert.equal(h.prevented(),0);
 }
});
