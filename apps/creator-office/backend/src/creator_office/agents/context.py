"""Per-run context shared by the flow, the tools and the event bridge."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import update

from creator_office.database import session_scope
from creator_office.events import publish_progress
from creator_office.media.ffmpeg import Cancelled
from creator_office.models import Run


@dataclass
class RunContext:
    run_id: str
    workspace_id: str
    project_id: str | None
    step_id: str | None
    agent: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    cancel: threading.Event = field(default_factory=threading.Event)
    # Raw (non-serialised) tool results, read by the code that persists the
    # outcome and by the local demo engine.
    tool_results: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[str] = field(default_factory=list)
    brief: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    _last_progress_write: float = 0.0

    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise Cancelled("Tarea cancelada.")

    def progress(self, fraction: float | None, label: str = "") -> None:
        """Report measured progress (``None`` clears the bar)."""
        self.check_cancelled()
        if fraction is not None:
            fraction = max(0.0, min(1.0, fraction))
        publish_progress(self.workspace_id, self.run_id, self.agent, fraction, label)
        now = time.monotonic()
        if fraction in (None, 1.0) or now - self._last_progress_write >= 1.0:
            self._last_progress_write = now
            with session_scope() as session:
                session.execute(
                    update(Run)
                    .where(Run.id == self.run_id)
                    .values(progress=fraction, progress_label=label[:240])
                )


_ACTIVE: dict[str, RunContext] = {}
_LOCK = threading.Lock()


def register(ctx: RunContext) -> None:
    with _LOCK:
        _ACTIVE[ctx.run_id] = ctx


def unregister(run_id: str) -> None:
    with _LOCK:
        _ACTIVE.pop(run_id, None)


def get(run_id: str) -> RunContext | None:
    with _LOCK:
        return _ACTIVE.get(run_id)


def request_cancel(run_id: str) -> bool:
    ctx = get(run_id)
    if ctx is None:
        return False
    ctx.cancel.set()
    return True


def task_name(run_id: str) -> str:
    return f"co-run:{run_id}"


def run_id_from_task_name(name: str | None) -> str | None:
    if name and name.startswith("co-run:"):
        return name.split(":", 1)[1]
    return None
