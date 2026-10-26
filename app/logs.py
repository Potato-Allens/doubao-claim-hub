# -*- coding: utf-8 -*-
"""日志滚动（agent D 交付）—— 严格实现 R63 契约第 8 节。

只做两件事：
  1) audit_log 只保留最新 N 条（默认 300，AUDIT_KEEP 可配）；
  2) 文本日志（uvicorn / 应用）裁剪到最新 N 行（原子写回）。
events（领取记录）**长期保留**，绝不删 —— 用户要在后台翻历史。

CLI：
  python -m app.logs            # 只读，打印统计 JSON
  python -m app.logs --apply    # 真正执行裁剪（audit + 文本日志）
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "codes.db"))
LOG_DIR = os.environ.get("LOG_DIR", os.path.join(os.path.dirname(DATA_DIR), "logs"))
LOG_FILE = os.environ.get("APP_LOG_FILE", os.path.join(LOG_DIR, "app.log"))
AUDIT_KEEP = int(os.environ.get("AUDIT_KEEP", "300"))

_lock = threading.RLock()


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _with_db(fn):
    """执行 fn(conn)；成功即提交（否则 DELETE 会被回滚）。"""
    c = None
    try:
        c = _conn()
        with c:
            return fn(c)
    finally:
        try:
            if c is not None:
                c.close()
        except Exception:
            pass


def _keep(keep=None) -> int:
    try:
        k = int(keep) if keep is not None else AUDIT_KEEP
    except Exception:
        k = 300
    return max(1, min(k, 100000))


def _human(e) -> str:
    s = str(e) or ""
    low = s.lower()
    if "unable to open database" in low:
        return "数据库文件不可用"
    if "no such table" in low:
        return "数据表不存在"
    if "locked" in low:
        return "数据库被占用，稍后重试"
    return s[:120]


# ---------------------------------------------------------------- audit 滚动
def trim_audit(keep=None) -> dict:
    """把 audit_log 裁到最新 keep 条。优先复用 agent A 的 app/audit.py，避免两份实现。"""
    k = _keep(keep)
    try:
        from app import audit as _audit  # type: ignore
    except Exception:
        _audit = None
    if _audit is not None:
        for name in ("trim", "trim_audit", "prune", "rotate"):
            fn = getattr(_audit, name, None)
            if callable(fn):
                try:
                    return _norm_trim(fn(k), k)
                except TypeError:
                    try:
                        return _norm_trim(fn(), k)
                    except Exception:
                        pass
                except Exception:
                    pass
    try:
        def _run(c):
            try:
                before = int(c.execute("SELECT COUNT(*) n FROM audit_log").fetchone()["n"])
            except sqlite3.OperationalError:
                return {"ok": True, "table": False, "removed": 0, "kept": 0, "keep": k}
            c.execute("DELETE FROM audit_log WHERE id NOT IN "
                      "(SELECT id FROM audit_log ORDER BY id DESC LIMIT ?)", (k,))
            after = int(c.execute("SELECT COUNT(*) n FROM audit_log").fetchone()["n"])
            return {"ok": True, "table": True, "removed": before - after, "kept": after, "keep": k}
        return _with_db(_run)
    except Exception as e:
        return {"ok": False, "error": _human(e), "removed": 0, "kept": 0, "keep": k}


def _norm_trim(r, k) -> dict:
    if isinstance(r, dict):
        return {"ok": r.get("ok", True), "kept": int(r.get("kept", r.get("total", 0)) or 0),
                "removed": int(r.get("removed", 0) or 0), "keep": k, "table": True}
    try:
        return {"ok": True, "kept": int(r or 0), "removed": 0, "keep": k, "table": True}
    except Exception:
        return {"ok": True, "kept": 0, "removed": 0, "keep": k, "table": True}


# ---------------------------------------------------------------- 文本日志滚动
def rotate_file_log(path=None, keep_lines=300) -> dict:
    p = path or LOG_FILE
    try:
        keep_lines = max(1, min(int(keep_lines), 1000000))
    except Exception:
        keep_lines = 300
    try:
        with _lock:
            if not os.path.exists(p):
                return {"ok": True, "path": p, "before": 0, "after": 0, "keep": keep_lines}
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            before = len(lines)
            if before <= keep_lines:
                return {"ok": True, "path": p, "before": before, "after": before, "keep": keep_lines}
            tail = lines[-keep_lines:]
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(tail) + "\n")
            os.replace(tmp, p)
        return {"ok": True, "path": p, "before": before, "after": len(tail), "keep": keep_lines}
    except Exception as e:
        return {"ok": False, "path": p, "error": _human(e), "before": 0, "after": 0, "keep": keep_lines}


def _count_lines(p) -> int:
    try:
        if not os.path.exists(p):
            return 0
        n = 0
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for _ in f:
                n += 1
        return n
    except Exception:
        return 0


def stats() -> dict:
    out = {"events": 0, "audit": 0, "file_lines": 0, "audit_keep": AUDIT_KEEP}
    try:
        def _run(c):
            for key, table in (("events", "events"), ("audit", "audit_log")):
                try:
                    out[key] = int(c.execute("SELECT COUNT(*) n FROM " + table).fetchone()["n"])
                except Exception:
                    out[key] = 0
        _with_db(_run)
    except Exception:
        pass
    out["file_lines"] = _count_lines(LOG_FILE)
    out["log_file"] = LOG_FILE
    return out


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    apply = ("--apply" in argv) or ("-a" in argv)
    result = {"apply": apply}
    if apply:
        result["audit"] = trim_audit()
        result["log"] = rotate_file_log()
    result["stats"] = stats()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
