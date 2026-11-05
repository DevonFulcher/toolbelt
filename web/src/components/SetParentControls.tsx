// A branch's stack parent, on its card: what it is, and a way to repoint it
// (`toolbelt git set-parent`). The change runs on the server in the background:
// the parent reads "Setting parent…" until it is done, and why it failed is
// shown here. The command's own rules decide what is refused; the options only
// leave out what can never be a parent (the branch itself, what is stacked on
// it, and the parent it already has).
import { useEffect, useState } from "react";
import { setParent } from "../api/client";
import type { SetParentInfo } from "../api/generated";
import { subtreeIds } from "../store/selectors";
import { useApp } from "../store/StoreContext";

/** The trunk, then the repo's other branches, as one string so the selector's result is stable. */
function useOptions(id: string, info: SetParentInfo): string[] {
  const joined = useApp((s) => {
    const branch = s.branchesById[id];
    if (!branch) return "";
    const excluded = new Set(subtreeIds(s, id));
    const names = s.branches
      .filter((other) => other.repo_id === branch.repo_id && !excluded.has(other.id))
      .map((other) => other.name);
    return JSON.stringify([info.trunk, ...names].filter((name, at, all) => name !== info.parent && all.indexOf(name) === at));
  });
  return joined === "" ? [] : (JSON.parse(joined) as string[]);
}

export function SetParentControls({ id, name, info }: { id: string; name: string; info: SetParentInfo }) {
  const options = useOptions(id, info);
  const [open, setOpen] = useState(false);
  const [choice, setChoice] = useState("");
  // The request is out, or accepted and not yet reflected in `info`.
  const [submitted, setSubmitted] = useState<{ parent: string; notice: string | null } | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  // Accepted: the card's own state takes over once the change has ended, with
  // the new parent or a reason.
  useEffect(() => {
    if (submitted && info.setting === null && (info.parent === submitted.parent || info.notice !== submitted.notice)) {
      setSubmitted(null);
      if (info.parent === submitted.parent) setOpen(false);
    }
  }, [info, submitted]);

  const working = submitted !== null || info.setting !== null;
  const target = choice !== "" && options.includes(choice) ? choice : (options[0] ?? "");

  const apply = (): void => {
    setProblem(null);
    setSubmitted({ parent: target, notice: info.notice });
    setParent(id, target).catch((error: unknown) => {
      setSubmitted(null);
      setProblem(error instanceof Error ? error.message : String(error));
    });
  };

  const reason = problem ?? info.notice;
  return (
    <>
      <span className="branch">{info.parent}</span>{" "}
      {working ? (
        <button type="button" className="agents-toggle" disabled>
          Setting parent…
        </button>
      ) : (
        <button
          type="button"
          className="agents-toggle"
          aria-expanded={open}
          title="Repoint this branch's stack parent"
          onClick={() => setOpen(!open)}
        >
          {open ? "Cancel" : "Change parent"}
        </button>
      )}
      {open && !working && (
        <div className="set-parent">
          <label>
            New parent{" "}
            <select value={target} onChange={(event) => setChoice(event.target.value)}>
              {options.map((option) => (
                <option key={option} value={option}>
                  {option === info.trunk ? `${option} (trunk)` : option}
                </option>
              ))}
            </select>
          </label>{" "}
          <button type="button" className="merge-button" disabled={target === ""} onClick={apply}>
            Set parent
          </button>
          <div className="set-parent-effect">
            {`Makes ${target || "the new parent"} the parent of ${name} (it must exist and form no cycle), merges it in and pushes ${name}, and points ${name}'s open PR at it. Commits inherited from ${info.parent} stay on ${name}.`}
          </div>
        </div>
      )}
      {reason !== null && (
        <div className="error" role="alert">
          {reason}
        </div>
      )}
    </>
  );
}
