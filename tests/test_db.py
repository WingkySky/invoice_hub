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


if __name__ == "__main__":
    unittest.main()
