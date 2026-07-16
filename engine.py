"""
引擎层 (Engine) —— 只负责"操作数据"：
  1. 用 IMAP 把各邮箱的发票邮件捞进来（附件式 + 正文链接式 PDF 都下）
  2. 用 config/rules.json 里的【数据驱动】规则提取字段
  3. 把结果写进 SQLite（db.py），并按发票号去重

所有"哪些是发票""字段怎么提"都来自 rules.json / 账号表，不写死在代码里。
"""
import imaplib
import email
import ssl
import json
import os
import re
import datetime as dt
from email.header import decode_header
from urllib.parse import urljoin

import shutil as _shutil

import requests
from bs4 import BeautifulSoup
import fitz  # PyMuPDF

import db

# ----------------------------------------------------------- OCR 兜底配置
# PyMuPDF 的 get_textpage_ocr 是 Tesseract 的封装。
# 需要本机装 tesseract.exe 并设置 TESSDATA_PREFIX 指向 tessdata 目录。
# 没装也能跑（只是扫描件/拍照件 PDF 无法识别）。
TESSERACT_AVAILABLE = bool(_shutil.which("tesseract"))
# 默认 OCR 语言：中文简体 + 英文（发票号、金额数字）
OCR_LANG = os.environ.get("INVOICE_HUB_OCR_LANG", "chi_sim+eng")
# OCR 渲染 dpi，300 对发票这类小字号文档足够
OCR_DPI = int(os.environ.get("INVOICE_HUB_OCR_DPI", "300"))

# 网易系邮箱（163/126/188/yeah）要求客户端发送 IMAP ID（RFC 2971）申报身份，
# 否则 SELECT 时返回 "Unsafe Login. Please contact kefu@188.com for help"。
# Python imaplib 默认不认识 ID 命令，必须先注册到 Commands 字典，
# 否则 _simple_command("ID", ...) 会在内部被拒绝（错误被 try/except 吞掉 → ID 实际没发出去）。
# 'NONAUTH' = login 之前可发；'AUTH' = login 之后可发；'SELECTED' = 选中文件夹后可发。
imaplib.Commands['ID'] = ('AUTH', 'SELECTED', 'NONAUTH')

HERE = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.path.join(HERE, "config", "rules.json")


def log(msg):
    print(msg, flush=True)
    FETCH_LOG.append(msg)


# 抓取运行状态（供 Web 控制台轮询展示）
FETCH_RUNNING = False
FETCH_LOG = []
FETCH_LAST_TS = ""


def load_rules():
    with open(RULES_PATH, encoding="utf-8") as f:
        return json.load(f)


# ----------------------------------------------------------- 工具
def decode_mime(s):
    if not s:
        return ""
    out = ""
    for text, enc in decode_header(s):
        if isinstance(text, bytes):
            try:
                out += text.decode(enc or "utf-8", errors="replace")
            except Exception:
                out += text.decode("utf-8", errors="replace")
        else:
            out += text
    return out


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]+', "_", s or "").strip()[:80]


# IMAP SINCE 需要 "DD-Mon-YYYY" 且月份必须为英文缩写，强制映射避免系统 locale 影响
_IMAP_MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
def imap_date(date_str):
    d = dt.datetime.strptime(date_str, "%Y-%m-%d")
    return f"{d.day:02d}-{_IMAP_MONTHS[d.month-1]}-{d.year}"


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


# ----------------------------------------------------------- 数据驱动解析
def coerce_money(v):
    try:
        return float(v.replace(",", "").replace("¥", "").replace("￥", "").strip())
    except Exception:
        return None


def apply_rules(text, rules):
    """按 rules.json 里的 pattern 列表顺序提取字段。换模板只改 rules.json。"""
    flat = re.sub(r"\s+", " ", text or "")
    out = {}
    fields = rules.get("fields", {})

    for field, spec in fields.items():
        if field == "city":
            # 城市是"在正文里找关键词"的启发式，不是正则捕获
            for c in spec.get("cities", []):
                if c in (text or ""):
                    out["city"] = c
                    break
            continue
        for pat in spec.get("patterns", []):
            m = re.search(pat, flat)
            if m:
                val = m.group(1).strip() if m.lastindex else m.group(0).strip()
                if spec.get("coerce") == "money":
                    val = coerce_money(val)
                out[field] = val
                break
        # 兜底：没命中"价税合计"等明确字段时，取正文里最大的 ¥ 金额（最可能是合计）
        if field not in out and spec.get("fallback") == "max_money":
            moneys = re.findall(r"[¥￥]\s*([\d,]+\.\d{2})", flat)
            if moneys:
                out[field] = max(coerce_money(x) for x in moneys)
    return out


def _ocr_textpage(page):
    """对单页做 OCR，返回 fitz.TextPage。失败返回 None。"""
    try:
        return page.get_textpage_ocr(
            flags=fitz.TEXTFLAGS_TEXT,
            language=OCR_LANG,
            dpi=OCR_DPI,
        )
    except Exception as e:
        log(f"[OCR] 页面 OCR 失败: {e}")
        return None


def extract_text_from_pdf(pdf_path):
    """提取 PDF 全文。若 PDF 无文本层（扫描件/拍照件），自动 fallback 到 OCR。
    Tesseract 不可用时直接返回空字符串，由后续 note 字段记录"未识别"原因。"""
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return ""
    # 第一遍：尝试原生文本层（电子发票走快速路径）
    parts = []
    needs_ocr = False
    for page in doc:
        text = page.get_text()
        if text.strip():
            parts.append(text)
        else:
            # 至少一页文本层为空 → 整体改走 OCR
            needs_ocr = True
            break
    if not needs_ocr:
        return "".join(parts)
    # 第二遍：OCR 兜底（扫描件）
    if not TESSERACT_AVAILABLE:
        log(f"[OCR] {os.path.basename(pdf_path)} 无文本层但 Tesseract 未安装，跳过")
        return ""
    log(f"[OCR] {os.path.basename(pdf_path)} 无文本层，启动 OCR（lang={OCR_LANG}, dpi={OCR_DPI}）")
    parts = []
    for page in doc:
        tp = _ocr_textpage(page)
        if tp:
            parts.append(tp.get_text())
    return "".join(parts)


def _is_company_name(s):
    """判断字符串是否像公司名：含「有限公司/公司/事务所/中心/院/厂/店」等关键字，且为纯中文/英文/数字组合。"""
    s = (s or "").strip()
    if len(s) < 4 or len(s) > 40:
        return False
    keywords = ("有限公司", "有限责任公司", "公司", "事务所", "中心", "学院", "医院", "研究院",
                "工厂", "商店", "饭店", "酒店", "银行", "局", "所", "站", "社")
    return any(k in s for k in keywords)


def _is_tax_id(s):
    """判断是否为统一社会信用代码/纳税人识别号：18 位，字母数字混合。"""
    s = (s or "").strip()
    return bool(re.fullmatch(r"[A-Z0-9]{18}", s))


def _parse_dict_blocks(d):
    """从 fitz get_text('dict') 的返回里提取文本块及其 bbox 坐标。"""
    blocks = []
    for blk in d.get("blocks", []):
        if blk.get("type") != 0:
            continue
        lines = []
        for ln in blk.get("lines", []):
            txt = "".join(s["text"] for s in ln.get("spans", []))
            if txt.strip():
                lines.append(txt.strip())
        if not lines:
            continue
        text = "".join(lines)
        bbox = blk["bbox"]
        blocks.append({
            "x0": bbox[0], "y0": bbox[1],
            "x1": bbox[2], "y1": bbox[3],
            "cx": (bbox[0] + bbox[2]) / 2,
            "cy": (bbox[1] + bbox[3]) / 2,
            "text": text,
        })
    return blocks


def extract_fields_by_layout(pdf_path):
    """基于 PDF 坐标布局提取字段（专门应对全电发票这种「标签竖排、值横排」的版式）。
    策略：
      1. 用 fitz get_text('dict') 取每个 text block 的 bbox 坐标
      2. 按 x 坐标把页面分成左右两半（购买方在左，销售方在右）
      3. 在「名称：」标签附近找公司名；在「统一社会信用代码」附近找税号
      4. 顶部区域找发票号、开票日期
    返回 dict，字段可能不全（缺失的由后续正则法补）。"""
    result = {}
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return result
    if not doc.page_count:
        return result
    page = doc[0]
    page_w = page.rect.width
    mid_x = page_w / 2
    d = page.get_text("dict")
    blocks = _parse_dict_blocks(d)

    # 原生 dict 没拿到块（扫描件/拍照件）→ 尝试 OCR 的 TextPage 再取 dict
    if not blocks and TESSERACT_AVAILABLE:
        tp = _ocr_textpage(page)
        if tp:
            d = page.get_text("dict", tp=tp)
            blocks = _parse_dict_blocks(d)

    if not blocks:
        return result

    # —— 购买方 / 销售方：公司名 + 税号 ——
    # 先按 y 排序，找到上方区域（y < 页面高度的 40%）里的所有块
    top_blocks = [b for b in blocks if b["cy"] < page.rect.height * 0.4]

    def find_company_and_tax(side_blocks):
        """在一组块里找「公司名」和「税号」，按 y 距离配对。"""
        comps = [b for b in side_blocks if _is_company_name(b["text"])]
        taxids = [b for b in side_blocks if _is_tax_id(b["text"])]
        company = None
        taxid = None
        if comps:
            company = min(comps, key=lambda b: b["y0"])["text"]
        if taxids:
            taxid = min(taxids, key=lambda b: b["y0"])["text"]
        return company, taxid

    left_blocks = [b for b in top_blocks if b["cx"] < mid_x]
    right_blocks = [b for b in top_blocks if b["cx"] >= mid_x]

    buyer, buyer_tax = find_company_and_tax(left_blocks)
    seller, seller_tax = find_company_and_tax(right_blocks)
    if buyer:
        result["buyer"] = buyer
    if seller:
        result["seller"] = seller

    # —— 发票号：找 15-20 位纯数字的块，在顶部区域 ——
    for b in top_blocks:
        m = re.fullmatch(r"\d{15,20}", b["text"].strip())
        if m:
            result["invoice_no"] = b["text"].strip()
            break

    # —— 开票日期：找 "XXXX年XX月XX日" 格式 ——
    for b in top_blocks:
        m = re.search(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", b["text"])
        if m:
            result["invoice_date"] = m.group(0)
            break

    # —— 城市：在左半区公司名里找城市关键词 ——
    city_names = ["北京", "天津", "上海", "重庆",
                  "广州", "深圳", "杭州", "南京", "苏州", "成都", "武汉", "西安",
                  "东莞", "佛山", "宁波", "青岛", "大连", "厦门", "无锡", "长沙",
                  "郑州", "济南", "福州", "合肥", "南昌", "南宁", "昆明", "贵阳",
                  "太原", "石家庄", "沈阳", "长春", "哈尔滨", "兰州", "乌鲁木齐"]
    for name in (buyer, seller):
        if not name:
            continue
        for c in city_names:
            if c in name:
                result["city"] = c
                break
        if "city" in result:
            break

    return result


def parse_pdf_to_invoice(pdf_path, account_id, email_id=None, source_type="attachment"):
    """把一个 PDF 解析成发票记录 dict（未入库）。
    两步提取：先布局法（坐标，应对全电发票竖排标签），再正则法（兜底，补全金额等）。"""
    rules = load_rules()
    text = extract_text_from_pdf(pdf_path)
    # 第一步：布局法（坐标）——应对全电发票等标签和值不在同一行的版式
    layout_info = extract_fields_by_layout(pdf_path)
    # 第二步：正则法（rules.json）——补全金额等布局法没提取的字段
    regex_info = apply_rules(text, rules)
    # 合并：布局法优先，正则法兜底
    info = {**regex_info, **layout_info}
    rel = os.path.relpath(pdf_path, db.HERE)
    inv = {
        "email_id": email_id,
        "account_id": account_id,
        "buyer": info.get("buyer", ""),
        "seller": info.get("seller", ""),
        "amount": info.get("amount"),
        "invoice_no": info.get("invoice_no", ""),
        "invoice_date": (info.get("invoice_date") or "").replace(" ", ""),
        "city": info.get("city", ""),
        "pdf_path": rel,
        "source_type": source_type,
        "note": "" if info.get("invoice_no") else "未识别到发票号",
    }
    return inv


# ----------------------------------------------------------- IMAP 拉取
def _send_imap_id(M, tag=""):
    """向网易系邮箱（163/126/188/yeah）发送 IMAP ID 命令（RFC 2971）申报客户端身份。
    必须先在模块顶部注册 imaplib.Commands['ID']，否则 imaplib 会拒绝发送。
    网易官方要求字段：name / version / vendor / support-email。"""
    try:
        # 参数构造：("name" "INVOICE-HUB" "version" "1.0" "vendor" "INVOICE-HUB" "support-email" "noreply@local")
        args = ("name", "INVOICE-HUB",
                "version", "1.0",
                "vendor", "INVOICE-HUB",
                "support-email", "noreply@local")
        arg_str = '("' + '" "'.join(args) + '")'
        typ, dat = M._simple_command("ID", arg_str)
        # 消费 untagged 响应（* ID (...)），避免污染后续命令的应答队列
        M._untagged_response(typ, dat, "ID")
        if tag:
            log(f"    [{tag}] ID 已发送")
    except Exception as e:
        log(f"    [{tag or 'ID'} 命令失败, 已忽略] {type(e).__name__}: {e}")


def connect(acc):
    """登录并选中文件夹，返回可用连接。任何一步失败都显式抛错（让测试/抓取都能感知）。"""
    host = (acc.get("imap_host") or "").strip()
    if not host:
        raise RuntimeError("未配置 IMAP 主机（该账号不是可抓取的邮箱）")
    log(f"  连接 {acc['email']} ({host}:{acc.get('imap_port',993)}) ...")
    ctx = ssl.create_default_context()
    try:
        if acc.get("use_ssl", 1):
            M = imaplib.IMAP4_SSL(host, int(acc.get("imap_port", 993) or 993), ssl_context=ctx)
        else:
            M = imaplib.IMAP4(host, int(acc.get("imap_port", 143) or 143))
    except Exception as e:
        raise RuntimeError(f"连接失败：{e}")
    # 关键：网易系邮箱要求 IMAP ID。login 之前发一次（NONAUTH 状态）。
    _send_imap_id(M, "pre-login")
    try:
        M.login(acc["email"], acc["password"])
    except Exception as e:
        raise RuntimeError(f"登录失败：{e}")
    # login 之后再发一次 ID（AUTH 状态）——网易官方 Java 示例就是 login 后发 ID。
    # 双保险：有些邮箱只认 pre-login ID，有些只认 post-login ID，两个都发覆盖所有情况。
    _send_imap_id(M, "post-login")
    # 选中文件夹：先按配置，失败回退 INBOX；再失败显式报错
    folder = (acc.get("folder") or "INBOX").strip() or "INBOX"
    typ, data = M.select(folder)
    if typ != "OK" and folder.upper() != "INBOX":
        log(f"    文件夹 '{folder}' 选中失败，回退 INBOX")
        typ, data = M.select("INBOX")
    if typ != "OK":
        M.logout()
        raise RuntimeError(f"选中文件夹失败：'{folder}'（服务器返回 {typ} {data}）")
    return M


def match_invoice(subject, keywords):
    s = (subject or "").lower()
    return any(k.lower() in s for k in keywords)


def extract_body(msg):
    html, text = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if part.get_content_type() == "text/html":
                html += decoded
            elif part.get_content_type() == "text/plain":
                text += decoded
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html = decoded
            else:
                text = decoded
        except Exception:
            pass
    return html, text


def get_attachments(msg):
    atts = []
    for part in msg.walk():
        fname = decode_mime(part.get_filename())
        if fname and fname.lower().endswith(".pdf"):
            payload = part.get_payload(decode=True)
            if payload:
                atts.append((fname, payload))
    return atts


PDF_LINK_PATTERNS = [re.compile(x, re.I) for x in ["下载", "pdf", "发票", "download", "查看"]]


def find_pdf_links(html, text):
    links = []
    if html:
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            label = a.get_text(strip=True)
            if not href.lower().startswith("http"):
                continue
            score = 10 if href.lower().endswith(".pdf") else 0
            for pat in PDF_LINK_PATTERNS:
                if pat.search(label) or pat.search(href):
                    score += 2
            if score > 0:
                links.append((score, href, label))
    for m in re.findall(r"https?://[^\s\"'<>]+", text or ""):
        if m.lower().endswith(".pdf"):
            links.append((10, m, "text-link"))
    links.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for _, href, label in links:
        if href not in seen:
            seen.add(href)
            out.append((href, label))
    return out


def try_download_pdf(url, session, out_path):
    try:
        r = session.get(url, timeout=30, allow_redirects=True)
        content = r.content
        ct = r.headers.get("Content-Type", "").lower()
        if content[:4] == b"%PDF" or "pdf" in ct:
            with open(out_path, "wb") as f:
                f.write(content)
            return True
        if "html" in ct:
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.find_all("a", href=True):
                h = a["href"]
                if h.lower().endswith(".pdf"):
                    h = urljoin(url, h)
                    r2 = session.get(h, timeout=30)
                    if r2.content[:4] == b"%PDF":
                        with open(out_path, "wb") as f:
                            f.write(r2.content)
                        return True
        return False
    except Exception:
        return False


def fetch_account(acc, rules, session, since_override=None):
    """拉一个邮箱的发票邮件 → 下载 PDF → 解析 → 入库。返回新增发票数。
    - 统一用 IMAP UID 语义（uid search / uid fetch），UID 单调稳定。
    - fetch_mode='incremental'（默认）：只处理 uid > last_uid 的邮件。
    - fetch_mode='full'：拉 default_since 范围内全部，email_exists 兜底去重。
    - 不动邮箱状态（不 STORE \\Seen、不删邮件）。"""
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
        # 幂等 + 自愈判定：邮件不存在 / 或发票或 PDF 缺失 → 需重新拉取；
        # 已齐全（非发票邮件 / 发票+文件均在）→ 跳过，绝不重复下载。
        if not db.needs_refetch(acc["id"], uid_str):
            continue
        typ, data = M.uid("fetch", str(uid), "(RFC822)")
        if typ != "OK":
            continue
        msg = email.message_from_bytes(data[0][1])
        subject = decode_mime(msg.get("Subject"))
        sender = decode_mime(msg.get("From"))
        date = decode_mime(msg.get("Date"))
        html, text = extract_body(msg)
        is_inv = match_invoice(subject, keywords)

        # 取（或建）邮件行 id，供发票回链。insert 用 OR IGNORE，已存在则复用原 id。
        eid = db.get_email_id(acc["id"], uid_str)
        if eid is None:
            eid = db.insert_email({"account_id": acc["id"], "uid": uid_str, "subject": subject,
                                    "from_addr": sender, "date": date, "body_text": text,
                                    "body_html": html, "is_invoice": 1 if is_inv else 0})
        else:
            db.set_email_invoice(eid, is_inv)

        if not is_inv:
            # 非发票邮件：已存档（is_invoice=0），跳过解析
            new_max_uid = max(new_max_uid, uid)
            continue

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

        # email_id 回链，保证"发票→邮件"可追踪，删除/对账时才不会误删
        for p, stype in pdfs:
            inv = parse_pdf_to_invoice(p, acc["id"], email_id=eid, source_type=stype)
            if db.insert_invoice(inv):
                new_inv += 1
        new_max_uid = max(new_max_uid, uid)

    M.logout()
    # 推进水位线
    if new_max_uid > last_uid:
        db.update_last_uid(acc["id"], new_max_uid)
    return new_inv


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


def reparse_all_pdfs():
    """重新解析所有已入库发票的 PDF（用最新的解析规则），更新 buyer/seller/city/amount 等字段。
    返回 (处理数, 更新数)。"""
    import os as _os
    all_invoices = db.get_invoices({}, page=1, page_size=0)
    total = len(all_invoices)
    updated = 0
    log(f"[重新解析] 共 {total} 条发票，开始重新解析 PDF...")
    for idx, inv in enumerate(all_invoices):
        pdf_rel = inv.get("pdf_path")
        if not pdf_rel:
            continue
        pdf_path = _os.path.join(db.HERE, pdf_rel)
        if not _os.path.isfile(pdf_path):
            continue
        try:
            new_inv = parse_pdf_to_invoice(pdf_path, inv["account_id"],
                                           email_id=inv.get("email_id"),
                                           source_type=inv.get("source_type", "attachment"))
        except Exception as e:
            log(f"  [{idx+1}/{total}] #{inv['id']} 解析失败: {e}")
            continue
        # 比较需要更新的字段
        fields_to_update = {}
        for key in ("buyer", "seller", "amount", "invoice_no", "invoice_date", "city", "note"):
            old_val = inv.get(key) or ""
            new_val = new_inv.get(key) or ""
            if str(old_val) != str(new_val):
                fields_to_update[key] = new_val
        if fields_to_update:
            db.update_invoice_fields(inv["id"], fields_to_update)
            updated += 1
            log(f"  [{idx+1}/{total}] #{inv['id']} 更新: {', '.join(fields_to_update.keys())}")
    log(f"[重新解析] 完成，更新了 {updated}/{total} 条")
    return total, updated


def reconcile(dry_run=False):
    """对账修复：让【磁盘 PDF】与【发票表】重新对齐。
    原则：表是真相源，文件是派生物——任何"下了却没进表"的孤儿 PDF 都重新解析入库（而非删除），
    这样既能找回你想要的发票，又始终满足"文件必有对应表行"。
    流程：
      1. 收集发票表已引用的所有 pdf_path；
      2. 遍历每个账号本地 PDF 目录，凡是"未被任何发票引用"的 PDF → 解析并插入发票表
         （若文件名以 uid_ 开头，则回链到对应 emails 行）；
      3. dry_run=True 只报告、不写库、不动文件。
    返回报告 dict。
    """
    report = {"reingested": [], "skipped": 0, "failed": []}
    c = db.conn()
    referenced = {r["pdf_path"] for r in c.execute(
        "SELECT pdf_path FROM invoices WHERE pdf_path IS NOT NULL").fetchall()}
    c.close()

    for acc in db.get_accounts():
        acc_dir = os.path.join(db.PDF_DIR, safe_name(acc["email"]))
        if not os.path.isdir(acc_dir):
            continue
        for fname in sorted(os.listdir(acc_dir)):
            if not fname.lower().endswith(".pdf"):
                continue
            rel = os.path.relpath(os.path.join(acc_dir, fname), db.HERE)
            if rel in referenced:
                report["skipped"] += 1
                continue
            # 尝试从文件名解析 uid，回链到 emails
            email_id = None
            m = re.match(r"^(\d+)_", fname)
            if m:
                email_id = db.get_email_id(acc["id"], m.group(1))
            pdf_path = os.path.join(db.HERE, rel)
            try:
                inv = parse_pdf_to_invoice(pdf_path, acc["id"],
                                           email_id=email_id, source_type="reconcile")
            except Exception as e:
                log(f"[对账] {fname} 解析失败，跳过: {e}")
                report["failed"].append(rel)
                continue
            if dry_run:
                report["reingested"].append({"file": rel, "invoice_no": inv.get("invoice_no")})
                continue
            if db.insert_invoice(inv):
                log(f"[对账] 回填 {fname} -> 号={inv.get('invoice_no')} 买方={inv.get('buyer')}")
                report["reingested"].append({"file": rel, "invoice_no": inv.get("invoice_no")})
    log(f"[对账] 完成：回填 {len(report['reingested'])} 张，跳过 {report['skipped']} 张，"
         f"失败 {len(report['failed'])} 张")
    return report
