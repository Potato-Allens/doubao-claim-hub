# -*- coding: utf-8 -*-
"""R689/R690：领取成功的库级幂等 + 重启后恢复领取态（隔离 DB，不碰生产库）

跑法：cd /root/doubao-claim && ./.venv/bin/python tests/test_late_idempotent.py
物证：
  · DEMO0008 02:13:12 手动复查 success → 02:13:18 核验 success（同码两条）
  · 7 个一码两条成功（DEMO0007/DEMO0008/DEMO0005/DEMO0004/DEMO0003/DEMO0002/DEMO0009）
  · 重启后 Session 的 claimed 默认 False → 已领过的码会重新出码
"""
import os, sys, time, sqlite3

DB = "/root/_t689.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DB_PATH"] = DB
sys.path.insert(0, "/root/doubao-claim")

from app import store
store.init()
from app import sessions as S

FAILS = []

def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (("  | " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(name)

def succ(code):
    c = sqlite3.connect(DB, timeout=8)
    try:
        return c.execute("SELECT COUNT(*) FROM events WHERE code=? AND kind='success'",
                         (code,)).fetchone()[0]
    finally:
        c.close()

def new_sess(code):
    s = S.Session(code)
    time.sleep(0.2)
    return s

R = lambda acct: {"items": [{"end": "2026-11-20 00:00:00", "source": "pc_membership_gift"}],
                  "account": acct, "entry": {}}

# 1) 同 uid：库里已有 success → 必须跳过，且 claimed 回填
store.log_event("TST689A", "success", account="t / +852******66", uid="u689", note="seed")
s = new_sess("TST689A")
with s.lock:
    s.status, s.claimed = "waiting", False
    s.known = {"uid": "u689"}
s._apply_late_success(R("t / +852******66"), tag="R689自检")
check("同 uid 重复补记被跳过", succ("TST689A") == 1 and s.status == "success", succ("TST689A"))
check("去重后 claimed 回填", s.claimed is True and s.claims >= 1, "%s/%s" % (s.claims, s.claimed))
check("去重后换码被拒（成功不会被盖）", s.rotate_now().get("ok") is False)

# 2) uid 不同但账号相同（重启后 uid 丢了）→ 也要挡住
store.log_event("TST689B", "success", account="t / +852******77", uid="uAAA", note="seed")
s2 = new_sess("TST689B")
with s2.lock:
    s2.status, s2.claimed = "waiting", False
    s2.known = {"uid": "uBBB"}
s2._apply_late_success(R("t / +852******77"), tag="R689自检")
check("同账号不同 uid 也被跳过", succ("TST689B") == 1 and s2.status == "success", succ("TST689B"))
check("同账号去重后 claimed 回填", s2.claimed is True)

# 3) 全新码：仍要正常补记（不能误伤）
s3 = new_sess("TST689C")
with s3.lock:
    s3.status, s3.claimed = "waiting", False
    s3.known = {"uid": "uCCC"}
s3._apply_late_success(R("t / +852******88"), tag="R689自检")
check("新单正常补记", succ("TST689C") == 1 and s3.status == "success" and s3.claimed is True,
      "%d | %s" % (succ("TST689C"), s3.status))

# 4) 落库竞态：_finish 入口判定通过后、真正落库前，别处已落成 success
s4 = new_sess("TST689D")
with s4.lock:
    s4.status, s4.claimed, s4.claims = "verifying", False, 0
def _fake_note(_r):
    with s4.lock:
        s4.status, s4.claimed, s4.claims = "success", True, 1
s4._note_membership = _fake_note
s4._finish({"got": True, "account": "t / +852******99",
            "items": [{"end": "2026-11-20 00:00:00", "source": "pc_membership_gift"}],
            "entry": {}})
check("落库前 mid-flight 成功 → 丢弃本次结果",
      s4.status == "success" and s4.claims == 1 and succ("TST689D") == 0,
      "claims=%s events=%s" % (s4.claims, succ("TST689D")))

# 5) R690：重启后新 Session 必须从 events 恢复 claimed，且不再出码
s5 = S.Session("TST689A")
time.sleep(0.2)
check("重启后从 events 恢复 claimed", s5.claimed is True and s5.status == "success",
      "%s | %s" % (s5.claimed, s5.status))
s5.ensure()
time.sleep(0.6)
check("恢复后不再出码", s5.seq == 0 and s5.device is None, "seq=%s dev=%s" % (s5.seq, s5.device))

# 6) R690：codes.status=done 的码同样锁死
_g = store.generate(1, batch="T689", note="r690 test")
_code6 = (_g[0] if isinstance(_g, list) and _g else "TST689E")
store.add_claim(_code6, 1)
s6 = S.Session(_code6)
time.sleep(0.2)
check("status=done 也锁死", s6.claimed is True and s6.status == "success",
      "%s | %s" % (s6.claimed, s6.status))

# 7) R693：同卡第二账号只记录告警，不改状态
store.log_event("TST689F", "success", account="t / +852******11", uid="uA", note="seed")
s7 = new_sess("TST689F")
_cap = []
_orig = S._log
S._log = lambda m: _cap.append(str(m))
try:
    s7._note_second_account("uB", "自检")
finally:
    S._log = _orig
check("同卡第二账号写告警", any("R693" in m for m in _cap), _cap[:1])

for x in (s, s2, s3, s4, s5, s6, s7):
    try:
        with x.lock:
            x.dead = True
    except Exception:
        pass

print("ALL PASS (%d checks)" % (8 + 3) if not FAILS else "FAILED: " + ", ".join(FAILS))
sys.exit(0 if not FAILS else 3)