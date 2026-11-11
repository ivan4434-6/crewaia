// Workspace members, roles and invitations.

import { useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { PageHead } from "../App";
import { api } from "../api";
import type { Session } from "../api";
import { browserTimeZone, formatDate, knownTimeZones } from "../format";
import { useAction, useResource, useSession, useSessionState, useToast } from "../live";
import { Badge, Button, Confirm, ErrorBox, Field, Loading, Notice, Panel } from "../ui";

interface Member {
  id: string;
  name: string;
  email: string;
  role: string;
  role_label: string;
  is_you: boolean;
  joined_at: string;
}

interface MembersPayload {
  members: Member[];
  invites: { id: string; role: string; role_label: string; expires_at: string }[];
  roles: { key: string; label: string }[];
}

const ROLE_HELP: Record<string, string> = {
  owner: "Todo lo del editor y, además, gestiona el equipo, las cuentas conectadas y el proveedor de IA.",
  editor: "Crea proyectos, revisa y aprueba resultados, edita, programa y publica.",
  viewer: "Solo consulta: ve proyectos, resultados y actividad, sin cambiar nada.",
};

export function TeamPage() {
  const session = useSession();
  const { setSession, refresh } = useSessionState();
  const navigate = useNavigate();
  const toast = useToast();
  const team = useResource<MembersPayload>(() => api.get("/api/workspace/members"), [session.workspace.id], (message) => message.kind === "event" && message.type.startsWith("workspace."));
  const { busy, run } = useAction();
  const [name, setName] = useState(session.workspace.name);
  const [zone, setZone] = useState(session.workspace.timezone);
  const here = browserTimeZone();
  const [inviteRole, setInviteRole] = useState("editor");
  const [link, setLink] = useState<{ url: string; role: string } | null>(null);
  const [removing, setRemoving] = useState<Member | null>(null);
  const [newName, setNewName] = useState("");
  const data = team.data;

  const rename = (event: FormEvent) => {
    event.preventDefault();
    void run(
      "rename",
      async () => {
        await api.patch("/api/workspace", { name });
        await refresh();
      },
      "Nombre del espacio actualizado.",
    );
  };
  const changeZone = (value: string) =>
    run(
      "zone",
      async () => {
        await api.patch("/api/workspace", { timezone: value });
        setZone(value);
        await refresh();
      },
      "Zona horaria actualizada.",
    );
  const invite = () =>
    run("invite", async () => {
      const result = await api.post<{ path: string; role_label: string }>("/api/workspace/invites", { role: inviteRole });
      setLink({ url: `${window.location.origin}${result.path}`, role: result.role_label });
      await team.reload();
    });
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.ok("Enlace copiado.");
    } catch {
      toast.info("No se pudo copiar automáticamente: selecciona el enlace y cópialo.");
    }
  };
  const changeRole = (member: Member, role: string) =>
    run(
      `role-${member.id}`,
      async () => {
        await api.patch(`/api/workspace/members/${member.id}`, { role });
        await Promise.all([team.reload(), member.is_you ? refresh() : Promise.resolve()]);
      },
      "Rol actualizado.",
    );
  const remove = (member: Member) =>
    run(
      "remove",
      async () => {
        await api.delete(`/api/workspace/members/${member.id}`);
        setRemoving(null);
        if (member.is_you) {
          await refresh();
          navigate("/");
        } else {
          await team.reload();
        }
      },
      member.is_you ? "Has salido del espacio." : `${member.name} ya no forma parte del espacio.`,
    );
  const switchTo = (workspaceId: string) =>
    run(`switch-${workspaceId}`, async () => {
      setSession(await api.post<Session>("/api/session/workspace", { workspace_id: workspaceId }));
      navigate("/");
    });
  const create = (event: FormEvent) => {
    event.preventDefault();
    void run(
      "create",
      async () => {
        setSession(await api.post<Session>("/api/workspaces", { name: newName, timezone: here }));
        setNewName("");
        navigate("/");
      },
      "Espacio de trabajo creado.",
    );
  };

  return (
    <>
      <PageHead title="Equipo">Personas con acceso a «{session.workspace.name}» y lo que puede hacer cada una. Cada espacio de trabajo está aislado de los demás.</PageHead>
      {team.error && !data ? <ErrorBox error={team.error} onRetry={() => void team.reload()} /> : null}
      {team.loading && !data ? <Loading /> : null}
      {data ? (
        <>
          <Panel title="Miembros">
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Persona</th>
                    <th>Rol</th>
                    <th>Desde</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {data.members.map((member) => (
                    <tr key={member.id}>
                      <td>
                        <strong>{member.name}</strong> {member.is_you ? <Badge tone="info">Tú</Badge> : null}
                        <div className="small muted">{member.email}</div>
                      </td>
                      <td>
                        {session.isOwner ? (
                          <label>
                            <span className="sr-only">Rol de {member.name}</span>
                            <select value={member.role} disabled={busy !== null} style={{ width: "auto" }} onChange={(event) => void changeRole(member, event.target.value)}>
                              {data.roles.map((role) => (
                                <option key={role.key} value={role.key}>
                                  {role.label}
                                </option>
                              ))}
                            </select>
                          </label>
                        ) : (
                          member.role_label
                        )}
                      </td>
                      <td className="nowrap">{formatDate(member.joined_at)}</td>
                      <td>
                        {session.isOwner || member.is_you ? (
                          <Button small variant="ghost" icon={member.is_you ? "logout" : "trash"} onClick={() => setRemoving(member)}>
                            {member.is_you ? "Salir del espacio" : "Quitar"}
                          </Button>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <dl className="kv" style={{ marginTop: 14 }}>
              {data.roles.map((role) => (
                <div key={role.key} style={{ display: "contents" }}>
                  <dt>{role.label}</dt>
                  <dd className="soft">{ROLE_HELP[role.key]}</dd>
                </div>
              ))}
            </dl>
          </Panel>

          {session.isOwner ? (
            <Panel title="Invitar a alguien">
              <div className="stack">
                <p className="soft">Genera un enlace de un solo uso, válido 7 días, y envíaselo a la persona. Si ya tiene cuenta, se unirá con ella; si no, creará una.</p>
                <div className="row">
                  <label>
                    <span className="sr-only">Rol de la invitación</span>
                    <select value={inviteRole} style={{ width: "auto" }} onChange={(event) => setInviteRole(event.target.value)}>
                      {data.roles.map((role) => (
                        <option key={role.key} value={role.key}>
                          {role.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <Button variant="primary" icon="plus" busy={busy === "invite"} onClick={() => void invite()}>
                    Crear enlace de invitación
                  </Button>
                </div>
                {link ? (
                  <Notice tone="ok" title={`Enlace creado (rol: ${link.role}).`}>
                    Cópialo ahora: por seguridad no se volverá a mostrar.
                    <div className="copy-box" style={{ marginTop: 8 }}>
                      <input type="text" readOnly value={link.url} aria-label="Enlace de invitación" onFocus={(event) => event.target.select()} />
                      <Button small onClick={() => void copy(link.url)}>
                        Copiar
                      </Button>
                    </div>
                  </Notice>
                ) : null}
                {data.invites.length ? (
                  <div className="stack stack-sm">
                    <h4>Invitaciones pendientes</h4>
                    {data.invites.map((item) => (
                      <div key={item.id} className="card row row-between">
                        <span>
                          Rol {item.role_label.toLowerCase()} · caduca el {formatDate(item.expires_at)}
                        </span>
                        <Button
                          small
                          variant="ghost"
                          icon="cross"
                          busy={busy === `revoke-${item.id}`}
                          onClick={() =>
                            void run(`revoke-${item.id}`, async () => {
                              await api.delete(`/api/workspace/invites/${item.id}`);
                              await team.reload();
                            }, "Invitación revocada.")
                          }
                        >
                          Revocar
                        </Button>
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            </Panel>
          ) : null}

          <Panel title="Espacios de trabajo">
            <div className="stack">
              {session.isOwner ? (
                <form className="row" onSubmit={rename}>
                  <div style={{ flex: "1 1 260px" }}>
                    <Field label="Nombre de este espacio">
                      <input type="text" value={name} minLength={2} maxLength={120} required onChange={(event) => setName(event.target.value)} />
                    </Field>
                  </div>
                  <Button type="submit" busy={busy === "rename"} disabled={name.trim() === session.workspace.name || name.trim().length < 2} style={{ alignSelf: "flex-end" }}>
                    Cambiar nombre
                  </Button>
                </form>
              ) : null}
              <div className="stack stack-sm">
                {session.isOwner ? (
                  <form
                    className="row"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void changeZone(zone.trim());
                    }}
                  >
                    <div style={{ flex: "1 1 260px" }}>
                      <Field label="Zona horaria de este espacio">
                        <input type="text" list="zonas-horarias" value={zone} maxLength={64} required autoComplete="off" spellCheck={false} onChange={(event) => setZone(event.target.value)} />
                      </Field>
                      <datalist id="zonas-horarias">
                        {knownTimeZones().map((item) => (
                          <option key={item} value={item} />
                        ))}
                      </datalist>
                    </div>
                    <Button type="submit" busy={busy === "zone"} disabled={!zone.trim() || zone.trim() === session.workspace.timezone} style={{ alignSelf: "flex-end" }}>
                      Cambiar zona
                    </Button>
                  </form>
                ) : (
                  <p>
                    <span className="field-label">Zona horaria de este espacio</span> {session.workspace.timezone}
                  </p>
                )}
                <p className="small soft">
                  {session.isOwner ? "Se escribe con su nombre IANA, por ejemplo «Europe/Madrid»: empieza a escribir para ver la lista. " : ""}
                  La analítica agrupa las publicaciones por hora del día en esta zona ({session.workspace.timezone_label}), y las fechas que quedan escritas en el registro de actividad y en los paquetes manuales la usan y la nombran.
                  En pantalla, cada persona ve las fechas en la hora de su propio navegador.
                </p>
                {session.isOwner && here && here !== session.workspace.timezone ? (
                  <Notice tone="info">
                    Este navegador está en <strong>{here}</strong> y el espacio usa <strong>{session.workspace.timezone}</strong>.{" "}
                    <button type="button" className="link-btn" disabled={busy !== null} onClick={() => void changeZone(here)}>
                      Usar {here}
                    </button>
                  </Notice>
                ) : null}
              </div>
              <ul className="stack stack-sm">
                {session.workspaces.map((workspace) => (
                  <li key={workspace.id} className="card row row-between">
                    <span>
                      <strong>{workspace.name}</strong> <span className="small muted">· {data.roles.find((role) => role.key === workspace.role)?.label ?? workspace.role}</span>
                    </span>
                    {workspace.id === session.workspace.id ? (
                      <Badge tone="accent">Abierto ahora</Badge>
                    ) : (
                      <Button small busy={busy === `switch-${workspace.id}`} onClick={() => void switchTo(workspace.id)}>
                        Abrir
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
              <form className="row" onSubmit={create}>
                <div style={{ flex: "1 1 260px" }}>
                  <Field label="Crear otro espacio" hint="Para otro canal, marca o cliente. Tendrá su propio kit de marca, cuentas y equipo.">
                    <input type="text" value={newName} minLength={2} maxLength={120} placeholder="Nombre del espacio nuevo" onChange={(event) => setNewName(event.target.value)} />
                  </Field>
                </div>
                <Button type="submit" icon="plus" busy={busy === "create"} disabled={newName.trim().length < 2}>
                  Crear
                </Button>
              </form>
            </div>
          </Panel>
        </>
      ) : null}
      {removing ? (
        <Confirm
          title={removing.is_you ? "Salir del espacio" : `Quitar a ${removing.name}`}
          confirmLabel={removing.is_you ? "Salir" : "Quitar del espacio"}
          danger
          busy={busy === "remove"}
          onClose={() => setRemoving(null)}
          onConfirm={() => void remove(removing)}
        >
          <p>{removing.is_you ? `Dejarás de tener acceso a «${session.workspace.name}». Para volver necesitarás una invitación nueva.` : `${removing.name} dejará de tener acceso a este espacio. Lo que hizo se conserva en el historial.`}</p>
        </Confirm>
      ) : null}
    </>
  );
}
