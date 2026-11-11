"""Local stand-in used when no AI provider is configured.

It plugs into CrewAI as a custom ``BaseLLM`` so the whole orchestration path
(agents, tasks, tools, events, flows) is the real one. What it is *not* is a
language model: its replies come from ``demo_plans`` and are labelled as demo
output everywhere they are shown.
"""

from __future__ import annotations

import json
from typing import Any

from crewai import BaseLLM

from creator_office.agents import context, demo_plans
from creator_office.agents.llm import DEMO_MODEL


class DemoLLM(BaseLLM):
    def __init__(self) -> None:
        super().__init__(model=DEMO_MODEL, temperature=0)

    def supports_function_calling(self) -> bool:
        # Tools are requested with the plain-text ReAct protocol.
        return False

    def supports_stop_words(self) -> bool:
        return False

    def get_context_window_size(self) -> int:
        return 32_000

    def get_input_tokens_count(
        self, messages: Any, tools: Any = None, response_model: Any = None
    ) -> int:
        # Nothing is sent to a model, so there are no tokens to count.
        return 0

    def call(  # type: ignore[override]
        self,
        messages: Any,
        tools: Any = None,
        callbacks: Any = None,
        available_functions: Any = None,
        **kwargs: Any,
    ) -> str:
        task = kwargs.get("from_task")
        run_id = context.run_id_from_task_name(getattr(task, "name", None))
        ctx = context.get(run_id) if run_id else None
        if ctx is None:
            return "Thought: I now know the final answer\nFinal Answer: Motor local de demostración: no es un modelo de lenguaje."
        if ctx.cancel.is_set():
            return "Thought: I now know the final answer\nFinal Answer: {}"
        plan = demo_plans.PLANS.get(ctx.kind)
        if plan is None:
            return "Thought: I now know the final answer\nFinal Answer: {}"
        kind, name, payload = plan(ctx)
        if kind == "tool":
            return (
                f"Thought: I need to use the {name} tool.\n"
                f"Action: {name}\n"
                f"Action Input: {json.dumps(payload, ensure_ascii=False)}"
            )
        return "Thought: I now know the final answer\nFinal Answer: " + json.dumps(
            payload, ensure_ascii=False
        )
