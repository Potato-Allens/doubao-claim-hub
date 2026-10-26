# -*- coding: utf-8 -*-
"""出口代理池（agent D 交付）—— 严格实现 R63 契约第 4 节。

目标：让「同一张兑换码」从出码到核验全程走同一个出口 IP（会话级粘性），
并把出口选择做成**线程内上下文**，交给 sdk.py 的 requests / curl_cffi 使用。

安全底线：
  * 任何异常都不许冒泡成 500；测试失败自动回落直连，绝不因代理挂掉拖垮整站。
  * 绝不把密码写进日志（对外展示一律掩码成 ***）。
"""
from __future__ import annotations

import hashlib
import json
import re
import random
import string
import threading
import time
from urllib.parse import urlsplit

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

# ---- 默认配置（用户给的 1024 账号，可在后台改） ----
DEFAULTS = {
    "enabled": False,
    "mode": "api",                     # api | list | off
    "host": "proxy.example.com",
    "port": 3000,
    "username_template": "USERNAME-region-{region}-sid-{sid}-t-5",   # 换成你自己的账号模板
    "region": "Rand",                    # 出口地区：Rand/auto = 按「兜底地区」固定；也可填 US/SG/JP/DE...（固定区域）
    "fallback_region": "US",             # ★R71：地区填 Rand/auto 时的兜底国家。实测美国出口组合成功率高，随机落到 HK/TH/RU 是重灾区
    "password": "",                       # 在后台「出口代理」里填，不要写进代码
    "protocol": "http",
    "sid_digits": 8,
    "sticky_minutes": 10,
    "list_text": "",
    "last_test": {},
}

_KEY = "proxy_"                       # settings 表键名前缀
_SID_ALPHABET = string.ascii_uppercase + string.digits

_tl = threading.local()               # 线程内当前出口
_lock = threading.RLock()
_leases = {}                          # sid -> {"key","ts","expire","url"}
_sid_override = {}                      # key -> {"sid","at"}：rotate 后覆盖确定性 sid（换出口用）
_counters = {"ok": 0, "fail": 0}
_last_error = ""
_rr = 0                               # list 模式轮询指针


# ---------------------------------------------------------------- 配置读写
def _skey(name: str) -> str:
    return _KEY + name


def _coerce(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    if isinstance(cfg, dict):
        out.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    out["enabled"] = bool(out.get("enabled"))
    m = str(out.get("mode") or "api").lower()
    out["mode"] = m if m in ("api", "list", "off") else "api"
    out["host"] = str(out.get("host") or DEFAULTS["host"]).strip()
    try:
        out["port"] = int(str(out.get("port") or DEFAULTS["port"]).strip())
    except Exception:
        out["port"] = DEFAULTS["port"]
    out["username_template"] = str(out.get("username_template") or DEFAULTS["username_template"])
    out["region"] = (re.sub(r"[^A-Za-z0-9_-]", "", str(out.get("region") or "Rand")).strip() or "Rand")
    out["fallback_region"] = (re.sub(r"[^A-Za-z]", "", str(out.get("fallback_region") or "US")).strip().upper() or "US")[:6]
    out["password"] = str(out.get("password") or "")
    out["protocol"] = str(out.get("protocol") or "http").lower().replace("://", "").strip(":/")
    try:
        out["sid_digits"] = max(4, min(int(out.get("sid_digits") or 8), 24))
    except Exception:
        out["sid_digits"] = 8
    try:
        out["sticky_minutes"] = max(1, min(int(out.get("sticky_minutes") or 10), 120))
    except Exception:
        out["sticky_minutes"] = 10
    out["list_text"] = str(out.get("list_text") or "")
    lt = out.get("last_test")
    out["last_test"] = lt if isinstance(lt, dict) else {}
    return out


def get_config() -> dict:
    """读取当前代理配置（含 password，供后台编辑；对外展示请自行掩码）。"""
    cfg = dict(DEFAULTS)
    try:
        from app import store  # type: ignore
    except Exception:
        return _coerce(cfg)
    for k in DEFAULTS:
        try:
            raw = store.get_setting(_skey(k), "")
        except Exception:
            continue
        if raw in ("", None):
            continue
        try:
            cfg[k] = json.loads(raw)
        except Exception:
            cfg[k] = raw          # 兼容手写非 JSON 值
    return _coerce(cfg)


def set_config(**kw) -> dict:
    """写入配置（只认白名单字段），返回最新配置。"""
    allowed = set(DEFAULTS.keys()) - {"last_test"}
    with _lock:
        for k, v in (kw or {}).items():
            if k not in allowed:
                continue
            if k == "enabled":
                v = bool(v)
            elif k in ("port", "sid_digits", "sticky_minutes"):
                try:
                    v = int(v)
                except Exception:
                    continue
            else:
                v = str(v)
            try:
                from app import store  # type: ignore
                store.set_setting(_skey(k), json.dumps(v, ensure_ascii=False))
            except Exception:
                pass
    return get_config()


# ---------------------------------------------------------------- 出口构造
def _sid_for(key: str, digits: int) -> str:
    """[旧逻辑，仅作兜底] key（兑换码）-> 哈希 sid；无 key 时随机。

    ★R73 起不再默认使用：1024 是「按 sid 绑定出口节点」，`region-XX` 只在该 sid **第一次**使用时生效
    （线上实测：同一个 sid 依次请求 region-US / region-HK / region-US，出口 IP 一模一样 203.0.113.30/US）。
    所以 sid = sha256(code) 会导致「某张码一旦在旧策略下绑到 HK 节点，之后就算 region-US 也永远拿 HK 出口」。
    现在 lease() 会给新码**现场随机**一个 sid 并记进 _sid_override（进程内保持稳定）。
    """
    if not key:
        return "".join(random.choice(_SID_ALPHABET) for _ in range(digits))
    h = hashlib.sha256(str(key).encode("utf-8")).hexdigest().upper()
    keep = "".join(ch for ch in h if ch in _SID_ALPHABET)
    return (keep + "0" * digits)[:digits]


# ---- 地区解析（R71）----------------------------------------------------------
# 「Rand / auto / 空」= 随机国家，会让同一张码的出口在 HK/TH/RU 之间乱跳（生产实测：误切 HK 19 次，
# 是失败重灾区）。R71 起把 auto 解析成**固定的兜底国家**，保证同一张码出口国家确定。
_AUTO_REGIONS = {"", "RAND", "AUTO", "RANDOM", "ANY", "AUTOROTATE", "AUTO-ROTATE"}


def is_auto_region(region) -> bool:
    return str(region or "").strip().upper() in _AUTO_REGIONS


def _resolve_region(cfg: dict, region: str = None) -> str:
    """把 NULL/auto/Rand 解析成确定的出口国家（默认兜底 US）。"""
    reg = str(region if region is not None else (cfg or {}).get("region") or "").strip().upper()
    if is_auto_region(reg):
        reg = str((cfg or {}).get("fallback_region") or "US").strip().upper()
    return reg or "US"


def _api_proxy_url(cfg: dict, sid: str, region: str = None) -> str:
    reg = _resolve_region(cfg, region)
    user = cfg["username_template"].replace("{sid}", sid).replace("{region}", reg)
    # 兼容模板里写死的 region-Rand / region-US：后台改了「出口地区」一定生效
    user = re.sub(r"(?i)region-[A-Za-z]{2,}", "region-" + reg, user)
    proto = cfg["protocol"] or "http"
    return "%s://%s:%s@%s:%d" % (proto, user, cfg["password"], cfg["host"], cfg["port"])


def _norm_list_entry(s: str):
    s = (s or "").strip()
    if not s or s.startswith("#") or s.startswith("//"):
        return None
    if "://" in s:
        return s if "@" in s else None
    parts = [p.strip() for p in s.split(":")]
    if len(parts) == 4:
        h, p, u, pw = parts
        return "http://%s:%s@%s:%s" % (u, pw, h, p)
    if len(parts) == 2:
        return "http://%s:%s" % (parts[0], parts[1])
    return None


def _list_urls(cfg: dict):
    urls = []
    for raw in (cfg.get("list_text") or "").splitlines():
        u = _norm_list_entry(raw)
        if u:
            urls.append(u)
    return urls


def _pick_list(cfg: dict, key: str = ""):
    global _rr
    urls = _list_urls(cfg)
    if not urls:
        return None
    if key:
        idx = int(hashlib.sha256(str(key).encode("utf-8")).hexdigest(), 16) % len(urls)
        return urls[idx]
    with _lock:
        u = urls[_rr % len(urls)]
        _rr += 1
    return u


def _url_host_port(url: str):
    try:
        sp = urlsplit(url)
        return sp.hostname or "", sp.port or 0
    except Exception:
        return "", 0


def _prune():
    now = time.time()
    for sid in [s for s, v in _leases.items() if (v.get("expire") or 0) <= now]:
        _leases.pop(sid, None)


def _set_last_error(msg: str):
    global _last_error
    with _lock:
        _last_error = _mask(msg)[:200]


# ---------------------------------------------------------------- 换出口
def rotate(key: str = ""):
    """把这张码的出口**强制换成一个全新 sid**（旧出口 IP 连不通/被掐断时用）。

    背景（生产实测 2026-10-13）：sid 是由 code 哈希出来的确定性值，所以一旦某个
    出口 IP 坏了，这张码会一直失败到 sid 过期（实测 DEMO0024 连续 4 分钟每次
    都 SSL_connect 被中断）。此函数给该 code 记一个覆盖 sid，lease() 会用它。
    """
    cfg = get_config()
    if not cfg["enabled"] or cfg["mode"] != "api":
        return lease(key)
    k = str(key or "")
    digits = max(4, int(cfg.get("sid_digits") or 8))
    with _lock:
        ov = _sid_override.get(k) or {}
        old = ov.get("sid") or (_sid_for(k, digits) if k else "")
        if old:
            _leases.pop(old, None)
        if k:
            _keep = (_sid_override.get(k) or {}).get("region")   # 换 sid 不能丢地区
            _sid_override[k] = {"sid": "".join(random.choice(_SID_ALPHABET) for _ in range(digits)),
                                "at": time.time(), "region": _keep}
            if len(_sid_override) > 4000:          # 控制内存
                for kk in sorted(_sid_override, key=lambda x: _sid_override[x].get("at") or 0)[:1000]:
                    _sid_override.pop(kk, None)
    return lease(k)


# ---------------------------------------------------------------- 出口地区
def set_region(key: str, region: str):
    """给**这张码**单独指定出口地区（例：买家手机号是美国 → region-US）。

    地区变了，同一个 sid 对应的出口节点也就变了，所以顺手清掉旧租约。
    """
    k = str(key or "")
    reg = re.sub(r"[^A-Za-z0-9_-]", "", str(region or "")).strip()
    if not k or not reg:
        return lease(key)
    cfg = get_config()
    if not is_auto_region(reg):
        reg = _resolve_region(cfg, reg)
    if not cfg["enabled"] or cfg["mode"] == "off":
        return None
    if cfg["mode"] == "list":
        return lease(k)
    with _lock:
        ov = _sid_override.get(k) or {}
        if ov.get("region") == reg:
            return lease(k)
        sid = ov.get("sid") or _sid_for(k, max(4, int(cfg.get("sid_digits") or 8)))
        _leases.pop(sid, None)                      # 旧出口租约作废
        _sid_override[k] = {"sid": ov.get("sid") or "", "at": time.time(), "region": reg}
        if len(_sid_override) > 4000:
            for kk in sorted(_sid_override, key=lambda x: _sid_override[x].get("at") or 0)[:1000]:
                _sid_override.pop(kk, None)
    return lease(k)


def clear_region(key: str):
    """清掉这张码的出口地区覆盖 → 跟随全局配置（前端「自动」选项）。"""
    k = str(key or "")
    if not k:
        return lease(key)
    with _lock:
        ov = _sid_override.get(k)
        if ov and ov.get("region"):
            sid = ov.get("sid") or ""
            if sid:
                _leases.pop(sid, None)
            nxt = dict(ov)
            nxt.pop("region", None)
            if nxt.get("sid"):
                _sid_override[k] = nxt
            else:
                _sid_override.pop(k, None)
    return lease(k)


def region_of(key: str) -> str:
    """这张码实际使用的出口地区。"""
    cfg = get_config()
    return _resolve_region(cfg, (_sid_override.get(str(key or "")) or {}).get("region") or cfg.get("region"))


# ---------------------------------------------------------------- 领取出口
def lease(key: str = ""):
    """领取一个粘性出口；同一张码永远同一出口 IP。开关关闭时返回 None。"""
    cfg = get_config()
    if not cfg["enabled"] or cfg["mode"] == "off":
        return None
    if cfg["mode"] == "list":
        url = _pick_list(cfg, key)
        if not url:
            _set_last_error("代理名单为空，请先在后台填写代理地址")
            return None
        h, p = _url_host_port(url)
        return {"url": url, "host": h, "port": p, "sid": "",
                "expire": time.time() + cfg["sticky_minutes"] * 60, "origin": "list"}
    _ov = _sid_override.get(str(key or ""))
    if key and not _ov:
        # ★R73：第一次为这张码建出口 -> 现随机一个全新 sid（避免复用/继承旧绑定），进程内保持稳定
        try:
            _ov = {"sid": "".join(random.choice(_SID_ALPHABET) for _ in range(max(4, int(cfg.get("sid_digits") or 8)))),
                   "at": time.time()}
            with _lock:
                _sid_override[str(key)] = _ov
                if len(_sid_override) > 4000:            # 控制内存
                    for kk in sorted(_sid_override, key=lambda x: _sid_override[x].get("at") or 0)[:1000]:
                        _sid_override.pop(kk, None)
        except Exception:
            _ov = None
    sid = (_ov or {}).get("sid") or _sid_for(key, cfg["sid_digits"])
    url = _api_proxy_url(cfg, sid, (_ov or {}).get("region"))
    p = {"url": url, "host": cfg["host"], "port": cfg["port"], "sid": sid,
         "expire": time.time() + cfg["sticky_minutes"] * 60, "origin": "api"}
    try:
        with _lock:
            _leases[sid] = {"key": str(key or ""), "ts": time.time(),
                            "expire": p["expire"], "url": url}
            _prune()
    except Exception:
        pass
    return p


# ---------------------------------------------------------------- 线程上下文
def set_current(p):
    """把当前线程的出口固定为 p（同一线程所有出网请求都走它）。"""
    try:
        _tl.proxy = p
    except Exception:
        pass


def current():
    return getattr(_tl, "proxy", None)


def clear_current():
    try:
        delattr(_tl, "proxy")
    except Exception:
        pass


def session_proxies():
    """给 requests / curl_cffi 用的 proxies dict；没开代理池则返回 None（= 直连）。"""
    p = current()
    if not p:
        return None
    url = p.get("url") if isinstance(p, dict) else None
    if not url:
        return None
    return {"http": url, "https": url}


# ---------------------------------------------------------------- 统计 / 测试
def stats() -> dict:
    cfg = get_config()
    with _lock:
        _prune()
        now = time.time()
        alive = sum(1 for x in _leases.values() if (x.get("expire") or 0) > now)
        size = len(_list_urls(cfg)) if cfg["mode"] == "list" else len(_leases)
        counters = dict(_counters)
        le = _last_error
    return {"enabled": cfg["enabled"], "mode": cfg["mode"], "size": size, "alive": alive,
            "ok": counters["ok"], "fail": counters["fail"], "last_error": le,
            "sticky_minutes": cfg["sticky_minutes"], "region": _resolve_region(cfg, cfg.get("region")),
            "region_raw": cfg.get("region") or "Rand", "fallback_region": cfg.get("fallback_region") or "US",
            "last_test": cfg.get("last_test") or {}}


def _mask(s) -> str:
    """把 password / user:pass 掩码，绝不外泄凭据。"""
    try:
        s = str(s or "")
        cfg = get_config()
        pw = cfg.get("password") or ""
        if pw and pw in s:
            s = s.replace(pw, "***")
        if "://" in s and "@" in s:
            head, rest = s.split("://", 1)
            if "@" in rest:
                cred, host = rest.split("@", 1)
                if ":" in cred:
                    u = cred.split(":", 1)[0]
                    s = head + "://" + u + ":***@" + host
    except Exception:
        pass
    return s


def _human_err(e) -> str:
    low = (str(e) or "").lower()
    if "timed out" in low or "timeout" in low:
        return "连接代理超时（检查地址/端口或本机网络）"
    if "407" in low or "proxy authentication" in low or "auth" in low and "proxy" in low:
        return "代理认证失败（检查账号/密码是否过期）"
    if "refused" in low:
        return "代理拒绝连接（检查地址/端口）"
    if "getaddrinfo" in low or "name or service not known" in low or "nodename" in low or "resolve" in low:
        return "代理域名无法解析（检查地址填写）"
    if "ssl" in low:
        return "代理 TLS 握手失败（协议可能应为 http）"
    if "connect" in low:
        return "无法连上代理服务器（检查地址/端口/白名单）"
    return "连接代理失败：" + _mask(e)[:80]


def _probe_target(cfg: dict, region: str = None):
    if cfg["mode"] == "list":
        u = _pick_list(cfg, "")
        return {"url": u} if u else None
    sid = _sid_for("", cfg["sid_digits"])
    return {"url": _api_proxy_url(cfg, sid, region)}


def _probe(p: dict) -> dict:
    url = p.get("url")
    proxies = {"http": url, "https": url}
    t0 = time.time()
    try:
        if requests is None:
            return {"ok": False, "error": "缺少 requests 库，无法测试代理"}
        r = requests.get("http://ipinfo.io/ip", proxies=proxies, timeout=8,
                         headers={"User-Agent": "curl/8.0"})
        ms = int((time.time() - t0) * 1000)
        sc = getattr(r, "status_code", 0)
        body = (r.text or "").strip()
        if sc == 200 and body and len(body) <= 64:
            return {"ok": True, "ip": body, "ms": ms}
        low = body.lower()
        if "forbidden ip" in low or (sc == 403 and "not supported" in low):
            return {"ok": False, "error": "代理拒绝了本机出口 IP（该 IP 未加入 1024 后台白名单）", "ms": ms, "detail": body[:120]}
        if sc == 403:
            return {"ok": False, "error": "代理返回 403（被拒：检查白名单或目标域名）", "ms": ms, "detail": body[:120]}
        if sc == 407:
            return {"ok": False, "error": "代理认证失败（检查账号/密码）", "ms": ms}
        return {"ok": False, "error": "代理返回异常（HTTP %s）" % sc, "ms": ms, "detail": body[:120]}
    except Exception as e:
        return {"ok": False, "error": _human_err(e)}


def _persist_last_test(info: dict):
    try:
        from app import store  # type: ignore
        store.set_setting(_skey("last_test"), json.dumps(info, ensure_ascii=False))
    except Exception:
        pass


def test(n: int = 1) -> dict:
    """真连一次 ipinfo.io/ip。绝不抛异常：失败返回中文人话错误。"""
    cfg = get_config()
    try:
        tries = max(1, min(int(n or 1), 10))
    except Exception:
        tries = 1
    results = []
    best = None
    for _i in range(tries):
        tgt = _probe_target(cfg)
        if not tgt:
            info = {"ok": False, "error": "代理名单为空，请先在后台填写代理地址", "tried": len(results)}
            _set_last_error(info["error"])
            _persist_last_test(info)
            return info
        r = _probe(tgt)
        results.append(r)
        if r.get("ok"):
            best = r
            break
    now = int(time.time())
    if best:
        with _lock:
            _counters["ok"] += 1
        info = {"ok": True, "ip": best["ip"], "ms": best["ms"], "tried": len(results), "at": now}
    else:
        err = (results[-1].get("error") if results else "") or "连接代理失败"
        with _lock:
            _counters["fail"] += 1
        _set_last_error(err)
        info = {"ok": False, "error": err, "tried": len(results), "at": now}
    _persist_last_test(info)
    return info
