// Research and script: versioned documents a person reviews, edits and approves.

import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../../api";
import type { Check, DocVersion, DocumentInfo, ProjectDetail, Step } from "../../api";
import { formatDate, formatDateTime, plural } from "../../format";
import { useAction, useResource, useSession } from "../../live";
import { Badge, Button, CheckList, ChipInput, Counter, Empty, ErrorBox, Field, Loading, Modal, Notice } from "../../ui";
import type { Tone } from "../../ui";
import type { ApproveControl } from "../Project";
import { PlatformTag, WithPlaceholders, useAgents } from "../shared";
import { useProject } from "./context";

// --- Versions ------------------------------------------------------------------

function VersionLine({ document, onHistory }: { document: DocumentInfo; onHistory: () => void }) {
  const agents = useAgents();
  const version = document.version;
  const author = version.author_type === "agent" ? (agents[version.author]?.name ?? version.author) : version.author;
  return (
    <div className="row small soft">
      <Badge>Versión {version.number}</Badge>
      <span>
        {author} · {formatDateTime(version.created_at)}
        {version.provider ? ` · ${version.provider}` : ""}
        {version.note ? ` · ${version.note}` : ""}
      </span>
      {document.versions > 1 ? (
        <button type="button" className="link-btn" onClick={onHistory}>
          Historial ({document.versions})
        </button>
      ) : null}
    </div>
  );
}

function VersionsModal({ kind, title, onClose }: { kind: "research" | "script"; title: string; onClose: () => void }) {
  const { project, setProject, canEdit } = useProject();
  const agents = useAgents();
  const versions = useResource<{ versions: DocVersion[] }>(() => api.get(`/api/projects/${project.id}/documents/${kind}`), [project.id, kind]);
  const { busy, run } = useAction();
  const [open, setOpen] = useState<string | null>(null);

  const restore = (version: DocVersion) =>
    run(
      version.id,
      async () => {
        setProject(await api.post<ProjectDetail>(`/api/projects/${project.id}/documents/${kind}/restore`, { version_id: version.id }));
        onClose();
      },
      `Versión ${version.number} restaurada como versión nueva.`,
    );

  return (
    <Modal title={`Historial · ${title}`} onClose={onClose} wide>
      {versions.loading && !versions.data ? <Loading /> : null}
      {versions.error ? <ErrorBox error={versions.error} onRetry={() => void versions.reload()} /> : null}
      <p className="small soft">Restaurar no borra nada: crea una versión nueva con el contenido de la elegida.</p>
      {(versions.data?.versions ?? []).map((version) => (
        <div key={version.id} className="version-row">
          <div className="grow">
            <div className="row">
              <strong>Versión {version.number}</strong>
              {version.is_current ? <Badge tone="accent">Actual</Badge> : null}
              <Badge>{version.author_type === "agent" ? (agents[version.author]?.name ?? version.author) : version.author}</Badge>
            </div>
            <div className="small soft">
              {formatDateTime(version.created_at)}
              {version.provider ? ` · ${version.provider}` : ""}
              {version.note ? ` · ${version.note}` : ""}
            </div>
            {open === version.id ? (
              <div className="card" style={{ marginTop: 8 }}>
                {kind === "script" ? <ScriptView content={version.content as ScriptContent} /> : <ResearchSummary content={version.content as ResearchContent} />}
              </div>
            ) : null}
          </div>
          <div className="row">
            <Button small onClick={() => setOpen(open === version.id ? null : version.id)}>
              {open === version.id ? "Ocultar" : "Ver"}
            </Button>
            {canEdit && !version.is_current ? (
              <Button small icon="retry" busy={busy === version.id} onClick={() => void restore(version)}>
                Restaurar
              </Button>
            ) : null}
          </div>
        </div>
      ))}
    </Modal>
  );
}

// --- Research ------------------------------------------------------------------

interface Finding {
  claim: string;
  source_title: string;
  site: string;
  url: string;
  published: string | null;
  confidence: "alta" | "media" | "baja";
  confidence_reason: string;
}

interface Angle {
  title: string;
  rationale: string;
  audience: string;
  keywords: string[];
  source_urls: string[];
}

interface ResearchContent {
  angles: Angle[];
  findings: Finding[];
  keywords: string[];
  unverified: string[];
  notes: string;
  sources?: { requested: string[]; read: number; failed: { url: string; reason: string }[] };
}

const CONFIDENCE_TONE: Record<string, Tone> = { alta: "ok", media: "warn", baja: "bad" };

function SourceLink({ url, children }: { url: string; children?: ReactNode }) {
  return (
    <a href={url} target="_blank" rel="noopener noreferrer nofollow">
      {children ?? url}
    </a>
  );
}

function ResearchSummary({ content }: { content: ResearchContent }) {
  return (
    <div className="stack stack-sm small">
      <div>
        <strong>Ángulos:</strong> {content.angles.map((angle) => angle.title).join(" · ") || "ninguno"}
      </div>
      <div>
        <strong>Hallazgos con fuente:</strong> {content.findings.length}
      </div>
      {content.findings.map((finding, index) => (
        <div key={index} className="soft">
          — {finding.claim} (<SourceLink url={finding.url}>{finding.source_title || finding.url}</SourceLink>)
        </div>
      ))}
    </div>
  );
}

export function ResearchPanel({ step, onApproveChange }: { step: Step; onApproveChange: (control: ApproveControl) => void }) {
  const { project, canEdit } = useProject();
  const document = project.documents.research;
  const content = document?.version.content as ResearchContent | undefined;
  const reviewing = step.status === "awaiting_approval" && canEdit;
  const [chosen, setChosen] = useState("");
  const [custom, setCustom] = useState("");
  const [history, setHistory] = useState(false);
  const decided = step.output.chosen_angle as string | undefined;

  useEffect(() => {
    if (!reviewing) {
      onApproveChange({});
      return;
    }
    const ready = chosen === "__custom" ? custom.trim().length > 0 : chosen !== "";
    onApproveChange({
      payload: chosen === "__custom" ? { custom_angle: custom.trim() } : { chosen_angle: chosen },
      blocked: ready ? undefined : "Elige uno de los ángulos propuestos o escribe el tuyo para poder aprobar.",
      label: "Aprobar con este ángulo",
    });
  }, [reviewing, chosen, custom, onApproveChange]);

  if (!document || !content) {
    return step.status === "working" || step.status === "queued" ? null : (
      <Empty icon="search" title="Aún no hay investigación">
        Cuando Scout termine verás aquí los ángulos propuestos y cada hallazgo con su enlace, fuente, fecha y nivel de confianza.
      </Empty>
    );
  }

  const byUrl = Object.fromEntries(content.findings.map((finding) => [finding.url, finding]));
  const sources = content.sources;
  return (
    <div className="stack stack-lg">
      <VersionLine document={document} onHistory={() => setHistory(true)} />

      {sources ? (
        sources.requested.length === 0 ? (
          <Notice tone="warn" title="Scout no tenía ninguna fuente permitida.">
            Sin enlaces de referencia ni fuentes en el kit de marca no puede verificar nada, así que los ángulos salen solo de tu brief. Añade enlaces en «Editar brief» o en el kit de marca y vuelve a lanzarlo.
          </Notice>
        ) : (
          <div className="stack stack-sm">
            <h4>Fuentes consultadas</h4>
            <p className="soft">
              {sources.read > 0 ? `Scout leyó ${plural(sources.read, "elemento")} de ${plural(sources.requested.length, "fuente permitida", "fuentes permitidas")}.` : "Scout no consiguió leer ninguna de las fuentes permitidas."}
            </p>
            {sources.failed.length ? (
              <ul className="stack stack-sm">
                {sources.failed.map((item) => (
                  <li key={item.url} className="notice notice-warn">
                    <span>
                      No se pudo leer <SourceLink url={item.url} />: {item.reason}
                    </span>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        )
      ) : null}

      <div className="stack stack-sm">
        <h4>Ángulos propuestos</h4>
        {decided && !reviewing ? (
          <Notice tone="ok" title="Ángulo aprobado:">
            «{decided}»{step.output.angle_is_custom ? " (escrito por ti)" : ""}
          </Notice>
        ) : null}
        <div className="stack stack-sm" role={reviewing ? "radiogroup" : undefined} aria-label="Ángulos propuestos">
          {content.angles.map((angle) => {
            const supported = angle.source_urls.filter((url) => byUrl[url]);
            const inner = (
              <div className="grow">
                <strong>
                  <WithPlaceholders text={angle.title} />
                </strong>
                <div className="soft">
                  <WithPlaceholders text={angle.rationale} />
                </div>
                {angle.audience ? <div className="small soft">Audiencia: {angle.audience}</div> : null}
                <div className="source-line">
                  {supported.length ? (
                    <>
                      <Badge tone="ok" icon="link">
                        {plural(supported.length, "fuente lo respalda", "fuentes lo respaldan")}
                      </Badge>
                      {supported.map((url) => (
                        <SourceLink key={url} url={url}>
                          {byUrl[url].site || byUrl[url].source_title}
                        </SourceLink>
                      ))}
                    </>
                  ) : (
                    <Badge tone="warn" icon="warning">
                      Sin fuentes que lo respalden
                    </Badge>
                  )}
                  {angle.keywords.map((keyword) => (
                    <Badge key={keyword}>{keyword}</Badge>
                  ))}
                </div>
              </div>
            );
            return reviewing ? (
              <label key={angle.title} className="angle">
                <input type="radio" name="angle" checked={chosen === angle.title} onChange={() => setChosen(angle.title)} />
                {inner}
              </label>
            ) : (
              <div key={angle.title} className="angle" style={{ cursor: "default", borderColor: decided === angle.title ? "#5eead4" : undefined }}>
                {inner}
              </div>
            );
          })}
          {reviewing ? (
            <label className="angle">
              <input type="radio" name="angle" checked={chosen === "__custom"} onChange={() => setChosen("__custom")} />
              <div className="grow stack stack-sm">
                <strong>Otro ángulo (lo escribo yo)</strong>
                <input type="text" value={custom} maxLength={300} placeholder="Escribe el enfoque que quieres" onFocus={() => setChosen("__custom")} onChange={(event) => setCustom(event.target.value)} aria-label="Tu propio ángulo" />
              </div>
            </label>
          ) : null}
        </div>
      </div>

      <div className="stack stack-sm">
        <h4>Hallazgos con fuente ({content.findings.length})</h4>
        {content.findings.length === 0 ? (
          <p className="muted">No hay hallazgos verificados. Scout solo registra afirmaciones que pueda enlazar a una fuente leída.</p>
        ) : (
          content.findings.map((finding, index) => (
            <div key={`${finding.url}-${index}`} className="finding">
              <div>{finding.claim}</div>
              <div className="source-line">
                <SourceLink url={finding.url}>{finding.source_title || finding.url}</SourceLink>
                {finding.site ? <span>· {finding.site}</span> : null}
                <span>· {finding.published ? `Fecha: ${formatDate(finding.published)}` : "Sin fecha declarada"}</span>
                <Badge tone={CONFIDENCE_TONE[finding.confidence] ?? "neutral"} title={finding.confidence_reason}>
                  Confianza {finding.confidence}
                </Badge>
              </div>
              {finding.confidence_reason ? <div className="small muted">{finding.confidence_reason}</div> : null}
            </div>
          ))
        )}
      </div>

      {content.unverified.length ? (
        <div className="stack stack-sm">
          <h4>Sin verificar</h4>
          <Notice tone="warn" title="Scout no pudo verificar lo siguiente:">
            <ul className="bullets" style={{ marginTop: 6 }}>
              {content.unverified.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </Notice>
        </div>
      ) : null}

      {content.keywords.length ? (
        <div className="stack stack-sm">
          <h4>Palabras clave</h4>
          <div className="row">
            {content.keywords.map((keyword) => (
              <Badge key={keyword}>{keyword}</Badge>
            ))}
          </div>
        </div>
      ) : null}

      {content.notes ? (
        <div className="stack stack-sm">
          <h4>Notas de Scout</h4>
          <p className="prose soft">{content.notes}</p>
        </div>
      ) : null}

      {history ? <VersionsModal kind="research" title="Investigación" onClose={() => setHistory(false)} /> : null}
    </div>
  );
}

// --- Script --------------------------------------------------------------------

interface ScriptSection {
  heading: string;
  text: string;
  visual: string;
}

interface PlatformCopy {
  platform: string;
  title: string;
  description: string;
  hashtags: string[];
}

interface ScriptContent {
  working_title: string;
  hooks: string[];
  outline: string[];
  sections: ScriptSection[];
  cta: string;
  titles: string[];
  platform_copy: PlatformCopy[];
  notes: string;
  copy_checks?: { platform: string; status: string; checks: Check[] }[];
  placeholders?: number;
}

function ScriptView({ content }: { content: ScriptContent }) {
  return (
    <div className="stack">
      <h3>
        <WithPlaceholders text={content.working_title} />
      </h3>
      {content.hooks.length ? (
        <div className="stack stack-sm">
          <h4>Ganchos</h4>
          <ol className="bullets" style={{ listStyle: "decimal" }}>
            {content.hooks.map((hook, index) => (
              <li key={index}>
                <WithPlaceholders text={hook} />
              </li>
            ))}
          </ol>
        </div>
      ) : null}
      {content.sections.length ? (
        <div className="stack">
          <h4>Guion</h4>
          {content.sections.map((section, index) => (
            <div key={index} className="script-section">
              <strong className="pixel">{section.heading}</strong>
              <p className="prose">
                <WithPlaceholders text={section.text} />
              </p>
              {section.visual ? (
                <p className="small soft">
                  En pantalla: <WithPlaceholders text={section.visual} />
                </p>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
      {content.cta ? (
        <div className="stack stack-sm">
          <h4>Llamada a la acción</h4>
          <p className="prose">
            <WithPlaceholders text={content.cta} />
          </p>
        </div>
      ) : null}
      {content.titles.length ? (
        <div className="stack stack-sm">
          <h4>Títulos alternativos</h4>
          <ul className="bullets">
            {content.titles.map((title, index) => (
              <li key={index}>
                <WithPlaceholders text={title} />
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function CopyView({ content }: { content: ScriptContent }) {
  const { catalog } = useSession();
  if (!content.platform_copy.length) return null;
  return (
    <div className="stack stack-sm">
      <h4>Textos por plataforma</h4>
      {content.platform_copy.map((copy) => {
        const spec = catalog.platforms.find((platform) => platform.key === copy.platform);
        const report = content.copy_checks?.find((item) => item.platform === copy.platform);
        // Show only the fields the platform has: TikTok and Instagram publish a single text.
        const single = spec && (!spec.description_max || !spec.title_max);
        const title = single ? "" : copy.title;
        const body = !spec || !single ? copy.description : spec.description_max ? copy.description || copy.title : copy.title || copy.description;
        return (
          <div key={copy.platform} className="card stack stack-sm">
            <PlatformTag platform={copy.platform} />
            {title ? (
              <div>
                <span className="small muted">Título</span>
                <div className="prose">
                  <WithPlaceholders text={title} />
                </div>
              </div>
            ) : null}
            {body ? (
              <div>
                <span className="small muted">{single ? "Texto de la publicación" : "Descripción"}</span>
                <div className="prose">
                  <WithPlaceholders text={body} />
                </div>
              </div>
            ) : null}
            {copy.hashtags.length ? (
              <div className="row">
                {copy.hashtags.map((tag) => (
                  <Badge key={tag}>{tag}</Badge>
                ))}
              </div>
            ) : null}
            {!title && !body ? <span className="muted">Sin texto todavía.</span> : null}
            {report ? <CheckList checks={report.checks} compact /> : null}
          </div>
        );
      })}
      <p className="small muted">Los límites que se comprueban son valores de referencia de cada plataforma y pueden cambiar; consulta su documentación oficial.</p>
    </div>
  );
}

export function ScriptPanel({ step }: { step: Step }) {
  const { project, canEdit } = useProject();
  const document = project.documents.script;
  const content = document?.version.content as ScriptContent | undefined;
  const [editing, setEditing] = useState(false);
  const [history, setHistory] = useState(false);

  if (!document || !content) {
    return step.status === "working" || step.status === "queued" ? null : (
      <Empty icon="edit" title="Aún no hay guion">
        Writer lo escribirá cuando apruebes la investigación: ganchos, guion por secciones, llamada a la acción y textos para cada plataforma.
      </Empty>
    );
  }

  const busy = step.status === "working" || step.status === "queued";
  return (
    <div className="stack stack-lg">
      <div className="row row-between">
        <VersionLine document={document} onHistory={() => setHistory(true)} />
        {canEdit && !editing ? (
          <Button small icon="edit" disabled={busy} title={busy ? "Writer está rehaciendo el guion" : undefined} onClick={() => setEditing(true)}>
            Editar guion
          </Button>
        ) : null}
      </div>
      {content.placeholders ? (
        <Notice tone="warn" title={`${plural(content.placeholders, "marcador", "marcadores")} por completar.`}>
          Los textos entre [corchetes] son huecos que hay que rellenar a mano{step.output.engine?.mode === "demo" ? ": el motor de demostración no redacta contenido" : ""}. Edita el guion para completarlos.
        </Notice>
      ) : null}
      {editing ? (
        <ScriptEditor content={content} onClose={() => setEditing(false)} />
      ) : (
        <div className="split">
          <ScriptView content={content} />
          <div className="stack">
            <CopyView content={content} />
            {content.outline.length ? (
              <div className="stack stack-sm">
                <h4>Esquema</h4>
                <ol className="bullets" style={{ listStyle: "decimal" }}>
                  {content.outline.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ol>
              </div>
            ) : null}
            {content.notes ? (
              <div className="stack stack-sm">
                <h4>Notas de Writer</h4>
                <p className="prose soft">{content.notes}</p>
              </div>
            ) : null}
          </div>
        </div>
      )}
      {history ? <VersionsModal kind="script" title="Guion" onClose={() => setHistory(false)} /> : null}
    </div>
  );
}

function ScriptEditor({ content, onClose }: { content: ScriptContent; onClose: () => void }) {
  const { project, setProject } = useProject();
  const { catalog } = useSession();
  const [draft, setDraft] = useState<ScriptContent>(() => ({
    ...content,
    hooks: [...content.hooks],
    outline: [...content.outline],
    titles: [...content.titles],
    sections: content.sections.map((section) => ({ ...section })),
    platform_copy: content.platform_copy.map((copy) => ({ ...copy, hashtags: [...copy.hashtags] })),
  }));
  const [note, setNote] = useState("");
  const { busy, run } = useAction();

  const lines = (value: string) => value.split("\n");
  const setSection = (index: number, patch: Partial<ScriptSection>) =>
    setDraft({ ...draft, sections: draft.sections.map((section, i) => (i === index ? { ...section, ...patch } : section)) });
  const setCopy = (index: number, patch: Partial<PlatformCopy>) =>
    setDraft({ ...draft, platform_copy: draft.platform_copy.map((copy, i) => (i === index ? { ...copy, ...patch } : copy)) });
  const move = (index: number, delta: number) => {
    const sections = [...draft.sections];
    const target = index + delta;
    if (target < 0 || target >= sections.length) return;
    [sections[index], sections[target]] = [sections[target], sections[index]];
    setDraft({ ...draft, sections });
  };

  const save = () =>
    run(
      "save",
      async () => {
        const clean = {
          working_title: draft.working_title.trim() || project.title,
          hooks: draft.hooks.map((item) => item.trim()).filter(Boolean),
          outline: draft.outline.map((item) => item.trim()).filter(Boolean),
          titles: draft.titles.map((item) => item.trim()).filter(Boolean),
          sections: draft.sections.filter((section) => section.heading.trim() || section.text.trim()),
          cta: draft.cta,
          platform_copy: draft.platform_copy,
          notes: draft.notes,
        };
        setProject(await api.post<ProjectDetail>(`/api/projects/${project.id}/documents/script`, { content: clean, note }));
        onClose();
      },
      "Guion guardado como versión nueva.",
    );

  return (
    <div className="stack stack-lg">
      <Field label="Título de trabajo">
        <input type="text" value={draft.working_title} onChange={(event) => setDraft({ ...draft, working_title: event.target.value })} />
      </Field>
      <Field label="Ganchos (uno por línea)">
        <textarea value={draft.hooks.join("\n")} rows={Math.max(3, draft.hooks.length)} onChange={(event) => setDraft({ ...draft, hooks: lines(event.target.value) })} />
      </Field>
      <div className="stack">
        <h4>Secciones del guion</h4>
        {draft.sections.map((section, index) => (
          <div key={index} className="script-section stack stack-sm">
            <div className="row">
              <input type="text" className="grow" style={{ flex: "1 1 200px" }} value={section.heading} aria-label={`Título de la sección ${index + 1}`} onChange={(event) => setSection(index, { heading: event.target.value })} />
              <Button small disabled={index === 0} onClick={() => move(index, -1)} aria-label={`Subir sección ${index + 1}`}>
                ↑
              </Button>
              <Button small disabled={index === draft.sections.length - 1} onClick={() => move(index, 1)} aria-label={`Bajar sección ${index + 1}`}>
                ↓
              </Button>
              <Button small icon="trash" onClick={() => setDraft({ ...draft, sections: draft.sections.filter((_, i) => i !== index) })} aria-label={`Eliminar sección ${index + 1}`} />
            </div>
            <Field label="Lo que se dice">
              <textarea value={section.text} onChange={(event) => setSection(index, { text: event.target.value })} />
            </Field>
            <Field label="En pantalla (texto o plano sugerido)">
              <input type="text" value={section.visual} onChange={(event) => setSection(index, { visual: event.target.value })} />
            </Field>
          </div>
        ))}
        <div>
          <Button small icon="plus" onClick={() => setDraft({ ...draft, sections: [...draft.sections, { heading: "Nueva sección", text: "", visual: "" }] })}>
            Añadir sección
          </Button>
        </div>
      </div>
      <Field label="Llamada a la acción">
        <textarea value={draft.cta} rows={2} onChange={(event) => setDraft({ ...draft, cta: event.target.value })} />
      </Field>
      <Field label="Títulos alternativos (uno por línea)">
        <textarea value={draft.titles.join("\n")} rows={Math.max(3, draft.titles.length)} onChange={(event) => setDraft({ ...draft, titles: lines(event.target.value) })} />
      </Field>
      <div className="stack">
        <h4>Textos por plataforma</h4>
        {draft.platform_copy.map((copy, index) => {
          const spec = catalog.platforms.find((platform) => platform.key === copy.platform);
          const tagText = copy.hashtags.join(" ");
          return (
            <div key={copy.platform} className="card stack stack-sm">
              <PlatformTag platform={copy.platform} />
              {spec?.title_max ? (
                <Field label={spec.description_max ? "Título" : "Texto de la publicación"} counter={<Counter value={copy.title.length + (spec.description_max ? 0 : tagText.length + (tagText ? 1 : 0))} max={spec.title_max} />}>
                  {spec.description_max ? (
                    <input type="text" value={copy.title} onChange={(event) => setCopy(index, { title: event.target.value })} />
                  ) : (
                    <textarea value={copy.title} rows={3} onChange={(event) => setCopy(index, { title: event.target.value })} />
                  )}
                </Field>
              ) : null}
              {spec?.description_max ? (
                <Field label={spec.title_max ? "Descripción" : "Texto de la publicación"} counter={<Counter value={copy.description.length + tagText.length + (tagText ? 2 : 0)} max={spec.description_max} />}>
                  <textarea value={copy.description} rows={4} onChange={(event) => setCopy(index, { description: event.target.value })} />
                </Field>
              ) : null}
              <Field label="Hashtags" hint={spec?.hashtags_max ? `Máximo de referencia: ${spec.hashtags_max}. Se añaden al final del texto.` : "Se añaden al final del texto."}>
                <ChipInput values={copy.hashtags} onChange={(hashtags) => setCopy(index, { hashtags })} placeholder="#etiqueta" prefix="#" />
              </Field>
            </div>
          );
        })}
      </div>
      <Field label="Nota de esta versión (opcional)" hint="Para recordar qué cambiaste.">
        <input type="text" value={note} maxLength={300} onChange={(event) => setNote(event.target.value)} />
      </Field>
      <div className="row row-end">
        <Button variant="ghost" onClick={onClose}>
          Descartar cambios
        </Button>
        <Button variant="primary" icon="check" busy={busy === "save"} onClick={() => void save()}>
          Guardar como versión nueva
        </Button>
      </div>
    </div>
  );
}
