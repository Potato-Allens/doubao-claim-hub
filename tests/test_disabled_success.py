# -*- coding: utf-8 -*-
"""R701\uff1a\u5df2\u9886\u53d6\u6210\u529f\u3001\u4f46\u540e\u6765\u88ab\u505c\u7528\u7684\u7801 \u2014\u2014 \u4e70\u5bb6\u7684\u6210\u529f\u9875\u4e0d\u80fd\u88ab\u62b9\u6389

\u8dd1\u6cd5\uff1acd /root/doubao-claim && ./.venv/bin/python tests/test_disabled_success.py
\u7528\u4e34\u65f6\u7801\uff08\u7528\u5b8c\u5373\u5220\uff09\uff1a
  A) \u6210\u529f\u540e\u88ab\u505c\u7528  \u2192 /claim 200\u3001/api/state 200 success + \u8d26\u53f7\u5230\u671f\u3001/api/next \u62d2\u7edd
  B) \u6ca1\u9886\u8fc7\u5c31\u88ab\u505c\u7528  \u2192 \u4ecd\u7136 403\uff08\u4e0d\u80fd\u53d8\u6210\u53ef\u7528\uff09
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
    req.add_header("User-Agent", "R701-test")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except Exception:
                return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body

def cleanup(code):
    c = sqlite3.connect(store.DB_PATH, timeout=10)
    try:
        c.execute("DELETE FROM events WHERE code=?", (code,))
        c.execute("DELETE FROM codes WHERE code=?", (code,))
        c.commit()
    finally:
        c.close()

c1 = store.generate(1, batch="R701E2E", note="R701 disabled-after-success")[0]
c2 = store.generate(1, batch="R701E2E", note="R701 disabled-no-success")[0]
c3 = store.generate(1, batch="R701E2E", note="R701 success-then-status-reset")[0]
try:
    store.add_claim(c1, 1)                     # \u2192 status=done
    store.log_event(c1, "success", account="R701\u53f7 / 139****1111", uid="777000222",
                    note="30 \u5929\u4f53\u9a8c\u5957\u9910\u5df2\u5230\u8d26\uff08\u5230\u671f 2026-11-21 03:30\uff09",
                    detail={"account": "R701\u53f7 / 139****1111", "uid": "777000222",
                            "after_member": "\u6807\u51c6\u5957\u9910", "after_expiry": "2026-11-21 03:30",
                            "member": "\u6807\u51c6\u5957\u9910", "expiry": "2026-11-21 03:30"})
    store.set_status(c1, "disabled")           # \u540e\u53f0\u505c\u7528
    store.set_status(c2, "disabled")           # \u6ca1\u9886\u8fc7\u5c31\u505c\u7528

    st, body = call("/claim?k=%s" % c1)
    check("A \u6210\u529f\u540e\u88ab\u505c\u7528\uff1a/claim \u4ecd\u80fd\u6253\u5f00\u9875\u9762", st == 200 and isinstance(body, str) and "<html" in body.lower(), st)

    st2, s = call("/api/state?k=%s&qs=0" % c1)
    check("A \u6210\u529f\u5feb\u7167\u6ca1\u88ab 403", st2 == 200 and s.get("status") == "success" and s.get("claimed") is True,
          "%s | %s" % (st2, s.get("status")))
    check("A \u5feb\u7167\u4ecd\u6709\u8d26\u53f7", s.get("account") == "R701\u53f7 / 139****1111", s.get("account"))
    check("A \u5feb\u7167\u4ecd\u6709\u5230\u671f", s.get("after_expiry") == "2026-11-21 03:30", s.get("after_expiry"))

    st3, n = call("/api/next?k=%s" % c1, method="POST")
    check("A \u6362\u7801\u88ab\u62d2\uff08\u4e0d\u662f 403\uff09", st3 == 200 and n.get("ok") is False, "%s | %s" % (st3, n))

    st4, _ = call("/api/state?k=%s&qs=0" % c2)
    check("B \u6ca1\u9886\u8fc7\u5c31\u505c\u7528\uff1a/api/state \u4ecd\u7136 403", st4 == 403, st4)
    st5, _ = call("/claim?k=%s" % c2)
    check("B \u6ca1\u9886\u8fc7\u5c31\u505c\u7528\uff1a/claim \u4ecd\u7136 403", st5 == 403, st5)

    # C) \u6210\u529f\u540e\u540e\u53f0\u72b6\u6001\u88ab\u6539\u56de unused\uff08\u5f02\u5e38\u6570\u636e/\u8bef\u64cd\u4f5c\uff09\u2014\u2014 \u6210\u529f\u9875\u4e5f\u4e0d\u80fd\u6ca1
    store.add_claim(c3, 1)
    store.log_event(c3, "success", account="R701C\u53f7", uid="777000333",
                    note="30 \u5929\u4f53\u9a8c\u5957\u9910\u5df2\u5230\u8d26\uff08\u5230\u671f 2026-11-22 03:30\uff09",
                    detail={"account": "R701C\u53f7", "after_expiry": "2026-11-22 03:30"})
    store.set_status(c3, "unused")
    st6, s6 = call("/api/state?k=%s&qs=0" % c3)
    check("C \u6210\u529f\u540e\u72b6\u6001\u88ab\u6539\u56de unused\uff1astate \u4ecd\u662f success", st6 == 200 and s6.get("status") == "success", "%s | %s" % (st6, s6.get("status")))
    st7, _ = call("/claim?k=%s" % c3)
    check("C \u8fd9\u79cd\u7801 /claim \u4e5f\u80fd\u6253\u5f00", st7 == 200, st7)
finally:
    cleanup(c1)
    cleanup(c2)
    cleanup(c3)
    print("cleanup done:", c1, c2, c3)

print("ALL PASS (%d checks)" % 9 if not FAILS else "FAILED: " + ", ".join(FAILS))
sys.exit(0 if not FAILS else 3)