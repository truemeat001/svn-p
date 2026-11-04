import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {folderCountsFor,filteredRows,fileName,safeText} from '../web/model.js';
function previousCounts(rows){
 const folders=new Set(['']);
 for(const row of rows){const parts=row.path.split('/');for(let i=1;i<parts.length;i++)folders.add(parts.slice(0,i).join('/'));}
 return new Map([...folders].map(folder=>[folder,filteredRows(rows,{folder}).length]));
}
function entries(counts){return [...counts].sort(([a],[b])=>a.localeCompare(b));}
function equivalent(rows){assert.deepEqual(entries(folderCountsFor(rows)),entries(previousCounts(rows)));}
test('folder counts preserve directory status rows, ancestors and sibling boundaries',()=>{
 const rows=['file.txt','Assets','Assets/Player','Assets/Player/a.cs','Assets/Player/b.cs','Assets/PlayerBackup/a.cs','Assets/PlayerBackup','other/x'].map(path=>({path}));
 equivalent(rows);
 assert.deepEqual(entries(folderCountsFor(rows)),entries(new Map([['',8],['Assets',6],['Assets/Player',3],['Assets/PlayerBackup',2],['other',1]])));
});
test('empty, root-only, Unicode, escaped markup and unusual display paths match previous filters',()=>{
 for(const paths of [[],['a','b'],['','.', './x', './dir/z'],['中文/文件.txt','中文','中文/更深/图.png','<tag>/&.txt'],['/foo/x','/foo','//','/','a//b','a/','a'],['a/a/a/a','a/a/a','a/a','a']])equivalent(paths.map(path=>({path})));
});
test('counts do not mutate paths, IDs or changelists or include labels in directory identity',()=>{
 const rows=Object.freeze([Object.freeze({id:'native\\id',path:'ui/path.txt',changelist:'other/label'}),Object.freeze({id:'native2',path:'ui',changelist:null})]);
 const before=JSON.stringify(rows);assert.deepEqual(entries(folderCountsFor(rows)),[['',2],['ui',2]]);assert.equal(JSON.stringify(rows),before);
});
test('500 deterministic randomized collections equal the original filter-based counts',()=>{
 let seed=0x5298602;const random=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/2**32;};
 const segments=['Assets','Asset','目录','a','a_backup','two words','<file>','nested'];
 for(let run=0;run<500;run++){
  const rows=[];const count=Math.floor(random()*120);
  for(let i=0;i<count;i++){
   const depth=1+Math.floor(random()*6),parts=Array.from({length:depth},()=>segments[Math.floor(random()*segments.length)]);
   rows.push({id:String(i),path:parts.join('/'),changelist:i%3?'group':'',item:i%2?'modified':'normal'});
  }
  equivalent(rows);
 }
});
test('each source path is inspected twice, independent of sibling-folder count',()=>{
 let reads=0;const rows=Array.from({length:10000},(_,i)=>({get path(){reads++;return `directory${i}/file.txt`;}}));
 const counts=folderCountsFor(rows);assert.equal(reads,20000);assert.equal(counts.size,10001);assert.equal(counts.get(''),10000);assert.equal(counts.get('directory9999'),1);
});
test('actual tree renderer uses counts, escapes names and retains directory-only filtering',()=>{
 const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
 const fragment=source.slice(source.indexOf('function renderTree(){'),source.indexOf('function render(){'));
 const rows=[{id:'p',path:'<dir>'},{id:'f',path:'<dir>/a.txt'},{id:'s',path:'<dir>_sibling/x.txt'}];
 const state={rows,folder:'',workspace:'<workspace>',selected:new Set(['p','f']),collapsed:new Set(['group'])};
 const buttons=Array.from({length:3},()=>({classList:{add(){}},removeAttribute(){}}));
 const tree={innerHTML:'',querySelectorAll(){return buttons;}};let renders=0;
 // No filteredRows binding: the renderer must not rescan all rows per folder.
 const context=vm.createContext({state,$:()=>tree,folderCountsFor,fileName,esc:safeText,icon:()=>'',render(){renders++;}});
 vm.runInContext(fragment,context);vm.runInContext('renderTree()',context);
 assert.match(tree.innerHTML,/&lt;workspace&gt;/);assert.match(tree.innerHTML,/&lt;dir&gt;/);assert.match(tree.innerHTML,/folder-count\">2</);
 buttons[1].onclick();assert.equal(state.folder,'<dir>');assert.deepEqual([...state.selected],['p','f']);assert.equal(state.collapsed.size,0);assert.equal(renders,1);
});
