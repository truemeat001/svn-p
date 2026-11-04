export const STATES = {
  modified: {label:'已修改', short:'M', tone:'blue'}, added: {label:'新增', short:'A', tone:'green'},
  deleted: {label:'已删除',short:'D',tone:'red'}, unversioned: {label:'未版本化',short:'?',tone:'muted'},
  conflicted: {label:'冲突',short:'!',tone:'orange'}, missing: {label:'缺失',short:'!',tone:'red'},
  normal: {label:'无内容修改',short:'·',tone:'muted'}, none: {label:'属性更改',short:'P',tone:'blue'},
  replaced: {label:'已替换',short:'R',tone:'blue'}, external: {label:'外部引用',short:'X',tone:'muted'},
  ignored: {label:'已忽略',short:'I',tone:'muted'}, obstructed: {label:'阻塞',short:'!',tone:'red'},
  incomplete: {label:'不完整',short:'!',tone:'red'},
};
export function stateFor(row) {
  if (row.tree_conflicted || row.props === 'conflicted') return STATES.conflicted;
  if (row.props === 'modified' && ['normal','none'].includes(row.item)) return STATES.none;
  return STATES[row.item] || {label: row.item || '未知状态', short:'·',tone:'muted'};
}
export function filteredRows(rows, {query='', filter='all', folder=''}={}) {
  const q = query.trim().toLocaleLowerCase();
  return rows.filter(row => (!q || `${row.path} ${row.changelist || ''}`.toLocaleLowerCase().includes(q))
    && (!folder || row.path === folder || row.path.startsWith(`${folder}/`))
    && (filter === 'all' || (filter === 'conflicted' ? stateFor(row) === STATES.conflicted : filter === 'modified' ? row.item === 'modified' || row.props === 'modified' : row.item === filter)));
}
export function groupsFor(rows) {
  const groups = new Map();
  for (const row of rows) { const key=row.changelist || ''; if(!groups.has(key)) groups.set(key,[]); groups.get(key).push(row); }
  return [...groups].sort(([a],[b]) => !a ? -1 : !b ? 1 : a.localeCompare(b));
}
export function summary(rows) {
  return {total: rows.length, modified:rows.filter(r=>r.item==='modified' || r.props==='modified').length,
    conflicted: rows.filter(r=>stateFor(r)===STATES.conflicted).length,
    unversioned:rows.filter(r=>r.item==='unversioned').length};
}
export function reviewSelection(rows, selectedIds) {
  return rows.filter(row=>selectedIds.has(row.id)); // Never expands a filtered selection or directory.
}
export function safeText(value) { return String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
export function fileName(path) { return path.replace(/\\/g,'/').split('/').at(-1); }
export function parentPath(path) { return path.split('/').slice(0,-1).join('/') || '工作副本根目录'; }

// Catalog scans and status reads can finish out of order across tabs/refreshes.
export function isStaleWorkspaceCatalog(catalog,currentVersion){
 return Number.isSafeInteger(catalog?.current?.selectionVersion)&&catalog.current.selectionVersion<currentVersion;
}

function numericRevision(value){return typeof value==='string'&&/^(0|[1-9][0-9]{0,18})$/.test(value)&&BigInt(value)<=9223372036854775807n;}
// Revision tokens never become arbitrary command arguments. Keep integers as strings.
export function parseRevision(value,{working=false,history=false}={}){
 if(typeof value!=='string')throw Error('修订必须是文本。');
 const token=value.trim().toUpperCase();
 if(numericRevision(token))return token;
 if(token==='HEAD'||(!history&&token==='BASE')||(!history&&working&&token==='WORKING'))return token;
 throw Error(history?'请输入 HEAD 或非负整数修订。':'请输入 BASE、HEAD 或非负整数修订；WORKING 仅用于右侧。');
}
export function parseComparisonPair(from,to){
 const fromRevision=parseRevision(from),toRevision=parseRevision(to,{working:true});
 if(fromRevision==='BASE'&&toRevision==='BASE')throw Error('不支持 BASE → BASE；查看本地修改请选择 BASE → WORKING，或输入明确的数字修订进行比较。');
 return {fromRevision,toRevision};
}
export function canReadFile(row){return !!row&&!['unversioned','ignored','external','obstructed','incomplete'].includes(row.item)&&!row.switched&&!row.file_external;}
export function sameReadScope(a,b){return !!a&&!!b&&['workspaceId','selectionVersion','snapshotToken','id','path'].every(key=>a[key]===b[key]);}
export function validateFileResponse(data,scope,kind,revision){
 if(data?.mode!=='live'||!sameReadScope(data,scope))throw Error('文件结果已过期或身份不匹配，请刷新状态。');
 if(kind==='diff'){
  parseComparisonPair(data.fromRevision,data.toRevision);
  if(![data.undecodableBytes,data.escapedControls].every(n=>Number.isSafeInteger(n)&&n>=0&&n<=8388608)||data.textEncoding!==(data.undecodableBytes===0&&data.escapedControls===0?'utf-8':'utf-8-visible-escapes'))throw Error('差异编码信息缺失或无效；无法确认是否需要显示转义警告。');
  if(!(['BASE','HEAD'].includes(data.fromRevision)||numericRevision(data.fromRevision))||!(['BASE','HEAD','WORKING'].includes(data.toRevision)||numericRevision(data.toRevision))||data.fromRevision!==revision.fromRevision||data.toRevision!==revision.toRevision||typeof data.text!=='string'||typeof data.truncated!=='boolean'||data.binaryPayloadsIncluded!==false||!Number.isSafeInteger(data.sourceUtf8Bytes)||!Number.isSafeInteger(data.returnedUtf8Bytes)||data.returnedUtf8Bytes<0||data.sourceUtf8Bytes<data.returnedUtf8Bytes||data.truncated!==(data.sourceUtf8Bytes>data.returnedUtf8Bytes)||data.returnedUtf8Bytes>262144||new TextEncoder().encode(data.text).length!==data.returnedUtf8Bytes)throw Error('差异响应无效。');
 }else{
  if(!(data.startRevision==='HEAD'||numericRevision(data.startRevision))||data.startRevision!==revision.startRevision||!Array.isArray(data.entries)||data.entries.length>50||typeof data.hasMore!=='boolean'||(data.hasMore?!numericRevision(data.nextStartRevision):data.nextStartRevision!==null))throw Error('历史响应无效。');
  let previous=null;
  for(const entry of data.entries){
   if(!numericRevision(entry.revision)||!['author','date','message'].every(key=>typeof entry[key]==='string')||!Array.isArray(entry.changedPaths)||entry.changedPaths.some(p=>!p||!['path','action','kind'].every(key=>typeof p[key]==='string')||!(p.copyfrom_path===null||typeof p.copyfrom_path==='string')||!(p.copyfrom_rev===null||numericRevision(p.copyfrom_rev))))throw Error('历史条目无效。');
   const n=BigInt(entry.revision);if(previous!==null&&n>=previous||revision.startRevision!=='HEAD'&&n>BigInt(revision.startRevision))throw Error('历史顺序无效。');previous=n;
  }
  if(data.hasMore&&(previous===null||BigInt(data.nextStartRevision)!==previous-1n))throw Error('历史分页无效。');
  if(new TextEncoder().encode(JSON.stringify(data)).length>524288)throw Error('历史响应超出上限。');
 }
 return data;
}

// Native changelists are local file labels, never server-side numbered changes.
export function parseChangelistTarget(action,value){
 if(action==='remove')return null;
 if(action!=='assign'||typeof value!=='string'||/[\uD800-\uDFFF]/u.test(value)||/[\u0000-\u001f\u007f-\u009f]/u.test(value))throw Error('请输入不含控制字符的变更列表名称。');
 const name=value.trim();
 if(!name||[...name].length>128)throw Error('名称须为 1–128 个字符；前后空白会移除。');
 return name;
}
export function validateChangelistPlan(data,request,rows){
 const bad=()=>{throw Error('归组预检结果与当前工作区、选区或目标不匹配，请重新预检。');};
 if(data?.mode!=='live'||!['workspaceId','selectionVersion','snapshotToken','action','name'].every(key=>data[key]===request[key])||data.localOnly!==true||typeof data.planToken!=='string'||!data.planToken||data.planToken.length>64||/[\u0000-\u001f\u007f-\u009f]/u.test(data.planToken)||data.expiresIn!==120||!Array.isArray(data.files)||data.files.length!==request.ids.length||request.ids.length<1||request.ids.length>100||new Set(request.ids).size!==request.ids.length)bad();
 const expected=new Map(rows.filter(row=>request.ids.includes(row.id)).map(row=>[row.id,row]));
 if(expected.size!==request.ids.length)bad();
 for(const file of data.files){const row=expected.get(file?.id);if(!row||file.path!==row.path||file.fromChangelist!==(row.changelist||null))bad();expected.delete(file.id);}
 if(expected.size)bad();return data;
}
export function canRelabelFile(row){return !!row&&['normal','none','modified','added','deleted','replaced'].includes(row.item)&&row.props!=='conflicted'&&!['switched','file_external','tree_conflicted','wc_locked'].some(key=>row[key])&&(!row.kind||['file','unknown'].includes(row.kind));}

// Shelves are verified local backups, not simulated or server-side changelists.
export function canShelfFile(row){return !!row&&(['modified','added','deleted','unversioned'].includes(row.item)||['normal','none'].includes(row.item)&&row.props==='modified')&&row.props!=='conflicted'&&!['switched','file_external','tree_conflicted','wc_locked'].some(key=>row[key])&&(!row.kind||['file','unknown'].includes(row.kind));}
export function parseShelfName(value){return parseChangelistTarget('assign',value);}
export function sameShelfScope(a,b){return !!a&&!!b&&['workspaceId','selectionVersion','snapshotToken'].every(key=>a[key]===b[key]);}
export function validateShelfFiles(files,totalBytes){
 if(!Array.isArray(files)||files.length<1||files.length>100||!Number.isSafeInteger(totalBytes)||totalBytes<0)throw Error('Shelf 文件范围无效。');
 const paths=new Set();let total=0;
 for(const file of files){if(!file||typeof file.path!=='string'||!file.path||file.path.startsWith('/')||file.path.includes('\\')||file.path.split('/').some(p=>!p||p==='.'||p==='..')||/[\u0000-\u001f\u007f-\u009f]/u.test(file.path)||paths.has(file.path)||typeof file.state!=='string'||file.kind!=='file'||!Number.isSafeInteger(file.size)||file.size<0)throw Error('Shelf 文件信息无效。');paths.add(file.path);total+=file.size;}
 if(total!==totalBytes)throw Error('Shelf 大小校验不匹配。');return files;
}
export function validateShelfPlan(data,request,files){
 if(data?.mode!=='live'||!sameShelfScope(data,request)||data.operation!==request.operation||data.localOnly!==true||data.backupOnly!==(request.operation==='create')||data.expiresIn!==120||typeof data.planToken!=='string'||!data.planToken||data.planToken.length>128||/[\u0000-\u001f\u007f-\u009f]/u.test(data.planToken)||typeof data.name!=='string'||!data.name)throw Error('Shelf 预检身份或范围无效，请重新预检。');
 validateShelfFiles(data.files,data.totalBytes);
 if(request.operation==='create'&&(data.name!==request.name||!Array.isArray(request.ids)||request.ids.length!==files.length||new Set(request.ids).size!==files.length||files.some(f=>!request.ids.includes(f.id))))throw Error('Shelf 创建选区不匹配。');
 if(request.operation==='restore'&&data.shelfId!==request.shelfId)throw Error('Shelf 恢复目标不匹配。');
 const expected=new Map(files.map(f=>[f.path,f]));
 if(expected.size!==data.files.length)throw Error('Shelf 文件范围不匹配。');
 for(const f of data.files){const old=expected.get(f.path);if(!old||request.operation==='restore'&&['state','kind','size'].some(k=>f[k]!==old[k]))throw Error('Shelf 文件范围不匹配。');expected.delete(f.path);}
 if(expected.size)throw Error('Shelf 文件范围不完整。');return data;
}

// Repository commits never expand an explicit selection into its whole changelist.
export function commitScopeRows(rows,selectedIds,operation='selected',name=null){
 if(!['selected','changelist'].includes(operation)||operation==='selected'&&name!==null||operation==='changelist'&&(typeof name!=='string'||!name))throw Error('提交范围无效。');
 const files=operation==='selected'?reviewSelection(rows,selectedIds):rows.filter(row=>row.changelist===name);
 if(!files.length||files.length>100||operation==='selected'&&files.length!==selectedIds.size||new Set(files.map(f=>f.id)).size!==files.length)throw Error('提交范围必须包含 1–100 个明确文件；完整变更列表不能截取子集。');
 return files;
}
export function parseCommitMessage(value){
 if(typeof value!=='string'||!value.trim()||/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\uD800-\uDFFF]/u.test(value)||new TextEncoder().encode(value).length>8192)throw Error('提交说明不能为空，须为有效文本且不超过 8192 UTF-8 字节。');
 return value;
}
export function validateCommitPlan(data,request,rows){
 const bad=()=>{throw Error('提交预检身份、选区或文件状态不匹配，请重新预检。');};
 parseCommitMessage(request.message);
 if(data?.mode!=='live'||!sameShelfScope(data,request)||!['operation','name','keepChangelist'].every(k=>data[k]===request[k])||typeof request.keepChangelist!=='boolean'||typeof data.planToken!=='string'||!data.planToken||data.planToken.length>128||/[\u0000-\u001f\u007f-\u009f]/u.test(data.planToken)||data.expiresIn!==120||!Array.isArray(data.files)||!Array.isArray(request.ids)||new Set(request.ids).size!==request.ids.length)bad();
 const files=commitScopeRows(rows,new Set(request.ids),request.operation,request.name),expected=new Map(files.map(f=>[f.id,f]));
 if(files.length!==request.ids.length||files.some(f=>!request.ids.includes(f.id))||data.files.length!==files.length)bad();
 for(const file of data.files){const row=expected.get(file?.id);if(!row||file.path!==row.path||file.fromChangelist!==(row.changelist||null)||file.item!==row.item||file.props!==row.props)bad();expected.delete(file.id);}
 if(expected.size)bad();return data;
}

const COMMIT_WARNINGS = {
 'post-commit-hook':'提交后钩子执行失败',
 'additional-output':'SVN 返回了额外诊断信息',
 'stderr-output':'SVN 返回了标准错误诊断信息',
 'unrecognized-output':'无法识别 SVN 输出',
 'output-limit':'SVN 输出达到上限',
 'status-refresh-failed':'提交后的本地状态刷新失败',
 'workspace-changed':'提交期间工作区发生变化',
};
// Validate against the captured preflight, not mutable UI state. Never render raw output.
export function validateCommitResult(payload,request,files){
 const bad=()=>{throw Error('提交响应身份、结果或文件范围无效。');};
 const result=payload?.commitResult;
 if(payload?.mode!=='live'||payload.workspaceId!==request.workspaceId||payload.selectionVersion!==request.selectionVersion||typeof payload.statusRefreshed!=='boolean'||!result||!['operation','name','keepChangelist'].every(k=>result[k]===request[k])||result.commandCompleted!==true||result.automaticRetryAllowed!==false||!Array.isArray(result.files)||result.files.some(p=>typeof p!=='string')||JSON.stringify([...result.files].sort())!==JSON.stringify(files.map(f=>f.path).sort()))bad();
 if(!['committed','completed-unverified'].includes(result.outcome)||!(result.reportedRevision===null||typeof result.reportedRevision==='string'&&/^[1-9][0-9]{0,18}$/.test(result.reportedRevision)&&BigInt(result.reportedRevision)<=9223372036854775807n)||((result.outcome==='committed')!==(result.reportedRevision!==null)))bad();
 if(!Array.isArray(result.warnings)||result.warnings.some(w=>typeof w!=='string'||!Object.hasOwn(COMMIT_WARNINGS,w))||new Set(result.warnings).size!==result.warnings.length)bad();
 if(!Array.isArray(payload.rows)||payload.rows.some(r=>!r||typeof r.id!=='string'||typeof r.path!=='string'))bad();
 if(payload.statusRefreshed){if(typeof payload.snapshotToken!=='string'||!payload.snapshotToken||payload.snapshotToken===request.snapshotToken||result.warnings.includes('status-refresh-failed')||result.warnings.includes('workspace-changed'))bad();}
 else if(payload.snapshotToken!==null||payload.rows.length!==0||!result.warnings.some(w=>['status-refresh-failed','workspace-changed'].includes(w)))bad();
 return result;
}
export function commitResultSummary(result,{statusRefreshed=false,stale=false}={}){
 const hook=result.warnings.includes('post-commit-hook');
 let message=result.outcome==='committed'
  ?hook?`SVN报告已提交r${result.reportedRevision}，但提交后钩子执行失败，勿重复提交。`:`SVN报告已提交r${result.reportedRevision}，勿重复提交。`
  :'提交命令已完成，但未识别到有效修订号；请检查仓库历史确认结果，勿重复提交。';
 if(result.outcome==='committed')message+=' 修订号来自 SVN 报告，尚未独立核实仓库历史。';
 const warnings=result.warnings.filter(w=>w!=='post-commit-hook'||result.outcome!=='committed').map(w=>COMMIT_WARNINGS[w]);
 if(warnings.length)message+=` 警告：${warnings.join('；')}。`;
 message+=stale?' 窗口或工作范围已改变，未采用返回状态；请显式刷新本地状态并检查仓库历史。':statusRefreshed?' 已重新读取本地状态。':' 本地状态不可用；请显式刷新本地状态并检查仓库历史。';
 return message+' 不会自动重试。';
}


// Context menus act on one explicit focus target and the existing exact selection.
// Opening a menu is not an operation and must never expand a changelist/folder.
export function contextMenuSelection(rows,selected,targetId){
 if(!rows.some(row=>row.id===targetId))throw Error('菜单目标已不在当前状态中。');
 const ids=new Set(rows.map(row=>row.id));
 return selected.has(targetId)?new Set([...selected].filter(id=>ids.has(id))):new Set([targetId]);
}
export function captureContextMenuScope(state){
 return {rows:state.rows,mode:state.mode,workspaceId:state.workspaceId,selectionVersion:state.selectionVersion,
  snapshotToken:state.snapshotToken,focused:state.focused,ids:JSON.stringify([...state.selected].sort())};
}
export function sameContextMenuScope(scope,state){
 return !!scope&&!state.busy&&!state.failed&&['rows','mode','workspaceId','selectionVersion','snapshotToken','focused']
  .every(key=>scope[key]===state[key])&&scope.ids===JSON.stringify([...state.selected].sort());
}
export function contextMenuPosition(x,y,width,height,viewportWidth,viewportHeight){
 const inset=8;
 return {left:Math.max(inset,Math.min(Number.isFinite(x)?x:inset,viewportWidth-width-inset)),
  top:Math.max(inset,Math.min(Number.isFinite(y)?y:inset,viewportHeight-height-inset))};
}

// Repository locks are distinct from local wc_locked administrative flags.
export function lockScopeRows(rows,selected){
 const files=reviewSelection(rows,selected);
 if(!files.length||files.length>100||files.length!==selected.size||new Set(files.map(f=>f.id)).size!==files.length||files.some(f=>!['normal','modified'].includes(f.item)||!['none','normal','modified'].includes(f.props)||['switched','file_external','tree_conflicted','wc_locked','copied','moved_from','moved_to'].some(k=>f[k])||f.kind&&!['file','unknown'].includes(f.kind)))throw Error('请选择 1–100 个普通已版本化文件；目录、冲突和不安全条目不可用。');
 return files;
}
export function parseLockComment(action,value){
 if(!['lock','unlock'].includes(action)||typeof value!=='string'||action==='unlock'&&value!==''||/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\uD800-\uDFFF]/u.test(value)||new TextEncoder().encode(value).length>4096)throw Error('锁定说明须为有效文本，不超过 4096 UTF-8 字节；解锁不能附说明。');
 if(/(?:opaquelocktoken:|urn:uuid:)[^\s"'<>]+/.test(value)||/[a-z][a-z0-9+.-]*:\/\/[^\s/@:]+:[^\s/@]+@/i.test(value))throw Error('锁定说明不能包含密码或锁令牌。');
 return value;
}
export function validateLockPlan(data,request,rows){
 const bad=()=>{throw Error('锁定预检身份、范围或仓库锁信息不匹配，请重新预检。');};
 parseLockComment(request.action,request.comment);
 if(data?.mode!=='live'||!sameShelfScope(data,request)||data.action!==request.action||data.comment!==request.comment||data.contactsRepository!==true||data.expiresIn!==120||typeof data.planToken!=='string'||!data.planToken||data.planToken.length>128||/[\u0000-\u001f\u007f-\u009f]/u.test(data.planToken)||!Array.isArray(request.ids)||new Set(request.ids).size!==request.ids.length||!Array.isArray(data.files))bad();
 const files=lockScopeRows(rows,new Set(request.ids)),expected=new Map(files.map(f=>[f.id,f]));
 if(data.files.length!==files.length)bad();
 for(const f of data.files){const row=expected.get(f?.id);if(!row||f.path!==row.path||f.item!==row.item||f.props!==row.props||f.fromChangelist!==(row.changelist||null)||!['locked','ownedByWorkingCopy','localTokenPresent','staleLocalToken'].every(k=>typeof f[k]==='boolean')||!['workingRevision','serverLastChangedRevision'].every(k=>typeof f[k]==='string'&&/^(0|[1-9][0-9]{0,18})$/.test(f[k])&&BigInt(f[k])<=9223372036854775807n)||request.action==='lock'&&BigInt(f.workingRevision)<BigInt(f.serverLastChangedRevision)||f.staleLocalToken||(request.action==='lock'?(f.locked||f.localTokenPresent||f.ownedByWorkingCopy):(!f.locked||!f.localTokenPresent||!f.ownedByWorkingCopy)))bad();expected.delete(f.id);}
 if(expected.size)bad();return data;
}
const LOCK_WARNINGS={'operation-outcome-unknown':'仓库操作结果未知','status-refresh-failed':'本地状态刷新失败','workspace-changed':'工作区发生变化','command-diagnostics':'SVN 返回额外诊断'};
export function validateLockResult(payload,request,files){
 const bad=()=>{throw Error('锁定操作响应无效。');},r=payload?.lockResult;
 if(payload?.mode!=='live'||payload.workspaceId!==request.workspaceId||payload.selectionVersion!==request.selectionVersion||typeof payload.statusRefreshed!=='boolean'||!r||r.action!==request.action||!['verified','uncertain'].includes(r.outcome)||r.verified!==(r.outcome==='verified')||!Array.isArray(r.files)||JSON.stringify([...r.files].sort())!==JSON.stringify(files.map(f=>f.path).sort())||!Array.isArray(r.warnings)||r.warnings.some(w=>typeof w!=='string'||!Object.hasOwn(LOCK_WARNINGS,w))||new Set(r.warnings).size!==r.warnings.length)bad();
 if(!Array.isArray(payload.rows)||payload.rows.some(row=>!row||typeof row.id!=='string'||typeof row.path!=='string'))bad();
 if(payload.statusRefreshed){if(typeof payload.snapshotToken!=='string'||!payload.snapshotToken||payload.snapshotToken===request.snapshotToken||r.warnings.some(w=>['status-refresh-failed','workspace-changed'].includes(w)))bad();}
 else if(payload.snapshotToken!==null||payload.rows.length)bad();
 if(r.verified&&r.warnings.includes('operation-outcome-unknown')||!r.verified&&!r.warnings.includes('operation-outcome-unknown'))bad();
 return r;
}
export function lockResultSummary(result,{statusRefreshed=false,stale=false}={}){
 let message=result.verified?`已核实${result.action==='lock'?'锁定':'解锁'}完成（${result.files.length} 个文件）。`:'锁定操作结果未知。请先用原版可信 SVN 客户端检查仓库锁和本地令牌，再决定下一步；不要直接重试。';
 if(result.warnings.length)message+=` 警告：${result.warnings.map(w=>LOCK_WARNINGS[w]).join('；')}。`;
 if(!statusRefreshed||stale)message+=' 本地状态不可用，请显式刷新；已核实的操作结果不会因此撤销。';
 return message+' 不会自动重试或强制抢锁。';
}

// Add is local scheduling, never an implicit part of commit or directory traversal.
export function addScopeRows(rows,selected){
 const files=reviewSelection(rows,selected);
 if(!files.length||files.length>100||files.length!==selected.size||new Set(files.map(f=>f.id.toLowerCase())).size!==files.length||files.some(f=>f.item!=='unversioned'||!['none','normal'].includes(f.props)||f.changelist||['switched','file_external','tree_conflicted','wc_locked','copied','moved_from','moved_to'].some(k=>f[k])||f.kind&&!['file','unknown'].includes(f.kind)))throw Error('请选择 1–100 个未版本化普通文件；目录、链接和不安全条目不可用。');
 return files;
}
export function validateAddPlan(data,request,rows){
 const bad=()=>{throw Error('添加预检身份或完整文件范围不匹配，请重新预检。');};
 if(data?.mode!=='live'||!sameShelfScope(data,request)||data.localOnly!==true||data.autoProps!==false||data.expiresIn!==120||typeof data.planToken!=='string'||!data.planToken||data.planToken.length>64||/[\u0000-\u001f\u007f-\u009f]/u.test(data.planToken)||!Array.isArray(request.ids)||new Set(request.ids).size!==request.ids.length||!Array.isArray(data.files))bad();
 const files=addScopeRows(rows,new Set(request.ids)),expected=new Map(files.map(f=>[f.id,f.path]));
 if(data.files.length!==files.length)bad();
 for(const file of data.files){if(!file||!expected.has(file.id)||file.path!==expected.get(file.id))bad();expected.delete(file.id);}
 if(expected.size)bad();return data;
}
const ADD_WARNINGS={'status-refresh-failed':'本地状态刷新失败','workspace-changed':'工作区发生变化'};
export function validateAddResult(payload,request,files){
 const bad=()=>{throw Error('添加操作响应身份、范围或结果无效。');},result=payload?.addResult;
 if(payload?.mode!=='live'||payload.workspaceId!==request.workspaceId||payload.selectionVersion!==request.selectionVersion||typeof payload.statusRefreshed!=='boolean'||!result||result.action!=='add'||result.outcome!=='verified'||result.verified!==true||result.autoProps!==false||!Array.isArray(result.files)||result.files.some(p=>typeof p!=='string')||JSON.stringify([...result.files].sort())!==JSON.stringify(files.map(f=>f.path).sort())||!Array.isArray(result.warnings)||result.warnings.some(w=>typeof w!=='string'||!Object.hasOwn(ADD_WARNINGS,w))||new Set(result.warnings).size!==result.warnings.length)bad();
 if(!Array.isArray(payload.rows)||payload.rows.some(r=>!r||typeof r.id!=='string'||typeof r.path!=='string'))bad();
 if(payload.statusRefreshed){if(typeof payload.snapshotToken!=='string'||!payload.snapshotToken||payload.snapshotToken===request.snapshotToken||result.warnings.length)bad();}
 else if(payload.snapshotToken!==null||payload.rows.length||!result.warnings.length)bad();
 return result;
}
export function addResultSummary(result,{statusRefreshed=false,stale=false}={}){
 let message=`已核实添加 ${result.files.length} 个文件到本地版本控制，尚未提交到仓库。`;
 if(result.warnings.length)message+=` 警告：${result.warnings.map(w=>ADD_WARNINGS[w]).join('；')}。`;
 if(!statusRefreshed||stale)message+=' 当前文件列表不可用，请显式刷新；已完成的添加不会因此撤销。';
 return message+' 未启用 auto-props；提交前请核对文件属性和差异。';
}

export function canReadProperties(row){
 return !!row&&['normal','none','modified','added'].includes(row.item)&&['none','normal','modified'].includes(row.props)&&!['switched','file_external','tree_conflicted','wc_locked','copied','moved_from','moved_to'].some(k=>row[k])&&(!row.kind||['file','unknown'].includes(row.kind));
}
export function validatePropertyResponse(data,scope,expectedBaseAvailable){
 const bad=()=>{throw Error('属性响应身份、范围或显示格式无效；未显示部分结果。');};
 if(typeof expectedBaseAvailable!=='boolean'||data?.baseAvailable!==expectedBaseAvailable||data?.mode!=='live'||!sameReadScope(data,scope)||data.localOnly!==true||data.displayOnly!==true||typeof data.baseAvailable!=='boolean'||data.baseUnavailableReason!==(data.baseAvailable?null:'added-file')||!Array.isArray(data.properties)||data.properties.length>128||new TextEncoder().encode(JSON.stringify(data)).length>524288)bad();
 const names=new Set();let bytes=0;
 const value=v=>{
  if(v===null)return;
  if(!v||!['text','binary'].includes(v.kind)||!Number.isSafeInteger(v.byteLength)||v.byteLength<0||v.byteLength>65536||typeof v.sha256!=='string'||!/^[a-f0-9]{64}$/.test(v.sha256)||typeof v.textTruncated!=='boolean'||typeof v.redacted!=='boolean'||!Number.isSafeInteger(v.escapedControls)||v.escapedControls<0||v.escapedControls>65536)bad();
  bytes+=v.byteLength;
  if(v.kind==='binary'){if(v.text!==null||v.textTruncated||v.redacted||v.escapedControls)bad();}
  else if(typeof v.text!=='string'||new TextEncoder().encode(v.text).length>4096||/[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\uD800-\uDFFF]/u.test(v.text))bad();
 };
 for(const property of data.properties){
  if(!property||typeof property.name!=='string'||!property.name||property.name.length>256||/[\u0000-\u0020\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\uD800-\uDFFF]/u.test(property.name)||names.has(property.name))bad();names.add(property.name);
  value(property.base);value(property.working);
  const expected=!data.baseAvailable?'unbased':property.base===null?'added':property.working===null?'deleted':property.base.sha256===property.working.sha256&&property.base.byteLength===property.working.byteLength?'unchanged':'modified';
  if(property.base===null&&property.working===null||!data.baseAvailable&&(property.base!==null||property.working===null)||property.change!==expected)bad();
 }
 if(bytes>262144)bad();return data;
}
