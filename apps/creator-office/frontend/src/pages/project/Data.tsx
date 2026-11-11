// The two data-driven steps: comments (Community) and metrics (Analyst).

import { useState } from "react";
import { Link } from "react-router-dom";
import { api, query } from "../../api";
import type { Step } from "../../api";
import { plural } from "../../format";
import { useAction, useSession } from "../../live";
import { Badge, Button, Notice } from "../../ui";
import { ImportCommentsModal } from "../Community";
import { useProject } from "./context";

export function CommunityStepPanel({ step }: { step: Step }) {
  const { project, reload, canEdit } = useProject();
  const session = useSession();
  const [importing, setImporting] = useState(false);
  const { busy, run } = useAction();
  const output = step.output as { counts?: Record<string, number>; replies?: number; notes?: string };
  const categories = session.catalog.comment_categories;

  const sync = () =>
    run(
      "sync",
      async () => {
        await api.post(`/api/projects/${project.id}/comments/sync`);
        await reload();
      },
      "Sincronización en marcha.",
    );

  return (
    <div className="stack stack-lg">
      <p className="soft">
        Community clasifica cada comentario (pregunta, positivo, spam, incidencia u otros), explica por qué y sugiere una respuesta con tu voz de marca. Las respuestas se aprueban una a una en Comunidad; nada se envía sin tu confirmación.
      </p>
      <div className="row">
        <Badge>{plural(project.comment_counts.total, "comentario")}</Badge>
        {project.comment_counts.pending_replies ? (
          <Badge tone="warn" icon="eye">
            {plural(project.comment_counts.pending_replies, "respuesta espera", "respuestas esperan")} tu aprobación
          </Badge>
        ) : null}
        {Object.entries(output.counts ?? {}).map(([key, count]) => (
          <Badge key={key}>
            {categories[key] ?? key}: {count}
          </Badge>
        ))}
      </div>
      {output.notes ? <p className="small soft prose">Notas de Community: {output.notes}</p> : null}
      <div className="row">
        <Link className="btn btn-primary" to={`/comunidad${query({ proyecto: project.id })}`}>
          <span>Abrir comentarios y respuestas</span>
        </Link>
        {canEdit ? (
          <>
            <Button icon="upload" onClick={() => setImporting(true)}>
              Importar comentarios a mano
            </Button>
            <Button icon="retry" busy={busy === "sync"} onClick={() => void sync()}>
              Sincronizar cuentas conectadas
            </Button>
          </>
        ) : null}
      </div>
      {importing ? <ImportCommentsModal projectId={project.id} publications={project.publications} onClose={() => setImporting(false)} onDone={reload} /> : null}
    </div>
  );
}

export function AnalysisStepPanel({ step }: { step: Step }) {
  const { project } = useProject();
  const output = step.output as { summary?: string; data_gaps?: string[]; recommendations?: number; with_metrics?: number; sources?: { platform?: number; manual?: number } };
  const published = project.publications.filter((item) => item.status === "published").length;

  return (
    <div className="stack stack-lg">
      <p className="soft">
        Analyst trabaja solo con cifras que existen: las que lee de una cuenta conectada o las que tú añades a mano. Distingue el origen de cada dato, dice cuáles faltan y solo compara cuando hay piezas suficientes.
      </p>
      {published === 0 ? (
        <Notice tone="info" icon="clock">
          Este proyecto aún no tiene publicaciones marcadas como publicadas, así que no hay nada que medir.
        </Notice>
      ) : null}
      {output.summary ? (
        <div className="stack stack-sm">
          <h4>Resumen</h4>
          <p className="prose">{output.summary}</p>
        </div>
      ) : null}
      {typeof output.with_metrics === "number" ? (
        <div className="row">
          <Badge>{plural(output.with_metrics, "pieza con métricas", "piezas con métricas")}</Badge>
          <Badge tone="ok">De la plataforma: {output.sources?.platform ?? 0}</Badge>
          <Badge tone="info">Añadidas a mano: {output.sources?.manual ?? 0}</Badge>
          <Badge tone="accent">{plural(output.recommendations ?? 0, "recomendación", "recomendaciones")}</Badge>
        </div>
      ) : null}
      {output.data_gaps?.length ? (
        <Notice tone="warn" title="Lo que los datos no permiten concluir:">
          <ul className="bullets" style={{ marginTop: 6 }}>
            {output.data_gaps.map((gap, index) => (
              <li key={index}>{gap}</li>
            ))}
          </ul>
        </Notice>
      ) : null}
      <div>
        <Link className="btn btn-primary" to={`/analitica${query({ proyecto: project.id })}`}>
          <span>Abrir analítica y añadir métricas</span>
        </Link>
      </div>
    </div>
  );
}
