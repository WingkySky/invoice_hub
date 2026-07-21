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
import socket
import threading
import time
from email.header import decode_header
from urllib.parse import urljoin

import shutil as _shutil

import requests
from bs4 import BeautifulSoup
import fitz  # PyMuPDF

import base64
import gzip

# 51发票（百望云）PDF 下载依赖国密 SM4(ECB) 解密。gmssl 是纯 Python 实现，安装：
#   pip install gmssl
# 若未安装，51发票短链下载会在运行时给出明确报错提示。
try:
    from gmssl import sm4 as _gmssl_sm4
except ImportError:
    _gmssl_sm4 = None

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

# =========================================================== IMAP 连接管理
# 腾讯企业邮箱对"短时间频繁登录"极敏感，会触发风控导致测试/抓取全部失败；
# 同时 IMAP 空闲连接会被服务端静默断开（断链），复用死连接会直接报错。
# 本区块统一解决这两类问题：
#   1) 连接池复用 —— 同一账号尽量只登录一次，规避频繁登录风控；
#   2) 线程安全借还 —— 同一账号连接串行使用，杜绝多线程往同一 socket 交错发命令导致协议错乱断链；
#   3) 存活探活 + 自动重连 —— 借出前用 NOOP 探活，死连接丢弃并重建；
#   4) 登录节流 + 退避重试 —— 同一账号登录最小间隔 + 瞬时网络错误指数退避；
#   5) 风控识别 —— 登录被风控拦截时给出明确提示而非反复重试（重试只会加重风控）。

# 登录节流：同一账号两次登录之间最小间隔（秒）。避免短时间反复登录触发腾讯企业邮箱风控。
_LOGIN_MIN_GAP = 3.0
# 登录/建连失败时的瞬时网络错误最大重试次数（指数退避）。
_MAX_LOGIN_RETRY = 3
# 连接池空闲回收阈值（秒）。腾讯 IMAP 空闲超时通常 < 30min，提前回收更稳，避免借到半死连接。
_POOL_TIMEOUT = 240

# key = (acc_id, password)；value = (imap_obj, last_used_ts)
_POOL = {}
_POOL_LOCK = threading.Lock()          # 保护 _POOL 的全局锁
_CHECKOUT = {}                         # key -> 借还锁，保证同一账号连接串行"借出"
_CHECKOUT_LOCK = threading.Lock()      # 保护 _CHECKOUT 字典
_LOGIN_AT = {}                         # key -> 上次成功登录的时间戳（用于登录节流）


class ImapLoginError(RuntimeError):
    """登录/连接失败的统一异常。
    retryable=True  → 瞬时网络错误（断链/超时/连接重置），可退避重试；
    retryable=False → 账号/授权码/风控类错误，不应狂重试（否则加重风控）。"""

    def __init__(self, msg: str, retryable: bool = False):
        super().__init__(msg)
        self.retryable = retryable


def _detect_provider(acc):
    """根据 provider 字段或 IMAP 主机推断邮箱服务商，用于决定 IMAP ID 申报内容。
    返回 'tencent' / 'netease' / 'generic'。腾讯企业邮箱域名含 exmail / qq.com。"""
    prov = (acc.get("provider") or "").lower()
    host = (acc.get("imap_host") or "").lower()
    if prov in ("tencent", "exmail", "qq") or "exmail" in host or "qq.com" in host:
        return "tencent"
    if prov in ("netease", "163", "126", "yeah", "188") or any(
        h in host for h in ("163.com", "126.com", "yeah.net", "188.com")
    ):
        return "netease"
    return "generic"


def _is_risk_control(msg):
    """启发式判断 IMAP 报错是否来自邮箱风控（登录过密/账号锁定/临时限制等）。
    命中则返回 True，让上层给出明确提示且不反复重试。"""
    m = (msg or "").lower()
    keys = ("too many", "频繁", "frequency", "locked", "lock", "风控",
            "rate limit", "ratelimit", "temporarily", "blocked", "临时锁定",
            "try again later", "exceed", "超过", "异常登录", "安全", "denied")
    return any(k in m for k in keys)


def _imap_id_args(provider):
    """返回 IMAP ID 命令的参数字符串（RFC 2971）。不同服务商期望字段不同：
    - netease：强制要求 name/version/vendor/support-email，否则 SELECT 报 Unsafe Login；
    - tencent（企业邮箱 exmail）：建议申报标准 ID，避免被判定为陌生客户端触发风控；
    - generic：最小可用 ID。"""
    if provider in ("netease", "tencent"):
        args = ("name", "INVOICE-HUB", "version", "1.0",
                "vendor", "INVOICE-HUB", "support-email", "noreply@local")
    else:
        args = ("name", "INVOICE-HUB", "version", "1.0")
    return '("' + '" "'.join(args) + '")'


def _send_imap_id(M, tag="", provider="generic"):
    """发送 IMAP ID 命令（RFC 2971）申报客户端身份。
    必须在模块顶部注册 imaplib.Commands['ID'] 才能发送。provider 决定字段内容。"""
    try:
        arg_str = _imap_id_args(provider)
        typ, dat = M._simple_command("ID", arg_str)
        M._untagged_response(typ, dat, "ID")  # 消费 untagged 响应，避免污染后续命令队列
        if tag:
            log(f"    [{tag}] ID 已发送")
    except Exception as e:
        log(f"    [{tag or 'ID'} 命令失败, 已忽略] {type(e).__name__}: {e}")


def conn_alive(M):
    """用 NOOP 轻量探活：连接正常返回 True；断链/超时返回 False。
    比 select() 更轻，且不依赖已选中的文件夹。"""
    try:
        typ, _ = M.noop()
        return typ == "OK"
    except Exception:
        return False


def _pool_key(acc):
    return (acc["id"], acc.get("password", ""))


def _account_lock(acc):
    """获取（或创建）该账号专属的借还锁，保证同一账号连接串行使用。"""
    key = _pool_key(acc)
    with _CHECKOUT_LOCK:
        if key not in _CHECKOUT:
            _CHECKOUT[key] = threading.RLock()
        return _CHECKOUT[key]


def _pool_cleanup():
    """回收空闲超时或已死的连接。"""
    now = time.time()
    with _POOL_LOCK:
        stale = [k for k, (m, t) in _POOL.items() if now - t > _POOL_TIMEOUT]
        for k in stale:
            m = _POOL[k][0]
            try:
                if conn_alive(m):
                    m.logout()
            except Exception:
                pass
            del _POOL[k]


def pool_get(acc):
    """借出一个 IMAP 连接：优先复用池内健康连接，否则新建（含登录节流 + 网络错误退避重试）。
    同一账号连接串行借出（_account_lock），避免多线程交错命令导致协议错乱断链。"""
    acc_lock = _account_lock(acc)
    acc_lock.acquire()
    try:
        _pool_cleanup()
        key = _pool_key(acc)
        with _POOL_LOCK:
            if key in _POOL:
                M, _ = _POOL.pop(key)
                if conn_alive(M):
                    return M
                # 连接已死，丢弃并重建
                try:
                    M.logout()
                except Exception:
                    pass
        return connect(acc)
    except Exception:
        # 借出失败必须释放账号锁，否则会死锁后续同账号请求
        acc_lock.release()
        raise


def pool_put(acc, M):
    """归还连接回池中（不主动断开，留给 _pool_cleanup 或进程退出）。
    归还前再探活一次：若连接已死则直接登出丢弃，避免下次借到死连接。
    M 为 None 时也安全返回（仍会释放借还锁）。"""
    acc_lock = _account_lock(acc)
    try:
        if M is None:
            return
        if conn_alive(M):
            with _POOL_LOCK:
                _POOL[_pool_key(acc)] = (M, time.time())
        else:
            try:
                M.logout()
            except Exception:
                pass
    finally:
        acc_lock.release()


def _discard(M):
    """仅登出并丢弃一个连接，不动连接池与借还锁（供重试时丢弃已死连接）。"""
    if M is None:
        return
    try:
        M.logout()
    except Exception:
        pass


def _maybe_retry_network(acc, exc, attempt, stage):
    """瞬时网络错误的退避重试：达到上限则抛出 retryable=True 的 ImapLoginError。"""
    if attempt >= _MAX_LOGIN_RETRY:
        raise ImapLoginError(
            f"{stage}失败（网络错误，已重试{attempt}次）：{exc}", retryable=True)
    backoff = min(2 ** attempt, 30)  # 指数退避 1s/2s/4s...，封顶 30s
    log(f"    [网络错误] {stage}失败：{exc}；{backoff}s 后第 {attempt + 1} 次重试")
    time.sleep(backoff)
    return connect(acc, _attempt=attempt + 1)


def connect(acc, _attempt=0):
    """登录并选中文件夹，返回可用连接。
    带三重保护：
      1) 登录节流（_LOGIN_MIN_GAP）—— 同一账号两次登录最小间隔，降低腾讯企业邮箱风控概率；
      2) 瞬时网络错误（断链/超时/连接重置/BYE）自动指数退避重试；
      3) 账号/风控类错误（密码错/授权码错/登录过密/账号锁定）直接抛出且 retryable=False，不狂重试。
    """
    host = (acc.get("imap_host") or "").strip()
    if not host:
        raise RuntimeError("未配置 IMAP 主机（该账号不是可抓取的邮箱）")
    provider = _detect_provider(acc)
    # 登录节流：同一账号两次登录之间至少间隔 _LOGIN_MIN_GAP 秒
    key = _pool_key(acc)
    now = time.time()
    last = _LOGIN_AT.get(key)
    if last is not None:
        gap = _LOGIN_MIN_GAP - (now - last)
        if gap > 0:
            log(f"    登录节流：等待 {gap:.1f}s 以避免频繁登录触发风控")
            time.sleep(gap)
    log(f"  连接 {acc['email']} ({host}:{acc.get('imap_port', 993)}) ...")
    ctx = ssl.create_default_context()
    try:
        if acc.get("use_ssl", 1):
            M = imaplib.IMAP4_SSL(host, int(acc.get("imap_port", 993) or 993), ssl_context=ctx)
        else:
            M = imaplib.IMAP4(host, int(acc.get("imap_port", 143) or 143))
    except (OSError, socket.error, imaplib.IMAP4.abort) as e:
        # 网络层建连失败 → 瞬时，可重试
        return _maybe_retry_network(acc, e, _attempt, "连接")

    # 关键：网易系/腾讯企业邮箱要求 IMAP ID。login 之前发一次（NONAUTH 状态）。
    _send_imap_id(M, "pre-login", provider)
    try:
        M.login(acc["email"], acc["password"])
    except imaplib.IMAP4.abort as e:
        # 服务端在登录阶段断开（断链）→ 瞬时，可重试
        return _maybe_retry_network(acc, e, _attempt, "登录")
    except imaplib.IMAP4.error as e:
        # 登录被拒：密码/授权码错 或 风控（频繁登录、账号锁定）
        msg = str(e)
        try:
            M.logout()
        except Exception:
            pass
        if _is_risk_control(msg):
            raise ImapLoginError(
                f"登录被邮箱风控拦截（疑似短时间内登录过密）：{msg}。"
                f"请稍候再试，或到邮箱网页端解除限制后重试。", retryable=False)
        raise ImapLoginError(f"登录失败：{msg}", retryable=False)
    except (OSError, socket.error) as e:
        return _maybe_retry_network(acc, e, _attempt, "登录")

    # login 之后再发一次 ID（AUTH 状态）—— 双保险：部分邮箱只认 post-login ID。
    _send_imap_id(M, "post-login", provider)
    # 选中文件夹：多文件夹配置的其余文件夹在 _imap_search_fetch 中逐个重新 SELECT，
    # 此处只确保连接建立时有一个有效的选中态（兼容单文件夹与历史行为）。
    # 取配置的首个文件夹作为默认选中态；失败回退 INBOX；再失败显式报错。
    folders = get_folders(acc)
    folder = folders[0]
    typ, data = M.select(_imap_folder_arg(folder))
    if typ != "OK" and folder.upper() != "INBOX":
        log(f"    文件夹 '{folder}' 选中失败，回退 INBOX")
        typ, data = M.select(_imap_folder_arg("INBOX"))
    if typ != "OK":
        try:
            M.logout()
        except Exception:
            pass
        raise ImapLoginError(f"选中文件夹失败：'{folder}'（服务器返回 {typ} {data}）", retryable=False)
    _LOGIN_AT[key] = time.time()
    return M


def get_folders(acc):
    """返回该账号要扫描的文件夹名列表（去重、保序、空值回退 INBOX）。

    兼容两种配置：
      - 单文件夹字符串（旧）："INBOX" / "Sent Messages"
      - 多文件夹（新）：用逗号「,」「，」或分号「;」「；」分隔，如 "INBOX,Sent Messages"
    这样同一账号可一次扫描收件箱 + 已发送等多个位置，覆盖更多发票邮件。"""
    raw = acc.get("folder") or "INBOX"
    if isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    else:
        # 兼容中英文逗号/分号分隔的多文件夹配置
        items = re.split(r"[,;，；]", str(raw))
    folders = []
    for f in items:
        f = (f or "").strip()
        if not f:
            f = "INBOX"
        if f not in folders:   # 去重，保留首个出现顺序
            folders.append(f)
    if not folders:
        folders = ["INBOX"]
    return folders


def _imap_folder_arg(folder):
    """构造腾讯 IMAP 能正确解析的文件夹引用参数。

    腾讯 IMAP 对带空格/特殊字符的文件夹名要求参数本身是「带双引号的字符串」，
    再由 imaplib 标准转义一层。实测：M.select('"Sent Messages"') 成功，
    而 M.select('Sent Messages') 被拒 'Select parameters!'；对无空格的 INBOX 两种都行。
    故统一在此给 folder 包裹双引号（若已带引号则原样返回），让 imaplib 再次转义后
    发给服务器的恰为 "\\"Sent Messages\\"" 这种腾讯能接受的形式。"""
    if '"' in folder:
        return folder
    return f'"{folder}"'


def _select_folder(M, folder):
    """选中目标文件夹；成功返回 True，失败返回 False。

    连接可能被池复用、停留在上次选中的文件夹，故每次借到连接后都要显式 SELECT 目标文件夹。
    选中失败（文件夹不存在/不可选）由调用方决定如何处置（通常跳过该文件夹）。
    文件夹名引用经 _imap_folder_arg 处理，兼容腾讯 IMAP 对带空格名的特殊要求。"""
    try:
        sel_typ, _ = M.select(_imap_folder_arg(folder))
    except (imaplib.IMAP4.abort, OSError, socket.error):
        return False
    return sel_typ == "OK"


def _fetch_one_folder(acc, since_imap, mode, last_uid, folder, is_primary):
    """对单个文件夹执行 IMAP 搜索 + 逐封拉取邮件原文到内存，返回 (pending, scanned_max)。

    连接策略（修复「全量只回 1 张」）：
      - 搜索与拉取统一走 pool_get / pool_put 借还连接，不直接 connect+logout；
      - 拉取按批次（BATCH）进行，每批借一个健康连接、拉完归还；
      - 若某批中途被服务端断开，丢弃死连接、用新连接从「断点」续拉剩余邮件；
      - 单封拉取返回非 OK 时记日志并跳过该封，不中断整批。
    每次借到连接后显式 SELECT 目标文件夹（连接可能被池复用、停留在上次文件夹）。

    增量水位线语义：
      - 主文件夹（is_primary=True，即配置的首个文件夹，通常为 INBOX）：
        按账号级 last_uid 增量过滤，并推进水位线；
      - 附加文件夹（is_primary=False，如 Sent Messages）：
        不同文件夹的 UID 空间相互独立，无法共用账号级水位线，故不按 last_uid 过滤，
        而是全量拉取后由 db.needs_refetch 做幂等去重（已处理的邮件会被跳过）。
    搜索被拒等非瞬态情况返回空列表（按"无新邮件"处理，不崩溃）。"""
    # 1) 搜索（借一个连接；瞬断则重建连接重试一次）
    M = pool_get(acc)
    search_ok = False
    try:
        # 确保连接选中目标文件夹（pool 复用可能停留在别的文件夹）
        if not _select_folder(M, folder):
            log(f"    [IMAP 文件夹 '{folder}' 不存在或不可选，跳过]")
            return [], last_uid
        typ, data = M.uid("search", None, "SINCE", since_imap)
        search_ok = True
    except (imaplib.IMAP4.abort, OSError, socket.error):
        # 搜索阶段瞬断：丢弃死连接、重建后重试一次
        _discard(M)
        try:
            M = connect(acc)
            if not _select_folder(M, folder):
                log(f"    [IMAP 文件夹 '{folder}' 不存在或不可选，跳过]")
                return [], last_uid
            typ, data = M.uid("search", None, "SINCE", since_imap)
            search_ok = True
        except Exception as e:
            log(f"    [IMAP 文件夹 '{folder}' 搜索失败] {type(e).__name__}: {e}")
            return [], last_uid
    finally:
        # 归还（或丢弃）当前连接；search_ok=True 时 M 为搜索所用连接
        if M is not None and conn_alive(M):
            pool_put(acc, M)
        elif M is not None:
            _discard(M)
    if not search_ok or typ != "OK":
        log(f"    [IMAP 文件夹 '{folder}' 搜索失败] typ={typ if search_ok else '?'}")
        return [], last_uid
    uids = [int(u) for u in data[0].split()]
    # 增量模式：主文件夹用账号级水位线过滤 + 200 上限兜底；
    #          附加文件夹不做 uid 过滤（UID 空间不同），仅保留 200 上限，靠 needs_refetch 幂等去重。
    # full 模式全量不截断、不过滤。
    if mode == "incremental":
        if last_uid and is_primary:
            uids = [u for u in uids if u > last_uid]
        uids = uids[-200:]
    pending = []
    scanned_max = last_uid
    remaining = list(reversed(uids))   # 从高 uid 向低处理（与历史行为一致）
    idx = 0          # 续拉断点：已成功处理到的位置
    attempts = 0     # 防止极端情况下无限循环
    BATCH = 25       # 每批借一个连接、拉完归还，降低长连接被服务端掐断的概率
    while idx < len(remaining) and attempts < 50:
        M = pool_get(acc)
        try:
            # 确保选中目标文件夹（连接可能来自池，停留在别的文件夹）
            if not _select_folder(M, folder):
                _discard(M)
                M = None
                break
            end = min(idx + BATCH, len(remaining))
            pos = idx
            while pos < end:
                uid = remaining[pos]
                # 出现在 SINCE 结果里即算"已扫描"，推进水位线（放在 needs_refetch 之前）。
                scanned_max = max(scanned_max, uid)
                uid_str = str(uid)
                # 幂等 + 自愈判定：增量模式才依赖 needs_refetch 做去重/自愈。
                # full 模式是"按 default_since 全量重扫"，必须忽略旧判定，否则新增的发票
                # 识别规则（附件/OFD/文件名格式）无法作用于历史邮件。
                if mode == "incremental" and not db.needs_refetch(acc["id"], uid_str):
                    pos += 1
                    continue
                try:
                    typ, data = M.uid("fetch", uid_str, "(RFC822)")
                except (imaplib.IMAP4.abort, OSError, socket.error):
                    # 单封拉取时连接已死：丢弃死连接，跳出本批，外层用新连接从断点续拉
                    _discard(M)
                    M = None
                    break
                if typ != "OK" or not data or not data[0]:
                    # 该封拉取失败（非瞬态）：记日志跳过，绝不静默吞掉
                    log(f"    [跳过 uid={uid}] 拉取返回 {typ}")
                    pos += 1
                    continue
                pending.append((uid, data[0][1]))
                pos += 1
            if M is None:
                # 本批因断链中断：记录断点 pos，外层 while 会借新连接从该点续拉。
                # 关键：这里【不能 break】——此 break 退出的是最外层 while 循环，
                # 会直接让整个函数返回，导致后续邮件被丢弃（正是"全量只回 1 张"的同类问题）。
                # 只更新 idx，让外层循环继续借新连接续拉即可。
                idx = pos
            else:
                idx = end   # 整批成功，推进到下一批
        finally:
            if M is not None:
                pool_put(acc, M)
        attempts += 1
    if len(remaining) != len(pending):
        log(f"    [IMAP/{folder}] 搜索命中 {len(remaining)} 封，成功拉取 {len(pending)} 封"
            + (f"，跳过 {len(remaining) - len(pending)} 封" if len(remaining) > len(pending) else ""))
    return pending, scanned_max


def _imap_search_fetch(acc, since_imap, mode, last_uid):
    """执行一次 IMAP 搜索 + 逐封拉取邮件原文到内存，返回 (pending, scanned_max)。

    多文件夹支持：遍历 acc.folder 配置的所有文件夹（逗号/分号分隔，或单文件夹），
    各文件夹独立 SELECT + 搜索 + 拉取后合并结果。
    连接策略、续拉逻辑、200 上限等见 _fetch_one_folder 注释。
    增量水位线（last_uid）只由首个文件夹（主文件夹，通常为 INBOX）推进，
    附加文件夹靠 db.needs_refetch 幂等去重，避免不同 UID 空间互相污染水位线。"""
    folders = get_folders(acc)
    all_pending = []
    scanned_max = last_uid
    for i, folder in enumerate(folders):
        is_primary = (i == 0)   # 首个文件夹为主文件夹，负责推进账号级水位线
        try:
            pending, sm = _fetch_one_folder(acc, since_imap, mode, last_uid, folder, is_primary)
            all_pending.extend(pending)
            if is_primary:
                scanned_max = max(scanned_max, sm)
        except Exception as e:
            log(f"    [IMAP 文件夹 '{folder}' 处理失败] {type(e).__name__}: {e}")
    return all_pending, scanned_max

HERE = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.path.join(HERE, "config", "rules.json")


def log(msg):
    # QUIET=True 时只写入 FETCH_LOG（供 Web 轮询），不往 stdout 打印。
    # CLI 的 --json 模式用它来抑制抓取过程日志，只输出最终 JSON 结果。
    if not QUIET:
        print(msg, flush=True)
    FETCH_LOG.append(msg)


# 抓取运行状态（供 Web 控制台轮询展示）
FETCH_RUNNING = False
FETCH_LOG = []
FETCH_LAST_TS = ""
QUIET = False  # --json 模式下置 True，让 log() 静默（仅记录到 FETCH_LOG）

# 连接池 / 连接管理相关函数已统一放在顶部「IMAP 连接管理」区块（pool_get / pool_put / connect 等），
# 此处不再重复定义，避免冲突与逻辑分叉。


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
    - '2026-01-01~2026-07-15'：闭区间，取左边界作为 SINCE 起始
    - None / '' / 非法：兜底 '01-Jan-2000'（拉尽量早）
    """
    today = today or dt.date.today()
    if not expr:
        return imap_date("2000-01-01")
    expr = str(expr).strip()
    # 日期区间: YYYY-MM-DD~YYYY-MM-DD → 取左边作为 SINCE
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})~(\d{4}-\d{2}-\d{2})", expr)
    if m:
        return imap_date(m.group(1))
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

    健壮性约束（关键）：任何异常——Tesseract 未安装、语言包缺失、损坏 PDF、
    以及新版 PyMuPDF 里 TextPage API 变更——都就地吞掉并返回空字符串，
    绝不让「单页 OCR 失败」中断整封邮件乃至整账号的抓取。
    doc 在 finally 中关闭，避免 200+ 邮件轮询时文件句柄泄漏。"""
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        log(f"[PDF] 无法打开 {os.path.basename(pdf_path)}: {e}")
        return ""
    try:
        # 第一遍：尝试原生文本层（电子发票走快速路径）
        parts = []
        needs_ocr = False
        for page in doc:
            try:
                text = page.get_text()
            except Exception:
                text = ""
            if text.strip():
                parts.append(text)
            else:
                # 至少一页文本层为空 → 整体改走 OCR
                needs_ocr = True
                break
        if not needs_ocr:
            return "".join(parts)
        # 第二遍：OCR 兜底（扫描件）。Tesseract 不可用则直接返回空串。
        if not TESSERACT_AVAILABLE:
            log(f"[OCR] {os.path.basename(pdf_path)} 无文本层但 Tesseract 未安装，跳过")
            return ""
        log(f"[OCR] {os.path.basename(pdf_path)} 无文本层，启动 OCR（lang={OCR_LANG}, dpi={OCR_DPI}）")
        parts = []
        for page in doc:
            try:
                tp = _ocr_textpage(page)
                if tp:
                    # 新版 PyMuPDF(>=1.24) 的 TextPage 用 extractTEXT() 取文本，
                    # 旧的 tp.get_text() / page.get_text(textpage=) 均已移除，调用会抛异常。
                    parts.append(tp.extractTEXT())
            except Exception as e:
                log(f"[OCR] 页面文本提取失败: {e}")
        return "".join(parts)
    finally:
        try:
            doc.close()
        except Exception:
            pass


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
    doc = None
    try:
        try:
            doc = fitz.open(pdf_path)
        except Exception:
            # 损坏/非 PDF 文件直接放弃，返回空结果（不抛异常中断抓取）
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
                # 新版 PyMuPDF 的 TextPage 用 extractDICT() 取 dict（旧 tp= 关键字已移除）
                d = tp.extractDICT()
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

        # —— 备注：找"备注"标签附近的文本 ——
        # 备注可能形如"备注:XXX"/"备注：XXX"（标签和内容在同一块），
        # 也可能"备注"单独成块、内容在其下方或右侧。
        # 部分全电发票"备"和"注"竖排被切成两个块，需要合并识别。
        remark_label = None  # 备注"标签块"（或合并后的"注"块位置）
        for b in blocks:
            txt = b["text"].strip()
            if "备注" not in txt:
                continue
            # 情况1：标签和内容在同一块（如"备注:20260710晟联数码"）
            m = re.match(r"备注\s*[:：]\s*(.+)", txt)
            if m and m.group(1).strip():
                result["remark"] = m.group(1).strip()[:500]
                break
            # 情况2：纯"备注"标签块（含冒号也接受），向下/向右找内容块
            if len(txt) <= 4:
                remark_label = b
                break

        # 情况3：未找到完整"备注"块时，尝试"备"+"注"竖排分块合并
        if "remark" not in result and remark_label is None:
            for bei in blocks:
                if bei["text"].strip() != "备":
                    continue
                # 找"备"正下方 x 相近的"注"块（竖排标签）
                zhu = next((o for o in blocks
                            if o is not bei
                            and o["text"].strip() == "注"
                            and o["cy"] > bei["cy"]
                            and abs(o["cx"] - bei["cx"]) < 10), None)
                if zhu:
                    remark_label = zhu  # 以"注"块作为备注区域起点
                    break

        # 若定位到备注标签，提取其下方/右侧的文本作为备注内容
        if "remark" not in result and remark_label is not None:
            remark_y = remark_label["cy"]
            remark_x = remark_label["cx"]
            # 排除发票其它字段的标签词，避免把"开票人"等误识别为备注
            label_words = ("开票人", "收款人", "复核人", "销货方", "销售方",
                           "购买方", "价税合计", "合计", "应税劳务",
                           "规格型号", "单价", "数量", "密码区")
            # 标记需排除的块：含标签词的块 + 与其同行右侧的块（标签的值）
            excluded = set()
            for o in blocks:
                if any(w in o["text"] for w in label_words):
                    excluded.add(id(o))
                    for v in blocks:
                        if v is o:
                            continue
                        if abs(v["cy"] - o["cy"]) < 8 and v["cx"] > o["cx"]:
                            excluded.add(id(v))
            below = [other for other in blocks
                     if other is not remark_label
                     and id(other) not in excluded
                     and other["cy"] > remark_y
                     and abs(other["cx"] - remark_x) < page_w * 0.5]
            below.sort(key=lambda o: o["cy"])
            if below:
                result["remark"] = " ".join(o["text"].strip() for o in below)[:500]

        return result
    finally:
        # doc 必须存活到上面所有 page.rect 访问结束后再关闭，否则 page 会被 detach 报 "page is None"
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


# PDF / 附件发票识别的「强证据」关键词：PDF 正文含其一才视为发票。
# 普通 PDF（合同/报告/账单）即使恰好含一串 20 位数字（被 rules.json 的裸数字模式命中），
# 也不应被当作发票入库——这是把"只是因为它是 PDF"挡在门外的关键闸门。
# 同时作为 rules.json 缺失 pdf_invoice_keywords 时的安全兜底（避免全量拒收）。
PDF_INVOICE_KEYWORDS = ["发票", "增值税", "普通发票", "专用发票", "电子发票",
                        "全电发票", "价税合计", "销售方", "购买方", "开票日期",
                        "纳税人识别号", "发票代码", "发票号码", "税额"]


def _has_invoice_evidence(text, rules):
    """判断 PDF 正文是否「像发票」——非发票 PDF 的过滤闸门。

    正向证据：正文含发票强特征关键词（发票/增值税/价税合计/销售方/购买方/开票日期…）。
    没有正向证据的 PDF（合同/报告/账单/行程单等）即使恰好含一串 20 位数字，
    也不能当作发票，否则会"只是因为它是 PDF"就被误入库。
    注意：纯图片/扫描件 PDF 无文本层时，这里自然返回 False，由文件名格式
    （dzfp_… 等，见 parse_invoice_filename）作为另一条证据链兜底。"""
    if not text:
        return False
    low = text.lower()
    for k in rules.get("pdf_invoice_keywords", PDF_INVOICE_KEYWORDS):
        if k.lower() in low:
            return True
    return False


def parse_pdf_to_invoice(pdf_path, account_id, email_id=None, source_type="attachment", filename=None):
    """把一个 PDF 解析成发票记录 dict（未入库）。
    两步提取：先布局法（坐标，应对全电发票竖排标签），再正则法（兜底，补全金额等）。

    关键闸门：只有「像发票」的 PDF（正文含发票强特征词，或文件名是发票格式）才返回发票号；
    普通 PDF（合同/报告等）一律返回空发票号，调用方据此跳过，杜绝"只是因为它是 PDF 就被入库"。
    filename 可选，传入后能利用文件名格式（dzfp_发票号_销售方_日期）作为发票证据，
    应对扫描件 PDF 无文本层、但文件名已写明发票号的情形。"""
    rules = load_rules()
    text = extract_text_from_pdf(pdf_path)
    fname = filename or os.path.basename(pdf_path)
    # 文件名是发票格式（dzfp_/电子发票_…）本身即强证据，扫描件 PDF 也能借此识别
    file_inv = parse_invoice_filename(fname, account_id, email_id=email_id)
    # 发票正向证据：正文含发票关键词，或文件名是已知发票格式
    is_invoice_pdf = _has_invoice_evidence(text, rules) or bool(file_inv)
    # 第一步：布局法（坐标）——应对全电发票等标签和值不在同一行的版式。
    # 仅在确认像发票时才跑（布局法可能触发 OCR），普通 PDF 直接跳过，省时且避免误解析。
    layout_info = extract_fields_by_layout(pdf_path) if is_invoice_pdf else {}
    # 第二步：正则法（rules.json）——补全金额等布局法没提取的字段
    regex_info = apply_rules(text, rules)
    # 合并：布局法优先，正则法兜底
    info = {**regex_info, **layout_info}
    rel = os.path.relpath(pdf_path, db.HERE)
    if not is_invoice_pdf:
        # 非发票 PDF：不返回任何发票字段，调用方会因 invoice_no 为空而跳过，不污染台账
        return {
            "email_id": email_id,
            "account_id": account_id,
            "buyer": "", "seller": "", "amount": None,
            "invoice_no": "", "invoice_date": "", "city": "",
            "pdf_path": rel, "source_type": source_type,
            "note": "未识别为发票（无发票特征，已跳过）",
        }
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
        "remark": info.get("remark", ""),
        "note": "" if info.get("invoice_no") else "未识别到发票号",
    }
    # 文件名是发票格式（dzfp_…）时，用文件名里的发票号/销售方/日期兜底补全
    # （扫描件 PDF 无文本层，但文件名已写明，避免漏字段）。复用 _coalesce_invoice 的
    # 「PDF 内容优先、文件名补空」合并规则，保持单一真相源。
    inv, _ = _coalesce_invoice(inv, file_inv, None, source_type,
                               account_id=account_id, email_id=email_id)
    return inv


def parse_subject_to_invoice(subject, account_id, email_id=None, source_type="subject"):
    """当邮件无可用 PDF（PDF 藏在登录/二维码页后、或解析失败）时，从「邮件标题」兜底提取发票字段。
    返回发票 dict（未入库）；若标题里找不到发票号（不足以判定为发票）则返回 None。
    适用：51fapiao / 航信等平台把 发票号、金额、销售方 直接写进标题的发票通知邮件——
    这类邮件的 PDF 无法直接下载，但标题里的数据完整可用，不应浪费。
    返回的 pdf_path 为空字符串（本就无 PDF），配合 db.needs_refetch 的 source_type 判定，
    不会被当作「PDF 丢失」反复重拉。"""
    s = subject or ""
    # 发票号：标题里的「发票号码：XXXXXXXXXXXXXXXXX」（没有则不足以判定为发票）
    m = re.search(r"发票号码[:：]?\s*([0-9]{15,20})", s)
    invoice_no = m.group(1) if m else ""
    if not invoice_no:
        return None
    # 金额：价税合计金额 / 金额（保留两位小数）
    amount = None
    m = re.search(r"价税合计金额[为:]+\s*([\d,]+\.\d{2})", s) or \
        re.search(r"金额[为:]+\s*([\d,]+\.\d{2})", s)
    if m:
        amount = coerce_money(m.group(1))
    # 销售方：来自【XXX】
    seller = ""
    m = re.search(r"来自【(.+?)】", s)
    if m:
        seller = m.group(1).strip()
    # 购买方：购方名称：XXX
    buyer = ""
    m = re.search(r"购方名称[:：]\s*([^\s\[\]]+)", s)
    if m:
        buyer = m.group(1).strip()

    has_extra = bool(seller or buyer or amount is not None)
    return {
        "email_id": email_id,
        "account_id": account_id,
        "buyer": buyer,
        "seller": seller,
        "amount": amount,
        "invoice_no": invoice_no,
        "invoice_date": "",
        "city": "",
        "pdf_path": "",
        "source_type": source_type,
        "note": "" if has_extra else "仅从标题提取，无PDF",
    }


# ----------------------------------------------------------- 51发票（百望云）专用下载
# 51发票的查看链接形如 https://a.51fapiao.cn/v/{code}，会 302 跳转到
# https://yun.51fapiao.cn/openapi/invoice/layoutfile?uuid={uuid} 这个 Vue SPA。
# 该页面用 SM4(ECB, key=08e3b47e22e7bdc5) 对 uuid / 接口参数 / 响应做加解密。
# 完整链路（已逆向验证，2026-07）：
#   1) uuid → URL-safe base64 还原 + SM4 解密 → datagram{fphm,kprq,username,userId}
#   2) POST /api/v2.0/h5.js.noLogin.download.info（body 见 _fiftyone_call）→ invoiceInfo{Nsrsbh,FileName}
#   3) POST /api/v2.0/h5.js.noLogin.download → 下载凭证；zipCode=1 时 datagram 是 gzip(SM4密文)，
#      先 gzip 解压再 SM4 解密得 {wjl: <PDF 的 base64>}
FIFTYONE_SM4_KEY = "08e3b47e22e7bdc5"
FIFTYONE_HOST = "https://yun.51fapiao.cn"
FIFTYONE_INFO_API = "/api/v2.0/h5.js.noLogin.download.info"
FIFTYONE_DOWNLOAD_API = "/api/v2.0/h5.js.noLogin.download"
# 用于识别「邮件里的链接是否属于 51发票」的域名标记
FIFTYONE_HOST_MARKS = ("a.51fapiao.cn", "yun.51fapiao.cn", "51fapiao.cn")


def is_51fapiao_url(url):
    """判断链接是否指向 51发票（百望云）的「发票查看」页。
    只认能拿到 uuid 的链接：短链 /v/{code} 或带 uuid= 的落地页；
    排除 www.51fapiao.cn 登录页、ei.51fapiao.cn 图片等噪音链接。"""
    u = (url or "").lower()
    if "51fapiao.cn" not in u:
        return False
    return ("/v/" in u) or ("uuid=" in u) or ("/invoice/layoutfile" in u)


def _fiftyone_sm4_dec(b64_text):
    """SM4-ECB 解密：输入 base64 字符串（cipherType=text），输出明文 UTF-8。"""
    if _gmssl_sm4 is None:
        raise RuntimeError("51发票下载需要 gmssl 库，请先执行: pip install gmssl")
    raw = base64.b64decode(b64_text)
    c = _gmssl_sm4.CryptSM4()
    c.set_key(FIFTYONE_SM4_KEY.encode(), _gmssl_sm4.SM4_DECRYPT)
    plain = c.crypt_ecb(raw)
    pad = plain[-1]
    if 1 <= pad <= 16:
        plain = plain[:-pad]
    return plain.decode("utf-8", "replace")


def _fiftyone_sm4_enc(obj):
    """SM4-ECB 加密：输入对象 → base64 字符串（cipherType=base64）。"""
    if _gmssl_sm4 is None:
        raise RuntimeError("51发票下载需要 gmssl 库，请先执行: pip install gmssl")
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    p = 16 - len(s) % 16
    s = s + bytes([p]) * p
    c = _gmssl_sm4.CryptSM4()
    c.set_key(FIFTYONE_SM4_KEY.encode(), _gmssl_sm4.SM4_ENCRYPT)
    return base64.b64encode(c.crypt_ecb(s)).decode()


def _fiftyone_uuid_to_datagram(uuid):
    """把短链里的 uuid 还原成发票查询参数 datagram（SM4 解密）。"""
    e = uuid.replace("-", "+").replace("_", "/")
    while len(e) % 4:
        e += "="
    return json.loads(_fiftyone_sm4_dec(e))


def _fiftyone_short_to_uuid(short_url, session):
    """跟随 51发票短链 302 重定向，从最终 URL 的 ?uuid= 参数取出 uuid。"""
    r = session.get(short_url, allow_redirects=True, timeout=20)
    m = re.search(r"uuid=([^&\s#]+)", r.url)
    if not m:
        raise RuntimeError("51发票短链未携带 uuid 参数: " + short_url)
    return m.group(1)


def _fiftyone_call(api_path, interface_code, datagram_obj, session):
    """复刻 51发票前端 request 封装：真实请求体是 JSON 对象（含 interfaceCode/datagram 等），
    而非裸加密串——这是逆向时最容易踩的坑。"""
    body = {
        "interfaceCode": interface_code,
        "zipCode": "0",
        "encryptCode": "3",
        "access_token": "",
        "datagram": _fiftyone_sm4_enc(datagram_obj),
        "signtype": "",
        "signature": "",
    }
    headers = {
        "content-type": "application/json",
        "token": "",
        "X-NSR-SBH": "1",
        "User-Agent": "Mozilla/5.0",
    }
    r = session.post(FIFTYONE_HOST + api_path, json=body, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def _fiftyone_decode_response(resp):
    """处理响应的 zipCode：1=先 gzip 解压再 SM4 解密；0=直接 SM4 解密。返回解密后的 dict。"""
    dg = resp.get("datagram", "")
    if str(resp.get("zipCode")) == "1":
        # 响应是 gzip(SM4密文)：base64 解码 → gzip 解压 → 得到 SM4 密文(base64) → SM4 解密
        decompressed = gzip.decompress(base64.b64decode(dg))
        dg = base64.b64encode(decompressed).decode()
    return json.loads(_fiftyone_sm4_dec(dg))


def fetch_51fapiao_pdf(short_url, out_path, session):
    """从 51发票短链下载真实 PDF 到 out_path。成功返回 True，失败返回 False（不抛异常，
    让调用方回退到标题兜底，不阻塞整封邮件的处理）。
    这是 51发票（百望云）官方 H5 下载链路的复刻：短链→uuid→SM4→查发票信息→下载 PDF。"""
    try:
        uuid = _fiftyone_short_to_uuid(short_url, session)
        datagram = _fiftyone_uuid_to_datagram(uuid)
        # 1) 查发票信息，拿到购买方税号(Nsrsbh)与文件名(FileName)
        info = _fiftyone_call(FIFTYONE_INFO_API, "h5.js.noLogin.download.info",
                              {"fphm": datagram["fphm"], "kprq": datagram["kprq"],
                               "username": datagram["username"], "userId": datagram.get("userId")},
                              session)
        if info.get("code") != 1000:
            log(f"    [51发票] 查询发票信息失败: {info.get('messge') or info.get('msg')}")
            return False
        inv = _fiftyone_decode_response(info)
        nsrsbh = inv.get("Nsrsbh")
        # 2) 下载 PDF（xzlx=0 表示 PDF；1=OFD；2=zip）
        dl = _fiftyone_call(FIFTYONE_DOWNLOAD_API, "h5.js.noLogin.download",
                            {"fphm": datagram["fphm"], "kprq": datagram["kprq"],
                             "xzlx": "0", "username": datagram["username"], "nsrsbh": nsrsbh},
                            session)
        if dl.get("code") != 1000:
            log(f"    [51发票] 下载失败: {dl.get('messge') or dl.get('msg')}")
            return False
        blob = _fiftyone_decode_response(dl)
        wjl = blob.get("wjl")
        if not wjl:
            log("    [51发票] 响应中无 PDF 数据(wjl)")
            return False
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(wjl))
        return True
    except Exception as e:
        log(f"    [51发票] 下载异常: {e}")
        return False


# ----------------------------------------------------------- IMAP 拉取
# _send_imap_id / connect 等连接管理函数已统一上移至顶部「IMAP 连接管理」区块（provider 感知、带风控识别与重试），
# 此处不再重复定义。下方只放"拉取后处理"相关逻辑。


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
    """返回邮件附件中的 PDF / OFD 文件列表。
    中国电子发票常见以 .ofd 格式发送，不能只认 .pdf；
    返回 [(fname, payload, ext)]，ext 为 'pdf' 或 'ofd'。"""
    atts = []
    for part in msg.walk():
        fname = decode_mime(part.get_filename())
        if fname:
            lower = fname.lower()
            if lower.endswith(".pdf"):
                payload = part.get_payload(decode=True)
                if payload:
                    atts.append((fname, payload, "pdf"))
            elif lower.endswith(".ofd"):
                payload = part.get_payload(decode=True)
                if payload:
                    atts.append((fname, payload, "ofd"))
    return atts


# 常见电子发票文件名/标题格式：
#   dzfp_发票号_销售方_日期
#   电子发票_发票号_销售方_日期
#   发票_发票号_销售方_日期
#   增值税电子普通发票_发票号_销售方_日期
_INVOICE_FILENAME_PATTERNS = [
    re.compile(r"dzfp[_-](\d{20})[_-](.+?)[_-](\d{8,14})\b", re.I),
    re.compile(r"电子发票[_-](\d{20})[_-](.+?)[_-](\d{8,14})\b", re.I),
    re.compile(r"发票[_-](\d{20})[_-](.+?)[_-](\d{8,14})\b", re.I),
    re.compile(r"增值税电子(?:普通|专用)发票[_-](\d{20})[_-](.+?)[_-](\d{8,14})\b", re.I),
]


def parse_invoice_filename(subject, account_id, email_id=None, source_type="subject"):
    """从邮件标题/文件名格式中提取发票字段。
    适配 dzfp_发票号_销售方_日期 这类平台自动生成的文件名（常见于电子发票邮件）。
    返回发票 dict（未入库）；标题不匹配则返回 None。"""
    s = subject or ""
    for pat in _INVOICE_FILENAME_PATTERNS:
        m = pat.search(s)
        if m:
            invoice_no = m.group(1)
            seller = m.group(2).strip()
            date_str = m.group(3)
            # 尝试把 20260717 / 20260717085020 整理成标准开票日期
            invoice_date = ""
            if len(date_str) >= 8:
                invoice_date = f"{date_str[:4]}年{date_str[4:6]}月{date_str[6:8]}日"
            return {
                "email_id": email_id,
                "account_id": account_id,
                "buyer": "",
                "seller": seller,
                "amount": None,
                "invoice_no": invoice_no,
                "invoice_date": invoice_date,
                "city": "",
                "pdf_path": "",
                "source_type": source_type,
                "note": "从标题文件名提取",
            }
    return None


def _coalesce_invoice(base, file_inv, sinv, source_type, account_id=None, email_id=None):
    """为一个附件解析出最终发票记录，并消除「PDF 内容 / 附件文件名 / 邮件标题」三来源的冲突。

    背景：一封邮件可能夹多个发票附件（批量发票邮件），每个附件的文件名各自携带
    真实的发票号（如 dzfp_发票号_销售方_日期）。因此发票号的权威来源必须是
    「该附件自身」，而非整封邮件共享的标题——否则多张发票会被合并成一张、其余丢失。

    发票号优先级：PDF 内容 > 附件文件名 > 邮件标题（标题仅作最后兜底）。
    其余字段以「非空优先」从三来源合并，避免互相覆盖。
    返回 (inv_dict, invoice_no)；invoice_no 为空表示此附件无法判定为发票，调用方应跳过。
    account_id / email_id 仅用于「PDF 解析失败导致 base=None、且标题也无号」的极端兜底，
    保证 skeleton 仍带 account_id（发票表 account_id 为 NOT NULL），不触发入库崩溃。"""
    invoice_no = (base or {}).get("invoice_no") \
        or (file_inv or {}).get("invoice_no") \
        or (sinv or {}).get("invoice_no") \
        or ""
    inv = dict(base) if base else {
        "email_id": email_id if email_id is not None else (sinv or {}).get("email_id"),
        "account_id": account_id if account_id is not None else (sinv or {}).get("account_id"),
        "buyer": "", "seller": "", "amount": None,
        "invoice_date": "", "city": "", "pdf_path": "", "note": "",
    }
    # 字段以「非空优先」从文件名 / 标题补全会话，保证图片型/扫描型 PDF 也能拿到字段
    for src in (file_inv, sinv):
        if not src:
            continue
        for k in ("buyer", "seller", "invoice_date", "city"):
            if not inv.get(k) and src.get(k):
                inv[k] = src[k]
    inv["invoice_no"] = invoice_no
    inv["source_type"] = source_type
    # 备注标明发票号来源（便于排查）
    if not (base or {}).get("invoice_no"):
        inv["note"] = "发票号/字段来自" + ("文件名" if file_inv else "标题")
    return inv, invoice_no


def _format_priority(source_type, pdf_path):
    """格式优先级（仅用于同号发票合并时择优保留）：PDF(2) > OFD(1) > 其它(0)。"""
    ext = ""
    if pdf_path:
        ext = pdf_path.lower().rsplit(".", 1)[-1]
    if ext == "pdf" or source_type == "pdf":
        return 2
    if ext == "ofd" or source_type == "ofd":
        return 1
    return 0


def _find_sibling_pdf(account_dir, invoice_no):
    """在账号附件目录下查找与发票号「同号」且为 PDF 格式的文件。

    用途：OFD 行择优降级到 PDF——即便 PDF 阶段因解析失败/分类遗漏漏建了发票行，
    只要磁盘上存在同号 PDF，就优先采用它，保证「有 PDF 就预览 PDF」。
    仅按文件名中的发票号匹配（带数字边界，避免子串误匹配），零网络成本。找不到返回 None。"""
    if not invoice_no or not os.path.isdir(account_dir):
        return None
    pat = re.compile(r"(?<!\d)" + re.escape(invoice_no) + r"(?!\d)")
    for fn in os.listdir(account_dir):
        if fn.lower().endswith(".pdf") and pat.search(fn):
            return os.path.join(account_dir, fn)
    return None


def _invoice_update_payload(existing, inv):
    """根据现有发票行与新解析结果，算出入库更新字段（PDF / OFD 两处复用）。

    规则：
      - 业务字段（buyer/seller/amount/invoice_date/city/note/remark）只更新非空值；
      - 文件/格式「择优选」：新候选格式不优于已存 → 保留原 PDF/OFD 文件与格式，**绝不降级**
        （例如 OFD 撞上已存的 PDF，只补业务字段，不动 pdf_path/source_type）；
        新候选格式更优或同级（PDF 优先于 OFD）→ 采纳其文件与格式。
      - 成功解析（note 为空）时清掉上一轮「来自文件名/标题」占位说明，避免误导。"""
    payload = {}
    for k in ("buyer", "seller", "amount", "invoice_date", "city", "note", "remark"):
        v = inv.get(k)
        if v not in (None, ""):
            payload[k] = v
    # 格式择优选：比较新候选与已存的优先级与文件路径
    new_pri = _format_priority(inv.get("source_type"), inv.get("pdf_path"))
    old_pri = _format_priority(existing.get("source_type"), existing.get("pdf_path"))
    new_path = inv.get("pdf_path")
    if new_pri > old_pri:
        # 格式升级（如 OFD→PDF）：采纳更优格式；文件路径不同则一并更新
        if new_path and new_path != existing.get("pdf_path"):
            payload["pdf_path"] = new_path
        payload["source_type"] = inv.get("source_type") or existing.get("source_type")
    elif new_pri == old_pri and new_path and new_path != existing.get("pdf_path"):
        # 同级（PDF→PDF / OFD→OFD）且文件不同：采纳新文件路径，格式不变
        payload["pdf_path"] = new_path
        payload["source_type"] = inv.get("source_type") or existing.get("source_type")
    # 成功解析（note 为空）时清掉上一轮「来自文件名/标题」的占位说明，避免误导
    if not inv.get("note") and (existing.get("note") or "").startswith("发票号/字段来自"):
        payload["note"] = ""
    return payload

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


def process_pending(acc, rules, session, pending):
    """离线阶段：对 (uid, raw_bytes) 列表逐封解析发票并入库，返回新增发票数。

    raw_bytes 为 RFC822 原文——IMAP 的 RFC822 fetch 与腾讯官方 API 的 EML 都是此格式，
    故 IMAP 与腾讯 API 两条抓取路径共用本函数，避免重复实现解析/入库逻辑。
    单封邮件处理异常就地捕获并跳过，绝不中断整账号抓取。"""
    keywords = (
        json.loads(acc["keywords_override"])
        if acc.get("keywords_override")
        else rules.get("invoice_keywords", [])
    )
    acc_dir = os.path.join(db.PDF_DIR, safe_name(acc["email"]))
    os.makedirs(acc_dir, exist_ok=True)
    new_inv = 0
    for uid, raw in pending:
        try:
            uid_str = str(uid)
            msg = email.message_from_bytes(raw)
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

            # 附件发票探测开关（rules.json: detect_attachment_invoices，默认开）。
            # 关掉则完全回到「只看主题关键词」的旧行为，便于在误判严重时一键回退。
            detect_attachment = rules.get("detect_attachment_invoices", True)

            # 邮件标题级发票字段兜底（强信号）：
            #   1) 传统「发票号码：XXX」写法；
            #   2) 电子发票文件名格式：dzfp_发票号_销售方_日期（如 dzfp_2644..._安徽邦信置业_20260717）。
            # 第二种常见于标题即附件文件名的系统通知邮件，即便标题里没写「发票」二字。
            sinv = parse_subject_to_invoice(subject, acc["id"], email_id=eid) or \
                   parse_invoice_filename(subject, acc["id"], email_id=eid)

            # 收集附件：PDF 可解析；OFD 是中国电子发票官方格式但当前无法解析，
            # 所以 OFD 附件要和标题文件名兜底配合才能确定发票号。
            attachments = []
            if is_inv or detect_attachment:
                for i, (fname, payload, ext) in enumerate(get_attachments(msg), 1):
                    p = os.path.join(acc_dir, f"{uid_str}_{i}_{safe_name(fname)}")
                    with open(p, "wb") as f:
                        f.write(payload)
                    attachments.append((p, ext))
            pdfs = [(p, "attachment") for p, _ in attachments if _ == "pdf"]
            ofds = [(p, "ofd") for p, _ in attachments if _ == "ofd"]
            has_attachment_pdf = bool(pdfs)
            has_attachment_ofd = bool(ofds)

            # 是否进入解析阶段：
            #   - 主题命中关键词（强信号），或
            #   - 存在 PDF 附件，或
            #   - 存在 OFD 附件，或
            #   - 标题符合发票文件名格式（如 dzfp_...）。
            # 四者皆否 → 直接存档（is_invoice=0）跳过，避免对无信号的普通邮件做无谓联网扫描。
            if (not is_inv) and (not has_attachment_pdf) and (not has_attachment_ofd) and (not sinv):
                continue

            # 主题命中关键词时，额外联网发现「正文 PDF 链接 / 51发票短链」候选
            # （仅对关键词邮件扫描，控制非发票邮件的联网范围与抓取成本）。
            if is_inv and not pdfs and not ofds:
                # 51发票短链形如 a.51fapiao.cn/v/xxx，不以 .pdf 结尾，find_pdf_links 会过滤掉，
                # 故先单独扫描全部链接抽取 51发票短链，走专用下载链路。
                # 去重：同一短链在 HTML 中常出现多次，避免重复请求
                seen_links = set()
                all_links = re.findall(r'https?://[^\s"\'<>]+', (html or "") + "\n" + (text or ""))
                for url in all_links:
                    if url in seen_links:
                        continue
                    seen_links.add(url)
                    if is_51fapiao_url(url):
                        p = os.path.join(acc_dir, f"{uid_str}_51fapiao.pdf")
                        if fetch_51fapiao_pdf(url, p, session):
                            pdfs.append((p, "51fapiao"))
                            break
                # 其余 PDF 直链仍走原逻辑
                if not pdfs:
                    for j, (url, _label) in enumerate(find_pdf_links(html, text)[:5], 1):
                        p = os.path.join(acc_dir, f"{uid_str}_link{j}.pdf")
                        if try_download_pdf(url, session, p):
                            pdfs.append((p, "link"))
                            break

            # email_id 回链，保证"发票→邮件"可追踪，删除/对账时才不会误删
            inserted_any = False

            # 1) 解析 PDF 附件。
            #    关键：每个附件用「自身文件名」解析出发票号（批量发票邮件里每封附件各自独立），
            #    而不是用整封邮件共享的标题去覆盖，避免把多张发票合并成一张、其余丢失。
            #    单封附件解析失败（OCR 异常 / 损坏 PDF）就地捕获，不影响本封邮件的其它附件、更不中断整账号。
            for p, stype in pdfs:
                fname = os.path.basename(p)
                file_inv = parse_invoice_filename(fname, acc["id"], email_id=eid)
                try:
                    # filename 带入：让"文件名是发票格式"（dzfp_… 类扫描件 PDF）也能作为发票证据
                    base = parse_pdf_to_invoice(p, acc["id"], email_id=eid, source_type=stype, filename=fname)
                except Exception as e:
                    log(f"    [解析失败] {fname}: {e}")
                    base = None
                # 附件自身的发票号只来自「PDF 内容」或「文件名」，不借用邮件标题（sinv）：
                # 避免把"恰好出现在发票邮件里的普通 PDF（合同/报告）"误挂到标题的发票号上，
                # 造成重复行 / 误判行。标题级兜底统一在步骤 3 处理。
                inv, inv_no = _coalesce_invoice(base, file_inv, None, stype,
                                               account_id=acc["id"], email_id=eid)
                # 只有"附件自身就是发票"（解析出发票号，或文件名是发票格式）才入库；
                # 否则只是非发票 PDF，丢弃，不污染台账。
                if not inv_no:
                    continue
                # 同号发票行已存在 → 更新补全，不重复插入
                if inv_no:
                    existing = db.get_invoice_by_no(acc["id"], inv_no)
                    if existing:
                        db.update_invoice_fields(existing["id"], _invoice_update_payload(existing, inv))
                        inserted_any = True
                        continue
                if db.insert_invoice(inv):
                    new_inv += 1
                    inserted_any = True

            # 2) OFD 附件：无法直接解析内容，完全依赖「附件文件名」解析出发票号。
            #    同样按每个附件自身处理，批量发票邮件可拆成多张发票。
            for p, stype in ofds:
                fname = os.path.basename(p)
                file_inv = parse_invoice_filename(fname, acc["id"], email_id=eid)
                # OFD 只能靠文件名识别发票号；同样不借用标题，避免误挂
                inv, inv_no = _coalesce_invoice(None, file_inv, None, stype,
                                               account_id=acc["id"], email_id=eid)
                if not inv_no:
                    # 该 OFD 文件名和标题都看不出发票号 → 不硬造行
                    continue
                # 格式优先级：同一发票号若磁盘上存在同号 PDF（哪怕 PDF 阶段解析失败/漏收），
                # 优先采用 PDF，降级才用 OFD——避免「明明有 PDF 却只留了 OFD 导致无法预览」。
                pdf_sibling = _find_sibling_pdf(acc_dir, inv_no)
                if pdf_sibling:
                    p = pdf_sibling
                    stype = "pdf"
                    inv["source_type"] = "pdf"
                inv["pdf_path"] = os.path.relpath(p, db.HERE)
                # 同号发票行已存在（多为 PDF 阶段已建行）→ 更新补全，优先保留已有 PDF 路径
                existing = db.get_invoice_by_no(acc["id"], inv_no)
                if existing:
                    db.update_invoice_fields(existing["id"], _invoice_update_payload(existing, inv))
                    inserted_any = True
                    continue
                if db.insert_invoice(inv):
                    new_inv += 1
                    inserted_any = True

            # 3) 标题/文件名兜底：上面 PDF/OFD 都没产出发票行，但标题本身含发票号（sinv）→ 补一行。
            #    仍是整封邮件兜底的最后一手，对批量邮件通常已在附件阶段处理完，这里多针对「纯标题发票」。
            if not inserted_any and sinv:
                # 对关键词邮件，这是传统 Fix 1；对非关键词邮件，这是 dzfp_... 这类文件名的兜底
                if db.insert_invoice(sinv):
                    new_inv += 1
                    inserted_any = True

            # Fix 3：有发票信号但既没解析出字段、也没标题兜底 → 翻回非发票，
            # 避免把普通邮件常驻误判、且每次都被重复拉取。
            # 但若该邮件「已有发票行」，不翻 0，以免把有效发票误判成非发票。
            if not inserted_any and not db.email_has_invoice(eid):
                db.set_email_invoice(eid, 0)

            # 附件/文件名即发票情形：主题未命中关键词、但确实确认出发票 → 纠正邮件的 is_invoice 标记，
            # 保证 UI 里这封邮件归类到「发票邮件」而非被当成普通邮件。
            if inserted_any and not is_inv:
                db.set_email_invoice(eid, 1)
        except Exception as ex:
            # 单封邮件处理异常（解析/附件/字段提取等）就地捕获，跳过该封继续下一封，
            # 避免一封坏邮件（如损坏的原始报文）中断整账号、甚至让"全量只回 1 张"雪上加霜。
            log(f"    [邮件处理失败 uid={uid}] {type(ex).__name__}: {ex}")
            continue
    return new_inv


def fetch_account(acc, rules, session, since_override=None):
    """拉一个邮箱的发票邮件 → 下载 PDF → 解析 → 入库。返回新增发票数。
    - 统一用 IMAP UID 语义（uid search / uid fetch），UID 单调稳定。
    - fetch_mode='incremental'（默认）：只处理 uid > last_uid 的邮件，
      并在结束时把水位线推进到"本次已扫描到的最大 uid"。有 200 上限兜底。
    - fetch_mode='full'：按 default_since 全量重扫，不做水位线过滤、不做 200
      上限截断、也不写回 last_uid（一次性重扫，不污染增量水位线）。
      去重靠 needs_refetch + invoice_no UNIQUE 兜底；对首次下载失败的发票邮件，
      needs_refetch 会返回 True 从而自动补拉缺失的 PDF。
    - 不动邮箱状态（不 STORE \\Seen、不删邮件）。
    - 连接管理：IMAP 会话只用于"搜索 + 拉取邮件原文"，拉完立即归还连接池；
      后续 PDF 下载 / OCR / 解析等慢操作在离线阶段进行，不占用 IMAP 连接，
      从而规避腾讯 IMAP 空闲超时导致的"半路断链"。同一账号连接串行借还，避免并发冲突。"""
    # 腾讯官方 API 适配层：fetch_method='tencent_api' 时走官方接口而非 IMAP
    if acc.get("fetch_method") == "tencent_api":
        import tencent_mail
        return tencent_mail.fetch_account(acc, rules, session, since_override)

    last_uid = acc.get("last_uid") or 0
    mode = acc.get("fetch_mode") or "incremental"
    since_expr = since_override or acc.get("default_since") or "90d"
    since_imap = parse_since_expr(since_expr)

    # —— IMAP 阶段：只做搜索 + 拉取邮件原文到内存；分批借还连接 + 断链自动续拉 ——
    # 详见 _imap_search_fetch。连接统一走 pool_get / pool_put，不直接 connect+logout；
    # 同一账号连接由 pool 内账号锁串行借还，规避腾讯 IMAP 空闲超时导致的"半路断链"。
    pending, scanned_max = [], last_uid
    try:
        pending, scanned_max = _imap_search_fetch(acc, since_imap, mode, last_uid)
    except Exception as e:
        log(f"    [IMAP 阶段失败] {type(e).__name__}: {e}")

    # —— 离线阶段：解析 + 入库（IMAP 与腾讯官方 API 共用 process_pending）——
    new_inv = process_pending(acc, rules, session, pending)

    # 推进水位线：只有 incremental 模式才写回 last_uid。
    # full 模式是"按 default_since 全量重扫"，是一次性操作，不应污染增量水位线
    # （否则一次 full 之后，incremental 会以为 full 扫过的最大 uid 之前都处理干净了）。
    if mode == "incremental" and scanned_max > last_uid:
        db.update_last_uid(acc["id"], scanned_max)
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
        # 仅重新解析真正的 PDF；OFD 等其它格式无法用 PyMuPDF 解析，
        # 若强行解析会把发票号清空，反而损坏已入库的 OFD 发票行，故跳过。
        if not pdf_rel or not pdf_rel.lower().endswith(".pdf"):
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
        for key in ("buyer", "seller", "amount", "invoice_no", "invoice_date", "city", "note", "remark"):
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
            # 只回填真正识别出发票的 PDF；普通 PDF（合同/报告）解析不出发票号，
            # 不应被当成发票入库，否则会污染台账。
            if inv.get("invoice_no") and db.insert_invoice(inv):
                log(f"[对账] 回填 {fname} -> 号={inv.get('invoice_no')} 买方={inv.get('buyer')}")
                report["reingested"].append({"file": rel, "invoice_no": inv.get("invoice_no")})
    log(f"[对账] 完成：回填 {len(report['reingested'])} 张，跳过 {report['skipped']} 张，"
         f"失败 {len(report['failed'])} 张")
    return report
