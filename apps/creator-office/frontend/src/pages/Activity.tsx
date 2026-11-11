// Audit log, job history and measured usage.

import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { PageHead, useOffice } from "../App";
import { api, query } from "../api";
import type { ActivityEvent, EngineInfo, Run } from "../api";
import { formatCost, formatDateTime, formatDuration, formatNumber } from "../format";
import { useResource, useSession, useToast } from "../live";
import { Button, Empty, ErrorBox, Field, Loading, Notice, Tabs } from "../ui";
import { EventLine, RunStatusBadge, usageSummary, useAgents, useNow } from "./shared";

type Tab = "registro" | "tareas" | "consumo";

export function ActivityPage() {
  const [tab, setTab] = useState<Tab>("registro");
  return (
    <>
      <PageHead title="Actividad">Quién hizo qué y cuándo: personas, agentes, el orquestador y las plataformas. El registro no se puede editar.</PageHead>
      <Tabs
        label="Secciones de actividad"
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "registro", label: "Registro" },
          { id: "tareas", label: "Tareas de los agentes" },
          { id: "consumo", label: "Consumo" },
        ]}
      />
      {tab === "registro" ? <AuditLog /> : tab === "tareas" ? <Runs /> : <Usage />}
    </>
  );
}

const ACTOR_TYPES: [string, string][] = [
  ["", "Todos"],
  ["user", "Personas"],
  ["agent", "Agentes"],
  ["orchestrator", "Orquestador"],
  ["platform", "Plataformas"],
  ["system", "Sistema"],
];

function AuditLog() {
  const session = useSession();
  const office = useOffice();
  const toast = useToast();
  const now = useNow();
  const [params, setParams] = useSearchParams();
  const [text, setText] = useState(params.get("q") ?? "");
  const q = params.get("q") ?? "";
  const projectId = params.get("proyecto") ?? "";
  const [actorType, setActorType] = useState("");
  const [agent, setAgent] = useState("");
  const [technical, setTechnical] = useState(false);
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [fresh, setFresh] = useState(0);

  const fetchPage = (before: number) =>
    api.get<{ events: ActivityEvent[]; next_before: number | null }>(`/api/activity${query({ q, project_id: projectId, actor_type: agent ? "" : actorType, agent, technical, before, limit: 100 })}`);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchPage(0)
      .then((page) => {
        if (cancelled) return;
        setEvents(page.events);
        setNext(page.next_before);
        setError(null);
      })
      .catch((caught: Error) => !cancelled && setError(caught))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, projectId, actorType, agent, technical, session.workspace.id, fresh]);

  const more = async () => {
    if (!next) return;
    try {
      const page = await fetchPage(next);
      setEvents((current) => [...current, ...page.events]);
      setNext(page.next_before);
    } catch (caught) {
      toast.error(caught);
    }
  };
  const update = (changes: Record<string, string>) => {
    const merged: Record<string, string> = { q, proyecto: projectId, ...changes };
    setParams(Object.fromEntries(Object.entries(merged).filter(([, value]) => value)), { replace: true });
  };

  return (
    <div className="stack">
      <form
        className="filters"
        onSubmit={(event) => {
          event.preventDefault();
          update({ q: text.trim() });
        }}
      >
        <Field label="Buscar en el registro">
          <input type="search" value={text} placeholder="Texto del mensaje" onChange={(event) => setText(event.target.value)} />
        </Field>
        <Field label="Proyecto">
          <select value={projectId} onChange={(event) => update({ proyecto: event.target.value })}>
            <option value="">Todos</option>
            {(office.data?.projects_list ?? []).map((project) => (
              <option key={project.id} value={project.id}>
                {project.title}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Quién">
          <select
            value={agent ? `agent:${agent}` : actorType}
            onChange={(event) => {
              const value = event.target.value;
              if (value.startsWith("agent:")) {
                setAgent(value.slice(6));
                setActorType("");
              } else {
                setAgent("");
                setActorType(value);
              }
            }}
          >
            {ACTOR_TYPES.map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
            {session.catalog.agents.map((item) => (
              <option key={item.key} value={`agent:${item.key}`}>
                Solo {item.name}
              </option>
            ))}
          </select>
        </Field>
        <Button type="submit" icon="search">
          Buscar
        </Button>
        <Button icon="retry" onClick={() => setFresh(fresh + 1)}>
          Actualizar
        </Button>
        <label className="check-row" style={{ alignItems: "center", minHeight: 38 }}>
          <input type="checkbox" checked={technical} onChange={(event) => setTechnical(event.target.checked)} />
          <span>Incluir eventos técnicos</span>
        </label>
      </form>
      {error ? <ErrorBox error={error} onRetry={() => setFresh(fresh + 1)} /> : null}
      {loading && events.length === 0 ? <Loading /> : null}
      {!loading && !error && events.length === 0 ? (
        <Empty icon="list" title="Nada en el registro">
          {q || projectId || actorType || agent ? "Ningún evento coincide con los filtros." : "Cada acción de una persona, un agente o una plataforma quedará registrada aquí."}
        </Empty>
      ) : null}
      {events.length ? (
        <div className="panel">
          <ul className="feed">
            {events.map((event) => (
              <EventLine key={event.id} event={event} now={now} absolute />
            ))}
          </ul>
        </div>
      ) : null}
      {next ? (
        <div>
          <Button onClick={() => void more()}>Cargar más antiguos</Button>
        </div>
      ) : null}
    </div>
  );
}

const RUN_KIND: Record<string, string> = { step: "Paso del proyecto", render: "Exportación", design: "Diseño", publish: "Publicación", sync_comments: "Sincronizar comentarios", sync_metrics: "Sincronizar métricas" };

function Runs() {
  const session = useSession();
  const agents = useAgents();
  const [status, setStatus] = useState("");
  const runs = useResource<{ runs: Run[] }>(() => api.get(`/api/runs${query({ status, limit: 150 })}`), [status, session.workspace.id], (message) => message.kind === "event" && message.type.startsWith("run."));
  return (
    <div className="stack">
      <div className="filters">
        <Field label="Estado">
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">Todas</option>
            <option value="active">En cola o en marcha</option>
            <option value="done">Terminadas</option>
            <option value="error">Con error</option>
            <option value="cancelled">Canceladas</option>
          </select>
        </Field>
      </div>
      {runs.error && !runs.data ? <ErrorBox error={runs.error} onRetry={() => void runs.reload()} /> : null}
      {runs.loading && !runs.data ? <Loading /> : null}
      {runs.data && runs.data.runs.length === 0 ? (
        <Empty icon="list" title="Sin tareas">
          Aquí aparece cada trabajo que ejecuta un agente, con su duración real y el motor que usó.
        </Empty>
      ) : null}
      {runs.data && runs.data.runs.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="col-main">Tarea</th>
                <th>Agente</th>
                <th>Estado</th>
                <th>Inicio</th>
                <th className="num">Duración</th>
                <th>Motor y consumo</th>
              </tr>
            </thead>
            <tbody>
              {runs.data.runs.map((run) => (
                <tr key={run.id}>
                  <td className="col-main">
                    <div>{run.title}</div>
                    <span className="small muted">
                      {RUN_KIND[run.kind] ?? run.kind}
                      {run.project_id && run.project ? (
                        <>
                          {" · "}
                          <Link to={`/proyectos/${run.project_id}`}>{run.project}</Link>
                        </>
                      ) : null}
                    </span>
                    {run.error ? <div className="small" style={{ color: "#ff9db0" }}>{run.error}</div> : null}
                  </td>
                  <td>{agents[run.agent]?.name ?? "Orquestador"}</td>
                  <td>
                    <RunStatusBadge status={run.status} />
                  </td>
                  <td className="nowrap">{formatDateTime(run.started_at ?? run.created_at)}</td>
                  <td className="num nowrap">{run.seconds !== null ? formatDuration(run.seconds) : "—"}</td>
                  <td className="small">{["done", "error", "cancelled"].includes(run.status) ? usageSummary(run) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

interface UsagePayload {
  agents: {
    agent: string;
    name: string;
    runs: number;
    errors: number;
    seconds: number;
    llm_calls: number;
    total_tokens: number;
    cost_usd: number | null;
    provider_runs: number;
    demo_runs: number;
    tool_runs: number;
  }[];
  totals: { runs: number; seconds: number; total_tokens: number; cost_usd: number | null };
  engine: EngineInfo;
  notes: string[];
}

function Usage() {
  const session = useSession();
  const usage = useResource<UsagePayload>(() => api.get("/api/usage"), [session.workspace.id]);
  const data = usage.data;
  return (
    <div className="stack">
      {usage.error && !data ? <ErrorBox error={usage.error} onRetry={() => void usage.reload()} /> : null}
      {usage.loading && !data ? <Loading /> : null}
      {data ? (
        <>
          <div className="stats">
            <div className="stat">
              <strong>{data.totals.runs}</strong>
              <span>tareas terminadas</span>
            </div>
            <div className="stat">
              <strong>{formatDuration(data.totals.seconds)}</strong>
              <span>de trabajo medido</span>
            </div>
            <div className="stat">
              <strong>{formatNumber(data.totals.total_tokens)}</strong>
              <span>tokens informados por el proveedor</span>
            </div>
            <div className="stat">
              <strong>{data.totals.cost_usd === null ? "—" : formatCost(data.totals.cost_usd)}</strong>
              <span>{data.totals.cost_usd === null ? "coste sin estimar" : "coste estimado (USD)"}</span>
            </div>
          </div>
          {data.engine.mode === "demo" ? (
            <Notice tone="info">El motor local de demostración no consume tokens ni tiene coste de IA. Con un proveedor conectado verás aquí los tokens que informe y el coste estimado con los precios que configures.</Notice>
          ) : null}
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Agente</th>
                  <th className="num">Tareas</th>
                  <th className="num">Errores</th>
                  <th className="num">Tiempo</th>
                  <th className="num">Con IA</th>
                  <th className="num">Demostración</th>
                  <th className="num">Herramientas</th>
                  <th className="num">Tokens</th>
                  <th className="num">Coste estimado</th>
                </tr>
              </thead>
              <tbody>
                {data.agents.map((row) => (
                  <tr key={row.agent}>
                    <td>{row.name}</td>
                    <td className="num">{row.runs}</td>
                    <td className="num">{row.errors}</td>
                    <td className="num">{row.runs ? formatDuration(row.seconds) : "—"}</td>
                    <td className="num">{row.provider_runs}</td>
                    <td className="num">{row.demo_runs}</td>
                    <td className="num">{row.tool_runs}</td>
                    <td className="num">{row.provider_runs ? formatNumber(row.total_tokens) : "—"}</td>
                    <td className="num">{row.cost_usd === null ? "—" : formatCost(row.cost_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <ul className="bullets small muted">
            {data.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}
