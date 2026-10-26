# -*- coding: utf-8 -*-
"""R63 多租户认证与权限（总台 L0 / 代理商 L1 / 子代理商 L2）。

只被 app/tenant_api.py 使用；主控在 main.py 里调用：
    authz.set_secret(SECRET)
    authz.set_deps(admin_ok=_admin_ok, client_ip=_client_ip, base_url=_base_url)

cookie `tn` = "<id>.<hmac_sha256(secret, 'tn:'+id)[:32]>"
    httponly / samesite=lax / path=/ / max_age=12h

依赖 app/tenants.py（线A）提供：get / get_by_name / child_ids / touch_login。
本文件不含任何租户表 SQL —— 全部走 tenants 模块，避免重复实现。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time

from app import guard, otp, store, tenants

# --------------------------------------------------------------------------- #
# 密钥（主控注入 main.py 的 SECRET）
# --------------------------------------------------------------------------- #
# 兜底密钥不再用固定字符串（固定值等于把签名密钥公开）。
# 正常路径由 main.py 的 _load_secret() 注入持久化密钥，这里只是极端兜底。
SECRET = os.environ.get("SITE_SECRET", "") or secrets.token_hex(32)

_warned = False


def set_secret(s: str):
    """主控注入 HMAC 密钥（复用 main.py 的 SITE_SECRET）。"""
    global SECRET
    if s:
        SECRET = str(s)


def _sign(v: str) -> str:
    return hmac.new(SECRET.encode("utf-8"), v.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# cookie
# --------------------------------------------------------------------------- #
TN_MAX_AGE = int(os.environ.get("TN_SESSION_HOURS", "12")) * 3600
TN_COOKIE = "tn"


# ---- R66 会话纪元 ---------------------------------------------------------- #
# 改密码 / 被上级或总台重置 / 被停用封禁 -> 纪元 +1 -> 该账号所有旧 cookie 立刻失效。
def epoch(tid) -> str:
    try:
        return str(store.get_setting("tn_epoch_%d" % int(tid)) or "0")
    except Exception:
        return "0"


def bump_epoch(tid, why: str = "") -> str:
    """把这个账号所有已登录的浏览器踢下线（返回新纪元）。"""
    try:
        tid = int(tid)
    except Exception:
        return "0"
    if tid <= 0:
        return "0"
    try:
        n = int(epoch(tid)) + 1
    except Exception:
        n = 1
    try:
        store.set_setting("tn_epoch_%d" % tid, str(n))
    except Exception:
        return epoch(tid)
    try:
        from app import audit as _a
        _a.log("session_kill", actor_type="tenant", actor_id=tid, target=str(tid),
               detail="该账号的登录状态已全部失效（%s）" % (str(why or "")[:100]), level="warn")
    except Exception:
        pass
    return str(n)


def cookie_for(tid) -> str:
    """租户 id -> cookie 值（含会话纪元）；非法 id 返回空串。"""
    try:
        tid = int(tid or 0)
    except Exception:
        return ""
    if tid <= 0:
        return ""
    e = epoch(tid)
    return "%d.%s.%s" % (tid, e, _sign("tn:%d:%s" % (tid, e)))


def cookie_valid(v) -> int:
    """校验签名 + 会话纪元，返回租户 id；非法返回 0。"""
    s = str(v or "")
    if not s or "." not in s:
        return 0
    parts = s.split(".")
    if len(parts) == 3:                       # 新格式：<id>.<epoch>.<sig>
        sid, ep, sig = parts
        if not sid.isdigit() or not ep.isdigit():
            return 0
        tid = int(sid)
        if tid <= 0 or not hmac.compare_digest(sig, _sign("tn:%s:%s" % (sid, ep))):
            return 0
        return tid if epoch(tid) == ep else 0
    if len(parts) == 2:                       # 旧格式：<id>.<sig>（仅纪元仍为 0 才认）
        sid, sig = parts
        if not sid.isdigit():
            return 0
        tid = int(sid)
        if tid <= 0 or epoch(tid) != "0":
            return 0
        return tid if hmac.compare_digest(sig, _sign("tn:%s" % sid)) else 0
    return 0


def set_cookie(resp, tid, secure: bool = False):
    """在响应上写入 tn cookie。"""
    try:
        resp.set_cookie(TN_COOKIE, cookie_for(tid), max_age=TN_MAX_AGE, httponly=True,
                        samesite="lax", path="/", secure=bool(secure))
    except Exception:
        pass
    return resp


def clear_cookie(resp):
    try:
        resp.delete_cookie(TN_COOKIE, path="/")
    except Exception:
        pass
    return resp


# --------------------------------------------------------------------------- #
# 依赖注入（避免循环 import main.py）
# --------------------------------------------------------------------------- #
_ADMIN_OK = None
_CLIENT_IP = None
_BASE_URL = None


def set_deps(admin_ok=None, client_ip=None, base_url=None):
    global _ADMIN_OK, _CLIENT_IP, _BASE_URL
    if admin_ok is not None:
        _ADMIN_OK = admin_ok
    if client_ip is not None:
        _CLIENT_IP = client_ip
    if base_url is not None:
        _BASE_URL = base_url


def client_ip(request) -> str:
    if _CLIENT_IP is not None:
        try:
            v = _CLIENT_IP(request)
            if v:
                return str(v)
        except Exception:
            pass
    try:
        return (request.client.host if request.client else "") or "?"
    except Exception:
        return "?"


def base_url(request) -> str:
    if _BASE_URL is not None:
        try:
            v = _BASE_URL(request)
            if v:
                return str(v).rstrip("/")
        except Exception:
            pass
    try:
        host = request.headers.get("host") or "127.0.0.1:8080"
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    except Exception:
        host, proto = "127.0.0.1:8080", "http"
    return "%s://%s" % (proto, host)


def is_admin(request) -> bool:
    """总台（adm cookie）校验；主控注入 _admin_ok。未注入 => 一律拒绝（安全兜底）。"""
    if _ADMIN_OK is None:
        return False
    try:
        return bool(_ADMIN_OK(request))
    except Exception:
        return False


def is_https(request) -> bool:
    try:
        if (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip() == "https":
            return True
        return (request.url.scheme or "") == "https"
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 滑动窗口限速（与 main.py 同风格，模块级，不 import main）
# --------------------------------------------------------------------------- #
_rl: dict = {}
_rl_lock = threading.Lock()


def allow(key: str, limit: int, per_sec: int) -> bool:
    """True = 放行，False = 超限。"""
    now = time.time()
    with _rl_lock:
        if len(_rl) > 8000:
            _rl.clear()
        arr = [t for t in (_rl.get(key) or []) if now - t < per_sec]
        if len(arr) >= limit:
            _rl[key] = arr
            return False
        arr.append(now)
        _rl[key] = arr
        return True


# --------------------------------------------------------------------------- #
# 登录 / 会话
# --------------------------------------------------------------------------- #
LOGIN_MAX_FAIL = int(os.environ.get("TN_LOGIN_MAX_FAIL", "8"))
LOGIN_BAN_SEC = int(os.environ.get("TN_LOGIN_BAN_SEC", str(24 * 3600)))
LOGIN_RATE = int(os.environ.get("TN_LOGIN_RATE", "30"))       # 每分钟 / 每 IP


def login(username: str, pwd: str, ip: str = "") -> tuple:
    """校验账号密码 -> (tenant|None, message)。

    · 密码错 / 账号不存在：都返回同一句「账号或密码不对」（不泄露账号是否存在）
    · banned：直接拒绝（中文原因）
    · disabled：允许登录（只读）
    """
    username = (username or "").strip()
    pwd = pwd or ""
    if not username or not pwd:
        return None, "请输入账号和密码"
    if len(username) > 64 or len(pwd) > 200:
        return None, "账号或密码不对"
    t = None
    try:
        t = tenants.get_by_name(username)
    except Exception:
        t = None
    if not t:
        try:
            guard.add_fail(ip, LOGIN_MAX_FAIL, LOGIN_BAN_SEC, reason="代理商登录失败过多")
        except Exception:
            pass
        return None, "账号或密码不对"
    stored = str(t.get("pwd_hash") or "")
    ok = False
    try:
        ok = bool(stored) and otp.verify_password(stored, pwd)
    except Exception:
        ok = False
    if not ok:
        try:
            guard.add_fail(ip, LOGIN_MAX_FAIL, LOGIN_BAN_SEC, reason="代理商登录失败过多")
        except Exception:
            pass
        return None, "账号或密码不对"
    status = str(t.get("status") or "active")
    if status == "banned":
        return None, "该账号已被封禁，请联系上级管理员"
    try:
        guard.clear_fails(ip)
    except Exception:
        pass
    try:
        tenants.touch_login(int(t.get("id") or 0), ip)
    except Exception:
        pass
    return t, ""


def from_request(request):
    """从 cookie `tn` 解析当前租户；banned / 不存在 -> None。"""
    try:
        v = request.cookies.get(TN_COOKIE) or ""
    except Exception:
        v = ""
    tid = cookie_valid(v)
    if not tid:
        return None
    try:
        t = tenants.get(tid)
    except Exception:
        t = None
    if not t:
        return None
    if str(t.get("status") or "active") == "banned":
        return None
    return t


# --------------------------------------------------------------------------- #
# 权限助手
# --------------------------------------------------------------------------- #
def level_of(t) -> int:
    try:
        return int(t.get("level") or 2)
    except Exception:
        return 2


def tid_of(t) -> int:
    try:
        return int(t.get("id") or 0)
    except Exception:
        return 0


def scope_ids(t) -> list:
    """可见租户 id 列表。L0 -> []（空列表 = 全站）；L1 -> [自己]+名下；L2 -> [自己]。"""
    if not t:
        return []
    tid = tid_of(t)
    if tid <= 0:
        return []
    lvl = level_of(t)
    if lvl <= 0:
        return []
    if lvl == 1:
        kids = []
        try:
            kids = [int(x) for x in (tenants.child_ids(tid) or [])]
        except Exception:
            kids = []
        out = [tid]
        for k in kids:
            if k not in out:
                out.append(k)
        return out
    return [tid]


def can_manage(t, target) -> bool:
    """L0 可管所有人；L1 只能管 parent_id == 自己 的 L2；L2 谁都不能管。"""
    if not t or not target:
        return False
    lvl = level_of(t)
    if lvl <= 0:
        return True
    if lvl != 1:
        return False
    try:
        return int(target.get("parent_id") or 0) == tid_of(t)
    except Exception:
        return False


def readonly(t) -> bool:
    """disabled = 可登录但只读（不许写操作）。"""
    return bool(t) and str(t.get("status") or "") == "disabled"


def owner_name(t) -> str:
    if not t:
        return "游客"
    return str(t.get("username") or ("#%d" % tid_of(t)))
