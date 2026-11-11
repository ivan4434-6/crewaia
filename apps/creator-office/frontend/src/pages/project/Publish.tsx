// Pre-flight review and publications.
//
// Two separate human decisions gate everything that leaves the app: approving
// the content, and then confirming the publication itself. "Publicado" appears
// only when the platform confirmed it or a person declared it, and says which.

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api";
import type { Check, DocVersion, PlatformInfo, ProjectDetail, Publication, Step } from "../../api";
import { formatDateTime, formatDuration, fromLocalInput, plural, toLocalInput } from "../../format";
import { useAction, useResource, useSession } from "../../live";
import { Badge, Button, CheckList, ChipInput, Confirm, Counter, Empty, ErrorBox, Field, Icon, Loading, Modal, Notice } from "../../ui";
import { PlatformTag, RunProgress, WithPlaceholders } from "../shared";
import { jobFor, useProject } from "./context";

// --- Pre-flight ----------------------------------------------------------------

export function PreflightPanel({ step }: { step: Step }) {
  const { project, setProject, canEdit } = useProject();
  const { busy, run } = useAction();
  const output = step.output as { checks?: Check[]; counts?: Record<string, number> };
  const checks = output.checks ?? [];
  const areas: string[] = [];
  for (const item of checks) if (item.area && !areas.includes(item.area)) areas.push(item.area);
  const rerun = () => run("rerun", async () => setProject(await api.post<ProjectDetail>(`/api/projects/${project.id}/steps/preflight`, { action: "rerun" })), "Revisión relanzada.");

  return (
    <div className="stack stack-lg">
      <p className="soft">
        Antes de preparar la publicación, el orquestador comprueba con reglas (sin IA) el formato, los subtítulos, los textos, los derechos y los metadatos. Cada línea sale de los archivos exportados y de lo que has aprobado; nada se da por bueno sin
        medirlo.
      </p>
      {checks.length === 0 ? (
        step.status === "working" || step.status === "queued" ? null : (
          <Empty icon="check" title="La revisión aún no se ha hecho">
            Se ejecuta sola cuando el guion, las piezas de vídeo y los diseños están aprobados.
          </Empty>
        )
      ) : (
        <>
          <div className="row">
            <Badge tone="ok" icon="check">
              {plural(output.counts?.pass ?? 0, "correcta", "correctas")}
            </Badge>
            <Badge tone="warn" icon="warning">
              {plural(output.counts?.warn ?? 0, "aviso")}
            </Badge>
            <Badge tone="bad" icon="cross">
              {plural(output.counts?.fail ?? 0, "problema")}
            </Badge>
            {canEdit && project.state === "active" && ["completed", "blocked", "error"].includes(step.status) ? (
              <Button small icon="retry" busy={busy === "rerun"} onClick={() => void rerun()}>
                Volver a revisar
              </Button>
            ) : null}
          </div>
          <div className="split split-even">
            {areas.map((area) => (
              <div key={area} className="card stack stack-sm">
                <h4>{area}</h4>
                <CheckList checks={checks.filter((item) => item.area === area)} />
              </div>
            ))}
          </div>
          <p className="small muted">Los avisos no impiden publicar; los problemas sí, hasta que los resuelvas y la revisión se repita.</p>
        </>
      )}
    </div>
  );
}

// --- Publications --------------------------------------------------------------

export function PublishPanel({ step }: { step: Step }) {
  const { project } = useProject();
  const [showDiscarded, setShowDiscarded] = useState(false);
  const live = project.publications.filter((item) => item.status !== "discarded");
  const discarded = project.publications.filter((item) => item.status === "discarded");

  return (
    <div className="stack stack-lg">
      <Notice tone="info" icon="lock">
        Nada se publica por su cuenta. Cada publicación necesita dos decisiones tuyas: <strong>aprobar el contenido</strong> y, después, <strong>confirmar la publicación</strong>. Solo se usan las APIs oficiales de cada plataforma; si la cuenta no
        está conectada, tienes la exportación manual.
      </Notice>
      {live.length === 0 && discarded.length === 0 ? (
        step.status === "working" || step.status === "queued" ? null : (
          <Empty icon="megaphone" title="Aún no hay borradores de publicación">
            Publisher prepara un borrador por cada pieza y plataforma cuando la revisión previa se supera.
          </Empty>
        )
      ) : null}
      {live.map((publication) => (
        <PublicationCard key={publication.id} publication={publication} />
      ))}
      {discarded.length ? (
        <div className="stack">
          <button type="button" className="link-btn" onClick={() => setShowDiscarded(!showDiscarded)}>
            {showDiscarded ? "Ocultar" : "Mostrar"} descartadas ({discarded.length})
          </button>
          {showDiscarded ? discarded.map((publication) => <PublicationCard key={publication.id} publication={publication} />) : null}
        </div>
      ) : null}
    </div>
  );
}

const YOUTUBE_CATEGORIES: [string, string][] = [
  ["22", "Gente y blogs"],
  ["27", "Educación"],
  ["28", "Ciencia y tecnología"],
  ["26", "Consejos y estilo"],
  ["24", "Entretenimiento"],
  ["23", "Comedia"],
  ["20", "Videojuegos"],
  ["10", "Música"],
  ["17", "Deportes"],
  ["19", "Viajes y eventos"],
  ["25", "Noticias y política"],
  ["1", "Cine y animación"],
  ["2", "Motor"],
  ["15", "Mascotas y animales"],
];

const TIKTOK_PRIVACY: [string, string][] = [
  ["", "Elige quién puede verlo…"],
  ["PUBLIC_TO_EVERYONE", "Todo el mundo"],
  ["MUTUAL_FOLLOW_FRIENDS", "Amigos (seguidores mutuos)"],
  ["FOLLOWER_OF_CREATOR", "Seguidores"],
  ["SELF_ONLY", "Solo yo"],
];

type Options = Record<string, unknown>;

function OptionsEditor({ provider, options, onChange, disabled, hasCover }: { provider: string; options: Options; onChange: (options: Options) => void; disabled: boolean; hasCover: boolean }) {
  const set = (key: string, value: unknown) => onChange({ ...options, [key]: value });
  if (provider === "youtube") {
    return (
      <div className="stack stack-sm">
        <div className="form-grid">
          <Field label="Visibilidad" hint="Mientras Google no audite tu proyecto de API, YouTube deja como privados los vídeos subidos por API.">
            <select value={String(options.privacy ?? "private")} disabled={disabled} onChange={(event) => set("privacy", event.target.value)}>
              <option value="private">Privado</option>
              <option value="unlisted">Oculto (solo con enlace)</option>
              <option value="public">Público</option>
            </select>
          </Field>
          <Field label="Categoría">
            <select value={String(options.category_id ?? "22")} disabled={disabled} onChange={(event) => set("category_id", event.target.value)}>
              {YOUTUBE_CATEGORIES.map(([id, name]) => (
                <option key={id} value={id}>
                  {name}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <label className="check-row">
          <input type="checkbox" checked={Boolean(options.made_for_kids)} disabled={disabled} onChange={(event) => set("made_for_kids", event.target.checked)} />
          <span>Este vídeo está creado para niños (declaración que exige YouTube)</span>
        </label>
        {hasCover ? (
          <label className="check-row">
            <input type="checkbox" checked={options.set_thumbnail !== false} disabled={disabled} onChange={(event) => set("set_thumbnail", event.target.checked)} />
            <span>Subir también la miniatura (la cuenta debe tener las miniaturas personalizadas activadas)</span>
          </label>
        ) : null}
      </div>
    );
  }
  if (provider === "tiktok") {
    return (
      <div className="stack stack-sm">
        <Field label="Quién puede ver el vídeo" hint="TikTok exige elegirlo en cada publicación. Las apps sin auditar solo pueden publicar en «Solo yo»; las opciones reales de tu cuenta se comprueban al publicar.">
          <select value={String(options.privacy_level ?? "")} disabled={disabled} onChange={(event) => set("privacy_level", event.target.value)}>
            {TIKTOK_PRIVACY.map(([id, name]) => (
              <option key={id} value={id}>
                {name}
              </option>
            ))}
          </select>
        </Field>
        <div className="row">
          <label className="check-row">
            <input type="checkbox" checked={Boolean(options.disable_comment)} disabled={disabled} onChange={(event) => set("disable_comment", event.target.checked)} />
            <span>Sin comentarios</span>
          </label>
          <label className="check-row">
            <input type="checkbox" checked={Boolean(options.disable_duet)} disabled={disabled} onChange={(event) => set("disable_duet", event.target.checked)} />
            <span>Sin dúos</span>
          </label>
          <label className="check-row">
            <input type="checkbox" checked={Boolean(options.disable_stitch)} disabled={disabled} onChange={(event) => set("disable_stitch", event.target.checked)} />
            <span>Sin «stitch»</span>
          </label>
        </div>
      </div>
    );
  }
  return (
    <label className="check-row">
      <input type="checkbox" checked={options.share_to_feed !== false} disabled={disabled} onChange={(event) => set("share_to_feed", event.target.checked)} />
      <span>Mostrar también en el feed del perfil</span>
    </label>
  );
}

const EDITABLE_OPTIONS: Record<string, string[]> = {
  youtube: ["privacy", "made_for_kids", "category_id", "set_thumbnail"],
  tiktok: ["privacy_level", "disable_comment", "disable_duet", "disable_stitch"],
  instagram: ["share_to_feed"],
};

function pickOptions(publication: Publication): Options {
  const out: Options = {};
  for (const key of EDITABLE_OPTIONS[publication.provider] ?? []) if (key in publication.options) out[key] = publication.options[key];
  return out;
}

function statusBadge(publication: Publication) {
  if (publication.status === "published") {
    return publication.confirmation === "platform" ? (
      <Badge tone="ok" icon="check" title="La API de la plataforma devolvió el identificador de la publicación.">
        Publicado · confirmado por {publication.platform_name}
      </Badge>
    ) : (
      <Badge tone="info" icon="user" title="Una persona declaró haberlo publicado desde la app oficial. La aplicación no lo ha comprobado.">
        Publicado · declarado a mano
      </Badge>
    );
  }
  if (publication.status === "scheduled") return <Badge tone="info" icon="clock">Programado</Badge>;
  if (publication.status === "publishing") return <Badge tone="accent" icon="upload">Publicando</Badge>;
  if (publication.status === "error") return <Badge tone="bad" icon="warning">Error</Badge>;
  if (publication.status === "discarded") return <Badge icon="trash">Descartado</Badge>;
  return publication.approved ? <Badge tone="ok" icon="check">Borrador · contenido aprobado</Badge> : <Badge tone="warn" icon="eye">Borrador · sin aprobar</Badge>;
}

const SCHEDULE_TEXT: Record<string, string> = {
  platform: "Ya está subido: la propia plataforma lo publicará a esa hora.",
  connector: "Esta aplicación lo subirá a la cuenta conectada a esa hora (el servidor debe estar en marcha).",
  reminder: "Es un recordatorio: a esa hora te avisaremos para que lo publiques a mano.",
};

export function PublicationCard({ publication }: { publication: Publication }) {
  const { project, reload, canEdit } = useProject();
  const session = useSession();
  const spec = session.catalog.platforms.find((item) => item.key === publication.platform) as PlatformInfo;
  const { busy, run } = useAction();
  const [title, setTitle] = useState(publication.title);
  const [description, setDescription] = useState(publication.description);
  const [tags, setTags] = useState(publication.tags);
  const [options, setOptions] = useState<Options>(() => pickOptions(publication));
  const [designId, setDesignId] = useState(publication.design?.id ?? "");
  const [dialog, setDialog] = useState<"publish" | "schedule" | "manual" | "discard" | "history" | null>(null);

  // Reload the form when the server's copy changes (an agent rewrote it, someone else edited it).
  useEffect(() => {
    setTitle(publication.title);
    setDescription(publication.description);
    setTags(publication.tags);
    setOptions(pickOptions(publication));
    setDesignId(publication.design?.id ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [publication.updated_at]);

  const editable = canEdit && (publication.status === "draft" || publication.status === "error");
  const dirty =
    title !== publication.title ||
    description !== publication.description ||
    tags.join("\n") !== publication.tags.join("\n") ||
    JSON.stringify(options) !== JSON.stringify(pickOptions(publication)) ||
    designId !== (publication.design?.id ?? "");
  const readiness = publication.readiness;
  const job = jobFor(project, publication.id);
  const designs = project.designs.filter((design) => design.kind === spec.cover && design.url);

  const mutate = (name: string, request: () => Promise<unknown>, done?: string) =>
    run(
      name,
      async () => {
        await request();
        await reload();
      },
      done,
    );
  const action = (name: string, body: Record<string, unknown> = {}, done?: string) => mutate(name, () => api.post(`/api/publications/${publication.id}/actions`, { action: name, ...body }), done);
  const save = () =>
    mutate(
      "save",
      () => api.patch(`/api/publications/${publication.id}`, { title, description, tags, options, ...(designId ? { design_id: designId } : { clear_design: true }) }),
      publication.approved ? "Cambios guardados. Hay que volver a aprobar el contenido." : "Cambios guardados.",
    );

  const captionOnly = !spec.title_max || !spec.description_max;
  return (
    <article className="pub" aria-label={`Publicación de ${publication.platform_name}: ${publication.clip_title}`}>
      <header className="pub-head">
        <div className="row">
          <PlatformTag platform={publication.platform} name={publication.platform_name} />
          <span className="soft">· {publication.clip_title || "Pieza"}</span>
        </div>
        {statusBadge(publication)}
      </header>

      <div className="pub-body">
        <div className="stack stack-sm">
          {publication.render?.url ? (
            <video className={`player ${publication.render.format === "vertical" ? "player-vertical" : ""}`} controls preload="none" poster={publication.render.poster_url ?? undefined} src={publication.render.url} aria-label="Pieza de vídeo de esta publicación" />
          ) : (
            <div className="piece-preview">
              <span style={{ padding: 12 }}>Sin pieza de vídeo lista</span>
            </div>
          )}
          {publication.changed_after_publication.length ? (
            <span className="small" style={{ color: "#fbbf24" }}>
              Después de publicarla se cambió {publication.changed_after_publication.join(" y ")} de esta pieza: lo que ves aquí ya no es lo que salió
              {publication.published_media?.duration ? ` (salió un vídeo de ${formatDuration(publication.published_media.duration)})` : ""}.
            </span>
          ) : publication.render?.media ? (
            <span className="small soft">{formatDuration(publication.render.media.duration)}</span>
          ) : null}
          {publication.design?.url ? (
            <div className="stack stack-sm">
              <span className="small muted">{spec.cover === "thumbnail" ? "Miniatura" : "Portada"}</span>
              <img className="pub-cover" src={publication.design.url} alt={spec.cover === "thumbnail" ? "Miniatura" : "Portada"} />
            </div>
          ) : null}
        </div>

        <fieldset className="stack" disabled={!editable} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
          <legend className="sr-only">Contenido de la publicación</legend>
          {spec.title_max ? (
            <Field label={captionOnly ? "Texto de la publicación" : "Título"} counter={<Counter value={title.length} max={spec.title_max} />}>
              {captionOnly ? <textarea value={title} rows={4} onChange={(event) => setTitle(event.target.value)} /> : <input type="text" value={title} onChange={(event) => setTitle(event.target.value)} />}
            </Field>
          ) : null}
          {spec.description_max ? (
            <Field label={captionOnly ? "Texto de la publicación" : "Descripción"} counter={<Counter value={description.length} max={spec.description_max} />}>
              <textarea value={description} rows={captionOnly ? 5 : 6} onChange={(event) => setDescription(event.target.value)} />
            </Field>
          ) : null}
          {spec.tags_total_max ? (
            <Field label="Etiquetas" hint="Palabras clave del vídeo (no son hashtags)." counter={<Counter value={tags.join(",").length} max={spec.tags_total_max} />}>
              <ChipInput values={tags} onChange={setTags} placeholder="Añadir etiqueta" max={60} />
            </Field>
          ) : null}
          {designs.length || publication.design ? (
            <Field label={spec.cover === "thumbnail" ? "Miniatura" : "Portada"}>
              <select value={designId} onChange={(event) => setDesignId(event.target.value)}>
                <option value="">Sin portada (la plataforma elige un fotograma)</option>
                {designs.map((design) => (
                  <option key={design.id} value={design.id}>
                    {String(design.spec.headline ?? "Diseño")} · versión {design.version}
                  </option>
                ))}
              </select>
            </Field>
          ) : null}
          <OptionsEditor provider={publication.provider} options={options} onChange={setOptions} disabled={!editable} hasCover={Boolean(designId) && spec.cover === "thumbnail"} />
          {editable && dirty ? (
            <div className="row">
              <Button variant="primary" icon="check" busy={busy === "save"} onClick={() => void save()}>
                Guardar cambios
              </Button>
              <Button
                variant="ghost"
                onClick={() => {
                  setTitle(publication.title);
                  setDescription(publication.description);
                  setTags(publication.tags);
                  setOptions(pickOptions(publication));
                  setDesignId(publication.design?.id ?? "");
                }}
              >
                Deshacer
              </Button>
            </div>
          ) : null}
        </fieldset>

        <div className="stack">
          <div className="stack stack-sm">
            <h4>Requisitos</h4>
            <CheckList checks={publication.validation} compact />
            <span className="small muted">
              Límites de referencia de {publication.platform_name}; pueden cambiar.{" "}
              <a href={spec.docs_url} target="_blank" rel="noopener noreferrer">
                Documentación oficial
              </a>
            </span>
          </div>
          <div className="stack stack-sm">
            <h4>Cuenta</h4>
            {readiness.connected ? (
              <span className="row">
                <Badge tone="ok" icon="plug">
                  Conectada
                </Badge>
                <span className="small">{readiness.account_name}</span>
              </span>
            ) : (
              <Notice tone="warn" icon="plug" title={`${readiness.connection_label}.`} actions={<Link to="/integraciones">Abrir Integraciones</Link>}>
                {readiness.connection_detail} Mientras tanto puedes descargar el paquete, publicarlo tú desde la app oficial y marcarlo como publicado.
              </Notice>
            )}
          </div>
        </div>
      </div>

      <footer className="pub-status">
        <div className="grow stack stack-sm">
          <strong>{readiness.summary}.</strong>
          {publication.status === "scheduled" ? (
            <span className="small">
              Para el {formatDateTime(publication.scheduled_at)}. {SCHEDULE_TEXT[publication.schedule_mode] ?? ""}
            </span>
          ) : null}
          {publication.status === "published" ? (
            <span className="small">
              {formatDateTime(publication.published_at)}
              {publication.url ? (
                <>
                  {" · "}
                  <a href={publication.url} target="_blank" rel="noopener noreferrer">
                    Abrir en {publication.platform_name}
                  </a>
                </>
              ) : (
                " · sin enlace"
              )}
              {typeof publication.options.platform_visibility === "string" && publication.options.platform_visibility ? ` · visibilidad según la plataforma: ${publication.options.platform_visibility}` : ""}
            </span>
          ) : null}
          {typeof publication.options.platform_note === "string" && publication.options.platform_note ? <span className="small soft">{publication.options.platform_note}</span> : null}
          {publication.status === "error" && publication.error ? <span className="small" style={{ color: "#ff9db0" }}>{publication.error}</span> : null}
          {publication.status === "publishing" ? job ? <RunProgress run={job} /> : <span className="small soft">La plataforma está procesando el vídeo. El estado se comprueba solo cada pocos segundos.</span> : null}
          {readiness.blockers.length && publication.status === "draft" ? <span className="small" style={{ color: "#fbbf24" }}>Pendiente: {readiness.blockers.join(" · ")}</span> : null}
        </div>
        {canEdit ? (
          <div className="row">
            {editable && !publication.approved ? (
              <Button variant="primary" icon="check" busy={busy === "approve"} disabled={dirty || !readiness.content_ready} title={dirty ? "Guarda antes los cambios" : undefined} onClick={() => void action("approve", {}, "Contenido aprobado.")}>
                Aprobar contenido
              </Button>
            ) : null}
            {publication.status === "draft" && publication.approved ? (
              <>
                {readiness.can_publish_api ? (
                  <Button variant="primary" icon="send" disabled={dirty} onClick={() => setDialog("publish")}>
                    Publicar…
                  </Button>
                ) : null}
                <Button icon="calendar" disabled={dirty} onClick={() => setDialog("schedule")}>
                  Programar…
                </Button>
                <Button variant="ghost" busy={busy === "unapprove"} onClick={() => void action("unapprove", {}, "Aprobación retirada.")}>
                  Retirar aprobación
                </Button>
              </>
            ) : null}
            {publication.status === "error" && publication.approved && readiness.can_publish_api ? (
              <Button variant="primary" icon="retry" onClick={() => setDialog("publish")}>
                Reintentar…
              </Button>
            ) : null}
            {publication.status === "scheduled" && publication.schedule_mode !== "platform" ? (
              <Button icon="cross" busy={busy === "unschedule"} onClick={() => void action("unschedule", {}, "Publicación desprogramada.")}>
                Desprogramar
              </Button>
            ) : null}
            {readiness.can_export && !["publishing", "discarded"].includes(publication.status) ? (
              <a className="btn" href={`/api/publications/${publication.id}/export`} title="Vídeo, portada y textos en un zip para publicarlo tú">
                <Icon name="download" size={14} />
                <span>Paquete manual</span>
              </a>
            ) : null}
            {["draft", "error"].includes(publication.status) || (publication.status === "scheduled" && publication.schedule_mode !== "platform") ? (
              <Button icon="check" onClick={() => setDialog("manual")}>
                Ya la publiqué…
              </Button>
            ) : null}
            {publication.status === "discarded" ? (
              <Button icon="retry" busy={busy === "restore"} onClick={() => void action("restore", {}, "Publicación recuperada como borrador.")}>
                Recuperar
              </Button>
            ) : null}
            {["draft", "error"].includes(publication.status) || (publication.status === "scheduled" && publication.schedule_mode !== "platform") ? (
              <Button variant="ghost" icon="trash" onClick={() => setDialog("discard")}>
                Descartar
              </Button>
            ) : null}
            <button type="button" className="link-btn small" onClick={() => setDialog("history")}>
              Historial
            </button>
          </div>
        ) : null}
      </footer>

      {dialog === "publish" ? <PublishDialog publication={publication} onClose={() => setDialog(null)} onDone={reload} /> : null}
      {dialog === "schedule" ? <ScheduleDialog publication={publication} onClose={() => setDialog(null)} onDone={reload} /> : null}
      {dialog === "manual" ? <ManualDialog publication={publication} onClose={() => setDialog(null)} onDone={reload} /> : null}
      {dialog === "history" ? <PublicationHistory publication={publication} onClose={() => setDialog(null)} /> : null}
      {dialog === "discard" ? (
        <Confirm
          title="Descartar la publicación"
          confirmLabel="Descartar"
          danger
          busy={busy === "discard"}
          onClose={() => setDialog(null)}
          onConfirm={() => void action("discard", {}, "Publicación descartada.").then(() => setDialog(null))}
        >
          <p>El borrador de {publication.platform_name} deja de aparecer en el calendario. Podrás recuperarlo desde «Mostrar descartadas».</p>
        </Confirm>
      ) : null}
    </article>
  );
}

function Summary({ publication }: { publication: Publication }) {
  const text = publication.title || publication.description;
  return (
    <div className="card stack stack-sm">
      <PlatformTag platform={publication.platform} name={publication.platform_name} />
      <div className="prose">
        <WithPlaceholders text={text.length > 280 ? `${text.slice(0, 280)}…` : text} />
      </div>
      <span className="small soft">
        Pieza: {publication.clip_title || "vídeo"}
        {publication.render?.media ? ` · ${formatDuration(publication.render.media.duration)}` : ""}
        {publication.readiness.account_name ? ` · cuenta: ${publication.readiness.account_name}` : ""}
      </span>
    </div>
  );
}

function PublishDialog({ publication, onClose, onDone }: { publication: Publication; onClose: () => void; onDone: () => Promise<void> }) {
  const [confirmed, setConfirmed] = useState(false);
  const { busy, run } = useAction();
  const publish = () =>
    run(
      "publish",
      async () => {
        await api.post(`/api/publications/${publication.id}/actions`, { action: "publish", confirm: true });
        await onDone();
        onClose();
      },
      "Confirmado. Publisher está enviando el vídeo; verás aquí lo que responda la plataforma.",
    );
  return (
    <Modal
      title={`Publicar en ${publication.platform_name}`}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancelar
          </Button>
          <Button variant="primary" icon="send" busy={busy === "publish"} disabled={!confirmed} onClick={() => void publish()}>
            Publicar ahora
          </Button>
        </>
      }
    >
      <div className="stack">
        <p>Esta es la confirmación final. Al aceptar, el vídeo se envía a tu cuenta a través de la API oficial de {publication.platform_name}.</p>
        <Summary publication={publication} />
        {publication.readiness.warnings.length ? (
          <Notice tone="warn" title="Avisos pendientes:">
            {publication.readiness.warnings.join(" · ")}
          </Notice>
        ) : null}
        <label className="check-row">
          <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} data-autofocus />
          <span>
            <strong>Confirmo que quiero publicar este contenido ahora</strong> en la cuenta «{publication.readiness.account_name}».
          </span>
        </label>
        <p className="small muted">Solo se mostrará como «Publicado» cuando {publication.platform_name} lo confirme. Si la plataforma lo rechaza, verás su respuesta y nada quedará marcado como publicado.</p>
      </div>
    </Modal>
  );
}

function ScheduleDialog({ publication, onClose, onDone }: { publication: Publication; onClose: () => void; onDone: () => Promise<void> }) {
  const api_ok = publication.readiness.can_publish_api;
  const onPlatform = api_ok && publication.capabilities.schedule === "platform";
  const [mode, setMode] = useState<"platform" | "connector" | "reminder">(onPlatform ? "platform" : api_ok ? "connector" : "reminder");
  const [when, setWhen] = useState(() => toLocalInput(publication.scheduled_at ?? new Date(Date.now() + 24 * 3600 * 1000).toISOString()));
  const [confirmed, setConfirmed] = useState(false);
  const { busy, run } = useAction();
  const iso = fromLocalInput(when);
  const needsConfirm = mode !== "reminder";

  const submit = () =>
    run(
      "schedule",
      async () => {
        const body = mode === "platform" ? { action: "publish", confirm: true, mode: "platform", scheduled_at: iso } : { action: "schedule", mode, confirm: mode === "connector", scheduled_at: iso };
        await api.post(`/api/publications/${publication.id}/actions`, body);
        await onDone();
        onClose();
      },
      mode === "reminder" ? "Recordatorio programado." : "Programación confirmada.",
    );

  return (
    <Modal
      title={`Programar en ${publication.platform_name}`}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancelar
          </Button>
          <Button variant="primary" icon="calendar" busy={busy === "schedule"} disabled={!iso || (needsConfirm && !confirmed)} onClick={() => void submit()}>
            {mode === "reminder" ? "Programar recordatorio" : "Confirmar programación"}
          </Button>
        </>
      }
    >
      <div className="stack">
        <Summary publication={publication} />
        <Field label="Fecha y hora" hint="En tu zona horaria.">
          <input type="datetime-local" value={when} onChange={(event) => setWhen(event.target.value)} data-autofocus />
        </Field>
        <div className="stack stack-sm" role="radiogroup" aria-label="Cómo se publicará">
          {onPlatform ? (
            <label className="angle">
              <input type="radio" name="mode" checked={mode === "platform"} onChange={() => setMode("platform")} />
              <span>
                <strong>Subir ahora y que {publication.platform_name} lo publique a esa hora.</strong>
                <span className="small soft" style={{ display: "block" }}>
                  El vídeo se sube ya como privado con fecha de publicación. Después solo se puede cambiar desde la propia plataforma.
                </span>
              </span>
            </label>
          ) : null}
          {api_ok ? (
            <label className="angle">
              <input type="radio" name="mode" checked={mode === "connector"} onChange={() => setMode("connector")} />
              <span>
                <strong>Que esta aplicación lo suba a esa hora.</strong>
                <span className="small soft" style={{ display: "block" }}>
                  Necesita que el servidor esté en marcha y la cuenta siga conectada en ese momento. Puedes desprogramarlo antes.
                </span>
              </span>
            </label>
          ) : null}
          <label className="angle">
            <input type="radio" name="mode" checked={mode === "reminder"} onChange={() => setMode("reminder")} />
            <span>
              <strong>Solo un recordatorio: lo publicaré yo a mano.</strong>
              <span className="small soft" style={{ display: "block" }}>
                A esa hora recibirás un aviso. No se envía nada a la plataforma.
              </span>
            </span>
          </label>
        </div>
        {!api_ok ? (
          <Notice tone="info" icon="plug">
            {publication.readiness.connected ? "Esta publicación no puede salir por API desde este servidor" : `${publication.platform_name} está pendiente de conexión`}: solo está disponible el recordatorio.
          </Notice>
        ) : null}
        {needsConfirm ? (
          <label className="check-row">
            <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
            <span>
              <strong>Confirmo que quiero que se publique automáticamente</strong> en la cuenta «{publication.readiness.account_name}» en esa fecha.
            </span>
          </label>
        ) : null}
      </div>
    </Modal>
  );
}

function ManualDialog({ publication, onClose, onDone }: { publication: Publication; onClose: () => void; onDone: () => Promise<void> }) {
  const [url, setUrl] = useState("");
  const [when, setWhen] = useState(() => toLocalInput(new Date().toISOString()));
  const { busy, run } = useAction();
  const submit = () =>
    run(
      "manual",
      async () => {
        await api.post(`/api/publications/${publication.id}/actions`, { action: "mark_published", url: url.trim(), published_at: fromLocalInput(when) });
        await onDone();
        onClose();
      },
      "Marcada como publicada (declarada a mano).",
    );
  return (
    <Modal
      title="Marcar como publicada a mano"
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancelar
          </Button>
          <Button variant="primary" icon="check" busy={busy === "manual"} onClick={() => void submit()}>
            Declarar que está publicada
          </Button>
        </>
      }
    >
      <div className="stack">
        <p>
          Usa esto cuando hayas subido el vídeo tú desde la app oficial de {publication.platform_name}. Quedará como <strong>«Publicado · declarado a mano»</strong>: la aplicación no puede comprobarlo.
        </p>
        <Field label="Enlace a la publicación (recomendado)" hint="Sirve para abrirla desde aquí y para tu historial.">
          <input type="url" value={url} placeholder="https://…" maxLength={2000} onChange={(event) => setUrl(event.target.value)} data-autofocus />
        </Field>
        <Field label="Fecha y hora en que la publicaste">
          <input type="datetime-local" value={when} max={toLocalInput(new Date().toISOString())} onChange={(event) => setWhen(event.target.value)} />
        </Field>
        <p className="small muted">Las métricas y los comentarios de una publicación declarada a mano no se pueden leer por API: podrás añadirlos a mano en Analítica y Comunidad.</p>
      </div>
    </Modal>
  );
}

function PublicationHistory({ publication, onClose }: { publication: Publication; onClose: () => void }) {
  const versions = useResource<{ versions: DocVersion[] }>(() => api.get(`/api/publications/${publication.id}/versions`), [publication.id]);
  return (
    <Modal title={`Historial del texto · ${publication.platform_name}`} onClose={onClose} wide>
      {versions.loading && !versions.data ? <Loading /> : null}
      {versions.error ? <ErrorBox error={versions.error} onRetry={() => void versions.reload()} /> : null}
      {versions.data && versions.data.versions.length === 0 ? <p className="muted">Todavía no hay versiones guardadas.</p> : null}
      {(versions.data?.versions ?? []).map((version) => {
        const content = version.content as { title?: string; description?: string; tags?: string[] };
        return (
          <div key={version.id} className="version-row">
            <div className="grow">
              <div className="row">
                <strong>Versión {version.number}</strong>
                {version.is_current ? <Badge tone="accent">Actual</Badge> : null}
                <Badge>{version.author}</Badge>
                <span className="small soft">
                  {formatDateTime(version.created_at)}
                  {version.provider ? ` · ${version.provider}` : ""}
                </span>
              </div>
              {content.title ? <div className="prose">{content.title}</div> : null}
              {content.description ? <div className="prose small soft">{content.description}</div> : null}
              {content.tags?.length ? <div className="small muted">Etiquetas: {content.tags.join(", ")}</div> : null}
            </div>
          </div>
        );
      })}
    </Modal>
  );
}
