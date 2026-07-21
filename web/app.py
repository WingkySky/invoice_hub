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
import uuid
import tempfile
import threading
import datetime as dt
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import db
import engine
import api
import matching

INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

# 自动抓取（Web 控制台里可开关，单位：秒，0=关闭）
AUTO_INTERVAL = 0
LAST_AUTO = 0
UPLOAD_ACC_EMAIL = "upload@local"

# 导出 zip 临时目录（后台导出任务把 zip 落到这里，下载后清理）
EXPORT_DIR = os.path.join(db.DATA_DIR, "exports")

# 模板匹配：上传文件和结果的临时缓存（file_id -> {template_bytes, template_info, columns_map, result}）
MATCH_FILES = {}
_MATCH_LOCK = threading.Lock()
MATCH_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "standard_template.xlsx")


def _json_default(o):
    """JSON 序列化兜底：把 datetime/date 转 ISO 字符串（template_info.rows[].date 等）。"""
    if isinstance(o, (dt.datetime, dt.date)):
        return o.isoformat()
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


def _json(handler, obj, status=200):
    body = json.dumps(obj, ensure_ascii=False, default=_json_default).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, OSError):
        pass  # client disconnected


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


# ----------------------------------------------------------- 通用后台任务管理
# 统一「删除 / 上传 / 导出 / 账号删除」等耗时操作的异步执行与进度回报，
# 避免任何重活阻塞 HTTP 请求线程。各任务不再各自维护散落的 STATE 变量。
# 前端用 GET /api/jobs/<job_id> 轮询进度；产出文件（如导出 zip）的任务
# 完成后可 GET /api/jobs/<job_id>/download 取回。
JOBS = {}                      # job_id -> {kind, running, done, total, msg, error, result, result_name}
_JOBS_LOCK = threading.Lock()


def start_job(kind, target, *args, total=None):
    """启动一个后台任务：target(report, *args) 在守护线程执行；
    report(done, total, msg) 用于回报进度。返回 job_id（12 位 hex 字符串）。"""
    jid = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        JOBS[jid] = {"kind": kind, "running": True, "done": 0, "total": total or 0,
                     "msg": "处理中…", "error": None, "result": None, "result_name": None}
    threading.Thread(target=_job_runner, args=(jid, target, args), daemon=True).start()
    return jid


def _job_runner(jid, target, args):
    report = _JobReporter(jid)
    try:
        target(report, *args)
    except Exception as e:
        _update_job(jid, running=False, error=f"{type(e).__name__}: {e}",
                    msg=f"失败：{type(e).__name__}: {e}")
    else:
        _update_job(jid, running=False)


class _JobReporter:
    """进度回报器：report(done, total, msg) 更新进度；report.result(path, name) 挂接产出文件。"""
    def __init__(self, jid):
        self.jid = jid

    def __call__(self, done=None, total=None, msg=None):
        _update_job(self.jid, done=done, total=total, msg=msg)

    def result(self, path, name=None):
        _update_job(self.jid, result=path, result_name=name)


def _update_job(jid, **kw):
    with _JOBS_LOCK:
        j = JOBS.get(jid)
        if j:
            j.update({k: v for k, v in kw.items() if v is not None})


def get_job(jid):
    with _JOBS_LOCK:
        j = JOBS.get(jid)
        return dict(j) if j else None


# ----------------------------------------------------------- 各任务的具体执行体
def _run_delete(report, ids):
    db.delete_invoices(ids, on_progress=report)


def _run_delete_account(report, acc_id):
    db.delete_account(acc_id)
    report(msg="账号已删除")


def _run_upload(report, files):
    """后台逐文件写入磁盘 + PyMuPDF 解析 + 入库，回报导入进度。"""
    acc = _ensure_upload_account()
    up_dir = os.path.join(db.PDF_DIR, "upload")
    os.makedirs(up_dir, exist_ok=True)
    total = len(files)
    added = 0
    for i, f in enumerate(files, 1):
        name = f.get("name", "upload.pdf")
        b64 = f.get("b64", "")
        try:
            raw_b = base64.b64decode(b64)
        except Exception:
            raw_b = b""
        if not raw_b:
            continue
        dest = os.path.join(up_dir, engine.safe_name(name))
        with open(dest, "wb") as fh:
            fh.write(raw_b)
        inv = engine.parse_pdf_to_invoice(dest, acc["id"], source_type="upload")
        if db.insert_invoice(inv):
            added += 1
        report(i, total, f"已导入 {added}/{total}")
    report(total, total, f"已导入 {added}/{total} 个")


def _run_export(report, ids):
    path = _build_export_zip(ids, report)
    if not path:
        raise ValueError("没有可导出的发票")
    report.result(path, "发票打包.zip")


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

    def _send_file(self, path, ctype, attachment_name=None):
        if not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # attachment_name 给定时以附件形式下发（Content-Disposition: attachment），
        # 浏览器会下载而非尝试内嵌渲染——用于 OFD 等浏览器无法直接显示的格式，避免"无法加载"。
        # 注意：HTTP 头只能 latin-1，filename= 必须 ASCII；中文名放 filename*=UTF-8''（百分号编码），
        # 否则含中文名会触发 UnicodeEncodeError 直接 500。
        if attachment_name:
            asc = attachment_name.encode("ascii", "ignore").decode() or "download"
            # RFC 5987: filename*=UTF-8''<percent-encoded>
            # 注意 f-string 中相邻单引号会被解析为空串拼接，需用变量避免
            quoted = quote(attachment_name)
            disposition = "attachment; filename=\"" + asc + "\"; filename*=UTF-8''" + quoted
            self.send_header("Content-Disposition", disposition)
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

        if path == "/api/invoices/delete/status":
            # 兼容旧前端：批量删除改走通用任务后，这里转发到对应 job 已不再维护；
            # 保留该路由仅返回空态，避免旧前端轮询报错（新前端用 /api/jobs/<id>）。
            _json(self, {"running": False, "done": 0, "total": 0, "msg": ""})
            return

        # 通用后台任务：进度轮询
        m = re.match(r"^/api/jobs/([0-9a-f]{12})$", path)
        if m:
            j = get_job(m.group(1))
            if not j:
                self.send_error(404)
                return
            _json(self, j)
            return

        # 通用后台任务：取回产出文件（如导出 zip），下载后即清理临时文件
        m = re.match(r"^/api/jobs/([0-9a-f]{12})/download$", path)
        if m:
            j = get_job(m.group(1))
            if not j or not j.get("result") or not os.path.isfile(j["result"]):
                self.send_error(404)
                return
            # 根据文件扩展名判断 Content-Type：match_export 产出 xlsx，其余为 zip
            ctype = "application/zip"
            if j.get("result", "").endswith(".xlsx"):
                ctype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            self._send_file(j["result"], ctype, j.get("result_name") or "download")
            try:
                os.remove(j["result"])
            except OSError:
                pass
            return

        # 模板匹配：下载标准模板
        if path == "/api/match/template/download":
            self._send_file(MATCH_TEMPLATE, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "标准模板.xlsx")
            return

        # 模板匹配：查询匹配结果
        m = re.match(r"^/api/match/result/([0-9a-f]{12})$", path)
        if m:
            fid = m.group(1)
            with _MATCH_LOCK:
                f = MATCH_FILES.get(fid)
            if not f:
                self.send_error(404)
                return
            _json(self, f.get("result") or {"ok": False, "msg": "尚未匹配"})
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
            # 标题兜底的发票没有 PDF（pdf_path 为空），直接 404，避免 os.path.join 解析到项目根目录
            if not r["pdf_path"]:
                self.send_error(404)
                return
            pdf_abs = os.path.join(db.HERE, r["pdf_path"])
            if not os.path.isfile(pdf_abs):
                self.send_error(404)
                return
            # OFD 是电子发票官方格式，浏览器无法内嵌渲染成 PDF：
            # 以附件形式提供下载（而非 application/pdf 硬塞），否则前端会显示"无法加载"。
            if r["pdf_path"].lower().endswith(".ofd"):
                self._send_file(pdf_abs, "application/octet-stream",
                                attachment_name=os.path.basename(r["pdf_path"]))
                return
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

        # 删除账号（含级联本地 PDF 目录，可能较久，走后台任务不阻塞请求线程）
        if path.startswith("/api/accounts/") and path.endswith("/delete"):
            acc_id = _account_id_from_path(path)
            jid = start_job("delete_account", _run_delete_account, acc_id)
            _json(self, {"ok": True, "job_id": jid})
            return

        # 批量删除发票（走后台任务，立即返回 job_id，前端轮询进度，避免大批量卡死）
        if path == "/api/invoices/delete":
            ids = payload.get("ids", [])
            jid = start_job("delete", _run_delete, ids, total=len(ids))
            _json(self, {"ok": True, "job_id": jid})
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

        # 对账修复：把磁盘上"下了却没进表"的孤儿 PDF 回填进库（后台线程）
        if path == "/api/reconcile":
            if engine.FETCH_RUNNING:
                _json(self, {"ok": False, "msg": "有任务在运行，请稍候"})
                return
            dry = bool(payload.get("dry_run", False))
            def _reconcile_job():
                engine.FETCH_RUNNING = True
                try:
                    engine.reconcile(dry_run=dry)
                finally:
                    engine.FETCH_RUNNING = False
            threading.Thread(target=_reconcile_job, daemon=True).start()
            _json(self, {"ok": True, "msg": "已启动对账修复" + ("（仅预览，不写库）" if dry else "")})
            return

        # 连接测试（统一走 api 门面，与 CLI 共用同一套逻辑，避免分叉）
        if path.startswith("/api/accounts/") and path.endswith("/test"):
            acc_id = _account_id_from_path(path)
            _json(self, api.test_connection(acc_id))
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

        # 网页上传本地 PDF（逐文件写盘+解析较重，走后台任务并回报进度）
        if path == "/api/upload":
            files = payload.get("files", [])
            jid = start_job("upload", _run_upload, files, total=len(files))
            _json(self, {"ok": True, "job_id": jid})
            return

        # 勾选/全选 → 打包 PDF + 清单导出（后台构建 zip，完成后前端按 job 下载，避免大批量阻塞）
        if path == "/api/export":
            ids = payload.get("ids", [])
            jid = start_job("export", _run_export, ids, total=len(ids))
            _json(self, {"ok": True, "job_id": jid})
            return

        # 模板匹配：上传 xlsx 文件，解析模板结构（列映射、行数据、ambiguous 标记）
        if path == "/api/match/upload":
            import base64
            b64 = payload.get("b64", "")
            try:
                file_bytes = base64.b64decode(b64)
            except Exception:
                _json(self, {"ok": False, "msg": "文件解码失败"})
                return
            try:
                info = matching.parse_template(file_bytes)
            except Exception as e:
                _json(self, {"ok": False, "msg": f"解析失败：{type(e).__name__}: {e}"})
                return
            fid = uuid.uuid4().hex[:12]
            with _MATCH_LOCK:
                MATCH_FILES[fid] = {"template_bytes": file_bytes, "template_info": info, "columns_map": info.get("columns"), "result": None, "original_name": payload.get("name", "")}
            _json(self, {"ok": True, "file_id": fid, "template_info": info})
            return

        # 模板匹配：用户确认列映射
        if path == "/api/match/columns":
            fid = payload.get("file_id")
            columns_map = payload.get("columns_map")
            with _MATCH_LOCK:
                f = MATCH_FILES.get(fid)
            if not f:
                _json(self, {"ok": False, "msg": "文件不存在，请重新上传"})
                return
            with _MATCH_LOCK:
                MATCH_FILES[fid]["columns_map"] = columns_map
            _json(self, {"ok": True, "file_id": fid})
            return

        # 模板匹配：启动匹配后台任务
        if path == "/api/match/run":
            fid = payload.get("file_id")
            date_range_days = int(payload.get("date_range_days", 30))
            overwrite = bool(payload.get("overwrite", False))
            with _MATCH_LOCK:
                f = MATCH_FILES.get(fid)
            if not f:
                _json(self, {"ok": False, "msg": "文件不存在，请重新上传"})
                return
            def _match_job(report, fid, date_range_days, overwrite):
                with _MATCH_LOCK:
                    f = MATCH_FILES.get(fid)
                if not f:
                    return
                template_bytes = f["template_bytes"]
                columns_map = f["columns_map"]
                try:
                    result = matching.run_match(template_bytes, columns_map, None, date_range_days, overwrite)
                    with _MATCH_LOCK:
                        MATCH_FILES[fid]["result"] = result
                    report(msg="匹配完成")
                except Exception as e:
                    report(msg=f"匹配失败：{type(e).__name__}: {e}")
                    raise
            jid = start_job("match", _match_job, fid, date_range_days, overwrite)
            _json(self, {"ok": True, "job_id": jid, "file_id": fid})
            return

        # 模板匹配：生成回填 xlsx（后台任务，复用 /api/jobs/<id>/download 下载）
        if path == "/api/match/export":
            fid = payload.get("file_id")
            confirmed = payload.get("confirmed_matched", [])  # [{"row_idx": int, "invoice_no": str}, ...]
            with _MATCH_LOCK:
                f = MATCH_FILES.get(fid)
            if not f:
                _json(self, {"ok": False, "msg": "文件不存在"})
                return
            def _export_match_job(report, fid, confirmed):
                with _MATCH_LOCK:
                    f = MATCH_FILES.get(fid)
                if not f:
                    return
                template_bytes = f["template_bytes"]
                columns_map = f["columns_map"]
                xlsx_bytes = matching.generate_filled_xlsx(template_bytes, columns_map, confirmed)
                os.makedirs(EXPORT_DIR, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix="match_", suffix=".xlsx", dir=EXPORT_DIR)
                os.close(fd)
                with open(tmp, "wb") as fh:
                    fh.write(xlsx_bytes)
                # 默认文件名：原文件名去掉 .xlsx 后缀 + "_已回填.xlsx"
                orig = f.get("original_name", "") or "模板"
                if orig.lower().endswith(".xlsx"):
                    orig = orig[:-5]
                elif orig.lower().endswith(".xls"):
                    orig = orig[:-4]
                report.result(tmp, orig + "_已回填.xlsx")
                report(msg="已生成回填文件")
            jid = start_job("match_export", _export_match_job, fid, confirmed)
            _json(self, {"ok": True, "job_id": jid})
            return

        self.send_error(404)

    # ----------------------------------------------------- 导出 zip
    def _export_zip(self, ids):
        """[已废弃] 导出改为后台任务（见 _run_export / _build_export_zip），
        此同步版本保留仅为兼容，不再被路由调用。"""
        path = _build_export_zip(ids, None)
        if not path:
            self.send_error(404)
            return
        self._send_file(path, "application/zip", "发票打包.zip")
        try:
            os.remove(path)
        except OSError:
            pass


def _build_export_zip(ids, report):
    """构建导出 zip（PDF 打包 + csv/md/xlsx 清单）写入临时文件，返回文件路径；
    无可选导出时返回 None。report(done,total,msg) 用于回报进度，
    避免在请求线程里同步阻塞大批量导出。清单生成逻辑与原版完全一致。"""
    rows = db.get_invoices_by_ids(ids)
    if not rows:
        return None
    os.makedirs(EXPORT_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="export_", suffix=".zip", dir=EXPORT_DIR)
    os.close(fd)
    total = len(rows)
    zf = zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED)
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
        # 每 50 张回报一次进度，避免大量 PDF 时前端无反馈
        if i % 50 == 0 or i == total:
            if report:
                report(i, total, f"打包中 {i}/{total}")
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
    if report:
        report(total, total, f"已打包 {total} 张")
    return tmp


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
        "fetch_method": p.get("fetch_method") or "imap",
    }


def run(port=8000):
    db.init()
    # 清理上次遗留的导出临时 zip（正常流程下载后即删，异常退出可能残留）
    if os.path.isdir(EXPORT_DIR):
        for _f in os.listdir(EXPORT_DIR):
            try:
                os.remove(os.path.join(EXPORT_DIR, _f))
            except OSError:
                pass
    threading.Thread(target=scheduler_loop, daemon=True).start()
    # 用 ThreadingHTTPServer 替代单线程 HTTPServer：
    # 否则「连接测试」「手动抓取」「自动抓取」等并发请求会被串行排队，
    # 对 agent 高频调用 / 多标签页同时操作不友好。
    # 每个请求各自开 SQLite 连接（db.conn 每调用新建），配合 WAL 模式天然并发安全；
    # 引擎里的 IMAP 连接池也带锁，跨线程复用安全。
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"发票中枢控制台已启动: http://127.0.0.1:{port}")
    print("（Ctrl+C 停止）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    run()
