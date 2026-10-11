// The serialized state catalog (src/toolbelt/ui/pr_states.py) the page embeds,
// for the node tests. tests/test_ui_script.py passes it in TT_STATE_CATALOG
// (a path to a JSON file); run standalone, it asks Python for it.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

function load() {
  if (process.env.TT_STATE_CATALOG) {
    return JSON.parse(readFileSync(process.env.TT_STATE_CATALOG, "utf8"));
  }
  const out = execFileSync("uv", ["run", "python", "-m", "toolbelt.ui.pr_states"], {
    encoding: "utf8",
  });
  return JSON.parse(out);
}

export const catalog = load();
