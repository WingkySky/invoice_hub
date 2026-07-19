"""
高层门面（Facade）—— 供 agent / 后续 skill / CLI / Web 统一调用的程序接口。

设计原则：
  - 本文件【不写任何传输层逻辑】（不碰 HTTP、不碰 argparse），只把 engine / db 的
    核心能力包装成「返回纯 dict / 基础类型」的函数。
  - 这样 CLI（hub.py）可 import 本模块输出结构化结果；Web（web/app.py）可 import 本模块
    处理请求；agent 或 WorkBuddy skill 也可直接 import 调用——无需起服务、无需解析打印文本。
  - 所有函数返回「可被 json.dumps 序列化」的对象（dict / list / 基础类型），便于 --json 与 HTTP。

为什么单独成层：连接测试、抓取、发票查询等逻辑在 Web 与 CLI 都要用，抽到这里作为单一真相源，
避免 web/app.py 与 hub.py 各写一份导致逻辑分叉（修改要改两处）。
"""
from typing import Any, Dict, List, Optional

import db
import engine


def list_accounts(enabled_only: bool = False) -> List[Dict[str, Any]]:
    """列出全部（或仅启用）账号，返回脱敏后的 dict 列表（不含明文密码）。"""
    rows = db.get_accounts(enabled_only=enabled_only)
    return [_safe_account(a) for a in rows]


def get_account(acc_id: int) -> Optional[Dict[str, Any]]:
    """按 id 取单个账号（脱敏）；不存在返回 None。"""
    acc = db.get_account(acc_id)
    return _safe_account(acc) if acc else None


def add_account(fields: Dict[str, Any]) -> Dict[str, Any]:
    """新增或按 email 更新账号。fields 同 db.upsert_account 的字段。返回脱敏后的账号。"""
    db.upsert_account(fields)
    # upsert 后按 email 取回最新行（email 是唯一键）
    email = fields.get("email")
    acc = next((a for a in db.get_accounts() if a["email"] == email), None)
    return _safe_account(acc) if acc else {}


def toggle_account(acc_id: int, enabled: bool) -> Dict[str, Any]:
    """启用/停用账号，返回结果摘要。"""
    db.set_account_enabled(acc_id, 1 if enabled else 0)
    return {"ok": True, "id": acc_id, "enabled": bool(enabled)}


def delete_account(acc_id: int) -> Dict[str, Any]:
    """删除账号（含级联本地 PDF 目录）。"""
    db.delete_account(acc_id)
    return {"ok": True, "id": acc_id}


def test_connection(acc_id: int) -> Dict[str, Any]:
    """测试单个账号的 IMAP 连接（登录 + 选中文件夹 + NOOP 探活）。

    复用连接池，验证通过即归还、不做 logout，避免每次都新建登录触发腾讯企业邮箱风控。
    返回 {"ok": bool, "msg": str}。
    """
    acc = db.get_account(acc_id)
    if not acc:
        return {"ok": False, "msg": "账号不存在"}
    try:
        M = engine.pool_get(acc)
        try:
            # pool_get 已做登录 + 选中文件夹校验 + NOOP 探活
            return {"ok": True, "msg": "连接成功（登录+选中文件夹均通过）"}
        finally:
            engine.pool_put(acc, M)
    except Exception as e:
        return {"ok": False, "msg": f"连接失败：{type(e).__name__}: {e}"}


def fetch(acc_id: Optional[int] = None, since: Optional[str] = None) -> Dict[str, Any]:
    """触发一次抓取。acc_id 为空抓全部启用账号；since 为可选临时覆盖（不写回配置）。

    返回 {"ok": bool, "new": int}。
    """
    try:
        new = engine.fetch_all(since_override=since, acc_id=acc_id)
        return {"ok": True, "new": int(new)}
    except Exception as e:
        return {"ok": False, "new": 0, "msg": f"{type(e).__name__}: {e}"}


def get_invoices(filters: Optional[Dict[str, Any]] = None,
                 page: int = 1, page_size: int = 50) -> Dict[str, Any]:
    """分页查询发票，返回 {"rows": [...], "total": int, "page": int, "page_size": int}。"""
    filters = filters or {}
    rows = db.get_invoices(filters, page=page, page_size=page_size)
    total = db.get_invoices_count(filters)
    return {"rows": [dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}


def get_stats(filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """发票合计统计，返回 {"count": int, "total": float, "by_buyer": {...}}。"""
    return db.get_stats(filters or {})


def reconcile(dry_run: bool = False) -> Dict[str, Any]:
    """对账修复（回填孤儿 PDF）。返回报告 dict。"""
    return engine.reconcile(dry_run=dry_run)


def reparse_all() -> Dict[str, Any]:
    """用最新规则重新解析所有已入库 PDF。返回 {"total": int, "updated": int}。"""
    total, updated = engine.reparse_all_pdfs()
    return {"total": int(total), "updated": int(updated)}


def _safe_account(a: Dict[str, Any]) -> Dict[str, Any]:
    """脱敏：不返回明文密码，只给 password_set 标记（前端据此提示'已保存'）。"""
    d = dict(a)
    d["password_set"] = bool(d.get("password"))
    d.pop("password", None)
    return d
