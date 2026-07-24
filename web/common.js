/* ======================================================== 票归集 · 公共脚本（所有页面共享）
 * 放置跨页面复用的基础能力：API 封装、格式化、后台任务轮询、顶部导航。
 * 各独立页面（invoices.html / companies.html / index.html）先加载本文件，再加载自己的业务脚本。
 */
const API = (p, opt) => fetch(p, opt).then(r => r.json());

function escapeHtml(s){ return (s||'').replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function fmtMoney(v){
  const n = parseFloat(v);
  if (isNaN(n)) return v || '';
  return '¥' + n.toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2});
}

// 日期格式化：把后端 ISO 字符串（含 T00:00:00）或日期对象截成 YYYY-MM-DD
function fmtDate(v){
  if(!v) return '';
  const s = String(v);
  const m = s.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})/);
  if(m){ return m[1] + '-' + m[2].padStart(2,'0') + '-' + m[3].padStart(2,'0'); }
  return s;
}

function stripEmpty(o){ const r={}; for(const k in o) if(o[k]!=='' && o[k]!=null) r[k]=o[k]; return r; }

// 列表「发票文件」单元格：根据 pdf_path 后缀决定展示与行为
function pdfCell(r){
  const p = r.pdf_path || '';
  if (!p) return '<span style="color:#bbb">仅标题</span>';
  const isOfd = p.toLowerCase().endsWith('.ofd');
  const label = isOfd ? '下载OFD' : '查看';
  return `<a href="/api/pdf/${r.id}" target="_blank"${isOfd ? ' download' : ''}>${label}</a>`;
}

// 归属状态徽章：已归类=绿、未归类=红、歧义=橙；悬停显示结构化原因（用户+开发者可见）
function attrBadge(r){
  const s = r.attribution_status || 'unclassified';
  const reason = r.attribution_reason || '';
  const map = { classified:['已归类','on'], unclassified:['未归类','off'], ambiguous:['歧义','warn'] };
  const [label, cls] = map[s] || [s, 'off'];
  const tip = reason ? ' title="'+escapeHtml(reason)+'"' : '';
  return `<span class="badge ${cls}"${tip}>${label}</span>`;
}

/* ---------- 后台任务进度（删除/上传/导出/回填 通用） ---------- */
function setExp(t){ const e=document.getElementById('expMsg'); if(e) e.textContent=t; }
function showJobBar(){ const b=document.getElementById('jobBar'); if(b) b.style.display='block'; updateJobBar({done:0,total:1}); }
function hideJobBar(){ const b=document.getElementById('jobBar'); if(b) b.style.display='none'; }
function updateJobBar(j){
  const f=document.getElementById('jobBarFill'); if(!f) return;
  const t=+j.total||0, d=+j.done||0;
  f.style.width = t>0 ? Math.min(100, Math.round(d/t*100))+'%' : '0%';
}
// 轮询后台任务：onProgress(job) 每次收到状态调用；onDone(job) 任务结束（含失败）调用。最多约 14 分钟。
async function pollJob(jid, onProgress, onDone){
  for(let k=0; k<2400; k++){
    let j;
    try { j = await API('/api/jobs/'+jid); }
    catch(e){ await new Promise(r=>setTimeout(r,400)); continue; }
    if(j.error){ j.running=false; if(onProgress) onProgress(j); if(onDone) onDone(j); return; }
    if(onProgress) onProgress(j);
    if(!j.running){ if(onDone) onDone(j); return; }
    await new Promise(r=>setTimeout(r,350));
  }
}

/* ---------- 顶部导航（跨页面复用） ----------
 * active: 'invoices' | 'companies' | 'console'
 * 发票台账 / 公司管理 为独立页面；邮箱账号、抓取、模板匹配统一收进「操作台」(index.html)。
 * 避免同一组功能在顶部导航和页面内子 tab 重复出现。
 */
function renderNav(active){
  const nav = document.getElementById('topnav');
  if(!nav) return;
  const items = [
    {k:'invoices', label:'发票台账', href:'invoices.html'},
    {k:'companies', label:'公司管理', href:'companies.html'},
    {k:'console',  label:'操作台',   href:'index.html'},
  ];
  nav.innerHTML = items.map(it=>
    `<a class="nav-link${it.k===active?' active':''}" href="${it.href}">${it.label}</a>`
  ).join('');
}
