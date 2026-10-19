import hashlib
import hmac
import os
import secrets
import time

import jwt

SECRET = os.getenv("JWT_SECRET", "change-this-secret-in-production")


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2**14, r=8, p=1, dklen=64).hex()
    return f"{salt}:{h}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":")
    except ValueError:
        return False
    test = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2**14, r=8, p=1, dklen=64).hex()
    return hmac.compare_digest(test, h)


def sign_token(uid: int) -> str:
    return jwt.encode({"uid": uid, "exp": int(time.time()) + 30 * 86400}, SECRET, algorithm="HS256")


def decode_token(token: str | None) -> int | None:
    if not token:
        return None
    try:
        return jwt.decode(token, SECRET, algorithms=["HS256"])["uid"]
    except Exception:
        return None
