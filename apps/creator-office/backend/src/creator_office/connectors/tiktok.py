"""TikTok connector: Login Kit (OAuth v2) + Content Posting API + Display API."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

from creator_office.connectors.base import (
    Account,
    AuthExpiredError,
    Capabilities,
    ConnectorError,
    ProgressFn,
    PublishRequest,
    PublishResult,
    RateLimitedError,
    TokenBundle,
    client,
    expiry,
    raise_for_response,
)
from creator_office.settings import get_settings

PROVIDER = "tiktok"
NAME = "TikTok"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
TOKEN_URL = f"{API}/oauth/token/"
SCOPES = ["user.info.basic", "video.publish", "video.list"]
MIN_CHUNK = 5 * 1024 * 1024
CHUNK = 10 * 1024 * 1024
PRIVACY_LEVELS = {
    "PUBLIC_TO_EVERYONE": "Público",
    "MUTUAL_FOLLOW_FRIENDS": "Amigos",
    "FOLLOWER_OF_CREATOR": "Seguidores",
    "SELF_ONLY": "Solo yo",
}

CAPABILITIES = Capabilities(
    schedule="app",
    comments=False,
    reply=False,
    metrics=("views", "likes", "comments", "shares"),
    metrics_missing=("watch_time_minutes", "avg_view_duration_s", "retention_pct"),
    comments_note="TikTok no ofrece una API oficial para leer ni responder comentarios desde apps de terceros: impórtalos a mano.",
)

REQUIREMENTS = [
    "Una app en TikTok for Developers con los productos «Login Kit» y «Content Posting API» (Direct Post).",
    "La URI de redirección indicada abajo registrada en la app.",
    "Hasta que TikTok audite la app, todo lo publicado por API queda visible solo para la cuenta («Solo yo»).",
    "TikTok no programa por API: esta aplicación sube el vídeo a la hora que confirmes.",
]


def missing_settings() -> list[str]:
    settings = get_settings()
    return [
        name
        for name, value in (
            ("TIKTOK_CLIENT_KEY", settings.tiktok_client_key),
            ("TIKTOK_CLIENT_SECRET", settings.tiktok_client_secret),
        )
        if not value
    ]


def authorize_url(state: str, code_challenge: str) -> str:
    settings = get_settings()
    return (
        AUTH_URL
        + "?"
        + urlencode(
            {
                "client_key": settings.tiktok_client_key,
                "scope": ",".join(SCOPES),
                "response_type": "code",
                "redirect_uri": settings.oauth_redirect_uri(PROVIDER),
                "state": state,
            }
        )
    )


def _token_request(
    data: dict[str, str], transport: httpx.BaseTransport | None
) -> TokenBundle:
    settings = get_settings()
    with client(transport) as http:
        response = http.post(
            TOKEN_URL,
            data={
                "client_key": settings.tiktok_client_key,
                "client_secret": settings.tiktok_client_secret,
                **data,
            },
        )
    raise_for_response(response, NAME)
    payload = response.json()
    if payload.get("error"):
        raise ConnectorError(
            f"TikTok rechazó la autorización: {payload.get('error_description') or payload['error']}",
            code="auth_expired",
        )
    return TokenBundle(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token", ""),
        expires_at=expiry(payload.get("expires_in")),
        refresh_expires_at=expiry(payload.get("refresh_expires_in")),
        scopes=str(payload.get("scope", "")).split(","),
        extra={"open_id": payload.get("open_id", "")},
    )


def exchange_code(
    code: str, code_verifier: str, transport: httpx.BaseTransport | None = None
) -> TokenBundle:
    return _token_request(
        {
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": get_settings().oauth_redirect_uri(PROVIDER),
        },
        transport,
    )


def refresh(
    bundle: TokenBundle, transport: httpx.BaseTransport | None = None
) -> TokenBundle:
    if not bundle.refresh_token:
        raise AuthExpiredError()
    fresh = _token_request(
        {"grant_type": "refresh_token", "refresh_token": bundle.refresh_token},
        transport,
    )
    fresh.extra = {**bundle.extra, **fresh.extra}
    return fresh


def revoke(bundle: TokenBundle, transport: httpx.BaseTransport | None = None) -> None:
    settings = get_settings()
    with client(transport) as http:
        http.post(
            f"{API}/oauth/revoke/",
            data={
                "client_key": settings.tiktok_client_key,
                "client_secret": settings.tiktok_client_secret,
                "token": bundle.access_token,
            },
        )


def _headers(bundle: TokenBundle) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {bundle.access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }


def _data(response: httpx.Response) -> dict[str, Any]:
    """TikTok wraps results as ``{"data": ..., "error": {"code": "ok" | ...}}``."""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    code = (error or {}).get("code", "") if isinstance(error, dict) else ""
    if code and code != "ok":
        message = (error or {}).get("message", "")
        if code in ("access_token_invalid", "scope_not_authorized"):
            raise AuthExpiredError(
                f"TikTok no acepta la autorización actual ({code}). Vuelve a conectar la cuenta."
            )
        if code in (
            "rate_limit_exceeded",
            "spam_risk_too_many_posts",
            "spam_risk_too_many_pending_share",
        ):
            raise RateLimitedError(
                f"TikTok ha limitado las publicaciones de esta cuenta ({code}). Inténtalo más tarde."
            )
        if code == "unaudited_client_can_only_post_to_private_accounts":
            raise ConnectorError(
                "La app de TikTok aún no está auditada: solo puede publicar en cuentas privadas y con visibilidad «Solo yo».",
                code="unaudited",
            )
        raise ConnectorError(f"TikTok devolvió el error «{code}»: {message}", code=code)
    raise_for_response(response, NAME)
    return dict(payload.get("data") or {})


def account(
    bundle: TokenBundle, transport: httpx.BaseTransport | None = None
) -> Account:
    with client(transport) as http:
        response = http.get(
            f"{API}/user/info/",
            params={"fields": "open_id,display_name"},
            headers=_headers(bundle),
        )
    user = _data(response).get("user", {})
    return Account(
        id=user.get("open_id") or bundle.extra.get("open_id", ""),
        name=user.get("display_name", "Cuenta de TikTok"),
    )


def creator_info(
    bundle: TokenBundle, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    with client(transport) as http:
        response = http.post(
            f"{API}/post/publish/creator_info/query/", headers=_headers(bundle)
        )
    return _data(response)


def plan_chunks(size: int, chunk: int = CHUNK) -> tuple[int, int]:
    """Return (chunk_size, total_chunk_count) following TikTok's upload rules.

    Files under 5 MB go in one piece; otherwise the trailing bytes are merged
    into the last chunk, so the count is ``floor(size / chunk_size)``.
    """
    if size < MIN_CHUNK or size <= chunk:
        return size, 1
    return chunk, size // chunk


def publish(
    bundle: TokenBundle,
    request: PublishRequest,
    on_progress: ProgressFn | None = None,
    transport: httpx.BaseTransport | None = None,
) -> PublishResult:
    options = request.options
    privacy = options.get("privacy_level")
    if privacy not in PRIVACY_LEVELS:
        raise ConnectorError(
            "Elige quién puede ver el vídeo en TikTok antes de publicar.",
            code="missing_privacy",
        )
    size = request.video_path.stat().st_size
    chunk_size, count = plan_chunks(size)
    with client(transport) as http:
        info = _data(
            http.post(
                f"{API}/post/publish/creator_info/query/", headers=_headers(bundle)
            )
        )
        allowed = info.get("privacy_level_options") or []
        if allowed and privacy not in allowed:
            names = ", ".join(PRIVACY_LEVELS.get(level, level) for level in allowed)
            raise ConnectorError(
                f"Esta cuenta de TikTok solo admite estas visibilidades: {names}.",
                code="privacy_not_allowed",
            )
        limit = info.get("max_video_post_duration_sec")
        duration = float(options.get("duration_s") or 0)
        if limit and duration and duration > float(limit):
            raise ConnectorError(
                f"El vídeo dura {duration:.0f} s y esta cuenta admite hasta {limit} s.",
                code="too_long",
            )
        init = http.post(
            f"{API}/post/publish/video/init/",
            headers=_headers(bundle),
            json={
                "post_info": {
                    "title": request.title[:2200],
                    "privacy_level": privacy,
                    "disable_comment": bool(options.get("disable_comment", False))
                    or bool(info.get("comment_disabled")),
                    "disable_duet": bool(options.get("disable_duet", False))
                    or bool(info.get("duet_disabled")),
                    "disable_stitch": bool(options.get("disable_stitch", False))
                    or bool(info.get("stitch_disabled")),
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": count,
                },
            },
        )
        data = _data(init)
        publish_id, upload_url = data.get("publish_id", ""), data.get("upload_url", "")
        if not publish_id or not upload_url:
            raise ConnectorError(
                "TikTok no devolvió la dirección de subida.", retryable=True
            )
        with request.video_path.open("rb") as handle:
            for index in range(count):
                first = index * chunk_size
                last = size - 1 if index == count - 1 else first + chunk_size - 1
                handle.seek(first)
                body = handle.read(last - first + 1)
                response = http.put(
                    upload_url,
                    content=body,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(body)),
                        "Content-Range": f"bytes {first}-{last}/{size}",
                    },
                )
                if response.status_code not in (200, 201, 206):
                    raise_for_response(response, NAME)
                    raise ConnectorError(
                        f"Respuesta inesperada de TikTok durante la subida ({response.status_code}).",
                        retryable=True,
                    )
                if on_progress:
                    on_progress(
                        (last + 1) / size * 0.9,
                        f"Subiendo a TikTok: {(last + 1) // 1_048_576} de {max(1, size // 1_048_576)} MB",
                    )
        username = info.get("creator_username", "")
        for attempt in range(
            18
        ):  # up to ~3 minutes; afterwards the scheduler keeps checking
            result = _status(http, bundle, publish_id, username)
            if result.state != "processing":
                return result
            if on_progress:
                on_progress(
                    0.9 + min(0.09, attempt * 0.005), "TikTok está procesando el vídeo"
                )
            time.sleep(float(options.get("_poll_seconds", 10)))
    return PublishResult(
        "processing", pending_ref=publish_id, note="TikTok sigue procesando el vídeo."
    )


def _status(
    http: httpx.Client, bundle: TokenBundle, publish_id: str, username: str = ""
) -> PublishResult:
    data = _data(
        http.post(
            f"{API}/post/publish/status/fetch/",
            headers=_headers(bundle),
            json={"publish_id": publish_id},
        )
    )
    status = data.get("status", "")
    if status == "PUBLISH_COMPLETE":
        ids = (
            data.get("publicaly_available_post_id")
            or data.get("publicly_available_post_id")
            or []
        )
        post_id = str(ids[0]) if ids else ""
        url = (
            f"https://www.tiktok.com/@{username}/video/{post_id}"
            if post_id and username
            else ""
        )
        note = (
            ""
            if post_id
            else "TikTok confirmó la publicación; el identificador público aparece cuando el vídeo pasa la moderación y es público."
        )
        return PublishResult(
            "published", post_id, url, note=note, pending_ref=publish_id
        )
    if status == "FAILED":
        raise ConnectorError(
            f"TikTok no pudo publicar el vídeo: {data.get('fail_reason', 'motivo no indicado')}.",
            code="failed",
        )
    if status == "SEND_TO_USER_INBOX":
        raise ConnectorError(
            "TikTok envió el vídeo a la bandeja de la app para que lo termines allí: no se ha publicado.",
            code="inbox",
        )
    return PublishResult("processing", pending_ref=publish_id)


def check_status(
    bundle: TokenBundle, ref: str, transport: httpx.BaseTransport | None = None
) -> PublishResult:
    with client(transport) as http:
        return _status(http, bundle, ref)


def fetch_comments(
    bundle: TokenBundle, post_id: str, transport: httpx.BaseTransport | None = None
) -> list[dict[str, Any]]:
    raise ConnectorError(CAPABILITIES.comments_note, code="unsupported")


def reply(
    bundle: TokenBundle,
    comment_id: str,
    text: str,
    transport: httpx.BaseTransport | None = None,
) -> str:
    raise ConnectorError(CAPABILITIES.comments_note, code="unsupported")


def fetch_metrics(
    bundle: TokenBundle, post_id: str, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    with client(transport) as http:
        response = http.post(
            f"{API}/video/query/",
            params={"fields": "id,view_count,like_count,comment_count,share_count"},
            headers=_headers(bundle),
            json={"filters": {"video_ids": [post_id]}},
        )
    videos = _data(response).get("videos") or []
    if not videos:
        raise ConnectorError(
            "TikTok no devolvió datos de este vídeo.", code="not_found"
        )
    video = videos[0]
    values = {
        target: float(video[source])
        for source, target in (
            ("view_count", "views"),
            ("like_count", "likes"),
            ("comment_count", "comments"),
            ("share_count", "shares"),
        )
        if video.get(source) is not None
    }
    return {
        "values": values,
        "missing": list(CAPABILITIES.metrics_missing),
        "notes": [
            "La API de TikTok no facilita tiempo de visualización ni retención: añádelos a mano desde TikTok Studio si los necesitas."
        ],
    }
