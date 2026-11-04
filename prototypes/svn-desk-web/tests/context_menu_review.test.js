// Independent review: actual event listener code, DOM adapter only, not browser acceptance.
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const events=source.slice(source.indexOf("$('file-context-menu').addEventListener('click'"),source.indexOf("$('refresh').onclick="));
function harness(){
 const handlers={menu:{},document:{},window:{}},closed=[],dispatched=[];
 const document={activeElement:null,addEventListener:(name,fn)=>handlers.document[name]=fn};
 const buttons=Array.from({length:4},(_,index)=>({index,inside:true,focus(){document.activeElement=this;}}));
 const menu={contains:target=>!!target?.inside,querySelectorAll:()=>buttons,addEventListener:(name,fn)=>handlers.menu[name]=fn};
 const context=vm.createContext({$:()=>menu,document,window:{addEventListener:(name,fn)=>handlers.window[name]=fn},fileMenuScope:{},closeFileMenu:restore=>closed.push(restore===true),runFileMenuAction:id=>dispatched.push(id)});
 vm.runInContext(events,context);
 function key(key,shiftKey=false){const e={key,shiftKey,prevented:false,stopped:false,preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;}};handlers.menu.keydown(e);return e;}
 return {handlers,closed,dispatched,buttons,document,key};
}
test('menu navigation wraps and Home/End reach enabled edge actions',()=>{
 const h=harness();h.buttons[0].focus();
 let e=h.key('ArrowUp');assert.equal(h.document.activeElement,h.buttons[3]);assert(e.prevented&&e.stopped);
 h.key('ArrowDown');assert.equal(h.document.activeElement,h.buttons[0]);
 h.key('End');assert.equal(h.document.activeElement,h.buttons[3]);
 h.key('Home');assert.equal(h.document.activeElement,h.buttons[0]);
 assert.deepEqual(h.dispatched,[]);assert.deepEqual(h.closed,[]);
});
test('Escape restores focus and stops propagation; Tab permits browser traversal',()=>{
 for(const shift of [false,true]){const h=harness();const e=h.key('Tab',shift);assert.deepEqual(h.closed,[true]);assert.equal(e.prevented,false);assert.equal(e.stopped,false);}
 const h=harness(),e=h.key('Escape');assert.deepEqual(h.closed,[true]);assert(e.prevented&&e.stopped);
});
test('inside events preserve menu; external pointer/focus/scroll and window events close without stealing focus',()=>{
 for(const name of ['pointerdown','focusin','scroll']){const h=harness();h.handlers.document[name]({target:{inside:true}});assert.deepEqual(h.closed,[]);h.handlers.document[name]({target:{}});assert.deepEqual(h.closed,[false]);}
 for(const name of ['resize','blur']){const h=harness();h.handlers.window[name]();assert.deepEqual(h.closed,[false]);}
});
test('click delegates only explicit enabled action; arbitrary menu content has no action',()=>{
 const h=harness();
 h.handlers.menu.click({target:{closest:()=>null}});
 h.handlers.menu.click({target:{closest:()=>({disabled:true,dataset:{fileAction:'commit'}})}});
 assert.deepEqual(h.dispatched,[]);
 h.handlers.menu.click({target:{closest:()=>({disabled:false,dataset:{fileAction:'review'}})}});
 assert.deepEqual(h.dispatched,['review']);
});
test('row keyboard listener claims only ContextMenu and Shift+F10',()=>{
 const fragment=source.match(/tr\.addEventListener\('keydown',event=>\{[^\n]+/)[0];
 const calls=[];let handler;const row={id:'target'};
 vm.runInNewContext(fragment,{tr:{addEventListener:(_,fn)=>handler=fn},row,openFileMenu:(e,r,keyboard)=>calls.push([r,keyboard])});
 for(const [key,shiftKey,expected] of [['F10',false,0],['Enter',false,0],['ContextMenu',false,1],['F10',true,1]]){
  const before=calls.length;let stopped=false;handler({key,shiftKey,stopImmediatePropagation(){stopped=true;}});assert.equal(calls.length-before,expected);assert.equal(stopped,!!expected);
 }
 assert(calls.every(([r,keyboard])=>r===row&&keyboard===true));
});
