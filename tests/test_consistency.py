"""
一致性测试：
  1. 删除发票 → 级联删除本地 PDF 文件（满足"表没数据，文件也不应存在"）
  2. needs_refetch 幂等/自愈判定：齐全则跳过，缺失则重拉
  3. reconcile 把磁盘孤儿 PDF 回填进发票表
"""
import os
import sys
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import db
import engine

# 用临时目录隔离，不污染真实 data/
_TMP = tempfile.mkdtemp(prefix="invhub_test_")
db.HERE = _TMP
db.DATA_DIR = os.path.join(_TMP, "data")
db.DB_PATH = os.path.join(db.DATA_DIR, "invoice_hub.db")
db.PDF_DIR = os.path.join(db.DATA_DIR, "pdfs")
db.init()


def _make_pdf(path, text):
    """用 PyMuPDF 写一张含指定文本的 PDF（保证可被解析）。
    china-s 是内置简体中文字体，否则中文会被写成占位点导致解析失败。
    长文本按词换行，避免单行超出页宽被截断、导致后面的发票号/关键词丢失。"""
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) > 28:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    y = 60
    for ln in lines:
        page.insert_text((50, y), ln, fontname="china-s")
        y += 20
    doc.save(path)
    doc.close()


def _sample_text():
    return ("购买方 名称: 广州市友谊对外服务有限公司 统一社会信用代码 91120100XXX "
            "销售方 名称: 天津晨景信息技术有限公司 统一社会信用代码 91120100YYY "
            "价税合计 ¥4698.97 发票号码 26122000000924510046 开票日期 2026年07月09日")


class ConsistencyTest(unittest.TestCase):

    def setUp(self):
        # 每个用例前清空表
        c = db.conn()
        c.executescript("DELETE FROM invoices; DELETE FROM emails; DELETE FROM accounts;")
        c.commit()
        c.close()
        # 准备一个账号 + 它的 PDF 目录
        db.upsert_account({"name": "测试", "email": "t@163.com",
                            "imap_host": "imap.163.com", "imap_port": 993,
                            "use_ssl": 1, "folder": "INBOX", "password": "x", "enabled": 1})
        self.acc = db.get_accounts()[0]
        self.acc_dir = os.path.join(db.PDF_DIR, db._safe_dir(self.acc["email"]))
        os.makedirs(self.acc_dir, exist_ok=True)

    def tearDown(self):
        if os.path.isdir(self.acc_dir):
            shutil.rmtree(self.acc_dir, ignore_errors=True)

    # 1) 级联删除 --------------------------------------------------
    def test_cascade_delete_removes_file(self):
        pdf = os.path.join(self.acc_dir, "10_link1.pdf")
        _make_pdf(pdf, _sample_text())
        self.assertTrue(os.path.isfile(pdf))
        db.insert_email({"account_id": self.acc["id"], "uid": "10", "subject": "发票",
                         "from_addr": "a", "date": "2026-07-09", "body_text": "",
                         "body_html": "", "is_invoice": 1})
        eid = db.get_email_id(self.acc["id"], "10")
        inv = engine.parse_pdf_to_invoice(pdf, self.acc["id"], email_id=eid, source_type="link")
        self.assertTrue(db.insert_invoice(inv))
        self.assertEqual(db.get_invoices_count(), 1)
        # 删除该发票 → 文件应被级联删除
        db.delete_invoices([inv_id_for(self.acc["id"], "26122000000924510046")])
        self.assertFalse(os.path.isfile(pdf), "删除发票后 PDF 应被级联删除")
        self.assertEqual(db.get_invoices_count(), 0)

    # 2) needs_refetch 判定 ------------------------------------------
    def test_needs_refetch_present_skips(self):
        pdf = os.path.join(self.acc_dir, "11_link1.pdf")
        _make_pdf(pdf, _sample_text())
        db.insert_email({"account_id": self.acc["id"], "uid": "11", "subject": "发票",
                         "from_addr": "a", "date": "2026-07-09", "body_text": "",
                         "body_html": "", "is_invoice": 1})
        eid = db.get_email_id(self.acc["id"], "11")
        inv = engine.parse_pdf_to_invoice(pdf, self.acc["id"], email_id=eid, source_type="link")
        db.insert_invoice(inv)
        # 发票+文件都齐全 → 不需要重拉
        self.assertFalse(db.needs_refetch(self.acc["id"], "11"),
                         "已齐全的发票不应被判定为重拉")

    def test_needs_refetch_missing_file_refetches(self):
        pdf = os.path.join(self.acc_dir, "12_link1.pdf")
        _make_pdf(pdf, _sample_text())
        db.insert_email({"account_id": self.acc["id"], "uid": "12", "subject": "发票",
                         "from_addr": "a", "date": "2026-07-09", "body_text": "",
                         "body_html": "", "is_invoice": 1})
        eid = db.get_email_id(self.acc["id"], "12")
        inv = engine.parse_pdf_to_invoice(pdf, self.acc["id"], email_id=eid, source_type="link")
        db.insert_invoice(inv)
        os.remove(pdf)  # 模拟文件丢失
        # 发票行在、但文件没了 → 应判定为重拉以恢复
        self.assertTrue(db.needs_refetch(self.acc["id"], "12"),
                        "发票在但文件丢失应判定为重拉恢复")

    def test_needs_refetch_new_email(self):
        # 邮件根本不存在 → 需重拉
        self.assertTrue(db.needs_refetch(self.acc["id"], "999"))

    # 3) reconcile 回填孤儿 PDF ------------------------------------
    def test_reconcile_reingests_orphan_pdf(self):
        # 磁盘上有一张 PDF，但没进发票表（模拟"下了却没解析"的场景）
        pdf = os.path.join(self.acc_dir, "13_link1.pdf")
        _make_pdf(pdf, _sample_text())
        self.assertEqual(db.get_invoices_count(), 0)
        rep = engine.reconcile(dry_run=False)
        self.assertEqual(len(rep["reingested"]), 1, "孤儿 PDF 应被回填")
        self.assertEqual(db.get_invoices_count(), 1)
        inv = db.get_invoices()[0]
        self.assertEqual(inv["invoice_no"], "26122000000924510046")
        self.assertEqual(inv["amount"], 4698.97)

    def test_reconcile_dry_run_no_write(self):
        pdf = os.path.join(self.acc_dir, "14_link1.pdf")
        _make_pdf(pdf, _sample_text())
        rep = engine.reconcile(dry_run=True)
        self.assertEqual(len(rep["reingested"]), 1)
        self.assertEqual(db.get_invoices_count(), 0, "dry_run 不应写库")


def inv_id_for(account_id, invoice_no):
    c = db.conn()
    r = c.execute("SELECT id FROM invoices WHERE account_id=? AND invoice_no=?",
                  (account_id, invoice_no)).fetchone()
    c.close()
    return r["id"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
