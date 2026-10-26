# -*- coding: utf-8 -*-
"""设备（"PC 电脑"身份）来源：
  - random : 纯本地随机生成 device_id + 指纹（默认，服务器可用，零依赖）
  - pool   : 从 data/devices.json 读取**预注册好的真实设备码**（由 Windows 端工具导出）

同时提供设备池的**管理接口**（供后台导入 / 导出 / 热加载）：
  import_items() / export_items() / pool_stats() / clear_pool() / reload()

向后兼容：DeviceSource 的 take() / remaining() / pool_total 行为不变。
"""
from __future__ import annotations

import json
import os
import random
import secrets
import threading
import uuid

_DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
_POOL_FILE = os.path.join(_DATA_DIR, "devices.json")

_MODELS = ["PRIME Z690-P WIFI D4", "B550 AORUS ELITE", "MAG B660M MORTAR WIFI DDR4",
           "TUF GAMING B550M-PLUS", "Z790 AORUS ELITE AX", "ROG STRIX B760-F GAMING WIFI",
           "PRO B660M-A DDR4", "X670E AORUS MASTER", "B760M DS3H DDR4", "Z590-A PRO"]
_PREFIX = ["WD-WX", "S3Z", "S5G", "MZ", "Z6", "CT"]

_io_lock = threading.RLock()
_INSTANCES = []          # 运行中的 DeviceSource（供 reload() 热加载）


def _rand_did() -> str:
    """真实 device_id 是 15~16 位十进制整数（首位非 0）。★R211：CSPRNG，避免设备号可预测/可串联。"""
    n = 15 + secrets.randbelow(2)
    return str(1 + secrets.randbelow(9)) + "".join(secrets.choice("0123456789") for _ in range(n - 1))


def random_device() -> dict:
    did = _rand_did()
    return {
        "did": did,
        # 官方规律：install_id = device_id + 4096（多个真实样本验证，低 12 位是子序列）
        "iid": str(int(did) + 4096),
        "uuid": str(uuid.uuid4()).upper(),
        "serial": secrets.choice(_PREFIX) + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789") for _ in range(10)),
        "mac": ":".join("%02X" % secrets.randbelow(256) for _ in range(6)),
        "model": secrets.choice(_MODELS),
        "origin": "random",
    }


# ---------------------------------------------------------------- 解析 / 归一
def _mk_record(did, iid=None, uuid_=None, serial="", mac="", model="", origin="pool"):
    did = str(did).strip()
    if not did.isdigit():
        return None
    ii = ""
    if iid not in (None, ""):
        ii = str(iid).strip()
    if not ii.isdigit():
        ii = str(int(did) + 4096)
    return {
        "did": did,
        "iid": ii,
        "uuid": str(uuid_ or "").strip() or str(uuid.uuid4()).upper(),
        "serial": str(serial or "").strip(),
        "mac": str(mac or "").strip(),
        "model": str(model or "").strip(),
        "origin": str(origin or "pool"),
    }


def _parse_line(line):
    s = str(line or "").strip().strip('"').strip("'").rstrip(",").strip()
    if not s:
        return None
    if s[0] in "{[(":
        try:
            return _norm_item(json.loads(s))
        except Exception:
            pass
    if "," in s or "\t" in s:
        sep = "," if "," in s else "\t"
        parts = [p.strip().strip('"').strip("'") for p in s.split(sep)]
        if parts and parts[0].isdigit():
            return _mk_record(parts[0], parts[1] if len(parts) > 1 else None)
        return None
    if s.isdigit():
        return _mk_record(s)
    return None


def _norm_item(x):
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return _mk_record(str(int(x)))
    if isinstance(x, str):
        return _parse_line(x)
    if isinstance(x, dict):
        did = x.get("did") or x.get("device_id") or x.get("deviceId") or x.get("deviceid")
        iid = x.get("iid") or x.get("install_id") or x.get("installId")
        return _mk_record(did, iid, x.get("uuid"), x.get("serial") or x.get("sn"),
                          x.get("mac") or x.get("mac_addr"), x.get("model"),
                          x.get("origin") or "pool")
    if isinstance(x, (list, tuple)) and x:
        return _mk_record(x[0], x[1] if len(x) > 1 else None)
    return None


def _from_obj(doc):
    """整份 JSON（{"devices":[...]} / [...] / 单个 dict）-> 记录列表。"""
    out = []
    if isinstance(doc, dict):
        inner = doc.get("devices")
        if inner is None and isinstance(doc.get("list"), list):
            inner = doc.get("list")
        if inner is None:
            one = _norm_item(doc)
            return [one] if one else []
        doc = inner
    if isinstance(doc, list):
        for x in doc:
            it = _norm_item(x)
            if it:
                out.append(it)
    return out


def _parse_lines(text):
    items = []
    skipped = 0
    for raw in str(text or "").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        it = _parse_line(s)
        if it:
            items.append(it)
        else:
            skipped += 1
    return items, skipped


# ---------------------------------------------------------------- 文件读写
def _read_file():
    try:
        with open(_POOL_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"devices": []}
    except Exception:
        return {"devices": []}


def _file_rows(cur):
    if isinstance(cur, dict):
        rows = cur.get("devices")
    elif isinstance(cur, list):
        rows = cur
    else:
        rows = None
    return rows or []


def _write_file_atomic(obj):
    d = os.path.dirname(_POOL_FILE)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = _POOL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _POOL_FILE)


# ---------------------------------------------------------------- 池管理接口
def import_items(text, merge=True):
    """粘贴文本导入。每行可以是：did / did,iid / JSON 一行 / 整份 {"devices":[...]}。
    只有 did 是纯数字才收。返回 {'added','skipped','total'}。"""
    items = []
    skipped = 0
    t = str(text or "").strip()
    if t[:1] in ("{", "["):
        try:
            items = _from_obj(json.loads(t))
        except Exception:
            items = []
        if not items:
            items, skipped = _parse_lines(t)
    else:
        items, skipped = _parse_lines(text or "")

    with _io_lock:
        cur = _read_file()
        existing = []
        if merge:
            for x in _file_rows(cur):
                r = _norm_item(x)
                if r:
                    existing.append(r)
        seen = set()
        merged = []
        for r in existing:
            if r["did"] in seen:
                continue
            seen.add(r["did"])
            merged.append(r)
        added = 0
        for r in items:
            if r["did"] in seen:
                skipped += 1
                continue
            seen.add(r["did"])
            merged.append(r)
            added += 1
        _write_file_atomic({"devices": merged})

    _reload_instances()
    return {"added": added, "skipped": skipped, "total": len(merged)}


def export_items(limit=100000):
    """导出设备池为 JSON 文本（供后台下载）。"""
    with _io_lock:
        cur = _read_file()
    rows = []
    for x in _file_rows(cur):
        r = _norm_item(x)
        if r:
            rows.append(r)
    try:
        limit = int(limit)
    except Exception:
        limit = 100000
    if limit > 0:
        rows = rows[:limit]
    return json.dumps({"devices": rows}, ensure_ascii=False, indent=2)


def pool_stats():
    with _io_lock:
        cur = _read_file()
    total = len([r for r in (_norm_item(x) for x in _file_rows(cur)) if r])
    remaining = None
    for inst in list(_INSTANCES):
        if getattr(inst, "mode", "") == "pool":
            try:
                remaining = inst.remaining()
            except Exception:
                remaining = None
            break
    if remaining is None:
        remaining = total
    return {"total": total, "remaining": remaining, "used": max(0, total - remaining), "file": _POOL_FILE}


def clear_pool():
    with _io_lock:
        _write_file_atomic({"devices": []})
    _reload_instances()
    return {"ok": True, "total": 0}


def _reload_instances():
    n = 0
    for inst in list(_INSTANCES):
        try:
            inst._load(force=True)
            n += 1
        except Exception:
            pass
    return n


def reload():
    """热加载设备池（MODE=pool 时让运行中的 DeviceSource 立即拿到新设备）。"""
    n = _reload_instances()
    out = {"ok": True, "instances": n}
    out.update(pool_stats())
    return out


# ---------------------------------------------------------------- 取设备
class DeviceSource:
    """按 MODE 提供设备：pool 优先用真实设备码，用尽或未配置则回落到 random。"""

    def __init__(self, mode: str = "random"):
        self.mode = (mode or "random").lower()
        self._lock = threading.Lock()
        self._pool = []
        self.pool_total = 0
        try:
            _INSTANCES.append(self)
        except Exception:
            pass
        if self.mode == "pool":
            self._load()

    def _load(self, force: bool = False):
        with self._lock:
            if force:
                self._pool = []
                self.pool_total = 0
            try:
                with open(_POOL_FILE, encoding="utf-8") as f:
                    arr = json.load(f)
                for x in _file_rows(arr):
                    r = _norm_item(x)
                    if not r:
                        continue
                    r["origin"] = "pool"
                    self._pool.append(r)
                self.pool_total = len(self._pool)
                random.shuffle(self._pool)
            except FileNotFoundError:
                self.pool_total = 0
            except Exception:
                self.pool_total = 0

    def take(self) -> dict:
        if self.mode == "pool":
            with self._lock:
                if self._pool:
                    return self._pool.pop()
        return random_device()

    def remaining(self) -> int:
        with self._lock:
            return len(self._pool)

    def reload(self) -> int:
        self._load(force=True)
        return self.remaining()
