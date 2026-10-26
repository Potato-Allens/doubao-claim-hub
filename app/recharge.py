# -*- coding: utf-8 -*-
"""R80：充值码（额度充值卡）—— 总台生成，代理商 / 子代理自助充值。

设计（防破）：
  · 码 = 前缀 "DB" + 16 位随机字符（32 字符表，剔除 0 O 1 I L）=> 80 bit 熵，
    用 secrets（CSPRNG）生成，纯随机、不能从已有码推导下一个；
  · "面额" 只存库、不编码进码本身 -> 就算格式被猜中，也推不出这张码值多少张；
  · 兑换走 UPDATE ... WHERE code=? AND status='unused' 条件更新 -> 原子，
    天然防并发 / 重放 / 重复兑换；
  · 表与 store 同库（codes.db）WAL，UNIQUE 索引兜底；
  · 限速与失败熔断放在接口层（tenant_api：每 IP + 每账号 + 每 IP 尝试总数）。

对外：init / gen / redeem / listing / listing_by_users / summary_by_users /
      stats / batches / void / fmt / norm
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import time

from app import store

_PREFIX = "DB"
_LOCK = store._lock
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
BODY_LEN = int(os.environ.get("RC_BODY_LEN", "16"))
MAX_AMOUNT = int(os.environ.get("RC_MAX_AMOUNT", "100000"))
MAX_COUNT = int(os.environ.get("RC_MAX_COUNT", "2000"))
STATUS_TEXT = {"unused": "未使用", "used": "已充值", "void": "已作废"}

_ready = False

DBSQL = """
CREATE TABLE IF NOT EXISTS recharge_codes(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT UNIQUE NOT NULL,
    amount       INTEGER NOT NULL DEFAULT 0,
    batch        TEXT DEFAULT '',
    note         TEXT DEFAULT '',
    status       TEXT DEFAULT 'unused',
    created_by   TEXT DEFAULT '',
    created_at   INTEGER DEFAULT 0,
    used_by      INTEGER DEFAULT 0,
    used_by_name TEXT DEFAULT '',
    used_at      INTEGER DEFAULT 0,
    used_ip      TEXT DEFAULT '',
    used_before  INTEGER DEFAULT 0,
    used_after   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rc_status ON recharge_codes(status, id);
CREATE INDEX IF NOT EXISTS idx_rc_batch  ON recharge_codes(batch);
CREATE INDEX IF NOT EXISTS idx_rc_user   ON recharge_codes(used_by, used_at);
"""


def _conn():
    os.makedirs(os.path.dirname(store.DB_PATH), exist_ok=True)
    c = sqlite3.connect(store.DB_PATH, timeout=15, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init():
    global _ready
    if _ready:
        return
    with _LOCK, _conn() as c:
        c.executescript(DBSQL)
    _ready = True


def norm(s):
    return "".join(ch for ch in str(s or "").upper() if ch.isalnum())


def fmt(code):
    s = norm(code)
    if not s:
        return ""
    body = s[len(_PREFIX):] if s.startswith(_PREFIX) else s
    parts = [body[i:i + 4] for i in range(0, len(body), 4)]
    return "-".join([_PREFIX] + parts)


def _mint():
    return _PREFIX + "".join(secrets.choice(_ALPHABET) for _ in range(BODY_LEN))


def _decorate(r):
    d = dict(r)
    d["code_str"] = fmt(d.get("code"))
    d["status_text"] = STATUS_TEXT.get(str(d.get("status") or ""), str(d.get("status") or ""))
    t = int(d.get("created_at") or 0)
    d["created_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""
    u = int(d.get("used_at") or 0)
    d["used_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(u)) if u else ""
    return d

def gen(amount, count, batch="", note="", operator=""):
    """生成 count 个面额 amount 的充值码。返回 [{code, code_str, amount, batch, note}]。"""
    try:
        amount = int(amount)
    except Exception:
        raise ValueError("面额要填数字")
    if amount <= 0 or amount > MAX_AMOUNT:
        raise ValueError("面额要在 1 ~ %d 之间" % MAX_AMOUNT)
    try:
        count = int(count)
    except Exception:
        raise ValueError("数量要填数字")
    if count <= 0 or count > MAX_COUNT:
        raise ValueError("单次最多生成 %d 个" % MAX_COUNT)
    init()
    batch = str(batch or "").strip()[:40]
    note = str(note or "").strip()[:120]
    operator = str(operator or "").strip()[:64]
    now = int(time.time())
    out = []
    with _LOCK, _conn() as c:
        for _i in range(count):
            code = ""
            rid = 0
            for _t in range(10):
                cand = _mint()
                try:
                    cur = c.execute("INSERT INTO recharge_codes"
                                    "(code,amount,batch,note,status,created_by,created_at)"
                                    " VALUES(?,?,?,?,'unused',?,?)",
                                    (cand, amount, batch, note, operator, now))
                    code = cand
                    rid = int(cur.lastrowid or 0)
                    break
                except sqlite3.IntegrityError:
                    continue
            if not code:
                raise ValueError("生成失败，请重试")
            out.append({"id": rid, "code": code, "code_str": fmt(code), "amount": amount,
                        "batch": batch, "note": note})
    return out


def _unclaim(code):
    """入账失败时把码退回「未使用」，绝不吞掉用户的钱。"""
    try:
        with _LOCK, _conn() as c:
            c.execute("UPDATE recharge_codes SET status='unused', used_by=0, used_by_name='',"
                      " used_at=0, used_ip='' WHERE code=? AND status='used'", (code,))
    except Exception:
        pass


def redeem(code, tid, username="", ip=""):
    """兑换。成功 {ok:True, amount, before, after}；失败 {ok:False, reason, message}。"""
    s = norm(code)
    if not s:
        return {"ok": False, "reason": "format", "message": "请填写充值码"}
    if not s.startswith(_PREFIX) or len(s) != len(_PREFIX) + BODY_LEN:
        return {"ok": False, "reason": "format", "message": "充值码格式不对，请核对后重试"}
    init()
    tid = int(tid or 0)
    if not tid:
        return {"ok": False, "reason": "format", "message": "请先用账号登录再充值"}
    now = int(time.time())
    with _LOCK, _conn() as c:
        cur = c.execute("UPDATE recharge_codes SET status='used', used_by=?, used_by_name=?,"
                        " used_at=?, used_ip=? WHERE code=? AND status='unused'",
                        (tid, str(username or "")[:64], now, str(ip or "")[:64], s))
        if cur.rowcount == 0:
            r = c.execute("SELECT status, used_by_name FROM recharge_codes WHERE code=?",
                          (s,)).fetchone()
            if not r:
                return {"ok": False, "reason": "notfound", "message": "没有这个充值码，请核对后重试"}
            if str(r["status"] or "") == "used":
                who = str(r["used_by_name"] or "").strip()
                return {"ok": False, "reason": "used",
                        "message": "这个充值码已经被用过了" + ("（%s）" % who if who else "")}
            return {"ok": False, "reason": "void", "message": "这个充值码已作废"}
        row = c.execute("SELECT amount FROM recharge_codes WHERE code=?", (s,)).fetchone()
    amount = int((row["amount"] if row else 0) or 0)
    if amount <= 0:
        _unclaim(s)
        return {"ok": False, "reason": "void", "message": "这个充值码面额异常，已自动回收，请联系总台"}
    from app import quota
    try:
        before = int(quota.available(tid))
        res = quota.grant(tid, amount, operator=str(username or ""), reason="充值码充值",
                          memo="充值码 %s" % fmt(s), kind="recharge")
        after = int(res.get("available") if isinstance(res, dict) else quota.available(tid))
    except Exception as e:
        _unclaim(s)
        return {"ok": False, "reason": "quota", "message": "充值失败：%s" % str(e)[:80]}
    with _LOCK, _conn() as c:
        c.execute("UPDATE recharge_codes SET used_before=?, used_after=? WHERE code=?",
                  (before, after, s))
    return {"ok": True, "amount": amount, "before": before, "after": after,
            "message": "充值成功！到账 %d 张，当前可用 %d 张" % (amount, after)}


def listing(status="", batch="", q="", limit=50, offset=0):
    init()
    where, args = " WHERE 1=1", []
    status = str(status or "").strip()
    if status in STATUS_TEXT:
        where += " AND status=?"
        args.append(status)
    batch = str(batch or "").strip()
    if batch:
        where += " AND batch=?"
        args.append(batch)
    q = str(q or "").strip()
    if q:
        kw = "%" + norm(q) + "%"
        where += " AND (code LIKE ? OR batch LIKE ? OR note LIKE ? OR used_by_name LIKE ?)"
        args += [kw, "%" + q + "%", "%" + q + "%", "%" + q + "%"]
    lim = max(1, min(int(limit), 500))
    off = max(0, int(offset))
    with _LOCK, _conn() as c:
        total = int(c.execute("SELECT COUNT(*) n FROM recharge_codes" + where, args).fetchone()["n"])
        rows = c.execute("SELECT * FROM recharge_codes" + where +
                         " ORDER BY id DESC LIMIT ? OFFSET ?", args + [lim, off]).fetchall()
    return {"rows": [_decorate(r) for r in rows], "total": total, "limit": lim, "offset": off,
            "pages": max(1, (total + lim - 1) // lim) if lim else 1}


def listing_by_users(ids, limit=50, offset=0):
    """某支账号（自己 + 名下子代理）用过的充值记录。"""
    init()
    ids = sorted({int(i) for i in (ids or []) if i})
    lim = max(1, min(int(limit), 200))
    off = max(0, int(offset))
    if not ids:
        return {"rows": [], "total": 0, "pages": 1, "limit": lim, "offset": off}
    ph = ",".join("?" * len(ids))
    where = " WHERE used_by IN (%s)" % ph
    with _LOCK, _conn() as c:
        total = int(c.execute("SELECT COUNT(*) n FROM recharge_codes" + where, ids).fetchone()["n"])
        rows = c.execute("SELECT * FROM recharge_codes" + where +
                         " ORDER BY used_at DESC, id DESC LIMIT ? OFFSET ?",
                         ids + [lim, off]).fetchall()
    return {"rows": [_decorate(r) for r in rows], "total": total, "limit": lim, "offset": off,
            "pages": max(1, (total + lim - 1) // lim) if lim else 1}


def summary_by_users(ids):
    init()
    ids = sorted({int(i) for i in (ids or []) if i})
    out = {"count": 0, "amount": 0, "last_at": 0, "last_str": ""}
    if not ids:
        return out
    ph = ",".join("?" * len(ids))
    with _LOCK, _conn() as c:
        r = c.execute("SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt, COALESCE(MAX(used_at),0) lt"
                      " FROM recharge_codes WHERE used_by IN (%s) AND status='used'" % ph,
                      ids).fetchone()
    out["count"] = int(r["n"] or 0)
    out["amount"] = int(r["amt"] or 0)
    out["last_at"] = int(r["lt"] or 0)
    if out["last_at"]:
        out["last_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(out["last_at"]))
    return out


def stats():
    init()
    out = {"total": 0, "unused": 0, "used": 0, "void": 0,
           "amount_total": 0, "amount_unused": 0, "amount_used": 0}
    with _LOCK, _conn() as c:
        for r in c.execute("SELECT status, COUNT(*) n, COALESCE(SUM(amount),0) amt"
                           " FROM recharge_codes GROUP BY status"):
            st = str(r["status"] or "")
            n, amt = int(r["n"] or 0), int(r["amt"] or 0)
            if st in ("unused", "used", "void"):
                out[st] += n
            out["total"] += n
            out["amount_total"] += amt
            if st == "unused":
                out["amount_unused"] += amt
            elif st == "used":
                out["amount_used"] += amt
    return out


def batches(limit=60):
    init()
    with _LOCK, _conn() as c:
        rows = c.execute("SELECT batch, COUNT(*) n FROM recharge_codes WHERE batch <> ''"
                         " GROUP BY batch ORDER BY MAX(id) DESC LIMIT ?", (int(limit),)).fetchall()
    return [{"batch": str(r["batch"]), "n": int(r["n"] or 0)} for r in rows]


def void(ids, operator=""):
    """作废未使用的充值码，返回作废条数。已使用 / 已作废的不动。"""
    init()
    clean = []
    for i in (ids or []):
        try:
            clean.append(int(i))
        except Exception:
            continue
    if not clean:
        return 0
    ph = ",".join("?" * len(clean))
    with _LOCK, _conn() as c:
        cur = c.execute("UPDATE recharge_codes SET status='void'"
                        " WHERE id IN (%s) AND status='unused'" % ph, clean)
        return int(cur.rowcount or 0)