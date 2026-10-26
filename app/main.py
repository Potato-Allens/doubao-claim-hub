# -*- coding: utf-8 -*-
"""
豆包 30 天会员 · 兑换码领取系统（Docker / 服务器部署版）
========================================================
· 前台：兑换链接 http://<host>:<port>/<CODE>  → 校验通过后进入领取页（无需再次验证）
· 领取页：一个二维码 + 换二维码 + 领取成功/失败提示
· 后台：/admin  输入数量 → 批量生成兑换码 → 复制/导出链接，查看使用状态

机制：官方活动「PC阳光普照二期」按【账号 × PC设备】判定"新设备首次登录"赠 30 天；
      设备身份由请求参数决定 ⇒ 换一套设备指纹 = 换一台新电脑 ⇒ 可再次领取（已有订阅自动顺延）。

环境变量
--------
MODE            random | pool   设备码来源（pool 读 data/devices.json 预注册的真实设备码）
PORT            8080
BASE_URL        生成链接用的前缀，如 http://1.2.3.4:8080（默认按请求 Host 自动推断）
ADMIN_PASSWORD  后台密码（务必修改）
SITE_SECRET     签发 cookie 用的密钥（不设则自动生成并持久化到 data/.secret）
REQUIRE_CODE    1|0  是否强制用兑换码进入领取页（默认 1）
VERIFY_RETRY    5    到账核验重试轮数（每轮间隔 7s；福利异步到账，失败还会后台延迟复查补记）
DEMO            0|1  开启演示接口 /api/_demo（预览成功/失败样式）
CLAIMS_PER_CODE 1    每个兑换码允许成功领取的次数
ADMIN_IP_WHITELIST   例 1.2.3.4,10.0.0.0/8  预置后台 IP 白名单（后台「安全」面板里也能改）
LOGIN_BAN_SEC        登录连续失败自动封禁的秒数（默认 86400 = 24 小时）
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from urllib.parse import parse_qs

# 日志里含 emoji/中文，Windows 控制台默认 GBK 会抛错；统一切到 UTF-8
try:
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from app import guard, otp, store
from app.sessions import SM, SOURCE, claims_limit, set_logger
from app.phone_login import (valid_cn_mobile, normalize_mobile, mask_mobile,
                            parse_mobile, mobile_key, mask_any, country_list,
                            region_for_iso, CODE_TTL)

# ---- R63 多租户：三级代理体系（总台 / 代理商 / 子代理商）+ 审计 + 出口代理池 ----
_MULTI_OK = False
_MULTI_ERR = ""
try:
    from app import audit as audit_mod          # noqa: F401
    from app import authz as authz_mod          # noqa: F401
    from app import tenant_api                  # noqa: F401
    from app import tenants as tenants_mod      # noqa: F401
    _MULTI_OK = True
except Exception as _e:      # 开发期允许缺模块
    audit_mod = authz_mod = tenant_api = tenants_mod = None
    _MULTI_ERR = str(_e)[:200]

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
MODE = os.environ.get("MODE", "random").lower()
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "CHANGE_ME_PLEASE")
REQUIRE_CODE = os.environ.get("REQUIRE_CODE", "1") == "1"
# ★R87 总闸：SITE_CLOSED=1 时关闭**全部对外入口**（买家卡密页 / 总台 /admin /
# 代理商 /a / 一次性下载 /d/{token} / 所有 API），只留**本机** /healthz 供观测。
# 数据一个字节都不动；SITE_CLOSED=0（或删掉这行）+ 重启即可恢复。
SITE_CLOSED = os.environ.get("SITE_CLOSED", "0") == "1"
VERIFY_RETRY = int(os.environ.get("VERIFY_RETRY", "3"))
DEMO = os.environ.get("DEMO", "0") == "1"
CLAIMS_PER_CODE = int(os.environ.get("CLAIMS_PER_CODE", "0"))   # 0 = 不限次（一码可多次领）
AUTO_NEXT = os.environ.get("AUTO_NEXT", "0") == "1"

# ---- 安全相关 ----
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"        # 在 nginx 后开启，才信任 X-Forwarded-For
# ★R84 防 Host 头注入：兑换链接 / 子代理后台网址 / 下载链接都靠 _base_url() 拼，
#   而 Host 头客户端完全可控。配 BASE_URL 最稳（Host 不参与拼接）；其次 ALLOWED_HOSTS。
ALLOWED_HOSTS = [h.strip().lower() for h in (os.environ.get("ALLOWED_HOSTS", "") or "").split(",") if h.strip()]
_HOST_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:[]")
ADMIN_SESSION_HOURS = int(os.environ.get("ADMIN_SESSION_HOURS", "12"))   # 后台登录有效期
LOGIN_MAX_FAIL = int(os.environ.get("LOGIN_MAX_FAIL", "8"))              # N 次失败后锁
LOGIN_LOCK_SEC = int(os.environ.get("LOGIN_LOCK_SEC", "600"))            # 锁多久（秒）
CODE_RATE = int(os.environ.get("CODE_RATE", "90"))                       # 卡密入口 每分钟/每 IP
# ★R85 /healthz 默认只回 {"ok":true}，不再向公网暴露设备池余量/会话构成/卡密库存；
#   本机(127.0.0.1/::1)或 HEALTHZ_DETAIL=1 时才给详细数据（docker HEALTHCHECK 走本机）。
HEALTHZ_DETAIL = os.environ.get("HEALTHZ_DETAIL", "0") == "1"
LOGIN_BAN_SEC = int(os.environ.get("LOGIN_BAN_SEC", str(24 * 3600)))     # 登录连续失败 -> 自动封禁多久（默认 24h）
SHOW_ADMIN_REAL = os.environ.get("SHOW_ADMIN_REAL", "0") == "1"           # 1=非白名单返回 403；0=装作路径不存在(404)
# 白名单兜底：这两个路径永远可达（不然换个 IP 就再也进不去后台了）
LOGIN_ALLOW_PATHS = ("/admin/login", "/admin/api/authinfo")
# ★R85b：静态资源不在白名单里拦。代理商/子代理的控制台(console.html)会引用 /admin.css，
#   被白名单拦掉页面就掉样式（实测多个代理商设备 IP 都吃 404）。这两个是纯前端代码、零密钥。
STATIC_ALLOW_PATHS = ("/admin.css", "/admin.js")

QR_TTL = 60
REFRESH_BELOW = 18
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))


def _load_secret() -> str:
    env = os.environ.get("SITE_SECRET")
    if env:
        return env
    os.makedirs(DATA_DIR, exist_ok=True)
    p = os.path.join(DATA_DIR, ".secret")
    try:
        if os.path.exists(p):
            s = open(p, encoding="utf-8").read().strip()
            if s:
                return s
        s = secrets.token_hex(32)
        with open(p, "w", encoding="utf-8") as f:
            f.write(s)
        return s
    except Exception:
        return secrets.token_hex(32)


SECRET = _load_secret()
store.init()

app = FastAPI(title="Doubao Claim", docs_url=None, redoc_url=None, openapi_url=None)
# ★R85：原来只关了 /docs 与 /redoc，FastAPI 默认还挂着一个 /openapi.json，
#   公网裸访问即可拿到全部 91 个接口（含 /admin/api/* 与 /a/api/*）的路径与参数表。


def _log(msg: str):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


set_logger(_log)


# --------------------------------------------------------------------------- #
# R66 一次性下载链接（/d/<token>）
#   必须在这里注册：末尾的通配 /{code} 会把后注册的单段路由全部吃掉。
# --------------------------------------------------------------------------- #
_R66_DL = False
try:
    from app import dl as dl_mod
    dl_mod.init()
    app.include_router(dl_mod.router)
    _R66_DL = True
except Exception as _dle:      # 开发期允许缺模块，绝不拖垮主站
    dl_mod = None
    _log("✗ R66 下载链接模块未加载：%s" % str(_dle)[:200])


# --------------------------------------------------------------------------- #
# cookie 签名
# --------------------------------------------------------------------------- #
def _sign(v: str) -> str:
    return hmac.new(SECRET.encode(), v.encode(), hashlib.sha256).hexdigest()[:32]


def _ticket(code: str) -> str:
    return "%s.%s" % (code, _sign("ticket:" + code))


def _ticket_code(ticket: str) -> str:
    if not ticket or "." not in ticket:
        return ""
    code, sig = ticket.rsplit(".", 1)
    return code if hmac.compare_digest(sig, _sign("ticket:" + code)) else ""


def _pwd_hash(p: str) -> str:
    """后台密码指纹：改动 SECRET 或密码都会让旧 cookie 失效。"""
    return hashlib.sha256((SECRET + "|pwd|" + p).encode()).hexdigest()


def _admin_cred() -> str:
    """当前有效的后台密码凭据（改过密码就用库里的，否则用环境变量）。"""
    stored = store.get_setting("admin_password_hash")
    return stored or ("env:" + ADMIN_PASSWORD)


def _admin_pwd_ok(p: str) -> bool:
    stored = store.get_setting("admin_password_hash")
    if stored:
        if stored.startswith("pbkdf2$"):
            return otp.verify_password(stored, p)
        return hmac.compare_digest(stored, _pwd_hash(p))       # 兼容旧格式（登录成功后自动升级）
    return hmac.compare_digest(p, ADMIN_PASSWORD)


# --------------------------------------------------------------------------- #
# 安全基建：真实 IP / 限速 / https 判断 / 谷歌验证器(2FA)
# --------------------------------------------------------------------------- #
def _client_ip(request: Request) -> str:
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for") or ""
        if xff:
            return xff.split(",")[0].strip()
        xr = request.headers.get("x-real-ip")
        if xr:
            return xr.strip()
    return (request.client.host if request.client else "") or "?"


_rl: dict = {}
_rl_lock = threading.Lock()


def _rl_prune(now: float) -> None:
    """★R211：表满只淘汰陈旧键，绝不整体 clear（否则 8000 个新键就能重置全站限速）。"""
    if len(_rl) <= 8000:
        return
    for k in [k for k, v in _rl.items() if not v or now - v[-1] > 3600]:
        _rl.pop(k, None)
    while len(_rl) > 8000:
        _rl.pop(next(iter(_rl)), None)


def _rate_limit(key: str, limit: int, per_sec: int) -> bool:
    """滑动窗口限速：True = 放行，False = 超限。"""
    now = time.time()
    with _rl_lock:
        _rl_prune(now)
        arr = [t for t in (_rl.get(key) or []) if now - t < per_sec]
        if len(arr) >= limit:
            _rl[key] = arr
            return False
        arr.append(now)
        _rl[key] = arr
        return True


def _is_https(request: Request) -> bool:
    if BASE_URL.startswith("https://"):
        return True
    # ★R84：只有明确配了 TRUST_PROXY（确实挂在反代后面）才认这个头，否则客户端可随意伪造
    if TRUST_PROXY and (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower() == "https":
        return True
    return request.url.scheme == "https"


# ★R487_ADMGATE：分布式撞库全局熔断（白名单 IP 不受影响，避免把自己锁在外面）
_ADM_GATE = {"n": 0, "t0": 0.0, "until": 0.0}
_ADM_GATE_LOCK = threading.Lock()


def _adm_gate_blocked(ip: str) -> bool:
    try:
        if guard.allowed(ip):
            return False
    except Exception:
        pass
    now = time.time()
    with _ADM_GATE_LOCK:
        if now < _ADM_GATE["until"]:
            return True
    # ★R497_SUBGATE
    key = _sub24(ip)
    if key:
        with _ADM_SUB_LOCK:
            until = float((_ADM_SUB.get(key) or {}).get("until") or 0)
        if now < until:
            return True
        try:
            if now < float(_adm_sub_load().get(key) or 0):
                return True
        except Exception:
            pass
    return False


def _adm_gate_fail(ip: str) -> None:
    try:
        if guard.allowed(ip):
            return
    except Exception:
        pass
    now = time.time()
    with _ADM_GATE_LOCK:
        if now - _ADM_GATE["t0"] > 300:
            _ADM_GATE["t0"] = now
            _ADM_GATE["n"] = 0
        _ADM_GATE["n"] += 1
        if _ADM_GATE["n"] >= 30:
            _ADM_GATE["until"] = now + 180
            _ADM_GATE["n"] = 0
            _ADM_GATE["t0"] = now
            _log("🛑 后台登录全局熔断：5 分钟内非白名单失败 30 次 → 暂停 3 分钟")
    # ★R497_SUBGATE：同段多 IP 协同撞库 → 段级熔断
    try:
        _k = _sub24(ip)
        if _k:
            _adm_sub_fail(_k, ip, now)
    except Exception:
        pass


def _adm_gate_reset() -> None:
    with _ADM_GATE_LOCK:
        _ADM_GATE.update({"n": 0, "t0": 0.0, "until": 0.0})


# ★R497_SUBGATE：网段级熔断。
# 单 IP 失败 8 次就自动封，但撞库方换一个 IP 就能继续；
# 实测出现 198.51.100.0/24 一个段 20 个 IP 轮留试。这里只作用于 /admin/login，
# 客户站不受影响；白名单 IP 完全免疫。
_ADM_SUB = {}
_ADM_SUB_LOCK = threading.Lock()
_ADM_SUB_WIN = 600          # 统计窗口 10 分钟
_ADM_SUB_MIN_FAIL = 12      # 窗口内失败总数
_ADM_SUB_MIN_IPS = 4        # 且至少多少个不同 IP
_ADM_SUB_BAN = 1800         # 整段停 30 分钟
_ADM_SUB_KEY = "adm_sub_bans"


def _sub24(ip: str) -> str:
    try:
        p = str(ip or "").split(".")
        if len(p) != 4 or not all(x.isdigit() and 0 <= int(x) <= 255 for x in p):
            return ""
        return ".".join(p[:3]) + ".0/24"
    except Exception:
        return ""


def _adm_sub_load() -> dict:
    try:
        d = json.loads(store.get_setting(_ADM_SUB_KEY) or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _adm_sub_save(d: dict) -> None:
    try:
        now = time.time()
        d = {k: v for k, v in d.items() if float(v or 0) > now}
        store.set_setting(_ADM_SUB_KEY, json.dumps(d, ensure_ascii=False))
    except Exception:
        pass


def _adm_sub_fail(key: str, ip: str, now: float) -> None:
    trigger = False
    with _ADM_SUB_LOCK:
        d = _ADM_SUB.get(key)
        if not d or now - float(d.get("first") or 0) > _ADM_SUB_WIN:
            d = {"first": now, "ips": {}, "n": 0, "until": 0.0}
            _ADM_SUB[key] = d
        d["n"] = int(d.get("n") or 0) + 1
        ips = d.setdefault("ips", {})
        ips[ip] = int(ips.get(ip) or 0) + 1
        if now >= float(d.get("until") or 0) and d["n"] >= _ADM_SUB_MIN_FAIL and len(ips) >= _ADM_SUB_MIN_IPS:
            d["until"] = now + _ADM_SUB_BAN
            trigger = True
        n_ips, n_fail = len(ips), d["n"]
        if len(_ADM_SUB) > 400:
            for k in [k for k, v in _ADM_SUB.items() if now >= float(v.get("until") or 0)
                      and now - float(v.get("first") or 0) > _ADM_SUB_WIN]:
                _ADM_SUB.pop(k, None)
    if trigger:
        _log("🛑 网段熔断：%s 在 %d 秒内 %d 个 IP 失败 %d 次 → 整段停 %d 分钟"
             % (key, _ADM_SUB_WIN, n_ips, n_fail, _ADM_SUB_BAN // 60))
        sd = _adm_sub_load()
        sd[key] = now + _ADM_SUB_BAN
        _adm_sub_save(sd)


def _fail_login(ip: str, why: str, request: Request):
    _log("⚠ 后台登录失败(%s) ip=%s ua=%s" % (why, ip, (request.headers.get("user-agent") or "")[:70]))
    n = guard.add_fail(ip, LOGIN_MAX_FAIL, LOGIN_BAN_SEC)
    if n >= LOGIN_MAX_FAIL:
        _log("🛑 该 IP 后台登录失败 %d 次，已自动封禁 %d 分钟：%s" % (n, LOGIN_BAN_SEC // 60, ip))
    _adm_gate_fail(ip)


def _totp_enabled() -> bool:
    return store.get_setting("totp_enabled") == "1" and bool(store.get_setting("totp_secret"))


def _use_recovery(code: str) -> bool:
    """恢复码一次性校验：命中即从库里删掉。"""
    code = (code or "").strip()
    if not code:
        return False
    try:
        left = json.loads(store.get_setting("recovery_hashes") or "[]")
    except Exception:
        left = []
    h = otp.recovery_hash(code)
    if h in left:
        left.remove(h)
        store.set_setting("recovery_hashes", json.dumps(left))
        _log("⚠ 使用了一个恢复码（剩余 %d 个）" % len(left))
        return True
    return False


def _check_2fa(code: str) -> bool:
    """密码正确后校验第二因子：动态码 或 恢复码。★R211：同一时间片只认一次（防重放）。"""
    ctr = otp.match_counter(store.get_setting("totp_secret"), code)
    if ctr >= 0:
        try:
            last = int(store.get_setting("totp_last_counter") or 0)
        except Exception:
            last = 0
        if ctr <= last:
            _log("⚠ 后台 2FA 动态码重放被拒（counter=%d <= %d）" % (ctr, last))
            return False
        store.set_setting("totp_last_counter", str(ctr))
        return True
    return _use_recovery(code)


def _admin_cookie() -> str:
    """★R84：cookie 里带签发时间戳 + HMAC，服务端能真正判过期（旧版是个永不过期的静态串）。"""
    ts = int(time.time())
    return "%d.%s" % (ts, _sign("admin:%s:%d" % (_admin_cred(), ts)))


def _owner_scope(owner_id):
    """R711：「只看某代理」→ 该账号 + 其直接下级的 id 列表；0/非法 = 全站(None)。

    历史坑：前端 console.js 一直在传 owner_id，但 /admin/api/logs 与 /admin/api/list
    没接这个参数，FastAPI 静默忽略，于是「只看 a005」之后表格还是全站 57358 条/1148 页。
    """
    try:
        oid = int(owner_id or 0)
    except Exception:
        oid = 0
    if oid <= 0:
        return None
    try:
        from app.tenants import scope_ids as _scope_ids
        ids = sorted({int(x) for x in _scope_ids(oid) if int(x or 0) > 0})
        return ids or [oid]
    except Exception:
        return [oid]


def _admin_ok(request: Request) -> bool:
    """校验 adm cookie：格式/签名/有效期（ADMIN_SESSION_HOURS）三重检查。"""
    c = request.cookies.get("adm") or ""
    if not c or "." not in c:
        return False
    ts, sig = c.split(".", 1)
    if not ts.isdigit():
        return False
    age = time.time() - int(ts)
    if age < -300 or age > 3600 * ADMIN_SESSION_HOURS:      # 未来时间 / 超期 -> 一律无效
        return False
    return hmac.compare_digest(sig, _sign("admin:%s:%s" % (_admin_cred(), ts)))


# --------------------------------------------------------------------------- #
# 状态机：见 app/sessions.py（每兑换码一套设备指纹 + 一条独立扫码会话）
# --------------------------------------------------------------------------- #
def snapshot(code: str = "") -> dict:
    """兼容旧接口：带 code 返回该会话快照，不带则返回全局概览。"""
    if code:
        s = SM.get(code)
        return s.snapshot() if s else {"ok": False, "message": "会话不存在"}
    return {"ok": True, "sessions": SM.stats(), "pool_left": SOURCE.remaining()}


# --------------------------------------------------------------------------- #
# 页面渲染
# --------------------------------------------------------------------------- #
# 静态资源版本号：手机浏览器会把 CSS/JS 长期缓存，改了样式它却还拿旧的（踩过坑），
# 所以页面里的 /xxx.css /xxx.js 一律带上 ?v=<mtime+size>，文件一改版本号就变。
_ASSET_FILES = ("style.css", "admin.css", "console.css", "app.js", "admin.js", "console.js")


def _asset_ver(fn: str) -> str:
    try:
        st = os.stat(os.path.join(STATIC_DIR, fn))
        return "%x%x" % (int(st.st_mtime), int(st.st_size) & 0xFFFFF)
    except Exception:
        return "0"


def _build_id() -> str:
    """★R283_BUILD_WATCH：前端构建号（app.js 的 mtime+size）。热更新 JS 后，开着的旧页面会自动刷新一次。"""
    return _asset_ver("app.js")


def _stamp_assets(html: str) -> str:
    for fn in _ASSET_FILES:
        html = html.replace('"/%s"' % fn, '"/%s?v=%s"' % (fn, _asset_ver(fn)))
    return html


def _page(name: str, media: str = "text/html", versioned: bool = False):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as f:
        html = f.read()
    if media == "text/html":
        html = _stamp_assets(html)
        # ★R282b/283：手机浏览器/微信 webview 会无视 no-cache 缓存 HTML，导致 ?v= 不更新、前端热修永远收不到
        return HTMLResponse(html, media_type=media, headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache", "Expires": "0"})
    # ★R299_ASSET_CACHE：只有带 ?v= 的请求才长缓存。
    #   旧页面/webview 里仍是 <script src="/app.js">；若这里继续回 7 天缓存，
    #   前端热修对老客户端永远不生效（会一直显示几天前的旧红字）。
    if not versioned:
        return HTMLResponse(html, media_type=media, headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache", "Expires": "0"})
    return HTMLResponse(html, media_type=media,
                        headers={"Cache-Control": "public, max-age=604800"})


def _asset(name: str, media: str, request: Request):
    """★R299_ASSET_ETAG：带 ?v= 长缓存；不带 ?v= 用 ETag 协商（no-cache + 304）。
    既保证老页面/老 webview 能拿到前端热修，又不会每次都重下 9KB gzip。"""
    if request.query_params.get("v"):
        return _page(name, media, versioned=True)
    ver = _asset_ver(name)
    etag = '"%s"' % ver
    headers = {"ETag": etag,
               "Cache-Control": "no-cache, must-revalidate, max-age=0",
               "Pragma": "no-cache", "Expires": "0"}
    if (request.headers.get("if-none-match") or "").strip() == etag:
        return Response(status_code=304, headers=headers)
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as f:
        body = f.read()
    return Response(content=body, media_type=media, headers=headers)


def _msg_page(title: str, desc: str, icon: str = "🙈", status_code: int = 200,
              back: str = "/", back_text: str = "返回首页"):
    html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>%s</title>
<link rel="stylesheet" href="/style.css"></head><body>
<div class="aurora"></div><div style="min-height:70vh;display:grid;place-items:center;width:100%%">
<div class="card" style="max-width:420px;text-align:center;padding:36px 28px">
<div style="font-size:52px;line-height:1">%s</div>
<h2 style="margin:14px 0 8px;font-size:19px">%s</h2>
<p class="dim" style="margin:0;line-height:1.8">%s</p>
<div style="margin-top:20px"><a class="btn btn--primary" style="text-decoration:none" href="%s">%s</a></div>
</div></div></body></html>""" % (title, icon, title, desc, back, back_text)
    return HTMLResponse(html, status_code=status_code)


def _plain_page(title: str, desc: str, icon: str, status_code: int):
    """极简拦截页：不依赖任何静态资源，也不泄露站点信息。"""
    html = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>%s</title></head><body style=\"margin:0;background:#0b0d12;color:#e7e9ee;"
            "font:15px/1.7 system-ui,'Segoe UI',sans-serif\">"
            "<div style=\"min-height:80vh;display:grid;place-items:center;text-align:center;padding:24px\">"
            "<div><div style=\"font-size:46px;line-height:1\">%s</div>"
            "<h2 style=\"margin:12px 0 6px;font-size:18px\">%s</h2>"
            "<p style=\"margin:0;color:#98a0b3\">%s</p></div></div></body></html>") % (title, icon, title, desc)
    return HTMLResponse(html, status_code=status_code)


def _has_success(code: str) -> bool:
    """★R701：这张码是否已经成功领取过（查 events，不依赖内存会话）。"""
    try:
        from app.sessions import last_success_detail
        return bool(last_success_detail(code))
    except Exception:
        return False


def _done_snap(code: str, rec: dict) -> dict:
    """★R691：已领取（done）的码直接给 success 快照 —— 不建会话、不出码。

    只用于 /api/state 的只读回包；快照字段与 Session.snapshot() 对齐，前端无需改动。
    """
    # ★R699：把最后一次 success 的账号/到期从库里带回来，避免重启后成功页丢信息
    _inf = {}
    try:
        from app.sessions import last_success_detail as _lsd
        _inf = _lsd(code) or {}
    except Exception:
        _inf = {}
    return {
        "ok": True, "status": "success",
        "message": (("🎉 领取成功！本码已完成领取（到期 %s，北京时间）"
                     % _inf["after_expiry"]) if _inf.get("after_expiry")
                    else "🎉 领取成功！本码已完成领取（该卡密只能领取一次）"),
        "remain": 0, "seq": 0, "qr_stale": True, "qr_paused": True,
        "verify_elapsed": 0, "verify_attempt": 0, "verify_total": 0,
        "relogin_required": False, "qr": None, "device": None,
        "detail": {}, "history": [], "mode": MODE,
        "pool_left": SOURCE.remaining(), "pool_total": SOURCE.pool_total,
        "auto_next": AUTO_NEXT, "code": code,
        "claims": max(int((rec or {}).get("claims") or 0), 1), "max_claims": 1,
        "claimed": True, "account": _inf.get("account") or "", "uid": _inf.get("uid") or "",
        "before_member": "", "before_expiry": "",
        "after_member": _inf.get("after_member") or "", "after_expiry": _inf.get("after_expiry") or "",
        "member": _inf.get("member") or "", "expiry": _inf.get("expiry") or "",
        "done_at": _inf.get("done_at") or "",
        "retrying": False, "final_fail": False,
        "late_tries": 0, "late_total": 0, "late_next_in": 0, "can_recheck": False,
        "region": "", "acct_iso": "", "exit_cc": "", "acct_iso_src": "",
        "phone": {"number": "", "state": "idle", "cc": "", "cooldown": 0, "ttl": CODE_TTL},
        "build": _build_id(),
    }


def _done_op_reply(code: str, msg: str = "该兑换码已完成领取，无需再操作"):
    """★R692b：没有会话但库里已完成 → 给手机号/复查接口一个中性 done 回包。"""
    try:
        rec = store.get(code) or {}
    except Exception:
        rec = {}
    if str(rec.get("status") or "") != "done" and not _has_success(code):
        return None
    out = _done_snap(code, rec)
    out["op"] = {"ok": False, "done": True, "need_qr": False,
                 "verify_subtype": "", "ec": None, "message": msg}
    return JSONResponse(out)


def _sess(code: str):
    """取会话。免码模式（REQUIRE_CODE=0）下没有 ticket 就归到一个固定会话，避免 500。

    ★R209d：停用/已完成的码不允许再凭旧 ticket 重建会话。
    否则买家页面已打开时，后台点「停用」后 SM.kill 只是删了内存，
    下一次轮询又会 SM.get(code) 新建一个会话，等于没停。
    """
    if code:
        s = SM.get(code, create=False)
        if s is not None:
            return s
        if REQUIRE_CODE:
            try:
                rec = store.get(code) or {}
                if not rec or str(rec.get("status") or "") in ("disabled", "done"):
                    return None
            except Exception:
                pass
        return SM.get(code)
    return None if REQUIRE_CODE else SM.get("FREE")



# --------------------------------------------------------------------------- #
# 兑换码入口
# --------------------------------------------------------------------------- #
def _host_ok(h: str) -> str:
    """Host 头白名单式清洗：只接受 <IP|主机名>[:端口]，含任何怪字符一律判废。"""
    h = (h or "").strip()
    if not h or len(h) > 120:
        return ""
    if any(ch not in _HOST_CHARS for ch in h):
        return ""
    if h.startswith(".") or h.endswith(".") or ".." in h:
        return ""
    head, sep, port = h.partition(":")
    if not head:
        return ""
    if sep and (not port.isdigit() or not (0 < int(port) < 65536)):
        return ""
    return h


def _base_url(request: Request) -> str:
    """对外链接前缀。优先 BASE_URL > ALLOWED_HOSTS > 清洗过的 Host > 安全兜底。

    ★R84：Host 头可被伪造，用它拼链接会把「兑换链接 / 子代理后台网址 / 下载链接」
    污染成攻击者域名（钓鱼）。所以这里做三重收敛，绝不原样回显未校验的 Host。
    """
    if BASE_URL:
        return BASE_URL
    host = _host_ok(request.headers.get("host") or "")
    if ALLOWED_HOSTS:
        if host.lower() not in ALLOWED_HOSTS:
            host = ALLOWED_HOSTS[0]
    if not host:
        host = "127.0.0.1:" + str(os.environ.get("PORT", "8080"))
    proto = "http"
    if TRUST_PROXY and (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower() == "https":
        proto = "https"
    elif request.url.scheme == "https":
        proto = "https"
    return "%s://%s" % (proto, host)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if _ticket_code(request.cookies.get("ticket") or ""):
        return RedirectResponse("/claim?k=" + _ticket_code(request.cookies.get("ticket") or ""))
    return _page("landing.html")


@app.get("/claim")
def claim(request: Request):
    code = _ticket_code(request.cookies.get("ticket") or "")
    _k = (request.query_params.get("k") or "").strip().upper()
    # ★R303：cookie 丢了也能用链接里的 k 重新进来（下面会把 ticket 补回去）
    if REQUIRE_CODE and not code and _k:
        _rec0 = store.get(_k)
        # ★R701：已领取成功的码即使被停用，也要能用链接重新打开成功页
        if _rec0 and (_rec0.get("status") != "disabled" or _has_success(_k)):
            code = _k
    if REQUIRE_CODE and not code:
        return _msg_page("需要兑换码", "请使用你收到的专属兑换链接打开本页面。", "🔑", 403)
    # ★R210：裸 /claim 一律拒绝——必须带 ?k=<兑换码> 从专属链接跳进来
    if REQUIRE_CODE and _k != code:
        return _msg_page("需要兑换码", "请使用你收到的专属兑换链接打开本页面。", "🔑", 403)
    rec = store.get(code) if code else None
    # ★R701：已领完（或被停用但已领到）的码，也把页面给它 —— 让 /api/state 的 success 快照
    # （账号 / 到期时间）显示出来；否则买家点开链接只看到一张「已完成领取」的静态页。
    _done_like = bool(rec and (str(rec.get("status") or "") == "done" or _has_success(code)))
    if REQUIRE_CODE and _done_like:
        _resp = _page("index.html")
        if code and not _ticket_code(request.cookies.get("ticket") or ""):
            _resp.set_cookie("ticket", _ticket(code), max_age=86400 * 7, httponly=True,
                             samesite="lax", secure=_is_https(request), path="/")
        return _resp
    if REQUIRE_CODE and (not rec or str(rec.get("status") or "") == "disabled"):
        return _msg_page("兑换码无效", "该兑换码不存在或已被停用。", "🚫", 403)
    sess = _sess(code)
    if sess is None:
        return _msg_page("需要兑换码", "请使用你收到的专属兑换链接打开本页面。", "🔐", 403)
    sess.note_meta(_client_ip(request), request.headers.get("user-agent") or "")
    sess.ensure()
    _resp = _page("index.html")
    # ★R303：上面是靠 k 兜底进来的（没 cookie）→ 把 ticket 补回去，后续接口不再走兜底。
    if REQUIRE_CODE and code and not _ticket_code(request.cookies.get("ticket") or ""):
        _resp.set_cookie("ticket", _ticket(code), max_age=86400 * 7, httponly=True,
                         samesite="lax", secure=_is_https(request), path="/")
    return _resp


def redeem(code: str, request: Request):   # 通配路由，必须在最后注册（见文件末尾）
    ip = _client_ip(request)
    if not _rate_limit("code:" + ip, CODE_RATE, 60):
        _log("⚠ 卡密入口被限速 ip=%s" % ip)
        return _msg_page("访问过于频繁", "请稍后再试。", "🛑", 429)
    code = (code or "").strip().upper()
    if not code or len(code) > 32 or not code.isalnum():
        return _msg_page("兑换码无效", "链接格式不正确，请核对后重试。", "🚫", 404)
    rec = store.get(code)
    if not rec:
        return _msg_page("兑换码不存在", "请确认链接是否完整，或联系发卡方。", "🚫", 404)
    # ★R701：已完成 / 已停用但已领到 → 直接送到领取页，让买家看到自己的成功详情
    if str(rec.get("status") or "") == "done" or _has_success(code):
        _resp = RedirectResponse("/claim?k=" + code, status_code=302)
        _resp.set_cookie("ticket", _ticket(code), max_age=86400 * 7, httponly=True,
                         samesite="lax", secure=_is_https(request), path="/")
        return _resp
    if rec.get("status") == "disabled":
        return _msg_page("兑换码已停用", "该兑换码已被停用，请联系发卡方。", "🚫", 403)
    # ★R85：原来这里直接信 X-Real-IP / X-Forwarded-For（客户端随手可伪造），
    #   卡密记录里的来源 IP 能被写成任意值（污染审计）。统一走 _client_ip（尊重 TRUST_PROXY）。
    ip = _client_ip(request)
    ua = request.headers.get("user-agent") or ""
    if not store.touch(code, ip, ua):
        return _msg_page("兑换码不可用", "该兑换码已完成领取或已被停用。", "🚫", 403)
    SM.note(code, ip, ua)
    store.log_event(code, "enter", ip=ip, ua=ua, note="打开兑换链接进入领取页",
                    owner_id=int(rec.get("owner_id") or 0))
    sess = _sess(code)
    if sess is None:
        return _msg_page("兑换码不可用", "该兑换码已被停用。", "🚫", 403)
    sess.note_meta(ip, ua)
    sess.ensure()
    resp = RedirectResponse("/claim?k=" + code, status_code=302)
    resp.set_cookie("ticket", _ticket(code), max_age=86400 * 7, httponly=True, samesite="lax",
                    secure=_is_https(request), path="/")
    _log("兑换码 %s 通过校验（ip=%s）" % (code, ip))
    return resp


# --------------------------------------------------------------------------- #
# 领取页 API（需 ticket）
# --------------------------------------------------------------------------- #
def _need_ticket(request: Request):
    code = _ticket_code(request.cookies.get("ticket") or "")
    if REQUIRE_CODE and not code:
        # ★R303_K_FALLBACK：部分手机浏览器/微信 webview 会丢 cookie，或用户把链接复制到另一个 App 打开；
        # 此时 /claim?k= 页面能出来但所有接口都 403，用户看到的就是「页面没了」。
        # 兑换码本身就是链接里的凭证，这里允许 k 兜底（仍校验存在且未停用，并限速防暴力猜）。
        k = (request.query_params.get("k") or "").strip().upper()
        if k and len(k) <= 32 and k.isalnum():
            # ★R549_KFB_BUDGET：k 兜底限速 120→600/分钟。
            # 每张页面 1.5s 轮询 ≈ 40 次/分钟；旧阈值下「同一 WiFi 3 个用户 + webview 丢 cookie」
            # 会一起撞 120 上限，之后每次轮询都 403「需要兑换码」= 买家眼里的「页面没了」。
            if not _rate_limit("kfb:%s" % _client_ip(request), 600, 60):
                raise HTTPException(status_code=429, detail="请求过快，请稍候再试")
            _rec = store.get(k)
            # ★R701：已领取成功的码即使被停用，也要让买家能打开自己的成功页
            if _rec and (_rec.get("status") != "disabled" or _has_success(k)):
                code = k
    # ★R442：卡密被停用/删除 -> 立刻踢掉在途会话并 403。
    # 旧逻辑只有 k 兜底路径查了 status，cookie 路径会把「已停用」的码继续当活会话用：
    # 页面开着就一直轮询 /api/state → 每 50 秒重出一张码，代理商点「停用」等于没停。
    if REQUIRE_CODE and code:
        try:
            _rc = store.get(code)
        except Exception:
            _rc = "?"
        if _rc != "?" and (not _rc or str((_rc or {}).get("status") or "") == "disabled"):
            # ★R701：已经成功领取过的码 —— 停用只该挡住「还没领到的人」，
            # 不能把买家的成功页一起抹掉（历史上 94 张码踩过这个坑）。
            if _has_success(code):
                _log("[%s] R701 该码已停用但已领取成功 → 保留成功页（不踢会话、不 403）" % code)
            else:
                try:
                    SM.kill(code)
                except Exception:
                    pass
                raise HTTPException(status_code=403, detail="该兑换码已被停用")
    if REQUIRE_CODE and not code:
        raise HTTPException(status_code=403, detail="需要兑换码")
    return code


# ★R86：同步 def 会占 anyio 线程池（默认只有 40 个 token）——出码/登录把池子占满时，
# 连「看一眼状态」都要排队，买家页面就整体卡住。snapshot() 纯内存 + RLock（微秒级），
# 放事件循环里跑最合适，所以改成 async。
@app.get("/api/state")
async def api_state(request: Request, qs: int = 0):
    code = _need_ticket(request)
    # ★R86c：限速键从「纯 IP」改成「兑换码 + IP」分桶。
    # 原来一群买家连同一个 WiFi（同一公网 IP）会互相挤爆（每人 40 次/分钟，15 个人就顶到 600），
    # 前端拿到 429 只把状态标成「网络不太稳定」、不再刷新 ⇒ 买家扫了码页面也不动 = 看起来「卡/扫不上」。
    # 分桶后同一 IP 上每个兑换码各用各的配额，同 WiFi 的其他人不再互相影响。
    if not _rate_limit("state:%s:%s" % (code or "-", _client_ip(request)), 600, 60):
        _log("⚠ /api/state 限速 429 code=%s ip=%s" % (code or "-", _client_ip(request)))
        raise HTTPException(status_code=429, detail="请求过快")
    sess = _sess(code)
    if sess is None:
        try:
            _rec = store.get(code) or {}
        except Exception:
            _rec = {}
        # ★R701：只要这张码有过 success，不管库里是 done / disabled / 其它状态，
        # 一律给成功快照 —— 买家的成功页不能被任何后台操作抹掉（历史 94 张码踩过）。
        if _rec.get("status") == "done" or (_rec and _has_success(code)):
            return JSONResponse(_done_snap(code, _rec))
        if _rec.get("status") == "disabled":
            raise HTTPException(status_code=403, detail="该兑换码已被停用")
        raise HTTPException(status_code=403, detail="需要兑换码")
    # ★R249_STATE_ENSURE：站点重启/会话被 GC 后，前端只会轮询 /api/state，不会重新进 /claim。
    # 旧逻辑这里只 get-or-create，不触发出码 → 页面永远停在「二维码生成中…」（线上「卡住」的来源之一）。
    try:
        sess.ensure()
    except Exception:
        pass
    sess.note_meta(_client_ip(request), request.headers.get("user-agent") or "")
    snap = sess.snapshot()
    # R214: 客户端已持有同一 seq 的二维码 → 不再重复下发 ~45KB base64（人多时省 95% 带宽）
    try:
        if qs and int(qs) == int(snap.get("seq") or 0):
            snap["qr"] = None
    except Exception:
        pass
    # ★R283：带上构建号，前端发现与服务端不一致就自动 reload（拿新 app.js）
    try:
        snap["build"] = _build_id()
    except Exception:
        pass
    return JSONResponse(snap)


# ★R86：同样改 async，并且入口换成 sess.rotate_now() —— 立刻返回、出码丢后台线程。
# 老实现同步忙等 12s 再串行出码，单个请求最坏占死一个线程 76s，几个「换一张」就能拖垮全站。
@app.post("/api/next")
async def api_next(request: Request):
    code = _need_ticket(request)
    # ★R86c：同 /api/state —— 按「兑换码 + IP」分桶，别让同 WiFi 的一群人互相抢配额。
    if not _rate_limit("next:%s:%s" % (code or "-", _client_ip(request)), 40, 60):
        _log("⚠ /api/next 限速 429 code=%s ip=%s" % (code or "-", _client_ip(request)))
        raise HTTPException(status_code=429, detail="请求过快")
    sess = _sess(code)
    if sess is None:
        try:
            _rec = store.get(code) or {}
        except Exception:
            _rec = {}
        # ★R701：done 或「有过 success」（含停用）一律回已达上限
        if _rec.get("status") == "done" or (_rec and _has_success(code)):
            return JSONResponse({"ok": False, "message": "该兑换码已达可领次数上限，请换新码"})
        raise HTTPException(status_code=403, detail="需要兑换码")
    sess.note_meta(_client_ip(request), request.headers.get("user-agent") or "")
    with sess.lock:
        if sess._locked_done():
            return JSONResponse({"ok": False, "message": "该兑换码已达可领次数上限，请换新码"})
    return JSONResponse(sess.rotate_now())


# --------------------------------------------------------------------------- #
# 手机号通道 API（买家自己的手机号 + 短信验证码登录领取）
#   契约：响应统一 = dict(sess.snapshot()) + {"op": {"ok": bool, "message": str}}
# --------------------------------------------------------------------------- #
async def _phone_body(request: Request) -> dict:
    """容错解析请求体：拿不到 JSON 就当空字典。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    return body if isinstance(body, dict) else {}


def _phone_reply(sess, op: dict) -> JSONResponse:
    """手机号通道统一响应：整份快照 + op（前端不用改就能画）。"""
    out = dict(sess.snapshot())
    out["op"] = {"ok": bool(op.get("ok")), "message": str(op.get("message") or ""),
                 "need_qr": bool(op.get("need_qr")),
                 "done": bool(op.get("done")),          # ★R692：已完成领取 → 前端锁手机号面板
                 "verify_subtype": str(op.get("verify_subtype") or ""),
                 # ★R281_REPLY_EC：不能在这里把 ec 丢掉，否则前端拿不到 1203 只能靠文案匹配
                 "ec": op.get("ec")}
    return JSONResponse(out)


@app.post("/api/phone/send")
async def api_phone_send(request: Request):
    code = _need_ticket(request)
    ip = _client_ip(request)
    body = await _phone_body(request)
    raw = str(body.get("phone") or "").strip()
    _iso, _dial, _nat, _ok = parse_mobile(raw)
    if not _ok:
        _log("[%s] 手机号通道 发码被拒：号码格式不合法 ip=%s" % (code or "-", ip))
        return JSONResponse(status_code=400,
                            content={"ok": False, "cc": _iso,
                                     "message": "请输入正确的手机号（可带国家前缀，如 +1 5551234567；不确定的话请手动选国家）"})
    m = mobile_key(raw)
    # ★R86c：原来「每 IP 8 次/小时」——同一 WiFi 的第 9 个人就被关在门外
    # （线上历史日志实测 `发码限速` 命中 140 次，手机号通道总调用 1659 次）。
    # 改成「兑换码 + IP」分桶：每个买家的码 8 次/小时，同 WiFi 的其他人互不挤占。
    if not _rate_limit("phs:%s:%s" % (code or "-", ip), 8, 3600):
        _log("[%s] 手机号通道 发码限速(IP) ip=%s" % (code or "-", ip))
        raise HTTPException(status_code=429, detail="发码过于频繁，请稍后再试")
    if not _rate_limit("phm:" + m, 5, 86400):
        _log("[%s] 手机号通道 发码限速(号码) %s" % (code or "-", mask_any(raw)))
        raise HTTPException(status_code=429, detail="该手机号今日发码次数已达上限，请明天再试")
    if not _rate_limit("phc:" + (code or "-"), 40, 86400):
        _log("[%s] 手机号通道 发码限速(兑换码)" % (code or "-"))
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    sess = _sess(code)
    if sess is None:
        _done = _done_op_reply(code)
        if _done is not None:
            return _done
        raise HTTPException(status_code=403, detail="需要兑换码")
    sess.note_meta(ip, request.headers.get("user-agent") or "")
    op = await asyncio.to_thread(sess.phone_send, raw)
    _log("[%s] 手机号通道 发码 ok=%s msg=%s" % (code or "-", op.get("ok"), str(op.get("message"))[:60]))
    return _phone_reply(sess, op)


@app.post("/api/phone/login")
async def api_phone_login(request: Request):
    code = _need_ticket(request)
    ip = _client_ip(request)
    body = await _phone_body(request)
    # ★ 传给 Session 的必须是**原始号码**：mobile_key() 只对 CN 幂等，
    #   把 key 再喂回去会把 "+1 555..." 的 key("15551234567") 二次解析成大陆号。
    raw = str(body.get("phone") or "").strip()
    m = mobile_key(raw)
    sms = str(body.get("code") or "").strip()
    # ★R86c：同「兑换码 + IP」分桶（原来一群人在同一 WiFi 下会共用 30 次/小时）
    if not _rate_limit("phl:%s:%s" % (code or "-", ip), 30, 3600):
        _log("[%s] 手机号通道 登录限速(IP) ip=%s" % (code or "-", ip))
        raise HTTPException(status_code=429, detail="登录过于频繁，请稍后再试")
    sess = _sess(code)
    if sess is None:
        _done = _done_op_reply(code)
        if _done is not None:
            return _done
        raise HTTPException(status_code=403, detail="需要兑换码")
    sess.note_meta(ip, request.headers.get("user-agent") or "")
    op = await asyncio.to_thread(sess.phone_login, raw or m, sms)
    if not op.get("ok"):
        # 同 IP 登录失败计数：一小时内错满 12 次直接 429（普通失败的仍是 200 + op.ok=false）
        if not _rate_limit("phlbad:%s:%s" % (code or "-", ip), 12, 3600):
            _log("[%s] 手机号通道 登录失败次数过多，暂缓 ip=%s" % (code or "-", ip))
            raise HTTPException(status_code=429, detail="验证码错误次数过多，请稍后再试")
    _log("[%s] 手机号通道 登录 ok=%s msg=%s" % (code or "-", op.get("ok"), str(op.get("message"))[:60]))
    return _phone_reply(sess, op)


@app.get("/api/countries")
def api_countries():
    """供前端下拉：手机号/账号地区。"""
    return JSONResponse({"ok": True, "countries": country_list()})


def _seg_of(acct: str) -> str:
    """把「昵称 / +85********」归到号段；只用于聚合统计。"""
    s = str(acct or "").split("/")[-1].strip()
    if s.startswith("+"):
        for p in ("+852", "+886", "+853", "+1"):
            if s.startswith(p):
                return p
        return s[:4]
    if s[:1].isdigit():
        return "+86"
    return "其他"


@app.get("/api/stats/segments")
def api_stats_segments(minutes: int = 120):
    """★R679：近 N 分钟各号段领取成功率（聚合口径，不含任何个人信息）。"""
    import sqlite3 as _sq
    try:
        minutes = max(10, min(int(minutes or 120), 1440))
    except Exception:
        minutes = 120
    since = int(time.time()) - minutes * 60
    agg = {}
    try:
        c = _sq.connect(store.DB_PATH, timeout=10)
        c.row_factory = _sq.Row
        for r in c.execute("SELECT kind, account FROM events WHERE created_at>=? AND kind IN ('success','fail') AND account<>''", (since,)):
            d = agg.setdefault(_seg_of(r["account"]), [0, 0])
            d[0 if r["kind"] == "success" else 1] += 1
        c.close()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:120]})
    items = []
    for seg, (ok, bad) in agg.items():
        n = ok + bad
        if n < 3:
            continue
        items.append({"seg": seg, "ok": ok, "n": n, "rate": int(round(100.0 * ok / n))})
    items.sort(key=lambda x: -x["n"])
    return JSONResponse({"ok": True, "minutes": minutes, "items": items,
                         "total": sum(i["n"] for i in items)})


@app.post("/api/region")
async def api_region(request: Request):
    """二维码通道：买家选「账号地区」→ 本码出口地区跟着走。"""
    code = _need_ticket(request)
    ip = _client_ip(request)
    body = await _phone_body(request)
    cc = str(body.get("cc") or "").strip().upper()
    sess = _sess(code)
    if sess is None:
        _done = _done_op_reply(code)
        if _done is not None:
            return _done
        raise HTTPException(status_code=403, detail="需要兑换码")
    if not _rate_limit("phr:" + (code or "-"), 40, 3600):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    sess.note_meta(ip, request.headers.get("user-agent") or "")
    op = sess.set_account_region(cc)
    _log("[%s] 账号地区 %s -> 出口 %s ok=%s" % (code or "-", cc, region_for_iso(cc), op.get("ok")))
    return _phone_reply(sess, op)


@app.post("/api/phone/reset")
async def api_phone_reset(request: Request):
    code = _need_ticket(request)
    ip = _client_ip(request)
    await _phone_body(request)              # 请求体任意，容错读掉即可
    sess = _sess(code)
    if sess is None:
        _done = _done_op_reply(code)
        if _done is not None:
            return _done
        raise HTTPException(status_code=403, detail="需要兑换码")
    sess.note_meta(ip, request.headers.get("user-agent") or "")
    op = sess.phone_reset()
    _log("[%s] 手机号通道 重置 ok=%s ip=%s" % (code or "-", op.get("ok"), ip))
    return _phone_reply(sess, op)


@app.post("/api/recheck")
async def api_recheck(request: Request):
    """★R76：买家点「重新核验」——立刻再查一次权益到账（失败横幅里的按钮）。

    背景：礼包是异步发放的（实测中位 24s，尾巴 10 分钟以上）。以前第一次查不到就直接判「本次未领取到」，
    买家关掉页面后即使到账了也永远不知道 —— 这是「显示失败但已到账」的根因。
    """
    code = _need_ticket(request)
    ip = _client_ip(request)
    await _phone_body(request)              # 请求体任意，容错读掉即可
    sess = _sess(code)
    if sess is None:
        _done = _done_op_reply(code)
        if _done is not None:
            return _done
        raise HTTPException(status_code=403, detail="需要兑换码")
    if not _rate_limit("rchk:" + (code or "-"), 30, 600):
        raise HTTPException(status_code=429, detail="查询太频繁，请稍后再试")
    sess.note_meta(ip, request.headers.get("user-agent") or "")
    # ★R336_RECHECK_THREAD：recheck_now() 会同步打两次官方接口（每次 timeout 8s，实测把事件循环
    # 钉死过 20.7s）——async 处理函数里直接调用会让全站二维码轮询/状态刷新一起卡住。
    op = await asyncio.to_thread(sess.recheck_now)
    _log("[%s] 手动复查 ok=%s got=%s msg=%s" % (code or "-", op.get("ok"), op.get("got"), op.get("message")))
    return _phone_reply(sess, op)


@app.post("/api/_demo")
def api_demo(request: Request, r: str = "ok"):
    if not DEMO:
        raise HTTPException(status_code=404, detail="not found")
    sess = _sess(_need_ticket(request))
    if sess is None:
        raise HTTPException(status_code=403, detail="需要兑换码")
    sess.note_meta(_client_ip(request), request.headers.get("user-agent") or "")
    if r == "ok":
        sess.account = "138****8888 / 豆包用户"
        store.log_event(sess.code, "login", account=sess.account,
                        note="登录成功（%s），开始核验权益" % sess.account,
                        owner_id=sess.owner_id)
        sess._finish({"got": True, "account": "138****8888 / 豆包用户",
                 "items": [{"source": "pc_membership_gift", "tag": "pc_gift", "product": "体验套餐",
                            "end": time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 30 * 86400)),
                            "is_gift": True}],
                 "entry": {"has_active": True, "subs_status": "free_vip"}, "pop": "30天免费订阅福利已到账"})
    else:
        sess._finish({"got": False, "account": "+1******20 / 居家健康", "items": [],
                 "entry": {"has_active": False, "subs_status": "free", "country": "US"},
                 "pop": None, "campaign_status": 3})
    return JSONResponse(sess.snapshot())


# ★R328_CODE_STALE：改完 py 文件却没重启，线上仍跑旧逻辑（2026-10-19 17:23「页面没了」就是这个）。
# 这里把「磁盘最新 py mtime vs 本进程启动时刻」暴露到 /healthz，cron 巡检据此告警。
_BOOT_TS = time.time()
_CODE_STALE_CACHE = {"at": 0.0, "val": None}


def _code_stale() -> dict:
    now = time.time()
    c = _CODE_STALE_CACHE
    if c["val"] is not None and (now - c["at"]) < 60:
        return c["val"]
    newest, newest_f = 0.0, ""
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        for fn in os.listdir(base):
            if not fn.endswith(".py"):
                continue
            try:
                m = os.path.getmtime(os.path.join(base, fn))
            except OSError:
                continue
            if m > newest:
                newest, newest_f = m, fn
    except Exception:
        pass
    _stale_s = int(max(0.0, newest - _BOOT_TS))
    val = {"stale": _stale_s > 5, "stale_s": _stale_s,
           "newest": newest_f,
           "boot_cst": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_BOOT_TS))}
    c["at"], c["val"] = now, val
    return val


@app.get("/healthz")
async def healthz(request: Request):   # ★R285：纯内存统计，改 async 不再占用线程池令牌
    """★R85 去敏：公网只回 {"ok":true}；本机/显式开关才返回运营明细。"""
    ip = _client_ip(request)
    if HEALTHZ_DETAIL or ip in ("127.0.0.1", "::1", "localhost"):
        _gate = {}                      # ★R188：闸门观测（限值 / 当前在途 / 峰值 / 等待次数与总等待秒数）
        try:
            import sys as _sys
            _sm = _sys.modules.get("app.sessions")
            _gate = _sm.GEN_GATE.stats() if _sm is not None else {}
        except Exception:
            _gate = {}
        return {"ok": True, "mode": MODE, "pool_left": SOURCE.remaining(),
                "sessions": SM.stats(), "codes": store.stats_cached(), "gate": _gate,
                "code": _code_stale()}
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 后台
# --------------------------------------------------------------------------- #
@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    if not _admin_ok(request):
        return _page("admin_login.html")
    return _page("admin.html")


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page():
    """★ 兜底后台入口（白名单永久放行）：换网络 / 换 IP 导致 /admin 打不开时，
    直接访问 /admin/login 用「密码 + 谷歌动态码」登录；登录成功会自动把当时的 IP 加进白名单。"""
    return _page("admin_login.html")


@app.get("/admin/api/authinfo")
def admin_authinfo():
    """登录页用：只告诉前端要不要动态码（不含敏感信息）。"""
    return {"totp_enabled": _totp_enabled()}


@app.post("/admin/login")
async def admin_login(request: Request):
    """密码 +（启用 2FA 后）谷歌动态码/恢复码。不依赖 python-multipart。"""
    ip = _client_ip(request)
    if _adm_gate_blocked(ip):
        return _msg_page("尝试过于频繁", "系统正在拦截异常登录，请 3 分钟后再试。", "🛑", 429,
                         back="/admin", back_text="重新登录")
    if not _rate_limit("login:" + ip, LOGIN_MAX_FAIL, LOGIN_LOCK_SEC):
        _log("⚠ 后台登录被限速 ip=%s" % ip)
        n = guard.add_fail(ip, LOGIN_MAX_FAIL, LOGIN_BAN_SEC)
        if n >= LOGIN_MAX_FAIL:
            _log("🛑 该 IP 反复试密码，已自动封禁 %d 分钟：%s" % (LOGIN_BAN_SEC // 60, ip))
        return _msg_page("尝试过于频繁", "该 IP 连续失败过多，请 %d 分钟后再试。" % (LOGIN_LOCK_SEC // 60),
                         "🛑", 429, back="/admin", back_text="重新登录")

    try:
        raw = (await request.body()).decode("utf-8", "replace")
    except Exception:
        # ★R694：扫描器/浏览器中途断开 → 按空密码处理，别让 ClientDisconnect 打满日志
        _log("⚠ 后台登录请求被中断（客户端断开） ip=%s" % ip)
        raw = ""
    pwd = code = ""
    try:
        q = parse_qs(raw)
        pwd = (q.get("password") or [""])[0]
        code = (q.get("code") or [""])[0]
    except Exception:
        pass
    if not pwd and raw.strip().startswith("{"):
        try:
            j = json.loads(raw)
            pwd = str(j.get("password") or "")
            code = str(j.get("code") or "")
        except Exception:
            pass

    if not _admin_pwd_ok(pwd):
        _fail_login(ip, "密码", request)
        await asyncio.sleep(0.5)        # ★R85：原为 time.sleep，会卡死整个事件循环（放大暴力破解）
        return _msg_page("密码错误", "请返回重试。", "🔒", 403, back="/admin", back_text="重新登录")

    if _totp_enabled() and not _check_2fa(code):
        _fail_login(ip, "动态码" if code else "缺动态码", request)
        return _msg_page("动态码不正确", "请输入谷歌验证器当前 6 位动态码；换手机时可用一个恢复码登录。",
                         "🔐", 403, back="/admin", back_text="重新登录")

    stored = store.get_setting("admin_password_hash")
    if not stored:
        store.set_setting("admin_password_hash", otp.hash_password(pwd))
        _log("已把后台密码升级为 PBKDF2 哈希存储（不再明文/弱哈希）")
    elif not stored.startswith("pbkdf2$") or otp.needs_rehash(stored):
        store.set_setting("admin_password_hash", otp.hash_password(pwd))
        _log("已把旧格式的密码哈希自动升级为 PBKDF2 存储")

    resp = RedirectResponse("/admin", status_code=302)
    resp.set_cookie("adm", _admin_cookie(), max_age=3600 * ADMIN_SESSION_HOURS,
                    httponly=True, samesite="lax", secure=_is_https(request), path="/")
    guard.clear_fails(ip)
    _adm_gate_reset()
    # ★ 登录成功 = 本人无误 → 自动把当前 IP 加进白名单（防止换 IP 把自己锁在外面）
    try:
        if guard.whitelist_on() and not guard.allowed(ip) and guard.valid_rule(ip):
            saved = guard.set_whitelist(list(guard.whitelist()) + [ip])
            _log("访问控制：登录成功后自动把当前 IP 加白 %s（名单 %d 条）" % (ip, len(saved)))
    except Exception as ex:
        _log("自动加白失败：%s" % str(ex)[:120])
    _log("后台登录成功 ip=%s 2FA=%s" % (ip, _totp_enabled()))
    return resp


@app.get("/admin/api/security")
def admin_security(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    try:
        left = len(json.loads(store.get_setting("recovery_hashes") or "[]"))
    except Exception:
        left = 0
    return {"ok": True, "totp_enabled": _totp_enabled(), "recovery_left": left,
            "session_hours": ADMIN_SESSION_HOURS,
            "password_source": "数据库" if store.get_setting("admin_password_hash") else "环境变量",
            "trust_proxy": TRUST_PROXY, "rate": {"code": CODE_RATE, "login": LOGIN_MAX_FAIL}}


@app.post("/admin/api/totp/setup")
def admin_totp_setup(request: Request):
    """生成待绑定密钥 + 二维码（此时尚未启用）。"""
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    secret = otp.new_secret()
    store.set_setting("totp_pending", secret)
    uri = otp.provisioning_uri(secret, "admin")
    return {"ok": True, "secret": secret, "uri": uri, "qr_svg": otp.qr_svg(uri), "period": otp.PERIOD}


@app.post("/admin/api/totp/enable")
async def admin_totp_enable(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    try:
        code = str((await request.json()).get("code") or "")
    except Exception:
        code = ""
    pending = store.get_setting("totp_pending")
    if not pending:
        return JSONResponse({"ok": False, "message": "请先点「生成二维码」"}, status_code=400)
    if not otp.verify(pending, code):
        return JSONResponse({"ok": False, "message": "动态码不对，请确认手机时间已自动同步"}, status_code=400)
    codes = otp.new_recovery_codes(10)
    store.set_setting("totp_secret", pending)
    store.set_setting("totp_enabled", "1")
    store.set_setting("totp_pending", "")
    store.set_setting("recovery_hashes", json.dumps([otp.recovery_hash(c) for c in codes]))
    _log("★ 后台已启用谷歌验证器（2FA），同时发放 10 个恢复码")
    return {"ok": True, "message": "已启用两步验证", "recovery_codes": codes}


@app.post("/admin/api/totp/disable")
async def admin_totp_disable(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    try:
        code = str((await request.json()).get("code") or "")
    except Exception:
        code = ""
    if not _totp_enabled():
        return JSONResponse({"ok": False, "message": "当前未启用两步验证"}, status_code=400)
    if not _check_2fa(code):
        return JSONResponse({"ok": False, "message": "动态码不正确"}, status_code=400)
    store.set_setting("totp_enabled", "")
    store.set_setting("totp_secret", "")
    store.set_setting("recovery_hashes", "[]")
    _log("⚠ 后台已关闭谷歌验证器（2FA）")
    return {"ok": True, "message": "已关闭两步验证（建议尽快重新绑定）"}


@app.post("/admin/api/totp/recovery")
async def admin_totp_recovery(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    try:
        code = str((await request.json()).get("code") or "")
    except Exception:
        code = ""
    if not _check_2fa(code):
        return JSONResponse({"ok": False, "message": "动态码不正确"}, status_code=400)
    codes = otp.new_recovery_codes(10)
    store.set_setting("recovery_hashes", json.dumps([otp.recovery_hash(c) for c in codes]))
    return {"ok": True, "message": "已重新生成 10 个恢复码（旧的全部作废）", "recovery_codes": codes}


SETTING_KEYS = ()   # ★R209：可领次数固定为 1，不再提供任何可修改的建码配置


@app.post("/admin/api/setting")
async def admin_setting(request: Request):
    """运行时改配置（R209 起已无可修改项）。"""
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        body = {}
    key = str(body.get("key") or "")
    if key not in SETTING_KEYS:
        return JSONResponse({"ok": False, "message": "该设置已下线：每张卡密固定只能领取 1 次"}, status_code=400)
    val = str(body.get("value") or "")
    if key == "claims_per_code":
        try:
            n = int(val)
        except Exception:
            return JSONResponse({"ok": False, "message": "必须是数字"}, status_code=400)
        if n not in range(0, 1000):
            return JSONResponse({"ok": False, "message": "取值 0~999（0=不限次）"}, status_code=400)
        store.set_setting(key, str(n))
        _log("配置更新：claims_per_code=%s（0=不限次）" % n)
        return JSONResponse({"ok": True, "message": "已保存：每张码可领 %s" % ("不限次" if n == 0 else "%d 次" % n),
                             "claims_per_code": n})
    return JSONResponse({"ok": False, "message": "未处理"}, status_code=400)


@app.post("/admin/api/password")
async def admin_password(request: Request):
    """后台内自助改密码（写入 SQLite，重启不丢；改完旧 cookie 全部失效）。"""
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        body = {}
    old = str(body.get("old") or "")
    new = str(body.get("new") or "")
    if not _admin_pwd_ok(old):
        return JSONResponse({"ok": False, "message": "原密码不正确"}, status_code=400)
    if len(new) < 8:
        return JSONResponse({"ok": False, "message": "新密码至少 8 位"}, status_code=400)
    if new == old:
        return JSONResponse({"ok": False, "message": "新密码不能与原密码相同"}, status_code=400)
    store.set_setting("admin_password_hash", otp.hash_password(new))
    _log("后台密码已更新")
    resp = JSONResponse({"ok": True, "message": "密码已更新，其他已登录的浏览器会自动退出"})
    resp.set_cookie("adm", _admin_cookie(), max_age=3600 * ADMIN_SESSION_HOURS,
                    httponly=True, samesite="lax", secure=_is_https(request), path="/")
    return resp


@app.get("/admin/logout")
def admin_logout():
    resp = RedirectResponse("/admin", status_code=302)
    resp.delete_cookie("adm")
    return resp


@app.post("/admin/api/gen")
async def admin_gen(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    count = int(body.get("count") or 0)
    note = str(body.get("note") or "")[:120]
    # ★R209：一码一次，前端不再传 claims，服务端也强制 1
    mc = 1
    made = store.generate(count, note=note, max_claims=mc)
    base = _base_url(request)
    return JSONResponse({"ok": True, "count": len(made), "max_claims": (0 if mc is None else mc),
                         "items": [{"code": c, "url": "%s/%s" % (base, c)} for c in made]})


@app.get("/admin/api/list")
def admin_list(request: Request, status: str = "", batch: str = "",
               limit: int = 200, offset: int = 0, owner_id: int = 0):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    base = _base_url(request)
    lim = max(1, min(int(limit), 2000))
    off = max(0, int(offset))
    ids = _owner_scope(owner_id)                    # ★R711 归属过滤
    rows = store.listing(status, batch, lim, off, owner_ids=ids)
    for r in rows:
        r["url"] = "%s/%s" % (base, r["code"])
        for k in ("created_at", "used_at"):
            if r.get(k):
                r[k + "_str"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r[k]))
    total = store.count(status, batch, ids)
    return JSONResponse({"ok": True, "rows": rows, "stats": store.stats(ids),
                         "claims_per_code": 1,   # ★R209：固定 1 次
                         "filtered": total, "total": total,
                         "limit": lim, "offset": off,
                         "pages": max(1, (total + lim - 1) // lim),
                         "events": store.events_stats(),
                         "batches": store.batches(), "base": base})


@app.get("/admin/api/logs")
def admin_logs(request: Request, code: str = "", kind: str = "", limit: int = 50, offset: int = 0,
               owner_id: int = 0):
    """领取记录：对方扫码 / 领取成功 / 未到账 全记录（带时间、IP、账号），支持分页与筛选。

    ★R711：owner_id 真正生效。此前本接口没接 owner_id（前端一直在传），
    FastAPI 静默忽略多余参数，所以「只看 a005」仍然返回全站 57358 条 / 1148 页。
    """
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    lim = max(1, min(int(limit), 500))
    off = max(0, int(offset))
    code = (code or "").strip().upper()
    kind = (kind or "").strip().lower()
    if kind not in ("enter", "scan", "success", "fail"):
        kind = ""
    ids = _owner_scope(owner_id)
    total = store.events_count(code, kind, ids)
    rows = store.events(code, kind, lim, off, ids)
    return JSONResponse({"ok": True, "rows": rows, "total": total, "limit": lim, "offset": off,
                         "pages": max(1, (total + lim - 1) // lim),
                         "stats": store.events_stats(code, "", ids),
                         "kind_text": store.KIND_TEXT,
                         "code": code, "kind": kind,
                         "owner_id": int(owner_id or 0), "owner_ids": ids or []})


@app.post("/admin/api/set")
async def admin_set(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    body = await request.json()
    code = str(body.get("code") or "").strip().upper()
    status = str(body.get("status") or "unused")
    rec = store.get(code) or {}
    if not rec:
        return JSONResponse({"ok": False, "message": "卡密不存在"}, status_code=404)
    # ★R209：停用立刻踢掉在途会话；启用按真实状态恢复（领过的一律回 done）
    if status == "disabled":
        store.set_status(code, "disabled")
        try:
            SM.kill(code)
        except Exception:
            pass
    elif status == "unused":
        if int(rec.get("claims") or 0) >= 1:
            status = "done"
        elif int(rec.get("visits") or 0) > 0:
            status = "visiting"
        store.set_status(code, status)
    else:
        store.set_status(code, status)
    return JSONResponse({"ok": True, "stats": store.stats(),
                         "status": (store.get(code) or {}).get("status")})


@app.post("/admin/api/codes/disable")
async def admin_codes_disable(request: Request):
    """★R209：总台批量停用/启用卡密（与代理商接口同语义）。"""
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw = body.get("codes")
    if not raw:
        raw = [body.get("code")]
    codes = [str(x or "").strip().upper() for x in (raw or []) if x][:1000]
    disable = bool(body.get("disabled", True))
    done = []
    for code in codes:
        rec = store.get(code)
        if not rec:
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
    return JSONResponse({"ok": True, "count": len(done), "codes": done, "stats": store.stats()})


@app.get("/admin/api/export")
def admin_export(request: Request, status: str = "", batch: str = "", fmt: str = "url"):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    base = _base_url(request)
    rows = store.listing(status, batch, 100000, 0)
    if fmt == "code":
        txt = "\n".join(r["code"] for r in rows)
    elif fmt == "csv":
        txt = "code,url,status,created\n" + "\n".join(
            "%s,%s/%s,%s,%s" % (r["code"], base, r["code"], r["status"], r.get("created_at_str", "")) for r in rows)
    else:
        txt = "\n".join("%s/%s" % (base, r["code"]) for r in rows)
    ext = "csv" if fmt == "csv" else "txt"
    return PlainTextResponse(txt, headers={
        "Content-Disposition": "attachment; filename=codes_%s.%s" % (time.strftime("%Y%m%d_%H%M%S"), ext)})


# --------------------------------------------------------------------------- #
# R63 多租户路由挂载（必须在末尾 /{code} 通配之前；也必须晚于 _base_url 定义）
# --------------------------------------------------------------------------- #
if _MULTI_OK:
    try:
        authz_mod.set_secret(SECRET)
        tenant_api.set_deps(admin_ok=_admin_ok, client_ip=_client_ip, base_url=_base_url)
        app.include_router(tenant_api.router)
        # ---- R64 绑定后台网址：代理商专属入口 /a/s/<slug>（独立异常，不影响上面） ----
        try:
            from app import sites as sites_mod
            sites_mod.set_deps(admin_ok=_admin_ok, client_ip=_client_ip, base_url=_base_url)
            sites_mod.set_stamp(_stamp_assets)
            app.include_router(sites_mod.router)
        except Exception as _se:
            _log("R64 绑定网址模块未加载: %s" % str(_se)[:160])
    except Exception as _e:
        _MULTI_OK = False
        _MULTI_ERR = str(_e)[:200]


# --------------------------------------------------------------------------- #
# R63 真实设备码池（Windows 采集端导出 -> 后台批量导入）
# --------------------------------------------------------------------------- #
def _devmod():
    try:
        from app import devices as d
        return d
    except Exception:
        return None


@app.get("/admin/api/devices")
def admin_devices(request: Request):
    """设备池状态 + 前 20 条样本（真实设备码池才是「新设备」判定的命门）。"""
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    d = _devmod()
    if d is None:
        return {"ok": False, "message": "设备模块不可用"}
    try:
        st = d.pool_stats()
        sample = []
        try:
            import json as _json
            arr = _json.loads(d.export_items(20) or "{}")
            sample = arr.get("devices", []) if isinstance(arr, dict) else []
        except Exception:
            sample = []
        return {"ok": True, "stats": st, "sample": sample, "mode": MODE}
    except Exception as e:
        return {"ok": False, "message": "读取设备池失败：%s" % str(e)[:120]}


@app.post("/admin/api/devices/import")
async def admin_devices_import(request: Request):
    """粘贴导入：每行 did / did,iid / 一行 JSON / 整份 {"devices":[...]} 都吃。"""
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    d = _devmod()
    if d is None:
        return JSONResponse({"ok": False, "message": "设备模块不可用"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    text = str(body.get("text") or "")
    if not text.strip():
        return JSONResponse({"ok": False, "message": "请粘贴设备码内容"}, status_code=400)
    try:
        r = d.import_items(text)
        _log("设备码池导入：新增 %s / 跳过 %s / 合计 %s" % (r.get("added"), r.get("skipped"), r.get("total")))
        return {"ok": True, "result": r, "stats": d.pool_stats(),
                "message": "已导入 %s 个设备码（跳过 %s 条无效）" % (r.get("added"), r.get("skipped"))}
    except Exception as e:
        return JSONResponse({"ok": False, "message": "导入失败：%s" % str(e)[:120]}, status_code=400)


@app.get("/admin/api/devices/export")
def admin_devices_export(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    d = _devmod()
    if d is None:
        raise HTTPException(status_code=400, detail="设备模块不可用")
    txt = d.export_items()
    return PlainTextResponse(txt, headers={
        "Content-Disposition": "attachment; filename=devices_%s.json" % time.strftime("%Y%m%d_%H%M%S")})


@app.post("/admin/api/devices/clear")
async def admin_devices_clear(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    d = _devmod()
    if d is None:
        return JSONResponse({"ok": False, "message": "设备模块不可用"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if str(body.get("confirm") or "") != "CLEAR":
        return JSONResponse({"ok": False, "message": "危险操作：请输入 CLEAR 确认清空设备池"}, status_code=400)
    d.clear_pool()
    _log("⚠ 设备码池已被清空")
    return {"ok": True, "message": "设备池已清空", "stats": d.pool_stats()}


# --------------------------------------------------------------------------- #
# R63 控制台页面（总台 & 代理商共用一套前端）
# --------------------------------------------------------------------------- #
@app.get("/admin/console", response_class=HTMLResponse)
def admin_console(request: Request):
    """总台运营控制台（代理商/额度/审计/代理池）。未登录回登录页。"""
    if not _admin_ok(request):
        return _page("admin_login.html")
    return _page("console.html")


@app.get("/a/login", response_class=HTMLResponse)
def tenant_login_page():
    """代理商/子代理商登录页（不需要 IP 白名单，随时可达）。"""
    return _page("tenant_login.html")


@app.get("/a", response_class=HTMLResponse)
def tenant_console(request: Request):
    """代理商控制台。cookie 校验交给前端调 /a/api/me（401 自动跳登录页）。"""
    return _page("console.html")


# --------------------------------------------------------------------------- #
# 静态
# --------------------------------------------------------------------------- #
@app.get("/style.css")
def css(request: Request):
    return _asset("style.css", "text/css", request)


@app.get("/app.js")
def js(request: Request):
    return _asset("app.js", "application/javascript", request)


@app.get("/admin.css")
def admin_css(request: Request):
    return _asset("admin.css", "text/css", request)


@app.get("/admin.js")
def admin_js(request: Request):
    return _asset("admin.js", "application/javascript", request)


@app.get("/console.css")
def console_css(request: Request):
    return _asset("console.css", "text/css", request)


@app.get("/console.js")
def console_js(request: Request):
    return _asset("console.js", "application/javascript", request)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("Content-Security-Policy",
                 "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                 "script-src 'self' 'unsafe-inline'; connect-src 'self'; form-action 'self'; "
                 "frame-ancestors 'none'; base-uri 'none'")
    if _is_https(request):
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.url.path.startswith("/admin"):
        h["Cache-Control"] = "no-store"
    return resp


@app.middleware("http")
async def _access_control(request: Request, call_next):
    """IP 封禁（全站）+ 后台 IP 白名单（只护 /admin*）。"""
    ip = _client_ip(request)
    if guard.is_banned(ip):
        return _plain_page("访问被拒绝", "你的 IP 已被本站限制访问。", "🛑", 403)
    if request.url.path.startswith("/admin"):
        if guard.whitelist_on() and not guard.allowed(ip):
            # ★ 兜底 1：登录页 / 登录接口永远可达 —— 换网络、换 IP 后照样能登录进来
            #          （登录成功会自动把当时的 IP 加进白名单，见 admin_login）
            if request.url.path in LOGIN_ALLOW_PATHS or request.url.path in STATIC_ALLOW_PATHS:
                return await call_next(request)
            # ★ 兜底 2：已经登录过的浏览器（带合法 adm cookie）不受 IP 变化影响
            if _admin_ok(request):
                return await call_next(request)
            _log("⚠ 后台访问被白名单拦截：ip=%s path=%s ua=%s"
                 % (ip, request.url.path, (request.headers.get("user-agent") or "")[:60]))
            if SHOW_ADMIN_REAL:
                return _plain_page("无权访问", "该后台仅允许指定 IP 访问。", "🚫", 403)
            return _plain_page("页面不存在", "你访问的页面不存在。", "🙈", 404)
    return await call_next(request)


MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(2 * 1024 * 1024)))


@app.middleware("http")
async def _body_limit(request: Request, call_next):
    """★R211：全局请求体上限（默认 2MB），防超大 body 把单进程内存打爆。"""
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > MAX_BODY_BYTES:
                return _plain_page("请求过大", "请求体超过 %d KB 上限。" % (MAX_BODY_BYTES // 1024), "🛑", 413)
        except ValueError:
            return _plain_page("请求不合法", "Content-Length 格式不正确。", "🛑", 400)
    # ★R211b：本服务没有任何流式上传需求，直接拒绝 chunked —— 否则可绕过 Content-Length 限制
    te = (request.headers.get("transfer-encoding") or "").strip().lower()
    if te and te != "identity":
        return _plain_page("请求过大", "本接口不支持分块上传，请带 Content-Length 提交。", "🛑", 413)
    return await call_next(request)


@app.get("/admin/api/guard")
def admin_guard_get(request: Request):
    """访问控制面板：白名单 / 封禁列表 / 当前 IP。"""
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    ip = _client_ip(request)
    return {"ok": True, "ip": ip, "whitelist": guard.whitelist(),
            "whitelist_on": guard.whitelist_on(), "bans": guard.bans(),
            "allowed": guard.allowed(ip), "banned": bool(guard.is_banned(ip))}


@app.post("/admin/api/guard/whitelist")
async def admin_guard_whitelist(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        body = {}
    items = body.get("items")
    if not isinstance(items, list):
        items = []
    on = bool(body.get("on"))
    ip = _client_ip(request)
    if on and guard.valid_rule(ip) and guard.valid_rule(ip) not in [guard.valid_rule(x) for x in items]:
        items = list(items) + [ip]        # 开启时自动把当前 IP 加进去，防止把自己锁外面
    saved = guard.set_whitelist(items)
    guard.set_whitelist_on(on)
    _log("访问控制：白名单%s，共 %d 条" % ("开启" if on else "关闭", len(saved)))
    msg = ("已开启白名单（%d 条），只有名单里的 IP 能打开后台" % len(saved)) if on else "已关闭白名单（任何 IP 都能打开后台）"
    return JSONResponse({"ok": True, "whitelist": saved, "whitelist_on": on, "message": msg})


@app.post("/admin/api/guard/ban")
async def admin_guard_ban(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        body = {}
    ip = guard.normalize(str(body.get("ip") or ""))
    if not guard.valid_rule(ip):
        return JSONResponse({"ok": False, "message": "IP 格式不对"}, status_code=400)
    try:
        mins = int(body.get("mins") or 60)
    except Exception:
        mins = 60
    mins = max(1, min(mins, 60 * 24 * 365))
    guard.ban(ip, mins * 60, reason="管理员手动封禁")
    _log("访问控制：手动封禁 %s（%d 分钟）" % (ip, mins))
    return JSONResponse({"ok": True, "message": "已封禁 %s（%d 分钟）" % (ip, mins)})


@app.post("/admin/api/guard/unban")
async def admin_guard_unban(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        body = {}
    ip = str(body.get("ip") or "")
    ok = guard.unban(ip)
    _log("访问控制：解封 %s（%s）" % (ip, "成功" if ok else "不在列表"))
    return JSONResponse({"ok": ok, "message": ("已解封 " + ip) if ok else "该 IP 不在封禁列表中"})


# ★R295：只由 _startup() 显式调用；此前 decorator + 显式调用会起两个看门狗线程
def _start_loop_lag_watch():
    """★R285：事件循环卡顿取证。uvicorn 单进程下任何 async 端点里的同步阻塞
    （sqlite/磁盘/锁）都会让所有用户一起卡住。这里用旁路线程采样主线程栈：
    同一个栈停留 >=0.6s 且不是 uvloop 空闲态，就把栈写进日志。"""
    import sys as _sys, threading as _th, traceback as _tb
    mid = _th.main_thread().ident
    _IDLE = {"run", "run_forever", "_run_once", "serve", "_serve", "run_until_complete"}

    def _watch():
        last = ""; since = time.time(); last_report = 0.0
        while True:
            time.sleep(0.2)
            try:
                f = _sys._current_frames().get(mid)
                if f is None:
                    continue
                top = f.f_code.co_name
                key = "%s:%s:%s" % (f.f_code.co_filename, f.f_lineno, top)
                now = time.time()
                if key != last:
                    last = key; since = now
                    continue
                if top in _IDLE:
                    since = now
                    continue
                if now - since >= 0.6 and now - last_report >= 10:
                    last_report = now
                    st = "".join(_tb.format_stack(f, limit=8))
                    _log("★R285 事件循环疑似卡顿 %.1fs（主线程栈）：\n%s" % (now - since, st))
            except Exception:
                pass

    _th.Thread(target=_watch, daemon=True, name="loop-lag-watch").start()


@app.on_event("startup")
def _startup():
    # ★R86：同步 def 路由跑在 anyio 默认线程池（只有 40 个 token）。出码/登录这类阻塞活儿
    # 一旦把池子占满，所有同步接口一起排队 → 页面「卡」。这里把池子调大（env THREAD_POOL_SIZE 可覆盖）。
    try:
        import anyio
        _tp = int(os.environ.get("THREAD_POOL_SIZE", "96"))
        anyio.to_thread.current_default_thread_limiter().total_tokens = _tp
        _log("★R86 线程池上限 = %d（THREAD_POOL_SIZE 可调）" % _tp)
    except Exception as _e:
        _log("线程池调整失败（不影响启动）: %s" % str(_e)[:120])
    try:
        _start_loop_lag_watch()
        _log("★R285 事件循环卡顿看门狗已启动（>=0.6s 采样写日志）")
    except Exception as _e:
        _log("卡顿看门狗启动失败（不影响服务）: %s" % str(_e)[:120])
    try:
        from app import sessions as _sessmod
        _log("★R86 出码参数：刷新提前量 %ss / 轮询 %ss(未扫)-%ss(已扫) / 换码等待 %ss"
             % (_sessmod.REFRESH_BELOW, _sessmod.POLL_SEC, _sessmod.POLL_FAST_SEC, _sessmod.ROTATE_WAIT))
    except Exception:
        pass
    _log("服务启动 MODE=%s REQUIRE_CODE=%s BASE_URL=%s 设备池=%d/%d 兑换码=%s"
         % (MODE, REQUIRE_CODE, BASE_URL or "(auto)", SOURCE.remaining(), SOURCE.pool_total, store.stats()))
    # ★R259：短信会话落盘自检（R255）。重启后这行能直接证明恢复机制在位、有多少会话可救。
    try:
        _psd = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "phone_state")
        _nps = len([x for x in os.listdir(_psd) if x.endswith(".json")]) if os.path.isdir(_psd) else 0
        _log("★R259 短信会话落盘：%d 个可恢复（TTL %.0f 分钟）" % (_nps, 7))
    except Exception:
        pass
    # ---- R63 多租户初始化 ----
    if _MULTI_OK:
        try:
            tenants_mod.init()
            tstats = tenants_mod.stats()
            _log("★ 多租户已就绪：代理商 %d 个（一级 %d / 二级 %d），状态 active=%d disabled=%d banned=%d"
                 % (tstats.get("total", 0), tstats.get("l1", 0), tstats.get("l2", 0),
                    tstats.get("active", 0), tstats.get("disabled", 0), tstats.get("banned", 0)))
        except Exception as e:
            _log("✗ 多租户初始化失败：%s" % str(e)[:200])
    else:
        _log("✗ 多租户模块未加载（%s）—— 三级代理/控制台不可用" % (_MULTI_ERR or "缺文件"))
    try:
        from app import proxy as _pxmod
        pc = _pxmod.get_config()
        _log("出口代理池：%s（%s @ %s:%s），粘性 %s 分钟"
             % ("★开启" if pc.get("enabled") else "关闭", pc.get("mode"),
                pc.get("host"), pc.get("port"), pc.get("sticky_minutes")))
    except Exception as e:
        _log("出口代理池不可用：%s" % str(e)[:160])
    try:
        from app import logs as _logsmod
        _ls = _logsmod.stats()
        _log("日志：领取记录 %s 条 / 审计 %s 条（审计只留最新 300 条）" % (_ls.get("events"), _ls.get("audit")))
        _logsmod.trim_audit()
    except Exception as e:
        _log("日志模块不可用：%s" % str(e)[:160])
    # ---- R76 持久化补核 worker（礼包异步发放：买家关页面/进程重启也还能补记成功）----
    try:
        from app import reconcile as _rcmod
        _th = _rcmod.start()
        _st = _rcmod.stats()
        _log("★ 持久补核：%s（间隔 %ss / 最长 %ss / 待补 %s）" % (
            "已启动" if _th else "未启动（RECONCILE!=1）",
            _st.get("interval"), _st.get("max_age"), _st.get("pending")))
    except Exception as e:
        _log("持久补核不可用：%s" % str(e)[:160])
    if not store.get_setting("admin_password_hash") and ADMIN_PASSWORD == "CHANGE_ME_PLEASE":
        _log("⚠ 后台密码仍是默认值，请到 /admin → 设置 里改掉")
    if MODE == "pool" and SOURCE.pool_total == 0:
        _log("⚠ pool 模式但设备池为空，请检查 data/devices.json")
    _log("后台密码来源：%s" % ("数据库（/admin 里改过）" if store.get_setting("admin_password_hash") else "环境变量 ADMIN_PASSWORD"))
    _log("每张卡密可领次数：1 次（R209 固定，不可配置）")
    _log("两步验证(谷歌验证器)：%s" % ("已启用" if _totp_enabled() else "★ 未启用（建议到 /admin → 安全 扫码绑定）"))
    if not store.get_setting("admin_password_hash"):
        _log("⚠ 后台密码当前来自环境变量，登录成功后会升级为 PBKDF2 哈希存储")
    _log("访问控制：后台 IP 白名单=%s（%d 条）；当前封禁 %d 个 IP"
         % ("★开启" if guard.whitelist_on() else "关闭", len(guard.whitelist()), len(guard.bans())))
    if guard.whitelist_on() and not guard.whitelist():
        _log("⚠ 白名单已开启但名单为空 = 等于不限制，请到 /admin → 安全 里填 IP")
    if not TRUST_PROXY:
        _log("限速按直连 IP 统计（若走 nginx，请设 TRUST_PROXY=1）")
    if BASE_URL:
        _log("对外链接前缀：%s（已固定，不受 Host 头影响）" % BASE_URL)
    elif ALLOWED_HOSTS:
        _log("对外链接前缀：按 ALLOWED_HOSTS 收敛（%s）" % ",".join(ALLOWED_HOSTS))
    else:
        _log("⚠ 未配 BASE_URL / ALLOWED_HOSTS：对外链接会跟随请求 Host，"
             "存在被伪造污染（钓鱼）的风险，建议配 BASE_URL")
    _log("会话机制：每兑换码独立设备指纹 + 独立二维码（MAX_SESSIONS=%s）"
         % os.environ.get("MAX_SESSIONS", "150"))
    if _R66_DL:
        try:
            _ds = dl_mod.stats()
            _log("★ 一次性下载链接：%d 条（可下载 %d / 已下载 %d / 过期 %d / 已撤销 %d），目录 %s"
                 % (_ds.get("total", 0), _ds.get("active", 0), _ds.get("used", 0),
                    _ds.get("expired", 0), _ds.get("revoked", 0), "data/dl"))
        except Exception as _de:
            _log("下载链接统计失败：%s" % str(_de)[:120])
    else:
        _log("✗ 一次性下载链接不可用（app/dl.py 未加载）")

# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# R213 卡密链接兑换站（前台 /redeem，后台 /admin 内嵌面板）
# --------------------------------------------------------------------------- #
try:
    from app import redeem as redeem_mod
    redeem_mod.install(app, db_path=store.DB_PATH, secret=SECRET,
                       admin_ok=_admin_ok, client_ip=_client_ip,
                       base_url=_base_url, log=_log, static_dir=STATIC_DIR)
    _log("★ R213 兑换站已挂载：/redeem（后台 /admin 内嵌面板）")
except Exception as _rde:
    _log("✗ R213 兑换站未加载：%s" % str(_rde)[:200])

# 通配兑换路由必须【最后】注册，否则 /healthz、/admin、/api/* 都会被它吃掉
# --------------------------------------------------------------------------- #
app.add_api_route("/{code}", redeem, methods=["GET"])


# --------------------------------------------------------------------------- #
# ★R87 总闸：放在**最外层**（最后一个 add_middleware）—— 不管路由白名单有没有漏，这里都漏不了。
# http 与 websocket 全拦；例外只有一个：**本机**访问 /healthz。
# --------------------------------------------------------------------------- #
CLOSED_HTML = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
               "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
               "<title>站点已暂停</title></head>"
               "<body style=\"margin:0;min-height:100vh;display:grid;place-items:center;"
               "background:#0f1115;color:#e8eaf0;font:16px/1.7 system-ui,-apple-system,'Segoe UI',sans-serif\">"
               "<div style=\"text-align:center;padding:40px 24px\">"
               "<div style=\"font-size:40px;line-height:1\">🚧</div>"
               "<h2 style=\"margin:16px 0 6px;font-size:19px;font-weight:600\">站点已暂停</h2>"
               "<p style=\"margin:0;color:#8b93a7\">本站暂时不可访问，请稍后再试。</p>"
               "</div></body></html>")

_LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost", "testclient", "")


class _ClosedGate:
    """★R87：SITE_CLOSED=1 时除本机 /healthz 外一律 503。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if SITE_CLOSED and scope.get("type") in ("http", "websocket"):
            path = scope.get("path") or ""
            host = ((scope.get("client") or ("", 0))[0] or "")
            if not (path == "/healthz" and host in _LOCAL_HOSTS):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = CLOSED_HTML.encode("utf-8")
                await send({"type": "http.response.start", "status": 503, "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode()),
                ]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


# ★R274：静态资源/页面 gzip（app.js 34KB→~9KB），省买家流量；放在总闸内侧，不影响封站逻辑
try:
    from starlette.middleware.gzip import GZipMiddleware

    class _GzipSafe(GZipMiddleware):
        """★R274：只压页面/脚本；/d/ 是一次性大文件下载，绝不能整包缓冲压缩。"""
        _SKIP = ("/d/", "/admin/api/dl", "/a/api/dl")

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http":
                _p = scope.get("path") or ""
                if any(_p.startswith(x) for x in self._SKIP):
                    await self.app(scope, receive, send)
                    return
            await super().__call__(scope, receive, send)

    app.add_middleware(_GzipSafe, minimum_size=1024)
except Exception:
    pass
app.add_middleware(_ClosedGate)
