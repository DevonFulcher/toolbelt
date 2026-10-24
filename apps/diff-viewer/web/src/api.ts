import type { DifftasticFile } from "./difftastic";

export interface ChangedFile {
  status: string;
  path: string;
  old_path: string | null;
}

export interface FileDiff extends ChangedFile {
  old_text: string;
  new_text: string;
  difftastic: DifftasticFile;
}

export interface Revisions {
  repo: string;
  base: string;
  head: string;
}

async function call<T>(command: string, args: Record<string, unknown>): Promise<T> {
  if ("__TAURI_INTERNALS__" in window) {
    const { invoke } = await import("@tauri-apps/api/core");
    return invoke<T>(command, args);
  }
  const { file, ...rest } = args as { file?: ChangedFile } & Record<string, string | number>;
  const query = new URLSearchParams(Object.entries(rest).map(([key, value]) => [key, String(value)]));
  if (file) {
    query.set("status", file.status);
    query.set("path", file.path);
    if (file.old_path) query.set("old_path", file.old_path);
  }
  const response = await fetch(`/api/${command}?${query}`);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error);
  return body as T;
}

export const changedFiles = (revisions: Revisions) => call<ChangedFile[]>("changed_files", { ...revisions });

export const fileDiff = (revisions: Revisions, file: ChangedFile) =>
  call<FileDiff>("file_diff", { ...revisions, file });

export type Side = "old" | "new";

export interface DefinitionTarget {
  /** Relative to the revision's root; null when the target is outside the checkout. */
  path: string | null;
  uri: string;
  line: number;
  character: number;
}

interface HoverResult {
  contents: string | { kind?: string; value: string; language?: string } | (string | { value: string })[];
}

export interface Position {
  side: Side;
  path: string;
  line: number;
  character: number;
}

export async function lspHover(revisions: Revisions, position: Position): Promise<string | null> {
  const result = await call<HoverResult | null>("lsp_hover", { ...revisions, ...position });
  if (!result) return null;
  const { contents } = result;
  const parts = Array.isArray(contents) ? contents : [contents];
  return parts.map((part) => (typeof part === "string" ? part : part.value)).join("\n\n");
}

export const lspDefinition = (revisions: Revisions, position: Position) =>
  call<DefinitionTarget | null>("lsp_definition", { ...revisions, ...position });

export const fileText = (revisions: Revisions, side: Side, path: string) =>
  call<string>("file_text", { ...revisions, side, path });
