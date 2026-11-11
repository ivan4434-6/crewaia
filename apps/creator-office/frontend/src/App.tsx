// Routes and the application shell (sidebar, top bar, notifications, search).

import { createContext, useContext, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { api, query } from "./api";
import type { NotificationItem, OfficeState, Session } from "./api";
import { browserTimeZone, formatDateTime, timeSince } from "./format";
import type { IconName } from "./icons";
import { LiveProvider, useAction, useLive, useLiveMessages, useResource, useSession, useSessionState, useToast } from "./live";
import type { Resource } from "./live";
import { ActivityPage } from "./pages/Activity";
import { AnalyticsPage } from "./pages/Analytics";
import { AuthPage, JoinPage } from "./pages/Auth";
import { BrandPage } from "./pages/Brand";
import { CalendarPage } from "./pages/Calendar";
import { CommunityPage } from "./pages/Community";
import { IntegrationsPage } from "./pages/Integrations";
import { OfficePage } from "./pages/Office";
import { ProjectPage } from "./pages/Project";
import { NewProjectPage, ProjectsPage } from "./pages/Projects";
import { TeamPage } from "./pages/Team";
import { Badge, Button, Empty, Field, Icon, Loading, Modal } from "./ui";

export function App() {
  const { session, loading } = useSessionState();
  const location = useLocation();
  if (loading) {
    return (
      <div className="screen-center">
        <Loading label="Abriendo la oficina…" />
      </div>
    );
  }
  const back = `${location.pathname}${location.search}`;
  return (
    <Routes>
      <Route path="/entrar" element={session ? <Navigate to="/" replace /> : <AuthPage mode="login" />} />
      <Route path="/registro" element={session ? <JoinPage /> : <AuthPage mode="register" />} />
      <Route
        path="/*"
        element={
          session ? (
            <LiveProvider key={session.workspace.id}>
              <Shell />
            </LiveProvider>
          ) : (
            <Navigate to={back === "/" ? "/entrar" : `/entrar${query({ volver: back })}`} replace />
          )
        }
      />
    </Routes>
  );
}

// --- Shared office state -------------------------------------------------------

const OfficeContext = createContext<Resource<OfficeState> | null>(null);

/** The live office overview, loaded once by the shell and shared with every page. */
export function useOffice(): Resource<OfficeState> {
  const value = useContext(OfficeContext);
  if (!value) throw new Error("Shell missing");
  return value;
}

export function useTitle(title: string): void {
  useEffect(() => {
    document.title = title ? `${title} · Creator Office AI` : "Creator Office AI";
  }, [title]);
}

// --- Shell ---------------------------------------------------------------------

const NAV: { to: string; label: string; icon: IconName; end?: boolean; count?: (office: OfficeState) => number }[] = [
  {
    to: "/",
    label: "Oficina",
    icon: "office",
    end: true,
    count: (office) => office.agents.reduce((total, agent) => total + agent.awaiting_approval.length + agent.errors.length + agent.blocked.length, 0),
  },
  { to: "/proyectos", label: "Proyectos", icon: "folder" },
  { to: "/calendario", label: "Calendario", icon: "calendar", count: (office) => office.pending_publications },
  { to: "/comunidad", label: "Comunidad", icon: "chat", count: (office) => office.pending_replies },
  { to: "/analitica", label: "Analítica", icon: "chart" },
];

const NAV_SETTINGS: { to: string; label: string; icon: IconName }[] = [
  { to: "/marca", label: "Kit de marca", icon: "palette" },
  { to: "/integraciones", label: "Integraciones", icon: "plug" },
  { to: "/equipo", label: "Equipo", icon: "team" },
  { to: "/actividad", label: "Actividad", icon: "list" },
];

function Shell() {
  const session = useSession();
  const live = useLive();
  const toast = useToast();
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const office = useResource<OfficeState>(
    () => api.get("/api/office"),
    [session.workspace.id],
    (message) => message.kind === "event" || message.kind === "notification",
  );

  useLiveMessages((message) => {
    if (message.kind === "notification") toast.info(message.title);
  });

  useEffect(() => setMenuOpen(false), [location.pathname]);

  const engine = office.data?.capabilities.engine ?? session.capabilities.engine;
  return (
    <OfficeContext.Provider value={office}>
      <div className="shell">
        {menuOpen ? <button type="button" className="sidebar-scrim" aria-label="Cerrar menú" onClick={() => setMenuOpen(false)} /> : null}
        <aside className={`sidebar ${menuOpen ? "open" : ""}`} aria-label="Navegación principal">
          <Link to="/" className="brand">
            <BrandMark />
            <span className="brand-name">
              Creator Office
              <span>AI</span>
            </span>
          </Link>
          <nav className="nav">
            {NAV.map((item) => {
              const count = office.data && item.count ? item.count(office.data) : 0;
              return (
                <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => (isActive ? "active" : "")}>
                  <Icon name={item.icon} />
                  {item.label}
                  {count > 0 ? (
                    <span className="nav-count" title="Pendiente de tu decisión">
                      {count}
                    </span>
                  ) : null}
                </NavLink>
              );
            })}
            <div className="nav-label">Configuración</div>
            {NAV_SETTINGS.map((item) => (
              <NavLink key={item.to} to={item.to} className={({ isActive }) => (isActive ? "active" : "")}>
                <Icon name={item.icon} />
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="sidebar-foot">
            <Link to="/integraciones" className="row" style={{ textDecoration: "none" }} title="Motor con el que trabajan los agentes">
              <Badge tone={engine.mode === "demo" ? "warn" : "accent"} icon={engine.mode === "demo" ? "info" : "sparkle"}>
                {engine.mode === "demo" ? "Modo demostración" : "IA conectada"}
              </Badge>
            </Link>
            <span className="muted">{engine.mode === "demo" ? "Sin proveedor de IA: los agentes usan sus herramientas reales y dejan [marcadores] donde haría falta un modelo." : engine.model}</span>
            <span className={`live-dot ${live.connected ? "" : "off"}`} role="status">
              {live.connected ? "En directo" : "Reconectando…"}
            </span>
          </div>
        </aside>
        <div className="main">
          <header className="topbar">
            <button type="button" className="icon-btn menu-btn" aria-label="Abrir menú" aria-expanded={menuOpen} onClick={() => setMenuOpen(true)}>
              <Icon name="menu" />
            </button>
            <Search />
            <span className="grow" />
            <WorkspaceSwitcher />
            <Notifications unread={office.data?.unread_notifications ?? 0} onChange={office.reload} />
            <UserMenu />
          </header>
          <main className="content" id="contenido">
            <Routes>
              <Route index element={<OfficePage />} />
              <Route path="proyectos" element={<ProjectsPage />} />
              <Route path="proyectos/nuevo" element={<NewProjectPage />} />
              <Route path="proyectos/:id" element={<ProjectPage />} />
              <Route path="calendario" element={<CalendarPage />} />
              <Route path="comunidad" element={<CommunityPage />} />
              <Route path="analitica" element={<AnalyticsPage />} />
              <Route path="integraciones" element={<IntegrationsPage />} />
              <Route path="marca" element={<BrandPage />} />
              <Route path="actividad" element={<ActivityPage />} />
              <Route path="equipo" element={<TeamPage />} />
              <Route path="*" element={<NotFound />} />
            </Routes>
          </main>
        </div>
      </div>
    </OfficeContext.Provider>
  );
}

export function BrandMark() {
  return (
    <svg className="brand-mark" viewBox="0 0 16 16" shapeRendering="crispEdges" aria-hidden="true">
      <rect width="16" height="16" fill="#07080e" />
      <rect x="1" y="1" width="14" height="14" fill="#232640" />
      <rect x="2" y="3" width="12" height="8" fill="#07080e" />
      <rect x="3" y="4" width="10" height="6" fill="#5eead4" />
      <rect x="5" y="6" width="2" height="2" fill="#07080e" />
      <rect x="9" y="6" width="2" height="2" fill="#07080e" />
      <rect x="7" y="11" width="2" height="2" fill="#07080e" />
      <rect x="4" y="13" width="8" height="1" fill="#fbbf24" />
    </svg>
  );
}

function NotFound() {
  useTitle("No encontrado");
  return (
    <Empty icon="search" title="Esta página no existe" action={<Link to="/">Volver a la oficina</Link>}>
      Puede que el enlace esté mal escrito o que el contenido se haya eliminado.
    </Empty>
  );
}

// --- Popovers ------------------------------------------------------------------

function usePopover() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return { open, setOpen, ref };
}

interface SearchResult {
  type: string;
  type_label: string;
  title: string;
  snippet: string;
  href: string;
  at?: string;
}

function Search() {
  const { open, setOpen, ref } = usePopover();
  const [text, setText] = useState("");
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [failed, setFailed] = useState("");
  const navigate = useNavigate();
  const sequence = useRef(0);

  useEffect(() => {
    const needle = text.trim();
    if (needle.length < 2) {
      setResults(null);
      return;
    }
    const mine = ++sequence.current;
    const timer = window.setTimeout(async () => {
      try {
        const data = await api.get<{ results: SearchResult[] }>(`/api/search${query({ q: needle })}`);
        if (mine === sequence.current) {
          setResults(data.results);
          setFailed("");
        }
      } catch (error) {
        if (mine === sequence.current) setFailed(error instanceof Error ? error.message : "No se pudo buscar.");
      }
    }, 250);
    return () => window.clearTimeout(timer);
  }, [text]);

  const go = (href: string) => {
    setOpen(false);
    setText("");
    navigate(href);
  };

  return (
    <div className="topbar-search anchor" ref={ref} role="search">
      <Icon name="search" size={14} />
      <input
        type="search"
        value={text}
        placeholder="Buscar proyectos, guiones, comentarios…"
        aria-label="Buscar en el espacio de trabajo"
        onChange={(event) => {
          setText(event.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && results?.length) go(results[0].href);
        }}
      />
      {open && text.trim().length >= 2 ? (
        <div className="popover popover-left" role="listbox" aria-label="Resultados">
          {failed ? <div className="popover-item">{failed}</div> : null}
          {!failed && results === null ? <div className="popover-item muted">Buscando…</div> : null}
          {!failed && results && results.length === 0 ? <div className="popover-item muted">Nada coincide con «{text.trim()}».</div> : null}
          {(results ?? []).map((result, index) => (
            <button key={`${result.href}-${index}`} type="button" className="popover-item" role="option" aria-selected={false} onClick={() => go(result.href)}>
              <span className="row">
                <Badge>{result.type_label}</Badge>
                <strong>{result.title || formatDateTime(result.at)}</strong>
              </span>
              <span className="small soft">{result.snippet}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

const NOTIFICATION_ICON: Record<NotificationItem["type"], IconName> = { done: "check", error: "warning", approval: "eye", reminder: "clock" };

function Notifications({ unread, onChange }: { unread: number; onChange: () => Promise<void> }) {
  const { open, setOpen, ref } = usePopover();
  const [items, setItems] = useState<NotificationItem[] | null>(null);
  const navigate = useNavigate();
  const toast = useToast();

  const load = async () => {
    try {
      const data = await api.get<{ notifications: NotificationItem[] }>("/api/notifications");
      setItems(data.notifications);
    } catch (error) {
      toast.error(error);
    }
  };

  useEffect(() => {
    if (open) void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useLiveMessages((message) => {
    if (open && message.kind === "notification") void load();
  });

  const markRead = async (body: { ids?: string[]; all?: boolean }) => {
    try {
      await api.post("/api/notifications/read", body);
      await Promise.all([load(), onChange()]);
    } catch (error) {
      toast.error(error);
    }
  };

  return (
    <div className="anchor" ref={ref}>
      <button type="button" className="icon-btn" aria-label={unread ? `Avisos: ${unread} sin leer` : "Avisos"} aria-expanded={open} onClick={() => setOpen(!open)}>
        <Icon name="bell" />
        {unread > 0 ? <span className="dot-count">{unread > 99 ? "99+" : unread}</span> : null}
      </button>
      {open ? (
        <div className="popover">
          <div className="popover-head">
            <h3>Avisos</h3>
            {unread > 0 ? (
              <button type="button" className="link-btn small" onClick={() => void markRead({ all: true })}>
                Marcar todo como leído
              </button>
            ) : null}
          </div>
          {items === null ? <Loading /> : null}
          {items && items.length === 0 ? <div className="popover-item muted">No hay avisos. Aquí aparecerán las aprobaciones pendientes, los errores y los recordatorios.</div> : null}
          {(items ?? []).map((item) => (
            <button
              key={item.id}
              type="button"
              className={`popover-item ${item.read ? "" : "unread"}`}
              onClick={() => {
                setOpen(false);
                if (!item.read) void markRead({ ids: [item.id] });
                if (item.link) navigate(item.link);
              }}
            >
              <span className="row">
                <Icon name={NOTIFICATION_ICON[item.type] ?? "info"} size={12} />
                <strong>{item.title}</strong>
              </span>
              {item.body ? <span className="small soft">{item.body}</span> : null}
              <span className="small muted" style={{ display: "block" }}>
                {timeSince(item.created_at)}
              </span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function WorkspaceSwitcher() {
  const session = useSession();
  const { setSession } = useSessionState();
  const navigate = useNavigate();
  const { busy, run } = useAction();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");

  const switchTo = (workspaceId: string) =>
    run("switch", async () => {
      setSession(await api.post<Session>("/api/session/workspace", { workspace_id: workspaceId }));
      navigate("/");
    });

  const create = (event: FormEvent) => {
    event.preventDefault();
    void run(
      "create",
      async () => {
        setSession(await api.post<Session>("/api/workspaces", { name, timezone: browserTimeZone() }));
        setCreating(false);
        setName("");
        navigate("/");
      },
      "Espacio de trabajo creado.",
    );
  };

  return (
    <>
      <label className="hide-narrow">
        <span className="sr-only">Espacio de trabajo</span>
        <select
          value={session.workspace.id}
          disabled={busy !== null}
          style={{ maxWidth: 220 }}
          onChange={(event) => {
            if (event.target.value === "__new") setCreating(true);
            else void switchTo(event.target.value);
          }}
        >
          {session.workspaces.map((workspace) => (
            <option key={workspace.id} value={workspace.id}>
              {workspace.name}
            </option>
          ))}
          <option value="__new">＋ Nuevo espacio…</option>
        </select>
      </label>
      {creating ? (
        <Modal title="Nuevo espacio de trabajo" onClose={() => setCreating(false)}>
          <form className="stack" onSubmit={create}>
            <p className="soft">Cada espacio tiene sus propios proyectos, kit de marca, cuentas conectadas y miembros. Nada se comparte entre espacios.</p>
            <Field label="Nombre del espacio">
              <input type="text" value={name} minLength={2} maxLength={120} required onChange={(event) => setName(event.target.value)} data-autofocus />
            </Field>
            <div className="row row-end">
              <Button variant="ghost" onClick={() => setCreating(false)}>
                Cancelar
              </Button>
              <Button type="submit" variant="primary" busy={busy === "create"}>
                Crear espacio
              </Button>
            </div>
          </form>
        </Modal>
      ) : null}
    </>
  );
}

function UserMenu() {
  const session = useSession();
  const { setSession } = useSessionState();
  const { open, setOpen, ref } = usePopover();
  const navigate = useNavigate();
  const toast = useToast();

  const logout = async () => {
    try {
      await api.post("/api/auth/logout");
    } catch (error) {
      toast.error(error);
      return;
    }
    setSession(null);
    navigate("/entrar");
  };

  return (
    <div className="anchor" ref={ref}>
      <button type="button" className="icon-btn" aria-label={`Cuenta de ${session.user.name}`} aria-expanded={open} onClick={() => setOpen(!open)}>
        <Icon name="user" />
      </button>
      {open ? (
        <div className="popover" style={{ width: 280 }}>
          <div className="popover-item" style={{ cursor: "default" }}>
            <strong>{session.user.name}</strong>
            <span className="small soft" style={{ display: "block" }}>
              {session.user.email}
            </span>
            <span className="row" style={{ marginTop: 6 }}>
              <Badge tone="info">{session.role_label}</Badge>
              <span className="small muted">en {session.workspace.name}</span>
            </span>
          </div>
          <Link className="popover-item" to="/equipo" onClick={() => setOpen(false)}>
            Equipo y espacios
          </Link>
          <button type="button" className="popover-item" onClick={() => void logout()}>
            <span className="row">
              <Icon name="logout" size={12} /> Cerrar sesión
            </span>
          </button>
        </div>
      ) : null}
    </div>
  );
}

export function PageHead({ title, children, actions }: { title: string; children?: ReactNode; actions?: ReactNode }) {
  useTitle(title);
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {children ? <p>{children}</p> : null}
      </div>
      {actions ? <div className="row">{actions}</div> : null}
    </div>
  );
}
