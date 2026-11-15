"""Подпись запросов бот -> агент (HMAC-SHA256). Файл одинаков в agent/ и bot/."""
import hashlib
import hmac
import secrets
import time


def sign(secret: str, method: str, path: str, ts: str, nonce: str, body: bytes) -> str:
    msg = "\n".join([method.upper(), path, ts, nonce, hashlib.sha256(body).hexdigest()])
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def make_headers(secret: str, method: str, path: str, body: bytes = b"") -> dict:
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    return {
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Signature": sign(secret, method, path, ts, nonce, body),
    }
