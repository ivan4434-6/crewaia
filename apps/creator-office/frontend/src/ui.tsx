// Shared interface pieces. Plain HTML controls with a pixel-art skin.

import { useEffect, useId, useRef, useState } from "react";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { ApiError } from "./api";
import type { Check, CheckStatus } from "./api";
import { ICONS } from "./icons";
import type { IconName } from "./icons";

export function Icon({ name, size = 16, className = "" }: { name: IconName; size?: number; className?: string }) {
  const rows = ICONS[name];
  const rects: ReactNode[] = [];
  rows.forEach((row, y) => {
    // Merge horizontal runs so each icon is a handful of rects.
    let x = 0;
    while (x < row.length) {
      if (row[x] !== "#") {
        x++;
        continue;
      }
      let end = x;
      while (end < row.length && row[end] === "#") end++;
      rects.push(<rect key={`${x}-${y}`} x={x} y={y} width={end - x} height={1} />);
      x = end;
    }
  });
  return (
    <svg className={`icon ${className}`} width={size} height={size} viewBox="0 0 12 12" shapeRendering="crispEdges" fill="currentColor" aria-hidden="true" focusable="false">
      {rects}
    </svg>
  );
}

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  icon?: IconName;
  busy?: boolean;
  small?: boolean;
}

export function Button({ variant = "secondary", icon, busy = false, small = false, children, className = "", disabled, type = "button", ...rest }: ButtonProps) {
  return (
    <button type={type} className={`btn btn-${variant} ${small ? "btn-small" : ""} ${className}`} disabled={disabled || busy} aria-busy={busy || undefined} {...rest}>
      {busy ? <span className="spinner" aria-hidden="true" /> : icon ? <Icon name={icon} size={small ? 12 : 14} /> : null}
      {children ? <span>{children}</span> : null}
    </button>
  );
}

export type Tone = "neutral" | "ok" | "warn" | "bad" | "info" | "accent";

export function Badge({ tone = "neutral", icon, children, title }: { tone?: Tone; icon?: IconName; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {icon ? <Icon name={icon} size={10} /> : null}
      {children}
    </span>
  );
}

const STEP_TONES: Record<string, { tone: Tone; icon: IconName }> = {
  pending: { tone: "neutral", icon: "clock" },
  queued: { tone: "info", icon: "clock" },
  working: { tone: "accent", icon: "play" },
  awaiting_approval: { tone: "warn", icon: "eye" },
  completed: { tone: "ok", icon: "check" },
  blocked: { tone: "bad", icon: "lock" },
  error: { tone: "bad", icon: "cross" },
  paused: { tone: "neutral", icon: "pause" },
  cancelled: { tone: "neutral", icon: "stop" },
  skipped: { tone: "neutral", icon: "check" },
};

export function StepBadge({ status, label }: { status: string; label: string }) {
  const style = STEP_TONES[status] ?? STEP_TONES.pending;
  return (
    <Badge tone={style.tone} icon={style.icon}>
      {label}
    </Badge>
  );
}

export function stepTone(status: string): Tone {
  return (STEP_TONES[status] ?? STEP_TONES.pending).tone;
}

export function Panel({ title, actions, children, tone, className = "", id }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; tone?: Tone; className?: string; id?: string }) {
  return (
    <section className={`panel ${tone ? `panel-${tone}` : ""} ${className}`} id={id}>
      {title || actions ? (
        <header className="panel-head">
          {title ? <h2>{title}</h2> : <span />}
          {actions ? <div className="panel-actions">{actions}</div> : null}
        </header>
      ) : null}
      {children}
    </section>
  );
}

export function Modal({ title, onClose, children, footer, wide = false }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const node = ref.current;
    const focusable = node?.querySelector<HTMLElement>("[data-autofocus], input, textarea, select, button");
    (focusable ?? node)?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
      }
      if (event.key === "Tab" && node) {
        const items = Array.from(node.querySelectorAll<HTMLElement>("a[href], button:not([disabled]), input:not([disabled]), textarea, select, [tabindex]:not([tabindex='-1'])"));
        if (!items.length) return;
        const first = items[0];
        const last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    document.body.classList.add("modal-open");
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.classList.remove("modal-open");
      previous?.focus?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className={`modal ${wide ? "modal-wide" : ""}`} role="dialog" aria-modal="true" aria-labelledby={titleId} ref={ref} tabIndex={-1}>
        <header className="modal-head">
          <h2 id={titleId}>{title}</h2>
          <button type="button" className="icon-btn" aria-label="Cerrar" onClick={onClose}>
            <Icon name="cross" size={14} />
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {footer ? <footer className="modal-foot">{footer}</footer> : null}
      </div>
    </div>
  );
}

export function Field({ label, hint, error, children, counter }: { label: string; hint?: ReactNode; error?: string; children: ReactNode; counter?: ReactNode }) {
  return (
    <label className={`field ${error ? "field-error" : ""}`}>
      <span className="field-label">
        <span>{label}</span>
        {counter ? <span className="field-counter">{counter}</span> : null}
      </span>
      {children}
      {error ? <span className="field-msg field-msg-error">{error}</span> : hint ? <span className="field-msg">{hint}</span> : null}
    </label>
  );
}

export function Counter({ value, max }: { value: number; max: number | null | undefined }) {
  if (!max) return <>{value}</>;
  return <span className={value > max ? "over" : ""}>{`${value}/${max}`}</span>;
}

const CHECK_ICON: Record<CheckStatus, IconName> = { pass: "check", warn: "warning", fail: "cross", info: "info" };
const CHECK_WORD: Record<CheckStatus, string> = { pass: "Correcto", warn: "Aviso", fail: "No cumple", info: "Nota" };

export function CheckList({ checks, compact = false }: { checks: Check[]; compact?: boolean }) {
  if (!checks.length) return null;
  return (
    <ul className={`checks ${compact ? "checks-compact" : ""}`}>
      {checks.map((item, index) => (
        <li key={`${item.id}-${index}`} className={`check check-${item.status}`}>
          <span className="check-icon" title={CHECK_WORD[item.status]}>
            <Icon name={CHECK_ICON[item.status]} size={12} />
            <span className="sr-only">{CHECK_WORD[item.status]}:</span>
          </span>
          <span>
            <strong>{item.label}</strong>
            {item.detail ? <span className="check-detail"> — {item.detail}</span> : null}
            {item.action ? <span className="check-action"> {item.action}</span> : null}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function ProgressBar({ value, label }: { value: number | null | undefined; label?: string }) {
  const known = typeof value === "number";
  const percent = known ? Math.round(Math.max(0, Math.min(1, value)) * 100) : null;
  return (
    <div className="progress-wrap">
      <div
        className={`progress ${known ? "" : "progress-unknown"}`}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent ?? undefined}
        aria-label={label || "Progreso"}
      >
        <div className="progress-fill" style={known ? { width: `${percent}%` } : undefined} />
      </div>
      <div className="progress-text">
        <span>{label || (known ? "" : "En curso")}</span>
        {known ? <span>{percent}%</span> : null}
      </div>
    </div>
  );
}

export function Empty({ icon = "info", title, children, action }: { icon?: IconName; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <Icon name={icon} size={36} />
      <h3>{title}</h3>
      {children ? <p>{children}</p> : null}
      {action}
    </div>
  );
}

export function Loading({ label = "Cargando…" }: { label?: string }) {
  return (
    <div className="loading" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function ErrorBox({ error, onRetry }: { error: ApiError | Error; onRetry?: () => void }) {
  return (
    <div className="notice notice-bad" role="alert">
      <Icon name="warning" />
      <div>
        <strong>No se pudo cargar.</strong> {error.message}
        {onRetry ? (
          <div className="notice-actions">
            <Button small icon="retry" onClick={onRetry}>
              Reintentar
            </Button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function Notice({ tone = "info", icon, title, children, actions }: { tone?: Tone; icon?: IconName; title?: ReactNode; children?: ReactNode; actions?: ReactNode }) {
  const fallback: Record<Tone, IconName> = { neutral: "info", info: "info", ok: "check", warn: "warning", bad: "warning", accent: "sparkle" };
  return (
    <div className={`notice notice-${tone}`}>
      <Icon name={icon ?? fallback[tone]} />
      <div>
        {title ? <strong>{title} </strong> : null}
        {children}
        {actions ? <div className="notice-actions">{actions}</div> : null}
      </div>
    </div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange, label }: { tabs: { id: T; label: ReactNode; badge?: ReactNode }[]; value: T; onChange: (id: T) => void; label: string }) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((tab) => (
        <button key={tab.id} type="button" role="tab" aria-selected={tab.id === value} className={`tab ${tab.id === value ? "tab-on" : ""}`} onClick={() => onChange(tab.id)}>
          {tab.label}
          {tab.badge ? <span className="tab-badge">{tab.badge}</span> : null}
        </button>
      ))}
    </div>
  );
}

export function Confirm({ title, children, confirmLabel, danger = false, busy = false, onConfirm, onClose }: { title: string; children: ReactNode; confirmLabel: string; danger?: boolean; busy?: boolean; onConfirm: () => void; onClose: () => void }) {
  return (
    <Modal
      title={title}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancelar
          </Button>
          <Button variant={danger ? "danger" : "primary"} busy={busy} onClick={onConfirm} data-autofocus>
            {confirmLabel}
          </Button>
        </>
      }
    >
      {children}
    </Modal>
  );
}

/** A list of short strings edited as removable chips. */
export function ChipInput({ values, onChange, placeholder, prefix = "", max = 30 }: { values: string[]; onChange: (values: string[]) => void; placeholder: string; prefix?: string; max?: number }) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const parts = draft
      .split(/[,\n]/)
      .map((part) => part.trim())
      .filter(Boolean)
      .map((part) => (prefix && !part.startsWith(prefix) ? prefix + part : part));
    if (!parts.length) return;
    onChange(Array.from(new Set([...values, ...parts])).slice(0, max));
    setDraft("");
  };
  return (
    <div className="chip-input">
      {values.map((value) => (
        <span key={value} className="chip">
          {value}
          <button type="button" aria-label={`Quitar ${value}`} onClick={() => onChange(values.filter((item) => item !== value))}>
            ×
          </button>
        </span>
      ))}
      <input
        value={draft}
        placeholder={placeholder}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === ",") {
            event.preventDefault();
            add();
          } else if (event.key === "Backspace" && !draft && values.length) {
            onChange(values.slice(0, -1));
          }
        }}
        onBlur={add}
      />
    </div>
  );
}

export function FileButton({ label, accept, onFile, icon = "upload", variant = "secondary", disabled = false, small = false }: { label: string; accept: string; onFile: (file: File) => void; icon?: IconName; variant?: ButtonVariant; disabled?: boolean; small?: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <input
        ref={input}
        type="file"
        accept={accept}
        className="sr-only"
        tabIndex={-1}
        aria-hidden="true"
        disabled={disabled}
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) onFile(file);
          event.target.value = "";
        }}
      />
      <Button variant={variant} icon={icon} small={small} disabled={disabled} onClick={() => input.current?.click()}>
        {label}
      </Button>
    </>
  );
}

export function EngineTag({ label, demo }: { label: string; demo: boolean }) {
  return (
    <Badge tone={demo ? "warn" : "accent"} icon={demo ? "info" : "sparkle"} title={demo ? "Sin proveedor de IA: los textos son plantillas con marcadores por completar." : "Generado con el proveedor de IA configurado."}>
      {demo ? "Modo demostración" : label}
    </Badge>
  );
}
