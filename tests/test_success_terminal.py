# -*- coding: utf-8 -*-
"""R703: status=success is terminal even when claimed=False (CLAIMS_PER_CODE=0)."""
import sys, time, traceback
sys.path.insert(0, "/root/doubao-claim")
from app import sessions as S

FAILS = []
def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + ((" | " + str(extra)) if extra else ""))
    if not cond: FAILS.append(name)

def main():
    s = S.Session("TST703A")
    time.sleep(0.2)
    msg = "SUCCESS-TERMINAL"
    with s.lock:
        s.status, s.claimed, s.claims, s.max_claims = "success", False, 1, 0
        s.account, s.message, s.seq = "tester", msg, 7
        s.qr_b64 = "iVBORw0KGgo="
    check("locked_done sees success without claimed", s._locked_done() is True)
    check("new qr refused", s._new_qr_same_device() is False)
    check("rotate_now refused", s.rotate_now().get("ok") is False)
    check("do_rotate refused", s._do_rotate().get("ok") is False)
    check("auto rotate paused", s._maybe_rotate_expiring("success", "tok", None, 0) is True)
    check("qr hidden", s.snapshot().get("qr") is None)
    s._on_logged_in({}, None)
    check("late login ignored", s.status == "success" and s.message == msg, s.status)
    r = s.phone_login("+8613800000000", "123456")
    check("phone login refused", r.get("ok") is False and s.status == "success", r)
    s._verify_flow({}, None)
    check("verify skipped", s.status == "success" and s.message == msg, s.status)
    check("seq unchanged", s.seq == 7, s.seq)
    with s.lock:
        s.dead = True
    print()
    if FAILS:
        print("FAILED:", ", ".join(FAILS)); return 1
    print("ALL PASS (10 checks)"); return 0

if __name__ == "__main__":
    try: sys.exit(main())
    except Exception:
        traceback.print_exc(); sys.exit(2)
