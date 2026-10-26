# -*- coding: utf-8 -*-
"""R63 接口层：多租户 HTTP API（总台 /admin/api/* + 代理商 /a/*）。

只注册在 router 上，由主控在 main.py 里 app.include_router(tenant_api.router)。
数据层全部调用线A/线D 的模块，本文件不重复实现（唯一例外见 _reclaim 注释）：

  app.tenants : init/create/get/get_by_name/authenticate/set_status/set_password/
                set_max_sub_agents/set_remark/touch_login/children/child_ids/
                listing/stats/sub_agents_used
  app.quota   : available/grant/allocate/spend/refund/summary/ledger/stats_total + QuotaError
  app.audit   : log/query/actions/levels
  app.store   : generate/listing/events/events_count/events_stats/get_setting
  app.proxy   : get_config/set_config/stats/test（可选，缺失时相关端点返回 503）
  app.authz   : cookie / 登录 / 权限助手 / 限速
"""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import audit, authz, guard, otp, quota, recharge, store, tenants
from app.sessions import SM
from app.quota import QuotaError

router = APIRouter()

ADMIN_NAME = "总台管理员"
MAX_GEN = int(os.environ.get("TENANT_MAX_GEN", "2000"))       # 代理商单次最多建多少张
GEN_RATE = int(os.environ.get("TENANT_GEN_RATE", "30"))       # 每分钟 / 每账号 建码次数
RECYCLE_RATE = int(os.environ.get("TENANT_RECYCLE_RATE", "30"))   # ★R86 一键回收：每 N 秒 / 每账号
RECYCLE_WIN = int(os.environ.get("TENANT_RECYCLE_WIN", "600"))
PROXY_KEYS = ("enabled", "mode", "host", "port", "username_template", "password",
              "list_text", "sid_digits", "sticky_minutes", "protocol", "region")
KINDS = ("enter", "scan", "login", "success", "fail")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _j(obj, code: int = 200):
    if isinstance(obj, dict):
        obj.setdefault("ok", bool(code < 400))
    return JSONResponse(obj, status_code=code)


def _err(msg: str, code: int = 400):
    return JSONResponse({"ok": False, "message": msg}, status_code=code)


def _unauth():
    return _err("未登录或登录已过期，请重新登录", 401)


def _readonly():
    return _err("账号已被停用，仅可查看", 403)


def _ip(request: Request) -> str:
    return authz.client_ip(request)


def _ua(request: Request) -> str:
    try:
        return (request.headers.get("user-agent") or "")[:200]
    except Exception:
        return ""


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


def _qs(request: Request, k: str, d: str = "") -> str:
    try:
        return str(request.query_params.get(k) or d).strip()
    except Exception:
        return d


def _qi(request: Request, k: str, d: int = 0, lo: int = 0, hi: int = 10 ** 9) -> int:
    try:
        v = int(str(request.query_params.get(k) or d))
    except Exception:
        v = d
    return max(lo, min(v, hi))


def _clean(t):
    """去掉密码哈希与明文密码，绝不外泄。"""
    if not t:
        return None
    d = dict(t)
    d.pop("pwd_hash", None)
    d.pop("pwd_plain", None)
    return d


def _clean_rows(rows):
    return [_clean(r) for r in (rows or [])]


def _default_claims() -> int:
    """★R209：固定 1 次。"""
    return 1


def _code_stats(owner_ids=None, status: str = "") -> dict:
    """按归属统计卡密（只读；store 没有 owner 维度的汇总接口）。

    owner_ids=None -> 全站；[] -> 空集（什么也看不到）。
    """
    out = {"total": 0, "total_all": 0, "claims": 0, "by_status": {}}
    try:
        with store._lock:
            c = store._conn()
            try:
                q = ("SELECT status, COUNT(*) n, COALESCE(SUM(claims),0) cl "
                     "FROM codes WHERE 1=1")
                args = []
                if owner_ids is not None:
                    ids = [int(x) for x in owner_ids]
                    if ids:
                        q += " AND owner_id IN (%s)" % ",".join("?" * len(ids))
                        args += ids
                    else:
                        q += " AND 1=0"
                q += " GROUP BY status"
                by = {}
                total = 0
                claims = 0
                for r in c.execute(q, args).fetchall():
                    n = int(r["n"] or 0)
                    cl = int(r["cl"] or 0)
                    by[str(r["status"] or "")] = n
                    total += n
                    claims += cl
            finally:
                try:
                    c.close()
                except Exception:
                    pass
    except Exception:
        return out
    out["by_status"] = by
    out["total_all"] = total
    out["claims"] = claims
    out["total"] = int(by.get(status, 0)) if status else total
    return out


def _reclaim(parent, child, n, operator, reason, memo=""):
    """从子代理商收回 n 张额度：child.quota_total -= n 且 parent.quota_given -= n（同一事务 + 2 条流水）。

    ！偏差说明：线A 交付的 app/quota.py 只有 allocate（向下划拨）没有 reclaim（向上回收），
    这里是接口层补的最小事务（同库同锁，条件 UPDATE 防超发）。建议后续把它移进 quota.py。
    """
    n = int(n)
    if n <= 0:
        raise QuotaError("回收数量必须是正整数")
    pid = int(parent.get("id") or 0)
    cid = int(child.get("id") or 0)
    if pid <= 0 or cid <= 0 or pid == cid:
        raise QuotaError("回收对象不正确")
    with store._lock:
        c = store._conn()
        try:
            cur = c.execute("UPDATE tenants SET quota_total=quota_total-? "
                            "WHERE id=? AND (quota_total-quota_given-quota_used)>=?",
                            (n, cid, n))
            if cur.rowcount == 0:
                raise QuotaError("对方可用额度不足，最多能回收 %d 张" % max(0, quota.available(cid)))
            cur2 = c.execute("UPDATE tenants SET quota_given=quota_given-? "
                             "WHERE id=? AND quota_given>=?", (n, pid, n))
            if cur2.rowcount == 0:
                raise QuotaError("回收失败：该张数已不在可回收范围")
            now = int(time.time())
            pn = str(parent.get("username") or pid)
            cn = str(child.get("username") or cid)
            rows = (
                (cid, pid, -n, reason or ("被上级 %s 回收" % pn)),
                (pid, cid, n, "回收自 %s" % cn),
            )
            for f, t, delta, rsn in rows:
                c.execute("INSERT INTO quota_ledger(ts,from_id,to_id,delta,kind,reason,memo,"
                          "operator,operator_level,balance_after) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (now, f, t, delta, "recycle", str(rsn)[:120], str(memo or "")[:200],
                           str(operator or "")[:64], 1, 0))
            c.commit()
        except Exception:
            try:
                c.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                c.close()
            except Exception:
                pass
    return {"delta": -n, "child_available": quota.available(cid), "parent_available": quota.available(pid)}


def _recycle(parent, child, operator, reason="", memo="", include_visiting=False):
    """★R86 一键回收子代理：未用完的卡密 + 全部剩余可用额度，一次性收回到上级（同一事务）。

    口径（全部条件 UPDATE，防并发超发）：
      1) 把该子代理名下 status=unused（可选带 visiting）的卡密整批置为 disabled -> 链接立刻失效；
      2) 这些码当初是从子代理额度里扣掉的，按实际改动条数 k 把 child.quota_used 回滚 k；
      3) 回滚之后 child 的「剩余可用额度」整份划回上级：child.quota_total -= n、parent.quota_given -= n
         => 子代理可用额度归 0，上级可用额度 += n（n = 剩余额度 + 本次回收的卡密张数）。

    返回：{"codes": 停用张数, "quota": 纯剩余额度, "moved": 回到上级的总张数, 双方当前可用}
    """
    pid = int(parent.get("id") or 0)
    cid = int(child.get("id") or 0)
    if pid <= 0 or cid <= 0 or pid == cid:
        raise QuotaError("回收对象不正确")
    states = ["unused"] + (["visiting"] if include_visiting else [])
    with store._lock:
        c = store._conn()
        try:
            row = c.execute("SELECT * FROM tenants WHERE id=?", (cid,)).fetchone()
            if not row:
                raise QuotaError("子代理账号不存在")
            used0 = max(0, int(row["quota_used"] or 0))
            cur = c.execute("UPDATE codes SET status='disabled' WHERE owner_id=? AND status IN (%s)"
                            % ",".join("?" * len(states)), [cid] + states)
            k = max(0, int(cur.rowcount or 0))
            back = min(k, used0)
            if back > 0:
                c.execute("UPDATE tenants SET quota_used=quota_used-? WHERE id=? AND quota_used>=?",
                          (back, cid, back))
            n = max(0, int(row["quota_total"] or 0) - int(row["quota_given"] or 0) - (used0 - back))
            if n > 0:
                if c.execute("UPDATE tenants SET quota_total=quota_total-? "
                             "WHERE id=? AND (quota_total-quota_given-quota_used)>=?",
                             (n, cid, n)).rowcount == 0:
                    raise QuotaError("子代理额度刚刚有变动，请刷新后重试")
                if c.execute("UPDATE tenants SET quota_given=quota_given-? "
                             "WHERE id=? AND quota_given>=?", (n, pid, n)).rowcount == 0:
                    raise QuotaError("上级额度账不平，已回滚")
                now = int(time.time())
                pn = str(parent.get("username") or pid)
                cn = str(child.get("username") or cid)
                detail = "剩余额度 %d 张 + 未用卡密 %d 张" % (n - back, back)
                for f, t2, delta, rsn in ((cid, pid, -n, "被上级 %s 回收（%s）" % (pn, detail)),
                                          (pid, cid, n, "回收自 %s（%s）" % (cn, detail))):
                    c.execute("INSERT INTO quota_ledger(ts,from_id,to_id,delta,kind,reason,memo,"
                              "operator,operator_level,balance_after) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (now, f, t2, delta, "recycle", str(rsn)[:120], str(memo or "")[:200],
                               str(operator or "")[:64], 1, 0))
            c.commit()
            r2 = c.execute("SELECT * FROM tenants WHERE id=?", (cid,)).fetchone()
            r3 = c.execute("SELECT * FROM tenants WHERE id=?", (pid,)).fetchone()
        except Exception:
            try:
                c.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                c.close()
            except Exception:
                pass
    def _av(r):
        if not r:
            return 0
        return max(0, int(r["quota_total"] or 0) - int(r["quota_given"] or 0) - int(r["quota_used"] or 0))
    return {"codes": k, "quota": max(0, n - back), "moved": n,
            "child_available": _av(r2), "parent_available": _av(r3)}



def _proxy_mod():
    try:
        from app import proxy
        return proxy
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 依赖注入（主控 main.py 调用；转发给 authz，主控按契约就是调 tenant_api.set_deps）
# --------------------------------------------------------------------------- #
def set_deps(admin_ok=None, client_ip=None, base_url=None):
    """注入 总台校验回调 / 真实 IP / 站点前缀。"""
    authz.set_deps(admin_ok=admin_ok, client_ip=client_ip, base_url=base_url)


def set_secret(s):
    """注入 cookie 签名密钥（等价 authz.set_secret）。"""
    authz.set_secret(s)


# --------------------------------------------------------------------------- #
# 总台仪表盘 / 代理商管理 / 额度 / 审计 / 代理池
# --------------------------------------------------------------------------- #
@router.get("/admin/api/dashboard")
def admin_dashboard(request: Request):
    if not authz.is_admin(request):
        return _unauth()
    ts = tenants.stats()
    cs = store.stats()
    es = store.events_stats()
    qs = quota.stats_total()
    led = quota.ledger(limit=10).get("rows", [])
    try:
        l1 = tenants.listing(level=1, limit=500).get("rows", [])
    except Exception:
        l1 = []
    tops = []
    for r in sorted(l1, key=lambda x: int(x.get("quota_total") or 0), reverse=True)[:5]:
        tops.append({"id": r.get("id"), "username": r.get("username"), "status": r.get("status"),
                     "quota_total": int(r.get("quota_total") or 0),
                     "quota_used": int(r.get("quota_used") or 0),
                     "quota_given": int(r.get("quota_given") or 0),
                     "available": int(r.get("available") or 0)})
    try:
        _all_ids = [int(r.get("id") or 0) for r in (tenants.listing(limit=500).get("rows") or [])]
        rc_in = int(quota.kind_income(_all_ids, "recharge")) if _all_ids else 0
    except Exception:
        rc_in = 0
    return _j({
        "ok": True,
        "tenants": ts,
        "codes": {"total": int(cs.get("total") or 0), "claims": int(cs.get("claims") or 0),
                  "unused": int(cs.get("unused") or 0), "visiting": int(cs.get("visiting") or 0),
                  "done": int(cs.get("done") or 0)},
        "events": es,
        "quota": {"given": int(qs.get("given") or 0), "used": int(qs.get("used") or 0),
                  "left": int(qs.get("left") or 0), "total": int(qs.get("total") or 0)},
        "recent": led,
        "tops": tops,
        "recharge": {"in": rc_in},
    })


@router.get("/admin/api/tenants")
def admin_tenants(request: Request):
    if not authz.is_admin(request):
        return _unauth()
    lim = _qi(request, "limit", 50, 1, 500)
    off = _qi(request, "offset", 0, 0, 10 ** 7)
    q = _qs(request, "q")[:64]
    lv_raw = _qs(request, "level")
    pid_raw = _qs(request, "parent_id")
    lv = int(lv_raw) if lv_raw.lstrip("-").isdigit() else None
    pid = int(pid_raw) if pid_raw.lstrip("-").isdigit() else None
    res = tenants.listing(parent_id=pid, level=lv, q=q, limit=lim, offset=off)
    return _j({"ok": True, "rows": _clean_rows(res.get("rows")), "total": int(res.get("total") or 0),
               "pages": int(res.get("pages") or 1), "limit": lim, "offset": off,
               "stats": tenants.stats()})


@router.post("/admin/api/tenants")
async def admin_create_tenant(request: Request):
    if not authz.is_admin(request):
        return _unauth()
    b = await _body(request)
    try:
        username = str(b.get("username") or "").strip()
        password = str(b.get("password") or "")
        parent_id = int(b.get("parent_id") or 0)
        level = int(b.get("level") or (2 if parent_id > 0 else 1))
        max_sub = int(b.get("max_sub_agents") or 0)
        quota_n = int(b.get("quota") or 0)
    except Exception:
        return _err("参数格式不对")
    remark = str(b.get("remark") or "")[:200]
    why = _pwd_why(password)      # ★R211：建号也必须满足 8 位强度
    if why:
        return _err(why)
    if level not in (1, 2):
        return _err("账号级别只能是 1（代理商）或 2（子代理商）")
    if level == 2:
        if parent_id <= 0:
            return _err("二级账号必须指定上级账号")
        parent = tenants.get(parent_id)
        if not parent:
            return _err("上级账号不存在", 404)
        if int(parent.get("level") or 0) >= 2:
            return _err("子代理商不能再开下级")
    try:
        t = tenants.create(username, password, level=level,
                           parent_id=(parent_id if level == 2 else 0),
                           max_sub_agents=(max_sub if level == 1 else 0),
                           remark=remark, created_by=ADMIN_NAME)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err("创建失败：%s" % str(e)[:120], 500)
    # R65 记住明文密码，总台随时可查/转发
    try:
        from app import sites as _sites
        _sites.remember_pwd(t["id"], password)
    except Exception:
        pass
    if quota_n:
        try:
            quota.grant(t["id"], quota_n, operator=ADMIN_NAME, reason="建号发放额度")
        except QuotaError as e:
            audit.log("create_tenant", actor_name=ADMIN_NAME, target=username,
                      detail="账号已建但发额度失败：%s" % e, ip=_ip(request), ua=_ua(request), level="warn")
            return _j({"ok": False, "tenant": _clean(tenants.get(t["id"])),
                       "message": "账号已创建，但发放额度失败：%s" % e}, 400)
    t2 = tenants.get(t["id"])
    audit.log("create_tenant", actor_name=ADMIN_NAME, target=username,
              detail="新建%s（id=%s），额度 %d 张" % (t2.get("level_text"), t2.get("id"), quota_n),
              ip=_ip(request), ua=_ua(request))
    return _j({"ok": True, "tenant": _clean(t2),
               "message": "已创建%s「%s」%s" % (t2.get("level_text"), username,
                                             ("，发放 %d 张额度" % quota_n) if quota_n else "")})


BATCH_MAX = int(os.environ.get("TENANT_BATCH_MAX", "100"))   # ★R634 单次批量建号上限


@router.post("/admin/api/tenants/batch")
async def admin_batch_create_tenants(request: Request):
    """★R634 总台专用：批量创建一级代理商（账号自动编号）。

    权限：只有总台（level=0）能调；代理商 / 子代理商调一律 401。
    逐条独立 try/except：一个失败不影响其余；返回明文密码 + 专属网址，
    前端可一键复制「账号 / 密码 / 链接」，每行一条。
    """
    if not authz.is_admin(request):
        return _unauth()
    b = await _body(request)
    try:
        count = int(b.get("count") or 0)
        quota_n = int(b.get("quota") or 0)
        max_sub = int(b.get("max_sub_agents") or 0)
    except Exception:
        return _err("参数格式不对")
    if count < 1 or count > BATCH_MAX:
        return _err("一次最多批量创建 %d 个，最少 1 个" % BATCH_MAX)
    if quota_n < 0:
        return _err("额度不能是负数")
    if max_sub < 0:
        return _err("可开下级名额不能是负数")
    prefix = str(b.get("prefix") or "").strip() or "agent"
    if len(prefix) > 24 or any(ch.isspace() for ch in prefix):
        return _err("账号前缀最多 24 个字符，且不能有空格")
    password = str(b.get("password") or "")
    if password:
        why = _pwd_why(password)
        if why:
            return _err(why)
    remark = str(b.get("remark") or "")[:200]

    from app import sites as _sites

    rows, failed = [], []
    seq, tries = 0, 0
    while (len(rows) + len(failed)) < count and tries < count * 20 + 50:
        tries += 1
        seq += 1
        username = "%s%03d" % (prefix, seq)
        pwd = password or _sites.gen_pwd(12)
        try:
            t = tenants.create(username, pwd, level=1, parent_id=0,
                               max_sub_agents=max_sub, remark=remark,
                               created_by=ADMIN_NAME)
        except ValueError as e:
            msg = str(e)
            if "已存在" in msg:
                continue          # 重名：自动顺延下一个编号，不算失败
            failed.append({"username": username, "error": msg})
            continue
        except Exception as e:
            failed.append({"username": username, "error": str(e)[:120]})
            continue
        tid = int(t["id"])
        note = ""
        try:
            _sites.remember_pwd(tid, pwd)
        except Exception:
            pass
        if quota_n:
            try:
                quota.grant(tid, quota_n, operator=ADMIN_NAME, reason="批量建号发放额度")
            except Exception as e:
                note = "账号已建但发额度失败：%s" % str(e)[:100]
        url = ""
        try:
            url = _sites._entry_url(request, tenants.get(tid) or t)
        except Exception:
            url = ""
        rows.append({"id": tid, "username": username, "password": pwd,
                     "quota": quota_n, "url": url,
                     "warn": note})
        try:
            audit.log("create_tenant", actor_name=ADMIN_NAME, target=username,
                      detail="批量新建代理商（id=%s），额度 %d 张" % (tid, quota_n),
                      ip=_ip(request), ua=_ua(request))
        except Exception:
            pass
    if not rows:
        return _err("一个都没建成功：%s" % ((failed[0]["error"] if failed else "未知原因")), 500)
    return _j({"ok": True, "rows": rows, "failed": failed,
               "created": len(rows),
               "message": "已创建 %d 个代理商%s" % (
                   len(rows), ("，%d 个失败" % len(failed)) if failed else "")})


DELETE_MAX = int(os.environ.get("TENANT_DELETE_MAX", "200"))   # ★R635 单次批量删除上限


def _del_backup_dir() -> str:
    try:
        base = os.path.dirname(os.path.abspath(store.DB_PATH or "")) if getattr(store, "DB_PATH", "") else ""
    except Exception:
        base = ""
    if not base:
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    d = os.path.join(base, "deletions")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


@router.post("/admin/api/tenants/batch_delete")
async def admin_batch_delete_tenants(request: Request):
    """★R635 总台专用：批量删除代理商，连同名下全部数据一起删。

    级联范围：tenants（含一级名下的二级子代理）、codes、events、pending_verify、
    downloads（行 + data/dl 下文件）、quota_ledger、settings(tn_epoch)。
    删除前把待删数据落一份 JSON 备份到 data/deletions/；审计留 delete_tenant。
    """
    if not authz.is_admin(request):
        return _unauth()
    import json as _json
    from datetime import datetime as _dt
    b = await _body(request)
    raw = b.get("ids")
    if not isinstance(raw, list):
        return _err("参数格式不对")
    ids = []
    for x in raw:
        try:
            i = int(x)
        except Exception:
            continue
        if i > 0 and i not in ids:
            ids.append(i)
    if not ids:
        return _err("请先勾选要删除的账号")
    if len(ids) > DELETE_MAX:
        return _err("一次最多删除 %d 个" % DELETE_MAX)
    if not b.get("confirm"):
        return _err("请先确认再删除")

    targets = []
    for i in ids:
        t = tenants.get(i)
        if not t:
            return _err("账号 #%d 不存在（可能已被删掉），请刷新后再试" % i, 404)
        if int(t.get("level") or 0) not in (1, 2):
            return _err("只能删除代理商 / 子代理商账号")
        targets.append(t)
    all_ids = set(ids)
    for t in targets:
        if int(t.get("level") or 0) == 1:
            try:
                all_ids.update(int(x) for x in tenants.child_ids(int(t["id"])))
            except Exception:
                pass
    all_ids = sorted(all_ids)
    ph = ",".join("?" * len(all_ids))
    names = {}
    for i in all_ids:
        t = tenants.get(i)
        if t:
            names[i] = str(t.get("username") or "")

    with store._lock, store._conn() as c:
        trows = [dict(r) for r in c.execute("SELECT * FROM tenants WHERE id IN (%s)" % ph, all_ids)]
        crows = [dict(r) for r in c.execute("SELECT * FROM codes WHERE owner_id IN (%s)" % ph, all_ids)]
        drows = [dict(r) for r in c.execute("SELECT token,path,owner_id FROM downloads WHERE owner_id IN (%s)" % ph, all_ids)]
        lrows = [dict(r) for r in c.execute("SELECT * FROM quota_ledger WHERE to_id IN (%s) OR from_id IN (%s)" % (ph, ph), all_ids + all_ids)]
        counts = {}
        counts["events"] = c.execute(
            "DELETE FROM events WHERE owner_id IN (%s) OR code IN (SELECT code FROM codes WHERE owner_id IN (%s))"
            % (ph, ph), all_ids + all_ids).rowcount
        counts["pending_verify"] = c.execute("DELETE FROM pending_verify WHERE owner_id IN (%s)" % ph, all_ids).rowcount
        counts["downloads"] = c.execute("DELETE FROM downloads WHERE owner_id IN (%s)" % ph, all_ids).rowcount
        counts["codes"] = c.execute("DELETE FROM codes WHERE owner_id IN (%s)" % ph, all_ids).rowcount
        counts["quota_ledger"] = c.execute(
            "DELETE FROM quota_ledger WHERE to_id IN (%s) OR from_id IN (%s)" % (ph, ph), all_ids + all_ids).rowcount
        counts["settings"] = c.execute(
            "DELETE FROM settings WHERE k IN (%s)" % ",".join("?" * len(all_ids)),
            ["tn_epoch_%d" % i for i in all_ids]).rowcount
        counts["tenants"] = c.execute("DELETE FROM tenants WHERE id IN (%s)" % ph, all_ids).rowcount

    # 杀掉这些卡密还在跑的会话，避免删完还继续写记录
    try:
        for _r in crows:
            _cd = str(_r.get("code") or "")
            if _cd:
                SM.kill(_cd)
    except Exception:
        pass

    backup = ""
    try:
        d = _del_backup_dir()
        backup = os.path.join(d, "del_%s.json" % _dt.now().strftime("%Y%m%d_%H%M%S"))
        with open(backup, "w", encoding="utf-8") as f:
            _json.dump({"ts": _dt.now().isoformat(timespec="seconds"), "ids": all_ids,
                        "counts": counts, "tenants": trows, "codes": crows,
                        "downloads": drows, "quota_ledger": lrows},
                       f, ensure_ascii=False)
    except Exception:
        backup = ""

    # 删上传文件（只动 data 目录下的路径，防止误删）
    drop_files = 0
    try:
        base = os.path.dirname(os.path.abspath(store.DB_PATH))
        for r in drows:
            fp = str(r.get("path") or "")
            if fp and os.path.abspath(fp).startswith(base + os.sep) and os.path.isfile(fp):
                try:
                    os.remove(fp); drop_files += 1
                except Exception:
                    pass
    except Exception:
        pass
    counts["files"] = drop_files

    shown = [names.get(i) or ("#%d" % i) for i in all_ids]
    audit.log("delete_tenant", actor_name=ADMIN_NAME, target=",".join(shown)[:200],
              detail="批量删除 %d 个账号及全部数据：%s" % (len(all_ids), _json.dumps(counts, ensure_ascii=False)),
              ip=_ip(request), ua=_ua(request), level="danger")
    return _j({"ok": True, "deleted": len(all_ids), "names": shown, "counts": counts,
               "backup": os.path.basename(backup) if backup else "",
               "message": "已删除 %d 个账号（含 %d 张卡密）" % (len(all_ids), counts.get("codes", 0))})


@router.post("/admin/api/tenants/{tid}/quota")
async def admin_tenant_quota(request: Request, tid: int):
    if not authz.is_admin(request):
        return _unauth()
    t = tenants.get(tid)
    if not t:
        return _err("账号不存在", 404)
    if int(t.get("level") or 0) <= 0:
        return _err("总台账号不需要额度")
    b = await _body(request)
    try:
        delta = int(b.get("delta") or 0)
    except Exception:
        return _err("调整数量不正确")
    if delta == 0:
        return _err("调整数量不能为 0")
    reason = str(b.get("reason") or "").strip()[:120]
    memo = str(b.get("memo") or "")[:200]
    try:
        quota.grant(tid, delta, operator=ADMIN_NAME,
                    reason=reason or ("总台发放额度" if delta > 0 else "总台回收额度"), memo=memo)
    except QuotaError as e:
        return _err(str(e))
    t2 = tenants.get(tid)
    act = "quota_grant" if delta > 0 else "quota_recycle"
    audit.log(act, actor_name=ADMIN_NAME, target=str(t2.get("username") or tid),
              detail="%s %d 张，该账号当前可用 %d 张" % ("发放" if delta > 0 else "回收",
                                                       abs(delta), int(t2.get("available") or 0)),
              ip=_ip(request), ua=_ua(request))
    led = quota.ledger(tid=tid, limit=1).get("rows", [])
    return _j({"ok": True, "tenant": _clean(t2), "ledger": (led[0] if led else None),
               "available": int(t2.get("available") or 0),
               "message": "已%s %d 张，当前可用 %d 张" % ("发放" if delta > 0 else "回收",
                                                        abs(delta), int(t2.get("available") or 0))})


@router.post("/admin/api/tenants/{tid}/status")
async def admin_tenant_status(request: Request, tid: int):
    if not authz.is_admin(request):
        return _unauth()
    t = tenants.get(tid)
    if not t:
        return _err("账号不存在", 404)
    if int(t.get("level") or 0) <= 0:
        return _err("总台账号不能停用")
    b = await _body(request)
    status = str(b.get("status") or "").strip()
    if status not in tenants.STATUSES:
        return _err("状态值不正确")
    try:
        tenants.set_status(tid, status)
    except ValueError as e:
        return _err(str(e))
    if status != "active":
        authz.bump_epoch(tid, "账号状态改为%s" % status)     # 停用/封禁 -> 立刻踢下线
    t2 = tenants.get(tid)
    audit.log("set_status", actor_name=ADMIN_NAME, target=str(t2.get("username") or tid),
              detail="状态改为%s" % t2.get("status_text"), ip=_ip(request), ua=_ua(request), level="warn")
    return _j({"ok": True, "tenant": _clean(t2),
               "message": "已把「%s」设为%s" % (t2.get("username"), t2.get("status_text"))})


@router.post("/admin/api/tenants/{tid}/password")
async def admin_tenant_password(request: Request, tid: int):
    """总台改某账号的登录账号 / 登录密码（两个都可改，只改传上来的那个）。"""
    if not authz.is_admin(request):
        return _unauth()
    t = tenants.get(tid)
    if not t:
        return _err("账号不存在", 404)
    b = await _body(request)
    new = str(b.get("new") or b.get("password") or "")
    uname = str(b.get("username") or "").strip()
    if uname and uname != str(t.get("username") or ""):
        try:
            tenants.set_username(tid, uname)
        except ValueError as e:
            return _err(str(e))
        audit.log("set_username", actor_name=ADMIN_NAME, target=str(t.get("username") or tid),
                  detail="登录账号改为「%s」" % uname, ip=_ip(request), ua=_ua(request), level="warn")
    if not new:
        t2 = tenants.get(tid)
        pwd_show = ""
        try:
            from app import sites as _sites0
            pwd_show = _sites0.get_pwd(tid)
        except Exception:
            pwd_show = ""
        return _j({"ok": True, "tenant": _clean(t2), "username": t2.get("username"),
                   "password": pwd_show, "password_changed": False,
                   "message": "已把登录账号改为「%s」" % t2.get("username")})
    why = _pwd_why(new)
    if why:
        return _err(why)
    try:
        tenants.set_password(tid, new)
    except ValueError as e:
        return _err(str(e))
    # R65 记住明文密码
    try:
        from app import sites as _sites
        _sites.remember_pwd(tid, new)
    except Exception:
        pass
    authz.bump_epoch(tid, "总台重置密码")                   # 旧登录全部失效
    t2 = tenants.get(tid) or t
    audit.log("set_password", actor_name=ADMIN_NAME, target=str(t2.get("username") or tid),
              detail="重置了该账号密码", ip=_ip(request), ua=_ua(request), level="warn")
    return _j({"ok": True, "tenant": _clean(t2), "username": t2.get("username"),
               "password": new, "password_changed": True,
               "message": "已重置「%s」的密码" % t2.get("username")})


@router.get("/admin/api/quota/ledger")
def admin_ledger(request: Request):
    if not authz.is_admin(request):
        return _unauth()
    lim = _qi(request, "limit", 50, 1, 500)
    off = _qi(request, "offset", 0, 0, 10 ** 7)
    tid_raw = _qs(request, "tenant_id")
    tid = int(tid_raw) if tid_raw.lstrip("-").isdigit() else None
    res = quota.ledger(tid=tid, limit=lim, offset=off, kind=_qs(request, "kind")[:24])
    return _j({"ok": True, **res})


@router.get("/admin/api/audit")
def admin_audit(request: Request):
    if not authz.is_admin(request):
        return _unauth()
    lim = _qi(request, "limit", 50, 1, 500)
    off = _qi(request, "offset", 0, 0, 10 ** 7)
    res = audit.query(scope_ids=None, limit=lim, offset=off,
                      action=_qs(request, "action")[:32], q=_qs(request, "q")[:64])
    return _j({"ok": True, **res, "actions": audit.actions(), "levels": audit.levels()})


@router.get("/admin/api/proxy")
def admin_proxy_get(request: Request):
    if not authz.is_admin(request):
        return _unauth()
    p = _proxy_mod()
    if p is None:
        return _err("出口代理模块还没装好（app/proxy.py 缺失），请稍后再试", 503)
    try:
        cfg = p.get_config()
        st = p.stats()
    except Exception as e:
        return _err("代理配置读取失败：%s" % str(e)[:120], 500)
    cfg = dict(cfg or {})
    # ★ 密码绝不回传明文：只给「是否已设置」标记，改密码时才需要重新输入
    cfg["password_set"] = bool(cfg.get("password"))
    cfg["password"] = "***" if cfg.get("password") else ""
    return _j({"ok": True, "config": cfg, "stats": st})


@router.post("/admin/api/proxy")
async def admin_proxy_set(request: Request):
    if not authz.is_admin(request):
        return _unauth()
    p = _proxy_mod()
    if p is None:
        return _err("出口代理模块还没装好（app/proxy.py 缺失），请稍后再试", 503)
    b = await _body(request)
    kw = dict((k, b[k]) for k in PROXY_KEYS if k in b)
    # 留空 / 掩码 => 保持原密码不变（后台不再回显明文密码）
    if str(kw.get("password") or "").strip() in ("", "***", "••••••", "********"):
        kw.pop("password", None)
    if not kw:
        return _err("没有需要保存的字段")
    try:
        cfg = p.set_config(**kw)
    except Exception as e:
        return _err("保存失败：%s" % str(e)[:120])
    audit.log("proxy_update", actor_name=ADMIN_NAME, target="出口代理池",
              detail="修改字段：%s" % ",".join(sorted(kw.keys())),
              ip=_ip(request), ua=_ua(request), level="warn")
    cfg = dict(cfg or {})
    cfg["password_set"] = bool(cfg.get("password"))
    cfg["password"] = "***" if cfg.get("password") else ""
    return _j({"ok": True, "config": cfg, "message": "代理配置已保存"})


@router.post("/admin/api/proxy/test")
async def admin_proxy_test(request: Request):
    if not authz.is_admin(request):
        return _unauth()
    p = _proxy_mod()
    if p is None:
        return _err("出口代理模块还没装好（app/proxy.py 缺失），请稍后再试", 503)
    b = await _body(request)
    try:
        n = max(1, min(int(b.get("n") or 1), 10))
    except Exception:
        n = 1
    try:
        r = p.test(n)
    except Exception as e:
        return _err("测试失败：%s" % str(e)[:120])
    ok = bool((r or {}).get("ok"))
    audit.log("proxy_update", actor_name=ADMIN_NAME, target="出口代理池",
              detail="连通性测试%s" % ("成功" if ok else "失败"),
              ip=_ip(request), ua=_ua(request), level="info" if ok else "warn")
    msg = ("代理连通，出口 IP：%s" % ((r or {}).get("ip") or "?")) if ok else \
          ("代理不通：%s" % ((r or {}).get("error") or "未知原因"))
    return _j({"ok": True, "result": r, "message": msg})


# --------------------------------------------------------------------------- #
# 代理商侧：登录 / 会话
# --------------------------------------------------------------------------- #
@router.post("/a/login")
async def a_login(request: Request):
    ip = _ip(request)
    if not authz.allow("tnlogin:" + ip, authz.LOGIN_RATE, 60):
        return _err("操作太频繁，请稍后再试", 429)
    if guard.is_banned(ip):
        return _err("该 IP 已被封禁，无法登录", 403)
    b = await _body(request)
    username = str(b.get("username") or "").strip()
    pwd = str(b.get("password") or "")
    t, msg = authz.login(username, pwd, ip)
    if not t:
        audit.log("login_fail", actor_name=(username or "未知"), actor_type="tenant",
                  target=username, detail=msg, ip=ip, ua=_ua(request), level="warn")
        return _err(msg, 401)
    # R64 绑定网址：子代理必须走上级给的专属网址（/a/s/...），通用入口不放行
    # ★R211：原来只要 site 非空就能绕过 —— 现在必须真的解析出该入口，且本人 tid 在允许集合内
    if int(t.get("level") or 0) >= 2:
        _site = str(b.get("site") or "").strip()
        _sub = str(b.get("sub") or "").strip()
        _ok_site = False
        if _site:
            try:
                from app import sites as _sites_mod
                _owner, _srow = _sites_mod.resolve(_site, _sub)
                if _owner and int(t.get("id") or 0) in _sites_mod.allowed_ids(_owner, _srow):
                    _ok_site = True
            except Exception:
                _ok_site = False
        if not _ok_site:
            audit.log("login_fail", actor_name=str(t.get("username")), actor_type="tenant",
                      actor_id=int(t.get("id") or 0), target=str(t.get("username")),
                      detail="子代理使用通用入口/他人入口登录被拒", ip=ip, ua=_ua(request), level="warn")
            return _err("子代理请使用上级给你的专属网址登录", 403)
    audit.log("login", actor_name=str(t.get("username")), actor_type="tenant",
              actor_id=int(t.get("id") or 0), target=str(t.get("username")),
              detail="登录成功（%s）" % t.get("level_text"), ip=ip, ua=_ua(request))
    me = _clean(tenants.get(t["id"]) or t)
    resp = JSONResponse({"ok": True, "me": me, "tenant": me, "readonly": authz.readonly(t),
                         "message": "登录成功"})
    authz.set_cookie(resp, t.get("id"), secure=authz.is_https(request))
    return resp


@router.get("/a/logout")
def a_logout(request: Request):
    t = authz.from_request(request)
    if t:
        audit.log("logout", actor_name=str(t.get("username")), actor_type="tenant",
                  actor_id=int(t.get("id") or 0), target=str(t.get("username")),
                  detail="退出登录", ip=_ip(request), ua=_ua(request))
    resp = JSONResponse({"ok": True, "message": "已退出登录"})
    authz.clear_cookie(resp)
    return resp


@router.get("/a/api/me")
def a_me(request: Request):
    t = authz.from_request(request)
    if not t:
        return _unauth()
    d = _clean(tenants.get(t["id"]) or t)
    d["readonly"] = authz.readonly(d)
    d["sub_left"] = max(0, int(d.get("max_sub_agents") or 0) - tenants.sub_agents_used(int(d.get("id") or 0)))
    d["parent_name"] = ""
    pid = int(d.get("parent_id") or 0)
    if pid > 0:
        p = tenants.get(pid)
        if p:
            d["parent_name"] = str(p.get("username") or "")
    return _j({"ok": True, "me": d})


# --------------------------------------------------------------------------- #
# 代理商 / 子代理商：本人修改自己的密码（一级 / 二级 同一套接口）
# --------------------------------------------------------------------------- #
PWD_MIN = int(os.environ.get("TENANT_PWD_MIN", "8"))   # R66d：与后台弹窗提示一致（原默认 6，前端要 8，规则打架）
PWD_MAX = int(os.environ.get("TENANT_PWD_MAX", "64"))
PWD_RATE = int(os.environ.get("TENANT_PWD_RATE", "5"))          # 每 N 秒 / 每账号
PWD_RATE_SEC = int(os.environ.get("TENANT_PWD_RATE_SEC", "600"))


def _pwd_why(pwd: str) -> str:
    '''返回空串=通过，否则中文原因。'''
    if not pwd:
        return "请填写新密码"
    if len(pwd) < PWD_MIN:
        return "新密码至少 %d 位" % PWD_MIN
    if len(pwd) > PWD_MAX:
        return "新密码最长 %d 位" % PWD_MAX
    if any(ch.isspace() for ch in pwd):
        return "新密码不能包含空格"
    return ""


@router.post("/a/api/me/password")
async def a_change_my_password(request: Request):
    '''代理商 / 子代理商本人改自己的密码（两级同一套）。'''
    t = authz.from_request(request)
    if not t:
        return _unauth()
    tid = authz.tid_of(t)
    if authz.readonly(t):
        return _readonly()
    ip = _ip(request)
    if not authz.allow("tnpwd:%d" % tid, PWD_RATE, PWD_RATE_SEC):
        return _err("改密码太频繁了，请过一会再试", 429)
    b = await _body(request)
    old = str(b.get("old") or b.get("old_password") or "")
    new = str(b.get("new") or b.get("new_password") or "")
    if not old:
        return _err("请输入原密码")
    why = _pwd_why(new)
    if why:
        return _err(why)
    if new == old:
        return _err("新密码不能和原密码一样")
    stored = str(t.get("pwd_hash") or "")
    ok = False
    try:
        ok = bool(stored) and otp.verify_password(stored, old)
    except Exception:
        ok = False
    if not ok:
        audit.log("set_password", actor_name=str(t.get("username")), actor_type="tenant",
                  actor_id=tid, target=str(t.get("username")),
                  detail="原密码错误，本人改密码失败", ip=ip, ua=_ua(request), level="warn")
        try:
            guard.add_fail(ip, authz.LOGIN_MAX_FAIL, authz.LOGIN_BAN_SEC,
                           reason="代理商改密码原密码错误过多")
        except Exception:
            pass
        return _err("原密码不对", 400)
    try:
        tenants.set_password(tid, new)
    except ValueError as e:
        return _err(str(e))
    try:
        from app import sites as _sites
        _sites.remember_pwd(tid, new)
    except Exception:
        pass
    try:
        guard.clear_fails(ip)
    except Exception:
        pass
    # R66：改完密码把这个账号所有旧登录踢掉（其它浏览器/别的手机全部失效），
    #      当前这台浏览器立刻补发一张新 cookie —— 自己不需要重新登录。
    authz.bump_epoch(tid, "本人修改密码")
    audit.log("set_password", actor_name=str(t.get("username")), actor_type="tenant",
              actor_id=tid, target=str(t.get("username")),
              detail="本人修改了自己的密码（%s）" % t.get("level_text"),
              ip=ip, ua=_ua(request), level="warn")
    resp = _j({"ok": True, "message": "密码已修改，其它已登录的浏览器已全部退出"})
    authz.set_cookie(resp, tid, secure=authz.is_https(request))
    return resp


@router.get("/a/api/dashboard")
def a_dashboard(request: Request):
    t = authz.from_request(request)
    if not t:
        return _unauth()
    lvl = authz.level_of(t)
    tid = authz.tid_of(t)
    scope = authz.scope_ids(t)
    cs = _code_stats(owner_ids=scope)
    es = store.events_stats(owner_ids=scope)
    qsum = {"total": 0, "given": 0, "used": 0, "available": 0}
    for i in scope:
        try:
            s = quota.summary(i)
        except Exception:
            continue
        for k in ("total", "given", "used", "available"):
            qsum[k] += int(s.get(k) or 0)
    try:
        rc_in = int(quota.kind_income(scope, "recharge"))
    except Exception:
        rc_in = 0
    led = quota.ledger(scope_ids=scope, limit=10).get("rows", [])
    kids = tenants.children(tid) if lvl == 1 else []
    ts = {"total": 1 + len(kids), "l1": (1 if lvl == 1 else 0), "l2": len(kids),
          "active": 0, "disabled": 0, "banned": 0}
    for x in ([tenants.get(tid) or t] + list(kids)):
        st = str((x or {}).get("status") or "")
        if st in ts:
            ts[st] += 1
    tops = []
    for r in sorted(kids, key=lambda x: int(x.get("quota_total") or 0), reverse=True)[:5]:
        tops.append({"id": r.get("id"), "username": r.get("username"), "status": r.get("status"),
                     "quota_total": int(r.get("quota_total") or 0),
                     "quota_used": int(r.get("quota_used") or 0),
                     "available": int(r.get("available") or 0)})
    me = _clean(tenants.get(tid) or t)
    me["sub_left"] = max(0, int(me.get("max_sub_agents") or 0) - tenants.sub_agents_used(tid))
    return _j({"ok": True, "me": me, "tenants": ts,
               "codes": {"total": cs["total_all"], "claims": cs["claims"], "by_status": cs["by_status"]},
               "events": es, "quota": qsum, "recharge": {"in": rc_in},
               "recent": led, "tops": tops,
               "readonly": authz.readonly(t)})


@router.get("/a/api/tenants")
def a_tenants(request: Request):
    t = authz.from_request(request)
    if not t:
        return _unauth()
    lvl = authz.level_of(t)
    tid = authz.tid_of(t)
    lim = _qi(request, "limit", 50, 1, 500)
    off = _qi(request, "offset", 0, 0, 10 ** 7)
    q = _qs(request, "q")[:64]
    if lvl >= 2:
        me = _clean(tenants.get(tid) or t)
        return _j({"ok": True, "rows": [me], "total": 1, "pages": 1, "limit": lim, "offset": off,
                   "sub_left": 0})
    res = tenants.listing(parent_id=tid, level=2, q=q, limit=lim, offset=off)
    return _j({"ok": True, "rows": _clean_rows(res.get("rows")), "total": int(res.get("total") or 0),
               "pages": int(res.get("pages") or 1), "limit": lim, "offset": off,
               "sub_left": max(0, int(t.get("max_sub_agents") or 0) - tenants.sub_agents_used(tid))})


@router.post("/a/api/tenants/{tid}/password")
async def a_tenant_password(request: Request, tid: int):
    """代理商（L1）给自己名下子代理改「登录账号 / 登录密码」。

    只改传上来的那部分：username 与现名不同才改账号；new 非空才改密码。
    改密码后子代理所有旧登录立即失效（bump_epoch）。
    """
    t = authz.from_request(request)
    if not t:
        return _unauth()
    if authz.readonly(t):
        return _readonly()
    kid = tenants.get(tid)
    if not kid:
        return _err("账号不存在", 404)
    if not authz.can_manage(t, kid):
        audit.log("acl", actor_name=str(t.get("username")), actor_type="tenant",
                  actor_id=authz.tid_of(t), target=str(kid.get("username") or tid),
                  detail="越权尝试修改他人账号密码", ip=_ip(request), ua=_ua(request),
                  level="danger")
        return _err("只能改自己名下子代理的账号密码", 403)
    if int(kid.get("level") or 0) != 2:
        return _err("只能改子代理的账号密码", 403)
    me_tid = authz.tid_of(t)
    if not authz.allow("tnchildpwd:%d" % me_tid, PWD_RATE, PWD_RATE_SEC):
        return _err("改动太频繁了，请过一会再试", 429)
    ip = _ip(request)
    b = await _body(request)
    uname = str(b.get("username") or "").strip()
    new = str(b.get("new") or b.get("password") or "")
    old_name = str(kid.get("username") or "")
    rename = bool(uname) and uname != old_name
    if not rename and not new:
        return _err("请填写要修改的登录账号或新密码")
    if rename:
        try:
            tenants.set_username(tid, uname)
        except ValueError as e:
            return _err(str(e))
        audit.log("set_username", actor_name=str(t.get("username")), actor_type="tenant",
                  actor_id=me_tid, target=old_name,
                  detail="把子代理登录账号改为「%s」" % uname,
                  ip=ip, ua=_ua(request), level="warn")
    if new:
        why = _pwd_why(new)
        if why:
            return _err(why)
        try:
            tenants.set_password(tid, new)
        except ValueError as e:
            return _err(str(e))
        try:
            from app import sites as _sites
            _sites.remember_pwd(tid, new)
        except Exception:
            pass
        authz.bump_epoch(tid, "上级修改密码")
        audit.log("set_password", actor_name=str(t.get("username")), actor_type="tenant",
                  actor_id=me_tid, target=str((tenants.get(tid) or kid).get("username") or tid),
                  detail="为子代理修改了登录密码", ip=ip, ua=_ua(request), level="warn")
    t2 = _clean(tenants.get(tid) or kid)
    pwd_show = new
    if not pwd_show:
        try:
            from app import sites as _sites2
            pwd_show = _sites2.get_pwd(tid)
        except Exception:
            pwd_show = ""
    msg = "已保存：登录账号「%s」" % t2.get("username")
    if new:
        msg += "，密码已更新（该账号其它设备已全部退出）"
    return _j({"ok": True, "tenant": t2, "username": t2.get("username"),
               "password": pwd_show, "password_changed": bool(new), "message": msg})


@router.post("/a/api/tenants")
async def a_create_tenant(request: Request):
    t = authz.from_request(request)
    if not t:
        return _unauth()
    lvl = authz.level_of(t)
    tid = authz.tid_of(t)
    if lvl != 1:
        audit.log("acl", actor_name=str(t.get("username")), actor_type="tenant", actor_id=tid,
                  target="/a/api/tenants", detail="越权尝试开通下级账号（%s）" % t.get("level_text"),
                  ip=_ip(request), ua=_ua(request), level="danger")
        return _err("只有一级代理商可以开通子代理商", 403)
    if authz.readonly(t):
        return _readonly()
    b = await _body(request)
    try:
        username = str(b.get("username") or "").strip()
        password = str(b.get("password") or "")
        quota_n = int(b.get("quota") or 0)
    except Exception:
        return _err("参数格式不对")
    if quota_n < 0:
        return _err("额度不能是负数")
    why = _pwd_why(password)      # ★R211：建号密码强度
    if why:
        return _err(why)
    # 一共三级（总台 / 代理商 / 子代理商）：子代理不给下级名额，客户端传什么都不理会
    memo = str(b.get("memo") or b.get("remark") or "")[:200]
    max_agents = int(t.get("max_sub_agents") or 0)
    try:
        with store._lock:      # 名额检查 + 建号 原子化（RLock 可重入）
            used = tenants.sub_agents_used(tid)
            if used >= max_agents:
                return _err("可开子代理名额已用完（%d/%d），请让上级给你增加名额" % (used, max_agents))
            if quota_n > quota.available(tid):
                return _err("可用额度不足：你当前可用 %d 张，本次要划出 %d 张"
                            % (quota.available(tid), quota_n))
            sub = tenants.create(username, password, level=2, parent_id=tid, max_sub_agents=0,
                                 remark=memo, created_by=str(t.get("username") or ""))
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err("开通失败：%s" % str(e)[:120], 500)
    # R65 记住明文密码，上级随时可查/转发
    try:
        from app import sites as _sites
        _sites.remember_pwd(int(sub["id"]), password)
    except Exception:
        pass
    if quota_n > 0:
        try:
            quota.allocate(tid, int(sub["id"]), quota_n, operator=str(t.get("username")),
                           operator_level=1, reason="开通子代理商划拨")
        except QuotaError as e:
            try:
                tenants.set_status(int(sub["id"]), "disabled")
            except Exception:
                pass
            audit.log("create_tenant", actor_name=str(t.get("username")), actor_type="tenant", actor_id=tid,
                      target=username, detail="账号已建但划拨额度失败：%s" % e,
                      ip=_ip(request), ua=_ua(request), level="warn")
            return _j({"ok": False, "tenant": _clean(tenants.get(int(sub["id"]))),
                       "message": "账号已创建（已置为停用），但划拨额度失败：%s" % e}, 400)
    audit.log("create_tenant", actor_name=str(t.get("username")), actor_type="tenant", actor_id=tid,
              target=username, detail="开通子代理商，划拨 %d 张额度" % quota_n,
              ip=_ip(request), ua=_ua(request))
    sub2 = tenants.get(int(sub["id"]))
    return _j({"ok": True, "tenant": _clean(sub2), "available": quota.available(tid),
               "message": "已开通子代理商「%s」%s" % (username, ("，划拨 %d 张额度" % quota_n) if quota_n else "")})


@router.post("/a/api/tenants/{id}/quota")
async def a_tenant_quota(request: Request, id: int):
    t = authz.from_request(request)
    if not t:
        return _unauth()
    target = tenants.get(id)
    if not target:
        return _err("账号不存在", 404)
    if not authz.can_manage(t, target):
        audit.log("acl", actor_name=str(t.get("username")), actor_type="tenant", actor_id=authz.tid_of(t),
                  target=str(target.get("username") or id),
                  detail="越权尝试调整他人额度（%s）" % t.get("level_text"),
                  ip=_ip(request), ua=_ua(request), level="danger")
        return _err("没有权限操作该账号", 403)
    if authz.readonly(t):
        return _readonly()
    b = await _body(request)
    try:
        delta = int(b.get("delta") or 0)
    except Exception:
        return _err("调整数量不正确")
    if delta == 0:
        return _err("调整数量不能为 0")
    reason = str(b.get("reason") or "").strip()[:120]
    memo = str(b.get("memo") or "")[:200]
    op = str(t.get("username") or "")
    try:
        if delta > 0:
            quota.allocate(authz.tid_of(t), id, delta, operator=op, operator_level=1,
                           reason=reason or "追加额度", memo=memo)
            act, word = "quota_allocate", "追加"
        else:
            _reclaim(t, target, -delta, op, reason or "回收额度", memo)
            act, word = "quota_recycle", "回收"
    except QuotaError as e:
        return _err(str(e))
    t2 = tenants.get(id)
    audit.log(act, actor_name=op, actor_type="tenant", actor_id=authz.tid_of(t),
              target=str(target.get("username") or id),
              detail="%s %d 张，对方当前可用 %d 张" % (word, abs(delta), int((t2 or {}).get("available") or 0)),
              ip=_ip(request), ua=_ua(request))
    led = quota.ledger(tid=id, limit=1).get("rows", [])
    return _j({"ok": True, "tenant": _clean(t2), "ledger": (led[0] if led else None),
               "available": quota.available(authz.tid_of(t)),
               "message": "已%s %d 张，你当前可用 %d 张" % (word, abs(delta), quota.available(authz.tid_of(t)))})


@router.post("/a/api/tenants/{id}/status")
async def a_tenant_status(request: Request, id: int):
    t = authz.from_request(request)
    if not t:
        return _unauth()
    target = tenants.get(id)
    if not target:
        return _err("账号不存在", 404)
    if not authz.can_manage(t, target):
        audit.log("acl", actor_name=str(t.get("username")), actor_type="tenant", actor_id=authz.tid_of(t),
                  target=str(target.get("username") or id),
                  detail="越权尝试改他人账号状态（%s）" % t.get("level_text"),
                  ip=_ip(request), ua=_ua(request), level="danger")
        return _err("没有权限操作该账号", 403)
    if authz.readonly(t):
        return _readonly()
    b = await _body(request)
    status = str(b.get("status") or "").strip()
    if status not in tenants.STATUSES:
        return _err("状态值不正确")
    try:
        tenants.set_status(id, status)
    except ValueError as e:
        return _err(str(e))
    if status != "active":
        authz.bump_epoch(id, "账号状态改为%s" % status)     # 停用/封禁 -> 立刻踢下线
    t2 = tenants.get(id)
    audit.log("set_status", actor_name=str(t.get("username")), actor_type="tenant",
              actor_id=authz.tid_of(t), target=str(t2.get("username") or id),
              detail="把子代理状态改为%s" % t2.get("status_text"),
              ip=_ip(request), ua=_ua(request), level="warn")
    return _j({"ok": True, "tenant": _clean(t2),
               "message": "已把「%s」设为%s" % (t2.get("username"), t2.get("status_text"))})


@router.post("/a/api/tenants/{id}/recycle")
async def a_tenant_recycle(request: Request, id: int):
    """★R86 代理商(L1)「一键回收」名下子代理：未用完的卡密 + 剩余可用额度，全部收回。

    body: { dry?: bool, visiting?: bool, lock?: bool }
      dry=1     只预览（弹窗二次确认用），不落库
      visiting  true = 连带回收「已进入领取但未完成」的卡密（默认不动，避免打断正在领的人）
      lock      true = 回收的同时把子代理账号停用并踢下线
    """
    t = authz.from_request(request)
    if not t:
        return _unauth()
    if authz.readonly(t):
        return _readonly()
    kid = tenants.get(id)
    if not kid:
        return _err("账号不存在", 404)
    if not authz.can_manage(t, kid):
        audit.log("acl", actor_name=str(t.get("username")), actor_type="tenant",
                  actor_id=authz.tid_of(t), target=str(kid.get("username") or id),
                  detail="越权尝试回收他人子代理的额度与卡密",
                  ip=_ip(request), ua=_ua(request), level="danger")
        return _err("只能回收自己名下子代理的额度与卡密", 403)
    if int(kid.get("level") or 0) != 2:
        return _err("只能回收子代理", 403)
    me = authz.tid_of(t)
    if not authz.allow("tnrecycle:%d" % me, RECYCLE_RATE, RECYCLE_WIN):
        return _err("回收操作太频繁，请过一会儿再试", 429)
    b = await _body(request)
    dry = bool(b.get("dry"))
    inc_v = bool(b.get("visiting"))
    lock = bool(b.get("lock"))
    cid = int(kid.get("id") or 0)
    st = (_code_stats(owner_ids=[cid]).get("by_status") or {})
    unused = int(st.get("unused") or 0)
    visiting = int(st.get("visiting") or 0)
    av_c = quota.available(cid)
    if dry:
        return _j({"ok": True, "dry": True, "id": cid, "username": kid.get("username"),
                   "quota": av_c, "unused": unused, "visiting": visiting,
                   "done": int(st.get("done") or 0), "disabled": int(st.get("disabled") or 0),
                   "will_codes": unused + (visiting if inc_v else 0),
                   "will_total": av_c + unused + (visiting if inc_v else 0),
                   "my_available": quota.available(me)})
    try:
        r = _recycle(t, kid, operator=str(t.get("username") or ""),
                     reason="回收子代理 %s" % str(kid.get("username") or cid),
                     memo=("含已进入领取 %d 张" % visiting) if inc_v else "仅未使用卡密",
                     include_visiting=inc_v)
    except QuotaError as e:
        return _err(str(e))
    if lock:
        try:
            tenants.set_status(cid, "disabled")
        except ValueError as e:
            return _err(str(e))
        authz.bump_epoch(cid, "被上级回收并停用")
    audit.log("recycle", actor_name=str(t.get("username")), actor_type="tenant", actor_id=me,
              target=str(kid.get("username") or cid),
              detail="一键回收子代理：%d 张卡密置为停用 + %d 张额度收回（对方剩余 %d 张）%s"
                     % (r["codes"], r["moved"], r["child_available"], "，并停用该账号" if lock else ""),
              ip=_ip(request), ua=_ua(request), level="warn")
    if r["moved"] <= 0 and r["codes"] <= 0:
        msg = "这个子代理已经回收干净了：剩余额度 0 张、也没有未使用的卡密"
    else:
        msg = "已回收：%d 张卡密停用、%d 张额度回到你账上（该子代理剩余 %d 张）" % (
            r["codes"], r["moved"], r["child_available"])
    if lock:
        msg += "，账号已停用"
    return _j({"ok": True, "codes": r["codes"], "quota": r["quota"], "moved": r["moved"],
               "child_available": r["child_available"], "my_available": r["parent_available"],
               "locked": bool(lock), "message": msg})


@router.get("/a/api/codes")
def a_codes(request: Request):
    t = authz.from_request(request)
    if not t:
        return _unauth()
    scope = authz.scope_ids(t)
    lim = _qi(request, "limit", 50, 1, 500)
    off = _qi(request, "offset", 0, 0, 10 ** 7)
    status = _qs(request, "status")[:16]
    if status not in ("unused", "visiting", "done", "disabled"):
        status = ""
    c = _code_stats(owner_ids=scope, status=status)
    rows = store.listing(status, "", lim, off, owner_ids=scope)
    base = authz.base_url(request)
    for r in rows:
        r["url"] = "%s/%s" % (base, r["code"])
        ts = int(r.get("created_at") or 0)
        r["created_at_str"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""
    total = c["total"]
    return _j({"ok": True, "rows": rows, "total": total,
               "pages": max(1, (total + lim - 1) // lim) if lim else 1,
               "limit": lim, "offset": off, "base": base,
               "stats": c["by_status"], "claims": c["claims"], "total_all": c["total_all"]})


@router.post("/a/api/gen")
async def a_gen(request: Request):
    t = authz.from_request(request)
    if not t:
        return _unauth()
    if authz.readonly(t):
        return _readonly()
    tid = authz.tid_of(t)
    lvl = authz.level_of(t)
    if not authz.allow("tngen:%d" % tid, GEN_RATE, 60):
        return _err("生成太频繁了，歇一会儿再试", 429)
    b = await _body(request)
    try:
        count = int(b.get("count") or 0)
    except Exception:
        return _err("数量不正确")
    if count <= 0:
        return _err("请填写要生成的数量")
    if count > MAX_GEN:
        return _err("单次最多生成 %d 张" % MAX_GEN)
    note = str(b.get("note") or "")[:120]
    mc = 1   # ★R209：一码一次，忽略前端传值
    av = quota.available(tid)
    if count > av:
        return _err("可用额度不足：你还剩 %d 张，本次要 %d 张" % (av, count))
    user = str(t.get("username") or "")
    root = tid if lvl == 1 else int(t.get("parent_id") or 0)
    try:
        quota.spend(tid, count, operator=user, reason="生成卡密")
    except QuotaError as e:
        return _err(str(e))
    try:
        made = store.generate(count, note=note, max_claims=mc, owner_id=tid, root_id=root,
                              created_by=user)
    except Exception as e:
        try:
            quota.refund(tid, count, reason="生成失败回滚", operator=user)
        except Exception:
            pass
        audit.log("gen_codes", actor_name=user, actor_type="tenant", actor_id=tid, target="%d 张" % count,
                  detail="生成失败已回滚：%s" % str(e)[:100], ip=_ip(request), ua=_ua(request), level="warn")
        return _err("生成失败：%s" % str(e)[:120], 500)
    base = authz.base_url(request)
    items = [{"code": c, "url": "%s/%s" % (base, c), "owner_id": tid} for c in made]
    left = quota.available(tid)
    audit.log("gen_codes", actor_name=user, actor_type="tenant", actor_id=tid, target="%d 张" % len(made),
              detail="生成了 %d 张卡密（每码限领 1 次），剩余可用 %d 张" % (len(made), left),
              ip=_ip(request), ua=_ua(request))
    return _j({"ok": True, "count": len(made), "items": items, "max_claims": mc, "available": left,
               "message": "已生成 %d 张卡密，剩余可用 %d 张" % (len(made), left)})


@router.post("/a/api/codes/disable")
async def a_codes_disable(request: Request):
    """★R209：代理商随时停用/启用自己名下的卡密链接（停用即踢掉在途会话）。"""
    t = authz.from_request(request)
    if not t:
        return _unauth()
    if authz.readonly(t):
        return _readonly()
    b = await _body(request)
    raw = b.get("codes")
    if not raw:
        raw = [b.get("code")]
    codes = [str(x or "").strip().upper() for x in (raw or []) if x][:500]
    if not codes:
        return _err("请选择要操作的卡密")
    disable = bool(b.get("disabled", True))
    scope = authz.scope_ids(t)
    tid = authz.tid_of(t)
    done = []
    for code in codes:
        rec = store.get(code)
        if not rec:
            continue
        if int(rec.get("owner_id") or 0) not in scope and int(rec.get("root_id") or 0) not in scope:
            continue
        if disable:
            store.set_status(code, "disabled")
            try:
                SM.kill(code)
            except Exception:
                pass
        else:
            claims = int(rec.get("claims") or 0)
            status = "done" if claims >= 1 else ("visiting" if int(rec.get("visits") or 0) > 0 else "unused")
            store.set_status(code, status)
        done.append(code)
    user = str(t.get("username") or "")
    audit.log("disable_codes" if disable else "enable_codes", actor_name=user, actor_type="tenant",
              actor_id=tid, target="%d 条" % len(done),
              detail=("停用" if disable else "启用") + "卡密：" + ",".join(done[:20]),
              ip=_ip(request), ua=_ua(request))
    return _j({"ok": True, "count": len(done), "codes": done})


@router.get("/a/api/logs")
def a_logs(request: Request):
    t = authz.from_request(request)
    if not t:
        return _unauth()
    scope = authz.scope_ids(t)
    lim = _qi(request, "limit", 50, 1, 500)
    off = _qi(request, "offset", 0, 0, 10 ** 7)
    code = _qs(request, "code").upper()[:16]
    kind = _qs(request, "kind").lower()
    if kind not in KINDS:
        kind = ""
    rows = store.events(code, kind, lim, off, owner_ids=scope)
    total = store.events_count(code, kind, owner_ids=scope)
    return _j({"ok": True, "rows": rows, "total": total,
               "pages": max(1, (total + lim - 1) // lim) if lim else 1,
               "limit": lim, "offset": off,
               "stats": store.events_stats(code, "", owner_ids=scope),
               "kind_text": store.KIND_TEXT, "code": code, "kind": kind})


@router.get("/a/api/quota/ledger")
def a_ledger(request: Request):
    t = authz.from_request(request)
    if not t:
        return _unauth()
    lim = _qi(request, "limit", 50, 1, 500)
    off = _qi(request, "offset", 0, 0, 10 ** 7)
    res = quota.ledger(scope_ids=authz.scope_ids(t), limit=lim, offset=off, kind=_qs(request, "kind")[:24])
    return _j({"ok": True, **res, "me": _clean(tenants.get(authz.tid_of(t)))})


@router.get("/a/api/audit")
def a_audit(request: Request):
    t = authz.from_request(request)
    if not t:
        return _unauth()
    lim = _qi(request, "limit", 50, 1, 500)
    off = _qi(request, "offset", 0, 0, 10 ** 7)
    res = audit.query(scope_ids=authz.scope_ids(t), limit=lim, offset=off,
                      action=_qs(request, "action")[:32], q=_qs(request, "q")[:64])
    return _j({"ok": True, **res, "actions": audit.actions(), "levels": audit.levels()})


# --------------------------------------------------------------------------- #
# R66 一次性下载链接（总台 /admin/api/dl* + 代理商 /a/api/dl*）
#   生成 -> 服务器落盘 -> 返回 /d/<token>；默认下载 1 次后链接即作废。
# --------------------------------------------------------------------------- #
DL_GEN_RATE = int(os.environ.get("DL_GEN_RATE", "20"))        # 每分钟 / 每账号
DL_MAX_HITS = int(os.environ.get("DL_MAX_HITS", "100000"))    # 单条链接最多可下载几次
DL_TTL_MAX = int(os.environ.get("DL_TTL_MAX_HOURS", "720"))   # 有效期上限（小时）
DL_PAGE = int(os.environ.get("DL_PAGE", "50"))
DL_TOTAL_CAP = int(os.environ.get("DL_TOTAL_CAP_MB", "256")) * 1024 * 1024   # ★R211：单账号占用上限


def _dl_mod():
    """懒加载 app/dl.py（缺失也不拖垮整个多租户模块）。"""
    try:
        from app import dl as m
        return m
    except Exception:
        return None


def _dl_parse(b: dict) -> dict:
    """统一解析生成参数（总台 / 代理商共用）。"""
    src = str(b.get("source") or "batch").strip().lower()
    if src not in ("batch", "text"):
        src = "batch"
    fmt = str(b.get("fmt") or "url").strip().lower()
    if fmt not in ("url", "code", "csv"):
        fmt = "url"
    try:
        ttl = int(b.get("ttl_hours", 24))
    except Exception:
        ttl = 24
    ttl = max(0, min(ttl, DL_TTL_MAX))
    try:
        mh = int(b.get("max_hits", 1))
    except Exception:
        mh = 1
    mh = max(1, min(mh, DL_MAX_HITS))
    return {"source": src, "fmt": fmt,
            "batch": str(b.get("batch") or "")[:64],
            "status": str(b.get("status") or "")[:16],
            "name": str(b.get("name") or "")[:120],
            "text": str(b.get("text") or "")[:131072],   # ★R211：400KB -> 128KB
            "note": str(b.get("note") or "")[:120],
            "ttl_hours": ttl, "max_hits": mh}


def _dl_build(request, p: dict, owner_id: int, root_id: int, created_by: str, owner_ids=None):
    """返回 (row, 错误信息)。row 已带 url。"""
    m = _dl_mod()
    if not m:
        return None, "下载链接模块不可用"
    base = authz.base_url(request)
    name = p["name"]
    ext = ""
    n_lines = 0
    if p["source"] == "text":
        content = p["text"]
        if not content.strip():
            return None, "下载内容为空"
        if not name:
            name = "内容.txt"
    else:
        st = p["status"] if p["status"] in ("unused", "visiting", "done", "disabled") else ""
        rows = store.listing(st, p["batch"], 100000, 0, owner_ids=owner_ids)
        if not rows:
            return None, "没有符合条件的卡密（换个批次或状态再试）"
        for r in rows:
            ts = int(r.get("created_at") or 0)
            r["created_at_str"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""
        content = m.make_batch_text(rows, p["fmt"], base)
        n_lines = len(rows)
        if not name:
            tag = p["batch"] or time.strftime("%Y%m%d_%H%M%S")
            name = "卡密_%s_%s.txt" % (tag, p["fmt"])
        if p["fmt"] == "csv":
            ext = ".csv"
    try:
        row = m.make(content, name, note=p["note"], owner_id=owner_id, root_id=root_id,
                     created_by=created_by, ttl_hours=p["ttl_hours"], max_hits=p["max_hits"],
                     ext=ext)
    except Exception as e:
        return None, "生成失败：%s" % str(e)[:120]
    row["url"] = base + "/d/" + str(row.get("token") or "")
    row["code_count"] = n_lines
    return row, ""


def _dl_detail(p: dict, row: dict) -> str:
    return "文件名 %s / %s / 可下载 %d 次 / %s" % (
        row.get("name") or "", row.get("size_str") or "0 B", int(p["max_hits"]),
        ("%d 小时有效" % p["ttl_hours"]) if p["ttl_hours"] else "永不过期")


@router.get("/admin/api/dl")
def admin_dl_list(request: Request):
    """总台：一次性下载链接列表 + 统计。"""
    if not authz.is_admin(request):
        return _unauth()
    m = _dl_mod()
    if not m:
        return _err("下载链接模块不可用", 503)
    lim = _qi(request, "limit", DL_PAGE, 1, 200)
    off = _qi(request, "offset", 0, 0, 10 ** 7)
    st = _qs(request, "status")[:16]
    if st not in ("active", "used", "expired", "revoked"):
        st = ""
    q = _qs(request, "q")[:64]
    base = authz.base_url(request)
    rows = m.listing(None, lim, off, st, q)
    for r in rows:
        r["url"] = base + "/d/" + str(r.get("token") or "")
    total = m.count(None, st, q)
    return _j({"ok": True, "rows": rows, "total": total,
               "pages": max(1, (total + lim - 1) // lim) if lim else 1,
               "limit": lim, "offset": off, "stats": m.stats(None)})


@router.post("/admin/api/dl")
async def admin_dl_create(request: Request):
    """总台：生成一次性下载链接。"""
    if not authz.is_admin(request):
        return _unauth()
    m = _dl_mod()
    if not m:
        return _err("下载链接模块不可用", 503)
    ip = _ip(request)
    if not authz.allow("admindl:" + str(ip), DL_GEN_RATE, 60):
        return _err("生成太频繁了，歇一会儿再试", 429)
    p = _dl_parse(await _body(request))
    row, why = _dl_build(request, p, 0, 0, ADMIN_NAME, None)
    if why:
        return _err(why)
    audit.log("dl_create", actor_name=ADMIN_NAME, actor_type="admin", actor_id=0,
              target=str(row.get("name") or ""), detail="总台生成一次性下载链接：" + _dl_detail(p, row),
              ip=ip, ua=_ua(request))
    return _j({"ok": True, "item": row,
               "message": "已生成，可下载 %d 次；把链接发给客户即可" % int(p["max_hits"])})


@router.post("/admin/api/dl/revoke")
async def admin_dl_revoke(request: Request):
    """总台：立刻作废一条下载链接。"""
    if not authz.is_admin(request):
        return _unauth()
    m = _dl_mod()
    if not m:
        return _err("下载链接模块不可用", 503)
    tok = str((await _body(request)).get("token") or "")[:200]
    okr = bool(tok) and m.revoke(tok)
    audit.log("dl_revoke", actor_name=ADMIN_NAME, actor_type="admin", actor_id=0,
              target=tok[:10] + "…", detail="总台撤销下载链接（%s）" % ("成功" if okr else "未找到"),
              ip=_ip(request), ua=_ua(request), level="warn")
    return _j({"ok": okr, "message": "已撤销，链接立刻失效" if okr else "没有找到这条链接"},
              200 if okr else 404)


@router.get("/a/api/dl")
def a_dl_list(request: Request):
    """代理商 / 子代理商：只能看到自己体系内的下载链接。"""
    t = authz.from_request(request)
    if not t:
        return _unauth()
    m = _dl_mod()
    if not m:
        return _err("下载链接模块不可用", 503)
    scope = authz.scope_ids(t)
    lim = _qi(request, "limit", DL_PAGE, 1, 200)
    off = _qi(request, "offset", 0, 0, 10 ** 7)
    st = _qs(request, "status")[:16]
    if st not in ("active", "used", "expired", "revoked"):
        st = ""
    q = _qs(request, "q")[:64]
    base = authz.base_url(request)
    rows = m.listing(scope, lim, off, st, q)
    for r in rows:
        r["url"] = base + "/d/" + str(r.get("token") or "")
    total = m.count(scope, st, q)
    return _j({"ok": True, "rows": rows, "total": total,
               "pages": max(1, (total + lim - 1) // lim) if lim else 1,
               "limit": lim, "offset": off, "stats": m.stats(scope),
               "me": _clean(tenants.get(authz.tid_of(t)))})


@router.post("/a/api/dl")
async def a_dl_create(request: Request):
    """代理商 / 子代理商：生成一次性下载链接（只包含自己体系内的卡密）。"""
    t = authz.from_request(request)
    if not t:
        return _unauth()
    if authz.readonly(t):
        return _readonly()
    m = _dl_mod()
    if not m:
        return _err("下载链接模块不可用", 503)
    tid = authz.tid_of(t)
    lvl = authz.level_of(t)
    ip = _ip(request)
    if not authz.allow("tndl:%d" % tid, DL_GEN_RATE, 60):
        return _err("生成太频繁了，歇一会儿再试", 429)
    p = _dl_parse(await _body(request))
    scope = authz.scope_ids(t)
    # ★R211：单账号下载文件占用上限，防止 20/分钟 × 大文本把磁盘写满
    try:
        _used = m.total_bytes(scope) + (len(p.get("text") or "") if p.get("source") == "text" else 0)
        if _used > DL_TOTAL_CAP:
            return _err("你名下的一次性下载文件已占用 %.1f MB（上限 %d MB），请先撤销旧链接"
                        % (_used / 1048576.0, DL_TOTAL_CAP // 1048576), 429)
    except Exception:
        pass
    user = str(t.get("username") or "")
    root = tid if lvl == 1 else int(t.get("parent_id") or 0)
    row, why = _dl_build(request, p, tid, root, user, scope)
    if why:
        return _err(why)
    audit.log("dl_create", actor_name=user, actor_type="tenant", actor_id=tid,
              target=str(row.get("name") or ""),
              detail="生成一次性下载链接：" + _dl_detail(p, row), ip=ip, ua=_ua(request))
    return _j({"ok": True, "item": row,
               "message": "已生成，可下载 %d 次；把链接发给客户即可" % int(p["max_hits"])})


@router.post("/a/api/dl/revoke")
async def a_dl_revoke(request: Request):
    """代理商 / 子代理商：撤销自己体系内的下载链接（越权 -> 拒绝 + 审计）。"""
    t = authz.from_request(request)
    if not t:
        return _unauth()
    if authz.readonly(t):
        return _readonly()
    m = _dl_mod()
    if not m:
        return _err("下载链接模块不可用", 503)
    tid = authz.tid_of(t)
    tok = str((await _body(request)).get("token") or "")[:200]
    okr = bool(tok) and m.revoke(tok, owner_ids=authz.scope_ids(t))
    lvl = "warn" if not okr else "info"
    audit.log("dl_revoke", actor_name=str(t.get("username") or ""), actor_type="tenant",
              actor_id=tid, target=tok[:10] + "…",
              detail="撤销下载链接（%s）" % ("成功" if okr else "未找到或不属于自己"),
              ip=_ip(request), ua=_ua(request), level=lvl)
    return _j({"ok": okr, "message": "已撤销，链接立刻失效" if okr else "没有找到这条链接（或不属于你）"},
              200 if okr else 404)


# --------------------------------------------------------------------------- #
# R80 充值码：总台生成 -> 代理商 / 子代理自助充值
#   防破：80bit CSPRNG 随机码 + 金额只存库 + 原子条件更新 + 三层限速 + 全程审计
# --------------------------------------------------------------------------- #
RC_GEN_RATE = int(os.environ.get("RC_GEN_RATE", "20"))       # 每分钟 / 每 IP 生成次数
RC_TRY_RATE = int(os.environ.get("RC_TRY_RATE", "15"))       # 每 IP 兑换尝试次数
RC_TRY_WIN = int(os.environ.get("RC_TRY_WIN", "600"))        # 上面那个的窗口（秒）
RC_ACCT_RATE = int(os.environ.get("RC_ACCT_RATE", "10"))     # 每账号兑换尝试次数
RC_ACCT_WIN = int(os.environ.get("RC_ACCT_WIN", "600"))


@router.post("/admin/api/recharge/gen")
async def admin_recharge_gen(request: Request):
    if not authz.is_admin(request):
        return _unauth()
    ip = _ip(request)
    if not authz.allow("rcgen:" + ip, RC_GEN_RATE, 60):
        return _err("操作太频繁，请稍后再试", 429)
    b = await _body(request)
    try:
        rows = recharge.gen(b.get("amount"), b.get("count"),
                            batch=b.get("batch"), note=b.get("note"), operator=ADMIN_NAME)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err("生成失败：%s" % str(e)[:80])
    amount = int(rows[0]["amount"]) if rows else 0
    total = amount * len(rows)
    audit.log("recharge_gen", actor_name=ADMIN_NAME, target="充值码",
              detail="生成 %d 个充值码，每个 %d 张，合计 %d 张" % (len(rows), amount, total),
              ip=ip, ua=_ua(request), level="warn")
    return _j({"ok": True, "rows": rows, "count": len(rows), "amount": amount, "total": total,
               "message": "已生成 %d 个充值码（每个 %d 张，合计 %d 张）" % (len(rows), amount, total)})


@router.get("/admin/api/recharge")
def admin_recharge_list(request: Request):
    if not authz.is_admin(request):
        return _unauth()
    res = recharge.listing(status=_qs(request, "status")[:16], batch=_qs(request, "batch")[:40],
                           q=_qs(request, "q")[:64],
                           limit=_qi(request, "limit", 50, 1, 500),
                           offset=_qi(request, "offset", 0, 0, 10 ** 7))
    return _j({"ok": True, "stats": recharge.stats(), "batches": recharge.batches(), **res})


@router.post("/admin/api/recharge/void")
async def admin_recharge_void(request: Request):
    """作废未使用的充值码（已用 / 已作废的不动）。"""
    if not authz.is_admin(request):
        return _unauth()
    b = await _body(request)
    ids = b.get("ids")
    if not isinstance(ids, list):
        ids = [ids] if ids is not None else []
    n = recharge.void(ids, operator=ADMIN_NAME)
    if not n:
        return _err("没有可作废的充值码（只能作废「未使用」的）")
    audit.log("recharge_void", actor_name=ADMIN_NAME, target="充值码",
              detail="作废 %d 个充值码" % n, ip=_ip(request), ua=_ua(request), level="warn")
    return _j({"ok": True, "n": n, "message": "已作废 %d 个充值码" % n})


@router.post("/a/api/recharge")
async def a_recharge(request: Request):
    """代理商 / 子代理：输入充值码给自己加张数。"""
    t = authz.from_request(request)
    if not t:
        return _unauth()
    if authz.readonly(t):
        return _readonly()
    tid = authz.tid_of(t)
    ip = _ip(request)
    uname = str(t.get("username") or "")
    if not authz.allow("rctu:" + str(tid), RC_ACCT_RATE, RC_ACCT_WIN):
        return _err("尝试太频繁了，请过几分钟再试", 429)
    if not authz.allow("rcip:" + ip, RC_TRY_RATE, RC_TRY_WIN):
        audit.log("recharge_fail", actor_name=uname, actor_type="tenant", actor_id=tid,
                  target="充值码", detail="同一网络兑换尝试过多，已限流",
                  ip=ip, ua=_ua(request), level="danger")
        return _err("这个网络尝试次数太多，请稍后再试", 429)
    r = recharge.redeem((await _body(request)).get("code"), tid, username=uname, ip=ip)
    if not r.get("ok"):
        audit.log("recharge_fail", actor_name=uname, actor_type="tenant", actor_id=tid,
                  target="充值码", detail=str(r.get("message") or "")[:120],
                  ip=ip, ua=_ua(request), level="warn")
        return _err(str(r.get("message") or "充值失败"), 400)
    audit.log("recharge_ok", actor_name=uname, actor_type="tenant", actor_id=tid,
              target="充值码",
              detail="充值成功 %d 张，可用 %d -> %d 张" % (int(r["amount"]), int(r["before"]),
                                                         int(r["after"])),
              ip=ip, ua=_ua(request))
    led = quota.ledger(tid=tid, limit=1).get("rows", [])
    return _j({"ok": True, "amount": int(r["amount"]), "before": int(r["before"]),
               "after": int(r["after"]), "me": _clean(tenants.get(tid) or t),
               "ledger": (led[0] if led else None), "message": r.get("message")})


@router.get("/a/api/recharge/mine")
def a_recharge_mine(request: Request):
    """我这一支（含名下子代理）的充值记录 + 汇总。"""
    t = authz.from_request(request)
    if not t:
        return _unauth()
    tid = authz.tid_of(t)
    scope = authz.scope_ids(t)
    lim = _qi(request, "limit", 50, 1, 200)
    off = _qi(request, "offset", 0, 0, 10 ** 7)
    res = recharge.listing_by_users(scope, limit=lim, offset=off)
    return _j({"ok": True, "me": _clean(tenants.get(tid) or t),
               "summary": recharge.summary_by_users(scope), **res})
