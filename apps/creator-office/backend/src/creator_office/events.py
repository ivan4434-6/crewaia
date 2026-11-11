"""Activity log, live updates and notifications.

Persisted events are the single source of truth for "what happened": they are
written in the same transaction as the state change they describe and only
broadcast to connected browsers after that transaction commits.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.orm import Session

from creator_office.database import utcnow
from creator_office.models import Event, Notification

Message = dict[str, Any]
logger = logging.getLogger(__name__)


class Broadcaster:
    """Thread-safe fan-out of messages to SSE subscribers, per workspace."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[
            str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[Message]]]
        ] = defaultdict(set)

    def subscribe(self, workspace_id: str) -> tuple[asyncio.Queue[Message], Any]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=500)
        entry = (loop, queue)
        with self._lock:
            self._subscribers[workspace_id].add(entry)

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers[workspace_id].discard(entry)

        return queue, unsubscribe

    def publish(self, workspace_id: str, message: Message) -> None:
        with self._lock:
            targets = list(self._subscribers.get(workspace_id, ()))
        for loop, queue in targets:
            # A full or closed subscriber never blocks the worker that emits.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_offer, queue, message)


def _offer(queue: asyncio.Queue[Message], message: Message) -> None:
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(message)


broadcaster = Broadcaster()

_PENDING = "co_pending_broadcasts"
_CALLBACKS = "co_after_commit"


def _queue_broadcast(session: Session, workspace_id: str, message: Message) -> None:
    session.info.setdefault(_PENDING, []).append((workspace_id, message))


def after_commit(session: Session, callback: Callable[[], None]) -> None:
    """Run ``callback`` once the session's transaction is durable (e.g. start a job)."""
    session.info.setdefault(_CALLBACKS, []).append(callback)


@sa_event.listens_for(Session, "after_commit")
def _flush_broadcasts(session: Session) -> None:
    for workspace_id, message in session.info.pop(_PENDING, []):
        broadcaster.publish(workspace_id, message)
    for callback in session.info.pop(_CALLBACKS, []):
        try:
            callback()
        except Exception:
            logger.exception("after-commit callback failed")


@sa_event.listens_for(Session, "after_rollback")
def _drop_broadcasts(session: Session) -> None:
    session.info.pop(_PENDING, None)
    session.info.pop(_CALLBACKS, None)


def event_to_dict(row: Event) -> Message:
    return {
        "id": row.id,
        "type": row.type,
        "message": row.message,
        "actor_type": row.actor_type,
        "actor": row.actor,
        "project_id": row.project_id,
        "step_id": row.step_id,
        "run_id": row.run_id,
        "data": row.data or {},
        "created_at": row.created_at.isoformat(),
    }


def record_event(
    session: Session,
    *,
    workspace_id: str,
    type: str,
    message: str,
    actor_type: str = "system",
    actor: str = "",
    project_id: str | None = None,
    step_id: str | None = None,
    run_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> Event:
    row = Event(
        workspace_id=workspace_id,
        type=type,
        message=message,
        actor_type=actor_type,
        actor=actor,
        project_id=project_id,
        step_id=step_id,
        run_id=run_id,
        data=data or {},
    )
    session.add(row)
    session.flush()
    _queue_broadcast(session, workspace_id, {"kind": "event", **event_to_dict(row)})
    return row


def notify(
    session: Session,
    *,
    workspace_id: str,
    type: str,
    title: str,
    body: str = "",
    link: str = "",
) -> Notification:
    row = Notification(
        workspace_id=workspace_id, type=type, title=title, body=body, link=link
    )
    session.add(row)
    session.flush()
    _queue_broadcast(
        session,
        workspace_id,
        {
            "kind": "notification",
            "id": row.id,
            "type": type,
            "title": title,
            "body": body,
            "link": link,
        },
    )
    return row


def settle_notifications(session: Session, workspace_id: str, link: str) -> int:
    """Close the open notices that point at ``link``: what they asked for has been decided.

    Without this the bell keeps counting approvals that were given long ago.
    """
    rows = list(
        session.scalars(
            select(Notification).where(
                Notification.workspace_id == workspace_id,
                Notification.link == link,
                Notification.read_at.is_(None),
                Notification.type.in_(("approval", "error")),
            )
        )
    )
    for row in rows:
        row.read_at = utcnow()
    return len(rows)


def publish_progress(
    workspace_id: str, run_id: str, agent: str, progress: float | None, label: str
) -> None:
    """Ephemeral progress tick; the durable value lives on the run row."""
    broadcaster.publish(
        workspace_id,
        {
            "kind": "progress",
            "run_id": run_id,
            "agent": agent,
            "progress": progress,
            "label": label,
        },
    )
