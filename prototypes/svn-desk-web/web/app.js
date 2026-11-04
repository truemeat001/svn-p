import {STATES,stateFor,filteredRows,folderCountsFor,groupsFor,summary,reviewSelection,safeText as esc,fileName,parentPath,isStaleWorkspaceCatalog,parseRevision,parseComparisonPair,canReadFile,sameReadScope,validateFileResponse,parseChangelistTarget,validateChangelistPlan,canRelabelFile,canShelfFile,parseShelfName,sameShelfScope,validateShelfFiles,validateShelfPlan,validateShelfResult,shelfResultSummary,commitScopeRows,parseCommitMessage,validateCommitPlan,validateCommitResult,commitResultSummary,contextMenuSelection,captureContextMenuScope,sameContextMenuScope,contextMenuPosition,lockScopeRows,parseLockComment,validateLockPlan,validateLockResult,lockResultSummary,addScopeRows,validateAddPlan,validateAddResult,addResultSummary,canReadProperties,validatePropertyResponse,fileOpSource,parseFileDestination,validateFileOpPlan,validateFileOpResult,fileOpResultSummary} from './model.js';
import {demoRows,demoDiff,demoHistory} from './demo-data.js';
const $=id=>document.getElementById(id);
const paths={
 refresh:'M20 7v5h-5 M4 17v-5h5 M6.1 7A7 7 0 0 1 18 6l2 6 M4 12l2 6a7 7 0 0 0 11.9-1',
 folder:'M3 6h6l2 2h10v11H3z', 'chevron-down':'m7 10 5 5 5-5', 'chevron-right':'m10 7 5 5-5 5',
 diff:'M4 4h6v16H4z M14 4h6v16h-6z M6 8h2 M16 8h2 M16 13h2 M17 12v2',
 checklist:'m3 6 1 1 2-2 M9 6h12 m-18 6 1 1 2-2 M9 12h12 m-18 6 1 1 2-2 M9 18h12',
 undo:'m8 3-5 5 5 5 M3 8h11a6 6 0 0 1 0 12h-4', upload:'M5 15v5h14v-5 M12 16V3 m-5 5 5-5 5 5',
 lock:'M6 10h12v11H6z M8 10V7a4 4 0 0 1 8 0v3 M12 14v3',computer:'M3 4h18v13H3z M8 21h8 M12 17v4',
 branch:'M6 6v12 M6 11h7a5 5 0 0 0 5-5 M6 3a3 3 0 1 0 0 6 3 3 0 0 0 0-6 M6 15a3 3 0 1 0 0 6 3 3 0 0 0 0-6 M18 1a3 3 0 1 0 0 6 3 3 0 0 0 0-6',
 layers:'m12 3 10 5-10 5L2 8z m0 10-10-5 m10 5 10-5 m-20 5 10 5 10-5',
 alert:'m12 3 10 18H2z M12 9v5 M12 17v1',shield:'m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6z m-4 10 3 3 5-6',
 history:'M3 4v6h6 M3 10a9 9 0 1 1 2 9 M12 7v6l4 2',help:'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20 M9 8a3 3 0 1 1 4 3l-1 1v2 M12 17v1',
 'panel-right':'M3 3h18v18H3z M15 3v18',panel:'M3 3h18v18H3z M9 3v18',close:'m6 6 12 12 M6 18 18 6',
 search:'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14 M15 15l6 6',collapse:'m8 8 4-4 4 4 M4 12h16 m-12 4 4 4 4-4',
 'filter-x':'M3 4h18l-7 8v7l-4 2V12z M17 15l5 5 m-5 0 5-5', code:'m8 5-6 7 6 7 m8-14 6 7-6 7 m-3-16-2 18',
 terminal:'M3 4h18v16H3z m4 4 4 4-4 4 M13 16h4',file:'M5 2h9l5 5v15H5z M14 2v6h5 M8 12h8 M8 16h6',
 image:'M3 3h18v18H3z m0 13 5-5 5 6 4-4 4 5 M15 7h1',server:'M3 3h18v7H3z M3 14h18v7H3z M6 6h1 M6 17h1',
 copy:'M8 8h13v13H8z M16 8V3H3v13h5',check:'m4 12 5 5L20 6',
};
const icon=(name)=>`<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name]||paths.file}"></path></svg>`;
function paintIcons(root=document){root.querySelectorAll('[data-icon]').forEach(e=>{e.innerHTML=icon(e.dataset.icon);});}
const token=document.querySelector('meta[name="svn-session"]').content;
const standalone=token==='__SESSION_TOKEN__';
const state={mode:standalone?'demo':'loading',rows:[],query:'',filter:'all',folder:'',selected:new Set(),focused:null,collapsed:new Set(),preview:'diff',logs:[],busy:false,failed:false,workspace:'Project Atlas',root:'D:\\Workspace\\ProjectAtlas',workspaceId:null,selectionVersion:0};
let toastTimer;
let fileMenuScope=null,fileMenuRow=null;
function focusFileRow(id){const index=state.rows.findIndex(row=>row.id===id);if(index>=0)$('file-body').querySelector(`[data-row-index="${index}"]`)?.focus({preventScroll:true});}
function closeFileMenu(restoreFocus=false){
 const id=fileMenuRow;fileMenuScope=null;fileMenuRow=null;
 const menu=$('file-context-menu');if(!menu)return;
 menu.classList.add('hidden');menu.replaceChildren();
 if(restoreFocus&&id!==null)focusFileRow(id);
}
function fileMenuActions(){
 const read=state.mode==='demo'||readsAvailable();
 return [
  {id:'diff',label:'查看此文件差异…',hint:'仅焦点文件；真实模式仍需点击读取',enabled:read,run:showDiff},
  {id:'properties',label:'查看此文件属性…',hint:'仅焦点文件；本地 WORKING / BASE 对照，需显式读取',enabled:state.mode==='demo'||propertyReadsAvailable(),run:showProperties},
  {id:'history',label:'查看此文件历史…',hint:'仅焦点文件；不会立即访问仓库',enabled:read,run:showHistory},
  {id:'review',label:'审查完整选区…',hint:'包含被筛选或折叠隐藏的选中项',enabled:state.selected.size>0,run:review},
  {id:'changelist',label:'移动选区到变更列表…',hint:state.mode==='demo'?'仅演示模拟；仍需预检并确认':'需启用本地归组；仍需预检并确认',enabled:changelistsAvailable(),run:openChangelists},
  {id:'add',label:'添加选区到版本控制…',hint:'仅未版本化普通文件；不添加目录、不提交',enabled:!$('add-selected').disabled,run:openAdd},
  {id:'file-move',label:'移动／重命名此选中文件…',hint:'完整选区必须只有一个文件；目标必须不存在',enabled:!$('move-file').disabled,run:()=>openFileOp('move')},
  {id:'file-copy',label:'带历史复制此选中文件…',hint:'保留源文件；不创建父目录、不提交',enabled:!$('copy-file').disabled,run:()=>openFileOp('copy')},
  {id:'shelf',label:'从选区创建本地 Shelf…',hint:'需真实模式及 Shelf 启用；仍需预检并确认',enabled:shelfCreateAvailable(),run:openCreateShelf},
  {id:'lock',label:'锁定明确选区…',hint:'需启用仓库锁；预检会访问仓库',enabled:!$('lock-selected').disabled,run:()=>openLock('lock')},
  {id:'unlock',label:'解锁明确选区…',hint:'仅本工作副本持有的锁；不强制解锁',enabled:!$('unlock-selected').disabled,run:()=>openLock('unlock')},
  {id:'commit',label:'提交明确选区…',hint:'不扩大到整个组；仍需预检并确认',enabled:!$('commit-selected').disabled,run:()=>openCommit('selected')},
 ];
}
function openFileMenu(event,row,keyboard=false){
 event.preventDefault();
 if(state.busy||state.failed||$('modal').open||!state.rows.includes(row))return;
 state.selected=contextMenuSelection(state.rows,state.selected,row.id);state.focused=row.id;render();
 fileMenuRow=row.id;fileMenuScope=captureContextMenuScope(state);
 const menu=$('file-context-menu'),visible=new Set(displayed().map(item=>item.id));
 const hidden=[...state.selected].filter(id=>!visible.has(id)).length;
 menu.innerHTML=`<div class="file-menu-heading" role="presentation"><strong>${esc(fileName(row.path))}</strong><span>${esc(row.path)}</span><span>完整选区 ${state.selected.size} 项${hidden?` · 含 ${hidden} 项不可见`:''}</span></div>${fileMenuActions().map(action=>`<button type="button" role="menuitem" tabindex="-1" data-file-action="${action.id}" ${action.enabled?'':'disabled'}><strong>${esc(action.label)}</strong><small>${esc(action.hint)}</small></button>`).join('')}`;
 menu.classList.remove('hidden');
 const anchor=$('file-body').querySelector(`[data-row-index="${state.rows.indexOf(row)}"]`).getBoundingClientRect();
 const pos=contextMenuPosition(keyboard?anchor.left+36:event.clientX,keyboard?anchor.top+anchor.height:event.clientY,menu.offsetWidth,menu.offsetHeight,innerWidth,innerHeight);
 // Setting individual CSSOM properties is compatible with the bridge's no-inline-style CSP.
 menu.style.left=`${pos.left}px`;menu.style.top=`${pos.top}px`;
 menu.querySelector('button:not(:disabled)')?.focus({preventScroll:true});
}
function runFileMenuAction(id){
 if(!sameContextMenuScope(fileMenuScope,state)||$('modal').open){closeFileMenu();return;}
 const action=fileMenuActions().find(item=>item.id===id);
 if(!action?.enabled)return;
 closeFileMenu(true);action.run();
}

let diffFrom='BASE',diffTo='WORKING';
let readGeneration=0,readPending=false,diffResult=null,diffError='',historyResult=null,historyStarts=[],historyStart='HEAD',historySelected=null,historyError='',lastFocus=null,propertyResult=null,propertyError='';
function invalidateReads(closeHistory=true){diffFrom='BASE';diffTo='WORKING';readGeneration++;readPending=false;diffResult=null;diffError='';propertyResult=null;propertyError='';historyResult=null;historyStarts=[];historyStart='HEAD';historySelected=null;historyError='';if(closeHistory&&modalView==='history'&&$('modal').open)$('modal').close();}
function readScope(){const row=state.rows.find(r=>r.id===state.focused);return row?{workspaceId:state.workspaceId,selectionVersion:state.selectionVersion,snapshotToken:state.snapshotToken,id:row.id,path:row.path}:null;}
function readsAvailable(){return state.mode==='live'&&!state.busy&&!state.failed&&state.fileReadsEnabled&&typeof state.snapshotToken==='string'&&canReadFile(state.rows.find(r=>r.id===state.focused));}
function showDiff(){invalidateReads();state.preview='diff';renderPreview();$('preview-panel').classList.remove('collapsed');}
let modalView='',workspaceRequest=0,workspaceCatalog=null;
let changelistGeneration=0,changelistPlan=null,changelistApplying=false,changelistSelection='';
function selectionKey(){return JSON.stringify([...state.selected].sort());}
function invalidateChangelist(message='选区或状态已改变，请重新预检。'){changelistGeneration++;changelistPlan=null;if($('changelist-confirm'))$('changelist-confirm').disabled=true;if($('changelist-result'))$('changelist-result').textContent=message;}
function changelistsAvailable(){const rows=reviewSelection(state.rows,state.selected);return rows.length===state.selected.size&&rows.every(canRelabelFile)&&!state.busy&&!state.failed&&state.selected.size>0&&state.selected.size<=100&&(state.mode==='demo'||state.mode==='live'&&state.changelistWritesEnabled===true&&typeof state.snapshotToken==='string');}
function notify(message){$('toast').textContent=message;$('toast').classList.remove('hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.add('hidden'),3500);}
function log(message,level='INFO'){state.logs.push({time:new Date().toLocaleTimeString('en-GB'),message,level});state.logs=state.logs.slice(-100);$('activity-log').innerHTML=state.logs.map(r=>`<div class="log-row"><span class="log-time">${esc(r.time)}</span><span class="log-level ${r.level==='WARN'?'warn':''}">${esc(r.level)}</span><span class="log-text">${esc(r.message)}</span></div>`).join('');$('log-counter').textContent=state.logs.length;$('activity-log').scrollTop=$('activity-log').scrollHeight;}
const matches=()=>filteredRows(state.rows,state);
const displayed=()=>matches().filter(r=>!state.collapsed.has(r.changelist||''));
const statusTag=row=>{const s=stateFor(row);return `<span class="status-tag ${s.tone}"><b>${esc(s.short)}</b>${esc(s.label)}</span>`;};
const fileIcon=row=>/\.(png|jpg|jpeg|svg)$/i.test(row.path)?`<span class="file-icon">${icon('image')}</span>`:/\.cs$/i.test(row.path)?'<span class="file-type">C#</span>':`<span class="file-icon">${icon('file')}</span>`;
function renderTree(){
 const counts=folderCountsFor(state.rows);
 const sorted=[...counts.keys()].sort((a,b)=>a.localeCompare(b));
 $('folder-tree').innerHTML=sorted.map((folder,i)=>{const depth=folder?folder.split('/').length:0;const count=counts.get(folder);return `<button class="folder-item ${folder===state.folder?'selected':''}" data-folder-index="${i}" aria-label="筛选目录 ${esc(folder||'全部目录')}"><span class="tree-arrow">${folder?'›':'⌄'}</span>${icon(folder?'folder':'computer')}<span class="folder-label">${esc(folder?fileName(folder):state.workspace)}</span><span class="folder-count">${count}</span></button>`;}).join('');
 // No inline style is needed under strict CSP: depth encoded with finite classes.
 $('folder-tree').querySelectorAll('button').forEach((button,i)=>{button.removeAttribute('style');button.classList.add(`depth-${Math.min(4,sorted[i]?sorted[i].split('/').length:0)}`);button.onclick=()=>{state.folder=sorted[i];state.collapsed.clear();render();};});
}
function render(){
 closeFileMenu();
 if(changelistSelection!==selectionKey()){changelistSelection=selectionKey();invalidateAdd();invalidateFileOp();invalidateChangelist();invalidateShelf();invalidateLock();invalidateCommit();}
 updateShelfButtons();updateCommitButtons();updateLockButtons();updateAddButton();updateFileOpButtons();
 $('move-changelist').disabled=!changelistsAvailable();
 $('move-changelist').title=state.mode==='live'&&!state.changelistWritesEnabled?'需由启动参数显式启用本地 changelist 写入':'仅支持最多 100 个已版本化普通文件；冲突、缺失、外部引用、切换或锁定项不可用，目录与链接由预检拒绝';
 if(lastFocus!==state.focused){invalidateReads();lastFocus=state.focused;}
 const rows=matches(),groups=groupsFor(rows),visible=displayed(),counts=summary(state.rows);
 $('file-body').innerHTML=groups.map(([name,items],groupIndex)=>`<tr class="group-row"><td colspan="5"><button data-group-index="${groupIndex}" aria-expanded="${!state.collapsed.has(name)}">${icon(state.collapsed.has(name)?'chevron-right':'chevron-down')}${icon(name?'layers':'folder')}<span class="group-label">${esc(name||'默认变更列表')}</span><span class="group-count">${items.length} 个文件</span><span class="group-meta">${name?'本地标签':'未分组'}</span></button>${name&&state.commitsEnabled===true?`<button class="group-commit" data-commit-group="${groupIndex}" ${!commitsAvailable()?'disabled':''}>提交完整变更列表…</button>`:''}</td></tr>${state.collapsed.has(name)?'':items.map(row=>`<tr class="file-row ${state.selected.has(row.id)?'selected':''} ${state.focused===row.id?'focused':''}" tabindex="0" data-row-index="${state.rows.indexOf(row)}" aria-label="${esc(fileName(row.path))}，${esc(stateFor(row).label)}" aria-selected="${state.selected.has(row.id)}"><td class="check-cell"><input type="checkbox" ${state.selected.has(row.id)?'checked':''} aria-label="选择 ${esc(row.path)}"></td><td title="${esc(row.path)}">${fileIcon(row)}${esc(fileName(row.path))}</td><td>${statusTag(row)}</td><td class="file-path path-column" title="${esc(parentPath(row.path))}">${esc(parentPath(row.path))}</td><td class="revision-cell">${row.revision===null||row.revision===undefined?'—':`r${esc(row.revision)}`}</td></tr>`).join('')}`).join('');
 $('file-body').querySelectorAll('[data-group-index]').forEach(b=>b.onclick=()=>{const group=groups[Number(b.dataset.groupIndex)][0];state.collapsed.has(group)?state.collapsed.delete(group):state.collapsed.add(group);render();});
 $('file-body').querySelectorAll('[data-commit-group]').forEach(b=>b.onclick=()=>openCommit('changelist',groups[Number(b.dataset.commitGroup)][0]));
 $('file-body').querySelectorAll('[data-row-index]').forEach(tr=>{
  const row=state.rows[Number(tr.dataset.rowIndex)];
  tr.onclick=event=>{state.focused=row.id;if(event.target.type==='checkbox'||event.ctrlKey||event.metaKey){state.selected.has(row.id)?state.selected.delete(row.id):state.selected.add(row.id);}else{state.selected=new Set([row.id]);}render();};
  tr.oncontextmenu=event=>openFileMenu(event,row);
  tr.addEventListener('keydown',event=>{if(event.key==='ContextMenu'||event.shiftKey&&event.key==='F10'){event.stopImmediatePropagation();openFileMenu(event,row,true);}});
  tr.ondblclick=()=>{showDiff();};
  tr.onkeydown=e=>{if(!['ArrowDown','ArrowUp',' '].includes(e.key))return;e.preventDefault();state.focused=row.id;if(e.key===' '){state.selected.has(row.id)?state.selected.delete(row.id):state.selected.add(row.id);}else{const idx=visible.findIndex(r=>r.id===row.id),target=visible[Math.max(0,Math.min(visible.length-1,idx+(e.key==='ArrowDown'?1:-1)))];state.focused=target.id;}render();const focusIndex=state.rows.findIndex(r=>r.id===state.focused);$('file-body').querySelector(`[data-row-index="${focusIndex}"]`)?.focus();};
 });
 const allCollapsed=groups.length>0 && groups.every(([name])=>state.collapsed.has(name));$('collapse-all').innerHTML=`${icon('collapse')}${allCollapsed?'展开分组':'折叠分组'}`;
 const selectedVisible=visible.filter(r=>state.selected.has(r.id)).length,hiddenCount=state.selected.size-selectedVisible;
 $('select-visible').checked=visible.length>0 && selectedVisible===visible.length;$('select-visible').indeterminate=selectedVisible>0 && selectedVisible<visible.length;
 $('select-visible').disabled=!visible.length;$('empty-state').classList.toggle('hidden',rows.length>0);
 $('visible-count').textContent=`显示 ${rows.length} / ${state.rows.length} 项${visible.length!==rows.length?` · ${rows.length-visible.length} 项已折叠`:''}`;
 $('selection-summary').textContent=`已选择 ${state.selected.size} 项${hiddenCount?`（含 ${hiddenCount} 项不可见）`:''}`;
 for(const id of ['pending-count','tab-count','headline-count','filter-all-count'])$(id).textContent=counts.total;
 for(const id of ['conflict-count','filter-conflict-count'])$(id).textContent=counts.conflicted;
 $('selected-count').textContent=state.selected.size;$('review').disabled=!state.selected.size||state.failed;$('view-diff').disabled=!state.focused||state.failed;
 $('scope-note').textContent=`${groupsFor(state.rows).length} 个本地变更列表 · ${state.mode==='demo'?'示例工作副本':'仅当前工作副本'}`;
 $('status-summary').textContent=state.failed?'状态未知':state.mode==='select'?'待选工作区':`${counts.total} 项状态 · ${counts.conflicted} 个冲突`;
 $('empty-state').querySelector('strong').textContent=state.failed?'状态未知，未显示结果':state.mode==='select'?'请选择一个授权工作区':'没有匹配的更改';
 $('empty-state').querySelector('p').textContent=state.failed?'读取失败不代表工作副本干净。':state.mode==='select'?'点击上方打开工作区，仅从已授权目录中选择。':'试试其他关键词，或清除目录和状态筛选。';
 $('folder-scope').classList.toggle('hidden',!state.folder);$('folder-scope').innerHTML=`目录范围：${esc(state.folder)} <button id="reset-folder">清除</button>`;if($('reset-folder'))$('reset-folder').onclick=()=>{state.folder='';render();};
 document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b.dataset.filter===state.filter));
 $('conflict-nav').classList.toggle('active',state.filter==='conflicted');$('pending-nav').classList.toggle('active',state.filter!=='conflicted');
 renderTree();renderDetail();renderPreview();
}
function renderDetail(){
 const row=state.rows.find(r=>r.id===state.focused);
 if(!row){$('detail-content').innerHTML='<div class="detail-section"><p>选择文件查看详情。</p></div>';return;}
 const s=stateFor(row),demo=state.mode==='demo';
 $('detail-content').innerHTML=`<div class="detail-hero"><div class="detail-file-icon">${icon(/\.cs$/.test(row.path)?'code':'file')}</div><h2>${esc(fileName(row.path))}</h2><div class="detail-path">${esc(row.path)}</div>${statusTag(row)}</div><section class="detail-section"><div class="detail-eyebrow">WORKING COPY</div><dl><dt>变更列表</dt><dd class="blue">${esc(row.changelist||'默认变更列表')}</dd><dt>BASE 修订</dt><dd>${row.revision===null||row.revision===undefined?'—':`r${esc(row.revision)}`}</dd><dt>最后作者</dt><dd>${esc(row.last_changed_author||'未返回')}</dd><dt>属性状态</dt><dd>${esc(row.props==='none'?'无属性更改':row.props)}</dd><dt>节点类型</dt><dd>${row.kind==='file'?(demo?'文件（示例）':'文件'):'状态未提供'}</dd></dl></section><div class="detail-warning">${icon(s===STATES.conflicted?'alert':'shield')}<span>${s===STATES.conflicted?'此项存在冲突。原型仅显示状态，不提供自动解决或合并。':demo?'当前为演示数据。差异和历史也是示例，未读取真实工程。':'可显式读取只读差异或历史。历史与修订比较会访问仓库；本地 changelist 与 Shelf 需启动时显式启用、预检及独立确认。Shelf 恢复会修改文件；不会提交。'}</span></div><button class="detail-action" id="detail-diff">${icon('diff')}查看差异预览</button><button class="detail-action" id="detail-properties">${icon('file')}查看本地属性</button><button class="detail-action" id="detail-history">${icon('history')}查看文件历史${demo?'（示例）':''}</button><div class="detail-footer">SVN changelist 是工作副本本地标签，<br>不会生成服务器编号提交单。</div>`;
 $('detail-diff').onclick=()=>{showDiff();};$('detail-properties').onclick=showProperties;$('detail-history').onclick=showHistory;
}
function renderPreview(){
 const row=state.rows.find(r=>r.id===state.focused),demo=state.mode==='demo';
 $('preview-panel').classList.toggle('live-preview',state.mode==='live'&&['diff','properties'].includes(state.preview));
 document.querySelectorAll('[data-preview]').forEach(b=>b.classList.toggle('active',b.dataset.preview===state.preview));
 $('diff-filename').textContent=row?fileName(row.path):'未选择文件';$('preview-kind').textContent=demo?'示例内容 · 非真实文件':state.preview==='properties'?'真实本地属性 · 显式读取':'真实只读差异 · 显式读取';
 $('diff-stats').textContent='';document.querySelector('.diff-endpoints').textContent=demo?'BASE → WORKING':diffResult?`${diffResult.fromRevision} → ${diffResult.toRevision}`:'按下方修订读取';
 if(!row){$('diff-content').innerHTML='<div class="diff-empty">选择文件以查看可用预览。</div>';return;}
 if(state.preview==='properties'&&!demo){document.querySelector('.diff-endpoints').textContent='BASE → WORKING · 本地属性';renderProperties();return;}
 if(state.preview==='properties'){$('diff-content').innerHTML=`<div class="properties-grid"><span>相对路径</span><span>${esc(row.path)}</span><span>文本状态</span><span>${esc(row.item)}</span><span>属性状态</span><span>${esc(row.props)}</span><span>数据来源</span><span>${demo?'内置示例状态；未提供真实属性值':'本地 svn status --xml'}</span></div>`;return;}
 if(!demo){renderLiveDiff();return;}
 if(demo && row.id===demoRows[0].id){
  let old=24,working=24;$('diff-stats').innerHTML='+5 <span>−2</span>';
  $('diff-content').innerHTML=demoDiff.map(([kind,code])=>{const oldNo=kind==='h'||kind==='+'?'':old++,newNo=kind==='h'||kind==='-'?'':working++;return `<div class="diff-line ${kind==='+'?'add':kind==='-'?'remove':kind==='h'?'hunk':''}"><span class="line-no">${oldNo}</span><span class="line-no">${newNo}</span><span class="line-sign">${kind==='h'?'':esc(kind)}</span><span class="line-code">${esc(code)}</span></div>`;}).join('');
 }else{$('diff-content').innerHTML=`<div class="diff-empty">${demo?'该示例没有内置差异内容。选择 PlayerController.cs 可体验文本差异预览。':'此原型只接入本地 status 元数据，未读取真实文件内容或执行 diff。'}<br>文件：${esc(row.path)}</div>`;}
}
function modal(title,content){invalidateCheckout();$('checkout-confirm').classList.add('hidden');closeFileMenu();$('file-op-confirm').classList.add('hidden');$('add-confirm').classList.add('hidden');invalidateAdd();invalidateFileOp();$('lock-confirm').classList.add('hidden');invalidateLock();invalidateCommit();$('commit-confirm').classList.add('hidden');invalidateShelf();$('shelf-confirm').classList.add('hidden');$('changelist-confirm').classList.add('hidden');invalidateChangelist();invalidateReads();renderPreview();modalView='generic';workspaceRequest++;$('modal-title').textContent=title;$('modal-content').innerHTML=content;if(!$('modal').open)$('modal').showModal();}
function showHistory(){
 const row=state.rows.find(r=>r.id===state.focused);
 if(!row)return notify('先选择一个文件。');
 if(state.mode!=='demo'){modal(`${fileName(row.path)} · 只读历史`,'');modalView='history';renderHistory();return;}
 if(row.id!==demoRows[0].id)return modal('没有此文件的示例历史',`<p>${esc(row.path)}</p><p>仅 PlayerController.cs 内置了演示历史。没有向真实仓库查询。</p>`);
 modal('PlayerController.cs · 示例历史',`<p class="history-disclaimer">以下是虚构历史，用于体验布局。未访问真实仓库。</p><div class="history-list">${demoHistory.map(h=>`<div class="history-item"><strong>r${h.revision}</strong><small>${esc(h.author)} · ${esc(h.time)}</small><p>${esc(h.message)}</p></div>`).join('')}</div>`);
}
function diffMarkup(){
 if(!diffResult)return '';
 const warning=diffResult.undecodableBytes||diffResult.escapedControls?`<p class="read-error diff-encoding-warning" role="alert"><strong>显示转义警告：不是完整、精确的补丁。</strong> 文件名元数据或文件内容中有无法按 UTF-8 解码的字节（${diffResult.undecodableBytes} 个），或需转义的控制字符（${diffResult.escapedControls} 个）。可见字节转义仅用于展示；有效 UTF-8 正文已保留，未知字节未猜测为其他编码。请勿将此预览直接用作补丁。</p>`:'';
 return `${warning}<p class="read-notice">${esc(diffResult.fromRevision)} → ${esc(diffResult.toRevision)} · ${diffResult.returnedUtf8Bytes} / ${diffResult.sourceUtf8Bytes} UTF-8 字节${diffResult.truncated?' · 预览已截断（256 KiB 上限）':''}。不包含二进制负载。</p>${diffResult.text?`<pre class="live-diff-text">${esc(diffResult.text)}</pre>`:'<p class="read-notice">此次命令没有返回文本差异；这不证明二进制内容相同，也不代表整个工作副本干净。</p>'}`;
}
function renderLiveDiff(){
 const enabled=readsAvailable();
 $('diff-content').innerHTML=`<div class="live-read"><div class="revision-controls"><label>从 <input id="diff-from" value="${esc(diffFrom)}" aria-label="起始修订"></label><label>到 <input id="diff-to" value="${esc(diffTo)}" aria-label="结束修订"></label><button class="subtle-button" id="read-diff" ${!enabled||readPending?'disabled':''}>读取差异</button></div><p class="read-notice read-network">BASE → WORKING：本地读取；其他修订：访问仓库，使用现有 SVN 凭据。点击「读取差异」才发起请求。</p><details class="read-restrictions"><summary>读取范围与限制</summary><p>支持 BASE、HEAD、非负整数；WORKING 仅限右侧。不支持 BASE → BASE；本地修改请选择 BASE → WORKING。网络挂载目录的本地读取也可能访问文件系统。使用现有 SVN 配置；不写工作副本。预览最多 256 KiB，不包含二进制负载，截断会明确标记。</p></details>${!enabled?'<p class="read-notice">当前条目或状态不支持文件读取。请确保已启用配套严格后端并刷新状态。未版本化、忽略、external、switched 与 file external 不可用；服务端会再次验证。</p>':''}${readPending?'<p role="status">正在读取… 关闭或导航后不再显示结果；服务端命令可能持续至超时。</p>':''}${diffError?`<p class="read-error" role="alert">${esc(diffError)} 未显示旧结果；失败不代表没有差异。</p>`:''}${diffMarkup()}</div>`;
 for(const id of ['diff-from','diff-to'])$(id).oninput=event=>editReadRevision(event,false);
 $('read-diff').onclick=()=>{let revisions;try{revisions=parseComparisonPair($('diff-from').value,$('diff-to').value);}catch(error){diffResult=null;diffError=error.message;renderLiveDiff();return;}requestDiff(revisions);};
}
function propertyReadsAvailable(){return state.mode==='live'&&!state.busy&&!state.failed&&state.propertyReadsEnabled===true&&typeof state.snapshotToken==='string'&&!!state.snapshotToken&&canReadProperties(state.rows.find(row=>row.id===state.focused));}
function showProperties(){invalidateReads();state.preview='properties';renderPreview();$('preview-panel').classList.remove('collapsed');}
const PROPERTY_CHANGES={added:'新增属性',deleted:'已移除属性',modified:'值已修改',unchanged:'未改变',unbased:'新增文件，无 BASE'};
function propertyValueMarkup(value){
 if(value===null)return '<span class="property-absent">无此属性</span>';
 const notes=[`${value.byteLength} 原始字节`,value.kind==='binary'?'二进制值，不显示负载':null,value.textTruncated?'显示已截断':null,value.redacted?'URL 凭据已遮蔽':null,value.escapedControls?`${value.escapedControls} 个控制字符已转义`:null].filter(Boolean).join(' · ');
 return `<small class="property-value-note">${esc(notes)}</small>${value.kind==='text'?`<pre class="property-value-text">${value.text===''?'（空文本值）':esc(value.text)}</pre>`:''}<small class="property-digest">SHA-256 ${esc(value.sha256)}</small>`;
}
function renderProperties(){
 const available=propertyReadsAvailable();
 const result=propertyResult?`<p class="read-notice">${propertyResult.properties.length} 项本地属性。${propertyResult.baseAvailable?'变更判断依据完整原始属性字节，不依赖截断或遮蔽后的显示文本。':'此文件为新增文件，没有 BASE 属性；这不是读取失败或空 BASE。'} 仅供审查，不能作为精确导出或补丁。</p>${propertyResult.properties.length?`<div class="property-comparison">${propertyResult.properties.map(property=>`<section class="property-entry"><div class="property-heading"><strong>${esc(property.name)}</strong><span>${esc(PROPERTY_CHANGES[property.change])}</span></div><div class="property-values"><div><h4>BASE</h4>${!propertyResult.baseAvailable?'<span class="property-absent">新增文件，没有 BASE</span>':propertyValueMarkup(property.base)}</div><div><h4>WORKING</h4>${propertyValueMarkup(property.working)}</div></div></section>`).join('')}</div>`:'<p class="read-notice">当前明确文件在已读取的范围中没有显式属性；不推断父目录继承属性或其他文件。</p>'}`:'';
 $('diff-content').innerHTML=`<div class="live-read"><button id="read-properties" class="subtle-button" ${!available||readPending?'disabled':''}>读取本地属性对照</button><p class="read-notice">只读取当前文件的显式 WORKING / BASE 属性，不访问仓库，不修改属性，不读取父目录继承属性。点击按钮才发起请求；属性可能包含项目私有信息，请谨慎分享。</p>${!available?'<p class="read-notice">当前文件或配套后端不支持属性读取。请刷新有效状态；冲突、删除、替换、复制/移动、目录、外部引用和锁定条目不可用。</p>':''}${readPending?'<p role="status">正在读取本地属性… 切换文件、预览或关闭面板后不采用迟到结果。</p>':''}${propertyError?`<p class="read-error" role="alert">${esc(propertyError)} 未显示旧结果；失败不代表没有属性。</p>`:''}${result}</div>`;
 $('read-properties').onclick=requestProperties;
}
async function requestProperties(){
 if(readPending||!propertyReadsAvailable()||state.preview!=='properties')return;
 const scope=readScope(),expectedBaseAvailable=state.rows.find(row=>row.id===state.focused).item!=='added',generation=++readGeneration;readPending=true;propertyResult=null;propertyError='';renderProperties();
 try{const {path,...request}=scope,data=await api('/api/file-properties',request);
  if(generation!==readGeneration||state.preview!=='properties'||!sameReadScope(scope,readScope()))return;
  propertyResult=validatePropertyResponse(data,scope,expectedBaseAvailable);log(`本地属性审查完成：${data.properties.length} 项；未访问仓库或修改属性。`);
 }catch(error){if(generation!==readGeneration||state.preview!=='properties'||!sameReadScope(scope,readScope()))return;propertyError=error.message;}
 finally{if(generation===readGeneration){readPending=false;renderPreview();}}
}

function editReadRevision(event,history){
 const id=event.target.id,start=event.target.selectionStart,end=event.target.selectionEnd;
 const from=history?diffFrom:$('diff-from').value,to=history?diffTo:$('diff-to').value,revision=history?event.target.value:historyStart;
 invalidateReads(false);diffFrom=from;diffTo=to;historyStart=revision;
 if(history)renderHistory();else renderPreview();
 $(id).focus();$(id).setSelectionRange(start,end);
}
async function requestDiff(revisions,inHistory=false){
 if(readPending||!readsAvailable())return;
 if(!inHistory){diffFrom=revisions.fromRevision;diffTo=revisions.toRevision;}
 const scope=readScope(),generation=++readGeneration;readPending=true;diffResult=null;diffError='';
 const paint=()=>inHistory?renderHistory():renderPreview();paint();
 try{
  revisions=parseComparisonPair(revisions.fromRevision,revisions.toRevision);
  const {path,...body}=scope;const data=await api('/api/file-diff',{...body,...revisions});
  if(generation!==readGeneration||!sameReadScope(scope,readScope())||inHistory&&(!$('modal').open||modalView!=='history'))return;
  diffResult=validateFileResponse(data,scope,'diff',revisions);log(`只读差异完成：${revisions.fromRevision} → ${revisions.toRevision}${data.truncated?'（截断预览）':''}。`);
 }catch(error){if(generation!==readGeneration||!sameReadScope(scope,readScope()))return;diffError=error.message;}
 finally{if(generation===readGeneration){readPending=false;paint();}}
}
function renderHistory(){
 if(modalView!=='history'||!$('modal').open)return;
 const entry=historyResult?.entries.find(e=>e.revision===historySelected);
 $('modal-content').innerHTML=`<p class="history-disclaimer">历史读取与下方修订比较会访问当前文件的 SVN 仓库，使用现有 SVN 配置与凭据。不会提交或写入工作副本；不会跟随此列表的其他路径发起读取。每页最多 50 条，仅保留当前页；返回较新页会重新请求。</p><div class="revision-controls"><label>起始修订 <input id="history-start" value="${esc(historyStart)}" aria-label="历史起始修订"></label><button class="subtle-button" id="read-history" ${!readsAvailable()||readPending?'disabled':''}>读取历史（访问仓库）</button></div>${!readsAvailable()?'<p class="read-error">当前条目或状态不支持读取历史。请确保启用配套严格后端并刷新状态；未版本化、忽略和外部／切换条目不可用。</p>':''}${readPending?'<p role="status">正在读取… 关闭后不会重新打开此窗口。</p>':''}${historyError?`<p class="read-error" role="alert">${esc(historyError)} 历史未知，未显示缓存结果。</p>`:''}${historyResult?`<div class="history-pagination"><button id="history-newer" class="subtle-button" ${!historyStarts.length||readPending?'disabled':''}>较新一页（访问仓库）</button><span>${historyResult.entries.length} 条 · 起始 ${esc(historyStart)}</span><button id="history-older" class="subtle-button" ${!historyResult.hasMore||readPending?'disabled':''}>较旧一页（访问仓库）</button></div>${!historyResult.entries.length?'<p>此次请求未返回历史条目；不推断其他路径或范围。</p>':''}<div class="history-list">${historyResult.entries.map(e=>`<button class="history-item history-select ${e.revision===historySelected?'active':''}" data-revision="${esc(e.revision)}" ${readPending?'disabled':''}><strong>r${esc(e.revision)}</strong><small>${esc(e.author)} · ${esc(e.date)}</small><p>${esc(e.message)}</p></button>`).join('')}</div>`:''}${entry?`<section class="history-detail"><h3>r${esc(entry.revision)} · 修订详情</h3><p>${esc(entry.author)} · ${esc(entry.date)}</p><p class="commit-message">${esc(entry.message)}</p><p>整次提交的变更路径，可能超出当前文件范围（仅展示，不可跳转读取）：</p><ul>${entry.changedPaths.map(p=>`<li>${esc(p.action)} ${esc(p.path)} · ${esc(p.kind)}${p.copyfrom_path!==null?` ← ${esc(p.copyfrom_path)} @ ${esc(p.copyfrom_rev)}`:''}</li>`).join('')}</ul><p>只比较当前状态行文件 ${esc(readScope()?.path)}；不会比较其他变更路径。访问仓库可能因增删或历史路径变化而失败。</p><button id="history-compare" class="subtle-button" ${BigInt(entry.revision)===0n||readPending?'disabled':''}>比较 r${BigInt(entry.revision)>0n?BigInt(entry.revision)-1n:0n} → r${esc(entry.revision)}（访问仓库）</button></section>`:''}${diffError?`<p class="read-error" role="alert">${esc(diffError)} 未显示旧差异；失败不代表没有差异。</p>`:''}${diffMarkup()}`;
 $('history-start').oninput=event=>editReadRevision(event,true);
 $('read-history').onclick=()=>{try{const start=parseRevision($('history-start').value,{history:true});requestHistory(start,[]);}catch(error){historyResult=null;diffResult=null;historyError=error.message;renderHistory();}};
 if($('history-older'))$('history-older').onclick=()=>requestHistory(historyResult.nextStartRevision,[...historyStarts,historyStart].slice(-100));
 if($('history-newer'))$('history-newer').onclick=()=>requestHistory(historyStarts.at(-1),historyStarts.slice(0,-1));
 document.querySelectorAll('[data-revision]').forEach(button=>button.onclick=()=>{readGeneration++;diffResult=null;diffError='';historySelected=button.dataset.revision;renderHistory();});
 if($('history-compare'))$('history-compare').onclick=()=>requestDiff({fromRevision:String(BigInt(entry.revision)-1n),toRevision:entry.revision},true);
}
async function requestHistory(startRevision,back){
 if(readPending||!readsAvailable())return;
 const scope=readScope(),generation=++readGeneration;readPending=true;historyResult=null;historySelected=null;historyError='';diffResult=null;diffError='';historyStart=startRevision;historyStarts=back;renderHistory();
 try{
  const {path,...body}=scope;const data=await api('/api/file-history',{...body,startRevision});
  if(generation!==readGeneration||!sameReadScope(scope,readScope())||!$('modal').open||modalView!=='history')return;
  historyResult=validateFileResponse(data,scope,'history',{startRevision});log(`只读历史完成：${data.entries.length} 条；已访问仓库。`);
 }catch(error){if(generation!==readGeneration||!sameReadScope(scope,readScope()))return;historyError=error.message;}
 finally{if(generation===readGeneration){readPending=false;renderHistory();}}
}

function review(){const rows=reviewSelection(state.rows,state.selected);modal(`审查选中项 · ${rows.length} 个文件`,`<p>这是只读审查，不会提交。列表包含已选择但被筛选或折叠隐藏的文件；不会自动扩展目录范围。</p><div>${rows.map(r=>`<div class="review-file">${statusTag(r)}<span>${esc(r.path)}</span></div>`).join('')}</div><p class="history-disclaimer">${state.mode==='demo'?'当前为演示数据。':'当前为真实本地状态。'} 普通还原未接入；本地归组、Shelf 与仓库提交需分别启用、预检并独立确认。</p>`);log(`打开只读审查：${rows.length} 项明确选择；未发送写操作。`);}
function changelistFiles(rows){return rows.map(row=>`<li><span class="changelist-path">${esc(row.path)}</span><span>来源：${esc(row.changelist||'默认变更列表（未分组）')}</span></li>`).join('');}
function openChangelists(){
 if(!changelistsAvailable())return;
 const rows=reviewSelection(state.rows,state.selected),names=groupsFor(state.rows).map(([name])=>name).filter(Boolean);
 modal('移动到本地变更列表',`<p>${state.mode==='demo'?'演示模拟：只更改当前页面数据，不发送 HTTP 写请求。':'真实本地操作：仅修改当前 SVN 工作副本的原生 changelist 标签。'} 没有服务器 CL 编号，不修改文件内容，不提交，不创建 Shelf。</p><p>只操作以下明确选中的文件（含筛选或折叠隐藏项），不自动展开目录。目录、链接、硬链接、嵌套工作副本和不支持的状态会被预检拒绝。命名组为空时自动消失，不保存空组。预检不锁定文件，请勿同时使用其他 SVN 工具修改此选区。</p><ul id="changelist-files" class="changelist-files">${changelistFiles(rows)}</ul><div class="changelist-controls"><label>操作<select id="changelist-action"><option value="assign">移动到命名变更列表</option><option value="remove">移除标签，回到默认变更列表</option></select></label><label>已有名称或新名称<input id="changelist-name" list="changelist-names" autocomplete="off" placeholder="输入本地标签名称"><datalist id="changelist-names">${names.map(name=>`<option value="${esc(name)}"></option>`).join('')}</datalist></label></div><p>名称前后空白会移除，区分大小写，最多 128 个字符，不允许控制字符。</p><button id="changelist-preflight" class="subtle-button">预检明确选区</button><p id="changelist-result" role="status">先预检，再单独确认执行。</p>`);
 modalView='changelists';$('changelist-confirm').classList.remove('hidden');
 $('changelist-name').oninput=()=>invalidateChangelist('目标已编辑，请重新预检。');
 $('changelist-action').onchange=()=>{$('changelist-name').disabled=$('changelist-action').value==='remove';invalidateChangelist('操作已改变，请重新预检。');};
 $('changelist-preflight').onclick=preflightChangelists;
 $('changelist-confirm').onclick=applyChangelists;
}
async function preflightChangelists(){
 if(!changelistsAvailable()||modalView!=='changelists')return;
 invalidateChangelist();
 const rows=reviewSelection(state.rows,state.selected).map(row=>({...row}));
 let request;
 try{const action=$('changelist-action').value;request={workspaceId:state.workspaceId,selectionVersion:state.selectionVersion,snapshotToken:state.snapshotToken,ids:rows.map(row=>row.id),action,name:parseChangelistTarget(action,$('changelist-name').value)};}catch(error){$('changelist-result').textContent=error.message;return;}
 const generation=changelistGeneration,key=selectionKey(),demo=state.mode==='demo';
 invalidateReads();setBusy(true);renderPreview();$('changelist-preflight').disabled=true;$('changelist-result').textContent='正在预检；此步骤不修改标签。';$('changelist-files').innerHTML=changelistFiles(rows);
 try{
  const result=demo?{...request,mode:'live',planToken:'demo-local',expiresIn:120,localOnly:true,files:rows.map(row=>({id:row.id,path:row.path,fromChangelist:row.changelist||null}))}:await api('/api/changelist-preflight',request);
  if(generation!==changelistGeneration||modalView!=='changelists'||!$('modal').open||key!==selectionKey())return;
  validateChangelistPlan(result,request,rows);
  const plan={request,result,key,rows,demo,expiresAt:Date.now()+result.expiresIn*1000};changelistPlan=plan;
  $('changelist-result').textContent=`预检通过：${rows.length} 个明确文件 → ${request.action==='remove'?'默认变更列表（移除标签）':`本地标签「${request.name}」`}。${demo?'仅演示模拟。':''} 请核对上方完整路径和来源；确认在 120 秒后失效。`;
  $('changelist-confirm').disabled=false;
  setTimeout(()=>{if(changelistPlan===plan)invalidateChangelist('预检已过期，请重新预检。');},result.expiresIn*1000);
 }catch(error){if(generation===changelistGeneration&&modalView==='changelists'&&$('modal').open)$('changelist-result').textContent=`预检失败：${error.message} 未发送执行请求。`;}
 finally{setBusy(false);renderPreview();if($('changelist-preflight'))$('changelist-preflight').disabled=false;}
}
async function applyChangelists(){
 const plan=changelistPlan;
 if(!plan||!changelistsAvailable()||changelistApplying||modalView!=='changelists'||!$('modal').open)return;
 if(plan.key!==selectionKey()||Date.now()>=plan.expiresAt){invalidateChangelist('预检已过期或选区已改变，请重新预检。');return;}
 const {request}=plan;
 let target;
 try{target=parseChangelistTarget($('changelist-action').value,$('changelist-name').value);}catch(error){invalidateChangelist(error.message);return;}
 if(request.action!==$('changelist-action').value||request.name!==target||request.workspaceId!==state.workspaceId||request.selectionVersion!==state.selectionVersion||request.snapshotToken!==state.snapshotToken){invalidateChangelist();return;}
 changelistPlan=null;changelistApplying=true;invalidateReads();setBusy(true);renderPreview();
 for(const id of ['changelist-confirm','changelist-preflight','changelist-action','changelist-name'])$(id).disabled=true;
 $('changelist-result').textContent='正在修改本地标签… 关闭窗口不会取消已发送请求；请等待结果，不要重复执行。';
 try{
  if(plan.demo){const ids=new Set(request.ids);state.rows=state.rows.map(row=>ids.has(row.id)?{...row,changelist:request.name}:row);render();}
  else{
   const payload=await api('/api/changelist-apply',{workspaceId:request.workspaceId,selectionVersion:request.selectionVersion,snapshotToken:request.snapshotToken,planToken:plan.result.planToken,confirmed:true});
   const result=payload?.changelistResult;
   if(payload.mode!=='live'||payload.workspaceId!==request.workspaceId||payload.selectionVersion!==request.selectionVersion||typeof payload.snapshotToken!=='string'||!payload.snapshotToken||payload.snapshotToken===request.snapshotToken||!result||result.action!==request.action||result.name!==request.name||result.count!==request.ids.length)throw Error('执行响应身份或结果无效。');
   applySnapshot(payload);
  }
  const message=`${plan.demo?'演示模拟完成':'本地标签修改完成'}：${request.ids.length} 项 → ${request.action==='remove'?'默认变更列表':request.name}；未提交、未修改文件内容。`;
  log(message);notify(message);if(modalView==='changelists'&&$('modal').open)$('changelist-result').textContent=message;
 }catch(error){
  const message=`本地归组执行结果未能确认，可能已部分或全部生效。请显式刷新状态并重新审查；不会自动重试。${error.message}`;
  failSnapshot(message);log(message,'WARN');notify('归组结果未知，请刷新并重新审查。');if(modalView==='changelists'&&$('modal').open)$('changelist-result').textContent=message;
 }finally{changelistApplying=false;setBusy(false);render();}
}

let shelfGeneration=0,shelfPlan=null,shelfApplying=false,shelfChoice=null;
function shelfScope(){return {workspaceId:state.workspaceId,selectionVersion:state.selectionVersion,snapshotToken:state.snapshotToken};}
function shelvesAvailable(){return state.mode==='live'&&state.shelfWritesEnabled===true&&!state.failed&&!state.busy&&typeof state.snapshotToken==='string'&&!!state.snapshotToken;}
function shelfCreateAvailable(){const rows=reviewSelection(state.rows,state.selected);return shelvesAvailable()&&rows.length===state.selected.size&&rows.length>0&&rows.length<=100&&rows.every(canShelfFile);}
function updateShelfButtons(){if(!$('create-shelf'))return;$('create-shelf').disabled=!shelfCreateAvailable();$('browse-shelves').disabled=!shelvesAvailable();$('create-shelf').title=$('browse-shelves').title='仅真实模式且启动时显式启用 Shelf 写入可用；不提供模拟备份。';}
function invalidateShelf(message='范围或状态已改变，请重新预检。'){shelfGeneration++;shelfPlan=null;if($('shelf-confirm'))$('shelf-confirm').disabled=true;if($('shelf-result'))$('shelf-result').textContent=message;}
function shelfFilesMarkup(files){return `<ol class="shelf-files">${files.map(f=>`<li><strong>${esc(f.path)}</strong><span>${esc(f.state||f.item)} · ${esc(f.kind||'待核验')} ${Number.isSafeInteger(f.size)?`· ${f.size} 字节`:''}</span></li>`).join('')}</ol>`;}
const shelfWarning='<p class="workspace-warning">仅当前工作副本的本地 Shelf，不是服务器提交单。创建只备份，不清除源文件；恢复须满足原始 BASE、干净目标且无冲突，始终保留备份。不支持目录或部分恢复。操作期间请勿在其他程序或窗口编辑工作副本。</p>';
function openCreateShelf(){
 if(!shelfCreateAvailable())return;
 shelfChoice=null;const rows=reviewSelection(state.rows,state.selected);
 modal('创建本地 Shelf',`${shelfWarning}<p>以下为全部 ${rows.length} 个已选文件，包括筛选或折叠后不可见的选项。不会扩大选区。</p><label class="shelf-name-label">Shelf 名称 <input id="shelf-name" maxlength="128" autocomplete="off" placeholder="输入便于识别的备份名称"></label><div id="shelf-files">${shelfFilesMarkup(rows)}</div><button class="subtle-button" id="shelf-preflight">预检完整选区</button><p id="shelf-result" role="status">预检不会创建备份。预检通过后需单独确认。</p>`);
 modalView='shelf-create';$('shelf-name').oninput=()=>invalidateShelf('名称已改变，请重新预检。');$('shelf-preflight').onclick=preflightShelf;$('shelf-confirm').textContent='确认创建本地备份';$('shelf-confirm').classList.remove('hidden');
}
async function openShelfBrowser(){
 if(!shelvesAvailable())return;
 shelfChoice=null;modal('本地 Shelf',`${shelfWarning}<p id="shelf-result" role="status">正在读取此工作副本的本地 Shelf…</p>`);modalView='shelf-browser';
 const generation=shelfGeneration,scope=shelfScope();setBusy(true);
 try{
  const data=await api('/api/shelves-list',scope);
  if(generation!==shelfGeneration||!sameShelfScope(scope,shelfScope())||modalView!=='shelf-browser'||!$('modal').open)return;
  if(data?.mode!=='live'||!sameShelfScope(data,scope)||!Array.isArray(data.shelves))throw Error('Shelf 列表身份无效。');
  const seen=new Set();for(const shelf of data.shelves){if(!shelf||typeof shelf.id!=='string'||!shelf.id||seen.has(shelf.id)||typeof shelf.name!=='string')throw Error('Shelf 列表信息无效。');seen.add(shelf.id);if(!shelf.error){validateShelfFiles(shelf.files,shelf.totalBytes);if(shelf.entryCount!==shelf.files.length||typeof shelf.createdAt!=='string'||![1,2].includes(shelf.format))throw Error('Shelf 元数据无效。');}}
  $('modal-content').innerHTML=`${shelfWarning}<p>仅列出当前工作副本的本地备份。选择一个 Shelf 后可审查其完整范围。</p><div class="shelf-list">${data.shelves.map((s,i)=>`<button class="subtle-button shelf-item" data-shelf-index="${i}" ${s.error?'disabled':''}><strong>${esc(s.name)}</strong><span>${esc(s.createdAt||'')} · ${esc(s.entryCount??'?')} 个文件 · ${esc(s.format||'未知格式')}</span>${s.error?`<span class="read-error">不可恢复：${esc(s.error)}</span>`:''}</button>`).join('')||'<p>没有本地 Shelf。</p>'}</div><div id="shelf-preview"></div><p id="shelf-result" role="status">选择后仍需预检与独立确认，不会自动恢复。</p>`;
  document.querySelectorAll('[data-shelf-index]').forEach(button=>button.onclick=()=>{
   if(state.busy||shelfApplying)return;invalidateShelf('请核对完整备份范围后预检。');shelfChoice=data.shelves[Number(button.dataset.shelfIndex)];
   $('shelf-preview').innerHTML=`<h3>${esc(shelfChoice.name)} · 完整恢复</h3><p>${shelfChoice.files.length} 个文件 · ${shelfChoice.totalBytes} 字节；不能选择子集。</p><div id="shelf-files">${shelfFilesMarkup(shelfChoice.files)}</div><button class="subtle-button" id="shelf-preflight">预检完整恢复</button>`;
   $('shelf-preflight').onclick=preflightShelf;$('shelf-confirm').textContent='确认恢复整个 Shelf';$('shelf-confirm').classList.remove('hidden');
  });
 }catch(error){if(generation===shelfGeneration&&$('shelf-result'))$('shelf-result').textContent=`读取失败：${error.message} 请关闭后重新打开；未执行恢复。`;}
 finally{setBusy(false);}
}
async function preflightShelf(){
 const create=modalView==='shelf-create';if(!shelvesAvailable()||create&&!shelfCreateAvailable()||!create&&!shelfChoice)return;
 invalidateShelf();const scope=shelfScope(),key=selectionKey(),generation=shelfGeneration,view=modalView;
 let request;const files=create?reviewSelection(state.rows,state.selected):shelfChoice.files;
 try{request={...scope,operation:create?'create':'restore',...(create?{ids:files.map(f=>f.id),name:parseShelfName($('shelf-name').value)}:{shelfId:shelfChoice.id})};}catch(error){$('shelf-result').textContent=error.message;return;}
 setBusy(true);$('shelf-preflight').disabled=true;if($('shelf-name'))$('shelf-name').disabled=true;$('shelf-result').textContent='正在预检完整文件范围；尚未发送执行请求。';
 try{
  const result=await api('/api/shelf-preflight',request);
  if(generation!==shelfGeneration||!sameShelfScope(scope,shelfScope())||key!==selectionKey()||modalView!==view||!$('modal').open)return;
  validateShelfPlan(result,request,files);if(!create&&result.name!==shelfChoice.name)throw Error('Shelf 名称不匹配。');
  const plan={request,result,key,view,expiresAt:Date.now()+120000};shelfPlan=plan;
  $('shelf-files').innerHTML=shelfFilesMarkup(result.files);$('shelf-result').textContent=`预检通过：${result.name}，${result.files.length} 个文件，${result.totalBytes} 字节。${create?'确认后创建真实本地备份，源文件保持不变。':'确认后恢复以上整个 Shelf，备份仍保留。'} 请核对完整路径；确认 120 秒后失效。`;$('shelf-confirm').disabled=false;
  setTimeout(()=>{if(shelfPlan===plan)invalidateShelf('预检已过期，请重新预检。');},120000);
 }catch(error){if(generation===shelfGeneration&&$('shelf-result'))$('shelf-result').textContent=`预检失败：${error.message} 未发送执行请求。`;}
 finally{setBusy(false);if($('shelf-preflight'))$('shelf-preflight').disabled=false;if($('shelf-name'))$('shelf-name').disabled=false;}
}
function preserveShelfResult(message){$('shelf-last-result').textContent=message;$('shelf-last-result').classList.remove('hidden');if($('shelf-result'))$('shelf-result').textContent=message;log(message,'WARN');notify(message);}
async function applyShelf(){
 const plan=shelfPlan;if(!plan||!shelvesAvailable()||shelfApplying||!$('modal').open||modalView!==plan.view)return;
 if(!sameShelfScope(plan.request,shelfScope())||plan.key!==selectionKey()||Date.now()>=plan.expiresAt){invalidateShelf();return;}
 if(plan.request.operation==='create'&&parseShelfName($('shelf-name').value)!==plan.request.name||plan.request.operation==='restore'&&shelfChoice?.id!==plan.request.shelfId){invalidateShelf();return;}
 const generation=shelfGeneration;shelfPlan=null;shelfApplying=true;invalidateReads();setBusy(true);renderPreview();
 for(const id of ['shelf-confirm','shelf-preflight','shelf-name'])if($(id))$(id).disabled=true;
 $('shelf-result').textContent='请求已发送，正在执行。关闭窗口不等于取消；不要重复操作或同时编辑文件。';let verifiedResult=null;
 try{
  const payload=await api('/api/shelf-apply',{workspaceId:plan.request.workspaceId,selectionVersion:plan.request.selectionVersion,snapshotToken:plan.request.snapshotToken,planToken:plan.result.planToken,confirmed:true});
  const result=validateShelfResult(payload,plan.request,plan.result);verifiedResult=result;
  const stale=generation!==shelfGeneration||modalView!==plan.view||!$('modal').open||!sameShelfScope(plan.request,shelfScope());
  const message=shelfResultSummary(result,{statusRefreshed:payload.statusRefreshed,stale});preserveShelfResult(message);
  try{if(stale||!payload.statusRefreshed)failSnapshot(message);else applySnapshot(payload);}
  finally{if($('shelf-result'))$('shelf-result').textContent=message;}
 }catch(error){
  const message=verifiedResult?shelfResultSummary(verifiedResult,{statusRefreshed:false,stale:true}):'Shelf 执行结果未知，可能已部分或全部生效。请显式刷新并重新审查；不会自动重试。';
  preserveShelfResult(message);try{failSnapshot(message);}finally{if($('shelf-result'))$('shelf-result').textContent=message;}
 }finally{shelfApplying=false;setBusy(false);render();}
}

let commitGeneration=0,commitPlan=null,commitApplying=false,commitChoice=null;
function invalidateCommit(message='提交范围或说明已改变，请重新预检。'){commitGeneration++;commitPlan=null;if($('commit-confirm'))$('commit-confirm').disabled=true;if($('commit-result'))$('commit-result').textContent=message;}
function commitsAvailable(){return state.mode==='live'&&state.commitsEnabled===true&&!state.busy&&!state.failed&&typeof state.snapshotToken==='string'&&!!state.snapshotToken;}
function updateCommitButtons(){if(!$('commit-selected'))return;let valid=false;try{commitScopeRows(state.rows,state.selected);valid=true;}catch{}$('commit-selected').disabled=!commitsAvailable()||!valid;$('commit-selected').title='真实仓库写入；仅启动时显式启用后可用。只提交准确选区，不自动扩展变更列表。';document.querySelectorAll('[data-commit-group]').forEach(b=>b.disabled=!commitsAvailable());}
function commitFilesMarkup(rows){return `<ol class="commit-files">${rows.map(row=>`<li><strong>${esc(row.path)}</strong><span>${esc(row.item)} / ${esc(row.props)} · 来源：${esc(row.changelist||row.fromChangelist||'默认变更列表（未分组）')}</span></li>`).join('')}</ol>`;}
function openCommit(operation='selected',name=null){
 if(!commitsAvailable())return;let rows;try{rows=commitScopeRows(state.rows,state.selected,operation,name);}catch(error){notify(error.message);return;}
 modal(operation==='selected'?'提交明确选中项':'提交完整变更列表',`<p class="workspace-warning">这是真实仓库写入，将使用现有 SVN 配置和凭据。提交可能被仓库钩子拒绝；预检不检查远端是否过期，不锁定文件。提交期间请勿使用其他 SVN 工具或编辑这些文件。失败或网络超时可能意味着结果未知，不会自动重试。</p><p>${operation==='selected'?'仅提交以下准确选区，不会因同属一个变更列表而加入其他成员。':`提交命名变更列表「${esc(name)}」的全部成员，包括干净文件以及被搜索、目录筛选或折叠隐藏的成员；不会截取子集。`}共 ${rows.length} 个文件。</p><div id="commit-files">${commitFilesMarkup(rows)}</div><label class="commit-message-label">提交说明（必填，最多 8192 UTF-8 字节）<textarea id="commit-message" rows="5" aria-label="提交说明" placeholder="描述本次修改及原因"></textarea></label><label class="commit-keep"><input type="checkbox" id="commit-keep">提交后保留原生 changelist 标签</label><button class="subtle-button" id="commit-preflight">预检</button><p id="commit-result" role="status">预检不会提交；通过后必须另行确认仓库写入。</p>`);
 commitChoice={operation,name};modalView='commit';$('commit-confirm').classList.remove('hidden');$('commit-message').oninput=()=>{invalidateLock();invalidateCommit();};$('commit-keep').onchange=()=>{invalidateLock();invalidateCommit();};$('commit-preflight').onclick=preflightCommit;
}
function commitRequest(){const rows=commitScopeRows(state.rows,state.selected,commitChoice.operation,commitChoice.name);return {...shelfScope(),...commitChoice,ids:rows.map(r=>r.id),message:parseCommitMessage($('commit-message').value),keepChangelist:$('commit-keep').checked};}
async function preflightCommit(){
 if(!commitsAvailable()||modalView!=='commit'||!$('modal').open)return;invalidateLock();invalidateCommit();let request;try{request=commitRequest();}catch(error){$('commit-result').textContent=error.message;return;}
 const generation=commitGeneration,key=selectionKey();setBusy(true);$('commit-preflight').disabled=true;$('commit-result').textContent='正在预检完整范围；尚未提交到仓库。';
 try{const result=await api('/api/commit-preflight',request);
  if(generation!==commitGeneration||key!==selectionKey()||!sameShelfScope(request,shelfScope())||modalView!=='commit'||!$('modal').open)return;
  if(JSON.stringify(request)!==JSON.stringify(commitRequest()))return;
  validateCommitPlan(result,request,state.rows);const plan={request,result,key,expiresAt:Date.now()+result.expiresIn*1000};commitPlan=plan;
  $('commit-files').innerHTML=commitFilesMarkup(result.files);$('commit-result').textContent=`预检通过：${result.files.length} 个完整路径。未检查远端过期状态；请核对范围和说明，再确认提交到仓库。确认在 ${result.expiresIn} 秒后失效。`;$('commit-confirm').disabled=false;
  setTimeout(()=>{if(commitPlan===plan)invalidateCommit('提交预检已过期，请重新预检。');},result.expiresIn*1000);
 }catch(error){if(generation===commitGeneration&&modalView==='commit'&&$('modal').open)$('commit-result').textContent=`预检失败：${error.message} 未发送提交请求。`;}
 finally{setBusy(false);if($('commit-preflight'))$('commit-preflight').disabled=false;}
}
function preserveCommitResult(message){$('commit-last-result').textContent=message;$('commit-last-result').classList.remove('hidden');if($('commit-result'))$('commit-result').textContent=message;log(message,'WARN');notify(message);}
async function applyCommit(){
 const plan=commitPlan;if(!plan||commitApplying||!commitsAvailable()||modalView!=='commit'||!$('modal').open)return;
 try{if(Date.now()>=plan.expiresAt||plan.key!==selectionKey()||JSON.stringify(plan.request)!==JSON.stringify(commitRequest())){invalidateLock();invalidateCommit();return;}}catch(error){invalidateCommit(error.message);return;}
 const generation=commitGeneration;commitPlan=null;commitApplying=true;invalidateReads();setBusy(true);renderPreview();
 for(const id of ['commit-confirm','commit-preflight','commit-message','commit-keep'])$(id).disabled=true;
 $('commit-result').textContent='正在提交到仓库。关闭不等于取消；不要重复操作。';let completedResult=null;
 try{
  const payload=await api('/api/commit-apply',{workspaceId:plan.request.workspaceId,selectionVersion:plan.request.selectionVersion,snapshotToken:plan.request.snapshotToken,planToken:plan.result.planToken,confirmed:true});
  const result=validateCommitResult(payload,plan.request,plan.result.files);completedResult=result;
  const stale=generation!==commitGeneration||modalView!=='commit'||!$('modal').open||!sameShelfScope(plan.request,shelfScope());
  const message=commitResultSummary(result,{statusRefreshed:payload.statusRefreshed,stale});
  preserveCommitResult(message);
  try{if(stale||!payload.statusRefreshed)failSnapshot(message);else applySnapshot(payload);}
  finally{if($('commit-result')){$('commit-result').textContent=message;$('commit-result').setAttribute('role',result.warnings.length||result.outcome!=='committed'||stale?'alert':'status');}}
 }catch(error){
  const message=completedResult?commitResultSummary(completedResult,{statusRefreshed:false,stale:true}):'提交结果未知：未收到可验证的提交响应。仓库可能已写入。请显式刷新本地状态并检查仓库历史，再决定下一步；不会自动重试。';
  preserveCommitResult(message);try{failSnapshot(message);}finally{if($('commit-result'))$('commit-result').textContent=message;}
 }
 finally{commitApplying=false;setBusy(false);render();}
}

let lockGeneration=0,lockPlan=null,lockApplying=false,lockChoice='lock',lockInspectionRequired=false;
function invalidateLock(message='范围或说明已改变，请重新预检。'){lockGeneration++;lockPlan=null;if($('lock-confirm'))$('lock-confirm').disabled=true;if($('lock-result'))$('lock-result').textContent=message;}
function locksAvailable(){return !lockInspectionRequired&&state.mode==='live'&&state.locksEnabled===true&&!state.busy&&!state.failed&&typeof state.snapshotToken==='string'&&!!state.snapshotToken;}
function updateLockButtons(){if($('lock-inspected')){$('lock-inspected').classList.toggle('hidden',!lockInspectionRequired);$('lock-inspected').disabled=state.busy||state.failed||!state.snapshotToken;}let valid=false;try{lockScopeRows(state.rows,state.selected);valid=true;}catch{}for(const id of ['lock-selected','unlock-selected'])if($(id))$(id).disabled=!locksAvailable()||!valid;}
function openLock(action='lock'){
 if(!locksAvailable()||!['lock','unlock'].includes(action))return;let rows;try{rows=lockScopeRows(state.rows,state.selected);}catch(error){notify(error.message);return;}
 modal(action==='lock'?'锁定明确选区':'解锁明确选区',`<p class="workspace-warning">真实仓库操作，使用现有 SVN 配置与凭据。预检会联系仓库查询当前锁和修订，但不会改变锁，也不会将说明发送到仓库。仅在另行确认锁定时将说明发送到仓库。锁可能阻止他人提交，但不会阻止本地编辑；svn:needs-lock 可能改变本地文件写权限。多文件操作不是原子的，失败可能已有部分文件生效。不会强制解锁、抢锁或自动重试。请勿同时使用其他 SVN 工具操作这些文件。</p><p>完整明确选区 ${rows.length} 项，包含被筛选或折叠隐藏的成员；不扩大到整个变更列表。</p><div id="lock-files">${commitFilesMarkup(rows)}</div>${action==='lock'?'<label class="commit-message-label">锁定说明（可选，最多 4096 UTF-8 字节）<textarea id="lock-comment" rows="3" aria-label="锁定说明"></textarea></label>':''}<button id="lock-preflight" class="subtle-button">预检明确选区（访问仓库）</button><p id="lock-result" role="status">尚未查询仓库锁。工作副本管理锁不代表仓库锁。</p>`);
 lockChoice=action;modalView='lock';$('lock-confirm').textContent=action==='lock'?'确认锁定并发送说明到仓库':'确认解锁仓库文件';$('lock-confirm').classList.remove('hidden');if($('lock-comment'))$('lock-comment').oninput=()=>invalidateLock();$('lock-preflight').onclick=preflightLock;
}
function lockRequest(){return {...shelfScope(),action:lockChoice,ids:lockScopeRows(state.rows,state.selected).map(r=>r.id),comment:parseLockComment(lockChoice,lockChoice==='lock'?$('lock-comment').value:'')};}
async function preflightLock(){
 if(!locksAvailable()||modalView!=='lock'||!$('modal').open)return;invalidateLock();let request;try{request=lockRequest();}catch(error){$('lock-result').textContent=error.message;return;}
 const generation=lockGeneration,key=selectionKey();setBusy(true);$('lock-preflight').disabled=true;$('lock-result').textContent='正在访问仓库核对锁；尚未修改锁或向仓库发送说明。';
 try{const result=await api('/api/lock-preflight',request);
  if(generation!==lockGeneration||key!==selectionKey()||!sameShelfScope(request,shelfScope())||modalView!=='lock'||!$('modal').open||JSON.stringify(request)!==JSON.stringify(lockRequest()))return;
  validateLockPlan(result,request,state.rows);const plan={request,result,key,expiresAt:Date.now()+result.expiresIn*1000};lockPlan=plan;
  $('lock-files').innerHTML=commitFilesMarkup(result.files)+`<ul>${result.files.map(f=>`<li>${esc(f.path)}：${f.locked?'仓库有锁，本工作副本持有匹配令牌':'仓库无锁，本工作副本无令牌'} · 工作修订 r${esc(f.workingRevision)} / 仓库最后变更 r${esc(f.serverLastChangedRevision)}</li>`).join('')}</ul>`;
  $('lock-result').textContent='仓库预检通过；请核对完整范围和说明后单独确认。计划 120 秒内有效，仅使用一次；此后仓库仍可能发生变化。';$('lock-confirm').disabled=false;
  setTimeout(()=>{if(lockPlan===plan)invalidateLock('预检已过期，请重新预检。');},120000);
 }catch(error){if(generation===lockGeneration&&modalView==='lock'&&$('modal').open)$('lock-result').textContent=`预检失败：${error.message} 未发送锁定或解锁命令。`;}
 finally{setBusy(false);if($('lock-preflight'))$('lock-preflight').disabled=false;}
}
function preserveLockResult(message){$('lock-last-result').textContent=message;$('lock-last-result').classList.remove('hidden');if($('lock-result'))$('lock-result').textContent=message;log(message,'WARN');notify(message);}
async function applyLock(){
 const plan=lockPlan;if(!plan||lockApplying||!locksAvailable()||modalView!=='lock'||!$('modal').open)return;
 try{if(Date.now()>=plan.expiresAt||plan.key!==selectionKey()||JSON.stringify(plan.request)!==JSON.stringify(lockRequest())){invalidateLock();return;}}catch(error){invalidateLock(error.message);return;}
 const generation=lockGeneration;lockPlan=null;lockApplying=true;invalidateReads();setBusy(true);renderPreview();
 for(const id of ['lock-confirm','lock-preflight','lock-comment'])if($(id))$(id).disabled=true;
 $('lock-result').textContent='请求已发送；关闭不等于取消，不要重复操作。';let validatedResult=null;
 try{const payload=await api('/api/lock-apply',{workspaceId:plan.request.workspaceId,selectionVersion:plan.request.selectionVersion,snapshotToken:plan.request.snapshotToken,planToken:plan.result.planToken,confirmed:true});
  const result=validateLockResult(payload,plan.request,plan.result.files),stale=generation!==lockGeneration||modalView!=='lock'||!$('modal').open||!sameShelfScope(plan.request,shelfScope());
  validatedResult=result;const message=lockResultSummary(result,{statusRefreshed:payload.statusRefreshed,stale});
  if(!result.verified)lockInspectionRequired=true;
  preserveLockResult(message);
  try{if(!result.verified||stale||!payload.statusRefreshed)failSnapshot(message);else applySnapshot(payload);}
  finally{if($('lock-result'))$('lock-result').textContent=message;}
 }catch(error){const rejected=Number.isInteger(error.httpStatus)&&error.httpStatus>=400&&error.httpStatus<500;
  const message=validatedResult?lockResultSummary(validatedResult,{statusRefreshed:false,stale:true}):rejected?'请求在执行前被拒绝，未发送锁定或解锁命令。请刷新并重新审查选区后预检。':'锁定操作结果未知：未收到可验证响应。请先用原版可信 SVN 客户端检查仓库锁和本地令牌，再显式刷新；不要直接重试。';
  if(!validatedResult&&!rejected||validatedResult&&!validatedResult.verified)lockInspectionRequired=true;
  preserveLockResult(message);try{failSnapshot(message);}finally{if($('lock-result'))$('lock-result').textContent=message;}

 }finally{lockApplying=false;setBusy(false);render();}
}


// Checkout is session-scoped, never inferred from the current file selection.
let checkoutGeneration=0,checkoutConfig=null,checkoutPlan=null,checkoutApplying=false,checkoutAttempted=false;
function invalidateCheckout(){checkoutGeneration++;checkoutPlan=null;if($('checkout-confirm'))$('checkout-confirm').disabled=true;}
function updateCheckoutButton(){if($('open-checkout'))$('open-checkout').disabled=standalone||state.mode==='demo'||state.busy||checkoutApplying;}
function checkoutActive(generation){return generation===checkoutGeneration&&modalView==='checkout'&&$('modal').open;}
function checkoutIdentity(value,expected){return value&&typeof value.url==='string'&&value.url.length>0&&typeof value.destination==='string'&&value.destination.length>0&&(!expected||value.url===expected.url&&value.destination===expected.destination);}
function checkoutVerified(value,expected){return checkoutIdentity(value,expected)&&value.verified===true&&typeof value.workspaceId==='string'&&value.workspaceId.length>0;}
function preserveCheckoutResult(message){$('checkout-last-result').textContent=message;$('checkout-last-result').classList.remove('hidden');if(modalView==='checkout'&&$('checkout-result'))$('checkout-result').textContent=message;log(message,'WARN');}
function checkoutSuccess(result){return `检出已核实：${result.url} → ${result.destination}。未自动切换工作副本；请打开「工作副本」并选择新副本。`;}
async function openCheckout(){
 if(standalone||state.mode==='demo'||state.busy||checkoutApplying)return;
 modal('检出新的工作副本','<p id="checkout-result" role="status">正在读取启动时授权的检出目标；不访问仓库。</p>');modalView='checkout';checkoutConfig=null;
 const generation=checkoutGeneration;
 try{const config=await api('/api/checkout');if(!checkoutActive(generation))return;
  if(config.enabled!==true){$('modal-content').innerHTML='<p>检出默认关闭。请在启动可信后端时指定唯一仓库 URL 和新目录目标，再重新打开此窗口。网页不接受任意 URL 或路径。</p>';return;}
  if(!checkoutIdentity(config)||!['ready','completed','uncertain'].includes(config.state))throw Error('检出配置无效。');
  checkoutConfig={...config};
  $('modal-content').innerHTML=`<p class="workspace-warning">只检出启动时授权的固定目标。确认执行才会访问仓库并创建本地工作副本；跳过 externals，不保存凭据。关闭窗口不等于取消已发送的请求。</p><div class="review-file"><strong>仓库 URL</strong><span>${esc(config.url)}</span></div><div class="review-file"><strong>新工作副本目录</strong><span>${esc(config.destination)}</span></div><p>预检只核对本地目标，不验证远程仓库或登录。每个会话只允许一次检出尝试；结果未知时不要重试或删除目录，请用可信 SVN 客户端核对。</p><button id="checkout-preflight" class="subtle-button" disabled>预检固定检出目标</button><p id="checkout-result" role="status">尚未执行检出。</p>`;
  if(config.state==='completed'){
   checkoutAttempted=true;if(!checkoutVerified(config.result,config))throw Error('已执行检出，但返回的核实结果无效。');preserveCheckoutResult(checkoutSuccess(config.result));
  }else if(config.state==='uncertain'||checkoutAttempted){checkoutAttempted=true;preserveCheckoutResult('检出结果未知，目标可能已经写入。本会话禁止再次检出；请用可信 SVN 客户端检查目标目录。');}
  else{$('checkout-preflight').disabled=false;$('checkout-preflight').onclick=preflightCheckout;$('checkout-confirm').classList.remove('hidden');}
 }catch(error){if(checkoutActive(generation))$('checkout-result').textContent=`无法准备检出：${error.message}`;}
}
async function preflightCheckout(){
 if(state.busy||checkoutApplying||checkoutAttempted||!checkoutConfig||checkoutConfig.state!=='ready'||modalView!=='checkout'||!$('modal').open)return;
 invalidateCheckout();const generation=checkoutGeneration,config={...checkoutConfig};setBusy(true);$('checkout-preflight').disabled=true;
 $('checkout-result').textContent='正在预检本地目标；尚未访问仓库或执行检出。';
 try{const plan=await api('/api/checkout-preflight',{});if(!checkoutActive(generation))return;
  if(!checkoutIdentity(plan,config)||typeof plan.planToken!=='string'||!plan.planToken||plan.expiresIn!==120||plan.ignoreExternals!==true||plan.remoteAccess!==false)throw Error('预检计划与固定目标不一致。');
  checkoutPlan={...plan,expiresAt:Date.now()+plan.expiresIn*1000};const saved=checkoutPlan;
  $('checkout-result').textContent='本地预检通过，计划 120 秒一次有效。请核对上方仓库 URL 和新目录；点击「确认访问仓库并检出」将访问该仓库并写入该目录。';$('checkout-confirm').disabled=false;
  setTimeout(()=>{if(checkoutPlan===saved){invalidateCheckout();if(checkoutActive(checkoutGeneration))$('checkout-result').textContent='预检已过期，请重新预检。';}},plan.expiresIn*1000);
 }catch(error){if(checkoutActive(generation))$('checkout-result').textContent=`预检失败：${error.message} 未执行检出。`;}
 finally{setBusy(false);if(checkoutActive(generation)&&$('checkout-preflight'))$('checkout-preflight').disabled=false;}
}
async function applyCheckout(){
 const plan=checkoutPlan;if(!plan||state.busy||checkoutApplying||checkoutAttempted||modalView!=='checkout'||!$('modal').open)return;
 if(Date.now()>=plan.expiresAt||!checkoutIdentity(plan,checkoutConfig)){invalidateCheckout();$('checkout-result').textContent='计划已失效，请重新预检。';return;}
 checkoutPlan=null;checkoutApplying=true;checkoutAttempted=true;setBusy(true);$('checkout-confirm').disabled=true;$('checkout-preflight').disabled=true;
 preserveCheckoutResult('检出请求已发送，正在等待核实。关闭不等于取消；请勿重复检出或修改目标目录。');
 try{const result=await api('/api/checkout-apply',{planToken:plan.planToken,confirmed:true});
  if(!checkoutVerified(result,plan)||result.ignoreExternals!==true)throw Error('检出核实结果无效。');
  preserveCheckoutResult(checkoutSuccess(result));
 }catch(error){const rejected=error.httpStatus>=400&&error.httpStatus<500;
  preserveCheckoutResult(rejected?'本次检出请求被拒绝，但本会话可能已有检出；请关闭并重新打开窗口核对保留结果，勿重复执行。':'检出结果未知，目标可能已部分或全部写入。本会话禁止再次检出，没有自动重试或回滚；请用可信 SVN 客户端核对目标目录。');
 }finally{checkoutApplying=false;setBusy(false);}
}

let fileOpGeneration=0,fileOpPlan=null,fileOpApplying=false,fileOpAction='move';
function invalidateFileOp(message='源文件、目标或状态已改变，请重新预检。'){fileOpGeneration++;fileOpPlan=null;if($('file-op-confirm'))$('file-op-confirm').disabled=true;if($('file-op-result'))$('file-op-result').textContent=message;}
function fileOpsAvailable(){return state.mode==='live'&&state.fileOpsEnabled===true&&!state.busy&&!state.failed&&typeof state.snapshotToken==='string'&&!!state.snapshotToken;}
function updateFileOpButtons(){let valid=false;try{fileOpSource(state.rows,state.selected);valid=true;}catch{}for(const id of ['move-file','copy-file'])if($(id))$(id).disabled=!fileOpsAvailable()||!valid;}
function openFileOp(action){
 if(!fileOpsAvailable()||!['move','copy'].includes(action))return;let row;try{row=fileOpSource(state.rows,state.selected);}catch(error){notify(error.message);return;}
 modal(action==='move'?'移动／重命名一个明确文件':'带历史复制一个明确文件',`<p class="workspace-warning">${action==='move'?'源文件会从原路径移走并登记待删除，目标带历史待新增。':'源文件保持原状，目标使用当前工作内容和属性带历史待新增。'} 仅本地操作，不上传、不提交、不覆盖、不创建父目录。移动、复制后的结构变更暂不支持本原型提交入口，请用原版可信 SVN 客户端审查${action==='move'?'移动两端':'复制目标'}后提交；标签不保证转移。失败可能已部分完成，不会自动重试或回滚。</p><div class="review-file"><strong>源文件</strong><span>${esc(row.path)}</span></div><label class="file-op-destination">精确目标文件路径（相对于工作副本根；目录分隔使用 /）<input id="file-op-destination" autocomplete="off" spellcheck="false" placeholder="existing-folder/new-name.txt" maxlength="2048"></label><p>只处理完整选区中的这一个文件，不使用多选中的焦点代替完整选区。目标父目录必须已存在且版本化；不能只改大小写。预检会读取本地身份、内容摘要和属性，不访问仓库。</p><button id="file-op-preflight" class="subtle-button">预检源文件与精确目标</button><div id="file-op-paths"></div><p id="file-op-result" role="status">尚未执行；请勿同时编辑文件或运行其他 SVN 操作。</p>`);
 fileOpAction=action;modalView='file-op';$('file-op-confirm').classList.remove('hidden');$('file-op-confirm').textContent=action==='move'?'确认移动／重命名此文件':'确认带历史复制此文件';$('file-op-preflight').onclick=preflightFileOp;$('file-op-destination').oninput=()=>invalidateFileOp('目标已修改，请重新预检。');
}
function fileOpRequest(){const row=fileOpSource(state.rows,state.selected);return {...shelfScope(),action:fileOpAction,id:row.id,destination:parseFileDestination($('file-op-destination').value,row.path)};}
async function preflightFileOp(){
 if(!fileOpsAvailable()||modalView!=='file-op'||!$('modal').open)return;invalidateFileOp();let request;try{request=fileOpRequest();}catch(error){$('file-op-result').textContent=error.message;return;}
 const generation=fileOpGeneration,key=selectionKey();setBusy(true);$('file-op-preflight').disabled=true;$('file-op-result').textContent='正在核对源文件、目标命名空间和内容／属性；尚未修改文件。';
 try{const result=await api('/api/file-op-preflight',request);
  if(generation!==fileOpGeneration||key!==selectionKey()||!sameShelfScope(request,shelfScope())||modalView!=='file-op'||!$('modal').open||JSON.stringify(request)!==JSON.stringify(fileOpRequest()))return;
  validateFileOpPlan(result,request,state.rows);const plan={request,result,key,expiresAt:Date.now()+result.expiresIn*1000};fileOpPlan=plan;
  $('file-op-paths').innerHTML=`<div class="review-file"><strong>源</strong><span>${esc(result.source.path)}</span></div><div class="review-file"><strong>目标</strong><span>${esc(result.destination)}</span></div>`;
  $('file-op-result').textContent='预检通过。请核对源与目标，再单独确认本地操作。计划 120 秒一次有效；操作后仍需用原版可信客户端审查并提交。';$('file-op-confirm').disabled=false;
  setTimeout(()=>{if(fileOpPlan===plan)invalidateFileOp('预检已过期，请重新预检。');},120000);
 }catch(error){if(generation===fileOpGeneration&&modalView==='file-op'&&$('modal').open)$('file-op-result').textContent=`预检失败：${error.message} 未执行文件操作。`;}
 finally{setBusy(false);if($('file-op-preflight'))$('file-op-preflight').disabled=false;}
}
function preserveFileOpResult(message){$('file-op-last-result').textContent=message;$('file-op-last-result').classList.remove('hidden');if($('file-op-result'))$('file-op-result').textContent=message;log(message,'WARN');notify(message);}
async function applyFileOp(){
 const plan=fileOpPlan;if(!plan||fileOpApplying||!fileOpsAvailable()||modalView!=='file-op'||!$('modal').open)return;
 try{if(Date.now()>=plan.expiresAt||plan.key!==selectionKey()||JSON.stringify(plan.request)!==JSON.stringify(fileOpRequest())){invalidateFileOp();return;}}catch(error){invalidateFileOp(error.message);return;}
 const generation=fileOpGeneration;fileOpPlan=null;fileOpApplying=true;invalidateReads();setBusy(true);renderPreview();
 for(const id of ['file-op-confirm','file-op-preflight','file-op-destination'])if($(id))$(id).disabled=true;
 $('file-op-result').textContent='文件操作请求已发送；关闭不等于取消，不要重复操作。';let verifiedResult=null;
 try{const payload=await api('/api/file-op-apply',{workspaceId:plan.request.workspaceId,selectionVersion:plan.request.selectionVersion,snapshotToken:plan.request.snapshotToken,planToken:plan.result.planToken,confirmed:true});
  const result=validateFileOpResult(payload,plan.request,plan.result);verifiedResult=result;const stale=generation!==fileOpGeneration||!sameShelfScope(plan.request,shelfScope())||modalView!=='file-op'||!$('modal').open;
  const message=fileOpResultSummary(result,{statusRefreshed:payload.statusRefreshed,stale});
  preserveFileOpResult(message);
  try{if(stale||!payload.statusRefreshed)failSnapshot(message);else applySnapshot(payload);}
  finally{if($('file-op-result'))$('file-op-result').textContent=message;}
 }catch(error){const rejected=Number.isInteger(error.httpStatus)&&error.httpStatus>=400&&error.httpStatus<500;
  const message=verifiedResult?fileOpResultSummary(verifiedResult,{statusRefreshed:false,stale:true}):rejected?'文件操作在执行前被拒绝，请刷新并重新审查源与目标。':'文件操作结果未知，源或目标可能已改变。没有自动重试或回滚；请用原版可信 SVN 客户端核对两端，再显式刷新。';
  preserveFileOpResult(message);try{failSnapshot(message);}finally{if($('file-op-result'))$('file-op-result').textContent=message;}
 }finally{fileOpApplying=false;setBusy(false);render();}
}

let addGeneration=0,addPlan=null,addApplying=false;
function invalidateAdd(message='选区或状态已改变，请重新预检。'){addGeneration++;addPlan=null;if($('add-confirm'))$('add-confirm').disabled=true;if($('add-result'))$('add-result').textContent=message;}
function addsAvailable(){return state.mode==='live'&&state.addsEnabled===true&&!state.busy&&!state.failed&&typeof state.snapshotToken==='string'&&!!state.snapshotToken;}
function updateAddButton(){let valid=false;try{addScopeRows(state.rows,state.selected);valid=true;}catch{}if($('add-selected'))$('add-selected').disabled=!addsAvailable()||!valid;}
function openAdd(){
 if(!addsAvailable())return;let rows;try{rows=addScopeRows(state.rows,state.selected);}catch(error){notify(error.message);return;}
 modal('添加明确选区到版本控制',`<p class="workspace-warning">仅将普通新文件登记为本地待添加状态，不提交、不上传、不改变文件内容。父目录必须已经版本化；不添加目录、不递归、不强制忽略项。本次明确禁用 auto-props，SVN 仍可能按文件类型设置默认属性，提交前请核对属性和差异。失败可能已登记部分文件，不会自动重试或回滚。</p><p>完整明确选区 ${rows.length} 项，包含被筛选或折叠隐藏的成员。预检只读取本地状态、身份和有界内容摘要。</p><div id="add-files">${rows.map(row=>`<div class="review-file"><span>${esc(row.path)}</span></div>`).join('')}</div><button id="add-preflight" class="subtle-button">预检明确选区</button><p id="add-result" role="status">尚未执行添加。请勿同时编辑文件或运行其他 SVN 操作。</p>`);
 modalView='add';$('add-confirm').classList.remove('hidden');$('add-preflight').onclick=preflightAdd;
}
function addRequest(){return {...shelfScope(),ids:addScopeRows(state.rows,state.selected).map(r=>r.id)};}
async function preflightAdd(){
 if(!addsAvailable()||modalView!=='add'||!$('modal').open)return;invalidateAdd();let request;try{request=addRequest();}catch(error){$('add-result').textContent=error.message;return;}
 const generation=addGeneration,key=selectionKey();setBusy(true);$('add-preflight').disabled=true;$('add-result').textContent='正在核对完整选区、版本化父目录与文件摘要；尚未添加。';
 try{const result=await api('/api/add-preflight',request);
  if(generation!==addGeneration||key!==selectionKey()||!sameShelfScope(request,shelfScope())||modalView!=='add'||!$('modal').open||JSON.stringify(request)!==JSON.stringify(addRequest()))return;
  validateAddPlan(result,request,state.rows);const plan={request,result,key,expiresAt:Date.now()+result.expiresIn*1000};addPlan=plan;
  $('add-result').textContent='本地预检通过，请核对完整范围后单独确认添加。计划 120 秒内有效，仅使用一次；不提交到仓库。';$('add-confirm').disabled=false;
  setTimeout(()=>{if(addPlan===plan)invalidateAdd('预检已过期，请重新预检。');},120000);
 }catch(error){if(generation===addGeneration&&modalView==='add'&&$('modal').open)$('add-result').textContent=`预检失败：${error.message} 未执行添加。`;}
 finally{setBusy(false);if($('add-preflight'))$('add-preflight').disabled=false;}
}
function preserveAddResult(message){$('add-last-result').textContent=message;$('add-last-result').classList.remove('hidden');if($('add-result'))$('add-result').textContent=message;log(message,'WARN');notify(message);}
async function applyAdd(){
 const plan=addPlan;if(!plan||addApplying||!addsAvailable()||modalView!=='add'||!$('modal').open)return;
 try{if(Date.now()>=plan.expiresAt||plan.key!==selectionKey()||JSON.stringify(plan.request)!==JSON.stringify(addRequest())){invalidateAdd();return;}}catch(error){invalidateAdd(error.message);return;}
 const generation=addGeneration;addPlan=null;addApplying=true;invalidateReads();setBusy(true);renderPreview();
 for(const id of ['add-confirm','add-preflight'])if($(id))$(id).disabled=true;
 $('add-result').textContent='添加请求已发送；关闭不等于取消，不要重复操作。';
 let verifiedResult=null;
 try{const payload=await api('/api/add-apply',{workspaceId:plan.request.workspaceId,selectionVersion:plan.request.selectionVersion,snapshotToken:plan.request.snapshotToken,planToken:plan.result.planToken,confirmed:true});
  const result=validateAddResult(payload,plan.request,plan.result.files);verifiedResult=result;const stale=generation!==addGeneration||modalView!=='add'||!$('modal').open||!sameShelfScope(plan.request,shelfScope());
  const message=addResultSummary(result,{statusRefreshed:payload.statusRefreshed,stale});
  preserveAddResult(message);
  try{if(stale||!payload.statusRefreshed)failSnapshot(message);else applySnapshot(payload);}
  finally{if($('add-result'))$('add-result').textContent=message;}
 }catch(error){const rejected=Number.isInteger(error.httpStatus)&&error.httpStatus>=400&&error.httpStatus<500;
  const message=verifiedResult?addResultSummary(verifiedResult,{statusRefreshed:false,stale:true}):rejected?'添加请求在执行前被拒绝。请刷新并重新审查选区后预检。':'添加结果未知，部分文件可能已经登记。没有自动重试或回滚；请显式刷新并核对待添加文件，再决定下一步。';
  preserveAddResult(message);try{failSnapshot(message);}finally{if($('add-result'))$('add-result').textContent=message;}
 }finally{addApplying=false;setBusy(false);render();}
}

async function api(path,body){
 const options={headers:{'X-SVN-Desk-Token':token},cache:'no-store'};
 if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
 const r=await fetch(path,options);let data;try{data=await r.json();}catch{throw Error('本地服务返回了无效响应。');}
 if(!r.ok){const error=Error(data.error||'本地请求失败。');error.httpStatus=r.status;throw error;}return data;
}
function updateMode(){
 updateCheckoutButton();
 const demo=state.mode==='demo',choose=state.mode==='select';
 $('mode-pill').innerHTML=`<span class="dot"></span>${state.failed?'读取失败 · 状态未知':demo?'演示数据 · 本地模拟':choose?'真实模式 · 待选工作区':state.mode==='live'?(state.locksEnabled?'真实状态 · 仓库锁已启用':state.commitsEnabled?'真实状态 · 仓库提交已启用':state.shelfWritesEnabled?'真实本地状态 · Shelf 可用':state.changelistWritesEnabled?'真实本地状态 · 本地归组可用':state.addsEnabled?'真实本地状态 · 添加文件已启用':state.fileOpsEnabled?'真实本地状态 · 文件操作已启用':'真实本地状态 · 只读'):'正在读取状态'}`;
 $('status-text').textContent=state.failed?'状态不可用，不代表工作副本干净':demo?'演示数据，未连接仓库':choose?'尚未选择工作副本，未运行 SVN':state.commitsEnabled?'本地状态 · 仓库提交须预检并确认':'状态仅来自本地 · 仓库按需读取';
 $('workspace-address').textContent=state.root;$('workspace-name').textContent=state.workspace;$('context-workspace').textContent=state.workspace;
 $('workspace-description').textContent=demo?'本地工作副本 · 示例':choose?'仅可选择授权候选':state.locksEnabled?'当前工作副本 · 仓库锁已启用':state.commitsEnabled?'当前工作副本 · 仓库提交已启用':state.shelfWritesEnabled?'当前工作副本 · Shelf 已启用':state.changelistWritesEnabled?'当前工作副本 · 本地标签可写':state.addsEnabled?'当前工作副本 · 添加文件已启用':state.fileOpsEnabled?'当前工作副本 · 文件操作已启用':'当前工作副本 · 只读';
 $('branch-label').textContent=demo?'trunk（示例）':'未查询分支';$('revision-label').textContent=demo?'BASE r2841（示例）':'修订见文件行';$('history-indicator').textContent=demo?'示例':'按需读取';
}
function resetWorkspaceScope(){invalidateLock();invalidateAdd();invalidateFileOp();invalidateCommit();invalidateShelf();invalidateChangelist();invalidateReads();state.selected.clear();state.focused=null;state.folder='';state.query='';state.filter='all';state.collapsed.clear();$('search').value='';}
function applySnapshot(payload,initial=false){
 invalidateLock();invalidateAdd();invalidateFileOp();state.addsEnabled=payload.addsEnabled===true;state.fileOpsEnabled=payload.fileOpsEnabled===true;invalidateCommit();state.locksEnabled=payload.locksEnabled===true;state.commitsEnabled=payload.commitsEnabled===true;
 invalidateShelf();invalidateChangelist();invalidateReads();state.shelfWritesEnabled=payload.shelfWritesEnabled===true;state.changelistWritesEnabled=payload.changelistWritesEnabled===true;state.snapshotToken=payload.snapshotToken;state.fileReadsEnabled=payload.fileReadsEnabled===true;state.propertyReadsEnabled=payload.propertyReadsEnabled===true;
 if(!['demo','live','select'].includes(payload.mode))throw Error('状态模式无效。');
 const rows=payload.mode==='demo'?structuredClone(demoRows):payload.rows;
 if(!Array.isArray(rows)||rows.some(r=>!r||typeof r.id!=='string'||typeof r.path!=='string'))throw Error('状态行无效。');
 const changed=state.workspaceId!==(payload.workspaceId||null)||state.mode!==payload.mode||state.selectionVersion!==(payload.selectionVersion||0);
 if(changed)resetWorkspaceScope();
 state.workspaceId=payload.workspaceId||null;state.selectionVersion=payload.selectionVersion||0;
 state.workspace=payload.workspace||state.workspace;state.root=payload.root||state.root;
 state.rows=rows;state.mode=payload.mode;state.failed=false;$('live-error').classList.add('hidden');
 const ids=new Set(rows.map(r=>r.id));state.selected=new Set([...state.selected].filter(id=>ids.has(id)));
 if(!ids.has(state.focused)){state.focused=state.mode==='demo'?demoRows[0].id:rows[0]?.id||null;if(initial&&state.mode==='demo'&&state.focused)state.selected.add(state.focused);}
 // Retain the original demo's initial folded default group.
 if(initial&&state.mode==='demo')state.collapsed.add('');
 updateMode();render();$('read-time').textContent=state.mode==='select'?'等候选择':`已读取 ${new Date().toLocaleTimeString('en-GB')}`;
}
function failSnapshot(message){invalidateLock();invalidateAdd();invalidateFileOp();state.addsEnabled=false;state.fileOpsEnabled=false;invalidateCommit();state.locksEnabled=false;state.commitsEnabled=false;invalidateShelf();state.shelfWritesEnabled=false;invalidateChangelist();invalidateReads();state.changelistWritesEnabled=false;state.snapshotToken=null;state.fileReadsEnabled=false;state.propertyReadsEnabled=false;state.failed=true;state.rows=[];state.selected.clear();state.focused=null;if(state.mode==='loading')state.mode='error';$('live-error').textContent=message;$('live-error').classList.remove('hidden');updateMode();render();$('read-time').textContent='状态未知';}
function setBusy(busy){if(busy)closeFileMenu();state.busy=busy;updateCheckoutButton();updateShelfButtons();updateCommitButtons();updateLockButtons();updateAddButton();updateFileOpButtons();$('move-changelist').disabled=!changelistsAvailable();$('refresh').disabled=busy;$('open-workspace').disabled=busy;}
async function refresh(initial=false){
 if(state.busy)return;invalidateLock();invalidateAdd();invalidateFileOp();invalidateCommit();invalidateShelf();invalidateChangelist();invalidateReads();setBusy(true);renderPreview();$('read-time').textContent='正在读取…';
 try{
  const config=standalone?{mode:'demo',workspace:'Project Atlas',root:'D:\\Workspace\\ProjectAtlas'}:await api('/api/config');
  // Identity is known even if the subsequent status read fails.
  state.workspace=config.workspace;state.root=config.root;
  if(config.mode==='live'||config.mode==='select')state.mode=config.mode;
  const payload=standalone?{mode:'demo'}:await api('/api/status');
  if(config.mode!==payload.mode||config.workspaceId!==payload.workspaceId||config.selectionVersion!==payload.selectionVersion)throw Error('工作区在读取期间发生变化，请重新刷新。');
  applySnapshot({...config,...payload,propertyReadsEnabled:payload.propertyReadsEnabled===true,fileOpsEnabled:payload.fileOpsEnabled===true,addsEnabled:payload.addsEnabled===true,locksEnabled:payload.locksEnabled===true,commitsEnabled:payload.commitsEnabled===true,shelfWritesEnabled:payload.shelfWritesEnabled===true,changelistWritesEnabled:payload.changelistWritesEnabled===true},initial);
  log(state.mode==='demo'?`加载 ${state.rows.length} 项内置演示状态；未运行 SVN。`:state.mode==='select'?'尚未选择工作副本；请打开工作区选择器。':`完成本地 svn status：${state.rows.length} 项状态；未查询远程。`);
 }catch(error){failSnapshot('状态读取失败，未显示任何部分或缓存结果。请检查服务、工作副本和 SVN 安装后重试。');log('状态读取失败；没有返回部分状态，没有切换到演示数据。','WARN');}
 finally{setBusy(false);renderPreview();renderHistory();}
}
const workspaceInstructions=()=>`<p>当前为演示模式，没有扫描本机目录。若要启用真实只读工作区，请使用可信后端和已安装的 SVN 启动：</p><code>python server.py --scan-parent "C:\\Work" --backend-source "C:\\Tools\\svn-desk"</code><p>可重复 --workspace 或 --scan-parent。可选 --settings-file 指定扫描范围外的专用偏好文件，父目录需已存在。网页不能输入任意路径，检出需启动时授权固定仓库 URL 与目标；不能普通还原；仓库提交需另行显式启用。</p>`;
function renderWorkspaceCatalog(data){
 workspaceCatalog=data;
 if(!data.enabled){$('modal-content').innerHTML=workspaceInstructions();return;}
 const candidates=data.candidates||[];
 $('modal-content').innerHTML=`<p>仅扫描启动时授权的目录，寻找已有 .svn 标记。候选不代表仓库身份验证成功；选择后仅运行本地 status，不访问仓库。</p><div class="workspace-tools"><button class="subtle-button" id="rescan-workspaces">重新扫描授权目录</button><span>${data.persistenceEnabled?'成功选择后保存本地偏好':'本次会话选择，不保存偏好'}</span></div>${data.warning?`<p class="workspace-warning" role="alert">${esc(data.warning)}</p>`:''}<div class="workspace-scan-notes">${(data.scans||[]).map(scan=>`<p>${esc(scan.name)}：${scan.complete?'扫描范围内完成':'扫描不完整'} · ${scan.count} 个候选${scan.reasons?.length?` · ${esc(scan.reasons.join(', '))}`:''}${scan.skippedLinks?` · 跳过 ${scan.skippedLinks} 个链接`:''}</p>`).join('')}</div><div class="workspace-candidates">${candidates.length?candidates.map((c,i)=>`<div class="workspace-candidate"><div><strong>${esc(c.name)}</strong><small>${esc(c.path)}</small><span>${c.relation==='nested'?'嵌套候选，不等于 external':'本地候选'} · 仓库身份未验证${c.id===data.selectedId?' · 当前':''}</span></div><button class="primary-button" data-workspace-index="${i}">选择并读取状态</button></div>`).join(''):'<p class="workspace-warning">没有找到可选工作区。扫描不完整时不代表目录中没有其他副本。</p>'}</div><p id="workspace-result" role="status"></p><p class="history-disclaimer">切换成功会清空文件选区、目录和搜索范围。差异与历史需显式读取；普通还原未接入；仓库提交与 Shelf 需单独显式启用与确认。</p>`;
 $('rescan-workspaces').onclick=loadWorkspaceCatalog;
 document.querySelectorAll('[data-workspace-index]').forEach(button=>button.onclick=()=>selectWorkspace(candidates[Number(button.dataset.workspaceIndex)].id));
}
async function loadWorkspaceCatalog(){
 const request=++workspaceRequest;
 $('modal-content').innerHTML='<p role="status">正在扫描已授权目录… 不读取文件内容，不运行 SVN。</p>';
 try{const data=await api('/api/workspaces');if(request===workspaceRequest&&$('modal').open&&modalView==='workspaces'){
  if(isStaleWorkspaceCatalog(data,state.selectionVersion))throw Error('工作区状态已更新，请重新扫描候选。');
  if(data.current&&data.current.selectionVersion>state.selectionVersion){
   if(data.current.mode==='select')applySnapshot({...data.current,rows:[]});
   else{state.workspace=data.current.workspace;state.root=data.current.root;resetWorkspaceScope();failSnapshot('当前工作区已改变，请刷新状态后继续。');}
  }
  renderWorkspaceCatalog(data);
 }}
 catch(error){if(request===workspaceRequest&&$('modal').open&&modalView==='workspaces'){$('modal-content').innerHTML=`<p class="workspace-warning" role="alert">${esc(error.message)}</p><button class="subtle-button" id="retry-workspaces">重试扫描</button>`;$('retry-workspaces').onclick=loadWorkspaceCatalog;}}
}
async function openWorkspaces(){
 if(state.busy)return;
 modal('选择工作区',standalone?workspaceInstructions():'');modalView='workspaces';
 if(!standalone)await loadWorkspaceCatalog();
}
async function selectWorkspace(id){
 if(state.busy)return;invalidateLock();invalidateAdd();invalidateFileOp();invalidateCommit();invalidateShelf();invalidateChangelist();invalidateReads();setBusy(true);
 document.querySelectorAll('[data-workspace-index],#rescan-workspaces').forEach(b=>b.disabled=true);
 $('workspace-result').textContent='正在读取此候选的本地状态…';
 state.rows=[];state.selected.clear();state.focused=null;render();$('read-time').textContent='正在切换…';
 try{
  const payload=await api('/api/workspace-selection',{id});
  applySnapshot(payload);log('已切换工作区；文件选区与筛选范围已清空。');
  const result=payload.warning|| (payload.selectionSaved?'工作区已选择，本地偏好已保存。':'工作区已选择，仅用于本次会话。');
  if($('modal').open&&modalView==='workspaces'){if(workspaceCatalog)renderWorkspaceCatalog({...workspaceCatalog,selectedId:payload.workspaceId,selectionVersion:payload.selectionVersion});$('workspace-result').textContent=result;}
  notify(result);
 }catch(error){failSnapshot('工作区选择未能确认。请刷新状态以确认当前工作区；没有显示缓存或演示结果。');if($('modal').open&&modalView==='workspaces')$('workspace-result').textContent=error.message;log('工作区选择未能确认，请刷新当前状态。','WARN');}
 finally{setBusy(false);renderPreview();renderHistory();document.querySelectorAll('[data-workspace-index],#rescan-workspaces').forEach(b=>b.disabled=false);}
}
function clearFilters(){state.query='';state.filter='all';state.folder='';$('search').value='';render();}
paintIcons();
$('file-context-menu').addEventListener('click',event=>{const button=event.target.closest('[data-file-action]');if(button&&!button.disabled)runFileMenuAction(button.dataset.fileAction);});
$('file-context-menu').addEventListener('keydown',event=>{
 const buttons=[...$('file-context-menu').querySelectorAll('button:not(:disabled)')];
 if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){
  event.preventDefault();event.stopPropagation();
  const index=buttons.indexOf(document.activeElement),next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length;
  buttons[next]?.focus();
 }else if(event.key==='Escape'){event.preventDefault();event.stopPropagation();closeFileMenu(true);}
 else if(event.key==='Tab'){closeFileMenu(true);}
});
document.addEventListener('pointerdown',event=>{if(!$('file-context-menu').contains(event.target))closeFileMenu();},true);
document.addEventListener('focusin',event=>{if(fileMenuScope&&!$('file-context-menu').contains(event.target))closeFileMenu();});
document.addEventListener('scroll',event=>{if(! $('file-context-menu').contains(event.target))closeFileMenu();},true);
window.addEventListener('resize',()=>closeFileMenu());window.addEventListener('blur',()=>closeFileMenu());
$('refresh').onclick=()=>refresh();$('search').addEventListener('input',e=>{state.query=e.target.value;state.collapsed.clear();render();});
document.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{state.filter=b.dataset.filter;state.collapsed.clear();render();});
$('pending-nav').onclick=clearFilters;$('pending-tab').onclick=clearFilters;$('conflict-nav').onclick=()=>{state.filter='conflicted';state.collapsed.clear();state.folder='';state.query='';$('search').value='';render();};
$('workspace-home').onclick=clearFilters;$('clear-filters').onclick=clearFilters;$('empty-reset').onclick=clearFilters;
$('select-visible').onchange=e=>{for(const row of displayed()){e.target.checked?state.selected.add(row.id):state.selected.delete(row.id);}render();};
$('collapse-all').onclick=()=>{const groups=groupsFor(matches()),all=groups.every(([name])=>state.collapsed.has(name));for(const [name]of groups){all?state.collapsed.delete(name):state.collapsed.add(name);}$('collapse-all').innerHTML=`${icon('collapse')}${all?'折叠分组':'展开分组'}`;render();};
$('move-file').onclick=()=>openFileOp('move');$('copy-file').onclick=()=>openFileOp('copy');$('file-op-confirm').onclick=applyFileOp;
$('add-selected').onclick=openAdd;$('add-confirm').onclick=applyAdd;
$('lock-inspected').onclick=()=>{if(state.busy||state.failed||!state.snapshotToken)return;lockInspectionRequired=false;updateLockButtons();};
$('lock-selected').onclick=()=>openLock('lock');$('unlock-selected').onclick=()=>openLock('unlock');$('lock-confirm').onclick=applyLock;
$('commit-selected').onclick=()=>openCommit();$('commit-confirm').onclick=applyCommit;
$('create-shelf').onclick=openCreateShelf;$('browse-shelves').onclick=openShelfBrowser;$('shelf-confirm').onclick=applyShelf;
$('view-diff').onclick=()=>{showDiff();};$('review').onclick=review;$('move-changelist').onclick=openChangelists;
document.querySelectorAll('[data-preview]').forEach(b=>b.onclick=()=>{invalidateReads();state.preview=b.dataset.preview;renderPreview();});
$('toggle-preview').onclick=()=>{invalidateReads();renderPreview();$('preview-panel').classList.toggle('collapsed');$('toggle-preview').setAttribute('aria-label',$('preview-panel').classList.contains('collapsed')?'展开差异面板':'折叠差异面板');};
$('toggle-log').onclick=()=>document.querySelector('.activity-panel').classList.toggle('collapsed');
$('close-detail').onclick=()=>{$('details-panel').classList.add('hidden');$('details-panel').classList.remove('force-visible');};
$('toggle-detail').onclick=()=>{if(innerWidth<=1050){$('details-panel').classList.remove('hidden');$('details-panel').classList.toggle('force-visible');}else $('details-panel').classList.toggle('hidden');};
$('nav-toggle').onclick=()=>$('sidebar').classList.toggle('force-visible');
$('workspace-tab').onclick=()=>{$('workspace-tab').classList.add('active');$('repository-tab').classList.remove('active');$('repository-notice').classList.add('hidden');$('folder-tree').classList.remove('hidden');};
$('repository-tab').onclick=()=>{$('repository-tab').classList.add('active');$('workspace-tab').classList.remove('active');$('repository-notice').classList.remove('hidden');$('folder-tree').classList.add('hidden');};
$('history-tab').onclick=showHistory;
$('open-workspace').onclick=openWorkspaces;
$('open-checkout').onclick=openCheckout;$('checkout-confirm').onclick=applyCheckout;
$('help').onclick=()=>modal('SVN Desk · Web 原型',`<p>现代 Web 视觉，保留工作区树、原生 changelist 分组、选中项审查、详情与独立日志的桌面工作流。</p><ul><li>真实可用：筛选、选中、折叠分组、目录范围、只读审查与布局切换。</li><li>演示：选定示例文件的差异、历史与工作副本身份。</li><li>可选桥接：真实本地 status、显式只读 diff 与仓库历史。</li><li>本地归组：需显式启用，预检后单独确认；演示模式仅模拟。Shelf：显式启用后支持真实本地备份与完整恢复；无演示模拟。仓库提交：需显式启用，完整范围预检后另行确认。文件移动／重命名与带历史复制：单文件、默认关闭、预检后独立确认；需用原版可信客户端提交结构变更。检出：启动时固定仓库 URL 与新目录，本地预检后独立确认，跳过 externals；完成后自行选择工作副本。未实现：普通还原、远程仓库浏览。</li></ul><p>快捷键：Ctrl/Cmd K 搜索 · F5 刷新 · ↑ ↓ 浏览 · 空格选择 · Shift+F10 / 菜单键打开文件菜单 · Esc 关闭菜单或弹窗</p><p>运行方式：浏览器原型。Tauri 壳仅提供源码，尚未编译或完成 Windows 验收。</p>`);
$('modal').addEventListener('cancel',()=>{invalidateLock();invalidateCheckout();invalidateAdd();invalidateFileOp();invalidateCommit();invalidateShelf();invalidateChangelist();invalidateReads(false);$('modal-content').innerHTML='';renderPreview();});
$('modal').addEventListener('close',()=>{invalidateCheckout();$('checkout-confirm').classList.add('hidden');$('file-op-confirm').classList.add('hidden');if(fileOpApplying)failSnapshot('文件操作请求仍在执行；关闭不等于取消。请等待结果，勿重复操作。');$('add-confirm').classList.add('hidden');if(addApplying)failSnapshot('添加请求仍在执行；关闭不等于取消。请等待结果，勿重复操作。');invalidateAdd();invalidateFileOp();$('lock-confirm').classList.add('hidden');if(lockApplying)failSnapshot('锁定请求仍在执行；关闭不等于取消。请等待结果，勿重复操作。');invalidateLock();invalidateCommit();$('commit-confirm').classList.add('hidden');if(commitApplying)failSnapshot('提交请求已发送；关闭不等于取消，仓库可能已写入。请等待请求结束后显式刷新并检查仓库历史，切勿直接重试。');invalidateShelf();$('shelf-confirm').classList.add('hidden');if(shelfApplying)failSnapshot('Shelf 请求已发送；关闭不等于取消，可能已部分或全部生效。请等待请求结束后显式刷新并重新审查。');$('changelist-confirm').classList.add('hidden');invalidateChangelist();if(changelistApplying)failSnapshot('归组请求仍在执行，关闭窗口不等于取消。结果返回前状态未知；请查看操作日志。');workspaceRequest++;modalView='';invalidateReads(false);$('modal-content').innerHTML='';renderPreview();});
$('modal-close').onclick=$('modal-done').onclick=()=>$('modal').close();$('modal').addEventListener('click',e=>{if(e.target===$('modal')){const r=$('modal').getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)$('modal').close();}});
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('search').focus();}if(e.key==='F5'){e.preventDefault();refresh();}});
log('启动 Web 原型；本地归组、Shelf 与仓库提交仅在分别显式启用、预检及独立确认后执行。','WARN');await refresh(true);
