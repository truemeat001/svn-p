// Independent real Python serializer -> JS validator contract checks; no SVN.
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {validatePropertyResponse} from '../web/model.js';
const scope={workspaceId:'w',selectionVersion:1,snapshotToken:'s',id:'a.txt',path:'a.txt'};
const values=JSON.parse(execFileSync('python3',['-c',`import json
from property_reads import _value
raws = [b'', bytes(range(256)), ('😀'*2000).encode(), ('a\\u200b\\u202e\\U000e0001b').encode(), b'a'*65536]
print(json.dumps([_value(raw, lambda s: s) for raw in raws]))`],{cwd:new URL('..',import.meta.url),encoding:'utf8'}));
const response=properties=>({...scope,mode:'live',localOnly:true,displayOnly:true,baseAvailable:true,baseUnavailableReason:null,properties});
test('actual backend display metadata validates without reinterpretation',()=>{
 for(const value of values){
  const data=response([{name:'😀'.repeat(128),base:value,working:value,change:'unchanged'}]);
  assert.equal(validatePropertyResponse(data,scope,true),data);
 }
});
test('one-sided properties preserve absence, binary and empty string distinctions',()=>{
 for(const value of values){
  for(const [base,working,change] of [[null,value,'added'],[value,null,'deleted']]){
   const data=response([{name:'x',base,working,change}]);
   assert.equal(validatePropertyResponse(data,scope,true),data);
  }
 }
});
test('digest equality does not conceal differing byte lengths',()=>{
 const left=values[0],right={...left,byteLength:1};
 assert.throws(()=>validatePropertyResponse(response([{name:'x',base:left,working:right,change:'unchanged'}]),scope,true));
 assert(validatePropertyResponse(response([{name:'x',base:left,working:right,change:'modified'}]),scope,true));
});
test('captured ordinary-file status cannot accept an added-file BASE-unavailable response',()=>{
 const data={...response([{name:'x',base:null,working:values[0],change:'unbased'}]),baseAvailable:false,baseUnavailableReason:'added-file'};
 assert.throws(()=>validatePropertyResponse(data,scope,true));
 assert.equal(validatePropertyResponse(data,scope,false),data);
});
test('captured added-file status cannot accept a normal BASE comparison',()=>{
 const data=response([{name:'x',base:values[0],working:values[0],change:'unchanged'}]);
 assert.throws(()=>validatePropertyResponse(data,scope,false));
 assert.equal(validatePropertyResponse(data,scope,true),data);
});
