// What every panel of the project screen shares.

import { createContext, useContext } from "react";
import type { ProjectDetail, Run, Step } from "../../api";

export interface ProjectCtx {
  project: ProjectDetail;
  /** Replace the project with the detail a mutation returned. */
  setProject: (project: ProjectDetail) => void;
  reload: () => Promise<void>;
  /** The signed-in person may change things and the project is not cancelled. */
  canEdit: boolean;
}

export const ProjectContext = createContext<ProjectCtx | null>(null);

export function useProject(): ProjectCtx {
  const value = useContext(ProjectContext);
  if (!value) throw new Error("ProjectContext missing");
  return value;
}

export function stepOf(project: ProjectDetail, kind: string): Step {
  return project.steps.find((step) => step.kind === kind) as Step;
}

/** The background job, if any, that is working on an export, design or publication right now. */
export function jobFor(project: ProjectDetail, targetId: string): Run | undefined {
  return project.jobs.find((job) => job.target_id === targetId);
}

export const FORMAT_LABELS: Record<string, string> = {
  vertical: "Vertical 9:16",
  horizontal: "Horizontal 16:9",
  square: "Cuadrado 1:1",
};

export const FRAMING_LABELS: Record<string, string> = {
  blur: "Completo con fondo desenfocado",
  crop: "Recortar para llenar",
  pad: "Completo con bandas negras",
};

/** Formats the target platforms need, in a stable order. */
export function neededFormats(project: ProjectDetail, platforms: { key: string; format: string }[]): string[] {
  const out: string[] = [];
  for (const key of project.platforms) {
    const format = platforms.find((platform) => platform.key === key)?.format;
    if (format && !out.includes(format)) out.push(format);
  }
  return out;
}
