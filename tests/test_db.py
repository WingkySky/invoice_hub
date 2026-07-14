import json
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


class LastUidTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = (db.DATA_DIR, db.DB_PATH, db.PDF_DIR)
        db.DATA_DIR = self.tmp
        db.DB_PATH = os.path.join(self.tmp, "test.db")
        db.PDF_DIR = os.path.join(self.tmp, "pdfs")
        db.init()
        db.upsert_account({"name": "a", "email": "a@x.com", "password": "p",
                           "provider": None, "imap_host": "h", "imap_port": 993,
                           "use_ssl": 1, "folder": "INBOX", "enabled": 1})
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


if __name__ == "__main__":
    unittest.main()
