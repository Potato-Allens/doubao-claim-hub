# -*- coding: utf-8 -*-
"""
豆包 / 抖音 passport「手机号 + 短信验证码」纯协议登录（生产模块）

逆向依据（实证，非猜测）
--------------------
accounts.doubao.com SSO 登录页：
    POST /passport/web/send_code/   body: mix_mode=1&mobile=<xor5hex>&type=<xor5hex>&is6Digits=1&fixed_mix_mode=1
    POST /passport/web/sms_login/   body: mix_mode=1&mobile=<xor5hex>&code=<xor5hex>&login_only=false&ignore_lark_account=true&fixed_mix_mode=1
签名 / 编码完全复用 app/sdk.py（jssdk 的 sign / qs / x-tt-passport-aid-sign / XOR-0x05）。
mobile 实值为 "+86 " + 11位号码；type=24 是登录验证码。

关键实测结论（2026-10-13 真机打通 1**********，uid 1000000000000001）
--------------------------------------------------------------
1. send_code 与 sms_login **必须同一个 client 实例**（同 cookie jar + 同 biz_trace_id）。
   分进程 / 分实例跑会稳定回 1203（伪「过期」）。
2. 新码不会立刻作废旧码（16:45 发的码 16:48 仍可用），但同一个码只能用一次。
3. 1202 = 验证码错误；1203 = 错误次数过多或验证码过期（**码不对同样可能回 1203**）；1003 = 手机号格式错。
4. 登录成功后账号即持有 pc_membership_gift（是否新发放由官方活动侧判定，本模块只负责登录）。
5. 号码支持任意国家（前缀自动识别）。**但实测（2026-10-13）：向境外号（+852）发码会被抖音 passport 风控要求滑块验证**（error_code 1105/1107 + verify_center_decision_conf，verify_event=tt_send_sms_login）；换两个不同香港出口、两个不同香港号码均复现。扫码通道不走 send_sms_login，不受此限制。

本模块**不落盘**、不打印验证码；所有出网都走 app/sdk.py 的代理钩子（会话级粘性）。
"""
from __future__ import annotations

import os
import random
import re

from app import sdk
from app import fingerprint as _fpmod   # R69 设备指纹

# —— 与 SSO 页面对齐的版本号（实测抓包） ——
PASSPORT_JSSDK_VERSION = "4.1.5-beta.0"
P_CA = "4.0.17"
P_CA_REAL = "1.0.0.938"

SEND_CODE = "/passport/web/send_code/"
SMS_LOGIN = "/passport/web/sms_login/"
CHECK_MOBILE_REGISTERED = "/passport/web/user/check_mobile_registered/"   # 只查注册态，不发短信（通道自检用）

SEND_COOLDOWN = 60          # 服务端返回的 retry_time（秒），前端倒计时用
CODE_TTL = 300              # 建议 5 分钟内提交（超时即失效）

_CN_MOBILE = re.compile(r"^1[3-9]\d{9}$")
# ★R279_NANP：+1 必须是合法 NANP 号码（区号/局号不能以 0/1 开头）。
_NANP = re.compile(r"^[2-9]\d{2}[2-9]\d{6}$")

# ---------------------------------------------------------------- 国家/地区表
# (区号, ISO, 名称, 国内号码最短, 最长, 默认出口地区)
# 出口地区为 代理服务商 的 region 参数；CN 无大陆出口（命中 0/3）。
# ★R678_CN_US：2026-10-19/20 生产数据（+86 账号按实际出口统计）
#   10-20：US 426 成功 / 323 失败 = 57%；HK 27 / 166 = 14%
#   10-19：US 64 / 62 = 51%；HK 4 / 24 = 14%（n≈1100，两天同向）
#   且 CN→HK 会在登录确认后改出口，触发「中途换 IP」，被官方风控降权。
#   故 +86 默认走 US（可用环境变量 CN_EXIT_REGION 覆盖，回滚设 HK）。
CN_EXIT_REGION = (os.environ.get("CN_EXIT_REGION", "US") or "US").strip().upper() or "US"
COUNTRIES = [
    ("86",  "CN", "中国大陆", 11, 11, CN_EXIT_REGION),
    ("852", "HK", "中国香港", 8, 8, "HK"),
    ("853", "MO", "中国澳门", 8, 8, "HK"),
    ("886", "TW", "中国台湾", 9, 9, "TW"),
    ("1",   "US", "美国/加拿大", 10, 10, "US"),
    ("81",  "JP", "日本", 10, 11, "JP"),
    ("82",  "KR", "韩国", 9, 11, "KR"),
    ("65",  "SG", "新加坡", 8, 8, "SG"),
    ("60",  "MY", "马来西亚", 9, 10, "MY"),
    ("44",  "GB", "英国", 10, 10, "GB"),
    ("61",  "AU", "澳大利亚", 9, 9, "AU"),
    ("49",  "DE", "德国", 10, 11, "DE"),
    ("33",  "FR", "法国", 9, 9, "FR"),
    ("31",  "NL", "荷兰", 9, 9, "NL"),
    ("84",  "VN", "越南", 9, 10, "VN"),
    ("91",  "IN", "印度", 10, 10, "IN"),
    ("66",  "TH", "泰国", 9, 9, "TH"),
    ("7",   "RU", "俄罗斯", 10, 10, "RU"),
]
_BY_DIAL = sorted(COUNTRIES, key=lambda x: -len(x[0]))
_BY_ISO = dict((c[1], c) for c in COUNTRIES)


def country_list() -> list:
    """供前端下拉用：[{iso, name, dial}]"""
    return [{"iso": c[1], "name": c[2], "dial": c[0], "min": c[3], "max": c[4],
             "region": c[5]} for c in COUNTRIES]


def parse_mobile(raw, default_iso: str = "CN"):
    """把买家输入解析成 (iso, dial, national, ok)。

    • 带 + / 00 / 直接写区号 → 自动识别国家；
    • 纯 11 位 1 开头 → 中国大陆；
    • 认不出来 → ok=False（前端会提示改用手动选国家）。
    """
    s = re.sub(r"[^\d+]", "", str(raw or ""))
    d = _BY_ISO.get(default_iso) or COUNTRIES[0]
    if not s:
        return default_iso, d[0], "", False
    intl = False
    if s.startswith("+"):
        s, intl = s[1:], True
    elif s.startswith("00"):
        s, intl = s[2:], True
    if intl:
        for dial, iso, name, lo, hi, reg in _BY_DIAL:
            if s.startswith(dial):
                nat = s[len(dial):]
                ok = (lo <= len(nat) <= hi) and (iso != "CN" or bool(_CN_MOBILE.match(nat)))
                # ★R279_NANP：+1 168xxxxxxx 这类非法 NANP 号码，官方 send_code 也许返回 success，
                # 但短信永远到不了，用户只会拿旧码一直 1203。这里直接判成格式不对。
                if ok and iso in ("US", "CA") and not _NANP.match(nat):
                    ok = False
                return iso, dial, nat, ok
        return default_iso, d[0], s, False
    if _CN_MOBILE.match(s):
        return "CN", "86", s, True
    for dial, iso, name, lo, hi, reg in _BY_DIAL:
        if dial == "1":
            continue                       # 无前缀的纯数字不能靠 "1" 去猜美国
        if s.startswith(dial) and lo <= len(s) - len(dial) <= hi:
            return iso, dial, s[len(dial):], True
    return default_iso, d[0], s, False


def iso_from_dial_prefix(s) -> str:
    """从 "+852******12" 这种（官方可能带掩码的）号码串里判国家。"""
    m = re.search(r"\+\s*(\d{1,4})", str(s or ""))
    if not m:
        return ""
    digits = m.group(1)
    for dial, iso, name, lo, hi, reg in _BY_DIAL:
        if digits.startswith(dial):
            return iso
    return ""


# ---------------------------------------------------------------- R84-Q 出口地区自动跟随
# R84-Q：识别不出账号国家时的**最后兜底出口地区**。
# 选 HK 的理由：① +86 无大陆出口、实测已映射到 HK；② 港区/大陆是最常见账号；③ 比 US 更接近
# 「账号地区 = 出口地区」的匹配原则。需要时可被环境变量 R84Q_DEFAULT_REGION 覆盖（例：US）。
DEFAULT_REGION = (os.environ.get("R84Q_DEFAULT_REGION", "HK") or "HK").strip().upper() or "HK"

_MASK_CHARS = re.compile(r"[*•·xX#]+")


def iso_from_any_number(s) -> str:
    """R84-Q：从**任意形态**的账号号码串里判国家（比 iso_from_dial_prefix 宽）。

    覆盖三种真实形态：
      1. "+852******12" / "00852******72"        → 带国际前缀（老口径，原样交给 iso_from_dial_prefix）
      2. "852******72" / "86 138 **** 8000"      → 被掩码/带空格、**没有 '+'**
      3. "13800138000"（纯 11 位 1 开头）          → 中国大陆
    认不出来 → ""（调用方走兜底）。
    """
    t = str(s or "").strip()
    if not t:
        return ""
    iso = iso_from_dial_prefix(t)          # ① 带 '+'/'00' 的标准口径
    if iso:
        return iso
    was_masked = bool(_MASK_CHARS.search(t))
    raw = _MASK_CHARS.sub("", t)           # ② 先剔掉掩码符号（* • · x #）
    raw = re.sub(r"[^\d+]", "", raw)       #    再去掉空格 / 横线等
    if not raw:
        return ""
    if raw.startswith("+"):
        return iso_from_dial_prefix(raw)
    if raw.startswith("00"):
        raw = raw[2:]
    if _CN_MOBILE.match(raw):
        return "CN"
    for dial, iso2, name, lo, hi, reg in _BY_DIAL:
        if dial == "1":
            continue                       # 无前缀的纯数字不能靠 "1" 猜美国/加拿大
        if not raw.startswith(dial):
            continue
        nat = raw[len(dial):]
        if lo <= len(nat) <= hi:
            return iso2
        if was_masked and nat:
            return iso2                    # 掩码吃掉了位数：只凭区号判（仅限确实被掩码的串）
    return ""


def pick_region(*isos, fallback: str = "") -> str:
    """R84-Q：按**优先级**挑第一个能映射成出口地区的国家；全识别不出 → fallback。

    调用方按「买家输入手机号国家 → 扫码账号真实国家 → 后台全局兜底 → 默认」的顺序传 iso。
    """
    for iso in isos:
        reg = region_for_iso(iso)
        if reg:
            return reg
    return fallback or ""


def mobile_key(raw) -> str:
    """用来比对「是不是同一个号」的稳定键：区号+国内号码。"""
    iso, dial, nat, ok = parse_mobile(raw)
    return (dial + nat) if ok else ""


def region_for_iso(iso: str) -> str:
    """该国家账号建议用的出口地区（实测有效的 region）。"""
    c = _BY_ISO.get(str(iso or "").upper())
    return c[5] if c else ""


def mask_any(raw) -> str:
    iso, dial, nat, ok = parse_mobile(raw)
    if not ok:
        return "***"
    nat = nat or ""
    if iso == "CN" and len(nat) == 11:
        return nat[:3] + "****" + nat[-4:]
    if len(nat) >= 4:
        return "+%s****%s" % (dial, nat[-4:])
    return "***"


def e164(raw) -> str:
    """发给官方的号码格式："+8618900000000"。"""
    iso, dial, nat, ok = parse_mobile(raw)
    return ("+%s %s" % (dial, nat)) if ok else ""


# 人性化错误（拿不到官方 description 时的兜底）
ERR_TEXT = {
    1003: "手机号格式不正确，请重新填写",
    1017: "操作过于频繁，请稍后再试",
    1202: "验证码错误，请重新输入",
    1203: "验证码已失效或错误次数过多，请重新获取验证码",
    1454: "该手机号还没有豆包账号，请先用豆包 App 注册后再领取",
    12: "操作过于频繁，请稍后再试",
    16: "当前通道无权限，请稍后重试",
    # ★R72：风控要求人机验证（实测 2026-10-13 晚上常见，by 境外号/高风险出口）。短信通道打不动，
    #   直接告诉买家换「扫码领取」通道，比甩一句「操作失败」转化高得多。
    1105: "官方要求人机验证（滑块/3D）：系统会自动尝试通过，通常 1~3 分钟，请保持页面打开；"
          "若 5 分钟仍没短信，请等 1~2 分钟再点一次，或改用「扫码领取」（成功率更高）",
    1107: "官方要求人机验证（滑块/3D）：系统会自动尝试通过，通常 1~3 分钟，请保持页面打开；"
          "若 5 分钟仍没短信，请等 1~2 分钟再点一次，或改用「扫码领取」（成功率更高）",
}


def _conf_dict(resp: dict) -> dict:
    """把 data.verify_center_decision_conf 解析成 dict（可能是 JSON 字符串 / dict / 空）。"""
    try:
        import json as _json
        conf = (((resp or {}).get("data") or {}).get("verify_center_decision_conf")) or ""
        if isinstance(conf, dict):
            return conf
        if isinstance(conf, str) and conf.strip():
            d = _json.loads(conf)
            return d if isinstance(d, dict) else {}
        return {}
    except Exception:
        return {}


def verify_subtype(resp: dict) -> str:
    """从官方返回的 verify_center_decision_conf 里读风控题型：slide（滑块）/ 3d（3D 旋转）/ 空。

    ★R82-M：同一个信封里题型可能落在 subtype / mode / ui_type 三个键上（R74 真机探测的
    `conf_keys` 实测含 subtype 与 ui_type），三个都读，都读不到才算「题型未知」。
    """
    d = _conf_dict(resp)
    for _k in ("subtype", "mode", "ui_type"):
        _v = str(d.get(_k) or "").strip().lower()
        if _v:
            return _v
    return ""


def has_conf(resp: dict) -> bool:
    """响应里**有没有**风控信封（`data.verify_center_decision_conf` 非空）。

    ★R82-M：线上 47 条「发码失败」既没有 error_code 也没有 description，只有这个信封
    （Gauss《豆包-手机号通道诊断-20261014》§2.3 的 R74 真机证据）。只认「有没有 conf」，
    才能把这 47 条认成风控，而不是回落成「操作失败，请稍后重试」。
    """
    try:
        conf = (((resp or {}).get("data") or {}).get("verify_center_decision_conf"))
    except Exception:
        conf = None
    if isinstance(conf, dict):
        return bool(conf)
    return bool(str(conf or "").strip())


def needs_verify(resp: dict) -> bool:
    """这条响应是不是「被风控要求人机验证」。

    ★R82-M 覆盖三类：① error_code = 1105/1107；② conf 里读得出题型（slide / 3d）；
    ③ **conf 存在但题型读不出**（线上那 47 条的形态）—— 以前漏掉的就是第 ③ 类。
    """
    try:
        ec = int((resp or {}).get("error_code") or 0)
    except Exception:
        ec = 0
    if ec in (1105, 1107):
        return True
    return bool(verify_subtype(resp)) or has_conf(resp)


def risk_kind(resp: dict) -> str:
    """风控题型的中文人话（题型读不出时返回「人机验证」，由调用方决定要不要加「题型未知」）。"""
    st = verify_subtype(resp)
    if "3d" in st:
        return "3D 旋转验证"
    if "slide" in st:
        return "滑块验证"
    return "人机验证"


def normalize_mobile(raw) -> str:
    """去掉 +86 / 空格 / 短横线，返回 11 位纯数字。"""
    s = re.sub(r"[\s\-()（）]", "", str(raw or ""))
    if s.startswith("+86"):
        s = s[3:]
    elif s.startswith("86") and len(s) == 13:
        s = s[2:]
    return s


def valid_cn_mobile(raw) -> bool:
    """只接受中国大陆号段（1[3-9] 开头 11 位）。"""
    return bool(_CN_MOBILE.match(normalize_mobile(raw)))


def mask_mobile(raw) -> str:
    s = normalize_mobile(raw)
    return (s[:3] + "****" + s[-4:]) if len(s) == 11 else "***"


def friendly_error(resp: dict, default: str = "操作失败，请稍后重试") -> str:
    """把官方错误翻译成买家看得懂的话（优先用官方 description，它本身就很人性化）。"""
    r = resp or {}
    # ★R248_NESTED_ERR：sms_login 的错误信封在 data 里（data.description / data.error_code）。
    #   以前只读顶层 → 1202「验证码错误」被吞成「操作失败，请稍后重试」，用户以为系统坏了不停重试，
    #   12 次后还被 429 暂缓。生产上 31/36 条登录失败都是这个假象（真因=输错码）。
    _d0 = r.get("data") if isinstance(r.get("data"), dict) else {}
    if r.get("_err") or r.get("_net"):
        return "网络异常，请稍后重试"
    # ★R82-M：非 JSON 响应（WAF/CDN 拦页）以前和风控一样回落成「操作失败」，现在点名是 HTTP 异常。
    if r.get("_http") and not needs_verify(r):
        return "官方通道返回异常（HTTP %s），请稍后重试" % _clip(r.get("_http"), 6)
    # ★R80/R82-M：风控（人机验证）优先说人话 —— 官方 description 常常只给「操作失败，请稍后重试」，
    #   买家照着重试只会白等；这里一律改成可执行的指引（换 +86 号 / 改走扫码领取）。
    if needs_verify(r):
        _kind = risk_kind(r)
        if not verify_subtype(r):
            _kind += "·题型未知"     # 只有信封、读不出题型（线上那 47 条的形态）
        # ★R248_UI_SHORT：原文案 3 行、像"账号坏了"，用户以为页面挂了；改成短句+两个动作。
        return ("官方要求人机验证（%s）：本次自动验证没通过，多数是点得太频繁。"
                "请等 1~2 分钟再点一次；扫码通道成功率更高，可直接点上方「扫码领取」" % _kind)
    ec = r.get("error_code")
    if ec is None:
        ec = _d0.get("error_code")
    # ★R258_1203：官方 description 是「错误次数过多或验证码过期」，用户看不懂到底该干嘛；
    # 1203 优先给可执行文案（重发会让旧码立即失效，这点必须点明）。
    try:
        if int(ec) == 1203:
            return ("验证码不正确或已过期（重新获取后旧验证码会立即失效），"
                    "请用最新短信里的 6 位码，或点「重新获取」")
    except Exception:
        pass
    # ★R499_EC7：官方 ec=7「访问太频繁」实测就是「发码后太快提交」，
    # 原文案「请稍后重试」等于没说；直接给可执行的秒数。
    try:
        if int(ec) == 7:
            return ("提交太快了（官方判频繁）：请等 15 秒再点一次，别连续点；"
                    "若提示验证码过期，点「重新获取」换新码")
    except Exception:
        pass
    d = str(r.get("description") or _d0.get("description") or "").strip()
    if d:
        return d
    try:
        return ERR_TEXT.get(int(ec), default)
    except Exception:
        return default


# ------------------------------------------------------- ★R82-M 发码失败打点
# 背景：Gauss《豆包-手机号通道诊断-20261014》§6 未决项 1 —— 生产代码从不落盘原始响应形状，
#       那 47 条「无 error_code 信封」永远只能靠推断。这里**只加字段，不改判定逻辑**。
def _clip(v, n: int) -> str:
    """压成一行、限长（note 要进 events 表，store 侧上限 300 字符）。"""
    s = str("" if v is None else v)
    s = s.replace("\r", " ").replace("\n", " ").replace("|", "/").replace("\uff5c", "/").strip()
    return s if len(s) <= n else s[:n] + "~"


def _field(r, key, n: int) -> str:
    """诊断字段取值：键不存在 → "-"；值为 None → "null"；否则截断。"""
    if key not in r:
        return "-"
    v = r.get(key)
    if v is None:
        return "null"
    s = _clip(v, n)
    return s if s else '\"\"'


def diag_note(resp: dict, http_status=None) -> str:
    """把「失败信封」压成一行诊断：raw_keys / error_code / message / description / has_conf /
    conf.subtype / conf 键数 / ui_type / verify_event / from / is_complex_sms / http_status / log_id。"""
    r = resp if isinstance(resp, dict) else {}
    d = _conf_dict(r)
    if http_status is None:
        http_status = r.get("_http")
    parts = [
        "keys=" + _clip(",".join(sorted(str(k) for k in r.keys())), 42),
        "ec=" + _field(r, "error_code", 12),
        "msg=" + _field(r, "message", 20),
        "desc=" + _field(r, "description", 20),
        "has_conf=" + ("1" if has_conf(r) else "0"),
        "conf_n=%d" % len(d),
        "st=" + _clip(d.get("subtype") or "-", 12),
        "ui=" + _clip(d.get("ui_type") or "-", 12),
        "ev=" + _clip(d.get("verify_event") or "-", 26),
        "from=" + _clip(d.get("from") or "-", 16),
        "cmplx=" + ("-" if d.get("is_complex_sms") is None
                    else ("1" if d.get("is_complex_sms") else "0")),
        "http=" + ("-" if http_status in (None, "") else _clip(http_status, 8)),
        "log_id=" + _clip(d.get("log_id"), 32),
    ]
    return "|".join(parts)


def send_fail_note(message: str, resp: dict, mobile: str = "") -> str:
    """发码失败的事件 note 成品（≤300 字符）：人话原因 + 错误码 + 「诊断 …」打点字段。"""
    r = resp if isinstance(resp, dict) else {}
    ec = r.get("error_code")
    head = "手机号通道：发送验证码失败（%s%s）" % (
        _clip(message, 40), ("｜错误码 %s" % _clip(ec, 12)) if ec else "")
    if mobile:
        head += " 号码 %s" % _clip(mobile, 20)
    return (head + "｜诊断 " + diag_note(r))[:299]


def account_iso(info: dict) -> str:
    """★R82-M（Gauss 提案 D）：从 account_info() 结果里读**账号真实国家**，而不是买家输入的国家。

    口径与扫码通道（sessions.py:495-505）一致：raw.country_code 优先，其次 mobile/account 的区号前缀。
    """
    try:
        raw = (info or {}).get("raw") or {}
        cc = str(raw.get("country_code") or "").strip().upper()
        if len(cc) == 2 and cc.isalpha():
            return cc
    except Exception:
        pass
    # R84-Q：再退一步 —— 账号串里可能是「不带加号、被掩码」的号码（例 "852******72"）
    return iso_from_any_number((info or {}).get("mobile") or (info or {}).get("account") or "")


class PhoneLoginClient:
    """手机号 + 短信验证码 纯协议登录。一个实例 = 一个独立会话（cookie 隔离）。

    ★ 同一个手机号必须先 send_code 再 sms_login，且必须用**同一个实例**。
    """

    def __init__(self, proxies=None, aid: int = None, pc: bool = True,
                 timeout: int = 20, logger=None, fp: dict = None, region: str = "HK"):
        self.aid = int(aid if aid is not None else sdk.AID_PC)
        self.pc = bool(pc)
        self.timeout = int(timeout)
        self.logger = logger or (lambda m: None)
        try:
            self.proxies = proxies if proxies is not None else sdk._px(None)
        except Exception:
            self.proxies = proxies
        # ★ R69：每会话一套自洽指纹（UA / device_id / verifyFp / 屏幕 / 显卡…）
        self.fp = fp if fp is not None else _fpmod.pick(region)
        self.ua = self.fp.get("ua") or sdk.UA
        self.s = sdk._mk_session(self.proxies)
        try:
            self.sdk_info = sdk.build_sdk_source_info(fp=self.fp)
        except Exception:
            self.sdk_info = ""
        self.ms_token = ""
        self.biz_trace_id = "".join(random.choice("0123456789abcdef") for _ in range(8))
        self.mobile = ""          # 已发码的号码（区号+国内号）
        self.iso = ""             # 号码国家
        self.sent_at = 0.0        # 上次发码时间戳
        self._captured = None     # ★R215：浏览器通道捕获的 send_code 参数

    # ------------------------------------------------------------ 参数 / 头
    def _base_params(self) -> dict:
        p = {
            "passport_jssdk_version": PASSPORT_JSSDK_VERSION,
            "passport_jssdk_type": "normal",
            "is_from_ttaccountsdk": 1,
            "aid": self.aid,
            "language": "zh",
            "account_app_language": "zh-CN",
            "ts": sdk.daily_ts(),
            "p_ca": P_CA,
            "p_ca_real": P_CA_REAL,
            "multi_login": 1,
            "account_sdk_source": "web",
            "account_sdk_source_info": self.sdk_info,
            "biz_trace_id": self.biz_trace_id,
            "device_platform": "web",
            "device_id": "",
        }
        if self.pc:
            _f = getattr(self, "fp", None) or {}
            _did = _f.get("device_id") or ""
            p.update({
                "doubao_device_platform": "desktop",
                "web_platform": "desktop",
                "pc_version": "2.31.4",
                "doubao_pc_version": "2.31.4",
                "channel": _f.get("channel") or "win",
                "version_code": _f.get("version_code") or "20800",
                "client_platform": "pc_client",
                # ★ R82-K：PC 客户端设备档案（对齐竞品 43 字段；老字段一个不动，全部 .get 兜底）
                "fp": _f.get("fp") or (("verify_" + _did) if _did else ""),
                "mac": _f.get("mac") or "",
                "pc_uuid": _f.get("pc_uuid") or "",
                "pc_serial": _f.get("pc_serial") or "",
                "device_token": _f.get("device_token") or "",
                "clientudid": _f.get("clientudid") or "",
                "cdid": _f.get("cdid") or "",
                "tea_uuid": _f.get("tea_uuid") or _did,
                "web_id": _f.get("web_id") or "",
                "tea_web_id": _f.get("tea_web_id") or "",
                "zijie_web_id": _f.get("zijie_web_id") or "",
                "web_tab_id": _f.get("web_tab_id") or "",
                "device_name": _f.get("device_name") or "",
                "device_model": _f.get("device_model") or "",
                "device_type": _f.get("device_type") or "windows_pc",
                "os_name": _f.get("os_name") or "Windows NT",
                "os_version": _f.get("os_version") or "",
                "os_label": _f.get("os_label") or "",
                "package_name": _f.get("package_name") or "",
                "package_id": _f.get("package_id") or "",
                "app_channel": _f.get("app_channel") or "",
                "runtime_version": _f.get("runtime_version") or "3.39.0",
                "chromium_version": _f.get("chromium_version") or "",
                "ssid": _f.get("ssid") or "un_support",
                "screen_inch": _f.get("screen_inch") or "",
                "login_start_ts": _f.get("login_start_ts") or "",
            })
        f = getattr(self, "fp", None) or {}
        if f:
            p.update({
                "device_id": f.get("device_id") or "",
                "iid": f.get("iid") or "",
                "install_id": f.get("install_id") or "",
                "openudid": f.get("openudid") or "",
                "web_region": f.get("region") or "HK",
                "verifyFp": f.get("verify_fp") or "",
                "s_v_web_id": f.get("verify_fp") or "",
                "webid": f.get("webid") or "",
                "ua": f.get("ua") or "",
            })
            if self.ms_token:
                p["msToken"] = self.ms_token
        # ★R215：复用 captcha-worker 浏览器通道成功发送时的那套参数（同会话，签名照常重算）
        if getattr(self, "_captured", None):
            for _k, _v in self._captured.items():
                if _k in ("sign", "qs", "ts", "aid"):
                    continue
                p[_k] = _v
            p["ts"] = sdk.daily_ts()
        return p

    def _headers(self, referer: str) -> dict:
        cks = {}
        try:
            cks = self.s.cookies.get_dict()
        except Exception:
            pass
        csrf = cks.get("passport_csrf_token") or cks.get("passport_csrf_token_default") or ""
        h = {
            "User-Agent": getattr(self, "ua", sdk.UA),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://accounts.doubao.com",
            "Referer": referer,
            "x-tt-passport-csrf-token": csrf,
            "x-tt-passport-trace-id": self.biz_trace_id,
        }
        # ★ R69：带浏览器指纹头（sec-ch-ua / sec-fetch-*），与 UA 自洽
        try:
            for k, v in _fpmod.headers(self.fp, referer).items():
                if k in ("Referer", "Accept-Language"):
                    continue
                h[k] = v
        except Exception:
            pass
        if self.ms_token:
            h["x-ms-token"] = self.ms_token
        return h

    def boot(self) -> dict:
        """先拿匿名 cookie（ttwid / passport_csrf_token 等），缺了 csrf 容易被风控。

        R69 起：先把整套指纹 cookie（device_id / s_v_web_id …）种进会话，再预热；
        并尽力取一次 msToken（拿不到也不阻断）。
        """
        sdk.seed_cookies(self.s, self.fp)
        for url, hdr in ((sdk.DOUBAO_HOST + "/chat/", {"User-Agent": self.ua, "Accept": "text/html"}),
                         (sdk.PASSPORT_HOST + "/?aid=%d" % self.aid,
                          {"User-Agent": self.ua, "Accept": "text/html"})):
            try:
                self.s.get(url, headers=hdr, timeout=self.timeout)
            except Exception as e:
                self.logger("boot %s 失败: %s" % (url, str(e)[:120]))
        try:
            self.ms_token = sdk.fetch_ms_token(self.s, self.fp) or ""
            if self.ms_token:
                self.logger("msToken 已获取 len=%d" % len(self.ms_token))
        except Exception:
            self.ms_token = ""
        return self.cookies()

    # ------------------------------------------------------------ 底层请求
    def request(self, path: str, body: dict | None = None, method: str = "POST") -> dict:
        p = self._base_params()
        body = body or {}
        sign, qs = sdk.sign_and_qs(p, body if method.upper() == "POST" else {})
        p["sign"] = sign
        p["qs"] = qs
        headers = self._headers("https://accounts.doubao.com/?aid=%d" % self.aid)
        headers["x-tt-passport-aid-sign"] = sdk.aid_sign(path, p["ts"], aid=self.aid)
        try:
            if method.upper() == "POST":
                r = self.s.post(sdk.PASSPORT_HOST + path, params=p, headers=headers,
                                data=sdk.obj_to_form(body), timeout=self.timeout)
            else:
                r = self.s.get(sdk.PASSPORT_HOST + path, params=p, headers=headers, timeout=self.timeout)
            try:
                return r.json()
            except Exception:
                return {"_http": r.status_code, "_text": (r.text or "")[:300], "message": "error"}
        except Exception as e:
            return {"_err": "%s: %s" % (type(e).__name__, str(e)[:180]), "_net": True, "message": "error"}

    # ------------------------------------------------------------ 业务动作
    def send_code(self, mobile: str, ctype: int = 24) -> dict:
        """发短信验证码。返回 {ok, message, error_code, need_qr, verify_subtype, has_conf, raw_keys, http_status, verify_event, note, log_id, retry_time, raw}。"""
        iso, dial, nat, ok = parse_mobile(mobile)
        if not ok:
            return {"ok": False, "error_code": 1003, "message": "手机号格式不正确，请重新填写",
                    "description": "手机号格式不正确，请重新填写", "raw": {}}
        data = {"mobile": "+%s %s" % (dial, nat), "type": int(ctype), "is6Digits": 1}
        resp = self.request(SEND_CODE, sdk.encrypt_params(data, ["mobile", "type"]))
        ok = (resp or {}).get("message") == "success"
        d = (resp or {}).get("data") or {}
        if not isinstance(d, dict):
            d = {}
        if ok:
            self.mobile = dial + nat
            self.iso = iso
            import time as _t
            self.sent_at = _t.time()
        _r = resp if isinstance(resp, dict) else {}
        _note = ""
        if not ok:
            # ★R82-M：失败一定留一行「信封形状」——events 的 note 由 send_fail_note() 生成
            #   （sessions 侧取 r["note"] 即落库）；同时写进文本日志，两条路都留痕。
            _note = send_fail_note(friendly_error(resp), resp, mask_any(mobile))
            try:
                self.logger("[发码诊断] %s %s" % (mask_any(mobile), diag_note(resp)))
            except Exception:
                pass
        return {"ok": ok, "mobile": mask_any(mobile), "error_code": _r.get("error_code"),
                "need_qr": (not ok) and needs_verify(resp),
                "verify_subtype": verify_subtype(resp),
                "has_conf": has_conf(resp),
                "raw_keys": sorted(str(k) for k in _r.keys()),
                "http_status": _r.get("_http"),
                "verify_event": str(_conf_dict(resp).get("verify_event") or ""),
                "note": _note,
                "message": "验证码已发送" if ok else friendly_error(resp),
                "log_id": d.get("log_id") or "", "retry_time": int(d.get("retry_time") or SEND_COOLDOWN),
                "raw": resp}

    def sms_login(self, mobile: str, code: str) -> dict:
        """用验证码登录（必须与 send_code 同一个实例）。返回 {ok, user_id, message, error_code, raw}。"""
        iso, dial, nat, ok = parse_mobile(mobile)
        c = re.sub(r"\D", "", str(code or ""))
        if not ok:
            return {"ok": False, "error_code": 1003, "message": "手机号格式不正确，请重新填写", "raw": {}}
        if not (4 <= len(c) <= 6):
            return {"ok": False, "error_code": 1202, "message": "请输入 6 位短信验证码", "raw": {}}
        data = {"mobile": "+%s %s" % (dial, nat), "code": c, "login_only": False, "ignore_lark_account": True}
        resp = self.request(SMS_LOGIN, sdk.encrypt_params(data, ["mobile", "code"]))
        d = (resp or {}).get("data") or {}
        ok = (resp or {}).get("message") == "success" or bool(d.get("user_id") or d.get("user_id_str"))
        return {"ok": ok, "mobile": mask_any(mobile),
                "user_id": str(d.get("user_id_str") or d.get("user_id") or ""),
                "error_code": (resp or {}).get("error_code"),
                "message": "登录成功" if ok else friendly_error(resp),
                "raw": resp}

    def probe(self) -> dict:
        """通道连通性自检（**绝不发短信**）：打 check_mobile_registered，只验证签名/参数/网络链路。
        用于部署后冒烟与排障。返回 {ok, error_code, message, raw}。"""
        data = {"mobile": "+86 13000000000", "type": 24}
        try:
            resp = self.request(CHECK_MOBILE_REGISTERED, sdk.encrypt_params(data, ["mobile", "type"]))
        except Exception as e:
            return {"ok": False, "error_code": None, "message": "%s: %s" % (type(e).__name__, str(e)[:160]), "raw": {}}
        err = resp.get("_err") if isinstance(resp, dict) else None
        return {"ok": not err, "error_code": (resp or {}).get("error_code") if isinstance(resp, dict) else None,
                "message": (resp or {}).get("description") or err or "OK", "raw": resp}

    def cookies(self) -> dict:
        try:
            return dict(self.s.cookies.get_dict())
        except Exception:
            return {}

    # ------------------------------------------------------------ ★R255 状态持久化
    def dump_state(self) -> dict:
        """序列化「已发码」会话（跨站点重启恢复用）。只存协议真正依赖的字段。"""
        cks = []
        try:
            # curl_cffi 的 Cookies 直接迭代得到的是 name 字符串，真正的 Cookie 对象在 .jar 里
            _jar = getattr(self.s.cookies, "jar", None) or self.s.cookies
            for ck in _jar:
                cks.append({"name": ck.name, "value": ck.value,
                            "domain": ck.domain or "", "path": ck.path or "/"})
        except Exception:
            pass
        return {"v": 1, "aid": self.aid, "pc": self.pc, "proxies": self.proxies,
                "fp": self.fp, "ms_token": self.ms_token, "biz_trace_id": self.biz_trace_id,
                "mobile": self.mobile, "iso": self.iso, "sent_at": self.sent_at,
                "captured": self._captured, "cookies": cks}

    @classmethod
    def from_state(cls, st: dict, logger=None):
        """从 dump_state() 的字典原样重建实例（cookie / 指纹 / msToken 全部还原）。"""
        try:
            c = cls(proxies=st.get("proxies"), aid=int(st.get("aid") or sdk.AID_PC),
                    pc=bool(st.get("pc", True)), logger=logger,
                    fp=st.get("fp") if isinstance(st.get("fp"), dict) else None)
        except Exception:
            return None
        try:
            for d in (st.get("cookies") or []):
                if not d.get("name"):
                    continue
                _dm = d.get("domain") or ""
                if _dm:
                    c.s.cookies.set(d.get("name"), d.get("value") or "",
                                    domain=_dm, path=(d.get("path") or "/"))
                else:
                    c.s.cookies.set(d.get("name"), d.get("value") or "")
        except Exception:
            pass
        c.ms_token = str(st.get("ms_token") or "")
        c.biz_trace_id = str(st.get("biz_trace_id") or c.biz_trace_id)
        c.mobile = str(st.get("mobile") or "")
        c.iso = str(st.get("iso") or "")
        c.sent_at = float(st.get("sent_at") or 0)
        c._captured = st.get("captured")
        return c


# 真身引用：离线单测里 phone_login.PhoneLoginClient 会被 conftest 的假客户端替换
REAL_CLIENT = PhoneLoginClient
