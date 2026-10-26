# -*- coding: utf-8 -*-
"""安全审计日志 —— 保留最新 KEEP 条（默认 300），超出的自动删除。

只能由 app/tenant_api.py 调用；本模块只负责定义。
"""
from __future__ import annotations

import os
import time

from . import store

_LOCK = store._lock
KEEP = int(os.environ.get("AUDIT_KEEP", "300"))

ACTION_TEXT = {
    "login": "登录", "logout": "退出登录", "login_fail": "登录失败",
    "create_tenant": "创建账号", "update_tenant": "修改账号",
    "quota_grant": "总台调整额度", "quota_recycle": "总台回收额度",
    "quota_allocate": "划拨额度", "set_status": "账号状态变更",
    "set_password": "重置密码", "gen_codes": "生成卡密",
    "export": "导出数据", "proxy_update": "代理配置修改",
    "acl": "访问控制", "admin_login": "总台登录",
    "dl_create": "生成下载链接", "dl_revoke": "撤销下载链接",
    "dl_fetch": "下载链接被使用", "dl_missing": "下载文件丢失",
    "session_kill": "会话失效",
}
LEVEL_TEXT = {"info": "信息", "warn": "警告", "danger": "危险"}


def _conn():
    return store._conn()


def _roll(c):
    keep = max(1, int(KEEP))
    c.execute("DELETE FROM audit_log WHERE id NOT IN "
              "(SELECT id FROM audit_log ORDER BY id DESC LIMIT ?)", (keep,))


def log(action, actor_name="", actor_type="admin", actor_id=0, target="", detail="",
        ip="", ua="", level="info"):
    """写一条审计。写失败绝不抛（不能影响主流程）。"""
    try:
        with _LOCK, _conn() as c:
            c.execute("INSERT INTO audit_log(ts,actor_type,actor_id,actor_name,action,target,"
                      "detail,ip,ua,level) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (int(time.time()), str(actor_type or "admin")[:24], int(actor_id or 0),
                       str(actor_name or "")[:64], str(action or "")[:48], str(target or "")[:120],
                       str(detail or "")[:400], str(ip or "")[:64], str(ua or "")[:200],
                       level if level in LEVEL_TEXT else "info"))
            _roll(c)
    except Exception:
        pass


def count() -> int:
    with _LOCK, _conn() as c:
        return int(c.execute("SELECT COUNT(*) n FROM audit_log").fetchone()["n"])


def query(scope_ids=None, limit=50, offset=0, action="", q=""):
    """scope_ids=可见租户 id 列表（None=全站）；按 actor_id 过滤。"""
    where, args = " WHERE 1=1", []
    if scope_ids is not None:
        ids = sorted({int(i) for i in scope_ids if i})
        if ids:
            where += " AND actor_id IN (%s)" % ",".join("?" * len(ids))
            args += ids
        else:
            where += " AND 1=0"
    if action:
        where += " AND action=?"
        args.append(str(action))
    q = str(q or "").strip()
    if q:
        where += " AND (target LIKE ? OR detail LIKE ? OR actor_name LIKE ?)"
        args += ["%" + q + "%", "%" + q + "%", "%" + q + "%"]
    lim = max(1, min(int(limit), 500))
    off = max(0, int(offset))
    with _LOCK, _conn() as c:
        total = int(c.execute("SELECT COUNT(*) n FROM audit_log" + where, args).fetchone()["n"])
        rows = c.execute("SELECT * FROM audit_log" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                         args + [lim, off]).fetchall()
        st = c.execute("SELECT level, COUNT(*) n FROM audit_log" + where +
                       " GROUP BY level", args).fetchall()
    stats = {"total": total, "info": 0, "warn": 0, "danger": 0}
    for r in st:
        lv = r["level"] or "info"
        if lv in stats:
            stats[lv] = int(r["n"] or 0)
    out = []
    for r in rows:
        d = dict(r)
        d["action_text"] = ACTION_TEXT.get(d.get("action"), d.get("action") or "")
        d["level_text"] = LEVEL_TEXT.get(d.get("level"), d.get("level") or "")
        t = int(d.get("ts") or 0)
        d["time_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""
        out.append(d)
    pages = (total + lim - 1) // lim if lim else 0
    return {"rows": out, "total": total, "pages": pages, "stats": stats,
            "limit": lim, "offset": off}


def actions() -> dict:
    return dict(ACTION_TEXT)


def levels() -> dict:
    return dict(LEVEL_TEXT)
