# -*- coding: utf-8 -*-
"""R217: 手机号通道「官方人机验证（3D/滑块）」自动通过（多 worker 版）。

把 send_code 被 1105/1107 拦下的号码交给本地 captcha-worker（真 Chromium），
由它点选 3D / 拖动滑块；验证通过后把 cookie + 发送参数交回纯协议客户端。
worker 地址：CAPTCHA_WORKER_URLS（逗号分隔，默认 http://127.0.0.1:8099,http://127.0.0.1:8100）
关闭开关：CAPTCHA_WORKER_OFF=1
排队等待：CAPTCHA_WORKER_WAIT（默认 300 秒）
单次超时：CAPTCHA_WORKER_TIMEOUT（默认 420 秒）
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

_RAW = (os.environ.get("CAPTCHA_WORKER_URLS") or os.environ.get("CAPTCHA_WORKER_URL", "http://127.0.0.1:8099,http://127.0.0.1:8100") or "")
WORKER_URLS = [u.strip().rstrip("/") for u in _RAW.split(",") if u.strip()]
WORKER_URL = WORKER_URLS[0] if WORKER_URLS else ""
TIMEOUT = int(os.environ.get("CAPTCHA_WORKER_TIMEOUT", "420") or "420")
WAIT = int(os.environ.get("CAPTCHA_WORKER_WAIT", "300") or "300")

# 本机调用必须绕过任何 http_proxy 环境变量（否则会把 127.0.0.1 也发给代理）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def enabled() -> bool:
    if not WORKER_URLS:
        return False
    return str(os.environ.get("CAPTCHA_WORKER_OFF", "")).strip().lower() not in ("1", "true", "yes", "on")


def _proxy_url(proxies):
    if isinstance(proxies, dict):
        return str(proxies.get("https") or proxies.get("http") or "")
    if isinstance(proxies, str):
        return proxies
    return ""


def health() -> dict:
    out = {}
    for u in WORKER_URLS:
        try:
            with _OPENER.open(u + "/health", timeout=4) as r:
                out[u] = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            out[u] = {"ok": False, "err": "%s: %s" % (type(e).__name__, str(e)[:80])}
    return out


def _post(url: str, body: bytes) -> tuple:
    req = urllib.request.Request(url + "/solve_send", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return (200, d if isinstance(d, dict) else {"ok": False, "reason": "bad_worker_reply"})
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return (429, {"ok": False, "reason": "busy"})
        try:
            d = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            d = {"ok": False, "reason": "http_%s" % e.code}
        return (e.code, d)
    except Exception as e:
        return (0, {"ok": False, "reason": "worker_%s" % type(e).__name__, "err": str(e)[:160]})


def solve_send(phone: str, proxies=None, ua: str = "", logger=None) -> dict:
    """返回 {ok, reason, cookies, params, mobile_ticket, verify_subtype, elapsed}。"""
    _log = logger or (lambda m: None)
    if not enabled():
        return {"ok": False, "reason": "disabled"}
    body = json.dumps({"phone": phone, "ua": ua or "", "proxy": _proxy_url(proxies)},
                      ensure_ascii=False).encode("utf-8")
    tries = max(1, WAIT // 3)
    last = None
    net_dead = 0
    slide_tried = 0   # ★R277：slide 已串行尝试的 worker 数
    for i in range(tries):
        busy = 0
        net_fail = 0
        for u in WORKER_URLS:
            code, d = _post(u, body)
            if code == 429:
                busy += 1
                last = d
                continue
            if d.get("ok"):
                return d
            # ★R277_SLIDE_CAP：slide 每台 worker 内部已换设备重试 2 次（R266，约 40s/台），
            # 再串行 5 台会把用户拖到 4~6 分钟。最多串 2 台；外层 R251 还会再给一轮。
            if str(d.get("reason") or "") == "captcha_failed" and str(d.get("subtype") or "") == "slide":
                slide_tried += 1
                if slide_tried >= int(os.environ.get("CAPTCHA_SLIDE_MAX_WORKERS", "2") or "2"):
                    _log("[captcha-worker] slide 已试 %d 台 → 停止串行换 worker（R277，避免长时间卡进度条）" % slide_tried)
                    return d
            # ★R248_NO_CHAIN_3D：3d 的 captcha_failed 是一次 3~5 分钟的完整尝试，
            # 再换 worker 重来会把人拖到 10~15 分钟；slide 失败只要 ~25s（r248 会先换题），
            # 换一台再抽一次很划算，所以只对非 slide 的 captcha_failed 立即返回。
            if str(d.get("reason") or "") == "captcha_failed" and str(d.get("subtype") or "") != "slide":
                _log("[captcha-worker] captcha_failed(%s) → 不再串行换 worker（3d 重跑太贵）"
                     % (str(d.get("subtype") or "") or "?"))
                return d
            if code == 0:
                net_fail += 1
                last = d
                continue          # worker 掉线，换下一个
            # 业务失败（captcha_failed / trigger_xxx…）：留作兜底，换下一个 worker 再试一次
            last = d
        # ★R249_NET_RETRY：全部 worker 都连不上（刚好在重启/崩溃）时，旧逻辑当场把
        # worker_URLError 丢给用户；这里给 3 轮、每轮 2s 的短重试，最多多等 ~6s。
        if net_fail >= len(WORKER_URLS):
            net_dead += 1
            if net_dead < 3 and (i + 1) < tries:
                _log("[captcha-worker] 全部连不上（第 %d/3 次）→ 2s 后重试" % net_dead)
                time.sleep(2)
                continue
            return last
        if busy >= len(WORKER_URLS):
            if i == 0:
                _log("[captcha-worker] 全部忙，排队等待（最多 %ds）" % WAIT)
            time.sleep(3)
            continue
        if last is not None:
            return last
        time.sleep(2)
    return last or {"ok": False, "reason": "busy"}
