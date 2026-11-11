"""Connector registry: which platforms exist, what each one needs, and tokens."""

from __future__ import annotations

import threading
from dataclasses import asdict
from datetime import timedelta
from types import ModuleType
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from creator_office.connectors import instagram, tiktok, youtube
from creator_office.connectors.base import (
    AuthExpiredError,
    ConnectorError,
    NotConnectedError,
    TokenBundle,
)
from creator_office.database import utcnow
from creator_office.models import Connection
from creator_office.pipeline.catalog import PLATFORMS
from creator_office.security import decrypt_json, encrypt_json
from creator_office.settings import get_settings

CONNECTORS: dict[str, ModuleType] = {
    "youtube": youtube,
    "tiktok": tiktok,
    "instagram": instagram,
}

MANUAL_ALTERNATIVE = (
    "Descarga el paquete de la publicación (vídeo, portada y textos), súbelo tú desde la app oficial "
    "y márcala como publicada indicando el enlace."
)

_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()


def provider_lock(workspace_id: str, provider: str) -> threading.Lock:
    """One upload at a time per account, so the app never bursts a platform."""
    with _locks_guard:
        return _locks.setdefault((workspace_id, provider), threading.Lock())


def connection_for(
    session: Session, workspace_id: str, provider: str
) -> Connection | None:
    return session.scalar(
        select(Connection).where(
            Connection.workspace_id == workspace_id, Connection.provider == provider
        )
    )


def status(session: Session, workspace_id: str, provider: str) -> dict[str, Any]:
    module = CONNECTORS[provider]
    missing = module.missing_settings()
    connection = connection_for(session, workspace_id, provider)
    if connection is not None and connection.status == "connected":
        state, label = "connected", "Conectado"
        detail = f"Cuenta: {connection.account_name}"
    elif connection is not None:
        state, label = "expired", "Requiere reconexión"
        detail = connection.error or "La autorización ha caducado o se ha revocado."
    elif missing:
        state, label = "needs_setup", "Pendiente de conexión"
        detail = (
            "Faltan las credenciales de la app de desarrollador en el servidor: "
            + ", ".join(missing)
            + "."
        )
    else:
        state, label = "ready", "Pendiente de conexión"
        detail = "La app de desarrollador está configurada. Falta autorizar tu cuenta."
    extra_warning = ""
    if provider == "instagram" and not instagram.base_url_is_public():
        extra_warning = "APP_BASE_URL no es una dirección https pública: Instagram no podrá descargar los vídeos para publicarlos por API."
    return {
        "provider": provider,
        "name": module.NAME,
        "state": state,
        "label": label,
        "detail": detail,
        "warning": extra_warning,
        "account_name": connection.account_name if connection else "",
        "account_id": connection.account_id if connection else "",
        "scopes": connection.scopes if connection else [],
        "requested_scopes": module.SCOPES,
        "connected_at": connection.created_at.isoformat() if connection else None,
        "missing_settings": missing,
        "redirect_uri": get_settings().oauth_redirect_uri(provider),
        "requirements": module.REQUIREMENTS,
        "capabilities": asdict(module.CAPABILITIES),
        "platforms": [
            key for key, value in PLATFORMS.items() if value["provider"] == provider
        ],
        "manual_alternative": MANUAL_ALTERNATIVE,
    }


def all_statuses(session: Session, workspace_id: str) -> dict[str, dict[str, Any]]:
    return {
        provider: status(session, workspace_id, provider) for provider in CONNECTORS
    }


def store_bundle(connection: Connection, bundle: TokenBundle) -> None:
    connection.secret = encrypt_json(bundle.to_secret())
    connection.expires_at = bundle.expires_at
    if bundle.scopes:
        connection.scopes = bundle.scopes


def get_bundle(
    session: Session, workspace_id: str, provider: str, transport: Any = None
) -> tuple[Connection, TokenBundle]:
    """Return a usable token, refreshing it first when it is about to expire."""
    connection = connection_for(session, workspace_id, provider)
    if connection is None:
        raise NotConnectedError(
            f"No hay ninguna cuenta de {CONNECTORS[provider].NAME} conectada."
        )
    if connection.status != "connected":
        raise AuthExpiredError(
            connection.error
            or "La autorización ha caducado. Vuelve a conectar la cuenta."
        )
    bundle = TokenBundle.from_secret(decrypt_json(connection.secret))
    module = CONNECTORS[provider]
    needs_refresh = bundle.expires_soon()
    if provider == "instagram" and bundle.expires_at is not None:
        # Long-lived tokens last 60 days and can only be renewed while valid.
        needs_refresh = bundle.expires_at <= utcnow() + timedelta(days=7)
    if needs_refresh:
        try:
            bundle = (
                module.refresh(bundle, transport)
                if transport is not None
                else module.refresh(bundle)
            )
        except ConnectorError as exc:
            if (
                exc.code == "auth_expired"
                or bundle.expires_at is None
                or bundle.expires_at <= utcnow()
            ):
                connection.status, connection.error = "expired", exc.user_message
                session.flush()
                raise AuthExpiredError(exc.user_message) from exc
        else:
            store_bundle(connection, bundle)
            session.flush()
    return connection, bundle


def mark_expired(
    session: Session, workspace_id: str, provider: str, message: str
) -> None:
    connection = connection_for(session, workspace_id, provider)
    if connection is not None:
        connection.status, connection.error = "expired", message
        session.flush()
