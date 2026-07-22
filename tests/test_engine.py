import datetime as dt
import os
import sys
import tempfile
import unittest
from unittest import mock

import fitz  # PyMuPDF，造测试用 PDF

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import engine


class ParseSinceExprTest(unittest.TestCase):
    def test_relative_days(self):
        fixed = dt.date(2026, 7, 15)
        self.assertEqual(engine.parse_since_expr("90d", today=fixed), "16-Apr-2026")

    def test_absolute_date(self):
        self.assertEqual(engine.parse_since_expr("2026-07-01"), "01-Jul-2026")

    def test_date_range(self):
        self.assertEqual(engine.parse_since_expr("2026-01-01~2026-07-15"), "01-Jan-2026")
        self.assertEqual(engine.parse_since_expr("2026-03-01~2026-06-30"), "01-Mar-2026")

    def test_empty_fallback(self):
        self.assertEqual(engine.parse_since_expr(None), "01-Jan-2000")
        self.assertEqual(engine.parse_since_expr(""), "01-Jan-2000")

    def test_invalid_falls_back(self):
        self.assertEqual(engine.parse_since_expr("abc"), "01-Jan-2000")


class _FakeIMAP:
    """最小 IMAP 桩：uid('search') 返回固定 uid 列表，uid('fetch') 返回空壳邮件。"""

    def __init__(self, uids):
        self._uids = " ".join(str(u) for u in uids).encode()
        self.fetched = []

    def uid(self, cmd, *args):
        if cmd == "search":
            return "OK", [self._uids]
        if cmd == "fetch":
            self.fetched.append(int(args[0]))
            # 一封无附件、无链接、非发票的最小邮件
            raw = b"Subject: hi\r\nFrom: a@b.com\r\nDate: x\r\n\r\nbody"
            return "OK", [(b"1", raw)]
        return "OK", [b""]

    def logout(self):
        pass


class FetchAccountWatermarkTest(unittest.TestCase):
    """锁定水位线 / 全量抓取的核心语义（详见 fetch_account docstring）。"""

    def _run(self, mode, last_uid, server_uids, needs_refetch=True):
        acc = {"id": 1, "email": "t@x.com", "fetch_mode": mode, "last_uid": last_uid}
        fake = _FakeIMAP(server_uids)
        updated = {}
        with mock.patch.object(engine, "connect", return_value=fake), \
             mock.patch.object(engine.db, "needs_refetch", return_value=needs_refetch), \
             mock.patch.object(engine.db, "get_email_id", return_value=1), \
             mock.patch.object(engine.db, "set_email_invoice"), \
             mock.patch.object(engine.db, "PDF_DIR", tempfile.mkdtemp()), \
             mock.patch.object(engine.db, "update_last_uid",
                               side_effect=lambda a, u: updated.__setitem__(a, u)):
            engine.fetch_account(acc, {"invoice_keywords": ["发票"]}, session=None)
        return fake, updated

    def test_full_mode_no_200_cap(self):
        """full 模式必须扫描范围内全部 uid，不能被 [-200:] 截断。"""
        server_uids = list(range(1, 251))  # 250 封
        fake, _ = self._run("full", last_uid=0, server_uids=server_uids)
        self.assertEqual(len(fake.fetched), 250)
        self.assertIn(1, fake.fetched)  # 最旧的一封也被扫到

    def test_full_mode_does_not_write_watermark(self):
        """full 模式是一次性重扫，不应污染增量水位线。"""
        _, updated = self._run("full", last_uid=100, server_uids=[101, 102, 103])
        self.assertEqual(updated, {})

    def test_incremental_applies_200_cap(self):
        """incremental 模式保留 200 上限兜底。"""
        server_uids = list(range(1, 251))
        fake, _ = self._run("incremental", last_uid=0, server_uids=server_uids)
        self.assertEqual(len(fake.fetched), 200)
        self.assertNotIn(1, fake.fetched)  # 最旧的 50 封被上限挡掉

    def test_incremental_watermark_uses_scanned_max(self):
        """水位线推进到"已扫描的最大 uid"，即使这些 uid 被 needs_refetch 跳过。"""
        _, updated = self._run("incremental", last_uid=100,
                               server_uids=[101, 105, 110], needs_refetch=False)
        self.assertEqual(updated, {1: 110})

    def test_full_mode_ignores_needs_refetch(self):
        """full 模式必须忽略 needs_refetch 的旧判定，重新拉取并处理历史邮件，
        否则新增的发票识别规则（附件/OFD/文件名格式）无法作用于旧邮件。"""
        fake, _ = self._run("full", last_uid=100,
                            server_uids=[101, 102], needs_refetch=False)
        self.assertEqual(len(fake.fetched), 2)


class ImapConnectionMgmtTest(unittest.TestCase):
    """回归：连接池复用 + 风控识别，是修复腾讯企业邮箱"断链/风控"的核心。"""

    def test_pool_reuses_connection_without_relogin(self):
        """pool_get 借出后 pool_put 归还，再次 pool_get 应复用同一连接（不重新登录）。
        这正是规避腾讯企业邮箱"频繁登录触发风控"的关键。"""
        acc = {"id": 7, "email": "a@exmail.qq.com", "imap_host": "imap.exmail.qq.com",
               "imap_port": 993, "use_ssl": 1, "folder": "INBOX", "password": "pw"}
        fake = _FakeIMAP(uids=[])
        fake.noop = lambda: ("OK", [b""])  # 让探活通过，模拟健康连接
        calls = {"login": 0}

        orig_connect = engine.connect

        def fake_connect(a, _attempt=0):
            calls["login"] += 1
            return fake

        engine.connect = fake_connect
        try:
            m1 = engine.pool_get(acc)
            engine.pool_put(acc, m1)
            m2 = engine.pool_get(acc)
            engine.pool_put(acc, m2)
            self.assertIs(m1, m2, "连接应被复用，而非重新登录")
            self.assertEqual(calls["login"], 1, "两次借还应只登录一次")
        finally:
            engine.connect = orig_connect
            # 清理池中残留，避免污染其它测试
            engine._POOL.pop((acc["id"], acc["password"]), None)

    def test_risk_control_detection(self):
        """_is_risk_control 应识别腾讯/网易返回的登录过密、账号锁定等风控文案。"""
        self.assertTrue(engine._is_risk_control("LOGIN FAILED: too many attempts"))
        self.assertTrue(engine._is_risk_control("账号已被临时锁定 lock"))
        self.assertTrue(engine._is_risk_control("please try again later"))
        self.assertFalse(engine._is_risk_control("authentication failed: bad password"))


class ParseInvoiceFilenameTest(unittest.TestCase):
    """电子发票文件名格式（dzfp_...）常见于标题即附件文件名的系统邮件。"""

    def test_dzfp_pattern(self):
        subject = "dzfp_26442000008130218281_字节跳动科技有限公司_20260717085020 - test@example.cn"
        inv = engine.parse_invoice_filename(subject, account_id=1, email_id=2)
        self.assertIsNotNone(inv)
        self.assertEqual(inv["invoice_no"], "26442000008130218281")
        self.assertEqual(inv["seller"], "字节跳动科技有限公司")
        self.assertEqual(inv["invoice_date"], "2026年07月17日")
        self.assertEqual(inv["source_type"], "subject")

    def test_electronic_invoice_pattern(self):
        subject = "电子发票_12345678901234567890_某公司_20250101"
        inv = engine.parse_invoice_filename(subject, account_id=1, email_id=2)
        self.assertIsNotNone(inv)
        self.assertEqual(inv["invoice_no"], "12345678901234567890")

    def test_non_matching_subject(self):
        self.assertIsNone(engine.parse_invoice_filename("会议通知", account_id=1, email_id=2))


class ExtractTextRobustnessTest(unittest.TestCase):
    """回归：单页 OCR 失败（Tesseract 未装 / 语言包缺失 / 新版 PyMuPDF API 变更）
    绝不能抛异常中断整封邮件乃至整账号的抓取，而应就地返回空串。"""

    def test_broken_pdf_returns_empty(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4\n%%EOF\nthis is not a real pdf")
            path = f.name
        try:
            # 即便解析失败也必须返回字符串、不抛异常
            self.assertEqual(engine.extract_text_from_pdf(path), "")
        finally:
            os.unlink(path)

    def test_layout_extract_never_raises_on_broken_pdf(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"garbage-not-pdf")
            path = f.name
        try:
            # extract_fields_by_layout 对任意坏文件都应返回 {} 而非抛 "page is None"
            self.assertEqual(engine.extract_fields_by_layout(path), {})
        finally:
            os.unlink(path)


class CoalesceInvoiceTest(unittest.TestCase):
    """回归：批量发票邮件里每封附件各自携带发票号（dzfp_文件名），
    发票号权威来源必须是「附件自身」，而非整封邮件共享的标题——否则多张发票被合并成一张。"""

    def test_pdf_content_wins_over_filename(self):
        base = {"invoice_no": "111", "seller": "PDF卖方", "amount": 10.0,
                "invoice_date": "2026年01月01日", "pdf_path": "a.pdf", "note": ""}
        file_inv = {"invoice_no": "222", "seller": "文件名卖方", "amount": None,
                    "invoice_date": "2026年02月02日"}
        sinv = {"invoice_no": "333", "seller": "标题卖方"}
        inv, no = engine._coalesce_invoice(base, file_inv, sinv, "attachment")
        # 发票号取 PDF 内容，而不是文件名/标题
        self.assertEqual(no, "111")
        self.assertEqual(inv["seller"], "PDF卖方")

    def test_filename_fallback_when_pdf_blank(self):
        file_inv = {"invoice_no": "222", "seller": "文件名卖方", "amount": None,
                    "invoice_date": "2026年02月02日"}
        inv, no = engine._coalesce_invoice(None, file_inv, None, "attachment")
        # PDF 解析不出号时，回退到附件文件名里的发票号（批量发票的关键）
        self.assertEqual(no, "222")
        self.assertEqual(inv["seller"], "文件名卖方")

    def test_empty_when_nothing_resolves(self):
        inv, no = engine._coalesce_invoice(None, None, None, "attachment",
                                           account_id=1, email_id=9)
        # 三来源都没有发票号 → 返回空号，调用方应跳过（不硬造行）
        self.assertEqual(no, "")
        self.assertEqual(inv["account_id"], 1)  # 极端兜底仍带 account_id，不触发入库崩溃


class InvoiceRecognitionGateTest(unittest.TestCase):
    """锁定「非发票 PDF 不应入库」：parse_pdf_to_invoice 仅在像发票时才返回发票号。

    这是用户反馈的第二个问题——之前只要邮件带 PDF 附件、且 PDF 里恰有一串 20 位数字，
    就会被当成发票入库（"只是因为它是 PDF"）。现在正文必须含发票强特征词，或文件名是
    发票格式，才视为发票。"""

    def _make_pdf(self, lines, path):
        doc = fitz.open()
        page = doc.new_page()
        y = 50
        for ln in lines:
            # china-s 是 PyMuPDF 内置简体中文字体，否则 insert_text 会把中文写成占位点
            page.insert_text((50, y), ln, fontname="china-s")
            y += 20
        doc.save(path)
        doc.close()

    def test_non_invoice_pdf_rejected(self):
        # 合同类 PDF：含一串 20 位数字，但无任何发票特征关键词 → 不应被当作发票
        d = tempfile.mkdtemp()
        p = os.path.join(d, "contract.pdf")
        self._make_pdf([
            "合作协议",
            "本合同编号 12345678901234567890",
            "双方于近日签署，自生效日起履行。",
        ], p)
        inv = engine.parse_pdf_to_invoice(p, 1)
        self.assertEqual(inv["invoice_no"], "")
        self.assertEqual(inv["seller"], "")
        self.assertIsNone(inv["amount"])

    def test_invoice_pdf_accepted(self):
        # 真发票 PDF：含「发票」关键词 + 20 位发票号 → 正常识别
        d = tempfile.mkdtemp()
        p = os.path.join(d, "inv.pdf")
        self._make_pdf([
            "增值税电子普通发票",
            "发票号码：24412000000012345678",
            "销售方名称：某科技有限公司 纳税人识别号：91110000ABCDEF1234",
            "购买方名称：某客户公司 纳税人识别号：91110000ABCDEF5678",
            "价税合计 100.00",
        ], p)
        inv = engine.parse_pdf_to_invoice(p, 1)
        self.assertEqual(inv["invoice_no"], "24412000000012345678")
        self.assertIn("科技", inv["seller"])

    def test_scan_pdf_with_invoice_filename_accepted(self):
        # 扫描件 PDF 无文本层，但文件名是发票格式（dzfp_…）→ 仍应识别出发票号
        d = tempfile.mkdtemp()
        fname = "dzfp_24412000000012345678_某科技有限公司_20260717.pdf"
        p = os.path.join(d, fname)
        doc = fitz.open()
        doc.new_page()
        doc.save(p)
        doc.close()
        inv = engine.parse_pdf_to_invoice(p, 1, filename=fname)
        self.assertEqual(inv["invoice_no"], "24412000000012345678")
        self.assertIn("科技", inv["seller"])

    def test_has_invoice_evidence_keywords(self):
        rules = engine.load_rules()
        self.assertTrue(engine._has_invoice_evidence("这是一张增值税普通发票", rules))
        self.assertFalse(engine._has_invoice_evidence("双方签署的合作协议", rules))
        # rules.json 缺失 pdf_invoice_keywords 时回退到内置常量，不会全量拒收
        self.assertTrue(engine._has_invoice_evidence("电子发票", {}))


class FormatPriorityTest(unittest.TestCase):
    """同号发票「PDF 优先于 OFD」的优先级逻辑。"""

    def test_format_priority_ordering(self):
        self.assertEqual(engine._format_priority("pdf", "a.pdf"), 2)
        self.assertEqual(engine._format_priority("ofd", "a.ofd"), 1)
        self.assertEqual(engine._format_priority("subject", ""), 0)
        self.assertEqual(engine._format_priority("pdf", "a.ofd"), 2)  # source_type 标 pdf → 高优
        self.assertEqual(engine._format_priority("ofd", "a.pdf"), 2)  # pdf_path 为 pdf → 高优

    def test_ofd_never_downgrades_existing_pdf(self):
        """已存 PDF 行，被同号 OFD 撞到时只补业务字段，pdf_path/source_type 不动（不降级）。"""
        existing = {"source_type": "pdf", "pdf_path": "data/x/invoice.pdf",
                    "seller": "", "note": "发票号/字段来自文件名"}
        ofd = {"source_type": "ofd", "pdf_path": "data/x/invoice.ofd",
               "seller": "某销售方", "note": ""}
        payload = engine._invoice_update_payload(existing, ofd)
        self.assertNotIn("pdf_path", payload)          # 文件不被 OFD 覆盖
        self.assertNotIn("source_type", payload)       # 格式不被降级为 ofd
        self.assertEqual(payload["seller"], "某销售方")  # 业务字段仍补
        self.assertEqual(payload["note"], "")          # 占位说明被清空

    def test_pdf_upgrades_existing_ofd(self):
        """已存 OFD 行，被同号 PDF 撞到时升级为 PDF（采纳文件与格式）。"""
        existing = {"source_type": "ofd", "pdf_path": "data/x/invoice.ofd", "seller": ""}
        pdf = {"source_type": "pdf", "pdf_path": "data/x/invoice.pdf", "seller": "某销售方"}
        payload = engine._invoice_update_payload(existing, pdf)
        self.assertEqual(payload["pdf_path"], "data/x/invoice.pdf")
        self.assertEqual(payload["source_type"], "pdf")

    def test_find_sibling_pdf(self):
        d = tempfile.mkdtemp()
        # 同号 PDF 应被找到
        open(os.path.join(d, "34894_7_dzfp_26442000008130218281_字节跳动.pdf"), "w").close()
        # 其它号 PDF 不应被「子串误匹配」误命中
        open(os.path.join(d, "264420000081302182810.pdf"), "w").close()
        got = engine._find_sibling_pdf(d, "26442000008130218281")
        self.assertIsNotNone(got)
        self.assertTrue(got.endswith("26442000008130218281_字节跳动.pdf"))
        # 不存在同号时返回 None
        self.assertIsNone(engine._find_sibling_pdf(d, "99999999999999999999"))


if __name__ == "__main__":
    unittest.main()
