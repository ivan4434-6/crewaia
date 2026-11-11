// Sign in, sign up (optionally with an invitation) and joining a workspace.

import { useEffect, useState } from "react";
import type { ChangeEvent, FormEvent, ReactNode } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { BrandMark, useTitle } from "../App";
import { ApiError, api, query } from "../api";
import type { Session } from "../api";
import { browserTimeZone } from "../format";
import { useSessionState } from "../live";
import { DeskScene } from "../sprites";
import { Button, Field, Loading, Notice } from "../ui";

interface AuthConfig {
  allow_registration: boolean;
  invite: { valid: boolean; workspace: string; role_label: string } | null;
  invite_invalid: boolean;
}

const TEAM: { key: string; name: string; color: string }[] = [
  { key: "scout", name: "Scout", color: "#5eead4" },
  { key: "writer", name: "Writer", color: "#a78bfa" },
  { key: "clipper", name: "Clipper", color: "#f472b6" },
  { key: "editor", name: "Editor", color: "#38bdf8" },
  { key: "designer", name: "Designer", color: "#e879f9" },
  { key: "publisher", name: "Publisher", color: "#fbbf24" },
  { key: "community", name: "Community", color: "#34d399" },
  { key: "analyst", name: "Analyst", color: "#818cf8" },
];

/** Only same-site paths are accepted as a place to return to after signing in. */
function safeReturnPath(value: string | null): string {
  return value && value.startsWith("/") && !value.startsWith("//") && !value.startsWith("/\\") ? value : "/";
}

function useAuthConfig(invite: string) {
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [failed, setFailed] = useState("");
  useEffect(() => {
    let cancelled = false;
    api
      .get<AuthConfig>(`/api/auth/config${query({ invite })}`)
      .then((data) => !cancelled && setConfig(data))
      .catch((error: Error) => !cancelled && setFailed(error.message));
    return () => {
      cancelled = true;
    };
  }, [invite]);
  return { config, failed };
}

function AuthFrame({ children }: { children: ReactNode }) {
  return (
    <div className="auth">
      <section className="auth-art" aria-label="Creator Office AI">
        <div className="stack">
          <div className="row">
            <BrandMark />
            <span className="brand-name" style={{ fontSize: 20 }}>
              Creator Office <span style={{ display: "inline" }}>AI</span>
            </span>
          </div>
          <h1>Tu oficina de agentes para crear vídeo.</h1>
          <p>Ocho especialistas investigan, escriben, recortan, editan, diseñan y preparan tus publicaciones para YouTube, Shorts, TikTok y Reels. Tú revisas y apruebas cada paso: nada se publica sin tu confirmación.</p>
        </div>
        <div className="auth-desks" aria-hidden="true">
          {TEAM.map((agent) => (
            <DeskScene key={agent.key} agent={agent.key} color={agent.color} status="idle" title={agent.name} />
          ))}
        </div>
      </section>
      <section className="auth-form">{children}</section>
    </div>
  );
}

export function AuthPage({ mode }: { mode: "login" | "register" }) {
  const [params] = useSearchParams();
  const invite = params.get("invitacion") ?? "";
  const { config, failed } = useAuthConfig(invite);
  const { setSession } = useSessionState();
  const navigate = useNavigate();
  const [form, setForm] = useState({ name: "", email: "", password: "", workspace_name: "" });
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  useTitle(mode === "login" ? "Entrar" : "Crear cuenta");

  const set = (key: keyof typeof form) => (event: ChangeEvent<HTMLInputElement>) => setForm({ ...form, [key]: event.target.value });
  const fieldError = (field: string) => (error && error.data.field === field ? error.message : undefined);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      // Signing in from an invitation link: the link travels in `volver`. An account left without any
      // workspace can only come back with it, so the sign-in request carries it too.
      const pending = invite || new URLSearchParams((params.get("volver") ?? "").split("?")[1] ?? "").get("invitacion") || "";
      const session =
        mode === "login"
          ? await api.post<Session & { invite_used?: boolean }>("/api/auth/login", { email: form.email, password: form.password, invite: pending })
          : await api.post<Session>("/api/auth/register", { ...form, invite, timezone: browserTimeZone() });
      setSession(session);
      const joined = "invite_used" in session && session.invite_used === true;
      navigate(mode === "login" && !joined ? safeReturnPath(params.get("volver")) : "/", { replace: true });
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : new ApiError(0, "Error inesperado. Inténtalo de nuevo."));
    } finally {
      setBusy(false);
    }
  };

  const keep = query({ invitacion: invite, volver: params.get("volver") });
  const closed = mode === "register" && config !== null && !config.allow_registration && !config.invite;

  return (
    <AuthFrame>
      <div className="auth-card stack stack-lg">
        <div>
          <h1>{mode === "login" ? "Entrar" : "Crear cuenta"}</h1>
          <p className="soft" style={{ marginTop: 6 }}>
            {mode === "login" ? "Accede a tu espacio de trabajo." : config?.invite ? "Crea tu cuenta para unirte al equipo." : "Crea tu cuenta y tu primer espacio de trabajo."}
          </p>
        </div>
        {failed ? <Notice tone="bad">{failed}</Notice> : null}
        {config?.invite ? (
          <Notice tone="accent" icon="team" title="Invitación válida.">
            Te unirás a «{config.invite.workspace}» con rol {config.invite.role_label.toLowerCase()}.
            {mode === "login" ? " Entra con tu cuenta y la aceptarás en el siguiente paso." : ""}
          </Notice>
        ) : null}
        {config?.invite_invalid ? (
          <Notice tone="warn" title="La invitación no es válida o ha caducado.">
            Pide a quien te invitó que genere un enlace nuevo.
          </Notice>
        ) : null}
        {closed ? (
          <Notice tone="warn" title="El registro está cerrado.">
            Para crear una cuenta necesitas el enlace de invitación de un espacio de trabajo.
          </Notice>
        ) : (
          <form className="stack" onSubmit={submit}>
            {mode === "register" ? (
              <Field label="Tu nombre" error={fieldError("name")}>
                <input type="text" value={form.name} onChange={set("name")} autoComplete="name" required maxLength={120} />
              </Field>
            ) : null}
            <Field label="Correo electrónico" error={fieldError("email")}>
              <input type="email" value={form.email} onChange={set("email")} autoComplete="email" required maxLength={254} />
            </Field>
            <Field label="Contraseña" hint={mode === "register" ? "Al menos 10 caracteres." : undefined} error={fieldError("password")}>
              <input
                type="password"
                value={form.password}
                onChange={set("password")}
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                required
                minLength={mode === "register" ? 10 : undefined}
                maxLength={200}
              />
            </Field>
            {mode === "register" && !config?.invite ? (
              <Field label="Nombre de tu espacio de trabajo" hint="Opcional. Por ejemplo, el nombre de tu canal o de tu marca.">
                <input type="text" value={form.workspace_name} onChange={set("workspace_name")} maxLength={120} />
              </Field>
            ) : null}
            {error && !error.data.field ? (
              <div className="notice notice-bad" role="alert">
                <span>{error.message}</span>
              </div>
            ) : null}
            <Button type="submit" variant="primary" busy={busy}>
              {mode === "login" ? "Entrar" : "Crear cuenta"}
            </Button>
          </form>
        )}
        <p className="soft">
          {mode === "login" ? (
            <>
              ¿Aún no tienes cuenta? <Link to={`/registro${keep}`}>Crear cuenta</Link>
            </>
          ) : (
            <>
              ¿Ya tienes cuenta? <Link to={`/entrar${invite ? query({ volver: `/registro${query({ invitacion: invite })}` }) : ""}`}>Entrar</Link>
            </>
          )}
        </p>
      </div>
    </AuthFrame>
  );
}

/** A signed-in person opening an invitation link: join that workspace with the current account. */
export function JoinPage() {
  const [params] = useSearchParams();
  const invite = params.get("invitacion") ?? "";
  const { config, failed } = useAuthConfig(invite);
  const { session, setSession } = useSessionState();
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useTitle("Invitación");

  if (!invite) return <Navigate to="/" replace />;

  const accept = async () => {
    setBusy(true);
    setError("");
    try {
      setSession(await api.post<Session>("/api/workspace/invites/accept", { token: invite }));
      navigate("/", { replace: true });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "No se pudo aceptar la invitación.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthFrame>
      <div className="auth-card stack stack-lg">
        <h1>Invitación</h1>
        {failed ? <Notice tone="bad">{failed}</Notice> : null}
        {!config && !failed ? <Loading /> : null}
        {config?.invite ? (
          <>
            <Notice tone="accent" icon="team" title={`Te han invitado a «${config.invite.workspace}».`}>
              Te unirás como {config.invite.role_label.toLowerCase()} con tu cuenta actual ({session?.user.email}). Tus otros espacios no cambian.
            </Notice>
            {error ? (
              <div className="notice notice-bad" role="alert">
                <span>{error}</span>
              </div>
            ) : null}
            <div className="row">
              <Button variant="primary" busy={busy} onClick={() => void accept()}>
                Unirme al espacio
              </Button>
              <Link to="/">Ahora no</Link>
            </div>
          </>
        ) : null}
        {config && !config.invite ? (
          <>
            <Notice tone="warn" title="La invitación no es válida o ha caducado.">
              Pide a quien te invitó que genere un enlace nuevo.
            </Notice>
            <Link to="/">Volver a la oficina</Link>
          </>
        ) : null}
      </div>
    </AuthFrame>
  );
}
