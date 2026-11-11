// Pieces several screens share: agent lookups, activity lines, live run progress.

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { ActivityEvent, AgentInfo, Run } from "../api";
import { formatCost, formatDateTime, timeSince } from "../format";
import { useLive, useSession } from "../live";
import { Badge, ProgressBar } from "../ui";

export function useAgents(): Record<string, AgentInfo> {
  const { catalog } = useSession();
  return useMemo(() => Object.fromEntries(catalog.agents.map((agent) => [agent.key, agent])), [catalog]);
}

/** Re-renders on an interval so relative times ("hace 3 minutos") stay true. */
export function useNow(intervalMs = 30000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}

export function PlatformTag({ platform, name }: { platform: string; name?: string }) {
  const { catalog } = useSession();
  const label = name ?? catalog.platforms.find((item) => item.key === platform)?.name ?? platform;
  return (
    <span className={`platform-tag platform-${platform}`}>
      <i aria-hidden="true" />
      {label}
    </span>
  );
}

const ACTOR_LABEL: Record<ActivityEvent["actor_type"], string> = {
  user: "Persona",
  agent: "Agente",
  orchestrator: "Orquestador",
  system: "Sistema",
  platform: "Plataforma",
};

export function ActorMark({ event }: { event: ActivityEvent }) {
  const agents = useAgents();
  const agent = event.actor_type === "agent" ? agents[event.actor] : undefined;
  let background = "#3a3f63";
  let letter = "·";
  let color = "#f1f2fb";
  let title = ACTOR_LABEL[event.actor_type];
  if (agent) {
    background = agent.color;
    letter = agent.name[0];
    color = "#07080e";
    title = `Agente: ${agent.name}`;
  } else if (event.actor_type === "user") {
    background = "#f1f2fb";
    letter = (event.actor[0] ?? "?").toUpperCase();
    color = "#07080e";
    title = `Persona: ${event.actor}`;
  } else if (event.actor_type === "orchestrator") {
    background = "#6b7190";
    letter = "→";
  } else if (event.actor_type === "platform") {
    background = "#fbbf24";
    letter = "P";
    color = "#07080e";
    title = `Plataforma: ${event.actor}`;
  }
  return (
    <span className="feed-who" style={{ background, color }} title={title} aria-hidden="true">
      {letter}
    </span>
  );
}

export function actorName(event: ActivityEvent, agents: Record<string, AgentInfo>): string {
  if (event.actor_type === "agent") return agents[event.actor]?.name ?? event.actor;
  if (event.actor_type === "user") return event.actor;
  if (event.actor_type === "platform") return event.actor || "Plataforma";
  return ACTOR_LABEL[event.actor_type];
}

export function EventLine({ event, now, fresh = false, showProject = true, absolute = false }: { event: ActivityEvent; now: number; fresh?: boolean; showProject?: boolean; absolute?: boolean }) {
  const agents = useAgents();
  return (
    <li className={`feed-item ${fresh ? "fresh" : ""}`}>
      <ActorMark event={event} />
      <div>
        <div>{event.message}</div>
        <div className="feed-meta">
          <span>{actorName(event, agents)}</span>
          <span aria-hidden="true">·</span>
          <time dateTime={event.created_at} title={formatDateTime(event.created_at)}>
            {absolute ? formatDateTime(event.created_at) : timeSince(event.created_at, now)}
          </time>
          {showProject && event.project_id && event.project ? (
            <>
              <span aria-hidden="true">·</span>
              <Link to={`/proyectos/${event.project_id}`}>{event.project}</Link>
            </>
          ) : null}
        </div>
      </div>
    </li>
  );
}

/** Progress of a job: the server's last stored value, refined by live ticks while it runs. */
export function RunProgress({ run, fallbackLabel }: { run: Run; fallbackLabel?: string }) {
  const { progress } = useLive();
  const live = progress[run.id];
  const value = live ? live.progress : run.progress;
  const label = (live ? live.label : run.progress_label) || fallbackLabel || (run.status === "queued" ? "En cola" : "En curso");
  return <ProgressBar value={run.status === "queued" ? 0 : value} label={label} />;
}

const RUN_STATUS: Record<Run["status"], { label: string; tone: "info" | "accent" | "ok" | "bad" | "neutral" }> = {
  queued: { label: "En cola", tone: "info" },
  working: { label: "En marcha", tone: "accent" },
  done: { label: "Terminada", tone: "ok" },
  error: { label: "Error", tone: "bad" },
  cancelled: { label: "Cancelada", tone: "neutral" },
};

export function RunStatusBadge({ status }: { status: Run["status"] }) {
  const style = RUN_STATUS[status] ?? RUN_STATUS.queued;
  return <Badge tone={style.tone}>{style.label}</Badge>;
}

/** How a finished job was produced and what it measurably cost. */
export function usageSummary(run: Run): string {
  const usage = run.usage as { engine?: string; model?: string; total_tokens?: number; llm_calls?: number; cost_usd?: number | null; cost_basis?: string };
  if (usage.engine === "demo") return "Motor local de demostración · sin coste de IA";
  if (usage.engine === "provider") {
    const parts = [usage.model ?? run.model];
    if (usage.total_tokens) parts.push(`${usage.total_tokens.toLocaleString("es")} tokens`);
    else parts.push("el proveedor no informó de tokens");
    if (typeof usage.cost_usd === "number") parts.push(`≈ ${formatCost(usage.cost_usd)} (estimado)`);
    return parts.join(" · ");
  }
  return "Herramienta local · sin IA";
}

export function Segmented<T extends string>({ value, options, onChange, label }: { value: T; options: { id: T; label: ReactNode }[]; onChange: (value: T) => void; label: string }) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((option) => (
        <button key={option.id} type="button" aria-pressed={option.id === value} onClick={() => onChange(option.id)}>
          {option.label}
        </button>
      ))}
    </div>
  );
}

/** Highlights [bracketed] gaps the demo engine (or a person) left to be filled in. */
export function WithPlaceholders({ text }: { text: string }) {
  const parts = text.split(/(\[[^\]\n]{2,}\])/g);
  return (
    <>
      {parts.map((part, index) =>
        part.startsWith("[") && part.endsWith("]") ? (
          <mark key={index} className="placeholder-mark" title="Marcador por completar">
            {part}
          </mark>
        ) : (
          <span key={index}>{part}</span>
        ),
      )}
    </>
  );
}

/** One pip per pipeline step, coloured by its real status. */
export function Pips({ steps }: { steps: { kind: string; status: string; title: string; followup?: boolean }[] }) {
  const { catalog } = useSession();
  const describe = (step: { status: string; title: string; followup?: boolean }) =>
    `${step.title}${step.followup ? " (seguimiento)" : ""}: ${catalog.step_status_labels[step.status] ?? step.status}`;
  const firstFollowup = steps.findIndex((step) => step.followup);
  return (
    <div className="pips" role="img" aria-label={steps.map(describe).join(". ")}>
      {steps.map((step, index) => (
        <span key={step.kind} className={`pip pip-${step.status}${index === firstFollowup && index > 0 ? " pip-gap" : ""}`} title={describe(step)} />
      ))}
    </div>
  );
}
