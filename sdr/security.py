"""Senhas, tokens de sessão, criptografia de credenciais e hash de telefones (supressão)."""
import base64
import hashlib
import hmac
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

from .config import cfg


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(cfg.SECRET_KEY.encode()).digest())
    return Fernet(key)


def encrypt(s: str | None) -> str | None:
    return _fernet().encrypt(s.encode()).decode() if s else None


def decrypt(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return _fernet().decrypt(s.encode()).decode()
    except InvalidToken:
        return None


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, s, h = stored.split("$")
        salt, expected = base64.b64decode(s), base64.b64decode(h)
        got = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got, expected)
    except Exception:
        return False


def new_token() -> tuple[str, str]:
    t = secrets.token_urlsafe(32)
    return t, hashlib.sha256(t.encode()).hexdigest()


def token_hash(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def phone_hash(e164: str) -> str:
    """Hash HMAC do telefone: permite manter a supressão mesmo após apagar o lead (LGPD)."""
    return hmac.new(cfg.PHONE_PEPPER.encode(), e164.encode(), hashlib.sha256).hexdigest()


def mask_phone(e164: str) -> str:
    d = "".join(ch for ch in e164 if ch.isdigit())
    return f"+{d[:2]} {d[2:4]} ****-{d[-4:]}" if len(d) >= 8 else "****"
