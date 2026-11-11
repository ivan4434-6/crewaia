"""Publications: drafts, validation, readiness, manual export and API publishing.

A publication only becomes "published" in two ways, and the record says which:
the platform's API returned an identifier (``confirmation="platform"``), or a
person stated they posted it themselves (``confirmation="manual"``).
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from creator_office import clock, storage
from creator_office.agents.context import RunContext
from creator_office.connectors import instagram, registry
from creator_office.connectors import platform_specs as specs
from creator_office.connectors.base import ConnectorError, PublishRequest, PublishResult
from creator_office.database import session_scope, utcnow
from creator_office.events import notify, record_event
from creator_office.models import (
    Asset,
    Clip,
    Comment,
    Design,
    Document,
    DocumentVersion,
    Metric,
    Project,
    Publication,
    Render,
    Workspace,
)
from creator_office.pipeline.catalog import PLATFORMS
from creator_office.services import analytics
from creator_office.settings import get_settings
from creator_office.text import shorten

STATUS_LABELS = {
    "draft": "Borrador",
    "scheduled": "Programado",
    "publishing": "Publicando",
    "published": "Publicado",
    "error": "Error",
    "discarded": "Descartado",
}


def content(pub: Publication) -> dict[str, Any]:
    return {
        "title": pub.title,
        "description": pub.description,
        "tags": list(pub.tags or []),
        "options": dict(pub.options or {}),
    }


# --- Drafts and versions -----------------------------------------------------


def ensure_publications(session: Session, project: Project) -> list[Publication]:
    """One draft per ready piece and platform that uses that piece's format."""
    existing = {
        (p.platform, p.render_id): p
        for p in session.scalars(
            select(Publication).where(
                Publication.project_id == project.id, Publication.status != "discarded"
            )
        )
    }
    renders = list(
        session.scalars(
            select(Render)
            .where(Render.project_id == project.id, Render.status == "ready")
            .order_by(Render.created_at)
        )
    )
    designs = {
        d.kind: d
        for d in session.scalars(
            select(Design)
            .where(Design.project_id == project.id, Design.status == "ready")
            .order_by(Design.created_at)
        )
    }
    created: list[Publication] = []
    for platform in project.platforms:
        spec = specs.SPECS[platform]
        for render in renders:
            if render.format != spec.video_format:
                continue
            if (platform, render.id) in existing:
                pub = existing[(platform, render.id)]
            else:
                pub = Publication(
                    workspace_id=project.workspace_id,
                    project_id=project.id,
                    platform=platform,
                    render_id=render.id,
                )
                session.add(pub)
                created.append(pub)
            design = designs.get(spec.cover)
            if design is not None and pub.design_id is None:
                pub.design_id = design.id
    session.flush()
    return created


def save_version(
    session: Session,
    pub: Publication,
    *,
    author_type: str,
    author: str,
    note: str = "",
    provider: str = "",
) -> None:
    document = session.scalar(
        select(Document).where(
            Document.kind == "publication", Document.ref_id == pub.id
        )
    )
    if document is None:
        document = Document(
            workspace_id=pub.workspace_id,
            project_id=pub.project_id,
            kind="publication",
            ref_id=pub.id,
            title=f"Publicación · {PLATFORMS[pub.platform]['name']}",
        )
        session.add(document)
        session.flush()
    number = (
        session.scalar(
            select(func.max(DocumentVersion.number)).where(
                DocumentVersion.document_id == document.id
            )
        )
        or 0
    ) + 1
    version = DocumentVersion(
        document_id=document.id,
        number=number,
        content=content(pub),
        author_type=author_type,
        author=author,
        note=note,
        provider=provider,
    )
    session.add(version)
    session.flush()
    document.current_version_id = version.id


# --- What an approval covers ---------------------------------------------------
#
# Approving a publication approves its text, its video and its cover. Every
# export and every recomposition writes a new file, so the ids of those files
# say exactly which video and cover a person saw. Three rules follow:
#   - a draft whose video or cover changes loses its approval;
#   - a publication waiting for its date (or being uploaded) keeps its files:
#     they cannot be replaced until it is unscheduled;
#   - a publication that already went out remembers the files it went out with.


def media_fingerprint(session: Session, pub: Publication) -> dict[str, str]:
    """The video and cover files this publication would send right now."""
    render = session.get(Render, pub.render_id) if pub.render_id else None
    design = session.get(Design, pub.design_id) if pub.design_id else None
    return {
        "video": (render.asset_id or "") if render else "",
        "cover": (design.asset_id or "") if design else "",
    }


def _changed(before: Any, now: dict[str, str]) -> list[str]:
    if not isinstance(before, dict):
        return []
    return [
        label
        for key, label in (("video", "el vídeo"), ("cover", "la portada"))
        if before.get(key, "") != now[key]
    ]


def changed_since_approval(session: Session, pub: Publication) -> list[str]:
    """What is no longer what the person approved: "el vídeo", "la portada". Empty when the approval still holds."""
    if pub.approved_at is None:
        return []
    return _changed(
        (pub.options or {}).get("_approved_media"), media_fingerprint(session, pub)
    )


def changed_since_publication(session: Session, pub: Publication) -> list[str]:
    """What was replaced in the workspace after this publication went out."""
    return _changed(
        (pub.options or {}).get("_published_media"), media_fingerprint(session, pub)
    )


def remember_approved(session: Session, pub: Publication) -> None:
    pub.options = {
        **(pub.options or {}),
        "_approved_media": media_fingerprint(session, pub),
    }


def remember_published(session: Session, pub: Publication) -> None:
    """Keep the facts of what went out; the piece may be exported again later for other platforms."""
    _, asset = _render_media(session, pub)
    media = ((asset.meta or {}).get("media") or {}) if asset else {}
    video = media.get("video") or {}
    pub.options = {
        **(pub.options or {}),
        "_published_media": {
            **media_fingerprint(session, pub),
            "duration": media.get("duration"),
            "width": video.get("width"),
            "height": video.get("height"),
            "size": asset.size if asset else None,
        },
    }


def is_sent(pub: Publication) -> bool:
    """The files have left: published, or uploaded and held by the platform until its date."""
    return pub.status == "published" or (
        pub.status == "scheduled"
        and (pub.options or {}).get("schedule_mode") == "platform"
    )


def committed_reason(
    session: Session, *, render_id: str = "", design_id: str = ""
) -> str:
    """Why this video or cover must stay as it is right now; empty when it may change."""
    column = (
        Publication.render_id == render_id
        if render_id
        else Publication.design_id == design_id
    )
    what = "Esta pieza" if render_id else "Este diseño"
    for pub in session.scalars(
        select(Publication).where(
            column, Publication.status.in_(("scheduled", "publishing"))
        )
    ):
        name = PLATFORMS[pub.platform]["name"]
        if pub.status == "publishing":
            return f"{what} se está publicando ahora mismo en {name}. Espera a que termine."
        if not is_sent(pub):
            return f"{what} está en una publicación programada de {name}, que ya tiene el contenido aprobado. Desprográmala antes de cambiarlo."
    return ""


def recheck_approvals(session: Session, project_id: str) -> int:
    """A video or a cover of the project was replaced: approvals given to the previous file no longer hold."""
    withdrawn = 0
    rows = session.scalars(
        select(Publication).where(
            Publication.project_id == project_id,
            Publication.status.in_(("draft", "error")),
            Publication.approved_at.is_not(None),
        )
    )
    for pub in rows:
        changed = changed_since_approval(session, pub)
        if not changed:
            continue
        pub.approved_at, pub.approved_by = None, None
        validate(session, pub)
        withdrawn += 1
        record_event(
            session,
            workspace_id=pub.workspace_id,
            project_id=pub.project_id,
            actor_type="orchestrator",
            actor="orchestrator",
            type="publication.approval_withdrawn",
            message=(
                f"Se retiró la aprobación de la publicación de {PLATFORMS[pub.platform]['name']} «{shorten(pub.title or pub.description)}» "
                f"porque cambió {' y '.join(changed)} después de aprobarla. Hay que revisarla y aprobarla de nuevo."
            ),
            data={"publication_id": pub.id, "changed": changed},
        )
    return withdrawn


# --- Validation and readiness ------------------------------------------------


def _render_media(
    session: Session, pub: Publication
) -> tuple[Render | None, Asset | None]:
    render = session.get(Render, pub.render_id) if pub.render_id else None
    asset = (
        session.get(Asset, render.asset_id)
        if render and render.asset_id and render.status == "ready"
        else None
    )
    return render, asset


def validate(session: Session, pub: Publication) -> list[specs.Check]:
    render, asset = _render_media(session, pub)
    media = (asset.meta or {}).get("media") if asset else None
    checks = specs.validate_video(pub.platform, media, asset.size if asset else 0)
    checks += specs.validate_metadata(
        pub.platform, pub.title, pub.description, list(pub.tags or [])
    )
    spec = specs.SPECS[pub.platform]
    design = session.get(Design, pub.design_id) if pub.design_id else None
    if design is not None and design.status == "ready":
        failing = [c for c in design.checks if c["status"] == "fail"]
        if failing:
            checks.append(
                specs.check(
                    "cover",
                    "warn",
                    "Portada",
                    "La portada tiene avisos sin resolver: " + failing[0]["detail"],
                )
            )
        else:
            checks.append(
                specs.check(
                    "cover",
                    "pass",
                    "Portada",
                    "Miniatura lista."
                    if spec.cover == "thumbnail"
                    else "Portada lista.",
                )
            )
    else:
        checks.append(
            specs.check(
                "cover",
                "warn",
                "Portada",
                "Sin portada: la plataforma elegirá un fotograma del vídeo.",
            )
        )
    clip = session.get(Clip, render.clip_id) if render else None
    source = session.get(Asset, clip.asset_id) if clip else None
    if (
        source is not None
        and not source.rights_declared
        and render is not None
        and render.origin != "upload"
    ):
        checks.append(
            specs.check(
                "rights",
                "fail",
                "Derechos",
                "Falta declarar los derechos de uso del vídeo original.",
            )
        )
    elif source is not None or (render is not None and render.origin == "upload"):
        checks.append(
            specs.check(
                "rights",
                "pass",
                "Derechos",
                "Declaraste tener derechos sobre el material.",
            )
        )
    if pub.platform == "instagram_reels" and not instagram.base_url_is_public():
        checks.append(
            specs.check(
                "public_url",
                "info",
                "Acceso público",
                "Este servidor no es accesible desde internet, así que Instagram no puede descargar el vídeo por API. Usa la exportación manual.",
            )
        )
    pub.validation = checks
    return checks


def readiness(pub: Publication, connection_status: dict[str, Any]) -> dict[str, Any]:
    """Answer the question the UI must make obvious: can this really go out?"""
    checks = pub.validation or []
    failing = [c for c in checks if c["status"] == "fail"]
    blockers = [f"{c['label']}: {c['detail']}" for c in failing]
    content_ready = not failing
    approved = pub.approved_at is not None
    connected = connection_status["state"] == "connected"
    api_possible = connected and (
        pub.platform != "instagram_reels" or instagram.base_url_is_public()
    )
    if pub.status == "published":
        summary = (
            "Publicada (confirmada por la plataforma)"
            if pub.confirmation == "platform"
            else "Publicada (declarada a mano)"
        )
    elif not content_ready:
        summary = f"No está lista: {len(failing)} requisito{'s' if len(failing) != 1 else ''} sin cumplir"
    elif not approved:
        summary = "Contenido listo. Falta tu aprobación"
    elif api_possible:
        summary = "Lista para publicar con la cuenta conectada"
    elif connected:
        summary = (
            "Lista, pero Instagram no puede acceder a este servidor: exportación manual"
        )
    else:
        summary = "Lista para exportación manual (cuenta sin conectar)"
    return {
        "content_ready": content_ready,
        "approved": approved,
        "connected": connected,
        "can_publish_api": content_ready and approved and api_possible,
        "can_export": content_ready,
        "blockers": blockers,
        "warnings": [
            f"{c['label']}: {c['detail']}" for c in checks if c["status"] == "warn"
        ],
        "summary": summary,
        "connection_label": connection_status["label"],
        "connection_detail": connection_status["detail"],
        "account_name": connection_status.get("account_name", ""),
    }


# --- Manual export -----------------------------------------------------------


def export_zip(session: Session, pub: Publication) -> tuple[str, bytes]:
    """Bundle video, cover and texts for posting by hand from the official app."""
    _, asset = _render_media(session, pub)
    spec = specs.SPECS[pub.platform]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        if asset is not None:
            archive.write(storage.absolute_path(asset), arcname="video.mp4")
        design = session.get(Design, pub.design_id) if pub.design_id else None
        cover = (
            session.get(Asset, design.asset_id) if design and design.asset_id else None
        )
        if cover is not None:
            archive.write(storage.absolute_path(cover), arcname="portada.jpg")
        lines = [f"Plataforma: {spec.name}", ""]
        if spec.title_max:
            lines += ["TÍTULO" if spec.description_max else "TEXTO", pub.title, ""]
        if spec.description_max:
            # Platforms with a single text call it "Texto" everywhere else in the app.
            lines += ["DESCRIPCIÓN" if spec.title_max else "TEXTO", pub.description, ""]
        if pub.tags:
            lines += ["ETIQUETAS", ", ".join(pub.tags), ""]
        if pub.scheduled_at:
            workspace = session.get(Workspace, pub.workspace_id)
            lines += [
                "FECHA PREVISTA",
                clock.stamp(
                    pub.scheduled_at, workspace.settings if workspace else None
                ),
                "",
            ]
        lines += [
            "Este paquete se ha exportado para publicarlo a mano desde la app oficial.",
            "Después, marca la publicación como publicada en Creator Office AI e indica el enlace.",
        ]
        archive.writestr("textos.txt", "\n".join(lines))
    return f"publicacion-{pub.platform}-{pub.id}.zip", buffer.getvalue()


# --- API publishing ----------------------------------------------------------


def _fail(
    session: Session,
    pub: Publication,
    message: str,
    *,
    run_id: str | None,
    retryable: bool = False,
) -> None:
    pub.status, pub.error, pub.progress = "error", message, None
    record_event(
        session,
        workspace_id=pub.workspace_id,
        project_id=pub.project_id,
        run_id=run_id,
        actor_type="platform",
        actor=pub.platform,
        type="publication.failed",
        message=f"No se publicó «{shorten(pub.title or pub.description)}» en {PLATFORMS[pub.platform]['name']}: {message}",
        data={"publication_id": pub.id, "retryable": retryable},
    )
    notify(
        session,
        workspace_id=pub.workspace_id,
        type="error",
        title=f"Error al publicar en {PLATFORMS[pub.platform]['name']}",
        body=message,
        link=f"/proyectos/{pub.project_id}?tab=publicar",
    )


def _apply_result(
    session: Session, pub: Publication, result: PublishResult, run_id: str | None
) -> None:
    name = PLATFORMS[pub.platform]["name"]
    options = dict(pub.options or {})
    label = shorten(pub.title or pub.description)
    if result.state == "processing":
        options["pending_ref"] = result.pending_ref
        pub.options, pub.status = options, "publishing"
        record_event(
            session,
            workspace_id=pub.workspace_id,
            project_id=pub.project_id,
            run_id=run_id,
            actor_type="platform",
            actor=pub.platform,
            type="publication.processing",
            message=f"{name} recibió «{label}» y lo está procesando. Aún no está publicado.",
            data={"publication_id": pub.id},
        )
        return
    options.pop("pending_ref", None)
    if result.pending_ref:
        options["platform_ref"] = result.pending_ref
    pub.platform_post_id = result.post_id or pub.platform_post_id
    pub.url = result.url or pub.url
    pub.error, pub.progress = "", None
    pub.confirmation = "platform"
    options["platform_note"] = result.note
    if result.visibility:
        options["platform_visibility"] = result.visibility
    if result.state == "scheduled":
        options["schedule_mode"] = "platform"
        pub.options, pub.status = options, "scheduled"
        remember_published(session, pub)
        record_event(
            session,
            workspace_id=pub.workspace_id,
            project_id=pub.project_id,
            run_id=run_id,
            actor_type="platform",
            actor=pub.platform,
            type="publication.scheduled_on_platform",
            message=f"{name} confirmó la subida de «{label}» y lo publicará en la fecha programada.",
            data={"publication_id": pub.id, "post_id": pub.platform_post_id},
        )
        return
    pub.options, pub.status, pub.published_at = options, "published", utcnow()
    if (
        "_published_media" not in options
    ):  # already recorded when the platform took the upload to hold it until its date
        remember_published(session, pub)
    record_event(
        session,
        workspace_id=pub.workspace_id,
        project_id=pub.project_id,
        run_id=run_id,
        actor_type="platform",
        actor=pub.platform,
        type="publication.published",
        message=f"{name} confirmó la publicación de «{label}»."
        + (f" {result.note}" if result.note else ""),
        data={
            "publication_id": pub.id,
            "post_id": pub.platform_post_id,
            "url": pub.url,
        },
    )
    notify(
        session,
        workspace_id=pub.workspace_id,
        type="done",
        title=f"Publicado en {name}",
        body=label,
        link=f"/proyectos/{pub.project_id}?tab=publicar",
    )


def run_publish(
    ctx: RunContext, publication_id: str, transport: Any = None
) -> dict[str, Any]:
    """Upload through the official API. Runs in a worker; reports only what the platform confirms."""
    with session_scope() as session:
        pub = session.get(Publication, publication_id)
        if pub is None:
            return {"status": "missing"}
        provider = PLATFORMS[pub.platform]["provider"]
        module = registry.CONNECTORS[provider]
        if pub.approved_at is None:
            _fail(session, pub, "La publicación no está aprobada.", run_id=ctx.run_id)
            return {"status": "error"}
        changed = changed_since_approval(session, pub)
        if changed:
            # Last line of defence: nothing a person did not see leaves the workspace.
            pub.approved_at, pub.approved_by = None, None
            _fail(
                session,
                pub,
                f"Cambió {' y '.join(changed)} después de aprobarla. Revísala y apruébala de nuevo.",
                run_id=ctx.run_id,
            )
            return {"status": "error"}
        validate(session, pub)
        failing = [c for c in pub.validation if c["status"] == "fail"]
        if failing:
            _fail(
                session,
                pub,
                "No cumple los requisitos: "
                + "; ".join(f"{c['label']}: {c['detail']}" for c in failing),
                run_id=ctx.run_id,
            )
            return {"status": "error"}
        try:
            _, bundle = registry.get_bundle(
                session, pub.workspace_id, provider, transport
            )
        except ConnectorError as exc:
            _fail(
                session,
                pub,
                exc.user_message + " " + registry.MANUAL_ALTERNATIVE,
                run_id=ctx.run_id,
            )
            return {"status": "error"}
        _, asset = _render_media(session, pub)
        if asset is None:
            _fail(
                session,
                pub,
                "No hay vídeo exportado para esta publicación.",
                run_id=ctx.run_id,
            )
            return {"status": "error"}
        design = session.get(Design, pub.design_id) if pub.design_id else None
        cover = (
            session.get(Asset, design.asset_id) if design and design.asset_id else None
        )
        options = dict(pub.options or {})
        options["duration_s"] = ((asset.meta or {}).get("media") or {}).get("duration")
        options["is_short"] = pub.platform == "youtube_shorts"
        base = get_settings().app_base_url.rstrip("/")
        public = instagram.base_url_is_public()
        description = pub.description
        request = PublishRequest(
            video_path=storage.absolute_path(asset),
            title=pub.title,
            description=description,
            tags=list(pub.tags or []),
            options=options,
            cover_path=storage.absolute_path(cover) if cover else None,
            publish_at=pub.scheduled_at
            if options.get("schedule_mode") == "platform"
            and pub.scheduled_at
            and pub.scheduled_at > utcnow()
            else None,
            public_video_url=base + storage.signed_path(asset.id, ttl=3600)
            if public
            else "",
            public_cover_url=base + storage.signed_path(cover.id, ttl=3600)
            if public and cover
            else "",
        )
        pub.status, pub.error, pub.progress = "publishing", "", 0.0
        workspace_id = pub.workspace_id

    def on_progress(fraction: float, label: str) -> None:
        ctx.progress(fraction, label)

    lock = registry.provider_lock(workspace_id, provider)
    with lock:
        try:
            result = (
                module.publish(bundle, request, on_progress, transport)
                if transport is not None
                else module.publish(bundle, request, on_progress)
            )
        except ConnectorError as exc:
            with session_scope() as session:
                pub = session.get(Publication, publication_id)
                if pub is not None:
                    if exc.code == "auth_expired":
                        registry.mark_expired(
                            session, workspace_id, provider, exc.user_message
                        )
                    hint = ""
                    if exc.retry_after:
                        hint = (
                            f" Espera al menos {exc.retry_after} s antes de reintentar."
                        )
                    elif exc.retryable:
                        hint = " Puedes reintentarlo."
                    _fail(
                        session,
                        pub,
                        exc.user_message + hint,
                        run_id=ctx.run_id,
                        retryable=exc.retryable,
                    )
            return {"status": "error", "error": exc.user_message}
    with session_scope() as session:
        pub = session.get(Publication, publication_id)
        if pub is not None:
            _apply_result(session, pub, result, ctx.run_id)
    return {"status": result.state, "post_id": result.post_id}


def check_pending(publication_id: str, transport: Any = None) -> None:
    """Ask the platform about a post it was still processing, or a native schedule that is due."""
    with session_scope() as session:
        pub = session.get(Publication, publication_id)
        if pub is None:
            return
        provider = PLATFORMS[pub.platform]["provider"]
        module = registry.CONNECTORS[provider]
        options = dict(pub.options or {})
        ref = (
            options.get("pending_ref")
            if pub.status == "publishing"
            else pub.platform_post_id
        )
        if not ref:
            return
        try:
            _, bundle = registry.get_bundle(
                session, pub.workspace_id, provider, transport
            )
            result = (
                module.check_status(bundle, ref, transport)
                if transport is not None
                else module.check_status(bundle, ref)
            )
        except ConnectorError as exc:
            if pub.status == "publishing":
                _fail(
                    session, pub, exc.user_message, run_id=None, retryable=exc.retryable
                )
            else:
                options["platform_note"] = (
                    f"No se pudo comprobar el estado en la plataforma: {exc.user_message}"
                )
                pub.options = options
            return
        if pub.status == "scheduled" and result.state == "scheduled":
            return
        if pub.status == "publishing" and result.state == "processing":
            checks = int(options.get("pending_checks", 0)) + 1
            options["pending_checks"] = checks
            pub.options = options
            if checks > 30:
                _fail(
                    session,
                    pub,
                    "La plataforma no terminó de procesar el vídeo. Comprueba la cuenta antes de reintentar.",
                    run_id=None,
                )
            return
        _apply_result(session, pub, result, None)


# --- Comments and metrics sync ----------------------------------------------


def readable_comment_posts(
    session: Session, workspace_id: str, project_id: str
) -> tuple[int, int]:
    """How many platform-confirmed posts a project has, and how many can be read for comments right now."""
    posts = list(
        session.scalars(
            select(Publication).where(
                Publication.project_id == project_id,
                Publication.status == "published",
                Publication.platform_post_id != "",
            )
        )
    )
    if not posts:
        return 0, 0
    statuses = registry.all_statuses(session, workspace_id)
    readable = 0
    for post in posts:
        status = statuses[PLATFORMS[post.platform]["provider"]]
        if status["state"] == "connected" and status["capabilities"]["comments"]:
            readable += 1
    return len(posts), readable


def sync_comments(
    ctx: RunContext, project_id: str, transport: Any = None
) -> dict[str, Any]:
    """Pull comments for platform-confirmed posts of a project through connected accounts."""
    summary: dict[str, Any] = {"new": 0, "posts": 0, "skipped": [], "errors": []}
    with session_scope() as session:
        pubs = list(
            session.scalars(
                select(Publication).where(
                    Publication.project_id == project_id,
                    Publication.status == "published",
                    Publication.platform_post_id != "",
                )
            )
        )
        targets = [(p.id, p.platform, p.platform_post_id, p.workspace_id) for p in pubs]
    for index, (pub_id, platform, post_id, workspace_id) in enumerate(targets):
        provider = PLATFORMS[platform]["provider"]
        module = registry.CONNECTORS[provider]
        name = PLATFORMS[platform]["name"]
        ctx.progress(index / max(1, len(targets)), f"Leyendo comentarios de {name}")
        if not module.CAPABILITIES.comments:
            summary["skipped"].append(module.CAPABILITIES.comments_note)
            continue
        with session_scope() as session:
            try:
                _, bundle = registry.get_bundle(
                    session, workspace_id, provider, transport
                )
                items = (
                    module.fetch_comments(bundle, post_id, transport)
                    if transport is not None
                    else module.fetch_comments(bundle, post_id)
                )
            except ConnectorError as exc:
                summary["errors"].append(f"{name}: {exc.user_message}")
                continue
            known = set(
                session.scalars(
                    select(Comment.external_id).where(Comment.publication_id == pub_id)
                )
            )
            for item in items:
                if not item.get("text") or item["external_id"] in known:
                    continue
                session.add(
                    Comment(
                        workspace_id=workspace_id,
                        project_id=project_id,
                        publication_id=pub_id,
                        platform=platform,
                        source="platform",
                        external_id=item["external_id"],
                        author=item.get("author", ""),
                        text=item["text"][:5000],
                        posted_at=_parse_datetime(item.get("posted_at")),
                    )
                )
                summary["new"] += 1
            summary["posts"] += 1
    summary["skipped"] = sorted(set(summary["skipped"]))
    return summary


def _parse_datetime(value: Any) -> Any:
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00").replace("+0000", "+00:00")
        )
    except ValueError:
        return None


def sync_metrics(
    ctx: RunContext, project_id: str | None, workspace_id: str, transport: Any = None
) -> dict[str, Any]:
    summary: dict[str, Any] = {"updated": 0, "errors": [], "notes": []}
    with session_scope() as session:
        query = select(Publication).where(
            Publication.workspace_id == workspace_id,
            Publication.status == "published",
            Publication.platform_post_id != "",
        )
        if project_id:
            query = query.where(Publication.project_id == project_id)
        targets = [
            (p.id, p.platform, p.platform_post_id) for p in session.scalars(query)
        ]
    for index, (pub_id, platform, post_id) in enumerate(targets):
        provider = PLATFORMS[platform]["provider"]
        module = registry.CONNECTORS[provider]
        name = PLATFORMS[platform]["name"]
        ctx.progress(index / max(1, len(targets)), f"Leyendo métricas de {name}")
        with session_scope() as session:
            try:
                _, bundle = registry.get_bundle(
                    session, workspace_id, provider, transport
                )
                data = (
                    module.fetch_metrics(bundle, post_id, transport)
                    if transport is not None
                    else module.fetch_metrics(bundle, post_id)
                )
            except ConnectorError as exc:
                summary["errors"].append(f"{name}: {exc.user_message}")
                continue
            values = analytics.clean_values(data.get("values", {}))
            if values:
                session.add(
                    Metric(
                        workspace_id=workspace_id,
                        publication_id=pub_id,
                        source="platform",
                        values=values,
                        created_by=provider,
                    )
                )
                summary["updated"] += 1
            summary["notes"] += data.get("notes", [])
    summary["notes"] = sorted(set(summary["notes"]))
    return summary
