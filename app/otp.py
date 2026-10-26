# -*- coding: utf-8 -*-
"""安全组件：TOTP（谷歌验证器 / Google Authenticator）+ 密码哈希 + 恢复码 + 二维码。

全部基于标准库实现（hmac/hashlib/base64/struct），不引入第三方依赖；
只有生成二维码图片时才会用到可选的 qrcode 库（装不上会自动降级为手填密钥）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from urllib.parse import quote

DIGITS = 6
PERIOD = 30
WINDOW = 1          # 允许前后各 1 个时间片（±30s 时钟漂移）
ISSUER = "豆包卡密后台"

# --------------------------------------------------------------------------- #
# TOTP
# --------------------------------------------------------------------------- #
def new_secret(nbytes: int = 20) -> str:
    """随机 Base32 密钥（160bit，Google Authenticator 兼容）。"""
    return base64.b32encode(os.urandom(nbytes)).decode().rstrip("=")


def _key(secret_b32: str) -> bytes:
    s = (secret_b32 or "").strip().upper().replace(" ", "")
    s += "=" * ((8 - len(s) % 8) % 8)
    return base64.b32decode(s, casefold=True)


def _hotp(secret_b32: str, counter: int, digits: int = DIGITS) -> str:
    h = hmac.new(_key(secret_b32), struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def code_at(secret_b32: str, at: float | None = None, offset: int = 0) -> str:
    t = int(at if at is not None else time.time())
    return _hotp(secret_b32, t // PERIOD + offset)


def match_counter(secret_b32: str, code: str, window: int = WINDOW, at: float | None = None) -> int:
    """★R211：返回命中的 TOTP 时间片序号；未命中返回 -1（供上层做重放拦截）。"""
    code = str(code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == DIGITS) or not secret_b32:
        return -1
    base = int(at if at is not None else time.time()) // PERIOD
    try:
        for off in range(-window, window + 1):
            if hmac.compare_digest(_hotp(secret_b32, base + off), code):
                return base + off
    except Exception:
        return -1
    return -1


def verify(secret_b32: str, code: str, window: int = WINDOW, at: float | None = None) -> bool:
    code = str(code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == DIGITS):
        return False
    if not secret_b32:
        return False
    try:
        for off in range(-window, window + 1):
            if hmac.compare_digest(code_at(secret_b32, at, off), code):
                return True
    except Exception:
        return False
    return False


def remaining_seconds(at: float | None = None) -> int:
    t = int(at if at is not None else time.time())
    return PERIOD - (t % PERIOD)


def provisioning_uri(secret_b32: str, account: str = "admin") -> str:
    """otpauth:// 链接：Noto/Google/Authy 等验证器都能扫。"""
    label = quote("%s:%s" % (ISSUER, account), safe="")
    return ("otpauth://totp/%s?secret=%s&issuer=%s&algorithm=SHA1&digits=%d&period=%d"
            % (label, secret_b32, quote(ISSUER, safe=""), DIGITS, PERIOD))


def qr_svg(text: str, box_size: int = 6, border: int = 2) -> str:
    """返回内联 SVG（失败返回空串，前端降级为手填密钥）。"""
    try:
        import qrcode                     # 可选依赖
        import qrcode.image.svg
        from io import BytesIO
        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage,
                          box_size=box_size, border=border)
        buf = BytesIO()
        img.save(buf)
        svg = buf.getvalue().decode("utf-8")
        i = svg.find("<svg")          # 去掉 XML 声明，否则内联进 HTML 会被当作文本
        return svg[i:] if i >= 0 else svg
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# 密码哈希（PBKDF2-HMAC-SHA256，随机盐，可升级迭代次数）
# --------------------------------------------------------------------------- #
ITERATIONS = 200_000


def hash_password(pwd: str, iterations: int = ITERATIONS) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt, iterations)
    return "pbkdf2$%d$%s$%s" % (
        iterations,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(stored: str, pwd: str) -> bool:
    try:
        algo, iters, salt_b64, dk_b64 = (stored or "").split("$")
        if algo != "pbkdf2":
            return False
        salt = base64.b64decode(salt_b64)
        want = base64.b64decode(dk_b64)
        got = hashlib.pbkdf2_hmac("sha256", (pwd or "").encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(got, want)
    except Exception:
        return False


def needs_rehash(stored: str) -> bool:
    try:
        algo, iters, _, _ = (stored or "").split("$")
        return algo != "pbkdf2" or int(iters) < ITERATIONS
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# 恢复码（一次性，存哈希）
# --------------------------------------------------------------------------- #
def new_recovery_codes(n: int = 10) -> list:
    out = []
    for _ in range(n):
        raw = secrets.token_hex(5).upper()
        out.append(raw[:5] + "-" + raw[5:])
    return out


def recovery_hash(code: str) -> str:
    c = (code or "").strip().upper().replace(" ", "").replace("-", "")
    return hashlib.sha256(("recovery|" + c).encode()).hexdigest()
