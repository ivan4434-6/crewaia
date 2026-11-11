"""Accounts, sessions, workspaces, members and invitations."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from creator_office import clock
from creator_office.api import serializers
from creator_office.api.deps import (
    ROLE_LABELS,
    ApiError,
    Current,
    Db,
    Owner,
    _load_session,
)
from creator_office.database import utcnow
from creator_office.events import record_event
from creator_office.models import AuthSession, Invite, Membership, User, Workspace
from creator_office.security import (
    build_session_cookie,
    hash_password,
    login_throttle,
    new_token,
    session_expiry,
    token_hash,
    verify_password,
)
from creator_office.services import brand as brand_service
from creator_office.settings import get_settings

router = APIRouter(prefix="/api", tags=["auth"])

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Verifying against this when the account does not exist keeps response times alike.
_DUMMY_HASH = hash_password("creator-office-placeholder")


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    # An invitation link opened before signing in. At registration it decides the workspace; at
    # sign-in it lets an account that was left without any workspace come back.
    invite: str = Field(default="", max_length=200)


class Registration(Credentials):
    name: str = Field(max_length=120)
    workspace_name: str = Field(default="", max_length=120)
    # The browser's zone, used as the starting value for the new workspace.
    timezone: str = Field(default="", max_length=64)


def _workspace_brief(workspace: Workspace) -> dict[str, Any]:
    zone = clock.zone_name(workspace.settings)
    return {
        "id": workspace.id,
        "name": workspace.name,
        "timezone": zone,
        "timezone_label": clock.label(zone),
    }


def _new_workspace(name: str, timezone: str) -> Workspace:
    zone = clock.canonical(timezone)
    return Workspace(
        name=name,
        brand=brand_service.default_brand(name),
        settings={"timezone": zone} if zone else {},
    )


def _is_secure(request: Request) -> bool:
    return (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    )


def _open_session(
    db: Session, request: Request, response: Response, user: User, workspace_id: str
) -> AuthSession:
    token = new_token()
    row = AuthSession(
        token_hash=token_hash(token),
        user_id=user.id,
        workspace_id=workspace_id,
        csrf_token=new_token(),
        user_agent=request.headers.get("user-agent", "")[:255],
        expires_at=session_expiry(),
    )
    db.add(row)
    response.headers.append(
        "set-cookie", build_session_cookie(token, secure_request=_is_secure(request))
    )
    return row


def _session_payload(db: Session, user: User, session: AuthSession) -> dict[str, Any]:
    memberships = list(
        db.scalars(
            select(Membership)
            .where(Membership.user_id == user.id)
            .order_by(Membership.created_at)
        )
    )
    workspaces = {
        w.id: w
        for w in db.scalars(
            select(Workspace).where(
                Workspace.id.in_([m.workspace_id for m in memberships])
            )
        )
    }
    current = next(
        (m for m in memberships if m.workspace_id == session.workspace_id),
        memberships[0],
    )
    workspace = workspaces[current.workspace_id]
    return {
        "user": serializers.user(user),
        "workspace": _workspace_brief(workspace),
        "role": current.role,
        "role_label": ROLE_LABELS[current.role],
        "csrf": session.csrf_token,
        "workspaces": [
            {
                "id": m.workspace_id,
                "name": workspaces[m.workspace_id].name,
                "role": m.role,
            }
            for m in memberships
            if m.workspace_id in workspaces
        ],
        "capabilities": serializers.capabilities(db, workspace.id),
        "catalog": serializers.catalog(),
        "limits": {"max_upload_mb": get_settings().max_upload_mb},
    }


def _log_joined(db: Session, workspace_id: str, user: User, role: str) -> None:
    record_event(
        db,
        workspace_id=workspace_id,
        actor_type="user",
        actor=user.name,
        type="workspace.member_joined",
        message=f"{user.name} se unió al espacio con una invitación (rol {ROLE_LABELS[role].lower()}).",
        data={"user_id": user.id, "role": role},
    )


def _valid_invite(db: Session, token: str) -> Invite | None:
    if not token:
        return None
    invite = db.get(Invite, token_hash(token))
    if invite is None or invite.used_by is not None or invite.expires_at <= utcnow():
        return None
    return invite


@router.get("/auth/config")
def auth_config(db: Db, invite: str = "") -> dict[str, Any]:
    """What the sign-in screen needs before anyone is authenticated."""
    row = _valid_invite(db, invite)
    workspace = db.get(Workspace, row.workspace_id) if row else None
    return {
        "allow_registration": get_settings().allow_registration,
        "invite": {
            "valid": True,
            "workspace": workspace.name,
            "role_label": ROLE_LABELS[row.role],
        }
        if row and workspace
        else None,
        "invite_invalid": bool(invite) and row is None,
    }


@router.post("/auth/register")
def register(
    body: Registration, request: Request, response: Response, db: Db
) -> dict[str, Any]:
    invite = _valid_invite(db, body.invite)
    if body.invite and invite is None:
        raise ApiError(
            400,
            "La invitación no es válida o ha caducado. Pide una nueva.",
            "invite_invalid",
        )
    if not get_settings().allow_registration and invite is None:
        raise ApiError(
            403,
            "El registro está cerrado. Necesitas una invitación.",
            "registration_closed",
        )
    email = body.email.strip().lower()
    name = " ".join(body.name.split())
    if not _EMAIL.match(email):
        raise ApiError(
            422, "Escribe un correo electrónico válido.", "invalid_email", field="email"
        )
    if len(name) < 2:
        raise ApiError(422, "Escribe tu nombre.", "invalid_name", field="name")
    if len(body.password) < 10:
        raise ApiError(
            422,
            "La contraseña debe tener al menos 10 caracteres.",
            "weak_password",
            field="password",
        )
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise ApiError(
            409,
            "Ya existe una cuenta con ese correo. Inicia sesión.",
            "email_taken",
            field="email",
        )
    user = User(email=email, name=name, password_hash=hash_password(body.password))
    db.add(user)
    db.flush()
    if invite is not None:
        workspace_id, role = invite.workspace_id, invite.role
        invite.used_by = user.id
    else:
        workspace_name = (
            " ".join(body.workspace_name.split()) or f"Estudio de {name.split()[0]}"
        )
        workspace = _new_workspace(workspace_name[:120], body.timezone)
        db.add(workspace)
        db.flush()
        workspace_id, role = workspace.id, "owner"
    db.add(Membership(workspace_id=workspace_id, user_id=user.id, role=role))
    db.flush()
    if invite is not None:
        _log_joined(db, workspace_id, user, role)
    session = _open_session(db, request, response, user, workspace_id)
    db.flush()
    return _session_payload(db, user, session)


@router.post("/auth/login")
def login(
    body: Credentials, request: Request, response: Response, db: Db
) -> dict[str, Any]:
    email = body.email.strip().lower()
    key = f"{request.client.host if request.client else '-'}|{email}"
    if login_throttle.blocked(key):
        raise ApiError(
            429,
            "Demasiados intentos fallidos. Espera unos minutos antes de volver a probar.",
            "throttled",
        )
    user = db.scalar(select(User).where(User.email == email))
    if (
        not verify_password(body.password, user.password_hash if user else _DUMMY_HASH)
        or user is None
    ):
        login_throttle.fail(key)
        raise ApiError(
            401, "El correo o la contraseña no son correctos.", "bad_credentials"
        )
    login_throttle.reset(key)
    membership = db.scalar(
        select(Membership)
        .where(Membership.user_id == user.id)
        .order_by(Membership.created_at)
    )
    invite_used = False
    if membership is None:
        # The account was removed from (or left) its last workspace. It must not become a dead end:
        # an invitation brings it back, and where anyone may register it simply gets a workspace of its own.
        invite = _valid_invite(db, body.invite)
        if invite is not None:
            membership = Membership(
                workspace_id=invite.workspace_id, user_id=user.id, role=invite.role
            )
            invite.used_by, invite_used = user.id, True
            _log_joined(db, invite.workspace_id, user, invite.role)
        elif get_settings().allow_registration:
            workspace = _new_workspace(f"Estudio de {user.name.split()[0]}"[:120], "")
            db.add(workspace)
            db.flush()
            membership = Membership(
                workspace_id=workspace.id, user_id=user.id, role="owner"
            )
        else:
            raise ApiError(
                403,
                "Tu cuenta ya no pertenece a ningún espacio de trabajo. Pide una invitación nueva y abre su enlace para volver a entrar.",
                "no_workspace",
            )
        db.add(membership)
        db.flush()
    session = _open_session(db, request, response, user, membership.workspace_id)
    db.flush()
    return {**_session_payload(db, user, session), "invite_used": invite_used}


@router.post("/auth/logout")
def logout(request: Request, response: Response, db: Db) -> dict[str, bool]:
    session = _load_session(request, db)
    if session is not None:
        db.delete(session)
    response.headers.append(
        "set-cookie",
        build_session_cookie("", secure_request=_is_secure(request), clear=True),
    )
    return {"ok": True}


@router.get("/session")
def session_info(auth: Current, db: Db) -> dict[str, Any]:
    return _session_payload(db, auth.user, auth.session)


class WorkspaceSwitch(BaseModel):
    workspace_id: str


@router.post("/session/workspace")
def switch_workspace(body: WorkspaceSwitch, auth: Current, db: Db) -> dict[str, Any]:
    membership = db.scalar(
        select(Membership).where(
            Membership.user_id == auth.user.id,
            Membership.workspace_id == body.workspace_id,
        )
    )
    if membership is None:
        raise ApiError(404, "No perteneces a ese espacio de trabajo.", "not_found")
    auth.session.workspace_id = body.workspace_id
    db.flush()
    return _session_payload(db, auth.user, auth.session)


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    timezone: str = Field(default="", max_length=64)


class WorkspaceChange(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    timezone: str | None = Field(default=None, max_length=64)


@router.post("/workspaces")
def create_workspace(body: WorkspaceCreate, auth: Current, db: Db) -> dict[str, Any]:
    name = " ".join(body.name.split())
    workspace = _new_workspace(name, body.timezone)
    db.add(workspace)
    db.flush()
    db.add(Membership(workspace_id=workspace.id, user_id=auth.user.id, role="owner"))
    auth.session.workspace_id = workspace.id
    db.flush()
    return _session_payload(db, auth.user, auth.session)


@router.patch("/workspace")
def change_workspace(body: WorkspaceChange, auth: Owner, db: Db) -> dict[str, Any]:
    workspace = auth.workspace
    if body.name is not None:
        name = " ".join(body.name.split())
        if len(name) < 2:
            raise ApiError(
                422, "Escribe un nombre para el espacio.", "invalid_name", field="name"
            )
        if name != workspace.name:
            workspace.name = name
            auth.log(
                db,
                "workspace.renamed",
                f"{auth.user.name} cambió el nombre del espacio a «{name}».",
            )
    if body.timezone is not None:
        zone = clock.canonical(body.timezone)
        if zone is None:
            raise ApiError(
                422,
                "Esa zona horaria no existe. Usa un nombre como «Europe/Madrid».",
                "invalid_timezone",
                field="timezone",
            )
        if zone != clock.zone_name(workspace.settings):
            workspace.settings = {**(workspace.settings or {}), "timezone": zone}
            auth.log(
                db,
                "workspace.timezone_changed",
                f"{auth.user.name} cambió la zona horaria del espacio a {zone}.",
            )
    return _workspace_brief(workspace)


@router.get("/workspace/members")
def members(auth: Current, db: Db) -> dict[str, Any]:
    rows = db.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.workspace_id == auth.workspace_id)
        .order_by(Membership.created_at)
    ).all()
    invites = []
    if auth.role == "owner":
        invites = [
            {
                "id": i.token_hash[:12],
                "role": i.role,
                "role_label": ROLE_LABELS[i.role],
                "expires_at": serializers.iso(i.expires_at),
            }
            for i in db.scalars(
                select(Invite).where(
                    Invite.workspace_id == auth.workspace_id,
                    Invite.used_by.is_(None),
                    Invite.expires_at > utcnow(),
                )
            )
        ]
    return {
        "members": [
            {
                **serializers.user(u),
                "role": m.role,
                "role_label": ROLE_LABELS[m.role],
                "is_you": u.id == auth.user.id,
                "joined_at": serializers.iso(m.created_at),
            }
            for m, u in rows
        ],
        "invites": invites,
        "roles": [{"key": key, "label": label} for key, label in ROLE_LABELS.items()],
    }


class InviteRequest(BaseModel):
    role: str = "editor"


@router.post("/workspace/invites")
def create_invite(body: InviteRequest, auth: Owner, db: Db) -> dict[str, Any]:
    if body.role not in ROLE_LABELS:
        raise ApiError(422, "Rol no válido.", "invalid_role")
    token = new_token()
    db.add(
        Invite(
            token_hash=token_hash(token),
            workspace_id=auth.workspace_id,
            role=body.role,
            created_by=auth.user.id,
            expires_at=utcnow() + timedelta(days=7),
        )
    )
    auth.log(
        db,
        "workspace.invite_created",
        f"{auth.user.name} creó una invitación con rol {ROLE_LABELS[body.role].lower()}.",
    )
    # The token is only shown once: it is stored hashed.
    return {
        "token": token,
        "path": f"/registro?invitacion={token}",
        "expires_in_days": 7,
        "role_label": ROLE_LABELS[body.role],
    }


class InviteAccept(BaseModel):
    token: str = Field(max_length=200)


@router.post("/workspace/invites/accept")
def accept_invite(body: InviteAccept, auth: Current, db: Db) -> dict[str, Any]:
    """Join the inviting workspace with the account that is already signed in."""
    invite = _valid_invite(db, body.token)
    if invite is None:
        raise ApiError(
            400,
            "La invitación no es válida o ha caducado. Pide una nueva.",
            "invite_invalid",
        )
    already = db.scalar(
        select(Membership).where(
            Membership.workspace_id == invite.workspace_id,
            Membership.user_id == auth.user.id,
        )
    )
    if (
        already is None
    ):  # someone who is already a member keeps their role and does not use up the invitation
        db.add(
            Membership(
                workspace_id=invite.workspace_id, user_id=auth.user.id, role=invite.role
            )
        )
        invite.used_by = auth.user.id
        _log_joined(db, invite.workspace_id, auth.user, invite.role)
    auth.session.workspace_id = invite.workspace_id
    db.flush()
    return _session_payload(db, auth.user, auth.session)


@router.delete("/workspace/invites/{invite_id}")
def revoke_invite(invite_id: str, auth: Owner, db: Db) -> dict[str, bool]:
    for invite in db.scalars(
        select(Invite).where(
            Invite.workspace_id == auth.workspace_id, Invite.used_by.is_(None)
        )
    ):
        if invite.token_hash.startswith(invite_id):
            db.delete(invite)
            return {"ok": True}
    raise ApiError(404, "La invitación ya no existe.", "not_found")


class RoleChange(BaseModel):
    role: str


def _owner_count(db: Session, workspace_id: str) -> int:
    return (
        db.scalar(
            select(func.count(Membership.id)).where(
                Membership.workspace_id == workspace_id, Membership.role == "owner"
            )
        )
        or 0
    )


@router.patch("/workspace/members/{user_id}")
def change_role(user_id: str, body: RoleChange, auth: Owner, db: Db) -> dict[str, bool]:
    if body.role not in ROLE_LABELS:
        raise ApiError(422, "Rol no válido.", "invalid_role")
    membership = db.scalar(
        select(Membership).where(
            Membership.workspace_id == auth.workspace_id, Membership.user_id == user_id
        )
    )
    if membership is None:
        raise ApiError(404, "Esa persona no pertenece a este espacio.", "not_found")
    if (
        membership.role == "owner"
        and body.role != "owner"
        and _owner_count(db, auth.workspace_id) <= 1
    ):
        raise ApiError(
            409, "El espacio necesita al menos un propietario.", "last_owner"
        )
    membership.role = body.role
    target = db.get(User, user_id)
    auth.log(
        db,
        "workspace.role_changed",
        f"{auth.user.name} cambió el rol de {target.name if target else user_id} a {ROLE_LABELS[body.role].lower()}.",
    )
    return {"ok": True}


@router.delete("/workspace/members/{user_id}")
def remove_member(user_id: str, auth: Current, db: Db) -> dict[str, bool]:
    if user_id != auth.user.id and auth.role != "owner":
        raise ApiError(
            403, "Solo un propietario puede quitar a otras personas.", "owner_only"
        )
    membership = db.scalar(
        select(Membership).where(
            Membership.workspace_id == auth.workspace_id, Membership.user_id == user_id
        )
    )
    if membership is None:
        raise ApiError(404, "Esa persona no pertenece a este espacio.", "not_found")
    if membership.role == "owner" and _owner_count(db, auth.workspace_id) <= 1:
        raise ApiError(
            409, "El espacio necesita al menos un propietario.", "last_owner"
        )
    target = db.get(User, user_id)
    db.delete(membership)
    auth.log(
        db,
        "workspace.member_removed",
        f"{auth.user.name} quitó a {target.name if target else user_id} del espacio.",
    )
    return {"ok": True}
