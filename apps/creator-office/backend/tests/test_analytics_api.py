"""Figures typed by hand, what counts as incomplete, and the workspace's time zone."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from creator_office import clock
from creator_office.database import session_scope, utcnow
from creator_office.models import Publication, Workspace
from creator_office.text import number
from tests.helpers import Api, drain


def published(
    api: Api, project_id: str, platform: str, when: datetime, title: str = "Pieza"
) -> str:
    """A publication that a person declared as published at ``when`` (no media needed for the statistics)."""
    with session_scope() as session:
        row = Publication(
            workspace_id=api.session["workspace"]["id"],
            project_id=project_id,
            platform=platform,
            title=title,
            status="published",
            confirmation="manual",
            published_at=when,
        )
        session.add(row)
        session.flush()
        return row.id


def test_figures_typed_by_hand_are_stored_as_typed_or_refused_by_name(api: Api) -> None:
    pid = api.post(
        "/api/projects", {"title": "Rutina de grabación", "platforms": ["youtube"]}
    )["id"]
    pub = published(api, pid, "youtube", utcnow() - timedelta(days=2))
    metrics = f"/api/publications/{pub}/metrics"

    # One bad figure refuses the whole entry and names the field; the good ones are not saved without it.
    refused = api.post(
        metrics, {"values": {"views": 1200, "retention_pct": 140}}, expect=422
    )
    assert (
        refused["code"] == "invalid_values" and "Retención media" in refused["message"]
    )
    assert (
        api.post(metrics, {"values": {"views": -5}}, expect=422)["code"]
        == "invalid_values"
    )
    # 1.84 views is what «1.840» becomes when a thousands point is read as a decimal one: refused, not stored.
    assert (
        "Visualizaciones"
        in api.post(metrics, {"values": {"views": 1.84}}, expect=422)["message"]
    )
    assert (
        api.post(metrics, {"values": {"views": "muchas"}}, expect=422)["code"]
        == "invalid_values"
    )
    assert (
        api.post(metrics, {"values": {"visitas": 10}}, expect=422)["code"]
        == "invalid_values"
    )
    assert api.post(metrics, {"values": {}}, expect=422)["code"] == "empty"
    future = (utcnow() + timedelta(days=1)).isoformat()
    assert (
        api.post(metrics, {"values": {"views": 10}, "captured_at": future}, expect=422)[
            "code"
        ]
        == "bad_date"
    )
    assert api.get("/api/analytics")["stats"]["with_metrics"] == 0

    api.post(
        metrics,
        {"values": {"views": 1200, "retention_pct": 61.5, "likes": 90, "comments": ""}},
    )
    item = api.get("/api/analytics")["stats"]["publications"][0]
    assert item["values"] == {"views": 1200.0, "retention_pct": 61.5, "likes": 90.0}
    assert item["source"] == "manual" and item["missing"] == ["Comentarios"]


def test_a_figure_the_platform_never_reports_does_not_make_a_piece_incomplete(
    api: Api,
) -> None:
    pid = api.post(
        "/api/projects",
        {"title": "Rutina de grabación", "platforms": ["youtube", "instagram_reels"]},
    )["id"]
    when = utcnow() - timedelta(days=2)
    reel = published(api, pid, "instagram_reels", when, "Reel")
    video = published(api, pid, "youtube", when, "Vídeo")
    figures = {"views": 800, "likes": 40, "comments": 3}
    api.post(f"/api/publications/{reel}/metrics", {"values": figures})
    api.post(f"/api/publications/{video}/metrics", {"values": figures})

    stats = api.get("/api/analytics")["stats"]
    missing = {item["title"]: item["missing"] for item in stats["publications"]}
    # Instagram has no average retention to give; YouTube does, so there it is a gap.
    assert missing == {"Reel": [], "Vídeo": ["Retención media"]}
    assert stats["incomplete"] == 1

    drain()
    gaps = api.get("/api/analytics")["reports"][0]["data_gaps"]
    assert (
        "1 de 2 piezas tiene datos incompletos (falta algún indicador básico)." in gaps
    )
    assert (
        "2 piezas usan cifras añadidas a mano: no están verificadas con la plataforma."
        in gaps
    )


def test_hours_and_dates_follow_the_time_zone_of_the_workspace(
    api: Api, other_browser: Callable[[], Api]
) -> None:
    # A workspace that never declared a zone works in UTC and says so.
    assert api.session["workspace"]["timezone"] == "UTC"
    pid = api.post(
        "/api/projects",
        {"title": "Rutina de grabación", "platforms": ["instagram_reels"]},
    )["id"]
    # 17:30 UTC in July is 19:30 in Madrid: evening for the audience, afternoon on the server's clock.
    pub = published(
        api, pid, "instagram_reels", datetime(2026, 7, 14, 17, 30, tzinfo=UTC)
    )
    api.post(
        f"/api/publications/{pub}/metrics",
        {"values": {"views": 500, "likes": 20, "comments": 1}},
    )

    def hours() -> tuple[str, list[str]]:
        comparison = next(
            c
            for c in api.get("/api/analytics")["stats"]["comparisons"]
            if c["dimension"] == "hour_bucket"
        )
        return comparison["label"], [g["name"] for g in comparison["groups"]]

    assert hours() == ("Hora de publicación (UTC)", ["Tarde (12-18 h)"])

    assert (
        api.patch("/api/workspace", {"timezone": "Marte/Olimpo"}, expect=422)["code"]
        == "invalid_timezone"
    )
    changed = api.patch("/api/workspace", {"timezone": "Europe/Madrid"})
    assert changed == {
        **changed,
        "timezone": "Europe/Madrid",
        "timezone_label": "hora de Madrid",
        "name": "Canal de Ana",
    }
    assert hours() == ("Hora de publicación (hora de Madrid)", ["Noche (18-24 h)"])
    assert api.get("/api/session")["workspace"]["timezone"] == "Europe/Madrid"

    # Dates written into sentences use that zone, with its summer time, and name it.
    with session_scope() as session:
        settings = session.get(Workspace, api.session["workspace"]["id"]).settings
    assert (
        clock.stamp(datetime(2031, 1, 15, 9, 0, tzinfo=UTC), settings)
        == "15/01/2031 a las 10:00 (hora de Madrid)"
    )
    assert (
        clock.stamp(datetime(2031, 7, 15, 9, 0, tzinfo=UTC), settings)
        == "15/07/2031 a las 11:00 (hora de Madrid)"
    )
    assert (
        clock.stamp(datetime(2031, 1, 15, 9, 0, tzinfo=UTC), None)
        == "15/01/2031 a las 09:00 (UTC)"
    )

    # Only the owner changes it, and a new workspace starts with the zone of the browser that created it.
    invite = api.post("/api/workspace/invites", {"role": "editor"})["token"]
    editor = other_browser().register("Bea", "bea@example.com", invite=invite)
    assert (
        editor.patch("/api/workspace", {"timezone": "UTC"}, expect=403)["code"]
        == "owner_only"
    )
    lima = other_browser().register(
        "Carla",
        "carla@example.com",
        workspace="Canal de Carla",
        timezone="America/Lima",
    )
    assert lima.session["workspace"]["timezone_label"] == "hora de Lima"
    unknown = other_browser().register(
        "Dani", "dani@example.com", workspace="Canal de Dani", timezone="No/Existe"
    )
    assert unknown.session["workspace"]["timezone"] == "UTC"


def test_figures_are_written_the_way_the_interface_shows_them() -> None:
    assert [number(v) for v in (1840, 18400, 8.8, 12345.6, 7.0, 99.96)] == [
        "1840",
        "18.400",
        "8,8",
        "12.345,6",
        "7",
        "100",
    ]
