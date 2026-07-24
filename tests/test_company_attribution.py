"""
公司归属维度（P0）端到端验证。

要点：
- 全部跑在临时库上（覆盖 db.DB_PATH），不碰用户的真实 data/invoice_hub.db。
- 覆盖：公司 CRUD、自动归属（classified/unclassified/ambiguous）、buyer_tax 回填、
  从发票导入候选公司、历史回填、批量人工改所属公司、按公司/状态过滤、归属概况统计，
  以及 hub.py companies 子命令的代码路径（list/add/update/delete/import/backfill/assign）。
"""
import os
import sys
import io
import json
import tempfile
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db
import api
import hub

# ---- 1. 重定向到临时库 ----
_tmp = tempfile.mkdtemp(prefix="invhub_test_")
db.DATA_DIR = os.path.join(_tmp, "data")
db.PDF_DIR = os.path.join(_tmp, "pdfs")
db.DB_PATH = os.path.join(db.DATA_DIR, "invoice_hub.db")
db.init()

failures = []
def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {extra}")
        failures.append(name)

def inv_row(buyer, account_id, **kw):
    d = {
        "email_id": None, "account_id": account_id, "buyer": buyer,
        "seller": "某销售方", "amount": 100.0, "invoice_no": kw.get("invoice_no", f"INV-{buyer}-{id(buyer)}"),
        "invoice_date": "2026-07-01", "city": "广州", "pdf_path": "", "source_type": "test",
        "note": "", "remark": "", "buyer_tax": kw.get("buyer_tax", ""),
    }
    return d

# ---- 2. 准备账号 ----
acc = db.upsert_account({"name": "测试账号", "email": "test@x.com", "imap_host": "", "imap_port": 0,
                         "use_ssl": 0, "folder": "", "password": "", "enabled": 1,
                         "fetch_method": "imap"})
acc_id = acc["id"]

# ---- 3. 公司 CRUD ----
c1 = api.create_company({"name": "南沙友谊", "tax_id": "91440101MA5AAAAA1X", "aliases": "南沙友谊人才,友谊公司"})
c2 = api.create_company({"name": "星辰科技", "tax_id": "91440101MA5BBBBB2Y", "aliases": ["星辰", "星科技"]})
check("create_company 返回 id", isinstance(c1.get("id"), int), str(c1))
def _as_list(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v
check("create_company 别名解析一致", _as_list(c1.get("aliases")) == ["南沙友谊人才", "友谊公司"], str(c1.get("aliases")))
check("create_company 别名以 JSON 入库", json.loads(db.get_company_by_name("南沙友谊")["aliases"]) == ["南沙友谊人才", "友谊公司"], str(db.get_company_by_name("南沙友谊")["aliases"]))

# 重名不重复创建
c1_dup = api.create_company({"name": "南沙友谊", "tax_id": "", "aliases": ""})
check("重名 company 合并（同 id）", c1_dup["id"] == c1["id"], str(c1_dup))

# ---- 4. 自动归属 ----
ia = db.insert_invoice(inv_row("南沙友谊人才服务有限公司", acc_id, buyer_tax="91440101MA5AAAAA1X"))
ib = db.insert_invoice(inv_row("星辰科技有限公司", acc_id, buyer_tax="91440101MA5BBBBB2Y"))
ic = db.insert_invoice(inv_row("未知买方XYZ", acc_id))
id_empty = db.insert_invoice(inv_row("", acc_id))
ie = db.insert_invoice(inv_row("友谊公司", acc_id))

def get_attr(inv_id):
    for r in db.get_invoices():
        if r["id"] == inv_id:
            return r
    return {}

ra = get_attr(ia)
rb = get_attr(ib)
rc = get_attr(ic)
re_ = get_attr(ie)
check("别名子串匹配 -> classified (C1)", ra["attribution_status"] == "classified" and ra["company_id"] == c1["id"], str(ra))
check("别名匹配 -> classified (C2)", rb["attribution_status"] == "classified" and rb["company_id"] == c2["id"], str(rb))
check("无匹配 -> unclassified", rc["attribution_status"] == "unclassified", str(rc))
check("buyer 为空 -> unclassified(buyer 为空)", "buyer 为空" in (rc and get_attr(id_empty)["attribution_reason"]), str(get_attr(id_empty)))
check("精确别名匹配 -> classified (C1)", re_["attribution_status"] == "classified" and re_["company_id"] == c1["id"], str(re_))

# buyer_tax 回填到公司（自动补 tax_id）
c1r = api.get_company(c1["id"])
check("发票 buyer_tax 回填公司 tax_id", c1r["tax_id"] == "91440101MA5AAAAA1X", str(c1r))

# ---- 5. 歧义 ----
c3 = api.create_company({"name": "友谊", "tax_id": "", "aliases": "友谊"})  # 新增会与 C1 的别名"友谊公司"冲突
db.insert_invoice(inv_row("友谊", acc_id))  # buyer 同时命中 C1(别名友谊公司) 与 C3(别名友谊)
amb = db.get_invoices(filters={"attribution_status": "ambiguous"})
check("歧义发票被识别", any(r["buyer"] == "友谊" for r in amb), f"ambiguous={len(amb)}")

# ---- 6. 从发票导入候选公司 ----
added = api.import_companies_from_invoices()
check("import 从发票导入候选公司(>=1)", added.get("added", 0) >= 1, str(added))
cands = api.list_companies()
check("候选公司'未知买方XYZ'已生成", any(c["name"] == "未知买方XYZ" for c in cands), str([c["name"] for c in cands]))

# ---- 7. 历史回填 ----
bf = api.backfill_company()
check("backfill 返回统计", set(["scanned","classified","unclassified","ambiguous"]).issubset(bf.keys()), str(bf))
_total = len(db.get_invoices())
check("backfill 后三类计数合计=发票总数", bf["classified"]+bf["unclassified"]+bf["ambiguous"] == _total, str(bf))
check("backfill 处理后无残留待归属", bf["scanned"] >= 0 and (bf["classified"]+bf["unclassified"]+bf["ambiguous"]) == _total, str(bf))

# ---- 8. 批量人工改所属公司 ----
upd = api.assign_invoices([ic], c2["id"])
rc2 = get_attr(ic)
check("assign 人工指定 -> classified", rc2["attribution_status"] == "classified" and rc2["company_id"] == c2["id"], str(rc2))
check("assign 返回 updated=1", upd.get("updated") == 1, str(upd))
# 置为未归类
api.assign_invoices([ic], None)
rc3 = get_attr(ic)
check("assign None -> 未归类", rc3["attribution_status"] == "unclassified", str(rc3))

# ---- 9. 按公司 / 状态 过滤 ----
c1_rows = db.get_invoices(filters={"company_id": c1["id"]})
check("按 company_id 过滤", all(r["company_id"] == c1["id"] for r in c1_rows), f"n={len(c1_rows)}")
uncls = db.get_invoices(filters={"attribution_status": "unclassified"})
check("按 attribution_status 过滤", all(r["attribution_status"] == "unclassified" for r in uncls), f"n={len(uncls)}")
multi = db.get_invoices(filters={"attribution_status": "unclassified,ambiguous"})
check("多值 status 过滤", all(r["attribution_status"] in ("unclassified","ambiguous") for r in multi), f"n={len(multi)}")

# ---- 10. 归属概况统计 ----
s = api.attribution_summary()
total = len(db.get_invoices())
check("summary.total 等于发票总数", s["total"] == total, f"{s['total']} vs {total}")
check("summary 三类计数合计=total", s["classified"]+s["unclassified"]+s["ambiguous"] == s["total"], str(s))

# ---- 11. hub.py companies 子命令（代码路径） ----
hub.OUT_JSON = True
def run_companies(argv):
    old = sys.stdout
    buf = io.StringIO()
    sys.stdout = buf
    try:
        ap = hub.build_parser()
        args = ap.parse_args(["companies"] + argv)
        hub.cmd_companies(args)
    finally:
        sys.stdout = old
    return buf.getvalue()

out = run_companies(["list"])
check("CLI companies list 输出 JSON", '"companies"' in out, out[:120])
out = run_companies(["add", "--name", "CLI测试公司", "--tax-id", "91440101MA5CCCC3Z", "--aliases", "CLI简称"])
check("CLI companies add 成功", '"ok": true' in out.lower() and "CLI测试公司" in out, out[:200])
# 取新公司 id
cid = None
for c in api.list_companies():
    if c["name"] == "CLI测试公司":
        cid = c["id"]
check("CLI add 后可在列表取到", cid is not None)
out = run_companies(["update", str(cid), "--name", "CLI测试公司改名"])
check("CLI companies update", '"ok": true' in out.lower(), out[:200])
out = run_companies(["delete", str(cid)])
check("CLI companies delete", '"ok": true' in out.lower(), out[:200])
out = run_companies(["import"])
check("CLI companies import", '"ok": true' in out.lower(), out[:200])
out = run_companies(["backfill"])
check("CLI companies backfill", '"scanned"' in out, out[:200])
out = run_companies(["assign", "--ids", str(ic), "--company-id", str(c1["id"])])
check("CLI companies assign", '"ok": true' in out.lower(), out[:200])

# ---- 汇总 ----
print("\n==== 结果 ====")
if failures:
    print(f"失败 {len(failures)} 项: {failures}")
    sys.exit(1)
else:
    print("全部通过 ✓")
