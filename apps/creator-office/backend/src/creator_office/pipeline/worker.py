"""Background jobs: agent steps, exports, uploads, syncs, and the scheduler tick.

Jobs run in a small thread pool inside the server process. Each job is a row
in ``runs``; the office reads those rows to decide whether an agent is really
working, so a desk can only show "Trabajando" while a job is executing here.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import select

from creator_office.agents import context
from creator_office.agents.context import RunContext
from creator_office.agents.steps import StepFailedError
from creator_office.database import session_scope, utcnow
from creator_office.events import notify, record_event
from creator_office.media.ffmpeg import Cancelled
from creator_office.models import (
    Design,
    Project,
    Publication,
    Render,
    Run,
    Step,
    Workspace,
)
from creator_office.pipeline import engine
from creator_office.pipeline.catalog import AGENTS, PLATFORMS
from creator_office.services import production, publishing
from creator_office.settings import get_settings
from creator_office.text import shorten

logger = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_ticker: threading.Thread | None = None
_stop = threading.Event()
_lock = threading.Lock()
TICK_SECONDS = 20


# --- Handlers ----------------------------------------------------------------


def _run_step(ctx: RunContext) -> None:
    from creator_office.pipeline.flow import StepFlow

    flow = StepFlow(suppress_flow_events=True)
    flow.kickoff(inputs={"run_id": ctx.run_id})


def _agent_event(
    ctx: RunContext, type_: str, message: str, data: dict[str, Any] | None = None
) -> None:
    with session_scope() as session:
        record_event(
            session,
            workspace_id=ctx.workspace_id,
            project_id=ctx.project_id,
            run_id=ctx.run_id,
            actor_type="agent",
            actor=ctx.agent,
            type=type_,
            message=message,
            data=data,
        )


def _run_render(ctx: RunContext) -> None:
    render_id = ctx.payload["render_id"]
    label = ctx.payload.get("label", "pieza")
    _agent_event(
        ctx,
        "render.started",
        f"Editor empieza a exportar «{label}».",
        {"render_id": render_id},
    )
    result = production.render_one(ctx, render_id, label=f"Exportando «{label}»")
    if result.get("status") != "ready":
        raise StepFailedError(result.get("error", "La exportación falló."))
    ctx.progress(1.0, "Exportación terminada")
    with session_scope() as session:
        project = session.get(Project, ctx.project_id)
        if project is not None:
            engine.mark_stale(
                session,
                project,
                "edit",
                "Se volvió a exportar una pieza después de este paso.",
            )
            if production.pieces_pending(session, project) == 0:
                engine.clips_caught_up(session, project)
            engine.advance(session, project)
    _agent_event(
        ctx,
        "render.finished",
        f"Editor terminó de exportar «{label}».",
        {"render_id": render_id},
    )


def _run_design(ctx: RunContext) -> None:
    design_id = ctx.payload["design_id"]
    _agent_event(
        ctx,
        "design.started",
        "Designer empieza a recomponer el diseño.",
        {"design_id": design_id},
    )
    result = production.compose_one(design_id)
    if result.get("status") != "ready":
        raise StepFailedError(result.get("error", "No se pudo componer el diseño."))
    with session_scope() as session:
        project = session.get(Project, ctx.project_id)
        if project is not None:
            engine.mark_stale(
                session, project, "design", "Se cambió un diseño después de este paso."
            )
    _agent_event(
        ctx,
        "design.finished",
        "Designer terminó de recomponer el diseño.",
        {"design_id": design_id},
    )


def _run_publish(ctx: RunContext) -> None:
    publishing.run_publish(ctx, ctx.payload["publication_id"])


def _run_sync_comments(ctx: RunContext) -> None:
    _agent_event(
        ctx,
        "comments.sync_started",
        "Community empieza a leer comentarios de las cuentas conectadas.",
    )
    summary = publishing.sync_comments(ctx, ctx.project_id or "")
    parts = [f"{summary['new']} comentarios nuevos de {summary['posts']} publicaciones"]
    parts += summary["errors"] + summary["skipped"]
    _agent_event(
        ctx,
        "comments.sync_finished",
        "Community terminó de sincronizar: " + ". ".join(parts) + ".",
        summary,
    )
    with session_scope() as session:
        run = session.get(Run, ctx.run_id)
        if run is not None:
            run.result = summary
        project = session.get(Project, ctx.project_id)
        if project is not None and summary["new"]:
            engine.wake_data_step(session, project, "community")


def _run_sync_metrics(ctx: RunContext) -> None:
    _agent_event(
        ctx,
        "metrics.sync_started",
        "Analyst empieza a leer métricas de las cuentas conectadas.",
    )
    summary = publishing.sync_metrics(ctx, ctx.project_id, ctx.workspace_id)
    parts = (
        [f"{summary['updated']} publicaciones actualizadas"]
        + summary["errors"]
        + summary["notes"]
    )
    _agent_event(
        ctx,
        "metrics.sync_finished",
        "Analyst terminó de sincronizar: " + ". ".join(parts) + ".",
        summary,
    )
    with session_scope() as session:
        run = session.get(Run, ctx.run_id)
        if run is not None:
            run.result = summary
        if summary["updated"]:
            query = select(Project).where(
                Project.workspace_id == ctx.workspace_id, Project.state == "active"
            )
            if ctx.project_id:
                query = query.where(Project.id == ctx.project_id)
            for project in session.scalars(query):
                engine.wake_data_step(session, project, "analysis")


HANDLERS: dict[str, Callable[[RunContext], None]] = {
    "step": _run_step,
    "render": _run_render,
    "design": _run_design,
    "publish": _run_publish,
    "sync_comments": _run_sync_comments,
    "sync_metrics": _run_sync_metrics,
}


# --- Execution ---------------------------------------------------------------


def _execute(run_id: str) -> None:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None or run.status != "queued":
            return
        if run.cancel_requested:
            run.status, run.finished_at = "cancelled", utcnow()
            return
        run.status, run.started_at = "working", utcnow()
        step = session.get(Step, run.step_id) if run.step_id else None
        ctx = RunContext(
            run_id=run.id,
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            step_id=run.step_id,
            agent=run.agent,
            kind=step.kind if step is not None else run.kind,
            payload=dict(run.payload or {}),
        )
        kind = run.kind
        record_event(
            session,
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            step_id=run.step_id,
            run_id=run.id,
            actor_type="system",
            type="run.started",
            message=f"Tarea en marcha: {run.title}",
            data={"agent": run.agent, "kind": kind},
        )
    context.register(ctx)
    status, error = "done", ""
    try:
        HANDLERS[kind](ctx)
    except Cancelled:
        status = "cancelled"
    except Exception as exc:
        if ctx.cancel.is_set():
            status = "cancelled"
        else:
            status, error = "error", _user_message(exc)
            if not isinstance(exc, StepFailedError):
                logger.exception("Run %s (%s) failed", run_id, kind)
    finally:
        context.unregister(run_id)
    _finish(run_id, status, error)


def _user_message(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    if isinstance(exc, (StepFailedError, RuntimeError)):
        return text
    return f"{type(exc).__name__}: {text}"


def _finish(run_id: str, status: str, error: str) -> None:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.status, run.finished_at, run.error = status, utcnow(), error
        if status != "done":
            run.progress = None
        step = session.get(Step, run.step_id) if run.step_id else None
        project = session.get(Project, run.project_id) if run.project_id else None
        if run.kind == "step" and step is not None and project is not None:
            if status == "error":
                engine.step_failed(session, project, step, run, error)
            elif status == "cancelled" and step.status == "working":
                step.status = "paused"
        elif status == "error":
            if run.kind == "render":
                render = session.get(Render, run.payload.get("render_id", ""))
                if render is not None and render.status == "rendering":
                    render.status, render.error = "error", error
            if run.kind == "design":
                design = session.get(Design, run.payload.get("design_id", ""))
                if design is not None:
                    design.error = error
            if run.kind == "publish":
                pub = session.get(Publication, run.payload.get("publication_id", ""))
                if pub is not None and pub.status == "publishing":
                    pub.status, pub.progress = "error", None
                    pub.error = f"La subida se interrumpió por un error interno ({error}). Comprueba en la plataforma si llegó a publicarse antes de reintentar."
            name = AGENTS[run.agent].name if run.agent in AGENTS else "El sistema"
            record_event(
                session,
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                run_id=run.id,
                actor_type="agent" if run.agent in AGENTS else "system",
                actor=run.agent,
                type="run.failed",
                message=f"{name} no pudo terminar «{run.title}»: {error[:400]}",
                data={"agent": run.agent, "kind": run.kind},
            )
            notify(
                session,
                workspace_id=run.workspace_id,
                type="error",
                title=f"Error: {run.title}",
                body=error[:400],
                link=f"/proyectos/{run.project_id}" if run.project_id else "/actividad",
            )
        record_event(
            session,
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            step_id=run.step_id,
            run_id=run.id,
            actor_type="system",
            type="run.finished",
            message=f"Tarea terminada ({status}): {run.title}",
            data={"agent": run.agent, "kind": run.kind, "status": status},
        )


def submit(run_id: str) -> None:
    with _lock:
        executor = _executor
    if executor is None:
        return  # the server is not running jobs (e.g. tests driving them by hand)
    executor.submit(_safe_execute, run_id)


def _safe_execute(run_id: str) -> None:
    try:
        _execute(run_id)
    except Exception:
        logger.exception("Unhandled failure executing run %s", run_id)


def run_now(run_id: str) -> None:
    """Execute a queued run in the calling thread (used by tests)."""
    _execute(run_id)


# --- Recovery and scheduler --------------------------------------------------


def recover() -> list[str]:
    """After a restart, fail what was interrupted and return what is still queued."""
    message = (
        "El servidor se reinició mientras esta tarea se ejecutaba. Vuelve a lanzarla."
    )
    with session_scope() as session:
        for run in session.scalars(select(Run).where(Run.status == "working")):
            run.status, run.finished_at, run.error, run.progress = (
                "error",
                utcnow(),
                message,
                None,
            )
            step = session.get(Step, run.step_id) if run.step_id else None
            if step is not None and step.status == "working":
                step.status, step.error = "error", message
        for render in session.scalars(
            select(Render).where(Render.status == "rendering")
        ):
            render.status, render.error, render.progress = (
                ("ready" if render.asset_id else "error"),
                message,
                None,
            )
        for pub in session.scalars(
            select(Publication).where(Publication.status == "publishing")
        ):
            if not (pub.options or {}).get("pending_ref"):
                pub.status, pub.progress = "error", None
                pub.error = (
                    "El servidor se reinició durante la subida y no se sabe si la plataforma llegó a recibirla. "
                    "Compruébalo en la cuenta antes de reintentar."
                )
        return [
            run.id
            for run in session.scalars(
                select(Run).where(Run.status == "queued").order_by(Run.created_at)
            )
        ]


def tick() -> None:
    """Scheduler duties: due publications, posts still processing, opt-in sync."""
    now = utcnow()
    due_checks: list[str] = []
    with session_scope() as session:
        due = session.scalars(
            select(Publication).where(
                Publication.status == "scheduled", Publication.scheduled_at <= now
            )
        )
        for pub in due:
            mode = (pub.options or {}).get("schedule_mode", "reminder")
            project = session.get(Project, pub.project_id)
            name = PLATFORMS[pub.platform]["name"]
            label = shorten(pub.title or pub.description)
            if mode == "platform":
                due_checks.append(pub.id)
            elif mode == "connector":
                if (
                    pub.approved_at is None
                    or project is None
                    or project.state == "cancelled"
                ):
                    continue
                pub.status, pub.progress = "publishing", None
                engine.enqueue_job(
                    session,
                    workspace_id=pub.workspace_id,
                    project_id=pub.project_id,
                    agent="publisher",
                    kind="publish",
                    title=f"Publicar en {name}: {label}",
                    payload={"publication_id": pub.id},
                    engine="connector",
                )
                record_event(
                    session,
                    workspace_id=pub.workspace_id,
                    project_id=pub.project_id,
                    actor_type="orchestrator",
                    actor="orchestrator",
                    type="publication.due",
                    message=f"Ha llegado la hora programada de «{label}» en {name}: se envía a la cuenta conectada.",
                    data={"publication_id": pub.id},
                )
            elif not pub.reminder_sent:
                pub.reminder_sent = True
                record_event(
                    session,
                    workspace_id=pub.workspace_id,
                    project_id=pub.project_id,
                    actor_type="orchestrator",
                    actor="orchestrator",
                    type="publication.reminder",
                    message=f"Ha llegado la hora de «{label}» en {name}. La cuenta no está conectada: publícala a mano y márcala como publicada.",
                    data={"publication_id": pub.id},
                )
                notify(
                    session,
                    workspace_id=pub.workspace_id,
                    type="reminder",
                    title=f"Hora de publicar en {name}",
                    body=f"«{label}»: descarga el paquete, publícalo desde la app oficial y márcalo como publicado.",
                    link=f"/proyectos/{pub.project_id}?tab=publicar",
                )
        for pub in session.scalars(
            select(Publication).where(Publication.status == "publishing")
        ):
            if (pub.options or {}).get("pending_ref"):
                due_checks.append(pub.id)
        _automations(session, now)
    for publication_id in due_checks:
        try:
            publishing.check_pending(publication_id)
        except Exception:
            logger.exception("Could not check publication %s", publication_id)


def _automations(session: Any, now: Any) -> None:
    """Opt-in only: periodic comment sync for workspaces that enabled it."""
    for workspace in session.scalars(select(Workspace)):
        hours = int(
            ((workspace.settings or {}).get("automations") or {}).get(
                "sync_comments_hours"
            )
            or 0
        )
        if hours <= 0:
            continue
        last = (workspace.settings or {}).get("last_comment_sync")
        if last and (now.timestamp() - float(last)) < hours * 3600:
            continue
        projects = session.scalars(
            select(Project).where(
                Project.workspace_id == workspace.id, Project.state == "active"
            )
        )
        started = False
        for project in projects:
            # Only where a connected account can actually read comments, and never on top of a sync that is still going.
            if not publishing.readable_comment_posts(session, workspace.id, project.id)[
                1
            ]:
                continue
            busy = session.scalar(
                select(Run.id)
                .where(
                    Run.project_id == project.id,
                    Run.kind == "sync_comments",
                    Run.status.in_(("queued", "working")),
                )
                .limit(1)
            )
            if busy is not None:
                continue
            engine.enqueue_job(
                session,
                workspace_id=workspace.id,
                project_id=project.id,
                agent="community",
                kind="sync_comments",
                title=f"Sincronizar comentarios · {project.title}",
                payload={"automatic": True},
                engine="connector",
            )
            started = True
        workspace.settings = {
            **(workspace.settings or {}),
            "last_comment_sync": now.timestamp(),
        }
        if started:
            record_event(
                session,
                workspace_id=workspace.id,
                actor_type="orchestrator",
                actor="orchestrator",
                type="automation.sync_comments",
                message=f"Sincronización automática de comentarios (regla activada: cada {hours} h).",
            )


def _tick_loop() -> None:
    while not _stop.wait(TICK_SECONDS):
        try:
            tick()
        except Exception:
            logger.exception("Scheduler tick failed")


def start() -> None:
    global _executor, _ticker
    with _lock:
        if _executor is not None:
            return
        _stop.clear()
        _executor = ThreadPoolExecutor(
            max_workers=max(1, get_settings().worker_concurrency),
            thread_name_prefix="co-worker",
        )
    for run_id in recover():
        submit(run_id)
    _ticker = threading.Thread(target=_tick_loop, name="co-scheduler", daemon=True)
    _ticker.start()


def stop() -> None:
    global _executor
    _stop.set()
    with _lock:
        executor, _executor = _executor, None
    if executor is not None:
        with session_scope() as session:
            for run in session.scalars(
                select(Run).where(Run.status.in_(("queued", "working")))
            ):
                context.request_cancel(run.id)
        executor.shutdown(wait=False, cancel_futures=True)
