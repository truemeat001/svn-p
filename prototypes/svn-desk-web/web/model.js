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
