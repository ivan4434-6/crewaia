// Session, live updates (server-sent events) and toasts.
//
// What the screens show about agents comes from two places only: the REST
// state they fetch, and the stream of events the server emits when something
// really happens. There are no client-side timers pretending to be progress.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiError, api, setCsrf, setUnauthenticatedHandler } from "./api";
import type { LiveMessage, Session } from "./api";

// --- Session -----------------------------------------------------------------

interface SessionState {
  session: Session | null;
  loading: boolean;
  setSession: (session: Session | null) => void;
  refresh: () => Promise<void>;
  canEdit: boolean;
  isOwner: boolean;
}

const SessionContext = createContext<SessionState | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSessionState] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);

  const setSession = useCallback((next: Session | null) => {
    setCsrf(next?.csrf ?? "");
    setSessionState(next);
  }, []);

  const refresh = useCallback(async () => {
    try {
      setSession(await api.get<Session>("/api/session"));
    } catch (error) {
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) setSession(null);
      else throw error;
    }
  }, [setSession]);

  useEffect(() => {
    setUnauthenticatedHandler(() => setSession(null));
    refresh()
      .catch(() => setSession(null))
      .finally(() => setLoading(false));
    return () => setUnauthenticatedHandler(null);
  }, [refresh, setSession]);

  const value = useMemo<SessionState>(
    () => ({
      session,
      loading,
      setSession,
      refresh,
      canEdit: session ? session.role !== "viewer" : false,
      isOwner: session?.role === "owner",
    }),
    [session, loading, setSession, refresh],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSessionState(): SessionState {
  const value = useContext(SessionContext);
  if (!value) throw new Error("SessionProvider missing");
  return value;
}

/** The signed-in session. Only call below the authenticated shell. */
export function useSession(): Session & { canEdit: boolean; isOwner: boolean } {
  const { session, canEdit, isOwner } = useSessionState();
  if (!session) throw new Error("No session");
  return { ...session, canEdit, isOwner };
}

// --- Live stream -------------------------------------------------------------

type Listener = (message: LiveMessage) => void;

interface RunProgress {
  progress: number | null;
  label: string;
}

interface LiveState {
  connected: boolean;
  subscribe: (listener: Listener) => () => void;
  progress: Record<string, RunProgress>;
}

const LiveContext = createContext<LiveState | null>(null);

export function LiveProvider({ children }: { children: ReactNode }) {
  const listeners = useRef(new Set<Listener>());
  const [connected, setConnected] = useState(false);
  const [progress, setProgress] = useState<Record<string, RunProgress>>({});
  const everConnected = useRef(false);

  useEffect(() => {
    const source = new EventSource("/api/stream");
    const emit = (message: LiveMessage) => listeners.current.forEach((listener) => listener(message));
    source.onopen = () => {
      setConnected(true);
      // After a reconnection events may have been missed: ask every screen to reload.
      if (everConnected.current) emit({ kind: "resync" });
      everConnected.current = true;
    };
    source.onerror = () => setConnected(false);
    source.onmessage = (raw) => {
      let message: LiveMessage;
      try {
        message = JSON.parse(raw.data) as LiveMessage;
      } catch {
        return;
      }
      if (message.kind === "progress") {
        const { run_id, progress: value, label } = message;
        setProgress((current) => ({ ...current, [run_id]: { progress: value, label } }));
      } else if (message.kind === "event" && message.type === "run.finished" && message.run_id) {
        const finished = message.run_id;
        setProgress((current) => {
          if (!(finished in current)) return current;
          const next = { ...current };
          delete next[finished];
          return next;
        });
      }
      emit(message);
    };
    return () => source.close();
  }, []);

  const subscribe = useCallback((listener: Listener) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);

  const value = useMemo(() => ({ connected, subscribe, progress }), [connected, subscribe, progress]);
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

export function useLive(): LiveState {
  const value = useContext(LiveContext);
  if (!value) throw new Error("LiveProvider missing");
  return value;
}

/** Runs `handler` for every live message while the component is mounted. */
export function useLiveMessages(handler: Listener): void {
  const { subscribe } = useLive();
  const latest = useRef(handler);
  latest.current = handler;
  useEffect(() => subscribe((message) => latest.current(message)), [subscribe]);
}

// --- Data loading ------------------------------------------------------------

export interface Resource<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => Promise<void>;
  setData: (value: T) => void;
}

/**
 * Loads data and keeps it fresh: reloads when `deps` change, when the live
 * stream reconnects, and whenever `refreshOn` says a live message is relevant.
 */
export function useResource<T>(
  fetcher: () => Promise<T>,
  deps: unknown[],
  refreshOn?: (message: LiveMessage) => boolean,
): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const latestFetcher = useRef(fetcher);
  latestFetcher.current = fetcher;
  const generation = useRef(0);
  const timer = useRef<number | null>(null);

  const load = useCallback(async (silent: boolean) => {
    const mine = ++generation.current;
    if (!silent) setLoading(true);
    try {
      const value = await latestFetcher.current();
      if (mine !== generation.current) return;
      setData(value);
      setError(null);
    } catch (caught) {
      if (mine !== generation.current) return;
      setError(caught instanceof ApiError ? caught : new ApiError(0, "Error inesperado al cargar los datos."));
    } finally {
      if (mine === generation.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    setData(null);
    void load(false);
    return () => {
      generation.current++;
      if (timer.current) window.clearTimeout(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useLiveMessages((message) => {
    if (message.kind !== "resync" && !(refreshOn && refreshOn(message))) return;
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => void load(true), 120);
  });

  const reload = useCallback(() => load(true), [load]);
  return { data, error, loading, reload, setData };
}

// --- Toasts ------------------------------------------------------------------

interface Toast {
  id: number;
  kind: "ok" | "error" | "info";
  text: string;
}

interface ToastApi {
  ok: (text: string) => void;
  error: (error: unknown) => void;
  info: (text: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const counter = useRef(0);

  const push = useCallback((kind: Toast["kind"], text: string) => {
    const id = ++counter.current;
    setToasts((current) => [...current.slice(-3), { id, kind, text }]);
    window.setTimeout(() => setToasts((current) => current.filter((toast) => toast.id !== id)), kind === "error" ? 9000 : 4500);
  }, []);

  const value = useMemo<ToastApi>(
    () => ({
      ok: (text) => push("ok", text),
      info: (text) => push("info", text),
      error: (error) => push("error", error instanceof Error ? error.message : String(error)),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast toast-${toast.kind}`}>
            <span>{toast.text}</span>
            <button type="button" aria-label="Cerrar aviso" onClick={() => setToasts((current) => current.filter((item) => item.id !== toast.id))}>
              ×
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const value = useContext(ToastContext);
  if (!value) throw new Error("ToastProvider missing");
  return value;
}

/** Wraps an async action: tracks "busy" and reports failures as a toast. */
export function useAction(): { busy: string | null; run: (name: string, action: () => Promise<unknown>, done?: string) => Promise<boolean> } {
  const [busy, setBusy] = useState<string | null>(null);
  const toast = useToast();
  const run = useCallback(
    async (name: string, action: () => Promise<unknown>, done?: string) => {
      setBusy(name);
      try {
        await action();
        if (done) toast.ok(done);
        return true;
      } catch (error) {
        toast.error(error);
        return false;
      } finally {
        setBusy(null);
      }
    },
    [toast],
  );
  return { busy, run };
}
