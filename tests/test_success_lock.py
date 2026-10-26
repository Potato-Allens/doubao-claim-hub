# -*- coding: utf-8 -*-
"""领取成功后状态不可被覆盖 —— 回归测试（R685/R686/R687）

跑法：
    cd /root/doubao-claim && ./.venv/bin/python tests/test_success_lock.py

这些用例对应线上真实事故：
  · DEMO0020：success 6 秒后被手机号重复登录盖成 fail
  · DEMO0021 / DEMO0022：success 后二维码仍在自动换码
  · 出码失败 3 次把 success 盖成 error
  · 迟到轮询把 success 改成 verifying（然后被 R685 挡掉 → 永久卡核验中）
"""
import sys, time, traceback

sys.path.insert(0, "/root/doubao-claim")
from app import sessions as S

FAILS = []

def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (("  | " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(name)

def new_sess(code):
    s = S.Session(code)
    time.sleep(0.15)
    return s

def main():
    # 1) claimed 会话：重复扫码登录必须被忽略
    s = new_sess("TSTLOCK1")
    with s.lock:
        s.status, s.claimed, s.claims, s.max_claims = "success", True, 1, 1
        s.account, s.message, s.seq = "t / +852******00", "🎉 领取成功！30 天体验套餐已到账", 7
    s._on_logged_in({}, None)
    check("dup qr login ignored", s.status == "success" and s.account == "t / +852******00", s.status)

    # 2) claimed 会话：核验流程跳过，成功文案保持原文
    s._verify_flow({}, None)
    check("verify skipped, message kept", s.status == "success" and s.message == "🎉 领取成功！30 天体验套餐已到账", s.message)

    # 3) claimed 会话：迟到的 fail 结果不能覆盖
    s._finish({"got": False, "items": [], "entry": {"has_active": False}})
    check("late fail dropped", s.status == "success", s.status)

    # 4) claimed 会话：换码被拒绝，seq 不变
    r = s.rotate_now()
    check("rotate refused", r.get("ok") is False and s.seq == 7, r.get("message"))

    # 5) 非 claimed 的 fail 会话：仍允许换码（不能误伤「失败后重试」）
    #    用 stub 代替真实出码，测试不碰网络、不烧设备
    s2 = new_sess("TSTLOCK2")
    with s2.lock:
        s2.status, s2.claimed = "fail", False
    s2._new_qr_same_device = lambda: True
    r2 = s2._do_rotate()
    check("fail-state rotate allowed", r2.get("ok") is True, r2.get("ok"))
    with s2.lock:
        s2.dead = True

    # 6) 出码过程中 claimed 翻转：恢复 success 且保留原文案
    s3 = new_sess("TSTLOCK3")
    with s3.lock:
        s3.status, s3.claimed = "success", False
        s3.message = "🎉 领取成功！30 天体验套餐已到账"
    def _fake_fail():
        s3.claimed = True
        return False
    s3._new_qr_same_device = _fake_fail
    r3 = s3._do_rotate()
    check("mid-flight claim keeps success", s3.status == "success" and s3.message.startswith("🎉 领取成功"),
          "%s | %s" % (s3.status, s3.message[:20]))
    check("mid-flight rotate refused", r3.get("ok") is False, r3.get("message"))

    # 7) snapshot 必须带 claimed 字段（前端据此锁按钮）
    check("snapshot exposes claimed", s.snapshot().get("claimed") is True)

    # 7b) R696：成功后不再下发二维码
    s.qr_b64 = "iVBORw0KGgo="
    with s.lock:
        s.claimed, s.status = False, "waiting"
    _q1 = s.snapshot().get("qr")
    with s.lock:
        s.claimed, s.status = True, "success"
    _q2 = s.snapshot().get("qr")
    check("qr hidden after success", bool(_q1) and _q2 is None, "%s -> %s" % (bool(_q1), _q2))
    with s.lock:
        s.qr_b64 = None

    # 7c) R697：出码失败风暴熔断也不能把 claimed 写成 error
    import app.proxy as _P
    _rot, _pf = _P.rotate, S._proxy_for
    _P.rotate = lambda *a, **k: None
    S._proxy_for = lambda *a, **k: None
    try:
        with s.lock:
            s.claimed, s.status = True, "success"
            s._fail_times = [time.time()] * 30
        s._qr_fail("test storm")
    finally:
        _P.rotate, S._proxy_for = _rot, _pf
    check("storm keeps success", s.status == "success", s.status)

    # 7d) R698：核验跑到一半成功落库 → 不能把失败文案写回页面
    class _FakeCookies:
        def get_dict(self): return {}
    class _FakeSess:
        cookies = _FakeCookies()
    class _FakeClient:
        s = _FakeSess()
    s5 = new_sess("TST698")
    with s5.lock:
        s5.status, s5.claimed = "verifying", False
        s5.message = "🎉 领取成功！30 天体验套餐已到账"
    s5._guard_exit_bg = lambda *a, **k: None
    def _fake_once(ck, dev, full=False, since_ms=None):
        # 模拟「核验期间手动复查/补核先把 success 落库」，它会同时写成功文案
        with s5.lock:
            s5.claimed, s5.status = True, "success"
            s5.message = "🎉 领取成功！30 天体验套餐已到账（延迟到账，已自动补记）"
        return {"got": False, "gift_duplicate": True,
                "gift_duplicate_effective": "2026-10-01 10:00"}
    s5._verify_once = _fake_once
    s5._verify_flow({}, _FakeClient())
    check("verify keeps success copy", s5.status == "success"
          and s5.message.startswith("🎉 领取成功"), "%s | %s" % (s5.status, s5.message[:24]))
    with s5.lock:
        s5.dead = True

    # 8) claimed 会话：发码请求必须直接拒绝（R692，不能白烧官方短信/3D 配额）
    rp = s.phone_send("+8613800000000")
    check("phone_send refused after success",
          rp.get("ok") is False and rp.get("done") is True and rp.get("cooldown") == 0, rp)

    for x in (s, s3):
        try:
            x.dead = True
        except Exception:
            pass

    print()
    if FAILS:
        print("FAILED:", ", ".join(FAILS))
        return 1
    print("ALL PASS (12 checks)")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)