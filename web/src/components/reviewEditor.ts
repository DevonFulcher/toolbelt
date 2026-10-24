// CodeMirror pieces of the review view: read-only editors, and hover and go
// to definition from the language server.
import { python } from "@codemirror/lang-python";
import { EditorState, type Extension } from "@codemirror/state";
import { oneDark } from "@codemirror/theme-one-dark";
import { EditorView, hoverTooltip } from "@codemirror/view";
import { fetchReviewDefinition, fetchReviewHover, type ReviewRevisions, type ReviewSide } from "../api/client";
import type { ReviewDefinitionTarget } from "../api/generated";

export const isPython = (path: string): boolean => path.endsWith(".py");

const prefersDark = (): boolean => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;

export function readOnlyExtensions(path: string): Extension[] {
  return [
    EditorState.readOnly.of(true),
    EditorView.editable.of(false),
    EditorView.lineWrapping,
    isPython(path) ? python() : [],
    prefersDark() ? oneDark : [],
  ];
}

export interface LspHandlers {
  revisions: ReviewRevisions;
  side: ReviewSide;
  /** The file's path on this side of the diff. */
  path: string;
  onDefinition: (target: ReviewDefinitionTarget) => void;
  onError: (message: string) => void;
}

function lspPosition(view: EditorView, pos: number) {
  const line = view.state.doc.lineAt(pos);
  // LSP and CodeMirror both count UTF-16 code units.
  return { line: line.number - 1, character: pos - line.from };
}

const messageOf = (error: unknown): string => (error instanceof Error ? error.message : String(error));

/** Hover tooltips, and Cmd/Ctrl-click to go to a definition. */
export function lspExtensions({ revisions, side, path, onDefinition, onError }: LspHandlers): Extension {
  return [
    hoverTooltip(async (view, pos) => {
      let text: string | null;
      try {
        text = (await fetchReviewHover(revisions, { side, path, ...lspPosition(view, pos) })).text;
      } catch (error) {
        onError(messageOf(error));
        return null;
      }
      if (text === null) return null;
      return {
        pos,
        create: () => {
          const dom = document.createElement("pre");
          dom.className = "review-hover";
          dom.textContent = text;
          return { dom };
        },
      };
    }),
    EditorView.domEventHandlers({
      mousedown: (event, view) => {
        if (!(event.metaKey || event.ctrlKey)) return false;
        const pos = view.posAtCoords({ x: event.clientX, y: event.clientY });
        if (pos === null) return false;
        event.preventDefault();
        fetchReviewDefinition(revisions, { side, path, ...lspPosition(view, pos) })
          .then(({ target }) => (target === null ? onError("No definition found") : onDefinition(target)))
          .catch((error: unknown) => onError(messageOf(error)));
        return true;
      },
    }),
  ];
}
