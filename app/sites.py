# -*- coding: utf-8 -*-
"""R64 绑定后台网址 —— 代理商专属入口。

规则
----
* 总台建代理商 -> 自动分配一个 slug，专属网址 = <本站>/a/s/<slug>
* 代理商可给每个子代理生成子网址 = <本站>/a/s/<slug>/<子slug>
* 根网址可登录：该代理商本人 + 名下全部子代理
* 子网址可登录：只有对应的那一个子代理
* 拿别的分支的网址登自己的账号 -> 403「该账号不属于此入口」
* 子代理无法从通用 /a/login 登录（tenant_api 里已拦），必须走上级给的专属网址

本模块只新增文件；main.py 里两行挂载即可，不改动 tenants/quota/audit/authz 的逻辑。
"""
from __future__ import annotations

import json
import os
import random
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import audit, authz, guard, store, tenants

router = APIRouter()

_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
SLUG_LEN = int(os.environ.get("SITE_SLUG_LEN", "7"))
_HTML_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_ADMIN_OK = None
_CLIENT_IP = None
_BASE_URL = None


def set_deps(admin_ok=None, client_ip=None, base_url=None):
    """由 main.py 注入（与 tenant_api 同一套依赖）。"""
    global _ADMIN_OK, _CLIENT_IP, _BASE_URL
    if admin_ok is not None:
        _ADMIN_OK = admin_ok
    if client_ip is not None:
        _CLIENT_IP = client_ip
    if base_url is not None:
        _BASE_URL = base_url


def _ip(request) -> str:
    try:
        if _CLIENT_IP is not None:
            return _CLIENT_IP(request)
        return authz.client_ip(request)
    except Exception:
        return "?"


def _ua(request) -> str:
    try:
        return (request.headers.get("user-agent") or "")[:200]
    except Exception:
        return ""


def _base(request) -> str:
    try:
        if _BASE_URL is not None:
            return str(_BASE_URL(request) or "").rstrip("/")
        return str(authz.base_url(request) or "").rstrip("/")
    except Exception:
        return ""


def _clean(t) -> dict:
    """去掉密码哈希等敏感字段再回给前端。"""
    d = dict(t or {})
    for k in ("pwd_hash", "pwd_plain", "site_slug"):
        d.pop(k, None)
    return d


def _admin_ok(request) -> bool:
    try:
        return bool(_ADMIN_OK and _ADMIN_OK(request))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 建表 / 迁移
# --------------------------------------------------------------------------- #
_ready = False


def ready_col() -> bool:
    """确保 tenants.site_slug 列存在（tenants 建表可能晚于本模块导入，所以懒加载）。"""
    global _ready
    if _ready:
        return True
    try:
        with store._lock, store._conn() as c:
            for _sql in ("ALTER TABLE tenants ADD COLUMN site_slug TEXT DEFAULT ''",
                         "ALTER TABLE tenants ADD COLUMN pwd_plain TEXT DEFAULT ''"):
                try:
                    c.execute(_sql)
                except Exception:
                    pass
            try:
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tenants_slug "
                          "ON tenants(site_slug) WHERE site_slug <> ''")
            except Exception:
                pass
            cols = [str(r["name"]) for r in c.execute("PRAGMA table_info(tenants)").fetchall()]
            _ready = "site_slug" in cols
    except Exception:
        _ready = False
    return _ready


def init():
    return ready_col()


# --------------------------------------------------------------------------- #
# slug
# --------------------------------------------------------------------------- #
def _gen() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(max(4, min(SLUG_LEN, 24))))   # ★R211：CSPRNG


def valid_slug(s) -> str:
    s = str(s or "").strip().lower()
    if not (4 <= len(s) <= 24):
        return ""
    for ch in s:
        if ch not in _ALPHABET:
            return ""
    return s


def row_by_slug(slug):
    slug = valid_slug(slug)
    if not slug or not ready_col():
        return None
    with store._lock, store._conn() as c:
        r = c.execute("SELECT * FROM tenants WHERE site_slug=?", (slug,)).fetchone()
        return dict(r) if r else None


def _new_slug() -> str:
    for _ in range(30):
        s = _gen()
        if not row_by_slug(s):
            return s
    return (_gen() + _gen())[:24]


def ensure_slug(tid) -> str:
    """返回该租户的专属 slug；没有就生成一个（幂等）。"""
    try:
        tid = int(tid or 0)
    except Exception:
        return ""
    if tid <= 0 or not ready_col():
        return ""
    for _ in range(8):
        try:
            with store._lock, store._conn() as c:
                r = c.execute("SELECT site_slug FROM tenants WHERE id=?", (tid,)).fetchone()
                if not r:
                    return ""
                cur = str(r["site_slug"] or "").strip()
                if cur:
                    return cur
                s = _new_slug()
                c.execute("UPDATE tenants SET site_slug=? WHERE id=?", (s, tid))
                return s
        except Exception:
            continue
    return ""


def rotate_slug(tid) -> str:
    """强制换一个新 slug（旧网址立即失效）。"""
    try:
        tid = int(tid or 0)
    except Exception:
        return ""
    if tid <= 0 or not ready_col():
        return ""
    for _ in range(8):
        try:
            with store._lock, store._conn() as c:
                s = _new_slug()
                c.execute("UPDATE tenants SET site_slug=? WHERE id=?", (s, tid))
                return s
        except Exception:
            continue
    return ""


# --------------------------------------------------------------------------- #
# 入口解析
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 账号密码明文（建号 / 重置时记一份，总台随时能查，方便直接转发给代理商）
# --------------------------------------------------------------------------- #
_PWD_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def gen_pwd(n: int = 10) -> str:
    n = max(8, min(int(n or 10), 32))
    return "".join(secrets.choice(_PWD_ALPHABET) for _ in range(n))   # ★R211：CSPRNG，防密码被预测


def remember_pwd(tid, pwd: str):
    """把登录密码明文存一份（供总台查看/转发）。失败不抛。"""
    try:
        tid = int(tid or 0)
    except Exception:
        return
    pwd = str(pwd or "")
    if tid <= 0 or not pwd or not ready_col():
        return
    try:
        with store._lock, store._conn() as c:
            c.execute("UPDATE tenants SET pwd_plain=? WHERE id=?", (pwd[:128], tid))
    except Exception:
        pass


def get_pwd(tid) -> str:
    """取明文密码；没存过返回空串。"""
    try:
        tid = int(tid or 0)
    except Exception:
        return ""
    if tid <= 0 or not ready_col():
        return ""
    try:
        with store._lock, store._conn() as c:
            r = c.execute("SELECT pwd_plain FROM tenants WHERE id=?", (tid,)).fetchone()
            return str(r["pwd_plain"] or "") if r else ""
    except Exception:
        return ""


def _entry_url(request, t) -> str:
    """按层级拼专属入口网址。

    R81 修复：子代理（L2）的入口是**两段式** `<父代理商 slug>/<子代理 slug>`，
    单段 `/a/s/<子slug>` 会被 resolve() 判成「入口不存在」-> 404。
    """
    tid = int(t.get("id") or 0)
    slug = ensure_slug(tid)
    if not slug:
        return ""
    if int(t.get("level") or 0) == 2:
        pid = int(t.get("parent_id") or 0)
        pslug = ensure_slug(pid) if pid else ""
        return site_url(request, pslug, slug) if pslug else ""
    return site_url(request, slug)


def _secret_payload(request, t) -> dict:
    tid = int(t.get("id") or 0)
    pwd = get_pwd(tid)
    return {"ok": True, "id": tid, "username": str(t.get("username") or ""),
            "password": pwd, "pwd_saved": bool(pwd),
            "level": int(t.get("level") or 0),
            "level_text": str(t.get("level_text") or ""),
            "status": str(t.get("status") or ""),
            "url": _entry_url(request, t)}


def resolve(slug, sub=""):
    """(owner, subrow)。owner 必须是 level=1；sub 为空时 subrow 为 None。"""
    owner = row_by_slug(slug)
    if not owner or int(owner.get("level") or 0) != 1:
        return None, None
    if str(owner.get("status") or "") == "banned":
        return None, None
    sub = str(sub or "").strip()
    if not sub:
        return owner, None
    sr = row_by_slug(sub)
    if not sr or int(sr.get("level") or 0) != 2:
        return None, None
    if int(sr.get("parent_id") or 0) != int(owner.get("id") or 0):
        return None, None
    if str(sr.get("status") or "") == "banned":
        return None, None
    return owner, sr


def allowed_ids(owner, sub=None) -> list:
    """该入口允许登录的租户 id 集合。"""
    if sub is not None:
        return [int(sub.get("id") or 0)]
    ids = [int(owner.get("id") or 0)]
    try:
        ids += [int(x) for x in tenants.child_ids(int(owner.get("id") or 0))]
    except Exception:
        pass
    return ids


def site_url(request, slug, sub="") -> str:
    b = _base(request)
    slug = str(slug or "")
    if not b or not slug:
        return ""
    return "%s/a/s/%s%s" % (b, slug, ("/" + str(sub)) if sub else "")


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
def _read(name: str) -> str:
    with open(os.path.join(_HTML_DIR, name), encoding="utf-8") as f:
        return f.read()


_STAMP = None


def set_stamp(fn):
    """主控注入静态资源版本号函数（让 /a/s/<slug> 入口页也带 ?v=）。"""
    global _STAMP
    _STAMP = fn


def _page(name: str, media: str = "text/html", inject: str = "", before: str = "", hide_total: bool = False):
    html = _read(name)
    if hide_total:
        html = html.replace('<p class="lgfoot" id="lgFoot">',
                            '<p class="lgfoot" id="lgFoot" style="display:none">', 1)
    if inject and before and before in html:
        html = html.replace(before, inject + "\n" + before, 1)
    if _STAMP is not None:
        try:
            html = _STAMP(html)
        except Exception:
            pass
    return HTMLResponse(html, media_type=media)


def _err_page(title: str, desc: str, status: int = 404):
    html = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>%s</title></head><body style=\"margin:0;background:#0b0c14;color:#e7e9ee;"
            "font:15px/1.7 system-ui,'Segoe UI',sans-serif\">"
            "<div style=\"min-height:80vh;display:grid;place-items:center;text-align:center;padding:24px\">"
            "<div><div style=\"font-size:46px;line-height:1\">🙈</div>"
            "<h2 style=\"margin:12px 0 6px;font-size:18px\">%s</h2>"
            "<p style=\"margin:0;color:#98a0b3\">%s</p></div></div></body></html>") % (title, title, desc)
    return HTMLResponse(html, status_code=status)


@router.get("/a/s/{slug}/console", response_class=HTMLResponse)
def site_console(slug: str, request: Request):
    """登录后进控制台（带上专属登录地址，会话过期能弹回自己的入口）。"""
    owner, _ = resolve(slug)
    if not owner:
        return _err_page("入口不存在", "这个后台网址不存在或已被停用。", 404)
    inject = "<script>window.__SITE_LOGIN=%s;</script>" % json.dumps("/a/s/" + valid_slug(slug))
    return _page("console.html", inject=inject, before='<script src="/console.js">')


@router.get("/a/s/{slug}", response_class=HTMLResponse)
def site_login(slug: str, request: Request):
    owner, _ = resolve(slug)
    if not owner:
        return _err_page("入口不存在", "这个后台网址不存在或已被停用，请找你的上级要新网址。", 404)
    return _page("tenant_login.html", hide_total=True)


@router.get("/a/s/{slug}/{sub}", response_class=HTMLResponse)
def site_login_sub(slug: str, sub: str, request: Request):
    owner, srow = resolve(slug, sub)
    if not owner or not srow:
        return _err_page("入口不存在", "这个后台网址不存在或已被停用，请找你的上级要新网址。", 404)
    return _page("tenant_login.html", hide_total=True)


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
@router.get("/a/api/site_info")
def site_info(request: Request, site: str = "", sub: str = ""):
    owner, srow = resolve(site, sub)
    if not owner:
        return JSONResponse({"ok": False, "message": "入口不存在或已停用"}, status_code=404)
    name = str(owner.get("username") or "")
    if srow:
        return {"ok": True, "owner": name, "level": 2,
                "title": "%s · %s" % (name, str(srow.get("username") or ""))}
    return {"ok": True, "owner": name, "level": 1, "title": "%s 专属入口" % name}


@router.post("/a/api/site_login")
async def site_login_post(request: Request):
    ip = _ip(request)
    if not authz.allow("tnsite:" + ip, authz.LOGIN_RATE, 60):
        return JSONResponse({"ok": False, "message": "操作太频繁，请稍后再试"}, status_code=429)
    if guard.is_banned(ip):
        return JSONResponse({"ok": False, "message": "该 IP 已被封禁，无法登录"}, status_code=403)
    try:
        b = await request.json()
    except Exception:
        b = {}
    if not isinstance(b, dict):
        b = {}
    site = str(b.get("site") or "").strip()
    sub = str(b.get("sub") or "").strip()
    username = str(b.get("username") or "").strip()
    pwd = str(b.get("password") or "")

    owner, srow = resolve(site, sub)
    if not owner:
        audit.log("login_fail", actor_name=(username or "未知"), actor_type="tenant",
                  target=username, detail="专属入口无效", ip=ip, ua=_ua(request), level="warn")
        return JSONResponse({"ok": False, "message": "这个后台网址已失效，请联系你的上级"}, status_code=404)

    t, msg = authz.login(username, pwd, ip)
    if not t:
        audit.log("login_fail", actor_name=(username or "未知"), actor_type="tenant",
                  target=username, detail=msg, ip=ip, ua=_ua(request), level="warn")
        return JSONResponse({"ok": False, "message": msg}, status_code=401)

    tid = int(t.get("id") or 0)
    if tid not in allowed_ids(owner, srow):
        audit.log("login_fail", actor_name=str(t.get("username")), actor_type="tenant",
                  actor_id=tid, target=str(t.get("username")),
                  detail="账号(%s)不属于该专属入口(%s)" % (t.get("username"), site),
                  ip=ip, ua=_ua(request), level="danger")
        return JSONResponse({"ok": False, "message": "该账号不属于这个入口，请用你的上级发给你的网址登录"},
                            status_code=403)

    audit.log("login", actor_name=str(t.get("username")), actor_type="tenant", actor_id=tid,
              target=str(t.get("username")), detail="专属入口登录(%s)" % site, ip=ip, ua=_ua(request))
    me = _clean(t)
    resp = JSONResponse({"ok": True, "me": me, "tenant": me, "readonly": authz.readonly(t),
                         "message": "登录成功"})
    authz.set_cookie(resp, tid, secure=authz.is_https(request))
    return resp


@router.get("/a/api/my_site")
def my_site(request: Request):
    t = authz.from_request(request)
    if not t:
        return JSONResponse({"ok": False, "message": "未登录"}, status_code=401)
    lvl = authz.level_of(t)
    tid = authz.tid_of(t)
    me = tenants.get(tid) or t
    uname = str(me.get("username") or "")

    if lvl >= 2:
        pid = int(me.get("parent_id") or 0)
        p = tenants.get(pid) or {}
        pslug = ensure_slug(pid)
        myslug = ensure_slug(tid)
        return {"ok": True, "level": 2, "account": uname, "owner": str(p.get("username") or ""),
                "url": site_url(request, pslug) if pslug else "",
                "my_url": site_url(request, pslug, myslug) if (pslug and myslug) else "",
                "can_reset": False, "subs": []}

    slug = ensure_slug(tid)
    subs = []
    for k in tenants.children(tid):
        kid = int(k.get("id") or 0)
        ks = ensure_slug(kid)
        subs.append({"id": kid, "username": str(k.get("username") or ""),
                     "status": str(k.get("status") or ""),
                     "url": site_url(request, slug, ks) if (slug and ks) else ""})
    return {"ok": True, "level": lvl, "account": uname, "owner": "",
            "url": site_url(request, slug) if slug else "",
            "my_url": "", "can_reset": True, "subs": subs}


@router.post("/a/api/my_site/reset")
async def my_site_reset(request: Request):
    t = authz.from_request(request)
    if not t:
        return JSONResponse({"ok": False, "message": "未登录"}, status_code=401)
    lvl = authz.level_of(t)
    tid = authz.tid_of(t)
    if lvl >= 2:
        return JSONResponse({"ok": False, "message": "子代理不能自己换网址，请联系你的上级"}, status_code=403)
    if authz.readonly(t):
        return JSONResponse({"ok": False, "message": "只读账号不能操作"}, status_code=403)
    try:
        b = await request.json()
    except Exception:
        b = {}
    try:
        target = int((b or {}).get("tid") or tid)
    except Exception:
        target = tid
    if target != tid:
        kid = tenants.get(target) or {}
        if int(kid.get("parent_id") or -1) != tid:
            return JSONResponse({"ok": False, "message": "只能改自己名下子代理的网址"}, status_code=403)
    if not tenants.get(target):
        return JSONResponse({"ok": False, "message": "账号不存在"}, status_code=404)
    slug = rotate_slug(target)
    if not slug:
        return JSONResponse({"ok": False, "message": "生成失败，请重试"}, status_code=500)
    audit.log("acl", actor_name=str(t.get("username")), actor_type="tenant", actor_id=tid,
              target=str(target), detail="重置专属网址", ip=_ip(request), ua=_ua(request), level="warn")
    return {"ok": True, "message": "已生成新网址，旧网址立即失效", "slug": slug}


@router.get("/admin/api/tenants/{tid}/secret")
def admin_tenant_secret(request: Request, tid: int):
    """总台查看某代理商的账号 / 密码 / 专属后台网址。"""
    if not _admin_ok(request):
        return JSONResponse({"ok": False, "message": "未授权"}, status_code=403)
    t = tenants.get(int(tid or 0))
    if not t:
        return JSONResponse({"ok": False, "message": "账号不存在"}, status_code=404)
    audit.log("acl", actor_name="总台", actor_type="admin", actor_id=0,
              target=str(t.get("username") or tid), detail="查看账号密码/专属网址",
              ip=_ip(request), ua=_ua(request), level="warn")
    return _secret_payload(request, t)


@router.post("/admin/api/tenants/{tid}/newpwd")
def admin_tenant_newpwd(request: Request, tid: int):
    """总台给代理商重置密码：自动生成并保存明文，方便直接发给对方。"""
    if not _admin_ok(request):
        return JSONResponse({"ok": False, "message": "未授权"}, status_code=403)
    t = tenants.get(int(tid or 0))
    if not t:
        return JSONResponse({"ok": False, "message": "账号不存在"}, status_code=404)
    pwd = gen_pwd(10)
    try:
        tenants.set_password(int(tid), pwd)
    except Exception as e:
        return JSONResponse({"ok": False, "message": "重置失败：%s" % str(e)[:80]}, status_code=400)
    remember_pwd(tid, pwd)
    try:
        authz.bump_epoch(int(tid), "总台重置密码")
    except Exception:
        pass
    audit.log("set_password", actor_name="总台", actor_type="admin", actor_id=0,
              target=str(t.get("username") or tid), detail="重置密码（已保存明文供分发）",
              ip=_ip(request), ua=_ua(request), level="warn")
    return {"ok": True, "password": pwd, "message": "已生成新密码，请转发给对方"}


@router.post("/a/api/tenants/{tid}/newpwd")
def tenant_child_newpwd(request: Request, tid: int):
    """一级代理商给自己名下子代理重置密码（自动生成 + 保存明文）。"""
    t = authz.from_request(request)
    if not t:
        return JSONResponse({"ok": False, "message": "未登录"}, status_code=401)
    if authz.level_of(t) != 1:
        return JSONResponse({"ok": False, "message": "只有一级代理商能重置子代理密码"}, status_code=403)
    if authz.readonly(t):
        return JSONResponse({"ok": False, "message": "只读账号不能操作"}, status_code=403)
    kid = tenants.get(int(tid or 0))
    if not kid or int(kid.get("parent_id") or 0) != authz.tid_of(t):
        return JSONResponse({"ok": False, "message": "只能操作自己名下子代理"}, status_code=403)
    pwd = gen_pwd(10)
    try:
        tenants.set_password(int(tid), pwd)
    except Exception as e:
        return JSONResponse({"ok": False, "message": "重置失败：%s" % str(e)[:80]}, status_code=400)
    remember_pwd(tid, pwd)
    try:
        authz.bump_epoch(int(tid), "上级重置密码")
    except Exception:
        pass
    audit.log("set_password", actor_name=str(t.get("username")), actor_type="tenant",
              actor_id=authz.tid_of(t), target=str(kid.get("username")),
              detail="为子代理重置密码", ip=_ip(request), ua=_ua(request), level="warn")
    return {"ok": True, "password": pwd, "message": "已生成新密码，请转发给子代理"}


@router.get("/a/api/tenants/{tid}/secret")
def tenant_child_secret(request: Request, tid: int):
    """一级代理商查看自己名下子代理的账号 / 密码 / 专属网址。"""
    t = authz.from_request(request)
    if not t:
        return JSONResponse({"ok": False, "message": "未登录"}, status_code=401)
    if authz.level_of(t) != 1:
        return JSONResponse({"ok": False, "message": "只有一级代理商能看子代理的密码"}, status_code=403)
    kid = tenants.get(int(tid or 0))
    if not kid:
        return JSONResponse({"ok": False, "message": "账号不存在"}, status_code=404)
    if int(kid.get("parent_id") or 0) != authz.tid_of(t):
        audit.log("acl", actor_name=str(t.get("username")), actor_type="tenant",
                  actor_id=authz.tid_of(t), target=str(kid.get("username")),
                  detail="越权尝试查看他人子代理密码", ip=_ip(request), ua=_ua(request), level="danger")
        return JSONResponse({"ok": False, "message": "只能查看自己名下子代理"}, status_code=403)
    slug = ensure_slug(authz.tid_of(t))
    kslug = ensure_slug(int(tid))
    pwd = get_pwd(int(tid))
    return {"ok": True, "id": int(tid), "username": str(kid.get("username") or ""),
            "password": pwd, "pwd_saved": bool(pwd), "level": 2, "level_text": "子代理商",
            "status": str(kid.get("status") or ""),
            "url": site_url(request, slug, kslug) if (slug and kslug) else ""}

@router.get("/admin/api/sites")
def admin_sites(request: Request):
    if not _admin_ok(request):
        return JSONResponse({"ok": False, "message": "未授权"}, status_code=403)
    # R81：一级 + 二级都回给总台（前端按 id 建 urlById 映射，总台列表里子代理那行才显示得出专属网址）
    rows = []
    for _lv in (1, 2):
        try:
            rows += (tenants.listing(parent_id=None, level=_lv, q="", limit=500, offset=0).get("rows") or [])
        except Exception:
            pass
    items = []
    for t in rows:
        tid = int(t.get("id") or 0)
        lv = int(t.get("level") or 0)
        try:
            n = len(tenants.children(tid)) if lv == 1 else 0
        except Exception:
            n = 0
        items.append({"id": tid, "username": str(t.get("username") or ""),
                      "status": str(t.get("status") or ""), "level": lv, "subs": n,
                      "url": _entry_url(request, t)})
    return {"ok": True, "rows": items, "total": len(items)}


@router.post("/admin/api/sites/reset")
async def admin_sites_reset(request: Request):
    if not _admin_ok(request):
        return JSONResponse({"ok": False, "message": "未授权"}, status_code=403)
    try:
        b = await request.json()
    except Exception:
        b = {}
    try:
        tid = int((b or {}).get("tid") or 0)
    except Exception:
        tid = 0
    if tid <= 0:
        return JSONResponse({"ok": False, "message": "参数不对"}, status_code=400)
    if not tenants.get(tid):
        return JSONResponse({"ok": False, "message": "账号不存在"}, status_code=404)
    slug = rotate_slug(tid)
    if not slug:
        return JSONResponse({"ok": False, "message": "生成失败，请重试"}, status_code=500)
    audit.log("acl", actor_name="总台", actor_type="admin", actor_id=0, target=str(tid),
              detail="重置代理商专属网址", ip=_ip(request), ua=_ua(request), level="warn")
    return {"ok": True, "message": "已生成新网址，旧网址立即失效", "slug": slug}


init()
