// HTTP client and the shapes the API returns. Every request goes to this same
// origin (relative URLs); the session lives in an HttpOnly cookie and every
// state-changing request carries the session's CSRF token.

export class ApiError extends Error {
  status: number;
  code: string;
  data: Record<string, unknown>;

  constructor(status: number, message: string, code = "", data: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.data = data;
  }
}

let csrfToken = "";
let onUnauthenticated: (() => void) | null = null;

export function setCsrf(token: string): void {
  csrfToken = token;
}

export function setUnauthenticatedHandler(handler: (() => void) | null): void {
  onUnauthenticated = handler;
}

async function parse<T>(response: Response): Promise<T> {
  const isJson = (response.headers.get("content-type") ?? "").includes("application/json");
  const payload = isJson ? await response.json() : null;
  if (!response.ok) {
    const message = payload?.message ?? `El servidor respondió con un error ${response.status}.`;
    const error = new ApiError(response.status, message, payload?.code ?? "", payload ?? {});
    if (response.status === 401 && error.code === "unauthenticated") onUnauthenticated?.();
    throw error;
  }
  return payload as T;
}

export async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken;
  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError(0, "No se pudo conectar con el servidor. Comprueba tu conexión e inténtalo de nuevo.", "network");
  }
  return parse<T>(response);
}

export const api = {
  get: <T>(url: string) => request<T>("GET", url),
  post: <T>(url: string, body: unknown = {}) => request<T>("POST", url, body),
  put: <T>(url: string, body: unknown) => request<T>("PUT", url, body),
  patch: <T>(url: string, body: unknown) => request<T>("PATCH", url, body),
  delete: <T>(url: string) => request<T>("DELETE", url),
};

/** Uploads a file as the raw request body and reports real progress. */
export function uploadFile<T>(
  url: string,
  file: File,
  onProgress?: (fraction: number) => void,
  signal?: AbortSignal,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("X-CSRF-Token", csrfToken);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.responseType = "json";
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress?.(event.loaded / event.total);
    };
    xhr.onerror = () => reject(new ApiError(0, "La subida se interrumpió. Comprueba tu conexión e inténtalo de nuevo.", "network"));
    xhr.onabort = () => reject(new ApiError(0, "Subida cancelada.", "aborted"));
    xhr.onload = () => {
      const payload = (xhr.response ?? {}) as Record<string, unknown>;
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload as T);
      else reject(new ApiError(xhr.status, (payload.message as string) ?? `Error ${xhr.status} al subir el archivo.`, (payload.code as string) ?? "", payload));
    };
    signal?.addEventListener("abort", () => xhr.abort());
    xhr.send(file);
  });
}

export function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "" && value !== false) search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

// --- Shapes ------------------------------------------------------------------

export type CheckStatus = "pass" | "warn" | "fail" | "info";

export interface Check {
  id?: string;
  status: CheckStatus;
  label: string;
  detail: string;
  area?: string;
  action?: string;
}

export interface AgentInfo {
  key: string;
  name: string;
  specialty: string;
  description: string;
  color: string;
}

export interface StepDef {
  kind: string;
  agent: string;
  title: string;
  produces: string;
  depends_on: string[];
  requires_approval: boolean;
  trigger: "auto" | "data";
  waiting_hint: string;
}

export interface PlatformInfo {
  key: string;
  name: string;
  provider: string;
  format: string;
  title_max: number;
  description_max: number;
  hashtags_max: number | null;
  tags_total_max: number | null;
  duration_max_s: number;
  duration_note: string;
  cover: string;
  docs_url: string;
}

export interface TemplateInfo {
  id: string;
  name: string;
  description: string;
  needs_video: boolean;
  platforms: string[];
  goal: string;
  options: Record<string, number>;
  structure: string[];
}

export interface Catalog {
  agents: AgentInfo[];
  steps: StepDef[];
  step_status_labels: Record<string, string>;
  platforms: PlatformInfo[];
  formats: Record<string, { width: number; height: number }>;
  templates: TemplateInfo[];
  languages: Record<string, string>;
  fonts: string[];
  layouts: string[];
  metric_fields: Record<string, { label: string; unit: string; whole: boolean }>;
  comment_categories: Record<string, string>;
  publication_status_labels: Record<string, string>;
}

export interface EngineInfo {
  mode: "demo" | "provider";
  provider: string;
  model: string;
  source: string;
  label: string;
  base_url?: string;
}

export interface Capabilities {
  engine: EngineInfo;
  ffmpeg: { available: boolean; detail: string };
  transcription: { configured: boolean; model: string; missing: string[]; alternative: string };
}

export interface Session {
  user: { id: string; name: string; email: string };
  /** `timezone` is an IANA name; `timezone_label` is how sentences name it ("hora de Madrid"). */
  workspace: { id: string; name: string; timezone: string; timezone_label: string };
  role: "owner" | "editor" | "viewer";
  role_label: string;
  csrf: string;
  workspaces: { id: string; name: string; role: string }[];
  capabilities: Capabilities;
  catalog: Catalog;
  limits: { max_upload_mb: number };
}

export interface Run {
  id: string;
  project_id: string | null;
  step_id: string | null;
  agent: string;
  kind: string;
  title: string;
  engine: string;
  provider: string;
  model: string;
  status: "queued" | "working" | "done" | "error" | "cancelled";
  progress: number | null;
  progress_label: string;
  usage: Record<string, unknown>;
  error: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  seconds: number | null;
  target_id: string | null;
  project?: string;
}

export interface Step {
  id: string;
  kind: string;
  agent: string;
  title: string;
  produces: string;
  depends_on: string[];
  requires_approval: boolean;
  trigger: "auto" | "data";
  waiting_hint: string;
  status: string;
  status_label: string;
  instructions: string;
  summary: string;
  output: Record<string, any>;
  blocked_code: string;
  blocked_reason: string;
  error: string;
  stale_reason: string;
  feedback: string;
  attempts: number;
  started_at: string | null;
  finished_at: string | null;
  decided_at: string | null;
  run: Run | null;
}

export interface Media {
  duration: number;
  video?: { width: number; height: number; codec: string; fps?: number } | null;
  audio?: { codec: string } | null;
}

export interface Asset {
  id: string;
  kind: string;
  filename: string;
  mime: string;
  size: number;
  url: string;
  poster_url: string | null;
  media: Media | null;
  rights_declared: boolean;
  rights_note: string;
  transcript: { available: boolean; source: string; segments: number };
  created_at: string;
}

export interface Clip {
  id: string;
  start: number;
  end: number;
  duration: number;
  title: string;
  reason: string;
  excerpt: string;
  signals: Record<string, any>;
  score: number | null;
  status: "suggested" | "selected" | "rejected";
  origin: "agent" | "user";
  position: number;
}

export interface RenderPiece {
  id: string;
  clip_id: string;
  format: string;
  settings: Record<string, any>;
  status: "pending" | "queued" | "rendering" | "ready" | "error" | "stale";
  progress: number | null;
  checks: Check[];
  error: string;
  origin: "agent" | "upload";
  url: string | null;
  poster_url: string | null;
  media: Media | null;
  size: number | null;
  filename: string | null;
  updated_at: string;
}

export interface Design {
  id: string;
  kind: "thumbnail" | "cover";
  spec: Record<string, any>;
  status: string;
  checks: Check[];
  version: number;
  approved: boolean;
  error: string;
  url: string | null;
  size: { width: number; height: number };
  updated_at: string;
}

export interface DocVersion {
  id: string;
  number: number;
  author_type: "agent" | "user";
  author: string;
  note: string;
  provider: string;
  created_at: string;
  is_current: boolean;
  content: Record<string, any>;
}

export interface DocumentInfo {
  kind: string;
  title: string;
  version: DocVersion;
  versions: number;
}

export interface Readiness {
  content_ready: boolean;
  approved: boolean;
  connected: boolean;
  can_publish_api: boolean;
  can_export: boolean;
  blockers: string[];
  warnings: string[];
  summary: string;
  connection_label: string;
  connection_detail: string;
  account_name: string;
}

export interface Publication {
  id: string;
  project_id: string;
  project?: string;
  platform: string;
  platform_name: string;
  provider: string;
  title: string;
  description: string;
  tags: string[];
  options: Record<string, any>;
  scheduled_at: string | null;
  schedule_mode: string;
  status: "draft" | "scheduled" | "publishing" | "published" | "error" | "discarded";
  status_label: string;
  approved: boolean;
  approved_at: string | null;
  confirmation: "" | "platform" | "manual";
  platform_post_id: string;
  url: string;
  published_at: string | null;
  progress: number | null;
  error: string;
  validation: Check[];
  readiness: Readiness;
  capabilities: { schedule: string; comments: boolean; reply: boolean; metrics: string[]; metrics_missing: string[]; comments_note: string; needs_public_url: boolean };
  render: { id: string; format: string; status: string; url: string | null; poster_url: string | null; media: Media | null; size: number | null } | null;
  clip_title: string;
  design: { id: string; kind: string; url: string | null } | null;
  /** Facts of the file that went out, once it went out; `render` may have been exported again since. */
  published_media: { duration: number | null; width: number | null; height: number | null; size: number | null } | null;
  /** What was replaced in the workspace after it went out: "el vídeo", "la portada". */
  changed_after_publication: string[];
  created_at: string;
  updated_at: string;
}

export interface ProjectSummary {
  id: string;
  title: string;
  topic: string;
  audience: string;
  goal: string;
  language: string;
  tone: string;
  platforms: string[];
  deadline: string | null;
  references: { url: string; note: string }[];
  template: string;
  options: Record<string, number>;
  state: "active" | "paused" | "cancelled";
  source_asset_id: string | null;
  created_at: string;
  updated_at: string;
  steps: { kind: string; agent: string; status: string; title: string; followup: boolean }[];
  /** Production steps only; the follow-up steps (comments, analytics) repeat with new data. */
  progress: { done: number; total: number };
  attention: { kind: string; status: string; title: string; agent: string; reason: string }[];
}

export interface ProjectDetail extends Omit<ProjectSummary, "steps"> {
  steps: Step[];
  source: Asset | null;
  documents: { research: DocumentInfo | null; script: DocumentInfo | null };
  clips: Clip[];
  renders: RenderPiece[];
  designs: Design[];
  images: Asset[];
  publications: Publication[];
  jobs: Run[];
  comment_counts: { total: number; pending_replies: number };
}

export interface ActivityEvent {
  id: number;
  type: string;
  message: string;
  actor_type: "user" | "agent" | "orchestrator" | "system" | "platform";
  actor: string;
  project_id: string | null;
  step_id: string | null;
  run_id: string | null;
  data: Record<string, any>;
  created_at: string;
  project?: string;
}

export interface StepBrief {
  project_id: string;
  project: string;
  kind: string;
  title: string;
  summary: string;
  reason: string;
  since: string | null;
}

export interface OfficeAgent extends AgentInfo {
  status: "working" | "error" | "awaiting_approval" | "blocked" | "queued" | "idle";
  status_label: string;
  current: Run[];
  queue: Run[];
  awaiting_approval: StepBrief[];
  blocked: StepBrief[];
  errors: StepBrief[];
  paused: StepBrief[];
  last_run: Run | null;
}

export interface OfficeState {
  agents: OfficeAgent[];
  projects: { total: number; active: number };
  pending_publications: number;
  pending_replies: number;
  unread_notifications: number;
  capabilities: Capabilities;
  events: ActivityEvent[];
  projects_list: ProjectSummary[];
}

export interface NotificationItem {
  id: string;
  type: "done" | "error" | "approval" | "reminder";
  title: string;
  body: string;
  link: string;
  read: boolean;
  created_at: string;
}

export interface CommentItem {
  id: string;
  project_id: string;
  project?: string;
  publication_id: string | null;
  platform: string;
  platform_name: string;
  source: "platform" | "manual";
  author: string;
  text: string;
  posted_at: string | null;
  category: string;
  category_by: string;
  category_reason: string;
  reply_text: string;
  reply_status: string;
  reply_by: string;
  reply_sent_at: string | null;
  can_send: boolean;
  created_at: string;
}

export interface IntegrationStatus {
  provider: string;
  name: string;
  state: "connected" | "expired" | "needs_setup" | "ready";
  label: string;
  detail: string;
  warning: string;
  account_name: string;
  account_id: string;
  scopes: string[];
  requested_scopes: string[];
  connected_at: string | null;
  missing_settings: string[];
  redirect_uri: string;
  requirements: string[];
  capabilities: Publication["capabilities"];
  platforms: string[];
  manual_alternative: string;
}

export type LiveMessage =
  | ({ kind: "event" } & ActivityEvent)
  | { kind: "notification"; id: string; type: string; title: string; body: string; link: string }
  | { kind: "progress"; run_id: string; agent: string; progress: number | null; label: string }
  | { kind: "hello" }
  | { kind: "resync" };
