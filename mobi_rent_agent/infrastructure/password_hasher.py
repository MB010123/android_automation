"""Password hashing: Argon2id when the library is present, else stdlib scrypt."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

_HAS_ARGON2 = False
try:
    from argon2 import PasswordHasher as _Argon2Hasher
    from argon2.exceptions import VerifyMismatchError as _Argon2Mismatch

    _HAS_ARGON2 = True
    _ARGON2 = _Argon2Hasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32)
except ImportError:
    _ARGON2 = None
    _Argon2Mismatch = Exception  # type: ignore[misc,assignment]


def algorithm_name() -> str:
    return "argon2id" if _HAS_ARGON2 else "scrypt"


def hash_password(password: str) -> str:
    if _HAS_ARGON2:
        return _ARGON2.hash(password)
    salt = os.urandom(16)
    n, r, p = 2**14, 8, 1
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (
        n,
        r,
        p,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        secrets.compare_digest(b"0", b"1")
        return False
    if stored.startswith("$argon2"):
        if not _HAS_ARGON2:
            return False
        try:
            return bool(_ARGON2.verify(stored, password))
        except _Argon2Mismatch:
            return False
        except Exception:
            return False
    if stored.startswith("scrypt$"):
        parts = stored.split("$")
        if len(parts) != 6:
            return False
        try:
            n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
            salt = base64.b64decode(parts[4])
            expected = base64.b64decode(parts[5])
            digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected))
        except (ValueError, OSError):
            return False
        return hmac.compare_digest(digest, expected)
    return False
