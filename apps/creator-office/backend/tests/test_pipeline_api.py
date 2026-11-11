"""End to end through the HTTP API with the built-in demo engine and real FFmpeg."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from creator_office.database import session_scope, utcnow
from creator_office.models import Event, Publication
from creator_office.pipeline import worker
from tests.helpers import SRT, Api, drain

PROJECT = {
    "title": "Cómo planifico una semana de vídeos",
    "topic": "planificación de contenido para creadores",
    "audience": "creadores que publican solos",
    "goal": "ganar suscriptores",
    "platforms": ["tiktok"],
    "template": "repurpose",
    "options": {"clip_count": 1, "clip_min_s": 4, "clip_max_s": 6},
}


def steps(detail: dict) -> dict[str, dict]:
    return {s["kind"]: s for s in detail["steps"]}


def upload(
    api: Api, project_id: str, kind: str, filename: str, data: bytes, **params: str
) -> dict:
    query = "&".join(
        f"{k}={v}" for k, v in {"kind": kind, "filename": filename, **params}.items()
    )
    return api.request(
        "POST", f"/api/projects/{project_id}/uploads?{query}", content=data
    )


def test_project_runs_from_idea_to_manual_publication(
    api: Api, sample_video: Path
) -> None:
    detail = api.post("/api/projects", PROJECT)
    pid = detail["id"]
    assert steps(detail)["research"]["status"] == "queued"
    assert steps(detail)["community"]["status"] == "pending"

    # --- Scout: nothing invented when there is no source to read.
    drain()
    detail = api.get(f"/api/projects/{pid}")
    research = steps(detail)["research"]
    assert research["status"] == "awaiting_approval"
    assert research["output"]["engine"]["mode"] == "demo"
    document = detail["documents"]["research"]["version"]
    assert document["content"]["findings"] == []
    assert any(
        "fuentes permitidas" in note for note in document["content"]["unverified"]
    )
    assert document["provider"] == "Motor local de demostración"

    # Approval needs a decision about the angle.
    assert api.step(pid, "research", "approve", expect=409)["code"] == "not_possible"
    angle = document["content"]["angles"][0]["title"]
    detail = api.step(pid, "research", "approve", chosen_angle=angle)
    assert steps(detail)["research"]["status"] == "completed"
    assert steps(detail)["script"]["status"] == "queued"
    clips = steps(detail)["clips"]
    assert clips["status"] == "blocked" and clips["blocked_code"] == "missing_video"

    # --- Writer: the demo engine leaves bracketed gaps instead of writing copy.
    drain()
    detail = api.get(f"/api/projects/{pid}")
    script = detail["documents"]["script"]["version"]["content"]
    assert steps(detail)["script"]["status"] == "awaiting_approval"
    assert (
        "[" in script["sections"][0]["text"] and "[" in script["sections"][0]["visual"]
    )
    # Every gap a person will find in the editor is counted: four in the hooks, what is said and
    # what is shown in each section, and the call to action. The note that explains the brackets is not one.
    assert (
        script["placeholders"] == 4 + 2 * len(script["sections"]) + 1
        and "[corchetes]" in script["notes"]
    )
    assert script["working_title"] == angle

    # A person edits the script: new version, authored by them.
    script["sections"][0]["text"] = "Hoy te cuento cómo planifico una semana de vídeos."
    script["sections"][0]["visual"] = "Calendario de la semana en pantalla"
    detail = api.post(
        f"/api/projects/{pid}/documents/script",
        {"content": script, "note": "Gancho escrito"},
    )
    version = detail["documents"]["script"]["version"]
    assert (
        version["number"] == 2
        and version["author_type"] == "user"
        and version["author"] == "Ana"
    )
    assert version["content"]["placeholders"] == script["placeholders"] - 2
    # The step's own line describes the version that is current now, not the one Writer left.
    assert (
        f"{script['placeholders'] - 2} marcadores por completar"
        in steps(detail)["script"]["summary"]
    )
    versions = api.get(f"/api/projects/{pid}/documents/script")["versions"]
    assert [v["number"] for v in versions] == [2, 1]

    detail = api.step(pid, "script", "approve")
    assert steps(detail)["design"]["status"] == "queued"
    drain()

    # --- Upload: rights must be declared before Clipper touches the video.
    data = sample_video.read_bytes()
    detail = upload(api, pid, "source", "semana.mp4", data)["project"]
    assert detail["source"]["media"]["duration"] > 23
    assert detail["source"]["poster_url"]
    assert steps(detail)["clips"]["blocked_code"] == "missing_rights"
    detail = api.patch(
        f"/api/assets/{detail['source']['id']}",
        {"rights_declared": True, "rights_note": "Grabación propia"},
    )["project"]
    assert steps(detail)["clips"]["status"] == "queued"
    upload(api, pid, "subtitles", "semana.srt", SRT.encode())

    # --- Clipper: real timestamps from measured signals, with the transcript excerpt.
    drain()
    detail = api.get(f"/api/projects/{pid}")
    assert steps(detail)["clips"]["status"] == "awaiting_approval"
    assert steps(detail)["clips"]["output"]["transcript"]["available"] is True
    picked = [c for c in detail["clips"] if c["status"] == "selected"]
    assert len(picked) == 1
    clip = picked[0]
    assert 0 <= clip["start"] < clip["end"] <= detail["source"]["media"]["duration"]
    assert 3.5 <= clip["duration"] <= 6.5
    assert clip["excerpt"]
    assert clip["signals"]["picked"] is True

    detail = api.step(pid, "clips", "approve")
    assert steps(detail)["edit"]["status"] == "queued"

    # --- Editor: a real export with measured checks.
    drain()
    detail = api.get(f"/api/projects/{pid}")
    assert steps(detail)["edit"]["status"] == "awaiting_approval"
    (render,) = detail["renders"]
    assert render["status"] == "ready" and render["format"] == "vertical"
    assert (render["media"]["video"]["width"], render["media"]["video"]["height"]) == (
        1080,
        1920,
    )
    assert abs(render["media"]["duration"] - clip["duration"]) < 0.6
    assert render["settings"]["subtitles_burned"] is True
    assert all(c["status"] != "fail" for c in render["checks"])
    exported = api.get(render["url"], headers={"Range": "bytes=0-99"}, expect=206)
    assert (
        exported.headers["content-range"].startswith("bytes 0-99/")
        and len(exported.content) == 100
    )

    # Designs were composed while the video was missing: gradient background, checks measured.
    designs = {d["kind"]: d for d in detail["designs"]}
    assert set(designs) == {"cover"}
    assert designs["cover"]["status"] == "ready" and designs["cover"]["url"]
    assert {c["id"] for c in designs["cover"]["checks"]} >= {
        "contrast",
        "legibility",
        "safe_zone",
    }

    api.step(pid, "edit", "approve")
    detail = api.step(pid, "design", "approve")
    assert steps(detail)["preflight"]["status"] == "queued"

    # --- Pre-flight (rules) and Publisher (drafts only).
    drain()
    detail = api.get(f"/api/projects/{pid}")
    preflight = steps(detail)["preflight"]
    assert preflight["status"] == "completed"
    assert {c["area"] for c in preflight["output"]["checks"]} >= {
        "Formato",
        "Subtítulos",
        "Textos",
        "Metadatos",
        "Derechos",
        "Portadas",
        "Errores",
    }
    assert steps(detail)["publish"]["status"] == "awaiting_approval"
    (pub,) = detail["publications"]
    assert (
        pub["status"] == "draft"
        and pub["approved"] is False
        and pub["confirmation"] == ""
    )
    assert (
        pub["readiness"]["content_ready"] is True
        and pub["readiness"]["connected"] is False
    )
    assert "Pendiente de conexión" in pub["readiness"]["connection_label"]
    api.step(pid, "publish", "approve")

    # --- Nothing leaves without approval, confirmation and a connected account.
    action = f"/api/publications/{pub['id']}/actions"
    assert (
        api.post(action, {"action": "publish", "confirm": True}, expect=409)["code"]
        == "not_approved"
    )
    api.post(action, {"action": "approve"})
    assert (
        api.post(action, {"action": "publish"}, expect=422)["code"] == "not_confirmed"
    )
    refused = api.post(action, {"action": "publish", "confirm": True}, expect=409)
    assert (
        refused["code"] == "not_connected" and "TIKTOK_CLIENT_KEY" in refused["message"]
    )
    assert api.get(f"/api/projects/{pid}")["publications"][0]["status"] == "draft"

    # Manual path: export package, reminder at the scheduled time, then a human declaration.
    package = api.get(f"/api/publications/{pub['id']}/export")
    assert (
        package.headers["content-type"] == "application/zip"
        and len(package.content) > 10_000
    )
    when = (utcnow() + timedelta(hours=1)).isoformat()
    scheduled = api.post(
        action, {"action": "schedule", "scheduled_at": when, "mode": "reminder"}
    )["publication"]
    assert (
        scheduled["status"] == "scheduled" and scheduled["schedule_mode"] == "reminder"
    )
    logged = next(
        e["message"]
        for e in api.get("/api/activity")["events"]
        if e["type"] == "publication.scheduled"
    )
    assert "recordatorio para publicar a mano" in logged and logged.endswith(
        "(UTC)."
    )  # the date names its time zone
    with session_scope() as session:
        session.get(Publication, pub["id"]).scheduled_at = utcnow() - timedelta(
            minutes=1
        )
    worker.tick()
    reminders = [
        n
        for n in api.get("/api/notifications")["notifications"]
        if n["type"] == "reminder"
    ]
    assert len(reminders) == 1
    assert (
        api.get(f"/api/projects/{pid}")["publications"][0]["status"] == "scheduled"
    )  # a reminder is not a publication

    done = api.post(
        action,
        {"action": "mark_published", "url": "https://www.tiktok.com/@ana/video/1"},
    )["publication"]
    assert done["status"] == "published" and done["confirmation"] == "manual"
    assert "declarada a mano" in done["readiness"]["summary"]

    # --- Analyst wakes up only when metrics exist, and says where they came from.
    assert steps(api.get(f"/api/projects/{pid}"))["analysis"]["status"] == "pending"
    api.post(
        f"/api/publications/{pub['id']}/metrics",
        {"values": {"views": 1200, "likes": 90, "retention_pct": 41}},
    )
    drain()
    detail = api.get(f"/api/projects/{pid}")
    assert steps(detail)["analysis"]["status"] == "completed"
    overview = api.get("/api/analytics")
    assert overview["stats"]["sources"] == {"platform": 0, "manual": 1}
    assert overview["stats"]["totals"]["views"] == 1200
    assert all(not c["sufficient"] for c in overview["stats"]["comparisons"])
    assert overview["reports"][0]["data_gaps"]

    # --- Community: imported comments are triaged; replies wait for a person.
    api.post(
        f"/api/projects/{pid}/comments/import",
        {
            "text": "lucia: ¿Qué app usas para planificar?\nmarco: Gana dinero fácil en www.spam.xyz\nines: ¡Muy útil, gracias!",
            "platform": "tiktok",
        },
    )
    drain()
    detail = api.get(f"/api/projects/{pid}")
    assert steps(detail)["community"]["status"] == "awaiting_approval"
    comments = {
        c["author"]: c for c in api.get(f"/api/comments?project_id={pid}")["comments"]
    }
    assert (
        comments["lucia"]["category"] == "question"
        and comments["lucia"]["reply_status"] == "suggested"
    )
    assert (
        comments["marco"]["category"] == "spam"
        and comments["marco"]["reply_text"] == ""
    )
    assert comments["ines"]["category"] == "positive"
    assert comments["lucia"]["category_by"] == "rules"
    # A suggested reply with gaps cannot be approved as is; imported comments are answered by hand.
    reply = f"/api/comments/{comments['lucia']['id']}/reply"
    assert api.post(reply, {"action": "approve"}, expect=422)["code"] == "placeholders"
    api.patch(
        f"/api/comments/{comments['lucia']['id']}",
        {"reply_text": "¡Gracias! Uso una hoja de cálculo sencilla."},
    )
    api.post(reply, {"action": "approve"})
    assert api.post(reply, {"action": "send"}, expect=409)["code"] == "manual_only"
    assert (
        api.post(reply, {"action": "mark_sent"})["comment"]["reply_status"]
        == "sent_manual"
    )

    # --- The audit trail names who did what, in order.
    with session_scope() as session:
        events = list(
            session.scalars(
                select(Event).where(Event.project_id == pid).order_by(Event.id)
            )
        )
    types = [e.type for e in events]
    assert (
        types.index("step.started")
        < types.index("agent.tool_started")
        < types.index("agent.tool_finished")
        < types.index("step.awaiting_approval")
    )
    assert {
        "handoff",
        "step.approved",
        "publication.marked_published",
        "asset.rights",
        "document.edited",
    } <= set(types)
    assert "publication.published" not in types  # no platform ever confirmed anything
    approved = next(e for e in events if e.type == "step.approved")
    assert approved.actor_type == "user" and approved.actor == "Ana"
    usage = api.get("/api/usage")
    assert usage["totals"]["total_tokens"] == 0 and usage["totals"]["cost_usd"] is None
    assert next(a for a in usage["agents"] if a["agent"] == "editor")["seconds"] > 0
    # The pre-publication review belongs to no agent: it is counted as the orchestrator's work, not left out.
    review = next(a for a in usage["agents"] if a["agent"] == "orchestrator")
    assert review["runs"] == 1 and review["tool_runs"] == 1 and review["demo_runs"] == 0
    assert usage["totals"]["runs"] == sum(a["runs"] for a in usage["agents"])


def test_changing_a_clip_after_export_flags_editor_until_its_piece_is_redone(
    api: Api, sample_video: Path
) -> None:
    detail = api.post("/api/projects", PROJECT)
    pid = detail["id"]
    drain()
    angle = api.get(f"/api/projects/{pid}")["documents"]["research"]["version"][
        "content"
    ]["angles"][0]["title"]
    api.step(pid, "research", "approve", chosen_angle=angle)
    source = upload(api, pid, "source", "semana.mp4", sample_video.read_bytes())[
        "project"
    ]["source"]
    api.patch(f"/api/assets/{source['id']}", {"rights_declared": True})
    drain()
    api.step(pid, "clips", "approve")
    drain()
    detail = api.get(f"/api/projects/{pid}")
    (clip,) = [c for c in detail["clips"] if c["status"] == "selected"]
    (render,) = detail["renders"]
    assert (
        steps(detail)["edit"]["status"] == "awaiting_approval"
        and steps(detail)["edit"]["stale_reason"] == ""
    )
    # Without a transcript the clip is named after its span.
    assert clip["title"].startswith("Clip 0:")

    # Trimming: the piece is marked as outdated, Editor is flagged, and a name that restates the span follows it.
    start = clip["start"] + 1.0 if clip["start"] < 1 else clip["start"] - 1.0
    detail = api.patch(f"/api/clips/{clip['id']}", {"start": start})
    trimmed = next(c for c in detail["clips"] if c["id"] == clip["id"])
    assert trimmed["title"] != clip["title"] and trimmed["title"].startswith("Clip 0:")
    assert detail["renders"][0]["status"] == "stale"
    assert (
        steps(detail)["edit"]["stale_reason"]
        == "Se cambió el corte de un clip después de este paso."
    )

    # A name the person chose is theirs: another trim leaves it alone.
    api.patch(f"/api/clips/{clip['id']}", {"title": "El truco de grabar por bloques"})
    detail = api.patch(f"/api/clips/{clip['id']}", {"end": trimmed["end"] - 0.5})
    assert (
        next(c for c in detail["clips"] if c["id"] == clip["id"])["title"]
        == "El truco de grabar por bloques"
    )

    # Exporting it again by hand catches up: the flag goes away without relaunching the whole step.
    api.post(f"/api/clips/{clip['id']}/renders", {"format": render["format"]})
    drain()
    detail = api.get(f"/api/projects/{pid}")
    assert detail["renders"][0]["status"] == "ready"
    assert steps(detail)["edit"]["stale_reason"] == ""

    # A clip added afterwards has no piece yet, and Editor says so.
    detail = api.post(
        f"/api/projects/{pid}/clips", {"start": 16.5, "end": 21.0, "title": ""}
    )
    assert (
        steps(detail)["edit"]["stale_reason"]
        == "Se añadió un clip después de este paso."
    )
    assert (
        api.post(f"/api/projects/{pid}/clips", {"start": 30, "end": 40}, expect=422)[
            "code"
        ]
        == "bad_range"
    )


def test_office_reports_only_real_states(api: Api) -> None:
    office = api.get("/api/office")
    assert {a["status"] for a in office["agents"]} == {"idle"}
    assert office["events"] == []

    pid = api.post("/api/projects", {**PROJECT, "platforms": ["youtube"]})["id"]
    scout = next(a for a in api.get("/api/office")["agents"] if a["key"] == "scout")
    assert scout["status"] == "queued" and scout["queue"][0]["title"].startswith(
        "Investigación"
    )

    drain()
    office = api.get("/api/office")
    scout = next(a for a in office["agents"] if a["key"] == "scout")
    assert (
        scout["status"] == "awaiting_approval"
        and scout["awaiting_approval"][0]["project_id"] == pid
    )
    assert (
        scout["last_run"]["status"] == "done"
        and scout["last_run"]["seconds"] is not None
    )
    assert all(a["status"] == "idle" for a in office["agents"] if a["key"] != "scout")


def test_the_bell_counts_only_decisions_that_are_still_pending(api: Api) -> None:
    def unread() -> list[str]:
        return [
            n["title"]
            for n in api.get("/api/notifications")["notifications"]
            if not n["read"]
        ]

    pid = api.post("/api/projects", {**PROJECT, "platforms": ["youtube"]})["id"]
    drain()
    assert unread() == ["Scout espera tu aprobación"]
    assert api.get("/api/office")["unread_notifications"] == 1

    # Asking for changes answers the notice; the new result brings a new one.
    api.step(pid, "research", "request_changes", feedback="Más concreto")
    assert unread() == []
    drain()
    assert unread() == ["Scout espera tu aprobación"]

    angle = api.get(f"/api/projects/{pid}")["documents"]["research"]["version"][
        "content"
    ]["angles"][0]["title"]
    api.step(pid, "research", "approve", chosen_angle=angle)
    assert unread() == []
    drain()
    assert unread() == ["Writer espera tu aprobación"]

    # A deleted project takes its notices with it: they would lead nowhere.
    api.delete(f"/api/projects/{pid}")
    assert api.get("/api/notifications")["notifications"] == []


def test_pause_resume_retry_and_change_requests(api: Api) -> None:
    pid = api.post("/api/projects", {**PROJECT, "platforms": ["youtube"]})["id"]
    # Paused before it ran: the queued job is cancelled and nothing executes.
    detail = api.post(f"/api/projects/{pid}/state", {"action": "pause"})
    assert (
        detail["state"] == "paused" and steps(detail)["research"]["status"] == "paused"
    )
    assert drain() == []
    detail = api.post(f"/api/projects/{pid}/state", {"action": "resume"})
    assert steps(detail)["research"]["status"] == "queued"
    drain()

    # Asking for changes re-runs the agent with the feedback and keeps the old version.
    api.step(pid, "research", "request_changes", expect=409)
    detail = api.step(
        pid, "research", "request_changes", feedback="Céntrate en creadores de cocina"
    )
    assert steps(detail)["research"]["status"] == "queued"
    drain()
    detail = api.get(f"/api/projects/{pid}")
    assert steps(detail)["research"]["attempts"] == 2
    assert detail["documents"]["research"]["versions"] == 2
    versions = api.get(f"/api/projects/{pid}/documents/research")["versions"]
    assert versions[0]["note"] == "Céntrate en creadores de cocina"

    # Restoring brings back the earlier content as a new version.
    detail = api.post(
        f"/api/projects/{pid}/documents/research/restore",
        {"version_id": versions[1]["id"]},
    )
    assert detail["documents"]["research"]["version"]["number"] == 3

    # Skipping a step is recorded and lets the next one start.
    detail = api.step(pid, "research", "skip")
    assert (
        steps(detail)["research"]["status"] == "skipped"
        and steps(detail)["script"]["status"] == "queued"
    )

    detail = api.post(f"/api/projects/{pid}/state", {"action": "cancel"})
    assert (
        detail["state"] == "cancelled"
        and steps(detail)["script"]["status"] == "cancelled"
    )
    assert api.step(pid, "script", "rerun", expect=409)["code"] == "not_possible"
    api.delete(f"/api/projects/{pid}")
    api.get(f"/api/projects/{pid}", expect=404)
