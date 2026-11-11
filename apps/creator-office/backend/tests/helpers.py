"""Helpers shared by the tests: a signed-in browser and a way to run queued jobs."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from creator_office import database
from creator_office.models import Run
from creator_office.pipeline import worker

PASSWORD = "correct horse battery"


def drain(limit: int = 30) -> list[str]:
    """Run every queued job in this thread, in order, until none is left."""
    done: list[str] = []
    for _ in range(limit):
        with database.session_scope() as session:
            ids = [
                r.id
                for r in session.scalars(
                    select(Run).where(Run.status == "queued").order_by(Run.created_at)
                )
            ]
        if not ids:
            return done
        for run_id in ids:
            worker.run_now(run_id)
            done.append(run_id)
    raise AssertionError("jobs keep being queued")


class Api:
    """A signed-in browser: keeps the cookie and sends the CSRF header."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.csrf = ""
        self.session: dict[str, Any] = {}

    def register(
        self,
        name: str = "Ana",
        email: str = "ana@example.com",
        workspace: str = "Canal de Ana",
        invite: str = "",
        timezone: str = "",
    ) -> Api:
        response = self.client.post(
            "/api/auth/register",
            json={
                "name": name,
                "email": email,
                "password": PASSWORD,
                "workspace_name": workspace,
                "invite": invite,
                "timezone": timezone,
            },
        )
        assert response.status_code == 200, response.text
        self.session = response.json()
        self.csrf = self.session["csrf"]
        return self

    def request(
        self, method: str, url: str, expect: int | None = 200, **kwargs: Any
    ) -> Any:
        headers = {"X-CSRF-Token": self.csrf, **kwargs.pop("headers", {})}
        response = self.client.request(method, url, headers=headers, **kwargs)
        if expect is not None:
            assert response.status_code == expect, (
                f"{method} {url} → {response.status_code}: {response.text[:600]}"
            )
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response

    def get(self, url: str, expect: int | None = 200, **kwargs: Any) -> Any:
        return self.request("GET", url, expect, **kwargs)

    def post(
        self, url: str, json: Any = None, expect: int | None = 200, **kwargs: Any
    ) -> Any:
        return self.request(
            "POST", url, expect, json=json if json is not None else {}, **kwargs
        )

    def patch(self, url: str, json: Any, expect: int | None = 200) -> Any:
        return self.request("PATCH", url, expect, json=json)

    def delete(self, url: str, expect: int | None = 200) -> Any:
        return self.request("DELETE", url, expect)

    def step(
        self,
        project_id: str,
        kind: str,
        action: str,
        expect: int | None = 200,
        **extra: Any,
    ) -> Any:
        return self.post(
            f"/api/projects/{project_id}/steps/{kind}",
            {"action": action, **extra},
            expect,
        )


SRT = """1
00:00:00,500 --> 00:00:05,500
Hoy te cuento cómo planifico una semana de vídeos.

2
00:00:08,200 --> 00:00:13,800
El truco está en grabar por bloques y no cada día.

3
00:00:16,300 --> 00:00:21,700
Y al final reviso qué funcionó mirando la retención.
"""
