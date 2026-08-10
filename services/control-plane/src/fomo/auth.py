"""Small, dependency-free authentication primitives.

Passwords are stored as versioned, salted scrypt hashes. Session identifiers
are opaque random values persisted by the control plane, so logout can revoke
them server-side.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
_DUMMY_SALT = b"fomo-login-dummy"


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def new_session_id() -> str:
    """Return a 192-bit opaque token that fits the existing 36-char column."""
    return secrets.token_urlsafe(24)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return "$".join(
        (
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            _encode(salt),
            _encode(digest),
        )
    )


def verify_password(password: str, encoded: str | None) -> bool:
    parsed = _parse_hash(encoded) if encoded is not None else None
    if parsed is None:
        # Unknown users and corrupt legacy hashes still pay the same expensive
        # primitive, reducing account-enumeration timing differences.
        actual = _derive(password, _DUMMY_SALT, SCRYPT_N, SCRYPT_R, SCRYPT_P)
        hmac.compare_digest(actual, bytes(SCRYPT_DKLEN))
        return False
    n, r, p, salt, expected = parsed
    actual = _derive(password, salt, n, r, p)
    return hmac.compare_digest(actual, expected)


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=SCRYPT_DKLEN,
    )


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _parse_hash(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    try:
        algorithm, n_text, r_text, p_text, salt_text, digest_text = encoded.split("$")
        n, r, p = int(n_text), int(r_text), int(p_text)
        salt, digest = _decode(salt_text), _decode(digest_text)
    except (TypeError, ValueError):
        return None
    if (
        algorithm != "scrypt"
        or n != SCRYPT_N
        or r != SCRYPT_R
        or p != SCRYPT_P
        or len(salt) != 16
        or len(digest) != SCRYPT_DKLEN
    ):
        return None
    return n, r, p, salt, digest
