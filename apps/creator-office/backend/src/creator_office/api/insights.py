"""Community (comments and replies) and analytics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from creator_office.api import serializers
from creator_office.api.deps import (
    ApiError,
    Auth,
    Current,
    Db,
    Editor,
    owned,
    project_of,
)
from creator_office.connectors import registry
from creator_office.connectors.base import ConnectorError
from creator_office.database import utcnow
from creator_office.events import record_event
from creator_office.models import (
    Comment,
    Metric,
    Project,
    Publication,
    Recommendation,
    Step,
)
from creator_office.pipeline import engine
from creator_office.pipeline.catalog import PLATFORMS
from creator_office.services import analytics
from creator_office.services import community as community_service
from creator_office.services import publishing as publishing_service
from creator_office.text import choose, plural, shorten

router = APIRouter(prefix="/api", tags=["insights"])

REPLY_STATUS_LABELS = {
    "none": "Sin respuesta",
    "suggested": "Borrador sin aprobar",
    "approved": "Aprobada, sin enviar",
    "sent": "Enviada (confirmada por la plataforma)",
    "sent_manual": "Enviada a mano",
    "rejected": "Descartada",
}


# --- Comments ----------------------------------------------------------------


def _comment_sources(db: Session, workspace_id: str) -> list[dict[str, Any]]:
    """Per platform: can comments be read and answered through the API, and if not, why."""
    out = []
    for provider, status in registry.all_statuses(db, workspace_id).items():
        caps = status["capabilities"]
        if not caps["comments"]:
            state, detail = "unsupported", caps["comments_note"]
        elif status["state"] != "connected":
            state, detail = "not_connected", f"{status['label']}. {status['detail']}"
        else:
            state, detail = "ready", f"Conectado como {status['account_name']}."
        out.append(
            {
                "provider": provider,
                "name": status["name"],
                "state": state,
                "detail": detail,
                "can_reply": caps["reply"],
            }
        )
    return out


AUTO_SYNC_HOURS = (0, 6, 12, 24)


def _automation(auth: Auth) -> dict[str, Any]:
    """The one standing rule of the office. It is off until someone switches it on."""
    settings = auth.workspace.settings or {}
    last = settings.get("last_comment_sync")
    return {
        "sync_comments_hours": int(
            (settings.get("automations") or {}).get("sync_comments_hours") or 0
        ),
        "options": list(AUTO_SYNC_HOURS),
        "last_run_at": serializers.iso(datetime.fromtimestamp(float(last), tz=UTC))
        if last
        else None,
    }


class Automation(BaseModel):
    sync_comments_hours: int


@router.put("/comments/automation")
def set_automation(body: Automation, auth: Editor, db: Db) -> dict[str, Any]:
    """Read comments on a timer. It only reads: replies still need a person every time."""
    if body.sync_comments_hours not in AUTO_SYNC_HOURS:
        raise ApiError(
            422, "Elige una de las frecuencias disponibles.", "invalid_frequency"
        )
    settings = dict(auth.workspace.settings or {})
    settings["automations"] = {
        **(settings.get("automations") or {}),
        "sync_comments_hours": body.sync_comments_hours,
    }
    settings.pop(
        "last_comment_sync", None
    )  # a changed rule runs on the next scheduler tick, then every N hours
    auth.workspace.settings = settings
    auth.log(
        db,
        "automation.changed",
        f"{auth.user.name} activó la sincronización automática de comentarios cada {body.sync_comments_hours} h."
        if body.sync_comments_hours
        else f"{auth.user.name} desactivó la sincronización automática de comentarios.",
        data={"sync_comments_hours": body.sync_comments_hours},
    )
    db.flush()
    return _automation(auth)


@router.get("/comments")
def list_comments(
    auth: Current,
    db: Db,
    project_id: str = "",
    category: str = "",
    reply_status: str = "",
) -> dict[str, Any]:
    base = select(Comment).where(Comment.workspace_id == auth.workspace_id)
    if project_id:
        base = base.where(Comment.project_id == project_id)
    query = base
    if category:
        query = query.where(
            Comment.category == ("" if category == "pending" else category)
        )
    if reply_status:
        query = query.where(Comment.reply_status == reply_status)
    rows = list(db.scalars(query.order_by(Comment.created_at.desc()).limit(300)))
    projects = {
        p.id: p.title
        for p in db.scalars(
            select(Project).where(Project.workspace_id == auth.workspace_id)
        )
    }
    counts_query = select(Comment.category, func.count(Comment.id)).where(
        Comment.workspace_id == auth.workspace_id
    )
    if project_id:
        counts_query = counts_query.where(Comment.project_id == project_id)
    counts = {
        category or "pending": count
        for category, count in db.execute(counts_query.group_by(Comment.category))
    }
    return {
        "comments": [
            {**serializers.comment(c), "project": projects.get(c.project_id, "")}
            for c in rows
        ],
        "counts": counts,
        "total": sum(counts.values()),
        "sources": _comment_sources(db, auth.workspace_id),
        "reply_status_labels": REPLY_STATUS_LABELS,
        "automation": _automation(auth),
    }


class CommentImport(BaseModel):
    text: str = Field(max_length=200_000)
    platform: str = ""
    publication_id: str = ""


@router.post("/projects/{project_id}/comments/import")
def import_comments(
    project_id: str, body: CommentImport, auth: Editor, db: Db
) -> dict[str, Any]:
    project = project_of(db, auth, project_id)
    platform = body.platform
    publication_id = None
    if body.publication_id:
        pub = owned(db, auth, Publication, body.publication_id, "La publicación")
        if pub.project_id != project.id:
            raise ApiError(
                404, "La publicación no pertenece a este proyecto.", "not_found"
            )
        publication_id, platform = pub.id, pub.platform
    if platform and platform not in PLATFORMS:
        raise ApiError(422, "Plataforma desconocida.", "bad_platform")
    rows = community_service.parse_import(body.text)
    if not rows:
        raise ApiError(
            422,
            "No se encontró ningún comentario. Pega una línea por comentario («autor: texto») o un CSV con la columna «texto».",
            "empty",
        )
    existing = {
        (a, t)
        for a, t in db.execute(
            select(Comment.author, Comment.text).where(Comment.project_id == project.id)
        )
    }
    added = 0
    for row in rows[:1000]:
        if (row["author"], row["text"]) in existing:
            continue
        existing.add((row["author"], row["text"]))
        posted_at = None
        if row.get("posted_at"):
            try:
                posted_at = datetime.fromisoformat(row["posted_at"])
                posted_at = (
                    posted_at
                    if posted_at.tzinfo
                    else posted_at.replace(tzinfo=utcnow().tzinfo)
                )
            except ValueError:
                posted_at = None
        db.add(
            Comment(
                workspace_id=auth.workspace_id,
                project_id=project.id,
                publication_id=publication_id,
                platform=platform,
                source="manual",
                author=row["author"][:200],
                text=row["text"],
                posted_at=posted_at,
            )
        )
        added += 1
    duplicates = len(rows[:1000]) - added
    auth.log(
        db,
        "comments.imported",
        f"{auth.user.name} importó a mano {plural(added, 'comentario')} en «{project.title}»"
        + (f" ({duplicates} repetidos, omitidos)." if duplicates else "."),
        project_id=project.id,
        data={"added": added, "duplicates": duplicates},
    )
    db.flush()
    if added:
        engine.wake_data_step(db, project, "community")
    return {"added": added, "duplicates": duplicates}


@router.post("/projects/{project_id}/comments/sync")
def sync_comments(project_id: str, auth: Editor, db: Db) -> dict[str, Any]:
    project = project_of(db, auth, project_id)
    posts, readable = publishing_service.readable_comment_posts(
        db, auth.workspace_id, project.id
    )
    if not posts:
        raise ApiError(
            409,
            "Este proyecto no tiene publicaciones confirmadas por una plataforma, así que no hay de dónde leer comentarios. "
            "Alternativa: impórtalos a mano.",
            "nothing_to_sync",
        )
    if not readable:
        raise ApiError(
            409,
            "Ninguna de las plataformas de este proyecto permite leer comentarios con la conexión actual. Alternativa: impórtalos a mano.",
            "not_supported",
        )
    engine.enqueue_job(
        db,
        workspace_id=auth.workspace_id,
        project_id=project.id,
        agent="community",
        kind="sync_comments",
        title=f"Sincronizar comentarios · {project.title}",
        payload={},
        engine="connector",
        user_id=auth.user.id,
    )
    auth.log(
        db,
        "comments.sync_requested",
        f"{auth.user.name} pidió sincronizar los comentarios de «{project.title}».",
        project_id=project.id,
    )
    return {"ok": True}


class CommentPatch(BaseModel):
    category: str | None = None
    reply_text: str | None = Field(default=None, max_length=5000)


@router.patch("/comments/{comment_id}")
def edit_comment(
    comment_id: str, body: CommentPatch, auth: Editor, db: Db
) -> dict[str, Any]:
    comment = owned(db, auth, Comment, comment_id, "El comentario")
    if body.category is not None:
        if body.category not in community_service.CATEGORIES:
            raise ApiError(422, "Categoría desconocida.", "bad_category")
        comment.category, comment.category_by = body.category, "user"
        comment.category_reason = f"Clasificado a mano por {auth.user.name}."
    if body.reply_text is not None:
        if comment.reply_status in ("sent", "sent_manual"):
            raise ApiError(
                409,
                "La respuesta ya se envió y no se puede cambiar desde aquí.",
                "already_sent",
            )
        comment.reply_text = body.reply_text.strip()
        comment.reply_by = auth.user.name
        comment.reply_status = "suggested" if comment.reply_text else "none"
    return {"comment": serializers.comment(comment)}


class ReplyAction(BaseModel):
    action: str  # approve | send | mark_sent | reject


@router.post("/comments/{comment_id}/reply")
def reply_action(
    comment_id: str, body: ReplyAction, auth: Editor, db: Db
) -> dict[str, Any]:
    comment = owned(db, auth, Comment, comment_id, "El comentario")
    author = comment.author or "un comentario"
    if comment.reply_status in ("sent", "sent_manual") and body.action != "reject":
        raise ApiError(409, "La respuesta ya consta como enviada.", "already_sent")
    if (
        body.action in ("approve", "send", "mark_sent")
        and not comment.reply_text.strip()
    ):
        raise ApiError(422, "Escribe la respuesta antes de aprobarla.", "empty_reply")
    if (
        "[" in comment.reply_text
        and "]" in comment.reply_text
        and body.action in ("approve", "send")
    ):
        raise ApiError(
            422,
            "La respuesta todavía tiene marcadores entre corchetes [ ] sin completar.",
            "placeholders",
        )
    if body.action == "send" and comment.reply_status != "approved":
        # Two separate decisions, as with publications: approving the text, then sending it.
        raise ApiError(409, "Aprueba la respuesta antes de enviarla.", "not_approved")

    if body.action == "approve":
        comment.reply_status = "approved"
        auth.log(
            db,
            "reply.approved",
            f"{auth.user.name} aprobó la respuesta a {author}.",
            project_id=comment.project_id,
            data={"comment_id": comment.id},
        )

    elif body.action == "send":
        # Sending is the person's explicit action, and only the platform's answer marks it as sent.
        provider = PLATFORMS.get(comment.platform, {}).get("provider")
        if comment.source != "platform" or not comment.external_id or provider is None:
            raise ApiError(
                409,
                "Este comentario se importó a mano: responde desde la plataforma y márcalo como enviado.",
                "manual_only",
            )
        module = registry.CONNECTORS[provider]
        if not module.CAPABILITIES.reply:
            raise ApiError(
                409,
                f"{module.NAME} no permite responder comentarios desde su API. Responde desde la app oficial.",
                "not_supported",
            )
        try:
            _connection, bundle = registry.get_bundle(db, auth.workspace_id, provider)
            reply_id = module.reply(bundle, comment.external_id, comment.reply_text)
        except ConnectorError as exc:
            if exc.code == "auth_expired":
                registry.mark_expired(db, auth.workspace_id, provider, exc.user_message)
            db.commit()
            raise ApiError(
                502,
                f"{module.NAME} no aceptó la respuesta: {exc.user_message}",
                "platform_error",
            ) from exc
        comment.reply_status, comment.reply_sent_at = "sent", utcnow()
        auth.log(
            db,
            "reply.send_requested",
            f"{auth.user.name} envió la respuesta a {author}.",
            project_id=comment.project_id,
            data={"comment_id": comment.id},
        )
        record_event(
            db,
            workspace_id=auth.workspace_id,
            project_id=comment.project_id,
            actor_type="platform",
            actor=comment.platform,
            type="reply.sent",
            message=f"{module.NAME} confirmó la respuesta a {author}.",
            data={"comment_id": comment.id, "reply_id": reply_id},
        )

    elif body.action == "mark_sent":
        comment.reply_status, comment.reply_sent_at = "sent_manual", utcnow()
        auth.log(
            db,
            "reply.marked_sent",
            f"{auth.user.name} declaró haber respondido a mano a {author}.",
            project_id=comment.project_id,
            data={"comment_id": comment.id},
        )

    elif body.action == "reject":
        if comment.reply_status in ("sent", "sent_manual"):
            raise ApiError(409, "La respuesta ya se envió.", "already_sent")
        comment.reply_status = "rejected"
        auth.log(
            db,
            "reply.rejected",
            f"{auth.user.name} descartó la respuesta sugerida a {author}.",
            project_id=comment.project_id,
            data={"comment_id": comment.id},
        )
    else:
        raise ApiError(422, "Acción desconocida.", "bad_action")
    return {"comment": serializers.comment(comment)}


# --- Analytics ---------------------------------------------------------------


def _recommendation(row: Recommendation, projects: dict[str, str]) -> dict[str, Any]:
    return {
        "id": row.id,
        "text": row.text,
        "evidence": row.evidence or [],
        "basis": row.basis,
        "status": row.status,
        "project": projects.get(row.project_id or "", ""),
        "created_at": serializers.iso(row.created_at),
    }


@router.get("/analytics")
def analytics_overview(auth: Current, db: Db, project_id: str = "") -> dict[str, Any]:
    if project_id:
        project_of(db, auth, project_id)
    stats = analytics.compute(db, auth.workspace_id, project_id or None)
    projects = {
        p.id: p.title
        for p in db.scalars(
            select(Project).where(Project.workspace_id == auth.workspace_id)
        )
    }
    recommendations = list(
        db.scalars(
            select(Recommendation)
            .where(Recommendation.workspace_id == auth.workspace_id)
            .order_by(Recommendation.created_at.desc())
            .limit(60)
        )
    )
    published = list(
        db.scalars(
            select(Publication)
            .where(
                Publication.workspace_id == auth.workspace_id,
                Publication.status == "published",
            )
            .order_by(Publication.published_at.desc())
            .limit(200)
        )
    )
    with_metrics = {i["publication_id"] for i in stats["publications"]}
    reports = []
    for step in db.scalars(
        select(Step).where(
            Step.workspace_id == auth.workspace_id,
            Step.kind == "analysis",
            Step.status == "completed",
        )
    ):
        if project_id and step.project_id != project_id:
            continue
        output = step.output or {}
        reports.append(
            {
                "project_id": step.project_id,
                "project": projects.get(step.project_id, ""),
                "summary": output.get("summary", ""),
                "data_gaps": output.get("data_gaps", []),
                "engine": output.get("engine", {}),
                "finished_at": serializers.iso(step.finished_at),
            }
        )
    reports.sort(key=lambda r: r["finished_at"] or "", reverse=True)
    statuses = registry.all_statuses(db, auth.workspace_id)
    return {
        "stats": stats,
        "recommendations": [_recommendation(r, projects) for r in recommendations],
        "reports": reports,
        "published": [
            {
                "id": p.id,
                "project_id": p.project_id,
                "project": projects.get(p.project_id, ""),
                "platform": p.platform,
                "platform_name": PLATFORMS[p.platform]["name"],
                "title": shorten(p.title or p.description, 80),
                "published_at": serializers.iso(p.published_at),
                "confirmation": p.confirmation,
                "url": p.url,
                "has_metrics": p.id in with_metrics,
                "can_sync": bool(p.platform_post_id)
                and statuses[PLATFORMS[p.platform]["provider"]]["state"] == "connected",
            }
            for p in published
            if not project_id or p.project_id == project_id
        ],
        "sources": [
            {
                "provider": provider,
                "name": status["name"],
                "connected": status["state"] == "connected",
                "label": status["label"],
                "metrics": status["capabilities"]["metrics"],
                "metrics_missing": status["capabilities"]["metrics_missing"],
            }
            for provider, status in statuses.items()
        ],
        "fields": analytics.METRIC_FIELDS,
    }


class MetricInput(BaseModel):
    values: dict[str, Any]
    captured_at: datetime | None = None


@router.post("/publications/{publication_id}/metrics")
def add_metrics(
    publication_id: str, body: MetricInput, auth: Editor, db: Db
) -> dict[str, Any]:
    pub = owned(db, auth, Publication, publication_id, "La publicación")
    if pub.status != "published":
        raise ApiError(
            409,
            "Solo se pueden añadir métricas a una publicación ya publicada.",
            "not_published",
        )
    # What a person types is either stored as typed or refused by name: nothing is dropped in silence.
    invalid = analytics.invalid_fields(body.values)
    if invalid:
        raise ApiError(
            422,
            f"Revisa {choose(len(invalid), 'esta cifra', 'estas cifras')}: {', '.join(invalid)}. Deben ser números positivos (enteros en los recuentos); la retención, entre 0 y 100.",
            "invalid_values",
        )
    values = analytics.clean_values(body.values)
    if not values:
        raise ApiError(422, "Indica al menos una cifra.", "empty")
    captured = body.captured_at or utcnow()
    captured = captured if captured.tzinfo else captured.replace(tzinfo=utcnow().tzinfo)
    if captured > utcnow() + timedelta(minutes=5):
        raise ApiError(
            422, "La fecha en que miraste las cifras no puede ser futura.", "bad_date"
        )
    db.add(
        Metric(
            workspace_id=auth.workspace_id,
            publication_id=pub.id,
            source="manual",
            values=values,
            captured_at=captured,
            created_by=auth.user.name,
        )
    )
    auth.log(
        db,
        "metrics.added",
        f"{auth.user.name} añadió a mano métricas de «{shorten(pub.title or pub.description)}» ({PLATFORMS[pub.platform]['name']}).",
        project_id=pub.project_id,
        data={"publication_id": pub.id, "fields": sorted(values)},
    )
    db.flush()
    project = db.get(Project, pub.project_id)
    if project is not None:
        engine.wake_data_step(db, project, "analysis")
    return {"ok": True}


class SyncRequest(BaseModel):
    project_id: str = ""


def _syncable(db: Session, auth: Auth, project_id: str) -> bool:
    statuses = registry.all_statuses(db, auth.workspace_id)
    query = select(Publication).where(
        Publication.workspace_id == auth.workspace_id,
        Publication.status == "published",
        Publication.platform_post_id != "",
    )
    if project_id:
        query = query.where(Publication.project_id == project_id)
    return any(
        statuses[PLATFORMS[p.platform]["provider"]]["state"] == "connected"
        for p in db.scalars(query)
    )


@router.post("/analytics/sync")
def sync_metrics(body: SyncRequest, auth: Editor, db: Db) -> dict[str, Any]:
    project = project_of(db, auth, body.project_id) if body.project_id else None
    if not _syncable(db, auth, body.project_id):
        raise ApiError(
            409,
            "No hay publicaciones confirmadas por una plataforma con la cuenta conectada, así que no se pueden leer métricas. "
            "Alternativa: añade las cifras a mano en cada publicación.",
            "nothing_to_sync",
        )
    engine.enqueue_job(
        db,
        workspace_id=auth.workspace_id,
        project_id=project.id if project else None,
        agent="analyst",
        kind="sync_metrics",
        title="Sincronizar métricas" + (f" · {project.title}" if project else ""),
        payload={},
        engine="connector",
        user_id=auth.user.id,
    )
    auth.log(
        db,
        "metrics.sync_requested",
        f"{auth.user.name} pidió sincronizar las métricas.",
        project_id=project.id if project else None,
    )
    return {"ok": True}


class RecommendationPatch(BaseModel):
    status: str


@router.patch("/recommendations/{recommendation_id}")
def decide_recommendation(
    recommendation_id: str, body: RecommendationPatch, auth: Editor, db: Db
) -> dict[str, Any]:
    row = owned(db, auth, Recommendation, recommendation_id, "La recomendación")
    if body.status not in ("new", "accepted", "dismissed"):
        raise ApiError(422, "Estado desconocido.", "bad_status")
    row.status = body.status
    if body.status == "accepted":
        auth.log(
            db,
            "recommendation.accepted",
            f"{auth.user.name} aceptó una recomendación de Analyst: los agentes la tendrán en cuenta en próximos proyectos.",
            data={"recommendation_id": row.id},
        )
    elif body.status == "dismissed":
        auth.log(
            db,
            "recommendation.dismissed",
            f"{auth.user.name} descartó una recomendación de Analyst.",
            data={"recommendation_id": row.id},
        )
    return {"recommendation": _recommendation(row, {})}
