// One project: its brief, the pipeline board and the panel of the selected step.

import { useEffect, useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useTitle } from "../App";
import { ApiError, api, query } from "../api";
import type { ActivityEvent, ProjectDetail, Step } from "../api";
import { formatDate, formatDateTime, toLocalInput } from "../format";
import { useAction, useResource, useSession, useToast } from "../live";
import { Badge, Button, Confirm, EngineTag, ErrorBox, Field, Icon, Loading, Modal, Notice, Panel, StepBadge, stepTone } from "../ui";
import { ProjectContext, stepOf, useProject } from "./project/context";
import { AnalysisStepPanel, CommunityStepPanel } from "./project/Data";
import { ResearchPanel, ScriptPanel } from "./project/Documents";
import { PreflightPanel, PublishPanel } from "./project/Publish";
import { ClipsPanel, DesignPanel, EditPanel } from "./project/Studio";
import { EventLine, PlatformTag, RunProgress, useAgents, useNow } from "./shared";

const TONE_COLOR: Record<string, string> = { neutral: "#6b7190", ok: "#4ade80", warn: "#fbbf24", bad: "#fb7185", info: "#7aa2f7", accent: "#5eead4" };

export function ProjectPage() {
  const { id = "" } = useParams();
  const session = useSession();
  const [params, setParams] = useSearchParams();
  const resource = useResource<ProjectDetail>(() => api.get(`/api/projects/${id}`), [id], (message) => message.kind === "event" && message.project_id === id);
  const project = resource.data;
  useTitle(project?.title ?? "Proyecto");

  const requested = params.get("tab") === "publicar" ? "publish" : params.get("paso");
  const selected = useMemo(() => {
    if (!project) return "research";
    if (requested && project.steps.some((step) => step.kind === requested)) return requested;
    // Nothing chosen: open what needs a decision, else what is running, else the latest finished step.
    const order = ["awaiting_approval", "error", "blocked", "working", "queued"];
    for (const status of order) {
      const match = project.steps.find((step) => step.status === status && step.trigger === "auto");
      if (match) return match.kind;
    }
    const done = project.steps.filter((step) => step.status === "completed" && step.trigger === "auto");
    return done.length ? done[done.length - 1].kind : "research";
  }, [project, requested]);

  if (resource.error && !project) {
    return (
      <div className="stack">
        <ErrorBox error={resource.error} onRetry={resource.error.status === 404 ? undefined : () => void resource.reload()} />
        <Link to="/proyectos">Volver a proyectos</Link>
      </div>
    );
  }
  if (!project) return <Loading label="Abriendo el proyecto…" />;

  const select = (kind: string) => setParams({ paso: kind }, { replace: true });
  const step = stepOf(project, selected);
  return (
    <ProjectContext.Provider value={{ project, setProject: resource.setData, reload: resource.reload, canEdit: session.canEdit && project.state !== "cancelled" }}>
      <ProjectHeader />
      <NextActions onSelect={select} />
      <Board selected={selected} onSelect={select} />
      <StepPanel key={step.kind} step={step} onSelect={select} />
      <ProjectActivity />
    </ProjectContext.Provider>
  );
}

// --- Header --------------------------------------------------------------------

function ProjectHeader() {
  const { project, setProject, canEdit } = useProject();
  const session = useSession();
  const navigate = useNavigate();
  const { busy, run } = useAction();
  const [editing, setEditing] = useState(false);
  const [confirm, setConfirm] = useState<"cancel" | "delete" | null>(null);

  const changeState = (action: "pause" | "resume" | "cancel", done: string) =>
    run(action, async () => setProject(await api.post<ProjectDetail>(`/api/projects/${project.id}/state`, { action })), done);

  const remove = () =>
    run(
      "delete",
      async () => {
        await api.delete(`/api/projects/${project.id}`);
        navigate("/proyectos");
      },
      "Proyecto eliminado.",
    );

  return (
    <>
      <div className="page-head">
        <div className="grow">
          <div className="small">
            <Link to="/proyectos">Proyectos</Link> <span className="muted">/</span>
          </div>
          <h1 style={{ overflowWrap: "anywhere" }}>{project.title}</h1>
          <div className="row" style={{ marginTop: 8 }}>
            {project.state === "paused" ? <Badge icon="pause">En pausa</Badge> : null}
            {project.state === "cancelled" ? <Badge tone="bad" icon="stop">Cancelado</Badge> : null}
            {project.platforms.map((platform) => (
              <span key={platform} className="small">
                <PlatformTag platform={platform} />
              </span>
            ))}
            {project.deadline ? <span className="small soft">· Fecha límite: {formatDate(project.deadline)}</span> : null}
          </div>
        </div>
        {session.canEdit ? (
          <div className="row">
            {project.state === "active" ? (
              <Button icon="pause" busy={busy === "pause"} onClick={() => void changeState("pause", "Proyecto en pausa. Los trabajos en curso se detienen.")}>
                Pausar
              </Button>
            ) : null}
            {project.state !== "active" ? (
              <Button icon="play" variant="primary" busy={busy === "resume"} onClick={() => void changeState("resume", "Proyecto reanudado.")}>
                Reanudar
              </Button>
            ) : null}
            {canEdit ? (
              <Button icon="edit" onClick={() => setEditing(true)}>
                Editar brief
              </Button>
            ) : null}
            {project.state !== "cancelled" ? (
              <Button variant="ghost" icon="stop" onClick={() => setConfirm("cancel")}>
                Cancelar
              </Button>
            ) : null}
            <Button variant="ghost" icon="trash" onClick={() => setConfirm("delete")}>
              Eliminar
            </Button>
          </div>
        ) : null}
      </div>
      {project.state === "cancelled" ? (
        <Notice tone="warn" title="Este proyecto está cancelado.">
          Se conserva todo lo que se produjo, pero ningún agente trabaja en él. Reanúdalo para continuar.
        </Notice>
      ) : null}
      {project.state === "paused" ? (
        <Notice tone="info" icon="pause" title="Proyecto en pausa.">
          Ningún agente empieza trabajo nuevo hasta que lo reanudes.
        </Notice>
      ) : null}
      <BriefSummary />
      {editing ? <BriefEditor onClose={() => setEditing(false)} /> : null}
      {confirm === "cancel" ? (
        <Confirm
          title="Cancelar el proyecto"
          confirmLabel="Cancelar proyecto"
          danger
          busy={busy === "cancel"}
          onClose={() => setConfirm(null)}
          onConfirm={() => void changeState("cancel", "Proyecto cancelado.").then(() => setConfirm(null))}
        >
          <p>Se detienen los trabajos en curso y los pasos pendientes quedan cancelados. No se borra nada y podrás reanudarlo más adelante.</p>
        </Confirm>
      ) : null}
      {confirm === "delete" ? (
        <Confirm title="Eliminar el proyecto" confirmLabel="Eliminar definitivamente" danger busy={busy === "delete"} onClose={() => setConfirm(null)} onConfirm={() => void remove()}>
          <p>
            Se borrarán «{project.title}» y todos sus archivos: vídeo original, piezas exportadas, diseños, guiones y borradores de publicación. <strong>No se puede deshacer.</strong>
          </p>
          <p className="soft">Lo que ya esté publicado en las plataformas no se toca.</p>
        </Confirm>
      ) : null}
    </>
  );
}

function BriefSummary() {
  const { project } = useProject();
  const { catalog } = useSession();
  const template = catalog.templates.find((item) => item.id === project.template);
  return (
    <details className="panel">
      <summary>
        Brief del proyecto <span className="muted small">· {template?.name ?? project.template}</span>
      </summary>
      <dl className="kv">
        <dt>Tema</dt>
        <dd className="prose">{project.topic || <span className="muted">Sin indicar</span>}</dd>
        <dt>Audiencia</dt>
        <dd className="prose">{project.audience || <span className="muted">Sin indicar</span>}</dd>
        <dt>Objetivo</dt>
        <dd className="prose">{project.goal || <span className="muted">Sin indicar</span>}</dd>
        <dt>Tono</dt>
        <dd>{project.tone || <span className="muted">El del kit de marca</span>}</dd>
        <dt>Idioma</dt>
        <dd>{catalog.languages[project.language] ?? project.language}</dd>
        <dt>Fuentes</dt>
        <dd>
          {project.references.length ? (
            <ul className="stack stack-sm">
              {project.references.map((reference) => (
                <li key={reference.url}>
                  <a href={reference.url} target="_blank" rel="noopener noreferrer">
                    {reference.url}
                  </a>
                  {reference.note ? <span className="soft"> — {reference.note}</span> : null}
                </li>
              ))}
            </ul>
          ) : (
            <span className="muted">Ninguna en el proyecto (se usan las del kit de marca, si las hay)</span>
          )}
        </dd>
        <dt>Clips</dt>
        <dd>
          {project.options.clip_count} de entre {project.options.clip_min_s} y {project.options.clip_max_s} s
        </dd>
        <dt>Creado</dt>
        <dd>{formatDateTime(project.created_at)}</dd>
      </dl>
    </details>
  );
}

function BriefEditor({ onClose }: { onClose: () => void }) {
  const { project, setProject } = useProject();
  const { catalog } = useSession();
  const toast = useToast();
  const [form, setForm] = useState({
    title: project.title,
    topic: project.topic,
    audience: project.audience,
    goal: project.goal,
    tone: project.tone,
    language: project.language,
    deadline: project.deadline ? toLocalInput(project.deadline).slice(0, 10) : "",
  });
  const [platforms, setPlatforms] = useState(project.platforms);
  const [options, setOptions] = useState(project.options);
  const [references, setReferences] = useState(project.references.length ? project.references : [{ url: "", note: "" }]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (key: keyof typeof form) => (event: { target: { value: string } }) => setForm({ ...form, [key]: event.target.value });

  const save = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const flagged = (detail: ProjectDetail) => detail.steps.filter((step) => step.stale_reason).length;
      const updated = await api.patch<ProjectDetail>(`/api/projects/${project.id}`, {
        ...form,
        deadline: form.deadline ? new Date(`${form.deadline}T23:59:00`).toISOString() : null,
        clear_deadline: !form.deadline,
        platforms,
        options,
        references: references.filter((row) => row.url.trim()),
      });
      setProject(updated);
      // Nothing is redone on its own: the person decides which finished steps to launch again.
      toast.ok(flagged(updated) > flagged(project) ? "Brief guardado. Los pasos ya hechos quedan marcados como desactualizados: vuelve a lanzar los que quieras rehacer." : "Brief guardado.");
      onClose();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "No se pudo guardar.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Editar brief" onClose={onClose} wide>
      <form className="stack" onSubmit={save}>
        <Notice tone="info">Cambiar el brief no rehace el trabajo ya entregado: los pasos terminados se marcan como desactualizados y tú decides cuáles volver a lanzar.</Notice>
        {error ? (
          <div className="notice notice-bad" role="alert">
            <span>{error}</span>
          </div>
        ) : null}
        <div className="form-grid">
          <div className="span-2">
            <Field label="Título">
              <input type="text" value={form.title} onChange={set("title")} required minLength={3} maxLength={200} data-autofocus />
            </Field>
          </div>
          <div className="span-2">
            <Field label="Tema o idea">
              <textarea value={form.topic} onChange={set("topic")} maxLength={4000} rows={3} />
            </Field>
          </div>
          <Field label="Audiencia">
            <textarea value={form.audience} onChange={set("audience")} maxLength={2000} rows={2} />
          </Field>
          <Field label="Objetivo">
            <textarea value={form.goal} onChange={set("goal")} maxLength={2000} rows={2} />
          </Field>
          <Field label="Tono">
            <input type="text" value={form.tone} onChange={set("tone")} maxLength={120} />
          </Field>
          <Field label="Idioma del contenido">
            <select value={form.language} onChange={set("language")}>
              {Object.entries(catalog.languages).map(([code, name]) => (
                <option key={code} value={code}>
                  {name}
                </option>
              ))}
            </select>
          </Field>
          <div className="span-2">
            <Field label="Plataformas de destino">
              <div className="toggle-chips">
                {catalog.platforms.map((platform) => (
                  <label key={platform.key} className="toggle-chip">
                    <input
                      type="checkbox"
                      className="sr-only"
                      checked={platforms.includes(platform.key)}
                      onChange={(event) => setPlatforms(event.target.checked ? [...platforms, platform.key] : platforms.filter((key) => key !== platform.key))}
                    />
                    {platform.name}
                  </label>
                ))}
              </div>
            </Field>
          </div>
          <Field label="Fecha límite">
            <input type="date" value={form.deadline} onChange={set("deadline")} />
          </Field>
          <Field label="Número de clips">
            <input type="number" min={1} max={12} value={options.clip_count ?? 3} onChange={(event) => setOptions({ ...options, clip_count: Number(event.target.value) })} />
          </Field>
          <Field label="Duración mínima del clip (s)">
            <input type="number" min={3} max={600} value={options.clip_min_s ?? 15} onChange={(event) => setOptions({ ...options, clip_min_s: Number(event.target.value) })} />
          </Field>
          <Field label="Duración máxima del clip (s)">
            <input type="number" min={5} max={1800} value={options.clip_max_s ?? 60} onChange={(event) => setOptions({ ...options, clip_max_s: Number(event.target.value) })} />
          </Field>
          <div className="span-2 stack stack-sm">
            <span className="field-label">Fuentes que Scout puede leer</span>
            {references.map((row, index) => (
              <div key={index} className="row">
                <input
                  type="url"
                  className="grow"
                  style={{ flex: "2 1 220px" }}
                  value={row.url}
                  placeholder="https://…"
                  aria-label={`Enlace ${index + 1}`}
                  onChange={(event) => setReferences(references.map((item, i) => (i === index ? { ...item, url: event.target.value } : item)))}
                />
                <input
                  type="text"
                  style={{ flex: "1 1 140px" }}
                  value={row.note}
                  placeholder="Nota"
                  aria-label={`Nota del enlace ${index + 1}`}
                  onChange={(event) => setReferences(references.map((item, i) => (i === index ? { ...item, note: event.target.value } : item)))}
                />
                <button type="button" className="icon-btn" aria-label={`Quitar enlace ${index + 1}`} onClick={() => setReferences(references.filter((_, i) => i !== index))}>
                  <Icon name="trash" size={14} />
                </button>
              </div>
            ))}
            {references.length < 12 ? (
              <div>
                <Button small icon="plus" onClick={() => setReferences([...references, { url: "", note: "" }])}>
                  Añadir enlace
                </Button>
              </div>
            ) : null}
          </div>
        </div>
        <div className="row row-end">
          <Button variant="ghost" onClick={onClose}>
            Cancelar
          </Button>
          <Button type="submit" variant="primary" busy={busy}>
            Guardar brief
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// --- Board ---------------------------------------------------------------------

function NextActions({ onSelect }: { onSelect: (kind: string) => void }) {
  const { project, canEdit } = useProject();
  const agents = useAgents();
  if (!project.attention.length || project.state !== "active") return null;
  const words: Record<string, string> = { awaiting_approval: canEdit ? "espera tu aprobación" : "espera una aprobación", error: "terminó con error", blocked: "no puede empezar" };
  return (
    <Notice tone="warn" icon="eye" title={canEdit ? "Te toca a ti:" : "Pendiente de una decisión (tu rol es de solo lectura: decide un editor o un propietario):"}>
      <ul className="stack stack-sm" style={{ marginTop: 6 }}>
        {project.attention.map((item) => (
          <li key={item.kind} className="row">
            <span>
              <strong>{agents[item.agent]?.name ?? "La revisión previa"}</strong> {words[item.status] ?? item.status} en «{item.title}».
            </span>
            <button type="button" className="link-btn" onClick={() => onSelect(item.kind)}>
              Abrir
            </button>
          </li>
        ))}
      </ul>
    </Notice>
  );
}

function Board({ selected, onSelect }: { selected: string; onSelect: (kind: string) => void }) {
  const { project } = useProject();
  const agents = useAgents();
  return (
    <nav className="board" aria-label="Pasos del proyecto">
      {project.steps.map((step) => (
        <button
          key={step.kind}
          type="button"
          className="station"
          aria-pressed={step.kind === selected}
          style={{ borderTopColor: TONE_COLOR[stepTone(step.status)] }}
          onClick={() => onSelect(step.kind)}
        >
          <span className="station-agent">{agents[step.agent]?.name ?? "Orquestador"}</span>
          <span className="station-title">{step.title}</span>
          <StepBadge status={step.status} label={step.status_label} />
          {step.stale_reason ? <Badge tone="warn" icon="warning">Desactualizado</Badge> : null}
        </button>
      ))}
    </nav>
  );
}

// --- Step panel ----------------------------------------------------------------

export interface ApproveControl {
  /** Extra data sent with the approval (for example, the chosen angle). */
  payload?: Record<string, unknown>;
  /** When set, approval is not possible yet and this explains why. */
  blocked?: string;
  label?: string;
}

function StepPanel({ step, onSelect }: { step: Step; onSelect: (kind: string) => void }) {
  const { project } = useProject();
  const agents = useAgents();
  const agent = agents[step.agent];
  const [approve, setApprove] = useState<ApproveControl>({});
  let body: ReactNode = null;
  if (step.kind === "research") body = <ResearchPanel step={step} onApproveChange={setApprove} />;
  else if (step.kind === "script") body = <ScriptPanel step={step} />;
  else if (step.kind === "clips") body = <ClipsPanel step={step} />;
  else if (step.kind === "edit") body = <EditPanel step={step} />;
  else if (step.kind === "design") body = <DesignPanel step={step} />;
  else if (step.kind === "preflight") body = <PreflightPanel step={step} />;
  else if (step.kind === "publish") body = <PublishPanel step={step} />;
  else if (step.kind === "community") body = <CommunityStepPanel step={step} />;
  else if (step.kind === "analysis") body = <AnalysisStepPanel step={step} />;

  const engine = step.output.engine as { label?: string; mode?: string } | undefined;
  return (
    <Panel
      id={`paso-${step.kind}`}
      title={
        <span className="row">
          {agent ? <span className="agent-dot" style={{ background: agent.color }} aria-hidden="true" /> : <Icon name="check" />}
          {agent ? `${agent.name} · ` : ""}
          {step.title}
        </span>
      }
      actions={
        <>
          {engine?.label && step.agent !== "orchestrator" ? <EngineTag label={engine.label} demo={engine.mode === "demo"} /> : null}
          <StepBadge status={step.status} label={step.status_label} />
        </>
      }
    >
      <div className="stack stack-lg">
        <StepStatus step={step} approve={approve} onSelect={onSelect} />
        {body}
        {project.state !== "cancelled" && agent ? <StepInstructions step={step} agentName={agent.name} /> : null}
      </div>
    </Panel>
  );
}

function StepStatus({ step, approve, onSelect }: { step: Step; approve: ApproveControl; onSelect: (kind: string) => void }) {
  const { project, setProject, canEdit } = useProject();
  const agents = useAgents();
  const { busy, run } = useAction();
  const [asking, setAsking] = useState(false);
  const [skipping, setSkipping] = useState(false);
  const name = agents[step.agent]?.name ?? "La revisión previa";
  const active = project.state === "active";

  const act = (action: string, done?: string, extra: Record<string, unknown> = {}) =>
    run(action, async () => setProject(await api.post<ProjectDetail>(`/api/projects/${project.id}/steps/${step.kind}`, { action, ...extra })), done);

  const waitingFor = step.depends_on.map((kind) => stepOf(project, kind)).filter((dep) => !["completed", "skipped"].includes(dep.status));
  const fix: Record<string, { label: string; kind: string } | undefined> = {
    no_clips: { label: "Ir a la selección de clips", kind: "clips" },
    no_pieces: { label: "Ir a edición y exportación", kind: "edit" },
    missing_video: step.kind === "clips" ? undefined : { label: "Ir a la selección de clips", kind: "clips" },
    missing_rights: step.kind === "clips" ? undefined : { label: "Ir a la selección de clips", kind: "clips" },
  };
  const jump = fix[step.blocked_code];

  return (
    <div className="stack">
      {step.status === "working" || step.status === "queued" ? (
        <div className="card stack stack-sm">
          <div className="row row-between">
            <strong>{step.status === "queued" ? `${name} tiene este paso en cola.` : `${name} está trabajando en este paso.`}</strong>
            {canEdit ? (
              <Button small icon="pause" busy={busy === "pause"} onClick={() => void act("pause", "Paso en pausa.")}>
                Pausar
              </Button>
            ) : null}
          </div>
          {step.run ? <RunProgress run={step.run} /> : null}
          <span className="small muted">El progreso y los mensajes vienen de la ejecución real; si no hay porcentaje es que la tarea no permite medirlo.</span>
        </div>
      ) : null}

      {step.status === "pending" ? (
        <Notice tone="info" icon="clock">
          {step.trigger === "data"
            ? step.waiting_hint
            : waitingFor.length
              ? `Este paso empezará cuando ${waitingFor.length === 1 ? "termine" : "terminen"}: ${waitingFor.map((dep) => dep.title).join(", ")}.`
              : active
                ? "Este paso está a punto de empezar."
                : "Este paso empezará cuando reanudes el proyecto."}
        </Notice>
      ) : null}

      {step.status === "blocked" ? (
        <Notice
          tone="warn"
          icon="lock"
          title={`${name} no puede ${step.kind === "preflight" ? "dar el visto bueno" : "empezar"}.`}
          actions={
            canEdit ? (
              <>
                {jump ? (
                  <Button small onClick={() => onSelect(jump.kind)}>
                    {jump.label}
                  </Button>
                ) : null}
                <Button small icon="retry" busy={busy === "rerun"} onClick={() => void act("rerun")}>
                  Comprobar de nuevo
                </Button>
              </>
            ) : null
          }
        >
          {step.blocked_reason}
        </Notice>
      ) : null}

      {step.status === "error" ? (
        <Notice
          tone="bad"
          title={`${name} no pudo completar este paso.`}
          actions={
            canEdit && active ? (
              <Button small icon="retry" busy={busy === "rerun"} onClick={() => void act("rerun", "Paso relanzado.")}>
                Reintentar
              </Button>
            ) : null
          }
        >
          {step.error}
          {step.attempts > 1 ? <span className="muted"> ({step.attempts} intentos)</span> : null}
        </Notice>
      ) : null}

      {step.status === "paused" ? (
        <Notice
          tone="info"
          icon="pause"
          title="Paso en pausa."
          actions={
            canEdit && active ? (
              <Button small icon="play" busy={busy === "resume"} onClick={() => void act("resume", "Paso reanudado.")}>
                Reanudar
              </Button>
            ) : null
          }
        >
          {active ? "Al reanudar, el paso empieza de nuevo desde el principio." : "Reanuda el proyecto para continuar."}
        </Notice>
      ) : null}

      {step.status === "skipped" ? (
        <Notice
          tone="info"
          title="Paso omitido."
          actions={
            canEdit && active ? (
              <Button small icon="retry" busy={busy === "rerun"} onClick={() => void act("rerun", "Paso lanzado.")}>
                Lanzarlo ahora
              </Button>
            ) : null
          }
        >
          Decidiste continuar sin este paso.
        </Notice>
      ) : null}

      {step.status === "cancelled" ? <Notice tone="info">Este paso se canceló con el proyecto.</Notice> : null}

      {step.stale_reason && ["completed", "awaiting_approval"].includes(step.status) ? (
        <Notice
          tone="warn"
          title="Puede estar desactualizado."
          actions={
            canEdit && active ? (
              <Button small icon="retry" busy={busy === "rerun"} onClick={() => void act("rerun", "Paso relanzado.")}>
                Volver a lanzar a {name}
              </Button>
            ) : null
          }
        >
          {step.stale_reason} Puedes dejarlo como está o volver a lanzarlo.
        </Notice>
      ) : null}

      {step.summary && ["awaiting_approval", "completed"].includes(step.status) ? (
        <p>
          <strong>Resultado:</strong> {step.summary}.
        </p>
      ) : null}

      {step.feedback && ["queued", "working"].includes(step.status) ? (
        <p className="soft">
          <strong>Cambios que pediste:</strong> {step.feedback}
        </p>
      ) : null}

      {step.status === "awaiting_approval" && canEdit ? (
        <div className="card stack stack-sm" style={{ borderColor: "#6b4f08" }}>
          <strong>{name} espera tu decisión.</strong>
          <span className="small soft">Revisa el resultado de abajo. Al aprobar, el orquestador entrega el trabajo al siguiente agente.</span>
          {approve.blocked ? <span className="small" style={{ color: "#fbbf24" }}>{approve.blocked}</span> : null}
          <div className="row">
            <Button variant="primary" icon="check" busy={busy === "approve"} disabled={Boolean(approve.blocked)} onClick={() => void act("approve", "Aprobado.", approve.payload ?? {})}>
              {approve.label ?? "Aprobar y continuar"}
            </Button>
            <Button icon="edit" onClick={() => setAsking(true)}>
              Pedir cambios
            </Button>
            <Button variant="ghost" onClick={() => setSkipping(true)}>
              Omitir paso
            </Button>
          </div>
        </div>
      ) : null}

      {step.status === "completed" && canEdit && active && step.agent !== "orchestrator" ? (
        <div className="row">
          {step.requires_approval ? (
            <Button small icon="edit" onClick={() => setAsking(true)}>
              Pedir cambios
            </Button>
          ) : null}
          {!step.stale_reason ? (
            <Button small icon="retry" busy={busy === "rerun"} onClick={() => void act("rerun", "Paso relanzado.")}>
              Volver a lanzar
            </Button>
          ) : null}
        </div>
      ) : null}

      {["pending", "blocked", "error", "paused"].includes(step.status) && canEdit && step.trigger === "auto" ? (
        <div>
          <button type="button" className="link-btn small" onClick={() => setSkipping(true)}>
            Omitir este paso y continuar sin él
          </button>
        </div>
      ) : null}

      {asking ? (
        <ChangesModal
          agentName={name}
          warnDownstream={step.status === "completed"}
          busy={busy === "request_changes"}
          onClose={() => setAsking(false)}
          onSend={(feedback) => void act("request_changes", `Cambios enviados a ${name}.`, { feedback }).then((ok) => ok && setAsking(false))}
        />
      ) : null}
      {skipping ? (
        <Confirm
          title="Omitir este paso"
          confirmLabel="Omitir paso"
          busy={busy === "skip"}
          onClose={() => setSkipping(false)}
          onConfirm={() => void act("skip", "Paso omitido.").then(() => setSkipping(false))}
        >
          <p>
            El proyecto continuará sin «{step.title}». Los pasos siguientes trabajarán con lo que haya; si les falta algo, te lo dirán. Podrás lanzarlo más adelante.
          </p>
        </Confirm>
      ) : null}
    </div>
  );
}

function ChangesModal({ agentName, warnDownstream, busy, onClose, onSend }: { agentName: string; warnDownstream: boolean; busy: boolean; onClose: () => void; onSend: (feedback: string) => void }) {
  const [feedback, setFeedback] = useState("");
  return (
    <Modal
      title={`Pedir cambios a ${agentName}`}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancelar
          </Button>
          <Button variant="primary" icon="send" busy={busy} disabled={!feedback.trim()} onClick={() => onSend(feedback)}>
            Enviar y rehacer
          </Button>
        </>
      }
    >
      <div className="stack">
        <Field label="¿Qué quieres que cambie?" hint={`${agentName} vuelve a hacer el paso teniendo en cuenta esto. La versión actual se conserva en el historial.`} counter={`${feedback.length}/2000`}>
          <textarea value={feedback} maxLength={2000} rows={5} onChange={(event) => setFeedback(event.target.value)} data-autofocus />
        </Field>
        {warnDownstream ? <Notice tone="warn">Este paso ya estaba aprobado: los pasos posteriores se marcarán como desactualizados para que decidas si rehacerlos.</Notice> : null}
      </div>
    </Modal>
  );
}

function StepInstructions({ step, agentName }: { step: Step; agentName: string }) {
  const { project, setProject, canEdit } = useProject();
  const [text, setText] = useState(step.instructions);
  const { busy, run } = useAction();
  useEffect(() => setText(step.instructions), [step.instructions]);
  if (!canEdit && !step.instructions) return null;
  return (
    <details>
      <summary>
        Instrucciones permanentes para {agentName}
        {step.instructions ? " · definidas" : ""}
      </summary>
      <div className="stack stack-sm">
        <Field label={`Indicaciones que ${agentName} tendrá en cuenta cada vez que haga este paso`} counter={`${text.length}/2000`}>
          <textarea value={text} maxLength={2000} rows={3} disabled={!canEdit} onChange={(event) => setText(event.target.value)} />
        </Field>
        {canEdit ? (
          <div>
            <Button
              small
              busy={busy === "save"}
              disabled={text.trim() === step.instructions}
              onClick={() =>
                void run("save", async () => setProject(await api.patch<ProjectDetail>(`/api/projects/${project.id}/steps/${step.kind}`, { instructions: text })), "Instrucciones guardadas. Se aplican la próxima vez que se ejecute el paso.")
              }
            >
              Guardar instrucciones
            </Button>
          </div>
        ) : null}
      </div>
    </details>
  );
}

// --- Activity ------------------------------------------------------------------

function ProjectActivity() {
  const { project } = useProject();
  const now = useNow();
  const activity = useResource<{ events: ActivityEvent[] }>(
    () => api.get(`/api/activity${query({ project_id: project.id, limit: 40 })}`),
    [project.id],
    (message) => message.kind === "event" && message.project_id === project.id,
  );
  return (
    <Panel title="Historial del proyecto" actions={<Link to={`/actividad${query({ proyecto: project.id })}`}>Ver completo</Link>}>
      {activity.loading && !activity.data ? <Loading /> : null}
      {activity.error && !activity.data ? <ErrorBox error={activity.error} onRetry={() => void activity.reload()} /> : null}
      {activity.data ? (
        <ul className="feed scroll">
          {activity.data.events.map((event) => (
            <EventLine key={event.id} event={event} now={now} showProject={false} />
          ))}
        </ul>
      ) : null}
    </Panel>
  );
}
