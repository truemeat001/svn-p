import test from 'node:test';
import assert from 'node:assert/strict';
import {stateFor,STATES,filteredRows,groupsFor,summary,reviewSelection,safeText,fileName} from '../web/model.js';
import {demoRows} from '../web/demo-data.js';
test('fixture is explicitly scoped and has stable IDs',()=>{assert.equal(demoRows.length,12);assert.equal(new Set(demoRows.map(r=>r.id)).size,12);assert.deepEqual(summary(demoRows),{total:12,modified:6,conflicted:1,unversioned:2});});
test('groups retain SVN local labels and default group first',()=>{const g=groupsFor(demoRows);assert.equal(g.length,3);assert.equal(g[0][0],'');assert.equal(g[0][1].length,4);});
test('search matches case-insensitive paths and labels',()=>{assert.equal(filteredRows(demoRows,{query:'PLAYERCONTROLLER'}).length,1);assert.equal(filteredRows(demoRows,{query:'界面细节'}).length,3);assert.equal(filteredRows(demoRows,{query:'不存在'}).length,0);});
test('folder filter is segment bound, never selects prefix siblings',()=>{const rows=[{path:'Assets/Player/a.cs'},{path:'Assets/PlayerBackup/a.cs'},{path:'Assets/Player'}];assert.equal(filteredRows(rows,{folder:'Assets/Player'}).length,2);});
test('conflict filter includes tree and property conflicts',()=>{const rows=[{path:'a',item:'normal',tree_conflicted:true},{path:'b',item:'normal',props:'conflicted'},{path:'c',item:'modified'}];assert.equal(filteredRows(rows,{filter:'conflicted'}).length,2);});
test('modified properties never labelled clean',()=>assert.equal(stateFor({item:'normal',props:'modified'}),STATES.none));
test('review contains only explicit selection including filtered-hidden files',()=>{const selected=new Set([demoRows[0].id,demoRows[8].id]);assert.equal(filteredRows(demoRows,{query:'Forest'}).length,1);assert.deepEqual(reviewSelection(demoRows,selected).map(r=>r.id),[demoRows[0].id,demoRows[8].id]);});
test('no arbitrary stale selections included in review',()=>assert.equal(reviewSelection(demoRows,new Set(['../../private'])).length,0));
test('HTML-sensitive filenames are escaped',()=>assert.equal(safeText('<img src=x onerror="x">&\''),'&lt;img src=x onerror=&quot;x&quot;&gt;&amp;&#39;'));
test('file names handle Windows display separators',()=>assert.equal(fileName('C:\\Work\\a.cs'),'a.cs'));

test('modified filter includes property-only changes in normal/none states',()=>{const rows=[{path:'a',item:'normal',props:'modified'},{path:'b',item:'none',props:'modified'},{path:'c',item:'normal',props:'none'}];assert.equal(filteredRows(rows,{filter:'modified'}).length,2);});


import {isStaleWorkspaceCatalog} from '../web/model.js';
test('older workspace catalogs cannot roll back a newer selected generation',()=>{
 assert.equal(isStaleWorkspaceCatalog({current:{selectionVersion:2}},3),true);
 assert.equal(isStaleWorkspaceCatalog({current:{selectionVersion:3}},3),false);
 assert.equal(isStaleWorkspaceCatalog({current:{selectionVersion:4}},3),false);
 assert.equal(isStaleWorkspaceCatalog({enabled:false},3),false);
});
