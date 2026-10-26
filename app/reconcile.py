# -*- coding: utf-8 -*-
"""R75: 长尾补核 worker —— 持久化复查「页面显示失败、豆包其实已到账」。

生产实证（2026-10-13 18:31，code=DEMO0001）：
    18:31:11  fail    「境外号码 +1 本次未到账…」        <- 买家当时看到的
    18:32:05  success 「30 天体验套餐已到账（延迟补记，到期 2026-11-12 18:31）」
官方的 30 天福利是登录后「异步发放」的（实测中位 24s、最长 212s）。旧实现只在进程内会话
（sessions.Session._late_recheck）里复查，会话一过期 / 进程一重启就再也补不回来
=> 买家永远停在「本次未领取到」，而豆包那边其实已经到账。

本模块把这条长尾复查「落库」（与 store 同一张 codes.db 里的 pending_verify 表）：
    enqueue() 入队 -> 后台线程按退避序列复查 sdk.verify_subscription()
    真到账  -> store.add_claim() + success 事件（note 带「延迟补记」）+ 刷新活跃会话
    没到账  -> 退避重试；超窗 -> given_up（不写 success，页面维持失败）

只依赖 app.store / app.sdk（只读复用）。不改 sessions.py / main.py，由接线方调用 start()。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time

from app import store

# --------------------------------------------------------------------------- #
# 配置（默认全关，接线时用环境变量打开）
# --------------------------------------------------------------------------- #
DEFAULT_DELAYS = (60, 180, 300, 600, 1200, 2400)   # 退避序列，合计约 75 分钟
DEFAULT_MAX_AGE = 10800.0                          # 超过 3 小时的待复查记录直接放弃
SCAN_LIMIT = int(os.environ.get("RECONCILE_SCAN_LIMIT", "20"))   # R214: 高流量可调

_lock = threading.RLock()
_slock = threading.RLock()          # 保护下面的开关/线程句柄
_table_ready = False
_started = False
_thread = None
_stop = threading.Event()
_interval = 300.0
_max_age_override = None

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pending_verify(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT,
    account     TEXT,
    ck_json     TEXT,
    device_json TEXT,
    proxies_json TEXT,
    iso         TEXT,
    owner_id    INTEGER DEFAULT 0,
    ua          TEXT,
    ip          TEXT,
    created_at  INTEGER,
    tries       INTEGER DEFAULT 0,
    next_at     INTEGER,
    state       TEXT DEFAULT 'pending',   -- pending | done | given_up
    updated_at  INTEGER,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_pv_due  ON pending_verify(state, next_at);
CREATE INDEX IF NOT EXISTS idx_pv_code ON pending_verify(code, state);
"""


# --------------------------------------------------------------------------- #
# 日志（默认往 stdout 写；控制台是 GBK 时中文可能写不出，异常吞掉即可）
# --------------------------------------------------------------------------- #
def _default_log(msg) -> None:
    try:
        sys.stdout.write("[reconcile] %s\n" % msg)
        sys.stdout.flush()
    except Exception:
        pass


_log = _default_log


def set_logger(fn) -> None:
    """接线方把主日志函数注入进来（与 sessions.set_logger 同款）。"""
    global _log
    _log = fn if callable(fn) else _default_log


# --------------------------------------------------------------------------- #
# 环境变量 / 时间（都做成函数，方便测试与运行期改配置）
# --------------------------------------------------------------------------- #
def _enabled() -> bool:
    return str(os.environ.get("RECONCILE", "0")).strip().lower() in ("1", "true", "yes", "on", "y")


def _delays():
    """退避序列（秒）。RECONCILE_DELAYS="60,180,300"；空/非法就回落默认。"""
    raw = os.environ.get("RECONCILE_DELAYS", "")
    out = []
    for x in str(raw).split(","):
        x = x.strip()
        if not x:
            continue
        try:
            v = int(float(x))
        except Exception:
            continue
        if v > 0:
            out.append(v)
    return out or list(DEFAULT_DELAYS)


def _max_age() -> float:
    if _max_age_override is not None:
        return float(_max_age_override)
    try:
        return float(os.environ.get("RECONCILE_MAX_AGE", str(DEFAULT_MAX_AGE)))
    except Exception:
        return DEFAULT_MAX_AGE


def _now() -> float:
    """当前时间（测试会 monkeypatch 它来快进）。"""
    return time.time()


# --------------------------------------------------------------------------- #
# 存储（与 store 同一张库、同一套约定：WAL + timeout + Row）
# --------------------------------------------------------------------------- #
def _conn():
    os.makedirs(os.path.dirname(store.DB_PATH), exist_ok=True)
    c = sqlite3.connect(store.DB_PATH, timeout=15, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    with _lock:
        if _table_ready:
            return
        with _conn() as c:
            c.executescript(_TABLE_SQL)
        _table_ready = True


def _code_limit(code: str) -> int:
    """该卡密可领几次（复用 sessions.code_limit；拿不到就 0=不限次）。"""
    try:
        from app.sessions import code_limit
        return int(code_limit(code))
    except Exception:
        try:
            v = store.max_claims_of(code)
            return int(v) if v is not None and v >= 0 else 0
        except Exception:
            return 0


def _acct_key(a: str) -> str:
    """★R703：账号串归一化 —— '豆包32 / +1******20' 与 '豆包32 /' 视为同一账号。"""
    s = (a or "").strip()
    if "/" in s:
        s = s.split("/", 1)[0]
    return s.strip().lower()


def _has_success(code: str, account: str, uid: str = "") -> bool:
    """★R703 幂等闸门：同一 code + 同一账号（uid 优先，其次账号串）已有 success 就返回 True。

    线上物证（DEMO0002 / DEMO0003 / DEMO0004 / DEMO0005 / DEMO0006）：
    会话侧写的账号是 '豆包32 /'（没带手机号掩码），补核侧是 '豆包32 / +1******20'，
    旧逻辑只做**字符串全等**比较 → 补核又记了一条 success + 又加了一次 claim。
    现在：两边都有 uid 就以 uid 为准；uid 缺失才退回账号串精确/归一化比较。
    """
    a = (account or "").strip()
    u = (uid or "").strip()
    if not code or (not a and not u):
        return False
    try:
        for r in store.events(code=code, kind="success", limit=200):
            ra = (r.get("account") or "").strip()
            ru = str(r.get("uid") or "").strip()
            if u and ru:
                if ru == u:
                    return True
                continue              # 两个 uid 都在且不同 = 另一单，不做昵称兜底
            if a and ra and ra == a:
                return True
            if a and ra and _acct_key(a) and _acct_key(a) == _acct_key(ra):
                return True
    except Exception as e:
        _log("查成功事件失败 %s: %s" % (code, str(e)[:120]))
    return False


def _mark(code: str, account: str, state: str, note: str = "") -> None:
    """把某 code+account 的所有 pending 行改成 state（done / given_up）。"""
    try:
        with _lock, _conn() as c:
            c.execute("UPDATE pending_verify SET state=?, updated_at=?, note=? "
                      "WHERE code=? AND account=? AND state='pending'",
                      (state, int(_now()), (note or "")[:200], code, account or ""))
    except Exception as e:
        _log("标记状态失败 %s: %s" % (code, str(e)[:120]))


# --------------------------------------------------------------------------- #
# 对外接口 1/4：入队
# --------------------------------------------------------------------------- #
def enqueue(code: str, account: str, ck: dict, device: dict, proxies: dict | None = None,
            iso: str = "", owner_id: int = 0, ua: str = "", ip: str = "",
            since_ms: float = 0.0, first_delay: float | None = None) -> None:
    """把会话加进长尾复查队列（幂等、失败不影响主流程）。

    ★R506_VERIFY_PERSIST：first_delay 让「核验开始前」也能先落一条记录 ——
    主流程还在跑时先别抢（给 180s），跑完若已成功，worker 只看一眼 _has_success 就收尾；
    进程中途重启也不会让这笔单子凭空消失。
    """
    if not code:
        return
    try:
        _ensure_table()
        now = int(_now())
        account = (account or "").strip()
        if _has_success(code, account):
            _mark(code, account, "done", "已有成功记录，无需复查")
            _log("入队跳过（已有成功记录）%s / %s" % (code, account or "?"))
            return
        _dev = dict(device or {})
        if since_ms:
            try:
                _dev["_since_ms"] = int(float(since_ms))
            except Exception:
                pass
        first = int(first_delay if first_delay is not None else _delays()[0])
        with _lock, _conn() as c:
            row = c.execute("SELECT id FROM pending_verify WHERE state='pending' "
                            "AND code=? AND account=?", (code, account)).fetchone()
            if row:
                c.execute("UPDATE pending_verify SET next_at=?, updated_at=?, device_json=? WHERE id=?",
                          (now + first, now, json.dumps(_dev, ensure_ascii=False), row["id"]))
                _log("入队去重（已在队列）%s / %s" % (code, account or "?"))
                return
            c.execute(
                "INSERT INTO pending_verify(code,account,ck_json,device_json,proxies_json,iso,"
                "owner_id,ua,ip,created_at,tries,next_at,state,updated_at,note) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,0,?,'pending',?,'')",
                (code, account, json.dumps(ck or {}, ensure_ascii=False),
                 json.dumps(_dev, ensure_ascii=False),
                 json.dumps(proxies or {}, ensure_ascii=False) if proxies else "",
                 str(iso or ""), int(owner_id or 0), str(ua or "")[:200], str(ip or "")[:64],
                 now, now + first, now))
        _log("入队 %s / %s（首次复查 +%ss）" % (code, account or "?", first))
    except Exception as e:
        _log("入队失败 %s: %s" % (code, str(e)[:160]))


# --------------------------------------------------------------------------- #
# 对外接口 2/4：启动 / 停止
# --------------------------------------------------------------------------- #
def start(interval: float = None, max_age: float = None, force: bool = False):
    """启动后台复查线程（幂等：已在跑就返回同一个线程；RECONCILE!=1 且未 force 时不启动）。"""
    global _started, _thread, _interval, _max_age_override
    with _slock:
        if _started and _thread is not None and _thread.is_alive():
            return _thread
        if not force and not _enabled():
            _log("RECONCILE!=1，未启动（需要时设 RECONCILE=1）")
            return None
        try:
            _interval = float(interval if interval is not None
                              else os.environ.get("RECONCILE_INTERVAL", "300"))
        except Exception:
            _interval = 300.0
        if max_age is not None:
            _max_age_override = float(max_age)
        _stop.clear()
        _started = True
        _thread = threading.Thread(target=_loop, name="reconcile", daemon=True)
        _thread.start()
        _log("已启动 interval=%ss max_age=%ss delays=%s" % (_interval, _max_age(), _delays()))
        return _thread


def stop(join: bool = True, timeout: float = 3.0) -> None:
    """停止后台线程（幂等）。"""
    global _started
    _stop.set()
    t = _thread
    if join and t is not None and t.is_alive() and t is not threading.current_thread():
        try:
            t.join(timeout=timeout)
        except Exception:
            pass
    with _slock:
        if _thread is None or not _thread.is_alive():
            _started = False


def _loop() -> None:
    _log("worker loop start interval=%ss" % _interval)
    while not _stop.is_set():
        try:
            scan_once()
        except Exception as e:
            _log("scan_once 异常（已忽略）: %s" % str(e)[:200])
        _stop.wait(max(5.0, float(_interval)))
    _log("worker loop exit")


# --------------------------------------------------------------------------- #
# 对外接口 3/4：扫描一轮
# --------------------------------------------------------------------------- #
def _call_verify(ck, device, proxies, since_ms: float = 0.0):
    """唯一的出网口（测试可 monkeypatch 它；正式环境走 sdk.verify_subscription）。

    ★R211c：对账也是只读查询 —— 直连优先，只有直连出现网络错误才用代理补一次。
    这样后台补记不会因为代理抖动而整轮超时。
    """
    from app import sdk
    try:
        r = sdk.verify_subscription(ck, device, proxies=None, full=False, since_ms=since_ms)
    except Exception as e:
        r = {"got": False, "items": [], "errors": [str(e)[:160]]}
    if (not (r or {}).get("got")) and (not (r or {}).get("auth_invalid")) and proxies and (r or {}).get("errors"):
        try:
            r2 = sdk.verify_subscription(ck, device, proxies=proxies, full=False, since_ms=since_ms)
            if (r2 or {}).get("got"):
                return r2
        except Exception:
            pass
    return r


def _decode(blob):
    if not blob:
        return None
    try:
        return json.loads(blob)
    except Exception:
        return None


def _record_success(code: str, account: str, res: dict, owner_id: int = 0, ip: str = "", ua: str = "") -> bool:
    """真到账：记领取次数 + 写 success 事件（带「延迟补记」）+ 刷新活跃会话。

    返回 True=真的补记了；False=该账号早已有成功记录（账号归一化后匹配），只收尾。
    """
    # ★R209：停用的码不再补记
    try:
        if (store.get(code) or {}).get("status") == "disabled":
            _log("补记跳过（该码已停用）%s" % code)
            return False
    except Exception:
        pass
    items = (res or {}).get("items") or []
    _gift_ends = [i.get("end") for i in items
                  if i.get("source") == "pc_membership_gift" and i.get("end")]
    ends = _gift_ends or [i.get("end") for i in items if i.get("end")]
    acct = ((res or {}).get("account") or "").strip() or (account or "")
    # 兜底闸门：入队用的账号串与核验返回的账号串可能不完全一致（同一账号的不同写法），
    # 只要「核验返回的账号」已经有成功记录，就绝不重复记 claim。
    # ★R703：把核验返回的 uid 一起带进来（同 uid 不同账号串不再重复补记）
    if _has_success(code, acct, str((res or {}).get("uid") or "")):
        _log("补记跳过（该账号已有成功记录）%s / %s" % (code, acct or "?"))
        return False
    limit = _code_limit(code)
    try:
        store.add_claim(code, limit)
    except Exception as e:
        _log("add_claim 失败 %s: %s" % (code, str(e)[:120]))
    note = ("30 天体验套餐已到账（延迟补记" + ("，到期 " + " / ".join(ends) if ends else "") + "）")
    try:
        store.log_event(code, "success", account=acct, ip=ip, ua=ua, note=note, owner_id=owner_id,
                        uid=str((res or {}).get("uid") or ""))
    except Exception as e:
        _log("写 success 事件失败 %s: %s" % (code, str(e)[:120]))
    _flip_session(code, acct, items)
    _log("补记成功 %s / %s%s" % (code, acct or "?", ("（到期 " + " / ".join(ends) + "）") if ends else ""))
    return True


def _flip_session(code: str, acct: str, items: list) -> None:
    """若该卡密的活跃会话还在（还停在 fail），把它翻成 success（页面立刻能看到）。"""
    try:
        from app import sessions as _sess
        s = _sess.SM.get(code, create=False)
        if s is None:
            return
        with s.lock:
            if s.status == "success":
                return
            # ★R213：补记结果属于旧账号时，不要翻到新登录的页面上
            _cur = (getattr(s, "account", "") or "").strip()
            _new = (acct or "").strip()
            if _cur and _new and _cur != _new:
                _log("补记账号与当前登录账号不一致，跳过翻页 %s / %s != %s" % (code, _new, _cur))
                return
            limit = int(getattr(s, "max_claims", 0) or 0)
            s.claims = int(getattr(s, "claims", 0) or 0) + 1
            if acct:
                s.account = acct
            s.status = "success"
            if limit > 0 and s.claims >= limit:
                s.claimed = True
            s.message = ("\U0001F389 领取成功！30 天体验套餐已到账（延迟到账，已自动补记；请在豆包电脑版/网页版查看会员权益）")
            s.detail = dict(getattr(s, "detail", None) or {})
            s.detail.update({"account": s.account, "items": items or [], "hint": ""})
    except Exception as e:
        _log("刷新会话失败 %s: %s" % (code, str(e)[:140]))


def scan_once(limit: int = SCAN_LIMIT) -> dict:
    """处理一批到期记录（可手动调用；测试直接调它）。"""
    out = {"due": 0, "checked": 0, "success": 0, "retry": 0, "given_up": 0,
           "skipped": 0, "errors": 0, "next": 0}
    try:
        _ensure_table()
    except Exception as e:
        out["error"] = str(e)[:160]
        return out

    now = float(_now())
    try:
        with _lock, _conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM pending_verify WHERE state='pending' AND next_at<=? "
                "ORDER BY next_at ASC LIMIT ?", (int(now), max(1, int(limit)))).fetchall()]
    except Exception as e:
        out["error"] = str(e)[:160]
        return out
    out["due"] = len(rows)

    delays = _delays()
    max_age = _max_age()

    for r in rows:
        code, account = r.get("code") or "", r.get("account") or ""
        # 1) 幂等闸门：已经有成功记录 -> 直接收尾，不重复记 claim
        if _has_success(code, account):
            _mark(code, account, "done", "已有成功记录，直接收尾")
            out["skipped"] += 1
            out["checked"] += 1
            continue
        # 2) 超窗放弃
        if now - float(r.get("created_at") or 0) > max_age:
            _mark(code, account, "given_up", "超过最长复查窗口（%.0fs）" % max_age)
            out["given_up"] += 1
            out["checked"] += 1
            continue
        # 3) 复查一次
        try:
            _dev = _decode(r.get("device_json")) or {}
            _since = float(_dev.get("_since_ms") or 0.0)
            if not _since:
                # 老队列没有登录基线：用入队时刻回推约 2.5 分钟（登录→8 轮核验→入队约 1 分钟）
                _since = max(0.0, float(r.get("created_at") or 0) * 1000.0 - 150000.0)
            res = _call_verify(_decode(r.get("ck_json")), _dev,
                               _decode(r.get("proxies_json")), since_ms=_since) or {}
        except Exception as e:
            # 网络/协议异常只记日志继续退避，绝不炸线程、也不消耗一次「尝试」
            tries = int(r.get("tries") or 0)
            nxt = now + delays[min(tries, len(delays) - 1)]
            _bump(r["id"], tries, nxt, now, note="复查异常：%s" % str(e)[:120])
            out["errors"] += 1
            out["checked"] += 1
            continue
        if (res or {}).get("got"):
            recorded = _record_success(code, account, res, owner_id=int(r.get("owner_id") or 0),
                                       ip=r.get("ip") or "", ua=r.get("ua") or "")
            if recorded:
                _mark(code, account, "done", "延迟补记成功")
                out["success"] += 1
            else:
                _mark(code, account, "done", "该账号已有成功记录（归一化后匹配）")
                out["skipped"] += 1
        else:
            if (res or {}).get("auth_invalid") or (res or {}).get("relogin_required"):
                _mark(code, account, "given_up", "登录态已失效（会话过期），需重新登录")
                out["given_up"] += 1
                out["checked"] += 1
                continue
            if (res or {}).get("gift_duplicate"):
                _mark(code, account, "given_up", "该账号此前已领取过（非本次新增）")
                out["given_up"] += 1
                out["checked"] += 1
                continue
            tries = int(r.get("tries") or 0) + 1
            if tries >= len(delays):
                _mark(code, account, "given_up", "退避序列已用尽（%d 次）" % len(delays))
                out["given_up"] += 1
            else:
                nxt = now + delays[tries]
                _bump(r["id"], tries, nxt, now, note="第 %d 次复查未到账" % tries)
                out["retry"] += 1
        out["checked"] += 1

    try:
        with _lock, _conn() as c:
            d = c.execute("SELECT COUNT(*) n FROM pending_verify WHERE state='pending' AND next_at<=?",
                          (int(now),)).fetchone()
            out["next"] = int(d["n"] or 0)
    except Exception:
        pass
    return out


def _bump(pid: int, tries: int, next_at: float, updated_at: float, note: str = "") -> None:
    try:
        with _lock, _conn() as c:
            c.execute("UPDATE pending_verify SET tries=?, next_at=?, updated_at=?, note=? WHERE id=?",
                      (int(tries), int(next_at), int(updated_at), (note or "")[:200], int(pid)))
    except Exception as e:
        _log("更新退避失败 id=%s: %s" % (pid, str(e)[:120]))


# --------------------------------------------------------------------------- #
# 对外接口 4/4：统计 / 运维辅助
# --------------------------------------------------------------------------- #
def stats() -> dict:
    out = {"pending": 0, "done": 0, "given_up": 0, "due": 0,
           "running": bool(_started and _thread is not None and _thread.is_alive()),
           "enabled": _enabled(), "interval": _interval,
           "delays": list(_delays()), "max_age": _max_age()}
    try:
        _ensure_table()
        with _lock, _conn() as c:
            for r in c.execute("SELECT state, COUNT(*) n FROM pending_verify GROUP BY state").fetchall():
                out[str(r["state"])] = int(r["n"] or 0)
            d = c.execute("SELECT COUNT(*) n FROM pending_verify WHERE state='pending' AND next_at<=?",
                          (int(_now()),)).fetchone()
            out["due"] = int(d["n"] or 0)
    except Exception as e:
        out["error"] = str(e)[:160]
    for k in ("pending", "done", "given_up"):
        out.setdefault(k, 0)
    return out


def pending_rows(code: str = "", state: str = "", limit: int = 200) -> list:
    """查看队列（测试与后台排查用）。"""
    try:
        _ensure_table()
        q, args = "SELECT * FROM pending_verify WHERE 1=1", []
        if code:
            q += " AND code=?"; args.append(code)
        if state:
            q += " AND state=?"; args.append(state)
        q += " ORDER BY next_at ASC, id ASC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with _lock, _conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]
    except Exception as e:
        _log("查队列失败: %s" % str(e)[:120])
        return []


def clear_all() -> None:
    """清空队列表（测试/运维用；不动 codes / events）。"""
    try:
        _ensure_table()
        with _lock, _conn() as c:
            c.execute("DELETE FROM pending_verify")
    except Exception as e:
        _log("清空队列失败: %s" % str(e)[:120])


def reset_state() -> None:
    """清空内存态（模拟进程重启；不动数据库行）。"""
    global _started, _thread, _table_ready, _max_age_override
    _stop.set()
    with _slock:
        _started = False
        _thread = None
    _max_age_override = None
