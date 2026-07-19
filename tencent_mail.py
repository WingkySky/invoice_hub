"""腾讯企业邮箱官方 API 适配层。

为什么需要它：
    标准 IMAP 只能看到邮箱的"活跃"文件夹（INBOX/Sent 等），腾讯企业邮箱的
    「邮件归档」存储区对 IMAP 不可见（见 engine.py 诊断记录）。腾讯另提供官方
    HTTP API 可程序化读取邮件，本模块即对接该 API，作为 IMAP 之外的"适用层"。

两套产品变体（variant）——差异只在接口基址 BASE_URL：
    - "wecom" : 企业微信邮箱，基址 https://qyapi.weixin.qq.com/cgi-bin
                接口：exmail/app/get_mail_list （按时间窗+游标分页，取 mail_id 列表）
                      exmail/app/read_mail     （按 mail_id 取 EML 原文）
                认证：gettoken?corpid=&corpsecret= -> access_token
    - "exmail" : 经典腾讯企业邮箱(专业版)，基址 https://api.exmail.qq.com/cgi-bin
                接口路径与 wecom 同构（exmail/app/...），认证同 gettoken。
                （注意：经典版须为"专业版"且开启邮件 API；若返回权限错误，
                  请以管理员身份在 exmail 后台开通。）

无论哪套，最终都产出 (mail_id, eml_bytes)，无缝接入 engine.process_pending
（EML 即 RFC822 原文，与 IMAP 的 RFC822 fetch 完全一致）。

凭证放在 config/tencent.json（不入库，避免明文泄露），结构见该文件模板。
"""

import json
import os
import time
import argparse

import requests

import db
import engine  # 复用 process_pending（离线解析+入库）与 parse_since_expr

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config", "tencent.json")

# 两套变体的接口基址（仅此一处不同）
BASE_URLS = {
    "wecom": "https://qyapi.weixin.qq.com/cgi-bin",
    "exmail": "https://api.exmail.qq.com/cgi-bin",
}

# 内存级 token 缓存：避免每次请求都重新取 token（token 有效期约 7200s）
_TOKEN_CACHE = {}  # key: (variant, corpid) -> (token, expire_at)


def load_config():
    """读取 config/tencent.json；不存在或非法时返回 None（调用方据此报错）。"""
    if not os.path.isfile(CONFIG_PATH):
        return None
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(f"读取 {CONFIG_PATH} 失败：{e}")


def _account_cfg(cfg, acc):
    """从全局配置中取出适用于某账号的凭证。

    支持两种组织方式：
      1) 平铺：配置顶层直接含 corpid/corpsecret/variant（应用于所有 tencent_api 账号）；
      2) 按邮箱分桶：配置含 "accounts" 字典，key 为账号 email，value 为上述字段。
    两者都允许顶层 variant 作为缺省。"""
    email = (acc.get("email") or "").lower()
    bucket = (cfg.get("accounts") or {}).get(email)
    if bucket:
        merged = dict(cfg)
        merged.update(bucket)
        return merged
    return cfg


class TencentMailError(RuntimeError):
    """腾讯 API 返回业务错误（errcode != 0）或网络异常时抛出。"""


class TencentMailClient:
    """对接单个腾讯邮箱账号的官方 API 客户端。"""

    def __init__(self, cfg):
        self.variant = (cfg.get("variant") or "wecom").lower()
        if self.variant not in BASE_URLS:
            raise TencentMailError(
                f"不支持的 variant='{self.variant}'，仅支持 {list(BASE_URLS)}")
        self.base = BASE_URLS[self.variant]
        self.corpid = cfg.get("corpid")
        self.corpsecret = cfg.get("corpsecret")
        self.agentid = cfg.get("agentid")
        self.userid = cfg.get("userid")  # exmail 变体按 userid 过滤时可能需要
        if not self.corpid or not self.corpsecret:
            raise TencentMailError(
                "配置缺少 corpid / corpsecret，无法调用腾讯 API（见 config/tencent.json）")

    # —— 认证 ——
    def get_token(self):
        """取 access_token，带内存缓存与过期判断。"""
        cache_key = (self.variant, self.corpid)
        cached = _TOKEN_CACHE.get(cache_key)
        if cached:
            token, expire_at = cached
            if time.time() < expire_at - 60:  # 留 60s 余量
                return token
        url = f"{self.base}/gettoken"
        # 企业微信与经典企业邮箱的 gettoken 参数名一致（corpid + corpsecret）
        params = {"corpid": self.corpid, "corpsecret": self.corpsecret}
        try:
            resp = requests.get(url, params=params, timeout=15)
            data = resp.json()
        except Exception as e:
            raise TencentMailError(f"gettoken 请求失败：{e}")
        if data.get("errcode", 0) != 0:
            raise TencentMailError(
                f"gettoken 返回错误 errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
        token = data["access_token"]
        expire_at = time.time() + int(data.get("expires_in", 7200))
        _TOKEN_CACHE[cache_key] = (token, expire_at)
        return token

    # —— 列表 ——
    def list_mail_ids(self, begin_ts, end_ts, cursor=None, limit=100):
        """按时间窗分页列出邮件 id。

        返回 (ids, next_cursor, has_more)。
        wecom 官方入参：{begin_time, end_time, cursor, limit}。
        exmail 变体若参数名不同，诊断时会暴露，再据此调整。"""
        token = self.get_token()
        url = f"{self.base}/exmail/app/get_mail_list?access_token={token}"
        body = {
            "begin_time": int(begin_ts),
            "end_time": int(end_ts),
            "limit": limit,
        }
        if cursor:
            body["cursor"] = cursor
        # exmail 经典版可能要求指定 userid（应用所绑定的邮箱账号）
        if self.variant == "exmail" and self.userid:
            body["userid"] = self.userid
        try:
            resp = requests.post(url, json=body, timeout=20)
            data = resp.json()
        except Exception as e:
            raise TencentMailError(f"get_mail_list 请求失败：{e}")
        if data.get("errcode", 0) != 0:
            raise TencentMailError(
                f"get_mail_list 返回错误 errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
        ids = [m["mail_id"] for m in data.get("mail_list", [])]
        return ids, data.get("next_cursor"), data.get("has_more", 0)

    # —— 读取正文 ——
    def read_mail(self, mail_id):
        """按 mail_id 取邮件 EML 原文，返回 bytes（RFC822）。"""
        token = self.get_token()
        url = f"{self.base}/exmail/app/read_mail?access_token={token}"
        try:
            resp = requests.post(url, json={"mail_id": mail_id}, timeout=30)
            data = resp.json()
        except Exception as e:
            raise TencentMailError(f"read_mail 请求失败：{e}")
        if data.get("errcode", 0) != 0:
            raise TencentMailError(
                f"read_mail 返回错误 errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
        eml = data.get("mail_data") or ""
        # 腾讯返回的是 EML 文本；统一编码为 bytes 交给 email.message_from_bytes 解析。
        # 若将来发现是 base64，可在此解码；当前按文本 utf-8 处理。
        return eml.encode("utf-8")


def _since_to_ts(since_expr, fallback_days=90):
    """把 since 表达式（'90d' / '6mon' / '2025-01-01~2026-07-18'）转成 unix 时间戳。

    复用 engine.parse_since_expr 得到 IMAP 日期串（如 '01-Jul-2026'），再解析。
    解析失败时回退到 fallback_days 天前。"""
    import datetime
    try:
        imap_date = engine.parse_since_expr(since_expr)
        # engine 返回的 IMAP 日期格式为 'DD-Mon-YYYY'（英文月名）
        dt = datetime.datetime.strptime(imap_date, "%d-%b-%Y")
        return dt.timestamp()
    except Exception:
        return (datetime.datetime.now() - datetime.timedelta(days=fallback_days)).timestamp()


def fetch_account(acc, rules, session, since_override=None):
    """腾讯官方 API 抓取入口，签名与 engine.fetch_account 一致。

    流程：取时间窗 → 分页 list 出 mail_id → 逐个 read_mail 拿 EML →
    组装 pending 交给 engine.process_pending 解析入库。返回新增发票数。"""
    cfg = load_config()
    if not cfg:
        raise TencentMailError(
            f"缺少 {CONFIG_PATH}；请参考该目录下的 tencent.json 模板填入 corpid/corpsecret/variant")
    acfg = _account_cfg(cfg, acc)
    client = TencentMailClient(acfg)

    mode = acc.get("fetch_mode") or "incremental"
    now_ts = time.time()

    # 时间窗：full / 覆盖参数 → 用 default_since 起点；incremental → 从上次抓取时间起
    if since_override or mode == "full":
        since_expr = since_override or acc.get("default_since") or "90d"
        begin_ts = _since_to_ts(since_expr)
        engine.log(f"    [腾讯API] 全量/覆盖模式，时间窗起点={since_expr}")
    else:
        last = acc.get("last_fetch")
        if last:
            try:
                import datetime
                begin_ts = datetime.datetime.strptime(last, "%Y-%m-%d %H:%M").timestamp()
            except Exception:
                begin_ts = _since_to_ts(acc.get("default_since") or "90d")
        else:
            begin_ts = _since_to_ts(acc.get("default_since") or "90d")
        engine.log(f"    [腾讯API] 增量模式，时间窗起点=上次抓取({last})")

    # 分页收集 mail_id（同账号串行，规避腾讯接口限速；每批 list 含 limit 个 id）
    mail_ids = []
    cursor = None
    pages = 0
    while True:
        ids, cursor, has_more = client.list_mail_ids(begin_ts, now_ts, cursor=cursor)
        mail_ids.extend(ids)
        pages += 1
        # 防止异常情况下无限翻页
        if not has_more or not cursor or pages > 1000:
            break
    engine.log(f"    [腾讯API] 时间窗内共列出 {len(mail_ids)} 封邮件（{pages} 页）")

    # 逐个读取 EML 并组装 pending。read_mail 之间留少量间隔，规避每分钟配额。
    pending = []
    for mid in mail_ids:
        try:
            eml = client.read_mail(mid)
            if eml:
                pending.append((mid, eml))
            time.sleep(0.3)  # 限速：约 200 次/分钟，留余量
        except TencentMailError as e:
            engine.log(f"    [腾讯API 读取失败 mail_id={mid}] {e}")
            continue

    # 离线解析 + 入库（与 IMAP 路径共用，零重复逻辑）
    new_inv = engine.process_pending(acc, rules, session, pending)

    # incremental 模式：推进"上次抓取时间"作为下次增量起点
    if mode == "incremental":
        db.set_account_fetch(acc["id"], time.strftime("%Y-%m-%d %H:%M"))
    return new_inv


def diagnose(corpid, corpsecret, variant=None):
    """诊断：给定 corpid/corpsecret，探明走哪套接口、能否拿到 token 与邮件列表。

    返回结论字符串；CLI `python tencent_mail.py diagnose --corpid X --corpsecret Y` 调用。"""
    lines = []
    variants = [variant] if variant else list(BASE_URLS)
    for v in variants:
        lines.append(f"=== 探测 variant='{v}' ({BASE_URLS[v]}) ===")
        try:
            cli = TencentMailClient({"variant": v, "corpid": corpid, "corpsecret": corpsecret})
            token = cli.get_token()
            lines.append(f"  ✓ gettoken 成功（token 长度 {len(token)}）")
        except TencentMailError as e:
            lines.append(f"  ✗ gettoken 失败：{e}")
            continue
        # 试列最近 1 小时窗口的邮件，验证列表接口是否可用
        now = time.time()
        try:
            ids, cursor, has_more = cli.list_mail_ids(now - 3600, now, limit=5)
            lines.append(f"  ✓ get_mail_list 可用，返回 {len(ids)} 封（has_more={has_more}）")
            if ids:
                lines.append(f"    示例 mail_id: {ids[0]}")
        except TencentMailError as e:
            lines.append(f"  ✗ get_mail_list 失败：{e}")
    return "\n".join(lines)


def _cli():
    ap = argparse.ArgumentParser(description="腾讯企业邮箱官方 API 适配层")
    sub = ap.add_subparsers(dest="cmd")
    d = sub.add_parser("diagnose", help="诊断走哪套接口、凭证是否有效")
    d.add_argument("--corpid", required=True)
    d.add_argument("--corpsecret", required=True)
    d.add_argument("--variant", choices=list(BASE_URLS), default=None)
    args = ap.parse_args()
    if args.cmd == "diagnose":
        print(diagnose(args.corpid, args.corpsecret, args.variant))
    else:
        ap.print_help()


if __name__ == "__main__":
    _cli()
