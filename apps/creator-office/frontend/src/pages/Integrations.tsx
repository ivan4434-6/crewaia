// Integrations: what is really connected, what is pending and what each one needs.

import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { PageHead } from "../App";
import { api } from "../api";
import type { EngineInfo, IntegrationStatus } from "../api";
import { formatDate } from "../format";
import { useAction, useResource, useSession, useSessionState, useToast } from "../live";
import { Badge, Button, Confirm, ErrorBox, Field, Loading, Notice, Panel } from "../ui";
import type { Tone } from "../ui";
import { PlatformTag } from "./shared";

interface Preset {
  id: string;
  name: string;
  prefix: string;
  example: string;
  needs_key: boolean;
  extra: string;
  base_url?: string;
}

interface LlmPayload {
  engine: EngineInfo;
  saved: { model: string; base_url: string; price_input_per_mtok: number | null; price_output_per_mtok: number | null; api_key_hint: string; has_key: boolean } | null;
  env_model: string;
  presets: Preset[];
  demo_note: string;
}

interface IntegrationsPayload {
  platforms: IntegrationStatus[];
  llm: LlmPayload;
  transcription: { configured: boolean; model: string; missing: string[]; alternative: string };
  ffmpeg: { available: boolean; version: string; source: string; alternative: string };
  base_url: string;
  base_url_public: boolean;
  can_manage: boolean;
}

const STATE_TONE: Record<IntegrationStatus["state"], Tone> = { connected: "ok", expired: "bad", needs_setup: "warn", ready: "info" };

export function IntegrationsPage() {
  const session = useSession();
  const [params, setParams] = useSearchParams();
  const integrations = useResource<IntegrationsPayload>(() => api.get("/api/integrations"), [session.workspace.id], (message) => message.kind === "event" && message.type.startsWith("integration."));
  const data = integrations.data;
  const connected = params.get("conectado");
  const failed = params.get("error");
  const embedded = window.self !== window.top;

  return (
    <>
      <PageHead title="Integraciones">
        Aquí ves qué está conectado de verdad. Lo que no tiene credenciales aparece como «Pendiente de conexión», con lo que falta configurar y la alternativa manual. Nada se simula.
      </PageHead>
      {connected ? (
        <Notice tone="ok" title="Cuenta conectada." actions={<button type="button" className="link-btn" onClick={() => setParams({}, { replace: true })}>Cerrar aviso</button>}>
          {data?.platforms.find((item) => item.provider === connected)?.name ?? connected} ya puede usarse para publicar con tu confirmación.
        </Notice>
      ) : null}
      {failed ? (
        <Notice tone="bad" title="No se pudo conectar la cuenta." actions={<button type="button" className="link-btn" onClick={() => setParams({}, { replace: true })}>Cerrar aviso</button>}>
          {params.get("mensaje") ?? "La plataforma no completó la autorización."}
        </Notice>
      ) : null}
      {!session.isOwner ? (
        <Notice tone="info" icon="lock">
          Solo un propietario del espacio puede conectar o desconectar cuentas y cambiar el proveedor de IA. Tú puedes consultar el estado.
        </Notice>
      ) : null}
      {integrations.error && !data ? <ErrorBox error={integrations.error} onRetry={() => void integrations.reload()} /> : null}
      {integrations.loading && !data ? <Loading /> : null}
      {data ? (
        <>
          <h2>Plataformas de publicación</h2>
          {embedded && data.can_manage ? (
            <Notice tone="info">
              Estás viendo la aplicación incrustada en otra página. Las plataformas no permiten autorizar una cuenta dentro de un marco: para conectar, abre la aplicación en su propia pestaña.
            </Notice>
          ) : null}
          <div className="grid-cards" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))" }}>
            {data.platforms.map((platform) => (
              <PlatformCard key={platform.provider} platform={platform} canManage={data.can_manage} onChange={integrations.reload} />
            ))}
          </div>
          <p className="small muted">
            Dirección pública de este servidor (APP_BASE_URL): <code>{data.base_url}</code>
            {data.base_url_public ? "" : " — no es una dirección https pública, así que las plataformas no podrán devolver la autorización ni descargar archivos desde aquí."}
          </p>

          <h2>Proveedor de IA</h2>
          <LlmCard llm={data.llm} canManage={data.can_manage} onChange={integrations.reload} />

          <h2>Herramientas del servidor</h2>
          <div className="grid-cards" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))" }}>
            <div className="integration">
              <div className="row row-between">
                <h3>FFmpeg</h3>
                {data.ffmpeg.available ? (
                  <Badge tone="ok" icon="check">
                    Disponible
                  </Badge>
                ) : (
                  <Badge tone="warn" icon="warning">
                    No instalado
                  </Badge>
                )}
              </div>
              <p className="soft">Analiza los vídeos que subes y exporta las piezas en cada formato.</p>
              {data.ffmpeg.available ? (
                <span className="small muted">
                  Versión {data.ffmpeg.version || "desconocida"}
                  {data.ffmpeg.source ? ` · ${data.ffmpeg.source}` : ""}
                </span>
              ) : (
                <Notice tone="warn" title="Alternativa:">
                  {data.ffmpeg.alternative}
                </Notice>
              )}
            </div>
            <div className="integration">
              <div className="row row-between">
                <h3>Transcripción automática</h3>
                {data.transcription.configured ? (
                  <Badge tone="ok" icon="check">
                    Configurada
                  </Badge>
                ) : (
                  <Badge tone="warn" icon="plug">
                    Pendiente de conexión
                  </Badge>
                )}
              </div>
              <p className="soft">Convierte la voz del vídeo en texto para que Clipper sepa qué se dice y Editor pueda incrustar subtítulos.</p>
              {data.transcription.configured ? (
                <span className="small muted">Modelo: {data.transcription.model}</span>
              ) : (
                <>
                  <div className="stack stack-sm">
                    <span className="small">Falta configurar en el servidor:</span>
                    <div className="env-list">
                      {data.transcription.missing.map((name) => (
                        <code key={name}>{name}</code>
                      ))}
                    </div>
                  </div>
                  <Notice tone="info" title="Alternativa manual:">
                    {data.transcription.alternative}
                  </Notice>
                </>
              )}
            </div>
          </div>
        </>
      ) : null}
    </>
  );
}

const SCHEDULE_NOTE: Record<string, string> = {
  platform: "Programación: la propia plataforma publica a la hora elegida.",
  app: "Programación: la plataforma no la ofrece por API; esta aplicación sube el vídeo a la hora que confirmes.",
};

function PlatformCard({ platform, canManage, onChange }: { platform: IntegrationStatus; canManage: boolean; onChange: () => Promise<void> }) {
  const session = useSession();
  const toast = useToast();
  const { busy, run } = useAction();
  const [confirming, setConfirming] = useState(false);
  const fields = session.catalog.metric_fields;
  const names = platform.platforms.map((key) => session.catalog.platforms.find((item) => item.key === key)?.name ?? key);

  const connect = () =>
    run("connect", async () => {
      const result = await api.post<{ url: string }>(`/api/integrations/${platform.provider}/connect`);
      window.location.assign(result.url);
    });
  const disconnect = () =>
    run("disconnect", async () => {
      const result = await api.delete<{ note: string }>(`/api/integrations/${platform.provider}`);
      await onChange();
      setConfirming(false);
      toast.ok(`${platform.name} desconectado.${result.note ? ` ${result.note}` : ""}`);
    });
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.ok("Copiado.");
    } catch {
      toast.info("No se pudo copiar automáticamente: selecciona el texto y cópialo.");
    }
  };

  return (
    <div className="integration">
      <div className="row row-between">
        <PlatformTag platform={platform.provider} name={platform.name} />
        <Badge tone={STATE_TONE[platform.state]} icon={platform.state === "connected" ? "check" : "plug"}>
          {platform.label}
        </Badge>
      </div>
      <span className="small muted">Para: {names.join(" · ")}</span>
      <p>{platform.detail}</p>
      {platform.state === "connected" && platform.connected_at ? <span className="small muted">Conectada el {formatDate(platform.connected_at)}</span> : null}
      {platform.warning ? <Notice tone="warn">{platform.warning}</Notice> : null}

      {platform.state === "needs_setup" ? (
        <div className="stack stack-sm">
          <span className="small">Falta configurar en el servidor (archivo .env):</span>
          <div className="env-list">
            {platform.missing_settings.map((name) => (
              <code key={name}>{name}</code>
            ))}
          </div>
        </div>
      ) : null}

      {platform.state !== "connected" ? (
        <Notice tone="info" title="Alternativa manual:">
          {platform.manual_alternative}
        </Notice>
      ) : null}

      {canManage ? (
        <div className="row">
          {platform.state === "ready" || platform.state === "expired" ? (
            <Button variant="primary" icon="plug" busy={busy === "connect"} onClick={() => void connect()}>
              {platform.state === "expired" ? "Volver a conectar" : "Conectar cuenta"}
            </Button>
          ) : null}
          {platform.state === "connected" || platform.state === "expired" ? (
            <Button variant="ghost" icon="cross" onClick={() => setConfirming(true)}>
              Desconectar
            </Button>
          ) : null}
        </div>
      ) : null}

      <details>
        <summary>Qué hace falta y qué permite</summary>
        <div className="stack stack-sm small">
          <strong>Requisitos (los prepara quien administra el servidor)</strong>
          <ul className="bullets">
            {platform.requirements.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
          <strong>URI de redirección que hay que registrar</strong>
          <div className="copy-box">
            <input type="text" readOnly value={platform.redirect_uri} aria-label="URI de redirección" onFocus={(event) => event.target.select()} />
            <Button small onClick={() => void copy(platform.redirect_uri)}>
              Copiar
            </Button>
          </div>
          <strong>Permisos que se piden</strong>
          <div className="env-list">
            {platform.requested_scopes.map((scope) => (
              <code key={scope}>{scope}</code>
            ))}
          </div>
          <strong>Qué permite su API</strong>
          <ul className="bullets">
            <li>{SCHEDULE_NOTE[platform.capabilities.schedule] ?? ""}</li>
            <li>{platform.capabilities.comments ? `Comentarios: se pueden leer${platform.capabilities.reply ? " y responder (con tu confirmación)" : ""}.` : platform.capabilities.comments_note || "Comentarios: no disponibles por API."}</li>
            <li>Métricas: {platform.capabilities.metrics.map((key) => fields[key]?.label ?? key).join(", ")}.</li>
            {platform.capabilities.metrics_missing.length ? <li>No disponibles por API: {platform.capabilities.metrics_missing.map((key) => fields[key]?.label ?? key).join(", ")}.</li> : null}
            {platform.capabilities.needs_public_url ? <li>Necesita descargar el vídeo desde una dirección https pública de este servidor.</li> : null}
          </ul>
          <span className="muted">Estos datos describen cómo está escrito el conector; las plataformas cambian sus APIs y condiciones, así que confírmalos en su documentación.</span>
        </div>
      </details>

      {confirming ? (
        <Confirm title={`Desconectar ${platform.name}`} confirmLabel="Desconectar" danger busy={busy === "disconnect"} onClose={() => setConfirming(false)} onConfirm={() => void disconnect()}>
          <p>Se revoca el acceso y se borran los tokens guardados. Las publicaciones programadas para esta cuenta no saldrán automáticamente. Lo ya publicado no se toca.</p>
        </Confirm>
      ) : null}
    </div>
  );
}

function LlmCard({ llm, canManage, onChange }: { llm: LlmPayload; canManage: boolean; onChange: () => Promise<void> }) {
  const { refresh } = useSessionState();
  const { busy, run } = useAction();
  const [preset, setPreset] = useState("");
  const [model, setModel] = useState(llm.saved?.model ?? "");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(llm.saved?.base_url ?? "");
  const [priceIn, setPriceIn] = useState(llm.saved?.price_input_per_mtok?.toString() ?? "");
  const [priceOut, setPriceOut] = useState(llm.saved?.price_output_per_mtok?.toString() ?? "");
  const [test, setTest] = useState<{ ok: boolean; detail: string } | null>(null);
  const [removing, setRemoving] = useState(false);

  useEffect(() => {
    setModel(llm.saved?.model ?? "");
    setBaseUrl(llm.saved?.base_url ?? "");
    setPriceIn(llm.saved?.price_input_per_mtok?.toString() ?? "");
    setPriceOut(llm.saved?.price_output_per_mtok?.toString() ?? "");
    setApiKey("");
  }, [llm.saved]);

  const choose = (id: string) => {
    setPreset(id);
    const found = llm.presets.find((item) => item.id === id);
    if (!found) return;
    if (!model || llm.presets.some((item) => item.example === model || item.prefix === model)) setModel(found.example.includes("<") ? found.prefix : found.example);
    setBaseUrl(found.base_url && !found.base_url.includes("…") ? found.base_url : "");
  };
  const number = (value: string) => (value.trim() === "" ? null : Number(value.replace(",", ".")));
  const after = async () => {
    await Promise.all([onChange(), refresh()]);
  };

  const save = (event: FormEvent) => {
    event.preventDefault();
    setTest(null);
    void run(
      "save",
      async () => {
        await api.put("/api/integrations/llm", {
          model,
          base_url: baseUrl,
          api_key: apiKey ? apiKey : null,
          price_input_per_mtok: number(priceIn),
          price_output_per_mtok: number(priceOut),
        });
        await after();
      },
      "Proveedor de IA guardado. Pruébalo antes de lanzar un proyecto.",
    );
  };
  const runTest = () =>
    run("test", async () => {
      setTest(null);
      setTest(await api.post<{ ok: boolean; detail: string }>("/api/integrations/llm/test"));
    });
  const remove = () =>
    run(
      "remove",
      async () => {
        await api.delete("/api/integrations/llm");
        await after();
        setRemoving(false);
        setTest(null);
      },
      "Proveedor de IA quitado de este espacio.",
    );

  const selected = llm.presets.find((item) => item.id === preset);
  const demo = llm.engine.mode === "demo";
  return (
    <Panel
      title={
        <span className="row">
          Motor con el que trabajan los agentes
          {demo ? (
            <Badge tone="warn" icon="info">
              Modo demostración
            </Badge>
          ) : (
            <Badge tone="ok" icon="sparkle">
              {llm.engine.model}
            </Badge>
          )}
        </span>
      }
    >
      <div className="stack">
        {demo ? (
          <Notice tone="warn" title="Pendiente de conexión: no hay proveedor de IA.">
            {llm.demo_note}
          </Notice>
        ) : (
          <p className="soft">
            Los agentes usan <strong>{llm.engine.model}</strong> ({llm.engine.source === "workspace" ? "configurado en este espacio" : "configurado en el servidor con LLM_MODEL"}). El contenido que generan se etiqueta con el modelo que lo produjo.
          </p>
        )}
        {llm.env_model && llm.engine.source !== "env" ? <p className="small muted">El servidor también define LLM_MODEL={llm.env_model}; la configuración de este espacio tiene prioridad.</p> : null}
        {canManage ? (
          <form className="stack" onSubmit={save}>
            <div className="form-grid">
              <Field label="Proveedor" hint="Rellena un ejemplo de nombre de modelo. Puedes escribir cualquier otro que admita CrewAI.">
                <select value={preset} onChange={(event) => choose(event.target.value)}>
                  <option value="">Elige para ver un ejemplo…</option>
                  {llm.presets.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Modelo" hint={selected ? `Formato: ${selected.example}. Comprueba el nombre exacto en la documentación del proveedor.` : "Formato proveedor/modelo."}>
                <input type="text" value={model} required minLength={2} maxLength={120} placeholder="proveedor/modelo" onChange={(event) => setModel(event.target.value)} />
              </Field>
              <Field label="Clave de API" hint={llm.saved?.has_key ? `Hay una clave guardada (${llm.saved.api_key_hint}). Déjalo en blanco para conservarla.` : selected && !selected.needs_key ? "Este proveedor no suele necesitar clave." : "Se guarda cifrada y nunca se vuelve a mostrar."}>
                <input type="password" value={apiKey} maxLength={400} autoComplete="off" onChange={(event) => setApiKey(event.target.value)} />
              </Field>
              <Field label="URL base (opcional)" hint="Solo para servidores propios o compatibles con OpenAI.">
                <input type="url" value={baseUrl} maxLength={400} placeholder={selected?.base_url ?? "https://…/v1"} onChange={(event) => setBaseUrl(event.target.value)} />
              </Field>
              <Field label="Precio de entrada (USD por millón de tokens)" hint="Opcional. Sin precios no se estima el coste.">
                <input type="number" min={0} step="any" value={priceIn} onChange={(event) => setPriceIn(event.target.value)} />
              </Field>
              <Field label="Precio de salida (USD por millón de tokens)">
                <input type="number" min={0} step="any" value={priceOut} onChange={(event) => setPriceOut(event.target.value)} />
              </Field>
            </div>
            {selected?.extra ? (
              <p className="small muted">
                Este proveedor necesita un paquete adicional en el servidor: <code>pip install "crewai[{selected.extra}]"</code>. Si falta, la prueba lo dirá.
              </p>
            ) : null}
            <div className="row">
              <Button type="submit" variant="primary" icon="check" busy={busy === "save"}>
                Guardar proveedor
              </Button>
              <Button icon="play" busy={busy === "test"} disabled={demo} title={demo ? "Guarda antes un proveedor" : "Envía una petición mínima real al proveedor"} onClick={() => void runTest()}>
                Probar conexión
              </Button>
              {llm.saved ? (
                <Button variant="ghost" icon="trash" onClick={() => setRemoving(true)}>
                  Quitar
                </Button>
              ) : null}
            </div>
            {test ? (
              <Notice tone={test.ok ? "ok" : "bad"} title={test.ok ? "El proveedor respondió." : "La prueba falló."}>
                {test.detail}
              </Notice>
            ) : null}
          </form>
        ) : null}
        {removing ? (
          <Confirm title="Quitar el proveedor de IA" confirmLabel="Quitar proveedor" danger busy={busy === "remove"} onClose={() => setRemoving(false)} onConfirm={() => void remove()}>
            <p>Se borra la clave guardada. Los agentes volverán al motor de demostración (o al modelo definido en el servidor, si lo hay).</p>
          </Confirm>
        ) : null}
      </div>
    </Panel>
  );
}
