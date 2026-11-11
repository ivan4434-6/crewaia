// The studio: the uploaded video and its clips, exported pieces and designs.

import { useEffect, useRef, useState } from "react";
import type { DragEvent, FormEvent, RefObject } from "react";
import { Link } from "react-router-dom";
import { ApiError, api, query, uploadFile } from "../../api";
import type { Asset, Clip, Design, ProjectDetail, RenderPiece, Step } from "../../api";
import { formatBytes, formatClock, formatDuration, formatPrecise, parseClock, plural } from "../../format";
import { useAction, useSession, useToast } from "../../live";
import { Badge, Button, CheckList, Confirm, Empty, Field, FileButton, Icon, Modal, Notice, ProgressBar } from "../../ui";
import type { Tone } from "../../ui";
import { RunProgress, Segmented } from "../shared";
import { FORMAT_LABELS, FRAMING_LABELS, jobFor, neededFormats, useProject } from "./context";

const VIDEO_ACCEPT = "video/*,.mp4,.mov,.m4v,.webm,.mkv,.avi";
const IMAGE_ACCEPT = "image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp";

type UploadResult = { project: ProjectDetail };

/** Uploads with measured progress and a way to stop. */
function useUpload() {
  const { setProject } = useProject();
  const session = useSession();
  const toast = useToast();
  const [state, setState] = useState<{ name: string; fraction: number } | null>(null);
  const abort = useRef<AbortController | null>(null);

  const start = async (url: string, file: File, done: string): Promise<boolean> => {
    const limit = session.limits.max_upload_mb * 1024 * 1024;
    if (file.size > limit) {
      toast.error(new ApiError(413, `«${file.name}» pesa ${formatBytes(file.size)} y el máximo de este servidor es ${session.limits.max_upload_mb} MB.`));
      return false;
    }
    abort.current = new AbortController();
    setState({ name: file.name, fraction: 0 });
    try {
      const result = await uploadFile<UploadResult>(url, file, (fraction) => setState({ name: file.name, fraction }), abort.current.signal);
      setProject(result.project);
      toast.ok(done);
      return true;
    } catch (error) {
      if (!(error instanceof ApiError && error.code === "aborted")) toast.error(error);
      return false;
    } finally {
      setState(null);
      abort.current = null;
    }
  };
  return { uploading: state, start, cancel: () => abort.current?.abort() };
}

function UploadProgress({ state, onCancel }: { state: { name: string; fraction: number }; onCancel: () => void }) {
  return (
    <div className="card stack stack-sm">
      <ProgressBar value={state.fraction} label={state.fraction >= 1 ? `Procesando «${state.name}» en el servidor…` : `Subiendo «${state.name}»`} />
      <div>
        <Button small icon="cross" onClick={onCancel} disabled={state.fraction >= 1}>
          Cancelar subida
        </Button>
      </div>
    </div>
  );
}

// --- Source video --------------------------------------------------------------

function SourceUploader({ replacing = false, onDone }: { replacing?: boolean; onDone?: () => void }) {
  const { project } = useProject();
  const session = useSession();
  const { uploading, start, cancel } = useUpload();
  const [rights, setRights] = useState(false);
  const [note, setNote] = useState("");
  const [over, setOver] = useState(false);

  const send = async (file: File) => {
    const ok = await start(`/api/projects/${project.id}/uploads${query({ kind: "source", filename: file.name, rights, rights_note: note })}`, file, "Vídeo subido.");
    if (ok) onDone?.();
  };
  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setOver(false);
    const file = event.dataTransfer.files[0];
    if (file && rights) void send(file);
  };

  if (uploading) return <UploadProgress state={uploading} onCancel={cancel} />;
  return (
    <div className="stack">
      {replacing ? <Notice tone="warn">Al sustituir el vídeo se eliminan los clips propuestos sobre el anterior y Clipper vuelve a analizarlo.</Notice> : null}
      <label className="check-row">
        <input type="checkbox" checked={rights} onChange={(event) => setRights(event.target.checked)} />
        <span>
          <strong>Declaro que tengo derechos de uso sobre este vídeo</strong> (lo he grabado yo o tengo licencia o permiso para publicarlo). Esta aplicación no descarga ni reutiliza vídeos de terceros.
        </span>
      </label>
      <Field label="Nota sobre los derechos (opcional)" hint="Por ejemplo: «grabación propia» o «licencia de…». Queda en el historial.">
        <input type="text" value={note} maxLength={500} onChange={(event) => setNote(event.target.value)} />
      </Field>
      <div
        className={`drop ${over ? "over-drop" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
      >
        <Icon name="upload" size={28} />
        <span>{rights ? "Arrastra aquí el vídeo o elige un archivo" : "Marca la declaración de derechos para poder subir el vídeo"}</span>
        <FileButton label={replacing ? "Elegir el vídeo nuevo" : "Elegir vídeo"} accept={VIDEO_ACCEPT} variant="primary" disabled={!rights} onFile={(file) => void send(file)} />
        <span className="small muted">mp4, mov, m4v, webm, mkv o avi · hasta {session.limits.max_upload_mb} MB</span>
      </div>
    </div>
  );
}

function SourceFacts({ source }: { source: Asset }) {
  const media = source.media;
  return (
    <dl className="kv">
      <dt>Archivo</dt>
      <dd>
        {source.filename} · {formatBytes(source.size)}
      </dd>
      {media ? (
        <>
          <dt>Duración</dt>
          <dd>{formatDuration(media.duration)}</dd>
          {media.video ? (
            <>
              <dt>Imagen</dt>
              <dd>
                {media.video.width}×{media.video.height} px · {media.video.codec}
                {media.video.fps ? ` · ${Math.round(media.video.fps * 100) / 100} fps` : ""}
              </dd>
            </>
          ) : null}
          <dt>Audio</dt>
          <dd>{media.audio ? media.audio.codec : "Sin pista de audio"}</dd>
        </>
      ) : (
        <>
          <dt>Análisis</dt>
          <dd className="soft">FFmpeg no está disponible: no se han podido medir duración ni resolución.</dd>
        </>
      )}
    </dl>
  );
}

function SourceBlock({ video }: { video: RefObject<HTMLVideoElement> }) {
  const { project, setProject, canEdit } = useProject();
  const session = useSession();
  const source = project.source as Asset;
  const { uploading, start, cancel } = useUpload();
  const { busy, run } = useAction();
  const [replacing, setReplacing] = useState(false);
  const transcription = session.capabilities.transcription;

  const setRights = (declared: boolean) =>
    run("rights", async () => {
      const result = await api.patch<UploadResult>(`/api/assets/${source.id}`, { rights_declared: declared, rights_note: source.rights_note });
      setProject(result.project);
    });

  return (
    <div className="split">
      <video ref={video} className="player" controls preload="metadata" src={source.url} poster={source.poster_url ?? undefined} aria-label="Vídeo original del proyecto" />
      <div className="stack">
        <SourceFacts source={source} />
        {source.rights_declared ? (
          <div className="row">
            <Badge tone="ok" icon="check">
              Derechos declarados
            </Badge>
            {source.rights_note ? <span className="small soft">{source.rights_note}</span> : null}
            {canEdit ? (
              <button type="button" className="link-btn small" disabled={busy === "rights"} onClick={() => void setRights(false)}>
                Retirar
              </button>
            ) : null}
          </div>
        ) : (
          <Notice
            tone="warn"
            title="Falta declarar los derechos de uso."
            actions={
              canEdit ? (
                <Button small variant="primary" busy={busy === "rights"} onClick={() => void setRights(true)}>
                  Declaro que tengo derechos sobre este vídeo
                </Button>
              ) : null
            }
          >
            Sin esa declaración no se analiza, exporta ni publica nada de este vídeo.
          </Notice>
        )}
        <div className="stack stack-sm">
          <h4>Transcripción</h4>
          {source.transcript.available ? (
            <span className="row">
              <Badge tone="ok" icon="check">
                {plural(source.transcript.segments, "línea")}
              </Badge>
              <span className="small soft">{source.transcript.source === "upload" ? "De los subtítulos que subiste." : "De la transcripción automática."}</span>
            </span>
          ) : (
            <span className="small soft">
              Sin transcripción: Clipper elige los cortes solo con señales medidas de audio e imagen y no puede saber qué se dice.{" "}
              {source.transcript.source === "api"
                ? "La transcripción automática ya analizó este vídeo y no detectó voz. Si la tiene, sube los subtítulos."
                : transcription.configured
                  ? `La transcripción automática (${transcription.model}) se usará la próxima vez que analice el vídeo.`
                  : `Transcripción automática pendiente de conexión (faltan ${transcription.missing.join(", ")}).`}
            </span>
          )}
          {uploading ? <UploadProgress state={uploading} onCancel={cancel} /> : null}
          {canEdit && !uploading ? (
            <div className="row">
              <FileButton
                small
                label={source.transcript.available ? "Sustituir subtítulos (.srt o .vtt)" : "Subir subtítulos (.srt o .vtt)"}
                accept=".srt,.vtt"
                onFile={(file) => void start(`/api/projects/${project.id}/uploads${query({ kind: "subtitles", filename: file.name })}`, file, "Subtítulos subidos.")}
              />
              <Button small variant="ghost" icon="upload" onClick={() => setReplacing(true)}>
                Sustituir vídeo
              </Button>
            </div>
          ) : null}
        </div>
      </div>
      {replacing ? (
        <Modal title="Sustituir el vídeo original" onClose={() => setReplacing(false)}>
          <SourceUploader replacing onDone={() => setReplacing(false)} />
        </Modal>
      ) : null}
    </div>
  );
}

// --- Clips ---------------------------------------------------------------------

const METHOD_TEXT: Record<string, string> = {
  signals: "Cortes elegidos a partir de señales medidas: pausas, cambios de plano y volumen.",
  uniform: "No se detectaron pausas, cambios de plano ni subtítulos: el vídeo se dividió en tramos iguales. Revisa los puntos de corte.",
  full: "El vídeo dura menos que el mínimo por clip: se propone entero.",
};

export function ClipsPanel({ step }: { step: Step }) {
  const { project, canEdit } = useProject();
  const video = useRef<HTMLVideoElement>(null);
  const stopAt = useRef<number | null>(null);
  const [time, setTime] = useState(0);
  const [active, setActive] = useState<string | null>(null);
  const source = project.source;
  const duration = source?.media?.duration ?? 0;
  const working = step.status === "working" || step.status === "queued";

  useEffect(() => {
    const node = video.current;
    if (!node) return;
    const onTime = () => {
      setTime(node.currentTime);
      if (stopAt.current !== null && node.currentTime >= stopAt.current) {
        node.pause();
        stopAt.current = null;
      }
    };
    node.addEventListener("timeupdate", onTime);
    node.addEventListener("seeked", onTime);
    return () => {
      node.removeEventListener("timeupdate", onTime);
      node.removeEventListener("seeked", onTime);
    };
  }, [source?.id]);

  const preview = (clip: Clip) => {
    const node = video.current;
    if (!node) return;
    setActive(clip.id);
    stopAt.current = clip.end;
    node.currentTime = clip.start;
    void node.play().catch(() => undefined);
    node.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  const seek = (seconds: number) => {
    const node = video.current;
    if (!node) return;
    stopAt.current = null;
    node.currentTime = seconds;
  };

  if (!source) {
    return canEdit ? (
      <div className="stack">
        <h4>Vídeo original</h4>
        <p className="soft">Clipper analiza el vídeo que subas y propone los cortes con sus marcas de tiempo. Si todavía no lo has grabado, hazlo a partir del guion y súbelo cuando lo tengas.</p>
        <SourceUploader />
      </div>
    ) : (
      <Empty icon="video" title="Aún no hay vídeo">
        Cuando alguien con permisos de edición suba el vídeo original, Clipper propondrá los cortes.
      </Empty>
    );
  }

  const output = step.output as { method?: string; notes?: string };
  const selected = project.clips.filter((clip) => clip.status === "selected").length;
  return (
    <div className="stack stack-lg">
      <div className="stack stack-sm">
        <h4>Vídeo original</h4>
        <SourceBlock video={video} />
      </div>

      {project.clips.length ? (
        <div className="stack">
          <div className="row row-between">
            <h4>
              Clips · {plural(selected, "seleccionado")} de {project.clips.length}
            </h4>
            <span className="small soft">Marca los que quieres exportar. Puedes ajustar el corte o añadir uno propio.</span>
          </div>
          {output.method && METHOD_TEXT[output.method] ? <p className="small soft">{METHOD_TEXT[output.method]}</p> : null}
          {duration > 0 ? <Timeline clips={project.clips} duration={duration} time={time} active={active} onSeek={seek} onPick={preview} /> : null}
          <div className="clip-list">
            {project.clips.map((clip) => (
              <ClipRow key={clip.id} clip={clip} active={active === clip.id} disabled={!canEdit || working} time={time} duration={duration} onPreview={() => preview(clip)} />
            ))}
          </div>
          {output.notes ? <p className="small soft prose">Notas de Clipper: {output.notes}</p> : null}
        </div>
      ) : !working && step.status !== "blocked" ? (
        <p className="muted">Todavía no hay clips propuestos.</p>
      ) : null}

      {canEdit && duration > 0 && !working ? <NewClip time={time} duration={duration} /> : null}
    </div>
  );
}

function Timeline({ clips, duration, time, active, onSeek, onPick }: { clips: Clip[]; duration: number; time: number; active: string | null; onSeek: (seconds: number) => void; onPick: (clip: Clip) => void }) {
  const bar = useRef<HTMLDivElement>(null);
  return (
    <div>
      <div
        className="timeline"
        ref={bar}
        role="presentation"
        onClick={(event) => {
          if (event.target !== bar.current) return;
          const rect = event.currentTarget.getBoundingClientRect();
          onSeek(((event.clientX - rect.left) / rect.width) * duration);
        }}
      >
        {clips.map((clip) => (
          <button
            key={clip.id}
            type="button"
            className={`timeline-clip ${clip.status} ${active === clip.id ? "active" : ""}`}
            style={{ left: `${(clip.start / duration) * 100}%`, width: `${((clip.end - clip.start) / duration) * 100}%` }}
            title={`${clip.title} · ${formatClock(clip.start)}–${formatClock(clip.end)}`}
            aria-label={`Previsualizar ${clip.title}, de ${formatClock(clip.start)} a ${formatClock(clip.end)}`}
            onClick={() => onPick(clip)}
          />
        ))}
        <span className="timeline-head" style={{ left: `${Math.min(100, (time / duration) * 100)}%` }} />
      </div>
      <div className="row row-between small muted" style={{ marginTop: 3 }}>
        <span>0:00</span>
        <span>Posición: {formatPrecise(time)}</span>
        <span>{formatClock(duration)}</span>
      </div>
    </div>
  );
}

function ClipRow({ clip, active, disabled, time, duration, onPreview }: { clip: Clip; active: boolean; disabled: boolean; time: number; duration: number; onPreview: () => void }) {
  const { project, setProject } = useProject();
  const { busy, run } = useAction();
  const toast = useToast();
  const [title, setTitle] = useState(clip.title);
  const [start, setStart] = useState(formatPrecise(clip.start));
  const [end, setEnd] = useState(formatPrecise(clip.end));
  const [removing, setRemoving] = useState(false);

  useEffect(() => setTitle(clip.title), [clip.title]);
  useEffect(() => {
    setStart(formatPrecise(clip.start));
    setEnd(formatPrecise(clip.end));
  }, [clip.start, clip.end]);

  const patch = (name: string, body: Record<string, unknown>, done?: string) => run(name, async () => setProject(await api.patch<ProjectDetail>(`/api/clips/${clip.id}`, body)), done);
  const trimmed = start !== formatPrecise(clip.start) || end !== formatPrecise(clip.end);
  const saveTrim = () => {
    const from = parseClock(start);
    const to = parseClock(end);
    if (from === null || to === null) {
      toast.error(new Error("Escribe los tiempos como minutos:segundos, por ejemplo 1:23.5"));
      return;
    }
    if (to - from < 1) {
      toast.error(new Error("El clip debe durar al menos un segundo y terminar después de empezar."));
      return;
    }
    void patch("trim", { start: from, end: to }, "Corte ajustado.");
  };
  const hasPieces = project.renders.some((piece) => piece.clip_id === clip.id);
  const picked = Boolean(clip.signals.picked);
  const facts = typeof clip.signals.facts === "string" ? clip.signals.facts : "";

  return (
    <div className={`clip ${clip.status} ${active ? "active" : ""}`}>
      <div className="row row-between row-top">
        <label className="check-row grow">
          <input
            type="checkbox"
            checked={clip.status === "selected"}
            disabled={disabled || busy !== null}
            onChange={(event) => void patch("status", { status: event.target.checked ? "selected" : "suggested" })}
            aria-label={`Usar el clip ${clip.title}`}
          />
          <input
            type="text"
            value={title}
            maxLength={200}
            disabled={disabled}
            aria-label="Título del clip"
            onChange={(event) => setTitle(event.target.value)}
            onBlur={() => {
              if (title.trim() && title.trim() !== clip.title) void patch("title", { title });
              else setTitle(clip.title);
            }}
          />
        </label>
        <div className="row">
          {clip.origin === "user" ? <Badge tone="info">Marcado por ti</Badge> : picked ? <Badge tone="accent">Recomendado por Clipper</Badge> : <Badge>Alternativa</Badge>}
          {clip.status === "rejected" ? <Badge tone="bad">Descartado</Badge> : null}
        </div>
      </div>
      <div className="clip-times">
        <label>
          <span className="sr-only">Inicio</span>
          <input type="text" inputMode="decimal" value={start} disabled={disabled} onChange={(event) => setStart(event.target.value)} aria-label="Inicio del clip (minutos:segundos)" />
        </label>
        <span aria-hidden="true">→</span>
        <label>
          <span className="sr-only">Fin</span>
          <input type="text" inputMode="decimal" value={end} disabled={disabled} onChange={(event) => setEnd(event.target.value)} aria-label="Fin del clip (minutos:segundos)" />
        </label>
        <span className="small soft">{formatDuration(clip.duration)}</span>
        {!disabled ? (
          <>
            <Button small title="Usar la posición actual del reproductor como inicio" onClick={() => setStart(formatPrecise(Math.min(time, Math.max(0, duration - 1))))}>
              Inicio = posición
            </Button>
            <Button small title="Usar la posición actual del reproductor como fin" onClick={() => setEnd(formatPrecise(time))}>
              Fin = posición
            </Button>
          </>
        ) : null}
        {trimmed && !disabled ? (
          <Button small variant="primary" busy={busy === "trim"} onClick={saveTrim}>
            Guardar corte
          </Button>
        ) : null}
        <Button small icon="play" onClick={onPreview}>
          Ver
        </Button>
      </div>
      {clip.reason ? (
        <div className="small">
          <strong>{picked ? "Por qué puede funcionar: " : clip.origin === "user" ? "" : "Medido: "}</strong>
          {clip.reason}
        </div>
      ) : null}
      {facts && facts !== clip.reason ? <div className="small soft">Medido: {facts}</div> : null}
      {clip.excerpt ? <div className="quote small">«{clip.excerpt}»</div> : null}
      {!disabled ? (
        <div className="row small">
          {clip.status !== "rejected" ? (
            <button type="button" className="link-btn" onClick={() => void patch("status", { status: "rejected" })}>
              Descartar
            </button>
          ) : (
            <button type="button" className="link-btn" onClick={() => void patch("status", { status: "suggested" })}>
              Recuperar
            </button>
          )}
          <button type="button" className="link-btn" onClick={() => setRemoving(true)}>
            Eliminar
          </button>
        </div>
      ) : null}
      {removing ? (
        <Confirm
          title="Eliminar el clip"
          confirmLabel="Eliminar clip"
          danger
          busy={busy === "delete"}
          onClose={() => setRemoving(false)}
          onConfirm={() => void run("delete", async () => setProject(await api.delete<ProjectDetail>(`/api/clips/${clip.id}`)), "Clip eliminado.").then(() => setRemoving(false))}
        >
          <p>
            Se eliminará «{clip.title}»{hasPieces ? " junto con sus piezas exportadas y los borradores de publicación que las usan" : ""}. El vídeo original no se toca.
          </p>
        </Confirm>
      ) : null}
    </div>
  );
}

function NewClip({ time, duration }: { time: number; duration: number }) {
  const { project, setProject } = useProject();
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [title, setTitle] = useState("");
  const { busy, run } = useAction();
  const toast = useToast();

  const create = (event: FormEvent) => {
    event.preventDefault();
    const from = parseClock(start);
    const to = parseClock(end);
    if (from === null || to === null || to - from < 1) {
      toast.error(new Error("Indica inicio y fin (minutos:segundos). El clip debe durar al menos un segundo."));
      return;
    }
    if (from >= duration) {
      toast.error(new Error(`El inicio queda fuera del vídeo, que dura ${formatClock(duration)}.`));
      return;
    }
    void run(
      "create",
      async () => {
        setProject(await api.post<ProjectDetail>(`/api/projects/${project.id}/clips`, { start: from, end: to, title }));
        setStart("");
        setEnd("");
        setTitle("");
      },
      "Clip añadido y seleccionado.",
    );
  };

  return (
    <details>
      <summary>Marcar un clip propio</summary>
      <form className="stack stack-sm" onSubmit={create}>
        <p className="small soft">Coloca el reproductor donde quieras y usa «= posición», o escribe los tiempos como minutos:segundos.</p>
        <div className="clip-times">
          <input type="text" inputMode="decimal" value={start} placeholder="0:00.0" aria-label="Inicio del clip nuevo" onChange={(event) => setStart(event.target.value)} />
          <Button small onClick={() => setStart(formatPrecise(time))}>
            Inicio = posición
          </Button>
          <span aria-hidden="true">→</span>
          <input type="text" inputMode="decimal" value={end} placeholder="0:30.0" aria-label="Fin del clip nuevo" onChange={(event) => setEnd(event.target.value)} />
          <Button small onClick={() => setEnd(formatPrecise(time))}>
            Fin = posición
          </Button>
        </div>
        <div className="row">
          <input type="text" style={{ flex: "1 1 220px" }} value={title} maxLength={200} placeholder="Título del clip (opcional)" aria-label="Título del clip nuevo" onChange={(event) => setTitle(event.target.value)} />
          <Button type="submit" icon="plus" busy={busy === "create"}>
            Añadir clip
          </Button>
        </div>
      </form>
    </details>
  );
}

// --- Exports -------------------------------------------------------------------

const PIECE_STATUS: Record<RenderPiece["status"], { label: string; tone: Tone }> = {
  pending: { label: "Sin exportar", tone: "neutral" },
  queued: { label: "En cola", tone: "info" },
  rendering: { label: "Exportando", tone: "accent" },
  ready: { label: "Lista", tone: "ok" },
  error: { label: "Error", tone: "bad" },
  stale: { label: "Desactualizada", tone: "warn" },
};

export function EditPanel({ step }: { step: Step }) {
  const { project, canEdit } = useProject();
  const session = useSession();
  const formats = neededFormats(project, session.catalog.platforms);
  const [uploading, setUploading] = useState(false);
  const clips = project.clips.filter((clip) => clip.status === "selected" || project.renders.some((piece) => piece.clip_id === clip.id));
  const working = step.status === "working" || step.status === "queued";

  return (
    <div className="stack stack-lg">
      {!session.capabilities.ffmpeg.available ? (
        <Notice tone="warn" title="FFmpeg no está disponible en este servidor.">
          Editor no puede exportar aquí. Alternativa: descarga el vídeo original, edita cada clip con tu programa y sube las piezas terminadas con «Subir pieza ya editada».
        </Notice>
      ) : null}
      <div className="row row-between">
        <p className="soft grow">
          Formatos que necesitan tus plataformas: <strong>{formats.map((format) => FORMAT_LABELS[format]).join(" · ") || "ninguno"}</strong>. Las piezas se exportan en H.264 con el audio original; no se añade música ni material externo.
        </p>
        {canEdit ? (
          <Button icon="upload" onClick={() => setUploading(true)}>
            Subir pieza ya editada
          </Button>
        ) : null}
      </div>
      {clips.length === 0 ? (
        <Empty icon="scissors" title="No hay clips seleccionados">
          Selecciona al menos un clip en el paso de Clipper, o sube una pieza que ya hayas editado.
        </Empty>
      ) : null}
      {clips.map((clip) => {
        const mine = project.renders.filter((piece) => piece.clip_id === clip.id);
        const wanted = clip.status === "selected" ? formats : [];
        const all = [...wanted, ...mine.map((piece) => piece.format).filter((format) => !wanted.includes(format))];
        return (
          <div key={clip.id} className="stack stack-sm">
            <h4>
              {clip.title} · {formatClock(clip.start)}–{formatClock(clip.end)} · {formatDuration(clip.duration)}
              {clip.status !== "selected" ? " · no seleccionado" : ""}
            </h4>
            <div className="pieces">
              {all.map((format) => (
                <PieceCard key={format} clip={clip} format={format} piece={mine.find((item) => item.format === format)} locked={!canEdit || working} />
              ))}
            </div>
          </div>
        );
      })}
      {uploading ? <PieceUpload formats={formats} onClose={() => setUploading(false)} /> : null}
    </div>
  );
}

function PieceCard({ clip, format, piece, locked }: { clip: Clip; format: string; piece: RenderPiece | undefined; locked: boolean }) {
  const { project, setProject, reload } = useProject();
  const session = useSession();
  const { busy, run } = useAction();
  const [exporting, setExporting] = useState(false);
  const [removing, setRemoving] = useState(false);
  const job = piece ? jobFor(project, piece.id) : undefined;
  const status = piece ? PIECE_STATUS[piece.status] : PIECE_STATUS.pending;
  const inProgress = piece ? piece.status === "queued" || piece.status === "rendering" : false;
  const canExport = session.capabilities.ffmpeg.available && clip.status === "selected";
  const settings = (piece?.settings ?? {}) as { framing?: string; subtitles_burned?: boolean; uploaded?: boolean };

  return (
    <div className="piece">
      <div className="row row-between">
        <strong className="pixel">{FORMAT_LABELS[format] ?? format}</strong>
        <Badge tone={status.tone}>{status.label}</Badge>
      </div>
      <div className="piece-preview">
        {piece?.url ? (
          <video controls preload="none" poster={piece.poster_url ?? undefined} src={piece.url} aria-label={`Pieza ${FORMAT_LABELS[format] ?? format} de ${clip.title}`} />
        ) : (
          <span style={{ padding: 16 }}>{inProgress ? "Exportando…" : "Sin exportar"}</span>
        )}
      </div>
      {job ? (
        <div className="stack stack-sm">
          <RunProgress run={job} />
          {!locked ? (
            <div>
              <Button
                small
                icon="stop"
                busy={busy === "cancel"}
                onClick={() =>
                  void run("cancel", async () => {
                    await api.post(`/api/runs/${job.id}/cancel`);
                    await reload();
                  }, "Exportación cancelada.")
                }
              >
                Cancelar exportación
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}
      {piece?.status === "stale" ? <span className="small" style={{ color: "#fbbf24" }}>El corte del clip cambió después de exportar esta pieza: vuelve a exportarla.</span> : null}
      {piece?.error ? <span className="small" style={{ color: "#ff9db0" }}>{piece.error}</span> : null}
      {piece?.media?.video ? (
        <span className="small soft">
          {piece.media.video.width}×{piece.media.video.height} · {formatDuration(piece.media.duration)} · {formatBytes(piece.size)} ·{" "}
          {piece.origin === "upload" ? "subida por ti" : `${FRAMING_LABELS[settings.framing ?? "blur"] ?? settings.framing}${settings.subtitles_burned ? " · con subtítulos" : ""}`}
        </span>
      ) : null}
      {piece && piece.checks.length ? (
        <details open={piece.checks.some((item) => item.status === "fail" || item.status === "warn")}>
          <summary>Lista de comprobación</summary>
          <CheckList checks={piece.checks} compact />
        </details>
      ) : null}
      <div className="row">
        {!locked && canExport && !inProgress ? (
          <Button small variant={piece?.status === "ready" ? "secondary" : "primary"} icon={piece?.url ? "retry" : "play"} onClick={() => setExporting(true)}>
            {piece?.url ? "Volver a exportar" : "Exportar"}
          </Button>
        ) : null}
        {piece?.url ? (
          <a className="btn btn-small" href={`${piece.url}?download=1`}>
            <Icon name="download" size={12} />
            <span>Descargar</span>
          </a>
        ) : null}
        {piece && !locked && !inProgress ? (
          <Button small variant="ghost" icon="trash" onClick={() => setRemoving(true)}>
            Eliminar
          </Button>
        ) : null}
      </div>
      {exporting ? <ExportModal clip={clip} format={format} piece={piece} onClose={() => setExporting(false)} /> : null}
      {removing && piece ? (
        <Confirm
          title="Eliminar la pieza"
          confirmLabel="Eliminar pieza"
          danger
          busy={busy === "delete"}
          onClose={() => setRemoving(false)}
          onConfirm={() => void run("delete", async () => setProject(await api.delete<ProjectDetail>(`/api/renders/${piece.id}`)), "Pieza eliminada.").then(() => setRemoving(false))}
        >
          <p>Se borrará el archivo exportado y los borradores de publicación que lo usan. No afecta a nada ya publicado.</p>
        </Confirm>
      ) : null}
    </div>
  );
}

function ExportModal({ clip, format, piece, onClose }: { clip: Clip; format: string; piece: RenderPiece | undefined; onClose: () => void }) {
  const { project, setProject } = useProject();
  const previous = (piece?.settings ?? {}) as { framing?: string; subtitles?: boolean; offset_x?: number; offset_y?: number };
  const hasTranscript = Boolean(project.source?.transcript.available);
  const [framing, setFraming] = useState<"blur" | "crop" | "pad">((previous.framing as "blur" | "crop" | "pad") ?? "blur");
  const [subtitles, setSubtitles] = useState(previous.subtitles ?? hasTranscript);
  const [offsetX, setOffsetX] = useState(previous.offset_x ?? 0.5);
  const [offsetY, setOffsetY] = useState(previous.offset_y ?? 0.5);
  const { busy, run } = useAction();

  const submit = () =>
    run(
      "export",
      async () => {
        setProject(await api.post<ProjectDetail>(`/api/clips/${clip.id}/renders`, { format, framing, subtitles: subtitles && hasTranscript, offset_x: offsetX, offset_y: offsetY }));
        onClose();
      },
      "Exportación en cola: Editor se pone con ella.",
    );

  return (
    <Modal
      title={`Exportar «${clip.title}» · ${FORMAT_LABELS[format] ?? format}`}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancelar
          </Button>
          <Button variant="primary" icon="play" busy={busy === "export"} onClick={() => void submit()}>
            Exportar
          </Button>
        </>
      }
    >
      <div className="stack">
        <div className="stack stack-sm">
          <span className="field-label">Encuadre</span>
          <Segmented
            label="Encuadre"
            value={framing}
            onChange={setFraming}
            options={[
              { id: "blur", label: "Fondo desenfocado" },
              { id: "crop", label: "Recortar" },
              { id: "pad", label: "Bandas negras" },
            ]}
          />
          <span className="small soft">
            {framing === "blur"
              ? "Se ve la imagen completa; el hueco se rellena con la propia imagen desenfocada."
              : framing === "crop"
                ? "Llena todo el encuadre recortando lo que sobra. Elige qué parte se conserva."
                : "Se ve la imagen completa con bandas negras."}
          </span>
        </div>
        {framing === "crop" ? (
          <div className="form-grid">
            <Field label={`Posición horizontal del recorte: ${Math.round(offsetX * 100)} %`} hint="0 % = izquierda, 100 % = derecha.">
              <input type="range" min={0} max={1} step={0.05} value={offsetX} onChange={(event) => setOffsetX(Number(event.target.value))} />
            </Field>
            <Field label={`Posición vertical del recorte: ${Math.round(offsetY * 100)} %`} hint="0 % = arriba, 100 % = abajo.">
              <input type="range" min={0} max={1} step={0.05} value={offsetY} onChange={(event) => setOffsetY(Number(event.target.value))} />
            </Field>
          </div>
        ) : null}
        <label className="check-row">
          <input type="checkbox" checked={subtitles && hasTranscript} disabled={!hasTranscript} onChange={(event) => setSubtitles(event.target.checked)} />
          <span>
            Incrustar subtítulos dentro de la zona segura
            {!hasTranscript ? <span className="soft"> — no disponible: no hay transcripción ni subtítulos subidos para este vídeo.</span> : null}
          </span>
        </label>
        <p className="small muted">Salida: {FORMAT_LABELS[format] ?? format}, H.264 + AAC. Al terminar se mide el archivo y se comprueba contra los requisitos de cada plataforma.</p>
      </div>
    </Modal>
  );
}

function PieceUpload({ formats, onClose }: { formats: string[]; onClose: () => void }) {
  const { project } = useProject();
  const { uploading, start, cancel } = useUpload();
  const [format, setFormat] = useState(formats[0] ?? "vertical");
  const [clipId, setClipId] = useState("");
  const [rights, setRights] = useState(false);

  const send = async (file: File) => {
    const ok = await start(`/api/projects/${project.id}/uploads${query({ kind: "piece", filename: file.name, format, clip_id: clipId, rights })}`, file, "Pieza subida.");
    if (ok) onClose();
  };

  return (
    <Modal title="Subir una pieza ya editada" onClose={onClose}>
      <div className="stack">
        <p className="soft">Si has editado un clip con tu propio programa, súbelo aquí. Se medirá y pasará por las mismas comprobaciones que una pieza exportada por Editor.</p>
        <div className="form-grid">
          <Field label="Formato de la pieza">
            <select value={format} onChange={(event) => setFormat(event.target.value)}>
              {Object.entries(FORMAT_LABELS).map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Corresponde al clip" hint="Si eliges un clip, sustituye su pieza en ese formato.">
            <select value={clipId} onChange={(event) => setClipId(event.target.value)}>
              <option value="">Pieza nueva (sin clip)</option>
              {project.clips.map((clip) => (
                <option key={clip.id} value={clip.id}>
                  {clip.title}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <label className="check-row">
          <input type="checkbox" checked={rights} onChange={(event) => setRights(event.target.checked)} />
          <span>
            <strong>Declaro que tengo derechos de uso sobre todo el contenido de esta pieza</strong>, incluidos la música y los recursos que le haya añadido.
          </span>
        </label>
        {uploading ? <UploadProgress state={uploading} onCancel={cancel} /> : <FileButton label="Elegir archivo de vídeo" accept={VIDEO_ACCEPT} variant="primary" disabled={!rights} onFile={(file) => void send(file)} />}
      </div>
    </Modal>
  );
}

// --- Designs -------------------------------------------------------------------

const DESIGN_NAMES: Record<string, string> = { thumbnail: "Miniatura para YouTube (1280×720)", cover: "Portada vertical (1080×1920)" };
const LAYOUT_NAMES: Record<string, string> = { side: "Texto a un lado", band: "Banda inferior", center: "Centrado" };

export function DesignPanel({ step }: { step: Step }) {
  const { project, canEdit } = useProject();
  const session = useSession();
  const needed: string[] = [];
  for (const key of project.platforms) {
    const kind = session.catalog.platforms.find((platform) => platform.key === key)?.cover;
    if (kind && !needed.includes(kind)) needed.push(kind);
  }
  const missing = needed.filter((kind) => !project.designs.some((design) => design.kind === kind));
  const working = step.status === "working" || step.status === "queued";

  return (
    <div className="stack stack-lg">
      <p className="soft">
        Designer compone con tu <Link to="/marca">kit de marca</Link> (colores, tipografía y logotipo) y mide el resultado: contraste, tamaño del texto y zona segura. Las zonas seguras son márgenes aproximados, porque ninguna plataforma publica un
        diseño fijo de su interfaz.
      </p>
      {project.designs.length === 0 && !working && !missing.length ? <p className="muted">Todavía no hay diseños.</p> : null}
      {project.designs.map((design) => (
        <DesignEditor key={design.id} design={design} locked={!canEdit || working} />
      ))}
      {canEdit && !working ? missing.map((kind) => <DesignCreator key={kind} kind={kind} />) : null}
    </div>
  );
}

function DesignCreator({ kind }: { kind: string }) {
  const { project, setProject } = useProject();
  const [headline, setHeadline] = useState("");
  const { busy, run } = useAction();
  const create = (event: FormEvent) => {
    event.preventDefault();
    void run("create", async () => setProject(await api.post<ProjectDetail>(`/api/projects/${project.id}/designs`, { kind, headline })), "Diseño creado.");
  };
  return (
    <form className="card stack stack-sm" onSubmit={create}>
      <strong className="pixel">{DESIGN_NAMES[kind] ?? kind}</strong>
      <span className="small soft">Aún no existe. Puedes esperar a Designer o crearlo tú con un titular.</span>
      <div className="row">
        <input type="text" style={{ flex: "1 1 240px" }} value={headline} maxLength={90} required placeholder="Titular (pocas palabras)" aria-label="Titular del diseño" onChange={(event) => setHeadline(event.target.value)} />
        <Button type="submit" icon="plus" busy={busy === "create"}>
          Crear
        </Button>
      </div>
    </form>
  );
}

function DesignEditor({ design, locked }: { design: Design; locked: boolean }) {
  const { project, setProject } = useProject();
  const session = useSession();
  const toast = useToast();
  const spec = design.spec as { headline?: string; layout?: string; font?: string; background?: string; background_asset_id?: string; frame_time?: number };
  const [headline, setHeadline] = useState(spec.headline ?? "");
  const [layout, setLayout] = useState(spec.layout ?? "side");
  const [font, setFont] = useState(spec.font ?? session.catalog.fonts[0]);
  const [background, setBackground] = useState(spec.background ?? "gradient");
  const [assetId, setAssetId] = useState(spec.background_asset_id ?? "");
  const [frameTime, setFrameTime] = useState(spec.frame_time ?? 0);
  const { busy, run } = useAction();
  const [imageUpload, setImageUpload] = useState<number | null>(null);
  const duration = project.source?.media?.duration ?? 0;
  const hasVideo = Boolean(project.source?.media?.video) && session.capabilities.ffmpeg.available;

  useEffect(() => {
    setHeadline(spec.headline ?? "");
    setLayout(spec.layout ?? "side");
    setFont(spec.font ?? session.catalog.fonts[0]);
    setBackground(spec.background ?? "gradient");
    setAssetId(spec.background_asset_id ?? "");
    setFrameTime(spec.frame_time ?? 0);
    // Reset the form whenever a new composition arrives.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [design.version, design.updated_at]);

  const dirty =
    headline !== (spec.headline ?? "") ||
    layout !== (spec.layout ?? "side") ||
    font !== (spec.font ?? session.catalog.fonts[0]) ||
    background !== (spec.background ?? "gradient") ||
    (background === "asset" && assetId !== (spec.background_asset_id ?? "")) ||
    (background === "frame" && frameTime !== (spec.frame_time ?? 0));

  const apply = () => {
    if (background === "asset" && !assetId) {
      toast.error(new Error("Elige o sube la imagen de fondo."));
      return;
    }
    const body: Record<string, unknown> = { headline, layout, font, background };
    if (background === "asset") body.background_asset_id = assetId;
    if (background === "frame") body.frame_time = frameTime;
    void run("apply", async () => setProject(await api.patch<ProjectDetail>(`/api/designs/${design.id}`, body)), "Diseño recompuesto y medido de nuevo.");
  };

  const uploadImage = async (file: File) => {
    setImageUpload(0);
    try {
      const result = await uploadFile<{ asset: Asset; project: ProjectDetail }>(`/api/projects/${project.id}/uploads${query({ kind: "image", filename: file.name })}`, file, setImageUpload);
      setProject(result.project);
      setAssetId(result.asset.id);
      setBackground("asset");
    } catch (error) {
      toast.error(error);
    } finally {
      setImageUpload(null);
    }
  };

  return (
    <div className="stack stack-sm">
      <div className="row row-between">
        <h4>{DESIGN_NAMES[design.kind] ?? design.kind}</h4>
        <span className="row small soft">
          Versión {design.version}
          {design.approved ? (
            <Badge tone="ok" icon="check">
              Aprobado
            </Badge>
          ) : null}
        </span>
      </div>
      {design.error ? <Notice tone="bad">{design.error}</Notice> : null}
      <div className="split split-even">
        <div className="stack stack-sm">
          <div className="design-preview">
            {design.url ? <img src={design.url} alt={`${DESIGN_NAMES[design.kind] ?? design.kind}: ${spec.headline ?? ""}`} width={design.size.width} height={design.size.height} style={{ height: "auto" }} /> : <span className="muted" style={{ padding: 24 }}>Sin componer</span>}
          </div>
          <CheckList checks={design.checks} compact />
          {design.url ? (
            <div>
              <a className="btn btn-small" href={`${design.url}?download=1`}>
                <Icon name="download" size={12} />
                <span>Descargar JPG</span>
              </a>
            </div>
          ) : null}
        </div>
        <fieldset className="stack" disabled={locked} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
          <legend className="sr-only">Ajustes del diseño</legend>
          <Field label="Titular" hint="Pocas palabras: debe leerse en un móvil." counter={`${headline.length}/90`}>
            <input type="text" value={headline} maxLength={90} onChange={(event) => setHeadline(event.target.value)} />
          </Field>
          <div className="form-grid">
            <Field label="Composición">
              <select value={layout} onChange={(event) => setLayout(event.target.value)}>
                {session.catalog.layouts.map((item) => (
                  <option key={item} value={item}>
                    {LAYOUT_NAMES[item] ?? item}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Tipografía">
              <select value={font} onChange={(event) => setFont(event.target.value)}>
                {session.catalog.fonts.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <div className="stack stack-sm">
            <span className="field-label">Fondo</span>
            <Segmented
              label="Fondo"
              value={background}
              onChange={setBackground}
              options={[
                { id: "frame", label: "Fotograma del vídeo" },
                { id: "gradient", label: "Degradado de marca" },
                { id: "asset", label: "Imagen propia" },
              ]}
            />
            {background === "frame" ? (
              hasVideo && duration > 0 ? (
                <Field label={`Fotograma en ${formatClock(frameTime)}`}>
                  <input type="range" min={0} max={Math.max(0, Math.floor(duration * 10) / 10 - 0.1)} step={0.1} value={Math.min(frameTime, duration)} onChange={(event) => setFrameTime(Number(event.target.value))} />
                </Field>
              ) : (
                <span className="small soft">No hay vídeo del que extraer un fotograma (o falta FFmpeg): se usará el degradado de marca.</span>
              )
            ) : null}
            {background === "asset" ? (
              <div className="stack stack-sm">
                {project.images.length ? (
                  <select value={assetId} aria-label="Imagen de fondo" onChange={(event) => setAssetId(event.target.value)}>
                    <option value="">Elige una imagen…</option>
                    {project.images.map((image) => (
                      <option key={image.id} value={image.id}>
                        {image.filename}
                      </option>
                    ))}
                  </select>
                ) : (
                  <span className="small soft">Aún no has subido imágenes a este proyecto.</span>
                )}
                {imageUpload !== null ? <ProgressBar value={imageUpload} label="Subiendo imagen" /> : <FileButton small label="Subir imagen (png, jpg, webp)" accept={IMAGE_ACCEPT} onFile={(file) => void uploadImage(file)} />}
                <span className="small muted">Usa solo imágenes sobre las que tengas derechos.</span>
              </div>
            ) : null}
          </div>
          <div>
            <Button variant="primary" icon="retry" busy={busy === "apply"} disabled={!dirty || !headline.trim()} onClick={apply}>
              Recomponer con estos ajustes
            </Button>
          </div>
        </fieldset>
      </div>
    </div>
  );
}
