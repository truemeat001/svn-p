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
