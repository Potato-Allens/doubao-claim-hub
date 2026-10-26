# -*- coding: utf-8 -*-
"""每兑换码独立会话：独立设备指纹 + 独立二维码 + 独立领取状态机。

为什么必须这么做
----------------
官方活动按【账号 × PC设备】判定"新设备首次登录"赠 30 天体验套餐；
每张卡密必须独占一套设备指纹 + 一条扫码会话，多人同时在线才不会互相顶掉。
（旧版全局单会话：A 扫的时候 B 一进页面就把 A 的二维码顶掉了 → 多用户必炸。）
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time

from app.devices import DeviceSource
from app.sdk import (PassportClient, verify_subscription, account_info, QR_CHECK_TIMEOUT,
                     GIFT_NEW_TOLERANCE)
from app.phone_login import (PhoneLoginClient, valid_cn_mobile, normalize_mobile,
                            mask_mobile, friendly_error, SEND_COOLDOWN, CODE_TTL,
                            parse_mobile, mobile_key, region_for_iso, mask_any,
                            iso_from_dial_prefix, account_iso, pick_region, DEFAULT_REGION)

QR_TTL = 60
# ★R86：出码刷新提前量。原来 18s ⇒ 每张码实际只活 ~42s，线上 43,727 次 new 只换来 49 次 scanned。
# 现在降到 10s；而且即使进了提前量窗口，**已经被人扫过的码一律不换**（见 _run）。
REFRESH_BELOW = int(os.environ.get("QR_REFRESH_BELOW", "10"))
# ★R86 反卡顿参数（全部可 env 覆盖，出问题可一键回退）
SCAN_HOLD_GRACE = float(os.environ.get("QR_SCAN_HOLD_GRACE", "30"))   # 被扫过之后至少保 X 秒（日志口径用）
POLL_SEC = float(os.environ.get("QR_POLL_SEC", "3.0"))               # 未扫码时的后台轮询间隔（原 1.5s，砍半降代理压力）
POLL_FAST_SEC = float(os.environ.get("QR_POLL_FAST_SEC", "1.2"))     # 已扫码/确认中的轮询间隔
ROTATE_WAIT = float(os.environ.get("QR_ROTATE_WAIT", "2.0"))         # 换码请求最多等 busy 的秒数（原 12s）
QR_REQ_TIMEOUT = float(os.environ.get("QR_REQ_TIMEOUT", "10"))       # 出码请求超时（原 20s：代理抖一下买家就干等 20s）
# ★R86b（内存/代理压力）
UNWATCHED_AFTER = float(os.environ.get("QR_UNWATCHED_AFTER", "30"))   # 多久没人轮询页面就算「没人看」（秒）
POLL_IDLE_SEC = float(os.environ.get("QR_POLL_IDLE_SEC", "15"))       # 没人看的会话轮询间隔（3s→15s，代理压力砍 80%）
VERIFY_RETRY = int(os.environ.get("VERIFY_RETRY", "8"))        # 到账核验轮数（福利是登录后异步发放；实测中位 24s、P95≈200s）
VERIFY_SLEEP = float(os.environ.get("VERIFY_SLEEP", "7"))        # 每轮间隔秒（8×7 ≈ 56s 主窗口）
LATE_DELAYS = [float(x) for x in os.environ.get("LATE_RECHECK_DELAYS", "60,180,300,600,1200,2400").split(",") if x.strip()]
EXIT_GUARD = os.environ.get("EXIT_GUARD", "1") == "1"            # R71：出口国家一致性守卫
# R71 实测（Sartre 取证 + 生产只读库）：出口 HK 的记录 15 条全挂、成功 0；+852 的成功 5 条**全部**走 US 出口。
# 而且抓包通道「登录后才知道账号地区」，再切地区=出口中途跳变（fail 里 48% 带跳变标记，正是最大杀手）。
# R71 曾据此定「扫码通道不跟随账号地区」；**R84-Q 起改回「按买家扫码账号的地区自动跟随」**（爷爷要求）：
# 只在「账号地区已知、且与当前出口不一致」时才切，切完即冻结，尽量少跳变。
# R84-Q：扫码通道「按买家扫码账号地区自动跟随」**默认开启、只有显式才能关**。
# 历史坑：R71 曾把这行设成 0（扫码不跟随出口），线上于是出现「港区账号显示美国出口」。
# 现在：只有 EXIT_FOLLOW_SCAN_OFF=1 才关；老的 EXIT_FOLLOW_SCAN=0 只记录、不再生效。
FOLLOW_SCAN_OFF = os.environ.get("EXIT_FOLLOW_SCAN_OFF", "0") == "1"
EXIT_FOLLOW_SCAN = not FOLLOW_SCAN_OFF
# 代理粘性池偶尔会「要 US 却给 HK/别的国家」（线上实测：region-US 抽到 203.0.113.41(HK)、203.0.113.42(HK)）。
# 所以要允许换几次 sid 直到拿到目标国家（每次约 1~2s），拿不到就在记录里如实写明。
EXIT_GUARD_TRIES = int(os.environ.get("EXIT_GUARD_TRIES", "3"))
RECHECK_MIN_GAP = float(os.environ.get("RECHECK_MIN_GAP", "20"))   # 买家手动「重新核验」最短间隔（秒）
AUTO_NEXT = os.environ.get("AUTO_NEXT", "0") == "1"
CLAIMS_PER_CODE = int(os.environ.get("CLAIMS_PER_CODE", "0"))   # 0 = 不限次（一码可反复领）
MODE = os.environ.get("MODE", "random").lower()

MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "150"))
IDLE_TTL = int(os.environ.get("SESSION_IDLE_TTL", "5400"))      # 未完成会话 90 分钟后回收
DONE_TTL = int(os.environ.get("SESSION_DONE_TTL", "21600"))     # 已完成会话 6 小时后回收

SOURCE = DeviceSource(MODE)

# ===== R122 (2026-10-16) 并发拥堵治本 =====
# 线上实测（32 分钟窗口）：555 次换码里 543 次是「没人扫且快到期」= 98% 白烧出口带宽；
# 到 代理服务商:3000 的长连接稳定 95~108 条；出码 p50=6s / p90=12s（直连只要 0.4s）——
# 出口被自己人打满 ⇒ 真买家进来就是「转圈、扫不上」。
GEN_GATE_N = int(os.environ.get("QR_GEN_GATE", "5"))             # 同时在途出码数（保护出口）
POLL_JITTER = float(os.environ.get("QR_POLL_JITTER", "0.15"))    # 轮询抖动，打散羊群
POLL_DEAD_SEC = float(os.environ.get("QR_POLL_DEAD_SEC", "120")) # 页面早关了：几乎不轮询
POLL_DEAD_AFTER = float(os.environ.get("QR_POLL_DEAD_AFTER", "300"))
ROTATE_UNWATCHED = os.environ.get("QR_ROTATE_UNWATCHED", "0") == "1"
QR_UNSCANNED_MAX = max(1, int(os.environ.get("QR_UNSCANNED_MAX", "40")))   # ★R339_ROT_CAP：无人扫码时每会话最多自动出码次数
# ★R439：僵尸会话按「年龄」冻结 —— 真实买家 1 分钟内就扫；开着页面不扫的多半是走神/爬虫。
QR_ORPHAN_AGE = float(os.environ.get("QR_ORPHAN_AGE", "1200"))          # 20 分钟仍未扫码
QR_ORPHAN_MIN_ROT = max(1, int(os.environ.get("QR_ORPHAN_MIN_ROT", "3")))
# ★R188（2026-10-16）在途出码闸门改成「运行时可调」
# 背景：R185 压测 —— 18 个买家同一 WiFi 同时进入，出码 p50 2.95s / max 4.47s，全部被 GEN_GATE=5 串行化。
# 目标：一群人同时扫时能**不重启**放闸/收闸，用实测数据找最佳值（不牺牲出口健康）。
# 取值：后台 settings 的 qr_gen_gate（30 秒缓存）→ 未设置回落环境变量 QR_GEN_GATE（默认 5），夹在 1~64。
_GATE_DEFAULT = max(1, int(os.environ.get("QR_GEN_GATE", "5")))
_GATE_CACHE = {"n": _GATE_DEFAULT, "at": 0.0}


class _DynGate(object):
    """计数型信号量：限值可在运行时变化；等待中的线程每 0.25s 复查一次新限值。"""

    def __init__(self, n: int):
        self._cv = threading.Condition()
        self._n = max(1, int(n))
        self._cur = 0
        self._max_seen = 0
        self._waits = 0
        self._waited_s = 0.0

    def _refresh(self) -> int:
        now = time.time()
        if _GATE_CACHE["at"] and (now - _GATE_CACHE["at"]) < 30:
            return int(_GATE_CACHE["n"])
        v = None
        try:
            from app import store as _s
            v = _s.get_setting("qr_gen_gate")
        except Exception:
            v = None
        n = _GATE_DEFAULT
        try:
            if v is not None and str(v).strip() != "":
                n = int(str(v).strip())
        except Exception:
            n = _GATE_DEFAULT
        n = max(1, min(int(n), 64))
        _GATE_CACHE["n"], _GATE_CACHE["at"] = n, now
        return n

    def limit(self) -> int:
        return self._refresh()

    def stats(self) -> dict:
        with self._cv:
            return {"limit": self._refresh(), "cur": self._cur, "max_seen": self._max_seen,
                    "waits": self._waits, "waited_s": round(self._waited_s, 2)}

    def __enter__(self):
        t_wait = time.time()
        with self._cv:
            self._n = self._refresh()
            if self._cur >= self._n:
                self._waits += 1
                while self._cur >= self._n:
                    self._cv.wait(0.25)
                    self._n = self._refresh()
                self._waited_s += time.time() - t_wait
            self._cur += 1
            if self._cur > self._max_seen:
                self._max_seen = self._cur
        return self

    def __exit__(self, *exc):
        with self._cv:
            self._cur = max(0, self._cur - 1)
            self._cv.notify_all()
        return False


GEN_GATE = _DynGate(GEN_GATE_N)

# ★R164（2026-10-16）匿名段加速：出码请求直连 + 跳过 boot()
# 实证（R162/R163 同机对照）：出码 total p50 5.53s(走代理) → 0.46s(直连+跳过boot)，12/12 出码成功；
# 「直连建码 + 跨出口轮询」check_qrconnect 返回 status=new（无风控拒绝）=> 后续轮询/登录仍走原出口。
# 开关：后台 settings 的 qr_fast_anon（30 秒缓存）→ 出问题可**不重启**关掉；未设置时看环境变量 QR_FAST_ANON。
QR_FAST_ANON_DEFAULT = os.environ.get("QR_FAST_ANON", "0") == "1"
_FAST_CACHE = {"on": False, "at": 0.0}


def _qr_fast_anon() -> bool:
    n = time.time()
    if _FAST_CACHE["at"] and (n - _FAST_CACHE["at"]) < 30:
        return bool(_FAST_CACHE["on"])
    v = None
    try:
        from app import store as _s
        v = _s.get_setting("qr_fast_anon")
    except Exception:
        v = None
    if v is None or str(v).strip() == "":
        on = QR_FAST_ANON_DEFAULT
    else:
        on = str(v).strip().lower() in ("1", "true", "on", "yes")
    _FAST_CACHE["on"], _FAST_CACHE["at"] = on, n
    return on



_POLL_DIRECT_CACHE = {"on": True, "at": 0.0}


def _qr_poll_direct() -> bool:
    """★R205：轮询/出码是否直连（默认开）。

    代理只用于「按账号地区对齐」的登录信息读取与权益核验；二维码轮询与出口国家无关，
    走代理纯烧流量（事故期间轮询占代理流量 95%+）。
    """
    n = time.time()
    if _POLL_DIRECT_CACHE["at"] and (n - _POLL_DIRECT_CACHE["at"]) < 30:
        return bool(_POLL_DIRECT_CACHE["on"])
    v = None
    try:
        from app import store as _s
        v = _s.get_setting("qr_poll_direct")
    except Exception:
        v = None
    if v is None or str(v).strip() == "":
        on = os.environ.get("QR_POLL_DIRECT", "1") != "0"
    else:
        on = str(v).strip().lower() in ("1", "true", "on", "yes")
    _POLL_DIRECT_CACHE["on"], _POLL_DIRECT_CACHE["at"] = on, n
    return on


def _proxy_for(code: str):
    """这张卡密的出口代理（同一张码全程同一个出口 IP）；代理池没开就返回 None = 直连。"""
    try:
        from app import proxy as _p
        p = _p.lease(code)
        if not p:
            return None
        return {"http": p["url"], "https": p["url"]}
    except Exception:
        return None


def _owner_of(code: str):
    """卡密归属：owner_id(建码的那个租户) / root_id(顶层代理商)；总台建码=0。"""
    try:
        from app import store as _s
        rec = _s.get(code) or {}
        return int(rec.get("owner_id") or 0), int(rec.get("root_id") or 0)
    except Exception:
        return 0, 0


def code_limit(code: str) -> int:
    """★R209：固定 1 次——一张卡密只能成功领取一次，不可通过设置修改。"""
    return 1

# ★R255_PHONE_PERSIST：把「已发码」的手机号会话落盘，站点重启/夜间滚动更新后
# 用户 7 分钟内提交验证码仍能登录（官方验证码 TTL 5 分钟，代理租约 10 分钟）。
_PHONE_STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "data", "phone_state")
_PHONE_STATE_TTL = float(os.environ.get("PHONE_STATE_TTL", "420"))

def _phone_state_path(key: str) -> str:
    h = hashlib.sha1(str(key or "").encode("utf-8")).hexdigest()[:24]
    return os.path.join(_PHONE_STATE_DIR, h + ".json")

def _phone_state_save(key, client) -> None:
    try:
        if not key or client is None or not hasattr(client, "dump_state"):
            return
        st = client.dump_state()
        os.makedirs(_PHONE_STATE_DIR, exist_ok=True)
        p = _phone_state_path(key)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        try:
            os.chmod(tmp, 0o600)              # 里面是登录 cookie，只许 root 读
        except Exception:
            pass
        os.replace(tmp, p)
        try:
            if len(os.listdir(_PHONE_STATE_DIR)) > 300:   # 顺手清过期，防止长期堆积
                _now = time.time()
                for _fn in os.listdir(_PHONE_STATE_DIR):
                    _fp = os.path.join(_PHONE_STATE_DIR, _fn)
                    try:
                        if _now - os.path.getmtime(_fp) > _PHONE_STATE_TTL:
                            os.remove(_fp)
                    except Exception:
                        pass
        except Exception:
            pass
    except Exception as e:
        _log("[phone_state] save 失败: %s" % str(e)[:120])

def _phone_state_load(key):
    try:
        p = _phone_state_path(key)
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
        ts = float(st.get("sent_at") or 0)
        if ts <= 0 or (time.time() - ts) > _PHONE_STATE_TTL:
            try:
                os.remove(p)
            except Exception:
                pass
            return None
        return PhoneLoginClient.from_state(st, logger=_log)
    except Exception as e:
        _log("[phone_state] load 失败: %s" % str(e)[:120])
        return None

def _phone_state_drop(key) -> None:
    try:
        p = _phone_state_path(key)
        if key and os.path.exists(p):
            os.remove(p)
    except Exception:
        pass

_log = (lambda m: None)


def set_logger(fn):
    global _log
    _log = fn


def _seg_recent_rate(account: str, minutes: int = 120):
    """★R680：近 N 分钟该号段的成功率（失败文案动态提示，样本<8 返回 None）。"""
    try:
        import sqlite3 as _sq
        from app import store
        s = str(account or "").split("/")[-1].replace(" ", "").strip()
        if s.startswith("+852"):
            seg = "852"
        elif s.startswith("+1"):
            seg = "1"
        elif s.startswith("+86") or s[:1].isdigit():
            seg = "86"
        else:
            return None
        since = int(time.time()) - int(minutes) * 60
        ok = bad = 0
        c = _sq.connect(store.DB_PATH, timeout=8)
        try:
            for kind, acct in c.execute("SELECT kind, account FROM events WHERE created_at>=?"
                                       " AND kind IN ('success','fail') AND account<>''", (since,)):
                a = str(acct or "").split("/")[-1].strip()
                if seg == "852" and not a.startswith("+852"):
                    continue
                if seg == "1" and not a.startswith("+1"):
                    continue
                if seg == "86" and not (a[:1].isdigit() and not a.startswith("+")):
                    continue
                if kind == "success":
                    ok += 1
                else:
                    bad += 1
        finally:
            c.close()
        n = ok + bad
        if n < 8:
            return None
        return (int(round(100.0 * ok / n)), ok, n)
    except Exception:
        return None


def _ev(code: str, kind: str, account: str = "", ip: str = "", ua: str = "", note: str = "",
        owner_id: int = 0, uid: str = "", detail=None):
    """写一条领取记录（失败也不影响主流程）。

    R82-L：额外带 uid 与结构化 detail（before/after 会员到期时间等）。
    """
    try:
        from app import store
        store.log_event(code, kind, account=account, ip=ip, ua=ua, note=note, owner_id=owner_id,
                        uid=uid, detail=detail)
    except Exception:
        pass


def last_success_detail(code: str) -> dict:
    """★R699：从最近一条 success 事件里取回账号/uid/到期 —— 重启或会话回收后成功页不再空白。"""
    try:
        from app import store as _st
        import sqlite3 as _sq
        import json as _js
        _c = _sq.connect(_st.DB_PATH, timeout=8)
        try:
            row = _c.execute(
                "SELECT account,uid,note,detail,created_at FROM events WHERE code=? AND kind='success' "
                "ORDER BY created_at DESC, id DESC LIMIT 1", (code,)).fetchone()
        finally:
            _c.close()
        if not row:
            return {}
        out = {}
        try:
            _d = _js.loads(row[3] or "{}")
            if isinstance(_d, dict):
                for _k in ("account", "uid", "after_member", "after_expiry", "member", "expiry"):
                    if _d.get(_k):
                        out[_k] = _d.get(_k)
        except Exception:
            pass
        if row[0] and not out.get("account"):
            out["account"] = row[0]
        if row[1] and not out.get("uid"):
            out["uid"] = row[1]
        if not out.get("after_expiry"):
            _m = re.search(r"到期\s*([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2})", row[2] or "")
            if _m:
                out["after_expiry"] = _m.group(1)
                out["expiry"] = _m.group(1)
        try:
            # ★R699b：到账时间（北京时间，不依赖进程 TZ）
            out["done_at"] = time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(row[4]) + 8 * 3600))
        except Exception:
            pass
        return out
    except Exception:
        return {}


def _membership_of(result: dict):
    """R82-L：从一次核验结果里挑出「会员名 + 到期时间」。

    优先官方赠送项 pc_membership_gift；没有就退回第一条订阅记录；再没有才用会员入口的显示名。
    返回 (member, expiry)，都没有就是 ("", "")。
    """
    r = result or {}
    items = [i for i in (r.get("items") or []) if isinstance(i, dict)]
    gifts = [i for i in items if i.get("source") == "pc_membership_gift"]
    # ★R208：多个赠送项时取到期最晚的（历史赠送混在同一条记录里时，第一条不一定是本次）
    try:
        gifts.sort(key=lambda i: float(i.get("end_ms") or 0), reverse=True)
        items.sort(key=lambda i: float(i.get("end_ms") or 0), reverse=True)
    except Exception:
        pass
    pick = (gifts or items or [{}])[0]
    member = str(pick.get("product") or "").strip()
    expiry = str(pick.get("end") or "").strip()
    if not member:
        e = r.get("entry") or {}
        if e.get("has_active"):
            member = str(e.get("name") or "").strip()
    return member, expiry


def claims_limit() -> int:
    """★R209：固定 1 次（保留函数名兼容旧调用）。"""
    return 1


def _foreign_mobile(mob: str) -> bool:
    """境外号：豆包给的手机号形如 "+852******12"（境内则是 "152******34"，无国码前缀）。"""
    m = (mob or "").strip()
    return m.startswith("+") and not m.startswith("+86")


def _fail_hint(result: dict) -> str:
    """把「没拿到福利」的真正原因讲清楚（不再一律甩锅给 +86）。"""
    r = result or {}
    items = r.get("items") or []
    entry = r.get("entry") or {}
    errs = r.get("errors") or []
    if errs and not items and not entry:
        return "核验时网络异常（已自动重试），请重新扫码再试"
    # ★R484_COPY / R484b_COPY：同号重试 ≈3%（490 次补救成功里仅 17 次同 uid）→ 不再劝「换码再试」
    if _foreign_mobile(r.get("mobile")):
        cc = str(r.get("mobile")).split("*")[0] or "+?"
        return ("%s 号本次没拿到官方福利：活动按账号资格和号段放量发放，"
                "每个账号限一次；同一个号重复扫通常不会变，"
                "请换一个没领过的新号再试（境外号也能领，只是名额更少）" % cc)
    if r.get("gift_expired"):
        return "该账号此前已领过（活动每个账号限一次），本次不再发放"
    if r.get("gift_any"):
        return "该账号有历史领取记录，本次未重复发放"
    # ★R213：campaign_status=3 在成功样本里同样大量出现（883 条），不是「没资格」的信号；
    # 以前拿它当结论会误导买家，已删掉，只看订阅记录/会员入口这类硬证据。
    if not items and not entry.get("has_active"):
        return ("该账号本次没有收到官方福利：活动按账号资格发放，老号/已领过的号不再发放。"
                "同一个号重复扫通常不会变，"
                "请换一个没领过的新号再试")
    return "官方未返回发放记录（多为网络抖动，等 10 秒再扫一次即可）"


# ★R203：出口 403 全局熔断计数（60 秒窗口）
_TRIP = {"n": 0, "at": 0.0}
_TRIP_LOCK = threading.Lock()


class Session:
    """一张兑换码 = 一套设备指纹 + 一个二维码机。"""

    def __init__(self, code: str):
        self.code = code
        self.lock = threading.RLock()
        self._wake = threading.Event()      # R122：页面回来时立刻唤醒轮询线程
        self._last_poll = 0.0
        self.qr_stale = False
        self.owner_id, self.root_id = _owner_of(code)
        self.proxies = _proxy_for(code)
        self.device = None
        self.client = None
        self.token = None
        self.qr_b64 = None
        self.expire = 0.0
        self.status = "idle"
        self.message = "正在准备二维码…"
        self.detail = {}
        self.history = []
        self.seq = 0
        self.busy = False
        self.claims = 0
        self.max_claims = code_limit(code)
        self.claimed = False
        self.ip = ""                      # 买家 IP / UA（用于领取记录）
        self.ua = ""
        self.account = ""                 # 对方登录的账号（一确认登录就抓，实时给前端显示）
        # ---- 手机号通道（买家自己的号 + 短信验证码）----
        self.phone = ""                   # 脱敏号（如 1**********）
        self.phone_state = "idle"         # idle | sent | verifying
        self.phone_at = 0.0               # 上次发码时间（算冷却用）
        self._phone = None                # 复用的 PhoneLoginClient（同号必须同实例）
        self._login_inflight = False      # ★R249_LOGIN_INFLIGHT
        self._phone_key = ""              # ★R249 完整号码键（区号+号码；self.phone 是脱敏的）
        self._send_lock = threading.Lock()  # ★R250_SEND_SERIAL：同一会话发码/人机验证串行化
        self._scan_logged_seq = -1        # 同一个二维码只记一次「已扫码」
        self._scan_logged_seq = -1        # 同一个二维码只记一次「已扫码」
        self._scanned_seq = -1            # ★R86：哪个 seq 的码被扫过（-1=没扫过），换码时保护它
        self._scanned_at = 0.0            # ★R86：被扫到的时刻（换码日志里算「保住了多久」）
        self._late_started = False        # 延迟复查只起一次
        self.qr_fails = 0                 # 连续出码失败次数（≥3 自动换出口）
        self.verify_start = 0.0           # 核验开始时间（前端显示「已核验 Ns」）
        self.verify_baseline_ms = 0.0     # ★R212：本次登录的时间基线（识别旧礼包）
        self.verify_epoch = 0             # ★R213：每次登录自增；旧核验线程发现 epoch 变了就退场
        self.relogin_required = False     # ★R212：cookie 已过期，必须重新登录
        self.exit_info = ""               # 本会话真实出口 IP（国家）
        self.exit_scan = ""               # 出码那一刻的出口 IP（用来看中途有没有跳变）
        self.acct_iso = ""                 # 账号地区（CN/HK/US…）
        self.acct_iso_src = ""             # R84-Q：账号地区来源(input/account/global/default)
        self.exit_cc = ""                  # R84-Q：本会话**真实探测到**的出口国家（区别于「指定地区」）
        self.phone_cc = ""                 # 手机号国家（ISO）
        self.exit_region = ""              # 本码当前指定的出口地区
        self.region_manual = False         # True = 买家在页面上手动选过账号地区（自动跟随就不再覆盖）
        self.region_locked = False         # R74: freeze exit region once scan/login started
        self.region_aligned = False        # R76: 登录确认时允许做一次「按账号地区对齐」，只此一次
        self.late_state = ""               # ""|running|done|exhausted 延迟复查状态（前端「可能延迟到账」用）
        self.late_tries = 0                 # 延迟复查已试次数
        self.late_until = 0.0               # 下一次复查的时间点（前端显示倒计时）
        self._ck = {}                       # 登录态 cookie（手动「重新核验」/补核用）
        self._recheck_at = 0.0              # 上次手动复查时间（防连点）
        self.qr_fail_total = 0            # 本轮连续失败总数（换出口也不清零，成功才清零）
        self._orphan_since = 0.0          # ★R439：本会话第一次自动出码的时刻（扫到码就清零）
        self._retry_at = 0.0              # ★R101：上次「出码失败自动重排」的时刻（退避用）
        self._sid_at = 0.0                # ★R101：上次换出口 sid 的时刻（1 秒内只换一次）
        # ---- R82-L：本会话已知的账号身份 + 会员到期时间（uid / before / after）----
        self.known = {}
        self.created = time.time()
        self.seen = self.created
        self.dead = False
        # ★R690：重启/GC 后内存状态是空的 —— 已领过的码必须先从库里恢复，否则会重新出码
        self._restore_claimed_from_db()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _restore_claimed_from_db(self):
        """★R690：以库为准恢复「已领取」—— 进程重启后 claimed 不会凭空消失。"""
        try:
            from app import store as _st
            _rec = _st.get(self.code) or {}
            _done = (str(_rec.get("status") or "") == "done")
            _hit = False
            if not _done:
                import sqlite3 as _sq
                _c = _sq.connect(_st.DB_PATH, timeout=8)
                try:
                    _hit = bool(_c.execute(
                        "SELECT 1 FROM events WHERE code=? AND kind='success' LIMIT 1",
                        (self.code,)).fetchone())
                finally:
                    _c.close()
            if not (_done or _hit):
                return
            with self.lock:
                self.claims = max(int(_rec.get("claims") or 0), 1)
                self.max_claims = 1
                self.claimed = True
                self.status = "success"
                self.message = "🎉 领取成功！本码已完成领取（该卡密只能领取一次）"
                self.late_state = "done"
                self.late_until = 0.0
                self.qr_stale = True
                # ★R699：从库里把账号/到期一并恢复（否则重启后成功页是空白的）
                _det = last_success_detail(self.code)
                if _det:
                    if _det.get("account"):
                        self.account = _det["account"]
                    for _k in ("uid", "after_member", "after_expiry", "member", "expiry"):
                        if _det.get(_k):
                            self.known[_k] = _det[_k]
                    if _det.get("after_expiry"):
                        self.message = ("🎉 领取成功！本码已完成领取（到期 %s，北京时间）"
                                        % _det["after_expiry"])
            _log("[%s] R690 库里显示已完成领取 → 直接锁定，不再出码" % self.code)
        except Exception as e:
            _log("[%s] R690 恢复领取态失败(忽略): %s" % (self.code, str(e)[:100]))

    # ---------------------------------------------------------------- 快照
    def snapshot(self) -> dict:
        with self.lock:
            dev = self.device or {}
            return {
                "ok": True, "status": self.status, "message": self.message,
                "remain": max(0, round(self.expire - time.time())), "seq": self.seq,
                # ★R505_STALE_TRUTH：前端据此把「已过期且不会自动换」的码蒙层挡住
                "qr_stale": bool(self.qr_stale),
                "qr_paused": bool(self._auto_rot_paused()),
                "verify_elapsed": (max(0, int(time.time() - self.verify_start))
                                   if (self.status == "verifying" and self.verify_start) else 0),
                # ★R211：让前端进度条能真实推进（第 N/8 次）
                "verify_attempt": int(getattr(self, "verify_i", 0) or 0),
                "verify_total": int(VERIFY_RETRY),
                "relogin_required": bool(getattr(self, "relogin_required", False)),
                # ★R696：已领取成功后绝不再下发二维码（防前端把旧码画回来，也省一次 45KB）
                "qr": (("data:image/png;base64," + self.qr_b64)
                       if (self.qr_b64 and not bool(getattr(self, "claimed", False))
                           and self.status != "success") else None),
                "device": ({"did": dev.get("did"), "uuid": dev.get("uuid"),
                            "serial": dev.get("serial"), "mac": dev.get("mac"),
                            "model": dev.get("model"), "origin": dev.get("origin")}
                           if dev else None),
                "detail": self._detail_view(), "history": self.history[:10],
                "mode": MODE, "pool_left": SOURCE.remaining(),
                "pool_total": SOURCE.pool_total, "auto_next": AUTO_NEXT,
                "code": self.code, "claims": self.claims,
                "max_claims": self.max_claims, "claimed": bool(getattr(self, "claimed", False)),
                "account": self.account or ((self.detail or {}).get("account") or ""),
                # ★R82-L：uid + 领取前/后的会员与到期时间（接口直出，前端领取记录展示）
                "uid": (self.known or {}).get("uid") or "",
                "before_member": (self.known or {}).get("before_member") or "",
                "before_expiry": (self.known or {}).get("before_expiry") or "",
                "after_member": (self.known or {}).get("after_member") or "",
                "after_expiry": (self.known or {}).get("after_expiry") or "",
                "member": (self.known or {}).get("member") or "",
                "expiry": (self.known or {}).get("expiry") or "",
                # ★R76：前端据此把「本次未领取到」降级成「暂未查到到账（可能延迟）」
                "retrying": bool(self.status in ("fail", "error") and self.late_state == "running"),
                # ★R211d：近期已领取过 = 直接给定论，不再让买家等 40 分钟「可能延迟」
                "final_fail": bool(self.status in ("fail", "error")
                                   and (self.late_state == "exhausted"
                                        or getattr(self, "dup_recent", False)
                                        or getattr(self, "relogin_required", False))),
                "late_tries": int(self.late_tries or 0),
                "late_total": len(LATE_DELAYS or []),
                "late_next_in": (max(0, int(self.late_until - time.time()))
                                 if (self.status in ("fail", "error") and self.late_state == "running") else 0),
                "can_recheck": bool(self._ck),
                "region": self.exit_region or "",
                "acct_iso": self.acct_iso or "",
                # ★R84-Q：出口地区跟随的可观测字段（**新增**，不改旧字段出参结构）
                "exit_cc": self.exit_cc or "",
                "acct_iso_src": self.acct_iso_src or "",
                "phone": {
                    "number": self.phone, "state": self.phone_state, "cc": self.phone_cc or "",
                    "cooldown": (max(0, int(SEND_COOLDOWN - (time.time() - self.phone_at)))
                                 if self.phone_at else 0),
                    "ttl": CODE_TTL,
                },
            }

    def _detail_view(self) -> dict:
        """R82-L：detail + 账号身份/到期时间（前端 d.uid / d.before_expiry 直接可用）。"""
        d = dict(self.detail or {})
        for k in ("uid", "before_member", "before_expiry", "after_member", "after_expiry",
                  "member", "expiry", "via"):
            v = (self.known or {}).get(k)
            if v not in (None, ""):
                d[k] = v
        return d

    def _note_membership(self, result: dict, first: bool = False) -> None:
        """R82-L：把一次核验看到的「会员 + 到期时间」记进 known。

        first=True（本轮第一次核验）= 领取前（before）；每次都会刷新 after。
        """
        try:
            member, expiry = _membership_of(result)
        except Exception:
            return
        with self.lock:
            if first and ("before_member" not in self.known):
                self.known["before_member"] = member
                self.known["before_expiry"] = expiry
            self.known["after_member"] = member
            self.known["after_expiry"] = expiry
            self.known["member"] = member
            self.known["expiry"] = expiry

    def _ev_detail(self, result: dict = None) -> dict:
        """R82-L：写进 events.detail 的结构化快照（uid / before / after / 套餐）。"""
        # ★R255_EXITWAIT：出口守卫在后台跑，核验偶尔先结束（线上 1/87 出现 exit=?(?)）。
        # 这里最多等 2s，让守卫把真实出口 IP 写进来；等不到就照旧记空，绝不阻塞主流程。
        if not getattr(self, "exit_ip", "") and getattr(self, "proxies", None):
            _dl = time.time() + 2.0
            while time.time() < _dl and not getattr(self, "exit_ip", ""):
                time.sleep(0.1)
        k = dict(self.known or {})
        r = result or {}
        items = [{"source": (i or {}).get("source"), "product": (i or {}).get("product"),
                  "end": (i or {}).get("end")} for i in (r.get("items") or [])]
        k.update({"account": self.account or "", "items": items[:5],
                  "entry": r.get("entry") or {},
                  "gift_new": bool(r.get("gift_new")),
                  "gift_duplicate": bool(r.get("gift_duplicate")),
                  "gift_duplicate_effective": r.get("gift_duplicate_effective") or "",
                  "auth_invalid": bool(r.get("auth_invalid")),
                  "campaign_status": r.get("campaign_status"),
                  "exit": {"ip": getattr(self, "exit_ip", "") or "",
                           "cc": getattr(self, "exit_cc", "") or ""}})   # R253
        return k

    def note_meta(self, ip: str, ua: str):
        """记下这位买家的 IP / UA（领取记录里要用）。"""
        with self.lock:
            if ip and not self.ip:
                self.ip = ip
            if ua and not self.ua:
                self.ua = ua[:200]

    def alive(self) -> bool:
        return not self.dead

    # ---------------------------------------------------------------- 出码
    def _new_qr_same_device(self) -> bool:
        with self.lock:
            dev = self.device
            _st0, _cl0 = self.status, bool(getattr(self, "claimed", False))
        if _cl0 or _st0 in ("verifying", "success"):
            _log("[%s] R685 会话状态 %s（claimed=%s）→ 放弃出码，保住已有结果"
                 % (self.code, _st0, _cl0))
            return False
        if not dev:
            return False
        self._qr_t0 = time.time()       # ★R164：出码计时埋点
        # ★R203：代理可能在运行期被关闭/熔断 —— 换码时重新取租约，别让新客户端继续握死出口
        try:
            self.proxies = _proxy_for(self.code)
        except Exception:
            pass
        fast = _qr_fast_anon()          # ★R164：匿名段加速（默认关）
        c = None
        try:
            # ★R205：轮询/出码直连（省代理流量）；self.proxies 仍保留给登录读取/核验/手机号通道
            _poll_px = None if _qr_poll_direct() else self.proxies
            c = PassportClient(proxies=_poll_px, timeout=QR_REQ_TIMEOUT)
            if not fast:
                c.boot()
                with GEN_GATE:                 # R122：在途出码限流，避免一群人同时点把出口捶爆
                    d = c.get_qrcode(dev)
            else:
                # (1) 跳过 boot() 的 GET /chat/，只保留匿名 cookie 播种（实测省 ~2.3s，3/3 出码成功）
                _seed = None
                try:
                    from app import sdk as _sdkmod
                    _seed = getattr(_sdkmod, "seed_cookies", None)
                except Exception:
                    _seed = None
                if _seed is not None:
                    try:
                        _seed(c.s, getattr(c, "fp", None))
                    except Exception:
                        c.boot()
                else:
                    c.boot()
                # (2) 出码请求直连（卸掉代理最敏感的那次调用）；cookie 回灌，后续轮询/登录仍走原出口
                try:
                    _tmp = PassportClient(proxies=None, timeout=QR_REQ_TIMEOUT,
                                          fp=getattr(c, "fp", None))
                    if _seed is not None:
                        _seed(_tmp.s, getattr(c, "fp", None))
                    try:
                        _tmp.s.cookies.update(c.s.cookies)
                    except Exception:
                        pass
                    with GEN_GATE:
                        d = _tmp.get_qrcode(dev)
                    try:
                        c.s.cookies.update(_tmp.s.cookies)
                    except Exception:
                        pass
                    try:
                        _tmp.s.close()
                    except Exception:
                        pass
                except Exception:
                    with GEN_GATE:                 # 直连失败 → 自动退回原路径（走代理）
                        d = c.get_qrcode(dev)
        except Exception as e:
            # ★R248_FD_CLOSE：出码失败必须把这次建的 Session 关掉。
            # 老代码只在「出码成功」时关旧 client，失败路径每个 PassportClient 泄漏 2~4 个 fd
            # （BoringSSL + socket）。线上实测：两个失败会话刷 400 次后进程 fd 打满 1023/1024，
            # 之后所有出码 curl: (27)/(35)/(6)、SQLite unable to open database file、/healthz 超时。
            try:
                if c is not None:
                    c.s.close()
            except Exception:
                pass
            return self._qr_fail("%s: %s" % (type(e).__name__, str(e)[:140]))
        if d.get("error_code") != 0 or not d.get("qrcode"):
            try:
                if c is not None:
                    c.s.close()
            except Exception:
                pass
            return self._qr_fail("error_code=%s %s" % (d.get("error_code"), str(d.get("description") or d)[:140]))
        old = None
        with self.lock:
            if self._locked_done():
                _log("[%s] R685 新码生成期间已完成领取 → 丢弃这张新码" % self.code)
                try:
                    c.s.close()
                except Exception:
                    pass
                return False
            old, self.client = self.client, c
            self.token = d.get("token")
            self.qr_b64 = d["qrcode"].split(",", 1)[1]
            self.seq += 1                 # ★R548_QRSEQ：二维码版本号与图片同刻生效（不再提前加）
            self.expire = float(d.get("expire_time") or (time.time() + QR_TTL))
            self.qr_stale = False          # ★R505_STALE_TRUTH：拿到新码就清掉「已停」标记（否则前端会一直蒙层）
            try:
                # ★R511_POLL_WAKE：新码就绪立刻唤醒轮询线程。
                # 现场（08:51:52 换码后 75s 内没有任何轮询日志）证明：qr_stale 会话的轮询降到 120s 后，
                # 用户点「换一张」拿到新码，轮询线程还在睡 → 对方扫了新码也要等最多 2 分钟才被发现。
                self._wake.set()
            except Exception:
                pass
            self.status = "waiting"
            self.message = "请用豆包 App 扫码（二维码 60 秒有效）"
            self.detail = {}
            self.account = ""          # 新二维码 = 还没人登录：清掉上一个账号（无限换码时最容易被发现）
        if old is not None and old is not c:
            # R122：换码会新建一条出码会话，旧连接不关就会一直挂在出口上（实测常驻 95~108 条）。
            try:
                ss = getattr(old, "s", None)
                if ss is not None:
                    ss.close()
            except Exception:
                pass
        self.qr_fails = 0
        self.qr_fail_total = 0
        # 新二维码 = 新账号：上一轮「按账号地区自动切」的出口不该粘到这一轮（手动选的保留）
        if not getattr(self, "region_manual", False) and getattr(self, "exit_region", ""):
            try:
                from app import proxy as _p
                _p.clear_region(self.code)
                with self.lock:
                    self.exit_region = ""
                    self.acct_iso = ""
                    self.acct_iso_src = ""      # R84-Q：换新二维码 = 新账号，来源也清掉
                    self.exit_cc = ""           # R84-Q
                    self.proxies = _proxy_for(self.code)
                _log("[%s] 新二维码：出口地区恢复自动（跟随全局）" % self.code)
            except Exception as e:
                _log("[%s] 恢复自动出口失败: %s" % (self.code, str(e)[:120]))
        if not self.exit_scan:
            threading.Thread(target=self._probe_exit_async, daemon=True).start()
        _log("[%s] 二维码就绪 did=%s %s%s t=%.2fs" % (self.code, dev.get("did"), d.get("web_name"),
                                                       " fast=1" if fast else " fast=0",
                                                       time.time() - getattr(self, "_qr_t0", time.time())))
        return True

    def _poll_client(self):
        """★R170：轮询直连兜底客户端（cookie 跟随主客户端）。

        为什么：代理某个 sid 抖掉时 check_qrconnect 会连续 8 秒超时（线上实测单个会话连续 4 次
        = 卡住约 1 分钟，买家看到的就是「扫不上」）。直连兜底让会话立刻恢复；主路径（代理）不变，
        登录/领取仍用原代理客户端（账号地区一致性不受影响）。
        """
        c = getattr(self, "_pclient", None)
        if c is None:
            try:
                c = PassportClient(proxies=None, timeout=QR_CHECK_TIMEOUT)
            except Exception as _e0:
                _log("[%s] 直连客户端创建失败: %s: %s" % (self.code, type(_e0).__name__, str(_e0)[:120]))
                return None
            self._pclient = c
        try:
            src = self.client
            if src is not None:
                c.s.cookies.update(src.s.cookies)
        except Exception:
            pass
        return c

    def _go_direct(self, why: str = "") -> "PassportClient":
        """★R203：把本会话立刻切到直连（出口整片 403 时的止血开关）。

        为什么：proxy_enabled=False 只影响**新建**会话；事故中老会话仍握着构建时塞进去的
        死代理 client，轮询/登录/核验/手机号发码全部继续 403 ⇒ 买家「扫了没反应」。
        """
        with self.lock:
            old = self.client
            if old is None:
                return None
            try:
                nc = PassportClient(proxies=None, timeout=QR_REQ_TIMEOUT,
                                    fp=getattr(old, "fp", None))
                try:
                    nc.s.cookies.update(old.s.cookies)
                except Exception:
                    pass
            except Exception as e:
                _log("[%s] 切直连失败: %s" % (self.code, str(e)[:120]))
                return None
            self.client, self.proxies = nc, None
            try:
                self._pclient = None          # 旧直连兜底客户端的 cookie 已过时，下次重建
            except Exception:
                pass
        try:
            _s = getattr(old, "s", None)
            if _s is not None:
                _s.close()
        except Exception:
            pass
        _log("[%s] 出口故障 → 已切直连（%s）" % (self.code, str(why)[:60]))
        # 全局熔断：60 秒内 ≥8 个会话因出口 403 切直连 ⇒ 直接关代理开关，新会话也不再吃死出口
        try:
            now = time.time()
            with _TRIP_LOCK:
                if now - _TRIP["at"] > 60.0:
                    _TRIP["n"], _TRIP["at"] = 0, now
                _TRIP["n"] += 1
                _n = _TRIP["n"]
            if _n == 8:
                from app import proxy as _p
                if (_p.get_config() or {}).get("enabled"):
                    _p.set_config(enabled=False)
                    _log("[GLOBAL] 60 秒内 8 个会话出口 403 → 已自动关闭代理（熔断），全部走直连")
        except Exception:
            pass
        return nc

    def _qr_fail(self, reason: str) -> bool:
        """出码失败：**每次失败立刻换一个全新 sid（新出口 IP）**再试。

        ★R101（2026-10-15 复测）：老逻辑是「连续失败 3 次才换 sid」，而 _do_rotate()
        只试 3 次就放弃 → 第 3 次失败刚换完 sid 就没人再试了，会话永远停在
        「二维码申请失败」（日志里只有 出码失败#1/#2/#3，之后没有下文）。
        现在：每次失败立刻换 sid（1 秒内只换一次，避免抖动），配合 _do_rotate()
        的 3 次重试 = 3 个不同出口 IP，坏 IP 再也粘不住这张码；
        再加上 _run 的 error 自动重排（退避 5~60s），买家不点也不会一直转圈。
        """
        self.qr_fails += 1
        self.qr_fail_total += 1
        _log("[%s] 出码失败#%d：%s" % (self.code, self.qr_fails, str(reason)[:160]))
        # ★R268 失败风暴熔断：17:00 线上 DEMO0010 3 分钟打了 400+ 次出码（curl 27 内存分配失败），
        # 单独一个会话把出口和内存打爆，拖累整站。120s 内 ≥30 次失败 → 停自动重试 5 分钟。
        try:
            _now = time.time()
            _ft = [t for t in (getattr(self, "_fail_times", None) or []) if _now - t <= 120.0]
            _ft.append(_now)
            self._fail_times = _ft
            if len(_ft) >= 30:
                # ★R274b：熔断期内又攒满 30 次（说明 5 分钟后再试仍然打不通）→ 继续顺延，
                # 否则 _storm 一旦置位就再也不会重新暂停，第二次风暴会裸奔。
                if not getattr(self, "_storm", False):
                    _log("[%s] ★R268 出码失败风暴：120s 内 %d 次 → 暂停自动重试 300s" % (self.code, len(_ft)))
                self._storm = True
                self._retry_at = _now + 300.0
                with self.lock:
                    # ★R697：claimed 与其它入口对齐 —— 已领取的码绝不能被熔断写成 error
                    if (self.status not in ("success", "verifying", "scanned", "scan")
                            and not bool(getattr(self, "claimed", False))):
                        self.status = "error"
                        self.message = "线路异常，已暂停自动重试 5 分钟；可点「换一个二维码」重试，或改用扫码领取"
        except Exception:
            pass
        try:
            from app import proxy as _p
            if time.time() - float(getattr(self, "_sid_at", 0.0) or 0.0) >= 1.0:
                _p.rotate(self.code)                      # 换新 sid（下一个请求就走新出口 IP）
                self._sid_at = time.time()
                _log("[%s] 出码失败 → 已更换出口 sid（连续第 %d 次）" % (self.code, self.qr_fails))
            self.proxies = _proxy_for(self.code)          # 本会话后续请求用新出口
        except Exception as e:
            _log("[%s] 自动换出口失败: %s" % (self.code, str(e)[:120]))
        with self.lock:
            if (self.status not in ("success", "verifying", "scanned", "scan")
                    and not getattr(self, "_storm", False)):
                self.message = ("线路一直不通，正在自动重试（可点「换一个二维码」或联系发卡方）"
                                if self.qr_fail_total >= 6 else "线路波动，已自动更换出口重试…")
        return False

    # ---------------------------------------------------------- 换码（★R86）
    def _try_busy(self) -> bool:
        """抢 busy（没被占用返回 True）。所有「出码」动作都靠它串行化。"""
        with self.lock:
            if self.busy:
                return False
            self.busy = True
            return True

    def _release_busy(self):
        with self.lock:
            self.busy = False

    def _op_reply(self, ok: bool, message: str, **extra) -> dict:
        """★R86：会话快照里自带 `ok: True`（那是「这个会话可用」的意思），
        所以操作结果必须**最后覆盖**它 —— 否则前端会把「换码失败」当成成功处理（实测踩过）。
        """
        out = dict(self.snapshot())
        out.update(extra)
        out["ok"] = bool(ok)
        out["message"] = message
        return out
    def _expired_message(self) -> str:
        """★R505_STALE_TRUTH：码过期时该说的话（硬停 / 暂时没人看 / 正常换码 三态）。"""
        if self._auto_rot_paused():
            self.qr_stale = True
            return "二维码已过期：自动换码已暂停（省流量），请点下方「换一个二维码」继续"
        try:
            if (not ROTATE_UNWATCHED) and (time.time() - (self.seen or 0)) > UNWATCHED_AFTER:
                # R122 的老语义：页面回来就会自动换，别骗用户「正在换」也别叫人家白点
                return "二维码已过期，你回到本页面后会自动换一张新的…"
        except Exception:
            pass
        return "二维码已过期，正在为你换一张新的…"

    def _auto_rot_paused(self) -> bool:
        """★R505_STALE_TRUTH：自动换码现在是否「已经停下来」（不会自己再出新码）。

        线上现场（08:0x）：会话被 R339 上限 / R439 空闲冻结后，轮询仍把 message 写成
        「二维码已过期，正在为你换一张新的…」——买家盯着这句等，永远等不到。
        这里把三种「停」判定集中一处，供轮询文案和前端蒙层共用。
        """
        try:
            # ★R505b：只认「硬停」（上限 / 空闲冻结）。qr_stale 还包含「暂时没人看」这种会自动恢复的状态，
            # 不能算硬停，否则会给马上要自动换码的页面弹「请点按钮」。
            if int(getattr(self, "_auto_rot", 0) or 0) >= QR_UNSCANNED_MAX:
                return True
            if (self._orphan_since
                    and (time.time() - self._orphan_since) >= QR_ORPHAN_AGE
                    and int(getattr(self, "_auto_rot", 0) or 0) >= QR_ORPHAN_MIN_ROT):
                return True
        except Exception:
            pass
        return False

    def _poll_interval(self, st: str) -> float:
        """★R86b：后台轮询间隔。

        没人看页面的会话（买家关了标签页）没必要 3 秒跑一轮代理 —— 16 个僵尸码就能把
        代理服务商 打出 429/SSL 错误（上一轮定位到的「放大器」）。没人看就降到 15s。
        """
        if st in ("scanned", "scan"):
            return POLL_FAST_SEC                  # 对方正在扫/正在确认：盯紧
        # ★R505_STALE_TRUTH / R511b：只有「硬停」（上限/空闲冻结）才用 120s 慢轮询。
        # 不能用 qr_stale 判断 —— 它还包含「切后台去用 App 扫码」这种 20s 就会回来的状态，
        # 那种情况慢到 120s 会直接漏掉对方的扫描（手机上切到豆包 App 的典型场景）。
        if st == "waiting" and self._auto_rot_paused():
            return POLL_DEAD_SEC
        # ★R339_ROT_CAP：已停止自动换码的僵尸会话 → 120s 慢轮询（页面点一下按钮立即恢复）
        # 依据：前台僵尸页原本每 3s 打一次 check_qrconnect，一天约 2.8 万次，比出码本身更费流量。
        if st == "waiting" and int(getattr(self, "_auto_rot", 0) or 0) >= QR_UNSCANNED_MAX:
            return POLL_DEAD_SEC
        # R122：三档节流（原来只有两档，且 3s 一档把出口打满）
        age = time.time() - (self.seen or 0)
        if age > POLL_DEAD_AFTER:
            iv = POLL_DEAD_SEC                # 页面早关了：几乎不轮询，页面回来立刻唤醒
        elif age > UNWATCHED_AFTER:
            iv = POLL_IDLE_SEC                # 切后台（手机切到豆包 App 去扫码）：慢轮询但抓得住
        else:
            iv = POLL_SEC                     # 页面在前台
        return iv * (1.0 + POLL_JITTER * (random.random() * 2.0 - 1.0))
    def _drop_client(self, why: str = "") -> bool:
        """★R86b：放掉出码用的 curl_cffi/BoringSSL 会话（每个约 3~4MB 原生内存）。

        实测（2026-10-15）：进程跑 25h RSS 涨到 696MB、峰值 829.8MB，但**不是泄漏**——
        零出码的 3 分钟里 RSS 完全不动。真相是 MAX_SESSIONS=150 个会话各自常驻一个
        curl_cffi Session（含 BoringSSL 上下文）⇒ 150 × ≈4MB ≈ 600MB。
        会话一旦到了终态（已到账 / 延迟复查也判定结束），这些 client 再也不会被用到，
        所以这里直接关掉、置空，让它立刻把原生内存还给系统。

        安全性：self.client 只在 _run 轮询里用；verify 线程只是在启动那一刻读一次
        client.s.cookies，而终态（success/fail）是在那次读取「之后」才写下的。
        """
        with self.lock:
            if self.client is None:
                return False
            c, self.client = self.client, None
        try:
            s = getattr(c, "s", None)
            if s is not None:
                s.close()
        except Exception:
            pass
        _log("[%s] 已释放出码会话内存%s" % (self.code, ("（%s）" % why) if why else ""))
        return True
    def _rotate_guarded(self, why: str) -> bool:
        """抢到 busy 才出码；抢不到说明已经有一次出码在跑，直接跳过（绝不并发开码）。"""
        if not self._try_busy():
            return False
        try:
            _log("[%s] 换新码（%s）seq=%d" % (self.code, why, self.seq))
            ok = self._new_qr_same_device()
            if ok:
                # ★R439：自动换码也计入预算（旧代码只在手动路径 +1，QR_UNSCANNED_MAX 形同虚设）
                with self.lock:
                    if not self._orphan_since:
                        self._orphan_since = time.time()
                    self._auto_rot = int(getattr(self, "_auto_rot", 0) or 0) + 1
            return ok
        finally:
            self._release_busy()

    def _maybe_rotate_expiring(self, st, token, client, remain) -> bool:
        """★R86：二维码快到期时的换码决策。返回 True = 本轮已处理完（调用方 continue）。

        铁律：**已经被人扫过的码，只要还没自然过期，就绝不换**。
        换一张 = 旧 code_challenge/PKCE 全废 ⇒ 买家对准手机那一刻屏幕上那张已经死了，
        这正是线上「码扫不上」的头号原因（实测 new 43,727 次 vs scanned 只有 49 次）。
        """
        with self.lock:
            if self._locked_done() or self.status == "verifying":
                return True
        scanned = (st in ("scanned", "scan")) or (self._scanned_seq == self.seq)
        if scanned:
            self._auto_rot = 0                                        # ★R339_ROT_CAP
            self._orphan_since = 0.0                                  # ★R439
        # ★R439：页面开着 20 分钟一次都没扫 → 冻结自动换码（省出口/代理流量），
        # 页面点「换一个二维码」会清零预算，立刻恢复。
        if (not scanned) and self._orphan_since and (time.time() - self._orphan_since) >= QR_ORPHAN_AGE \
                and int(getattr(self, "_auto_rot", 0) or 0) >= QR_ORPHAN_MIN_ROT:
            with self.lock:
                if not self.qr_stale:
                    self.qr_stale = True
                    self.message = ("页面已空闲 %d 分钟，自动换码已暂停（省流量）→ 点下方「换一个二维码」继续"
                                  % max(1, int(QR_ORPHAN_AGE // 60)))
                    self._auto_rot = QR_UNSCANNED_MAX
            return False
        if (not scanned) and (not ROTATE_UNWATCHED) and (time.time() - (self.seen or 0)) > UNWATCHED_AFTER:
            # R122：没人看的会话绝不换码 —— 换出来没人扫、1 分多钟又过期、再换，纯烧出口。
            # 买家页面一回来：/api/state 刷新 seen + 唤醒轮询线程，这一支立刻放行换码。
            with self.lock:
                if not self.qr_stale:
                    self.qr_stale = True
                    self.message = "二维码已过期，你回到本页面后会自动换一张新的…"
            return False
        # ★R339_ROT_CAP：无人扫码的会话连续自动出码到上限 → 停止继续出码（省出口/代理流量）。
        # 依据 2026-10-20 全天：3,213 个会话出了 33,632 张码，约 150 个会话各出 40 张以上，
        # 单会话最高 1,159 张（约 75 秒一张，从头到尾没人扫）。cap=40 约省 39% 的出码。
        # 用户主动点「换一个二维码」会清零预算；一旦被扫码也清零。
        if (not scanned) and int(getattr(self, "_auto_rot", 0) or 0) >= QR_UNSCANNED_MAX:
            with self.lock:
                if not self.qr_stale:
                    self.qr_stale = True
                    self.message = "二维码已过期（长时间无人扫描，已暂停自动换码）→ 点下方「换一个二维码」继续"
            return False
        with self.lock:
            self.qr_stale = False
        if not scanned:
            # ★R248_ROTATE_THROTTLE：出码连续失败时 remain 会一直是负数，老代码 continue 后
            # 立刻又回到这里 → 每秒十几次「换新码+出码」，把 GEN_GATE/代理/uvicorn 全打满
            # （线上实测：两个会话 1 分钟刷 400+ 次，/healthz 直接超时）。
            # 这里按累计失败次数做指数退避（2s 起，封顶 30s），成功出码后自然不再进本分支。
            _now = time.time()
            _wait = max(2.0, min(30.0, 2.0 * float(self.qr_fail_total or 1)))
            if _now < float(getattr(self, "_rot_next", 0.0) or 0.0):
                return True
            self._rot_next = _now + _wait
            # 没人扫过：探一次服务端真实状态（本地 remain 只是本地推算），确认真没人扫才换
            s0 = None
            try:
                s0 = (client.check_qrconnect(token) or {}).get("status")
            except Exception as e:
                _log("[%s] 换码前探活失败: %s" % (self.code, str(e)[:100]))
                # ★R203：探活 403 时不能 sleep 空转（永远到不了直连兜底）→ 先用直连探真实状态
                _c2 = self._poll_client()
                if _c2 is not None:
                    try:
                        s0 = (_c2.check_qrconnect(token) or {}).get("status")
                        if s0:
                            _log("[%s] 探活改走直连成功 status=%s" % (self.code, str(s0)[:16]))
                            if self.proxies:
                                self._go_direct("探活403")
                    except Exception as _e2:
                        _log("[%s] 直连探活也失败: %s" % (self.code, str(_e2)[:100]))
            if s0 in ("scanned", "scan"):
                with self.lock:
                    if self.token == token:
                        self._scanned_seq, self._scanned_at = self.seq, time.time()
                        self.status, self.message = "scanned", "已扫描，请在手机上点击「确认登录」"
                return True
            if s0 is None and remain > 0:
                # 探活失败，但本地这张码可能还有效 → 先不换，下一轮再说
                time.sleep(1.0)
                return True
            self._rotate_guarded("没人扫且快到期")
            return True
        # 已经被人扫过
        if remain > 0:
            # 不换码，返回 False 让调用方继续正常轮询：必须继续抓「确认登录」（confirmed），
            # 否则最后这十几秒里买家点了确认我们收不到，白扫一次。
            return False
        # ★R216b：扫码后的「确认」经常晚于二维码本地到期时间。旧逻辑一到点立刻换码，
        #   旧 token/PKCE 当场作废 → 手机显示「登录成功」，页面却退回「等待扫码」。
        #   这里给扫码后的码一个确认宽限期（默认 180s），期间继续轮询同一 token；
        #   宽限期过了仍没确认才换码。
        grace = float(os.environ.get("QR_SCAN_CONFIRM_GRACE", "180"))
        scanned_age = time.time() - float(self._scanned_at or 0)
        if self._scanned_seq == self.seq and scanned_age < grace:
            with self.lock:
                self.message = "二维码已过期，正在等待你确认登录…（若已在手机上确认，请稍候）"
            return False
        # ★R686：换码前最后再探一次活 —— 买家可能刚好在这一秒点了「确认登录」。
        # 旧逻辑先换码后轮询，这一单的登录态就跟着旧码一起作废，手机上显示成功、页面上却是失败。
        try:
            _d = client.check_qrconnect(token) or {}
            _s1 = _d.get("status")
            if _d.get("error_code") == 0 and _s1 and _s1 not in ("new", "scanned", "scan", "expired", "canceled"):
                _log("[%s] R686 换码前最后一次探活：已确认 status=%s → 不换码，直接核验"
                     % (self.code, str(_s1)[:16]))
                threading.Thread(target=self._on_logged_in,
                                 args=(self.device or {}, client), daemon=True).start()
                return True
        except Exception as _e0:
            _log("[%s] R686 换码前探活异常（照常换码）: %s" % (self.code, str(_e0)[:90]))
        _log("[%s] 扫码后 %.0fs 仍未确认 → 换新码（旧码作废）" % (self.code, scanned_age))
        self._rotate_guarded("被扫过的码自然到期")
        return True
    def rotate(self, wait_s: float = None) -> dict:
        """换一张二维码（内部 / ensure() 用）。

        ★R86：等待从 12s 降到 ROTATE_WAIT（默认 2s）；HTTP 层不再直接调它，
        对外入口是 rotate_now()：立刻返回，出码丢后台线程。
        """
        if wait_s is None:
            wait_s = ROTATE_WAIT
        deadline = time.time() + wait_s
        while True:
            if self._try_busy():
                break
            if time.time() > deadline:
                return self._op_reply(False, "二维码正在生成，请稍等 2 秒再点")
            time.sleep(0.15)
        try:
            return self._do_rotate()
        finally:
            self._release_busy()

    def rotate_now(self) -> dict:
        """★R86：/api/next 的唯一入口 —— **立刻返回**，出码在后台线程里做。

        老实现是同步阻塞：最多忙等 12s，抢到之后还要串行出 3 次码
        （每次经代理 ~1.3s，失败一次还要吃满 20s 超时）⇒ 单个「换一张」最坏占死一个请求线程 76s。
        uvicorn --workers 1 的线程池被几个换码请求点满之后，别人连 /api/state 都在排队 —— 这就是「人多就卡」。
        """
        with self.lock:
            if self._locked_done():
                return self._op_reply(False, "该兑换码已完成领取，无需再生成二维码")
            if self.status in ("scanned", "scan"):
                return self._op_reply(False, "对方已经扫码了，正在手机上确认登录；现在换码会让这张直接作废")
            if self.busy:
                return self._op_reply(False, "正在生成二维码，请等 2 秒再点")
            self._auto_rot = 0          # ★R339_ROT_CAP：用户主动换码 → 重新给一轮预算
            self._orphan_since = time.time()   # ★R439：手动换码 = 有人在场，重新计时
            self._qr_force = True       # ★R344_QR_PAUSE：用户主动换码 → 二维码轮询立刻恢复
            self.busy = True
        _log("[%s] 收到换码请求（seq=%d）→ 后台出码，请求立刻返回" % (self.code, self.seq))
        threading.Thread(target=self._rotate_bg, daemon=True).start()
        try:
            self._wake.set()      # ★R511_POLL_WAKE：先叫醒一次，出码完成后 _new_qr_same_device 还会再叫醒
        except Exception:
            pass
        return self._op_reply(True, "已受理，正在生成新二维码…", accepted=True)

    def _rotate_bg(self):
        try:
            self._do_rotate()
        except Exception as e:
            _log("[%s] 后台换码异常: %s" % (self.code, str(e)[:200]))
        finally:
            self._release_busy()

    def _do_rotate(self) -> dict:
        """真正出码（调用方必须先持有 busy）。"""
        with self.lock:
            if self._locked_done() or self.status == "verifying":
                _log("[%s] R685 状态 %s（claimed=%s）→ 拒绝换码，保住已有结果"
                     % (self.code, self.status, bool(getattr(self, "claimed", False))))
                return self._op_reply(False, "已完成领取，无需再生成二维码")
        with self.lock:
            _prev_ok_msg = self.message if self.status == "success" else ""
            self.region_locked = False
        self.proxies = _proxy_for(self.code)
        dev = SOURCE.take()
        with self.lock:
            self.device = dev
            # ★R548_QRSEQ：seq 必须代表「当前 qr_b64 的版本」，不能在这里提前 +1。
            # 实测（换码后 0.1s 轮询）：snapshot 会返回「新 seq + 旧二维码」，
            # 前端把旧图记成新 seq，之后带 qs=新seq 轮询被 /api/state 去重剥离 →
            # 新图永远不下发，买家扫的是已作废的旧码（线上「换码后扫不上」根因）。
            self.status = "waiting"
            self.message = "正在申请二维码…"
            self.detail = {}
        _log("[%s] ★ 新设备 #%d did=%s uuid=%s（%s）"
             % (self.code, self.seq + 1, dev["did"], dev["uuid"], dev.get("origin")))
        # ★R439：首次出码就记下「页面开始等扫」的时刻（含 ensure 自动首码）
        with self.lock:
            if not self._orphan_since:
                self._orphan_since = time.time()
        for _ in range(3):
            if self._new_qr_same_device():
                # ★R268：出码成功后清空风暴计数
                try:
                    self._fail_times = []
                    self._storm = False
                except Exception:
                    pass
                try:
                    with self.lock:                       # ★R339_ROT_CAP
                        self._auto_rot = int(getattr(self, "_auto_rot", 0) or 0) + 1
                except Exception:
                    pass
                return dict({"ok": True}, **self.snapshot())
            time.sleep(1.5)
        with self.lock:
            # ★R687：出码期间刚好领取成功（claimed/成功）→ 不许把状态盖成 error
            if bool(getattr(self, "claimed", False)) or self.status == "success":
                _log("[%s] R687 出码失败但已完成领取 → 恢复成功状态" % self.code)
                self.status = "success"
                if _prev_ok_msg:
                    self.message = _prev_ok_msg
                else:
                    _m = self.message or ""
                    if (not _m) or ("正在申请二维码" in _m) or ("正在核验" in _m) or ("登录成功" in _m):
                        self.message = "🎉 领取成功！本码已完成领取"
                return self._op_reply(False, "已完成领取，无需再生成二维码")
            self.status, self.message = "error", "二维码申请失败，请重试"
        return self._op_reply(False, "二维码申请失败")

    def ensure(self):
        """首次进入页面时自动出一张码（不阻塞请求）。"""
        with self.lock:
            if self.device or self.busy:
                return
            # ★R690：已领取/已完成 → 绝不再出一张新码（买家不关页面也不会看到新二维码）
            if bool(getattr(self, "claimed", False)) or self.status == "success":
                return
        threading.Thread(target=self.rotate, daemon=True).start()

    # ------------------------------------------------------- 手机号通道
    def phone_send(self, phone) -> dict:
        # ★R692：本码已经领取成功 → 一律不发短信、不跑人机验证
        # （省官方短信/3D 配额，也避免成功界面被「验证码已发送」搅乱）
        with self.lock:
            if bool(getattr(self, "claimed", False)) or self.status == "success":
                _log("[%s] R692 该码已完成领取 → 忽略发码请求（不碰官方接口）" % self.code)
                return {"ok": False, "done": True, "cooldown": 0,
                        "message": "该兑换码已完成领取，无需再发送验证码"}
        # ★R344_QR_PAUSE：用户改走手机号 → 关掉「强制二维码」开关，轮询让位给短信
        try:
            self._qr_force = False
        except Exception:
            pass
        # ★R250_SEND_SERIAL：同一会话同时只允许一个发码/人机验证任务。
        # 线上 18:05 DEMO0011 连点两次 → 8099/8101 两个 worker 同时跑 3d，一个成功一个白跑 3 分钟。
        if not self._send_lock.acquire(blocking=False):
            _log("[%s] 手机号通道 上一次发码/验证还在进行 → 忽略重复请求" % self.code)
            return {"ok": False, "message": "上一次验证还在进行中，请稍候…（勿重复点击）", "cooldown": 5}
        try:
            return self._phone_send_inner(phone)
        finally:
            try:
                self._send_lock.release()
            except Exception:
                pass

    def _phone_send_inner(self, phone) -> dict:
        """给买家自己的手机号发短信验证码（同一张码全程复用同一个 PhoneLoginClient）。

        ★ 按买家手机号的国家选出口地区（+1 → US、+852 → HK、+86 → HK…）
        """
        iso, dial, nat, ok = parse_mobile(phone)
        m = mobile_key(phone)
        if not ok:
            _ev(self.code, "fail", ip=self.ip, ua=self.ua, owner_id=self.owner_id,
                note="手机号通道：号码格式不合法（请带国家前缀或手动选国家）")
            return {"ok": False, "message": "请输入正确的手机号（可带国家前缀，如 +1 5551234567）"}
        # ★R249_LOGIN_DONE_GUARD：已经用这个号登录成功，用户又点「获取验证码」时不要再打官方接口
        # （线上 10-18 17:58 复现：登录成功后 14s 再点发码，官方回「操作失败」，前端把成功界面盖红）。
        with self.lock:
            if (self.phone_state in ("verifying", "success")
                    and getattr(self, "_phone_key", "") == m
                    and (time.time() - float(getattr(self, "_login_ok_at", 0) or 0)) < 600):
                _log("[%s] 手机号通道 已登录，忽略重复发码请求" % self.code)
                return {"ok": True, "message": "已登录成功，正在核验权益，无需重新获取验证码",
                        "phone": self.phone, "cooldown": SEND_COOLDOWN}
        _reg = self._canary_region(iso, region_for_iso(iso))
        with self.lock:
            self.phone_cc = iso
            self.acct_iso = iso          # 账号地区 = 买家手机号国家（快照/领取记录可见）
            self.acct_iso_src = "input"  # R84-Q：① 买家输入的手机号国家（优先级最高）
        self._set_region(_reg, "手机号 %s（+%s）" % (iso, dial))
        with self.lock:
            c = self._phone
        if not (c is not None and getattr(c, "mobile", "") == m):
            # ★ 换了号码（或还没有实例）必须新建实例：写码与登录必须同一个 client，否则稳定回 1203
            c = PhoneLoginClient(proxies=self.proxies, logger=_log)
            try:
                c.boot()
            except Exception as e:
                _log("[%s] 手机号通道 boot 异常: %s" % (self.code, str(e)[:160]))
            with self.lock:
                self._phone = c
        # ★R248_DEDUPE：同一号码 25s 内重复点「获取验证码」→ 直接回成功，不重发。
        # 线上实测 17:17:40 DEMO0012：第一单已发码成功，1 秒后第二单被官方回「发送太频繁」，
        # 前端把成功提示盖成红色错误，用户以为没发出去。
        try:
            # ★R249_SEND_DEDUPE_55：官方发码间隔约 60s。25s 窗口 + 仅 phone_state=="sent" 时，
            # 登录中/状态刚切换的第二发会漏过去 → 官方回「太频繁」，前端把成功盖红。
            # 放宽到 55s，且不再依赖 phone_state（sent_at 只在真发成功后写入）。
            if (getattr(c, "mobile", "") == m
                    and (time.time() - float(getattr(c, "sent_at", 0) or 0)) < 55):
                _log("[%s] 手机号通道 55s 内重复请求 → 直接回成功（不重发）" % self.code)
                return {"ok": True, "message": "验证码已发送，请查看短信",
                        "phone": self.phone, "cooldown": SEND_COOLDOWN}
        except Exception:
            pass
        r = c.send_code(phone)
        # ★R203：发码网络类失败（403/隧道/超时/SSL）→ 直连重建同一号码的 client 重试一次
        if (not r.get("ok")) and self.proxies:
            _m = (str(r.get("message") or "") + " " + str(r.get("raw") or "")).lower()
            if any(k in _m for k in ("403", "connect tunnel", "tunnel connection", "超时", "timeout",
                                     "ssl", "网络异常", "connection", "proxy")):
                _log("[%s] 手机号通道 发码疑似出口故障 → 切直连重试一次" % self.code)
                try:
                    _c2 = PhoneLoginClient(proxies=None, logger=_log)
                    try:
                        _c2.boot()
                    except Exception:
                        pass
                    _r2 = _c2.send_code(phone)
                    if _r2.get("ok"):
                        with self.lock:
                            self._phone = _c2
                            self.proxies = None
                        r = _r2
                        _log("[%s] 手机号通道 直连重试成功" % self.code)
                    else:
                        # ★R215：直连重试也可能拿到风控信封（need_qr），必须把 r 换成 _r2，
                        # 否则后面的 captcha-worker 分支永远不触发（+1 号实战踩到）
                        # ★R248_DIRECT_ADOPT：代理隧道已坏，直连能通就必须把会话整体切到直连
                        # （否则后续题型重采样/captcha-worker 还在用坏代理，稳定网络异常）
                        with self.lock:
                            self._phone = _c2
                            self.proxies = None
                        c = _c2
                        r = _r2
                        _log("[%s] 手机号通道 直连重试失败: %s"
                             % (self.code, str(_r2.get("message") or "")[:100]))
                except Exception as _e:
                    _log("[%s] 手机号通道 直连重试异常: %s" % (self.code, str(_e)[:120]))
        # ★R215：官方风控（3D/滑块）→ 本机 captcha-worker 用真实浏览器过验证并重发短信
        if (not r.get("ok")) and r.get("need_qr"):
            try:
                from app import captcha_worker as _cw
                if _cw.enabled():
                    _sub = str(r.get("verify_subtype") or "")
                    _log("[%s] 手机号通道 触发风控(%s) -> captcha-worker 解题"
                         % (self.code, _sub or "?"))
                    # ★R251_SLIDE_RETRY：滑块（slide）在当前浏览器上下文里几乎必被拒（~4%），
                    # 但同一号换新设备再发一次，官方约一半概率改出 3D 题（3D 成功率远高）。
                    # 只对「captcha_failed + slide」重试 1 次；基建故障/3D 失败不重试。
                    _w = None
                    _retry_slide = (os.environ.get("CAPTCHA_SLIDE_RETRY", "1") or "1").strip() != "0"
                    # ★R351_SLIDE_TRIES：滑块碰上就是几乎必败（实测 slide 通过 0 次），
                    # 但重试时约 48% 会被官方改判成 3D（3D 通过率约 96%），所以多给一次机会。
                    _slide_tries = max(1, int(os.environ.get("CAPTCHA_SLIDE_TRIES", "2") or "2"))
                    _slide_budget = float(os.environ.get("CAPTCHA_SLIDE_BUDGET", "150") or "150")
                    _slide_t0 = time.time()
                    # ★R354_SLIDE_FAST：slide 实测 0/98 通过，不再烧 240s；重判 1 次仍 slide 就快速引导扫码
                    if _sub == "slide":
                        _log("[★R354] 手机号通道 slide 题型 → 最多重判 1 次（预算 %.0fs），仍失败直接引导扫码"
                             % _slide_budget)
                    for _ti in range(_slide_tries):
                        _w = _cw.solve_send(phone, proxies=self.proxies, ua=getattr(c, "ua", ""), logger=_log)
                        if _w.get("ok"):
                            break
                        _rr = str(_w.get("reason") or "")
                        _ss = str(_w.get("subtype") or _w.get("verify_subtype") or "")
                        if not (_retry_slide and _ti < (_slide_tries - 1)
                                and (time.time() - _slide_t0) < _slide_budget
                                and _rr.startswith("captcha_failed") and "slide" in (_rr + _ss)):
                            break
                        _log("[★R351] 手机号通道 滑块未通过 → 换新设备重试第 %d 次（%s，已耗 %.0fs）"
                             % (_ti + 1, self.code, time.time() - _slide_t0))
                        try:
                            time.sleep(random.uniform(0.8, 2.0))
                        except Exception:
                            pass
                    if _w.get("ok"):
                        # 浏览器通道的 cookie + 发送参数灌进同一个 client，后续 sms_login 才算同会话
                        for _ck in (_w.get("cookies") or []):
                            try:
                                c.s.cookies.set(_ck.get("name"), _ck.get("value"),
                                                domain=_ck.get("domain") or "",
                                                path=_ck.get("path") or "/")
                            except Exception:
                                pass
                        # ★R333_CAP_DIAG：CAP 路径登录 0/89 成功，先落一行「注入了哪些 cookie」便于定位
                        try:
                            _names = [(str(x.get("name") or ""), str(x.get("domain") or ""))
                                      for x in (_w.get("cookies") or [])]
                            _dup = {}
                            for _n, _d in _names:
                                _dup.setdefault(_n, []).append(_d)
                            _dup = {k: v for k, v in _dup.items() if len(set(v)) > 1}
                            _log("[%s] 手机号通道 CAP cookie 注入 n=%d 同名跨域=%s"
                                 % (self.code, len(_names), json.dumps(_dup, ensure_ascii=False)))
                        except Exception:
                            pass
                        _p = _w.get("params") or {}
                        if _p:
                            c._captured = _p
                        if _p.get("msToken"):
                            c.ms_token = _p["msToken"]
                        if _p.get("biz_trace_id"):
                            c.biz_trace_id = _p["biz_trace_id"]
                        if _w.get("ua"):
                            c.ua = _w["ua"]
                        # ★R335_CAP_WEB：浏览器发码用的是 web SDK（account_sdk_source=web、device_platform=web），
                        # 登录却一直带 pc_client / doubao_device_platform=desktop 等 PC 字段 → 混合设备信号，
                        # 10-19 全量 CAP 发码 95 次、手机号登录成功 0 次。这里把同一实例切到 web 参数集。
                        if os.environ.get("CAPTCHA_LOGIN_WEB", "1") != "0":
                            try:
                                c.pc = False
                                _keys = ",".join(sorted((_p or {}).keys()))[:220]
                                _log("[%s] 手机号通道 CAP 会话 → 登录参数切 web（pc=False aid=%s cap_keys=%s）"
                                     % (self.code, getattr(c, "aid", "?"), _keys))
                            except Exception as _e:
                                _log("[%s] CAP 切 web 参数异常: %s: %s"
                                     % (self.code, type(_e).__name__, str(_e)[:80]))
                        try:
                            c.mobile = "%s%s" % (dial, nat)
                            c.iso = iso
                            c.sent_at = time.time()
                        except Exception:
                            pass
                        if _w.get("sent") is False:
                            _log("[%s] 手机号通道 人机验证已通过（%s），改由纯协议重发真号 %s"
                                 % (self.code, _w.get("verify_subtype") or _sub, mask_any(phone)))
                            _r2 = c.send_code(phone)
                            r = _r2
                            if not _r2.get("ok"):
                                _log("[%s] 手机号通道 验证后重发失败: %s"
                                     % (self.code, str(_r2.get("message"))[:90]))
                        else:
                            r = {"ok": True, "message": "验证码已发送", "error_code": None,
                                 "verify_subtype": _w.get("verify_subtype") or _sub,
                                 "captcha": "auto", "elapsed": _w.get("elapsed")}
                        if r.get("ok"):
                            _log("[%s] 手机号通道 人机验证自动通过（%s，%.1fs，cookie=%d）"
                                 % (self.code, _w.get("verify_subtype") or _sub or "?",
                                    float(_w.get("elapsed") or 0), len(_w.get("cookies") or [])))
                            _ev(self.code, "phone_verify", ip=self.ip, ua=self.ua, owner_id=self.owner_id,
                                note="手机号通道：官方人机验证自动通过（%s）"
                                     % (_w.get("verify_subtype") or _sub or "?"))
                    else:
                        _rs = str(_w.get("reason") or "")
                        _log("[%s] 手机号通道 人机验证未通过: %s" % (self.code, _rs[:90]))
                        _ev(self.code, "fail", ip=self.ip, ua=self.ua, owner_id=self.owner_id,
                            note="手机号通道：人机验证未通过（%s）" % _rs[:60])
                        # ★R249_INFRA_MSG：worker 掉线/排队超时/页面崩 不是「号码被风控」，
                        # 不要再给用户看风控文案（会让人以为号废了）。need_qr=False 让前端显示原文。
                        if not _rs.startswith("captcha_failed"):
                            if _rs == "busy":
                                _friendly = "当前领取人数较多，验证服务排队超时，请稍后重试"
                            elif _rs == "rate_limited":
                                _friendly = "官方请求过于频繁，请稍后再试"
                            else:
                                _friendly = "人机验证服务暂时不可用，请稍后重试（稍后再点一次即可）"
                            r = {"ok": False, "message": _friendly, "need_qr": False,
                                 "verify_subtype": str(_w.get("subtype") or _sub or "")}
            except Exception as _e:
                _log("[%s] 手机号通道 captcha-worker 异常: %s" % (self.code, str(_e)[:140]))
        # _r215_captcha
        if r.get("ok"):
            with self.lock:
                self.phone = mask_any(phone)
                self._phone_key = m
                self.phone_at = time.time()
                self.phone_state = "sent"
            _log("[%s] 手机号通道 验证码已发送 %s" % (self.code, self.phone))
            try:
                _phone_state_save("%s|%s" % (self.code, m), c)   # ★R255：按会话+号码隔离
                self._phone_state_key = "%s|%s" % (self.code, m)
            except Exception:
                pass
            _ev(self.code, "phone_send", ip=self.ip, ua=self.ua, owner_id=self.owner_id,
                note="手机号通道：验证码已发送至 %s" % self.phone)
            return {"ok": True, "message": r.get("message") or "验证码已发送",
                    "phone": self.phone, "cooldown": SEND_COOLDOWN}
        msg = r.get("message") or friendly_error(r.get("raw") or {}, "验证码发送失败，请稍后重试")
        _log("[%s] 手机号通道 发码失败 %s" % (self.code, msg))
        _ev(self.code, "fail", ip=self.ip, ua=self.ua, owner_id=self.owner_id,
            note="手机号通道：发送验证码失败（%s%s）" % (msg, ("｜错误码 %s" % r.get("error_code")) if r.get("error_code") else ""))
        # ★R72：被风控要求人机验证时，带 need_qr 给前端 → 直接把买家引导到「扫码领取」
        # ★R254_SHORT_MSG：服务端也返回短句（长文只留日志）。旧前端缓存/op 缺失时，
        # 用户不会再看到「短信通道这个号暂时走不通」这种让人以为号废了的长文案。
        _ui_msg = msg
        if r.get("need_qr"):
            _st = str(r.get("verify_subtype") or "").strip()
            _st = {"slide": "滑块验证", "3d": "3D 旋转验证"}.get(_st.lower(), _st)
            _ui_msg = ("官方要求人机验证%s：本次自动验证没通过，多数是点得太频繁。"
                       "请等 1~2 分钟再点一次；扫码通道成功率更高，可直接点上方「扫码领取」。" % (("（%s）" % _st) if _st else ""))
        return {"ok": False, "message": _ui_msg, "phone": mask_any(phone), "cooldown": SEND_COOLDOWN,
                "need_qr": bool(r.get("need_qr")), "verify_subtype": str(r.get("verify_subtype") or "")}

    def phone_login(self, phone, code) -> dict:
        """买家手机号 + 短信验证码登录；成功后复用 _verify_flow 走权益核验（与扫码通道同一条收尾链路）。"""
        m = mobile_key(phone)
        with self.lock:
            if self._locked_done():
                _log("[%s] R685 该码已完成领取 → 忽略手机号重复登录" % self.code)
                return {"ok": False, "status": "success",
                        "message": "该兑换码已完成领取，无需重复登录"}
        with self.lock:
            c = self._phone
            # ★R248_LOGIN_DEDUPE：已经用这个号登录成功过，120 秒内重复提交（前端卡顿/用户连点）
            # 直接回成功 —— 否则官方会回 1203「验证码已失效」，把「登录成功」的界面盖成红色错误。
            # 线上实测 17:24:42 登录成功，7 秒后又来两次，都是 1203。
            # ★R269：不再依赖 phone_state（线上 08:47 成功 2 秒后的连点仍被判成 sent → 又去打官方接口，
            # 拿到「操作失败」把成功界面盖红）。只要本会话这个号成功登录过且 <300s，一律回成功。
            # ★R333_DEDUPE_NUM：R269 的去重原来只看「本会话成功过」，而 c.mobile 在换号发码时会被覆盖，
            # 导致「先用 A 号登录成功，再换 B 号」时 B 号被误判成重复登录（不请求官方、直接回成功 → 拿到 A 的权益）。
            # 线上 DEMO0013：22:41 CN 1********** 成功，22:52 +852 换号后被 R269 直接回成功。
            _ok_num = str(getattr(self, "_login_ok_num", "") or "")
            _same_num = (_ok_num == m) if _ok_num else (
                getattr(c, "mobile", "") == m or getattr(self, "_phone_key", "") == m)
            if (_same_num
                    and (time.time() - float(getattr(self, "_login_ok_at", 0) or 0)) < 3600):   # ★R288_LOGIN_ONCE 300->3600
                _log("[%s] 手机号通道 ★R269 忽略重复登录（成功 %.0fs 前）"
                     % (self.code, time.time() - float(getattr(self, "_login_ok_at", 0) or 0)))
                return {"ok": True, "message": "登录成功，正在核验权益…",
                        "status": getattr(self, "status", "verifying")}
            # ★R249_LOGIN_INFLIGHT：同一会话两路并发提交（多标签/连点/自动重试）时，
            # 第一路还在跟官方握手，第二路会拿到 1203（验证码已被消费）→ 把成功界面盖成红色错误。
            if (getattr(self, "_login_inflight", False)
                    and getattr(c, "mobile", "") == m):
                return {"ok": True, "message": "正在登录，请稍候…", "status": "verifying"}
            if c is not None and getattr(c, "mobile", "") == m:
                self._login_inflight = True
        # ★R500_LOGINCOOL：官方 ec=7「访问太频繁」按的是「同设备短时间反复提交」，
        # 实测 07:46:07 / 07:46:11 / 07:49:16 三次就中；发码后 10 秒提交也一样中。
        # 这里做会话级冷却：失败后 15s（ec=7/1203 则 60s）内不再打官方接口。
        try:
            _cool = float(getattr(self, "_login_cool_until", 0) or 0)
            if time.time() < _cool:
                _left = int(_cool - time.time()) + 1
                _log("[%s] 手机号通道 ★R500_LOGINCOOL 冷却中（剩 %ds）→ 本地挡回" % (self.code, _left))
                return {"ok": False, "ec": 7,
                        "message": "提交太频繁（官方风控）：请等 %d 秒再点一次，别连续点" % _left}
        except Exception:
            pass
        # ★R498_SENDGAP：发码后立刻提交，官方会回 ec=7「访问太频繁」，
        # 紧接着再点一次就是 1203 锁号（线上 07:45:58 发码 → 07:46:07 ec=7 → 07:46:11 ec=1203）。
        # 这里在本地先挡一下，不浪费官方的错误次数。
        try:
            _pa = float(getattr(self, "phone_at", 0) or 0)
            _gap = time.time() - _pa if _pa else 999
            if _gap < 5:
                _left = int(5 - _gap) + 1
                _log("[%s] 手机号通道 ★R499_LOGGAP 过早提交（发码后 %.1fs）→ 本地挡回，未请求官方"
                     % (self.code, _gap))
                return {"ok": False, "ec": None,
                        "message": "验证码刚发出，请等 %d 秒再点（太急会被官方判频繁）" % _left}
        except Exception:
            pass
        if not (c is not None and getattr(c, "mobile", "") == m):
            # ★R255：先从磁盘恢复「已发码」会话（夜间滚动更新/意外重启不再打断在途登录）
            _pk2 = "%s|%s" % (self.code, m)
            _rc = _phone_state_load(_pk2)
            if _rc is not None and getattr(_rc, "mobile", "") == m:
                with self.lock:
                    self._phone = _rc
                    self._phone_key = m
                    self.phone = self.phone or mask_any(phone)
                    self.phone_state = "sent"
                    self._login_inflight = True
                    self._phone_state_key = _pk2
                c = _rc
                _log("[%s] 手机号通道 ★R255 已恢复重启前的短信会话 %s" % (self.code, self.phone or "?"))
            else:
                # ★R249_LOST_PHONE：站点重启后内存里的 _phone 没了，但浏览器还留着号码和验证码；
                # 旧文案「请先获取验证码」会让人以为要重新点发送（还会碰 60s 冷却）。
                _lost = bool(getattr(self, "_phone_key", "") and getattr(self, "_phone_key", "") == m)
                # ★R254_LOST2：能提交 6 位码说明用户确实收到过短信；重启后内存状态全丢（_lost 也判不出），
                # 旧文案「请先获取验证码」会让人以为流程错了。统一提示重新获取。
                return {"ok": False, "message": ("验证码会话已失效（服务重启或已重置），请重新获取验证码"
                                                 if _lost else "验证码会话已失效（可能服务刚更新），请重新获取验证码")}
        # ★R336_LOGIN_THROTTLE：同一会话 180 秒内最多提交 6 次，超过直接拒绝（不再打官方接口）。
        #   线上 DEMO0014 18 秒内提交 12 次、DEMO0015 30 秒内 9 次，都是连点/脚本；
        #   每次都会消耗官方的重试额度，最后 1203 锁号，用户还以为是系统坏了。
        try:
            _t = getattr(self, "_ph_try", None)
            if _t is None:
                _t = self._ph_try = []
            _nowt = time.time()
            _t[:] = [x for x in _t if _nowt - x <= 180]
            if len(_t) >= 6:
                _log("[%s] 手机号通道 提交过频，已拦截（180s 内 %d 次）" % (self.code, len(_t)))
                with self.lock:
                    self._login_inflight = False
                return {"ok": False, "ec": 429,
                        "message": "提交太频繁了：官方会按次数锁号。请等 3 分钟再试，或直接用上方「扫码领取」。"}
            _t.append(_nowt)
        except Exception:
            pass
        # ★R333_LOGIN_RETRY：传输层异常（curl 超时/连接重置/SSL）时，官方可能已消费验证码，
        # 但更常见的是请求根本没到；2 秒后同会话重试一次，避免用户看到"网络异常"后用同一个码
        # 再点、拿到 1203 陷入死循环。重试仍失败则返回可读错误（不再抛 500）。
        try:
            r = c.sms_login(phone, code)
        except Exception as _e1:
            _log("[%s] 手机号通道 登录传输异常(%s: %s) → 2s 后同会话重试一次"
                 % (self.code, type(_e1).__name__, str(_e1)[:80]))
            try:
                time.sleep(2.0)
                r = c.sms_login(phone, code)
                _log("[%s] 手机号通道 登录传输异常后重试拿到响应" % self.code)
            except Exception as _e2:
                with self.lock:
                    self._login_inflight = False
                _log("[%s] 手机号通道 登录重试仍异常: %s: %s"
                     % (self.code, type(_e2).__name__, str(_e2)[:80]))
                _ev(self.code, "fail", account=self.account, ip=self.ip, ua=self.ua,
                    owner_id=self.owner_id,
                    note="手机号登录失败：网络异常（自动重试后仍失败）")
                return {"ok": False, "ec": None,
                        "message": "网络抖动，验证码可能仍有效：请等 5 秒再点一次「登录」，不要重新获取验证码"}
        # ★R336_LOGIN_NET_RETRY：request() 把 curl 异常吞成 {"_net": True}，上面的 except 打不到；
        #   这里对 _net 结果补一次同会话重试。重试仍失败 → 明确提示「码可能还有效、别重发」。
        _r0 = (r.get("raw") or {}) if isinstance(r, dict) else {}
        if _r0.get("_net"):
            _log("[%s] 手机号通道 登录传输异常(_net: %s) → 1.5s 后同会话重试一次"
                 % (self.code, str(_r0.get("_err") or "-")[:100]))
            try:
                time.sleep(1.5)
                r2 = c.sms_login(phone, code)
            except Exception as _e3:
                r2 = {"ok": False, "message": "网络异常，请稍后重试",
                      "raw": {"_net": True, "_err": "%s: %s" % (type(_e3).__name__, str(_e3)[:120])}}
            _r2 = (r2.get("raw") or {}) if isinstance(r2, dict) else {}
            if _r2.get("_net"):
                with self.lock:
                    self._login_inflight = False
                _log("[%s] 手机号通道 登录重试仍异常: %s"
                     % (self.code, str(_r2.get("_err") or "-")[:120]))
                try:
                    _ev(self.code, "fail", account=self.account, ip=self.ip, ua=self.ua,
                        owner_id=self.owner_id, note="手机号登录失败：网络异常（_net 重试后仍失败）")
                except Exception:
                    pass
                return {"ok": False, "ec": None,
                        "message": "网络抖动，验证码可能仍有效：请等 5 秒再点一次「登录」，不要重新获取验证码"}
            r = r2
            _log("[%s] 手机号通道 登录重试拿到响应 ok=%s" % (self.code, bool(r.get("ok"))))
        with self.lock:
            self._login_inflight = False
        # ★R352_EMPTY_ENV：官方通道偶尔回「空信封」（ec=- desc=-，HTTP 200 但无
        #   error_code/description），实测 169 条，用户只看到「操作失败，请稍后重试」→ 白等/连点/被 1203 锁号。
        #   这里同会话 1.2s 后同码重试一次；真成功就继续走成功链，仍空就给可执行文案。
        _r352_empty = False
        if not r.get("ok"):
            try:
                _rw = r.get("raw") or {}
                if not isinstance(_rw, dict):      # ★R493_DIAG：raw 可能是字符串
                    _rw = {"_text": str(_rw)[:200]}
                _dw = _rw.get("data") if isinstance(_rw.get("data"), dict) else {}
                _r352_empty = (r.get("error_code") is None
                               and _dw.get("error_code") is None
                               and not str(_dw.get("description") or "").strip()
                               and not _rw.get("_net") and not _rw.get("_http"))
            except Exception:
                _r352_empty = False
        if _r352_empty:
            _log("[★R352] 手机号通道 空信封(ec=- desc=-) → 1.2s 后同码重试一次")
            try:
                time.sleep(1.2)
                _r3 = c.sms_login(phone, code)
            except Exception as _e5:
                _r3 = {"ok": False, "message": "网络异常，请稍后重试",
                       "raw": {"_net": True, "_err": "%s: %s" % (type(_e5).__name__, str(_e5)[:100])}}
            if _r3.get("ok"):
                r = _r3
                _log("[★R352] 空信封重试成功，按登录成功继续")
            else:
                _r3w = _r3.get("raw") or {}
                if not isinstance(_r3w, dict):
                    _r3w = {"_text": str(_r3w)[:200]}
                _d3w = _r3w.get("data") if isinstance(_r3w.get("data"), dict) else {}
                _log("[★R352] 空信封重试仍失败 ec=%s desc=%s"
                     % (_r3.get("error_code") or _d3w.get("error_code") or "-",
                        str(_d3w.get("description") or "-")[:36]))
                r = _r3
        _diag = {}
        if not r.get("ok"):
            # ★R248_LOGIN_DIAG：把官方信封的 error_code/description 落一行，别再靠猜
            try:
                _raw = r.get("raw") or {}
                if not isinstance(_raw, dict):     # ★R493_DIAG
                    _raw = {"_text": str(_raw)[:200]}
                _dd = _raw.get("data") if isinstance(_raw.get("data"), dict) else {}
                _diag = {"ec": r.get("error_code") or _dd.get("error_code"),
                         "http": _raw.get("_http"), "cap": bool(getattr(c, "_captured", None)),
                         "pc": getattr(c, "pc", None),
                         "body": str(_raw.get("_text") or "")[:120],
                         "net": str(_raw.get("_err") or "")[:90]}
                # ★R336_LOGIN_DIAG2：把 HTTP 状态 / 非 JSON 正文 / 是否 CAP-web 会话也落下来。
                # 今日 13 单「操作失败，请稍后重试」的诊断行是 ec=- desc=- lid=-，看不到是被 WAF 挡了
                # 还是信封变了；这三项能直接区分。
                # ★R336_LOGIN_DIAG3：把传输层异常原文（net=）一起落下来，网络异常不再是无信息黑盒。
                _log("[%s] 手机号通道 登录诊断 ec=%s desc=%s lid=%s http=%s pc=%s cap=%s body=%s net=%s"
                     % (self.code, r.get("error_code") or _dd.get("error_code") or "-",
                        str(_dd.get("description") or "-")[:36],
                        str(_dd.get("log_id") or (_raw.get("log_id") or "-"))[:26],
                        _raw.get("_http") or "-",
                        getattr(c, "pc", None),
                        bool(getattr(c, "_captured", None)),
                        (str(_raw.get("_text") or "")[:120].replace("\n", " ") or "-"),
                        str(_raw.get("_err") or "-")[:90]))
            except Exception:
                pass
            msg = r.get("message") or friendly_error(r.get("raw") or {}, "登录失败，请检查验证码后重试")
            # ★R352_MSG：空信封给可执行文案（原文案「操作失败，请稍后重试」等于没说）
            if _r352_empty:
                _ecw = r.get("error_code")
                if _ecw is None:
                    _rw2 = r.get("raw") or {}
                    _dw2 = _rw2.get("data") if isinstance(_rw2.get("data"), dict) else {}
                    _ecw = _dw2.get("error_code")
                if str(_ecw) == "1203":
                    msg = "验证码已被使用或已过期，请点「重新获取」拿新码"
                else:
                    msg = ("官方通道暂时没返回结果，验证码通常仍然有效：请等 5 秒再点一次「登录」；"
                           "连续两次不行就点「重新获取」或改用上方「扫码领取」")
            # ★R269b：官方对过期/重复消费的码常回笼统「操作失败」；按发码时间说人话
            try:
                if str(msg).startswith("操作失败") and self.phone_at and (time.time() - self.phone_at) > 300:
                    msg = ("验证码已过期（发送于 %d 分钟前），请点「重新获取」后再输入"
                           % int((time.time() - self.phone_at) / 60))
            except Exception:
                pass
            _ev(self.code, "fail", account=self.account, ip=self.ip, ua=self.ua, owner_id=self.owner_id,
                note="手机号登录失败：%s" % msg, detail=_diag or None)
            # ★R281_LOGIN_EC：把官方 error_code 一并返回给前端，
            # 前端只锁 1203（限错/过期），不会因 1202（错一个数字）误锁 60s。
            _ec = r.get("error_code")
            if _ec is None:
                _raw2 = r.get("raw") or {}
                _d2 = _raw2.get("data") if isinstance(_raw2.get("data"), dict) else {}
                _ec = _d2.get("error_code")
            try:
                _ec = int(_ec) if _ec is not None else None
            except Exception:
                _ec = None
            # ★R500_LOGINCOOL：ec=7/频繁、ec=1203/锁号 → 60s；其他失败 15s
            try:
                _cd = 60 if _ec in (7, 1203) else 15
                self._login_cool_until = time.time() + _cd
                _log("[%s] 手机号通道 ★R500 失败 ec=%s → 本会话冷却 %ds" % (self.code, _ec, _cd))
            except Exception:
                pass
            return {"ok": False, "message": msg, "ec": _ec}
        # ---- 登录成功：先抢读账号（领取页马上看得到）→ 记 login → 起核验线程 ----
        try:
            ck = c.cookies()
        except Exception:
            ck = {}
        acct = ""
        info = {}
        if ck:
            try:
                info = account_info(ck, proxies=self.proxies) or {}
                acct = (info.get("account") or "").strip()
            except Exception as e:
                _log("[%s] 手机号通道读取账号异常: %s" % (self.code, str(e)[:120]))
        _uid = str(r.get("user_id") or "").strip()
        if not _uid:
            try:
                _uid = str(((info or {}).get("raw") or {}).get("user_id") or "").strip()
            except Exception:
                _uid = ""
        with self.lock:
            if acct:
                self.account = acct          # ★R213：短信登录成功就以本次账号为准
            self.dup_recent = False
            self.relogin_required = False
            if _uid:
                self.known["uid"] = _uid
            self.known["via"] = "sms"
            self.known["phone"] = self.phone or ""
            self.phone_state = "verifying"
            self.status = "verifying"
            self.message = ("登录成功" + ("：" + self.account if self.account else "")
                            + "，正在核验权益…")
        with self.lock:
            self._login_ok_at = time.time()
            self._login_cool_until = 0.0
            self._login_ok_num = m          # ★R333_DEDUPE_SET：记牢「哪个号成功过」
        _phone_state_drop("%s|%s" % (self.code, m))   # ★R255：登录已成功，落盘状态即可清理
        _ev(self.code, "login", account=self.account, ip=self.ip, ua=self.ua, owner_id=self.owner_id,
            note=("手机号登录成功（%s），开始核验权益" % self.account) if self.account
                 else "手机号登录成功，开始核验权益")
        with self.lock:
            try:
                self._ck = dict(c.cookies())
            except Exception:
                pass
        _log("[%s] 手机号通道登录成功 account=%s 开始核验" % (self.code, self.account or "?"))
        self._lock_region("手机号已登录")
        # ★R213d：短信登录成功也立刻开始核验，出口守卫丢后台（最坏 20s 不再挡路）
        self._region_settle = threading.Event()          # ★R271
        threading.Thread(target=self._guard_exit_bg,
                         args=(self.phone_cc and self.phone_cc or "",),
                         kwargs={"tag": "手机号国家", "follow": True}, daemon=True).start()
        dev = self.device or SOURCE.take()
        if not dev:
            _did = "".join(random.choice("0123456789") for _ in range(16))
            dev = {"did": _did, "iid": str(int(_did) + 4096)}
        with self.lock:
            if not self.device:
                self.device = dev
        threading.Thread(target=self._verify_flow, args=(dev, c), daemon=True).start()
        return {"ok": True, "message": "登录成功，正在核验权益…", "status": "verifying"}

    def phone_reset(self) -> dict:
        """换号 / 重来：丢掉当前手机号会话，让买家重新获取验证码。"""
        _unlock = False
        _pk = getattr(self, "_phone_state_key", "") or ""
        with self.lock:
            self._phone = None
            self.phone = ""
            self._phone_key = ""
            self.phone_state = "idle"
            self.phone_at = 0.0
            if self.status in ("success", "fail", "error"):
                self.region_locked = False
                _unlock = True
        _phone_state_drop(_pk)               # ★R255：重置 = 作废落盘会话
        _log("[%s] 手机号通道 已重置" % self.code)
        return {"ok": True, "message": "已重置，请重新获取验证码"}

    # ---------------------------------------------------------------- 收尾
    def _on_logged_in(self, device: dict, client: PassportClient):
        """对方在手机上点了「确认登录」：先抢读账号（领取页马上就能看到），记一条 login 记录，再开始核验。"""
        if self._locked_done():
            _log("[%s] R685 该码已完成领取 → 忽略重复登录（保留成功状态）" % self.code)
            return
        ck = {}
        try:
            ck = dict(client.s.cookies.get_dict())
        except Exception:
            pass
        info = {}
        if ck:
            try:
                info = account_info(ck, proxies=self.proxies) or {}
            except Exception as e:
                _log("[%s] 读取账号异常: %s" % (self.code, str(e)[:120]))
        acct = (info.get("account") or "").strip()
        iso = ""
        try:
            # ★R84-Q：统一口径 —— raw.country_code 优先，其次 mobile/account 的区号（含「无 +、被掩码」的形态）。
            iso = account_iso(info or {})
        except Exception:
            iso = ""
        try:
            _uid = str(((info or {}).get("raw") or {}).get("user_id") or "").strip()
        except Exception:
            _uid = ""
        with self.lock:
            if acct:
                self.account = acct          # ★R213：新登录必须以本次账号为准（旧账号名不能残留）
            self.dup_recent = False
            self.relogin_required = False
            if iso:
                self.acct_iso = iso
                self.acct_iso_src = "account"      # R84-Q：来源 = 扫码账号真实地区
            if _uid:
                self.known["uid"] = _uid
            self.known.setdefault("via", "qr")
            if not self.verify_start:
                self.verify_start = time.time()
            self.status = "verifying"
            self.message = ("登录成功" + ("：" + self.account if self.account else "")
                            + "，正在核验权益…")
        # ★ R71：出口一致性守卫（出口国家 = 账号地区，且尽量不中途跳变）
        _geo = ("｜账号地区 %s" % self.acct_iso) if self.acct_iso else ""
        # ★R213d：出口守卫最坏要 20s（连续探活 + 纠偏），绝不能挡在核验前面。
        # 登录已经完成，礼包资格在登录那一刻就定了；守卫改后台跑，买家立刻看到核验进度。
        self._region_settle = threading.Event()          # ★R271
        threading.Thread(target=self._guard_exit_bg, args=(iso,), daemon=True).start()
        try:
            from app import proxy as _pp
            _r = _pp.region_of(self.code)
            if _r:
                _geo += "｜出口地区 %s%s" % (_r, "" if _r == self.acct_iso else "（与账号地区不一致）")
        except Exception:
            pass
        _ev(self.code, "login", account=self.account, ip=self.ip, ua=self.ua, owner_id=self.owner_id,
            note=((("登录成功（%s），开始核验权益" % self.account) if self.account
                   else "对方已确认登录，开始核验权益") + _geo))
        threading.Thread(target=self._verify_flow, args=(device, client), daemon=True).start()

    def _code_disabled(self) -> bool:
        """★R209：该码是否已被停用（停用后所有在途核验/补记一律作废）。"""
        try:
            from app import store
            return (store.get(self.code) or {}).get("status") == "disabled"
        except Exception:
            return False

    def _finish(self, result: dict, epoch=None):
        # ★R213：旧登录的核验结果直接丢弃，绝不覆盖新登录的状态
        if epoch is not None and int(epoch) != int(getattr(self, "verify_epoch", 0) or 0):
            _log("[%s] 丢弃过期核验结果（已有新的登录在跑）" % self.code)
            return
        if bool(getattr(self, "claimed", False)) or (self.status == "success" and not bool(result.get("got"))):
            _log("[%s] R685 已有成功状态 → 丢弃本次核验结果（不覆盖成功）" % self.code)
            return
        # ★R209：链接一旦停用，正在跑的核验也不允许再记成功
        if self._code_disabled():
            _log("[%s] 链接已被停用 → 丢弃本次核验结果" % self.code)
            with self.lock:
                self.status, self.message = "fail", "该兑换码已被停用，请联系发卡方。"
            return
        got = bool(result.get("got"))
        if got:
            self._note_second_account((self.known or {}).get("uid") or "", "核验")
        items = result.get("items") or []
        # ★R208：优先展示赠送项的到期（历史订阅不混进来），统一北京时间
        _gift_ends = [i.get("end") for i in items
                      if i.get("source") == "pc_membership_gift" and i.get("end")]
        ends = _gift_ends or [i.get("end") for i in items if i.get("end")]
        limit = code_limit(self.code)
        self.max_claims = limit
        _acct = (result.get("account") or "").strip()
        _exit = getattr(self, "exit_info", "") or ""
        _exits = getattr(self, "exit_scan", "") or ""
        _etxt = ""
        if _exits and _exit:
            _ip1, _ip2 = _exits.split("（")[0], _exit.split("（")[0]
            # ★R71: 只有「国家变了」才叫跳变；同国家换 IP 是代理的正常行为（1024 的粘性出口约 100~170s 换一个 IP），
            #   以前一律写「中途跳变」会把正常换 IP 也标成失败原因，复盘时误导。
            _cc1 = (_exits.split("（")[1].rstrip("）") if "（" in _exits else "")
            _cc2 = (_exit.split("（")[1].rstrip("）") if "（" in _exit else "")
            if _ip1 == _ip2:
                _flag = ""
            elif _cc1 and _cc2 and _cc1 != _cc2:
                _flag = "（★中途换了国家：%s → %s）" % (_cc1, _cc2)
            else:
                _flag = "（中途换 IP，同国家）"
            _etxt = "出口 %s → %s%s" % (_exits, _exit, _flag)
        elif _exit or _exits:
            _etxt = "出口 " + (_exit or _exits)
        if _acct:
            self.account = _acct
        acct = self.account or ""
        self._note_membership(result)                  # R82-L：刷新 after（最终态）
        _relogin = bool(result.get("auth_invalid") or getattr(self, "relogin_required", False))
        _dup = bool(result.get("gift_duplicate") or getattr(self, "dup_recent", False))
        with self.lock:
            # ★R689c：落库前在锁内再判一次 —— 入口判过 claimed，但中间要跑几秒
            # 出口探测/会员解析，手动复查可能刚好在这几秒里落成 success（物证 DEMO0008）。
            if bool(getattr(self, "claimed", False)) or self.status == "success":
                _log("[%s] R689c 落库前已有成功 → 丢弃本次核验结果" % self.code)
                return
            if got:
                self.claims += 1
                self.status = "success"
                self.dup_recent = False
                self.relogin_required = False
                if limit > 0 and self.claims >= limit:
                    self.claimed = True
                    self.message = "🎉 领取成功！30 天体验套餐已到账（请在豆包电脑版/网页版查看会员权益；该卡密已达可领次数上限）"
                else:
                    self.message = ("🎉 领取成功！30 天体验套餐已到账（请在豆包电脑版/网页版查看会员权益）"
                                    + ("（本码不限次数，可继续换码再领）" if limit <= 0 else ""))
            else:
                self.status = "fail"
                # ★R208：历史赠送（已过期/剩余不足 25 天）不是本次到账，明确告知，别让买家以为是时区问题
                _stale_end = str(result.get("gift_stale_end") or "")
                _recent_end = str(result.get("gift_recent_end") or "")
                _dup_eff = str(result.get("gift_duplicate_effective") or "")
                if _relogin:
                    self.relogin_required = True
                    self.dup_recent = False
                    self.late_state = "exhausted"
                    self.late_until = 0.0
                    _hint = "会话已过期，请在手机上重新扫码或重新登录后再试"
                    self.message = "登录状态已失效：" + _hint
                elif _dup:
                    self.dup_recent = True
                    self.late_state = "exhausted"
                    self.late_until = 0.0
                    if _dup_eff:
                        _hint = ("该账号此前已领取过体验套餐（生效 %s，北京时间），"
                                 "本次没有新增；同一账号通常只能领一次" % _dup_eff)
                    else:
                        _dup_end = _recent_end or _stale_end or "?"
                        _hint = ("该账号此前已领取过体验套餐（到期 %s，北京时间），"
                                 "本次没有新增；同一账号通常只能领一次" % _dup_end)
                    self.message = "本次未新增福利：" + _hint
                elif result.get("gift_recent") and _recent_end:
                    self.dup_recent = True
                    self.late_state = "exhausted"
                    self.late_until = 0.0
                    _hint = ("该账号近期已领取过体验套餐（到期 %s，北京时间），"
                             "同一账号通常只能领一次" % _recent_end)
                    self.message = "本次未新增福利：" + _hint
                elif result.get("gift_any") and _stale_end:
                    _hint = ("该账号此前已领取过体验套餐（到期 %s，北京时间），本次未新增" % _stale_end)
                    self.message = "本次未新增福利：" + _hint
                else:
                    _hint = _fail_hint(result)
                    self.message = ("本次未领取成功：" + _hint) if _hint else "本次未检测到福利（官方暂未发放，后台仍在复查…）"
                    # R253b：162/192 属虚拟/新运营商号段，线上样本 0/59、7/61，远低于均值
                    try:
                        _pn = str(getattr(self, "phone", "") or "")
                        if _pn[:3] in ("162", "192"):
                            self.message += ("。提示：162/192 号段为虚拟/新运营商，官方活动通过率极低，"
                                             "建议改用页面下方「扫码领取」")
                    except Exception:
                        pass
                    # ★R680：按该号段近 2 小时的真实成功率给出动态建议
                    try:
                        _sr = _seg_recent_rate(getattr(self, "phone", "") or result.get("mobile") or "")
                        if _sr and _sr[0] < 30:
                            self.message += ("。提示：该号段近 2 小时成功率仅 %d%%（%d/%d），"
                                             "建议改用 +852 新号，或稍后再试" % (_sr[0], _sr[1], _sr[2]))
                    except Exception:
                        pass
                if not (_relogin or _dup):
                    if _foreign_mobile(result.get("mobile")):
                        self.message += "（可点「换一个二维码」，换一个新账号再领）"
                    else:
                        self.message += "（后台已记录，可点「换一个二维码」再试一次）"
            self.detail = {
                "account": acct, "items": items,
                "entry": result.get("entry") or {}, "popup": result.get("pop"),
                "campaign_status": result.get("campaign_status"),
                "gift_new": bool(result.get("gift_new")),
                "gift_duplicate": _dup,
                "gift_duplicate_effective": result.get("gift_duplicate_effective") or "",
                "auth_invalid": _relogin,
                "hint": ("" if got else _hint),   # 与买家页文案同源，保证一致
                # ★R699b：到账时间（北京时间），前端「到账时间」卡片用它
                "done_at": (time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600))
                            if got else ""),
                "exit": _exit, "exit_scan": _exits, "exit_note": _etxt,
            }
            code = self.code
            self.history.insert(0, {
                "time": time.strftime("%H:%M:%S"), "seq": self.seq, "got": got,
                "account": acct or "?",
                "did": (self.device or {}).get("did"),
                "end": " / ".join(ends) if ends else "—",
            })
        if got:
            try:
                from app import store
                store.add_claim(code, limit)
                _log("[%s] 已记入领取次数（limit=%s）" % (code, limit))
            except Exception as e:
                _log("[%s] add_claim 失败 %s" % (code, str(e)[:120]))
            _ev(code, "success", account=acct, ip=self.ip, ua=self.ua, owner_id=self.owner_id,
                uid=(self.known or {}).get("uid") or "", detail=self._ev_detail(result),
                note=("30 天体验套餐已到账" + ("（到期 " + " / ".join(ends) + "）" if ends else "")
                      + (("｜" + _etxt) if _etxt else "")))
        else:
            _ev(code, "fail", account=acct, ip=self.ip, ua=self.ua, owner_id=self.owner_id,
                uid=(self.known or {}).get("uid") or "", detail=self._ev_detail(result),
                note=(((self.detail or {}).get("hint") or "官方未发放福利")
                      + (("｜" + _etxt) if _etxt else "")))
        _log("[%s] 验收完成 got=%s account=%s exit=%s(%s)" % (
            code, got, acct or "?", getattr(self, "exit_ip", "") or "?", getattr(self, "exit_cc", "") or "?"))

    def _canary_region(self, iso: str, reg: str) -> str:
        """★R385_EXIT_CANARY：CN→HK 对照实验。
        线上证据：CN 号码走 HK 出口 5/35（14.3%%）、走 US 69/137（50.4%%），
        且 96.3%% 会话出口从头到尾没换国（非幸存者偏差）。
        EXIT_CANARY_CN_US=0（默认）时完全不生效；设 N 则只有 N%% 的码走 US。
        """
        try:
            _pct = float(os.environ.get("EXIT_CANARY_CN_US", "0") or 0)
            if (_pct > 0 and str(iso or "").upper() == "CN"
                    and str(reg or "").upper() == "HK"):
                import zlib
                if (zlib.crc32(str(self.code).encode("utf-8")) % 100) < int(_pct):
                    _log("[★R385] CN 出口 canary：本码走 US（pct=%d）" % int(_pct))
                    # ★R433：打标，避免下面的出口守卫又把它「纠偏」回 HK
                    self._canary_forced = True
                    return "US"
        except Exception:
            pass
        return reg

    def _lock_region(self, why: str = "") -> bool:
        """R74: freeze the exit region once the channel actually started."""
        with self.lock:
            if self.region_locked:
                return False
            self.region_locked = True
            _why = why or "-"
        _log("[%s] * exit region locked to %s (%s)" % (self.code, self.exit_region or "?", _why))
        return True

    def _set_region(self, region: str, why: str = "") -> str:
        """把**本码**的出口地区切到 region（现有请求立即生效）。"""
        if not region:
            return ""
        try:
            from app import proxy as _p
            before = _p.region_of(self.code)
            _p.set_region(self.code, region)
            with self.lock:
                self.proxies = _proxy_for(self.code)
                self.exit_region = region
            after = _p.region_of(self.code)
            if before != after:
                _log("[%s] 出口地区 %s → %s（%s）" % (self.code, before or "?", after, why or "-"))
                _ev(self.code, "enter", ip=self.ip, ua=self.ua, owner_id=self.owner_id,
                    note="出口地区切到 %s（%s）" % (after, why or "-"))
            return after
        except Exception as e:
            _log("[%s] 切出口地区失败: %s" % (self.code, str(e)[:120]))
            return ""

    def _rotate_after_region_change(self, label: str) -> bool:
        """R74: 出口地区变了 -> 旧二维码作废重出一张（保证出码/扫码/登录全程同一个出口国家）。"""
        try:
            if int(getattr(self, "seq", 0) or 0) < 1 or getattr(self, "device", None) is None:
                return False
            if getattr(self, "region_locked", False):
                return False                      # 已经开跑：不允许换，交给上层拒绝
            self.rotate(wait_s=0.5)
            _ev(self.code, "enter", ip=self.ip, ua=self.ua, owner_id=self.owner_id,
                note="出口地区切到 %s：旧二维码已作废，已按新出口重出一张（%s）" % (label, "手动选择"))
            return True
        except Exception as e:
            _log("[%s] rotate-after-region-change failed: %s" % (self.code, str(e)[:140]))
            return False

    def set_account_region(self, iso: str) -> dict:
        """二维码通道：买家先选「账号地区」，出码前就把出口地区定好。

        iso 传空 = 恢复「自动」（跟随后台全局 region）。
        """
        iso = str(iso or "").strip().upper()
        _cur = ""
        try:
            from app import proxy as _cp
            _cur = _cp.region_of(self.code)
        except Exception:
            _cur = getattr(self, "exit_region", "") or ""
        if getattr(self, "region_locked", False):
            _want = region_for_iso(iso) if iso else ""
            if _want and _cur and _want == _cur:
                return {"ok": True, "message": "exit region already %s" % _cur, "region": _cur, "cc": iso}
            _ev(self.code, "enter", ip=self.ip, ua=self.ua, owner_id=self.owner_id,
                note="exit region kept %s (already scanning/logging in)" % (_cur or "?"))
            return {"ok": False, "region": _cur, "cc": self.acct_iso,
                    "message": "已经开始领取了，出口地区不能再改（中途换国家容易被官方风控拦）。想换地区请点「换一个二维码」重新开始"}
        if not iso:
            with self.lock:
                self.acct_iso = ""
                self.region_manual = False      # 「自动」= 以后按账号地区自动跟随
            try:
                from app import proxy as _p
                before = _p.region_of(self.code)
                _p.clear_region(self.code)
                with self.lock:
                    self.proxies = _proxy_for(self.code)
                    self.exit_region = ""
                after = _p.region_of(self.code)
                if before != after:
                    _log("[%s] 出口地区 %s → %s（恢复自动）" % (self.code, before or "?", after))
                    _ev(self.code, "enter", ip=self.ip, ua=self.ua, owner_id=self.owner_id,
                        note="出口地区恢复自动（跟随全局 %s）" % after)
            except Exception as e:
                _log("[%s] 恢复出口地区失败: %s" % (self.code, str(e)[:120]))
            _rot = False
            try:
                from app import proxy as _p2
                _rot = self._rotate_after_region_change(_p2.region_of(self.code) or "自动")
            except Exception:
                _rot = False
            return {"ok": True, "region": "", "cc": "", "rotated": _rot,
                    "message": "已恢复自动出口，二维码已按新出口重出，请重新扫码" if _rot else "已恢复自动出口"}
        reg = region_for_iso(iso)
        if not reg:
            return {"ok": False, "message": "暂不支持这个地区"}
        with self.lock:
            self.acct_iso = iso
            self.region_manual = True           # 买家自己选的：不被「自动跟随」覆盖
        self._set_region(reg, "账号地区 %s（手动选择）" % iso)
        _rotated = self._rotate_after_region_change(reg)
        return {"ok": True, "region": reg, "cc": iso, "rotated": _rotated,
                "message": ("出口地区已切到 %s，二维码已按新出口重出，请重新扫码" % reg) if _rotated
                           else ("出口地区已切到 %s" % reg)}

    def follow_account_region(self, iso: str) -> str:
        """出口地区跟随**账号实际地区**（扫码通道：登录一确认就切）。

        买家如果自己在下拉里选过地区（region_manual），以买家的选择为准。
        """
        iso = str(iso or "").strip().upper()
        if not iso or getattr(self, "region_manual", False):
            return ""
        if getattr(self, "region_locked", False):
            _log("[%s] region locked (%s), skip follow %s" % (self.code, self.exit_region or "?", iso))
            return ""
        reg = region_for_iso(iso)
        if not reg or reg == (getattr(self, "exit_region", "") or ""):
            return ""
        with self.lock:
            self.acct_iso = iso
        self._set_region(reg, "账号地区 %s（自动跟随）" % iso)
        return reg

    def _probe_exit_async(self):
        """出码时间点的出口探一下（后台线程，不拖慢出码）。"""
        try:
            v = self._probe_exit()
            if v:
                self.exit_scan = v
        except Exception:
            pass

    def _exit_country(self):
        """本会话出口的真实 (ip, 国家码)。探不到就返回 ("","")。

        ★R262：ipinfo 偶尔被某些出口拦/限速（今天 129 次探到 ?（?）），加一个备用探测源；
        两个都失败才返回空，避免把「探测失败」误当成「出口未知」写进领取记录。
        """
        import requests
        from concurrent.futures import ThreadPoolExecutor, as_completed
        _srcs = (("https://ipinfo.io/json", "ip", "country"),
                 ("https://ipwho.is/", "ip", "country_code"),
                 ("http://ip-api.com/json/?fields=status,countryCode,query",
                  "query", "countryCode"))

        def _one(_t):
            try:
                _url, _ik, _ck = _t
                d = requests.get(_url, proxies=self.proxies, timeout=4).json() or {}
                _ip = (d.get(_ik) or "").strip()
                _cc = (d.get(_ck) or "").strip().upper()
                return (_ip, _cc) if _ip else None
            except Exception:
                return None

        # ★R273：三个探测源并发跑（原来串行 4s×3=12s，最坏把 R271 的 15s 对齐窗口吃满）。
        # 任一先成功即返回，整体最坏约 4s。
        _ex = ThreadPoolExecutor(max_workers=3)
        try:
            _futs = [_ex.submit(_one, _t) for _t in _srcs]
            for _f in as_completed(_futs):
                _r = None
                try:
                    _r = _f.result()
                except Exception:
                    _r = None
                if _r:
                    for _x in _futs:
                        _x.cancel()
                    return _r
        finally:
            try:
                _ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        return "", ""

    def _probe_exit(self) -> str:
        """读一下本会话的真实出口 IP / 国家，写进领取记录。

        为什么要记：官方规则写了「基于设备/位置/客户端版本限制部分用户无法参与」，
        而我们的代理默认是 region-Rand（随机国家）。记下出口才能知道「账号地区 vs 出口国家」到底有没有影响成功率。
        """
        ip, cc = self._exit_country()
        return ("%s%s" % (ip, ("（%s）" % cc) if cc else "")) if ip else ""

    def _probe_exit_bg(self):
        """★R213d：后台探出口，结果只用于领取记录，不阻塞核验。"""
        try:
            v = self._probe_exit()
            if v:
                self.exit_info = v
        except Exception:
            pass

    def _guard_exit_bg(self, iso: str, tag: str = "账号地区", follow=None):
        """★R213d：后台做出口对齐；失败只记日志，不影响主流程。
        ★R271：对齐结束（无论成败）都 set _region_settle，核验线程最多等 15s 再发第一次领取请求。
        """
        try:
            note = self._guard_exit(iso, tag, follow)
            if note:
                _log("[%s] 出口守卫(后台)：%s" % (self.code, note))
        except Exception as e:
            _log("[%s] 出口守卫(后台)异常: %s" % (self.code, str(e)[:120]))
        finally:
            try:
                getattr(self, "_region_settle", None) and self._region_settle.set()
            except Exception:
                pass

    def _follow_region(self, prefer_iso: str = "") -> tuple:
        """★R84-Q：决定「出口该跟随到哪个地区」，返回 (region, src)。

        优先级（爷爷的需求：按买家账号/手机号所属国家选同国出口）：
          ① prefer_iso —— 当前通道最可信的国家（扫码=账号真实地区；手机号=买家输入号国家）
          ② 另一条线索 —— 手机号国家 / 账号真实地区（prefer 认不出来时兜底）
          ③ 后台全局兜底地区（后台设置 region / fallback_region）
          ④ 默认 DEFAULT_REGION（HK）
        region 非空（代理没开时调用方已在 _guard_exit 里提前返回）。
        """
        _phone = getattr(self, "phone_cc", "") or ""
        _acct = getattr(self, "acct_iso", "") or ""
        reg = pick_region(prefer_iso, _phone, _acct)
        if reg:
            if region_for_iso(_acct) == reg:
                return reg, "account"
            if region_for_iso(_phone) == reg:
                return reg, "input"
            return reg, "prefer"
        try:
            from app import proxy as _p
            base = _p.region_of(self.code)          # ③ 后台全局兜底
        except Exception:
            base = ""
        if base:
            return base, "global"
        return DEFAULT_REGION, "default"            # ④ 最后默认

    def _guard_exit(self, want_iso: str, tag: str = "账号地区", follow: bool = None) -> str:
        """★R71 出口一致性守卫：出口国家必须与账号地区一致，且尽量一次到位不再乱跳。

        生产实测（2026-10-13，只读库）：
          ·「账号地区 US｜出口国家 US」146 次 → 几乎都成功（85 份礼包全在这里）
          · 出口与账号地区**不一致**的组合（HK|US、GB|US…）→ 全败
          · fail 里 137 条（≈48%）带「出口中途跳变」→ 跳变是主要杀手
        所以登录一确认就对齐出口；仍不一致就换一个全新 sid（同地区、新 IP）再探，最多纠偏 2 次。
        返回一句人话说明，写进领取记录。
        """
        if not EXIT_GUARD:
            return ""
        if follow is None:
            follow = EXIT_FOLLOW_SCAN
        if getattr(self, "region_locked", False):
            # ★R76：扫码通道要「按买家扫码账号的地区自动跟随」。登录确认这一瞬是**最后一次**改出口的机会
            #   （此后核验全程都在同一个出口上跑），所以允许一次对齐；对齐过就永久冻结。
            if follow and not getattr(self, "region_aligned", False):
                self.region_aligned = True
                _log("[%s] 登录确认：按账号地区对齐出口（最后一次允许的切换）" % self.code)
            else:
                follow = False
        if getattr(self, "region_manual", False):
            follow = False                     # 买家手选过地区：绝不覆盖，只校验
        if getattr(self, "_canary_forced", False):
            # ★R433：canary 指定的出口就是实验条件本身。旧逻辑会按 CN→HK 再纠偏 3 次，
            # 既拿不到 HK（白烧 3 个 sid + 9 次探活），又给记录写一行「出口不一致」污染实验数据。
            follow = False
        # ★R84-Q：按优先级算出「要跟随到哪个地区」（①通道国家 ②另一条线索 ③全局兜底 ④默认 HK）
        want, want_src = "", ""
        if follow:
            want, want_src = self._follow_region(want_iso)
        try:
            from app import proxy as _p
            if not (_p.get_config() or {}).get("enabled"):
                return ""                     # 没开代理池 = 直连，没有出口可换
            _base = _p.region_of(self.code)
            _note_pre = ""
            if not want:
                want = _base or DEFAULT_REGION  # 不跟随 = 保持当前出口地区（拿不到就退回默认）
                if want_iso and region_for_iso(want_iso) and region_for_iso(want_iso) != _base:
                    _note_pre = "（按实测不跟随账号地区）"
            if _p.region_of(self.code) != want:
                self._set_region(want, "%s %s（自动跟随）" % (tag, want_iso))
            ip, cc = self._exit_country()
            fixed = 0
            _pfail = 0
            # ★R262_PROBE_RETRY：探不到出口（ip/cc 全空）时也换一次 sid 重探；
            # 旧逻辑只有「探到了但不一致」才纠偏，探测失败会直接带着未知出口去领取。
            while (not cc or cc != want) and fixed < max(1, EXIT_GUARD_TRIES):
                if not cc and _pfail >= 1:
                    break
                _p.rotate(self.code)          # 换全新 sid：同地区、换 IP（别在坏节点上耗）
                # ★ 必须把本会话的 proxies 换成新租约，否则还在用旧 sid 探测/请求（上线首日实测到的坑：
                #   换完 sid 仍探到旧出口，日志会写「纠偏 2 次仍然不一致」）
                _np = _proxy_for(self.code)
                if _np:
                    with self.lock:
                        self.proxies = _np
                ip, cc = self._exit_country()
                fixed += 1
                if not cc:
                    _pfail += 1
            with self.lock:
                self.exit_region = want
                self.exit_cc = cc or ""            # R84-Q：记下**真实**探测到的出口国家
                self.exit_ip = ip or ""            # R253：记下真实出口 IP，供领取记录/成功率分析
                if follow and want_src:
                    self.acct_iso_src = want_src   # R84-Q：地区来源 account/input/global/default
            _log("[%s] 出口守卫：%s %s → 出口 %s(%s)，纠偏 %d 次，来源 %s"
                 % (self.code, tag, want_iso, ip or "?", cc or "?", fixed, want_src or "-"))
            if cc and cc == want:
                if fixed:
                    return "出口已纠偏为 %s（%s）%s" % (ip, cc, _note_pre)
                if _note_pre:
                    return "出口 %s（%s）保持与出码一致%s" % (ip, cc, _note_pre)
                return "出口 %s（%s）与%s %s 一致" % (ip, cc, tag, want_iso)
            return ("出口 %s（%s）与期望出口 %s 不一致（已试 %d 次仍拿不到 %s 出口）%s"
                    % (ip or "?", cc or "?", want, fixed, want, _note_pre))
        except Exception as e:
            _log("[%s] 出口守卫异常: %s" % (self.code, str(e)[:140]))
            return ""

    def _verify_once(self, ck: dict, device: dict, full: bool = True,
                     since_ms: float = 0.0) -> dict:
        """★R211：核验直连优先；只有直连出现网络错误才用代理补一次。

        核验全是只读查询，与出口地区无关；原来默认走代理，代理一抖单轮能拖 100s，
        买家看到的就是「进度条卡住不动」。
        """
        try:
            r = verify_subscription(ck, device, proxies=None, full=full, since_ms=since_ms)
        except Exception as e:
            _log("[%s] 验收异常(直连): %s" % (self.code, str(e)[:160]))
            r = {"got": False, "account": "?", "items": [], "errors": [str(e)[:160]]}
        r = r or {"got": False, "account": "?", "items": []}
        if (not r.get("got")) and (not r.get("auth_invalid")) and self.proxies and r.get("errors"):
            try:
                r2 = verify_subscription(ck, device, proxies=self.proxies, full=full, since_ms=since_ms)
                if (r2 or {}).get("got") or (not r.get("items") and (r2 or {}).get("items")):
                    _log("[%s] 直连核验异常 → 代理补核成功" % self.code)
                    return r2
            except Exception as e2:
                _log("[%s] 代理补核异常: %s" % (self.code, str(e2)[:120]))
        return r

    def _verify_flow(self, device: dict, client: PassportClient):
        with self.lock:
            if self._locked_done():
                _log("[%s] R685 该码已完成领取 → 跳过核验，保留成功状态" % self.code)
                if self.status != "success" or not self.message:
                    self.status = "success"
                    self.message = ("🎉 领取成功！本码已完成领取"
                                    + ("：" + self.account if self.account else ""))
                return
        ck = dict(client.s.cookies.get_dict())
        with self.lock:
            if ck:
                self._ck = dict(ck)           # R76：存档登录态，供「重新核验」与持久补核用
            # ★R213：同一张码可能被重复扫码，或扫码与手机号通道同时登录。
            # 每次登录开一个新纪元；旧核验线程发现 epoch 变了必须立刻退场 ——
            # 否则两套结果互相覆盖，买家会看到「进度条乱跳 / 显示成功又变失败」。
            self.verify_epoch = int(getattr(self, "verify_epoch", 0) or 0) + 1
            epoch = self.verify_epoch
            _adv = ""
            try:   # R684: 登录确认后立刻把号段风险告诉买家（不用等 10s 失败才知道）
                _sr1 = _seg_recent_rate(self.account or "")
                if _sr1 and _sr1[2] >= 20 and _sr1[0] < 30:
                    _adv = "（提示：该号段近 2 小时成功率仅 %d%%，官方在限量，建议改用 +852 新号）" % _sr1[0]
            except Exception:
                pass
            self.status, self.message = "verifying", "登录成功，正在核验权益到账…" + _adv
            # ★R211：每次登录都重置核验计时，避免沿用到上一个账号
            self.verify_start = time.time()
            self.verify_i = 0
            self.verify_baseline_ms = time.time() * 1000.0 - GIFT_NEW_TOLERANCE * 1000.0
            self.relogin_required = False
            self.dup_recent = False        # ★R211d：新登录 = 新账号，清掉上一轮的重复标记
        # ★R506_VERIFY_PERSIST：核验**开始前**先落一条持久补核记录。
        # 线上 08:13 现场：DEMO0016 已扫码+已确认、验收第 1 轮时进程重启 → 既没 success 也没 fail，
        # 买家页面回到「等待扫码」，这一单凭空消失。先入队后核验，重启也能被补核线程接住。
        try:
            from app import reconcile as _rc0
            _rc0.enqueue(self.code, self.account or "", ck, dict(device), proxies=self.proxies,
                         iso=self.acct_iso or "", owner_id=self.owner_id, ua=self.ua, ip=self.ip,
                         since_ms=self.verify_baseline_ms, first_delay=180.0)
        except Exception as _e0:
            _log("[%s] 核验前登记持久补核失败(忽略): %s" % (self.code, str(_e0)[:120]))
        # ★R213d：出口探针（最坏 20s）放后台，不再挡核验第一轮
        threading.Thread(target=self._probe_exit_bg, daemon=True).start()
        # ★R271：如果登录路径刚起了出口对齐，等它落定（最多 15s）再发第一轮领取请求。
        # 线上 87 条 fail 带「中途换了国家」（US→HK 60 条），全是核验跑到一半出口被切走。
        _ev = getattr(self, "_region_settle", None)
        if _ev is not None and not _ev.is_set():
            _t0 = time.time()
            if _ev.wait(15.0):
                _log("[%s] 出口对齐完成（%.1fs），开始核验" % (self.code, time.time() - _t0))
            else:
                _log("[%s] 出口对齐超时 15s，按当前出口继续核验" % self.code)
        result = {}
        _seg_low = False          # ★R682：低成功率号段 → 第 2 轮没货就收口（少等一轮）
        for i in range(max(1, VERIFY_RETRY)):
            if i:
                time.sleep(VERIFY_SLEEP)
            with self.lock:
                if epoch != int(getattr(self, "verify_epoch", 0) or 0):
                    _log("[%s] 登录核验已被新的登录取代 → 旧线程退出（第 %d 轮前）" % (self.code, i + 1))
                    return
                self.verify_i = i + 1
            result = self._verify_once(ck, device, full=(i == 0),
                                       since_ms=self.verify_baseline_ms)
            with self.lock:
                if epoch != int(getattr(self, "verify_epoch", 0) or 0):
                    _log("[%s] 登录核验已被新的登录取代 → 丢弃本轮结果（第 %d 轮）" % (self.code, i + 1))
                    return
            # ★R212：补记 uid（手机号通道没有走 _on_logged_in 的 uid 提取）
            if i == 0 and result.get("uid"):
                with self.lock:
                    self.known["uid"] = str(result.get("uid"))
            # ★R212：cookie 已过期 → 立即让买家重新登录，不再空等 8 轮
            if result.get("auth_invalid") or result.get("relogin_required"):
                with self.lock:
                    if self._locked_done():
                        _log("[%s] R698 核验中已完成领取 → 丢弃「登录态失效」文案" % self.code)
                        return
                    self.relogin_required = True
                    self.message = "登录状态已失效（会话过期），请在手机上重新扫码或重新登录后再试。"
                _log("[%s] 登录态已失效（%s）→ 提前结束核验，需重新登录"
                     % (self.code, result.get("auth_detail") or "session expired"))
                break
            # ★R212：到期日很新但生效时间早于本次登录 = 之前领过的旧礼包，不是本次到账
            if i == 0 and (not (result or {}).get("got")) and result.get("gift_duplicate"):
                _de = str(result.get("gift_duplicate_effective") or "")
                with self.lock:
                    if self._locked_done():
                        _log("[%s] R698 核验中已完成领取 → 丢弃「旧礼包」文案" % self.code)
                        return
                    self.dup_recent = True
                    self.message = ("该账号此前已领取过体验套餐"
                                    + ("（生效 %s，北京时间）" % _de if _de else "")
                                    + "，本次没有新增；同一账号通常只能领一次")
                _log("[%s] 发现旧礼包（生效 %s）→ 提前结束核验" % (self.code, _de or "?"))
                break
            # ★R211：该账号 0.5~5 天内刚领过 → 不用再等 8 轮，直接给明确结论
            if i == 0 and (not (result or {}).get("got")) and result.get("gift_recent") \
                    and result.get("gift_recent_end"):
                _log("[%s] 该账号近期已领取过（到期 %s）→ 提前结束核验"
                     % (self.code, result.get("gift_recent_end")))
                with self.lock:
                    if self._locked_done():
                        _log("[%s] R698 核验中已完成领取 → 丢弃「近期已领」文案" % self.code)
                        return
                    self.dup_recent = True
                    self.message = ("该账号近期已领取过体验套餐（到期 %s，北京时间），"
                                    "同一账号通常只能领一次" % result.get("gift_recent_end"))
                break
            # ★R212b：已有历史礼包且本次没有新增 → 老号不用再空等 8 轮
            if i == 0 and (not (result or {}).get("got")) and result.get("gift_any") \
                    and result.get("gift_stale_end"):
                _se = str(result.get("gift_stale_end") or "")
                with self.lock:
                    if self._locked_done():
                        _log("[%s] R698 核验中已完成领取 → 丢弃「历史礼包」文案" % self.code)
                        return
                    self.dup_recent = True
                    self.message = ("该账号此前已领取过体验套餐（到期 %s，北京时间），"
                                    "本次没有新增；同一账号通常只能领一次" % _se)
                _log("[%s] 历史礼包已存在（到期 %s）→ 提前结束核验" % (self.code, _se or "?"))
                break
            # ★R682：首轮就知道该号段整体塌了（<25%，n>=30）→ 标记，第 2 轮仍无货直接收口
            if i == 0 and not (result or {}).get("got"):
                try:
                    _sr0 = _seg_recent_rate(result.get("mobile") or getattr(self, "phone", "") or "")
                    if _sr0 and _sr0[0] < 30 and _sr0[2] >= 25:   # R683: align with red banner threshold
                        _seg_low = True
                        _log("[%s] 号段近2h成功率 %d%%（%d/%d）→ 第2轮无货即给结论"
                             % (self.code, _sr0[0], _sr0[1], _sr0[2]))
                except Exception:
                    pass
            # ★R213c：实测成功样本 97% 出现在前 2 轮（397+290），第 3 轮后合计仅 25 次；
            # 连续 3 轮「订阅记录为空 + 会员入口无有效订阅 + 无网络错误」时再空等，
            # 买家只会觉得进度条卡死。这里提前给结论；后台 40 分钟补核照常，
            # 晚到账仍会自动补记并把页面翻成成功。（★R682：低号段第 2 轮就收口）
            if (((i >= 1 and _seg_low) or i >= 2) and not (result or {}).get("got")
                    and not (result or {}).get("items")
                    and not ((result or {}).get("entry") or {}).get("has_active")
                    and not (result or {}).get("errors")):
                _log("[%s] 连续 %d 轮无订阅记录/无会员入口 → 提前给结论（后台补核继续）"
                     % (self.code, i + 1))
                break
            _log("[%s] 验收#%d account=%s items=%d got=%s"
                 % (self.code, i + 1, result.get("account"),
                    len(result.get("items") or []), result.get("got")))
            self._note_membership(result, first=(i == 0))        # R82-L：记 before/after
            if i == 0 and not self.acct_iso and not self.region_manual:
                # ★R84-Q：登录时没识别出账号地区 → 用核验返回的 country/mobile 补识别一次。
                #   **只记录/展示**，不在这里中途换出口（出口跳变是已知的最大杀手）。
                try:
                    _iso2 = account_iso({"raw": {"country_code": result.get("country")},
                                         "mobile": result.get("mobile")})
                except Exception:
                    _iso2 = ""
                if _iso2:
                    with self.lock:
                        self.acct_iso = _iso2
                        self.acct_iso_src = "account"
                    _log("[%s] 账号地区补识别：%s（出口保持 %s，不中途切换）"
                         % (self.code, _iso2, self.exit_region or "?"))
            if result.get("got"):
                break
            with self.lock:
                if self.status == "verifying":
                    self.message = ("登录成功，正在核验权益到账…（第 %d/%d 次，官方通常 1~5 分钟到账）"
                                    % (i + 1, max(1, VERIFY_RETRY)))
        self._finish(result, epoch=epoch)
        if (not (result or {}).get("got") and not self._late_started
                and not (result or {}).get("auth_invalid")
                and not getattr(self, "relogin_required", False)
                and not getattr(self, "dup_recent", False)
                and not (result or {}).get("gift_duplicate")):
            self._late_started = True
            with self.lock:
                self.late_state = "running"
                self.late_tries = 0
                self.late_until = time.time() + (LATE_DELAYS[0] if LATE_DELAYS else 60.0)
            # ★R76：交给**持久化**补核 worker（买家关掉页面/会话过期后也还能补记成功）
            try:
                from app import reconcile as _rc
                _rc.enqueue(self.code, self.account or _acct, ck, dict(device), proxies=self.proxies,
                            iso=self.acct_iso or "", owner_id=self.owner_id, ua=self.ua, ip=self.ip,
                            since_ms=self.verify_baseline_ms)
                _log("[%s] 已登记持久补核" % self.code)
            except Exception as e:
                _log("[%s] 登记持久补核失败(忽略): %s" % (self.code, str(e)[:120]))
            threading.Thread(target=self._late_recheck, args=(ck, dict(device)), daemon=True).start()

    def _late_recheck(self, ck: dict, device: dict):
        """福利是异步发放的：判失败后再隔一段时间复查，真到账了就自动补记为成功。"""
        with self.lock:
            epoch = int(getattr(self, "verify_epoch", 0) or 0)
        for _i, delay in enumerate(LATE_DELAYS or [60.0]):
            time.sleep(delay)
            with self.lock:
                # ★R213：买家重新登录/重新扫码后，旧登录态的补核必须作废
                if epoch != int(getattr(self, "verify_epoch", 0) or 0):
                    _log("[%s] 延迟复查已被新的登录取代 → 停止" % self.code)
                    return
                self.late_tries = _i + 1
                self.late_until = time.time() + (LATE_DELAYS[_i + 1] if _i + 1 < len(LATE_DELAYS) else 0.0)
            try:
                r = self._verify_once(ck, device, full=False,
                                      since_ms=self.verify_baseline_ms)
            except Exception as e:
                _log("[%s] 延迟复查异常: %s" % (self.code, str(e)[:140]))
                continue
            if r.get("auth_invalid") or r.get("relogin_required"):
                with self.lock:
                    self.relogin_required = True
                    self.late_state = "exhausted"
                    self.late_until = 0.0
                _log("[%s] 延迟复查：登录态已失效 → 停止补核" % self.code)
                return
            if r.get("gift_duplicate"):
                with self.lock:
                    self.dup_recent = True
                    self.late_state = "exhausted"
                    self.late_until = 0.0
                _log("[%s] 延迟复查：只是此前已领过的旧礼包 → 停止补核" % self.code)
                return
            if not (r or {}).get("got"):
                continue
            with self.lock:
                # ★R213：网络查询期间如果买家重新登录了，这份旧结果必须丢弃
                if epoch != int(getattr(self, "verify_epoch", 0) or 0):
                    _log("[%s] 延迟复查结果已过期 → 丢弃" % self.code)
                    return
            if self._code_disabled():
                _log("[%s] 链接已停用 → 停止延迟补记" % self.code)
                with self.lock:
                    self.late_state = "done"
                return
            _gli = (r.get("items") or [])
            _gift_ends = [i.get("end") for i in _gli
                          if i.get("source") == "pc_membership_gift" and i.get("end")]
            ends = _gift_ends or [i.get("end") for i in _gli if i.get("end")]
            limit = code_limit(self.code)
            acct = (r.get("account") or "").strip() or self.account
            # ★R689：后台持久补核同样必须查库（重启后内存 status 是空的）
            if self._db_success_guard(acct, "延迟复查"):
                return
            self._note_second_account((self.known or {}).get("uid") or "", "延迟复查")
            self._note_membership(r)                  # R82-L
            with self.lock:
                if self.status == "success":
                    self.late_state = "done"
                    return
                self.late_state = "done"
                self.claims += 1
                self.max_claims = limit
                self.account = acct
                self.status = "success"
                if limit > 0 and self.claims >= limit:
                    self.claimed = True
                self.message = "🎉 领取成功！30 天体验套餐已到账（延迟到账，已自动补记；请在豆包电脑版/网页版查看会员权益）"
                self.detail = dict(self.detail or {})
                self.detail.update({"account": acct, "items": r.get("items") or [],
                                    "entry": r.get("entry") or {}, "hint": ""})
            try:
                from app import store
                store.add_claim(self.code, limit)
            except Exception:
                pass
            _ev(self.code, "success", account=acct, ip=self.ip, ua=self.ua, owner_id=self.owner_id,
                uid=(self.known or {}).get("uid") or "", detail=self._ev_detail(r),
                note=("30 天体验套餐已到账（延迟补记" + ("，到期 " + " / ".join(ends) if ends else "") + "）"))
            _log("[%s] 延迟复查：补记成功 account=%s" % (self.code, acct or "?"))
            return
        with self.lock:
            self.late_state = "exhausted"
            self.late_until = 0.0
        _log("[%s] 延迟复查：%d 轮仍未到账 → 判定结束" % (self.code, len(LATE_DELAYS or [1])))

    def recheck_now(self) -> dict:
        """★R76：买家点「重新核验」—— 立刻用存下来的登录态再查一次权益到账。

        为什么需要：礼包是**异步发放**的（实测中位 24s，尾巴能到 10 分钟以上）。
        以前第一次查不到就直接告诉买家「本次未领取到」，买家关掉页面之后即使到账了也不知道。
        """
        now = time.time()
        with self.lock:
            if now - (self._recheck_at or 0) < RECHECK_MIN_GAP:
                return {"ok": False, "message": "刚查过啦，%d 秒后再试（我们也在后台自动复查）"
                        % max(1, int(RECHECK_MIN_GAP - (now - self._recheck_at)))}
            self._recheck_at = now
            ck = dict(self._ck or {})
            dev = dict(self.device or {})
            epoch = int(getattr(self, "verify_epoch", 0) or 0)
            already = (self.status == "success")
        if already:
            return {"ok": True, "got": True, "message": "已经领取成功啦"}
        if not ck:
            return {"ok": False, "message": "还没有可核验的登录信息，请重新扫码或重新登录"}
        try:
            r = self._verify_once(ck, dev, full=False,
                                  since_ms=self.verify_baseline_ms)
        except Exception as e:
            _log("[%s] 手动复查异常: %s" % (self.code, str(e)[:140]))
            return {"ok": False, "message": "查询失败，请稍后再试（我们也在后台自动复查）"}
        if r.get("auth_invalid") or r.get("relogin_required"):
            with self.lock:
                self.relogin_required = True
                self.late_state = "exhausted"
                self.late_until = 0.0
            return {"ok": False, "relogin_required": True,
                    "message": "登录状态已失效（会话过期），请在手机上重新扫码或重新登录后再试。"}
        if r.get("gift_duplicate"):
            _de = str(r.get("gift_duplicate_effective") or "")
            with self.lock:
                self.dup_recent = True
                self.late_state = "exhausted"
                self.late_until = 0.0
            return {"ok": True, "got": False,
                    "message": ("该账号此前已领取过体验套餐"
                                + ("（生效 %s，北京时间）" % _de if _de else "")
                                + "，本次没有新增；同一账号通常只能领一次")}
        if (r or {}).get("got"):
            self._apply_late_success(r, tag="手动复查", epoch=epoch)
            return {"ok": True, "got": True, "message": "🎉 查到已经到账，本页已更新"}
        with self.lock:
            if self.late_state != "done":
                self.late_state = "running"
        return {"ok": True, "got": False,
                "message": "暂时还没查到，官方是延迟发放的，通常再等 1~5 分钟；我们也在后台自动复查"}

    def _note_second_account(self, uid: str, tag: str = "核验") -> None:
        """★R693：同一张卡已经有过别的 uid 的 success → 记一条告警（只观测，不改行为）。"""
        try:
            import sqlite3 as _sq
            from app import store as _st
            _c = _sq.connect(_st.DB_PATH, timeout=8)
            try:
                _rows = _c.execute(
                    "SELECT DISTINCT uid FROM events WHERE code=? AND kind='success' AND uid<>''",
                    (self.code,)).fetchall()
            finally:
                _c.close()
            _other = [r[0] for r in _rows if r[0] and str(r[0]) != str(uid or "")]
            if _other:
                _log("[%s] ★R693 同卡第二账号成功（%s，此前 uid=%s）→ 一码多领"
                     % (self.code, tag, ",".join(str(x) for x in _other[:2])))
        except Exception:
            pass

    def _locked_done(self) -> bool:
        """★R698：本会话是否已经锁定为「领取成功」（调用方需持有 self.lock）。"""
        return bool(getattr(self, "claimed", False)) or self.status == "success"

    def _db_success_guard(self, acct: str, tag: str = "补记") -> bool:
        """★R689：库级幂等守卫 —— 该码（同 uid 或同账号）在 events 里已有 success 就直接收口。

        为什么必须查库：进程重启后内存状态清空，持久补核会把同一单再跑一遍，
        旧逻辑只查 self.status → 重复写 success 事件、重复 add_claim，
        成功率被算高（线上 01:36/01:40/01:49 三次重启后出现 7 个「一码两条成功」）。
        同时把 claimed/claims/message 回填，避免「status=success 但还能换码」把成功盖掉。
        """
        try:
            import sqlite3 as _sq
            from app import store as _st
            _uid0 = (self.known or {}).get("uid") or ""
            _c = _sq.connect(_st.DB_PATH, timeout=8)
            try:
                _dup = _c.execute(
                    "SELECT 1 FROM events WHERE code=? AND kind='success'"
                    " AND (uid=? OR (account<>'' AND account=?)) LIMIT 1",
                    (self.code, _uid0, acct or "\x00")).fetchone()
            finally:
                _c.close()
        except Exception as _e:
            _log("[%s] R689 查重失败（照常补记）: %s" % (self.code, str(_e)[:100]))
            return False
        if not _dup:
            return False
        _row = {}
        try:
            from app import store as _st2
            _row = _st2.get(self.code) or {}
        except Exception:
            pass
        with self.lock:
            self.claims = max(int(self.claims or 0), int(_row.get("claims") or 0), 1)
            _lim = int(code_limit(self.code) or 0)
            self.max_claims = _lim if _lim > 0 else max(int(self.max_claims or 0), 1)
            if self.max_claims <= 0 or self.claims >= self.max_claims:
                self.claimed = True
            self.status = "success"
            self.late_state = "done"
            self.late_until = 0.0
            if acct:
                self.account = acct
            # 已经在前台展示过「30 天套餐已到账」就别改写文案，只补状态/claimed
            if self.status != "success" or not self.message:
                self.message = ("🎉 领取成功！本码已完成领取"
                                + ("：" + self.account if self.account else ""))
        _log("[%s] R689 库里已有成功记录（%s）→ 跳过重复补记（%s）"
             % (self.code, acct or "?", tag))
        return True

    def _apply_late_success(self, r: dict, tag: str = "延迟补记", epoch=None) -> None:
        """把「延迟查到/手动查到」的到账结果落成成功（幂等：已成功就不再重复记 claim）。"""
        if epoch is not None and int(epoch) != int(getattr(self, "verify_epoch", 0) or 0):
            _log("[%s] 丢弃过期的补记结果（已有新的登录在跑）" % self.code)
            return
        if self._code_disabled():
            _log("[%s] 链接已停用 → 丢弃补记" % self.code)
            return
        _gli = (r.get("items") or [])
        _gift_ends = [i.get("end") for i in _gli
                      if i.get("source") == "pc_membership_gift" and i.get("end")]
        ends = _gift_ends or [i.get("end") for i in _gli if i.get("end")]
        limit = code_limit(self.code)
        acct = (r.get("account") or "").strip() or self.account
        # ★R689：库级幂等（同 code + 同 uid/账号已有 success 就不重复落库）
        if self._db_success_guard(acct, "补记"):
            return
        self._note_second_account((self.known or {}).get("uid") or "", "补记")
        self._note_membership(r)                      # R82-L
        with self.lock:
            if self.status == "success":
                self.late_state = "done"
                return
            self.claims += 1
            self.max_claims = limit
            self.account = acct
            self.status = "success"
            self.late_state = "done"
            if limit > 0 and self.claims >= limit:
                self.claimed = True
            self.message = "🎉 领取成功！30 天体验套餐已到账（延迟到账，已自动补记；请在豆包电脑版/网页版查看会员权益）"
            self.detail = dict(self.detail or {})
            self.detail.update({"account": acct, "items": r.get("items") or [],
                                "entry": r.get("entry") or {}, "hint": "",
                                # ★R699b：补记成功也要有「到账时间」
                                "done_at": time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600))})
        try:
            from app import store
            store.add_claim(self.code, limit)
        except Exception:
            pass
        _ev(self.code, "success", account=acct, ip=self.ip, ua=self.ua, owner_id=self.owner_id,
            uid=(self.known or {}).get("uid") or "", detail=self._ev_detail(r),
            note=("30 天体验套餐已到账（%s" % tag) + ("，到期 " + " / ".join(ends) if ends else "") + "）")
        _log("[%s] %s：补记成功 account=%s" % (self.code, tag, acct or "?"))

    # ---------------------------------------------------------------- 循环
    def _phone_active(self) -> bool:
        """★R344_QR_PAUSE：手机号通道进行中（发码后 15 分钟内）且用户没主动切回二维码。

        用途：暂停二维码轮询 —— 省一条出口流量，也避免「二维码已过期」覆盖「验证码已发送」。
        """
        try:
            if getattr(self, "_qr_force", False):
                return False
            if not (self._phone is not None and self.phone):
                return False
            _at = float(getattr(self, "phone_at", 0.0) or 0.0)
            return _at > 0.0 and (time.time() - _at) < 900.0
        except Exception:
            return False

    def _run(self):
        while self.alive() and not self.dead:
            try:
                with self.lock:
                    st, token, client, dev = self.status, self.token, self.client, self.device
                    remain, busy = self.expire - time.time(), self.busy
                # ★R101：出码彻底失败（_do_rotate 3 次都没成）→ 后台自动重排。
                # 老版本这里是「st in (success, fail, error) → 一直 sleep 0.8s」，
                # 页面会永远停在「二维码申请失败」，只能靠买家再点一次「换一张」。
                if st == "error":
                    back = min(60.0, max(5.0, 3.0 * float(self.qr_fail_total or 1)))
                    if (not busy) and (time.time() - float(getattr(self, "_retry_at", 0.0) or 0.0)) >= back:
                        if self._try_busy():
                            self._retry_at = time.time()
                            _log("[%s] 出码失败自动重排（累计失败 %d 次，退避 %.0fs）"
                                 % (self.code, self.qr_fail_total, back))
                            try:
                                self._do_rotate()
                            except Exception as e:
                                _log("[%s] 自动重排异常: %s" % (self.code, str(e)[:160]))
                            finally:
                                self._release_busy()
                        continue
                    time.sleep(0.8)
                    continue
                if not dev or not token or not client:
                    time.sleep(0.5); continue
                if st in ("success", "fail"):
                    time.sleep(0.8); continue
                if st == "verifying" or busy:
                    time.sleep(0.4); continue
                # ★R344_QR_PAUSE：手机号通道进行中 → 暂停二维码轮询（省出口流量 + 不覆盖手机号提示）
                if st == "waiting" and self._phone_active():
                    self._wake.wait(20.0)
                    self._wake.clear()
                # ★R86：快到期了。是否换码交给 _maybe_rotate_expiring 决策 ——
                # 铁律是「已经被人扫过的码，只要没自然过期就绝不换」
                # （线上 43,727 次 new vs 49 次 scanned 的元凶）。
                if remain < REFRESH_BELOW and self._maybe_rotate_expiring(st, token, client, remain):
                    time.sleep(0.5)          # ★R248：换码分支也要让出 CPU，绝不空转
                    continue
                try:
                    d = client.check_qrconnect(token)
                    self._poll_fails = 0
                except Exception as _pe:
                    # ★R172：轮询失败必须先留痕（R170 把日志放在 raise 之后，坏 sid 会话看不到原因）
                    _log("[%s] 轮询失败: %s" % (self.code, str(_pe)[:90]))
                    # ★R203：整片 403 / 隧道失败 = 代理供应商故障，先切直连（换 sid 只会换到下一个死出口）
                    _estr = str(_pe)
                    _dead = (("403" in _estr) or ("CONNECT tunnel" in _estr)
                             or ("Tunnel connection failed" in _estr))
                    if _dead and self.proxies:
                        _nc = self._go_direct("轮询403")
                        if _nc is not None:
                            client = _nc
                            self._poll_fails = 0
                            _log("[%s] 出口 403 → 本会话已切直连，后续轮询/登录/核验都走直连" % self.code)
                    if not _qr_fast_anon() and not (_dead and not self.proxies):
                        raise
                    self._poll_fails = int(getattr(self, "_poll_fails", 0) or 0) + 1
                    if self._poll_fails >= 2:
                        # 同一张码连续失败 ≥2 次：重建轮询客户端
                        _n_fail = int(getattr(self, "_poll_fails", 0) or 0)
                        _old = client
                        try:
                            if _qr_poll_direct():
                                # ★R211c：直连模式绝不在这里「换代理出口」。
                                # 旧逻辑会新建代理 client 并 self.client = 它 —— 代理一挂就
                                # 变成 403 → 重建 → 403 的死循环（线上 3 小时 3.7 万次重建）。
                                # 这里只重建直连客户端；self.proxies 保留给登录/核验用。
                                _nc = PassportClient(proxies=None, timeout=QR_REQ_TIMEOUT,
                                                     fp=getattr(_old, "fp", None))
                                try:
                                    _nc.s.cookies.update(_old.s.cookies)
                                except Exception:
                                    pass
                                with self.lock:
                                    self.client = _nc
                                client = _nc
                                self._poll_fails = 0
                                _log("[%s] 轮询连续失败 → 已重建直连会话（第 %d 次）"
                                     % (self.code, _n_fail))
                            else:
                                from app import proxy as _p
                                _p.rotate(self.code)
                                _np = _proxy_for(self.code)
                                _nc = PassportClient(proxies=_np, timeout=QR_REQ_TIMEOUT,
                                                     fp=getattr(_old, "fp", None))
                                try:
                                    _nc.s.cookies.update(_old.s.cookies)
                                except Exception:
                                    pass
                                with self.lock:
                                    self.proxies = _np
                                    self.client = _nc
                                client = _nc
                                self._poll_fails = 0
                                _log("[%s] 轮询连续失败 → 已换新出口并重建会话（第 %d 次）"
                                     % (self.code, _n_fail))
                            try:
                                _s = getattr(_old, "s", None)
                                if _s is not None:
                                    _s.close()
                            except Exception:
                                pass
                        except Exception as _e2:
                            _log("[%s] 重建会话失败: %s" % (self.code, str(_e2)[:100]))
                    _c2 = self._poll_client()
                    if _c2 is None:
                        _log("[%s] 直连兜底不可用（客户端创建失败）" % self.code)
                        raise
                    try:
                        d = _c2.check_qrconnect(token)
                    except Exception as _e4:
                        _log("[%s] 直连兜底失败 proxies=%r: %s"
                             % (self.code, getattr(_c2, "proxies", None), str(_e4)[:110]))
                        raise
                    _log("[%s] 直连兜底成功 status=%s"
                         % (self.code, str((d or {}).get("status"))[:16]))
                self._last_poll = time.time()
                s = d.get("status")
                with self.lock:
                    if self.token != token:
                        continue
                    # ★R687：这一轮轮询是在成功之前发出去的 —— 回来时已经 success/claimed，
                    # 绝不能再把状态写成 scanned/verifying（否则 _on_logged_in 被 R685 挡掉后会卡在核验中）
                    if bool(getattr(self, "claimed", False)) or self.status == "success":
                        _log("[%s] R687 轮询回包晚于成功（status=%s）→ 忽略" % (self.code, str(s)[:16]))
                        continue
                    # ★R86：未扫码时服务端一直回 "new"，而本地 status 是 "waiting" ⇒
                    # 老判断每轮都成立，每个码每 1.5~3s 白打一行（16 个码 ≈ 43 万行/天，纯烧磁盘）。
                    # "new" 不算状态变化，不打日志。
                    if s and s != "new" and s != self.status:
                        _log("[%s] 扫码状态: %s" % (self.code, s))
                    if s in ("scanned", "scan"):
                        self.status, self.message = "scanned", "已扫描，请在手机上点击「确认登录」"
                        self._scanned_seq, self._scanned_at = self.seq, time.time()
                        if self._scan_logged_seq != self.seq:
                            self._scan_logged_seq = self.seq
                            _ev(self.code, "scan", ip=self.ip, ua=self.ua, owner_id=self.owner_id,
                                note="对方已扫码（第 %d 个二维码）" % self.seq)
                            self._lock_region("对方已扫码")
                    elif s == "canceled":
                        if not self._phone_active():
                            self.status, self.message = "waiting", "你在手机上取消了登录，请重新扫码"
                    elif s == "expired":
                        if not self._phone_active():
                            # ★R505_STALE_TRUTH：停止自动换码时不许再说「正在为你换一张新的」，
                            # 否则买家会一直盯着死码等（线上最典型的「扫不上」）。
                            _want = self._expired_message()
                            if (self.message or "") != _want:
                                self.message = _want
                    elif s and s != "new":
                        self.status, self.message = "verifying", "正在核验…"
                if d.get("error_code") == 0 and s and s not in ("new", "scanned", "scan", "expired", "canceled"):
                    _log("[%s] 登录确认 status=%s → 先读账号再验收" % (self.code, s))
                    self._on_logged_in(dev, client)
                    time.sleep(0.6); continue
                self._wake.wait(self._poll_interval(st))
                self._wake.clear()
            except Exception as e:
                _log("[%s] worker 异常: %s" % (self.code, str(e)[:200]))
                time.sleep(POLL_SEC)


class Manager:
    """兑换码 → 会话 的注册表（带容量上限与空闲回收）。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.items: dict[str, Session] = {}
        self.started = time.time()
        threading.Thread(target=self._gc_loop, daemon=True).start()

    def get(self, code: str, create: bool = True):
        if not code:
            return None
        with self.lock:
            s = self.items.get(code)
            if s and not s.dead:
                was_idle = (time.time() - (s.seen or 0)) > UNWATCHED_AFTER
                s.seen = time.time()
                if was_idle:
                    # R122：页面离开过又回来 → 立刻唤醒轮询线程补码，不用干等 120s
                    try:
                        s._wake.set()
                    except Exception:
                        pass
                return s
            if not create:
                return None
            self._evict_for_capacity()
            s = Session(code)
            self.items[code] = s
            return s

    def note(self, code: str, ip: str = "", ua: str = ""):
        """把买家 IP/UA 转达给会话（还没建会话就先不管）。"""
        s = self.get(code, create=False)
        if s:
            s.note_meta(ip, ua)
        return s

    def kill(self, code: str):
        with self.lock:
            s = self.items.pop(code, None)
        if s:
            s.dead = True

    def stats(self) -> dict:
        with self.lock:
            items = list(self.items.values())
        d = {}
        for s in items:
            d[s.status] = d.get(s.status, 0) + 1
        d["total"] = len(items)
        d["uptime_s"] = int(time.time() - self.started)
        return d

    def _evict_for_capacity(self):
        # R214 热会话保护：正在被页面轮询（SEEN_HOT 秒内）的会话不剔，
        # 否则「剔掉->下一次轮询立即重建->再剔掉别人」会雪崩（实测 5 次/秒）。
        if len(self.items) < MAX_SESSIONS:
            return
        now = time.time()
        cand = []
        for s in self.items.values():
            fin = s.claimed or s.status in ("success", "fail", "error")
            if (not fin) and (now - (s.seen or 0)) < 60:
                continue
            cand.append(s)
        cand.sort(key=lambda s: (s.claimed, s.seen))
        for s in cand:
            if len(self.items) < MAX_SESSIONS:
                break
            self.items.pop(s.code, None)
            s.dead = True
            _log("会话回收(容量) %s" % s.code)
        if len(self.items) >= MAX_SESSIONS:
            hard = MAX_SESSIONS * 2
            if len(self.items) >= hard:
                extra = len(self.items) - MAX_SESSIONS + 1
                for s in sorted(self.items.values(), key=lambda x: (x.claimed, x.seen))[:extra]:
                    self.items.pop(s.code, None)
                    s.dead = True
                    _log("会话回收(硬上限) %s" % s.code)
            _log("会话容量告警：全热会话 %d 个，暂不回收（now=%.0f）" % (len(self.items), now))
    def _release_finished_clients(self) -> int:
        """★R86b：放掉**终态**会话的出码 client（每个 ≈3~4MB 原生内存），返回释放个数。

        实测：150 个会话各常驻一个 curl_cffi/BoringSSL 会话 ⇒ RSS ≈700MB（跑 25h 实测 696MB、
        峰值 829.8MB）。这些会话一旦到了终态就再也不会用到 client，必须立刻放掉。
        """
        freed = 0
        for s in list(self.items.values()):
            if s.status == "success" or (s.status in ("fail", "error")
                                         and s.late_state in ("done", "exhausted")):
                if s._drop_client("已结束"):
                    freed += 1
        return freed
    def _trim_heap(self):
        """★R86b：把 glibc 里「已释放但没还给内核」的内存页交回去。

        长跑的多线程 Python + curl_cffi 会把 RSS 顶到几百 MB（碎片/arena 高水位），
        malloc_trim(0) 是让 RSS 真正降下来的唯一办法；60 秒一次，代价≈0（非 glibc 平台自动跳过）。
        """
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass
    def _gc_loop(self):
        while True:
            time.sleep(60)
            try:
                now = time.time()
                with self.lock:
                    for code, s in list(self.items.items()):
                        fin = s.claimed or s.status in ("success", "fail", "error")
                        ttl = DONE_TTL if fin else IDLE_TTL
                        if now - s.seen > ttl:
                            s.dead = True
                            self.items.pop(code, None)
                            _log("会话回收(空闲) %s status=%s" % (code, s.status))
                # ★R86b：终态会话不再需要出码 client（每个 ≈3~4MB 原生内存）
                freed = self._release_finished_clients()
                if freed:
                    _log("★R86b 已释放 %d 个结束会话的出码内存" % freed)
                self._trim_heap()
            except Exception as e:
                _log("GC 异常: %s" % str(e)[:160])


SM = Manager()
