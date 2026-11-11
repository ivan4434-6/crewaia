"""Model → JSON for the UI. Nothing here invents values: absent data stays absent."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from creator_office.agents.llm import describe_engine
from creator_office.connectors import platform_specs as specs
from creator_office.connectors import registry
from creator_office.media import ffmpeg, thumbnails, transcription
from creator_office.models import (
    Asset,
    Clip,
    Comment,
    Design,
    Notification,
    Project,
    Publication,
    Render,
    Run,
    Step,
    User,
)
from creator_office.pipeline.catalog import (
    AGENT_ORDER,
    AGENTS,
    FORMATS,
    LANGUAGES,
    PLATFORMS,
    STEP_BY_KIND,
    STEPS,
    TEMPLATES,
)
from creator_office.services import analytics, documents, publishing
from creator_office.services import community as community_service

STEP_STATUS_LABELS = {
    "pending": "En espera",
    "queued": "En cola",
    "working": "Trabajando",
    "awaiting_approval": "Esperando aprobación",
    "completed": "Completado",
    "blocked": "Bloqueado",
    "error": "Error",
    "paused": "En pausa",
    "cancelled": "Cancelado",
    "skipped": "Omitido",
}


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def file_url(asset_id: str | None) -> str | None:
    return f"/api/files/{asset_id}" if asset_id else None


def catalog() -> dict[str, Any]:
    """Static reference data the UI needs to label things."""
    return {
        "agents": [
            {
                "key": a.key,
                "name": a.name,
                "specialty": a.specialty,
                "description": a.description,
                "color": a.color,
            }
            for a in AGENTS.values()
        ],
        "steps": [
            {
                "kind": s.kind,
                "agent": s.agent,
                "title": s.title,
                "produces": s.produces,
                "depends_on": list(s.depends_on),
                "requires_approval": s.requires_approval,
                "trigger": s.trigger,
                "waiting_hint": s.waiting_hint,
            }
            for s in STEPS
        ],
        "step_status_labels": STEP_STATUS_LABELS,
        "platforms": [
            {
                "key": key,
                "name": value["name"],
                "provider": value["provider"],
                "format": value["format"],
                "title_max": specs.SPECS[key].title_max,
                "description_max": specs.SPECS[key].description_max,
                "hashtags_max": specs.SPECS[key].hashtags_max,
                "tags_total_max": specs.SPECS[key].tags_total_max,
                "duration_max_s": specs.SPECS[key].duration_max_s,
                "duration_note": specs.SPECS[key].duration_note,
                "cover": specs.SPECS[key].cover,
                "docs_url": specs.SPECS[key].docs_url,
            }
            for key, value in PLATFORMS.items()
        ],
        "formats": {key: {"width": w, "height": h} for key, (w, h) in FORMATS.items()},
        "templates": [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "needs_video": t.needs_video,
                "platforms": list(t.platforms),
                "goal": t.goal,
                "options": t.options,
                "structure": list(t.structure),
            }
            for t in TEMPLATES
        ],
        "languages": LANGUAGES,
        "fonts": list(thumbnails.FONT_FILES),
        "layouts": list(thumbnails.LAYOUTS),
        "metric_fields": analytics.METRIC_FIELDS,
        "comment_categories": community_service.CATEGORIES,
        "publication_status_labels": publishing.STATUS_LABELS,
    }


def capabilities(db: Session, workspace_id: str) -> dict[str, Any]:
    """What this server can really do right now."""
    engine = describe_engine(db, workspace_id)
    ffmpeg_path = ffmpeg.locate()
    return {
        "engine": engine.public(),
        "ffmpeg": {
            "available": ffmpeg_path is not None,
            "detail": "FFmpeg disponible: análisis y exportación de vídeo activos."
            if ffmpeg_path
            else "FFmpeg no está instalado en el servidor: no se puede analizar ni exportar vídeo aquí. "
            "Puedes subir piezas ya editadas.",
        },
        "transcription": transcription.status(),
    }


def user(row: User) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "email": row.email}


# --- Assets and media --------------------------------------------------------


def asset(row: Asset | None) -> dict[str, Any] | None:
    if row is None:
        return None
    meta = row.meta or {}
    transcript = meta.get("transcript") or {}
    return {
        "id": row.id,
        "kind": row.kind,
        "filename": row.filename,
        "mime": row.mime,
        "size": row.size,
        "url": file_url(row.id),
        "poster_url": file_url(meta.get("poster_asset_id")),
        "media": meta.get("media"),
        "rights_declared": row.rights_declared,
        "rights_note": row.rights_note,
        "transcript": {
            "available": bool(transcript.get("segments")),
            "source": transcript.get("source", ""),
            "segments": len(transcript.get("segments") or []),
        },
        "created_at": iso(row.created_at),
    }


def clip(row: Clip) -> dict[str, Any]:
    return {
        "id": row.id,
        "start": row.start,
        "end": row.end,
        "duration": round(row.end - row.start, 2),
        "title": row.title,
        "reason": row.reason,
        "excerpt": row.excerpt,
        "signals": row.signals or {},
        "score": row.score,
        "status": row.status,
        "origin": row.origin,
        "position": row.position,
    }


def render(db: Session, row: Render) -> dict[str, Any]:
    export = db.get(Asset, row.asset_id) if row.asset_id else None
    meta = (export.meta or {}) if export else {}
    return {
        "id": row.id,
        "clip_id": row.clip_id,
        "format": row.format,
        "settings": row.settings or {},
        "status": row.status,
        "progress": row.progress,
        "checks": row.checks or [],
        "error": row.error,
        "origin": row.origin,
        "url": file_url(row.asset_id),
        "poster_url": file_url(meta.get("poster_asset_id")),
        "media": meta.get("media"),
        "size": export.size if export else None,
        "filename": export.filename if export else None,
        "updated_at": iso(row.updated_at),
    }


def design(row: Design) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "spec": row.spec or {},
        "status": row.status,
        "checks": row.checks or [],
        "version": row.version,
        "approved": row.approved,
        "error": row.error,
        "url": file_url(row.asset_id),
        "size": {
            "width": specs.DESIGN_SIZES[row.kind][0],
            "height": specs.DESIGN_SIZES[row.kind][1],
        },
        "updated_at": iso(row.updated_at),
    }


# --- Steps and runs ----------------------------------------------------------


def run(row: Run) -> dict[str, Any]:
    seconds = None
    if row.started_at and row.finished_at:
        seconds = round((row.finished_at - row.started_at).total_seconds(), 1)
    payload = row.payload or {}
    return {
        "id": row.id,
        "project_id": row.project_id,
        "step_id": row.step_id,
        "agent": row.agent,
        "kind": row.kind,
        "title": row.title,
        "engine": row.engine,
        "provider": row.provider,
        "model": row.model,
        "status": row.status,
        "progress": row.progress,
        "progress_label": row.progress_label,
        "usage": row.usage or {},
        "error": row.error,
        "created_at": iso(row.created_at),
        "started_at": iso(row.started_at),
        "finished_at": iso(row.finished_at),
        "seconds": seconds,
        # What a job without a step works on (an export, a design or a publication).
        "target_id": next(
            (
                payload[key]
                for key in ("render_id", "design_id", "publication_id")
                if payload.get(key)
            ),
            None,
        ),
    }


def step(row: Step, active_run: Run | None = None) -> dict[str, Any]:
    sdef = STEP_BY_KIND[row.kind]
    return {
        "id": row.id,
        "kind": row.kind,
        "agent": row.agent,
        "title": sdef.title,
        "produces": sdef.produces,
        "depends_on": list(sdef.depends_on),
        "requires_approval": sdef.requires_approval,
        "trigger": sdef.trigger,
        "waiting_hint": sdef.waiting_hint,
        "status": row.status,
        "status_label": STEP_STATUS_LABELS.get(row.status, row.status),
        "instructions": row.instructions,
        "summary": row.summary,
        "output": row.output or {},
        "blocked_code": row.blocked_code,
        "blocked_reason": row.blocked_reason,
        "error": row.error,
        "stale_reason": row.stale_reason,
        "feedback": row.feedback,
        "attempts": row.attempts,
        "started_at": iso(row.started_at),
        "finished_at": iso(row.finished_at),
        "decided_at": iso(row.decided_at),
        "run": run(active_run) if active_run else None,
    }


def _active_runs(
    db: Session, workspace_id: str, project_id: str | None = None
) -> list[Run]:
    query = select(Run).where(
        Run.workspace_id == workspace_id, Run.status.in_(("queued", "working"))
    )
    if project_id:
        query = query.where(Run.project_id == project_id)
    return list(db.scalars(query.order_by(Run.created_at)))


# --- Projects ----------------------------------------------------------------


def project_brief(row: Project) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "topic": row.topic,
        "audience": row.audience,
        "goal": row.goal,
        "language": row.language,
        "tone": row.tone,
        "platforms": list(row.platforms or []),
        "deadline": iso(row.deadline),
        "references": list(row.references or []),
        "template": row.template,
        "options": row.options or {},
        "state": row.state,
        "source_asset_id": row.source_asset_id,
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }


def project_summary(row: Project, steps: list[Step]) -> dict[str, Any]:
    ordered = sorted(steps, key=lambda s: s.position)
    core = [s for s in ordered if STEP_BY_KIND[s.kind].trigger == "auto"]
    done = sum(1 for s in core if s.status in ("completed", "skipped"))
    attention = [
        s for s in ordered if s.status in ("awaiting_approval", "blocked", "error")
    ]
    return {
        **project_brief(row),
        # Community and Analyst wake up with data and may run many times: they are follow-up, not production.
        "steps": [
            {
                "kind": s.kind,
                "agent": s.agent,
                "status": s.status,
                "title": STEP_BY_KIND[s.kind].title,
                "followup": STEP_BY_KIND[s.kind].trigger != "auto",
            }
            for s in ordered
        ],
        "progress": {"done": done, "total": len(core)},
        "attention": [
            {
                "kind": s.kind,
                "status": s.status,
                "title": STEP_BY_KIND[s.kind].title,
                "agent": s.agent,
                "reason": s.blocked_reason or s.error or s.summary,
            }
            for s in attention
        ],
    }


def project_list(db: Session, workspace_id: str) -> list[dict[str, Any]]:
    projects = list(
        db.scalars(
            select(Project)
            .where(Project.workspace_id == workspace_id)
            .order_by(Project.created_at.desc())
        )
    )
    steps_by_project: dict[str, list[Step]] = {}
    for row in db.scalars(select(Step).where(Step.workspace_id == workspace_id)):
        steps_by_project.setdefault(row.project_id, []).append(row)
    return [project_summary(p, steps_by_project.get(p.id, [])) for p in projects]


def document(db: Session, project_id: str, kind: str) -> dict[str, Any] | None:
    doc, version = documents.current(db, project_id, kind)
    if doc is None or version is None:
        return None
    return {
        "kind": kind,
        "title": doc.title,
        "version": documents.version_to_dict(
            version, current_id=doc.current_version_id
        ),
        "versions": len(documents.versions(db, doc)),
    }


def publication(
    db: Session, row: Publication, statuses: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    provider = PLATFORMS[row.platform]["provider"]
    status = (statuses or {}).get(provider) or registry.status(
        db, row.workspace_id, provider
    )
    piece = db.get(Render, row.render_id) if row.render_id else None
    clip_row = db.get(Clip, piece.clip_id) if piece else None
    cover = db.get(Design, row.design_id) if row.design_id else None
    export = db.get(Asset, piece.asset_id) if piece and piece.asset_id else None
    options = dict(row.options or {})
    sent = options.get("_published_media") if publishing.is_sent(row) else None
    return {
        "id": row.id,
        "project_id": row.project_id,
        "platform": row.platform,
        "platform_name": PLATFORMS[row.platform]["name"],
        "provider": provider,
        "title": row.title,
        "description": row.description,
        "tags": list(row.tags or []),
        "options": {k: v for k, v in options.items() if not k.startswith("_")},
        "scheduled_at": iso(row.scheduled_at),
        "schedule_mode": options.get("schedule_mode", ""),
        "status": row.status,
        "status_label": publishing.STATUS_LABELS.get(row.status, row.status),
        "approved": row.approved_at is not None,
        "approved_at": iso(row.approved_at),
        "confirmation": row.confirmation,
        "platform_post_id": row.platform_post_id,
        "url": row.url,
        "published_at": iso(row.published_at),
        "progress": row.progress,
        "error": row.error,
        "validation": row.validation or [],
        "readiness": publishing.readiness(row, status),
        "capabilities": status["capabilities"],
        "render": {
            "id": piece.id,
            "format": piece.format,
            "status": piece.status,
            "url": file_url(piece.asset_id),
            "poster_url": file_url(
                ((export.meta or {}) if export else {}).get("poster_asset_id")
            ),
            "media": ((export.meta or {}) if export else {}).get("media"),
            "size": export.size if export else None,
        }
        if piece
        else None,
        "clip_title": clip_row.title if clip_row else "",
        "design": {"id": cover.id, "kind": cover.kind, "url": file_url(cover.asset_id)}
        if cover
        else None,
        # Once it has gone out: the facts of the file that went out, and what was replaced here since then.
        "published_media": {
            k: sent.get(k) for k in ("duration", "width", "height", "size")
        }
        if sent
        else None,
        "changed_after_publication": publishing.changed_since_publication(db, row)
        if sent
        else [],
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }


def comment(row: Comment) -> dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "publication_id": row.publication_id,
        "platform": row.platform,
        "platform_name": PLATFORMS.get(row.platform, {}).get(
            "name", row.platform or "Sin plataforma"
        ),
        "source": row.source,
        "author": row.author,
        "text": row.text,
        "posted_at": iso(row.posted_at),
        "category": row.category,
        "category_by": row.category_by,
        "category_reason": row.category_reason,
        "reply_text": row.reply_text,
        "reply_status": row.reply_status,
        "reply_by": row.reply_by,
        "reply_sent_at": iso(row.reply_sent_at),
        "can_send": row.source == "platform" and bool(row.external_id),
        "created_at": iso(row.created_at),
    }


def notification(row: Notification) -> dict[str, Any]:
    return {
        "id": row.id,
        "type": row.type,
        "title": row.title,
        "body": row.body,
        "link": row.link,
        "read": row.read_at is not None,
        "created_at": iso(row.created_at),
    }


def project_detail(db: Session, row: Project) -> dict[str, Any]:
    steps = list(
        db.scalars(
            select(Step).where(Step.project_id == row.id).order_by(Step.position)
        )
    )
    runs = _active_runs(db, row.workspace_id, row.id)
    by_step = {r.step_id: r for r in runs if r.step_id}
    source = db.get(Asset, row.source_asset_id) if row.source_asset_id else None
    clips = list(
        db.scalars(
            select(Clip)
            .where(Clip.project_id == row.id)
            .order_by(Clip.position, Clip.start)
        )
    )
    renders = list(
        db.scalars(
            select(Render)
            .where(Render.project_id == row.id)
            .order_by(Render.created_at)
        )
    )
    designs = list(
        db.scalars(
            select(Design)
            .where(Design.project_id == row.id)
            .order_by(Design.kind.desc())
        )
    )
    statuses = registry.all_statuses(db, row.workspace_id)
    pubs = list(
        db.scalars(
            select(Publication)
            .where(Publication.project_id == row.id)
            .order_by(Publication.platform, Publication.created_at)
        )
    )
    images = list(
        db.scalars(
            select(Asset)
            .where(
                Asset.project_id == row.id,
                Asset.kind == "image",
                Asset.origin == "upload",
            )
            .order_by(Asset.created_at)
        )
    )
    return {
        **project_summary(row, steps),
        "steps": [step(s, by_step.get(s.id)) for s in steps],
        "source": asset(source),
        "documents": {
            "research": document(db, row.id, "research"),
            "script": document(db, row.id, "script"),
        },
        "clips": [clip(c) for c in clips],
        "renders": [render(db, r) for r in renders],
        "designs": [design(d) for d in designs],
        "images": [asset(a) for a in images],
        "publications": [publication(db, p, statuses) for p in pubs],
        "jobs": [run(r) for r in runs if not r.step_id],
        "comment_counts": {
            "total": db.scalar(
                select(func.count(Comment.id)).where(Comment.project_id == row.id)
            )
            or 0,
            "pending_replies": db.scalar(
                select(func.count(Comment.id)).where(
                    Comment.project_id == row.id, Comment.reply_status == "suggested"
                )
            )
            or 0,
        },
    }


# --- The office --------------------------------------------------------------


def office(db: Session, workspace_id: str) -> dict[str, Any]:
    """Live state of every agent, derived only from jobs and steps that exist."""
    projects = {
        p.id: p
        for p in db.scalars(select(Project).where(Project.workspace_id == workspace_id))
    }
    steps = [
        s
        for s in db.scalars(select(Step).where(Step.workspace_id == workspace_id))
        if s.project_id in projects and projects[s.project_id].state != "cancelled"
    ]
    runs = _active_runs(db, workspace_id)
    agents: list[dict[str, Any]] = []
    for key in AGENT_ORDER:
        info = AGENTS[key]
        mine = [s for s in steps if s.agent == key]
        working = [r for r in runs if r.agent == key and r.status == "working"]
        queued = [r for r in runs if r.agent == key and r.status == "queued"]
        waiting = [s for s in mine if s.status == "awaiting_approval"]
        blocked = [s for s in mine if s.status == "blocked"]
        errors = [s for s in mine if s.status == "error"]
        paused = [
            s
            for s in mine
            if s.status == "paused" and projects[s.project_id].state == "active"
        ]
        if working:
            status, label = "working", "Trabajando"
        elif errors:
            status, label = "error", "Error"
        elif waiting:
            status, label = "awaiting_approval", "Esperando aprobación"
        elif blocked:
            status, label = "blocked", "Bloqueado"
        elif queued:
            status, label = "queued", "En cola"
        else:
            status, label = "idle", "Libre"

        def brief(s: Step) -> dict[str, Any]:
            return {
                "project_id": s.project_id,
                "project": projects[s.project_id].title,
                "kind": s.kind,
                "title": STEP_BY_KIND[s.kind].title,
                "summary": s.summary,
                "reason": s.blocked_reason or s.error,
                "since": iso(s.finished_at or s.updated_at),
            }

        last = db.scalars(
            select(Run)
            .where(
                Run.workspace_id == workspace_id,
                Run.agent == key,
                Run.status.in_(("done", "error", "cancelled")),
            )
            .order_by(Run.finished_at.desc())
            .limit(1)
        ).first()
        agents.append(
            {
                "key": key,
                "name": info.name,
                "specialty": info.specialty,
                "description": info.description,
                "color": info.color,
                "status": status,
                "status_label": label,
                "current": [
                    {
                        **run(r),
                        "project": projects[r.project_id].title
                        if r.project_id in projects
                        else "",
                    }
                    for r in working
                ],
                "queue": [
                    {
                        **run(r),
                        "project": projects[r.project_id].title
                        if r.project_id in projects
                        else "",
                    }
                    for r in queued
                ],
                "awaiting_approval": [brief(s) for s in waiting],
                "blocked": [brief(s) for s in blocked],
                "errors": [brief(s) for s in errors],
                "paused": [brief(s) for s in paused],
                "last_run": run(last) if last else None,
            }
        )
    pending_publications = (
        db.scalar(
            select(func.count(Publication.id)).where(
                Publication.workspace_id == workspace_id,
                Publication.status == "draft",
                Publication.approved_at.is_(None),
                Publication.project_id.in_(
                    select(Step.project_id).where(
                        Step.workspace_id == workspace_id,
                        Step.kind == "publish",
                        Step.status == "completed",
                    )
                ),
            )
        )
        or 0
    )
    return {
        "agents": agents,
        "projects": {
            "total": len(projects),
            "active": sum(1 for p in projects.values() if p.state == "active"),
        },
        "pending_publications": pending_publications,
        "pending_replies": db.scalar(
            select(func.count(Comment.id)).where(
                Comment.workspace_id == workspace_id,
                Comment.reply_status == "suggested",
            )
        )
        or 0,
        "unread_notifications": db.scalar(
            select(func.count(Notification.id)).where(
                Notification.workspace_id == workspace_id,
                Notification.read_at.is_(None),
            )
        )
        or 0,
    }
