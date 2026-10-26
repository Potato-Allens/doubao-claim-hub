# -*- coding: utf-8 -*-
"""R691：已领取(done)的码在重启后，开着页面不再 403「需要兑换码」（生产端到端）

跑法：cd /root/doubao-claim && ./.venv/bin/python tests/test_done_state_live.py
用一张临时码（用完即删），只做只读 /api/state + /api/next 校验。
"""
import sys, json, sqlite3, urllib.request, urllib.error

sys.path.insert(0, "/root/doubao-claim")
from app import store

BASE = "http://127.0.0.1:8080"
FAILS = []

def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (("  | " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(name)

def call(path, method="GET"):
    req = urllib.request.Request(BASE + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {}

code = store.generate(1, batch="R691E2E", note="R691 e2e tmp")[0]
store.add_claim(code, 1)
# ★R699：写一条 success 事件，验证 done 快照会把账号/到期带回来
store.log_event(code, "success", account="R699测试号 / 138****0000", uid="999000111",
                note="30 天体验套餐已到账（到期 2026-11-20 03:09）",
                detail={"account": "R699测试号 / 138****0000", "uid": "999000111",
                        "after_member": "标准套餐", "after_expiry": "2026-11-20 03:09",
                        "member": "标准套餐", "expiry": "2026-11-20 03:09"})
try:
    st, s = call("/api/state?k=%s&qs=0" % code)
    check("done 码返回 success 快照", st == 200 and s.get("status") == "success" and s.get("claimed") is True,
          "%s | %s" % (st, s.get("status")))
    check("done 码不带二维码/seq=0", s.get("seq") == 0 and not s.get("qr"), "seq=%s" % s.get("seq"))
    st2, n = call("/api/next?k=%s" % code, method="POST")
    check("done 码换码被拒（不是 403）", st2 == 200 and n.get("ok") is False, "%s | %s" % (st2, n))
    st3, _ = call("/api/state?k=NOSUCHCODE99&qs=0")
    check("不存在的码仍然 403", st3 == 403, st3)
    check("★R699 done 快照带回账号", s.get("account") == "R699测试号 / 138****0000", s.get("account"))
    check("★R699 done 快照带回到期", s.get("after_expiry") == "2026-11-20 03:09", s.get("after_expiry"))
    check("★R699 成功文案带到期", "2026-11-20 03:09" in (s.get("message") or ""), s.get("message"))
finally:
    c = sqlite3.connect(store.DB_PATH, timeout=10)
    try:
        c.execute("DELETE FROM events WHERE code=?", (code,))
        c.execute("DELETE FROM codes WHERE code=?", (code,))
        c.commit()
    finally:
        c.close()
    print("cleanup done:", code)

print("ALL PASS (%d checks)" % 7 if not FAILS else "FAILED: " + ", ".join(FAILS))
sys.exit(0 if not FAILS else 3)