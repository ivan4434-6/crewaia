"""Request authentication, CSRF and workspace isolation.

Every query in the API goes through ``Auth.workspace_id``: a record that
belongs to another workspace is reported as "not found", never as forbidden,
so its existence does not leak.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from creator_office.database import Base, get_db, utcnow
from creator_office.events import record_event
from creator_office.models import AuthSession, Membership, Project, User, Workspace
from creator_office.security import SESSION_COOKIE, token_hash

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ROLE_RANK = {"viewer": 0, "editor": 1, "owner": 2}
ROLE_LABELS = {"owner": "Propietario", "editor": "Editor", "viewer": "Lector"}

# "function" scope: the transaction is committed before the response is sent,
# so a client that refetches right after a change always sees it.
Db = Annotated[Session, Depends(get_db, scope="function")]
Model = TypeVar("Model", bound=Base)


class ApiError(HTTPException):
    """An error whose ``detail`` is a message meant for the user, in Spanish."""

    def __init__(
        self, status_code: int, message: str, code: str = "", **extra: Any
    ) -> None:
        super().__init__(
            status_code=status_code, detail={"message": message, "code": code, **extra}
        )


def not_found(what: str = "El elemento") -> ApiError:
    return ApiError(
        404, f"{what} no existe o no pertenece a este espacio de trabajo.", "not_found"
    )


@dataclass
class Auth:
    user: User
    workspace: Workspace
    role: str
    session: AuthSession

    @property
    def workspace_id(self) -> str:
        return self.workspace.id

    @property
    def can_edit(self) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK["editor"]

    def log(
        self,
        db: Session,
        type_: str,
        message: str,
        *,
        project_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Audit trail entry for something this person did."""
        record_event(
            db,
            workspace_id=self.workspace_id,
            project_id=project_id,
            actor_type="user",
            actor=self.user.name,
            type=type_,
            message=message,
            data={"user_id": self.user.id, **(data or {})},
        )


def _load_session(request: Request, db: Session) -> AuthSession | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = db.get(AuthSession, token_hash(token))
    if row is None:
        return None
    if row.expires_at <= utcnow():
        db.delete(row)
        return None
    return row


def current_auth(request: Request, db: Db) -> Auth:
    session = _load_session(request, db)
    if session is None:
        raise ApiError(401, "Inicia sesión para continuar.", "unauthenticated")
    user = db.get(User, session.user_id)
    if user is None:
        raise ApiError(401, "Inicia sesión para continuar.", "unauthenticated")
    membership = db.scalar(
        select(Membership).where(
            Membership.user_id == user.id,
            Membership.workspace_id == session.workspace_id,
        )
    )
    if membership is None:
        # Removed from the workspace they had open: fall back to any other one.
        membership = db.scalar(
            select(Membership)
            .where(Membership.user_id == user.id)
            .order_by(Membership.created_at)
        )
        if membership is None:
            raise ApiError(
                403,
                "Tu cuenta ya no pertenece a ningún espacio de trabajo.",
                "no_workspace",
            )
        session.workspace_id = membership.workspace_id
    workspace = db.get(Workspace, membership.workspace_id)
    if workspace is None:
        raise ApiError(403, "El espacio de trabajo ya no existe.", "no_workspace")
    if request.method not in SAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not sent or not hmac.compare_digest(sent, session.csrf_token):
            raise ApiError(
                403,
                "La sesión ha cambiado. Recarga la página e inténtalo de nuevo.",
                "csrf",
            )
    return Auth(user=user, workspace=workspace, role=membership.role, session=session)


Current = Annotated[Auth, Depends(current_auth)]


def editor_auth(auth: Current) -> Auth:
    if not auth.can_edit:
        raise ApiError(403, "Tu rol en este espacio es de solo lectura.", "read_only")
    return auth


def owner_auth(auth: Current) -> Auth:
    if auth.role != "owner":
        raise ApiError(
            403, "Solo un propietario del espacio puede hacer esto.", "owner_only"
        )
    return auth


Editor = Annotated[Auth, Depends(editor_auth)]
Owner = Annotated[Auth, Depends(owner_auth)]


def owned(
    db: Session, auth: Auth, model: type[Model], ident: str, what: str = "El elemento"
) -> Model:
    """Load a row by id, but only if it belongs to the caller's workspace."""
    row = db.get(model, ident)
    if row is None or getattr(row, "workspace_id", None) != auth.workspace_id:
        raise not_found(what)
    return row


def project_of(db: Session, auth: Auth, project_id: str) -> Project:
    return owned(db, auth, Project, project_id, "El proyecto")
