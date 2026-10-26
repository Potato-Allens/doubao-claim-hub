# -*- coding: utf-8 -*-
"""额度（卡密张数）事务层 —— 全部走 SQL 条件 UPDATE，防并发超发。

口径：剩余可用 = quota_total - quota_given - quota_used
失败一律抛 QuotaError(中文人话)。
"""
from __future__ import annotations

import time

from . import store

_LOCK = store._lock

KIND_TEXT = {"grant": "总台发放", "recycle": "总台回收", "allocate": "划拨",
             "spend": "生成卡密", "refund": "生成回滚",
             "recharge": "充值码充值"}


class QuotaError(Exception):
    """额度异常（消息为中文人话）。"""


def _conn():
    return store._conn()


def _names(c, ids):
    ids = sorted({int(i) for i in ids if i})
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    try:
        rows = c.execute("SELECT id,username FROM tenants WHERE id IN (%s)" % ph, ids).fetchall()
        return {int(r["id"]): r["username"] for r in rows}
    except Exception:
        return {}


def _ledger_insert(c, from_id, to_id, delta, kind, reason, memo, operator, operator_level, balance_after):
    c.execute("INSERT INTO quota_ledger(ts,from_id,to_id,delta,kind,reason,memo,operator,"
              "operator_level,balance_after) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (int(time.time()), int(from_id or 0), int(to_id or 0), int(delta), kind,
               str(reason or "")[:120], str(memo or "")[:200], str(operator or "")[:64],
               int(operator_level or 0), int(balance_after)))


def _avail_row(r) -> int:
    return max(0, int(r["quota_total"] or 0) - int(r["quota_given"] or 0) - int(r["quota_used"] or 0))


def _fetch(c, tid):
    r = c.execute("SELECT * FROM tenants WHERE id=?", (int(tid),)).fetchone()
    if not r:
        raise QuotaError("账号不存在")
    return r


def available(tid) -> int:
    with _LOCK, _conn() as c:
        return _avail_row(_fetch(c, tid))


def grant(tid, n, operator="", reason="总台调整额度", memo="", kind=""):
    """总台增/减某代理商额度；n 为负表示回收。"""
    try:
        n = int(n)
    except Exception:
        raise QuotaError("调整数量不正确")
    if n == 0:
        raise QuotaError("调整数量不能为 0")
    tid = int(tid)
    with _LOCK, _conn() as c:
        if n > 0:
            cur = c.execute("UPDATE tenants SET quota_total=quota_total+? WHERE id=?", (n, tid))
        else:
            cur = c.execute("UPDATE tenants SET quota_total=quota_total+? "
                            "WHERE id=? AND (quota_total+? - quota_given - quota_used)>=0",
                            (n, tid, n))
        if cur.rowcount == 0:
            if not c.execute("SELECT id FROM tenants WHERE id=?", (tid,)).fetchone():
                raise QuotaError("账号不存在")
            raise QuotaError("回收数量超过该账号可用额度，无法回收")
        r = _fetch(c, tid)
        bal = _avail_row(r)
        kind = str(kind or "").strip() or ("grant" if n > 0 else "recycle")
        _ledger_insert(c, 0, tid, n, kind, reason, memo, operator, 0, bal)
        return {"id": tid, "delta": n, "kind": kind, "available": bal,
                "quota_total": int(r["quota_total"] or 0)}


def allocate(from_id, to_id, n, operator="", operator_level=0, reason="划拨额度", memo=""):
    """上级把自己余额划给下级：扣 from.quota_given、加 to.quota_total，写 2 条流水。"""
    try:
        n = int(n)
    except Exception:
        raise QuotaError("划拨数量不正确")
    if n <= 0:
        raise QuotaError("划拨数量必须是正整数")
    from_id, to_id = int(from_id), int(to_id)
    if from_id == to_id:
        raise QuotaError("不能给自己划拨额度")
    with _LOCK, _conn() as c:
        if not c.execute("SELECT id FROM tenants WHERE id=?", (to_id,)).fetchone():
            raise QuotaError("接收账号不存在")
        cur = c.execute("UPDATE tenants SET quota_given=quota_given+? "
                        "WHERE id=? AND (quota_total-quota_given-quota_used)>=?",
                        (n, from_id, n))
        if cur.rowcount == 0:
            if not c.execute("SELECT id FROM tenants WHERE id=?", (from_id,)).fetchone():
                raise QuotaError("划出账号不存在")
            raise QuotaError("可用额度不足，无法划拨")
        cur2 = c.execute("UPDATE tenants SET quota_total=quota_total+? WHERE id=?", (n, to_id))
        if cur2.rowcount == 0:
            raise QuotaError("接收账号不存在")
        rf = _fetch(c, from_id)
        rt = _fetch(c, to_id)
        nm = _names(c, [from_id, to_id])
        fn = nm.get(from_id, "账号%s" % from_id)
        tn = nm.get(to_id, "账号%s" % to_id)
        bal_f = _avail_row(rf)
        _ledger_insert(c, from_id, to_id, -n, "allocate", "划拨给 " + tn, memo, operator, operator_level, bal_f)
        bal_t = _avail_row(rt)
        _ledger_insert(c, from_id, to_id, n, "allocate", "来自 " + fn, memo, operator, operator_level, bal_t)
        return {"from": {"id": from_id, "available": bal_f},
                "to": {"id": to_id, "available": bal_t}, "delta": n}


def spend(tid, n, operator="", reason="生成卡密", memo=""):
    """自建卡密扣额度。"""
    try:
        n = int(n)
    except Exception:
        raise QuotaError("消耗数量不正确")
    if n <= 0:
        raise QuotaError("消耗数量必须是正整数")
    tid = int(tid)
    with _LOCK, _conn() as c:
        cur = c.execute("UPDATE tenants SET quota_used=quota_used+? "
                        "WHERE id=? AND (quota_total-quota_given-quota_used)>=?", (n, tid, n))
        if cur.rowcount == 0:
            if not c.execute("SELECT id FROM tenants WHERE id=?", (tid,)).fetchone():
                raise QuotaError("账号不存在")
            raise QuotaError("可用额度不足，无法生成卡密")
        r = _fetch(c, tid)
        bal = _avail_row(r)
        _ledger_insert(c, tid, 0, -n, "spend", reason, memo, operator, 0, bal)
        return {"id": tid, "delta": n, "available": bal}


def refund(tid, n, reason="生成失败回滚", memo="", operator=""):
    """建码失败回滚额度。"""
    try:
        n = int(n)
    except Exception:
        raise QuotaError("回滚数量不正确")
    if n <= 0:
        raise QuotaError("回滚数量必须是正整数")
    tid = int(tid)
    with _LOCK, _conn() as c:
        cur = c.execute("UPDATE tenants SET quota_used=quota_used-? "
                        "WHERE id=? AND quota_used>=?", (n, tid, n))
        if cur.rowcount == 0:
            if not c.execute("SELECT id FROM tenants WHERE id=?", (tid,)).fetchone():
                raise QuotaError("账号不存在")
            raise QuotaError("回滚数量超过已消耗额度")
        r = _fetch(c, tid)
        bal = _avail_row(r)
        _ledger_insert(c, 0, tid, n, "refund", reason, memo, operator, 0, bal)
        return {"id": tid, "delta": n, "available": bal}


def summary(tid) -> dict:
    with _LOCK, _conn() as c:
        r = _fetch(c, tid)
        return {"total": int(r["quota_total"] or 0), "given": int(r["quota_given"] or 0),
                "used": int(r["quota_used"] or 0), "available": _avail_row(r)}


def ledger(tid=None, scope_ids=None, limit=50, offset=0, kind=""):
    """流水。scope_ids=可见租户 id 列表（None=全站）；tid=只看与某账号相关的流水。"""
    where, args = " WHERE 1=1", []
    if scope_ids is not None:
        ids = sorted({int(i) for i in scope_ids if i})
        if ids:
            ph = ",".join("?" * len(ids))
            where += " AND (from_id IN (%s) OR to_id IN (%s))" % (ph, ph)
            args += ids + ids
        else:
            where += " AND 1=0"
    if tid is not None:
        where += " AND (from_id=? OR to_id=?)"
        args += [int(tid), int(tid)]
    if kind:
        where += " AND kind=?"
        args.append(str(kind))
    lim = max(1, min(int(limit), 500))
    off = max(0, int(offset))
    with _LOCK, _conn() as c:
        total = int(c.execute("SELECT COUNT(*) n FROM quota_ledger" + where, args).fetchone()["n"])
        rows = c.execute("SELECT * FROM quota_ledger" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                         args + [lim, off]).fetchall()
        nm = _names(c, [r["from_id"] for r in rows] + [r["to_id"] for r in rows])
    out = []
    for r in rows:
        d = dict(r)
        fid = int(d.get("from_id") or 0)
        tid2 = int(d.get("to_id") or 0)
        d["from_name"] = nm.get(fid, "总台" if fid == 0 else "账号%s" % fid)
        d["to_name"] = nm.get(tid2, "总台" if tid2 == 0 else "账号%s" % tid2)
        d["kind_text"] = KIND_TEXT.get(d.get("kind"), d.get("kind") or "")
        t = int(d.get("ts") or 0)
        d["time_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""
        out.append(d)
    pages = (total + lim - 1) // lim if lim else 0
    return {"rows": out, "total": total, "pages": pages, "limit": lim, "offset": off}


def kind_income(scope_ids, kind="recharge") -> int:
    """某类流水的入账合计（给仪表盘「累计充值」用）。"""
    ids = sorted({int(i) for i in (scope_ids or []) if i})
    if not ids:
        return 0
    ph = ",".join("?" * len(ids))
    with _LOCK, _conn() as c:
        r = c.execute("SELECT COALESCE(SUM(delta),0) s FROM quota_ledger "
                      "WHERE kind=? AND to_id IN (%s)" % ph,
                      [str(kind)] + ids).fetchone()
    return int(r["s"] or 0)


def stats_total() -> dict:
    """全站额度汇总（给总台仪表盘用）。"""
    with _LOCK, _conn() as c:
        r = c.execute("SELECT COALESCE(SUM(quota_total),0) t, COALESCE(SUM(quota_given),0) g, "
                      "COALESCE(SUM(quota_used),0) u FROM tenants").fetchone()
    total, given, used = int(r["t"] or 0), int(r["g"] or 0), int(r["u"] or 0)
    return {"given": given, "used": used, "left": max(0, total - given - used), "total": total}
