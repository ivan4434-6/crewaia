// Project list and the "new project" brief.

import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { PageHead } from "../App";
import { ApiError, api } from "../api";
import type { ProjectDetail, ProjectSummary, TemplateInfo } from "../api";
import { formatDate, plural, timeSince } from "../format";
import { useResource, useSession } from "../live";
import { Badge, Button, Empty, ErrorBox, Field, Icon, Loading, Notice, Panel } from "../ui";
import { Pips, PlatformTag, Segmented, useAgents, useNow } from "./shared";

type StateFilter = "all" | "active" | "paused" | "cancelled";

export function ProjectsPage() {
  const session = useSession();
  const projects = useResource<{ projects: ProjectSummary[] }>(() => api.get("/api/projects"), [session.workspace.id], (message) => message.kind === "event");
  const [filter, setFilter] = useState<StateFilter>("all");
  const [text, setText] = useState("");
  const now = useNow();
  const agents = useAgents();

  const list = useMemo(() => {
    const needle = text.trim().toLowerCase();
    return (projects.data?.projects ?? []).filter(
      (project) => (filter === "all" || project.state === filter) && (!needle || `${project.title} ${project.topic}`.toLowerCase().includes(needle)),
    );
  }, [projects.data, filter, text]);

  const total = projects.data?.projects.length ?? 0;
  return (
    <>
      <PageHead
        title="Proyectos"
        actions={
          session.canEdit ? (
            <Link className="btn btn-primary" to="/proyectos/nuevo">
              <Icon name="plus" size={14} />
              <span>Nuevo proyecto</span>
            </Link>
          ) : null
        }
      >
        Cada proyecto recorre la oficina: investigación, guion, clips, edición, diseño, revisión previa y publicación.
      </PageHead>
      {projects.error && !projects.data ? <ErrorBox error={projects.error} onRetry={() => void projects.reload()} /> : null}
      {projects.loading && !projects.data ? <Loading /> : null}
      {projects.data && total === 0 ? (
        <Empty
          icon="folder"
          title="Todavía no hay proyectos"
          action={
            session.canEdit ? (
              <Link className="btn btn-primary" to="/proyectos/nuevo">
                <span>Crear el primer proyecto</span>
              </Link>
            ) : null
          }
        >
          Empieza con una idea o con un vídeo ya grabado. Elige una plantilla y la oficina se pone en marcha.
        </Empty>
      ) : null}
      {total > 0 ? (
        <>
          <div className="row row-between">
            <Segmented
              label="Filtrar por estado"
              value={filter}
              onChange={setFilter}
              options={[
                { id: "all", label: "Todos" },
                { id: "active", label: "Activos" },
                { id: "paused", label: "En pausa" },
                { id: "cancelled", label: "Cancelados" },
              ]}
            />
            <label style={{ flex: "0 1 280px" }}>
              <span className="sr-only">Filtrar por título o tema</span>
              <input type="search" value={text} placeholder="Filtrar por título o tema" onChange={(event) => setText(event.target.value)} />
            </label>
          </div>
          {list.length === 0 ? <p className="muted">Ningún proyecto coincide con el filtro.</p> : null}
          <div className="grid-cards">
            {list.map((project) => (
              <Link key={project.id} to={`/proyectos/${project.id}`} className="card card-link project-card stack stack-sm">
                <div className="row row-between row-top">
                  <h3>{project.title}</h3>
                  {project.state === "paused" ? <Badge icon="pause">En pausa</Badge> : project.state === "cancelled" ? <Badge icon="stop">Cancelado</Badge> : null}
                </div>
                <div className="row">
                  {project.platforms.map((platform) => (
                    <span key={platform} className="small">
                      <PlatformTag platform={platform} />
                    </span>
                  ))}
                </div>
                <Pips steps={project.steps} />
                <span className="small soft">
                  {project.progress.done} de {project.progress.total} pasos de producción completados · actualizado {timeSince(project.updated_at, now)}
                </span>
                {project.attention.map((item) => (
                  <span key={item.kind} className="row small">
                    <Badge tone={item.status === "awaiting_approval" ? "warn" : "bad"} icon={item.status === "awaiting_approval" ? "eye" : "warning"}>
                      {item.status === "awaiting_approval" ? "Espera aprobación" : item.status === "error" ? "Error" : "Bloqueado"}
                    </Badge>
                    <span>
                      {agents[item.agent]?.name ?? "Revisión previa"} · {item.title}
                    </span>
                  </span>
                ))}
                {project.deadline ? <span className="small muted">Fecha límite: {formatDate(project.deadline)}</span> : null}
              </Link>
            ))}
          </div>
        </>
      ) : null}
    </>
  );
}

// --- New project ---------------------------------------------------------------

interface ReferenceRow {
  url: string;
  note: string;
}

export function NewProjectPage() {
  const session = useSession();
  const navigate = useNavigate();
  const templates = session.catalog.templates;
  const [template, setTemplate] = useState<TemplateInfo>(templates[0]);
  const [form, setForm] = useState({ title: "", topic: "", audience: "", goal: templates[0].goal, tone: "", language: "es", deadline: "" });
  const [platforms, setPlatforms] = useState<string[]>(templates[0].platforms);
  const [options, setOptions] = useState<Record<string, number>>(templates[0].options);
  const [references, setReferences] = useState<ReferenceRow[]>([{ url: "", note: "" }]);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);

  const choose = (next: TemplateInfo) => {
    // Keep what the person already typed; only replace values that still match the previous template.
    setForm((current) => ({ ...current, goal: current.goal === template.goal || !current.goal ? next.goal : current.goal }));
    setPlatforms(next.platforms);
    setOptions(next.options);
    setTemplate(next);
  };

  const set = (key: keyof typeof form) => (event: { target: { value: string } }) => setForm({ ...form, [key]: event.target.value });
  const fieldError = (field: string): string | undefined => {
    if (!error) return undefined;
    if (error.data.field === field) return error.message;
    const fields = error.data.fields as Record<string, string> | undefined;
    return fields?.[field];
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!platforms.length) {
      setError(new ApiError(422, "Elige al menos una plataforma de destino.", "no_platforms", { field: "platforms" }));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const project = await api.post<ProjectDetail>("/api/projects", {
        ...form,
        deadline: form.deadline ? new Date(`${form.deadline}T23:59:00`).toISOString() : null,
        platforms,
        template: template.id,
        options,
        references: references.filter((row) => row.url.trim()),
      });
      navigate(`/proyectos/${project.id}`);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : new ApiError(0, "Error inesperado. Inténtalo de nuevo."));
      window.scrollTo({ top: 0 });
    } finally {
      setBusy(false);
    }
  };

  if (!session.canEdit) {
    return (
      <>
        <PageHead title="Nuevo proyecto" />
        <Notice tone="warn" title="Tu rol es de solo lectura.">
          Pide a un propietario del espacio que te dé permisos de edición para crear proyectos.
        </Notice>
      </>
    );
  }

  const setOption = (key: string) => (event: { target: { value: string } }) => setOptions({ ...options, [key]: Number(event.target.value) });
  return (
    <>
      <PageHead title="Nuevo proyecto">Cuenta qué quieres hacer. Con este brief trabajan todos los agentes; podrás cambiarlo después.</PageHead>
      {error && !error.data.field && !error.data.fields ? (
        <div className="notice notice-bad" role="alert">
          <span>{error.message}</span>
        </div>
      ) : null}
      <form className="stack stack-lg" onSubmit={submit}>
        <Panel title="1 · Plantilla">
          <div className="template-grid" role="group" aria-label="Plantilla del proyecto">
            {templates.map((item) => (
              <button key={item.id} type="button" className="template" aria-pressed={item.id === template.id} onClick={() => choose(item)}>
                <strong>{item.name}</strong>
                <span className="small soft">{item.description}</span>
                <span className="row">{item.needs_video ? <Badge icon="video">Parte de un vídeo tuyo</Badge> : <Badge icon="edit">Parte de una idea</Badge>}</span>
              </button>
            ))}
          </div>
          <p className="small soft" style={{ marginTop: 10 }}>
            Estructura del guion: {template.structure.join(" → ")}
          </p>
        </Panel>

        <Panel title="2 · Brief">
          <div className="form-grid">
            <div className="span-2">
              <Field label="Título del proyecto" error={fieldError("title")}>
                <input type="text" value={form.title} onChange={set("title")} required minLength={3} maxLength={200} placeholder="Por ejemplo: Cómo organizo mi semana de grabación" />
              </Field>
            </div>
            <div className="span-2">
              <Field label="Tema o idea" hint="Qué quieres contar. Cuanto más concreto, mejor trabajan Scout y Writer." error={fieldError("topic")}>
                <textarea value={form.topic} onChange={set("topic")} maxLength={4000} rows={3} />
              </Field>
            </div>
            <Field label="Audiencia" hint="A quién le hablas.">
              <textarea value={form.audience} onChange={set("audience")} maxLength={2000} rows={2} />
            </Field>
            <Field label="Objetivo" hint="Qué quieres conseguir con este contenido.">
              <textarea value={form.goal} onChange={set("goal")} maxLength={2000} rows={2} />
            </Field>
            <Field label="Tono" hint="Opcional. Se suma a la voz de tu kit de marca.">
              <input type="text" value={form.tone} onChange={set("tone")} maxLength={120} placeholder="Cercano, directo, con humor…" />
            </Field>
            <Field label="Idioma del contenido" error={fieldError("language")}>
              <select value={form.language} onChange={set("language")}>
                {Object.entries(session.catalog.languages).map(([code, name]) => (
                  <option key={code} value={code}>
                    {name}
                  </option>
                ))}
              </select>
            </Field>
            <div className="span-2">
              <Field label="Plataformas de destino" error={fieldError("platforms")}>
                <div className="toggle-chips">
                  {session.catalog.platforms.map((platform) => (
                    <label key={platform.key} className="toggle-chip">
                      <input
                        type="checkbox"
                        className="sr-only"
                        checked={platforms.includes(platform.key)}
                        onChange={(event) => setPlatforms(event.target.checked ? [...platforms, platform.key] : platforms.filter((key) => key !== platform.key))}
                      />
                      {platform.name}
                      <span className="small muted">{platform.format === "vertical" ? "9:16" : "16:9"}</span>
                    </label>
                  ))}
                </div>
              </Field>
            </div>
            <Field label="Fecha límite" hint="Opcional. Aparece en el calendario." error={fieldError("deadline")}>
              <input type="date" value={form.deadline} onChange={set("deadline")} />
            </Field>
          </div>
        </Panel>

        <Panel title="3 · Fuentes para la investigación">
          <div className="stack">
            <Notice tone="info">
              Scout solo lee las direcciones que tú permites: las de esta lista y las fuentes fijas de tu <Link to="/marca">kit de marca</Link>. No busca por su cuenta ni inventa referencias; si no hay fuentes, lo dirá.
            </Notice>
            {fieldError("references") ? (
              <div className="notice notice-bad" role="alert">
                <span>{fieldError("references")}</span>
              </div>
            ) : null}
            {references.map((row, index) => (
              <div key={index} className="row row-top">
                <div className="grow">
                  <Field label={`Enlace ${index + 1}`}>
                    <input
                      type="url"
                      value={row.url}
                      placeholder="https://… (artículo, página o feed RSS)"
                      maxLength={2000}
                      onChange={(event) => setReferences(references.map((item, i) => (i === index ? { ...item, url: event.target.value } : item)))}
                    />
                  </Field>
                </div>
                <div className="grow">
                  <Field label="Nota (opcional)">
                    <input
                      type="text"
                      value={row.note}
                      maxLength={300}
                      placeholder="Por qué es relevante"
                      onChange={(event) => setReferences(references.map((item, i) => (i === index ? { ...item, note: event.target.value } : item)))}
                    />
                  </Field>
                </div>
                {references.length > 1 ? (
                  <button type="button" className="icon-btn" style={{ marginTop: 24 }} aria-label={`Quitar enlace ${index + 1}`} onClick={() => setReferences(references.filter((_, i) => i !== index))}>
                    <Icon name="trash" size={14} />
                  </button>
                ) : null}
              </div>
            ))}
            {references.length < 12 ? (
              <div>
                <Button small icon="plus" onClick={() => setReferences([...references, { url: "", note: "" }])}>
                  Añadir otro enlace
                </Button>
              </div>
            ) : null}
          </div>
        </Panel>

        <Panel title="4 · Clips">
          <div className="stack">
            {template.needs_video ? (
              <Notice tone="info" icon="video">
                Esta plantilla parte de un vídeo tuyo. Lo subirás en el siguiente paso, dentro del proyecto, declarando que tienes derechos de uso. La aplicación no descarga vídeos de terceros.
              </Notice>
            ) : (
              <p className="soft">Cuando grabes el vídeo podrás subirlo al proyecto y Clipper propondrá los cortes con estos límites.</p>
            )}
            <div className="form-grid">
              <Field label="Número de clips a proponer">
                <input type="number" min={1} max={12} value={options.clip_count ?? 3} onChange={setOption("clip_count")} />
              </Field>
              <span />
              <Field label="Duración mínima (segundos)">
                <input type="number" min={3} max={600} value={options.clip_min_s ?? 15} onChange={setOption("clip_min_s")} />
              </Field>
              <Field label="Duración máxima (segundos)">
                <input type="number" min={5} max={1800} value={options.clip_max_s ?? 60} onChange={setOption("clip_max_s")} />
              </Field>
            </div>
          </div>
        </Panel>

        <div className="row row-end">
          <Link className="btn btn-ghost" to="/proyectos">
            <span>Cancelar</span>
          </Link>
          <Button type="submit" variant="primary" busy={busy} icon="play">
            Crear proyecto y empezar
          </Button>
        </div>
        <p className="small muted" style={{ textAlign: "right" }}>
          Al crear el proyecto, Scout empieza la investigación. {plural(platforms.length, "plataforma seleccionada", "plataformas seleccionadas")}.
        </p>
      </form>
    </>
  );
}
