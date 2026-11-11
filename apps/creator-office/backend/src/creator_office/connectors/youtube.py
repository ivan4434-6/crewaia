"""YouTube connector: Google OAuth 2.0 + YouTube Data API v3 + YouTube Analytics API."""

from __future__ import annotations

from datetime import UTC, date, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from creator_office.connectors.base import (
    Account,
    Capabilities,
    ConnectorError,
    ProgressFn,
    PublishRequest,
    PublishResult,
    TokenBundle,
    client,
    expiry,
    raise_for_response,
)
from creator_office.settings import get_settings

PROVIDER = "youtube"
NAME = "YouTube"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint, not a secret
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
CHUNK = 8 * 1024 * 1024  # multiple of 256 KiB, as the resumable protocol requires

CAPABILITIES = Capabilities(
    schedule="platform",
    comments=True,
    reply=True,
    metrics=(
        "views",
        "watch_time_minutes",
        "avg_view_duration_s",
        "retention_pct",
        "likes",
        "comments",
        "shares",
    ),
)

REQUIREMENTS = [
    "Un proyecto en Google Cloud con «YouTube Data API v3» y «YouTube Analytics API» activadas.",
    "Un cliente OAuth de tipo «Aplicación web» con la URI de redirección indicada abajo.",
    "Mientras Google no audite el proyecto, los vídeos subidos por API quedan como privados.",
    "La cuota diaria por defecto (10 000 unidades) permite unas 6 subidas al día.",
]


def missing_settings() -> list[str]:
    settings = get_settings()
    return [
        name
        for name, value in (
            ("YOUTUBE_CLIENT_ID", settings.youtube_client_id),
            ("YOUTUBE_CLIENT_SECRET", settings.youtube_client_secret),
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
                "client_id": settings.youtube_client_id,
                "redirect_uri": settings.oauth_redirect_uri(PROVIDER),
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
    )


def _token_request(
    data: dict[str, str], transport: httpx.BaseTransport | None
) -> dict[str, Any]:
    settings = get_settings()
    with client(transport) as http:
        response = http.post(
            TOKEN_URL,
            data={
                "client_id": settings.youtube_client_id,
                "client_secret": settings.youtube_client_secret,
                **data,
            },
        )
    if response.status_code == 400 and "invalid_grant" in response.text:
        raise ConnectorError(
            "Google rechazó la autorización (caducada o revocada). Vuelve a conectar la cuenta.",
            code="auth_expired",
        )
    raise_for_response(response, NAME)
    return dict(response.json())


def exchange_code(
    code: str, code_verifier: str, transport: httpx.BaseTransport | None = None
) -> TokenBundle:
    payload = _token_request(
        {
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": get_settings().oauth_redirect_uri(PROVIDER),
        },
        transport,
    )
    return TokenBundle(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token", ""),
        expires_at=expiry(payload.get("expires_in")),
        scopes=str(payload.get("scope", "")).split(),
    )


def refresh(
    bundle: TokenBundle, transport: httpx.BaseTransport | None = None
) -> TokenBundle:
    if not bundle.refresh_token:
        raise ConnectorError(
            "No hay token de renovación. Vuelve a conectar la cuenta.",
            code="auth_expired",
        )
    payload = _token_request(
        {"refresh_token": bundle.refresh_token, "grant_type": "refresh_token"},
        transport,
    )
    return TokenBundle(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token") or bundle.refresh_token,
        expires_at=expiry(payload.get("expires_in")),
        scopes=str(payload.get("scope", "")).split() or bundle.scopes,
        extra=bundle.extra,
    )


def revoke(bundle: TokenBundle, transport: httpx.BaseTransport | None = None) -> None:
    with client(transport) as http:
        http.post(
            REVOKE_URL, params={"token": bundle.refresh_token or bundle.access_token}
        )


def _auth(bundle: TokenBundle) -> dict[str, str]:
    return {"Authorization": f"Bearer {bundle.access_token}"}


def account(
    bundle: TokenBundle, transport: httpx.BaseTransport | None = None
) -> Account:
    with client(transport) as http:
        response = http.get(
            f"{API}/channels",
            params={"part": "snippet", "mine": "true"},
            headers=_auth(bundle),
        )
    raise_for_response(response, NAME)
    items = response.json().get("items") or []
    if not items:
        raise ConnectorError(
            "La cuenta de Google autorizada no tiene ningún canal de YouTube.",
            code="no_channel",
        )
    return Account(id=items[0]["id"], name=items[0]["snippet"]["title"])


def _video_url(video_id: str, is_short: bool) -> str:
    return (
        f"https://www.youtube.com/shorts/{video_id}"
        if is_short
        else f"https://www.youtube.com/watch?v={video_id}"
    )


def publish(
    bundle: TokenBundle,
    request: PublishRequest,
    on_progress: ProgressFn | None = None,
    transport: httpx.BaseTransport | None = None,
) -> PublishResult:
    options = request.options
    privacy = options.get("privacy", "private")
    status: dict[str, Any] = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": bool(options.get("made_for_kids", False)),
    }
    if request.publish_at is not None:
        # YouTube schedules natively: the video stays private until publishAt.
        status["privacyStatus"] = "private"
        status["publishAt"] = request.publish_at.astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
    metadata = {
        "snippet": {
            "title": request.title,
            "description": request.description,
            "tags": request.tags,
            "categoryId": str(options.get("category_id", "22")),
        },
        "status": status,
    }
    size = request.video_path.stat().st_size
    with client(transport) as http:
        start = http.post(
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                **_auth(bundle),
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/mp4",
            },
            json=metadata,
        )
        raise_for_response(start, NAME)
        session_url = start.headers.get("location")
        if not session_url:
            raise ConnectorError(
                "YouTube no devolvió la dirección de subida.", retryable=True
            )
        video = _upload(http, session_url, request, size, on_progress)
        video_id = video.get("id")
        if not video_id:
            raise ConnectorError(
                "YouTube aceptó la subida pero no devolvió el identificador del vídeo."
            )
        note = ""
        if request.cover_path is not None and options.get("set_thumbnail", True):
            thumb = http.post(
                THUMBNAIL_URL,
                params={"videoId": video_id, "uploadType": "media"},
                headers={**_auth(bundle), "Content-Type": "image/jpeg"},
                content=request.cover_path.read_bytes(),
            )
            if thumb.status_code >= 400:
                note = "El vídeo se subió, pero YouTube rechazó la miniatura personalizada (el canal debe estar verificado)."
    actual = (video.get("status") or {}).get("privacyStatus", status["privacyStatus"])
    is_short = bool(options.get("is_short"))
    if request.publish_at is not None:
        return PublishResult(
            "scheduled",
            video_id,
            _video_url(video_id, is_short),
            actual,
            note or "Programado en YouTube.",
        )
    if actual != privacy:
        note = (
            note + " " if note else ""
        ) + f"YouTube lo dejó como «{actual}» en lugar de «{privacy}»."
    return PublishResult(
        "published", video_id, _video_url(video_id, is_short), actual, note
    )


def _upload(
    http: httpx.Client,
    session_url: str,
    request: PublishRequest,
    size: int,
    on_progress: ProgressFn | None,
) -> dict[str, Any]:
    sent = 0
    failures = 0
    with request.video_path.open("rb") as handle:
        while True:
            handle.seek(sent)
            chunk = handle.read(CHUNK)
            headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {sent}-{sent + len(chunk) - 1}/{size}",
            }
            try:
                response = http.put(session_url, content=chunk, headers=headers)
            except httpx.TransportError as exc:
                failures += 1
                if failures > 3:
                    raise ConnectorError(
                        "La conexión con YouTube se cortó durante la subida.",
                        retryable=True,
                    ) from exc
                sent = _resume_offset(http, session_url, size)
                continue
            if response.status_code in (200, 201):
                if on_progress:
                    on_progress(1.0, "Subida completada")
                return dict(response.json())
            if response.status_code == 308:
                received = response.headers.get("range", "")
                sent = int(received.rsplit("-", 1)[1]) + 1 if "-" in received else 0
                if on_progress:
                    on_progress(
                        sent / size,
                        f"Subiendo a YouTube: {sent // 1_048_576} de {size // 1_048_576} MB",
                    )
                continue
            if response.status_code >= 500 and failures < 3:
                failures += 1
                sent = _resume_offset(http, session_url, size)
                continue
            raise_for_response(response, NAME)
            raise ConnectorError(
                f"Respuesta inesperada de YouTube durante la subida ({response.status_code})."
            )


def _resume_offset(http: httpx.Client, session_url: str, size: int) -> int:
    response = http.put(
        session_url, headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"}
    )
    if response.status_code == 308:
        received = response.headers.get("range", "")
        return int(received.rsplit("-", 1)[1]) + 1 if "-" in received else 0
    raise_for_response(response, NAME)
    raise ConnectorError("No se pudo reanudar la subida a YouTube.", retryable=True)


def check_status(
    bundle: TokenBundle, post_id: str, transport: httpx.BaseTransport | None = None
) -> PublishResult:
    with client(transport) as http:
        response = http.get(
            f"{API}/videos",
            params={"part": "status", "id": post_id},
            headers=_auth(bundle),
        )
    raise_for_response(response, NAME)
    items = response.json().get("items") or []
    if not items:
        raise ConnectorError(
            "YouTube ya no encuentra este vídeo (puede haberse eliminado).",
            code="not_found",
        )
    status = items[0].get("status", {})
    if status.get("uploadStatus") in ("rejected", "failed"):
        reason = status.get("rejectionReason") or status.get("failureReason") or ""
        raise ConnectorError(f"YouTube rechazó el vídeo: {reason}", code="rejected")
    privacy = status.get("privacyStatus", "")
    if privacy == "private" and status.get("publishAt"):
        return PublishResult("scheduled", post_id, visibility=privacy)
    return PublishResult("published", post_id, visibility=privacy)


def fetch_comments(
    bundle: TokenBundle, post_id: str, transport: httpx.BaseTransport | None = None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page = ""
    with client(transport) as http:
        for _ in range(5):  # up to 500 most recent threads
            params = {
                "part": "snippet",
                "videoId": post_id,
                "maxResults": "100",
                "order": "time",
                "textFormat": "plainText",
            }
            if page:
                params["pageToken"] = page
            response = http.get(
                f"{API}/commentThreads", params=params, headers=_auth(bundle)
            )
            if response.status_code == 403 and "commentsDisabled" in response.text:
                return out
            raise_for_response(response, NAME)
            payload = response.json()
            for item in payload.get("items", []):
                top = item["snippet"]["topLevelComment"]
                snippet = top["snippet"]
                out.append(
                    {
                        "external_id": top["id"],
                        "author": snippet.get("authorDisplayName", ""),
                        "text": snippet.get("textOriginal")
                        or snippet.get("textDisplay", ""),
                        "posted_at": snippet.get("publishedAt"),
                    }
                )
            page = payload.get("nextPageToken", "")
            if not page:
                break
    return out


def reply(
    bundle: TokenBundle,
    comment_id: str,
    text: str,
    transport: httpx.BaseTransport | None = None,
) -> str:
    with client(transport) as http:
        response = http.post(
            f"{API}/comments",
            params={"part": "snippet"},
            headers=_auth(bundle),
            json={"snippet": {"parentId": comment_id, "textOriginal": text}},
        )
    raise_for_response(response, NAME)
    return str(response.json().get("id", ""))


def fetch_metrics(
    bundle: TokenBundle, post_id: str, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """Return ``{"values": {...}, "missing": [...]}`` with only what the APIs returned."""
    values: dict[str, float] = {}
    notes: list[str] = []
    with client(transport) as http:
        response = http.get(
            f"{API}/videos",
            params={"part": "statistics", "id": post_id},
            headers=_auth(bundle),
        )
        raise_for_response(response, NAME)
        items = response.json().get("items") or []
        if not items:
            raise ConnectorError(
                "YouTube ya no encuentra este vídeo.", code="not_found"
            )
        statistics = items[0].get("statistics", {})
        for source, target in (
            ("viewCount", "views"),
            ("likeCount", "likes"),
            ("commentCount", "comments"),
        ):
            if source in statistics:
                values[target] = float(statistics[source])
        today = date.today()
        report = http.get(
            ANALYTICS_URL,
            params={
                "ids": "channel==MINE",
                "startDate": (today - timedelta(days=365 * 3)).isoformat(),
                "endDate": today.isoformat(),
                "metrics": "estimatedMinutesWatched,averageViewDuration,averageViewPercentage,shares",
                "filters": f"video=={post_id}",
            },
            headers=_auth(bundle),
        )
        if report.status_code < 400:
            payload = report.json()
            names = [h["name"] for h in payload.get("columnHeaders", [])]
            rows = payload.get("rows") or []
            if rows:
                row = dict(zip(names, rows[0], strict=False))
                mapping = {
                    "estimatedMinutesWatched": "watch_time_minutes",
                    "averageViewDuration": "avg_view_duration_s",
                    "averageViewPercentage": "retention_pct",
                    "shares": "shares",
                }
                for source, target in mapping.items():
                    if row.get(source) is not None:
                        values[target] = float(row[source])
            else:
                notes.append(
                    "YouTube Analytics aún no tiene datos de este vídeo (suelen tardar hasta 48 h)."
                )
        else:
            notes.append(
                "YouTube Analytics no devolvió datos (permiso no concedido o API no activada)."
            )
    missing = [m for m in CAPABILITIES.metrics if m not in values]
    return {"values": values, "missing": missing, "notes": notes}
