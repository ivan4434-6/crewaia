// Analytics: only figures that exist, each labelled with where it came from.

import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { PageHead, useOffice } from "../App";
import { api, query } from "../api";
import { formatDate, formatDateTime, formatNumber, fromLocalInput, plural, readFigure, toLocalInput } from "../format";
import { useAction, useResource, useSession } from "../live";
import { Badge, Button, Empty, ErrorBox, Field, Loading, Modal, Notice, Panel } from "../ui";
import { PlatformTag } from "./shared";

interface MetricItem {
  publication_id: string;
  project_id: string;
  project: string;
  title: string;
  platform: string;
  platform_name: string;
  format: string;
  published_at: string | null;
  confirmation: string;
  source: "platform" | "manual";
  captured_at: string;
  snapshots: number;
  values: Record<string, number>;
  engagement_rate: number | null;
  missing: string[];
}

interface ComparisonGroup {
  name: string;
  pieces: number;
  views_median: number;
  retention_avg: number | null;
  retention_pieces: number;
  engagement_avg: number | null;
  engagement_pieces: number;
}

interface Comparison {
  dimension: string;
  label: string;
  sufficient: boolean;
  reason: string;
  groups: ComparisonGroup[];
}

interface Recommendation {
  id: string;
  text: string;
  evidence: { text: string; flag?: boolean }[];
  basis: "rules" | "llm";
  status: "new" | "accepted" | "dismissed";
  project: string;
  created_at: string;
}

interface PublishedItem {
  id: string;
  project_id: string;
  project: string;
  platform: string;
  platform_name: string;
  title: string;
  published_at: string | null;
  confirmation: "platform" | "manual" | "";
  url: string;
  has_metrics: boolean;
  can_sync: boolean;
}

interface AnalyticsPayload {
  stats: {
    published_total: number;
    with_metrics: number;
    without_metrics: number;
    sources: { platform: number; manual: number };
    incomplete: number;
    totals: Record<string, number>;
    publications: MetricItem[];
    comparisons: Comparison[];
    min_group: number;
  };
  recommendations: Recommendation[];
  reports: { project_id: string; project: string; summary: string; data_gaps: string[]; engine: { label?: string; mode?: string }; finished_at: string | null }[];
  published: PublishedItem[];
  sources: { provider: string; name: string; connected: boolean; label: string; metrics: string[]; metrics_missing: string[] }[];
  fields: Record<string, { label: string; unit: string; whole: boolean }>;
}

const FORMAT_NAMES: Record<string, string> = { vertical: "Vertical 9:16", horizontal: "Horizontal 16:9", square: "Cuadrado 1:1" };

export function AnalyticsPage() {
  const session = useSession();
  const office = useOffice();
  const [params, setParams] = useSearchParams();
  const projectId = params.get("proyecto") ?? "";
  const analytics = useResource<AnalyticsPayload>(
    () => api.get(`/api/analytics${query({ project_id: projectId })}`),
    [projectId, session.workspace.id],
    (message) => message.kind === "event" && (message.type.startsWith("metrics.") || message.type.startsWith("recommendation.") || message.type.startsWith("publication.") || message.data.kind === "analysis"),
  );
  const { busy, run } = useAction();
  const [adding, setAdding] = useState<PublishedItem | null>(null);
  const data = analytics.data;
  const projects = office.data?.projects_list ?? [];

  const sync = () =>
    run(
      "sync",
      async () => {
        await api.post("/api/analytics/sync", { project_id: projectId });
      },
      "Sincronización en marcha: Analyst está leyendo las métricas.",
    );
  const decide = (id: string, status: Recommendation["status"], done: string) =>
    run(
      `reco-${id}`,
      async () => {
        await api.patch(`/api/recommendations/${id}`, { status });
        await analytics.reload();
      },
      done,
    );

  return (
    <>
      <PageHead
        title="Analítica"
        actions={
          session.canEdit ? (
            <Button icon="retry" busy={busy === "sync"} onClick={() => void sync()}>
              Sincronizar métricas
            </Button>
          ) : null
        }
      >
        Analyst resume lo que hay, sin rellenar huecos: cada cifra indica si viene de la plataforma o si la añadiste a mano, y solo se comparan grupos con datos suficientes.
      </PageHead>

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
      </div>

      {analytics.error && !data ? <ErrorBox error={analytics.error} onRetry={() => void analytics.reload()} /> : null}
      {analytics.loading && !data ? <Loading /> : null}
      {data ? (
        <>
          <Panel title="Origen de los datos">
            <div className="grid-cards">
              {data.sources.map((source) => (
                <div key={source.provider} className="card stack stack-sm">
                  <div className="row row-between">
                    <PlatformTag platform={source.provider} name={source.name} />
                    {source.connected ? (
                      <Badge tone="ok" icon="check">
                        Conectado
                      </Badge>
                    ) : (
                      <Badge tone="warn" icon="plug">
                        {source.label}
                      </Badge>
                    )}
                  </div>
                  <span className="small soft">
                    {source.connected ? "Su API ofrece: " : "Con la cuenta conectada se podrían leer: "}
                    {source.metrics.map((key) => data.fields[key]?.label ?? key).join(", ")}.
                  </span>
                  {source.metrics_missing.length ? <span className="small muted">No disponibles por API: {source.metrics_missing.map((key) => data.fields[key]?.label ?? key).join(", ")}.</span> : null}
                  {!source.connected ? (
                    <span className="small muted">
                      Mientras tanto, añade las cifras a mano. <Link to="/integraciones">Abrir Integraciones</Link>
                    </span>
                  ) : null}
                </div>
              ))}
            </div>
          </Panel>

          {data.stats.published_total === 0 ? (
            <Empty icon="chart" title="Aún no hay nada publicado que medir">
              Las métricas aparecen cuando una publicación consta como publicada: confirmada por la plataforma o declarada a mano. Después se leen de la cuenta conectada o las añades tú.
            </Empty>
          ) : (
            <>
              <div className="stats">
                <div className="stat">
                  <strong>{data.stats.published_total}</strong>
                  <span>{data.stats.published_total === 1 ? "publicación" : "publicaciones"}</span>
                </div>
                <div className="stat">
                  <strong>{data.stats.with_metrics}</strong>
                  <span>con métricas</span>
                </div>
                <div className="stat">
                  <strong>{data.stats.without_metrics}</strong>
                  <span>sin métricas todavía</span>
                </div>
                <div className="stat">
                  <strong>{data.stats.sources.platform}</strong>
                  <span>leídas de la plataforma</span>
                </div>
                <div className="stat">
                  <strong>{data.stats.sources.manual}</strong>
                  <span>añadidas a mano</span>
                </div>
                {Object.entries(data.stats.totals).map(([key, value]) => (
                  <div className="stat" key={key}>
                    <strong>{formatNumber(value)}</strong>
                    <span>
                      {data.fields[key]?.label ?? key}
                      {data.fields[key]?.unit ? ` (${data.fields[key].unit})` : ""} en total
                    </span>
                  </div>
                ))}
              </div>
              {data.stats.incomplete ? (
                <Notice tone="warn" title={`${plural(data.stats.incomplete, "pieza tiene", "piezas tienen")} datos incompletos.`}>
                  Los totales solo suman las cifras que existen; las que faltan se indican en cada fila y no se estiman.
                </Notice>
              ) : null}

              <Panel title="Publicaciones">
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th className="col-main">Publicación</th>
                        <th>Origen de las cifras</th>
                        <th className="num">Visualizaciones</th>
                        <th className="num">Retención</th>
                        <th className="num">Me gusta</th>
                        <th className="num">Comentarios</th>
                        <th className="num">Interacción</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {data.published.map((item) => {
                        const metric = data.stats.publications.find((row) => row.publication_id === item.id);
                        const cell = (key: string, unit = "") => (metric && key in metric.values ? `${formatNumber(metric.values[key])}${unit}` : "—");
                        return (
                          <tr key={item.id}>
                            <td className="col-main">
                              <div className="stack stack-sm">
                                <PlatformTag platform={item.platform} name={item.platform_name} />
                                <Link to={`/proyectos/${item.project_id}?tab=publicar`}>{item.title || "Sin título"}</Link>
                                <span className="small muted">{item.project}</span>
                                <span className="row small">
                                  <span className="nowrap">Publicada el {formatDate(item.published_at)}</span>
                                  {item.confirmation === "platform" ? <Badge tone="ok">Confirmada por la plataforma</Badge> : <Badge tone="info">Declarada a mano</Badge>}
                                </span>
                              </div>
                            </td>
                            <td>
                              {metric ? (
                                <div className="stack stack-sm">
                                  {metric.source === "platform" ? <Badge tone="ok">Plataforma</Badge> : <Badge tone="info">A mano</Badge>}
                                  <span className="small muted">{formatDateTime(metric.captured_at)}</span>
                                  {metric.missing.length ? <span className="small" style={{ color: "#fbbf24" }}>Falta: {metric.missing.join(", ")}</span> : null}
                                </div>
                              ) : (
                                <span className="muted">Sin métricas</span>
                              )}
                            </td>
                            <td className="num">{cell("views")}</td>
                            <td className="num">{cell("retention_pct", " %")}</td>
                            <td className="num">{cell("likes")}</td>
                            <td className="num">{cell("comments")}</td>
                            <td className="num">{metric && metric.engagement_rate !== null ? `${formatNumber(metric.engagement_rate)} %` : "—"}</td>
                            <td>
                              {session.canEdit ? (
                                <Button small icon="edit" onClick={() => setAdding(item)}>
                                  {metric ? "Actualizar" : "Añadir métricas"}
                                </Button>
                              ) : null}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                <p className="small muted" style={{ marginTop: 8 }}>
                  Interacción = (me gusta + comentarios + compartidos + guardados) ÷ visualizaciones; solo se calcula cuando existen visualizaciones y me gusta.
                </p>
              </Panel>

              <Panel title="Comparaciones">
                <div className="split split-even">
                  {data.stats.comparisons.map((comparison) => (
                    <ComparisonCard key={comparison.dimension} comparison={comparison} minGroup={data.stats.min_group} />
                  ))}
                </div>
              </Panel>
            </>
          )}

          {data.reports.length ? (
            <Panel title="Resúmenes de Analyst">
              <div className="stack">
                {data.reports.map((report) => (
                  <div key={report.project_id} className="card stack stack-sm">
                    <div className="row row-between">
                      <Link to={`/proyectos/${report.project_id}?paso=analysis`}>
                        <strong>{report.project}</strong>
                      </Link>
                      <span className="row small soft">
                        {report.engine.label ? <Badge tone={report.engine.mode === "demo" ? "warn" : "accent"}>{report.engine.mode === "demo" ? "Calculado con reglas (modo demostración)" : report.engine.label}</Badge> : null}
                        {formatDateTime(report.finished_at)}
                      </span>
                    </div>
                    <p className="prose">{report.summary || "Sin resumen."}</p>
                    {report.data_gaps.length ? (
                      <ul className="evidence">
                        {report.data_gaps.map((gap, index) => (
                          <li key={index} className="flag">
                            {gap}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </div>
                ))}
              </div>
            </Panel>
          ) : null}

          <Panel title="Recomendaciones">
            {data.recommendations.length === 0 ? (
              <p className="muted">Todavía no hay recomendaciones. Analyst las propone cuando hay métricas, citando las cifras en las que se apoya.</p>
            ) : (
              <div className="stack">
                {data.recommendations.map((item) => (
                  <div key={item.id} className={`reco ${item.status}`}>
                    <div className="row row-between row-top">
                      <div className="grow">{item.text}</div>
                      <div className="row">
                        <Badge>{item.basis === "rules" ? "Calculada con reglas" : "Redactada por IA con tus cifras"}</Badge>
                        {item.status === "accepted" ? (
                          <Badge tone="ok" icon="check">
                            Aceptada
                          </Badge>
                        ) : item.status === "dismissed" ? (
                          <Badge>Descartada</Badge>
                        ) : null}
                      </div>
                    </div>
                    {item.evidence.length ? (
                      <ul className="evidence">
                        {item.evidence.map((evidence, index) => (
                          <li key={index} className={evidence.flag ? "flag" : ""}>
                            {evidence.text}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="small muted" style={{ marginTop: 6 }}>
                        Sin cifras de apoyo.
                      </p>
                    )}
                    <div className="row small muted" style={{ marginTop: 8 }}>
                      <span>
                        {item.project ? `${item.project} · ` : ""}
                        {formatDate(item.created_at)}
                      </span>
                      {session.canEdit ? (
                        item.status === "new" ? (
                          <>
                            <Button small variant="primary" icon="check" busy={busy === `reco-${item.id}`} onClick={() => void decide(item.id, "accepted", "Aceptada: los agentes la tendrán en cuenta en los próximos proyectos.")}>
                              Aceptar
                            </Button>
                            <Button small variant="ghost" onClick={() => void decide(item.id, "dismissed", "Recomendación descartada.")}>
                              Descartar
                            </Button>
                          </>
                        ) : (
                          <button type="button" className="link-btn" onClick={() => void decide(item.id, "new", "Recomendación restablecida.")}>
                            Deshacer
                          </button>
                        )
                      ) : null}
                    </div>
                  </div>
                ))}
                <p className="small muted">Las recomendaciones aceptadas se añaden al brief que reciben los agentes en los proyectos siguientes.</p>
              </div>
            )}
          </Panel>
        </>
      ) : null}
      {adding && data ? <MetricsModal item={adding} fields={data.fields} current={data.stats.publications.find((row) => row.publication_id === adding.id)?.values} onClose={() => setAdding(null)} onDone={analytics.reload} /> : null}
    </>
  );
}

function ComparisonCard({ comparison, minGroup }: { comparison: Comparison; minGroup: number }) {
  const max = Math.max(1, ...comparison.groups.map((group) => group.views_median));
  const label = (name: string) => (comparison.dimension === "format" ? (FORMAT_NAMES[name] ?? name) : name);
  return (
    <div className="card stack stack-sm">
      <div className="row row-between">
        <h4>{comparison.label}</h4>
        {comparison.sufficient ? (
          <Badge tone="ok" icon="check">
            Datos suficientes
          </Badge>
        ) : (
          <Badge tone="warn" icon="warning">
            Datos insuficientes
          </Badge>
        )}
      </div>
      {comparison.groups.length === 0 ? <span className="small muted">Ninguna pieza con visualizaciones.</span> : null}
      {comparison.sufficient ? (
        <div className="bars">
          {comparison.groups.map((group) => (
            <div key={group.name} className="bar-row" style={{ opacity: group.pieces >= minGroup ? 1 : 0.55 }}>
              <span title={label(group.name)}>{label(group.name)}</span>
              <div className="bar-track" role="img" aria-label={`${label(group.name)}: mediana de ${formatNumber(group.views_median)} visualizaciones en ${group.pieces} piezas`}>
                <div className="bar-fill" style={{ width: `${(group.views_median / max) * 100}%` }} />
              </div>
              <span className="nowrap">
                {formatNumber(group.views_median)} · {plural(group.pieces, "pieza")}
              </span>
            </div>
          ))}
          <span className="small muted">Mediana de visualizaciones por pieza. Los grupos con menos de {minGroup} piezas se muestran atenuados y no entran en la comparación.</span>
        </div>
      ) : (
        <>
          <span className="small soft">{comparison.reason}</span>
          {comparison.groups.length ? (
            <ul className="small muted bullets">
              {comparison.groups.map((group) => (
                <li key={group.name}>
                  {label(group.name)}: {plural(group.pieces, "pieza")} con visualizaciones
                </li>
              ))}
            </ul>
          ) : null}
        </>
      )}
    </div>
  );
}

function MetricsModal({ item, fields, current, onClose, onDone }: { item: PublishedItem; fields: AnalyticsPayload["fields"]; current: Record<string, number> | undefined; onClose: () => void; onDone: () => Promise<void> }) {
  const [values, setValues] = useState<Record<string, string>>(() => Object.fromEntries(Object.entries(current ?? {}).map(([key, value]) => [key, String(value).replace(".", ",")])));
  const [when, setWhen] = useState(() => toLocalInput(new Date().toISOString()));
  const { busy, run } = useAction();
  const filled = Object.values(values).some((value) => value.trim() !== "");
  // A figure that cannot be read is pointed out, never dropped: the person must know what was saved.
  const problem = (key: string): string | undefined => {
    const text = (values[key] ?? "").trim();
    if (!text) return undefined;
    const figure = readFigure(text, fields[key]?.whole ?? false);
    if (!figure.ok) {
      if (figure.reason === "whole") return "Esta cifra es un recuento: un número entero, sin decimales.";
      if (figure.reason === "ambiguous") return "¿Miles o decimales? Escríbela sin separador de miles: 1840 o 1,84.";
      return fields[key]?.whole ? "Escribe solo un número entero, por ejemplo 1840." : "Escribe solo un número, por ejemplo 1840 o 61,5.";
    }
    if (key === "retention_pct" && figure.value > 100) return "La retención es un porcentaje: entre 0 y 100.";
    return undefined;
  };
  const invalid = Object.keys(fields).some((key) => problem(key) !== undefined);

  const submit = () =>
    run(
      "save",
      async () => {
        const clean: Record<string, number> = {};
        for (const [key, value] of Object.entries(values)) {
          const figure = readFigure(value, fields[key]?.whole ?? false);
          if (value.trim() !== "" && figure.ok) clean[key] = figure.value;
        }
        await api.post(`/api/publications/${item.id}/metrics`, { values: clean, captured_at: fromLocalInput(when) });
        await onDone();
        onClose();
      },
      "Métricas guardadas como añadidas a mano. Analyst las tendrá en cuenta.",
    );

  return (
    <Modal
      title={`Métricas de «${item.title || item.platform_name}»`}
      onClose={onClose}
      wide
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancelar
          </Button>
          <Button variant="primary" icon="check" busy={busy === "save"} disabled={!filled || invalid} onClick={() => void submit()}>
            Guardar métricas
          </Button>
        </>
      }
    >
      <div className="stack">
        <p className="soft">
          Copia las cifras del panel de {item.platform_name}. Quedarán marcadas como <strong>añadidas a mano</strong> con su fecha. Deja en blanco lo que no tengas: no se estima nada.
        </p>
        <div className="form-grid">
          {Object.entries(fields).map(([key, field]) => (
            <Field key={key} label={`${field.label}${field.unit ? ` (${field.unit})` : ""}`} error={problem(key)}>
              {/* Text, not type="number": that one hands back an empty value for "61,5" in browsers set to English. */}
              <input type="text" inputMode="decimal" autoComplete="off" maxLength={16} value={values[key] ?? ""} onChange={(event) => setValues({ ...values, [key]: event.target.value })} />
            </Field>
          ))}
          <Field label="Fecha en que miraste las cifras">
            <input type="datetime-local" value={when} max={toLocalInput(new Date().toISOString())} onChange={(event) => setWhen(event.target.value)} />
          </Field>
        </div>
      </div>
    </Modal>
  );
}
