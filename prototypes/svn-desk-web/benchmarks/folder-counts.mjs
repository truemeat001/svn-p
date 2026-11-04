// Synthetic directory-rendering comparison. No browser layout, SVN or files read.
// Run: node benchmarks/folder-counts.mjs > folder-counts-result.json
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {performance} from 'node:perf_hooks';
import {folderCountsFor,filteredRows,fileName,safeText} from '../web/model.js';
const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const current=source.slice(source.indexOf('function renderTree(){'),source.indexOf('function render(){'));
const oldSetup=" const folders=new Set(['']);for(const row of state.rows){const parts=row.path.split('/');for(let i=1;i<parts.length;i++)folders.add(parts.slice(0,i).join('/'));}\n const sorted=[...folders].sort((a,b)=>a.localeCompare(b));";
const newSetup=" const counts=folderCountsFor(state.rows);\n const sorted=[...counts.keys()].sort((a,b)=>a.localeCompare(b));";
assert(current.includes(newSetup)&&current.includes('const count=counts.get(folder);'));
// Prior implementation from commit 5298602, with the same markup/event code.
const previous=current.replace(newSetup,oldSetup).replace('const count=counts.get(folder);','const count=filteredRows(state.rows,{folder}).length;');
function renderer(code,rows){
 const tree={innerHTML:'',querySelectorAll(){return [];}};
 const context=vm.createContext({state:{rows,folder:'',workspace:'Synthetic only'},$:()=>tree,folderCountsFor,filteredRows,fileName,esc:safeText,icon:()=>'',render(){}});
 vm.runInContext(code,context);
 return ()=>{const start=performance.now();vm.runInContext('renderTree()',context);return {milliseconds:performance.now()-start,html:tree.innerHTML};};
}
const results=[];
for(const count of [2000,4000,8000]){
 const rows=Array.from({length:count},(_,i)=>({id:String(i),path:`folder${String(i).padStart(5,'0')}/file.txt`}));
 const old=renderer(previous,rows),updated=renderer(current,rows),oldTimes=[],newTimes=[];
 for(let repeat=0;repeat<3;repeat++){
  const a=old(),b=updated();assert.equal(b.html,a.html,'Rendered directory markup must remain identical');oldTimes.push(a.milliseconds);newTimes.push(b.milliseconds);
 }
 const median=a=>Math.round(a.sort((x,y)=>x-y)[1]*100)/100;
 results.push({rows:count,folders:count+1,repeats:3,oldMedianMs:median(oldTimes),newMedianMs:median(newTimes),identicalMarkup:true});
}
console.log(JSON.stringify({scope:'Synthetic rows, actual tree renderer with DOM adapter; excludes browser DOM/layout and native Windows/SVN',results},null,2));
