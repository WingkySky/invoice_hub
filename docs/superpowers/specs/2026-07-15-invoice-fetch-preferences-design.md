# 发票抓取用户偏好与增量水位线设计

- **日期**：2026-07-15
- **状态**：待实现
- **方案**：方案 B（完整账号偏好）

## 1. 背景与现状

当前 invoice_hub 的发票抓取逻辑存在四个痛点：

1. **硬编码日期不灵活**：Web 抓取入口把 `since` 写死为 `2026-07-09` / `2026-07-01`
   - [web/app.py:96](../../../web/app.py#L96)（自动抓取 `"2026-07-01"`）
   - [web/app.py:160](../../../web/app.py#L160)（单账号抓取 `"2026-07-09"`）
   - [web/app.py:309](../../../web/app.py#L309)（单账号抓取 `"2026-07-09"`）
   - [web/app.py:315](../../../web/app.py#L315)（立即抓取全部默认 `"2026-07-09"`）
   - 想抓其他时段只能走 CLI。

2. **全量扫描低效**：每次都重新拉取 `SINCE` 范围内所有邮件，再逐个用 `email_exists()` 去重（[engine.py:451](../../../engine.py#L451)）。范围大时重复扫描成本高。

3. **账号无个性化偏好**：所有邮箱共用同一 `since_date`、同一 `INBOX` 文件夹、同一全局 `invoice_keywords`，无法按账号定制。

4. **抓取后状态无管理**：删发票记录后，`emails` 表的 UID 仍在，`email_exists()` 返回 True，下次抓取直接跳过——**删了就抓不回来**。且 [db.delete_invoices()](../../../db.py#L328) 只删 DB 记录，磁盘 PDF 文件保留成孤儿文件。

### 隐藏的语义问题
当前 `fetch_account` 用 `M.search()` 返回**序号**，却当作 `uid` 存进 DB 并用 `M.fetch(uid)` 拉取（[engine.py:437-453](../../../engine.py#L437-L453)）。IMAP 序号不稳定（邮箱删邮件后会重排），若引入水位线就会有 bug。本设计统一改用 UID 语义。

## 2. 目标

- 引入**本地水位线**（`last_uid`）实现增量抓取，不动邮箱服务器状态。
- 支持**账号级偏好**：时间范围、抓取模式、邮件文件夹、发票关键词覆盖。
- 删除发票时**彻底清理本地三件套**（invoices 记录 + 磁盘 PDF + emails 记录），并回退水位线，使其可重新抓取。
- 消除所有硬编码日期。
- **绝不触碰邮箱服务器上的邮件**（不删邮件、不改已读状态）。

## 3. 数据模型与迁移

### 3.1 `accounts` 表新增 4 列（`folder` 列已存在，复用）

| 列名 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `last_uid` | INTEGER | NULL | 水位线：该账号已处理到的最大 IMAP UID。NULL 表示从未抓过 |
| `fetch_mode` | TEXT | `'incremental'` | `'incremental'`：只拉 `UID > last_uid`；`'full'`：按 `default_since` 全量重扫 |
| `default_since` | TEXT | `'90d'` | 默认时间范围。`'90d'` = 最近 90 天；`'2026-07-01'` = 绝对日期 |
| `keywords_override` | TEXT | NULL | JSON 数组（如 `'["发票","行程单"]'`），覆盖全局 `invoice_keywords`；NULL = 用全局 |

### 3.2 其他表不动
- `emails` 表：已有 `UNIQUE(account_id, uid)`，继续承担"邮件级去重"。
- `invoices` 表：已有 `UNIQUE(invoice_no)`，继续承担"发票级去重"。

### 3.3 迁移策略（幂等）
在 `db.init()` 里检测列是否存在，缺则 `ALTER TABLE accounts ADD COLUMN ...`。SQLite `ADD COLUMN` 默认 NULL。代码层读取时兜底：
- `acc.get("fetch_mode") or "incremental"`
- `acc.get("default_since") or "90d"`

老账号 `last_uid=NULL` → 首次抓取走全量（`incremental` 模式下 `last_uid` 为 0/NULL 时不过滤，拉整个 SINCE 范围）。

## 4. 抓取流程改造（engine.py）

### 4.1 统一 UID 语义
- `M.uid("search", ...)` 返回 uid 列表
- `M.uid("fetch", uid, "(RFC822)")` 按 uid 拉取
- uid 单调递增且稳定，水位线才有意义

### 4.2 时间范围表达式解析
```python
def parse_since_expr(expr):
    """'90d' -> 今天-90天的 IMAP 日期; '2026-07-01' -> 该日期; None/'' -> '01-Jan-2000' 兜底"""
```
返回 IMAP SINCE 需要的 `DD-Mon-YYYY` 格式（复用现有 `imap_date()`）。

### 4.3 `fetch_account` 改造
```python
def fetch_account(acc, rules, session, since_override=None):
    mode = acc.get("fetch_mode") or "incremental"
    since_expr = since_override or acc.get("default_since") or "90d"
    since = parse_since_expr(since_expr)
    last_uid = acc.get("last_uid") or 0

    M = connect(acc)
    typ, data = M.uid("search", None, "SINCE", since)
    uids = [int(u) for u in data[0].split()]

    if mode == "incremental" and last_uid:
        uids = [u for u in uids if u > last_uid]   # 水位线过滤

    uids = uids[-200:]                              # 保留上限
    new_max = last_uid
    for uid in reversed(uids):
        if db.email_exists(acc["id"], str(uid)):
            continue
        # ... uid fetch + 解析入库（逻辑不变）...
        new_max = max(new_max, uid)

    if new_max > last_uid:
        db.update_last_uid(acc["id"], new_max)      # 推进水位线
```

### 4.4 关键词覆盖
```python
keywords = (json.loads(acc["keywords_override"])
            if acc.get("keywords_override") else rules.get("invoice_keywords", []))
```

### 4.5 签名调整
- `fetch_all(since_override=None, acc_id=None)`：去掉硬编码 since，改从账号配置读；`since_override` 为可选临时覆盖。
- `fetch_one(acc_id, since_override=None)`：同上。
- Web 调用处删除所有硬编码 `"2026-07-09"` / `"2026-07-01"`。

### 4.6 双保险去重保留
即使 `incremental` 模式，仍保留 `email_exists` 检查（防水位线与实际不符）。`full` 模式完全靠 `email_exists` + `invoice_no UNIQUE` 兜底。

### 4.7 不动邮箱状态
全程只用 IMAP 的 `SEARCH/FETCH/UID`，不调用 `STORE +FLAGS \Seen`，不改邮箱已读状态、不删邮箱邮件。

## 5. 删除流程改造（db.py）

### 5.1 删除范围（三件套）
删 invoices 记录时联动：
1. **磁盘 PDF 文件**：按 `pdf_path` 删本地 PDF
2. **emails 表记录**：仅当该 email 没被其他 invoice 引用时才删
3. **水位线回退**：删完 emails 后重算

### 5.2 `delete_invoices` 改造伪代码
```python
def delete_invoices(ids):
    ids = [int(x) for x in ids if str(x).isdigit()]
    if not ids: return 0
    c = conn()
    ph = ",".join("?" * len(ids))
    # 1. 取 pdf_path + email_id
    rows = c.execute(f"SELECT id, pdf_path, email_id FROM invoices WHERE id IN ({ph})", ids).fetchall()
    # 2. 删磁盘 PDF
    for r in rows:
        if r["pdf_path"]:
            pdf_abs = os.path.join(HERE, r["pdf_path"])
            if os.path.isfile(pdf_abs):
                try: os.remove(pdf_abs)
                except OSError: pass
    # 3. 删 invoices
    cur = c.execute(f"DELETE FROM invoices WHERE id IN ({ph})", ids)
    n = cur.rowcount
    # 4. 删无引用的 emails 记录
    accs_to_check = set()
    for r in rows:
        eid = r["email_id"]
        if eid is None: continue
        still_ref = c.execute("SELECT 1 FROM invoices WHERE email_id=?", (eid,)).fetchone()
        if not still_ref:
            er = c.execute("SELECT account_id FROM emails WHERE id=?", (eid,)).fetchone()
            if er: accs_to_check.add(er["account_id"])
            c.execute("DELETE FROM emails WHERE id=?", (eid,))
    c.commit()
    c.close()
    # 5. 回退水位线
    for acc_id in accs_to_check:
        new_last = get_max_uid_for_account(acc_id)  # emails 表该账号 max(uid)
        update_last_uid(acc_id, new_last)           # None if no emails left
    return n
```

### 5.3 水位线回退逻辑
```
删 emails 记录后：
  new_last_uid = SELECT max(uid) FROM emails WHERE account_id=?
  UPDATE accounts SET last_uid=? WHERE id=?
```
- 删的恰好是最高 UID → `last_uid` 回退到次高 → 下次 incremental 重拉该邮件
- 删的不是最高 UID → `max(uid)` 不变 → `last_uid` 不动
- 该账号 emails 全删空 → `last_uid = NULL` → 下次拉整个 SINCE 范围

### 5.4 一个 email 多 invoice 的处理
删 invoice 后检查 `SELECT 1 FROM invoices WHERE email_id=?`：
- 仍有其他 invoice 引用 → **不删** emails 记录、**不动**水位线
- 无引用 → 删 emails 记录 + 重算水位线

### 5.5 seed 导入的本地发票（email_id=NULL）
删时只删 invoice 记录 + PDF，不涉及 emails/水位线。

### 5.6 不动邮箱的保证
删除全程只操作本地 SQLite + 本地磁盘文件，不 import imaplib、不连接邮箱服务器。

## 6. Web UI 改造

### 6.1 账号编辑模态框新增 3 个偏好字段
现有模态框有 name/email/host/pass/folder，新增：

| 字段 | 控件 | 说明 |
|---|---|---|
| 抓取模式 | `<select>`：增量(默认)/全量 | 对应 `fetch_mode` |
| 默认时间范围 | `<input>` placeholder `"90d 或 2026-07-01"` | 对应 `default_since`，留空=90d |
| 发票关键词覆盖 | `<input>` placeholder `"发票,行程单（空=用全局）"` | 逗号分隔，存为 JSON 数组 |

### 6.2 抓取 Tab 改造
"立即抓取全部"按钮旁加一个**可选** since 输入框：
- 留空 → 各账号用自己的 `default_since`
- 填值 → 临时覆盖所有账号（一次性，不写入配置）

[index.html:740](../../../web/index.html#L740) 的硬编码 `{since:'2026-07-09'}` 删除。

### 6.3 账号列表卡片显示偏好摘要
在"最近抓取"旁加偏好标签，如：`增量 · 最近90天`。

### 6.4 后端 API 配套
- `POST /api/accounts`（保存）：接收新字段，`upsert_account` / `update_account` 加列。
- `POST /api/fetch`：`since` 参数可选（不传→账号默认；传→临时覆盖）。
- `POST /api/accounts/:id/fetch`：去掉硬编码 since，读账号配置。

### 6.5 不新增 API
复用现有 `/api/accounts` 保存接口扩展字段。

## 7. 不在范围内（YAGNI）

- 不引入 IMAP `UNSEEN`/`STORE \Seen`（不改邮箱状态）
- 不新建 `account_preferences` 独立表（偏好字段少，accounts 表加列足够）
- 不做账号级 PDF 解析规则覆盖（`rules.json` 保持全局，仅关键词可账号级覆盖）
- 不做抓取调度/cron 重构（保留现有"每 N 分钟自动抓取"，仅 since 改为读账号默认）

## 8. 测试要点

- **迁移幂等**：老库 `init()` 后列存在且老账号默认值正确
- **incremental 模式**：只拉 `uid > last_uid`，水位线推进
- **full 模式**：拉整个 SINCE 范围，email_exists 兜底
- **删除回退**：删最高 UID 邮件后 `last_uid` 回退；删非最高不动；全删空为 NULL
- **多 invoice 共享 email**：删一个 invoice 不删 emails，删最后一个才删
- **时间表达式**：`90d`/`2026-07-01`/空 三种解析正确
- **关键词覆盖**：NULL 用全局；有值用账号值
- **UID 语义**：`M.uid` 一致使用，序号/uid 不再混淆

## 9. 兼容性

- CLI `hub.py fetch --since X` 保留，作为临时覆盖（传给 `since_override`）
- 老库自动迁移，无需手动操作
- 现有 `reparse_all_pdfs` 不受影响
