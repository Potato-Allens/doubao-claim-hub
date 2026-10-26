# -*- coding: utf-8 -*-
"""访问控制：IP 白名单 + 自动/手动封禁（持久化在 SQLite 的 settings 表，重启不丢）。

设计要点
--------
* 白名单只护「后台入口」：配了白名单后，非白名单 IP 连后台页面/接口都看不到。
* 封禁是「全站」生效：被封的 IP 访问任何路径都 403。
* 白名单里的 IP 永远不会被自动封禁（防止把自己锁在外面）。
* 一键「把当前 IP 加白」：面板点一下即可，不用手打 IP。
"""
from __future__ import annotations

import ipaddress
import json
import os
import time

from app import store


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _split(s) -> list:
    return [x.strip() for x in (s or "").replace(";", ",").replace("\n", ",").split(",") if x.strip()]


def normalize(ip: str) -> str:
    try:
        return str(ipaddress.ip_address((ip or "").strip()))
    except Exception:
        return (ip or "").strip()


def valid_rule(x: str):
    """单个 IP 或 CIDR 段 -> 规范化字符串；非法返回 None。"""
    x = (x or "").strip()
    if not x:
        return None
    try:
        if "/" in x:
            return str(ipaddress.ip_network(x, strict=False))
        return str(ipaddress.ip_address(x))
    except Exception:
        return None


def parse_rules(items) -> list:
    out = []
    for it in items:
        v = valid_rule(it)
        if not v:
            continue
        try:
            out.append(ipaddress.ip_network(v, strict=False))
        except Exception:
            continue
    return out


def ip_in(ip: str, nets) -> bool:
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except Exception:
        return False
    for n in nets:
        try:
            if addr in n:
                return True
        except Exception:
            pass
    return False


# --------------------------------------------------------------------------- #
# 白名单（只作用于后台入口）
# --------------------------------------------------------------------------- #
def whitelist() -> list:
    raw = store.get_setting("ip_whitelist")
    if raw == "":
        raw = os.environ.get("ADMIN_IP_WHITELIST", "")   # 首次启动可用环境变量预置
    return _split(raw)


def whitelist_on() -> bool:
    v = store.get_setting("ip_whitelist_on")
    if v == "":
        return bool(whitelist())        # 预置了名单就默认开启
    return v == "1"


def set_whitelist(items) -> list:
    valid = []
    for it in items:
        v = valid_rule(it)
        if v and v not in valid:
            valid.append(v)
    store.set_setting("ip_whitelist", ",".join(valid))
    return valid


def set_whitelist_on(on: bool):
    store.set_setting("ip_whitelist_on", "1" if on else "0")


def allowed(ip: str) -> bool:
    nets = parse_rules(whitelist())
    if not nets:
        return True                     # 没配名单 = 不限制
    return ip_in(ip, nets)


# --------------------------------------------------------------------------- #
# 封禁
# --------------------------------------------------------------------------- #
def _load() -> dict:
    try:
        d = json.loads(store.get_setting("ip_bans") or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict):
    store.set_setting("ip_bans", json.dumps(d))


def bans(active_only: bool = True) -> list:
    now = time.time()
    out = []
    for ip, v in _load().items():
        if not isinstance(v, dict):
            continue
        until = float(v.get("until") or 0)
        if active_only and (not until or until < now):
            continue          # until=0 = 只记了失败次数、并未封禁
        out.append({"ip": ip, "until": until, "left": max(0, int(until - now)),
                    "reason": v.get("reason", ""), "fails": int(v.get("fails") or 0),
                    "at": float(v.get("at") or 0)})
    out.sort(key=lambda x: -x["at"])
    return out


def is_banned(ip: str):
    ip = normalize(ip)
    v = _load().get(ip)
    if not v or not isinstance(v, dict):
        return None
    until = float(v.get("until") or 0)
    if not until or until < time.time():
        return None       # until=0 = 未封禁（仅记过失败次数）
    return v


def ban(ip: str, secs: int, reason: str = "", fails: int = 0):
    ip = normalize(ip)
    if not ip:
        return
    d = _load()
    d[ip] = {"until": time.time() + max(60, int(secs)), "reason": reason,
             "fails": fails, "at": time.time()}
    _save(d)


def unban(ip: str) -> bool:
    ip = normalize(ip)
    d = _load()
    if ip in d:
        d.pop(ip, None)
        _save(d)
        return True
    return False


def add_fail(ip: str, limit: int, ban_secs: int, reason: str = "登录失败过多") -> int:
    """记一次失败；达到 limit 就自动封禁。返回当前失败次数。白名单 IP 永不自动封禁。"""
    ip = normalize(ip)
    if not ip or allowed(ip):
        return 0
    d = _load()
    v = d.get(ip) if isinstance(d.get(ip), dict) else {}
    fails = int(v.get("fails") or 0) + 1
    until = float(v.get("until") or 0)
    if fails >= limit:
        until = time.time() + max(60, int(ban_secs))
        reason = "%s（自动封禁）" % reason
    d[ip] = {"until": until, "reason": reason, "fails": fails, "at": time.time()}
    _save(d)
    return fails


def clear_fails(ip: str):
    ip = normalize(ip)
    d = _load()
    v = d.get(ip)
    if isinstance(v, dict) and float(v.get("until") or 0) < time.time():
        d.pop(ip, None)
        _save(d)
