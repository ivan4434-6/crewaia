"""Runs one agent on one step as a CrewAI crew."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from creator_office.agents import bridge, context
from creator_office.agents.context import RunContext
from creator_office.agents.llm import EngineInfo, build_llm, usage_from_metrics
from creator_office.agents.steps import Prepared, StepFailedError, StepSpec
from creator_office.agents.tools import TOOLS_BY_STEP
from creator_office.database import session_scope
from creator_office.media.ffmpeg import Cancelled
from creator_office.models import Project, Step, Workspace
from creator_office.pipeline.catalog import AGENTS


def _coerce(raw: str, spec: StepSpec) -> Any:
    """Parse the model's final answer when CrewAI could not."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise StepFailedError(
            "El agente no devolvió una respuesta con el formato esperado."
        )
    try:
        return spec.output_model.model_validate(json.loads(text[start : end + 1]))
    except (ValueError, ValidationError) as exc:
        raise StepFailedError(
            f"La respuesta del agente no tiene el formato esperado: {str(exc)[:200]}"
        ) from exc


def run_agent(
    ctx: RunContext, spec: StepSpec
) -> tuple[Any, EngineInfo, dict[str, Any]]:
    """Build the agent, task and crew for a step and run it to completion."""
    from crewai import Agent, Crew, Process, Task
    from crewai.events import crewai_event_bus

    bridge.install()
    with session_scope() as session:
        project = session.get(Project, ctx.project_id)
        step = session.get(Step, ctx.step_id)
        workspace = session.get(Workspace, ctx.workspace_id)
        if project is None or step is None or workspace is None:
            raise StepFailedError("El proyecto ya no existe.")
        prepared: Prepared = spec.prepare(session, project, step, workspace)
        llm, engine = build_llm(session, ctx.workspace_id)
    ctx.brief = prepared.brief
    ctx.payload["engine_label"] = engine.label

    info = AGENTS[ctx.agent]
    agent = Agent(
        role=info.role,
        goal=info.goal,
        backstory=info.backstory,
        llm=llm,
        tools=[tool(ctx) for tool in TOOLS_BY_STEP.get(spec.kind, [])],
        allow_delegation=False,
        verbose=False,
        max_iter=prepared.max_iter,
    )
    task = Task(
        name=context.task_name(ctx.run_id),
        description=prepared.description,
        expected_output=prepared.expected_output,
        agent=agent,
        output_pydantic=spec.output_model,
    )
    crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
    try:
        result = crew.kickoff()
    except Exception as exc:
        crewai_event_bus.flush()
        if ctx.cancel.is_set():
            raise Cancelled("Tarea cancelada.") from exc
        raise
    crewai_event_bus.flush()
    ctx.check_cancelled()
    output = (
        result.pydantic
        if result.pydantic is not None
        else _coerce(result.raw or "", spec)
    )
    return output, engine, usage_from_metrics(result.token_usage, engine)
