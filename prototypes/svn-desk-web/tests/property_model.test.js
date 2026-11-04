import test from 'node:test';import assert from 'node:assert/strict';
import {canReadProperties,validatePropertyResponse} from '../web/model.js';
const scope={workspaceId:'w',selectionVersion:1,snapshotToken:'s',id:'a',path:'src/a'};
const value=(text,hash='a')=>({kind:'text',text,byteLength:new TextEncoder().encode(text).length,sha256:hash.repeat(64),textTruncated:false,redacted:false,escapedControls:0});
const payload={...scope,mode:'live',localOnly:true,displayOnly:true,baseAvailable:true,baseUnavailableReason:null,properties:[{name:'svn:mime-type',change:'modified',base:value('text/plain'),working:value('text/html','b')}]};
test('property reads enable only safe ordinary versioned file states',()=>{
 for(const item of ['normal','none','modified','added'])assert(canReadProperties({item,props:'none',kind:'file'}));
 for(const patch of [{item:'unversioned'},{item:'deleted'},{item:'replaced'},{props:'conflicted'},{kind:'dir'},{switched:true},{file_external:true},{wc_locked:true},{tree_conflicted:true},{copied:true},{moved_from:'a'},{moved_to:'b'}])assert(!canReadProperties({item:'modified',props:'none',...patch}));
});
test('property response binds exact scope and local-only bounded contract',()=>{
 assert.equal(validatePropertyResponse(payload,scope,true),payload);
 for(const patch of [{mode:'demo'},{id:'other'},{path:'src/b'},{workspaceId:'other'},{selectionVersion:2},{snapshotToken:'old'},{localOnly:false},{displayOnly:false},{baseUnavailableReason:'unknown'},{properties:null}])assert.throws(()=>validatePropertyResponse({...payload,...patch},scope,true));
});
test('raw byte digests determine change even when display is identical or redacted',()=>{
 const same={...value('[REDACTED]','b'),redacted:true};
 assert.equal(validatePropertyResponse({...payload,properties:[{name:'test:token',change:'modified',base:{...same,sha256:'a'.repeat(64)},working:same}]},scope,true).properties[0].change,'modified');
 for(const change of ['unchanged','added','deleted','unbased','arbitrary'])assert.throws(()=>validatePropertyResponse({...payload,properties:[{...payload.properties[0],change}]},scope,true));
});
test('added-file missing BASE is explicit and not an empty normal BASE',()=>{
 const p={...payload,baseAvailable:false,baseUnavailableReason:'added-file',properties:[{name:'svn:mime-type',change:'unbased',base:null,working:value('text/plain')}]};assert.equal(validatePropertyResponse(p,scope,false),p);
 for(const patch of [{change:'added'},{base:value('text/plain')},{working:null}])assert.throws(()=>validatePropertyResponse({...p,properties:[{...p.properties[0],...patch}]},scope,false));
});
test('empty strings and absent properties remain distinguishable',()=>{
 const empty=value('');
 for(const property of [{name:'x',change:'added',base:null,working:empty},{name:'x',change:'deleted',base:empty,working:null},{name:'x',change:'unchanged',base:empty,working:empty}])assert(validatePropertyResponse({...payload,properties:[property]},scope,true));
 assert.throws(()=>validatePropertyResponse({...payload,properties:[{name:'x',change:'added',base:null,working:null}]},scope,true));
});
test('binary values expose only size and digest; malformed display metadata rejected',()=>{
 const binary={kind:'binary',text:null,byteLength:20,sha256:'a'.repeat(64),textTruncated:false,redacted:false,escapedControls:0};
 const property={name:'x',change:'unchanged',base:binary,working:binary};assert(validatePropertyResponse({...payload,properties:[property]},scope,true));
 for(const patch of [{text:'BINARY SECRET'},{textTruncated:true},{redacted:true},{escapedControls:1},{byteLength:65537},{sha256:'invalid'}])assert.throws(()=>validatePropertyResponse({...payload,properties:[{...property,base:{...binary,...patch}}]},scope,true));
});
test('display limits, controls, duplicate names and missing values fail closed',()=>{
 for(const text of ['\0','\r','\x1b','\u202e','\u200f','\u061c','\u2067','\ud800','x'.repeat(4097)])assert.throws(()=>validatePropertyResponse({...payload,properties:[{...payload.properties[0],working:value(text,'b')}]},scope,true));
 for(const name of ['', 'bad name','bad\u202ename','x'.repeat(257)])assert.throws(()=>validatePropertyResponse({...payload,properties:[{...payload.properties[0],name}]},scope,true));
 assert.throws(()=>validatePropertyResponse({...payload,properties:[payload.properties[0],payload.properties[0]]},scope,true));
 assert.throws(()=>validatePropertyResponse({...payload,properties:[{name:'x',change:'added',base:null}]},scope,true));
 const many=Array.from({length:129},(_,i)=>({...payload.properties[0],name:'p'+i}));assert.throws(()=>validatePropertyResponse({...payload,properties:many},scope,true));
 const large=Array.from({length:3},(_,i)=>({name:'p'+i,change:'unchanged',base:{...value('x'),byteLength:65536},working:{...value('x'),byteLength:65536}}));assert.throws(()=>validatePropertyResponse({...payload,properties:large},scope,true));
});
