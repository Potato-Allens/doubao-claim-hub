# -*- coding: utf-8 -*-
"""多租户数据层 —— 总台(0) / 代理商(1) / 子代理商(2)。

只依赖标准库 + app.store（同一个 SQLite 库） + app.otp（密码哈希）。
所有对外错误一律抛 ValueError(中文人话)，不抛英文堆栈。
"""
from __future__ import annotations

import sqlite3
import time

from . import store
from .otp import hash_password, verify_password

_LOCK = store._lock

STATUSES = ("active", "disabled", "banned")
STATUS_TEXT = {"active": "正常", "disabled": "已停用", "banned": "已封禁"}
LEVEL_TEXT = {0: "总台", 1: "代理商", 2: "子代理商"}


def _conn():
    return store._conn()


def _now() -> int:
    return int(time.time())


def _row(r):
    if not r:
        return None
    d = dict(r)
    total = int(d.get("quota_total") or 0)
    given = int(d.get("quota_given") or 0)
    used = int(d.get("quota_used") or 0)
    d["available"] = max(0, total - given - used)
    d["level_text"] = LEVEL_TEXT.get(int(d.get("level") or 0), "未知")
    d["status_text"] = STATUS_TEXT.get(d.get("status") or "", d.get("status") or "")
    ca = int(d.get("created_at") or 0)
    d["created_at_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ca)) if ca else ""
    ll = int(d.get("last_login_at") or 0)
    d["last_login_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ll)) if ll else ""
    return d


_DDL = """
CREATE TABLE IF NOT EXISTS tenants(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE, pwd_hash TEXT,
  parent_id INTEGER DEFAULT 0, level INTEGER DEFAULT 1,
  quota_total INTEGER DEFAULT 0,
  quota_given INTEGER DEFAULT 0,
  quota_used INTEGER DEFAULT 0,
  max_sub_agents INTEGER DEFAULT 0, sub_agents_used INTEGER DEFAULT 0,
  status TEXT DEFAULT 'active',
  remark TEXT DEFAULT '', created_by TEXT DEFAULT '', created_at INTEGER,
  last_login_at INTEGER DEFAULT 0, last_ip TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tenants_parent ON tenants(parent_id);
CREATE INDEX IF NOT EXISTS idx_tenants_level  ON tenants(level);
CREATE INDEX IF NOT EXISTS idx_tenants_status ON tenants(status);
CREATE TABLE IF NOT EXISTS quota_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER,
  from_id INTEGER, to_id INTEGER, delta INTEGER, kind TEXT,
  reason TEXT, memo TEXT, operator TEXT, operator_level INTEGER, balance_after INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ledger_from ON quota_ledger(from_id);
CREATE INDEX IF NOT EXISTS idx_ledger_to   ON quota_ledger(to_id);
CREATE INDEX IF NOT EXISTS idx_ledger_ts   ON quota_ledger(ts);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER,
  actor_type TEXT, actor_id INTEGER, actor_name TEXT,
  action TEXT, target TEXT, detail TEXT, ip TEXT, ua TEXT, level TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts    ON audit_log(ts);
"""


def init():
    """建 3 张新表 + 对 codes/events 补列（列已存在则忽略）。幂等，可反复调用。"""
    store.init()
    with _LOCK, _conn() as c:
        c.executescript(_DDL)
        for col in ("owner_id INTEGER DEFAULT 0", "root_id INTEGER DEFAULT 0",
                    "created_by TEXT DEFAULT ''"):
            try:
                c.execute("ALTER TABLE codes ADD COLUMN " + col)
            except Exception:
                pass
        try:
            c.execute("ALTER TABLE events ADD COLUMN owner_id INTEGER DEFAULT 0")
        except Exception:
            pass
        c.execute("CREATE INDEX IF NOT EXISTS idx_codes_owner  ON codes(owner_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_owner ON events(owner_id)")


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def create(username, pwd, level=1, parent_id=0, max_sub_agents=0, remark="", created_by=""):
    username = str(username or "").strip()
    pwd = str(pwd or "")
    if not username:
        raise ValueError("用户名不能为空")
    if len(username) > 32:
        raise ValueError("用户名过长（最多 32 个字符）")
    if any(ch.isspace() for ch in username):
        raise ValueError("用户名不能包含空格")
    if len(pwd) < 4:
        raise ValueError("密码至少 4 位")
    try:
        level = int(level)
    except Exception:
        raise ValueError("账号级别不正确")
    if level not in (1, 2):
        raise ValueError("账号级别只能是 1（代理商）或 2（子代理商）")
    parent_id = int(parent_id or 0)
    if level == 1:
        parent_id = 0
    try:
        max_sub_agents = max(0, min(int(max_sub_agents or 0), 100000))
    except Exception:
        raise ValueError("可开子代理数量不正确")
    with _LOCK, _conn() as c:
        if c.execute("SELECT id FROM tenants WHERE username=?", (username,)).fetchone():
            raise ValueError("该用户名已存在，请换一个")
        if parent_id and not c.execute("SELECT id FROM tenants WHERE id=?", (parent_id,)).fetchone():
            raise ValueError("上级账号不存在")
        try:
            cur = c.execute(
                "INSERT INTO tenants(username,pwd_hash,parent_id,level,quota_total,quota_given,"
                "quota_used,max_sub_agents,sub_agents_used,status,remark,created_by,created_at) "
                "VALUES(?,?,?,?,0,0,0,?,0,'active',?,?,?)",
                (username, hash_password(pwd), parent_id, level, max_sub_agents,
                 str(remark or "")[:200], str(created_by or "")[:64], _now()))
        except sqlite3.IntegrityError:
            raise ValueError("该用户名已存在，请换一个")
        tid = int(cur.lastrowid)
    return get(tid)


def get(tid):
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM tenants WHERE id=?", (int(tid),)).fetchone()
    return _row(r)


def get_by_name(username):
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM tenants WHERE username=?",
                      (str(username or "").strip(),)).fetchone()
    return _row(r)


def authenticate(username, pwd):
    """校验账号密码；成功返回账号 dict，失败返回 None（不区分「用户不存在/密码错」）。"""
    row = get_by_name(username)
    if not row:
        return None
    if not verify_password(row.get("pwd_hash") or "", pwd or ""):
        return None
    return row


def set_status(tid, status):
    status = str(status or "").strip()
    if status not in STATUSES:
        raise ValueError("状态值不正确")
    with _LOCK, _conn() as c:
        cur = c.execute("UPDATE tenants SET status=? WHERE id=?", (status, int(tid)))
        if cur.rowcount == 0:
            raise ValueError("账号不存在")
    return get(tid)


def set_username(tid, username):
    """改登录账号（不动密码）。空 / 超长 / 带空格 / 重名 -> ValueError。"""
    username = str(username or "").strip()
    if not username:
        raise ValueError("登录账号不能为空")
    if len(username) > 32:
        raise ValueError("登录账号最长 32 个字符")
    if any(ch.isspace() for ch in username):
        raise ValueError("登录账号不能包含空格")
    tid = int(tid)
    with _LOCK, _conn() as c:
        r = c.execute("SELECT id FROM tenants WHERE username=?", (username,)).fetchone()
        if r and int(r["id"]) != tid:
            raise ValueError("这个登录账号已经被占用了，换一个")
        try:
            cur = c.execute("UPDATE tenants SET username=? WHERE id=?", (username, tid))
        except sqlite3.IntegrityError:
            raise ValueError("这个登录账号已经被占用了，换一个")
        if cur.rowcount == 0:
            raise ValueError("账号不存在")
    return True


def set_password(tid, new_pwd):
    new_pwd = str(new_pwd or "")
    if len(new_pwd) < 4:
        raise ValueError("密码至少 4 位")
    with _LOCK, _conn() as c:
        cur = c.execute("UPDATE tenants SET pwd_hash=? WHERE id=?",
                        (hash_password(new_pwd), int(tid)))
        if cur.rowcount == 0:
            raise ValueError("账号不存在")
    return True


def set_max_sub_agents(tid, n):
    try:
        n = max(0, min(int(n), 100000))
    except Exception:
        raise ValueError("可开子代理数量不正确")
    with _LOCK, _conn() as c:
        cur = c.execute("UPDATE tenants SET max_sub_agents=? WHERE id=?", (n, int(tid)))
        if cur.rowcount == 0:
            raise ValueError("账号不存在")
    return get(tid)


def set_remark(tid, remark):
    with _LOCK, _conn() as c:
        c.execute("UPDATE tenants SET remark=? WHERE id=?", (str(remark or "")[:200], int(tid)))
    return get(tid)


def touch_login(tid, ip=""):
    with _LOCK, _conn() as c:
        c.execute("UPDATE tenants SET last_login_at=?, last_ip=? WHERE id=?",
                  (_now(), str(ip or "")[:64], int(tid)))


def children(parent_id, status="", q="", limit=500, offset=0):
    where, args = " WHERE parent_id=?", [int(parent_id)]
    if status:
        where += " AND status=?"; args.append(str(status))
    q = str(q or "").strip()
    if q:
        where += " AND (username LIKE ? OR remark LIKE ?)"; args += ["%" + q + "%", "%" + q + "%"]
    with _LOCK, _conn() as c:
        rows = c.execute("SELECT * FROM tenants" + where + " ORDER BY id ASC LIMIT ? OFFSET ?",
                         args + [max(1, min(int(limit), 2000)), max(0, int(offset))]).fetchall()
    return [_row(r) for r in rows]


def child_ids(parent_id):
    with _LOCK, _conn() as c:
        rows = c.execute("SELECT id FROM tenants WHERE parent_id=?", (int(parent_id),)).fetchall()
    return [int(r["id"]) for r in rows]


def scope_ids(tid):
    """可见范围：自己 + 直接下级（本系统只有三层，够用）。"""
    tid = int(tid)
    return [tid] + child_ids(tid)


def listing(parent_id=None, level=None, q="", limit=50, offset=0):
    where, args = " WHERE 1=1", []
    if parent_id is not None:
        where += " AND parent_id=?"; args.append(int(parent_id))
    if level is not None:
        where += " AND level=?"; args.append(int(level))
    q = str(q or "").strip()
    if q:
        where += " AND (username LIKE ? OR remark LIKE ?)"; args += ["%" + q + "%", "%" + q + "%"]
    lim = max(1, min(int(limit), 500))
    off = max(0, int(offset))
    with _LOCK, _conn() as c:
        total = int(c.execute("SELECT COUNT(*) n FROM tenants" + where, args).fetchone()["n"])
        rows = c.execute("SELECT * FROM tenants" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                         args + [lim, off]).fetchall()
    pages = (total + lim - 1) // lim if lim else 0
    out = [_row(r) for r in rows]
    # R81：库里 tenants.sub_agents_used 这列从不维护（永远是 0），这里按实时数量回填，
    # 只影响返回给前端的展示值（写路径照旧走 sub_agents_used()/can_add_sub()）。
    try:
        ids = [int(x.get("id") or 0) for x in out if x]
        if ids:
            ph = ",".join("?" * len(ids))
            with _LOCK, _conn() as c:
                cnt = {int(r["parent_id"]): int(r["n"] or 0) for r in c.execute(
                    "SELECT parent_id, COUNT(*) n FROM tenants WHERE level=2 AND parent_id IN (%s) "
                    "GROUP BY parent_id" % ph, ids).fetchall()}
            for x in out:
                if x:
                    x["sub_agents_used"] = cnt.get(int(x.get("id") or 0), 0)
    except Exception:
        pass
    return {"rows": out, "total": total, "pages": pages,
            "limit": lim, "offset": off}


def stats() -> dict:
    with _LOCK, _conn() as c:
        rows = c.execute("SELECT level, status, COUNT(*) n FROM tenants "
                         "GROUP BY level, status").fetchall()
    d = {"total": 0, "l1": 0, "l2": 0, "active": 0, "disabled": 0, "banned": 0}
    for r in rows:
        n = int(r["n"] or 0)
        lv = int(r["level"] or 0)
        st = r["status"] or ""
        d["total"] += n
        if lv == 1:
            d["l1"] += n
        elif lv == 2:
            d["l2"] += n
        if st in d:
            d[st] += n
    return d


def sub_agents_used(parent_id) -> int:
    with _LOCK, _conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM tenants WHERE parent_id=?",
                      (int(parent_id),)).fetchone()["n"]
    return int(n or 0)


def can_add_sub(parent_id, max_agents) -> bool:
    """名额校验：已开子代理数 < 允许上限。"""
    return sub_agents_used(parent_id) < int(max_agents or 0)
