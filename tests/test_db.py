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
