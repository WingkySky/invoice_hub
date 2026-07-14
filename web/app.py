"""
Web 控制台后端 —— 纯标准库 HTTP 服务，只做"把数据库读出来变成 JSON / 接收操作指令"。

前端 web/index.html 是一个【数据驱动】的通用控制台：
  - 账号管理：增 / 改 / 删 / 启用停用 / 连接测试
  - 抓取：点击触发（后台线程）+ 定时自动 + 进度日志轮询
  - 导入：网页直接上传本地 PDF 解析入库
  - 导出：勾选 / 全选发票 → 打包 PDF + 清单(csv/md/xlsx) 下载
代码只负责"操作数据"，不写死任何账号、规则或视图内容。
"""
import sys
import io
import os
import re
import csv
import json
import time
import base64
import zipfile
import threading
import datetime as dt
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, quote

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import db
import engine

INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

# 自动抓取（Web 控制台里可开关，单位：秒，0=关闭）
AUTO_INTERVAL = 0
LAST_AUTO = 0
UPLOAD_ACC_EMAIL = "upload@local"


def _json(handler, obj, status=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _parse_filters(qs):
    f = {}
    for k in ("account_id", "buyer", "city", "invoice_no", "date_from", "date_to", "keyword"):
        if qs.get(k):
            f[k] = qs[k][0]
    if f.get("account_id"):
        try:
            f["account_id"] = int(f["account_id"])
        except Exception:
            f.pop("account_id", None)
    return f


def _safe_acc(a):
    """脱敏：不返回明文密码，只给 password_set 标记（前端据此提示'已保存'）。"""
    d = dict(a)
    d["password_set"] = bool(d.get("password"))
    d.pop("password", None)
    return d


def _ensure_upload_account():
    for a in db.get_accounts():
        if a["email"] == UPLOAD_ACC_EMAIL:
            return a
    db.upsert_account({"name": "网页上传", "email": UPLOAD_ACC_EMAIL,
                        "provider": "upload", "imap_host": "", "imap_port": 0,
                        "use_ssl": 0, "folder": "", "password": "", "enabled": 1})
    return db.get_accounts()[-1]


# ----------------------------------------------------------- 抓取任务
def do_fetch_job(since_override=None, acc_id=None):
    engine.FETCH_RUNNING = True
    try:
        if acc_id:
            engine.fetch_one(acc_id, since_override=since_override)
        else:
            engine.fetch_all(since_override=since_override)
    finally:
        engine.FETCH_RUNNING = False


def scheduler_loop():
    global LAST_AUTO
    while True:
        time.sleep(15)
        if AUTO_INTERVAL > 0 and not engine.FETCH_RUNNING:
            now = time.time()
            if now - LAST_AUTO >= AUTO_INTERVAL:
                LAST_AUTO = now
                threading.Thread(target=do_fetch_job, args=(None,), daemon=True).start()


def _account_id_from_path(path):
    # /api/accounts/3[/...]
    parts = [p for p in path.split("/") if p]
    try:
        return int(parts[2])
    except (IndexError, ValueError):
        return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send_file(self, path, ctype):
        if not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ----------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path

        if path in ("/", "/index.html"):
            self._send_file(INDEX, "text/html; charset=utf-8")
            return

        if path == "/api/invoices":
            filters = _parse_filters(qs)
            try:
                page = max(1, int(qs.get("page", ["1"])[0] or "1"))
            except ValueError:
                page = 1
            try:
                page_size = int(qs.get("page_size", ["50"])[0] or "50")
            except ValueError:
                page_size = 50
            rows = db.get_invoices(filters, page=page, page_size=page_size)
            total = db.get_invoices_count(filters)
            _json(self, {"rows": rows, "total": total, "page": page, "page_size": page_size})
            return

        if path == "/api/accounts":
            _json(self, [_safe_acc(a) for a in db.get_accounts()])
            return

        if re.match(r"^/api/accounts/\d+$", path):
            acc = db.get_account(_account_id_from_path(path))
            _json(self, _safe_acc(acc) if acc else {})
            return

        if path.startswith("/api/accounts/") and path.endswith("/fetch"):
            acc_id = _account_id_from_path(path)
            threading.Thread(target=do_fetch_job, args=(None, acc_id), daemon=True).start()
            _json(self, {"ok": True, "msg": "已启动抓取"})
            return

        if path.startswith("/api/accounts/") and path.endswith("/status"):
            acc_id = _account_id_from_path(path)
            acc = db.get_account(acc_id) if acc_id else None
            _json(self, {"ok": True, "account": acc})
            return

        if path == "/api/cities":
            _json(self, db.distinct_cities())
            return

        if path == "/api/buyers":
            _json(self, db.distinct_buyers())
            return

        if path == "/api/stats":
            _json(self, db.get_stats(_parse_filters(qs)))
            return

        if path == "/api/fetch/status":
            _json(self, {
                "running": engine.FETCH_RUNNING,
                "auto_interval": AUTO_INTERVAL,
                "last_ts": engine.FETCH_LAST_TS,
                "log": engine.FETCH_LOG[-80:],
            })
            return

        if path.startswith("/api/pdf/"):
            inv_id = path.rsplit("/", 1)[-1]
            try:
                inv_id = int(inv_id)
            except Exception:
                self.send_error(400)
                return
            c = db.conn()
            r = c.execute("SELECT pdf_path FROM invoices WHERE id=?", (inv_id,)).fetchone()
            c.close()
            if not r:
                self.send_error(404)
                return
            pdf_abs = os.path.join(db.HERE, r["pdf_path"])
            self._send_file(pdf_abs, "application/pdf")
            return

        self.send_error(404)

    # ----------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        text = None
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                pass
        try:
            payload = json.loads(text) if text is not None else {}
        except Exception:
            payload = {}

        path = u.path

        # 新增账号
        if path == "/api/accounts":
            db.upsert_account(_acc_from_payload(payload))
            _json(self, {"ok": True, "msg": "已添加/更新账号"})
            return

        # 编辑账号（按 id 更新）
        if path.startswith("/api/accounts/") and path.endswith("/update"):
            acc_id = _account_id_from_path(path)
            body = _acc_from_payload(payload)
            body["id"] = acc_id
            db.update_account(body)
            _json(self, {"ok": True, "msg": "已保存修改"})
            return

        # 启用/停用
        if path.startswith("/api/accounts/") and path.endswith("/toggle"):
            acc_id = _account_id_from_path(path)
            db.set_account_enabled(acc_id, int(payload.get("enabled", 0)))
            _json(self, {"ok": True})
            return

        # 删除
        if path.startswith("/api/accounts/") and path.endswith("/delete"):
            acc_id = _account_id_from_path(path)
            db.delete_account(acc_id)
            _json(self, {"ok": True})
            return

        # 批量删除发票
        if path == "/api/invoices/delete":
            ids = payload.get("ids", [])
            n = db.delete_invoices(ids)
            _json(self, {"ok": True, "deleted": n})
            return

        # 删除单张发票
        if path.startswith("/api/invoices/") and path.endswith("/delete"):
            parts = [p for p in path.split("/") if p]
            try:
                inv_id = int(parts[2])
            except (IndexError, ValueError):
                inv_id = None
            n = db.delete_invoices([inv_id]) if inv_id else 0
            _json(self, {"ok": True, "deleted": n})
            return

        # 重新解析所有 PDF（后台线程）
        if path == "/api/invoices/reparse":
            if engine.FETCH_RUNNING:
                _json(self, {"ok": False, "msg": "有任务在运行，请稍候"})
                return
            def _reparse_job():
                engine.FETCH_RUNNING = True
                try:
                    engine.reparse_all_pdfs()
                finally:
                    engine.FETCH_RUNNING = False
            threading.Thread(target=_reparse_job, daemon=True).start()
            _json(self, {"ok": True, "msg": "已启动重新解析"})
            return

        # 连接测试
        if path.startswith("/api/accounts/") and path.endswith("/test"):
            acc_id = _account_id_from_path(path)
            acc = db.get_account(acc_id)
            if not acc:
                _json(self, {"ok": False, "msg": "账号不存在"})
                return
            try:
                M = engine.connect(acc)
                M.logout()
                _json(self, {"ok": True, "msg": "✓ 连接成功（登录+选中文件夹均通过）"})
            except Exception as e:
                _json(self, {"ok": False, "msg": f"✗ 连接失败：{type(e).__name__}: {e}"})
            return

        # 单账号抓取（前端「抓取」按钮 / 走 POST，与 GET 路由对齐）
        if path.startswith("/api/accounts/") and path.endswith("/fetch"):
            acc_id = _account_id_from_path(path)
            threading.Thread(target=do_fetch_job, args=(None, acc_id), daemon=True).start()
            _json(self, {"ok": True, "msg": "已启动抓取"})
            return

        # 立即抓取全部
        if path == "/api/fetch":
            since = payload.get("since") or None
            threading.Thread(target=do_fetch_job, args=(since,), daemon=True).start()
            _json(self, {"ok": True, "msg": "已启动抓取"})
            return

        # 自动抓取开关（interval 秒，0=关）
        if path == "/api/fetch/auto":
            global AUTO_INTERVAL, LAST_AUTO
            AUTO_INTERVAL = int(payload.get("interval", 0) or 0)
            LAST_AUTO = time.time()
            _json(self, {"ok": True, "auto_interval": AUTO_INTERVAL})
            return

        # 网页上传本地 PDF
        if path == "/api/upload":
            files = payload.get("files", [])
            acc = _ensure_upload_account()
            added = 0
            up_dir = os.path.join(db.PDF_DIR, "upload")
            os.makedirs(up_dir, exist_ok=True)
            for f in files:
                name = f.get("name", "upload.pdf")
                b64 = f.get("b64", "")
                try:
                    raw_b = base64.b64decode(b64)
                except Exception:
                    continue
                dest = os.path.join(up_dir, engine.safe_name(name))
                with open(dest, "wb") as fh:
                    fh.write(raw_b)
                inv = engine.parse_pdf_to_invoice(dest, acc["id"], source_type="upload")
                if db.insert_invoice(inv):
                    added += 1
            _json(self, {"ok": True, "added": added, "total": len(files)})
            return

        # 勾选/全选 → 打包 PDF + 清单导出
        if path == "/api/export":
            self._export_zip(payload.get("ids", []))
            return

        self.send_error(404)

    # ----------------------------------------------------- 导出 zip
    def _export_zip(self, ids):
        rows = db.get_invoices_by_ids(ids)
        if not rows:
            self.send_error(404)
            return
        buf = io.BytesIO()
        zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
        used = {}
        for i, r in enumerate(rows, 1):
            pdf_abs = os.path.join(db.HERE, r.get("pdf_path") or "")
            if os.path.isfile(pdf_abs):
                base = f"{i:02d}_{r.get('invoice_no') or 'no'}_{r.get('buyer') or 'x'}"
                base = re.sub(r'[\\/:*?"<>|]+', "_", base)[:80]
                fname = base + ".pdf"
                if fname in used:
                    fname = f"{base}_{used[fname]}.pdf"
                    used[fname.rsplit('.',1)[0]] = used.get(fname.rsplit('.',1)[0],0)+1
                zf.write(pdf_abs, os.path.join("pdfs", fname))
        # 清单（csv / md / xlsx）
        headers = ["序号", "邮箱", "买方主体", "发票号码", "金额", "开票日期", "销售方", "城市", "来源", "PDF文件"]
        def row_vals(i, r):
            return [i, r.get("account_name") or r.get("account_email") or "",
                    r.get("buyer", ""), r.get("invoice_no", ""),
                    r.get("amount", "") or "", r.get("invoice_date", ""),
                    r.get("seller", ""), r.get("city", ""),
                    r.get("source_type", ""), ""]
        # csv (utf-8-sig 兼容 Excel)
        csv_buf = io.StringIO()
        w = csv.writer(csv_buf)
        w.writerow(headers)
        for i, r in enumerate(rows, 1):
            w.writerow(row_vals(i, r))
        zf.writestr("清单.csv", "\ufeff" + csv_buf.getvalue())
        # md
        md = "# 发票清单\n\n"
        md += "| " + " | ".join(headers) + " |\n"
        md += "|" + "---|" * len(headers) + "\n"
        for i, r in enumerate(rows, 1):
            md += "| " + " | ".join(str(x) for x in row_vals(i, r)) + " |\n"
        md += f"\n共 {len(rows)} 张发票。\n"
        zf.writestr("清单.md", md)
        # xlsx
        try:
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "发票清单"
            ws.append(headers)
            for i, r in enumerate(rows, 1):
                ws.append(row_vals(i, r))
            xb = io.BytesIO()
            wb.save(xb)
            zf.writestr("清单.xlsx", xb.getvalue())
        except Exception:
            pass
        zf.close()
        data = buf.getvalue()
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M")
        cdisp = (f"attachment; filename=\"invoices_{ts}.zip\"; "
                  f"filename*=UTF-8''{quote(f'发票打包_{ts}.zip')}")
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", cdisp)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _acc_from_payload(p):
    return {
        "name": p.get("name", p.get("email")),
        "email": p.get("email"),
        "provider": p.get("provider"),
        "imap_host": p.get("imap_host"),
        "imap_port": int(p.get("imap_port", 993) or 993),
        "use_ssl": 1 if p.get("use_ssl", True) else 0,
        "folder": p.get("folder", "INBOX"),
        "password": (p.get("password") or "").strip(),
        "enabled": 1 if p.get("enabled", True) else 0,
        "fetch_mode": p.get("fetch_mode") or "incremental",
        "default_since": p.get("default_since") or "90d",
        "keywords_override": p.get("keywords_override") or None,
    }


def run(port=8000):
    db.init()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    srv = HTTPServer(("127.0.0.1", port), Handler)
    print(f"发票中枢控制台已启动: http://127.0.0.1:{port}")
    print("（Ctrl+C 停止）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    run()
