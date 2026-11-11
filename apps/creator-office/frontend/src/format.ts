// Formatting helpers. All user-facing text is Spanish; times are shown in the
// viewer's own time zone.

const dateTime = new Intl.DateTimeFormat("es", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const dateOnly = new Intl.DateTimeFormat("es", { day: "numeric", month: "long", year: "numeric" });
const timeOnly = new Intl.DateTimeFormat("es", { hour: "2-digit", minute: "2-digit" });
const relative = new Intl.RelativeTimeFormat("es", { numeric: "auto" });
const number = new Intl.NumberFormat("es");

export function formatDateTime(iso: string | null | undefined): string {
  return iso ? dateTime.format(new Date(iso)) : "—";
}

export function formatDate(iso: string | null | undefined): string {
  return iso ? dateOnly.format(new Date(iso)) : "—";
}

export function formatTime(iso: string | null | undefined): string {
  return iso ? timeOnly.format(new Date(iso)) : "—";
}

export function formatNumber(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : number.format(value);
}

const usd = new Intl.NumberFormat("es-ES", { minimumFractionDigits: 4, maximumFractionDigits: 4 });

/** An estimated cost in US dollars, with the cents of a cent that token prices need: "0,0123 USD". */
export function formatCost(value: number): string {
  return `${usd.format(value)} USD`;
}

/** "hace 3 minutos", "en 2 días"… relative to `now`. */
export function timeAgo(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const seconds = Math.round((new Date(iso).getTime() - now) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 45) return seconds <= 0 ? "ahora mismo" : "en unos segundos";
  if (abs < 3600) return relative.format(Math.round(seconds / 60), "minute");
  if (abs < 86400) return relative.format(Math.round(seconds / 3600), "hour");
  if (abs < 86400 * 30) return relative.format(Math.round(seconds / 86400), "day");
  return formatDate(iso);
}

/** For things that already happened: a stale `now` or a clock that is a little off never reads as the future. */
export function timeSince(iso: string | null | undefined, now: number = Date.now()): string {
  return iso ? timeAgo(iso, Math.max(now, new Date(iso).getTime())) : "—";
}

/** 83.4 → "1:23"; 3725 → "1:02:05". */
export function formatClock(totalSeconds: number | null | undefined): string {
  if (totalSeconds === null || totalSeconds === undefined || Number.isNaN(totalSeconds)) return "—";
  const whole = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const seconds = whole % 60;
  const mm = hours ? String(minutes).padStart(2, "0") : String(minutes);
  return (hours ? `${hours}:` : "") + `${mm}:${String(seconds).padStart(2, "0")}`;
}

/** 83.46 → "1:23.5". One decimal and no hours, so it round-trips through parseClock when trimming. */
export function formatPrecise(totalSeconds: number): string {
  const tenths = Math.round(Math.max(0, totalSeconds) * 10);
  const minutes = Math.floor(tenths / 600);
  const rest = (tenths - minutes * 600) / 10;
  return `${minutes}:${rest.toFixed(1).padStart(4, "0")}`;
}

/** 4.2 → "4 s"; 95 → "1 min 35 s"; 3700 → "1 h 1 min". */
export function formatDuration(totalSeconds: number | null | undefined): string {
  if (totalSeconds === null || totalSeconds === undefined) return "—";
  if (totalSeconds < 1) return "menos de 1 s";
  const whole = Math.round(totalSeconds);
  if (whole < 60) return `${whole} s`;
  if (whole < 3600) return `${Math.floor(whole / 60)} min ${whole % 60} s`;
  return `${Math.floor(whole / 3600)} h ${Math.floor((whole % 3600) / 60)} min`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 / 1024).toFixed(1).replace(".", ",")} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2).replace(".", ",")} GB`;
}

export function plural(count: number, singular: string, pluralForm?: string): string {
  return `${count} ${count === 1 ? singular : (pluralForm ?? `${singular}s`)}`;
}

/** Value for <input type="datetime-local"> in the viewer's time zone. */
export function toLocalInput(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return shifted.toISOString().slice(0, 16);
}

export function fromLocalInput(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

/** Parses "1:23", "83", "1:02:05" or "83,5" into seconds. */
export function parseClock(text: string): number | null {
  const clean = text.trim().replace(",", ".");
  if (!clean) return null;
  const parts = clean.split(":").map((part) => Number(part));
  if (parts.some((part) => Number.isNaN(part) || part < 0)) return null;
  return parts.reduce((total, part) => total * 60 + part, 0);
}

/** The IANA zone of this browser ("Europe/Madrid"), or "" when it cannot be told. */
export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

/** Zones the browser knows, for the workspace setting. Older browsers get a short list. */
export function knownTimeZones(): string[] {
  const intl = Intl as typeof Intl & { supportedValuesOf?: (key: string) => string[] };
  try {
    const zones = intl.supportedValuesOf?.("timeZone") ?? [];
    if (zones.length) return zones.includes("UTC") ? zones : ["UTC", ...zones];
  } catch {
    // fall through to the short list
  }
  return ["UTC", "Europe/Madrid", "Atlantic/Canary", "America/Mexico_City", "America/Bogota", "America/Lima", "America/Santiago", "America/Argentina/Buenos_Aires"];
}

export type Figure = { ok: true; value: number } | { ok: false; reason: "format" | "whole" | "ambiguous" };

/**
 * Read a figure typed by a person, without guessing.
 *
 * "1.840" is 1840 in Spain and 1.84 elsewhere. For a count (`whole`) it can only
 * be 1840. For a figure with decimals it could be either, so it is refused and
 * the person retypes it without the thousands mark.
 */
export function readFigure(text: string, whole: boolean): Figure {
  const clean = text.trim().replace(/[\s\u00a0\u202f]/g, "");
  if (/^\d+$/.test(clean)) return { ok: true, value: Number(clean) };
  const grouped = /^\d{1,3}([.,]\d{3})+$/.test(clean) && new Set(clean.replace(/\d/g, "")).size === 1;
  const decimal = /^\d+[.,]\d+$/.test(clean);
  if (whole) {
    if (grouped) return { ok: true, value: Number(clean.replace(/[.,]/g, "")) };
    return { ok: false, reason: decimal ? "whole" : "format" };
  }
  if (grouped && decimal) return { ok: false, reason: "ambiguous" };
  if (grouped) return { ok: true, value: Number(clean.replace(/[.,]/g, "")) };
  if (decimal) return { ok: true, value: Number(clean.replace(",", ".")) };
  return { ok: false, reason: "format" };
}
