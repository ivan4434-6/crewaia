"""Publications, calendar and platform integrations.

Nothing is sent to a platform without a signed-in person approving the content
and then confirming the action; and a publication is only reported as
published when the platform's API confirmed it or a person declared it.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from creator_office import clock
from creator_office.agents.llm import PROVIDER_PRESETS, build_llm, describe_engine
from creator_office.api import serializers
from creator_office.api.deps import ApiError, Auth, Current, Db, Editor, Owner, owned
from creator_office.connectors import instagram, registry
from creator_office.connectors import platform_specs as specs
from creator_office.connectors.base import ConnectorError, TokenBundle
from creator_office.database import utcnow
from creator_office.events import record_event
from creator_office.media import ffmpeg, transcription
from creator_office.models import (
    Connection,
    Design,
    Document,
    OAuthState,
    Project,
    Publication,
)
from creator_office.pipeline import engine
from creator_office.pipeline.catalog import PLATFORMS
from creator_office.security import decrypt_json, encrypt_json, mask_secret, new_token
from creator_office.services import documents, publishing
from creator_office.settings import get_settings
from creator_office.text import shorten

router = APIRouter(prefix="/api", tags=["publishing"])
logger = logging.getLogger(__name__)

OPTION_KEYS = {
    "youtube": {"privacy", "made_for_kids", "category_id", "set_thumbnail"},
    "tiktok": {"privacy_level", "disable_comment", "disable_duet", "disable_stitch"},
    "instagram": {"share_to_feed"},
}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=utcnow().tzinfo)


def _publication(
    db: Session, auth: Auth, publication_id: str
) -> tuple[Publication, Project]:
    pub = owned(db, auth, Publication, publication_id, "La publicación")
    project = owned(db, auth, Project, pub.project_id, "El proyecto")
    return pub, project


def _one(db: Session, pub: Publication) -> dict[str, Any]:
    return {"publication": serializers.publication(db, pub)}


def _label(pub: Publication) -> str:
    return shorten(pub.title or pub.description or "sin título")


def _name(pub: Publication) -> str:
    return PLATFORMS[pub.platform]["name"]


def _require_ready(db: Session, pub: Publication) -> None:
    publishing.validate(db, pub)
    failing = [c for c in pub.validation if c["status"] == "fail"]
    if failing:
        raise ApiError(
            409,
            "La publicación no cumple los requisitos: "
            + "; ".join(f"{c['label']}: {c['detail']}" for c in failing[:3]),
            "not_ready",
        )


@router.get("/publications")
def list_publications(
    auth: Current, db: Db, project_id: str = "", status: str = ""
) -> dict[str, Any]:
    query = select(Publication).where(Publication.workspace_id == auth.workspace_id)
    if project_id:
        query = query.where(Publication.project_id == project_id)
    if status:
        query = query.where(Publication.status == status)
    else:
        query = query.where(Publication.status != "discarded")
    statuses = registry.all_statuses(db, auth.workspace_id)
    projects = {
        p.id: p.title
        for p in db.scalars(
            select(Project).where(Project.workspace_id == auth.workspace_id)
        )
    }
    rows = list(db.scalars(query.order_by(Publication.created_at.desc()).limit(500)))
    return {
        "publications": [
            {
                **serializers.publication(db, p, statuses),
                "project": projects.get(p.project_id, ""),
            }
            for p in rows
        ],
        "integrations": statuses,
    }


class PublicationPatch(BaseModel):
    title: str | None = Field(default=None, max_length=5000)
    description: str | None = Field(default=None, max_length=10000)
    tags: list[str] | None = Field(default=None, max_length=60)
    options: dict[str, Any] | None = None
    design_id: str | None = None
    clear_design: bool = False


@router.patch("/publications/{publication_id}")
def edit_publication(
    publication_id: str, body: PublicationPatch, auth: Editor, db: Db
) -> dict[str, Any]:
    pub, project = _publication(db, auth, publication_id)
    if pub.status not in ("draft", "error"):
        raise ApiError(
            409,
            "Solo se puede editar un borrador. Desprograma la publicación para cambiarla.",
            "not_editable",
        )
    spec = specs.SPECS[pub.platform]
    content_changed = False
    if body.title is not None and spec.title_max and body.title.strip() != pub.title:
        pub.title, content_changed = body.title.strip(), True
    if (
        body.description is not None
        and spec.description_max
        and body.description.strip() != pub.description
    ):
        pub.description, content_changed = body.description.strip(), True
    if body.tags is not None and spec.tags_total_max:
        tags = [t.strip().lstrip("#") for t in body.tags if t.strip()]
        if tags != list(pub.tags or []):
            pub.tags, content_changed = tags, True
    if body.options is not None:
        allowed = OPTION_KEYS[PLATFORMS[pub.platform]["provider"]]
        options = {k: v for k, v in (pub.options or {}).items() if k not in allowed}
        options.update({k: v for k, v in body.options.items() if k in allowed})
        if options != dict(pub.options or {}):
            pub.options, content_changed = options, True
    if body.clear_design and pub.design_id:
        pub.design_id, content_changed = None, True
    elif body.design_id is not None and body.design_id != pub.design_id:
        design = owned(db, auth, Design, body.design_id, "El diseño")
        if design.project_id != pub.project_id:
            raise ApiError(404, "El diseño no pertenece a este proyecto.", "not_found")
        pub.design_id, content_changed = design.id, True
    if pub.status == "error":
        pub.status, pub.error = "draft", ""
    publishing.validate(db, pub)
    if content_changed:
        was_approved = pub.approved_at is not None
        pub.approved_at, pub.approved_by = None, None
        publishing.save_version(db, pub, author_type="user", author=auth.user.name)
        auth.log(
            db,
            "publication.edited",
            f"{auth.user.name} editó la publicación de {_name(pub)} «{_label(pub)}»."
            + (" Hay que volver a aprobarla." if was_approved else ""),
            project_id=project.id,
            data={"publication_id": pub.id},
        )
    return _one(db, pub)


@router.get("/publications/{publication_id}/versions")
def publication_versions(publication_id: str, auth: Current, db: Db) -> dict[str, Any]:
    pub, _project = _publication(db, auth, publication_id)
    document = db.scalar(
        select(Document).where(
            Document.kind == "publication", Document.ref_id == pub.id
        )
    )
    if document is None:
        return {"versions": []}
    return {
        "versions": [
            documents.version_to_dict(v, current_id=document.current_version_id)
            for v in documents.versions(db, document)
        ]
    }


class PublicationAction(BaseModel):
    action: str
    confirm: bool = False
    scheduled_at: datetime | None = None
    mode: str = ""  # schedule: platform | connector | reminder
    url: str = Field(default="", max_length=2000)
    published_at: datetime | None = None


def _enqueue_publish(db: Session, auth: Auth, pub: Publication) -> None:
    pub.status, pub.error, pub.progress = "publishing", "", None
    engine.enqueue_job(
        db,
        workspace_id=pub.workspace_id,
        project_id=pub.project_id,
        agent="publisher",
        kind="publish",
        title=f"Publicar en {_name(pub)}: {_label(pub)}",
        payload={"publication_id": pub.id},
        engine="connector",
        user_id=auth.user.id,
    )


def _require_api(db: Session, pub: Publication) -> dict[str, Any]:
    provider = PLATFORMS[pub.platform]["provider"]
    status = registry.status(db, pub.workspace_id, provider)
    if status["state"] != "connected":
        raise ApiError(
            409,
            f"{status['name']}: {status['label'].lower()}. {status['detail']} Alternativa: {registry.MANUAL_ALTERNATIVE}",
            "not_connected",
        )
    if provider == "instagram" and not instagram.base_url_is_public():
        raise ApiError(
            409,
            "Instagram necesita descargar el vídeo desde una dirección pública y este servidor no lo es. "
            + registry.MANUAL_ALTERNATIVE,
            "needs_public_url",
        )
    if provider == "tiktok" and not (pub.options or {}).get("privacy_level"):
        raise ApiError(
            409,
            "Elige quién puede ver el vídeo en TikTok antes de publicar.",
            "missing_privacy",
        )
    return status


def _require_approval(db: Session, pub: Publication) -> None:
    """The content must be approved, and it must still be the content that was approved."""
    if pub.approved_at is None:
        raise ApiError(
            409, "Aprueba primero el contenido de la publicación.", "not_approved"
        )
    changed = publishing.changed_since_approval(db, pub)
    if changed:
        pub.approved_at, pub.approved_by = None, None
        db.commit()
        raise ApiError(
            409,
            f"Cambió {' y '.join(changed)} después de aprobar esta publicación. Revísala y apruébala de nuevo.",
            "not_approved",
        )


@router.post("/publications/{publication_id}/actions")
def publication_action(
    publication_id: str, body: PublicationAction, auth: Editor, db: Db
) -> dict[str, Any]:
    pub, project = _publication(db, auth, publication_id)
    name, label, user = _name(pub), _label(pub), auth.user.name
    data = {"publication_id": pub.id, "platform": pub.platform}
    action = body.action

    if action == "approve":
        if pub.status not in ("draft", "error"):
            raise ApiError(
                409, "Esta publicación ya no es un borrador.", "not_possible"
            )
        _require_ready(db, pub)
        pub.approved_at, pub.approved_by = utcnow(), auth.user.id
        publishing.remember_approved(
            db, pub
        )  # the approval covers this video and this cover, not later ones
        if pub.status == "error":
            pub.status, pub.error = "draft", ""
        auth.log(
            db,
            "publication.approved",
            f"{user} aprobó el contenido de la publicación de {name} «{label}».",
            project_id=project.id,
            data=data,
        )

    elif action == "unapprove":
        if pub.status != "draft":
            raise ApiError(
                409,
                "Solo se puede retirar la aprobación de un borrador.",
                "not_possible",
            )
        pub.approved_at, pub.approved_by = None, None
        auth.log(
            db,
            "publication.unapproved",
            f"{user} retiró la aprobación de la publicación de {name} «{label}».",
            project_id=project.id,
            data=data,
        )

    elif action == "publish":
        # Final confirmation: the upload starts only from this explicit request.
        if pub.status not in ("draft", "error"):
            raise ApiError(409, "Esta publicación no está en borrador.", "not_possible")
        _require_approval(db, pub)
        if not body.confirm:
            raise ApiError(
                422, "Falta la confirmación final para publicar.", "not_confirmed"
            )
        _require_ready(db, pub)
        status = _require_api(db, pub)
        options = dict(pub.options or {})
        if body.mode == "platform":
            if status["capabilities"]["schedule"] != "platform":
                raise ApiError(
                    409,
                    f"{name} no permite programar desde su API. Usa la programación de esta aplicación.",
                    "not_supported",
                )
            when = _aware(body.scheduled_at) if body.scheduled_at else pub.scheduled_at
            if when is None or when <= utcnow() + timedelta(minutes=15):
                raise ApiError(
                    422,
                    "Elige una fecha de publicación al menos 15 minutos en el futuro.",
                    "bad_date",
                )
            pub.scheduled_at = when
            options["schedule_mode"] = "platform"
            message = f"{user} confirmó subir «{label}» a {name} para que la plataforma lo publique el {clock.stamp(when, auth.workspace.settings)}."
        else:
            options.pop("schedule_mode", None)
            pub.scheduled_at = None
            message = f"{user} confirmó publicar ahora «{label}» en {name} ({status['account_name']})."
        pub.options = options
        auth.log(db, "publication.confirmed", message, project_id=project.id, data=data)
        _enqueue_publish(db, auth, pub)

    elif action == "schedule":
        if pub.status not in ("draft", "scheduled"):
            raise ApiError(
                409,
                "Esta publicación no se puede programar en su estado actual.",
                "not_possible",
            )
        if (pub.options or {}).get(
            "schedule_mode"
        ) == "platform" and pub.status == "scheduled":
            raise ApiError(
                409,
                f"Ya está subida y programada en {name}: cambia la fecha desde la propia plataforma.",
                "on_platform",
            )
        _require_approval(db, pub)
        if body.scheduled_at is None:
            raise ApiError(422, "Elige fecha y hora.", "bad_date")
        when = _aware(body.scheduled_at)
        if when <= utcnow() + timedelta(minutes=2):
            raise ApiError(422, "La fecha debe estar en el futuro.", "bad_date")
        if body.mode not in ("connector", "reminder"):
            raise ApiError(422, "Modo de programación desconocido.", "bad_mode")
        _require_ready(db, pub)
        if body.mode == "connector":
            if not body.confirm:
                raise ApiError(
                    422,
                    "Confirma que quieres que se publique automáticamente a esa hora.",
                    "not_confirmed",
                )
            _require_api(db, pub)
            message = f"{user} programó «{label}» en {name}: esta aplicación lo subirá a la cuenta conectada el {clock.stamp(when, auth.workspace.settings)}."
        else:
            message = f"{user} programó un recordatorio para publicar a mano «{label}» en {name} el {clock.stamp(when, auth.workspace.settings)}."
        pub.options = {**(pub.options or {}), "schedule_mode": body.mode}
        pub.scheduled_at, pub.status, pub.reminder_sent = when, "scheduled", False
        auth.log(
            db,
            "publication.scheduled",
            message,
            project_id=project.id,
            data={**data, "mode": body.mode},
        )

    elif action == "unschedule":
        if pub.status != "scheduled":
            raise ApiError(409, "Esta publicación no está programada.", "not_possible")
        if (pub.options or {}).get("schedule_mode") == "platform":
            raise ApiError(
                409,
                f"Ya está subida y programada en {name}: cancélala desde la propia plataforma.",
                "on_platform",
            )
        options = dict(pub.options or {})
        options.pop("schedule_mode", None)
        pub.options, pub.status = options, "draft"
        auth.log(
            db,
            "publication.unscheduled",
            f"{user} desprogramó «{label}» de {name}.",
            project_id=project.id,
            data=data,
        )

    elif action == "mark_published":
        if pub.status in ("publishing", "published", "discarded"):
            raise ApiError(
                409,
                "Esta publicación no se puede marcar como publicada en su estado actual.",
                "not_possible",
            )
        if (pub.options or {}).get(
            "schedule_mode"
        ) == "platform" and pub.status == "scheduled":
            raise ApiError(
                409,
                f"Ya está subida a {name}: su estado se actualiza al consultar la plataforma.",
                "on_platform",
            )
        url = body.url.strip()
        if url and not url.startswith(("http://", "https://")):
            raise ApiError(
                422, "El enlace debe empezar por http:// o https://.", "bad_url"
            )
        when = _aware(body.published_at) if body.published_at else utcnow()
        if when > utcnow() + timedelta(minutes=5):
            raise ApiError(
                422, "La fecha de publicación no puede estar en el futuro.", "bad_date"
            )
        pub.status, pub.confirmation, pub.url, pub.published_at, pub.error = (
            "published",
            "manual",
            url,
            when,
            "",
        )
        publishing.remember_published(db, pub)
        auth.log(
            db,
            "publication.marked_published",
            f"{user} declaró haber publicado a mano «{label}» en {name}."
            + (f" Enlace: {url}" if url else " Sin enlace."),
            project_id=project.id,
            data=data,
        )

    elif action == "discard":
        if pub.status in ("publishing", "published"):
            raise ApiError(
                409,
                "No se puede descartar una publicación en curso o ya publicada.",
                "not_possible",
            )
        if (pub.options or {}).get(
            "schedule_mode"
        ) == "platform" and pub.status == "scheduled":
            raise ApiError(
                409,
                f"Ya está subida a {name}: bórrala desde la propia plataforma.",
                "on_platform",
            )
        pub.status = "discarded"
        auth.log(
            db,
            "publication.discarded",
            f"{user} descartó la publicación de {name} «{label}».",
            project_id=project.id,
            data=data,
        )

    elif action == "restore":
        if pub.status != "discarded":
            raise ApiError(409, "Esta publicación no está descartada.", "not_possible")
        pub.status, pub.approved_at, pub.approved_by = "draft", None, None
        publishing.validate(db, pub)
        auth.log(
            db,
            "publication.restored",
            f"{user} recuperó la publicación de {name} «{label}».",
            project_id=project.id,
            data=data,
        )

    else:
        raise ApiError(422, "Acción desconocida.", "bad_action")
    return _one(db, pub)


@router.get("/publications/{publication_id}/export")
def export_publication(publication_id: str, auth: Current, db: Db) -> Response:
    pub, project = _publication(db, auth, publication_id)
    filename, payload = publishing.export_zip(db, pub)
    auth.log(
        db,
        "publication.exported",
        f"{auth.user.name} descargó el paquete de exportación manual de {_name(pub)} «{_label(pub)}».",
        project_id=project.id,
        data={"publication_id": pub.id},
    )
    return Response(
        payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "no-store",
        },
    )


@router.get("/calendar")
def calendar(auth: Current, db: Db, start: datetime, end: datetime) -> dict[str, Any]:
    start, end = _aware(start), _aware(end)
    if end <= start or end - start > timedelta(days=400):
        raise ApiError(422, "Intervalo de fechas no válido.", "bad_range")
    projects = {
        p.id: p
        for p in db.scalars(
            select(Project).where(Project.workspace_id == auth.workspace_id)
        )
    }
    items: list[dict[str, Any]] = []
    for pub in db.scalars(
        select(Publication).where(
            Publication.workspace_id == auth.workspace_id,
            Publication.status != "discarded",
        )
    ):
        when = pub.published_at if pub.status == "published" else pub.scheduled_at
        if when is None or not (start <= when < end):
            continue
        items.append(
            {
                "type": "publication",
                "id": pub.id,
                "project_id": pub.project_id,
                "project": projects[pub.project_id].title
                if pub.project_id in projects
                else "",
                "platform": pub.platform,
                "platform_name": _name(pub),
                "title": _label(pub),
                "at": serializers.iso(when),
                "status": pub.status,
                "status_label": publishing.STATUS_LABELS[pub.status],
                "confirmation": pub.confirmation,
                "schedule_mode": (pub.options or {}).get("schedule_mode", ""),
                "approved": pub.approved_at is not None,
            }
        )
    for project in projects.values():
        if (
            project.deadline is not None
            and start <= project.deadline < end
            and project.state != "cancelled"
        ):
            items.append(
                {
                    "type": "deadline",
                    "id": project.id,
                    "project_id": project.id,
                    "project": project.title,
                    "title": "Fecha límite",
                    "at": serializers.iso(project.deadline),
                }
            )
    items.sort(key=lambda item: item["at"])
    unscheduled = [
        {
            "id": p.id,
            "project_id": p.project_id,
            "project": projects[p.project_id].title if p.project_id in projects else "",
            "platform": p.platform,
            "platform_name": _name(p),
            "title": _label(p),
            "approved": p.approved_at is not None,
        }
        for p in db.scalars(
            select(Publication)
            .where(
                Publication.workspace_id == auth.workspace_id,
                Publication.status == "draft",
            )
            .order_by(Publication.created_at.desc())
            .limit(50)
        )
    ]
    return {"items": items, "unscheduled": unscheduled}


# --- Integrations ------------------------------------------------------------


def _llm_payload(db: Session, auth: Auth) -> dict[str, Any]:
    info = describe_engine(db, auth.workspace_id)
    connection = db.scalar(
        select(Connection).where(
            Connection.workspace_id == auth.workspace_id, Connection.provider == "llm"
        )
    )
    key = decrypt_json(connection.secret).get("api_key", "") if connection else ""
    settings = get_settings()
    return {
        "engine": info.public(),
        "saved": {
            "model": connection.config.get("model", ""),
            "base_url": connection.config.get("base_url", ""),
            "price_input_per_mtok": connection.config.get("price_input_per_mtok"),
            "price_output_per_mtok": connection.config.get("price_output_per_mtok"),
            "api_key_hint": mask_secret(key),
            "has_key": bool(key),
        }
        if connection
        else None,
        "env_model": settings.llm_model,
        "presets": PROVIDER_PRESETS,
        "demo_note": (
            "Sin proveedor de IA configurado, los agentes funcionan con el motor local de demostración: ejecutan sus herramientas "
            "reales (lectura de fuentes, FFmpeg, composición, validaciones) pero no redactan ni valoran contenido; donde haría "
            "falta un modelo dejan marcadores entre [corchetes]."
        ),
    }


@router.get("/integrations")
def integrations(auth: Current, db: Db) -> dict[str, Any]:
    settings = get_settings()
    tool = ffmpeg.locate()
    return {
        "platforms": list(registry.all_statuses(db, auth.workspace_id).values()),
        "llm": _llm_payload(db, auth),
        "transcription": transcription.status(),
        "ffmpeg": {
            "available": tool is not None,
            "version": tool.version if tool else "",
            "source": tool.source if tool else "",
            "alternative": "Sin FFmpeg no se puede analizar ni exportar vídeo en el servidor: sube las piezas ya editadas desde el estudio.",
        },
        "base_url": settings.app_base_url,
        "base_url_public": instagram.base_url_is_public(),
        "can_manage": auth.role == "owner",
    }


@router.post("/integrations/{provider}/connect")
def connect(provider: str, auth: Owner, db: Db) -> dict[str, str]:
    module = registry.CONNECTORS.get(provider)
    if module is None:
        raise ApiError(404, "Integración desconocida.", "not_found")
    missing = module.missing_settings()
    if missing:
        raise ApiError(
            409,
            "Faltan las credenciales de la app de desarrollador en el servidor: "
            + ", ".join(missing)
            + ".",
            "needs_setup",
            missing=missing,
        )
    state, verifier = new_token(), new_token() + new_token()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    for stale in db.scalars(
        select(OAuthState).where(
            OAuthState.created_at < utcnow() - timedelta(minutes=30)
        )
    ):
        db.delete(stale)
    db.add(
        OAuthState(
            state=state,
            workspace_id=auth.workspace_id,
            user_id=auth.user.id,
            provider=provider,
            code_verifier=verifier,
        )
    )
    auth.log(
        db,
        "integration.connect_started",
        f"{auth.user.name} inició la conexión con {module.NAME}.",
        data={"provider": provider},
    )
    return {"url": module.authorize_url(state, challenge)}


def _back(result: str, provider: str, message: str = "") -> RedirectResponse:
    target = f"/integraciones?{result}={quote(provider)}"
    if message:
        target += f"&mensaje={quote(message[:300])}"
    return RedirectResponse(target, status_code=303)


@router.get("/integrations/{provider}/callback")
def oauth_callback(
    provider: str,
    request: Request,
    auth: Current,
    db: Db,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
) -> RedirectResponse:
    module = registry.CONNECTORS.get(provider)
    if module is None:
        return _back("error", provider, "Integración desconocida.")
    saved = db.get(OAuthState, state) if state else None
    if saved is not None:
        db.delete(saved)  # single use
    if error:
        return _back(
            "error",
            provider,
            f"{module.NAME} no concedió el acceso: {error_description or error}",
        )
    if (
        saved is None
        or saved.provider != provider
        or saved.user_id != auth.user.id
        or saved.workspace_id != auth.workspace_id
        or saved.created_at < utcnow() - timedelta(minutes=15)
    ):
        return _back(
            "error",
            provider,
            "La solicitud de conexión no es válida o ha caducado. Vuelve a intentarlo.",
        )
    if not code:
        return _back(
            "error", provider, "La plataforma no devolvió un código de autorización."
        )
    try:
        bundle: TokenBundle = module.exchange_code(code, saved.code_verifier)
        account = module.account(bundle)
    except ConnectorError as exc:
        return _back("error", provider, exc.user_message)
    except Exception:
        logger.exception("OAuth callback for %s failed", provider)
        return _back(
            "error",
            provider,
            "No se pudo completar la conexión por un error inesperado. Inténtalo de nuevo.",
        )
    if provider == "instagram" and not bundle.extra.get("user_id"):
        bundle.extra["user_id"] = account.id
    connection = registry.connection_for(db, auth.workspace_id, provider)
    if connection is None:
        connection = Connection(
            workspace_id=auth.workspace_id, provider=provider, created_by=auth.user.id
        )
        db.add(connection)
    connection.status, connection.error = "connected", ""
    connection.account_id, connection.account_name = account.id, account.name
    connection.config = {"account": account.extra}
    registry.store_bundle(connection, bundle)
    if not bundle.scopes:
        connection.scopes = list(module.SCOPES)
    auth.log(
        db,
        "integration.connected",
        f"{auth.user.name} conectó {module.NAME} ({account.name}).",
        data={"provider": provider, "account": account.name},
    )
    return _back("conectado", provider)


@router.delete("/integrations/{provider}")
def disconnect(provider: str, auth: Owner, db: Db) -> dict[str, Any]:
    if provider == "llm":
        connection = db.scalar(
            select(Connection).where(
                Connection.workspace_id == auth.workspace_id,
                Connection.provider == "llm",
            )
        )
        if connection is not None:
            db.delete(connection)
            auth.log(
                db,
                "integration.llm_removed",
                f"{auth.user.name} quitó el proveedor de IA del espacio.",
            )
        db.flush()
        return {"llm": _llm_payload(db, auth)}
    module = registry.CONNECTORS.get(provider)
    if module is None:
        raise ApiError(404, "Integración desconocida.", "not_found")
    connection = registry.connection_for(db, auth.workspace_id, provider)
    note = ""
    if connection is not None:
        try:
            module.revoke(TokenBundle.from_secret(decrypt_json(connection.secret)))
        except Exception:
            note = "No se pudo revocar el acceso en la plataforma; retíralo también desde los ajustes de tu cuenta."
        pending = db.scalar(
            select(Publication.id)
            .where(
                Publication.workspace_id == auth.workspace_id,
                Publication.status == "scheduled",
                Publication.platform.in_(
                    [k for k, v in PLATFORMS.items() if v["provider"] == provider]
                ),
            )
            .limit(1)
        )
        if pending is not None:
            note = (
                (note + " " if note else "")
                + "Hay publicaciones programadas para esta cuenta: no saldrán automáticamente."
            )
        db.delete(connection)
        auth.log(
            db,
            "integration.disconnected",
            f"{auth.user.name} desconectó {module.NAME}.",
            data={"provider": provider},
        )
    db.flush()
    return {"platform": registry.status(db, auth.workspace_id, provider), "note": note}


class LlmSettings(BaseModel):
    model: str = Field(min_length=2, max_length=120)
    api_key: str | None = Field(default=None, max_length=400)
    base_url: str = Field(default="", max_length=400)
    price_input_per_mtok: float | None = Field(default=None, ge=0)
    price_output_per_mtok: float | None = Field(default=None, ge=0)


@router.put("/integrations/llm")
def save_llm(body: LlmSettings, auth: Owner, db: Db) -> dict[str, Any]:
    model = body.model.strip()
    base_url = body.base_url.strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ApiError(
            422,
            "La URL base debe empezar por http:// o https://.",
            "bad_url",
            field="base_url",
        )
    connection = db.scalar(
        select(Connection).where(
            Connection.workspace_id == auth.workspace_id, Connection.provider == "llm"
        )
    )
    if connection is None:
        connection = Connection(
            workspace_id=auth.workspace_id, provider="llm", created_by=auth.user.id
        )
        db.add(connection)
    connection.config = {
        "model": model,
        "base_url": base_url,
        "price_input_per_mtok": body.price_input_per_mtok,
        "price_output_per_mtok": body.price_output_per_mtok,
    }
    if (
        body.api_key is not None
    ):  # ``None`` keeps the stored key; an empty string removes it
        connection.secret = (
            encrypt_json({"api_key": body.api_key.strip()})
            if body.api_key.strip()
            else ""
        )
    connection.status, connection.account_name = "connected", model
    auth.log(
        db,
        "integration.llm_saved",
        f"{auth.user.name} configuró el proveedor de IA: {model}.",
        data={"model": model},
    )
    db.flush()
    return {"llm": _llm_payload(db, auth)}


@router.post("/integrations/llm/test")
def test_llm(auth: Owner, db: Db) -> dict[str, Any]:
    """One tiny real request to the configured provider; reports exactly what came back."""
    info = describe_engine(db, auth.workspace_id)
    if info.is_demo:
        return {
            "ok": False,
            "detail": "No hay ningún proveedor de IA configurado: los agentes usan el motor local de demostración.",
        }
    try:
        llm, _info = build_llm(db, auth.workspace_id)
        reply = llm.call(
            [{"role": "user", "content": "Reply with the single word: OK"}]
        )
    except Exception as exc:
        record_event(
            db,
            workspace_id=auth.workspace_id,
            actor_type="system",
            type="integration.llm_test_failed",
            message=f"La prueba del proveedor de IA ({info.model}) falló: {str(exc)[:300]}",
        )
        return {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:500]}"}
    text = str(reply).strip()
    return {
        "ok": bool(text),
        "detail": f"El modelo {info.model} respondió: «{text[:120]}»"
        if text
        else "El modelo devolvió una respuesta vacía.",
    }
