# -*- coding: utf-8 -*-
"""
豆包 passport 纯协议 SDK（精简版，用于二维码领取服务）
- 复刻 passport jssdk 的 sign / qs / x-tt-passport-aid-sign
- 只用标准库 + requests，无浏览器、无客户端依赖
"""
from __future__ import annotations

import hashlib
import hmac as _hmaclib
import json
import os
import random
import time
import urllib.parse
from datetime import datetime, timezone
from datetime import datetime, timezone

# ★R86：轮询 check_qrconnect 单独用短超时（登录类请求仍是 20s）。
# 代理被限速时，20s 超时会让每张码的轮询线程各挂 20s ⇒ 线程越积越多，页面越来越卡。
QR_CHECK_TIMEOUT = float(os.environ.get("QR_CHECK_TIMEOUT", "8"))
# ★R211：核验是只读查询，单请求 8s 足够；原来 20s × 5 个接口 = 单轮最坏 100s，页面就像卡死
VERIFY_TIMEOUT = float(os.environ.get("VERIFY_TIMEOUT", "8"))
# ★R211：只认「本次新发放」——30 天套餐必须 >29.5 天。原来 25 天会把 2~5 天前领过的旧礼包当成新到账
GIFT_FRESH_DAYS = float(os.environ.get("GIFT_FRESH_DAYS", "29.5"))
# 近期重复：到期落在 25~29.5 天 = 大约 0.5~5 天前领过（同一账号通常不能再领）
GIFT_RECENT_DAYS = float(os.environ.get("GIFT_RECENT_DAYS", "25"))
# R212: login-time baseline tolerance (seconds)
GIFT_NEW_TOLERANCE = float(os.environ.get("GIFT_NEW_TOLERANCE", "90"))

# TLS 指纹：优先 curl_cffi 模拟 Chrome（绕过风控中间件），没有就退回 requests
try:
    from curl_cffi import requests as requests  # type: ignore
    _HAS_CFFI = True
except ImportError:  # pragma: no cover
    import requests  # type: ignore
    _HAS_CFFI = False


def _px(override=None):
    """出口代理：显式传入优先（会话级粘性），否则取当前线程的；都没开就 None = 直连。"""
    if override:
        return override
    try:
        from app import proxy as _proxy
        return _proxy.session_proxies()
    except Exception:
        return None


def _mk_session(px=None):
    px = px or _px()
    if _HAS_CFFI:
        s = None
        try:
            s = requests.Session(impersonate="chrome")
        except Exception:
            s = None
        if s is not None:
            if px:
                try:
                    s.proxies.update(px)
                except Exception:
                    pass
            return s
    s = requests.Session()
    if px:
        try:
            s.proxies.update(px)
        except Exception:
            pass
    return s

from app import fingerprint as _fpmod   # R69 设备指纹

APP_KEY = "2094abfcccefaeaffd24c622475fba10"   # passport jssdk appKey（硬编码）
AID = "497858"                                  # 豆包 web 的 aid
AID_PC = "582478"                               # 豆包电脑版 aid（二维码必须带这个才显示"豆包电脑版"）
PASSPORT_HOST = "https://accounts.doubao.com"
DOUBAO_HOST = "https://www.doubao.com"
JSSDK_VERSION = "4.4.6"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.7727.149 Safari/537.36")

# 验收用的业务接口 query string（与客户端一致）
BIZ_Q = ("?version_code=20800&language=zh&device_platform=web&doubao_device_platform=desktop&aid=582478"
         "&real_aid=582478&pkg_type=release_version&pc_version=2.31.4&doubao_pc_version=2.31.4"
         "&region=CN&sys_region=CN&samantha_web=1&web_platform=desktop&use-olympus-account=1&runtime=web"
         "&runtime_version=3.39.0&client_platform=pc_client&chromium_version=147.0.7727.149&channel=win")


# ----------------------------- 基础编码（逆向所得） -----------------------------
def xor5_hex(value) -> str:
    """SDK 的 encrypt()：UTF-8 每字节 XOR 0x05 -> 小写 hex。"""
    if value is None:
        return ""
    raw = value.encode("utf-8") if isinstance(value, str) else str(value).encode("utf-8")
    return "".join(format(b ^ 0x05, "x") for b in raw)


def js_encode(s) -> str:
    """等价 JS encodeURIComponent。"""
    return urllib.parse.quote(str(s), safe="-_.!~*'()")


def _js_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def obj_to_form(data: dict) -> str:
    return "&".join("%s=%s" % (js_encode(k), js_encode(_js_val(v))) for k, v in data.items())


def encrypt_params(data: dict, fields) -> dict:
    if not isinstance(data, dict) or not fields:
        return dict(data)
    out = {"mix_mode": 0}
    out.update({k: v for k, v in data.items() if v is not None})
    mode = fixed = 0
    for f in fields:
        if f in out and out[f] is not None:
            mode |= 1
            fixed |= 1
            out[f] = xor5_hex(out[f])
    out["mix_mode"] = mode
    out["fixed_mix_mode"] = fixed
    return out


def _sorted_kv(data: dict, limit: int = -1):
    keys = sorted(data.keys())
    if limit >= 0:
        keys = keys[:limit]
    parts = []
    for k in keys:
        v = data[k]
        v = json.dumps(v, separators=(",", ":"), ensure_ascii=False) if isinstance(v, (dict, list)) else v
        parts.append("%s=%s" % (k, v))
    return "&".join(parts), keys


def sign_and_qs(params: dict, extra: dict | None = None, app_key: str = APP_KEY):
    """sign = sha256(base前10个kv + '&' + body + '&app_key=..')；qs = encrypt(前10个key)"""
    base, keys = _sorted_kv(params, 10)
    extra_str = _sorted_kv(extra)[0] if extra else ""
    return hashlib.sha256(("%s&%s&app_key=%s" % (base, extra_str, app_key)).encode()).hexdigest(), xor5_hex(",".join(keys))


def daily_ts() -> int:
    """SDK 的 ts = 当天 UTC 12:00（固定值，不是当前时间）。"""
    now = datetime.now(timezone.utc)
    return int(datetime(now.year, now.month, now.day, 12, 0, 0, tzinfo=timezone.utc).timestamp())


def _hmac_hex(key: bytes, msg: bytes) -> str:
    return _hmaclib.new(key, msg, hashlib.sha256).hexdigest()


def _kdf_32(e: bytes, t: bytes) -> bytes:
    seed = bytes.fromhex(_hmac_hex(e, t))
    out, o, i = b"", "", 0
    while len(out) < 32:
        i += 1
        o = _hmac_hex(seed, bytes.fromhex(o) + b"" + bytes([i]))
        out += bytes.fromhex(o)
    return out[:32]


def aid_sign(path: str, ts=None, aid: str = AID, app_key: str = APP_KEY) -> str:
    ts = daily_ts() if ts is None else ts
    f = _kdf_32(str(ts).encode(), app_key.encode())
    return _hmac_hex(f, ("aid=%s&path=%s&ts=%s" % (aid, path, ts)).encode())


def build_sdk_source_info(hc: int = 8, fp: dict = None) -> str:
    """account_sdk_source_info（XOR-5 hex 的 JSON）。

    传 fp（R69 指纹）时，硬件/屏幕/webgl 与 UA 保持一致；不传则用原来的固定值。
    """
    now_ms = int(time.time() * 1000)
    fp = fp or {}
    _hw = fp.get("hw") or hc
    _scr = fp.get("screen") or (1440, 900)
    _in = fp.get("inner") or (1440, 900)
    _out = fp.get("outer") or (1440, 900)
    _wv = fp.get("wgl_v") or "Google Inc. (NVIDIA)"
    _wr = fp.get("wgl_r") or ("ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002504) "
                              "Direct3D11 vs_5_0 ps_5_0, D3D11)")
    info = {
        "hardwareConcurrency": _hw, "webdriver": False, "chromedriver": False, "shelldriver": False,
        "plugins": fp.get("plugins") or 3,
        "innerHeight": _in[1], "innerWidth": _in[0], "outerHeight": _out[1], "outerWidth": _out[0],
        "deviceMemory": fp.get("mem") or 8, "screenWidth": _scr[0], "screenHeight": _scr[1],
        "colorDepth": fp.get("depth") or 24, "devicePixelRatio": fp.get("dpr") or 1,
        "platform": fp.get("plat") or "Win32",
        "language": fp.get("lang") or "zh-CN",
        # R69-BG: locale / timezone / vendor / canvas / audio stay in the same profile (backward compatible)
        "languages": fp.get("langs") or ["zh-CN", "zh"],
        "timezone": fp.get("tz") or "Asia/Shanghai",
        "timezoneOffset": int(fp.get("tz_offset") if fp.get("tz_offset") is not None else -480),
        "vendor": fp.get("vendor") if fp.get("vendor") is not None else "Google Inc.",
        "productSub": fp.get("product_sub") or "20030107",
        "canvas": {"hash": fp.get("canvas_seed") or 0, "noise": 0},
        "audio": {"hash": fp.get("audio_seed") or 0},
        "uad": fp.get("ua") or UA,
        "webgl": {"vendor": _wv, "renderer": _wr},
        "automation": {"s": "00000000", "c": "0000", "p": "1000000", "s1": "10100000", "c1": "0011", "p1": "1"},
        "performance": {"timeOrigin": now_ms, "usedJSHeapSize": 90 * 1024 * 1024,
                        "navigationTiming": {"decodedBodySize": 539258, "entryType": "navigation",
                                             "initiatorType": "navigation", "name": "https://www.doubao.com/chat/",
                                             "renderBlockingStatus": "non-blocking", "serverTiming": "cdn-cache",
                                             "guleStart": "none", "guleDuration": "none"}},
        "browser": {"t": str(now_ms)[::-1], "bit_protocol": "", "bit_helper": False},
    }
    # ★ R82-K：把 PC 客户端身份一并塞进 source info（有才塞，老调用完全不受影响）
    pc = {kk: fp.get(kk) for kk in ("pc_uuid", "pc_serial", "device_token", "clientudid", "cdid",
                                    "tea_uuid", "device_type", "device_name", "device_model",
                                    "os_label", "package_name", "package_id", "app_channel",
                                    "runtime_version", "screen_inch", "ssid", "mac")
          if fp.get(kk)}
    if pc:
        info["pc"] = pc
    return xor5_hex(json.dumps(info, separators=(",", ":"), ensure_ascii=False))


# ----------------------------- 协议客户端 -----------------------------
# ----------------------------- R69 指纹 cookie / msToken -----------------------------
def seed_cookies(sess, fp: dict) -> None:
    """把指纹 cookie 种进会话（curl_cffi 与 requests 都兼容）。"""
    if not fp:
        return
    for name, val in (("device_id", fp.get("device_id")), ("iid", fp.get("iid")),
                      ("install_id", fp.get("install_id")), ("s_v_web_id", fp.get("verify_fp")),
                      ("webid", fp.get("webid")), ("_tea_web_id", fp.get("webid")),
                      # ★ R82-K：新增 web/设备身份 cookie（fp 没给就跳过，不改变旧行为）
                      ("tea_web_id", fp.get("tea_web_id")), ("web_id", fp.get("web_id")),
                      ("cdid", fp.get("cdid")), ("clientudid", fp.get("clientudid"))):
        if not val:
            continue
        for kw in ({"domain": ".doubao.com", "path": "/"}, {"domain": "www.doubao.com", "path": "/"},
                   {"path": "/"}, {}):
            try:
                sess.cookies.set(name, str(val), **kw)
                break
            except Exception:
                continue


MS_TOKEN_URLS = (
    "https://mssdk.bytedance.com/web/report?ms_appid=582478&aid=582478",
    "https://mssdk.bytedance.com/web/report?ms_appid=497858&aid=497858",
)


def fetch_ms_token(sess, fp: dict = None, timeout: int = 12) -> str:
    """尽力拿 msToken（拿不到就返回 ""，不阻断主流程）。"""
    for url in MS_TOKEN_URLS:
        for method in ("GET", "POST"):
            try:
                hdr = {"User-Agent": (fp or {}).get("ua") or UA,
                       "Accept": "application/json, text/plain, */*",
                       "Referer": "https://www.doubao.com/"}
                r = (sess.get(url, headers=hdr, timeout=timeout) if method == "GET"
                     else sess.post(url, headers=hdr, json={}, timeout=timeout))
                txt = r.text or ""
                if "msToken" in txt:
                    import re as _re
                    m = _re.search(r'"msToken"\s*:\s*"([^"]+)"', txt)
                    if m:
                        return m.group(1)
                    try:
                        j = r.json() or {}
                        if j.get("msToken"):
                            return j["msToken"]
                    except Exception:
                        pass
                ck = {}
                try:
                    ck = sess.cookies.get_dict()
                except Exception:
                    pass
                if ck.get("msToken"):
                    return ck["msToken"]
            except Exception:
                continue
    return ""


class PassportClient:
    """一个 session 对应一次扫码（token 绑定在该 session 的 cookie 上）。"""

    def __init__(self, timeout: int = 20, proxies=None, fp: dict = None):
        px = _px(proxies)
        self.proxies = px
        self.s = _mk_session(px)
        self.timeout = timeout
        self.fp = fp if fp is not None else _fpmod.pick("HK")     # R69 设备指纹
        self.ua = self.fp.get("ua") or UA
        self.sdk_source_info = build_sdk_source_info(fp=self.fp)
        self.biz_trace_id = "".join(random.choice("0123456789abcdef") for _ in range(8))

    # 基础 cookie（匿名）
    def boot(self):
        seed_cookies(self.s, getattr(self, "fp", None))
        try:
            self.s.get(DOUBAO_HOST + "/chat/", headers={"User-Agent": self.ua, "Accept": "text/html"},
                       timeout=self.timeout)
        except Exception:
            pass
        return dict(self.s.cookies)

    def _headers(self, referer="https://www.doubao.com/"):
        cks = self.s.cookies.get_dict()
        csrf = cks.get("passport_csrf_token") or cks.get("passport_csrf_token_default") or ""
        h = {
            "User-Agent": getattr(self, "ua", UA),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": referer,
            "Origin": "https://www.doubao.com",
            "Content-Type": "application/x-www-form-urlencoded",
            "x-tt-passport-csrf-token": csrf,
        }
        try:
            h.update({k: v for k, v in _fpmod.headers(getattr(self, "fp", None) or {}, referer).items()
                      if k != "Referer"})
        except Exception:
            pass
        return h

    def _base_params(self):
        return {
            "passport_jssdk_version": JSSDK_VERSION, "passport_jssdk_type": "normal", "is_from_ttaccountsdk": 1,
            "aid": AID, "language": "zh", "account_app_language": "zh-CN", "ts": daily_ts(),
            "p_ca": "4.0.26", "p_ca_real": "1.0.0.910", "multi_login": 1, "account_sdk_source": "web",
            "account_sdk_source_info": self.sdk_source_info, "biz_trace_id": self.biz_trace_id,
            "device_platform": "web", "os_type": 1, "terminal_type": 2,
        }
        f = getattr(self, "fp", None) or {}
        if f:
            p.update({
                "device_id": f.get("device_id") or "", "iid": f.get("iid") or "",
                "install_id": f.get("install_id") or "", "openudid": f.get("openudid") or "",
                "web_region": f.get("region") or "HK",
                "verifyFp": f.get("verify_fp") or "", "s_v_web_id": f.get("verify_fp") or "",
                "webid": f.get("webid") or "", "ua": f.get("ua") or "",
            })
        return p

    def request(self, path, body=None, method="POST", params=None, host=PASSPORT_HOST,
                referer="https://www.doubao.com/", timeout=None):
        p = self._base_params()
        if params:
            p.update(params)
        body = body or {}
        sign, qs = sign_and_qs(p, body if method.upper() == "POST" else {})
        p["sign"] = sign
        p["qs"] = qs
        headers = self._headers(referer)
        headers["x-tt-passport-aid-sign"] = aid_sign(path, p["ts"])
        kw = dict(params=p, headers=headers, timeout=(self.timeout if timeout is None else timeout))
        if method.upper() == "POST":
            kw["data"] = obj_to_form(body)
            r = self.s.post(host + path, **kw)
        else:
            r = self.s.get(host + path, **kw)
        try:
            return r.json()
        except Exception:
            return {"_http": r.status_code, "_text": r.text[:300]}

    # ---------------- 二维码 ----------------
    def get_qrcode(self, device):
        """申请登录二维码。必须带 aid=582478 才会返回 web_name=豆包电脑版。"""
        params = {
            "aid": AID_PC, "doubao_device_platform": "desktop", "web_platform": "desktop",
            "pc_version": "2.31.4", "doubao_pc_version": "2.31.4", "channel": "win", "version_code": "20800",
            "client_platform": "pc_client", "device_id": str(device["did"]),
            "iid": str(device.get("iid") or device["did"]), "tea_uuid": str(device["did"]),
            "next": "https://www.doubao.com/",
        }
        d = (self.request("/passport/web/get_qrcode/", {}, "GET", params=params, host=PASSPORT_HOST) or {}).get("data") or {}
        return d

    def check_qrconnect(self, token):
        # ★R86：轮询接口单独给短超时（默认 8s，env QR_CHECK_TIMEOUT 可调）。
        # 原来它跟登录共用 20s：代理一被限速，每张码的轮询线程各挂 20s ⇒ 线程越积越多，页面越来越卡。
        return (self.request("/passport/web/check_qrconnect/", None, "GET",
                             params={"token": token, "next": "https://www.doubao.com/"},
                             host=PASSPORT_HOST, timeout=QR_CHECK_TIMEOUT) or {}).get("data") or {}


# ----------------------------- 验收 -----------------------------
def _hdr(ck: dict):
    return {"User-Agent": UA, "content-type": "application/json", "agw-js-conv": "str",
            "Cookie": "; ".join("%s=%s" % (k, v) for k, v in ck.items())}


def account_info(ck: dict, proxies=None) -> dict:
    """只读账号信息（登录刚确认时先抢一步，好让领取页实时显示"登录账号"）。"""
    out = {"name": "", "mobile": "", "account": "", "raw": {}}
    try:
        r = requests.get("https://www.doubao.com/passport/account/info/v2/"
                         "?aid=582478&account_sdk_source=web&sdk_version=2.2.11-doubao.0"
                         "&device_platform=desktop", headers=_hdr(ck), timeout=12, proxies=_px(proxies))
        d = (r.json() or {}).get("data") or {}
    except Exception:
        return out
    name = d.get("name") or d.get("screen_name") or ""
    mobile = d.get("mobile") or ""
    out["name"], out["mobile"] = name, mobile
    out["account"] = ("%s / %s" % (name or "?", mobile)).strip(" /") if (name or mobile) else ""
    out["raw"] = {k: d.get(k) for k in ("name", "screen_name", "mobile", "email",
                                        "user_id", "sec_uid", "country_code") if d.get(k)}
    return out


def _fmt_cst(ms) -> str:
    """★R208：固定按北京时间（UTC+8）格式化，不依赖进程时区（代理/服务器 TZ 都不影响）。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(ms) / 1000.0 + 8 * 3600))
    except Exception:
        return ""


def verify_subscription(ck: dict, device: dict, proxies=None, full: bool = True,
                        since_ms: float = 0.0) -> dict:
    """验收：订阅记录(硬标准) + 会员入口 + 活动弹窗 + 活动资格

    ★R211b：full=False 时只查「账号 + 订阅记录 + 会员入口」，跳过只用于诊断的
    弹窗/活动接口 —— 复查（延迟补记/手动复查）只要判断到没到账，少 2 个请求就少两次超时。
    """
    hdr = _hdr(ck)
    dq = BIZ_Q + "&device_id=%s&iid=%s&tea_uuid=%s" % (device["did"], device.get("iid") or device["did"], device["did"])
    out = {"got": False, "items": [], "account": "", "entry": {}, "pop": None,
           "campaign_status": None, "errors": [], "gift_any": False, "gift_expired": False,
           "mobile": "", "country": "", "uid": "", "auth_invalid": False,
           "relogin_required": False, "auth_detail": "",
           "gift_new": False, "gift_duplicate": False}

    def _tag(tag, r):
        """收集网络层错误：用来区分「真没发放」和「没查成」。"""
        if isinstance(r, dict) and r.get("_err"):
            out["errors"].append("%s: %s" % (tag, str(r["_err"])[:120]))
        return r

    def post(url, body="{}", tag="post"):
        try:
            return _tag(tag, requests.post(url, headers=hdr, data=body, timeout=VERIFY_TIMEOUT, proxies=_px(proxies)).json())
        except Exception as e:
            return _tag(tag, {"_err": str(e)[:200]})

    def get(url, tag="get"):
        try:
            return _tag(tag, requests.get(url, headers=hdr, timeout=VERIFY_TIMEOUT, proxies=_px(proxies)).json())
        except Exception as e:
            return _tag(tag, {"_err": str(e)[:200]})

    a = get("https://www.doubao.com/passport/account/info/v2/?aid=582478&account_sdk_source=web"
            "&sdk_version=2.2.11-doubao.0&device_platform=desktop", tag="account")
    d = a.get("data") if isinstance(a.get("data"), dict) else {}
    _aerr = str(a.get("_err") or "")
    _aec = d.get("error_code") or a.get("error_code")
    _aname = str(d.get("name") or "")
    if (not _aerr) and (_aec == 13 or _aname == "account_info_error"):
        out["auth_invalid"] = True
        out["relogin_required"] = True
        out["auth_detail"] = str(d.get("description") or _aname or "login expired")[:120]
        out["account"] = "登录态已失效 /"
        out["entry"] = {"has_active": False, "name": "", "subs_status": "expired",
                        "country": d.get("country_code") or ""}
        return out
    out["account"] = "%s / %s" % (d.get("name") or d.get("screen_name") or "?", d.get("mobile") or "")
    out["mobile"] = d.get("mobile") or ""
    out["country"] = d.get("country_code") or d.get("country") or ""
    _uid = str(d.get("user_id") or d.get("user_id_str") or "").strip()
    if _uid and _uid != "0":
        out["uid"] = _uid

    sl = post("https://www.doubao.com/alice/commerce/sale/subscription/list/" + dq, tag="subscription/list")
    for g in ((sl.get("data") or {}).get("groups") or []):
        for x in (g.get("records") or []):
            ci = (x.get("campaign_infos") or [{}])[0]
            _eff = x.get("effective_time") or x.get("start_time") or x.get("create_time")
            out["items"].append({
                "source": x.get("subscription_source"),
                "tag": ci.get("subscription_campaign_tag"),
                "product": (x.get("display") or {}).get("product_name"),
                "end_ms": x.get("end_time"),
                "end": _fmt_cst(x.get("end_time")),
                "effective_ms": _eff,
                "effective": _fmt_cst(_eff),
                "start_ms": x.get("start_time") or x.get("create_time"),
                "is_gift": x.get("is_gift"),
                "status": x.get("status"),
                "subscription_id": x.get("subscription_id"),
            })
    gifts = [i for i in out["items"] if i.get("source") == "pc_membership_gift"]
    now_ms = time.time() * 1000
    out["gift_any"] = bool(gifts)
    out["gift_expired"] = bool(gifts) and all((float(i.get("end_ms") or 0) < now_ms) for i in gifts)
    _fresh = [i for i in gifts if float(i.get("end_ms") or 0) > now_ms + GIFT_FRESH_DAYS * 86400 * 1000]
    out["gift_fresh"] = bool(_fresh)
    if _fresh:
        out["gift_fresh_end"] = max((i.get("end") or "") for i in _fresh)
    _recent = [i for i in gifts
               if now_ms + GIFT_RECENT_DAYS * 86400 * 1000 < float(i.get("end_ms") or 0)
               <= now_ms + GIFT_FRESH_DAYS * 86400 * 1000]
    out["gift_recent"] = bool(_recent)
    if _recent:
        out["gift_recent_end"] = max((i.get("end") or "") for i in _recent)
    if gifts and not _fresh:
        out["gift_stale_end"] = max((i.get("end") or "") for i in gifts)

    # R212: use effective_time to tell THIS login's new gift from an old one.
    _with_eff = [i for i in gifts if i.get("effective_ms")]
    if since_ms and _with_eff:
        _since = float(since_ms)
        _new = [i for i in _fresh if float(i.get("effective_ms") or 0) >= _since]
        _new_ids = set(id(i) for i in _new)
        _dup = [i for i in _fresh if id(i) not in _new_ids]
        out["gift_new"] = bool(_new)
        out["got"] = bool(_new)
        if _new:
            out["gift_new_end"] = max((i.get("end") or "") for i in _new)
            out["gift_new_effective"] = max((i.get("effective") or "") for i in _new)
        out["gift_duplicate"] = bool(_dup)
        if _dup:
            out["gift_duplicate_end"] = max((i.get("end") or "") for i in _dup)
            out["gift_duplicate_effective"] = max((i.get("effective") or "") for i in _dup)
    else:
        out["gift_new"] = bool(_fresh)
        out["got"] = bool(_fresh)

    ec = post("https://www.doubao.com/alice/commerce/sale/subscription/entry/config/" + dq, tag="entry/config")
    e = ec.get("data") or {}
    # ★R213：新版 entry/config 不再返回 subs_status；用 has_active_subscription 兜底，
    # 否则前端/失败原因判断会把「免费号」当成「未知」。
    _ss = e.get("subs_status") or ("free_vip" if e.get("has_active_subscription") else "free")
    out["entry"] = {"has_active": e.get("has_active_subscription"), "name": e.get("membership_display_name"),
                    "subs_status": _ss, "country": e.get("country_code")}
    if not full:
        return out

    pop = post("https://www.doubao.com/biz/activity/get_pop_window" + dq,
               json.dumps({"pc_os": "Windows", "scene": 8, "surface": 2, "ug_campaign_info": {}}), tag="pop_window")
    pd = pop.get("data") or {}
    plist = pd.get("pop_window_list") or []
    out["pop"] = (plist[0].get("pop_window_conf", {}).get("text", {}).get("title") if plist else None)

    cq = post("https://www.doubao.com/alice/commerce/sale/campaign/page/query/" + dq, tag="campaign/query")
    cd = cq.get("data")
    out["campaign_status"] = cd.get("page_status") if isinstance(cd, dict) else None
    return out
