import { python } from "@codemirror/lang-python";
import { MergeView } from "@codemirror/merge";
import { EditorState, type Extension } from "@codemirror/state";
import { oneDark } from "@codemirror/theme-one-dark";
import { EditorView } from "@codemirror/view";
import { changedFiles, fileDiff, fileText, type ChangedFile, type DefinitionTarget, type Revisions, type Side } from "./api";
import { buildChanges } from "./changes";
import { lspExtensions } from "./lsp";

const normalizeLineEndings = (text: string) => text.replace(/\r\n?/g, "\n");

function languageExtensions(path: string): Extension[] {
  return path.endsWith(".py") ? [python()] : [];
}

const definitionPanel = document.querySelector<HTMLElement>("#definition")!;
const definitionTitle = document.querySelector<HTMLElement>("#definition-title")!;
const definitionBody = document.querySelector<HTMLElement>("#definition-body")!;
document.querySelector<HTMLButtonElement>("#definition-close")!.onclick = () => {
  definitionPanel.hidden = true;
};

/** Shows the file a definition lives in, with the definition's line selected. */
async function showDefinition(revisions: Revisions, side: Side, target: DefinitionTarget) {
  definitionBody.replaceChildren();
  definitionPanel.hidden = false;
  if (target.path === null) {
    definitionTitle.textContent = `${target.uri} (outside the checkout)`;
    return;
  }
  definitionTitle.textContent = `${target.path}:${target.line + 1} (${side} side)`;
  const text = await fileText(revisions, side, target.path);
  const view = new EditorView({
    doc: text,
    extensions: [oneDark, EditorState.readOnly.of(true), EditorView.editable.of(false), languageExtensions(target.path)],
    parent: definitionBody,
  });
  const line = view.state.doc.line(Math.min(target.line + 1, view.state.doc.lines));
  view.dispatch({
    selection: { anchor: line.from + target.character },
    effects: EditorView.scrollIntoView(line.from, { y: "center" }),
  });
}

const params = new URLSearchParams(location.search);
const form = document.querySelector<HTMLFormElement>("#revisions")!;
const fileList = document.querySelector<HTMLUListElement>("#files")!;
const diffRoot = document.querySelector<HTMLElement>("#diff")!;
const status = document.querySelector<HTMLElement>("#status")!;
for (const name of ["repo", "base", "head"]) {
  (form.elements.namedItem(name) as HTMLInputElement).value = params.get(name) ?? "";
}

function setStatus(text: string) {
  status.textContent = text;
}

async function showFile(revisions: Revisions, file: ChangedFile) {
  diffRoot.replaceChildren();
  setStatus(`Loading ${file.path}…`);
  try {
    const diff = await fileDiff(revisions, file);
    const { old_text: a, new_text: b, difftastic } = diff;
    const readOnly = (side: Side, path: string) => [
      oneDark,
      EditorState.readOnly.of(true),
      EditorView.editable.of(false),
      EditorView.lineWrapping,
      languageExtensions(path),
      path.endsWith(".py")
        ? lspExtensions({
            revisions,
            side,
            path,
            onDefinition: (target) => void showDefinition(revisions, side, target),
            onError: setStatus,
          })
        : [],
    ];
    new MergeView({
      a: { doc: a, extensions: readOnly("old", file.old_path ?? file.path) },
      b: { doc: b, extensions: readOnly("new", file.path) },
      parent: diffRoot,
      highlightChanges: true,
      gutter: true,
      collapseUnchanged: { margin: 3, minSize: 4 },
      // CodeMirror passes its own "\n"-normalized copies of the texts to `override`.
      diffConfig: {
        override: (x, y) =>
          x === normalizeLineEndings(a) && y === normalizeLineEndings(b) ? buildChanges(difftastic, a, b) : [],
      },
    });
    const structural = difftastic.language !== "Text";
    setStatus(`${file.path}: ${difftastic.language}${structural ? "" : " (line diff, no syntax tree)"}`);
  } catch (error) {
    setStatus(`${file.path}: ${error instanceof Error ? error.message : error}`);
  }
}

async function load() {
  const data = new FormData(form);
  const revisions = Object.fromEntries(data) as unknown as Revisions;
  history.replaceState(null, "", `?${new URLSearchParams(revisions as unknown as Record<string, string>)}`);
  fileList.replaceChildren();
  diffRoot.replaceChildren();
  setStatus("Loading changed files…");
  try {
    const files = await changedFiles(revisions);
    setStatus(`${files.length} changed files`);
    for (const file of files) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.textContent = `${file.status} ${file.path}`;
      button.onclick = () => {
        fileList.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
        button.classList.add("active");
        void showFile(revisions, file);
      };
      item.append(button);
      fileList.append(item);
    }
  } catch (error) {
    setStatus(error instanceof Error ? error.message : String(error));
  }
}

form.onsubmit = (event) => {
  event.preventDefault();
  void load();
};
if (params.get("repo")) void load();
