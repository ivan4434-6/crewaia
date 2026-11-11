"""Bridges CrewAI's runtime into the app's activity log.

What the office shows about an agent ("está consultando el modelo", "no pudo
usar la herramienta") comes from events CrewAI emits while the agent really
runs. Nothing in the UI is driven by timers or scripted sequences.

CrewAI delivers events to handlers on a thread pool, so arrival order is not
guaranteed. The ordered start/finish lines of each tool are therefore written
by the tool wrapper itself (``agents.tools.OfficeTool``); this module handles
the signals whose order does not matter.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from creator_office.agents import context
from creator_office.agents.tools import TOOL_LABELS
from creator_office.database import session_scope
from creator_office.events import publish_progress, record_event
from creator_office.pipeline.catalog import AGENTS

logger = logging.getLogger(__name__)
_installed = False


def _ctx_for(event: Any) -> context.RunContext | None:
    name = getattr(event, "task_name", None) or getattr(
        getattr(event, "from_task", None), "name", None
    )
    run_id = context.run_id_from_task_name(name)
    return context.get(run_id) if run_id else None


def _log(
    ctx: context.RunContext, type_: str, message: str, data: dict[str, Any]
) -> None:
    try:
        with session_scope() as session:
            record_event(
                session,
                workspace_id=ctx.workspace_id,
                project_id=ctx.project_id,
                step_id=ctx.step_id,
                run_id=ctx.run_id,
                actor_type="agent",
                actor=ctx.agent,
                type=type_,
                message=message,
                data=data,
            )
    except Exception:
        logger.exception("Could not record agent event %s", type_)


def install() -> None:
    """Register handlers once per process and silence CrewAI's console panels."""
    global _installed
    if _installed:
        return
    _installed = True

    from crewai.events import (
        LLMCallCompletedEvent,
        LLMCallStartedEvent,
        ToolUsageErrorEvent,
        crewai_event_bus,
    )
    from crewai.events.event_listener import event_listener
    from crewai.events.types.tool_usage_events import ToolValidateInputErrorEvent
    from crewai.hooks import before_llm_call

    if os.environ.get("CREATOR_OFFICE_CREWAI_CONSOLE", "").lower() not in (
        "1",
        "true",
        "yes",
    ):
        event_listener.formatter.verbose = False

    @crewai_event_bus.on(LLMCallStartedEvent)
    def _llm_started(_source: Any, event: LLMCallStartedEvent) -> None:
        ctx = _ctx_for(event)
        if ctx is None or ctx.cancel.is_set():
            return
        ctx.usage["llm_calls_started"] = int(ctx.usage.get("llm_calls_started", 0)) + 1
        publish_progress(
            ctx.workspace_id, ctx.run_id, ctx.agent, None, "Consultando el modelo de IA"
        )

    @crewai_event_bus.on(LLMCallCompletedEvent)
    def _llm_completed(_source: Any, event: LLMCallCompletedEvent) -> None:
        ctx = _ctx_for(event)
        if ctx is None or ctx.cancel.is_set():
            return
        publish_progress(
            ctx.workspace_id,
            ctx.run_id,
            ctx.agent,
            None,
            "Procesando la respuesta del modelo",
        )

    def _tool_misuse(event: Any) -> None:
        ctx = _ctx_for(event)
        if ctx is None or ctx.cancel.is_set():
            return
        label = TOOL_LABELS.get(event.tool_name, event.tool_name)
        name = AGENTS[ctx.agent].name if ctx.agent in AGENTS else "El agente"
        _log(
            ctx,
            "agent.tool_failed",
            f"{name} no pudo {label}: {str(event.error)[:300]}",
            {"tool": event.tool_name},
        )

    @crewai_event_bus.on(ToolUsageErrorEvent)
    def _tool_error(_source: Any, event: ToolUsageErrorEvent) -> None:
        _tool_misuse(event)

    @crewai_event_bus.on(ToolValidateInputErrorEvent)
    def _tool_input_error(_source: Any, event: ToolValidateInputErrorEvent) -> None:
        _tool_misuse(event)

    @before_llm_call
    def _stop_cancelled_runs(hook_context: Any) -> bool | None:
        """Block the next model call of a run the user paused or cancelled."""
        task = getattr(hook_context, "task", None)
        run_id = context.run_id_from_task_name(getattr(task, "name", None))
        ctx = context.get(run_id) if run_id else None
        if ctx is not None and ctx.cancel.is_set():
            return False
        return None
