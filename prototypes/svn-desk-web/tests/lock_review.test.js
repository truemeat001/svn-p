// Independent cross-flow audit: execute real callback registration, not regex.
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
test('lock integration preserves commit input and keep-label invalidation callbacks',()=>{
 const elements=new Map(); let invalidations=0;
 const $=id=>{if(!elements.has(id))elements.set(id,{classList:{remove(){}},disabled:false});return elements.get(id);};
 const context=vm.createContext({$,state:{rows:[],selected:new Set()},commitsAvailable:()=>true,commitScopeRows:()=>[{path:'synthetic.txt'}],modal(){},commitFilesMarkup:()=>'',esc:s=>s,notify(){},invalidateLock(){},invalidateCommit(){invalidations++;$('commit-confirm').disabled=true;},preflightCommit(){},commitChoice:null,modalView:null});
 vm.runInContext(source.slice(source.indexOf('function openCommit('),source.indexOf('function commitRequest(')),context);
 vm.runInContext('openCommit()',context);
 for(const [id,event] of [['commit-message','oninput'],['commit-keep','onchange']]){
  const before=invalidations; $('commit-confirm').disabled=false; $(id)[event]();
  assert.equal(invalidations,before+1,`${event} must consume existing commit review`);
  assert.equal($('commit-confirm').disabled,true);
 }
});
