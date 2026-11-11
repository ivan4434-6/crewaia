"""The office overview, activity log, jobs, notifications, search and live stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from creator_office.agents import context
from creator_office.api import serializers
from creator_office.api.deps import ApiError, Current, Db, Editor, current_auth, owned
from creator_office.database import session_scope, utcnow
from creator_office.events import broadcaster, event_to_dict
from creator_office.models import (
    ChatMessage,
    Comment,
    Document,
    DocumentVersion,
    Event,
    Notification,
    Project,
    Publication,
    Run,
    Step,
)
from creator_office.pipeline import commands
from creator_office.pipeline.catalog import AGENT_ORDER, AGENTS, PLATFORMS
from creator_office.text import shorten

router = APIRouter(prefix="/api", tags=["office"])


@router.get("/office")
def office(auth: Current, db: Db) -> dict[str, Any]:
    recent = list(
        db.scalars(
            select(Event)
            .where(
                Event.workspace_id == auth.workspace_id,
                Event.type.not_in(("run.started", "run.finished")),
            )
            .order_by(Event.id.desc())
            .limit(40)
        )
    )
    return {
        **serializers.office(db, auth.workspace_id),
        "capabilities": serializers.capabilities(db, auth.workspace_id),
        "events": [event_to_dict(e) for e in recent],
        "projects_list": serializers.project_list(db, auth.workspace_id)[:30],
    }


@router.get("/activity")
def activity(
    auth: Current,
    db: Db,
    project_id: str = "",
    agent: str = "",
    actor_type: str = "",
    kind: str = "",
    q: str = "",
    before: int = 0,
    limit: int = 100,
    technical: bool = False,
) -> dict[str, Any]:
    """The audit log: who did what and when (people, agents, platforms and the orchestrator)."""
    query = select(Event).where(Event.workspace_id == auth.workspace_id)
    if not technical:
        query = query.where(Event.type.not_in(("run.started", "run.finished")))
    if project_id:
        query = query.where(Event.project_id == project_id)
    if agent:
        query = query.where(Event.actor == agent, Event.actor_type == "agent")
    if actor_type:
        query = query.where(Event.actor_type == actor_type)
    if kind:
        query = query.where(Event.type.like(f"{kind}%"))
    if q.strip():
        query = query.where(Event.message.ilike(f"%{q.strip()}%"))
    if before:
        query = query.where(Event.id < before)
    limit = max(1, min(limit, 200))
    rows = list(db.scalars(query.order_by(Event.id.desc()).limit(limit + 1)))
    projects = {
        p.id: p.title
        for p in db.scalars(
            select(Project).where(Project.workspace_id == auth.workspace_id)
        )
    }
    return {
        "events": [
            {**event_to_dict(e), "project": projects.get(e.project_id or "", "")}
            for e in rows[:limit]
        ],
        "next_before": rows[limit - 1].id if len(rows) > limit else None,
    }


@router.get("/runs")
def runs(
    auth: Current,
    db: Db,
    status: str = "",
    agent: str = "",
    project_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    query = select(Run).where(Run.workspace_id == auth.workspace_id)
    if status == "active":
        query = query.where(Run.status.in_(("queued", "working")))
    elif status:
        query = query.where(Run.status == status)
    if agent:
        query = query.where(Run.agent == agent)
    if project_id:
        query = query.where(Run.project_id == project_id)
    rows = list(
        db.scalars(query.order_by(Run.created_at.desc()).limit(max(1, min(limit, 300))))
    )
    projects = {
        p.id: p.title
        for p in db.scalars(
            select(Project).where(Project.workspace_id == auth.workspace_id)
        )
    }
    return {
        "runs": [
            {**serializers.run(r), "project": projects.get(r.project_id or "", "")}
            for r in rows
        ]
    }


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, auth: Editor, db: Db) -> dict[str, Any]:
    run = owned(db, auth, Run, run_id, "La tarea")
    if run.status not in ("queued", "working"):
        raise ApiError(409, "Esta tarea ya ha terminado.", "not_running")
    run.cancel_requested = True
    if run.status == "queued":
        run.status, run.finished_at = "cancelled", utcnow()
    context.request_cancel(run.id)
    step = db.get(Step, run.step_id) if run.step_id else None
    if step is not None and step.status in ("queued", "working"):
        step.status = "paused"
    if run.kind == "publish":
        pub = db.get(Publication, (run.payload or {}).get("publication_id", ""))
        if pub is not None and pub.status == "publishing" and run.status == "cancelled":
            pub.status, pub.progress = "draft", None
    auth.log(
        db,
        "run.cancelled",
        f"{auth.user.name} canceló la tarea «{run.title}».",
        project_id=run.project_id,
        data={"run_id": run.id, "agent": run.agent},
    )
    return {"run": serializers.run(run)}


@router.get("/usage")
def usage(auth: Current, db: Db) -> dict[str, Any]:
    """Measured work per agent: durations always; tokens and cost only when a provider reported them."""
    rows = list(
        db.scalars(
            select(Run).where(
                Run.workspace_id == auth.workspace_id,
                Run.status.in_(("done", "error", "cancelled")),
            )
        )
    )
    agents: dict[str, dict[str, Any]] = {
        key: {
            "agent": key,
            "name": AGENTS[key].name,
            "runs": 0,
            "errors": 0,
            "seconds": 0.0,
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "cost_known_runs": 0,
            "provider_runs": 0,
            "demo_runs": 0,
            "tool_runs": 0,
        }
        for key in AGENT_ORDER
    }
    # Work that belongs to no agent (the pre-publication review) is the orchestrator's: it counts too.
    agents["orchestrator"] = {
        **agents[AGENT_ORDER[0]],
        "agent": "orchestrator",
        "name": "Orquestador",
    }
    for run in rows:
        bucket = agents.get(run.agent or "orchestrator")
        if bucket is None:
            continue
        bucket["runs"] += 1
        bucket["errors"] += 1 if run.status == "error" else 0
        if run.started_at and run.finished_at:
            bucket["seconds"] += (run.finished_at - run.started_at).total_seconds()
        data = run.usage or {}
        if data.get("engine") == "provider":
            bucket["provider_runs"] += 1
            for key in (
                "llm_calls",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
            ):
                bucket[key] += int(data.get(key) or 0)
            if data.get("cost_usd") is not None:
                bucket["cost_usd"] += float(data["cost_usd"])
                bucket["cost_known_runs"] += 1
        elif data.get("engine") == "demo":
            bucket["demo_runs"] += 1
        else:
            bucket["tool_runs"] += 1
    for bucket in agents.values():
        bucket["seconds"] = round(bucket["seconds"], 1)
        bucket["cost_usd"] = (
            round(bucket["cost_usd"], 4) if bucket["cost_known_runs"] else None
        )
    totals_cost = [b["cost_usd"] for b in agents.values() if b["cost_usd"] is not None]
    return {
        "agents": list(agents.values()),
        "totals": {
            "runs": sum(b["runs"] for b in agents.values()),
            "seconds": round(sum(b["seconds"] for b in agents.values()), 1),
            "total_tokens": sum(b["total_tokens"] for b in agents.values()),
            "cost_usd": round(sum(totals_cost), 4) if totals_cost else None,
        },
        "engine": serializers.capabilities(db, auth.workspace_id)["engine"],
        "notes": [
            "El tiempo es el medido en cada ejecución.",
            "Los tokens son los que informa el proveedor de IA; el coste es una estimación con los precios que tú configuras en Integraciones.",
            "El motor local de demostración y las herramientas (FFmpeg, composición, validaciones) no consumen tokens.",
        ],
    }


# --- Notifications -----------------------------------------------------------


@router.get("/notifications")
def notifications(auth: Current, db: Db) -> dict[str, Any]:
    rows = list(
        db.scalars(
            select(Notification)
            .where(Notification.workspace_id == auth.workspace_id)
            .order_by(Notification.created_at.desc())
            .limit(60)
        )
    )
    unread = (
        db.scalar(
            select(func.count(Notification.id)).where(
                Notification.workspace_id == auth.workspace_id,
                Notification.read_at.is_(None),
            )
        )
        or 0
    )
    return {
        "notifications": [serializers.notification(n) for n in rows],
        "unread": unread,
    }


class ReadRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)
    all: bool = False


@router.post("/notifications/read")
def mark_read(body: ReadRequest, auth: Current, db: Db) -> dict[str, bool]:
    query = select(Notification).where(
        Notification.workspace_id == auth.workspace_id, Notification.read_at.is_(None)
    )
    if not body.all:
        if not body.ids:
            return {"ok": True}
        query = query.where(Notification.id.in_(body.ids))
    for row in db.scalars(query):
        row.read_at = utcnow()
    return {"ok": True}


# --- Search ------------------------------------------------------------------


def _snippet(text: str, needle: str, width: int = 90) -> str:
    index = text.lower().find(needle.lower())
    if index == -1:
        return text[: width * 2] + ("…" if len(text) > width * 2 else "")
    start = max(0, index - width)
    end = min(len(text), index + len(needle) + width)
    return (
        ("…" if start else "")
        + text[start:end].strip()
        + ("…" if end < len(text) else "")
    )


def _plain(value: Any) -> str:
    """The readable text of a stored document: its string values, without keys or JSON punctuation."""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return " · ".join(
            part
            for part in (
                _plain(v)
                for k, v in value.items()
                if k not in ("url", "source_urls", "platform")
            )
            if part
        )
    if isinstance(value, list):
        return " · ".join(part for part in (_plain(v) for v in value) if part)
    return ""


@router.get("/search")
def search(auth: Current, db: Db, q: str = "") -> dict[str, Any]:
    needle = q.strip()
    if len(needle) < 2:
        return {"query": needle, "results": []}
    like = f"%{needle}%"
    ws = auth.workspace_id
    projects = {
        p.id: p for p in db.scalars(select(Project).where(Project.workspace_id == ws))
    }
    results: list[dict[str, Any]] = []
    for project in projects.values():
        brief = " · ".join(
            part for part in (project.topic, project.audience, project.goal) if part
        )
        if needle.lower() in f"{project.title} {brief}".lower():
            results.append(
                {
                    "type": "project",
                    "type_label": "Proyecto",
                    "title": project.title,
                    "snippet": _snippet(brief, needle),
                    "href": f"/proyectos/{project.id}",
                }
            )
    documents = db.execute(
        select(Document, DocumentVersion)
        .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
        .where(Document.workspace_id == ws, Document.kind.in_(("research", "script")))
    ).all()
    for document, version in documents:
        text = _plain(version.content)
        if needle.lower() in text.lower() and document.project_id in projects:
            step = "research" if document.kind == "research" else "script"
            results.append(
                {
                    "type": "document",
                    "type_label": document.title,
                    "title": projects[document.project_id].title,
                    "snippet": _snippet(text, needle),
                    "href": f"/proyectos/{document.project_id}?paso={step}",
                }
            )
    for pub in db.scalars(
        select(Publication)
        .where(
            Publication.workspace_id == ws,
            or_(Publication.title.ilike(like), Publication.description.ilike(like)),
        )
        .limit(20)
    ):
        results.append(
            {
                "type": "publication",
                "type_label": f"Publicación · {PLATFORMS[pub.platform]['name']}",
                "title": shorten(pub.title or pub.description, 80),
                "snippet": _snippet(f"{pub.title} {pub.description}", needle),
                "href": f"/proyectos/{pub.project_id}?tab=publicar",
            }
        )
    for comment in db.scalars(
        select(Comment)
        .where(Comment.workspace_id == ws, Comment.text.ilike(like))
        .limit(20)
    ):
        results.append(
            {
                "type": "comment",
                "type_label": "Comentario",
                "title": comment.author or "Comentario",
                "snippet": _snippet(comment.text, needle),
                "href": f"/comunidad?proyecto={comment.project_id}",
            }
        )
    for event in db.scalars(
        select(Event)
        .where(
            Event.workspace_id == ws,
            Event.message.ilike(like),
            Event.type.not_in(("run.started", "run.finished")),
        )
        .order_by(Event.id.desc())
        .limit(10)
    ):
        results.append(
            {
                "type": "event",
                "type_label": "Actividad",
                "title": "",
                "at": serializers.iso(event.created_at),
                "snippet": _snippet(event.message, needle),
                "href": f"/actividad?q={quote(needle)}",
            }
        )
    return {"query": needle, "results": results[:60]}


# --- Order console -----------------------------------------------------------


class Order(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    project_id: str | None = None


def _chat_message(row: ChatMessage) -> dict[str, Any]:
    return {
        "id": row.id,
        "role": row.role,
        "text": row.text,
        "action": row.action or {},
        "created_at": serializers.iso(row.created_at),
    }


@router.get("/chat")
def chat_history(auth: Current, db: Db) -> dict[str, Any]:
    rows = list(
        db.scalars(
            select(ChatMessage)
            .where(ChatMessage.workspace_id == auth.workspace_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(40)
        )
    )
    return {
        "messages": [_chat_message(m) for m in reversed(rows)],
        "help": commands.HELP,
    }


@router.post("/chat")
def chat_order(body: Order, auth: Current, db: Db) -> dict[str, Any]:
    project_id = body.project_id
    if project_id:
        project = db.get(Project, project_id)
        if project is None or project.workspace_id != auth.workspace_id:
            project_id = None
    asked = ChatMessage(
        workspace_id=auth.workspace_id,
        user_id=auth.user.id,
        role="user",
        text=body.text.strip(),
        project_id=project_id,
    )
    db.add(asked)
    db.flush()
    reply = commands.execute(
        db,
        workspace_id=auth.workspace_id,
        user_id=auth.user.id,
        user_name=auth.user.name,
        can_edit=auth.can_edit,
        text=body.text,
        project_id=project_id,
    )
    answer = ChatMessage(
        workspace_id=auth.workspace_id,
        role="orchestrator",
        text=reply.text,
        project_id=project_id,
        action={"ok": reply.ok, "links": reply.links, **reply.action},
    )
    db.add(answer)
    db.flush()
    return {"messages": [_chat_message(asked), _chat_message(answer)]}


# --- Live stream -------------------------------------------------------------


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    """Server-sent events: activity, notifications and progress for the caller's workspace."""
    with (
        session_scope() as db
    ):  # short-lived: no database session stays open while streaming
        workspace_id = current_auth(request, db).workspace_id

    async def events() -> AsyncIterator[str]:
        queue, unsubscribe = broadcaster.subscribe(workspace_id)
        try:
            yield "retry: 3000\n\n"
            yield 'data: {"kind": "hello"}\n\n'
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(message, ensure_ascii=False)}\n\n"
        finally:
            unsubscribe()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
