"""The orchestrator: decides which agent works next and records every handoff.

It is internal on purpose. It has no desk in the office; what users see of it
are the handoff lines in the activity feed and the state of the project board.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from creator_office.agents import context
from creator_office.agents.steps import SPECS, Blocked
from creator_office.database import utcnow
from creator_office.events import (
    after_commit,
    notify,
    record_event,
    settle_notifications,
)
from creator_office.models import Clip, Comment, Design, Project, Render, Run, Step
from creator_office.pipeline.catalog import AGENTS, STEP_BY_KIND, STEPS, StepDef
from creator_office.services import documents


class EngineError(ValueError):
    """An action that is not possible in the current state (shown to the user)."""


ACTIVE = ("queued", "working")
DONE = ("completed", "skipped")


def agent_name(key: str) -> str:
    return AGENTS[key].name if key in AGENTS else "El orquestador"


def steps_of(session: Session, project_id: str) -> dict[str, Step]:
    return {
        s.kind: s
        for s in session.scalars(select(Step).where(Step.project_id == project_id))
    }


def create_steps(session: Session, project: Project) -> None:
    for position, sdef in enumerate(STEPS):
        session.add(
            Step(
                workspace_id=project.workspace_id,
                project_id=project.id,
                kind=sdef.kind,
                agent=sdef.agent,
                position=position,
            )
        )
    session.flush()


def log(
    session: Session,
    project: Project,
    type_: str,
    message: str,
    *,
    actor_type: str = "orchestrator",
    actor: str = "orchestrator",
    step: Step | None = None,
    run: Run | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    record_event(
        session,
        workspace_id=project.workspace_id,
        project_id=project.id,
        step_id=step.id if step else None,
        run_id=run.id if run else None,
        actor_type=actor_type,
        actor=actor,
        type=type_,
        message=message,
        data=data,
    )


# --- Jobs --------------------------------------------------------------------


def _submit(session: Session, run: Run) -> None:
    from creator_office.pipeline import worker

    run_id = run.id
    after_commit(session, lambda: worker.submit(run_id))


def enqueue_job(
    session: Session,
    *,
    workspace_id: str,
    project_id: str | None,
    agent: str,
    kind: str,
    title: str,
    payload: dict[str, Any],
    engine: str = "tool",
    user_id: str = "",
    step_id: str | None = None,
) -> Run:
    run = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        step_id=step_id,
        agent=agent,
        kind=kind,
        title=title[:240],
        engine=engine,
        payload=payload,
        created_by=user_id,
    )
    session.add(run)
    session.flush()
    _submit(session, run)
    return run


def step_link(project: Project, step: Step) -> str:
    return f"/proyectos/{project.id}?paso={step.kind}"


def _settle(session: Session, project: Project, step: Step) -> None:
    """The decision or retry this step was waiting for has happened: its notices stop being pending."""
    settle_notifications(session, project.workspace_id, step_link(project, step))


def enqueue_step(
    session: Session, project: Project, step: Step, user_id: str = ""
) -> Run:
    sdef = STEP_BY_KIND[step.kind]
    step.status, step.error, step.blocked_code, step.blocked_reason = (
        "queued",
        "",
        "",
        "",
    )
    _settle(session, project, step)
    return enqueue_job(
        session,
        workspace_id=project.workspace_id,
        project_id=project.id,
        agent=sdef.agent if sdef.agent in AGENTS else "",
        kind="step",
        title=f"{sdef.title} · {project.title}",
        payload={"step_id": step.id},
        engine="rules" if sdef.agent == "orchestrator" else "crewai",
        user_id=user_id,
        step_id=step.id,
    )


def stop_runs(session: Session, step: Step) -> None:
    for run in session.scalars(
        select(Run).where(Run.step_id == step.id, Run.status.in_(ACTIVE))
    ):
        run.cancel_requested = True
        if run.status == "queued":
            run.status, run.finished_at = "cancelled", utcnow()
        context.request_cancel(run.id)


# --- Advancing the pipeline --------------------------------------------------


def blocked_for(session: Session, project: Project, sdef: StepDef) -> Blocked | None:
    if sdef.agent == "orchestrator":
        return None
    return SPECS[sdef.kind].precheck(session, project)


def advance(
    session: Session, project: Project, *, announce_blocked: bool = True
) -> dict[str, Blocked]:
    """Start every step whose turn has come; record why the others cannot start.

    Returns the steps that have just become blocked, keyed by kind.
    """
    newly_blocked: dict[str, Blocked] = {}
    if project.state != "active":
        return newly_blocked
    steps = steps_of(session, project.id)
    for sdef in STEPS:
        step = steps[sdef.kind]
        if step.status not in ("pending", "blocked"):
            continue
        if not all(steps[dep].status in DONE for dep in sdef.depends_on):
            if step.status == "blocked":
                step.status, step.blocked_code, step.blocked_reason = "pending", "", ""
            continue
        blocked = blocked_for(session, project, sdef)
        if blocked is None:
            enqueue_step(session, project, step)
            continue
        if sdef.trigger == "data":
            continue  # nothing to do yet; not a problem to flag
        if step.status != "blocked" or step.blocked_code != blocked.code:
            step.status, step.blocked_code = "blocked", blocked.code
            step.blocked_reason = f"{blocked.reason} {blocked.action}".strip()
            newly_blocked[sdef.kind] = blocked
            if announce_blocked:
                log(
                    session,
                    project,
                    "step.blocked",
                    f"{agent_name(sdef.agent)} no puede empezar «{sdef.title}»: {blocked.reason}",
                    step=step,
                    data={
                        "agent": sdef.agent,
                        "kind": sdef.kind,
                        "code": blocked.code,
                        "action": blocked.action,
                    },
                )
    return newly_blocked


def _party(agent: str, *, start: bool = False) -> str:
    """How a handoff names its giver or taker; the orchestrator is its review, not a character."""
    if agent in AGENTS:
        return AGENTS[agent].name
    return "La revisión previa" if start else "la revisión previa"


def after_completion(session: Session, project: Project, step: Step) -> None:
    """Hand the result over to whoever depends on it."""
    sdef = STEP_BY_KIND[step.kind]
    newly_blocked = advance(session, project, announce_blocked=False)
    steps = steps_of(session, project.id)
    giver = _party(sdef.agent, start=True)
    produced = sdef.produces[:1].lower() + sdef.produces[1:]
    announced: set[str] = set()
    for other in STEPS:
        if step.kind not in other.depends_on:
            continue
        target = steps[other.kind]
        taker = _party(other.agent)
        data: dict[str, Any] = {
            "from": sdef.agent,
            "to": other.agent,
            "from_step": step.kind,
            "to_step": other.kind,
            "to_status": target.status,
        }
        if target.status in ACTIVE:
            message = f"{giver} entrega a {taker}: {produced}. Empieza «{other.title}»."
        elif target.status == "blocked":
            blocked = newly_blocked.get(other.kind)
            if blocked is None:
                continue  # it was already blocked for the same reason; nothing new to say
            announced.add(other.kind)
            data.update({"code": blocked.code, "action": blocked.action})
            message = f"{giver} entrega a {taker}, que todavía no puede empezar «{other.title}»: {blocked.reason}"
        elif target.status == "pending" and other.trigger != "data":
            waiting = [
                STEP_BY_KIND[d].title
                for d in other.depends_on
                if steps[d].status not in DONE
            ]
            if not waiting:
                continue
            message = (
                f"{giver} entrega a {taker}, que aún espera: {', '.join(waiting)}."
            )
        else:
            continue
        log(session, project, "handoff", message, step=target, data=data)
    for kind, blocked in newly_blocked.items():
        if kind in announced:
            continue
        other = STEP_BY_KIND[kind]
        log(
            session,
            project,
            "step.blocked",
            f"{agent_name(other.agent)} no puede empezar «{other.title}»: {blocked.reason}",
            step=steps[kind],
            data={
                "agent": other.agent,
                "kind": kind,
                "code": blocked.code,
                "action": blocked.action,
            },
        )


def downstream_of(kind: str) -> set[str]:
    out: set[str] = set()
    frontier = [kind]
    while frontier:
        current = frontier.pop()
        for sdef in STEPS:
            if current in sdef.depends_on and sdef.kind not in out:
                out.add(sdef.kind)
                frontier.append(sdef.kind)
    return out


def mark_stale(
    session: Session, project: Project, changed_kind: str, reason: str
) -> None:
    affected = downstream_of(changed_kind)
    for step in steps_of(session, project.id).values():
        if (
            step.kind in affected
            and step.status in ("completed", "awaiting_approval")
            and not step.stale_reason
        ):
            step.stale_reason = reason


# Hints raised by changing the clips; they stop applying once every selected clip has an up-to-date piece.
CLIP_CHANGES = (
    "Se añadió un clip después de este paso.",
    "Cambió la selección de clips después de este paso.",
    "Se cambió el corte de un clip después de este paso.",
)


def clips_caught_up(session: Session, project: Project) -> None:
    """The pieces were exported by hand after the clips changed: Editor is no longer behind them."""
    step = steps_of(session, project.id).get("edit")
    if step is not None and step.stale_reason in CLIP_CHANGES:
        step.stale_reason = ""


def wake_data_step(session: Session, project: Project, kind: str) -> None:
    """New comments or metrics arrived: let Community or Analyst look at them."""
    step = steps_of(session, project.id).get(kind)
    if (
        step is None
        or step.status in ACTIVE
        or step.status in ("awaiting_approval", "paused", "cancelled")
    ):
        return
    step.status, step.blocked_code, step.blocked_reason = "pending", "", ""
    advance(session, project)


# --- Human decisions ---------------------------------------------------------


def approve_step(
    session: Session,
    project: Project,
    step: Step,
    user_id: str,
    user_name: str,
    payload: dict[str, Any],
) -> None:
    if step.status != "awaiting_approval":
        raise EngineError("Este paso no está esperando aprobación.")
    sdef = STEP_BY_KIND[step.kind]
    detail = ""
    if step.kind == "research":
        content = documents.current_content(session, project.id, "research")
        titles = [a["title"] for a in content.get("angles", [])]
        custom = str(payload.get("custom_angle") or "").strip()
        chosen = custom or payload.get("chosen_angle")
        if not chosen or (not custom and chosen not in titles):
            raise EngineError(
                "Elige uno de los ángulos propuestos o escribe el tuyo antes de aprobar."
            )
        step.output = {
            **(step.output or {}),
            "chosen_angle": chosen,
            "angle_is_custom": bool(custom),
        }
        detail = f" Ángulo elegido: «{chosen}»."
    elif step.kind == "clips":
        selected = session.scalar(
            select(Clip.id)
            .where(Clip.project_id == project.id, Clip.status == "selected")
            .limit(1)
        )
        if selected is None:
            raise EngineError("Selecciona al menos un clip antes de aprobar.")
    elif step.kind == "edit":
        ready = session.scalar(
            select(Render.id)
            .where(Render.project_id == project.id, Render.status == "ready")
            .limit(1)
        )
        if ready is None:
            raise EngineError("No hay ninguna pieza exportada que aprobar.")
    elif step.kind == "design":
        for design in session.scalars(
            select(Design).where(
                Design.project_id == project.id, Design.status == "ready"
            )
        ):
            design.approved = True
    step.status, step.decided_by, step.decided_at, step.feedback = (
        "completed",
        user_id,
        utcnow(),
        "",
    )
    _settle(session, project, step)
    log(
        session,
        project,
        "step.approved",
        f"{user_name} aprobó «{sdef.title}» de {agent_name(sdef.agent)}.{detail}",
        actor_type="user",
        actor=user_name,
        step=step,
        data={"agent": sdef.agent, "kind": step.kind},
    )
    after_completion(session, project, step)
    if step.kind == "community":
        pending = session.scalar(
            select(Comment.id)
            .where(Comment.project_id == project.id, Comment.category == "")
            .limit(1)
        )
        if pending is not None:
            wake_data_step(session, project, "community")


def request_changes(
    session: Session,
    project: Project,
    step: Step,
    user_id: str,
    user_name: str,
    feedback: str,
) -> None:
    if step.status not in ("awaiting_approval", "completed"):
        raise EngineError(
            "Solo se pueden pedir cambios sobre un resultado ya entregado."
        )
    feedback = feedback.strip()
    if not feedback:
        raise EngineError("Explica qué cambios quieres.")
    if project.state != "active":
        raise EngineError(
            "El proyecto está en pausa o cancelado. Reanúdalo para pedir cambios."
        )
    sdef = STEP_BY_KIND[step.kind]
    blocked = blocked_for(session, project, sdef)
    if blocked is not None:
        raise EngineError(
            f"{agent_name(sdef.agent)} no puede rehacerlo ahora: {blocked.reason}"
        )
    was_completed = step.status == "completed"
    step.feedback = feedback[:2000]
    log(
        session,
        project,
        "step.changes_requested",
        f"{user_name} pidió cambios a {agent_name(sdef.agent)} en «{sdef.title}»: {feedback[:300]}",
        actor_type="user",
        actor=user_name,
        step=step,
        data={"agent": sdef.agent, "kind": step.kind},
    )
    enqueue_step(session, project, step, user_id)
    if was_completed:
        mark_stale(
            session,
            project,
            step.kind,
            f"«{sdef.title}» se está rehaciendo después de este paso.",
        )


def rerun_step(
    session: Session, project: Project, step: Step, user_id: str, user_name: str
) -> None:
    if step.status in ACTIVE:
        raise EngineError("Este paso ya está en marcha.")
    if project.state != "active":
        raise EngineError("El proyecto está en pausa o cancelado. Reanúdalo primero.")
    sdef = STEP_BY_KIND[step.kind]
    steps = steps_of(session, project.id)
    missing = [
        STEP_BY_KIND[d].title for d in sdef.depends_on if steps[d].status not in DONE
    ]
    if missing:
        raise EngineError("Antes tienen que terminar: " + ", ".join(missing) + ".")
    blocked = blocked_for(session, project, sdef)
    if blocked is not None:
        step.status, step.blocked_code, step.blocked_reason = (
            "blocked",
            blocked.code,
            f"{blocked.reason} {blocked.action}".strip(),
        )
        raise EngineError(f"{blocked.reason} {blocked.action}".strip())
    was_completed = step.status == "completed"
    verb = "reintentó" if step.status in ("error", "cancelled") else "volvió a lanzar"
    log(
        session,
        project,
        "step.rerun",
        f"{user_name} {verb} «{sdef.title}».",
        actor_type="user",
        actor=user_name,
        step=step,
        data={"agent": sdef.agent, "kind": step.kind},
    )
    enqueue_step(session, project, step, user_id)
    if was_completed:
        mark_stale(
            session,
            project,
            step.kind,
            f"«{sdef.title}» se rehízo después de este paso.",
        )


def skip_step(session: Session, project: Project, step: Step, user_name: str) -> None:
    if step.status in ("completed", "skipped"):
        raise EngineError("Este paso ya está cerrado.")
    sdef = STEP_BY_KIND[step.kind]
    stop_runs(session, step)
    step.status, step.blocked_code, step.blocked_reason, step.error = (
        "skipped",
        "",
        "",
        "",
    )
    step.decided_at = utcnow()
    _settle(session, project, step)
    log(
        session,
        project,
        "step.skipped",
        f"{user_name} omitió «{sdef.title}».",
        actor_type="user",
        actor=user_name,
        step=step,
        data={"agent": sdef.agent, "kind": step.kind},
    )
    after_completion(session, project, step)


def pause_step(session: Session, project: Project, step: Step, user_name: str) -> None:
    if step.status not in (*ACTIVE, "pending", "blocked"):
        raise EngineError("Este paso no se puede pausar en su estado actual.")
    sdef = STEP_BY_KIND[step.kind]
    was_working = step.status == "working"
    stop_runs(session, step)
    step.status = "paused"
    log(
        session,
        project,
        "step.paused",
        f"{user_name} pausó «{sdef.title}»."
        + (
            " El trabajo en curso se detiene; al reanudar, el paso empieza de nuevo."
            if was_working
            else ""
        ),
        actor_type="user",
        actor=user_name,
        step=step,
        data={"agent": sdef.agent, "kind": step.kind},
    )


def resume_step(session: Session, project: Project, step: Step, user_name: str) -> None:
    if step.status not in ("paused", "cancelled"):
        raise EngineError("Este paso no está en pausa.")
    if project.state != "active":
        raise EngineError("El proyecto está en pausa o cancelado. Reanúdalo primero.")
    sdef = STEP_BY_KIND[step.kind]
    step.status = "pending"
    log(
        session,
        project,
        "step.resumed",
        f"{user_name} reanudó «{sdef.title}».",
        actor_type="user",
        actor=user_name,
        step=step,
        data={"agent": sdef.agent, "kind": step.kind},
    )
    advance(session, project)


def pause_project(session: Session, project: Project, user_name: str) -> None:
    if project.state != "active":
        raise EngineError("El proyecto no está activo.")
    project.state = "paused"
    for step in steps_of(session, project.id).values():
        if step.status in ACTIVE:
            stop_runs(session, step)
            step.status = "paused"
    log(
        session,
        project,
        "project.paused",
        f"{user_name} pausó el proyecto «{project.title}».",
        actor_type="user",
        actor=user_name,
    )


def resume_project(session: Session, project: Project, user_name: str) -> None:
    if project.state == "active":
        raise EngineError("El proyecto ya está activo.")
    project.state = "active"
    for step in steps_of(session, project.id).values():
        if step.status in ("paused", "cancelled"):
            step.status = "pending"
    log(
        session,
        project,
        "project.resumed",
        f"{user_name} reanudó el proyecto «{project.title}».",
        actor_type="user",
        actor=user_name,
    )
    advance(session, project)


def cancel_project(session: Session, project: Project, user_name: str) -> None:
    if project.state == "cancelled":
        raise EngineError("El proyecto ya está cancelado.")
    project.state = "cancelled"
    for step in steps_of(session, project.id).values():
        if step.status in (
            *ACTIVE,
            "pending",
            "blocked",
            "paused",
            "awaiting_approval",
        ):
            stop_runs(session, step)
            step.status = "cancelled"
            _settle(session, project, step)
    log(
        session,
        project,
        "project.cancelled",
        f"{user_name} canceló el proyecto «{project.title}».",
        actor_type="user",
        actor=user_name,
    )


# --- Results coming back from the worker -------------------------------------


def step_failed(
    session: Session, project: Project, step: Step, run: Run | None, message: str
) -> None:
    sdef = STEP_BY_KIND[step.kind]
    step.status, step.error, step.finished_at = "error", message[:2000], utcnow()
    name = agent_name(sdef.agent)
    log(
        session,
        project,
        "step.failed",
        f"{name} no pudo completar «{sdef.title}»: {message[:400]}",
        actor_type="agent" if sdef.agent in AGENTS else "orchestrator",
        actor=sdef.agent,
        step=step,
        run=run,
        data={"agent": sdef.agent, "kind": step.kind},
    )
    notify(
        session,
        workspace_id=project.workspace_id,
        type="error",
        title=f"Error de {name} en «{project.title}»",
        body=message[:400],
        link=step_link(project, step),
    )
