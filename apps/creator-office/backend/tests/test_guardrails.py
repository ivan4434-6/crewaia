"""What a model says is checked against what the tools really returned."""

from __future__ import annotations

import json
import socket
from typing import Any

import httpx
import pytest
from crewai import BaseLLM

from creator_office.agents import context, crew, steps
from creator_office.agents.llm import EngineInfo
from creator_office.media import sources
from tests.helpers import Api, drain

ARTICLE = """<!doctype html><html lang="es"><head><title>Estudio sobre vídeos cortos</title>
<meta name="description" content="El 62 % de los encuestados descubre canales nuevos a través de vídeos cortos.">
<meta property="article:published_time" content="2026-09-14T08:00:00Z"></head>
<body><article><p>El 62 % de los encuestados descubre canales nuevos a través de vídeos cortos.</p></article></body></html>"""

UNDATED = "<html><head><title>Página sin fecha</title></head><body><p>Los directos semanales fidelizan a la audiencia.</p></body></html>"


class HallucinatingLLM(BaseLLM):
    """Stands in for a real provider that calls the tool properly and then makes one source up."""

    def __init__(self) -> None:
        super().__init__(model="test-model", temperature=0)

    def supports_function_calling(self) -> bool:
        return False

    def get_input_tokens_count(
        self, messages: Any, tools: Any = None, response_model: Any = None
    ) -> int:
        return 0

    def call(
        self,
        messages: Any,
        tools: Any = None,
        callbacks: Any = None,
        available_functions: Any = None,
        **kwargs: Any,
    ) -> str:
        ctx = context.get(context.run_id_from_task_name(kwargs["from_task"].name) or "")
        assert ctx is not None
        if "fetch_sources" not in ctx.tool_calls:
            return "Thought: I must read the sources.\nAction: fetch_sources\nAction Input: {}"
        answer = {
            "angles": [
                {
                    "title": "Los cortos como puerta de entrada",
                    "rationale": "Lo respalda el estudio.",
                    "source_urls": [
                        "https://datos.example.org/estudio",
                        "https://inventado.example.com/informe",
                    ],
                },
            ],
            "findings": [
                {
                    "claim": "El 62 % descubre canales por vídeos cortos.",
                    "source_title": "Estudio",
                    "url": "https://datos.example.org/estudio",
                    "published": "2026-09-14",
                    "confidence": "alta",
                    "confidence_reason": "Fuente primaria reciente.",
                },
                {
                    "claim": "El 90 % de los creadores triunfa con IA.",
                    "source_title": "Informe 2026",
                    "url": "https://inventado.example.com/informe",
                    "published": "2026-01-01",
                    "confidence": "alta",
                    "confidence_reason": "Muy citado.",
                },
                {
                    "claim": "Los directos semanales fidelizan.",
                    "source_title": "Página",
                    "url": "https://blog.example.org/directos",
                    "published": None,
                    "confidence": "alta",
                    "confidence_reason": "Lo afirma la página.",
                },
            ],
            "keywords": ["vídeos cortos"],
            "unverified": [],
            "notes": "",
        }
        return "Thought: I now know the final answer\nFinal Answer: " + json.dumps(
            answer, ensure_ascii=False
        )


def test_findings_without_a_source_that_was_really_read_are_dropped(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    def site(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        pages = {"datos.example.org": ARTICLE, "blog.example.org": UNDATED}
        if request.url.host in pages:
            return httpx.Response(
                200,
                text=pages[request.url.host],
                headers={"content-type": "text/html; charset=utf-8"},
            )
        return httpx.Response(503)

    monkeypatch.setattr(
        sources, "new_client", lambda: httpx.Client(transport=httpx.MockTransport(site))
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    engine_info = EngineInfo(
        mode="provider",
        provider="openai",
        model="openai/test-model",
        source="workspace",
    )
    monkeypatch.setattr(
        crew,
        "build_llm",
        lambda session, workspace_id: (HallucinatingLLM(), engine_info),
    )

    pid = api.post(
        "/api/projects",
        {
            "title": "Vídeos cortos para crecer",
            "platforms": ["youtube"],
            "references": [
                {"url": "https://datos.example.org/estudio"},
                {"url": "blog.example.org/directos"},
                {"url": "https://caido.example.net/x"},
            ],
        },
    )["id"]
    drain()
    detail = api.get(f"/api/projects/{pid}")
    step = next(s for s in detail["steps"] if s["kind"] == "research")
    assert step["status"] == "awaiting_approval", step
    assert step["output"]["engine"]["model"] == "openai/test-model"
    content = detail["documents"]["research"]["version"]["content"]

    urls = [f["url"] for f in content["findings"]]
    assert urls == [
        "https://datos.example.org/estudio",
        "https://blog.example.org/directos",
    ]
    dated, undated = content["findings"]
    assert (
        dated["confidence"] == "alta"
        and dated["published"] == "2026-09-14"
        and dated["source_title"] == "Estudio sobre vídeos cortos"
    )
    assert (
        undated["confidence"] == "media" and undated["published"] is None
    )  # no date, so not "alta"
    assert content["angles"][0]["source_urls"] == ["https://datos.example.org/estudio"]
    notes = " ".join(content["unverified"])
    assert "inventado.example.com" in notes and "90 %" in notes
    assert "https://caido.example.net/x" in notes and "503" in notes
    assert content["sources"]["read"] == 2 and len(content["sources"]["failed"]) == 1

    # The run records that a provider was used and that it reported no token usage.
    run = next(r for r in api.get("/api/runs")["runs"] if r["agent"] == "scout")
    assert run["usage"]["engine"] == "provider" and run["usage"]["cost_usd"] is None
    assert "no informó" in run["usage"]["cost_basis"]


def test_private_addresses_are_never_fetched() -> None:
    for url in (
        "http://127.0.0.1:8000/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "file:///etc/passwd",
        "http://10.0.0.5/",
    ):
        result = sources.fetch_source(url)
        assert result.ok is False and result.items == [], url


def test_robots_txt_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    def site(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /privado/")
        return httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"})

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    with httpx.Client(transport=httpx.MockTransport(site)) as client:
        blocked = sources.fetch_source(
            "https://datos.example.org/privado/informe", client
        )
        allowed = sources.fetch_source("https://datos.example.org/publico", client)
    assert blocked.ok is False and "robots.txt" in blocked.error
    assert allowed.ok and allowed.items[0].published == "2026-09-14"


def test_analyst_figures_must_come_from_the_computed_statistics() -> None:
    stats = {
        "totals": {"views": 15230.0},
        "publications": [
            {"values": {"views": 1200.0, "retention_pct": 41.5}, "engagement_rate": 7.5}
        ],
    }
    assert (
        steps.unsupported_numbers(
            ["Las 15230 visualizaciones y el 41,5 % de retención"], stats
        )
        == []
    )
    assert steps.unsupported_numbers(
        ["Tu retención del 41.5 % supera la media del sector (35 %)"], stats
    ) == ["35"]
    assert steps.unsupported_numbers(["Publica 3 piezas por semana"], stats) == []
    # Thousands written the Spanish or the English way are the same figure; an invented one is still caught.
    assert (
        steps.unsupported_numbers(
            ["15.230 visualizaciones", "15,230 views", "1.200 en la mejor pieza"], stats
        )
        == []
    )
    assert steps.unsupported_numbers(
        ["Llegarás a 25.000 visualizaciones", "sube un 12,5 %"], stats
    ) == ["25.000", "12,5"]
