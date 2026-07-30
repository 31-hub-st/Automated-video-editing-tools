from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


PASSWORD_ITERATIONS = 600_000
PASSWORD_LENGTH = 8
PASSWORD_MIN_LENGTH = PASSWORD_LENGTH
PASSWORD_MAX_LENGTH = PASSWORD_LENGTH
DEFAULT_EMPLOYEE_PASSWORD = "xs123456"
DEFAULT_ADMIN_USERNAME = "storyforge-owner"
DEFAULT_ADMIN_PASSWORD = "xs123456"

# Used only to make an unknown account take the same PBKDF2 path as a known
# account.  It is not a usable account credential.
DUMMY_PASSWORD_VERIFIER = (
    "pbkdf2_sha256$600000$U3RvcnlGb3JnZUR1bW15IQ$"
    "_edNqnhU54wohHN2K3czftKyUYM99vywFF_ztDnX5lc"
)


def validate_new_password(password: str) -> str:
    value = str(password or "")
    if len(value) != PASSWORD_LENGTH:
        raise ValueError(f"密码必须恰好为 {PASSWORD_LENGTH} 个字符。")
    if any(not 0x21 <= ord(character) <= 0x7E for character in value):
        raise ValueError("密码只能使用可见 ASCII 字符，不能包含空格或中文。")
    return value


def hash_password(password: str) -> str:
    value = str(password or "")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", value.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    )


def password_matches(password: str, verifier: str) -> bool:
    try:
        scheme, raw_iterations, raw_salt, raw_digest = str(verifier or "").split(
            "$", 3
        )
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        if not 100_000 <= iterations <= 2_000_000:
            return False
        salt = base64.urlsafe_b64decode(raw_salt + "=" * (-len(raw_salt) % 4))
        expected = base64.urlsafe_b64decode(
            raw_digest + "=" * (-len(raw_digest) % 4)
        )
        actual = hashlib.pbkdf2_hmac(
            "sha256", str(password or "").encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


__all__ = [
    "DUMMY_PASSWORD_VERIFIER",
    "PASSWORD_ITERATIONS",
    "PASSWORD_LENGTH",
    "PASSWORD_MAX_LENGTH",
    "PASSWORD_MIN_LENGTH",
    "DEFAULT_ADMIN_PASSWORD",
    "DEFAULT_ADMIN_USERNAME",
    "DEFAULT_EMPLOYEE_PASSWORD",
    "hash_password",
    "password_matches",
    "validate_new_password",
]
