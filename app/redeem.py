# -*- coding: utf-8 -*-
"""R213 卡密链接兑换站
==========================================================
后台：/admin/redeem
  · 输入「每组卡密链接数量」和「兑换码数量」→ 批量生成卡密链接 + 兑换码
  · 一个兑换码 = 一组卡密链接
前台：/redeem
  · 输入兑换码 → 展示该组卡密链接 + 一键下载 txt
  · 结果持久化在浏览器 localStorage，刷新不丢；点「清除当前页面数据」才会清空

安全设计
  · 兑换码 20 位（32 字母表 ≈ 100 bit 熵），服务端用 HMAC-SHA256 索引，命中不依赖明文比较
  · 严格字符集/长度校验 + JSON 体积上限 + 全参数化 SQL，杜绝注入
  · 按 IP 滑动窗口限速 + 单码失败锁定 + 全局熔断；错误一律返回同一句话，防枚举
  · 兑换成功后绑定设备 cookie（HttpOnly），同设备刷新可再次读取，换设备不可复用
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

router = APIRouter()

# --------------------------------------------------------------------------- #
# 模块状态（install 时注入，避免与 main.py 循环导入）
# --------------------------------------------------------------------------- #
_CFG = {
    "db_path": "",
    "secret": b"",
    "admin_ok": None,
    "client_ip": None,
    "base_url": None,
    "log": None,
    "static_dir": "",
    "gen_claim_codes": None,
}

_LOCK = threading.RLock()
_RL: dict = {}
_RL_LOCK = threading.Lock()

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 32 字符，去掉 I/O/0/1
_CODE_LEN = 20
_GROUP = 5
_RE_CODE_NORM = re.compile(r"^[A-HJ-NP-Z2-9]{20}$")
_MAX_BODY = 4096

MAX_PER_GROUP = int(os.environ.get("REDEEM_MAX_PER_GROUP", "500"))
MAX_CODES = int(os.environ.get("REDEEM_MAX_CODES", "500"))
MAX_TOTAL = int(os.environ.get("REDEEM_MAX_TOTAL", "5000"))
RL_IP_LIMIT = int(os.environ.get("REDEEM_RL_IP_LIMIT", "15"))       # 单 IP / 窗口内最多尝试次数
RL_IP_WINDOW = int(os.environ.get("REDEEM_RL_IP_WINDOW", "600"))    # 窗口秒数
RL_ALL_LIMIT = int(os.environ.get("REDEEM_RL_ALL_LIMIT", "240"))    # 全站 / 分钟


def _log(msg: str) -> None:
    fn = _CFG.get("log")
    if fn:
        try:
            fn("[R213] " + msg)
            return
        except Exception:
            pass


def install(app, *, db_path: str, secret: str, admin_ok: Callable, client_ip: Callable,
            base_url: Callable, log: Optional[Callable] = None, static_dir: str = "",
            gen_claim_codes: Optional[Callable] = None) -> None:
    """挂载兑换站。所有外部依赖都以参数注入，主站只需两行接入。"""
    _CFG.update({
        "db_path": db_path,
        "secret": (secret or "redeem-fallback").encode("utf-8"),
        "admin_ok": admin_ok,
        "client_ip": client_ip,
        "base_url": base_url,
        "log": log,
        "static_dir": static_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
        "gen_claim_codes": gen_claim_codes or _default_gen_claim_codes,
    })
    init_db()
    app.include_router(router)


def _default_gen_claim_codes(count: int, note: str):
    from app import store
    return store.generate(count, note=note)


# --------------------------------------------------------------------------- #
# DB
# --------------------------------------------------------------------------- #
def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_CFG["db_path"]), exist_ok=True)
    c = sqlite3.connect(_CFG["db_path"], timeout=20, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=20000")
    return c


@contextlib.contextmanager
def _db():
    c = _conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db() -> None:
    with _LOCK, _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS redeem_batches(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_no    TEXT UNIQUE,
            note        TEXT DEFAULT '',
            per_group   INTEGER DEFAULT 1,
            code_count  INTEGER DEFAULT 1,
            link_count  INTEGER DEFAULT 0,
            state       TEXT DEFAULT 'active',
            created_at  INTEGER,
            created_by  TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS redeem_groups(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id    INTEGER NOT NULL,
            group_no    INTEGER NOT NULL,
            created_at  INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_rg_batch ON redeem_groups(batch_id);
        CREATE TABLE IF NOT EXISTS redeem_links(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id    INTEGER NOT NULL,
            group_id    INTEGER NOT NULL,
            seq         INTEGER NOT NULL,
            claim_code  TEXT NOT NULL,
            url         TEXT NOT NULL,
            created_at  INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_rl_group ON redeem_links(group_id);
        CREATE INDEX IF NOT EXISTS idx_rl_batch ON redeem_links(batch_id);
        CREATE TABLE IF NOT EXISTS redeem_codes(
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id     INTEGER NOT NULL,
            group_id     INTEGER NOT NULL,
            code_hash    TEXT NOT NULL UNIQUE,
            code_plain   TEXT NOT NULL,
            code_mask    TEXT DEFAULT '',
            state        TEXT DEFAULT 'unused',      -- unused | used | disabled
            tries        INTEGER DEFAULT 0,
            lock_until   INTEGER DEFAULT 0,
            created_at   INTEGER,
            used_at      INTEGER DEFAULT 0,
            used_ip      TEXT DEFAULT '',
            used_ua      TEXT DEFAULT '',
            used_device  TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_rc_batch ON redeem_codes(batch_id);
        CREATE INDEX IF NOT EXISTS idx_rc_state ON redeem_codes(state);
        CREATE TABLE IF NOT EXISTS redeem_attempts(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ip          TEXT DEFAULT '',
            ua          TEXT DEFAULT '',
            code_hash   TEXT DEFAULT '',
            ok          INTEGER DEFAULT 0,
            reason      TEXT DEFAULT '',
            created_at  INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_ra_created ON redeem_attempts(created_at);
        """)


# --------------------------------------------------------------------------- #
# 兑换码工具
# --------------------------------------------------------------------------- #
def normalize_code(raw) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(raw or "")).upper()


def valid_code(norm: str) -> bool:
    return bool(norm) and len(norm) == _CODE_LEN and _RE_CODE_NORM.match(norm) is not None


def extract_code(raw) -> str:
    """★R705：从「整条链接 / 带说明的文字」里抠出兑换码。

    线上真实案例：买家把 http://203.0.113.10:8080/... 粘进输入框，
    旧前端把它当成兑换码（截断成 HTTP4-52072-14318-0），一直报「请输入完整的 20 位兑换码」。
    这里先找 /redeem/<code> 段，再退回全文归一化，不改变严格校验。
    """
    text = str(raw or "")
    m = re.search(r"/redeem/([A-Za-z0-9][A-Za-z0-9-]{15,40})", text, re.I)
    if m:
        n = normalize_code(m.group(1))
        if valid_code(n):
            return n
    return normalize_code(text)


def new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))
    return "-".join(raw[i:i + _GROUP] for i in range(0, _CODE_LEN, _GROUP))


def code_hash(norm: str) -> str:
    return hmac.new(_CFG["secret"], ("redeem:" + norm).encode("ascii"), hashlib.sha256).hexdigest()


def mask_code(code: str) -> str:
    n = normalize_code(code)
    if len(n) < 12:
        return "****"
    return "%s-*****-%s" % (n[:5], n[-5:])


def _rate_limit(key: str, limit: int, window: int) -> bool:
    now = time.time()
    with _RL_LOCK:
        arr = [t for t in (_RL.get(key) or []) if now - t < window]
        if len(arr) >= limit:
            _RL[key] = arr
            return False
        arr.append(now)
        _RL[key] = arr
        if len(_RL) > 20000:
            for k in [k for k, v in _RL.items() if not v or now - v[-1] > 3600]:
                _RL.pop(k, None)
        return True


def _gen_batch_no() -> str:
    return time.strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)


async def _read_json(request: Request, limit: int = _MAX_BODY) -> dict:
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > limit:
                raise HTTPException(status_code=413, detail="body too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="bad content-length")
    try:
        raw = await request.body()
    except Exception:
        return {}                 # ★R694：客户端中途断开 → 当空 body，不抛 500
    if len(raw) > limit:
        raise HTTPException(status_code=413, detail="body too large")
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="bad json")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="bad json")
    return data


def _admin(request: Request) -> None:
    fn = _CFG.get("admin_ok")
    if not fn or not fn(request):
        raise HTTPException(status_code=403, detail="unauthorized")


def _ip(request: Request) -> str:
    fn = _CFG.get("client_ip")
    try:
        return (fn(request) if fn else "") or "?"
    except Exception:
        return "?"


def _base(request: Request) -> str:
    fn = _CFG.get("base_url")
    try:
        return (fn(request) if fn else "").rstrip("/")
    except Exception:
        return ""


def _static(name: str, media: str):
    p = os.path.join(_CFG["static_dir"], name)
    try:
        with open(p, encoding="utf-8") as f:
            data = f.read()
    except Exception:
        raise HTTPException(status_code=404)
    ver = "0"
    try:
        st = os.stat(p)
        ver = "%x%x" % (int(st.st_mtime), int(st.st_size) & 0xFFFFF)
    except Exception:
        pass
    return HTMLResponse(data, media_type=media,
                        headers={"Cache-Control": "no-cache",
                                 "X-Asset-Version": ver})


# --------------------------------------------------------------------------- #
# 后台 API
# --------------------------------------------------------------------------- #
@router.get("/admin/redeem", response_class=HTMLResponse)
def admin_redeem_page(request: Request):
    if not (_CFG.get("admin_ok") and _CFG["admin_ok"](request)):
        return RedirectResponse("/admin/login", status_code=302)
    with open(os.path.join(_CFG["static_dir"], "admin_redeem.html"), encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/admin/api/redeem/meta")
def admin_redeem_meta(request: Request):
    _admin(request)
    return {"ok": True, "max_per_group": MAX_PER_GROUP, "max_codes": MAX_CODES,
            "max_total": MAX_TOTAL, "code_len": _CODE_LEN}


@router.post("/admin/api/redeem/create")
async def admin_redeem_create(request: Request):
    _admin(request)
    body = await _read_json(request)
    base = _base(request)
    now = int(time.time())
    # R216: 生成是同步重活（几千次 INSERT），必须丢线程池，否则会堵死事件循环，
    #       让出码/轮询/兑换一起卡住。
    return await run_in_threadpool(_create_batch_sync, body, base, now)


def _create_batch_sync(body, base, now):
    mode = str(body.get("mode") or "per_group").strip()
    note = str(body.get("note") or "").strip()[:120]

    def _int(v, name):
        try:
            n = int(v)
        except Exception:
            raise HTTPException(status_code=400, detail="%s 必须是整数" % name)
        return n

    a = _int(body.get("per_group"), "卡密链接数量")
    b = _int(body.get("code_count"), "兑换码数量")
    if b < 1 or b > MAX_CODES:
        raise HTTPException(status_code=400, detail="兑换码数量需在 1~%d" % MAX_CODES)
    if mode == "total":
        if a < b or a % b != 0:
            raise HTTPException(status_code=400, detail="总链接数必须能被兑换码数量整除")
        per_group = a // b
    else:
        per_group = a
    if per_group < 1 or per_group > MAX_PER_GROUP:
        raise HTTPException(status_code=400, detail="每组链接数需在 1~%d" % MAX_PER_GROUP)
    total = per_group * b
    if total > MAX_TOTAL:
        raise HTTPException(status_code=400, detail="单批总链接数不能超过 %d" % MAX_TOTAL)

    batch_no = _gen_batch_no()
    gen = _CFG.get("gen_claim_codes") or _default_gen_claim_codes
    claim_codes = list(gen(total, "兑换站:%s%s" % (note, (" | " + batch_no) if batch_no else "")) or [])
    if len(claim_codes) != total:
        raise HTTPException(status_code=500, detail="卡密链接生成数量不符")

    out_groups = []
    with _LOCK, _db() as c:
        cur = c.execute("INSERT INTO redeem_batches(batch_no,note,per_group,code_count,link_count,state,created_at,created_by)"
                        " VALUES(?,?,?,?,?,'active',?,?)",
                        (batch_no, note, per_group, b, total, now, ""))
        batch_id = int(cur.lastrowid)
        k = 0
        for gno in range(1, b + 1):
            gid = int(c.execute("INSERT INTO redeem_groups(batch_id,group_no,created_at) VALUES(?,?,?)",
                                (batch_id, gno, now)).lastrowid)
            links = []
            for seq in range(1, per_group + 1):
                claim = claim_codes[k]
                k += 1
                url = "%s/%s" % (base, claim) if base else "/%s" % claim
                c.execute("INSERT INTO redeem_links(batch_id,group_id,seq,claim_code,url,created_at)"
                          " VALUES(?,?,?,?,?,?)", (batch_id, gid, seq, claim, url, now))
                links.append(url)
            code = new_code()
            h = code_hash(normalize_code(code))
            c.execute("INSERT INTO redeem_codes(batch_id,group_id,code_hash,code_plain,code_mask,state,created_at)"
                      " VALUES(?,?,?,?,?,'unused',?)", (batch_id, gid, h, code, mask_code(code), now))
            out_groups.append({"group_no": gno, "code": code})

    _log("创建兑换批次 %s：%d 个兑换码 × 每组 %d 条 = %d 条链接" % (batch_no, b, per_group, total))
    return {"ok": True, "batch_no": batch_no, "batch_id": batch_id, "per_group": per_group,
            "code_count": b, "link_count": total, "groups": out_groups}


@router.get("/admin/api/redeem/list")
def admin_redeem_list(request: Request, limit: int = 30, offset: int = 0):
    _admin(request)
    lim = max(1, min(int(limit), 200))
    off = max(0, int(offset))
    with _LOCK, _db() as c:
        rows = c.execute("""
            SELECT b.*,
                   (SELECT COUNT(*) FROM redeem_codes rc WHERE rc.batch_id=b.id) AS codes,
                   (SELECT COUNT(*) FROM redeem_codes rc WHERE rc.batch_id=b.id AND rc.state='used') AS used,
                   (SELECT COUNT(*) FROM redeem_codes rc WHERE rc.batch_id=b.id AND rc.state='disabled') AS disabled
            FROM redeem_batches b ORDER BY b.id DESC LIMIT ? OFFSET ?""", (lim, off)).fetchall()
        total = c.execute("SELECT COUNT(*) FROM redeem_batches").fetchone()[0]
        stats = c.execute(
            "SELECT (SELECT COUNT(*) FROM redeem_batches) AS batches,"
            " (SELECT COUNT(*) FROM redeem_codes) AS codes,"
            " (SELECT COUNT(*) FROM redeem_codes WHERE state='used') AS used").fetchone()
    items = []
    for r in rows:
        d = dict(r)
        d["created"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.get("created_at") or 0))
        items.append(d)
    return {"ok": True, "total": total, "items": items,
            "stats": {"batches": stats["batches"], "codes": stats["codes"],
                      "used": stats["used"],
                      "left": max(0, stats["codes"] - stats["used"])}}


@router.get("/admin/api/redeem/batch/{batch_id}")
def admin_redeem_batch(request: Request, batch_id: int, page: int = 1, size: int = 20, q: str = ""):
    # R216: 详情抽屉分页，避免一次拉整批（500 码 x 500 链接）把浏览器卡死
    _admin(request)
    size = max(5, min(int(size), 100))
    page = max(1, int(page))
    q = (q or "").strip().upper()
    with _LOCK, _db() as c:
        b = c.execute("SELECT * FROM redeem_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            raise HTTPException(status_code=404, detail="batch not found")
        where, args = " WHERE rc.batch_id=?", [batch_id]
        if q:
            where += " AND (rc.code_plain LIKE ? OR CAST(rg.group_no AS TEXT)=?)"
            args += ["%" + q + "%", q]
        total = c.execute("SELECT COUNT(*) FROM redeem_codes rc JOIN redeem_groups rg"
                          " ON rg.id=rc.group_id" + where, args).fetchone()[0]
        rows = c.execute("""SELECT rc.id, rc.code_plain, rc.code_mask, rc.state, rc.used_at,
                                   rc.used_ip, rg.group_no, rg.id AS gid
                            FROM redeem_codes rc JOIN redeem_groups rg ON rg.id=rc.group_id"""
                         + where + " ORDER BY rg.group_no LIMIT ? OFFSET ?",
                         args + [size, (page - 1) * size]).fetchall()
        gids = [r["gid"] for r in rows]
        by_group = {}
        if gids:
            ph = ",".join("?" * len(gids))
            for r in c.execute("SELECT group_id,url FROM redeem_links WHERE group_id IN (%s)"
                               " ORDER BY seq" % ph, gids):
                by_group.setdefault(r["group_id"], []).append(r["url"])
    items = []
    for r in rows:
        d = dict(r)
        d["used"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["used_at"])) if d.get("used_at") else ""
        d["links"] = by_group.get(r["gid"], [])
        items.append(d)
    pages = max(1, (total + size - 1) // size)
    return {"ok": True, "batch": dict(b), "items": items, "page": page,
            "size": size, "total": total, "pages": pages}


@router.post("/admin/api/redeem/disable")
async def admin_redeem_disable(request: Request):
    _admin(request)
    body = await _read_json(request)
    batch_id = body.get("batch_id")
    code_id = body.get("code_id")
    with _LOCK, _db() as c:
        if batch_id:
            bid = int(batch_id)
            c.execute("UPDATE redeem_codes SET state='disabled' WHERE batch_id=? AND state='unused'", (bid,))
            c.execute("UPDATE redeem_batches SET state='disabled' WHERE id=?", (bid,))
            return {"ok": True, "message": "该批次未使用的兑换码已全部停用"}
        if code_id:
            cid = int(code_id)
            c.execute("UPDATE redeem_codes SET state='disabled' WHERE id=? AND state='unused'", (cid,))
            return {"ok": True, "message": "已停用"}
    raise HTTPException(status_code=400, detail="缺少参数")


@router.get("/admin/api/redeem/export/{batch_id}", response_class=PlainTextResponse)
def admin_redeem_export(request: Request, batch_id: int):
    _admin(request)
    with _LOCK, _db() as c:
        b = c.execute("SELECT * FROM redeem_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            raise HTTPException(status_code=404, detail="batch not found")
        rows = c.execute("""
            SELECT rc.code_plain, rc.state, rg.group_no
            FROM redeem_codes rc JOIN redeem_groups rg ON rg.id=rc.group_id
            WHERE rc.batch_id=? ORDER BY rg.group_no""", (batch_id,)).fetchall()
        links = c.execute("""
            SELECT rl.url, rg.group_no FROM redeem_links rl JOIN redeem_groups rg ON rg.id=rl.group_id
            WHERE rl.batch_id=? ORDER BY rg.group_no, rl.seq""", (batch_id,)).fetchall()
    by_group = {}
    for r in links:
        by_group.setdefault(r["group_no"], []).append(r["url"])
    lines = ["# 兑换批次 %s  每组 %s 条  共 %s 个兑换码  创建 %s" %
             (b["batch_no"], b["per_group"], b["code_count"],
              time.strftime("%Y-%m-%d %H:%M", time.localtime(b["created_at"] or 0))), ""]
    for r in rows:
        lines.append("[组 %02d] 兑换码: %s  状态: %s" % (r["group_no"], r["code_plain"], r["state"]))
        for u in by_group.get(r["group_no"], []):
            lines.append("    " + u)
        lines.append("")
    txt = "\n".join(lines)
    return PlainTextResponse(txt, headers={
        "Content-Disposition": 'attachment; filename="redeem_%s.txt"' % b["batch_no"],
        "Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# 前台兑换
# --------------------------------------------------------------------------- #
@router.get("/redeem", response_class=HTMLResponse)
@router.get("/redeem/", response_class=HTMLResponse)
def redeem_page():
    with open(os.path.join(_CFG["static_dir"], "redeem.html"), encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/redeem.css")
def redeem_css():
    return _static("redeem.css", "text/css")


@router.get("/redeem.js")
def redeem_js():
    return _static("redeem.js", "application/javascript")


@router.get("/admin_redeem.css")
def admin_redeem_css():
    return _static("admin_redeem.css", "text/css")


@router.get("/admin_redeem.js")
def admin_redeem_js():
    return _static("admin_redeem.js", "application/javascript")


@router.get("/admin_redeem_panel.js")
def admin_redeem_panel_js():
    return _static("admin_redeem_panel.js", "application/javascript")


def _device_of(request: Request) -> str:
    d = request.cookies.get("rdv") or ""
    return d if re.match(r"^[0-9a-f]{32}$", d) else ""


def _payload(c, row) -> dict:
    links = [r["url"] for r in c.execute(
        "SELECT url FROM redeem_links WHERE group_id=? ORDER BY seq", (row["group_id"],)).fetchall()]
    g = c.execute("SELECT group_no, batch_id FROM redeem_groups WHERE id=?", (row["group_id"],)).fetchone()
    b = c.execute("SELECT batch_no FROM redeem_batches WHERE id=?", (g["batch_id"],)).fetchone() if g else None
    return {"ok": True,
            "code_tail": (row["code_plain"] or "")[-6:],
            "group_no": g["group_no"] if g else 0,
            "batch_no": b["batch_no"] if b else "",
            "count": len(links),
            "links": links,
            "redeemed_at": int(row["used_at"] or time.time())}


def _fail(c, ip, ua, h, reason) -> None:
    try:
        c.execute("INSERT INTO redeem_attempts(ip,ua,code_hash,ok,reason,created_at) VALUES(?,?,?,0,?,?)",
                  (ip[:64], ua[:200], h[:128], reason[:40], int(time.time())))
    except Exception:
        pass


@router.post("/api/redeem/claim")
async def api_redeem_claim(request: Request):
    ip = _ip(request)
    ua = (request.headers.get("user-agent") or "")[:200]

    # 限速：单 IP 10 分钟 15 次；全站 60 秒 240 次（熔断暴力扫描）
    if not _rate_limit("rd:ip:" + ip, RL_IP_LIMIT, RL_IP_WINDOW) or not _rate_limit("rd:all", RL_ALL_LIMIT, 60):
        return JSONResponse({"ok": False, "message": "尝试过于频繁，请稍后再试"}, status_code=429,
                            headers={"Cache-Control": "no-store"})

    body = await _read_json(request)
    norm = extract_code(body.get("code"))
    if not valid_code(norm):
        _log("兑换码格式不对 ip=%s len=%d mask=%s" % (ip, len(norm), mask_code(norm)))
        return JSONResponse({"ok": False, "message": "兑换码格式不正确：本站兑换码是 20 位（5-5-5-5），不含 I/O/0/1"},
                            status_code=400, headers={"Cache-Control": "no-store"})

    device = _device_of(request)
    if not device:
        device = secrets.token_hex(16)

    h = code_hash(norm)
    now = int(time.time())
    with _LOCK, _db() as c:
        row = c.execute("SELECT * FROM redeem_codes WHERE code_hash=?", (h,)).fetchone()
        if not row:
            _fail(c, ip, ua, h, "notfound")
            _log("兑换码未命中 ip=%s mask=%s" % (ip, mask_code(norm)))
            return JSONResponse({"ok": False, "message": "兑换码不存在或已被使用，请向发码方确认"},
                                status_code=404, headers={"Cache-Control": "no-store"})
        if row["state"] == "disabled":
            _fail(c, ip, ua, h, "disabled")
            return JSONResponse({"ok": False, "message": "兑换码无效或已被使用"},
                                status_code=404, headers={"Cache-Control": "no-store"})
        if row["state"] == "used":
            if row["used_device"] and hmac.compare_digest(str(row["used_device"]), device):
                payload = _payload(c, row)
                payload["restored"] = True
                resp = JSONResponse(payload, headers={"Cache-Control": "no-store"})
                resp.set_cookie("rdv", device, max_age=365 * 86400, httponly=True,
                                samesite="lax", secure=request.url.scheme == "https", path="/")
                return resp
            _fail(c, ip, ua, h, "used")
            return JSONResponse({"ok": False, "message": "兑换码无效或已被使用"},
                                status_code=404, headers={"Cache-Control": "no-store"})
        if int(row["lock_until"] or 0) > now:
            return JSONResponse({"ok": False, "message": "尝试过于频繁，请稍后再试"},
                                status_code=429, headers={"Cache-Control": "no-store"})

        cur = c.execute("UPDATE redeem_codes SET state='used', used_at=?, used_ip=?, used_ua=?, used_device=?"
                        " WHERE id=? AND state='unused'",
                        (now, ip[:64], ua, device, row["id"]))
        if cur.rowcount != 1:
            return JSONResponse({"ok": False, "message": "兑换码无效或已被使用"},
                                status_code=404, headers={"Cache-Control": "no-store"})
        row = c.execute("SELECT * FROM redeem_codes WHERE id=?", (row["id"],)).fetchone()
        payload = _payload(c, row)
        c.execute("INSERT INTO redeem_attempts(ip,ua,code_hash,ok,reason,created_at) VALUES(?,?,?,1,'ok',?)",
                  (ip[:64], ua, h[:128], now))

    _log("兑换成功 ip=%s group=%s count=%s" % (ip, payload.get("group_no"), payload.get("count")))
    resp = JSONResponse(payload, headers={"Cache-Control": "no-store"})
    resp.set_cookie("rdv", device, max_age=365 * 86400, httponly=True,
                    samesite="lax", secure=request.url.scheme == "https", path="/")
    return resp


@router.post("/api/redeem/reset")
def api_redeem_reset(request: Request):
    resp = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    resp.delete_cookie("rdv", path="/")
    return resp