"""
api 门面（facade）回归测试 —— 只覆盖【不触网】的函数，确保它们返回文档约定的 dict 形状、
且不会抛异常。需要真实 IMAP 的函数（fetch / test_connection）不在离线单测范围内。

目标：api 层是 CLI / Web / agent 的单一真相源，这一层一旦改坏，所有调用方都会受影响，
因此用单测守住返回结构与脱敏（明文密码不外泄）。
"""
import unittest

import db
import api


class ApiFacadeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()

    def test_list_accounts_returns_list_of_dict(self):
        rows = api.list_accounts()
        self.assertIsInstance(rows, list)
        for a in rows:
            self.assertIn("password_set", a)
            self.assertNotIn("password", a)  # 脱敏：明文密码不应外泄

    def test_get_account_nonexistent(self):
        self.assertIsNone(api.get_account(999999))

    def test_add_toggle_delete_cycle(self):
        # 用唯一邮箱避免与已存在账号冲突
        email = "api_test_@example.com"
        acc = api.add_account({"name": "apitest", "email": email, "imap_host": "",
                               "imap_port": 0, "use_ssl": 0, "folder": "", "password": "x", "enabled": 1})
        self.assertEqual(acc["email"], email)
        self.assertTrue(acc["password_set"])  # 密码已设，但明文不在返回里
        aid = acc["id"]
        # 启用/停用
        r = api.toggle_account(aid, False)
        self.assertFalse(r["enabled"])
        # 删除（清理测试数据，避免污染库）
        d = api.delete_account(aid)
        self.assertTrue(d["ok"])
        self.assertIsNone(api.get_account(aid))

    def test_get_invoices_shape(self):
        res = api.get_invoices(page=1, page_size=5)
        self.assertIn("rows", res)
        self.assertIn("total", res)
        self.assertIn("page", res)
        self.assertIn("page_size", res)
        self.assertIsInstance(res["rows"], list)

    def test_get_stats_shape(self):
        res = api.get_stats()
        self.assertIn("count", res)
        self.assertIn("total", res)
        self.assertIn("by_buyer", res)
        self.assertIsInstance(res["by_buyer"], dict)

    def test_reconcile_returns_dict(self):
        # 离线对账（dry_run），不写库，仅验证返回 dict 形状
        rep = api.reconcile(dry_run=True)
        self.assertIn("reingested", rep)
        self.assertIn("skipped", rep)
        self.assertIn("failed", rep)

    def test_reparse_all_shape(self):
        res = api.reparse_all()
        self.assertIn("total", res)
        self.assertIn("updated", res)


if __name__ == "__main__":
    unittest.main()
