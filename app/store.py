# -*- coding: utf-8 -*-
"""兑换码（卡密）存储 —— SQLite，零外部依赖。"""
from __future__ import annotations

import json
import os
import random
import secrets
import sqlite3
import threading
import time

DB_PATH = os.environ.get("DB_PATH", os.path.join(
    os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")), "codes.db"))

# 去掉容易看错的字符（0/O/1/I/L）
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_LEN = int(os.environ.get("CODE_LEN", "8"))

_lock = threading.RLock()


def _conn():
    # ★R336_DB_RETRY：10-19 16:57~17:08 CST 线上出现 352 次 sqlite3.OperationalError:
    # "unable to open database file"，每个请求都要先过 guard.whitelist_on() → 全站白屏 11 分钟。
    # 这里加 3 次短重试；并且只在必要时切 WAL（每次 connect 都执行 journal_mode=WAL 要抢写锁，
    # 高并发下反而更容易互相卡死）；无论哪一步失败都把连接关掉，避免 fd 泄漏。
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    last = None
    for _i in range(3):
        c = None
        try:
            c = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=15000")
            try:
                if str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                    c.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
            return c
        except sqlite3.OperationalError as _e:
            last = _e
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
            time.sleep(0.2 * (_i + 1))
    raise last


def init():
    with _lock, _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS codes(
            code       TEXT PRIMARY KEY,
            batch      TEXT,
            note       TEXT,
            status     TEXT DEFAULT 'unused',   -- unused | visiting | done | disabled
            visits     INTEGER DEFAULT 0,
            claims     INTEGER DEFAULT 0,       -- 已成功领取次数
            max_claims INTEGER DEFAULT 0,       -- 建码时设定：可领几次（0 = 不限次）
            created_at INTEGER,
            used_at    INTEGER,
            used_ip    TEXT,
            used_ua    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_codes_status  ON codes(status);
        CREATE INDEX IF NOT EXISTS idx_codes_batch   ON codes(batch);
        CREATE INDEX IF NOT EXISTS idx_codes_created ON codes(created_at);
        CREATE TABLE IF NOT EXISTS settings(
            k TEXT PRIMARY KEY,
            v TEXT
        );
        CREATE TABLE IF NOT EXISTS events(
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            code       TEXT,                 -- 兑换码
            kind       TEXT,                 -- enter | scan | success | fail
            account    TEXT,                 -- 领到的账号（成功才有）
            ip         TEXT,
            ua         TEXT,
            note       TEXT,                 -- 人类可读说明
            uid        TEXT DEFAULT '',      -- R82-L 豆包 uid
            detail     TEXT DEFAULT '',      -- R82-L 结构化明细（JSON：before/after 到期时间）
            created_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_events_code    ON events(code);
        CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
        CREATE INDEX IF NOT EXISTS idx_events_kind    ON events(kind);
        """)
        # 老库补列（列已存在会抛错，忽略即可）
        for col in ("claims INTEGER DEFAULT 0", "max_claims INTEGER DEFAULT 0",
                    "owner_id INTEGER DEFAULT 0", "root_id INTEGER DEFAULT 0",
                    "created_by TEXT DEFAULT ''"):
            try:
                c.execute("ALTER TABLE codes ADD COLUMN " + col)
            except Exception:
                pass
        for _ecol in ("owner_id INTEGER DEFAULT 0", "uid TEXT DEFAULT ''", "detail TEXT DEFAULT ''"):
            try:
                c.execute("ALTER TABLE events ADD COLUMN " + _ecol)
            except Exception:
                pass
        c.execute("CREATE INDEX IF NOT EXISTS idx_codes_owner  ON codes(owner_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_owner ON events(owner_id)")


def _gen_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_LEN))   # ★R211：CSPRNG，防 MT 状态预测


def generate(count: int, batch: str = "", note: str = "", max_claims=None,
             owner_id: int = 0, root_id: int = 0, created_by: str = "") -> list:
    """max_claims: 0 = 不限次；n = 可领 n 次；None = 用全局默认。

    owner_id/root_id/created_by: R63 多租户归属（老调用点不传即为 0=总台）。
    """
    count = max(1, min(int(count), 100000))
    batch = batch or time.strftime("%Y%m%d%H%M%S")
    mc = 1  # ★R209：一码一次（固定，不可配置）
    owner_id = int(owner_id or 0)
    root_id = int(root_id or 0)
    created_by = str(created_by or "")[:64]
    now = int(time.time())
    made = []
    with _lock, _conn() as c:
        while len(made) < count:
            code = _gen_code()
            try:
                c.execute("INSERT INTO codes(code,batch,note,status,claims,max_claims,created_at,"
                          "owner_id,root_id,created_by) VALUES(?,?,?,'unused',0,?,?,?,?,?)",
                          (code, batch, note, mc, now, owner_id, root_id, created_by))
                made.append(code)
            except sqlite3.IntegrityError:
                continue          # 撞码，重来
    return made


def get(code: str):
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
        return dict(r) if r else None


def touch(code: str, ip: str, ua: str) -> bool:
    """首次进入领取页：记一次访问。已完成/停用的码不放行。"""
    with _lock, _conn() as c:
        r = c.execute("SELECT status FROM codes WHERE code=?", (code,)).fetchone()
        if not r or r["status"] in ("done", "disabled"):
            return False
        c.execute("UPDATE codes SET visits=visits+1, "
                  "status=CASE WHEN status='unused' THEN 'visiting' ELSE status END, "
                  "used_ip=COALESCE(used_ip,?), used_ua=COALESCE(used_ua,?) WHERE code=?",
                  (ip, ua[:200], code))
        return True


def add_claim(code: str, limit=None):
    """记一次成功领取。limit=None 时用该码建码时设定的 max_claims（0=不限次）。"""
    with _lock, _conn() as c:
        r = c.execute("SELECT COALESCE(claims,0) cl, COALESCE(max_claims,0) ml, status st "
                      "FROM codes WHERE code=?", (code,)).fetchone()
        if not r:
            return 0
        # ★R209c：停用的码一律不落成功（并发/迟到回调/对账都挡在这一层）
        if str(r["st"] or "") == "disabled":
            return int(r["cl"] or 0)
        # ★R209：原子「只记一次」——并发/重复回调也不会把 claims 加到 2
        if int(r["cl"] or 0) >= 1:
            c.execute("UPDATE codes SET status='done', used_at=COALESCE(used_at,?) WHERE code=?",
                      (int(time.time()), code))
            return int(r["cl"] or 0)
        c.execute("UPDATE codes SET claims=1, status='done', used_at=? WHERE code=?",
                  (int(time.time()), code))
        return 1


def max_claims_of(code: str) -> int:
    """该卡密可领次数（0 = 不限次）；查不到返回 -1。"""
    with _lock, _conn() as c:
        r = c.execute("SELECT COALESCE(max_claims,0) ml FROM codes WHERE code=?", (code,)).fetchone()
        return 1 if r else -1   # ★R209：固定 1 次


def mark_done(code: str):
    with _lock, _conn() as c:
        c.execute("UPDATE codes SET status='done', used_at=? WHERE code=?", (int(time.time()), code))


def set_status(code: str, status: str):
    with _lock, _conn() as c:
        c.execute("UPDATE codes SET status=? WHERE code=?", (status, code))


def get_setting(k: str, default: str = "") -> str:
    with _lock, _conn() as c:
        r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default


def set_setting(k: str, v: str):
    with _lock, _conn() as c:
        c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


# --------------------------------------------------------------------------- #
# 领取记录（对方扫码 / 领取 成功-失败 全记录）
# --------------------------------------------------------------------------- #
KIND_TEXT = {"enter": "进入领取", "scan": "已扫码", "login": "登录成功",
             "success": "领取成功", "fail": "未到账"}


def log_event(code: str, kind: str, account: str = "", ip: str = "", ua: str = "", note: str = "",
              owner_id: int = 0, uid: str = "", detail=None):
    """记一条事件。kind: enter | scan | login | success | fail。

    R82-L：uid = 豆包 uid；detail = 结构化明细（dict 存成 JSON 字符串，放 before/after 会员到期时间等）。
    """
    try:
        _uid = str(uid or "")[:64]
        _det = ""
        if detail:
            _det = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        _det = _det[:2000]
        with _lock, _conn() as c:
            c.execute("INSERT INTO events(code,kind,account,ip,ua,note,created_at,owner_id,uid,detail) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (code or "", kind or "", (account or "")[:80], (ip or "")[:64],
                       (ua or "")[:200], (note or "")[:300], int(time.time()), int(owner_id or 0),
                       _uid, _det))
    except Exception:
        pass          # 记日志失败绝不能影响主流程


def _ev_where(code: str = "", kind: str = "", owner_ids=None):
    q, args = " WHERE 1=1", []
    if code:
        q += " AND code=?"; args.append(code.upper())
    if kind:
        q += " AND kind=?"; args.append(kind)
    if owner_ids is not None:
        ids = [int(x) for x in owner_ids]
        if ids:
            q += " AND owner_id IN (%s)" % ",".join("?" * len(ids))
            args += ids
        else:
            q += " AND 1=0"
    return q, args


def events(code: str = "", kind: str = "", limit: int = 50, offset: int = 0, owner_ids=None) -> list:
    q, args = _ev_where(code, kind, owner_ids)
    q += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args += [max(1, min(int(limit), 500)), max(0, int(offset))]
    with _lock, _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM events" + q, args).fetchall()]
    for r in rows:
        r["kind_text"] = KIND_TEXT.get(r.get("kind"), r.get("kind") or "")
        t = r.get("created_at") or 0
        r["time_str"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""
        # R82-L：uid 与结构化明细（detail 保留原始字符串，detail_obj 是解析后的 dict）
        r["uid"] = str(r.get("uid") or "")
        r["detail"] = str(r.get("detail") or "")
        try:
            r["detail_obj"] = json.loads(r["detail"]) if r["detail"] else {}
        except Exception:
            r["detail_obj"] = {}
        if not isinstance(r["detail_obj"], dict):
            r["detail_obj"] = {}
    return rows


def events_count(code: str = "", kind: str = "", owner_ids=None) -> int:
    q, args = _ev_where(code, kind, owner_ids)
    with _lock, _conn() as c:
        return int(c.execute("SELECT COUNT(*) n FROM events" + q, args).fetchone()["n"])


def events_stats(code: str = "", kind: str = "", owner_ids=None) -> dict:
    q, args = _ev_where(code, kind, owner_ids)
    with _lock, _conn() as c:
        rows = c.execute("SELECT kind, COUNT(*) n FROM events" + q + " GROUP BY kind", args).fetchall()
        d = {r["kind"]: r["n"] for r in rows}
        one = c.execute("SELECT COUNT(*) n FROM events" + q, args).fetchone()
    out = {k: int(d.get(k, 0)) for k in ("enter", "scan", "login", "success", "fail")}
    out["total"] = int(one["n"] or 0)
    return out


def count(status: str = "", batch: str = "", owner_ids=None) -> int:
    """R711：支持按归属代理过滤（owner_ids=None 全站；[] 空集）。"""
    q = "SELECT COUNT(*) n FROM codes WHERE 1=1"
    args = []
    if status:
        q += " AND status=?"; args.append(status)
    if batch:
        q += " AND batch=?"; args.append(batch)
    if owner_ids is not None:
        ids = [int(x) for x in owner_ids]
        if ids:
            q += " AND owner_id IN (%s)" % ",".join("?" * len(ids))
            args += ids
        else:
            q += " AND 1=0"
    with _lock, _conn() as c:
        return int(c.execute(q, args).fetchone()["n"])


def stats(owner_ids=None) -> dict:
    """R711：支持按归属代理过滤（owner_ids=None 全站；[] 空集）。"""
    w, args = "", []
    if owner_ids is not None:
        ids = [int(x) for x in owner_ids]
        if ids:
            w = " WHERE owner_id IN (%s)" % ",".join("?" * len(ids))
            args += ids
        else:
            w = " WHERE 1=0"
    with _lock, _conn() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM codes" + w + " GROUP BY status", args).fetchall()
        d = {r["status"]: r["n"] for r in rows}
        d["total"] = sum(d.values())
        row = c.execute("SELECT COALESCE(SUM(claims),0) n FROM codes" + w, args).fetchone()
        d["claims"] = int(row["n"] or 0)
        return d


# ★R333_STATS_TTL：/healthz 每 2 秒最多全表扫一次（codes 5.8 万行，单次 17~20ms）。
_STATS_CACHE = {"at": 0.0, "val": None}


def stats_cached(ttl: float = 2.0) -> dict:
    now = time.time()
    hit = _STATS_CACHE.get("val")
    if hit is not None and (now - float(_STATS_CACHE.get("at") or 0)) < ttl:
        return dict(hit)
    v = stats()
    _STATS_CACHE["at"] = now
    _STATS_CACHE["val"] = dict(v)
    return v


def _owner_map(conn, ids):
    """owner_id -> username（tenants 表还不存在时安全降级）。"""
    ids = sorted({int(i) for i in ids if i})
    if not ids:
        return {}
    try:
        ph = ",".join("?" * len(ids))
        rows = conn.execute("SELECT id,username FROM tenants WHERE id IN (%s)" % ph, ids).fetchall()
        return {int(r["id"]): r["username"] for r in rows}
    except Exception:
        return {}


def _fill_owner(conn, rows):
    om = _owner_map(conn, [(r.get("owner_id") or 0) for r in rows])
    for r in rows:
        oid = int(r.get("owner_id") or 0)
        r["owner_name"] = om.get(oid) or ("总台" if oid == 0 else "已删除账号")
    return rows


def listing(status: str = "", batch: str = "", limit: int = 200, offset: int = 0,
            owner_ids=None, root_ids=None) -> list:
    q = "SELECT * FROM codes WHERE 1=1"
    args = []
    if status:
        q += " AND status=?"
        args.append(status)
    if batch:
        q += " AND batch=?"
        args.append(batch)
    if owner_ids is not None:
        ids = [int(x) for x in owner_ids]
        if ids:
            q += " AND owner_id IN (%s)" % ",".join("?" * len(ids))
            args += ids
        else:
            q += " AND 1=0"
    if root_ids is not None:
        ids = [int(x) for x in root_ids]
        if ids:
            q += " AND root_id IN (%s)" % ",".join("?" * len(ids))
            args += ids
        else:
            q += " AND 1=0"
    q += " ORDER BY created_at DESC, code DESC LIMIT ? OFFSET ?"
    args += [max(1, min(int(limit), 2000)), max(0, int(offset))]
    with _lock, _conn() as c:
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
        return _fill_owner(c, rows)


def batches() -> list:
    with _lock, _conn() as c:
        rows = c.execute("SELECT batch, COUNT(*) n, MIN(created_at) t FROM codes GROUP BY batch "
                         "ORDER BY t DESC LIMIT 60").fetchall()
        return [dict(r) for r in rows]
