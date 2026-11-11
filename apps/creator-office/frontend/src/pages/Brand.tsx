// Brand kit: voice, colours, typography and the sources Scout may always read.

import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { PageHead } from "../App";
import { api, query, uploadFile } from "../api";
import { useAction, useResource, useSession, useToast } from "../live";
import { Button, ChipInput, ErrorBox, Field, FileButton, Loading, Notice, Panel, ProgressBar } from "../ui";

interface Brand {
  name: string;
  voice: string;
  do: string[];
  dont: string[];
  colors: Record<string, string>;
  font: string;
  layout: string;
  logo_asset_id: string | null;
  subtitles_default: boolean;
  default_hashtags: string[];
  allowed_sources: string[];
}

interface BrandPayload {
  brand: Brand;
  logo_url: string | null;
  defaults: Brand;
}

const COLOR_NAMES: Record<string, string> = { primary: "Principal", secondary: "Secundario", accent: "Acento", background: "Fondo", text: "Texto" };
const LAYOUT_NAMES: Record<string, string> = { side: "Texto a un lado", band: "Banda inferior", center: "Centrado" };

export function BrandPage() {
  const session = useSession();
  const toast = useToast();
  const resource = useResource<BrandPayload>(() => api.get("/api/brand"), [session.workspace.id]);
  const [brand, setBrand] = useState<Brand | null>(null);
  const [sources, setSources] = useState("");
  const [logoProgress, setLogoProgress] = useState<number | null>(null);
  const { busy, run } = useAction();
  const canEdit = session.canEdit;

  useEffect(() => {
    if (resource.data) {
      setBrand(resource.data.brand);
      setSources(resource.data.brand.allowed_sources.join("\n"));
    }
  }, [resource.data]);

  const save = (event: FormEvent) => {
    event.preventDefault();
    if (!brand) return;
    const allowed = sources
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    void run("save", async () => resource.setData(await api.put<BrandPayload>("/api/brand", { ...brand, allowed_sources: allowed })), "Kit de marca guardado. Los agentes lo usarán en los próximos trabajos.");
  };

  const uploadLogo = async (file: File) => {
    setLogoProgress(0);
    try {
      resource.setData(await uploadFile<BrandPayload>(`/api/brand/logo${query({ filename: file.name })}`, file, setLogoProgress));
      toast.ok("Logotipo actualizado.");
    } catch (error) {
      toast.error(error);
    } finally {
      setLogoProgress(null);
    }
  };

  const set = <K extends keyof Brand>(key: K, value: Brand[K]) => brand && setBrand({ ...brand, [key]: value });
  const dirty = resource.data && brand ? JSON.stringify({ ...brand, allowed_sources: sources.split("\n").map((line) => line.trim()).filter(Boolean) }) !== JSON.stringify(resource.data.brand) : false;

  return (
    <>
      <PageHead title="Kit de marca">Cómo suena y cómo se ve tu marca. Writer, Designer, Editor y Community lo aplican en cada trabajo.</PageHead>
      {!canEdit ? (
        <Notice tone="info" icon="lock">
          Tu rol es de solo lectura: puedes consultar el kit, pero no cambiarlo.
        </Notice>
      ) : null}
      {resource.error && !resource.data ? <ErrorBox error={resource.error} onRetry={() => void resource.reload()} /> : null}
      {resource.loading && !resource.data ? <Loading /> : null}
      {brand && resource.data ? (
        <form className="stack stack-lg" onSubmit={save}>
          <fieldset className="stack stack-lg" disabled={!canEdit} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
            <legend className="sr-only">Kit de marca</legend>
            <Panel title="Voz">
              <div className="form-grid">
                <Field label="Nombre de la marca o del canal">
                  <input type="text" value={brand.name} maxLength={200} onChange={(event) => set("name", event.target.value)} />
                </Field>
                <span />
                <div className="span-2">
                  <Field label="Cómo habla tu marca" hint="Tono, persona, tratamiento (tú/usted), expresiones propias… Writer y Community lo siguen.">
                    <textarea value={brand.voice} rows={4} maxLength={2000} placeholder="Cercana y directa, de tú, con ejemplos prácticos y sin tecnicismos." onChange={(event) => set("voice", event.target.value)} />
                  </Field>
                </div>
                <Field label="Siempre" hint="Pulsa Intro para añadir cada norma.">
                  <ChipInput values={brand.do} onChange={(values) => set("do", values)} placeholder="Por ejemplo: explicar con un ejemplo" />
                </Field>
                <Field label="Nunca" hint="Lo que hay que evitar.">
                  <ChipInput values={brand.dont} onChange={(values) => set("dont", values)} placeholder="Por ejemplo: prometer resultados" />
                </Field>
                <div className="span-2">
                  <Field label="Hashtags de marca" hint="Se proponen en los textos de cada plataforma.">
                    <ChipInput values={brand.default_hashtags} onChange={(values) => set("default_hashtags", values)} placeholder="#tumarca" prefix="#" />
                  </Field>
                </div>
              </div>
            </Panel>

            <Panel title="Identidad visual">
              <div className="stack">
                <div className="swatches">
                  {Object.entries(COLOR_NAMES).map(([key, name]) => (
                    <label key={key} className="swatch">
                      {name}
                      <span className="row">
                        <input type="color" value={brand.colors[key] ?? "#000000"} onChange={(event) => set("colors", { ...brand.colors, [key]: event.target.value })} />
                        <code>{brand.colors[key]}</code>
                      </span>
                    </label>
                  ))}
                </div>
                <div className="form-grid">
                  <Field label="Tipografía de titulares y subtítulos" hint="Fuentes libres incluidas en la aplicación.">
                    <select value={brand.font} onChange={(event) => set("font", event.target.value)}>
                      {session.catalog.fonts.map((font) => (
                        <option key={font} value={font}>
                          {font}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Composición preferida de miniaturas">
                    <select value={brand.layout} onChange={(event) => set("layout", event.target.value)}>
                      {session.catalog.layouts.map((layout) => (
                        <option key={layout} value={layout}>
                          {LAYOUT_NAMES[layout] ?? layout}
                        </option>
                      ))}
                    </select>
                  </Field>
                </div>
                <label className="check-row">
                  <input type="checkbox" checked={brand.subtitles_default} onChange={(event) => set("subtitles_default", event.target.checked)} />
                  <span>Incrustar subtítulos por defecto en las piezas, cuando exista transcripción</span>
                </label>
                <div className="stack stack-sm">
                  <span className="field-label">Logotipo</span>
                  <div className="row">
                    {resource.data.logo_url ? (
                      <img src={resource.data.logo_url} alt="Logotipo actual" style={{ maxHeight: 64, maxWidth: 200, background: "#0d0e18", border: "2px solid #07080e", padding: 6 }} />
                    ) : (
                      <span className="muted">Sin logotipo</span>
                    )}
                    {canEdit && logoProgress === null ? <FileButton small label={resource.data.logo_url ? "Cambiar" : "Subir logotipo"} accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp" onFile={(file) => void uploadLogo(file)} /> : null}
                    {canEdit && resource.data.logo_url ? (
                      <Button small variant="ghost" icon="trash" busy={busy === "logo"} onClick={() => void run("logo", async () => resource.setData(await api.delete<BrandPayload>("/api/brand/logo")), "Logotipo quitado.")}>
                        Quitar
                      </Button>
                    ) : null}
                  </div>
                  {logoProgress !== null ? <ProgressBar value={logoProgress} label="Subiendo logotipo" /> : null}
                  <span className="small muted">PNG con fondo transparente, preferiblemente. Aparece en miniaturas y portadas.</span>
                </div>
              </div>
            </Panel>

            <Panel title="Fuentes permanentes para Scout">
              <div className="stack">
                <Notice tone="info">
                  Scout solo lee direcciones que tú autorizas: estas y las que añadas en cada proyecto. Respeta el archivo robots.txt de cada sitio y no accede a páginas privadas ni a redes internas.
                </Notice>
                <Field label="Páginas o feeds RSS, una dirección por línea" hint="Por ejemplo, el blog de tu sector o el feed de tu propio canal.">
                  <textarea value={sources} rows={5} placeholder={"https://ejemplo.com/blog/feed.xml\nhttps://ejemplo.org/noticias"} onChange={(event) => setSources(event.target.value)} />
                </Field>
              </div>
            </Panel>
          </fieldset>
          {canEdit ? (
            <div className="row row-end">
              <Button
                variant="ghost"
                disabled={!dirty}
                onClick={() => {
                  setBrand(resource.data ? resource.data.brand : brand);
                  setSources(resource.data ? resource.data.brand.allowed_sources.join("\n") : sources);
                }}
              >
                Deshacer cambios
              </Button>
              <Button type="submit" variant="primary" icon="check" busy={busy === "save"} disabled={!dirty}>
                Guardar kit de marca
              </Button>
            </div>
          ) : null}
        </form>
      ) : null}
    </>
  );
}
