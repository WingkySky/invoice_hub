"""
数据层 (Data Layer) —— invoice_hub 的唯一真相源。

所有"管理对象"都落在 SQLite 里：
  - accounts : 被管理的邮箱账号（数据驱动的入口）
  - emails   : 抓取到的原始邮件（去重后存档）
  - invoices : 解析后的发票记录（前端 / 报表都从这里读）

代码只负责"操作数据"，不写死任何账号、规则或视图内容。
"""
import sqlite3
import os
import re
import json
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
DB_PATH = os.path.join(DATA_DIR, "invoice_hub.db")
PDF_DIR = os.path.join(DATA_DIR, "pdfs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  email       TEXT NOT NULL UNIQUE,
  provider    TEXT,
  imap_host   TEXT,
  imap_port   INTEGER DEFAULT 993,
  use_ssl     INTEGER DEFAULT 1,
  folder      TEXT DEFAULT 'INBOX',
  password    TEXT,
  enabled     INTEGER DEFAULT 1,
  last_fetch  TEXT,
  last_uid          INTEGER,
  fetch_mode        TEXT DEFAULT 'incremental',
  default_since     TEXT DEFAULT '90d',
  keywords_override TEXT,
  fetch_method      TEXT DEFAULT 'imap',
  created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS emails(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id  INTEGER NOT NULL,
  uid         TEXT NOT NULL,
  subject     TEXT,
  from_addr   TEXT,
  date        TEXT,
  body_text   TEXT,
  body_html   TEXT,
  is_invoice  INTEGER DEFAULT 0,
  fetched_at  TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(account_id, uid)
);

CREATE TABLE IF NOT EXISTS invoices(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  email_id     INTEGER,
  account_id   INTEGER NOT NULL,
  buyer        TEXT,
  seller       TEXT,
  amount       REAL,
  invoice_no   TEXT,
  invoice_date TEXT,
  city         TEXT,
  pdf_path     TEXT,
  source_type  TEXT,
  note         TEXT,
  fetched_at   TEXT DEFAULT CURRENT_TIMESTAMP
  -- 去重策略见 _migrate_invoices：仅对“非空发票号”建部分唯一索引，
  -- 空发票号（PDF 解析失败）各自独立成行，避免互相覆盖丢数据
);

CREATE INDEX IF NOT EXISTS idx_inv_account ON invoices(account_id);
CREATE INDEX IF NOT EXISTS idx_inv_buyer  ON invoices(buyer);
CREATE INDEX IF NOT EXISTS idx_inv_city    ON invoices(city);

-- 公司维度（P0 公司归属）：规范公司清单，作为发票归属的真相源。
-- tax_id 可选，= 统一社会信用代码（企业信用代码，18 位），用于校验/匹配；
--   注意 ≠ 发票"税收分类编码"（税目编号，属商品行项目，不在本功能范围）。
-- aliases 为 JSON 数组，存放同一家公司的全称/简称/常见错写，归属时按双向子串匹配。
CREATE TABLE IF NOT EXISTS companies(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE,        -- 规范公司名（候选公司初始即 buyer）
  tax_id     TEXT,                        -- 可选：统一社会信用代码
  aliases    TEXT,                        -- JSON 数组，如 ["南沙友谊","南沙友谊人才"]
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_company_name ON companies(name);
"""


def conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(PDF_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA busy_timeout=8000")
        c.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return c


def init():
    """建库建表。幂等，可重复调用。对老库补加新列。"""
    c = conn()
    c.executescript(SCHEMA)
    _migrate_accounts_columns(c)
    _migrate_invoices(c)
    _migrate_company_columns(c)
    c.commit()
    c.close()
    return DB_PATH


def _migrate_accounts_columns(c):
    """老库 accounts 表缺新列时 ALTER 补加。幂等。"""
    cols = {r[1] for r in c.execute("PRAGMA table_info(accounts)").fetchall()}
    additions = [
        ("last_uid", "INTEGER"),
        ("fetch_mode", "TEXT DEFAULT 'incremental'"),
        ("default_since", "TEXT DEFAULT '90d'"),
        ("keywords_override", "TEXT"),
        ("fetch_method", "TEXT DEFAULT 'imap'"),
    ]
    for name, decl in additions:
        if name not in cols:
            c.execute(f"ALTER TABLE accounts ADD COLUMN {name} {decl}")


def _migrate_invoices(c):
    """把 invoices.invoice_no 的「全量唯一」约束改为「仅非空唯一」的部分唯一索引。
    原因：发票号解析失败时 invoice_no=''，全量 UNIQUE 会让所有空号发票互相覆盖只留 1 张（静默丢数据）。
    注意：SQLite 不允许 DROP 由 UNIQUE 约束自动创建的索引，因此用「建新表→复制→删旧表→改名」重建；迁移幂等。"""
    try:
        c.execute("SAVEPOINT inv_mig")
        # 是否仍存在由 UNIQUE(invoice_no) 自动创建的索引（含空值约束）。
        # 注：自动索引的 sql 列为 NULL，不能用 LIKE 判断，只能按名称前缀识别。
        has_full = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND tbl_name='invoices' "
            "AND name LIKE 'sqlite_autoindex_invoices_%'"
        ).fetchone()
        if has_full:
            c.execute("""CREATE TABLE invoices_new(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id INTEGER,
                account_id INTEGER NOT NULL,
                buyer TEXT, seller TEXT, amount REAL,
                invoice_no TEXT, invoice_date TEXT, city TEXT,
                pdf_path TEXT, source_type TEXT, note TEXT,
                fetched_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""INSERT INTO invoices_new(id,email_id,account_id,buyer,seller,amount,
                invoice_no,invoice_date,city,pdf_path,source_type,note,fetched_at)
                SELECT id,email_id,account_id,buyer,seller,amount,
                invoice_no,invoice_date,city,pdf_path,source_type,note,fetched_at FROM invoices""")
            c.execute("DROP TABLE invoices")
            c.execute("ALTER TABLE invoices_new RENAME TO invoices")
            # 重建随表删除的普通索引
            c.execute("CREATE INDEX IF NOT EXISTS idx_inv_account ON invoices(account_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_inv_buyer  ON invoices(buyer)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_inv_city    ON invoices(city)")
        # 仅对“非空发票号”去重；空号（PDF 解析失败）各自独立成行，不再互相覆盖
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_inv_no_uniq "
                  "ON invoices(invoice_no) WHERE invoice_no IS NOT NULL AND invoice_no <> ''")
        # 补加 remark 列（存储从 PDF 提取的真实发票备注内容，区别于 note 字段记录的元信息）。幂等。
        inv_cols = {r[1] for r in c.execute("PRAGMA table_info(invoices)").fetchall()}
        if "remark" not in inv_cols:
            c.execute("ALTER TABLE invoices ADD COLUMN remark TEXT")
        c.execute("RELEASE inv_mig")
    except Exception as e:
        # 迁移失败不应中断整个 init（应用仍可启动）；回滚保存点后继续
        try:
            c.execute("ROLLBACK TO inv_mig")
            c.execute("RELEASE inv_mig")
        except Exception:
            pass
        print(f"[migrate_invoices] 迁移未完成（不影响运行）: {e}")


def _migrate_company_columns(c):
    """为 invoices 表补齐「公司归属维度」新增列（幂等）。不影响现有数据，老库公司相关字段均为 NULL。"""
    inv_cols = {r[1] for r in c.execute("PRAGMA table_info(invoices)").fetchall()}
    additions = [
        ("company_id", "INTEGER"),
        ("attribution_status", "TEXT DEFAULT 'unclassified'"),  # classified / unclassified / ambiguous
        ("attribution_reason", "TEXT"),                         # 未识别的结构化原因（用户+开发者可见）
        ("buyer_tax", "TEXT"),                                  # 发票解析出的购买方统一社会信用代码
    ]
    for name, decl in additions:
        if name not in inv_cols:
            c.execute(f"ALTER TABLE invoices ADD COLUMN {name} {decl}")
    # 索引（幂等）
    c.execute("CREATE INDEX IF NOT EXISTS idx_inv_company ON invoices(company_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_inv_attr ON invoices(attribution_status)")


# ----------------------------------------------------------- 买家匹配（与 matching.py 共享真相源）
def buyer_match(a, b):
    """买方模糊匹配：双向包含即视为匹配。模板常是简写（如'南沙友谊'），库内是全称（如'广州南沙友谊人才服务有限公司'）。"""
    if not a or not b:
        return False
    a, b = str(a).strip(), str(b).strip()
    return a == b or a in b or b in a


# ----------------------------------------------------------- 公司维度
# 归属解析的「公司清单」快照缓存：避免每次 insert_invoice 都查全表。
# 任何公司 CRUD 后失效，保证解析命中最新清单。
_COMPANIES_SNAPSHOT = None
_COMPANIES_SNAPSHOT_TS = 0.0


def _company_list_to_snapshot(rows):
    comps = []
    for r in rows:
        aliases = []
        try:
            aliases = json.loads(r["aliases"]) if r["aliases"] else []
        except Exception:
            aliases = []
        comps.append({
            "id": r["id"],
            "name": r["name"],
            "tax_id": r["tax_id"],
            "aliases": aliases,
        })
    return comps


def _get_companies_snapshot(force=False):
    """返回公司清单快照（含 name/tax_id/aliases）。带进程内缓存，CRUD 后失效。"""
    global _COMPANIES_SNAPSHOT, _COMPANIES_SNAPSHOT_TS
    if not force and _COMPANIES_SNAPSHOT is not None:
        return _COMPANIES_SNAPSHOT
    c = conn()
    rows = c.execute("SELECT id, name, tax_id, aliases FROM companies ORDER BY id").fetchall()
    c.close()
    _COMPANIES_SNAPSHOT = _company_list_to_snapshot(rows)
    _COMPANIES_SNAPSHOT_TS = 0.0
    return _COMPANIES_SNAPSHOT


def _invalidate_company_cache():
    global _COMPANIES_SNAPSHOT
    _COMPANIES_SNAPSHOT = None


def resolve_company(buyer, companies=None):
    """根据 buyer（购买方名称字符串）解析归属公司，返回 (company_id, status, reason)。

    - 公司清单为空        → (None, 'unclassified', '公司清单为空')
    - buyer 为空          → (None, 'unclassified', 'buyer 为空(解析失败)')
    - 精确匹配公司名       → (id,   'classified',  '自动匹配别名')
    - 命中某别名（双向子串）→ (id,   'classified',  '自动匹配别名')
    - 命中多家（歧义）     → (None, 'ambiguous',  '命中多公司(歧义)')
    - 无命中              → (None, 'unclassified', '无匹配别名')
    companies 可传入预加载清单，避免重复查库。
    """
    comps = companies if companies is not None else _get_companies_snapshot()
    if not comps:
        return (None, "unclassified", "公司清单为空")
    if not buyer:
        return (None, "unclassified", "buyer 为空(解析失败)")
    hits = []
    for c0 in comps:
        name = c0["name"] or ""
        if buyer == name:
            hits.append(c0)
            continue
        for a in (c0["aliases"] or []):
            a = a or ""
            if a and (buyer in a or a in buyer):
                hits.append(c0)
                break
    if len(hits) == 1:
        return (hits[0]["id"], "classified", "自动匹配别名")
    if len(hits) > 1:
        return (None, "ambiguous", "命中多公司(歧义)")
    return (None, "unclassified", "无匹配别名")


def _fill_company_tax_id(cid, buyer_tax):
    """若公司尚无 tax_id 且本发票带有 buyer 的统一社会信用代码，则回填（幂等）。"""
    if not cid or not buyer_tax:
        return
    c = conn()
    try:
        row = c.execute("SELECT tax_id FROM companies WHERE id=?", (cid,)).fetchone()
        if row and not row["tax_id"]:
            c.execute("UPDATE companies SET tax_id=? WHERE id=?", (buyer_tax, cid))
            c.commit()
            _invalidate_company_cache()
    finally:
        c.close()


def _compute_attribution(inv):
    """计算单张发票的归属三字段（company_id/status/reason），并顺便回填公司 tax_id。"""
    buyer = inv.get("buyer")
    buyer_tax = inv.get("buyer_tax")
    cid, status, reason = resolve_company(buyer)
    if cid is not None and status == "classified" and buyer_tax:
        _fill_company_tax_id(cid, buyer_tax)
    return cid, status, reason


def get_companies():
    c = conn()
    rows = c.execute("SELECT * FROM companies ORDER BY id").fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_company(company_id):
    c = conn()
    r = c.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    c.close()
    return dict(r) if r else None


def upsert_company(fields):
    """新增或按 name 更新公司（name 唯一）。aliases 接受 list 或 JSON 字符串或逗号分隔文本。"""
    f = dict(fields)
    name = (f.get("name") or "").strip()
    if not name:
        raise ValueError("公司名不能为空")
    aliases = f.get("aliases")
    if isinstance(aliases, (list, tuple)):
        aliases_json = json.dumps(list(aliases), ensure_ascii=False)
    elif isinstance(aliases, str) and aliases.strip():
        s = aliases.strip()
        if s.startswith("["):
            aliases_json = s
        else:
            aliases_json = json.dumps([x.strip() for x in s.split(",") if x.strip()], ensure_ascii=False)
    else:
        aliases_json = json.dumps([], ensure_ascii=False)
    tax_id = (f.get("tax_id") or "").strip() or None
    c = conn()
    try:
        c.execute(
            """INSERT INTO companies(name, tax_id, aliases)
               VALUES(:name, :tax_id, :aliases)
               ON CONFLICT(name) DO UPDATE SET
                 tax_id=COALESCE(excluded.tax_id, companies.tax_id),
                 aliases=COALESCE(NULLIF(excluded.aliases, '[]'), companies.aliases)""",
            {"name": name, "tax_id": tax_id, "aliases": aliases_json},
        )
        c.commit()
    finally:
        c.close()
    _invalidate_company_cache()
    # 新建公司后，把历史发票按新别名重新归属
    _reattribute_after_company_change()
    return get_company_by_name(name)


def get_company_by_name(name):
    c = conn()
    r = c.execute("SELECT * FROM companies WHERE name=?", (name,)).fetchone()
    c.close()
    return dict(r) if r else None


def update_company(company_id, fields):
    f = dict(fields)
    name = (f.get("name") or "").strip()
    if not name:
        raise ValueError("公司名不能为空")
    aliases = f.get("aliases")
    if isinstance(aliases, (list, tuple)):
        aliases_json = json.dumps(list(aliases), ensure_ascii=False)
    elif isinstance(aliases, str) and aliases.strip():
        s = aliases.strip()
        aliases_json = s if s.startswith("[") else json.dumps([x.strip() for x in s.split(",") if x.strip()], ensure_ascii=False)
    else:
        aliases_json = None
    tax_id = (f.get("tax_id") or "").strip() or None
    c = conn()
    try:
        if aliases_json is not None:
            c.execute(
                "UPDATE companies SET name=?, tax_id=?, aliases=? WHERE id=?",
                (name, tax_id, aliases_json, company_id),
            )
        else:
            c.execute(
                "UPDATE companies SET name=?, tax_id=? WHERE id=?",
                (name, tax_id, company_id),
            )
        c.commit()
    finally:
        c.close()
    _invalidate_company_cache()
    _reattribute_after_company_change()
    return get_company(company_id)


def delete_company(company_id):
    c = conn()
    try:
        # 该公司名下发票回退为未归类（不删除发票本身）
        c.execute(
            "UPDATE invoices SET company_id=NULL, attribution_status='unclassified', "
            "attribution_reason='公司已删除' WHERE company_id=?",
            (company_id,),
        )
        c.execute("DELETE FROM companies WHERE id=?", (company_id,))
        c.commit()
    finally:
        c.close()
    _invalidate_company_cache()
    return True


def import_companies_from_invoices():
    """从已归集发票的 buyer 去重，生成候选公司（name=buyer，空别名，tax_id 取首条 buyer_tax）。
    幂等：已存在同名规范公司则跳过；可重复执行。

    单条失败不阻塞整体——收集到 errors 列表里返回，便于前端显示给用户。
    """
    rows = get_distinct_buyers_with_tax()
    existing = {x["name"] for x in get_companies()}
    added = 0
    skipped = 0
    errors = []
    for r in rows:
        name = (r["buyer"] or "").strip()
        if not name or name in existing:
            skipped += 1
            continue
        try:
            upsert_company({"name": name, "tax_id": r.get("tax"), "aliases": []})
            existing.add(name)
            added += 1
        except Exception as e:
            errors.append({"buyer": name, "error": f"{type(e).__name__}: {e}"})
    return {"added": added, "skipped": skipped, "errors": errors}


def get_distinct_buyers_with_tax():
    """distinct buyer + 首条 buyer_tax（用于从发票导入候选公司）。"""
    c = conn()
    rows = c.execute(
        "SELECT DISTINCT buyer, (SELECT buyer_tax FROM invoices i2 WHERE i2.buyer=i.buyer "
        "AND i2.buyer_tax IS NOT NULL LIMIT 1) AS tax "
        "FROM invoices i WHERE buyer IS NOT NULL AND buyer<>'' ORDER BY buyer"
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def reattribute_invoice(inv_id):
    """对单张发票重新计算归属（读取其最新 buyer / buyer_tax）。供「按发票号更新后」「回填」复用。"""
    c = conn()
    try:
        r = c.execute("SELECT buyer, buyer_tax FROM invoices WHERE id=?", (inv_id,)).fetchone()
        if not r:
            return
        cid, status, reason = resolve_company(r["buyer"])
        if cid is not None and status == "classified" and r["buyer_tax"]:
            _fill_company_tax_id(cid, r["buyer_tax"])
        c.execute(
            "UPDATE invoices SET company_id=?, attribution_status=?, attribution_reason=? WHERE id=?",
            (cid, status, reason, inv_id),
        )
        c.commit()
    finally:
        c.close()


def _reattribute_after_company_change():
    """公司清单变更（增/改/删）后，对尚未正确归属的发票做轻量重归属。
    仅处理 company_id 为空或状态非 classified 的发票，避免全表重写。"""
    c = conn()
    try:
        rows = c.execute(
            "SELECT id FROM invoices WHERE company_id IS NULL OR attribution_status<>'classified'"
        ).fetchall()
        ids = [r["id"] for r in rows]
    finally:
        c.close()
    for iid in ids:
        try:
            reattribute_invoice(iid)
        except Exception:
            continue


def backfill_company_attribution(batch_size=200, on_progress=None):
    """对历史未归类/歧义发票重跑归属逻辑（R7 历史回填）。幂等可重跑。"""
    c = conn()
    try:
        rows = c.execute(
            "SELECT id FROM invoices WHERE company_id IS NULL OR attribution_status<>'classified'"
        ).fetchall()
        ids = [r["id"] for r in rows]
    finally:
        c.close()
    total = len(ids)
    done = 0
    for i in range(0, total, batch_size):
        chunk = ids[i:i + batch_size]
        for iid in chunk:
            try:
                reattribute_invoice(iid)
            except Exception:
                continue
            done += 1
        if on_progress:
            on_progress(done, total)
    # 回填后的统计
    c = conn()
    try:
        classified = c.execute("SELECT COUNT(*) AS n FROM invoices WHERE attribution_status='classified'").fetchone()["n"]
        unclassified = c.execute("SELECT COUNT(*) AS n FROM invoices WHERE attribution_status='unclassified'").fetchone()["n"]
        ambiguous = c.execute("SELECT COUNT(*) AS n FROM invoices WHERE attribution_status='ambiguous'").fetchone()["n"]
    finally:
        c.close()
    return {"scanned": total, "classified": classified, "unclassified": unclassified, "ambiguous": ambiguous}


# ----------------------------------------------------------- 账号 CRUD
def get_accounts(enabled_only=False):
    c = conn()
    sql = "SELECT * FROM accounts"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id"
    rows = c.execute(sql).fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_account(acc_id):
    c = conn()
    r = c.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    c.close()
    return dict(r) if r else None


def upsert_account(a):
    """新增或按 email 更新账号。provider 可选，缺省存 NULL。"""
    a = dict(a)  # 不污染调用方 dict
    a.setdefault("provider", None)
    a.setdefault("fetch_mode", "incremental")
    a.setdefault("fetch_method", "imap")
    a.setdefault("default_since", "90d")
    a.setdefault("keywords_override", None)
    c = conn()
    try:
        cur = c.execute(
            """INSERT INTO accounts(name,email,provider,imap_host,imap_port,use_ssl,
                 folder,password,enabled,fetch_mode,default_since,keywords_override,fetch_method)
               VALUES(:name,:email,:provider,:imap_host,:imap_port,:use_ssl,
                 :folder,:password,:enabled,:fetch_mode,:default_since,:keywords_override,:fetch_method)
               ON CONFLICT(email) DO UPDATE SET
                 name=excluded.name, provider=excluded.provider, imap_host=excluded.imap_host,
                 imap_port=excluded.imap_port, use_ssl=excluded.use_ssl, folder=excluded.folder,
                 password=excluded.password, enabled=excluded.enabled,
                 fetch_mode=excluded.fetch_mode, default_since=excluded.default_since,
                 keywords_override=excluded.keywords_override, fetch_method=excluded.fetch_method""",
            a,
        )
        last_id = cur.lastrowid
        c.commit()
        r = c.execute("SELECT * FROM accounts WHERE id=?", (last_id or 0,)).fetchone()
        # lastrowid 在 ON CONFLICT DO UPDATE 时可能为 0，退回按 email 查
        if not r:
            r = c.execute("SELECT * FROM accounts WHERE email=?", (a.get("email"),)).fetchone()
    finally:
        c.close()
    return dict(r) if r else None


def set_account_enabled(acc_id, enabled):
    c = conn()
    try:
        c.execute("UPDATE accounts SET enabled=? WHERE id=?", (1 if enabled else 0, acc_id))
        c.commit()
    finally:
        c.close()


def set_account_fetch(acc_id, ts):
    c = conn()
    try:
        c.execute("UPDATE accounts SET last_fetch=? WHERE id=?", (ts, acc_id))
        c.commit()
    finally:
        c.close()


def update_last_uid(acc_id, uid):
    """更新账号水位线。uid 传 None 则置 NULL。"""
    c = conn()
    try:
        c.execute("UPDATE accounts SET last_uid=? WHERE id=?", (uid, acc_id))
        c.commit()
    finally:
        c.close()


def get_max_uid_for_account(acc_id):
    """返回 emails 表中该账号最大的 uid（整数）；无记录返回 None。
    emails.uid 存为 TEXT，按整数比较需 CAST。"""
    c = conn()
    r = c.execute(
        "SELECT MAX(CAST(uid AS INTEGER)) AS m FROM emails WHERE account_id=?",
        (acc_id,),
    ).fetchone()
    c.close()
    return r["m"] if r and r["m"] is not None else None


def update_account(a):
    """按 id 更新账号配置；password 为空时不覆盖（保留原值）。"""
    c = conn()
    try:
        c.execute(
            """UPDATE accounts SET
                 name=:name, email=:email, provider=:provider, imap_host=:imap_host,
                 imap_port=:imap_port, use_ssl=:use_ssl, folder=:folder,
                 fetch_mode=:fetch_mode, default_since=:default_since,
                 keywords_override=:keywords_override, fetch_method=:fetch_method
               WHERE id=:id""",
            a,
        )
        if a.get("password"):
            c.execute("UPDATE accounts SET password=:password WHERE id=:id", a)
        c.commit()
    finally:
        c.close()

def delete_account(acc_id):
    c = conn()
    try:
        r = c.execute("SELECT email FROM accounts WHERE id=?", (acc_id,)).fetchone()
        c.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
        c.commit()
    finally:
        c.close()
    # 级联删除该账号的本地 PDF 目录（派生物，随账号一起清理）
    if r:
        acc_dir = os.path.join(PDF_DIR, _safe_dir(r["email"]))
        if os.path.isdir(acc_dir):
            try:
                import shutil as _sh
                _sh.rmtree(acc_dir)
            except OSError:
                pass


# ----------------------------------------------------------- 邮件 / 发票
def email_exists(account_id, uid):
    c = conn()
    r = c.execute("SELECT 1 FROM emails WHERE account_id=? AND uid=?", (account_id, uid)).fetchone()
    c.close()
    return r is not None

def get_email_id(account_id, uid):
    """返回该 (账号, uid) 邮件行的 id；不存在返回 None。供发票回链 / 抓取幂等判定。"""
    c = conn()
    r = c.execute("SELECT id FROM emails WHERE account_id=? AND uid=?", (account_id, uid)).fetchone()
    c.close()
    return r["id"] if r else None

def set_email_invoice(eid, is_invoice):
    """更新邮件的 is_invoice 标志（抓取时发现是/不是发票时校正）。"""
    c = conn()
    try:
        c.execute("UPDATE emails SET is_invoice=? WHERE id=?", (1 if is_invoice else 0, eid))
        c.commit()
    finally:
        c.close()

def needs_refetch(account_id, uid):
    """抓取时的【幂等 + 自愈】判定：该 uid 是否需要从服务器重新拉取。
    设计原则：表是真相源，文件是派生物。
      - 邮件不存在            → 需要（新邮件）
      - 邮件 is_invoice=0    → 不需要（非发票，已存档，不重复拉）
      - 邮件 is_invoice=1 且有发票行、且 PDF 文件仍在 → 不需要（已齐全，避免重复下载）
      - 邮件 is_invoice=1 但发票缺失 / PDF 文件丢失 → 需要（重新拉取以恢复）
    """
    c = conn()
    row = c.execute(
        """SELECT e.is_invoice AS is_inv, i.id AS inv_id, i.pdf_path AS pdf,
                  i.source_type AS src, e.body_html AS body
           FROM emails e LEFT JOIN invoices i ON i.email_id=e.id
           WHERE e.account_id=? AND e.uid=?""",
        (account_id, uid),
    ).fetchone()
    c.close()
    if row is None:
        return True
    if not row["is_inv"]:
        return False
    if row["inv_id"] is not None:
        # 标题兜底入库的发票本就无 PDF（source_type='subject'），通常视为已齐全、不重复拉取；
        # 但若邮件正文含 51发票短链，说明可通过专用链路补全真实 PDF，允许重抓。
        if row["src"] == "subject":
            if row["body"] and "51fapiao.cn" in row["body"]:
                return True
            return False
        # 有 PDF 且文件仍在 → 已齐全；PDF 丢失 → 需重拉以恢复
        if row["pdf"]:
            pdf_abs = os.path.join(HERE, row["pdf"])
            if os.path.isfile(pdf_abs):
                return False
    return True

def _safe_dir(name):
    """与 engine.safe_name 完全一致的目录名清洗，供级联删除账号目录使用。"""
    return re.sub(r'[\\/:*?"<>|]+', "_", name or "").strip()[:80]


def insert_email(e):
    c = conn()
    try:
        cur = c.execute(
            """INSERT OR IGNORE INTO emails(account_id,uid,subject,from_addr,date,body_text,body_html,is_invoice)
               VALUES(:account_id,:uid,:subject,:from_addr,:date,:body_text,:body_html,:is_invoice)""",
            e,
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def insert_invoice(inv):
    """插入发票；若 invoice_no 已存在则忽略（去重）。返回新行 id（已忽略则返回 None）。
    返回 id 而非布尔，方便调用方拿到主键；所有 `if insert_invoice(...)` 仍按真值工作。
    写入时自动计算归属三字段（company_id / attribution_status / attribution_reason）并采集 buyer_tax。"""
    inv = dict(inv)
    cid, status, reason = _compute_attribution(inv)
    inv["company_id"] = cid
    inv["attribution_status"] = status
    inv["attribution_reason"] = reason
    c = conn()
    try:
        cur = c.execute(
            """INSERT OR IGNORE INTO invoices(email_id,account_id,buyer,seller,amount,invoice_no,
                 invoice_date,city,pdf_path,source_type,note,remark,
                 company_id,attribution_status,attribution_reason,buyer_tax)
               VALUES(:email_id,:account_id,:buyer,:seller,:amount,:invoice_no,
                 :invoice_date,:city,:pdf_path,:source_type,:note,:remark,
                 :company_id,:attribution_status,:attribution_reason,:buyer_tax)""",
            inv,
        )
        new_id = cur.lastrowid if cur.rowcount > 0 else None
        c.commit()
    finally:
        c.close()
    return new_id


def get_invoice_by_no(account_id, invoice_no):
    """按 账号+发票号 取单条发票（精确匹配）。用于下载 PDF 后更新已有行、避免重复插入。"""
    if not invoice_no:
        return None
    c = conn()
    row = c.execute(
        "SELECT * FROM invoices WHERE account_id=? AND invoice_no=?",
        (account_id, invoice_no),
    ).fetchone()
    c.close()
    return dict(row) if row else None


def email_has_invoice(email_id):
    """该邮件是否已存在任意发票行（不论来源）。用于 Fix3 判定，避免把「有发票但暂未拿到 PDF」的邮件误翻成非发票。"""
    if not email_id:
        return False
    c = conn()
    row = c.execute(
        "SELECT 1 FROM invoices WHERE email_id=?", (email_id,)
    ).fetchone()
    c.close()
    return row is not None


def _apply_filters(sql, filters, params):
    """把筛选条件追加到 SQL 上（与 get_invoices / get_invoices_count 共用）。"""
    if filters.get("account_id"):
        sql += " AND i.account_id=?"
        params.append(filters["account_id"])
    if filters.get("buyer"):
        sql += " AND i.buyer LIKE ?"
        params.append(f"%{filters['buyer']}%")
    if filters.get("seller"):
        sql += " AND i.seller LIKE ?"
        params.append(f"%{filters['seller']}%")
    if filters.get("city"):
        sql += " AND i.city=?"
        params.append(filters["city"])
    if filters.get("invoice_no"):
        sql += " AND i.invoice_no LIKE ?"
        params.append(f"%{filters['invoice_no']}%")
    if filters.get("date_from") or filters.get("date_to"):
        # 库内 invoice_date 可能是中文格式（如"2026年07月10日"），也可能是 ISO 格式。
        # 直接字符串比较会让中文格式全部失配（"2026年07月10日" >= "2026-07-01" 为假），
        # 因此先把"年/月/日"替换成"-"再比较；纯 ISO 格式无中文字符，replace 后保持不变。
        norm = "replace(replace(replace(i.invoice_date, '年', '-'), '月', '-'), '日', '')"
        if filters.get("date_from"):
            sql += f" AND {norm} >= ?"
            params.append(filters["date_from"])
        if filters.get("date_to"):
            sql += f" AND {norm} <= ?"
            params.append(filters["date_to"])
    if filters.get("keyword"):
        kw = f"%{filters['keyword']}%"
        sql += " AND (i.buyer LIKE ? OR i.seller LIKE ? OR i.invoice_no LIKE ? OR i.city LIKE ?)"
        params += [kw, kw, kw, kw]
    if filters.get("company_id"):
        sql += " AND i.company_id=?"
        params.append(filters["company_id"])
    if filters.get("attribution_status"):
        # 支持逗号分隔多值，如 "unclassified,ambiguous"
        statuses = [s.strip() for s in str(filters["attribution_status"]).split(",") if s.strip()]
        if statuses:
            ph = ",".join("?" * len(statuses))
            sql += f" AND i.attribution_status IN ({ph})"
            params += statuses
    return sql, params


def get_invoices(filters=None, page=1, page_size=50):
    """按筛选条件读取发票（分页），并 join 出账号名/邮箱。
    page 从 1 开始；page_size<=0 表示不分页（返回全部，向后兼容）。"""
    filters = filters or {}
    c = conn()
    sql = (
        "SELECT i.*, a.name AS account_name, a.email AS account_email, "
        "c.name AS company_name "
        "FROM invoices i LEFT JOIN accounts a ON i.account_id=a.id "
        "LEFT JOIN companies c ON i.company_id=c.id WHERE 1=1"
    )
    params = []
    sql, params = _apply_filters(sql, filters, params)
    sql += " ORDER BY i.invoice_date DESC, i.id DESC"
    if page_size and page_size > 0:
        offset = max(0, (page - 1) * page_size)
        sql += " LIMIT ? OFFSET ?"
        params += [int(page_size), int(offset)]
    rows = c.execute(sql, params).fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_invoices_count(filters=None):
    """与 get_invoices 同筛选条件的总数（用于分页器）。"""
    filters = filters or {}
    c = conn()
    sql = "SELECT COUNT(*) AS n FROM invoices i WHERE 1=1"
    params = []
    sql, params = _apply_filters(sql, filters, params)
    n = c.execute(sql, params).fetchone()["n"]
    c.close()
    return n


def get_stats(filters=None):
    """合计金额与张数（与 get_invoices 用同样的筛选，统计全部匹配行，不分页）。
    用 SQL 聚合查询，避免把全部行取到 Python 里循环。"""
    filters = filters or {}
    c = conn()
    params = []
    sql = "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total FROM invoices i WHERE 1=1"
    sql, params = _apply_filters(sql, filters, params)
    row = c.execute(sql, params).fetchone()
    count = row["n"]
    total = round(float(row["total"]), 2)

    # 按买方聚合（SQL GROUP BY，而非 Python 循环）
    params2 = []
    sql2 = ("SELECT COALESCE(NULLIF(buyer, ''), '(未知)') AS buyer, "
            "COALESCE(SUM(amount), 0) AS s FROM invoices i WHERE 1=1")
    sql2, params2 = _apply_filters(sql2, filters, params2)
    sql2 += " GROUP BY COALESCE(NULLIF(buyer, ''), '(未知)') ORDER BY s DESC"
    by_buyer_rows = c.execute(sql2, params2).fetchall()
    c.close()
    by_buyer = {r["buyer"]: round(float(r["s"]), 2) for r in by_buyer_rows}
    return {"count": count, "total": total, "by_buyer": by_buyer}


def get_stats_by_company(filters=None):
    """按公司聚合金额与张数（R5 按公司分组统计）。未归类发票归入 '(未归类)'。
    返回 {"companies": {公司名: {"count","total"}}, "unclassified": {"count","total"}}。"""
    filters = filters or {}
    c = conn()
    params = []
    sql = ("SELECT COALESCE(c.name, '(未归类)') AS company_name, "
           "COALESCE(SUM(i.amount), 0) AS s, COUNT(*) AS n "
           "FROM invoices i LEFT JOIN companies c ON i.company_id=c.id WHERE 1=1")
    sql, params = _apply_filters(sql, filters, params)
    sql += " GROUP BY COALESCE(c.name, '(未归类)') ORDER BY s DESC"
    rows = c.execute(sql, params).fetchall()
    c.close()
    out = {}
    for r in rows:
        key = r["company_name"]
        if key == "(未归类)":
            continue
        out[key] = {"count": r["n"], "total": round(float(r["s"]), 2)}
    # 单独统计"未归类"（含 attribution_status 非 classified 的）
    c2 = conn()
    un = c2.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS s FROM invoices "
        "WHERE company_id IS NULL OR attribution_status<>'classified'"
    ).fetchone()
    c2.close()
    result = {"companies": out}
    if un["n"]:
        result["unclassified"] = {"count": un["n"], "total": round(float(un["s"]), 2)}
    return result


def attribution_summary():
    """归属概况：总数 / 各状态计数 / 各公司发票数。供 Web 公司管理页展示。"""
    c = conn()
    try:
        total = c.execute("SELECT COUNT(*) AS n FROM invoices").fetchone()["n"]
        classified = c.execute("SELECT COUNT(*) AS n FROM invoices WHERE attribution_status='classified'").fetchone()["n"]
        unclassified = c.execute("SELECT COUNT(*) AS n FROM invoices WHERE attribution_status='unclassified'").fetchone()["n"]
        ambiguous = c.execute("SELECT COUNT(*) AS n FROM invoices WHERE attribution_status='ambiguous'").fetchone()["n"]
        rows = c.execute(
            "SELECT COALESCE(c.name,'(未归类)') AS name, COUNT(*) AS n "
            "FROM invoices i LEFT JOIN companies c ON i.company_id=c.id "
            "GROUP BY COALESCE(c.name,'(未归类)') ORDER BY n DESC"
        ).fetchall()
    finally:
        c.close()
    by_company = {r["name"]: r["n"] for r in rows}
    return {"total": total, "classified": classified,
            "unclassified": unclassified, "ambiguous": ambiguous,
            "by_company": by_company}


def distinct_cities():
    c = conn()
    rows = c.execute("SELECT DISTINCT city FROM invoices WHERE city IS NOT NULL AND city<>'' ORDER BY city").fetchall()
    c.close()
    return [r["city"] for r in rows]


def distinct_buyers():
    c = conn()
    rows = c.execute("SELECT DISTINCT buyer FROM invoices WHERE buyer IS NOT NULL AND buyer<>'' ORDER BY buyer").fetchall()
    c.close()
    return [r["buyer"] for r in rows]


def distinct_sellers():
    """返回所有非空销售方列表（用于前端筛选下拉框）。"""
    c = conn()
    rows = c.execute("SELECT DISTINCT seller FROM invoices WHERE seller IS NOT NULL AND seller<>'' ORDER BY seller").fetchall()
    c.close()
    return [r["seller"] for r in rows]

def get_invoices_by_ids(ids):
    """按 id 列表取发票（导出打包用）。"""
    ids = [int(x) for x in ids if str(x).isdigit()]
    if not ids:
        return []
    c = conn()
    ph = ",".join("?" * len(ids))
    sql = (
        "SELECT i.*, a.name AS account_name, a.email AS account_email, "
        "c.name AS company_name "
        "FROM invoices i LEFT JOIN accounts a ON i.account_id=a.id "
        "LEFT JOIN companies c ON i.company_id=c.id "
        f"WHERE i.id IN ({ph}) ORDER BY i.id"
    )
    rows = c.execute(sql, ids).fetchall()
    c.close()
    return [dict(r) for r in rows]


def delete_invoices(ids, batch_size=200, on_progress=None):
    """按 id 列表批量删除发票：删 invoices 记录 + 本地磁盘 PDF + 无引用的 emails 记录，
    并重算涉及账号的水位线 last_uid。返回删除的发票总数。

    设计要点（应对大批量场景，避免界面卡死 / 长时间持锁）：
      - 分批（batch_size）提交：每个批次是独立短事务，写完即释放 WAL 写锁，
        期间其它读请求（列表 / 统计 / 进度轮询）始终可并发进行，UI 不再整页冻结。
      - 用集合化单条 SQL 替代「逐行 SELECT 1 校验」，消除 N+1 往返。
      - 绝不触碰邮箱服务器（不 import imaplib、不连接邮箱）。

    on_progress(done, total) 为可选回调，用于前台展示删除进度；
    batch_size 默认 200，可按数据量调整（单批越大事务越长、锁占用越久）。
    """
    ids = [int(x) for x in ids if str(x).isdigit()]
    if not ids:
        return 0
    total = len(ids)
    done = 0
    accs_to_check = set()
    # 分批处理：避免单个超大事务长期持有写锁，并让进度可反馈
    for i in range(0, total, batch_size):
        chunk = ids[i:i + batch_size]
        done += _delete_invoice_batch(chunk, accs_to_check)
        if on_progress:
            on_progress(done, total)
    # 重算受影响账号的水位线（emails 已删完）
    for acc_id in accs_to_check:
        update_last_uid(acc_id, get_max_uid_for_account(acc_id))
    return done


def _delete_invoice_batch(ids, accs_to_check):
    """删除一个批次的发票（独立短事务）。返回本批次删除的发票行数。
    accs_to_check 为调用方传入的 set，本函数把涉及账号塞进去供后续重算水位线。"""
    if not ids:
        return 0
    c = conn()
    ph = ",".join("?" * len(ids))
    try:
        # 1. 删前查询 pdf_path + email_id + account_id（删后就查不到了）
        rows = c.execute(
            f"SELECT v.id, v.pdf_path, v.email_id, e.account_id "
            f"FROM invoices v LEFT JOIN emails e ON v.email_id=e.id "
            f"WHERE v.id IN ({ph})", ids
        ).fetchall()
        # 2. 删磁盘 PDF 文件（无 PDF / 文件不存在则跳过，不报错）
        for r in rows:
            if r["pdf_path"]:
                pdf_abs = os.path.join(HERE, r["pdf_path"])
                if os.path.isfile(pdf_abs):
                    try:
                        os.remove(pdf_abs)
                    except OSError:
                        pass
        # 3. 收集涉及账号（用于删除后重算水位线）
        for r in rows:
            if r["account_id"] is not None:
                accs_to_check.add(r["account_id"])
        # 4. 删 invoices 记录（单条批量 DELETE）
        cur = c.execute(f"DELETE FROM invoices WHERE id IN ({ph})", ids)
        n = cur.rowcount
        # 5. 删"无其他 invoice 引用"的 emails 记录（集合化单条 SQL，替代 N+1 逐行校验）
        email_ids = [r["email_id"] for r in rows if r["email_id"] is not None]
        if email_ids:
            eph = ",".join("?" * len(email_ids))
            c.execute(
                f"DELETE FROM emails WHERE id IN ({eph}) "
                f"AND NOT EXISTS (SELECT 1 FROM invoices i WHERE i.email_id = emails.id)",
                email_ids,
            )
        c.commit()
        return n
    finally:
        c.close()


def get_invoice_by_id(inv_id):
    """按 id 取单条发票。"""
    c = conn()
    row = c.execute("SELECT * FROM invoices WHERE id=?", (int(inv_id),)).fetchone()
    c.close()
    return dict(row) if row else None


def update_invoice_fields(inv_id, fields):
    """按 id 更新发票的指定字段（fields 是 dict，只更新传入的 key）。"""
    if not fields:
        return False
    sets = ", ".join(f"{k}=?" for k in fields.keys())
    vals = list(fields.values()) + [int(inv_id)]
    c = conn()
    c.execute(f"UPDATE invoices SET {sets} WHERE id=?", vals)
    c.commit()
    c.close()
    return True


def assign_invoices(invoice_ids, company_id):
    """批量人工归属（R4）。company_id 可为 None（置为未归类）。
    返回实际更新的行数。"""
    ids = [int(x) for x in invoice_ids if str(x).isdigit()]
    if not ids:
        return 0
    if company_id is not None:
        if not get_company(company_id):
            raise ValueError("指定的公司不存在")
        cid, status, reason = company_id, "classified", "人工指定"
    else:
        cid, status, reason = None, "unclassified", "人工置为未归类"
    c = conn()
    try:
        ph = ",".join("?" * len(ids))
        cur = c.execute(
            f"UPDATE invoices SET company_id=?, attribution_status=?, attribution_reason=? "
            f"WHERE id IN ({ph})",
            [cid, status, reason] + ids,
        )
        n = cur.rowcount
        c.commit()
    finally:
        c.close()
    return n


def get_invoices_for_matching(buyer, date_from, date_to):
    """按买方模糊匹配取候选发票（供模板匹配引擎使用）。

    buyer: 买方名称（模糊匹配：库值包含模板值 或 模板值包含库值，因为模板常是简写如'南沙友谊'，
           而库内是全称如'广州南沙友谊人才服务有限公司'）
    date_from/date_to: 保留参数兼容，但库内 invoice_date 格式可能是'2026年07月10日'中文格式，
           SQL 字符串比较不可靠，因此实际日期过滤由 matching.py 用 _to_datetime 解析后做。
    返回 dict 列表，包含 id/amount/invoice_no/invoice_date/buyer/seller/note/remark/pdf_path。
    按 invoice_date 升序排列。"""
    c = conn()
    # 双向 LIKE：库值 LIKE '%模板值%' OR 模板值 LIKE '%库值%'
    # SQLite 不支持在 LIKE 右侧用列做模式，所以用 instr 双向判断
    rows = c.execute(
        """SELECT id, amount, invoice_no, invoice_date, buyer, seller, note, remark, pdf_path
           FROM invoices
           WHERE (instr(buyer, ?) > 0 OR instr(?, buyer) > 0)
             AND invoice_no IS NOT NULL AND invoice_no<>''
           ORDER BY invoice_date ASC""",
        (buyer, buyer),
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]
