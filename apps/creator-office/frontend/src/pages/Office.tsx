// The office: eight desks whose state is exactly what the server reports.

import { useEffect, useRef, useState } from "react";
import type { CSSProperties, FormEvent } from "react";
import { Link } from "react-router-dom";
import { PageHead, useOffice } from "../App";
import { api } from "../api";
import type { OfficeAgent, OfficeState, Run, StepBrief } from "../api";
import { formatDuration, plural, timeSince } from "../format";
import { useAction, useLiveMessages, useResource, useSession } from "../live";
import { DeskScene, PaperSprite, Plant, WaterCooler } from "../sprites";
import { Badge, Button, Empty, ErrorBox, Icon, Loading, Notice, Panel, StepBadge } from "../ui";
import { EventLine, Pips, RunProgress, RunStatusBadge, usageSummary, useNow } from "./shared";

export function OfficePage() {
  const office = useOffice();
  const session = useSession();
  const [selected, setSelected] = useState<string | null>(null);
  const data = office.data;

  return (
    <>
      <PageHead
        title="Oficina"
        actions={
          session.canEdit ? (
            <Link className="btn btn-primary" to="/proyectos/nuevo">
              <Icon name="plus" size={14} />
              <span>Nuevo proyecto</span>
            </Link>
          ) : null
        }
      >
        Cada mesa muestra lo que ese agente está haciendo de verdad ahora mismo. Pulsa una mesa para ver su trabajo, su cola y lo que espera tu decisión.
      </PageHead>
      {office.error && !data ? <ErrorBox error={office.error} onRetry={() => void office.reload()} /> : null}
      {!data && office.loading ? <Loading label="Entrando en la oficina…" /> : null}
      {data ? (
        <>
          <CapabilityNotices office={data} />
          <div className="office-layout">
            <div className="stack stack-lg">
              <Room office={data} selected={selected} onSelect={(key) => setSelected(selected === key ? null : key)} />
              {selected ? (
                <AgentDetail agent={data.agents.find((agent) => agent.key === selected) as OfficeAgent} onClose={() => setSelected(null)} />
              ) : data.projects.total === 0 ? (
                <Empty
                  icon="sparkle"
                  title="La oficina está lista y el equipo, libre"
                  action={
                    session.canEdit ? (
                      <Link className="btn btn-primary" to="/proyectos/nuevo">
                        <span>Crear el primer proyecto</span>
                      </Link>
                    ) : null
                  }
                >
                  Crea un proyecto con una idea o un vídeo y Scout empezará a investigar. Verás cada entrega entre agentes aquí, en directo.
                </Empty>
              ) : (
                <ProjectsStrip office={data} />
              )}
            </div>
            <div className="stack stack-lg">
              <Attention office={data} />
              <Feed office={data} />
              <OrderConsole />
            </div>
          </div>
        </>
      ) : null}
    </>
  );
}

function CapabilityNotices({ office }: { office: OfficeState }) {
  const { engine, ffmpeg } = office.capabilities;
  return (
    <>
      {engine.mode === "demo" ? (
        <Notice tone="warn" title="Modo demostración: no hay ningún proveedor de IA conectado." actions={<Link to="/integraciones">Conectar un proveedor de IA</Link>}>
          Los agentes ejecutan sus herramientas reales (leer tus fuentes, analizar y exportar vídeo, componer miniaturas, validar requisitos), pero no redactan ni valoran contenido: donde haría falta un modelo dejan
          marcadores entre [corchetes] para que los completes tú.
        </Notice>
      ) : null}
      {!ffmpeg.available ? (
        <Notice tone="warn" title="FFmpeg no está disponible en el servidor.">
          {ffmpeg.detail}
        </Notice>
      ) : null}
    </>
  );
}

// --- The room ------------------------------------------------------------------

interface Flight {
  id: number;
  fromX: number;
  fromY: number;
  toX: number;
  toY: number;
}

function Room({ office, selected, onSelect }: { office: OfficeState; selected: string | null; onSelect: (key: string) => void }) {
  const floor = useRef<HTMLDivElement>(null);
  const desks = useRef<Record<string, HTMLButtonElement | null>>({});
  const [flights, setFlights] = useState<Flight[]>([]);
  const counter = useRef(0);

  // A sheet of paper crosses the room only when the server records a real handoff between two agents.
  useLiveMessages((message) => {
    if (message.kind !== "event" || message.type !== "handoff") return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const from = desks.current[String(message.data.from)];
    const to = desks.current[String(message.data.to)];
    const room = floor.current;
    if (!from || !to || !room) return;
    const base = room.getBoundingClientRect();
    const a = from.getBoundingClientRect();
    const b = to.getBoundingClientRect();
    const id = ++counter.current;
    const flight = {
      id,
      fromX: a.left - base.left + a.width / 2 - 15,
      fromY: a.top - base.top + a.height * 0.28,
      toX: b.left - base.left + b.width / 2 - 15,
      toY: b.top - base.top + b.height * 0.28,
    };
    setFlights((current) => [...current, flight]);
    window.setTimeout(() => setFlights((current) => current.filter((item) => item.id !== id)), 1600);
  });

  const working = office.agents.filter((agent) => agent.status === "working").length;
  const waiting = office.agents.reduce((total, agent) => total + agent.awaiting_approval.length, 0);
  return (
    <section className="room" aria-label="Oficina de agentes">
      <div className="room-wall">
        <div className="room-window" aria-hidden="true" />
        <div className="room-board" role="status" aria-label="Resumen de la oficina">
          <span>
            <strong>{office.projects.active}</strong>
            {office.projects.active === 1 ? "proyecto activo" : "proyectos activos"}
          </span>
          <span>
            <strong>{working}</strong>
            {working === 1 ? "agente trabajando" : "agentes trabajando"}
          </span>
          <span>
            <strong>{waiting}</strong>
            {waiting === 1 ? "espera aprobación" : "esperan aprobación"}
          </span>
        </div>
        <WaterCooler className="room-decor" />
        <Plant className="room-decor" />
        <div className="room-window" aria-hidden="true" />
      </div>
      <div className="room-floor" ref={floor}>
        {office.agents.map((agent) => (
          <Desk
            key={agent.key}
            agent={agent}
            selected={selected === agent.key}
            onSelect={() => onSelect(agent.key)}
            register={(node) => {
              desks.current[agent.key] = node;
            }}
          />
        ))}
        {flights.map((flight) => (
          <div
            key={flight.id}
            className="handoff-paper"
            style={{ "--from-x": `${flight.fromX}px`, "--from-y": `${flight.fromY}px`, "--to-x": `${flight.toX}px`, "--to-y": `${flight.toY}px` } as CSSProperties}
            aria-hidden="true"
          >
            <PaperSprite />
          </div>
        ))}
      </div>
    </section>
  );
}

function deskLine(agent: OfficeAgent): string {
  if (agent.status === "working") return agent.current[0]?.title ?? "";
  if (agent.status === "error") return agent.errors[0]?.reason || "Un paso terminó con error.";
  if (agent.status === "awaiting_approval") {
    const first = agent.awaiting_approval[0];
    return agent.awaiting_approval.length > 1 ? `${agent.awaiting_approval.length} resultados por revisar` : `Por revisar: ${first.title} · ${first.project}`;
  }
  if (agent.status === "blocked") return agent.blocked[0]?.reason || "No puede empezar.";
  if (agent.status === "queued") return `En cola: ${agent.queue[0]?.title ?? ""}`;
  return agent.specialty;
}

function Desk({ agent, selected, onSelect, register }: { agent: OfficeAgent; selected: boolean; onSelect: () => void; register: (node: HTMLButtonElement | null) => void }) {
  const line = deskLine(agent);
  const run = agent.current[0];
  return (
    <button type="button" className="desk" aria-pressed={selected} onClick={onSelect} ref={register} aria-label={`${agent.name}: ${agent.status_label}. ${line}`}>
      <DeskScene agent={agent.key} color={agent.color} status={agent.status} title={`${agent.name}, ${agent.status_label}`} />
      <span className="desk-plate">
        <span className="desk-name">
          {agent.name}
          <i style={{ background: agent.color }} aria-hidden="true" />
        </span>
        <span className="desk-status">{agent.status === "idle" ? <Badge>Libre</Badge> : <StepBadge status={agent.status} label={agent.status_label} />}</span>
        {run ? <RunProgress run={run} fallbackLabel={run.title} /> : <span className="desk-task">{line}</span>}
      </span>
    </button>
  );
}

// --- Agent detail --------------------------------------------------------------

function BriefList({ items, action }: { items: StepBrief[]; action: string }) {
  return (
    <ul className="stack stack-sm">
      {items.map((item) => (
        <li key={`${item.project_id}-${item.kind}`} className="card">
          <div className="row row-between">
            <strong>
              {item.title} · {item.project}
            </strong>
            <Link className="btn btn-small" to={`/proyectos/${item.project_id}?paso=${item.kind}`}>
              <span>{action}</span>
            </Link>
          </div>
          {item.reason || item.summary ? <div className="small soft">{item.reason || item.summary}</div> : null}
        </li>
      ))}
    </ul>
  );
}

function AgentDetail({ agent, onClose }: { agent: OfficeAgent; onClose: () => void }) {
  const session = useSession();
  const office = useOffice();
  const { busy, run } = useAction();
  const now = useNow();
  const cancel = (job: Run) =>
    run(job.id, async () => {
      await api.post(`/api/runs/${job.id}/cancel`);
      await office.reload();
    }, "Tarea cancelada.");

  const nothing = !agent.current.length && !agent.queue.length && !agent.awaiting_approval.length && !agent.blocked.length && !agent.errors.length && !agent.paused.length;
  return (
    <Panel
      title={
        <span className="row">
          <span className="agent-dot" style={{ background: agent.color }} aria-hidden="true" />
          {agent.name} · {agent.specialty}
        </span>
      }
      actions={
        <button type="button" className="icon-btn" aria-label="Cerrar detalle" onClick={onClose}>
          <Icon name="cross" size={14} />
        </button>
      }
    >
      <div className="stack">
        <p className="soft">{agent.description}</p>
        {agent.current.length ? (
          <div className="stack stack-sm">
            <h4>Trabajando ahora</h4>
            {agent.current.map((job) => (
              <div key={job.id} className="card stack stack-sm">
                <div className="row row-between">
                  <strong>{job.title}</strong>
                  {session.canEdit ? (
                    <Button small icon="stop" busy={busy === job.id} onClick={() => void cancel(job)}>
                      Cancelar
                    </Button>
                  ) : null}
                </div>
                <RunProgress run={job} />
                <span className="small muted">Empezó {timeSince(job.started_at, now)}</span>
              </div>
            ))}
          </div>
        ) : null}
        {agent.queue.length ? (
          <div className="stack stack-sm">
            <h4>En cola</h4>
            {agent.queue.map((job) => (
              <div key={job.id} className="card row row-between">
                <span>{job.title}</span>
                {session.canEdit ? (
                  <Button small icon="cross" busy={busy === job.id} onClick={() => void cancel(job)}>
                    Quitar
                  </Button>
                ) : null}
              </div>
            ))}
          </div>
        ) : null}
        {agent.awaiting_approval.length ? (
          <div className="stack stack-sm">
            <h4>Espera tu aprobación</h4>
            <BriefList items={agent.awaiting_approval} action="Revisar" />
          </div>
        ) : null}
        {agent.errors.length ? (
          <div className="stack stack-sm">
            <h4>Con error</h4>
            <BriefList items={agent.errors} action="Ver y reintentar" />
          </div>
        ) : null}
        {agent.blocked.length ? (
          <div className="stack stack-sm">
            <h4>Bloqueado</h4>
            <BriefList items={agent.blocked} action="Resolver" />
          </div>
        ) : null}
        {agent.paused.length ? (
          <div className="stack stack-sm">
            <h4>En pausa</h4>
            <BriefList items={agent.paused} action="Abrir" />
          </div>
        ) : null}
        {nothing ? <p className="muted">{agent.name} no tiene nada pendiente.</p> : null}
        {agent.last_run ? (
          <div className="stack stack-sm">
            <h4>Último trabajo</h4>
            <div className="card stack stack-sm">
              <div className="row row-between">
                <span>{agent.last_run.title}</span>
                <RunStatusBadge status={agent.last_run.status} />
              </div>
              <span className="small soft">
                {timeSince(agent.last_run.finished_at, now)}
                {agent.last_run.seconds !== null ? ` · duró ${formatDuration(agent.last_run.seconds)}` : ""} · {usageSummary(agent.last_run)}
              </span>
              {agent.last_run.error ? <span className="small" style={{ color: "#ff9db0" }}>{agent.last_run.error}</span> : null}
            </div>
          </div>
        ) : null}
      </div>
    </Panel>
  );
}

// --- Side column ---------------------------------------------------------------

function Attention({ office }: { office: OfficeState }) {
  const { canEdit } = useSession();
  const rows: { key: string; agent: OfficeAgent; item: StepBrief; kind: "approval" | "error" | "blocked" }[] = [];
  for (const agent of office.agents) {
    for (const item of agent.awaiting_approval) rows.push({ key: `a-${item.project_id}-${item.kind}`, agent, item, kind: "approval" });
    for (const item of agent.errors) rows.push({ key: `e-${item.project_id}-${item.kind}`, agent, item, kind: "error" });
    for (const item of agent.blocked) rows.push({ key: `b-${item.project_id}-${item.kind}`, agent, item, kind: "blocked" });
  }
  const labels = { approval: "Revisar", error: "Ver error", blocked: "Resolver" } as const;
  const tones = { approval: "warn", error: "bad", blocked: "bad" } as const;
  const words = { approval: "Espera aprobación", error: "Error", blocked: "Bloqueado" } as const;
  const empty = rows.length === 0 && office.pending_publications === 0 && office.pending_replies === 0;
  return (
    <Panel title={canEdit ? "Necesita tu decisión" : "Pendiente de decisión"} tone={rows.length ? "warn" : undefined}>
      {empty ? <p className="muted">Nada pendiente. Cuando un agente termine algo o se quede bloqueado, aparecerá aquí.</p> : null}
      <ul>
        {rows.map(({ key, agent, item, kind }) => (
          <li key={key} className="attention-item">
            <span className="agent-dot" style={{ background: agent.color, marginTop: 5 }} aria-hidden="true" />
            <div className="grow">
              <div className="row">
                <strong>{agent.name}</strong>
                <Badge tone={tones[kind]}>{words[kind]}</Badge>
              </div>
              <div className="small">
                {item.title} · {item.project}
              </div>
              {item.reason || item.summary ? <div className="small soft">{kind === "approval" ? item.summary : item.reason}</div> : null}
            </div>
            <Link className="btn btn-small" to={`/proyectos/${item.project_id}?paso=${item.kind}`}>
              <span>{labels[kind]}</span>
            </Link>
          </li>
        ))}
        {office.pending_publications > 0 ? (
          <li className="attention-item">
            <Icon name="megaphone" />
            <div className="grow">{plural(office.pending_publications, "publicación preparada espera", "publicaciones preparadas esperan")} tu aprobación.</div>
            <Link className="btn btn-small" to="/calendario">
              <span>Ver</span>
            </Link>
          </li>
        ) : null}
        {office.pending_replies > 0 ? (
          <li className="attention-item">
            <Icon name="chat" />
            <div className="grow">{plural(office.pending_replies, "respuesta sugerida espera", "respuestas sugeridas esperan")} tu aprobación.</div>
            <Link className="btn btn-small" to="/comunidad">
              <span>Ver</span>
            </Link>
          </li>
        ) : null}
      </ul>
    </Panel>
  );
}

function Feed({ office }: { office: OfficeState }) {
  const now = useNow();
  const seen = useRef<Set<number> | null>(null);
  const fresh = new Set<number>();
  if (seen.current) {
    for (const event of office.events) if (!seen.current.has(event.id)) fresh.add(event.id);
  }
  useEffect(() => {
    seen.current = new Set(office.events.map((event) => event.id));
  }, [office.events]);
  const titles = Object.fromEntries(office.projects_list.map((project) => [project.id, project.title]));
  return (
    <Panel title="Actividad en directo" actions={<Link to="/actividad">Ver todo</Link>}>
      {office.events.length === 0 ? (
        <p className="muted">Todavía no ha pasado nada. Cada acción de un agente o de una persona quedará registrada aquí.</p>
      ) : (
        <ul className="feed scroll" aria-live="polite" aria-relevant="additions">
          {office.events.map((event) => (
            <EventLine key={event.id} event={{ ...event, project: event.project_id ? titles[event.project_id] : "" }} now={now} fresh={fresh.has(event.id)} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

interface ChatMessage {
  id: string;
  role: "user" | "orchestrator";
  text: string;
  action: { ok?: boolean; links?: { label: string; href: string }[] };
  created_at: string;
}

function OrderConsole() {
  const office = useOffice();
  const chat = useResource<{ messages: ChatMessage[]; help: string }>(() => api.get("/api/chat"), []);
  const [text, setText] = useState("");
  const { busy, run } = useAction();
  const log = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (log.current) log.current.scrollTop = log.current.scrollHeight;
  }, [chat.data]);

  const send = (event: FormEvent) => {
    event.preventDefault();
    const order = text.trim();
    if (!order) return;
    void run("send", async () => {
      const reply = await api.post<{ messages: ChatMessage[] }>("/api/chat", { text: order });
      chat.setData({ help: chat.data?.help ?? "", messages: [...(chat.data?.messages ?? []), ...reply.messages] });
      setText("");
      await office.reload();
    });
  };

  return (
    <Panel
      title={
        <span className="row">
          <Icon name="terminal" /> Consola de órdenes
        </span>
      }
    >
      <p className="small soft">Un intérprete de órdenes para el orquestador, no un chat con IA. Escribe «ayuda» para ver lo que entiende. Las aprobaciones y la publicación se hacen siempre revisando el resultado.</p>
      <div className="console-log" ref={log} aria-live="polite">
        {chat.loading && !chat.data ? <span className="muted">Cargando…</span> : null}
        {chat.data && chat.data.messages.length === 0 ? <span className="console-line muted">{chat.data.help}</span> : null}
        {(chat.data?.messages ?? []).map((message) => (
          <div key={message.id} className={`console-line ${message.role === "user" ? "user" : message.action.ok === false ? "bad" : ""}`}>
            {message.text}
            {(message.action.links ?? []).map((link) => (
              <div key={link.href}>
                → <Link to={link.href}>{link.label}</Link>
              </div>
            ))}
          </div>
        ))}
      </div>
      <form className="console-form" onSubmit={send}>
        <label className="grow">
          <span className="sr-only">Orden para el orquestador</span>
          <input type="text" value={text} maxLength={500} placeholder="estado · pausa [proyecto] · reintenta guion…" onChange={(event) => setText(event.target.value)} />
        </label>
        <Button type="submit" icon="send" busy={busy === "send"} disabled={!text.trim()}>
          Enviar
        </Button>
      </form>
    </Panel>
  );
}

function ProjectsStrip({ office }: { office: OfficeState }) {
  const active = office.projects_list.filter((project) => project.state !== "cancelled").slice(0, 6);
  if (!active.length) return null;
  return (
    <Panel title="Proyectos en marcha" actions={<Link to="/proyectos">Ver todos</Link>}>
      <div className="grid-cards">
        {active.map((project) => (
          <Link key={project.id} to={`/proyectos/${project.id}`} className="card card-link project-card stack stack-sm">
            <h3>{project.title}</h3>
            <Pips steps={project.steps} />
            <span className="small soft">
              {project.progress.done} de {project.progress.total} pasos de producción completados
              {project.state === "paused" ? " · en pausa" : ""}
              {project.attention.length ? ` · ${plural(project.attention.length, "asunto pendiente", "asuntos pendientes")}` : ""}
            </span>
          </Link>
        ))}
      </div>
    </Panel>
  );
}
