import {STATES,stateFor,filteredRows,groupsFor,summary,reviewSelection,safeText as esc,fileName,parentPath,isStaleWorkspaceCatalog,parseRevision,parseComparisonPair,canReadFile,sameReadScope,validateFileResponse,parseChangelistTarget,validateChangelistPlan,canRelabelFile,canShelfFile,parseShelfName,sameShelfScope,validateShelfFiles,validateShelfPlan,commitScopeRows,parseCommitMessage,validateCommitPlan,validateCommitResult,commitResultSummary} from './model.js';
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
let diffFrom='BASE',diffTo='WORKING';
let readGeneration=0,readPending=false,diffResult=null,diffError='',historyResult=null,historyStarts=[],historyStart='HEAD',historySelected=null,historyError='',lastFocus=null;
function invalidateReads(closeHistory=true){diffFrom='BASE';diffTo='WORKING';readGeneration++;readPending=false;diffResult=null;diffError='';historyResult=null;historyStarts=[];historyStart='HEAD';historySelected=null;historyError='';if(closeHistory&&modalView==='history'&&$('modal').open)$('modal').close();}
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
 const folders=new Set(['']);for(const row of state.rows){const parts=row.path.split('/');for(let i=1;i<parts.length;i++)folders.add(parts.slice(0,i).join('/'));}
 const sorted=[...folders].sort((a,b)=>a.localeCompare(b));
 $('folder-tree').innerHTML=sorted.map((folder,i)=>{const depth=folder?folder.split('/').length:0;const count=filteredRows(state.rows,{folder}).length;return `<button class="folder-item ${folder===state.folder?'selected':''}" data-folder-index="${i}" aria-label="筛选目录 ${esc(folder||'全部目录')}"><span class="tree-arrow">${folder?'›':'⌄'}</span>${icon(folder?'folder':'computer')}<span class="folder-label">${esc(folder?fileName(folder):state.workspace)}</span><span class="folder-count">${count}</span></button>`;}).join('');
 // No inline style is needed under strict CSP: depth encoded with finite classes.
 $('folder-tree').querySelectorAll('button').forEach((button,i)=>{button.removeAttribute('style');button.classList.add(`depth-${Math.min(4,sorted[i]?sorted[i].split('/').length:0)}`);button.onclick=()=>{state.folder=sorted[i];state.collapsed.clear();render();};});
}
function render(){
 if(changelistSelection!==selectionKey()){changelistSelection=selectionKey();invalidateChangelist();invalidateShelf();invalidateCommit();}
 updateShelfButtons();updateCommitButtons();
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
  tr.ondblclick=()=>{showDiff();};
  tr.onkeydown=e=>{if(!['ArrowDown','ArrowUp',' '].includes(e.key))return;e.preventDefault();if(e.key===' '){state.selected.has(row.id)?state.selected.delete(row.id):state.selected.add(row.id);}else{const idx=visible.findIndex(r=>r.id===row.id),target=visible[Math.max(0,Math.min(visible.length-1,idx+(e.key==='ArrowDown'?1:-1)))];state.focused=target.id;}render();const focusIndex=state.rows.findIndex(r=>r.id===state.focused);$('file-body').querySelector(`[data-row-index="${focusIndex}"]`)?.focus();};
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
 $('detail-content').innerHTML=`<div class="detail-hero"><div class="detail-file-icon">${icon(/\.cs$/.test(row.path)?'code':'file')}</div><h2>${esc(fileName(row.path))}</h2><div class="detail-path">${esc(row.path)}</div>${statusTag(row)}</div><section class="detail-section"><div class="detail-eyebrow">WORKING COPY</div><dl><dt>变更列表</dt><dd class="blue">${esc(row.changelist||'默认变更列表')}</dd><dt>BASE 修订</dt><dd>${row.revision===null||row.revision===undefined?'—':`r${esc(row.revision)}`}</dd><dt>最后作者</dt><dd>${esc(row.last_changed_author||'未返回')}</dd><dt>属性状态</dt><dd>${esc(row.props==='none'?'无属性更改':row.props)}</dd><dt>节点类型</dt><dd>${row.kind==='file'?(demo?'文件（示例）':'文件'):'状态未提供'}</dd></dl></section><div class="detail-warning">${icon(s===STATES.conflicted?'alert':'shield')}<span>${s===STATES.conflicted?'此项存在冲突。原型仅显示状态，不提供自动解决或合并。':demo?'当前为演示数据。差异和历史也是示例，未读取真实工程。':'可显式读取只读差异或历史。历史与修订比较会访问仓库；本地 changelist 与 Shelf 需启动时显式启用、预检及独立确认。Shelf 恢复会修改文件；不会提交。'}</span></div><button class="detail-action" id="detail-diff">${icon('diff')}查看差异预览</button><button class="detail-action" id="detail-history">${icon('history')}查看文件历史${demo?'（示例）':''}</button><div class="detail-footer">SVN changelist 是工作副本本地标签，<br>不会生成服务器编号提交单。</div>`;
 $('detail-diff').onclick=()=>{showDiff();};$('detail-history').onclick=showHistory;
}
function renderPreview(){
 const row=state.rows.find(r=>r.id===state.focused),demo=state.mode==='demo';
 $('preview-panel').classList.toggle('live-preview',state.mode==='live'&&state.preview==='diff');
 document.querySelectorAll('[data-preview]').forEach(b=>b.classList.toggle('active',b.dataset.preview===state.preview));
 $('diff-filename').textContent=row?fileName(row.path):'未选择文件';$('preview-kind').textContent=demo?'示例内容 · 非真实文件':'真实只读差异 · 显式读取';
 $('diff-stats').textContent='';document.querySelector('.diff-endpoints').textContent=demo?'BASE → WORKING':diffResult?`${diffResult.fromRevision} → ${diffResult.toRevision}`:'按下方修订读取';
 if(!row){$('diff-content').innerHTML='<div class="diff-empty">选择文件以查看可用预览。</div>';return;}
 if(state.preview==='properties'){$('diff-content').innerHTML=`<div class="properties-grid"><span>相对路径</span><span>${esc(row.path)}</span><span>文本状态</span><span>${esc(row.item)}</span><span>属性状态</span><span>${esc(row.props)}</span><span>数据来源</span><span>${demo?'内置示例数据':'本地 svn status --xml'}</span></div>`;return;}
 if(!demo){renderLiveDiff();return;}
 if(demo && row.id===demoRows[0].id){
  let old=24,working=24;$('diff-stats').innerHTML='+5 <span>−2</span>';
  $('diff-content').innerHTML=demoDiff.map(([kind,code])=>{const oldNo=kind==='h'||kind==='+'?'':old++,newNo=kind==='h'||kind==='-'?'':working++;return `<div class="diff-line ${kind==='+'?'add':kind==='-'?'remove':kind==='h'?'hunk':''}"><span class="line-no">${oldNo}</span><span class="line-no">${newNo}</span><span class="line-sign">${kind==='h'?'':esc(kind)}</span><span class="line-code">${esc(code)}</span></div>`;}).join('');
 }else{$('diff-content').innerHTML=`<div class="diff-empty">${demo?'该示例没有内置差异内容。选择 PlayerController.cs 可体验文本差异预览。':'此原型只接入本地 status 元数据，未读取真实文件内容或执行 diff。'}<br>文件：${esc(row.path)}</div>`;}
}
function modal(title,content){invalidateCommit();$('commit-confirm').classList.add('hidden');invalidateShelf();$('shelf-confirm').classList.add('hidden');$('changelist-confirm').classList.add('hidden');invalidateChangelist();invalidateReads();renderPreview();modalView='generic';workspaceRequest++;$('modal-title').textContent=title;$('modal-content').innerHTML=content;if(!$('modal').open)$('modal').showModal();}
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

function review(){const rows=reviewSelection(state.rows,state.selected);modal(`审查选中项 · ${rows.length} 个文件`,`<p>这是只读审查，不会提交。列表包含已选择但被筛选或折叠隐藏的文件；不会自动扩展目录范围。</p><div>${rows.map(r=>`<div class="review-file">${statusTag(r)}<span>${esc(r.path)}</span></div>`).join('')}</div><p class="history-disclaimer">${state.mode==='demo'?'当前为演示数据。':'当前为真实本地状态。'} 提交与还原未接入；本地归组与 Shelf 需分别启用、预检并确认。</p>`);log(`打开只读审查：${rows.length} 项明确选择；未发送写操作。`);}
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
async function applyShelf(){
 const plan=shelfPlan;if(!plan||!shelvesAvailable()||shelfApplying||!$('modal').open||modalView!==plan.view)return;
 if(!sameShelfScope(plan.request,shelfScope())||plan.key!==selectionKey()||Date.now()>=plan.expiresAt){invalidateShelf();return;}
 if(plan.request.operation==='create'&&parseShelfName($('shelf-name').value)!==plan.request.name||plan.request.operation==='restore'&&shelfChoice?.id!==plan.request.shelfId){invalidateShelf();return;}
 const generation=shelfGeneration;shelfPlan=null;shelfApplying=true;invalidateReads();setBusy(true);renderPreview();
 for(const id of ['shelf-confirm','shelf-preflight','shelf-name'])if($(id))$(id).disabled=true;
 $('shelf-result').textContent='请求已发送，正在执行。关闭窗口不等于取消；不要重复操作或同时编辑文件。';
 try{
  const payload=await api('/api/shelf-apply',{...shelfScope(),planToken:plan.result.planToken,confirmed:true});
  if(generation!==shelfGeneration||modalView!==plan.view||!$('modal').open||!sameShelfScope(plan.request,shelfScope()))throw Error('窗口或工作范围已改变，未采用迟到的执行响应。');
  const result=payload?.shelfResult,expected=plan.result.files.map(f=>f.path).sort();
  if(payload.mode!=='live'||payload.workspaceId!==plan.request.workspaceId||payload.selectionVersion!==plan.request.selectionVersion||typeof payload.snapshotToken!=='string'||!payload.snapshotToken||payload.snapshotToken===plan.request.snapshotToken||!result||result.operation!==plan.request.operation||result.name!==plan.result.name||typeof result.shelfId!=='string'||!result.shelfId||plan.request.operation==='restore'&&result.shelfId!==plan.request.shelfId||result.shelfRetained!==true||!Array.isArray(result.files)||JSON.stringify([...result.files].sort())!==JSON.stringify(expected))throw Error('执行响应身份或完整文件范围无效。');
  applySnapshot(payload);const message=`${result.operation==='create'?'本地 Shelf 已创建，源文件未清除':'整个 Shelf 已恢复，备份仍保留'}：${result.name} · ${result.files.length} 个文件。`;
  $('shelf-result').textContent=message;log(message);notify(message);
 }catch(error){
  const message=`Shelf 执行结果未知，可能已部分或全部生效。请显式刷新并重新审查；不会自动重试。${error.message}`;
  failSnapshot(message);if($('shelf-result'))$('shelf-result').textContent=message;log(message,'WARN');notify('Shelf 结果未知，请显式刷新并审查。');
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
 commitChoice={operation,name};modalView='commit';$('commit-confirm').classList.remove('hidden');$('commit-message').oninput=()=>invalidateCommit();$('commit-keep').onchange=()=>invalidateCommit();$('commit-preflight').onclick=preflightCommit;
}
function commitRequest(){const rows=commitScopeRows(state.rows,state.selected,commitChoice.operation,commitChoice.name);return {...shelfScope(),...commitChoice,ids:rows.map(r=>r.id),message:parseCommitMessage($('commit-message').value),keepChangelist:$('commit-keep').checked};}
async function preflightCommit(){
 if(!commitsAvailable()||modalView!=='commit'||!$('modal').open)return;invalidateCommit();let request;try{request=commitRequest();}catch(error){$('commit-result').textContent=error.message;return;}
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
async function applyCommit(){
 const plan=commitPlan;if(!plan||commitApplying||!commitsAvailable()||modalView!=='commit'||!$('modal').open)return;
 try{if(Date.now()>=plan.expiresAt||plan.key!==selectionKey()||JSON.stringify(plan.request)!==JSON.stringify(commitRequest())){invalidateCommit();return;}}catch(error){invalidateCommit(error.message);return;}
 const generation=commitGeneration;commitPlan=null;commitApplying=true;invalidateReads();setBusy(true);renderPreview();
 for(const id of ['commit-confirm','commit-preflight','commit-message','commit-keep'])$(id).disabled=true;
 $('commit-result').textContent='正在提交到仓库。关闭不等于取消；不要重复操作。';
 try{
  const payload=await api('/api/commit-apply',{workspaceId:plan.request.workspaceId,selectionVersion:plan.request.selectionVersion,snapshotToken:plan.request.snapshotToken,planToken:plan.result.planToken,confirmed:true});
  const result=validateCommitResult(payload,plan.request,plan.result.files);
  const stale=generation!==commitGeneration||modalView!=='commit'||!$('modal').open||!sameShelfScope(plan.request,shelfScope());
  const message=commitResultSummary(result,{statusRefreshed:payload.statusRefreshed,stale});
  if(stale||!payload.statusRefreshed)failSnapshot(message);else applySnapshot(payload);
  if($('commit-result')){$('commit-result').textContent=message;$('commit-result').setAttribute('role',result.warnings.length||result.outcome!=='committed'||stale?'alert':'status');}
  log(message,result.warnings.length||result.outcome!=='committed'||stale?'WARN':'INFO');notify(message);
 }catch(error){const message='提交结果未知：未收到可验证的提交响应。仓库可能已写入。请显式刷新本地状态并检查仓库历史，再决定下一步；不会自动重试。';failSnapshot(message);if($('commit-result'))$('commit-result').textContent=message;log(message,'WARN');notify('提交结果未知，请刷新并检查仓库历史；不要直接重试。');}
 finally{commitApplying=false;setBusy(false);render();}
}

async function api(path,body){
 const options={headers:{'X-SVN-Desk-Token':token},cache:'no-store'};
 if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
 const r=await fetch(path,options);let data;try{data=await r.json();}catch{throw Error('本地服务返回了无效响应。');}
 if(!r.ok)throw Error(data.error||'本地请求失败。');return data;
}
function updateMode(){
 const demo=state.mode==='demo',choose=state.mode==='select';
 $('mode-pill').innerHTML=`<span class="dot"></span>${state.failed?'读取失败 · 状态未知':demo?'演示数据 · 本地模拟':choose?'真实模式 · 待选工作区':state.mode==='live'?(state.commitsEnabled?'真实状态 · 仓库提交已启用':state.shelfWritesEnabled?'真实本地状态 · Shelf 可用':state.changelistWritesEnabled?'真实本地状态 · 本地归组可用':'真实本地状态 · 只读'):'正在读取状态'}`;
 $('status-text').textContent=state.failed?'状态不可用，不代表工作副本干净':demo?'演示数据，未连接仓库':choose?'尚未选择工作副本，未运行 SVN':state.commitsEnabled?'本地状态 · 仓库提交须预检并确认':'状态仅来自本地 · 仓库按需读取';
 $('workspace-address').textContent=state.root;$('workspace-name').textContent=state.workspace;$('context-workspace').textContent=state.workspace;
 $('workspace-description').textContent=demo?'本地工作副本 · 示例':choose?'仅可选择授权候选':state.commitsEnabled?'当前工作副本 · 仓库提交已启用':state.shelfWritesEnabled?'当前工作副本 · Shelf 已启用':state.changelistWritesEnabled?'当前工作副本 · 本地标签可写':'当前工作副本 · 只读';
 $('branch-label').textContent=demo?'trunk（示例）':'未查询分支';$('revision-label').textContent=demo?'BASE r2841（示例）':'修订见文件行';$('history-indicator').textContent=demo?'示例':'按需读取';
}
function resetWorkspaceScope(){invalidateCommit();invalidateShelf();invalidateChangelist();invalidateReads();state.selected.clear();state.focused=null;state.folder='';state.query='';state.filter='all';state.collapsed.clear();$('search').value='';}
function applySnapshot(payload,initial=false){
 invalidateCommit();state.commitsEnabled=payload.commitsEnabled===true;
 invalidateShelf();invalidateChangelist();invalidateReads();state.shelfWritesEnabled=payload.shelfWritesEnabled===true;state.changelistWritesEnabled=payload.changelistWritesEnabled===true;state.snapshotToken=payload.snapshotToken;state.fileReadsEnabled=payload.fileReadsEnabled===true;
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
function failSnapshot(message){invalidateCommit();state.commitsEnabled=false;invalidateShelf();state.shelfWritesEnabled=false;invalidateChangelist();invalidateReads();state.changelistWritesEnabled=false;state.snapshotToken=null;state.fileReadsEnabled=false;state.failed=true;state.rows=[];state.selected.clear();state.focused=null;if(state.mode==='loading')state.mode='error';$('live-error').textContent=message;$('live-error').classList.remove('hidden');updateMode();render();$('read-time').textContent='状态未知';}
function setBusy(busy){state.busy=busy;updateShelfButtons();updateCommitButtons();$('move-changelist').disabled=!changelistsAvailable();$('refresh').disabled=busy;$('open-workspace').disabled=busy;}
async function refresh(initial=false){
 if(state.busy)return;invalidateCommit();invalidateShelf();invalidateChangelist();invalidateReads();setBusy(true);renderPreview();$('read-time').textContent='正在读取…';
 try{
  const config=standalone?{mode:'demo',workspace:'Project Atlas',root:'D:\\Workspace\\ProjectAtlas'}:await api('/api/config');
  // Identity is known even if the subsequent status read fails.
  state.workspace=config.workspace;state.root=config.root;
  if(config.mode==='live'||config.mode==='select')state.mode=config.mode;
  const payload=standalone?{mode:'demo'}:await api('/api/status');
  if(config.mode!==payload.mode||config.workspaceId!==payload.workspaceId||config.selectionVersion!==payload.selectionVersion)throw Error('工作区在读取期间发生变化，请重新刷新。');
  applySnapshot({...config,...payload,commitsEnabled:payload.commitsEnabled===true,shelfWritesEnabled:payload.shelfWritesEnabled===true,changelistWritesEnabled:payload.changelistWritesEnabled===true},initial);
  log(state.mode==='demo'?`加载 ${state.rows.length} 项内置演示状态；未运行 SVN。`:state.mode==='select'?'尚未选择工作副本；请打开工作区选择器。':`完成本地 svn status：${state.rows.length} 项状态；未查询远程。`);
 }catch(error){failSnapshot('状态读取失败，未显示任何部分或缓存结果。请检查服务、工作副本和 SVN 安装后重试。');log('状态读取失败；没有返回部分状态，没有切换到演示数据。','WARN');}
 finally{setBusy(false);renderPreview();renderHistory();}
}
const workspaceInstructions=()=>`<p>当前为演示模式，没有扫描本机目录。若要启用真实只读工作区，请使用可信后端和已安装的 SVN 启动：</p><code>python server.py --scan-parent "C:\\Work" --backend-source "C:\\Tools\\svn-desk"</code><p>可重复 --workspace 或 --scan-parent。可选 --settings-file 指定扫描范围外的专用偏好文件，父目录需已存在。网页不能输入任意路径，不能检出或普通还原；仓库提交需另行显式启用。</p>`;
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
 if(state.busy)return;invalidateCommit();invalidateShelf();invalidateChangelist();invalidateReads();setBusy(true);
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
$('refresh').onclick=()=>refresh();$('search').addEventListener('input',e=>{state.query=e.target.value;state.collapsed.clear();render();});
document.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{state.filter=b.dataset.filter;state.collapsed.clear();render();});
$('pending-nav').onclick=clearFilters;$('pending-tab').onclick=clearFilters;$('conflict-nav').onclick=()=>{state.filter='conflicted';state.collapsed.clear();state.folder='';state.query='';$('search').value='';render();};
$('workspace-home').onclick=clearFilters;$('clear-filters').onclick=clearFilters;$('empty-reset').onclick=clearFilters;
$('select-visible').onchange=e=>{for(const row of displayed()){e.target.checked?state.selected.add(row.id):state.selected.delete(row.id);}render();};
$('collapse-all').onclick=()=>{const groups=groupsFor(matches()),all=groups.every(([name])=>state.collapsed.has(name));for(const [name]of groups){all?state.collapsed.delete(name):state.collapsed.add(name);}$('collapse-all').innerHTML=`${icon('collapse')}${all?'折叠分组':'展开分组'}`;render();};
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
$('help').onclick=()=>modal('SVN Desk · Web 原型',`<p>现代 Web 视觉，保留工作区树、原生 changelist 分组、选中项审查、详情与独立日志的桌面工作流。</p><ul><li>真实可用：筛选、选中、折叠分组、目录范围、只读审查与布局切换。</li><li>演示：选定示例文件的差异、历史与工作副本身份。</li><li>可选桥接：真实本地 status、显式只读 diff 与仓库历史。</li><li>本地归组：需显式启用，预检后单独确认；演示模式仅模拟。Shelf：显式启用后支持真实本地备份与完整恢复；无演示模拟。仓库提交：需显式启用，完整范围预检后另行确认。未实现：普通还原、远程仓库浏览。</li></ul><p>快捷键：Ctrl/Cmd K 搜索 · F5 刷新 · ↑ ↓ 浏览 · 空格选择 · Esc 关闭弹窗</p><p>运行方式：浏览器原型。Tauri 壳仅提供源码，尚未编译或完成 Windows 验收。</p>`);
$('modal').addEventListener('cancel',()=>{invalidateCommit();invalidateShelf();invalidateChangelist();invalidateReads(false);$('modal-content').innerHTML='';renderPreview();});
$('modal').addEventListener('close',()=>{invalidateCommit();$('commit-confirm').classList.add('hidden');if(commitApplying)failSnapshot('提交请求已发送；关闭不等于取消，仓库可能已写入。请等待请求结束后显式刷新并检查仓库历史，切勿直接重试。');invalidateShelf();$('shelf-confirm').classList.add('hidden');if(shelfApplying)failSnapshot('Shelf 请求已发送；关闭不等于取消，可能已部分或全部生效。请等待请求结束后显式刷新并重新审查。');$('changelist-confirm').classList.add('hidden');invalidateChangelist();if(changelistApplying)failSnapshot('归组请求仍在执行，关闭窗口不等于取消。结果返回前状态未知；请查看操作日志。');workspaceRequest++;modalView='';invalidateReads(false);$('modal-content').innerHTML='';renderPreview();});
$('modal-close').onclick=$('modal-done').onclick=()=>$('modal').close();$('modal').addEventListener('click',e=>{if(e.target===$('modal')){const r=$('modal').getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)$('modal').close();}});
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('search').focus();}if(e.key==='F5'){e.preventDefault();refresh();}});
log('启动 Web 原型；本地归组、Shelf 与仓库提交仅在分别显式启用、预检及独立确认后执行。','WARN');await refresh(true);
