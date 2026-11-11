"""Accounts, sessions, CSRF, roles and isolation between workspaces."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from creator_office import security
from creator_office.database import session_scope
from creator_office.models import Membership
from tests.helpers import PASSWORD, Api, drain

PROJECT = {
    "title": "Proyecto privado de Ana",
    "platforms": ["youtube"],
    "topic": "tema confidencial",
}


def test_registration_login_logout_and_cookie_flags(app_client: TestClient) -> None:
    assert app_client.get("/api/session").status_code == 401
    weak = app_client.post(
        "/api/auth/register",
        json={"name": "Ana", "email": "ana@example.com", "password": "corta"},
    )
    assert weak.status_code == 422 and weak.json()["field"] == "password"

    api = Api(app_client).register()
    cookie = app_client.cookies.get("co_session")
    assert (
        cookie
        and api.session["role"] == "owner"
        and api.session["workspace"]["name"] == "Canal de Ana"
    )
    assert (
        app_client.post(
            "/api/auth/register",
            json={"name": "Otra", "email": "ANA@example.com", "password": PASSWORD},
        ).status_code
        == 409
    )

    # State-changing requests need the CSRF token of the session.
    assert app_client.post("/api/projects", json=PROJECT).json()["code"] == "csrf"
    assert (
        app_client.post(
            "/api/projects", json=PROJECT, headers={"X-CSRF-Token": "forged"}
        ).status_code
        == 403
    )
    api.post("/api/projects", PROJECT)

    api.post("/api/auth/logout")
    assert app_client.get("/api/session").status_code == 401
    assert (
        app_client.post(
            "/api/auth/login",
            json={"email": "ana@example.com", "password": "incorrecta-123"},
        ).json()["code"]
        == "bad_credentials"
    )
    logged = app_client.post(
        "/api/auth/login", json={"email": "ana@example.com", "password": PASSWORD}
    )
    assert logged.status_code == 200
    header = logged.headers["set-cookie"]
    assert "HttpOnly" in header and "SameSite=Lax" in header and "Path=/" in header
    assert logged.json()["csrf"] != api.csrf  # a new session gets a new token


def test_login_is_throttled_after_repeated_failures(app_client: TestClient) -> None:
    Api(app_client).register()
    app_client.cookies.clear()
    for _ in range(security.login_throttle.max_failures):
        assert (
            app_client.post(
                "/api/auth/login",
                json={"email": "ana@example.com", "password": "nope-nope-nope"},
            ).status_code
            == 401
        )
    blocked = app_client.post(
        "/api/auth/login", json={"email": "ana@example.com", "password": PASSWORD}
    )
    assert blocked.status_code == 429 and blocked.json()["code"] == "throttled"


def test_workspaces_cannot_see_each_other(
    api: Api, other_browser: Callable[[], Api]
) -> None:
    pid = api.post("/api/projects", PROJECT)["id"]
    drain()
    detail = api.get(f"/api/projects/{pid}")
    api.step(pid, "research", "skip")
    drain()

    bea = other_browser().register("Bea", "bea@example.com", "Estudio de Bea")
    assert bea.get("/api/projects")["projects"] == []
    assert bea.get("/api/office")["events"] == []
    assert bea.get("/api/activity")["events"] == []
    assert bea.get("/api/search?q=confidencial")["results"] == []
    assert bea.get("/api/runs")["runs"] == []
    # Another workspace's records answer exactly like records that do not exist.
    missing = bea.get("/api/projects/does-not-exist", expect=404)
    assert bea.get(f"/api/projects/{pid}", expect=404) == missing
    bea.step(pid, "script", "approve", expect=404)
    bea.patch(f"/api/projects/{pid}", {"title": "Robado"}, expect=404)
    bea.delete(f"/api/projects/{pid}", expect=404)
    bea.get(f"/api/projects/{pid}/documents/research", expect=404)
    bea.post(f"/api/projects/{pid}/comments/import", {"text": "hola"}, expect=404)
    run_id = api.get("/api/runs")["runs"][0]["id"]
    bea.post(f"/api/runs/{run_id}/cancel", expect=404)
    assert api.get(f"/api/projects/{pid}")["title"] == detail["title"]

    # Switching to a workspace you do not belong to is refused.
    bea.post(
        "/api/session/workspace",
        {"workspace_id": api.session["workspace"]["id"]},
        expect=404,
    )


def test_private_files_need_a_session_or_a_valid_signature(
    api: Api, other_browser: Callable[[], Api], app_client: TestClient
) -> None:
    from creator_office import storage
    from creator_office.database import session_scope

    workspace_id = api.session["workspace"]["id"]
    with session_scope() as session:
        relative, destination = storage.new_path(workspace_id, "nota.png")
        destination.write_bytes(b"\x89PNG fake")
        asset_id = storage.register_file(
            session,
            workspace_id=workspace_id,
            relative_path=relative,
            filename="nota.png",
            kind="image",
            origin="upload",
            created_by="t",
        ).id
    assert api.get(f"/api/files/{asset_id}").content == b"\x89PNG fake"
    bea = other_browser().register("Bea", "bea@example.com")
    bea.get(f"/api/files/{asset_id}", expect=404)
    anonymous = other_browser()
    anonymous.get(f"/api/files/{asset_id}", expect=401)
    signed = storage.signed_path(asset_id, ttl=60)
    assert anonymous.get(signed).content == b"\x89PNG fake"
    anonymous.get(signed[:-4] + "0000", expect=401)
    anonymous.get(f"/api/files/{asset_id}?exp=1&sig=abc", expect=401)


def test_roles_and_invitations(api: Api, other_browser: Callable[[], Api]) -> None:
    pid = api.post("/api/projects", PROJECT)["id"]
    invite = api.post("/api/workspace/invites", {"role": "viewer"})
    assert invite["path"].startswith("/registro?invitacion=")

    carla = other_browser()
    config = carla.get(f"/api/auth/config?invite={invite['token']}")
    assert config["invite"] == {
        "valid": True,
        "workspace": "Canal de Ana",
        "role_label": "Lector",
    }
    carla.register("Carla", "carla@example.com", invite=invite["token"])
    assert (
        carla.session["role"] == "viewer"
        and carla.session["workspace"]["name"] == "Canal de Ana"
    )
    # An invitation works once.
    reused = other_browser().client.post(
        "/api/auth/register",
        json={
            "name": "Dani",
            "email": "dani@example.com",
            "password": PASSWORD,
            "invite": invite["token"],
        },
    )
    assert reused.status_code == 400 and reused.json()["code"] == "invite_invalid"

    assert carla.get(f"/api/projects/{pid}")["title"] == PROJECT["title"]
    assert carla.post("/api/projects", PROJECT, expect=403)["code"] == "read_only"
    assert carla.step(pid, "research", "skip", expect=403)["code"] == "read_only"
    assert (
        carla.post("/api/workspace/invites", {"role": "editor"}, expect=403)["code"]
        == "owner_only"
    )
    assert (
        carla.request(
            "PUT",
            "/api/integrations/llm",
            expect=403,
            json={"model": "openai/gpt-4o-mini"},
        )["code"]
        == "owner_only"
    )
    assert (
        "solo lectura"
        in carla.post("/api/chat", {"text": "pausa el proyecto"})["messages"][1]["text"]
    )
    assert api.get(f"/api/projects/{pid}")["state"] == "active"

    members = api.get("/api/workspace/members")["members"]
    carla_id = next(m["id"] for m in members if m["email"] == "carla@example.com")
    api.patch(f"/api/workspace/members/{carla_id}", {"role": "editor"})
    carla.step(pid, "research", "pause")
    # The last owner cannot be demoted or leave.
    ana_id = next(m["id"] for m in members if m["is_you"])
    assert (
        api.patch(f"/api/workspace/members/{ana_id}", {"role": "editor"}, expect=409)[
            "code"
        ]
        == "last_owner"
    )
    assert (
        api.delete(f"/api/workspace/members/{ana_id}", expect=409)["code"]
        == "last_owner"
    )

    api.delete(f"/api/workspace/members/{carla_id}")
    assert carla.get("/api/projects", expect=403)["code"] == "no_workspace"


def test_existing_account_joins_another_workspace_with_an_invitation(
    api: Api, other_browser: Callable[[], Api]
) -> None:
    bea = other_browser().register("Bea", "bea@example.com", "Estudio de Bea")
    invite = api.post("/api/workspace/invites", {"role": "editor"})
    assert (
        bea.post(
            "/api/workspace/invites/accept", {"token": "no-such-token"}, expect=400
        )["code"]
        == "invite_invalid"
    )
    joined = bea.post("/api/workspace/invites/accept", {"token": invite["token"]})
    assert joined["workspace"]["name"] == "Canal de Ana" and joined["role"] == "editor"
    assert {w["name"] for w in joined["workspaces"]} == {
        "Estudio de Bea",
        "Canal de Ana",
    }

    # The invitation is spent, and the inviting workspace's audit log says who joined.
    dani = other_browser().register("Dani", "dani@example.com")
    assert (
        dani.post(
            "/api/workspace/invites/accept", {"token": invite["token"]}, expect=400
        )["code"]
        == "invite_invalid"
    )
    assert any(
        e["type"] == "workspace.member_joined" and "Bea" in e["message"]
        for e in api.get("/api/activity")["events"]
    )

    # A member opening a new invitation to their own workspace keeps their role and does not use it up.
    spare = api.post("/api/workspace/invites", {"role": "viewer"})
    assert (
        api.post("/api/workspace/invites/accept", {"token": spare["token"]})["role"]
        == "owner"
    )
    assert len(api.get("/api/workspace/members")["invites"]) == 1


def test_an_account_left_without_a_workspace_is_not_locked_out(
    api: Api, other_browser: Callable[[], Api], settings_env: Callable[..., None]
) -> None:
    def invite(role: str = "editor") -> str:
        return api.post("/api/workspace/invites", {"role": role})["token"]

    bea = other_browser().register("Bea", "bea@example.com", invite=invite())
    api.delete(f"/api/workspace/members/{bea.session['user']['id']}")
    # Her open session stops working at once.
    assert bea.get("/api/projects", expect=403)["code"] == "no_workspace"

    def sign_in(**extra: str) -> Any:
        return other_browser().client.post(
            "/api/auth/login",
            json={"email": "bea@example.com", "password": PASSWORD, **extra},
        )

    # Where anyone may register, signing in again gives her a workspace of her own, not Ana's.
    again = sign_in()
    assert again.status_code == 200 and again.json()["role"] == "owner"
    assert (
        again.json()["workspace"]["name"] == "Estudio de Bea"
        and again.json()["invite_used"] is False
    )
    assert len(api.get("/api/workspace/members")["members"]) == 1

    # On a closed instance she gets nothing on her own; a new invitation is the way back, and it is used up.
    with session_scope() as session:
        for membership in session.scalars(
            select(Membership).where(Membership.user_id == bea.session["user"]["id"])
        ):
            session.delete(membership)
    settings_env(ALLOW_REGISTRATION="false")
    refused = sign_in()
    assert (
        refused.status_code == 403
        and refused.json()["code"] == "no_workspace"
        and "invitación nueva" in refused.json()["message"]
    )
    assert sign_in(invite="no-existe").status_code == 403
    token = invite("viewer")
    back = sign_in(invite=token)
    assert back.status_code == 200 and back.json()["invite_used"] is True
    assert (
        back.json()["role"] == "viewer"
        and back.json()["workspace"]["name"] == "Canal de Ana"
    )
    assert api.get(f"/api/auth/config?invite={token}")["invite_invalid"] is True
