/* ======================================================== 票归集 · index.js
 * index.html 的业务脚本：仅保留「邮箱账号 / 抓取 / 模板匹配」三个 tab。
 * 发票台账、公司管理已拆分为独立页面（invoices.html / companies.html），
 * 共享工具（API / 格式化 / 后台轮询 / 顶部导航）在 common.js。
 */
let editingIsUpload = false;  // 当前编辑的账号是否为本地上传虚拟账号

function formatSince(s){
  if (!s) return '最近90天';
  var m = s.match(/^(\d{4})-(\d{2})-(\d{2})~(\d{4})-(\d{2})-(\d{2})$/);
  if (m) return m[1]+'-'+m[2]+'-'+m[3]+' ～ '+m[4]+'-'+m[5]+'-'+m[6];
  var d = s.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (d) return d[1]+'-'+d[2]+'-'+d[3]+' 起';
  return s;
}

async function loadSelectors(){
  const accs = await API('/api/accounts');
  renderAccounts(accs);
  const st = await API('/api/fetch/status');
  const autoMinEl = document.getElementById('autoMin');
  if(autoMinEl) autoMinEl.value = Math.round((st.auto_interval||0)/60);
  const autoStateEl = document.getElementById('autoState');
  if(autoStateEl) autoStateEl.innerHTML = (st.auto_interval>0)
    ? `<b>已开启</b>（每 ${Math.round(st.auto_interval/60)} 分钟）` : '未开启';
}

async function loadAccounts(){
  const accs = await API('/api/accounts');
  renderAccounts(accs);
}

function renderAccounts(accs){
  document.getElementById('accCnt').textContent = accs.length;
  document.getElementById('acc_list').innerHTML = accs.length ? accs.map(a=>{
    // provider==='upload' 是后端写入的本地上传虚拟账号（非邮箱），禁用邮箱专属操作（测试/抓取）
    const isUpload = a.provider === 'upload';
    return `
    <div class="acc ${isUpload?'acc-local':''}">
      <span class="badge ${a.enabled?'on':'off'}">${a.enabled?'启用':'停用'}</span>
      ${isUpload ? '<span class="badge local">本地上传</span>' : ''}
      <div class="meta">
        <span class="nm">${escapeHtml(a.name)}</span>
        <span class="em">${isUpload ? '本地文件上传入口（非邮箱）' : escapeHtml(a.email) + ' · ' + escapeHtml(a.imap_host||'—') + ':' + (a.imap_port||'')}</span>
      </div>
      <span class="stat">最近抓取 ${escapeHtml(a.last_fetch||'从未')}</span>
        <span class="stat">${a.fetch_mode==='full'?'全量':'增量'} · ${a.fetch_method==='tencent_api'?'腾讯API':'IMAP'} · ${formatSince(a.default_since)}</span>
      <span class="btns">
        <button class="mini ghost" onclick="openEdit(${a.id})">编辑</button>
        ${isUpload ? '' : '<button class="mini ghost" onclick="testAcc(${a.id})">测试</button>'}
        ${isUpload ? '' : '<button class="mini" onclick="fetchAcc(${a.id})">抓取</button>'}
        <button class="mini ghost" onclick="toggleAcc(${a.id}, ${a.enabled?0:1})">${a.enabled?'停用':'启用'}</button>
        <button class="mini danger" onclick="delAcc(${a.id})">删除</button>
      </span>
    </div>`;
  }).join('') : '<div class="empty">还没有账号，点上方「添加邮箱账号」开始。</div>';
}

/* ======================================================== 账号弹窗（添加 + 编辑 统一） */
function openAdd(){
  editingIsUpload = false;
  // 恢复所有字段可编辑（编辑本地上传账号时曾被禁用，添加邮箱需全部可用）
  ['e_email','e_host','e_port','e_ssl','e_folder','e_pass'].forEach(id=>{
    const el = document.getElementById(id); el.disabled = false; el.style.opacity = '';
  });
  document.getElementById('accModalTitleText').textContent = '添加邮箱账号';
  document.getElementById('e_id').value = '';
  ['e_name','e_email','e_host','e_pass','e_folder'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('e_fetch_mode').value = 'incremental';
  document.getElementById('e_fetch_method').value = 'imap';
  document.getElementById('e_since_from').value = '';
  document.getElementById('e_since_to').value = '';
  document.getElementById('e_keywords').value = '';
  document.getElementById('e_port').value='993';
  document.getElementById('e_ssl').checked = true;
  document.getElementById('e_pass_label_hint').textContent = '';
  document.getElementById('e_pass_status').textContent = '';
  document.getElementById('e_pass').placeholder = '非登录密码，是邮箱网页端生成的授权码';
  document.getElementById('accMsg').textContent = '';
  document.getElementById('accMask').classList.add('show');
}

async function openEdit(id){
  const a = await API(`/api/accounts/${id}`);
  if(!a || a.id===undefined){ alert('读取账号失败'); return; }
  // 本地上传虚拟账号（provider==='upload'）：非邮箱，锁定 email、禁用 IMAP 配置，仅可改名称
  editingIsUpload = a.provider === 'upload';
  if(editingIsUpload){
    document.getElementById('accModalTitleText').textContent = '编辑本地上传账号';
    ['e_host','e_port','e_ssl','e_folder','e_pass'].forEach(id=>{ const el=document.getElementById(id); el.disabled=true; el.style.opacity='0.5'; });
    const em = document.getElementById('e_email'); em.disabled = true; em.style.opacity = '0.7';
  } else {
    document.getElementById('accModalTitleText').textContent = '编辑邮箱账号';
    ['e_email','e_host','e_port','e_ssl','e_folder','e_pass'].forEach(id=>{ const el=document.getElementById(id); el.disabled=false; el.style.opacity=''; });
  }
  document.getElementById('e_id').value = a.id;
  document.getElementById('e_name').value = a.name||'';
  document.getElementById('e_email').value = a.email||'';
  document.getElementById('e_host').value = a.imap_host||'';
  document.getElementById('e_port').value = a.imap_port||993;
  const passInput = document.getElementById('e_pass');
  const passStatus = document.getElementById('e_pass_status');
  const passHint = document.getElementById('e_pass_label_hint');
  passInput.value = '';
  if(a.password_set){
    passStatus.textContent = '✓ 已保存';
    passHint.textContent = '（留空=不修改）';
    passInput.placeholder = '••••••••••••';
    passInput.style.letterSpacing = '2px';
  } else {
    passStatus.textContent = '';
    passHint.textContent = '';
    passInput.placeholder = '请输入 IMAP 授权码';
    passInput.style.letterSpacing = 'normal';
  }
  document.getElementById('e_folder').value = a.folder||'INBOX';
  document.getElementById('e_fetch_mode').value = a.fetch_mode || 'incremental';
  document.getElementById('e_fetch_method').value = a.fetch_method || 'imap';
  // 解析 default_since 回显到日期选择器
  var ds = a.default_since || '';
  var m = ds.match(/^(\d{4}-\d{2}-\d{2})~(\d{4}-\d{2}-\d{2})$/);
  if (m) {
    document.getElementById('e_since_from').value = m[1];
    document.getElementById('e_since_to').value = m[2];
  } else {
    // 单日或 90d → 清空，让用户自己选
    document.getElementById('e_since_from').value = '';
    document.getElementById('e_since_to').value = '';
  }
  document.getElementById('e_keywords').value = a.keywords_override
    ? JSON.parse(a.keywords_override).join(',') : '';
  document.getElementById('e_ssl').checked = !!a.use_ssl;
  document.getElementById('accMsg').textContent = a.password_set
    ? '✅ 授权码已保存。密码框留空 = 保留原值；重新输入 = 覆盖。'
    : '⚠️ 尚未设置授权码，请输入后保存。';
  document.getElementById('accMask').classList.add('show');
  if(editingIsUpload){ document.getElementById('accMsg').textContent = '本地上传账号：无需邮箱配置，仅可修改名称。'; }
  passInput.oninput = function(){
    if(this.value){
      passStatus.textContent = '';
      this.style.letterSpacing = 'normal';
    } else if(a.password_set){
      passStatus.textContent = '✓ 已保存';
      this.style.letterSpacing = '2px';
    } else {
      passStatus.textContent = '';
      this.style.letterSpacing = 'normal';
    }
  };
}
function closeAccModal(){
  document.getElementById('accMask').classList.remove('show');
  document.getElementById('e_pass_status').textContent = '';
}

async function saveAcc(){
  const id = document.getElementById('e_id').value;
  const body = {
    name: document.getElementById('e_name').value.trim(),
    email: document.getElementById('e_email').value.trim(),
    imap_host: document.getElementById('e_host').value.trim(),
    imap_port: parseInt(document.getElementById('e_port').value)||993,
    password: document.getElementById('e_pass').value,
    folder: document.getElementById('e_folder').value.trim()||'INBOX',
    fetch_mode: document.getElementById('e_fetch_mode').value,
    fetch_method: document.getElementById('e_fetch_method').value||'imap',
    default_since: (() => {
      const f = document.getElementById('e_since_from').value;
      const t = document.getElementById('e_since_to').value;
      if (!f && !t) return '90d';
      if (f && t) return `${f}~${t}`;       // 闭区间 YYYY-MM-DD~YYYY-MM-DD
      if (f) return f;                       // 仅开始日期
      // 仅结束日期 → 从今天往前推
      const d = new Date();
      return d.toISOString().slice(0, 10);
    })(),
    keywords_override: document.getElementById('e_keywords').value.trim()
      ? JSON.stringify(document.getElementById('e_keywords').value.split(',').map(s=>s.trim()).filter(Boolean))
      : null,
    use_ssl: document.getElementById('e_ssl').checked,
    enabled: true,
  };
  // 本地上传账号无需 IMAP 主机；其余账号邮箱与主机必填
  if(!editingIsUpload && (!body.email || !body.imap_host)){ alert('邮箱和 IMAP 主机必填'); return; }
  if(editingIsUpload){ body.provider = 'upload'; }  // 防止 upsert 把 provider 刷成 NULL，导致失去本地上传标识
  const msg = document.getElementById('accMsg');
  msg.textContent = '保存中…';
  try{
    if(id){
      const r = await API(`/api/accounts/${id}/update`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
      msg.textContent = r.msg || '已保存';
      if(r.ok){ closeAccModal(); loadAccounts(); loadSelectors(); }
    } else {
      const r = await API('/api/accounts', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
      msg.textContent = r.msg || '已添加';
      if(r.ok){ closeAccModal(); loadAccounts(); loadSelectors(); }
    }
  }catch(e){ msg.textContent = '保存失败：'+e; }
}

async function toggleAcc(id, en){
  await API(`/api/accounts/${id}/toggle`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:en})});
  loadAccounts(); loadSelectors();
}
async function delAcc(id){
  if(!confirm('确认删除该账号？相关发票记录会保留。')) return;
  // 账号删除含级联目录清理，可能较久，走后台任务并轮询
  const r = await API(`/api/accounts/${id}/delete`, {method:'POST'});
  if(!(r.ok && r.job_id)){ alert('删除失败'); return; }
  await pollJob(r.job_id,
    j => { const m=document.getElementById('accMsg'); if(m) m.textContent = j.error ? ('删除失败：'+j.error) : (j.msg||'删除中…'); },
    j => {
      const m=document.getElementById('accMsg'); if(m) m.textContent = j.error ? ('删除失败：'+j.error) : '账号已删除';
      loadAccounts(); loadSelectors();
    });
}
async function testAcc(id){
  const msg = document.getElementById('accMsg');
  msg.textContent = '测试连接中…';
  const r = await API(`/api/accounts/${id}/test`, {method:'POST'});
  msg.textContent = r.msg || (r.ok?'连接成功':'连接失败');
  alert(r.msg || (r.ok?'连接成功':'连接失败'));
}
async function fetchAcc(id){
  await API(`/api/accounts/${id}/fetch`, {method:'POST'});
  // 切到抓取 Tab
  document.querySelector('.tab-btn[data-tab="fetch"]').click();
  startPoll();
}

/* ======================================================== 抓取 */
async function fetchAll(){
  const since = document.getElementById('fetchSinceInput').value.trim() || null;
  await API('/api/fetch', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify(since ? {since} : {})});
  document.querySelector('.tab-btn[data-tab="fetch"]').click();
}
async function setAuto(){
  const min = parseInt(document.getElementById('autoMin').value)||0;
  const r = await API('/api/fetch/auto', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({interval: min*60})});
  document.getElementById('autoState').innerHTML = (r.auto_interval>0) ? `<b>已开启</b>（每 ${min} 分钟）` : '未开启';
}
let pollTimer=null;
let pendingReparse = false;  // 标记：当前轮询的是「重新解析」任务（完成后自动切回台账）
// 跟踪抓取是否真正运行过。修复 bug：原逻辑只要 running=False 就清缓存 + load()，
// 会在两种场景误清空发票台账：
//   1) 用户只是切到「抓取」Tab（根本没在抓取）→ running=False → 误判完成 → 清缓存；
//   2) 点了「立即抓取」后后台线程还没来得及把 FETCH_RUNNING 设为 True（调度延迟），
//      首次轮询 running=False → 误判完成 → 清缓存 + load()，此时抓取刚开始写库，
//      /api/invoices 一旦失败，缓存已空，load() 的 catch 回退失效 → 台账归零。
let _fetchWasRunning = false;
function startPoll(){
  if(pollTimer) clearInterval(pollTimer);
  _fetchWasRunning = false;
  let idleTicks = 0;  // 空闲轮询计数：抓取一直没启动时，避免无限轮询空耗
  pollTimer = setInterval(async ()=>{
    const st = await API('/api/fetch/status');
    document.getElementById('fetchStatePill').innerHTML = st.running
      ? '<b style="color:var(--warn)">抓取运行中…</b>'
      : '<span style="color:var(--ok)">空闲</span>';
    const logEl = document.getElementById('fetchLog');
    if(st.log && st.log.length){
      logEl.textContent = st.log.join('\n');
      logEl.scrollTop = 1e9;
    }
    if(st.running){
      // 抓取正在运行：标记，等它真正完成时再刷新
      _fetchWasRunning = true;
      idleTicks = 0;
    } else if(_fetchWasRunning){
      // 抓取从「运行中」→「完成」：此时才刷新列表。
      // 关键：不清缓存！load() 成功会自动更新缓存（_cachedRows=rows），
      // 失败则回退旧缓存（renderRows(_cachedRows,...)），绝不归零。
      _fetchWasRunning = false;
      clearInterval(pollTimer); pollTimer=null;
      loadAccounts(); loadSelectors();
      if(pendingReparse){
        pendingReparse = false;
        document.getElementById('expMsg').textContent = '重新解析完成，已刷新列表';
      }

    } else {
      // 抓取从未运行（用户只是切到抓取 Tab，或后台线程还没起来）：
      // 不清缓存、不 load()，避免误清空台账。给后台线程几轮启动时间后停止轮询。
      idleTicks++;
      if(idleTicks > 5){ clearInterval(pollTimer); pollTimer=null; }
    }
  }, 1200);
}

function gotoFetchTab(){ document.querySelector('.tab-btn[data-tab="fetch"]').click(); }

/* ======================================================== 模板匹配 Tab */
// 状态：file_id（后端返回）、template_info（上传后概要）、result（匹配结果）、matchedSelected（勾选的回填项 row_idx -> {invoice_no, invoice_id}）
const matchState = {
  fileId: null,
  templateInfo: null,
  result: null,
  matchedSelected: new Map(),
};

function matchSetMsg(id, text, isError){
  const el = document.getElementById(id); if(!el) return;
  el.textContent = text || '';
  el.style.color = isError ? 'var(--bad)' : '';
}

// 下载标准模板（blob → a 标签触发下载）
async function matchDownloadTemplate(){
  matchSetMsg('matchUploadMsg', '下载中…');
  try{
    const r = await fetch('/api/match/template/download');
    if(!r.ok){ matchSetMsg('matchUploadMsg', '下载失败：'+r.status, true); return; }
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    const cd = r.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename\*?=(?:UTF-8'')?["']?([^"';]+)/i);
    a.download = m ? decodeURIComponent(m[1]) : '标准模板.xlsx';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(()=>URL.revokeObjectURL(a.href), 1000);
    matchSetMsg('matchUploadMsg', '已下载模板');
  }catch(e){ matchSetMsg('matchUploadMsg', '下载失败：'+e, true); }
}

// 上传 xlsx：FileReader 读取为 DataURL，去掉前缀得到 base64
function matchUploadFile(input){
  const f = (input.files||[])[0];
  if(!f) return;
  if(!f.name.toLowerCase().endsWith('.xlsx')){
    matchSetMsg('matchUploadMsg', '请选择 .xlsx 文件', true);
    input.value = '';
    return;
  }
  input.value = '';
  matchSetMsg('matchUploadMsg', '上传中…');
  const reader = new FileReader();
  reader.onload = async () => {
    let b64 = reader.result || '';
    const idx = b64.indexOf(',');
    if(idx >= 0) b64 = b64.slice(idx+1);  // 去掉 "data:...;base64," 前缀
    try{
      const r = await API('/api/match/upload', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ b64, name: f.name })
      });
      if(!r.ok){ matchSetMsg('matchUploadMsg', '上传失败：'+(r.error||''), true); return; }
      matchState.fileId = r.file_id;
      matchState.templateInfo = r.template_info;
      matchState.result = null;
      matchState.matchedSelected = new Map();
      matchSetMsg('matchUploadMsg', '上传成功：'+f.name);
      matchRenderSummary(r.template_info);
      matchShowConfig(r.template_info);
    }catch(e){ matchSetMsg('matchUploadMsg', '上传失败：'+e, true); }
  };
  reader.onerror = () => matchSetMsg('matchUploadMsg', '读取文件失败', true);
  reader.readAsDataURL(f);
}

// 渲染模板概要：总行数 / 已填发票号 / 待匹配 / 列映射
function matchRenderSummary(info){
  const total = info.total_rows || 0;
  const existing = info.existing_invoice_no_count || 0;
  const pending = Math.max(0, total - existing);
  document.getElementById('matchSummaryCards').innerHTML = `
    <div class="card"><div class="k">总行数</div><div class="v">${total}</div></div>
    <div class="card"><div class="k">已填发票号</div><div class="v">${existing}</div></div>
    <div class="card"><div class="k">待匹配行数</div><div class="v">${pending}</div></div>`;
  const cols = info.columns || {};
  const headers = info.headers || [];
  const colName = idx => (idx==null || idx<0 || idx>=headers.length) ? '—' : headers[idx];
  const map = [
    ['日期列', colName(cols.date)],
    ['商社列', colName(cols.merchant)],
    ['买方列', colName(cols.buyer)],
    ['金额列', colName(cols.amount)],
    ['回填列', colName(cols.invoice_no)],
  ];
  document.getElementById('matchColumnMap').innerHTML = `
    <div style="font-size:12px;color:var(--muted);margin-bottom:4px">识别到的列映射：</div>
    <div class="row" style="gap:6px">${map.map(([k,v])=>`<span class="tag">${k}: ${escapeHtml(v)}</span>`).join('')}</div>`;
  document.getElementById('matchSummary').style.display = 'block';
}

// 显示配置区，根据 ambiguous 决定是否显示列映射下拉
function matchShowConfig(info){
  document.getElementById('matchConfigPanel').style.display = 'block';
  document.getElementById('matchPreviewPanel').style.display = 'none';
  document.getElementById('matchUnmatchedPanel').style.display = 'none';
  matchSetMsg('matchRunMsg', '');
  matchSetMsg('matchColMsg', '');
  const picker = document.getElementById('matchColumnPicker');
  if(info && info.ambiguous){
    picker.style.display = 'block';
    const headers = info.headers || [];
    const opts = headers.map((h,i)=>`<option value="${i}">${i}: ${escapeHtml(h)}</option>`).join('');
    const cols = info.columns || {};
    ['amount','invoice_no','date','buyer','merchant'].forEach(k=>{
      const sel = document.getElementById('mp_'+k);
      sel.innerHTML = opts;
      if(cols[k]!=null) sel.value = cols[k];
    });
  } else {
    picker.style.display = 'none';
  }
}

// 确认列映射（ambiguous 场景）
async function matchConfirmColumns(){
  if(!matchState.fileId) return;
  const columns_map = {
    amount: parseInt(document.getElementById('mp_amount').value),
    invoice_no: parseInt(document.getElementById('mp_invoice_no').value),
    date: parseInt(document.getElementById('mp_date').value),
    buyer: parseInt(document.getElementById('mp_buyer').value),
    merchant: parseInt(document.getElementById('mp_merchant').value),
  };
  matchSetMsg('matchColMsg', '保存中…');
  try{
    const r = await API('/api/match/columns', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ file_id: matchState.fileId, columns_map })
    });
    if(!r.ok){ matchSetMsg('matchColMsg', '保存失败', true); return; }
    matchSetMsg('matchColMsg', '列映射已确认');
  }catch(e){ matchSetMsg('matchColMsg', '保存失败：'+e, true); }
}

// 启动匹配：POST /api/match/run → 轮询 job → 加载结果
async function matchStartRun(){
  if(!matchState.fileId){ alert('请先上传文件'); return; }
  const date_range_days = parseInt(document.getElementById('matchDateRange').value)||30;
  const overwrite = document.getElementById('matchOverwrite').checked;
  const btn = document.getElementById('matchRunBtn');
  btn.disabled = true;
  matchSetMsg('matchRunMsg', '启动中…');
  document.getElementById('matchJobBar').style.display = 'block';
  document.getElementById('matchJobBarFill').style.width = '0%';
  try{
    const r = await API('/api/match/run', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ file_id: matchState.fileId, date_range_days, overwrite })
    });
    if(!r.ok || !r.job_id){
      btn.disabled = false;
      document.getElementById('matchJobBar').style.display = 'none';
      matchSetMsg('matchRunMsg', '启动失败', true);
      return;
    }
    matchSetMsg('matchRunMsg', '匹配进行中…');
    await pollJob(r.job_id,
      j => {
        const t = +j.total||0, d = +j.done||0;
        const pct = t>0 ? Math.min(100, Math.round(d/t*100)) : 0;
        document.getElementById('matchJobBarFill').style.width = pct+'%';
        matchSetMsg('matchRunMsg', j.error ? ('失败：'+j.error) : (j.msg || `进行中 ${d}/${t}`));
      },
      async j => {
        btn.disabled = false;
        if(j.error){
          document.getElementById('matchJobBar').style.display = 'none';
          matchSetMsg('matchRunMsg', '失败：'+j.error, true);
          return;
        }
        document.getElementById('matchJobBarFill').style.width = '100%';
        matchSetMsg('matchRunMsg', '匹配完成，加载结果…');
        await matchLoadResult();
      });
  }catch(e){
    btn.disabled = false;
    document.getElementById('matchJobBar').style.display = 'none';
    matchSetMsg('matchRunMsg', '错误：'+e, true);
  }
}

// 加载匹配结果
async function matchLoadResult(){
  if(!matchState.fileId) return;
  try{
    const r = await API('/api/match/result/'+matchState.fileId);
    matchState.result = r;
    matchRenderResult(r);
  }catch(e){
    matchSetMsg('matchRunMsg', '加载结果失败：'+e, true);
  }
}

// 渲染结果表格 + 统计栏 + 未匹配清单
function matchRenderResult(r){
  document.getElementById('matchConfigPanel').style.display = 'none';
  document.getElementById('matchPreviewPanel').style.display = 'block';
  const stats = r.stats || {};
  document.getElementById('matchStats').innerHTML = `
    <div class="card"><div class="k">已匹配</div><div class="v" style="color:var(--ok)">${stats.matched||0}</div></div>
    <div class="card"><div class="k">凑票组</div><div class="v" style="color:var(--warn)">${stats.many_to_one_groups||0}</div></div>
    <div class="card"><div class="k">未匹配</div><div class="v" style="color:var(--bad)">${stats.unmatched||0}</div></div>
    <div class="card"><div class="k">跳过</div><div class="v" style="color:var(--muted)">${stats.skipped||0}</div></div>`;

  const matched = r.matched || [];
  const unmatched = r.unmatched || [];
  const skipped = r.skipped || [];

  // 空发票库提示：全部未匹配，或所有未匹配原因都是「无候选」
  const emptyBanner = document.getElementById('matchEmptyBanner');
  const showEmptyHint = (stats.matched === 0 && stats.unmatched > 0)
    || (unmatched.length > 0 && unmatched.every(u => (u.reason || '') === '无候选'));
  if (showEmptyHint) {
    emptyBanner.innerHTML = '<div style="background:#fdf6e3;border:1px solid #f0d882;color:#8a6d3b;padding:10px 15px;border-radius:6px;margin:10px 0;">未匹配到任何发票，请确认发票库中是否有对应买方的发票。可前往「发票管理」标签页抓取发票后再试。</div>';
  } else {
    emptyBanner.innerHTML = '';
  }

  // 默认勾选所有 matched
  matchState.matchedSelected = new Map();
  for(const m of matched){
    matchState.matchedSelected.set(m.row_idx, { invoice_no: m.invoice_no, invoice_id: m.invoice_id });
  }

  const matchedByRow = new Map();
  for(const m of matched) matchedByRow.set(m.row_idx, m);
  const unmatchedByRow = new Map();
  for(const u of unmatched) unmatchedByRow.set(u.row_idx, u);
  const skippedByRow = new Map();
  for(const s of skipped) skippedByRow.set(s.row_idx, s);

  // 以模板行为主轴，附加未在模板行的结果
  const templateRows = (matchState.templateInfo && matchState.templateInfo.rows) || [];
  const allRows = [];
  const seenRow = new Set();
  for(const tr of templateRows){
    seenRow.add(tr.row_idx);
    allRows.push({
      row_idx: tr.row_idx,
      date: tr.date, merchant: tr.merchant, buyer: tr.buyer, amount: tr.amount,
      matched: matchedByRow.get(tr.row_idx),
      unmatched: unmatchedByRow.get(tr.row_idx),
      skipped: skippedByRow.get(tr.row_idx),
    });
  }
  for(const m of matched){
    if(!seenRow.has(m.row_idx)){
      seenRow.add(m.row_idx);
      allRows.push({ row_idx: m.row_idx, date:'', merchant:'', buyer:'', amount: m.amount, matched: m });
    }
  }
  for(const u of unmatched){
    if(!seenRow.has(u.row_idx)){
      seenRow.add(u.row_idx);
      allRows.push({ row_idx: u.row_idx, date:'', merchant:'', buyer:'', amount: u.amount, unmatched: u });
    }
  }
  for(const s of skipped){
    if(!seenRow.has(s.row_idx)){
      seenRow.add(s.row_idx);
      allRows.push({ row_idx: s.row_idx, date:'', merchant:'', buyer:'', amount:'', skipped: s });
    }
  }
  allRows.sort((a,b)=>a.row_idx-b.row_idx);

  const tbody = document.getElementById('matchTbody');
  tbody.innerHTML = allRows.map(row=>{
    let bg = '', status = '', invoiceNo = '', matchType = '', checkable = false, checked = false;
    if(row.matched){
      checkable = true;
      checked = matchState.matchedSelected.has(row.row_idx);
      const mt = row.matched.match_type;
      if(mt === 'one_to_one'){
        bg = 'background:#e6f6ec'; status = '已匹配'; matchType = '一对一';
      } else if(mt === 'many_to_one'){
        bg = 'background:#fdf6e3'; status = '已匹配(凑票)'; matchType = '多对一';
      } else {
        bg = 'background:#e6f6ec'; status = '已匹配'; matchType = mt || '';
      }
      invoiceNo = row.matched.invoice_no || '';
    } else if(row.unmatched){
      bg = 'background:#fdeaea'; status = '未匹配'; matchType = row.unmatched.reason || '';
    } else if(row.skipped){
      bg = 'background:#f1f2f5'; status = '跳过'; matchType = row.skipped.reason || '';
    } else {
      bg = 'background:#fdeaea'; status = '未匹配'; matchType = '无候选';
    }
    const chk = checkable
      ? `<input type="checkbox" class="chk mchk" data-row="${row.row_idx}" ${checked?'checked':''} onchange="matchToggleRow(this, ${row.row_idx})">`
      : '<span style="color:#bbb">—</span>';
    return `<tr style="${bg}">
      <td class="checkbox-cell">${chk}</td>
      <td>${row.row_idx}</td>
      <td>${escapeHtml(fmtDate(row.date))}</td>
      <td>${escapeHtml(row.merchant||'')}</td>
      <td>${escapeHtml(row.buyer||'')}</td>
      <td class="num">${fmtMoney(row.amount)}</td>
      <td>${status}</td>
      <td>${escapeHtml(invoiceNo)}</td>
      <td>${escapeHtml(matchType)}</td>
    </tr>`;
  }).join('');

  // 未匹配清单：保留原模板记录的日期/商社/买方信息，让人能看出是哪条记录没匹配出来
  if(unmatched.length){
    document.getElementById('matchUnmatchedPanel').style.display = 'block';
    document.getElementById('matchUnmatchedCnt').textContent = `共 ${unmatched.length} 条`;
    // 从 templateInfo.rows 按 row_idx 查原记录信息（后端 unmatched 只回 row_idx/amount/reason）
    const tplRows = (matchState.templateInfo && matchState.templateInfo.rows) || [];
    const tplByRow = new Map();
    for(const tr of tplRows) tplByRow.set(tr.row_idx, tr);
    document.getElementById('matchUnmatchedTbody').innerHTML = unmatched.map(u=>{
      const tr = tplByRow.get(u.row_idx) || {};
      return `<tr>
        <td>${u.row_idx}</td>
        <td>${escapeHtml(fmtDate(tr.date))}</td>
        <td>${escapeHtml(tr.merchant||'')}</td>
        <td>${escapeHtml(tr.buyer||'')}</td>
        <td class="num">${fmtMoney(u.amount)}</td>
        <td>${escapeHtml(u.reason||'')}</td>
      </tr>`;
    }).join('');
  } else {
    document.getElementById('matchUnmatchedPanel').style.display = 'none';
  }
  document.getElementById('matchJobBar').style.display = 'none';
  matchSetMsg('matchRunMsg', '');
}

// 勾选/取消某条匹配
function matchToggleRow(box, rowIdx){
  if(box.checked){
    const m = matchState.result.matched.find(x=>x.row_idx===rowIdx);
    if(m) matchState.matchedSelected.set(rowIdx, { invoice_no: m.invoice_no, invoice_id: m.invoice_id });
  } else {
    matchState.matchedSelected.delete(rowIdx);
  }
}

// 切换"同时打包发票 PDF"勾选时显示/隐藏文件夹组织下拉
function matchTogglePdfOption(){
  const checked = document.getElementById('matchAlsoPdf').checked;
  document.getElementById('matchPdfGroupBy').style.display = checked ? '' : 'none';
  document.getElementById('matchPdfDateRange').style.display = checked ? 'inline-flex' : 'none';
}

// 从 Content-Disposition 解析下载文件名
function _parseDownloadName(resp, fallback){
  const cd = resp.headers.get('Content-Disposition') || '';
  const mStar = cd.match(/filename\*=UTF-8''([^";]+)/i);
  const mPlain = cd.match(/filename=["']?([^"';]+)/i);
  if(mStar){
    try{ return decodeURIComponent(mStar[1]); }
    catch(e){ return mStar[1]; }
  }
  if(mPlain){ return mPlain[1]; }
  return fallback;
}

// 触发浏览器下载一个已完成的 job 产物
async function _downloadJobResult(jobId, fallbackName){
  const resp = await fetch('/api/jobs/'+jobId+'/download');
  if(!resp.ok){ throw new Error('下载失败'); }
  const blob = await resp.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = _parseDownloadName(resp, fallbackName);
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(()=>URL.revokeObjectURL(a.href), 1000);
}

// 下载回填文件（可选同时打包发票 PDF + 清单到一个 zip）
async function matchDownloadExport(){
  if(!matchState.fileId){ alert('请先完成匹配'); return; }
  const confirmed = [];
  for(const [row_idx, info] of matchState.matchedSelected.entries()){
    confirmed.push({ row_idx, invoice_no: info.invoice_no, invoice_id: info.invoice_id });
  }
  if(!confirmed.length){ alert('请至少勾选一条匹配项用于回填'); return; }

  const alsoPdf = document.getElementById('matchAlsoPdf').checked;
  const group_by = document.getElementById('matchPdfGroupBy').value || 'none';
  const invoice_date_from = document.getElementById('matchInvoiceDateFrom').value || '';
  const invoice_date_to = document.getElementById('matchInvoiceDateTo').value || '';

  try{
    if(alsoPdf){
      // 打包成一个 zip：回填 xlsx + 发票 PDF + 清单（复用台账导出逻辑）
      matchSetMsg('matchExportMsg', '打包中（回填文件+发票PDF+清单）…');
      const r = await API('/api/match/export_pdfs', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ file_id: matchState.fileId, confirmed_matched: confirmed, group_by, invoice_date_from, invoice_date_to })
      });
      if(!r.ok || !r.job_id){ matchSetMsg('matchExportMsg', '启动失败：'+(r.msg||''), true); return; }
      await pollJob(r.job_id,
        j => matchSetMsg('matchExportMsg', j.error ? ('失败：'+j.error) : (j.msg||'打包中…')),
        async j => {
          if(j.error){ matchSetMsg('matchExportMsg', '失败：'+j.error, true); return; }
          matchSetMsg('matchExportMsg', '下载中…');
          await _downloadJobResult(r.job_id, '回填发票打包.zip');
        });
    } else {
      // 仅下载回填 xlsx
      matchSetMsg('matchExportMsg', '生成回填文件中…');
      const r = await API('/api/match/export', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ file_id: matchState.fileId, confirmed_matched: confirmed })
      });
      if(!r.ok || !r.job_id){ matchSetMsg('matchExportMsg', '启动失败', true); return; }
      await pollJob(r.job_id,
        j => matchSetMsg('matchExportMsg', j.error ? ('失败：'+j.error) : (j.msg||'生成回填文件中…')),
        async j => {
          if(j.error){ matchSetMsg('matchExportMsg', '失败：'+j.error, true); return; }
          matchSetMsg('matchExportMsg', '下载中…');
          await _downloadJobResult(r.job_id, '回填文件.xlsx');
        });
    }
    matchSetMsg('matchExportMsg', '已下载');
  }catch(e){ matchSetMsg('matchExportMsg', '错误：'+e, true); }
}

// 重新匹配：回到配置区
function matchRematch(){
  document.getElementById('matchPreviewPanel').style.display = 'none';
  document.getElementById('matchUnmatchedPanel').style.display = 'none';
  document.getElementById('matchConfigPanel').style.display = 'block';
  document.getElementById('matchJobBar').style.display = 'none';
  matchSetMsg('matchRunMsg', '');
  matchSetMsg('matchExportMsg', '');
}

// 导出未匹配清单：前端生成 CSV（带 UTF-8 BOM，Excel 可直接打开中文）
// 保留原模板记录的日期/商社/买方信息，让人能看出是哪条记录没匹配出来
function matchExportUnmatched(){
  const r = matchState.result;
  if(!r || !r.unmatched || !r.unmatched.length){ alert('没有未匹配行'); return; }
  const tplRows = (matchState.templateInfo && matchState.templateInfo.rows) || [];
  const tplByRow = new Map();
  for(const tr of tplRows) tplByRow.set(tr.row_idx, tr);
  const rows = [['行号','日期','商社','买方','支出金额','原因']];
  for(const u of r.unmatched){
    const tr = tplByRow.get(u.row_idx) || {};
    rows.push([u.row_idx, fmtDate(tr.date), tr.merchant||'', tr.buyer||'', u.amount||'', u.reason||'']);
  }
  const csv = rows.map(row => row.map(cell => {
    const s = String(cell);
    return /[",\n]/.test(s) ? '"'+s.replace(/"/g,'""')+'"' : s;
  }).join(',')).join('\n');
  // BOM 让 Excel 正确识别 UTF-8 中文
  const blob = new Blob(['\ufeff'+csv], {type:'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = '未匹配清单.csv';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(()=>URL.revokeObjectURL(a.href), 1000);
}

/* ======================================================== 公司管理 Tab */

/* ======================================================== 子 Tab 切换（账号 / 抓取 / 匹配） */
function switchTab(tab){
  document.querySelectorAll('.subtabs .tab-btn').forEach(b=>b.classList.toggle('active', b.dataset.tab===tab));
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.toggle('active', p.id==='tab-'+tab));
  if(tab==='accounts') loadAccounts();
  if(tab==='fetch') startPoll();
  // match 为惰性加载：用户上传/启动后才渲染结果，保持原行为
}
document.querySelectorAll('.subtabs .tab-btn').forEach(btn=>{
  btn.addEventListener('click', ()=> switchTab(btn.dataset.tab));
});

/* ======================================================== 启动 */
loadSelectors();
const _initTab = new URLSearchParams(location.search).get('tab') || 'accounts';
renderNav('console');   // 顶部导航统一显示「操作台」高亮
switchTab(_initTab);
