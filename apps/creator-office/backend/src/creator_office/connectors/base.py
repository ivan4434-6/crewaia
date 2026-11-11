"""Common pieces for the platform connectors.

Each platform has its own connector module with its own OAuth client, scopes
and endpoints. They only ever talk to the platform's official API, and they
report what the platform answered: a post is "published" when the API returns
an identifier for it, never before.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from creator_office.database import utcnow
from creator_office.net import http_client

ProgressFn = Callable[[float, str], None]
HTTP_TIMEOUT = httpx.Timeout(60.0, connect=15.0)


class ConnectorError(RuntimeError):
    """A platform call failed. ``user_message`` is safe to show in the UI."""

    def __init__(
        self,
        user_message: str,
        *,
        code: str = "platform_error",
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


class NotConnectedError(ConnectorError):
    def __init__(self, user_message: str = "La cuenta no está conectada.") -> None:
        super().__init__(user_message, code="not_connected")


class AuthExpiredError(ConnectorError):
    def __init__(
        self,
        user_message: str = "La autorización ha caducado o se ha revocado. Vuelve a conectar la cuenta.",
    ) -> None:
        super().__init__(user_message, code="auth_expired")


class RateLimitedError(ConnectorError):
    def __init__(self, user_message: str, retry_after: int | None = None) -> None:
        super().__init__(
            user_message, code="rate_limited", retryable=True, retry_after=retry_after
        )


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str = ""
    expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None
    scopes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_secret(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "refresh_expires_at": self.refresh_expires_at.isoformat()
            if self.refresh_expires_at
            else None,
            "scopes": self.scopes,
            "extra": self.extra,
        }

    @classmethod
    def from_secret(cls, data: dict[str, Any]) -> TokenBundle:
        def parse(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            expires_at=parse(data.get("expires_at")),
            refresh_expires_at=parse(data.get("refresh_expires_at")),
            scopes=list(data.get("scopes") or []),
            extra=dict(data.get("extra") or {}),
        )

    def expires_soon(self, margin_seconds: int = 120) -> bool:
        return self.expires_at is not None and self.expires_at <= utcnow() + timedelta(
            seconds=margin_seconds
        )


def expiry(seconds: Any) -> datetime | None:
    try:
        return utcnow() + timedelta(seconds=int(seconds))
    except (TypeError, ValueError):
        return None


@dataclass
class Account:
    id: str
    name: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PublishRequest:
    video_path: Path
    title: str
    description: str
    tags: list[str]
    options: dict[str, Any]
    cover_path: Path | None = None
    publish_at: datetime | None = None
    # Some platforms pull the file themselves and need a URL they can reach.
    public_video_url: str = ""
    public_cover_url: str = ""


@dataclass
class PublishResult:
    """What the platform confirmed."""

    state: str  # "published" | "scheduled" | "processing"
    post_id: str = ""
    url: str = ""
    visibility: str = ""
    note: str = ""
    pending_ref: str = ""  # id to poll while the platform is still processing


@dataclass
class Capabilities:
    publish: bool = True
    schedule: str = "app"  # "platform": the platform holds it until the date; "app": this app uploads at the date
    comments: bool = False
    reply: bool = False
    metrics: tuple[str, ...] = ()
    metrics_missing: tuple[str, ...] = ()
    comments_note: str = ""
    needs_public_url: bool = False


def raise_for_response(response: httpx.Response, platform: str) -> None:
    """Map HTTP failures to connector errors without leaking tokens."""
    status = response.status_code
    if status < 400:
        return
    try:
        body = response.json()
    except ValueError:
        body = {}
    detail = ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("code") or "")
        elif isinstance(error, str):
            detail = str(body.get("error_description") or error)
    detail = detail[:300] or response.text[:300]
    if status == 401:
        raise AuthExpiredError()
    if (
        status == 429
        or "quota" in detail.lower()
        or ("rate" in detail.lower() and "limit" in detail.lower())
    ):
        retry_after = response.headers.get("retry-after")
        raise RateLimitedError(
            f"{platform} ha limitado las peticiones de esta app ({detail}). Vuelve a intentarlo más tarde.",
            retry_after=int(retry_after)
            if retry_after and retry_after.isdigit()
            else None,
        )
    if status == 403:
        raise ConnectorError(
            f"{platform} rechazó la operación por permisos: {detail}", code="forbidden"
        )
    raise ConnectorError(
        f"{platform} respondió con un error {status}: {detail}", retryable=status >= 500
    )


def client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return http_client(timeout=HTTP_TIMEOUT, transport=transport)
