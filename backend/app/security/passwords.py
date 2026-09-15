"""密码哈希（PBKDF2-SHA256），无额外依赖。"""
from __future__ import annotations

import hashlib
import hmac
import secrets

_ITERATIONS = 390_000


def hash_password(plain: str) -> tuple[str, str]:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt.encode("ascii"), _ITERATIONS)
    return salt, dk.hex()


def verify_password(plain: str, salt: str, hash_hex: str) -> bool:
    if not salt or not hash_hex:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt.encode("ascii"), _ITERATIONS)
    return hmac.compare_digest(dk.hex(), hash_hex)
