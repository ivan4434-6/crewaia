"""Database schema.

Every row that belongs to a team carries ``workspace_id``; API queries always
filter by it so one workspace can never read another one's data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from creator_office.database import Base, UTCDateTime, new_id, utcnow


def _pk() -> Mapped[str]:
    return mapped_column(String(32), primary_key=True, default=new_id)


def _ws() -> Mapped[str]:
    return mapped_column(
        String(32), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )


def _created() -> Mapped[datetime]:
    return mapped_column(UTCDateTime, default=utcnow)


JsonDict = dict[str, Any]


# --- Identity ---------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = _pk()
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = _created()


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(String(120))
    brand: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    settings: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws()
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(
        String(16), default="owner"
    )  # owner | editor | viewer
    created_at: Mapped[datetime] = _created()


class Invite(Base):
    __tablename__ = "invites"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = _ws()
    role: Mapped[str] = mapped_column(String(16), default="editor")
    created_by: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = _created()


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[str] = mapped_column(String(32))
    csrf_token: Mapped[str] = mapped_column(String(64))
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


# --- Projects and pipeline --------------------------------------------------


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws()
    title: Mapped[str] = mapped_column(String(200))
    topic: Mapped[str] = mapped_column(Text, default="")
    audience: Mapped[str] = mapped_column(Text, default="")
    goal: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(16), default="es")
    tone: Mapped[str] = mapped_column(String(120), default="")
    platforms: Mapped[list[str]] = mapped_column(JSON, default=list)
    deadline: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    references: Mapped[list[JsonDict]] = mapped_column(JSON, default=list)
    template: Mapped[str] = mapped_column(String(40), default="repurpose")
    options: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    source_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[str] = mapped_column(
        String(16), default="active"
    )  # active | paused | cancelled
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class Step(Base):
    """One pipeline stage of a project, owned by one agent (or the orchestrator)."""

    __tablename__ = "steps"
    __table_args__ = (UniqueConstraint("project_id", "kind"),)

    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws()
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(24))
    agent: Mapped[str] = mapped_column(String(16))
    position: Mapped[int] = mapped_column(Integer, default=0)
    # pending | blocked | queued | working | awaiting_approval | completed |
    # error | paused | cancelled | skipped
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    instructions: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    output: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    blocked_code: Mapped[str] = mapped_column(String(40), default="")
    blocked_reason: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    stale_reason: Mapped[str] = mapped_column(Text, default="")
    feedback: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class Run(Base):
    """A unit of background work. Agent desks in the office mirror these rows."""

    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_status_created", "status", "created_at"),)

    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws()
    project_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    step_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    agent: Mapped[str] = mapped_column(String(16), default="")
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(240))
    engine: Mapped[str] = mapped_column(
        String(24), default=""
    )  # crewai | tool | connector | rules
    provider: Mapped[str] = mapped_column(String(60), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(
        String(16), default="queued"
    )  # queued | working | done | error | cancelled
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    progress_label: Mapped[str] = mapped_column(String(240), default="")
    payload: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    result: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    usage: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Event(Base):
    """Append-only activity and audit log."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = _ws()
    project_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    step_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_type: Mapped[str] = mapped_column(
        String(16)
    )  # user | agent | orchestrator | system | platform
    actor: Mapped[str] = mapped_column(String(64), default="")
    type: Mapped[str] = mapped_column(String(48), index=True)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws()
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # research | script | publication
    ref_id: Mapped[str] = mapped_column(String(32), default="")
    title: Mapped[str] = mapped_column(String(240))
    current_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = _created()


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id: Mapped[str] = _pk()
    document_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer)
    content: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    author_type: Mapped[str] = mapped_column(String(16))  # agent | user
    author: Mapped[str] = mapped_column(String(64))
    note: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = _created()


# --- Media ------------------------------------------------------------------


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws()
    project_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(
        String(16)
    )  # video | image | audio | subtitle | export | design | poster
    origin: Mapped[str] = mapped_column(
        String(16), default="upload"
    )  # upload | agent | system
    created_by: Mapped[str] = mapped_column(String(64), default="")
    filename: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    path: Mapped[str] = mapped_column(String(400))
    meta: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    rights_declared: Mapped[bool] = mapped_column(Boolean, default=False)
    rights_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _created()


class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws()
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    asset_id: Mapped[str] = mapped_column(String(32))
    start: Mapped[float] = mapped_column(Float)
    end: Mapped[float] = mapped_column(Float)
    title: Mapped[str] = mapped_column(String(200), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    excerpt: Mapped[str] = mapped_column(Text, default="")
    signals: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default="suggested"
    )  # suggested | selected | rejected
    origin: Mapped[str] = mapped_column(String(16), default="agent")  # agent | user
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()


class Render(Base):
    __tablename__ = "renders"

    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws()
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    clip_id: Mapped[str] = mapped_column(String(32), index=True)
    format: Mapped[str] = mapped_column(String(16))  # vertical | horizontal | square
    settings: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )  # pending | rendering | ready | error | stale
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    checks: Mapped[list[JsonDict]] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(16), default="agent")  # agent | upload
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class Design(Base):
    __tablename__ = "designs"

    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws()
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # thumbnail | cover
    spec: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="ready")  # ready | error
    asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    checks: Mapped[list[JsonDict]] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


# --- Publishing -------------------------------------------------------------


class Connection(Base):
    """An authorised account or provider. Secrets are encrypted at rest."""

    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("workspace_id", "provider"),)

    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws()
    provider: Mapped[str] = mapped_column(
        String(24)
    )  # youtube | tiktok | instagram | llm
    status: Mapped[str] = mapped_column(
        String(16), default="connected"
    )  # connected | expired | error
    account_id: Mapped[str] = mapped_column(String(120), default="")
    account_name: Mapped[str] = mapped_column(String(200), default="")
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    secret: Mapped[str] = mapped_column(Text, default="")
    config: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class OAuthState(Base):
    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(24))
    code_verifier: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = _created()


class Publication(Base):
    __tablename__ = "publications"

    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws()
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    platform: Mapped[str] = mapped_column(String(24), index=True)
    render_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    design_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    options: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    scheduled_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True, index=True
    )
    # draft | scheduled | publishing | published | error | discarded
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    approved_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # How "published" was established: "platform" (API confirmed) or "manual"
    # (a person declared it). Empty until one of them happens.
    confirmation: Mapped[str] = mapped_column(String(16), default="")
    platform_post_id: Mapped[str] = mapped_column(String(200), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    validation: Mapped[list[JsonDict]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


# --- Community and analytics ------------------------------------------------


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws()
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    publication_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    platform: Mapped[str] = mapped_column(String(24), default="")
    source: Mapped[str] = mapped_column(
        String(16), default="manual"
    )  # platform | manual
    external_id: Mapped[str] = mapped_column(String(200), default="")
    author: Mapped[str] = mapped_column(String(200), default="")
    text: Mapped[str] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    category: Mapped[str] = mapped_column(
        String(16), default=""
    )  # question | positive | spam | issue | other
    category_by: Mapped[str] = mapped_column(
        String(16), default=""
    )  # rules | llm | user
    category_reason: Mapped[str] = mapped_column(Text, default="")
    reply_text: Mapped[str] = mapped_column(Text, default="")
    # none | suggested | approved | sent | sent_manual | dismissed
    reply_status: Mapped[str] = mapped_column(String(16), default="none")
    reply_by: Mapped[str] = mapped_column(String(64), default="")
    reply_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = _created()


class Metric(Base):
    __tablename__ = "metrics"

    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws()
    publication_id: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(
        String(16), default="manual"
    )  # platform | manual
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    values: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = _created()


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws()
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[JsonDict]] = mapped_column(JSON, default=list)
    basis: Mapped[str] = mapped_column(String(16), default="rules")  # rules | llm
    status: Mapped[str] = mapped_column(
        String(16), default="new"
    )  # new | accepted | dismissed
    created_at: Mapped[datetime] = _created()


# --- Notifications and orchestrator chat ------------------------------------


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws()
    type: Mapped[str] = mapped_column(String(24))  # done | error | approval | reminder
    title: Mapped[str] = mapped_column(String(240))
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str] = mapped_column(String(240), default="")
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = _created()


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws()
    user_id: Mapped[str] = mapped_column(String(32), default="")
    role: Mapped[str] = mapped_column(String(16))  # user | orchestrator
    text: Mapped[str] = mapped_column(Text)
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()
