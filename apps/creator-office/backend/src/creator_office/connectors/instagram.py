"""Instagram connector: Instagram API with Instagram Login (professional accounts)."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode, urlparse

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

PROVIDER = "instagram"
NAME = "Instagram"
AUTH_URL = "https://www.instagram.com/oauth/authorize"
SHORT_TOKEN_URL = "https://api.instagram.com/oauth/access_token"  # noqa: S105 - endpoint, not a secret
GRAPH = "https://graph.instagram.com"
SCOPES = [
    "instagram_business_basic",
    "instagram_business_content_publish",
    "instagram_business_manage_comments",
    "instagram_business_manage_insights",
]

CAPABILITIES = Capabilities(
    schedule="app",
    comments=True,
    reply=True,
    metrics=(
        "views",
        "reach",
        "likes",
        "comments",
        "shares",
        "saves",
        "avg_view_duration_s",
        "watch_time_minutes",
    ),
    metrics_missing=("retention_pct",),
    needs_public_url=True,
)

REQUIREMENTS = [
    "Una cuenta profesional de Instagram (creador o empresa).",
    "Una app en Meta for Developers con el producto «Instagram» (API con inicio de sesión de Instagram).",
    "La URI de redirección indicada abajo registrada en la app.",
    "Instagram descarga el vídeo desde una URL pública: APP_BASE_URL debe ser una dirección https accesible desde internet.",
    "Instagram no programa por API: esta aplicación publica a la hora que confirmes. Límite: 100 publicaciones por API cada 24 h.",
]


def missing_settings() -> list[str]:
    settings = get_settings()
    return [
        name
        for name, value in (
            ("INSTAGRAM_APP_ID", settings.instagram_app_id),
            ("INSTAGRAM_APP_SECRET", settings.instagram_app_secret),
        )
        if not value
    ]


def base_url_is_public() -> bool:
    parsed = urlparse(get_settings().app_base_url)
    host = parsed.hostname or ""
    return (
        parsed.scheme == "https"
        and host not in ("localhost", "127.0.0.1", "0.0.0.0")  # noqa: S104 - compared, not bound
        and "." in host
    )


def _api() -> str:
    return f"{GRAPH}/{get_settings().instagram_graph_version}"


def authorize_url(state: str, code_challenge: str) -> str:
    settings = get_settings()
    return (
        AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": settings.instagram_app_id,
                "redirect_uri": settings.oauth_redirect_uri(PROVIDER),
                "response_type": "code",
                "scope": ",".join(SCOPES),
                "state": state,
            }
        )
    )


def _graph_error(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    code, message = error.get("code"), str(error.get("message", ""))[:300]
    if code == 190:
        raise AuthExpiredError()
    if code in (4, 17, 32, 613) or error.get("error_subcode") == 2207042:
        raise RateLimitedError(
            f"Instagram ha limitado las peticiones o publicaciones de esta cuenta: {message}"
        )
    if code == 10 or code == 200:
        raise ConnectorError(
            f"Instagram rechazó la operación por permisos: {message}", code="forbidden"
        )
    raise_for_response(response, NAME)


def exchange_code(
    code: str, code_verifier: str, transport: httpx.BaseTransport | None = None
) -> TokenBundle:
    settings = get_settings()
    with client(transport) as http:
        short = http.post(
            SHORT_TOKEN_URL,
            data={
                "client_id": settings.instagram_app_id,
                "client_secret": settings.instagram_app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": settings.oauth_redirect_uri(PROVIDER),
                "code": code,
            },
        )
        if short.status_code >= 400:
            raise ConnectorError(
                f"Instagram rechazó la autorización: {short.text[:200]}",
                code="auth_expired",
            )
        short_payload = short.json()
        if isinstance(short_payload.get("data"), list) and short_payload["data"]:
            short_payload = short_payload["data"][0]
        long_lived = http.get(
            f"{GRAPH}/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": settings.instagram_app_secret,
                "access_token": short_payload["access_token"],
            },
        )
        _graph_error(long_lived)
        payload = long_lived.json()
    permissions = short_payload.get("permissions") or []
    if isinstance(permissions, str):
        permissions = permissions.split(",")
    return TokenBundle(
        access_token=payload["access_token"],
        expires_at=expiry(payload.get("expires_in")),
        scopes=list(permissions),
        extra={"user_id": str(short_payload.get("user_id", ""))},
    )


def refresh(
    bundle: TokenBundle, transport: httpx.BaseTransport | None = None
) -> TokenBundle:
    """Long-lived tokens are refreshed with themselves (no refresh token)."""
    with client(transport) as http:
        response = http.get(
            f"{GRAPH}/refresh_access_token",
            params={
                "grant_type": "ig_refresh_token",
                "access_token": bundle.access_token,
            },
        )
    _graph_error(response)
    payload = response.json()
    return TokenBundle(
        access_token=payload["access_token"],
        expires_at=expiry(payload.get("expires_in")),
        scopes=bundle.scopes,
        extra=bundle.extra,
    )


def revoke(bundle: TokenBundle, transport: httpx.BaseTransport | None = None) -> None:
    """Instagram has no token revocation endpoint; the user removes the app from their account settings."""


def account(
    bundle: TokenBundle, transport: httpx.BaseTransport | None = None
) -> Account:
    with client(transport) as http:
        response = http.get(
            f"{_api()}/me",
            params={
                "fields": "user_id,username,account_type",
                "access_token": bundle.access_token,
            },
        )
    _graph_error(response)
    payload = response.json()
    return Account(
        id=str(
            payload.get("user_id")
            or payload.get("id")
            or bundle.extra.get("user_id", "")
        ),
        name="@" + payload.get("username", ""),
        extra={"account_type": payload.get("account_type", "")},
    )


def publish(
    bundle: TokenBundle,
    request: PublishRequest,
    on_progress: ProgressFn | None = None,
    transport: httpx.BaseTransport | None = None,
) -> PublishResult:
    if not request.public_video_url:
        raise ConnectorError(
            "Instagram necesita descargar el vídeo desde una URL pública y este servidor no es accesible desde internet "
            "(APP_BASE_URL debe ser https y público). Usa la exportación manual.",
            code="needs_public_url",
        )
    user_id = bundle.extra.get("user_id") or request.options.get("account_id", "")
    if not user_id:
        raise ConnectorError(
            "Falta el identificador de la cuenta de Instagram. Vuelve a conectarla.",
            code="auth_expired",
        )
    token = {"access_token": bundle.access_token}
    with client(transport) as http:
        params: dict[str, Any] = {
            "media_type": "REELS",
            "video_url": request.public_video_url,
            "caption": request.description[:2200],
            "share_to_feed": "true"
            if request.options.get("share_to_feed", True)
            else "false",
            **token,
        }
        if request.public_cover_url:
            params["cover_url"] = request.public_cover_url
        created = http.post(f"{_api()}/{user_id}/media", data=params)
        _graph_error(created)
        container_id = created.json().get("id", "")
        if not container_id:
            raise ConnectorError(
                "Instagram no devolvió el contenedor de la publicación.", retryable=True
            )
        if on_progress:
            on_progress(0.2, "Instagram está descargando y procesando el vídeo")
        poll = float(request.options.get("_poll_seconds", 10))
        for attempt in range(30):  # up to ~5 minutes
            status = http.get(
                f"{_api()}/{container_id}",
                params={"fields": "status_code,status", **token},
            )
            _graph_error(status)
            payload = status.json()
            code = payload.get("status_code", "")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise ConnectorError(
                    f"Instagram no pudo procesar el vídeo: {payload.get('status', code)}",
                    code="failed",
                )
            if on_progress:
                on_progress(
                    min(0.85, 0.2 + attempt * 0.03),
                    "Instagram está procesando el vídeo",
                )
            time.sleep(poll)
        else:
            return PublishResult(
                "processing",
                pending_ref=container_id,
                note="Instagram sigue procesando el vídeo.",
            )
        return _publish_container(http, bundle, user_id, container_id, on_progress)


def _publish_container(
    http: httpx.Client,
    bundle: TokenBundle,
    user_id: str,
    container_id: str,
    on_progress: ProgressFn | None = None,
) -> PublishResult:
    token = {"access_token": bundle.access_token}
    published = http.post(
        f"{_api()}/{user_id}/media_publish", data={"creation_id": container_id, **token}
    )
    _graph_error(published)
    media_id = str(published.json().get("id", ""))
    if not media_id:
        raise ConnectorError(
            "Instagram no confirmó la publicación (no devolvió identificador)."
        )
    url = ""
    detail = http.get(f"{_api()}/{media_id}", params={"fields": "permalink", **token})
    if detail.status_code < 400:
        url = detail.json().get("permalink", "")
    if on_progress:
        on_progress(1.0, "Publicado en Instagram")
    return PublishResult("published", media_id, url)


def check_status(
    bundle: TokenBundle, ref: str, transport: httpx.BaseTransport | None = None
) -> PublishResult:
    """Finish a container that was still processing when the publish call returned."""
    user_id = bundle.extra.get("user_id", "")
    with client(transport) as http:
        status = http.get(
            f"{_api()}/{ref}",
            params={
                "fields": "status_code,status",
                "access_token": bundle.access_token,
            },
        )
        _graph_error(status)
        payload = status.json()
        code = payload.get("status_code", "")
        if code == "PUBLISHED":
            raise ConnectorError(
                "Instagram indica que el contenedor ya se publicó, pero no devolvió el identificador. Compruébalo en la app.",
                code="unknown_post",
            )
        if code in ("ERROR", "EXPIRED"):
            raise ConnectorError(
                f"Instagram no pudo procesar el vídeo: {payload.get('status', code)}",
                code="failed",
            )
        if code != "FINISHED":
            return PublishResult("processing", pending_ref=ref)
        return _publish_container(http, bundle, user_id, ref)


def fetch_comments(
    bundle: TokenBundle, post_id: str, transport: httpx.BaseTransport | None = None
) -> list[dict[str, Any]]:
    with client(transport) as http:
        response = http.get(
            f"{_api()}/{post_id}/comments",
            params={
                "fields": "id,text,username,timestamp",
                "limit": "100",
                "access_token": bundle.access_token,
            },
        )
    _graph_error(response)
    return [
        {
            "external_id": item["id"],
            "author": item.get("username", ""),
            "text": item.get("text", ""),
            "posted_at": item.get("timestamp"),
        }
        for item in response.json().get("data", [])
    ]


def reply(
    bundle: TokenBundle,
    comment_id: str,
    text: str,
    transport: httpx.BaseTransport | None = None,
) -> str:
    with client(transport) as http:
        response = http.post(
            f"{_api()}/{comment_id}/replies",
            data={"message": text, "access_token": bundle.access_token},
        )
    _graph_error(response)
    return str(response.json().get("id", ""))


_INSIGHTS = {
    "views": "views",
    "reach": "reach",
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
    "saved": "saves",
    "ig_reels_avg_watch_time": "avg_view_duration_s",
    "ig_reels_video_view_total_time": "watch_time_minutes",
}


def fetch_metrics(
    bundle: TokenBundle, post_id: str, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    values: dict[str, float] = {}
    notes: list[str] = []
    with client(transport) as http:
        # Metric names change between Graph API versions, so each one is asked
        # for separately and an unsupported name only loses that figure.
        for metric, target in _INSIGHTS.items():
            response = http.get(
                f"{_api()}/{post_id}/insights",
                params={"metric": metric, "access_token": bundle.access_token},
            )
            if response.status_code == 400:
                continue
            _graph_error(response)
            for item in response.json().get("data", []):
                points = item.get("values") or []
                if not points or points[0].get("value") is None:
                    continue
                value = float(points[0]["value"])
                if metric in (
                    "ig_reels_avg_watch_time",
                    "ig_reels_video_view_total_time",
                ):
                    value = value / 1000  # milliseconds
                    if metric == "ig_reels_video_view_total_time":
                        value = value / 60
                values[target] = round(value, 2)
    if not values:
        notes.append(
            "Instagram no devolvió estadísticas de este Reel (pueden tardar o no estar disponibles para la cuenta)."
        )
    missing = [
        m
        for m in (*CAPABILITIES.metrics, *CAPABILITIES.metrics_missing)
        if m not in values
    ]
    return {"values": values, "missing": missing, "notes": notes}
