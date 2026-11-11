"""Passwords, sessions, CSRF and encryption at rest."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

from creator_office.database import utcnow
from creator_office.settings import get_settings

logger = logging.getLogger(__name__)

SESSION_COOKIE = "co_session"
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_LEN = 2**14, 8, 1, 32


# --- Passwords ---------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_LEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


# --- Session tokens ----------------------------------------------------------


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_expiry() -> datetime:
    return utcnow() + timedelta(hours=get_settings().session_ttl_hours)


def build_session_cookie(
    token: str, *, secure_request: bool, clear: bool = False
) -> str:
    """Serialise the session cookie by hand.

    ``Partitioned`` (CHIPS) keeps the cookie usable when the app is embedded in
    a third-party iframe, and Python's cookie module only knows that attribute
    from 3.14 on.
    """
    settings = get_settings()
    secure = {"true": True, "false": False}.get(
        settings.session_cookie_secure, secure_request
    )
    same_site = settings.session_cookie_samesite
    if same_site == "none":
        secure = True  # browsers reject SameSite=None without Secure
    parts = [
        f"{SESSION_COOKIE}={'' if clear else token}",
        "Path=/",
        "HttpOnly",
        f"SameSite={same_site.capitalize()}",
    ]
    parts.append(
        "Max-Age=0" if clear else f"Max-Age={settings.session_ttl_hours * 3600}"
    )
    if secure:
        parts.append("Secure")
        if settings.session_cookie_partitioned:
            parts.append("Partitioned")
    return "; ".join(parts)


# --- Login throttling --------------------------------------------------------


class LoginThrottle:
    """Small in-memory limiter: N failures per window per (ip, email)."""

    def __init__(self, max_failures: int = 8, window_seconds: int = 300) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        entries = self._failures[key]
        while entries and now - entries[0] > self.window:
            entries.popleft()
        return entries

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, time.monotonic())) >= self.max_failures

    def fail(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


login_throttle = LoginThrottle()


# --- Encryption at rest ------------------------------------------------------

_fernet: Fernet | None = None
_secret: str = ""
_fernet_lock = threading.Lock()


def _load_secret() -> str:
    settings = get_settings()
    key = settings.app_secret_key.strip()
    if not key:
        if settings.is_production:
            raise RuntimeError(
                "APP_SECRET_KEY is required in production. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
        key_file = settings.data_dir / "secret.key"
        if key_file.exists():
            key = key_file.read_text(encoding="utf-8").strip()
        else:
            key = Fernet.generate_key().decode()
            key_file.write_text(key, encoding="utf-8")
            key_file.chmod(0o600)
            logger.warning(
                "APP_SECRET_KEY not set: generated a development key at %s", key_file
            )
    return key


def fernet() -> Fernet:
    global _fernet, _secret
    with _fernet_lock:
        if _fernet is None:
            _secret = _load_secret()
            try:
                _fernet = Fernet(_secret.encode("utf-8"))
            except ValueError as exc:
                raise RuntimeError(
                    "APP_SECRET_KEY is not a valid key. Generate one with: "
                    'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
                ) from exc
        return _fernet


def derive_key(purpose: str) -> bytes:
    """A separate signing key per purpose, derived from the app secret."""
    fernet()
    return hmac.new(
        _secret.encode("utf-8"), purpose.encode("utf-8"), hashlib.sha256
    ).digest()


def reset_for_tests() -> None:
    global _fernet, _secret
    with _fernet_lock:
        _fernet, _secret = None, ""


def encrypt_json(data: dict[str, Any]) -> str:
    return fernet().encrypt(json.dumps(data).encode("utf-8")).decode("utf-8")


def decrypt_json(blob: str) -> dict[str, Any]:
    if not blob:
        return {}
    try:
        return dict(json.loads(fernet().decrypt(blob.encode("utf-8"))))
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError(
            "Stored credentials cannot be decrypted with the current APP_SECRET_KEY"
        ) from exc


def mask_secret(value: str) -> str:
    if not value:
        return ""
    return "••••" + value[-4:] if len(value) > 8 else "••••"


# --- Outbound URL guard (SSRF) ----------------------------------------------


class UnsafeUrlError(ValueError):
    pass


def assert_public_url(url: str) -> str:
    """Reject URLs that are not plain http(s) to a public address."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeUrlError("Solo se admiten direcciones http(s).")
    try:
        infos = socket.getaddrinfo(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"No se pudo resolver {parsed.hostname}.") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise UnsafeUrlError(
                "La dirección apunta a una red interna y no se consulta."
            )
    return parsed.geturl()
