# -*- coding: utf-8 -*-
"""R66 · 一次性下载链接
=========================================================
后台点「生成下载链接」-> 服务器把内容落成文件放到 DATA_DIR/dl/ -> 返回
``https://<host>/d/<token>``。客户点开就直接下载（不用来回传文件），
**默认下载一次后链接立刻销毁**（可配 2~N 次 / 有效期小时）。

安全设计
--------
* token = 32 字节 urlsafe 随机串：不可枚举、不可预测（抗爆破）。
* 文件只允许落在 DATA_DIR/dl/ 下（realpath 前缀校验），防目录穿越。
* 领取是**原子** UPDATE：`hits<max_hits` 且未过期才会命中，
  并发/多标签下只有一个人能拿到文件。
* 撤销 = 置 revoked 位（行保留，便于审计），不影响已下发的其他链接。
* 生成 / 领取 / 撤销 全部写 audit_log。
* 下载入口单独限速（每 IP 每分钟 90 次），命中失效链接不再回显任何细节。
"""
from __future__ import annotations

import os
import re
import secrets
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from app import store

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
DL_DIR = os.path.join(DATA_DIR, "dl")

TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
TOKEN_BYTES = int(os.environ.get("DL_TOKEN_BYTES", "32"))
MAX_HITS_CAP = int(os.environ.get("DL_MAX_HITS_CAP", "100000"))
DEFAULT_TTL_HOURS = int(os.environ.get("DL_TTL_HOURS", "24"))
DL_RATE = int(os.environ.get("DL_RATE", "90"))          # 每分钟 / 每 IP
KEEP_DAYS = int(os.environ.get("DL_KEEP_DAYS", "7"))    # 失效后保留几天再清文件

_MIME = {
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".zip": "application/zip",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
    ".apk": "application/vnd.android.package-archive",
    ".exe": "application/octet-stream",
}

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

STATUS_TEXT = {"active": "可下载", "used": "已下载", "expired": "已过期", "revoked": "已撤销"}

router = APIRouter()


# --------------------------------------------------------------------------- #
# 建表
# --------------------------------------------------------------------------- #
def init():
    os.makedirs(DL_DIR, exist_ok=True)
    with store._lock, store._conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS downloads(
            token      TEXT PRIMARY KEY,
            path       TEXT,
            name       TEXT,
            mime       TEXT,
            size       INTEGER DEFAULT 0,
            max_hits   INTEGER DEFAULT 1,
            hits       INTEGER DEFAULT 0,
            note       TEXT,
            owner_id   INTEGER DEFAULT 0,
            root_id    INTEGER DEFAULT 0,
            created_by TEXT DEFAULT '',
            created_at INTEGER,
            expires_at INTEGER DEFAULT 0,
            used_at    INTEGER DEFAULT 0,
            used_ip    TEXT,
            used_ua    TEXT,
            revoked    INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_dl_owner   ON downloads(owner_id);
        CREATE INDEX IF NOT EXISTS idx_dl_created ON downloads(created_at);
        """)
        for col in ("revoked INTEGER DEFAULT 0", "root_id INTEGER DEFAULT 0",
                    "created_by TEXT DEFAULT ''", "note TEXT"):
            try:
                c.execute("ALTER TABLE downloads ADD COLUMN " + col)
            except Exception:
                pass
    try:
        cleanup()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def safe_name(name: str, fallback: str = "download.txt") -> str:
    """把用户给的文件名洗成安全的（去分隔符/控制字符，防穿越）。"""
    n = _BAD.sub("_", str(name or "").strip())
    n = n.replace("..", "_").strip(". ")
    if not n:
        n = fallback
    return n[:120]


def human(n) -> str:
    try:
        n = float(n or 0)
    except Exception:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n


def mime_of(name: str) -> str:
    return _MIME.get(os.path.splitext(str(name or "").lower())[1], "application/octet-stream")


def inside_dl(path: str) -> bool:
    """只有 DATA_DIR/dl/ 里的文件才允许被下载。"""
    try:
        real = os.path.realpath(path)
        root = os.path.realpath(DL_DIR)
        return real == root or real.startswith(root + os.sep)
    except Exception:
        return False


def _new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)[:80]


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #
def make(content, name: str, mime: str = "", note: str = "", owner_id: int = 0,
         root_id: int = 0, created_by: str = "", ttl_hours=None, max_hits: int = 1,
         ext: str = "") -> dict:
    """落盘 + 登记，返回行（含 token）。ttl_hours<=0 = 永不过期。"""
    data = content if isinstance(content, (bytes, bytearray)) else str(content or "").encode("utf-8")
    data = bytes(data)
    nm = safe_name(name)
    if ext:
        e = str(ext)
        if not e.startswith("."):
            e = "." + e
        if not nm.lower().endswith(e.lower()):
            nm = os.path.splitext(nm)[0] + e
    try:
        mh = int(max_hits if max_hits is not None else 1)
    except Exception:
        mh = 1
    mh = max(1, min(mh, MAX_HITS_CAP))
    try:
        th = DEFAULT_TTL_HOURS if ttl_hours is None else int(ttl_hours)
    except Exception:
        th = DEFAULT_TTL_HOURS
    exp = 0 if th <= 0 else int(time.time()) + th * 3600

    os.makedirs(DL_DIR, exist_ok=True)
    for _ in range(6):
        tok = _new_token()
        path = os.path.join(DL_DIR, tok + (os.path.splitext(nm)[1] or ".txt"))
        try:
            with open(path, "xb") as f:
                f.write(data)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("生成下载文件失败（token 冲突）")

    now = int(time.time())
    with store._lock, store._conn() as c:
        c.execute("INSERT INTO downloads(token,path,name,mime,size,max_hits,hits,note,"
                  "owner_id,root_id,created_by,created_at,expires_at,revoked) "
                  "VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?,0)",
                  (tok, path, nm, str(mime or mime_of(nm)), len(data), mh,
                   str(note or "")[:200], int(owner_id or 0), int(root_id or 0),
                   str(created_by or "")[:64], now, exp))
    row = meta(tok)
    return row or {}


def make_batch_text(rows, fmt: str = "url", base: str = "") -> str:
    """把卡密行拼成可下载的文本内容。fmt: url | code | csv"""
    fmt = (fmt or "url").strip().lower()
    if fmt == "code":
        return "\n".join(str(r.get("code") or "") for r in (rows or []))
    if fmt == "csv":
        head = "code,url,status,created\n"
        body = "\n".join("%s,%s/%s,%s,%s" % (
            r.get("code") or "", base, r.get("code") or "", r.get("status") or "",
            r.get("created_at_str") or "") for r in (rows or []))
        return head + body
    return "\n".join("%s/%s" % (base, r.get("code") or "") for r in (rows or []))


# --------------------------------------------------------------------------- #
# 读取 / 领取 / 撤销
# --------------------------------------------------------------------------- #
def _out(r: dict) -> dict:
    now = int(time.time())
    r = dict(r)
    exp = int(r.get("expires_at") or 0)
    mh = int(r.get("max_hits") or 1)
    hits = int(r.get("hits") or 0)
    if int(r.get("revoked") or 0):
        st = "revoked"
    elif exp and exp <= now:
        st = "expired"
    elif hits >= mh:
        st = "used"
    else:
        st = "active"
    r["status"] = st
    r["status_text"] = STATUS_TEXT.get(st, st)
    r["size_str"] = human(r.get("size"))
    r["left_hits"] = max(0, mh - hits)
    r["once"] = (mh == 1)
    r["token_short"] = (str(r.get("token") or "")[:10] + "…") if r.get("token") else ""
    for k, tk in (("created_at", "created_str"), ("used_at", "used_str"), ("expires_at", "expires_str")):
        t = int(r.get(k) or 0)
        r[tk] = time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""
    r.pop("path", None)          # 绝不把服务器路径暴露给前端
    return r


def meta(token: str):
    tok = str(token or "").strip()
    if not tok or len(tok) > 200:
        return None
    with store._lock, store._conn() as c:
        r = c.execute("SELECT * FROM downloads WHERE token=?", (tok,)).fetchone()
    return _out(dict(r)) if r else None


def claim(token: str, ip: str = "", ua: str = "") -> dict:
    """原子领取：成功返回行（含真实 path），失败/抢不到返回 {}。"""
    tok = str(token or "").strip()
    if not tok or len(tok) > 200:
        return {}
    now = int(time.time())
    with store._lock, store._conn() as c:
        cur = c.execute(
            "UPDATE downloads SET hits=hits+1, used_at=?, used_ip=?, used_ua=? "
            "WHERE token=? AND revoked=0 AND hits<max_hits "
            "AND (expires_at=0 OR expires_at>?) AND path IS NOT NULL AND path<>''",
            (now, str(ip or "")[:64], str(ua or "")[:200], tok, now))
        if cur.rowcount != 1:
            return {}
        r = c.execute("SELECT * FROM downloads WHERE token=?", (tok,)).fetchone()
    return dict(r) if r else {}


def revoke(token: str, owner_ids=None) -> bool:
    tok = str(token or "").strip()
    if not tok:
        return False
    q, args = "SELECT token,revoked FROM downloads WHERE token=?", [tok]
    if owner_ids is not None:
        ids = sorted({int(i) for i in owner_ids if i})
        if not ids:
            return False
        q += " AND owner_id IN (%s)" % ",".join("?" * len(ids))
        args += ids
    with store._lock, store._conn() as c:
        r = c.execute(q, args).fetchone()
        if not r or int(r["revoked"] or 0):      # 不存在 / 已撤销 -> 幂等返回 False
            return False
        c.execute("UPDATE downloads SET revoked=1 WHERE token=?", (tok,))
    return True


def _where(owner_ids=None, status: str = "", q: str = ""):
    sql, args = " WHERE 1=1", []
    if owner_ids is not None:
        ids = sorted({int(i) for i in owner_ids if i})
        if ids:
            sql += " AND owner_id IN (%s)" % ",".join("?" * len(ids))
            args += ids
        else:
            sql += " AND 1=0"
    q = str(q or "").strip()
    if q:
        sql += " AND (token LIKE ? OR name LIKE ? OR note LIKE ?)"
        args += ["%" + q + "%"] * 3
    return sql, args


def listing(owner_ids=None, limit: int = 50, offset: int = 0, status: str = "", q: str = "") -> list:
    sql, args = _where(owner_ids, status, q)
    lim = max(1, min(int(limit or 50), 500))
    off = max(0, int(offset or 0))
    with store._lock, store._conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM downloads" + sql + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
            args + [lim, off]).fetchall()]
    out = [_out(r) for r in rows]
    st = (status or "").strip()
    if st:
        out = [r for r in out if r["status"] == st]
    return out


def count(owner_ids=None, status: str = "", q: str = "") -> int:
    sql, args = _where(owner_ids, "", q)
    with store._lock, store._conn() as c:
        rows = [_out(dict(r)) for r in c.execute("SELECT * FROM downloads" + sql, args).fetchall()]
    st = (status or "").strip()
    if st:
        return len([r for r in rows if r["status"] == st])
    return len(rows)


def stats(owner_ids=None) -> dict:
    sql, args = _where(owner_ids, "", "")
    with store._lock, store._conn() as c:
        rows = [_out(dict(r)) for r in c.execute("SELECT * FROM downloads" + sql, args).fetchall()]
    d = {"total": len(rows), "active": 0, "used": 0, "expired": 0, "revoked": 0, "hits": 0}
    for r in rows:
        d[r["status"]] = d.get(r["status"], 0) + 1
        d["hits"] += int(r.get("hits") or 0)
    return d


def total_bytes(owner_ids=None) -> int:
    """★R211：某体系内下载文件占用总字节数（防单账号写满磁盘）。"""
    sql, args = _where(owner_ids, "", "")
    with store._lock, store._conn() as c:
        r = c.execute("SELECT COALESCE(SUM(size),0) s FROM downloads" + sql, args).fetchone()
    return int((r["s"] if r else 0) or 0)


def cleanup(days: int = None):
    """清掉「已失效」且超过保留期的文件（含磁盘文件）。"""
    days = KEEP_DAYS if days is None else int(days)
    cut = int(time.time()) - max(0, days) * 86400
    removed = 0
    with store._lock, store._conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM downloads").fetchall()]
    for r in rows:
        st = _out(r)["status"]
        if st == "active":
            continue
        # 失效时间点：已下载=最后一次下载时间；已过期=过期时间；已撤销=生成时间（撤销即作废）
        if st == "used":
            ts = int(r.get("used_at") or r.get("created_at") or 0)
        elif st == "expired":
            ts = int(r.get("expires_at") or 0)
        else:
            ts = int(r.get("created_at") or 0)
        if ts and ts > cut:
            continue
        p = str(r.get("path") or "")
        try:
            if p and inside_dl(p) and os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass
        try:
            with store._lock, store._conn() as c:
                c.execute("DELETE FROM downloads WHERE token=?", (r.get("token"),))
            removed += 1
        except Exception:
            pass
    return removed


# --------------------------------------------------------------------------- #
# 下载入口限速（模块内自带，避免和 main.py 循环 import）
# --------------------------------------------------------------------------- #
_rl: dict = {}
_rl_lock = threading.Lock()


def _rate_ok(key: str, limit: int, per_sec: int) -> bool:
    now = time.time()
    with _rl_lock:
        if len(_rl) > 5000:
            _rl.clear()
        arr = [t for t in (_rl.get(key) or []) if now - t < per_sec]
        if len(arr) >= limit:
            _rl[key] = arr
            return False
        arr.append(now)
        _rl[key] = arr
        return True


def _client_ip(request: Request) -> str:
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for") or ""
        if xff:
            return xff.split(",")[0].strip()
        xr = request.headers.get("x-real-ip")
        if xr:
            return xr.strip()
    return (request.client.host if request.client else "") or "?"


def _audit(action, target="", detail="", ip="", level="info"):
    try:
        from app import audit as _a
        _a.log(action, actor_name="下载链接", actor_type="system", target=target,
               detail=detail, ip=ip, level=level)
    except Exception:
        pass


def _page(title: str, icon: str, desc: str, status_code: int = 200):
    html = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex,nofollow\">"
            "<title>%s</title><link rel=\"stylesheet\" href=\"/style.css\"></head><body>"
            "<div class=\"aurora\"></div>"
            "<div style=\"min-height:78vh;display:grid;place-items:center;width:100%%\">"
            "<div class=\"card\" style=\"max-width:430px;width:100%%;text-align:center;padding:34px 26px\">"
            "<div style=\"font-size:52px;line-height:1\">%s</div>"
            "<h2 style=\"margin:14px 0 8px;font-size:19px\">%s</h2>"
            "<p class=\"dim\" style=\"margin:0;line-height:1.8\">%s</p>"
            "</div></div></body></html>") % (title, icon, title, desc)
    return HTMLResponse(html, status_code=status_code,
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


@router.get("/d/{token}")
def dl_get(token: str, request: Request):
    ip = _client_ip(request)
    if not _rate_ok("dl:" + ip, DL_RATE, 60):
        return _page("请求过于频繁", "⏳", "请稍等一会儿再试。", 429)
    m = meta(token)
    if not m:
        return _page("链接无效", "🙈", "这个下载链接不存在，或已经被使用过了。", 404)
    st = m.get("status")
    if st == "revoked":
        return _page("链接已撤销", "🚫", "该下载链接已被发送者撤销，请联系他重新生成。", 410)
    if st == "expired":
        return _page("链接已过期", "⌛", "该下载链接已超过有效期，请联系发链接给你的人重新生成。", 410)
    if st == "used":
        return _page("链接已失效", "🔥", "这个链接只能下载一次，已经被使用过了。", 410)

    got = claim(token, ip=ip, ua=(request.headers.get("user-agent") or ""))
    if not got:
        return _page("链接已失效", "🔥", "这个链接只能下载一次，已经被使用过了。", 410)
    path = str(got.get("path") or "")
    if not inside_dl(path) or not os.path.isfile(path):
        _audit("dl_missing", target=m.get("token_short", ""), detail="文件缺失", ip=ip, level="warn")
        return _page("文件已被清理", "🫥", "文件已被清理，请联系发链接给你的人重新生成。", 410)

    name = safe_name(got.get("name") or "download.txt")
    mime = str(got.get("mime") or "") or mime_of(name)
    _audit("dl_fetch", target=name,
           detail="下载链接被使用 %s/%s" % (int(got.get("hits") or 0), int(got.get("max_hits") or 1)),
           ip=ip)
    return FileResponse(
        path, media_type=mime, filename=name,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                 "X-Robots-Tag": "noindex"})