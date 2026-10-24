import { type Extension } from "@codemirror/state";
import { EditorView, hoverTooltip } from "@codemirror/view";
import { lspDefinition, lspHover, type DefinitionTarget, type Revisions, type Side } from "./api";

export interface LspHandlers {
  revisions: Revisions;
  side: Side;
  /** Path of the file on this side of the diff. */
  path: string;
  onDefinition: (target: DefinitionTarget) => void;
  onError: (message: string) => void;
}

function lspPosition(view: EditorView, pos: number) {
  const line = view.state.doc.lineAt(pos);
  // LSP and CodeMirror both count UTF-16 code units.
  return { line: line.number - 1, character: pos - line.from };
}

/** Hover tooltips, and Cmd/Ctrl-click to go to a definition, backed by the language server. */
export function lspExtensions({ revisions, side, path, onDefinition, onError }: LspHandlers): Extension {
  return [
    hoverTooltip(async (view, pos) => {
      let text: string | null;
      try {
        text = await lspHover(revisions, { side, path, ...lspPosition(view, pos) });
      } catch (error) {
        onError(error instanceof Error ? error.message : String(error));
        return null;
      }
      if (!text) return null;
      return {
        pos,
        create: () => {
          const dom = document.createElement("pre");
          dom.className = "lsp-hover";
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
        lspDefinition(revisions, { side, path, ...lspPosition(view, pos) })
          .then((target) => (target ? onDefinition(target) : onError("No definition found")))
          .catch((error) => onError(error instanceof Error ? error.message : String(error)));
        return true;
      },
    }),
  ];
}
