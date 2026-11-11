"""CrewAI Flow that carries one pipeline step from claim to handoff.

    claim ─▶ route ─▶ <agent's crew> ─▶ gate ─▶ request_approval   (a person decides)
                  └▶ preflight_review ─┘     ├▶ hand_off           (next agent starts)
                                             └▶ report_blocked     (pre-flight found problems)

The flow is the unit of orchestration; the project-level sequencing lives in
``engine.advance``, which starts a new flow run whenever a step's turn comes.
State that must survive restarts is kept in the app database, not in the flow.
"""

from __future__ import annotations

from crewai.flow.flow import Flow, listen, or_, router, start
from pydantic import BaseModel

from creator_office.agents import context
from creator_office.agents.crew import run_agent
from creator_office.agents.steps import SPECS
from creator_office.database import session_scope, utcnow
from creator_office.events import notify
from creator_office.models import Project, Run, Step
from creator_office.pipeline import engine, preflight
from creator_office.pipeline.catalog import AGENTS, STEP_BY_KIND
from creator_office.text import plural


class StepState(BaseModel):
    run_id: str = ""
    route: str = "stop"
    requires_approval: bool = True
    summary: str = ""
    outcome: str = ""


class StepFlow(Flow[StepState]):
    """One step of a project: an agent works, then a person or the next agent takes over."""

    def _ctx(self) -> context.RunContext:
        ctx = context.get(self.state.run_id)
        if ctx is None:
            raise RuntimeError("Run context is gone")
        return ctx

    @start()
    def claim(self) -> None:
        ctx = self._ctx()
        with session_scope() as session:
            step = session.get(Step, ctx.step_id)
            project = session.get(Project, ctx.project_id)
            if (
                step is None
                or project is None
                or step.status != "queued"
                or project.state != "active"
            ):
                self.state.route = "stop"
                return
            sdef = STEP_BY_KIND[step.kind]
            blocked = engine.blocked_for(session, project, sdef)
            if blocked is not None and sdef.trigger != "data":
                step.status, step.blocked_code = "blocked", blocked.code
                step.blocked_reason = f"{blocked.reason} {blocked.action}".strip()
                self.state.route = "stop"
                return
            if blocked is not None:
                step.status = "pending"
                self.state.route = "stop"
                return
            step.status, step.started_at, step.finished_at = "working", utcnow(), None
            step.attempts += 1
            step.error, step.stale_reason = "", ""
            self.state.route = sdef.agent
            self.state.requires_approval = sdef.requires_approval
            name = engine.agent_name(sdef.agent)
            started = (
                f"{name} empieza «{sdef.title}»."
                if sdef.agent in AGENTS
                else f"Empieza la revisión previa de «{project.title}»."
            )
            engine.log(
                session,
                project,
                "step.started",
                started,
                actor_type="agent" if sdef.agent in AGENTS else "orchestrator",
                actor=sdef.agent,
                step=step,
                data={"agent": sdef.agent, "kind": step.kind, "attempt": step.attempts},
            )

    @router(claim)
    def route(self) -> str:
        return "to_" + self.state.route

    def _agent_work(self) -> None:
        ctx = self._ctx()
        spec = SPECS[ctx.kind]
        output, engine_info, usage = run_agent(ctx, spec)
        with session_scope() as session:
            step = session.get(Step, ctx.step_id)
            project = session.get(Project, ctx.project_id)
            run = session.get(Run, ctx.run_id)
            applied = spec.apply(session, ctx, output, engine_info, project, step)
            step.summary = applied.summary
            step.output = {**applied.output, "engine": engine_info.public()}
            if run is not None:
                run.usage, run.model, run.provider = (
                    usage,
                    engine_info.model,
                    engine_info.provider,
                )
                run.result = {"summary": applied.summary}
        self.state.summary = applied.summary

    @listen("to_scout")
    def scout(self) -> None:
        self._agent_work()

    @listen("to_writer")
    def writer(self) -> None:
        self._agent_work()

    @listen("to_clipper")
    def clipper(self) -> None:
        self._agent_work()

    @listen("to_editor")
    def editor(self) -> None:
        self._agent_work()

    @listen("to_designer")
    def designer(self) -> None:
        self._agent_work()

    @listen("to_publisher")
    def publisher(self) -> None:
        self._agent_work()

    @listen("to_community")
    def community(self) -> None:
        self._agent_work()

    @listen("to_analyst")
    def analyst(self) -> None:
        self._agent_work()

    @listen("to_orchestrator")
    def preflight_review(self) -> None:
        ctx = self._ctx()
        with session_scope() as session:
            step = session.get(Step, ctx.step_id)
            project = session.get(Project, ctx.project_id)
            report = preflight.review(session, project)
            previous = (step.output or {}).get("checks")
            report["changed"] = previous != report["checks"]
            step.output = report
            counts = report["counts"]
            if report["status"] == "fail":
                step.summary = f"{plural(counts['fail'], 'problema')} por resolver y {plural(counts['warn'], 'aviso')}"
                self.state.outcome = "blocked"
            else:
                step.summary = plural(
                    counts["pass"], "comprobación correcta", "comprobaciones correctas"
                ) + (f" y {plural(counts['warn'], 'aviso')}" if counts["warn"] else "")
            self.state.summary = step.summary

    @listen("to_stop")
    def stop(self) -> str:
        self.state.outcome = "stopped"
        return "stopped"

    @router(
        or_(
            scout,
            writer,
            clipper,
            editor,
            designer,
            publisher,
            community,
            analyst,
            preflight_review,
        )
    )
    def gate(self) -> str:
        if self.state.outcome == "blocked":
            return "blocked"
        return "needs_approval" if self.state.requires_approval else "auto_complete"

    @listen("needs_approval")
    def request_approval(self) -> str:
        ctx = self._ctx()
        with session_scope() as session:
            step = session.get(Step, ctx.step_id)
            project = session.get(Project, ctx.project_id)
            if step is None or project is None or step.status != "working":
                self.state.outcome = "stopped"
                return "stopped"
            sdef = STEP_BY_KIND[step.kind]
            name = engine.agent_name(sdef.agent)
            step.status, step.finished_at = "awaiting_approval", utcnow()
            engine.log(
                session,
                project,
                "step.awaiting_approval",
                f"{name} terminó «{sdef.title}» y espera tu aprobación: {step.summary}.",
                actor_type="agent",
                actor=sdef.agent,
                step=step,
                data={"agent": sdef.agent, "kind": step.kind},
            )
            notify(
                session,
                workspace_id=project.workspace_id,
                type="approval",
                title=f"{name} espera tu aprobación",
                body=f"{project.title}: {step.summary}",
                link=engine.step_link(project, step),
            )
        self.state.outcome = "awaiting_approval"
        return "awaiting_approval"

    @listen("auto_complete")
    def hand_off(self) -> str:
        ctx = self._ctx()
        with session_scope() as session:
            step = session.get(Step, ctx.step_id)
            project = session.get(Project, ctx.project_id)
            if step is None or project is None or step.status != "working":
                self.state.outcome = "stopped"
                return "stopped"
            sdef = STEP_BY_KIND[step.kind]
            name = engine.agent_name(sdef.agent)
            step.status, step.finished_at = "completed", utcnow()
            done = (
                f"{name} completó «{sdef.title}»: {step.summary}."
                if sdef.agent in AGENTS
                else f"Revisión previa superada: {step.summary}."
            )
            engine.log(
                session,
                project,
                "step.completed",
                done,
                actor_type="agent" if sdef.agent in AGENTS else "orchestrator",
                actor=sdef.agent,
                step=step,
                data={"agent": sdef.agent, "kind": step.kind},
            )
            if step.kind == "analysis":
                notify(
                    session,
                    workspace_id=project.workspace_id,
                    type="done",
                    title="Analyst tiene resultados nuevos",
                    body=f"{project.title}: {step.summary}",
                    link="/analitica",
                )
            engine.after_completion(session, project, step)
        self.state.outcome = "completed"
        return "completed"

    @listen("blocked")
    def report_blocked(self) -> str:
        ctx = self._ctx()
        with session_scope() as session:
            step = session.get(Step, ctx.step_id)
            project = session.get(Project, ctx.project_id)
            if step is None or project is None or step.status != "working":
                return "stopped"
            report = step.output or {}
            failing = [c for c in report.get("checks", []) if c["status"] == "fail"]
            step.status, step.blocked_code, step.finished_at = (
                "blocked",
                "preflight_failed",
                utcnow(),
            )
            step.blocked_reason = "La revisión previa encontró problemas: " + "; ".join(
                f"{c['label']}: {c['detail']}" for c in failing[:3]
            )
            if report.get("changed", True):
                engine.log(
                    session,
                    project,
                    "preflight.failed",
                    f"La revisión previa de «{project.title}» encontró {plural(len(failing), 'problema')} que hay que resolver antes de publicar.",
                    step=step,
                    data={
                        "kind": "preflight",
                        "failing": [c["label"] for c in failing],
                    },
                )
                notify(
                    session,
                    workspace_id=project.workspace_id,
                    type="error",
                    title="La revisión previa encontró problemas",
                    body=f"{project.title}: {step.summary}",
                    link=engine.step_link(project, step),
                )
        self.state.outcome = "blocked"
        return "blocked"
