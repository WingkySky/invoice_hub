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
  fetched_at   TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(invoice_no)
);

CREATE INDEX IF NOT EXISTS idx_inv_account ON invoices(account_id);
CREATE INDEX IF NOT EXISTS idx_inv_buyer  ON invoices(buyer);
CREATE INDEX IF NOT EXISTS idx_inv_city    ON invoices(city);
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
    ]
    for name, decl in additions:
        if name not in cols:
            c.execute(f"ALTER TABLE accounts ADD COLUMN {name} {decl}")


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
    a.setdefault("default_since", "90d")
    a.setdefault("keywords_override", None)
    c = conn()
    try:
        c.execute(
            """INSERT INTO accounts(name,email,provider,imap_host,imap_port,use_ssl,
                 folder,password,enabled,fetch_mode,default_since,keywords_override)
               VALUES(:name,:email,:provider,:imap_host,:imap_port,:use_ssl,
                 :folder,:password,:enabled,:fetch_mode,:default_since,:keywords_override)
               ON CONFLICT(email) DO UPDATE SET
                 name=excluded.name, provider=excluded.provider, imap_host=excluded.imap_host,
                 imap_port=excluded.imap_port, use_ssl=excluded.use_ssl, folder=excluded.folder,
                 password=excluded.password, enabled=excluded.enabled,
                 fetch_mode=excluded.fetch_mode, default_since=excluded.default_since,
                 keywords_override=excluded.keywords_override""",
            a,
        )
        c.commit()
    finally:
        c.close()


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
                 keywords_override=:keywords_override
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
        c.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
        c.commit()
    finally:
        c.close()


# ----------------------------------------------------------- 邮件 / 发票
def email_exists(account_id, uid):
    c = conn()
    r = c.execute("SELECT 1 FROM emails WHERE account_id=? AND uid=?", (account_id, uid)).fetchone()
    c.close()
    return r is not None


def insert_email(e):
    c = conn()
    try:
        c.execute(
            """INSERT OR IGNORE INTO emails(account_id,uid,subject,from_addr,date,body_text,body_html,is_invoice)
               VALUES(:account_id,:uid,:subject,:from_addr,:date,:body_text,:body_html,:is_invoice)""",
            e,
        )
        c.commit()
    finally:
        c.close()


def insert_invoice(inv):
    """插入发票；若 invoice_no 已存在则忽略（去重）。返回是否新增。"""
    c = conn()
    try:
        cur = c.execute(
            """INSERT OR IGNORE INTO invoices(email_id,account_id,buyer,seller,amount,invoice_no,
                 invoice_date,city,pdf_path,source_type,note)
               VALUES(:email_id,:account_id,:buyer,:seller,:amount,:invoice_no,
                 :invoice_date,:city,:pdf_path,:source_type,:note)""",
            inv,
        )
        inserted = cur.rowcount > 0
        c.commit()
    finally:
        c.close()
    return inserted


def _apply_filters(sql, filters, params):
    """把筛选条件追加到 SQL 上（与 get_invoices / get_invoices_count 共用）。"""
    if filters.get("account_id"):
        sql += " AND i.account_id=?"
        params.append(filters["account_id"])
    if filters.get("buyer"):
        sql += " AND i.buyer LIKE ?"
        params.append(f"%{filters['buyer']}%")
    if filters.get("city"):
        sql += " AND i.city=?"
        params.append(filters["city"])
    if filters.get("invoice_no"):
        sql += " AND i.invoice_no LIKE ?"
        params.append(f"%{filters['invoice_no']}%")
    if filters.get("date_from"):
        sql += " AND i.invoice_date >= ?"
        params.append(filters["date_from"])
    if filters.get("date_to"):
        sql += " AND i.invoice_date <= ?"
        params.append(filters["date_to"])
    if filters.get("keyword"):
        kw = f"%{filters['keyword']}%"
        sql += " AND (i.buyer LIKE ? OR i.seller LIKE ? OR i.invoice_no LIKE ? OR i.city LIKE ?)"
        params += [kw, kw, kw, kw]
    return sql, params


def get_invoices(filters=None, page=1, page_size=50):
    """按筛选条件读取发票（分页），并 join 出账号名/邮箱。
    page 从 1 开始；page_size<=0 表示不分页（返回全部，向后兼容）。"""
    filters = filters or {}
    c = conn()
    sql = (
        "SELECT i.*, a.name AS account_name, a.email AS account_email "
        "FROM invoices i LEFT JOIN accounts a ON i.account_id=a.id WHERE 1=1"
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

def get_invoices_by_ids(ids):
    """按 id 列表取发票（导出打包用）。"""
    ids = [int(x) for x in ids if str(x).isdigit()]
    if not ids:
        return []
    c = conn()
    ph = ",".join("?" * len(ids))
    sql = (
        "SELECT i.*, a.name AS account_name, a.email AS account_email "
        "FROM invoices i LEFT JOIN accounts a ON i.account_id=a.id "
        f"WHERE i.id IN ({ph}) ORDER BY i.id"
    )
    rows = c.execute(sql, ids).fetchall()
    c.close()
    return [dict(r) for r in rows]


def delete_invoices(ids):
    """按 id 列表删除发票：删 invoices 记录 + 本地磁盘 PDF + 无引用的 emails 记录，
    并重算涉及账号的水位线 last_uid。返回删除的发票数量。
    绝不触碰邮箱服务器（不 import imaplib、不连接邮箱）。"""
    ids = [int(x) for x in ids if str(x).isdigit()]
    if not ids:
        return 0
    c = conn()
    ph = ",".join("?" * len(ids))
    # 1. 取 pdf_path + email_id（删前查询，删后查不到）
    rows = c.execute(
        f"SELECT id, pdf_path, email_id FROM invoices WHERE id IN ({ph})", ids
    ).fetchall()
    # 2. 删磁盘 PDF 文件
    for r in rows:
        if r["pdf_path"]:
            pdf_abs = os.path.join(HERE, r["pdf_path"])
            if os.path.isfile(pdf_abs):
                try:
                    os.remove(pdf_abs)
                except OSError:
                    pass
    # 3. 删 invoices 记录
    cur = c.execute(f"DELETE FROM invoices WHERE id IN ({ph})", ids)
    n = cur.rowcount
    # 4. 删"无其他 invoice 引用"的 emails 记录，收集需重算水位线的账号
    accs_to_check = set()
    for r in rows:
        eid = r["email_id"]
        if eid is None:
            continue
        still_ref = c.execute(
            "SELECT 1 FROM invoices WHERE email_id=?", (eid,)
        ).fetchone()
        if not still_ref:
            er = c.execute(
                "SELECT account_id FROM emails WHERE id=?", (eid,)
            ).fetchone()
            if er:
                accs_to_check.add(er["account_id"])
            c.execute("DELETE FROM emails WHERE id=?", (eid,))
    c.commit()
    c.close()
    # 5. 重算水位线（在 emails 删除后）
    for acc_id in accs_to_check:
        new_last = get_max_uid_for_account(acc_id)
        update_last_uid(acc_id, new_last)
    return n


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
