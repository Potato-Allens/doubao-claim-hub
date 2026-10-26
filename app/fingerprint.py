# -*- coding: utf-8 -*-
"""R69/R69-BG 设备指纹 / UA 档案：给抖音 passport 请求一套像真机、且内部自洽的参数。

为什么需要（实测取证 2026-10-13）
------------------
向 +852 号码发验证码时，抖音 passport 下发了鲨鱼验证中心人机验证：
    {"error_code":1105/1107,"verify_center_decision_conf":"{...from:shark_admin,verify_event:tt_send_sms_login...}"}
而原先的请求很裸：
  * device_id = ""（空），iid / install_id / openudid 全缺
  * 预热后 cookie 只有 ttwid，没有 msToken / s_v_web_id
  * 每个会话都报同一套 hardwareConcurrency / webgl / screen（风控最爱聚类）

本模块生成每会话一套、互相不矛盾的档案：
    UA <-> 浏览器 <-> sec-ch-ua <-> 平台 <-> webgl <-> screen <-> dpr <-> 时区 <-> 语言 <-> 硬件。

R69-BG（本轮）修正的短板
--------------------
上一版 6 个档案里 4 个 Windows 档案的 UA 字符串**完全相同**（都是 Chrome/147.0.7727.149），
即 "UA 维度" 其实只有 Win / macOS 两档。现在改成：
    * 浏览器：Chrome 143/144/145/146/147 + Edge 146/147 + Firefox 133（无 sec-ch-ua）
    * 系统：Windows 10/11、macOS（Intel/M 系列）、Ubuntu Linux
    * 地区：语言 / 时区 / Accept-Language 与号码国别或出口国别一致
"""
from __future__ import annotations

import base64
import hashlib
import random
import re
import string
import time
import uuid

# 与 BIZ_Q 里的 chromium_version 保持一致的主流构建号（真实存在的相邻版本）
CHROME_BUILD = "147.0.7727.149"          # 兼容旧引用（默认最新档）
CHROME_MAJOR = "147"

CHROME_BUILDS = [
    ("143", "143.0.7499.169"),
    ("144", "144.0.7559.132"),
    ("145", "145.0.7632.159"),
    ("146", "146.0.7680.165"),
    ("147", "147.0.7727.149"),
]
EDGE_BUILDS = [
    ("146", "146.0.7680.80"),
    ("147", "147.0.7727.101"),
]
FIREFOX_VERS = ["132.0", "133.0", "134.0"]

# 地区 -> (主语言, Accept-Language, 时区, 时区偏移分钟)
REGION_LOCALE = {
    "HK": ("zh-HK", "zh-HK,zh;q=0.9,en;q=0.8", "Asia/Hong_Kong", 480),
    "CN": ("zh-CN", "zh-CN,zh;q=0.9,en;q=0.8", "Asia/Shanghai", 480),
    "TW": ("zh-TW", "zh-TW,zh;q=0.9,en;q=0.8", "Asia/Taipei", 480),
    "MO": ("zh-MO", "zh-MO,zh;q=0.9,en;q=0.8", "Asia/Macau", 480),
    "SG": ("en-SG", "en-SG,en;q=0.9,zh;q=0.8", "Asia/Singapore", 480),
    "JP": ("ja-JP", "ja-JP,ja;q=0.9,en;q=0.8", "Asia/Tokyo", 540),
    "KR": ("ko-KR", "ko-KR,ko;q=0.9,en;q=0.8", "Asia/Seoul", 540),
    "US": ("en-US", "en-US,en;q=0.9", "America/Los_Angeles", -480),
    "CA": ("en-CA", "en-CA,en;q=0.9,fr;q=0.8", "America/Toronto", -300),
    "GB": ("en-GB", "en-GB,en;q=0.9", "Europe/London", 0),
    "DE": ("de-DE", "de-DE,de;q=0.9,en;q=0.8", "Europe/Berlin", 60),
    "FR": ("fr-FR", "fr-FR,fr;q=0.9,en;q=0.8", "Europe/Paris", 60),
    "NL": ("nl-NL", "nl-NL,nl;q=0.9,en;q=0.8", "Europe/Amsterdam", 60),
    "AU": ("en-AU", "en-AU,en;q=0.9", "Australia/Sydney", 600),
    "MY": ("ms-MY", "ms-MY,en;q=0.9", "Asia/Kuala_Lumpur", 480),
    "TH": ("th-TH", "th-TH,th;q=0.9,en;q=0.8", "Asia/Bangkok", 420),
    "VN": ("vi-VN", "vi-VN,vi;q=0.9,en;q=0.8", "Asia/Ho_Chi_Minh", 420),
    "IN": ("en-IN", "en-IN,en;q=0.9", "Asia/Kolkata", 330),
    "BR": ("pt-BR", "pt-BR,pt;q=0.9,en;q=0.8", "America/Sao_Paulo", -180),
}

# 硬件档（互相自洽：OS + GPU + 屏幕 + dpr + 核数 + 内存 + 插件数）
HW_PROFILES = [
    {   # Win 11 + NVIDIA 独显 1080p
        "os": "Windows", "os_ver": "Windows NT 10.0; Win64; x64", "plat": "Win32", "sec_plat": "\"Windows\"",
        "hw": 8, "mem": 8, "screen": (1920, 1080), "outer": (1920, 1040), "inner": (1536, 864),
        "dpr": 1, "depth": 24, "plugins": 5,
        "wgl_v": "Google Inc. (NVIDIA)",
        "wgl_r": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002504) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {   # Win 10 + Intel 核显 1080p
        "os": "Windows", "os_ver": "Windows NT 10.0; Win64; x64", "plat": "Win32", "sec_plat": "\"Windows\"",
        "hw": 4, "mem": 8, "screen": (1920, 1080), "outer": (1920, 1040), "inner": (1536, 864),
        "dpr": 1.25, "depth": 24, "plugins": 3,
        "wgl_v": "Google Inc. (Intel)",
        "wgl_r": "ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E9B) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {   # Win 11 + AMD 2K 高刷
        "os": "Windows", "os_ver": "Windows NT 10.0; Win64; x64", "plat": "Win32", "sec_plat": "\"Windows\"",
        "hw": 16, "mem": 16, "screen": (2560, 1440), "outer": (2560, 1400), "inner": (2048, 1152),
        "dpr": 1, "depth": 24, "plugins": 5,
        "wgl_v": "Google Inc. (AMD)",
        "wgl_r": "ANGLE (AMD, AMD Radeon RX 6600 (0x000073FF) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {   # Win 10 老笔记本 768p
        "os": "Windows", "os_ver": "Windows NT 10.0; Win64; x64", "plat": "Win32", "sec_plat": "\"Windows\"",
        "hw": 4, "mem": 4, "screen": (1366, 768), "outer": (1366, 728), "inner": (1349, 649),
        "dpr": 1, "depth": 24, "plugins": 3,
        "wgl_v": "Google Inc. (Intel)",
        "wgl_r": "ANGLE (Intel, Intel(R) HD Graphics 4000 (0x00000166) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {   # Win 11 + NVIDIA 4K
        "os": "Windows", "os_ver": "Windows NT 10.0; Win64; x64", "plat": "Win32", "sec_plat": "\"Windows\"",
        "hw": 12, "mem": 16, "screen": (3840, 2160), "outer": (3840, 2120), "inner": (2560, 1400),
        "dpr": 1.5, "depth": 24, "plugins": 5,
        "wgl_v": "Google Inc. (NVIDIA)",
        "wgl_r": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 (0x00002786) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {   # macOS M2 14 英寸
        "os": "macOS", "os_ver": "Macintosh; Intel Mac OS X 10_15_7", "plat": "MacIntel", "sec_plat": "\"macOS\"",
        "hw": 8, "mem": 8, "screen": (1512, 982), "outer": (1512, 944), "inner": (1512, 855),
        "dpr": 2, "depth": 30, "plugins": 5,
        "wgl_v": "Google Inc. (Apple)",
        "wgl_r": "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
    },
    {   # macOS M3 16 英寸
        "os": "macOS", "os_ver": "Macintosh; Intel Mac OS X 10_15_7", "plat": "MacIntel", "sec_plat": "\"macOS\"",
        "hw": 12, "mem": 16, "screen": (1728, 1117), "outer": (1728, 1079), "inner": (1728, 989),
        "dpr": 2, "depth": 30, "plugins": 5,
        "wgl_v": "Google Inc. (Apple)",
        "wgl_r": "ANGLE (Apple, ANGLE Metal Renderer: Apple M3 Pro, Unspecified Version)",
    },
    {   # macOS Intel i7 13 英寸
        "os": "macOS", "os_ver": "Macintosh; Intel Mac OS X 10_15_7", "plat": "MacIntel", "sec_plat": "\"macOS\"",
        "hw": 8, "mem": 16, "screen": (1440, 900), "outer": (1440, 862), "inner": (1440, 776),
        "dpr": 2, "depth": 30, "plugins": 5,
        "wgl_v": "Google Inc. (Intel Inc.)",
        "wgl_r": "ANGLE (Intel Inc., Intel(R) Iris(TM) Plus Graphics 640, OpenGL 4.1)",
    },
    {   # Ubuntu 24.04 + NVIDIA
        "os": "Linux", "os_ver": "X11; Linux x86_64", "plat": "Linux x86_64", "sec_plat": "\"Linux\"",
        "hw": 8, "mem": 8, "screen": (1920, 1080), "outer": (1920, 1055), "inner": (1920, 969),
        "dpr": 1, "depth": 24, "plugins": 3,
        "wgl_v": "Google Inc. (NVIDIA Corporation)",
        "wgl_r": "ANGLE (NVIDIA Corporation, NVIDIA GeForce GTX 1660/PCIe/SSE2, OpenGL 4.5.0)",
    },
    {   # Ubuntu 22.04 + Mesa
        "os": "Linux", "os_ver": "X11; Linux x86_64", "plat": "Linux x86_64", "sec_plat": "\"Linux\"",
        "hw": 4, "mem": 8, "screen": (1600, 900), "outer": (1600, 875), "inner": (1600, 789),
        "dpr": 1, "depth": 24, "plugins": 3,
        "wgl_v": "Google Inc. (Mesa)",
        "wgl_r": "ANGLE (Mesa, Mesa Intel(R) UHD Graphics 620 (KBL GT2), OpenGL 4.6 (Core Profile))",
    },
]

_ALNUM = string.ascii_lowercase + string.digits
_HEXU = "0123456789ABCDEF"

# ---------------------------------------------------------------- R82-K PC 客户端身份
# 对齐竞品实测（work/r81_rival/t2_sendsms_full.json + FINDINGS.md）：竞品冒充的是
# 「豆包电脑版客户端」（DoubaoWork / SamanthaDoubao/2.31.4），随包报一整套 PC 设备身份。
# 这里按同样的结构规律补出来，且与 web 侧指纹自洽（OS/屏幕/显卡/内存同一档）。
PC_PACKAGE_NAME = "DoubaoWork_online_installer_2.25.18"
PC_PACKAGE_ID = "com.bot.pc.doubaowork"
PC_APP_CHANNEL = "1001"
PC_VERSION = "2.31.4"
PC_VERSION_CODE = "20800"
PC_RUNTIME_VERSION = "3.39.0"
PC_AID = "582478"
PC_CHROMIUM_VERSION = "146.0.7680.153"
PC_SSID = "un_support"
PC_CLIENT_PLATFORM = "pc_client"

# PC 主板型号（与竞品 device_model 同族）
PC_MODELS = ["PRIME B760M-A", "B660M DS3H DDR4", "PRIME Z690-P WIFI D4", "B550 AORUS ELITE",
             "MAG B660M MORTAR WIFI DDR4", "TUF GAMING B550M-PLUS", "Z790 AORUS ELITE AX",
             "ROG STRIX B760-F GAMING WIFI", "X670E AORUS MASTER", "Z590-A PRO"]

# 各 OS 档的 os_name / device_type / channel（与 UA、webgl 自洽）
PC_OS_META = {
    "Windows": {"os_name": "Windows NT", "device_type": "windows_pc", "channel": "win",
                "versions": ["10.0.26100", "10.0.26200", "10.0.22631", "10.0.19045"],
                "labels": ["Windows 11 24H2", "Windows 11 25H2", "Windows 11 23H2", "Windows 10 22H2"]},
    "macOS": {"os_name": "macOS", "device_type": "macos_pc", "channel": "mac",
              "versions": ["14.6", "15.1"], "labels": ["macOS 14.6 Sonoma", "macOS 15.1 Sequoia"]},
    "Linux": {"os_name": "Linux", "device_type": "linux_pc", "channel": "linux",
              "versions": ["24.04", "22.04"], "labels": ["Ubuntu 24.04 LTS", "Ubuntu 22.04 LTS"]},
}


def _rng(rng=None):
    """统一的随机源：传了 Random 实例就用它（可复现），否则用全局 random（旧行为）。"""
    return rng if rng is not None else random


def _rand(n: int, alphabet: str = string.digits, rng=None) -> str:
    r = _rng(rng)
    return "".join(r.choice(alphabet) for _ in range(n))


def _seed_ts(seed) -> int:
    """由 seed 推出一个稳定的毫秒时间戳（只为「同 seed 同结果」，不追求真实时钟）。"""
    h = hashlib.sha256(("%s" % seed).encode("utf-8")).hexdigest()
    return 1700000000000 + (int(h[:12], 16) % 400000000)


def gen_device_id(rng=None, ts_ms=None) -> str:
    """19 位雪花 ID 形状的 device_id（真机也是 19 位纯数字）。"""
    r = _rng(rng)
    ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
    head = r.choice("67") + _rand(1, rng=r)        # 2 位
    mid = str(ts)[-9:]                             # 9 位（时间戳尾）
    return head + mid + _rand(8, rng=r)            # 2+9+8 = 19 位


def gen_iid(did=None, rng=None) -> str:
    """install_id / iid：官方规律 = device_id + 4096（多枚真实样本验证）。"""
    if did:
        try:
            return str(int(did) + 4096)
        except Exception:
            pass
    return gen_device_id(rng)


def gen_install_id(did=None, rng=None) -> str:
    return gen_iid(did, rng)


def gen_openudid(rng=None) -> str:
    r = _rng(rng)
    return "-".join([_rand(8, string.hexdigits.lower(), rng=r), _rand(4, string.hexdigits.lower(), rng=r),
                     _rand(4, string.hexdigits.lower(), rng=r), _rand(4, string.hexdigits.lower(), rng=r),
                     _rand(12, string.hexdigits.lower(), rng=r)])


def gen_verify_fp(rng=None, ts_ms=None) -> str:
    """s_v_web_id / verifyFp：真机形如 verify_xxxxxxxx_XXXXXXXX_xxxx_xxxx_xxxx。"""
    r = _rng(rng)
    ts = ts_ms if ts_ms is not None else time.time() * 1000
    p = [base36(ts), _rand(6, _ALNUM + string.ascii_uppercase, rng=r), _rand(4, _ALNUM, rng=r),
         _rand(4, _ALNUM + string.ascii_uppercase, rng=r), _rand(4, _ALNUM, rng=r)]
    return "verify_" + "_".join(p)


def gen_snowflake19(rng=None) -> str:
    """19 位十进制「web id」形状（web_id / tea_web_id / zijie_web_id 真机都是 19 位）。"""
    r = _rng(rng)
    return r.choice("6789") + _rand(18, rng=r)


def gen_webid(rng=None) -> str:
    return gen_snowflake19(rng)


def gen_mac(rng=None) -> str:
    """6 组大写十六进制 MAC（XX:XX:XX:XX:XX:XX）。"""
    r = _rng(rng)
    return ":".join("%02X" % r.randint(0, 255) for _ in range(6))


def gen_pc_uuid(rng=None, ts_ms=None) -> str:
    """PC 设备 UUID：doubaowork_ + 大写 UUIDv1（版本位=1；node 随机且多播位=1，不泄露宿主 MAC）。"""
    r = _rng(rng)
    ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
    ticks = ts * 10000 + r.randint(0, 9999)          # 100ns 精度（UUIDv1 计时单位）
    time_low = ticks & 0xFFFFFFFF
    time_mid = (ticks >> 32) & 0xFFFF
    time_hi = ((ticks >> 48) & 0x0FFF) | 0x1000      # version nibble = 1
    clock_seq = r.getrandbits(14) | 0x8000           # variant = 10xx
    node = r.getrandbits(48) | 0x010000000000        # 多播位=1（非真实网卡）
    u = uuid.UUID(fields=(time_low, time_mid, time_hi, clock_seq >> 8, clock_seq & 0xFF, node))
    return "doubaowork_" + str(u).upper()


def gen_pc_serial(mac, rng=None) -> str:
    """PC 序列号：doubaowork_ + 32 hex（8 组 4 位，下划线分隔，尾部一个点）。
    竞品规律：末 12 个 hex 恰好等于 MAC（去冒号）。"""
    r = _rng(rng)
    mac_hex = re.sub(r"[^0-9A-Fa-f]", "", str(mac or "")).upper().rjust(12, "0")[-12:]
    body = "".join(r.choice(_HEXU) for _ in range(32 - 12)) + mac_hex
    return "doubaowork_" + "_".join(body[i:i + 4] for i in range(0, 32, 4)) + "."


def gen_device_token(rng=None) -> str:
    """设备令牌：78 字节 -> base32（去 padding）= 125 字符；头 2 字节固定 00 01。"""
    r = _rng(rng)
    raw = b"\x00\x01" + bytes(r.randint(0, 255) for _ in range(76))   # 2 + 76 = 78 B
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def gen_uuid4(rng=None) -> str:
    """小写 UUIDv4（cdid / clientudid / web_tab_id）。"""
    r = _rng(rng)
    return str(uuid.UUID(int=r.getrandbits(128), version=4))


def gen_device_name(osname="Windows", rng=None) -> str:
    r = _rng(rng)
    if osname == "macOS":
        return "MacBook-Pro-" + _rand(4, string.ascii_uppercase + string.digits, rng=r)
    if osname == "Linux":
        return "ubuntu-" + _rand(6, string.ascii_lowercase + string.digits, rng=r)
    alnum = string.ascii_uppercase + string.digits
    if r.random() < 0.5:
        return "DESKTOP-" + _rand(7, alnum, rng=r)
    return "WIN-" + _rand(8, alnum, rng=r)


def screen_inch_for(width, rng=None) -> float:
    """屏幕尺寸（英寸）粗估：按横向像素落档，与竞品同族（15.6 / 27.0 …）。"""
    r = _rng(rng)
    try:
        w = int(width)
    except Exception:
        w = 1920
    if w <= 1440:
        opts = [13.3, 14.0, 14.2, 15.6]
    elif w <= 1920:
        opts = [21.5, 23.8, 24.0, 27.0]
    elif w <= 2560:
        opts = [27.0, 28.0, 31.5]
    else:
        opts = [31.5, 32.0, 34.0]
    return r.choice(opts)


def base36(n) -> str:
    n = int(n)
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = _ALNUM[r] + out
    return out or "0"


def _pick_browser(rng=None) -> dict:
    """Chrome 143~147 / Edge / Firefox 混抽（默认偏向 Chrome，最像普通用户）。"""
    r = _rng(rng)
    k = r.random()
    if k < 0.70:
        major, build = r.choice(CHROME_BUILDS)
        return {"browser": "chrome", "brand": "Google Chrome", "build": build, "major": major}
    if k < 0.88:
        major, build = r.choice(EDGE_BUILDS)
        return {"browser": "edge", "brand": "Microsoft Edge", "build": build, "major": major}
    ver = r.choice(FIREFOX_VERS)
    return {"browser": "firefox", "brand": None, "build": ver, "major": ver.split(".")[0]}


def _ua(b: dict, hw: dict) -> str:
    if b["browser"] == "firefox":
        tpl = ("Mozilla/5.0 (%s; rv:%s) Gecko/20100101 Firefox/%s"
               if hw["os"] != "macOS" else "Mozilla/5.0 (%s; rv:%s) Gecko/20100101 Firefox/%s")
        return tpl % (hw["os_ver"], b["build"], b["build"])
    tail = " Safari/537.36" if b["browser"] == "chrome" else " Safari/537.36 Edg/%s" % b["build"]
    return ("Mozilla/5.0 (%s) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%s%s"
            % (hw["os_ver"], b["build"], tail))


def _sec_ch_ua(b: dict) -> str:
    if b["browser"] == "firefox":
        return ""                                  # Firefox 根本不发 sec-ch-ua
    return ('"%s";v="%s", "Chromium";v="%s", "Not_A Brand";v="24"'
            % (b["brand"], b["major"], b["major"]))


def locale_of(region: str):
    r = str(region or "HK").upper()
    if r == "UK":
        r = "GB"
    return REGION_LOCALE.get(r) or REGION_LOCALE["HK"]


def pick(region: str = "HK", seed=None) -> dict:
    """抽一套自洽的档案（每会话调一次）。region 决定 语言 / 时区 / Accept-Language。

    传 seed（int / str）→ 完全确定性：同一 seed 每次得到同一套档案（含 PC 客户端字段），
    便于回归测试与线上问题复现；不传 seed 则走全局随机（与旧版行为一致）。
    """
    rnd = random.Random(seed) if seed is not None else random
    ts_ms = _seed_ts(seed) if seed is not None else int(time.time() * 1000)
    b = _pick_browser(rnd)
    hw = dict(rnd.choice(HW_PROFILES))
    lang, accept_lang, tz, tz_off = locale_of(region)
    fp = dict(hw)
    # —— 先定设备身份（PC 侧字段都挂在这一套身份上） ——
    did = gen_device_id(rnd, ts_ms)
    iid = str(int(did) + 4096)                       # ★ 官方规律：iid = device_id + 4096
    mac = gen_mac(rnd)
    scr = hw.get("screen") or (1920, 1080)
    osname = hw.get("os") or "Windows"
    meta = PC_OS_META.get(osname) or PC_OS_META["Windows"]
    fp.update({
        "region": str(region or "HK").upper(),
        "browser": b["browser"],
        "browser_build": b["build"],
        "browser_major": b["major"],
        "ua": _ua(b, hw),
        "sec_ch_ua": _sec_ch_ua(b),
        "sec_ch_ua_mobile": "?0",
        "sec_plat": hw["sec_plat"],
        "lang": lang,
        "langs": [lang] + (["en"] if not lang.startswith("en") else []),
        "accept_language": accept_lang,
        "tz": tz,
        "tz_offset": tz_off,
        # ---------- web 侧（老字段，一个都不少，保持向后兼容） ----------
        "device_id": did,
        "iid": iid,
        "install_id": iid,
        "openudid": gen_openudid(rnd),
        "verify_fp": gen_verify_fp(rnd, ts_ms),
        "webid": gen_snowflake19(rnd),
        # ---------- R82-K：PC 客户端身份（对齐竞品 43 字段） ----------
        "fp": "verify_" + did,                       # ★ fp = "verify_" + device_id
        "mac": mac,
        "pc_uuid": gen_pc_uuid(rnd, ts_ms),          # doubaowork_ + UUIDv1 大写
        "pc_serial": gen_pc_serial(mac, rnd),        # doubaowork_ + 32hex（末 12 = MAC）
        "device_token": gen_device_token(rnd),       # base32 125 字符 -> 78B（头 00 01）
        "clientudid": gen_uuid4(rnd),
        "cdid": gen_uuid4(rnd),
        "tea_uuid": did,                             # tea_uuid = device_id
        "web_id": gen_snowflake19(rnd),
        "tea_web_id": gen_snowflake19(rnd),
        "zijie_web_id": gen_snowflake19(rnd),
        "web_tab_id": gen_uuid4(rnd),
        "device_name": gen_device_name(osname, rnd),
        "device_model": rnd.choice(PC_MODELS),
        "device_type": meta["device_type"],
        "channel": meta["channel"],
        "client_platform": PC_CLIENT_PLATFORM,
        "os_name": meta["os_name"],
        "os_version": rnd.choice(meta["versions"]),
        "os_label": rnd.choice(meta["labels"]),
        "package_name": PC_PACKAGE_NAME,
        "package_id": PC_PACKAGE_ID,
        "app_channel": PC_APP_CHANNEL,
        "pc_version": PC_VERSION,
        "version_code": PC_VERSION_CODE,
        "runtime_version": PC_RUNTIME_VERSION,
        "aid": PC_AID,
        "chromium_version": (b["build"] if b["browser"] in ("chrome", "edge") else PC_CHROMIUM_VERSION),
        "ssid": PC_SSID,
        "biz_trace_id": _rand(8, "0123456789abcdef", rng=rnd),
        "login_start_ts": ts_ms,
        "screen_inch": screen_inch_for(scr[0], rnd),
        "screen_width": scr[0],
        "screen_height": scr[1],
        "device_scale": hw.get("dpr") or 1,
        "hardware_concurrency": hw.get("hw") or 8,
        "device_memory": hw.get("mem") or 8,
        "webgl_vendor": hw.get("wgl_v") or "",
        "webgl_renderer": hw.get("wgl_r") or "",
    })
    # 每个会话独立的 canvas / audio 指纹种子（真机各不相同）
    fp["canvas_seed"] = rnd.randint(10 ** 6, 10 ** 7 - 1)
    fp["audio_seed"] = rnd.randint(10 ** 6, 10 ** 7 - 1)
    fp["fonts_seed"] = rnd.randint(1, 2 ** 31 - 1)
    fp["vendor"] = ("Google Inc." if b["browser"] != "firefox"
                    else "")
    fp["product_sub"] = "20030107" if b["browser"] != "firefox" else "20100101"
    return fp

def headers(fp: dict, referer: str = "https://www.doubao.com/") -> dict:
    """与指纹一致的浏览器请求头（Firefox 档不带 sec-ch-ua，与真机一致）。"""
    fp = fp or {}
    h = {
        "User-Agent": fp.get("ua") or "",
        "Accept-Language": fp.get("accept_language") or ("%s,zh;q=0.9,en;q=0.8" % (fp.get("lang") or "zh-CN")),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "Referer": referer,
    }
    if fp.get("sec_ch_ua"):
        h["sec-ch-ua"] = fp["sec_ch_ua"]
        h["sec-ch-ua-mobile"] = fp.get("sec_ch_ua_mobile") or "?0"
        h["sec-ch-ua-platform"] = fp.get("sec_plat") or "\"Windows\""
    return h
