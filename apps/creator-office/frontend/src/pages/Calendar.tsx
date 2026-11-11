// Editorial calendar: what is scheduled, what went out and what is still unscheduled.

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { PageHead } from "../App";
import { api, query } from "../api";
import { formatDateTime, formatTime } from "../format";
import { useResource, useSession } from "../live";
import { Badge, Button, ErrorBox, Loading, Panel } from "../ui";
import { PlatformTag } from "./shared";

interface CalendarItem {
  type: "publication" | "deadline";
  id: string;
  project_id: string;
  project: string;
  title: string;
  at: string;
  platform?: string;
  platform_name?: string;
  status?: string;
  status_label?: string;
  confirmation?: string;
  schedule_mode?: string;
  approved?: boolean;
}

interface CalendarPayload {
  items: CalendarItem[];
  unscheduled: { id: string; project_id: string; project: string; platform: string; platform_name: string; title: string; approved: boolean }[];
}

const DOW = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"];
const monthName = new Intl.DateTimeFormat("es", { month: "long", year: "numeric" });

function dayKey(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

/** The weeks (Monday first) that cover a month, as local dates. */
export function monthGrid(year: number, month: number): Date[] {
  const first = new Date(year, month, 1);
  const offset = (first.getDay() + 6) % 7;
  const start = new Date(year, month, 1 - offset);
  const last = new Date(year, month + 1, 0);
  const cells = Math.ceil((offset + last.getDate()) / 7) * 7;
  return Array.from({ length: cells }, (_, index) => new Date(start.getFullYear(), start.getMonth(), start.getDate() + index));
}

function itemNote(item: CalendarItem): string {
  if (item.type === "deadline") return "Fecha límite del proyecto";
  if (item.status === "published") return item.confirmation === "platform" ? "Publicado · confirmado por la plataforma" : "Publicado · declarado a mano";
  if (item.status === "scheduled") {
    if (item.schedule_mode === "platform") return "Programado en la plataforma";
    if (item.schedule_mode === "connector") return "Programado: lo subirá esta aplicación";
    return "Recordatorio para publicar a mano";
  }
  return item.status_label ?? "";
}

export function CalendarPage() {
  const session = useSession();
  const today = new Date();
  const [cursor, setCursor] = useState({ year: today.getFullYear(), month: today.getMonth() });
  const days = useMemo(() => monthGrid(cursor.year, cursor.month), [cursor]);
  const start = days[0];
  const end = new Date(days[days.length - 1].getFullYear(), days[days.length - 1].getMonth(), days[days.length - 1].getDate() + 1);
  const calendar = useResource<CalendarPayload>(
    () => api.get(`/api/calendar${query({ start: start.toISOString(), end: end.toISOString() })}`),
    [cursor.year, cursor.month, session.workspace.id],
    (message) => message.kind === "event" && (message.type.startsWith("publication.") || message.type.startsWith("project.")),
  );
  const byDay = useMemo(() => {
    const map: Record<string, CalendarItem[]> = {};
    for (const item of calendar.data?.items ?? []) {
      const key = dayKey(new Date(item.at));
      (map[key] ??= []).push(item);
    }
    return map;
  }, [calendar.data]);
  const move = (delta: number) => {
    const next = new Date(cursor.year, cursor.month + delta, 1);
    setCursor({ year: next.getFullYear(), month: next.getMonth() });
  };
  const todayKey = dayKey(today);
  const month = monthName.format(new Date(cursor.year, cursor.month, 1));
  const title = month.charAt(0).toUpperCase() + month.slice(1);
  const monthItems = (calendar.data?.items ?? []).filter((item) => new Date(item.at).getMonth() === cursor.month);

  return (
    <>
      <PageHead title="Calendario">Publicaciones programadas y publicadas, y las fechas límite de tus proyectos. Las horas se muestran en tu zona horaria.</PageHead>
      <div className="row row-between">
        <h2 aria-live="polite">{title}</h2>
        <div className="row">
          <Button onClick={() => move(-1)} aria-label="Mes anterior">
            ←
          </Button>
          <Button onClick={() => setCursor({ year: today.getFullYear(), month: today.getMonth() })}>Hoy</Button>
          <Button onClick={() => move(1)} aria-label="Mes siguiente">
            →
          </Button>
        </div>
      </div>
      {calendar.error && !calendar.data ? <ErrorBox error={calendar.error} onRetry={() => void calendar.reload()} /> : null}
      {calendar.loading && !calendar.data ? <Loading /> : null}
      <div className="calendar" role="grid" aria-label={`Calendario de ${title}`}>
        {DOW.map((name) => (
          <div key={name} className="calendar-dow" role="columnheader">
            {name}
          </div>
        ))}
        {days.map((day) => {
          const key = dayKey(day);
          const items = byDay[key] ?? [];
          return (
            <div key={key} role="gridcell" className={`calendar-day ${day.getMonth() === cursor.month ? "" : "other"} ${key === todayKey ? "today" : ""}`}>
              <span className="calendar-num">{day.getDate()}</span>
              {items.map((item) => (
                <Link
                  key={`${item.type}-${item.id}`}
                  to={item.type === "deadline" ? `/proyectos/${item.project_id}` : `/proyectos/${item.project_id}?tab=publicar`}
                  className={`calendar-item ${item.type === "deadline" ? "deadline" : (item.status ?? "")}`}
                  title={`${formatTime(item.at)} · ${item.type === "deadline" ? `Fecha límite: ${item.project}` : `${item.platform_name}: ${item.title}`} · ${itemNote(item)}`}
                >
                  {item.type === "deadline" ? `Límite: ${item.project}` : `${formatTime(item.at)} ${item.platform_name}`}
                </Link>
              ))}
            </div>
          );
        })}
      </div>

      <div className="split split-even">
        <Panel title="Este mes">
          {monthItems.length === 0 ? (
            <p className="muted">No hay nada programado ni publicado este mes.</p>
          ) : (
            <ul className="stack stack-sm">
              {monthItems.map((item) => (
                <li key={`${item.type}-${item.id}`} className="card stack stack-sm">
                  <div className="row row-between">
                    <strong>{formatDateTime(item.at)}</strong>
                    {item.type === "deadline" ? (
                      <Badge tone="warn" icon="clock">
                        Fecha límite
                      </Badge>
                    ) : item.status === "published" ? (
                      <Badge tone={item.confirmation === "platform" ? "ok" : "info"} icon="check">
                        {item.confirmation === "platform" ? "Publicado · plataforma" : "Publicado · a mano"}
                      </Badge>
                    ) : (
                      <Badge tone={item.status === "error" ? "bad" : "info"}>{item.status_label}</Badge>
                    )}
                  </div>
                  {item.platform ? <PlatformTag platform={item.platform} name={item.platform_name} /> : null}
                  <Link to={item.type === "deadline" ? `/proyectos/${item.project_id}` : `/proyectos/${item.project_id}?tab=publicar`}>{item.type === "deadline" ? item.project : item.title}</Link>
                  <span className="small soft">
                    {item.type === "deadline" ? "" : `${item.project} · `}
                    {itemNote(item)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
        <Panel title="Borradores sin programar">
          {(calendar.data?.unscheduled ?? []).length === 0 ? (
            <p className="muted">No hay borradores pendientes. Publisher los prepara cuando un proyecto supera la revisión previa.</p>
          ) : (
            <ul className="stack stack-sm">
              {(calendar.data?.unscheduled ?? []).map((item) => (
                <li key={item.id} className="card stack stack-sm">
                  <div className="row row-between">
                    <PlatformTag platform={item.platform} name={item.platform_name} />
                    {item.approved ? (
                      <Badge tone="ok" icon="check">
                        Contenido aprobado
                      </Badge>
                    ) : (
                      <Badge tone="warn" icon="eye">
                        Sin aprobar
                      </Badge>
                    )}
                  </div>
                  <Link to={`/proyectos/${item.project_id}?tab=publicar`}>{item.title}</Link>
                  <span className="small soft">{item.project}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </>
  );
}
