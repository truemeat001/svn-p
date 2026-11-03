import {STATES,stateFor,filteredRows,groupsFor,summary,reviewSelection,safeText as esc,fileName,parentPath} from './model.js';
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
const state={mode:standalone?'demo':'loading',rows:[],query:'',filter:'all',folder:'',selected:new Set(),focused:null,collapsed:new Set(['']),preview:'diff',logs:[],busy:false,failed:false,workspace:'Project Atlas',root:'D:\\Workspace\\ProjectAtlas'};
let toastTimer;
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
 const rows=matches(),groups=groupsFor(rows),visible=displayed(),counts=summary(state.rows);
 $('file-body').innerHTML=groups.map(([name,items],groupIndex)=>`<tr class="group-row"><td colspan="5"><button data-group-index="${groupIndex}" aria-expanded="${!state.collapsed.has(name)}">${icon(state.collapsed.has(name)?'chevron-right':'chevron-down')}${icon(name?'layers':'folder')}<span class="group-label">${esc(name||'默认变更列表')}</span><span class="group-count">${items.length} 个文件</span><span class="group-meta">${name?'本地标签':'未分组'}</span></button></td></tr>${state.collapsed.has(name)?'':items.map(row=>`<tr class="file-row ${state.selected.has(row.id)?'selected':''} ${state.focused===row.id?'focused':''}" tabindex="0" data-row-index="${state.rows.indexOf(row)}" aria-label="${esc(fileName(row.path))}，${esc(stateFor(row).label)}" aria-selected="${state.selected.has(row.id)}"><td class="check-cell"><input type="checkbox" ${state.selected.has(row.id)?'checked':''} aria-label="选择 ${esc(row.path)}"></td><td title="${esc(row.path)}">${fileIcon(row)}${esc(fileName(row.path))}</td><td>${statusTag(row)}</td><td class="file-path path-column" title="${esc(parentPath(row.path))}">${esc(parentPath(row.path))}</td><td class="revision-cell">${row.revision===null||row.revision===undefined?'—':`r${esc(row.revision)}`}</td></tr>`).join('')}`).join('');
 $('file-body').querySelectorAll('[data-group-index]').forEach(b=>b.onclick=()=>{const group=groups[Number(b.dataset.groupIndex)][0];state.collapsed.has(group)?state.collapsed.delete(group):state.collapsed.add(group);render();});
 $('file-body').querySelectorAll('[data-row-index]').forEach(tr=>{
  const row=state.rows[Number(tr.dataset.rowIndex)];
  tr.onclick=event=>{state.focused=row.id;if(event.target.type==='checkbox'||event.ctrlKey||event.metaKey){state.selected.has(row.id)?state.selected.delete(row.id):state.selected.add(row.id);}else{state.selected=new Set([row.id]);}render();};
  tr.ondblclick=()=>{state.preview='diff';renderPreview();$('preview-panel').classList.remove('collapsed');};
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
 $('status-summary').textContent=`${counts.total} 项状态 · ${counts.conflicted} 个冲突`;
 $('folder-scope').classList.toggle('hidden',!state.folder);$('folder-scope').innerHTML=`目录范围：${esc(state.folder)} <button id="reset-folder">清除</button>`;if($('reset-folder'))$('reset-folder').onclick=()=>{state.folder='';render();};
 document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b.dataset.filter===state.filter));
 $('conflict-nav').classList.toggle('active',state.filter==='conflicted');$('pending-nav').classList.toggle('active',state.filter!=='conflicted');
 renderTree();renderDetail();renderPreview();
}
function renderDetail(){
 const row=state.rows.find(r=>r.id===state.focused);
 if(!row){$('detail-content').innerHTML='<div class="detail-section"><p>选择文件查看详情。</p></div>';return;}
 const s=stateFor(row),demo=state.mode==='demo';
 $('detail-content').innerHTML=`<div class="detail-hero"><div class="detail-file-icon">${icon(/\.cs$/.test(row.path)?'code':'file')}</div><h2>${esc(fileName(row.path))}</h2><div class="detail-path">${esc(row.path)}</div>${statusTag(row)}</div><section class="detail-section"><div class="detail-eyebrow">WORKING COPY</div><dl><dt>变更列表</dt><dd class="blue">${esc(row.changelist||'默认变更列表')}</dd><dt>BASE 修订</dt><dd>${row.revision===null||row.revision===undefined?'—':`r${esc(row.revision)}`}</dd><dt>最后作者</dt><dd>${esc(row.last_changed_author||'未返回')}</dd><dt>属性状态</dt><dd>${esc(row.props==='none'?'无属性更改':row.props)}</dd><dt>节点类型</dt><dd>${row.kind==='file'?'文件（示例）':'状态未提供'}</dd></dl></section><div class="detail-warning">${icon(s===STATES.conflicted?'alert':'shield')}<span>${s===STATES.conflicted?'此项存在冲突。原型仅显示状态，不提供自动解决或合并。':demo?'当前为演示数据。差异和历史也是示例，未读取真实工程。':'只读状态已接入。差异、历史与写入尚未接入；不会访问仓库。'}</span></div><button class="detail-action" id="detail-diff">${icon('diff')}查看差异预览</button><button class="detail-action" id="detail-history">${icon('history')}查看文件历史${demo?'（示例）':''}</button><div class="detail-footer">SVN changelist 是工作副本本地标签，<br>不会生成服务器编号提交单。</div>`;
 $('detail-diff').onclick=()=>{state.preview='diff';renderPreview();$('preview-panel').classList.remove('collapsed');};$('detail-history').onclick=showHistory;
}
function renderPreview(){
 const row=state.rows.find(r=>r.id===state.focused),demo=state.mode==='demo';
 document.querySelectorAll('[data-preview]').forEach(b=>b.classList.toggle('active',b.dataset.preview===state.preview));
 $('diff-filename').textContent=row?fileName(row.path):'未选择文件';$('preview-kind').textContent=demo?'示例内容 · 非真实文件':'真实差异尚未接入';
 $('diff-stats').textContent='';
 if(!row){$('diff-content').innerHTML='<div class="diff-empty">选择文件以查看可用预览。</div>';return;}
 if(state.preview==='properties'){$('diff-content').innerHTML=`<div class="properties-grid"><span>相对路径</span><span>${esc(row.path)}</span><span>文本状态</span><span>${esc(row.item)}</span><span>属性状态</span><span>${esc(row.props)}</span><span>数据来源</span><span>${demo?'内置示例数据':'本地 svn status --xml'}</span></div>`;return;}
 if(demo && row.id===demoRows[0].id){
  let old=24,working=24;$('diff-stats').innerHTML='+5 <span>−2</span>';
  $('diff-content').innerHTML=demoDiff.map(([kind,code])=>{const oldNo=kind==='h'||kind==='+'?'':old++,newNo=kind==='h'||kind==='-'?'':working++;return `<div class="diff-line ${kind==='+'?'add':kind==='-'?'remove':kind==='h'?'hunk':''}"><span class="line-no">${oldNo}</span><span class="line-no">${newNo}</span><span class="line-sign">${kind==='h'?'':esc(kind)}</span><span class="line-code">${esc(code)}</span></div>`;}).join('');
 }else{$('diff-content').innerHTML=`<div class="diff-empty">${demo?'该示例没有内置差异内容。选择 PlayerController.cs 可体验文本差异预览。':'此原型只接入本地 status 元数据，未读取真实文件内容或执行 diff。'}<br>文件：${esc(row.path)}</div>`;}
}
function modal(title,content){$('modal-title').textContent=title;$('modal-content').innerHTML=content;if(!$('modal').open)$('modal').showModal();}
function showHistory(){
 const row=state.rows.find(r=>r.id===state.focused);
 if(!row)return notify('先选择一个文件。');
 if(state.mode!=='demo')return modal('文件历史尚未接入','<p>这轮只读取本地状态，没有执行 svn log 或访问仓库。</p>');
 if(row.id!==demoRows[0].id)return modal('没有此文件的示例历史',`<p>${esc(row.path)}</p><p>仅 PlayerController.cs 内置了演示历史。没有向真实仓库查询。</p>`);
 modal('PlayerController.cs · 示例历史',`<p class="history-disclaimer">以下是虚构历史，用于体验布局。未访问真实仓库。</p><div class="history-list">${demoHistory.map(h=>`<div class="history-item"><strong>r${h.revision}</strong><small>${esc(h.author)} · ${esc(h.time)}</small><p>${esc(h.message)}</p></div>`).join('')}</div>`);
}
function review(){const rows=reviewSelection(state.rows,state.selected);modal(`审查选中项 · ${rows.length} 个文件`,`<p>这是只读审查，不会提交。列表包含已选择但被筛选或折叠隐藏的文件；不会自动扩展目录范围。</p><div>${rows.map(r=>`<div class="review-file">${statusTag(r)}<span>${esc(r.path)}</span></div>`).join('')}</div><p class="history-disclaimer">${state.mode==='demo'?'当前为演示数据。':'当前为真实本地状态。'} 提交、还原、归组与 Shelf 操作均未接入。</p>`);log(`打开只读审查：${rows.length} 项明确选择；未发送写操作。`);}
async function api(path){const r=await fetch(path,{headers:{'X-SVN-Desk-Token':token},cache:'no-store'});if(!r.ok)throw Error('只读状态请求失败。');return r.json();}
function updateMode(){const demo=state.mode==='demo';$('mode-pill').innerHTML=`<span class="dot"></span>${state.failed?'读取失败 · 状态未知':demo?'演示数据 · 只读原型':state.mode==='live'?'真实本地状态 · 只读':'正在读取状态'}`;$('status-text').textContent=demo?'演示数据，未连接仓库':state.mode==='live'?'只读本地状态 · 未检查仓库连接':'状态不可用，不代表工作副本干净';$('workspace-address').textContent=state.root;$('workspace-name').textContent=state.workspace;$('context-workspace').textContent=state.workspace;$('workspace-description').textContent=demo?'本地工作副本 · 示例':'启动时指定 · 只读';$('branch-label').textContent=demo?'trunk（示例）':'未查询分支';$('revision-label').textContent=demo?'BASE r2841（示例）':'修订见文件行';$('history-indicator').textContent=demo?'示例':'未接入';}
async function refresh(initial=false){
 if(state.busy)return;state.busy=true;$('refresh').disabled=true;$('read-time').textContent='正在读取…';
 try{
  const config=standalone?{mode:'demo',workspace:'Project Atlas',root:'D:\\Workspace\\ProjectAtlas'}:await api('/api/config');
  const payload=standalone?{mode:'demo'}:await api('/api/status');
  if(config.mode!==payload.mode || !['demo','live'].includes(config.mode))throw Error('Mode mismatch');
  state.workspace=payload.workspace||config.workspace;state.root=payload.root||config.root;
  state.rows=payload.mode==='demo'?structuredClone(demoRows):payload.rows;
  if(!Array.isArray(state.rows))throw Error('Invalid rows');
  state.mode=payload.mode;state.failed=false;$('live-error').classList.add('hidden');
  const ids=new Set(state.rows.map(r=>r.id));state.selected=new Set([...state.selected].filter(id=>ids.has(id)));
  if(!ids.has(state.focused)){state.focused=state.mode==='demo'?demoRows[0].id:state.rows[0]?.id||null;if(initial&&state.focused)state.selected.add(state.focused);}
  updateMode();render();$('read-time').textContent=`已读取 ${new Date().toLocaleTimeString('en-GB')}`;
  log(state.mode==='demo'?`加载 ${state.rows.length} 项内置演示状态；未运行 SVN。`:`完成本地 svn status：${state.rows.length} 项状态；未查询远程。`);
 }catch(error){state.failed=true;state.rows=[];state.selected.clear();state.focused=null;if(state.mode==='loading')state.mode='error';$('live-error').textContent='状态读取失败，未显示任何部分或缓存结果。请检查服务、工作副本和 SVN 安装后重试。';$('live-error').classList.remove('hidden');updateMode();$('status-text').textContent='状态读取失败，不代表工作副本干净';render();$('read-time').textContent='读取失败';log('状态读取失败；没有返回部分状态，没有切换到演示数据。','WARN');}
 finally{state.busy=false;$('refresh').disabled=false;}
}
function clearFilters(){state.query='';state.filter='all';state.folder='';$('search').value='';render();}
paintIcons();
$('refresh').onclick=()=>refresh();$('search').addEventListener('input',e=>{state.query=e.target.value;state.collapsed.clear();render();});
document.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{state.filter=b.dataset.filter;state.collapsed.clear();render();});
$('pending-nav').onclick=clearFilters;$('pending-tab').onclick=clearFilters;$('conflict-nav').onclick=()=>{state.filter='conflicted';state.collapsed.clear();state.folder='';state.query='';$('search').value='';render();};
$('workspace-home').onclick=clearFilters;$('clear-filters').onclick=clearFilters;$('empty-reset').onclick=clearFilters;
$('select-visible').onchange=e=>{for(const row of displayed()){e.target.checked?state.selected.add(row.id):state.selected.delete(row.id);}render();};
$('collapse-all').onclick=()=>{const groups=groupsFor(matches()),all=groups.every(([name])=>state.collapsed.has(name));for(const [name]of groups){all?state.collapsed.delete(name):state.collapsed.add(name);}$('collapse-all').innerHTML=`${icon('collapse')}${all?'折叠分组':'展开分组'}`;render();};
$('view-diff').onclick=()=>{state.preview='diff';renderPreview();$('preview-panel').classList.remove('collapsed');};$('review').onclick=review;
document.querySelectorAll('[data-preview]').forEach(b=>b.onclick=()=>{state.preview=b.dataset.preview;renderPreview();});
$('toggle-preview').onclick=()=>{$('preview-panel').classList.toggle('collapsed');$('toggle-preview').setAttribute('aria-label',$('preview-panel').classList.contains('collapsed')?'展开差异面板':'折叠差异面板');};
$('toggle-log').onclick=()=>document.querySelector('.activity-panel').classList.toggle('collapsed');
$('close-detail').onclick=()=>{$('details-panel').classList.add('hidden');$('details-panel').classList.remove('force-visible');};
$('toggle-detail').onclick=()=>{if(innerWidth<=1050){$('details-panel').classList.remove('hidden');$('details-panel').classList.toggle('force-visible');}else $('details-panel').classList.toggle('hidden');};
$('nav-toggle').onclick=()=>$('sidebar').classList.toggle('force-visible');
$('workspace-tab').onclick=()=>{$('workspace-tab').classList.add('active');$('repository-tab').classList.remove('active');$('repository-notice').classList.add('hidden');$('folder-tree').classList.remove('hidden');};
$('repository-tab').onclick=()=>{$('repository-tab').classList.add('active');$('workspace-tab').classList.remove('active');$('repository-notice').classList.remove('hidden');$('folder-tree').classList.add('hidden');};
$('history-tab').onclick=showHistory;
$('open-workspace').onclick=()=>modal('打开真实工作副本',`<p>原型固定读取启动时指定的工作副本，不提供网页任意路径输入。以下方式仅运行本地 status：</p><code>python server.py --workspace "C:\\Work\\Project" \\\n  --backend-source "C:\\Tools\\svn-desk"</code><p>需要已有 SVN 工作副本、可信 SVN 命令行程序和原版 SVN Desk 后端。此环境尚未验证真实 SVN；当前示例不证明连接成功。</p>`);
$('help').onclick=()=>modal('SVN Desk · Web 原型',`<p>现代 Web 视觉，保留工作区树、原生 changelist 分组、选中项审查、详情与独立日志的桌面工作流。</p><ul><li>真实可用：筛选、选中、折叠分组、目录范围、只读审查与布局切换。</li><li>演示：选定示例文件的差异、历史与工作副本身份。</li><li>可选桥接：复用原后端读取真实本地 status。</li><li>未实现：提交、还原、归组、Shelf、远程仓库浏览。</li></ul><p>快捷键：Ctrl/Cmd K 搜索 · F5 刷新 · ↑ ↓ 浏览 · 空格选择 · Esc 关闭弹窗</p><p>运行方式：浏览器原型。Tauri 壳仅提供源码，尚未编译或完成 Windows 验收。</p>`);
$('modal-close').onclick=$('modal-done').onclick=()=>$('modal').close();$('modal').addEventListener('click',e=>{if(e.target===$('modal')){const r=$('modal').getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)$('modal').close();}});
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('search').focus();}if(e.key==='F5'){e.preventDefault();refresh();}});
log('启动只读 Web 原型；所有 SVN 写操作均未接入。','WARN');await refresh(true);
