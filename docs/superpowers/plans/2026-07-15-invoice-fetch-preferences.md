# 发票抓取用户偏好与增量水位线 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 invoice_hub 引入账号级抓取偏好与本地水位线增量抓取，彻底删除可重抓，消除硬编码日期。

**Architecture:** 在 `accounts` 表加 4 列（last_uid/fetch_mode/default_since/keywords_override）；engine 统一用 IMAP UID 语义并按水位线增量过滤；db.delete_invoices 改为彻底删本地三件套（invoices+PDF+emails）并重算水位线；Web 账号模态框加偏好字段、抓取 Tab 加可选 since 输入。全程不动邮箱服务器状态。

**Tech Stack:** Python 3 标准库（imaplib/sqlite3/unittest/http.server）、PyMuPDF、BeautifulSoup。测试用标准库 unittest，无需安装额外依赖。

**Spec:** [docs/superpowers/specs/2026-07-15-invoice-fetch-preferences-design.md](../specs/2026-07-15-invoice-fetch-preferences-design.md)

**环境变量：** 运行 Python 用 README 里的 `PYENV="C:/Users/Ifesco/.workbuddy/binaries/python/envs/default/Scripts/python.exe"`，下文记作 `$PY`。Windows PowerShell 下：`$PY = "C:/Users/Ifesco/.workbuddy/binaries/python/envs/default/Scripts/python.exe"`。

---

## 文件结构

**修改：**
- `db.py` — schema 加列、init 迁移、upsert/update 加字段、delete_invoices 改造、新增 update_last_uid/get_max_uid_for_account
- `engine.py` — 新增 parse_since_expr、fetch_account/fetch_all/fetch_one 改造、UID 语义统一、关键词覆盖
- `hub.py` — cmd_fetch 签名适配
- `web/app.py` — 去硬编码 since、API 接收新字段
- `web/index.html` — 账号模态框加偏好字段、抓取 Tab 加 since 输入、卡片偏好标签

**新建：**
- `tests/__init__.py` — 空文件，标记测试包
- `tests/test_db.py` — DB 层测试（迁移/删除回退/水位线）
- `tests/test_engine.py` — engine 纯函数测试（parse_since_expr）

---

## Task 1: DB schema 迁移（accounts 表加 4 列）

**Files:**
- Modify: `db.py` 的 `SCHEMA` 与 `init()`
- Test: `tests/test_db.py`

- [ ] **Step 1: 创建 tests 包**

创建空文件 `tests/__init__.py`。

- [ ] **Step 2: 写失败测试 — init 后 accounts 表有新列**

创建 `tests/test_db.py`：

```python
import os
import sqlite3
import tempfile
import unittest

import db


class SchemaMigrationTest(unittest.TestCase):
    def setUp(self):
        # 用临时目录隔离 DB
        self.tmp = tempfile.mkdtemp()
        self._orig_data_dir = db.DATA_DIR
        self._orig_db_path = db.DB_PATH
        self._orig_pdf_dir = db.PDF_DIR
        db.DATA_DIR = self.tmp
        db.DB_PATH = os.path.join(self.tmp, "test.db")
        db.PDF_DIR = os.path.join(self.tmp, "pdfs")

    def tearDown(self):
        db.DATA_DIR = self._orig_data_dir
        db.DB_PATH = self._orig_db_path
        db.PDF_DIR = self._orig_pdf_dir

    def _columns(self, table):
        c = sqlite3.connect(db.DB_PATH)
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
        c.close()
        return cols

    def test_init_creates_new_columns(self):
        db.init()
        cols = self._columns("accounts")
        for name in ("last_uid", "fetch_mode", "default_since", "keywords_override"):
            self.assertIn(name, cols, f"缺少列 {name}")

    def test_init_idempotent(self):
        db.init()
        db.init()  # 再跑一次不应报错
        cols = self._columns("accounts")
        self.assertIn("last_uid", cols)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 运行测试，确认失败**

```
$PY -m unittest tests.test_db -v
```
Expected: FAIL（`last_uid` 不在 accounts 表列里）

- [ ] **Step 4: 修改 SCHEMA 加 4 列**

在 `db.py` 的 `SCHEMA` 字符串里，accounts 表定义中 `last_fetch  TEXT,` 之后、`created_at  TEXT DEFAULT CURRENT_TIMESTAMP` 之前插入 4 列：

```python
  last_fetch  TEXT,
  last_uid          INTEGER,
  fetch_mode        TEXT DEFAULT 'incremental',
  default_since     TEXT DEFAULT '90d',
  keywords_override TEXT,
  created_at  TEXT DEFAULT CURRENT_TIMESTAMP
```

- [ ] **Step 5: 修改 init() 加幂等迁移逻辑**

把 `init()` 改为：

```python
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
```

- [ ] **Step 6: 运行测试，确认通过**

```
$PY -m unittest tests.test_db.SchemaMigrationTest -v
```
Expected: PASS

- [ ] **Step 7: 提交**

```
git add db.py tests/__init__.py tests/test_db.py
git commit -m "feat(db): accounts 表加 last_uid/fetch_mode/default_since/keywords_override 列 + 幂等迁移"
```

---

## Task 2: DB 辅助函数 update_last_uid / get_max_uid_for_account

**Files:**
- Modify: `db.py` 新增两个函数
- Test: `tests/test_db.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_db.py` 加：

```python
class LastUidTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = (db.DATA_DIR, db.DB_PATH, db.PDF_DIR)
        db.DATA_DIR = self.tmp
        db.DB_PATH = os.path.join(self.tmp, "test.db")
        db.PDF_DIR = os.path.join(self.tmp, "pdfs")
        db.init()
        db.upsert_account({"name": "a", "email": "a@x.com", "password": "p",
                           "imap_host": "h", "imap_port": 993, "use_ssl": 1,
                           "folder": "INBOX", "enabled": 1})
        self.acc_id = db.get_accounts()[0]["id"]

    def tearDown(self):
        db.DATA_DIR, db.DB_PATH, db.PDF_DIR = self._orig

    def test_update_and_read_last_uid(self):
        self.assertIsNone(db.get_account(self.acc_id)["last_uid"])
        db.update_last_uid(self.acc_id, 12345)
        self.assertEqual(db.get_account(self.acc_id)["last_uid"], 12345)

    def test_update_last_uid_none(self):
        db.update_last_uid(self.acc_id, 100)
        db.update_last_uid(self.acc_id, None)
        self.assertIsNone(db.get_account(self.acc_id)["last_uid"])

    def test_get_max_uid_for_account_no_emails(self):
        self.assertIsNone(db.get_max_uid_for_account(self.acc_id))

    def test_get_max_uid_for_account_with_emails(self):
        db.insert_email({"account_id": self.acc_id, "uid": "10", "subject": "s",
                         "from_addr": "f", "date": "d", "body_text": "",
                         "body_html": "", "is_invoice": 0})
        db.insert_email({"account_id": self.acc_id, "uid": "30", "subject": "s",
                         "from_addr": "f", "date": "d", "body_text": "",
                         "body_html": "", "is_invoice": 1})
        # emails.uid 是 TEXT，按数字比较需 CAST
        self.assertEqual(db.get_max_uid_for_account(self.acc_id), 30)
```

- [ ] **Step 2: 运行测试，确认失败**

```
$PY -m unittest tests.test_db.LastUidTest -v
```
Expected: FAIL（`module 'db' has no attribute 'update_last_uid'`）

- [ ] **Step 3: 实现 update_last_uid 与 get_max_uid_for_account**

在 `db.py` 的 `set_account_fetch` 函数之后加：

```python
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
```

- [ ] **Step 4: 运行测试，确认通过**

```
$PY -m unittest tests.test_db.LastUidTest -v
```
Expected: PASS

- [ ] **Step 5: 提交**

```
git add db.py tests/test_db.py
git commit -m "feat(db): 新增 update_last_uid / get_max_uid_for_account"
```

---

## Task 3: DB upsert_account / update_account 支持新字段

**Files:**
- Modify: `db.py` 的 `upsert_account` 与 `update_account`
- Test: `tests/test_db.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_db.py` 加：

```python
class AccountPrefsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = (db.DATA_DIR, db.DB_PATH, db.PDF_DIR)
        db.DATA_DIR = self.tmp
        db.DB_PATH = os.path.join(self.tmp, "test.db")
        db.PDF_DIR = os.path.join(self.tmp, "pdfs")
        db.init()

    def tearDown(self):
        db.DATA_DIR, db.DB_PATH, db.PDF_DIR = self._orig

    def test_upsert_account_stores_prefs(self):
        db.upsert_account({
            "name": "a", "email": "a@x.com", "password": "p",
            "imap_host": "h", "imap_port": 993, "use_ssl": 1,
            "folder": "INBOX", "enabled": 1,
            "fetch_mode": "full", "default_since": "2026-01-01",
            "keywords_override": '["发票","行程单"]',
        })
        a = db.get_accounts()[0]
        self.assertEqual(a["fetch_mode"], "full")
        self.assertEqual(a["default_since"], "2026-01-01")
        self.assertEqual(a["keywords_override"], '["发票","行程单"]')

    def test_update_account_changes_prefs(self):
        db.upsert_account({
            "name": "a", "email": "a@x.com", "password": "p",
            "imap_host": "h", "imap_port": 993, "use_ssl": 1,
            "folder": "INBOX", "enabled": 1,
        })
        acc_id = db.get_accounts()[0]["id"]
        db.update_account({
            "id": acc_id, "name": "a", "email": "a@x.com", "provider": None,
            "imap_host": "h", "imap_port": 993, "use_ssl": 1, "folder": "INBOX",
            "fetch_mode": "incremental", "default_since": "30d",
            "keywords_override": None,
        })
        a = db.get_account(acc_id)
        self.assertEqual(a["fetch_mode"], "incremental")
        self.assertEqual(a["default_since"], "30d")
        self.assertIsNone(a["keywords_override"])
```

- [ ] **Step 2: 运行测试，确认失败**

```
$PY -m unittest tests.test_db.AccountPrefsTest -v
```
Expected: FAIL（upsert 没存 fetch_mode 等）

- [ ] **Step 3: 修改 upsert_account 加新字段**

把 `upsert_account` 改为：

```python
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
```

- [ ] **Step 4: 修改 update_account 加新字段**

把 `update_account` 改为：

```python
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
```

- [ ] **Step 5: 运行测试，确认通过**

```
$PY -m unittest tests.test_db.AccountPrefsTest -v
```
Expected: PASS

- [ ] **Step 6: 提交**

```
git add db.py tests/test_db.py
git commit -m "feat(db): upsert/update_account 支持 fetch_mode/default_since/keywords_override"
```

---

## Task 4: DB delete_invoices 改造（三件套 + 水位线回退）

**Files:**
- Modify: `db.py` 的 `delete_invoices`
- Test: `tests/test_db.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_db.py` 顶部 import 加 `import json`，并加测试类：

```python
class DeleteInvoicesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = (db.DATA_DIR, db.DB_PATH, db.PDF_DIR, db.HERE)
        db.DATA_DIR = self.tmp
        db.DB_PATH = os.path.join(self.tmp, "test.db")
        db.PDF_DIR = os.path.join(self.tmp, "pdfs")
        db.HERE = self.tmp
        os.makedirs(db.PDF_DIR, exist_ok=True)
        db.init()
        db.upsert_account({"name": "a", "email": "a@x.com", "password": "p",
                           "imap_host": "h", "imap_port": 993, "use_ssl": 1,
                           "folder": "INBOX", "enabled": 1})
        self.acc_id = db.get_accounts()[0]["id"]
        # 建一封 email + 对应 invoice + PDF 文件
        db.insert_email({"account_id": self.acc_id, "uid": "100", "subject": "s",
                         "from_addr": "f", "date": "d", "body_text": "",
                         "body_html": "", "is_invoice": 1})
        self.email_id = db.conn().execute(
            "SELECT id FROM emails WHERE account_id=?", (self.acc_id,)).fetchone()["id"]
        self.pdf_rel = "pdfs/test.pdf"
        self.pdf_abs = os.path.join(self.tmp, self.pdf_rel)
        with open(self.pdf_abs, "wb") as f:
            f.write(b"%PDF-1.4 fake")
        db.insert_invoice({"email_id": self.email_id, "account_id": self.acc_id,
                           "buyer": "b", "seller": "s", "amount": 1.0,
                           "invoice_no": "INV001", "invoice_date": "2026-07-01",
                           "city": "", "pdf_path": self.pdf_rel,
                           "source_type": "attachment", "note": ""})
        self.inv_id = db.conn().execute(
            "SELECT id FROM invoices WHERE invoice_no='INV001'").fetchone()["id"]

    def tearDown(self):
        db.DATA_DIR, db.DB_PATH, db.PDF_DIR, db.HERE = self._orig

    def test_delete_removes_invoice_pdf_and_email(self):
        n = db.delete_invoices([self.inv_id])
        self.assertEqual(n, 1)
        # invoice 没了
        self.assertIsNone(db.get_invoice_by_id(self.inv_id))
        # PDF 文件没了
        self.assertFalse(os.path.isfile(self.pdf_abs))
        # email 没了（无其他 invoice 引用）
        c = db.conn()
        r = c.execute("SELECT 1 FROM emails WHERE id=?", (self.email_id,)).fetchone()
        c.close()
        self.assertIsNone(r)

    def test_delete_rolls_back_last_uid(self):
        db.update_last_uid(self.acc_id, 100)
        db.delete_invoices([self.inv_id])
        # email 全删空 -> last_uid 回退为 None
        self.assertIsNone(db.get_account(self.acc_id)["last_uid"])

    def test_delete_keeps_email_when_other_invoice_refs(self):
        # 同一 email 再加一条 invoice
        db.insert_invoice({"email_id": self.email_id, "account_id": self.acc_id,
                           "buyer": "b2", "seller": "s2", "amount": 2.0,
                           "invoice_no": "INV002", "invoice_date": "2026-07-02",
                           "city": "", "pdf_path": "", "source_type": "attachment",
                           "note": ""})
        inv2 = db.conn().execute(
            "SELECT id FROM invoices WHERE invoice_no='INV002'").fetchone()["id"]
        db.update_last_uid(self.acc_id, 100)
        db.delete_invoices([self.inv_id])
        # email 保留（还有 INV002 引用）
        c = db.conn()
        r = c.execute("SELECT 1 FROM emails WHERE id=?", (self.email_id,)).fetchone()
        c.close()
        self.assertIsNotNone(r)
        # last_uid 不动
        self.assertEqual(db.get_account(self.acc_id)["last_uid"], 100)

    def test_delete_seed_invoice_no_email(self):
        # email_id=NULL 的本地发票，只删 invoice + PDF
        db.insert_invoice({"email_id": None, "account_id": self.acc_id,
                           "buyer": "b3", "seller": "s3", "amount": 3.0,
                           "invoice_no": "INV003", "invoice_date": "2026-07-03",
                           "city": "", "pdf_path": self.pdf_rel,
                           "source_type": "seed", "note": ""})
        inv3 = db.conn().execute(
            "SELECT id FROM invoices WHERE invoice_no='INV003'").fetchone()["id"]
        db.delete_invoices([inv3])
        self.assertIsNone(db.get_invoice_by_id(inv3))
```

- [ ] **Step 2: 运行测试，确认失败**

```
$PY -m unittest tests.test_db.DeleteInvoicesTest -v
```
Expected: FAIL（PDF 文件仍在 / email 仍在 / last_uid 未回退）

- [ ] **Step 3: 重写 delete_invoices**

把 `db.py` 的 `delete_invoices` 替换为：

```python
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
```

- [ ] **Step 4: 确认 db.py 顶部已 import os**

检查 `db.py` 顶部有 `import os`（原有，应已存在）。若无则补加。

- [ ] **Step 5: 运行测试，确认通过**

```
$PY -m unittest tests.test_db.DeleteInvoicesTest -v
```
Expected: PASS

- [ ] **Step 6: 运行全部 db 测试确保无回归**

```
$PY -m unittest tests.test_db -v
```
Expected: 全部 PASS

- [ ] **Step 7: 提交**

```
git add db.py tests/test_db.py
git commit -m "feat(db): delete_invoices 彻底删本地三件套 + 重算水位线"
```

---

## Task 5: engine 新增 parse_since_expr

**Files:**
- Modify: `engine.py` 新增函数
- Test: `tests/test_engine.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_engine.py`：

```python
import datetime as dt
import unittest
from unittest.mock import patch

import engine


class ParseSinceExprTest(unittest.TestCase):
    def test_relative_days(self):
        with patch("engine.dt") as m_dt:
            m_dt.datetime.now.return_value = dt.datetime(2026, 7, 15)
            m_dt.timedelta = dt.timedelta
            m_dt.datetime.strptime = dt.datetime.strptime
            # dt 模块属性需透传
            import datetime as _dt
            m_dt.date = _dt.date
            self.assertEqual(engine.parse_since_expr("90d"), "16-Apr-2026")

    def test_absolute_date(self):
        self.assertEqual(engine.parse_since_expr("2026-07-01"), "01-Jul-2026")

    def test_empty_fallback(self):
        self.assertEqual(engine.parse_since_expr(None), "01-Jan-2000")
        self.assertEqual(engine.parse_since_expr(""), "01-Jan-2000")

    def test_invalid_falls_back(self):
        # 非法表达式兜底
        self.assertEqual(engine.parse_since_expr("abc"), "01-Jan-2000")


if __name__ == "__main__":
    unittest.main()
```

> 注：`parse_since_expr` 内部直接用 `datetime` 模块而非 patch 全局 `dt` alias，可简化测试。下面的实现用标准 `datetime` 模块，测试改为直接断言（无需 patch）。若按下方实现，把上面 `test_relative_days` 替换为：

```python
    def test_relative_days(self):
        # 用固定 today 计算：90 天前
        import datetime as _dt
        fixed = _dt.date(2026, 7, 15)
        result = engine.parse_since_expr("90d", today=fixed)
        self.assertEqual(result, "16-Apr-2026")
```

最终测试文件采用带 `today` 参数的版本（见 Step 2 实现签名）：

```python
import datetime as dt
import unittest

import engine


class ParseSinceExprTest(unittest.TestCase):
    def test_relative_days(self):
        fixed = dt.date(2026, 7, 15)
        self.assertEqual(engine.parse_since_expr("90d", today=fixed), "16-Apr-2026")

    def test_absolute_date(self):
        self.assertEqual(engine.parse_since_expr("2026-07-01"), "01-Jul-2026")

    def test_empty_fallback(self):
        self.assertEqual(engine.parse_since_expr(None), "01-Jan-2000")
        self.assertEqual(engine.parse_since_expr(""), "01-Jan-2000")

    def test_invalid_falls_back(self):
        self.assertEqual(engine.parse_since_expr("abc"), "01-Jan-2000")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试，确认失败**

```
$PY -m unittest tests.test_engine -v
```
Expected: FAIL（`module 'engine' has no attribute 'parse_since_expr'`）

- [ ] **Step 3: 实现 parse_since_expr**

在 `engine.py` 的 `imap_date` 函数之后加：

```python
def parse_since_expr(expr, today=None):
    """解析时间范围表达式，返回 IMAP SINCE 需要的 'DD-Mon-YYYY'。
    - '90d' / '30d'：相对今天往前 N 天
    - '2026-07-01'：绝对日期
    - None / '' / 非法：兜底 '01-Jan-2000'（拉尽量早）
    """
    today = today or dt.date.today()
    if not expr:
        return imap_date("2000-01-01")
    expr = str(expr).strip()
    m = re.fullmatch(r"(\d+)d", expr, re.I)
    if m:
        days = int(m.group(1))
        d = today - dt.timedelta(days=days)
        return f"{d.day:02d}-{_IMAP_MONTHS[d.month-1]}-{d.year}"
    try:
        # 绝对日期
        return imap_date(expr)
    except Exception:
        return imap_date("2000-01-01")
```

- [ ] **Step 4: 运行测试，确认通过**

```
$PY -m unittest tests.test_engine.ParseSinceExprTest -v
```
Expected: PASS

- [ ] **Step 5: 提交**

```
git add engine.py tests/test_engine.py
git commit -m "feat(engine): 新增 parse_since_expr 支持 'Nd' 与绝对日期表达式"
```

---

## Task 6: engine fetch_account 改造（UID 语义 + 水位线 + 关键词覆盖）

**Files:**
- Modify: `engine.py` 的 `fetch_account`

> 本任务涉及 IMAP 真实连接，难单测。采用结构化改造 + 手动验证。

- [ ] **Step 1: 改造 fetch_account**

把 `engine.py` 的 `fetch_account` 替换为：

```python
def fetch_account(acc, rules, session, since_override=None):
    """拉一个邮箱的发票邮件 → 下载 PDF → 解析 → 入库。返回新增发票数。
    - 统一用 IMAP UID 语义（uid search / uid fetch），UID 单调稳定。
    - fetch_mode='incremental'（默认）：只处理 uid > last_uid 的邮件。
    - fetch_mode='full'：拉 default_since 范围内全部，email_exists 兜底去重。
    - 不动邮箱状态（不 STORE \Seen、不删邮件）。"""
    last_uid = acc.get("last_uid") or 0
    mode = acc.get("fetch_mode") or "incremental"
    since_expr = since_override or acc.get("default_since") or "90d"
    since_imap = parse_since_expr(since_expr)

    # 账号级关键词覆盖（NULL 时用全局 rules）
    keywords = (
        json.loads(acc["keywords_override"])
        if acc.get("keywords_override")
        else rules.get("invoice_keywords", [])
    )

    M = connect(acc)
    typ, data = M.uid("search", None, "SINCE", since_imap)
    if typ != "OK":
        M.logout()
        return 0
    uids = [int(u) for u in data[0].split()]

    # 增量模式：水位线过滤
    if mode == "incremental" and last_uid:
        uids = [u for u in uids if u > last_uid]

    uids = uids[-200:]  # 保留上限，防水位线丢失后一次拉太多
    acc_dir = os.path.join(db.PDF_DIR, safe_name(acc["email"]))
    os.makedirs(acc_dir, exist_ok=True)
    new_inv = 0
    new_max_uid = last_uid

    for uid in reversed(uids):
        uid_str = str(uid)
        if db.email_exists(acc["id"], uid_str):
            continue
        typ, data = M.uid("fetch", str(uid), "(RFC822)")
        if typ != "OK":
            continue
        msg = email.message_from_bytes(data[0][1])
        subject = decode_mime(msg.get("Subject"))
        sender = decode_mime(msg.get("From"))
        date = decode_mime(msg.get("Date"))
        if not match_invoice(subject, keywords):
            # 非发票邮件也存一条（is_invoice=0）以便审计，但跳过解析
            db.insert_email({"account_id": acc["id"], "uid": uid_str, "subject": subject,
                            "from_addr": sender, "date": date, "body_text": "",
                            "body_html": "", "is_invoice": 0})
            new_max_uid = max(new_max_uid, uid)
            continue

        html, text = extract_body(msg)
        db.insert_email({"account_id": acc["id"], "uid": uid_str, "subject": subject,
                        "from_addr": sender, "date": date, "body_text": text,
                        "body_html": html, "is_invoice": 1})

        # 收集 PDF：附件 + 正文链接
        pdfs = []
        for i, (fname, payload) in enumerate(get_attachments(msg), 1):
            p = os.path.join(acc_dir, f"{uid_str}_{i}_{safe_name(fname)}")
            with open(p, "wb") as f:
                f.write(payload)
            pdfs.append((p, "attachment"))
        if not pdfs:
            for j, (url, _label) in enumerate(find_pdf_links(html, text)[:3], 1):
                p = os.path.join(acc_dir, f"{uid_str}_link{j}.pdf")
                if try_download_pdf(url, session, p):
                    pdfs.append((p, "link"))
                    break

        for p, stype in pdfs:
            inv = parse_pdf_to_invoice(p, acc["id"], email_id=None, source_type=stype)
            if db.insert_invoice(inv):
                new_inv += 1
        new_max_uid = max(new_max_uid, uid)

    M.logout()
    # 推进水位线
    if new_max_uid > last_uid:
        db.update_last_uid(acc["id"], new_max_uid)
    return new_inv
```

- [ ] **Step 2: 检查 imports**

确认 `engine.py` 顶部已有 `import re`、`import json`、`import datetime as dt`（原文件有，无需改）。

- [ ] **Step 3: 手动验证 — 抓取不报错**

```
$PY hub.py init
$PY hub.py accounts
$PY hub.py fetch
```
Expected: 不报 AttributeError / 不因 uid 方法失败。日志显示"新增发票 N 张"。

- [ ] **Step 4: 提交**

```
git add engine.py
git commit -m "feat(engine): fetch_account 统一 UID 语义 + 水位线增量过滤 + 关键词覆盖"
```

---

## Task 7: engine fetch_all / fetch_one 签名调整

**Files:**
- Modify: `engine.py` 的 `fetch_all` 与 `fetch_one`

- [ ] **Step 1: 改造 fetch_all**

把 `fetch_all` 替换为：

```python
def fetch_all(since_override=None, acc_id=None):
    """抓取全部启用账号。since_override 为可选临时覆盖（不写回配置）。"""
    global FETCH_RUNNING, FETCH_LAST_TS
    FETCH_RUNNING = True
    FETCH_LOG.clear()
    FETCH_LAST_TS = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        rules = load_rules()
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 invoice-hub"})
        total = 0
        for acc in db.get_accounts(enabled_only=True):
            if acc_id and acc["id"] != acc_id:
                continue
            if not acc.get("imap_host"):
                log(f"\n=== 邮箱: {acc['name']} ({acc['email']}) ===")
                log(f"  [跳过] 非 IMAP 账号（未配置 IMAP 主机，仅用于本地发票，跳过抓取）")
                continue
            log(f"\n=== 邮箱: {acc['name']} ({acc['email']}) ===")
            try:
                n = fetch_account(acc, rules, session, since_override=since_override)
                db.set_account_fetch(acc["id"], dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
                log(f"  新增发票 {n} 张")
                total += n
            except Exception as e:
                log(f"  [失败] {e}")
        log(f"\n全部完成，本次新增 {total} 张发票。")
        return total
    finally:
        FETCH_RUNNING = False
```

- [ ] **Step 2: 改造 fetch_one**

把 `fetch_one` 替换为：

```python
def fetch_one(acc_id, since_override=None):
    """抓单个账号（供 Web 单账号按钮调用）。"""
    acc = db.get_account(acc_id)
    if not acc:
        log(f"[跳过] 账号 #{acc_id} 不存在")
        return 0
    if not acc.get("enabled"):
        log(f"[跳过] 账号 {acc['email']} 已停用")
        return 0
    return fetch_account(acc, load_rules(), requests.Session(),
                         since_override=since_override)
```

- [ ] **Step 3: 手动验证**

```
$PY hub.py fetch
```
Expected: 正常运行，无签名错误。

- [ ] **Step 4: 提交**

```
git add engine.py
git commit -m "feat(engine): fetch_all/fetch_one 支持 since_override 覆盖，去掉硬编码 since"
```

---

## Task 8: hub.py cmd_fetch 适配

**Files:**
- Modify: `hub.py` 的 `cmd_fetch` 与 argparse `--since`

- [ ] **Step 1: 改 cmd_fetch**

把 `hub.py` 的 `cmd_fetch` 改为：

```python
def cmd_fetch(args):
    db.init()
    since_override = args.since  # None 或 '2026-07-01' 形式的临时覆盖
    if since_override:
        print(f"开始抓取（临时覆盖 since={since_override}，各账号偏好被忽略）...")
    else:
        print("开始抓取（各账号用自身 fetch_mode/default_since 偏好）...")
    engine.fetch_all(since_override=since_override)
```

- [ ] **Step 2: 改 argparse 默认值**

把 `hub.py` 里 `p_fetch.add_argument("--since", default="2026-07-09")` 改为：

```python
    p_fetch.add_argument("--since", default=None,
                         help="临时覆盖抓取起始日期（如 2026-07-01 或 90d）；留空用各账号偏好")
```

- [ ] **Step 3: 手动验证**

```
$PY hub.py fetch --help
```
Expected: `--since` 帮助显示新说明，default=None。

```
$PY hub.py fetch
```
Expected: 提示"各账号用自身偏好"，正常抓取。

- [ ] **Step 4: 提交**

```
git add hub.py
git commit -m "feat(hub): cmd_fetch 支持可选 --since 覆盖，默认读账号偏好"
```

---

## Task 9: web/app.py 去硬编码 + 接收新字段

**Files:**
- Modify: `web/app.py`

- [ ] **Step 1: 去掉自动抓取硬编码 since**

把 `web/app.py` 里（约第 94-96 行）自动抓取调用的 `args=("2026-07-01",)` 改为：

```python
                threading.Thread(target=do_fetch_job, args=(None,), daemon=True).start()
```

- [ ] **Step 2: 改 do_fetch_job 透传 since_override**

把 `do_fetch_job` 改为：

```python
def do_fetch_job(since_override=None, acc_id=None):
    engine.FETCH_RUNNING = True
    try:
        if acc_id:
            engine.fetch_one(acc_id, since_override=since_override)
        else:
            engine.fetch_all(since_override=since_override)
    finally:
        engine.FETCH_RUNNING = False
```

- [ ] **Step 3: 改单账号抓取路由去硬编码**

把 GET 与 POST 里 `path.endswith("/fetch")` 的两处（约 158-161、307-310 行），把 `args=("2026-07-09", acc_id)` 改为：

```python
            threading.Thread(target=do_fetch_job, args=(None, acc_id), daemon=True).start()
```

- [ ] **Step 4: 改立即抓取全部路由读 since**

把 `path == "/api/fetch"` 的 POST 处理（约 314-318 行）改为：

```python
        if path == "/api/fetch":
            since = payload.get("since") or None
            threading.Thread(target=do_fetch_job, args=(since,), daemon=True).start()
            _json(self, {"ok": True, "msg": "已启动抓取"})
            return
```

- [ ] **Step 5: 账号保存路由接收新字段**

找到 `web/app.py` 里处理账号保存的 POST 路由（构造 `body` dict 传给 `db.upsert_account` / `db.update_account` 的地方），加入 3 个字段。在 body dict 里补：

```python
            "fetch_mode": payload.get("fetch_mode") or "incremental",
            "default_since": payload.get("default_since") or "90d",
            "keywords_override": payload.get("keywords_override") or None,
```

> 若 body 是分 GET（新增用 upsert）/POST（编辑用 update）两条路径，两处都加。

- [ ] **Step 6: 手动验证**

```
$PY hub.py serve
```
浏览器打开 http://127.0.0.1:8000 ：
- 「邮箱账号」Tab → 添加/编辑账号 → 调用保存，检查后端不报错（可在终端看日志）
- 「抓取任务」Tab → 立即抓取全部 → 检查不传 since 时按账号偏好抓取

- [ ] **Step 7: 提交**

```
git add web/app.py
git commit -m "feat(web): 去硬编码 since + 账号 API 接收偏好字段"
```

---

## Task 10: web/index.html 账号模态框 + 抓取 Tab + 卡片标签

**Files:**
- Modify: `web/index.html`

- [ ] **Step 1: 账号模态框加 3 个偏好字段**

在 `index.html` 账号模态框（`e_folder` 那行之后）加：

```html
      <div><label for="e_fetch_mode">抓取模式</label>
        <select class="grow" id="e_fetch_mode">
          <option value="incremental">增量（只拉新邮件）</option>
          <option value="full">全量（按时间范围重扫）</option>
        </select>
      </div>
      <div><label for="e_default_since">默认时间范围</label>
        <input class="grow" id="e_default_since" placeholder="90d 或 2026-07-01（留空=90d）">
      </div>
      <div><label for="e_keywords">发票关键词覆盖</label>
        <input class="grow" id="e_keywords" placeholder="发票,行程单（空=用全局）">
      </div>
```

- [ ] **Step 2: openAccModal 清空新字段**

在 `openAccModal`（约 629 行，重置字段的循环）后补：

```javascript
  document.getElementById('e_fetch_mode').value = 'incremental';
  document.getElementById('e_default_since').value = '';
  document.getElementById('e_keywords').value = '';
```

- [ ] **Step 3: editAcc 回填新字段**

在 `editAcc`（约 663 行，`e_folder` 赋值后）补：

```javascript
  document.getElementById('e_fetch_mode').value = a.fetch_mode || 'incremental';
  document.getElementById('e_default_since').value = a.default_since || '';
  document.getElementById('e_keywords').value = a.keywords_override
    ? JSON.parse(a.keywords_override).join(',') : '';
```

- [ ] **Step 4: saveAcc 提交新字段**

在 `saveAcc` 的 `body` dict（约 692-699 行）补：

```javascript
    fetch_mode: document.getElementById('e_fetch_mode').value,
    default_since: document.getElementById('e_default_since').value.trim() || '90d',
    keywords_override: document.getElementById('e_keywords').value.trim()
      ? JSON.stringify(document.getElementById('e_keywords').value.split(',').map(s=>s.trim()).filter(Boolean))
      : null,
```

- [ ] **Step 5: 抓取 Tab 加可选 since 输入**

在「立即抓取全部」按钮旁（约 303 行）加输入框：

```html
        <input id="fetchSinceInput" placeholder="可选 since，如 30d 或 2026-07-01（留空=各账号偏好）"
               style="padding:6px 10px;border:1px solid #ddd;border-radius:6px;width:300px;margin-left:8px">
```

- [ ] **Step 6: fetchAll 读 since 输入**

把 `fetchAll`（约 740 行）改为：

```javascript
async function fetchAll(){
  const since = document.getElementById('fetchSinceInput').value.trim() || null;
  await API('/api/fetch', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify(since ? {since} : {})});
  document.querySelector('.tab-btn[data-tab="fetch"]').click();
}
```

- [ ] **Step 7: 卡片显示偏好标签**

在账号卡片渲染（约 614 行，"最近抓取"那行后）补偏好摘要：

```javascript
        <span class="stat">${a.fetch_mode==='full'?'全量':'增量'} · ${a.default_since||'90d'}</span>
```

- [ ] **Step 8: 手动验证**

```
$PY hub.py serve
```
浏览器验证：
- 账号模态框：3 个新字段出现，保存后编辑回填正确
- 抓取 Tab：since 输入框出现，留空抓取正常，填值抓取正常
- 账号卡片：显示"增量 · 90d"标签

- [ ] **Step 9: 提交**

```
git add web/index.html
git commit -m "feat(web): 账号模态框加偏好字段 + 抓取 Tab 加可选 since + 卡片偏好标签"
```

---

## 完成验证（全部任务后）

- [ ] **运行全部测试**

```
$PY -m unittest discover -s tests -v
```
Expected: 全部 PASS

- [ ] **端到端手动验证**

```
$PY hub.py init
$PY hub.py fetch
$PY hub.py serve
```
1. 添加账号时能配抓取模式/时间范围/关键词
2. 抓取按账号偏好执行，无硬编码日期
3. 删除发票后本地 PDF 消失，重新抓取能拉回该邮件
4. 邮箱客户端里邮件仍为未读（确认未动邮箱状态）

---

## Self-Review 记录

**1. Spec 覆盖：**
- 数据模型 4 列 → Task 1 ✓
- update_last_uid / get_max_uid_for_account → Task 2 ✓
- upsert/update 新字段 → Task 3 ✓
- delete_invoices 三件套 + 水位线回退 → Task 4 ✓
- parse_since_expr → Task 5 ✓
- fetch_account UID + 水位线 + 关键词 → Task 6 ✓
- fetch_all/fetch_one 签名 → Task 7 ✓
- hub.py CLI → Task 8 ✓
- web/app.py 去硬编码 + API → Task 9 ✓
- web/index.html UI → Task 10 ✓
- 兼容性（CLI --since 保留）→ Task 8 ✓

**2. 占位符扫描：** 无 TBD/TODO，每步含完整代码。

**3. 类型一致性：** `update_last_uid(acc_id, uid)` / `get_max_uid_for_account(acc_id)` 在 Task 2 定义、Task 4/6 调用，签名一致。`fetch_account(acc, rules, session, since_override=None)` 在 Task 6 定义、Task 7 调用，一致。`parse_since_expr(expr, today=None)` 一致。
