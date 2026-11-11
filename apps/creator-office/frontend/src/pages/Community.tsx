// Comments: classification and replies. Nothing is sent without a person's approval.

import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { PageHead, useOffice } from "../App";
import { api, query } from "../api";
import type { CommentItem, Publication } from "../api";
import { formatDateTime, plural } from "../format";
import { useAction, useResource, useSession, useToast } from "../live";
import { Badge, Button, Empty, ErrorBox, Field, Loading, Modal, Notice, Panel } from "../ui";
import type { Tone } from "../ui";
import { PlatformTag } from "./shared";

interface CommentSource {
  provider: string;
  name: string;
  state: "unsupported" | "not_connected" | "ready";
  detail: string;
  can_reply: boolean;
}

interface CommentsPayload {
  comments: CommentItem[];
  counts: Record<string, number>;
  total: number;
  sources: CommentSource[];
  reply_status_labels: Record<string, string>;
  automation: Automation;
}

interface Automation {
  sync_comments_hours: number;
  options: number[];
  last_run_at: string | null;
}

const CATEGORY_TONE: Record<string, Tone> = { question: "info", positive: "ok", spam: "bad", issue: "warn", other: "neutral" };
const CLASSIFIED_BY: Record<string, string> = { rules: "reglas de palabras clave", llm: "el modelo de IA", user: "una persona" };

export function CommunityPage() {
  const session = useSession();
  const office = useOffice();
  const [params, setParams] = useSearchParams();
  const projectId = params.get("proyecto") ?? "";
  const [category, setCategory] = useState("");
  const [replyStatus, setReplyStatus] = useState("");
  const [importing, setImporting] = useState(false);
  const { busy, run } = useAction();
  const comments = useResource<CommentsPayload>(
    () => api.get(`/api/comments${query({ project_id: projectId, category, reply_status: replyStatus })}`),
    [projectId, category, replyStatus, session.workspace.id],
    (message) => message.kind === "event" && (message.type.startsWith("comments.") || message.type.startsWith("reply.") || message.data.kind === "community"),
  );
  const projects = office.data?.projects_list ?? [];
  const data = comments.data;

  const sync = () =>
    run(
      "sync",
      async () => {
        await api.post(`/api/projects/${projectId}/comments/sync`);
      },
      "Sincronización en marcha: Community está leyendo los comentarios.",
    );

  return (
    <>
      <PageHead
        title="Comunidad"
        actions={
          session.canEdit ? (
            <>
              <Button icon="upload" disabled={!projectId} title={projectId ? undefined : "Elige antes un proyecto"} onClick={() => setImporting(true)}>
                Importar comentarios
              </Button>
              <Button icon="retry" disabled={!projectId} busy={busy === "sync"} title={projectId ? undefined : "Elige antes un proyecto"} onClick={() => void sync()}>
                Sincronizar
              </Button>
            </>
          ) : null
        }
      >
        Community clasifica los comentarios y sugiere respuestas con tu voz de marca. Tú decides qué se envía: ninguna respuesta sale sin tu aprobación.
      </PageHead>

      {data ? <Sources sources={data.sources} automation={data.automation} onChange={comments.reload} /> : null}

      <div className="filters">
        <Field label="Proyecto">
          <select value={projectId} onChange={(event) => setParams(event.target.value ? { proyecto: event.target.value } : {}, { replace: true })}>
            <option value="">Todos los proyectos</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.title}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Categoría">
          <select value={category} onChange={(event) => setCategory(event.target.value)}>
            <option value="">Todas{data ? ` (${data.total})` : ""}</option>
            <option value="pending">Sin clasificar{data?.counts.pending ? ` (${data.counts.pending})` : ""}</option>
            {Object.entries(session.catalog.comment_categories).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
                {data?.counts[key] ? ` (${data.counts[key]})` : ""}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Respuesta">
          <select value={replyStatus} onChange={(event) => setReplyStatus(event.target.value)}>
            <option value="">Cualquier estado</option>
            {Object.entries(data?.reply_status_labels ?? {}).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </Field>
      </div>

      {comments.error && !data ? <ErrorBox error={comments.error} onRetry={() => void comments.reload()} /> : null}
      {comments.loading && !data ? <Loading /> : null}
      {data && data.total === 0 ? (
        <Empty icon="chat" title="Todavía no hay comentarios">
          Llegan de dos formas: sincronizando una cuenta conectada que permita leerlos, o importándolos a mano (una línea por comentario, «autor: texto», o un CSV). Elige un proyecto para importar.
        </Empty>
      ) : null}
      {data && data.total > 0 && data.comments.length === 0 ? <p className="muted">Ningún comentario coincide con los filtros.</p> : null}
      <div className="stack">
        {(data?.comments ?? []).map((comment) => (
          <CommentCard key={comment.id} comment={comment} labels={data?.reply_status_labels ?? {}} onChange={comments.reload} showProject={!projectId} />
        ))}
      </div>
      {importing && projectId ? <ImportCommentsModal projectId={projectId} onClose={() => setImporting(false)} onDone={comments.reload} /> : null}
    </>
  );
}

function Sources({ sources, automation, onChange }: { sources: CommentSource[]; automation: Automation; onChange: () => Promise<void> }) {
  const session = useSession();
  const { busy, run } = useAction();
  const readable = sources.some((source) => source.state === "ready");
  const active = automation.sync_comments_hours > 0;
  const setRule = (hours: number) =>
    run(
      "rule",
      async () => {
        await api.put("/api/comments/automation", { sync_comments_hours: hours });
        await onChange();
      },
      hours ? `Community leerá los comentarios cada ${hours} horas.` : "Sincronización automática desactivada.",
    );
  return (
    <Panel title="De dónde salen los comentarios">
      <div className="grid-cards">
        {sources.map((source) => (
          <div key={source.provider} className="card stack stack-sm">
            <div className="row row-between">
              <PlatformTag platform={source.provider} name={source.name} />
              {source.state === "ready" ? (
                <Badge tone="ok" icon="check">
                  Lectura por API
                </Badge>
              ) : source.state === "not_connected" ? (
                <Badge tone="warn" icon="plug">
                  Pendiente de conexión
                </Badge>
              ) : (
                <Badge icon="info">Solo a mano</Badge>
              )}
            </div>
            <span className="small soft">{source.detail}</span>
            <span className="small muted">{source.state === "unsupported"
                ? "Importa los comentarios a mano y responde desde la app oficial."
                : !source.can_reply
                  ? "Las respuestas se envían desde la app oficial."
                  : source.state === "ready"
                    ? "Permite responder desde aquí, con tu confirmación."
                    : "Cuando la conectes podrás responder desde aquí, con tu confirmación. Hasta entonces, importa los comentarios a mano."}</span>
            {source.state === "not_connected" ? (
              <Link className="small" to="/integraciones">
                Abrir Integraciones
              </Link>
            ) : null}
          </div>
        ))}
      </div>
      <div className="row" style={{ marginTop: 14 }}>
        <label className="row" style={{ gap: 8 }}>
          <span className="field-label" style={{ margin: 0 }}>
            Lectura automática
          </span>
          <select
            style={{ width: "auto" }}
            value={automation.sync_comments_hours}
            disabled={!session.canEdit || busy !== null || (!readable && !active)}
            onChange={(event) => void setRule(Number(event.target.value))}
          >
            {automation.options.map((hours) => (
              <option key={hours} value={hours}>
                {hours ? `Cada ${hours} horas` : "Desactivada"}
              </option>
            ))}
          </select>
        </label>
        <span className="small muted">
          {!readable
            ? active
              ? "Regla activada, pero ahora mismo ninguna cuenta conectada permite leer comentarios: no se ejecuta."
              : "Disponible cuando conectes una cuenta que permita leer comentarios."
            : active
              ? `Community lee los comentarios de las publicaciones confirmadas.${automation.last_run_at ? ` Última comprobación: ${formatDateTime(automation.last_run_at)}.` : ""} Solo lee: las respuestas siguen necesitando tu aprobación.`
              : "Desactivada: los comentarios solo se leen cuando pulsas Sincronizar."}
        </span>
      </div>
    </Panel>
  );
}

function CommentCard({ comment, labels, onChange, showProject }: { comment: CommentItem; labels: Record<string, string>; onChange: () => Promise<void>; showProject: boolean }) {
  const session = useSession();
  const toast = useToast();
  const { busy, run } = useAction();
  const [reply, setReply] = useState(comment.reply_text);
  useEffect(() => setReply(comment.reply_text), [comment.reply_text]);

  const sent = comment.reply_status === "sent" || comment.reply_status === "sent_manual";
  const dirty = reply.trim() !== comment.reply_text;
  const hasGap = /\[[^\]\n]*\]/.test(reply);
  const mutate = (name: string, request: () => Promise<unknown>, done?: string) =>
    run(
      name,
      async () => {
        await request();
        await onChange();
      },
      done,
    );
  const replyAction = (action: string, done: string) => mutate(action, () => api.post(`/api/comments/${comment.id}/reply`, { action }), done);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(comment.reply_text);
      toast.ok("Respuesta copiada.");
    } catch {
      toast.info("No se pudo copiar automáticamente: selecciona el texto de la respuesta y cópialo.");
    }
  };

  return (
    <article className="comment">
      <div className="stack stack-sm">
        <div className="row">
          <strong>{comment.author || "Sin autor"}</strong>
          {comment.platform ? <PlatformTag platform={comment.platform} name={comment.platform_name} /> : <span className="small muted">Sin plataforma</span>}
          <Badge>{comment.source === "platform" ? "Leído de la plataforma" : "Importado a mano"}</Badge>
        </div>
        <div className="comment-text">{comment.text}</div>
        <div className="small muted">
          {comment.posted_at ? formatDateTime(comment.posted_at) : `Añadido el ${formatDateTime(comment.created_at)}`}
          {showProject && comment.project ? (
            <>
              {" · "}
              <Link to={`/proyectos/${comment.project_id}?paso=community`}>{comment.project}</Link>
            </>
          ) : null}
        </div>
        <div className="row">
          {comment.category ? (
            <Badge tone={CATEGORY_TONE[comment.category] ?? "neutral"}>{session.catalog.comment_categories[comment.category] ?? comment.category}</Badge>
          ) : (
            <Badge tone="warn" icon="clock">
              Sin clasificar
            </Badge>
          )}
          {session.canEdit ? (
            <label>
              <span className="sr-only">Cambiar categoría</span>
              <select
                value={comment.category}
                disabled={busy !== null}
                style={{ minHeight: 30, padding: "2px 6px", width: "auto" }}
                onChange={(event) => void mutate("category", () => api.patch(`/api/comments/${comment.id}`, { category: event.target.value }))}
              >
                {!comment.category ? <option value="">Clasificar…</option> : null}
                {Object.entries(session.catalog.comment_categories).map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
        </div>
        {comment.category ? (
          <span className="small muted">
            {comment.category_by === "user"
              ? comment.category_reason || "Clasificado a mano."
              : `Clasificado por ${CLASSIFIED_BY[comment.category_by] ?? comment.category_by}${comment.category_reason ? `: ${comment.category_reason}` : ""}`}
          </span>
        ) : null}
      </div>

      <div className="stack stack-sm">
        <div className="row row-between">
          <span className="field-label">Respuesta</span>
          <Badge tone={sent ? "ok" : comment.reply_status === "approved" ? "info" : comment.reply_status === "suggested" ? "warn" : "neutral"}>{labels[comment.reply_status] ?? comment.reply_status}</Badge>
        </div>
        <textarea
          value={reply}
          rows={4}
          maxLength={5000}
          disabled={!session.canEdit || sent}
          placeholder={comment.category === "spam" ? "El spam no lleva respuesta sugerida." : "Escribe una respuesta…"}
          aria-label={`Respuesta a ${comment.author || "este comentario"}`}
          onChange={(event) => setReply(event.target.value)}
        />
        {comment.reply_by && comment.reply_text ? <span className="small muted">Redactada por {comment.reply_by === "community" ? "Community (sugerencia)" : comment.reply_by}</span> : null}
        {hasGap && !sent ? <span className="small" style={{ color: "#fbbf24" }}>Quedan huecos entre [corchetes] por completar antes de aprobarla.</span> : null}
        {sent ? (
          <span className="small soft">
            {comment.reply_status === "sent" ? `${comment.platform_name} confirmó el envío` : "Declaraste haberla enviado a mano"}
            {comment.reply_sent_at ? ` el ${formatDateTime(comment.reply_sent_at)}` : ""}.
          </span>
        ) : session.canEdit ? (
          <div className="row">
            {dirty ? (
              <Button small variant="primary" busy={busy === "save"} onClick={() => void mutate("save", () => api.patch(`/api/comments/${comment.id}`, { reply_text: reply }), "Respuesta guardada.")}>
                Guardar texto
              </Button>
            ) : null}
            {!dirty && comment.reply_text && ["suggested", "none", "rejected"].includes(comment.reply_status) ? (
              <Button small variant="primary" icon="check" busy={busy === "approve"} disabled={hasGap} onClick={() => void replyAction("approve", "Respuesta aprobada. Todavía no se ha enviado.")}>
                Aprobar respuesta
              </Button>
            ) : null}
            {!dirty && comment.reply_status === "approved" ? (
              <>
                {comment.can_send ? (
                  <Button small variant="primary" icon="send" busy={busy === "send"} onClick={() => void replyAction("send", "Respuesta enviada y confirmada por la plataforma.")}>
                    Enviar por {comment.platform_name}
                  </Button>
                ) : null}
                <Button small icon="link" onClick={() => void copy()}>
                  Copiar
                </Button>
                <Button small icon="check" busy={busy === "mark_sent"} onClick={() => void replyAction("mark_sent", "Marcada como enviada a mano.")}>
                  Ya la envié a mano
                </Button>
              </>
            ) : null}
            {comment.reply_text && comment.reply_status !== "rejected" ? (
              <Button small variant="ghost" busy={busy === "reject"} onClick={() => void replyAction("reject", "Respuesta descartada.")}>
                Descartar
              </Button>
            ) : null}
          </div>
        ) : null}
        {!sent && comment.reply_status === "approved" && !comment.can_send ? (
          <span className="small muted">Este comentario no se puede responder por API: copia la respuesta, envíala desde la app oficial y márcala como enviada.</span>
        ) : null}
      </div>
    </article>
  );
}

export function ImportCommentsModal({ projectId, publications, onClose, onDone }: { projectId: string; publications?: Publication[]; onClose: () => void; onDone: () => Promise<void> }) {
  const session = useSession();
  const [text, setText] = useState("");
  const [platform, setPlatform] = useState("");
  const [publicationId, setPublicationId] = useState("");
  const { busy, run } = useAction();
  const toast = useToast();

  const submit = () =>
    run("import", async () => {
      const result = await api.post<{ added: number; duplicates: number }>(`/api/projects/${projectId}/comments/import`, { text, platform, publication_id: publicationId });
      toast.ok(`${plural(result.added, "comentario importado", "comentarios importados")}${result.duplicates ? ` (${result.duplicates} repetidos, omitidos)` : ""}. Community los clasificará ahora.`);
      await onDone();
      onClose();
    });

  const published = (publications ?? []).filter((item) => item.status === "published");
  return (
    <Modal
      title="Importar comentarios a mano"
      onClose={onClose}
      wide
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancelar
          </Button>
          <Button variant="primary" icon="upload" busy={busy === "import"} disabled={!text.trim()} onClick={() => void submit()}>
            Importar
          </Button>
        </>
      }
    >
      <div className="stack">
        <p className="soft">
          Pega los comentarios, uno por línea, con el formato <code>autor: texto</code>. También vale un CSV con las columnas <code>autor</code>, <code>texto</code> y, si quieres, <code>fecha</code>.
        </p>
        <Field label="Comentarios" counter={`${text.length}/200000`}>
          <textarea value={text} rows={9} maxLength={200000} placeholder={"marta_g: ¿Dónde puedo ver el vídeo completo?\nluis: Muy buen resumen, gracias"} onChange={(event) => setText(event.target.value)} data-autofocus />
        </Field>
        <div className="form-grid">
          {published.length ? (
            <Field label="Publicación a la que pertenecen" hint="Opcional.">
              <select value={publicationId} onChange={(event) => setPublicationId(event.target.value)}>
                <option value="">Sin asociar</option>
                {published.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.platform_name} · {(item.title || item.description).slice(0, 50)}
                  </option>
                ))}
              </select>
            </Field>
          ) : null}
          {!publicationId ? (
            <Field label="Plataforma" hint="Opcional.">
              <select value={platform} onChange={(event) => setPlatform(event.target.value)}>
                <option value="">Sin indicar</option>
                {session.catalog.platforms.map((item) => (
                  <option key={item.key} value={item.key}>
                    {item.name}
                  </option>
                ))}
              </select>
            </Field>
          ) : null}
        </div>
        <Notice tone="info" icon="info">
          Los comentarios importados a mano no se pueden responder por API: aprobarás la respuesta aquí y la enviarás tú desde la app oficial.
        </Notice>
      </div>
    </Modal>
  );
}
