// A way to repoint a branch's stack parent from its card (`toolbelt git
// set-parent`); the stack the card sits in already shows what the parent is. Choosing happens in a dialog, like merging; the
// change runs on the server in the background, so the dialog closes once the
// request is accepted and the card's button reads "Setting parent…" until it is
// done. Why it failed is shown on the card, and the button is there to try
// again. The command's own rules decide what is refused; the options only leave
// out what can never be a parent (the branch itself, what is stacked on it, the
// parent it already has, and finished branches); nothing is chosen until the
// person picks one.
import { useEffect, useState } from "react";
import { setParent } from "../api/client";
import type { SetParentInfo } from "../api/generated";
import { subtreeIds } from "../store/selectors";
import { useApp } from "../store/StoreContext";
import { Dialog } from "./Dialog";
import { StableSelect } from "./StableSelect";

/** The trunk, then the repo's other branches that are not finished, as one string so the selector's result is stable. */
function useOptions(id: string, info: SetParentInfo): string[] {
  const joined = useApp((s) => {
    const branch = s.branchesById[id];
    if (!branch) return "";
    const excluded = new Set(subtreeIds(s, id));
    const names = s.branches
      .filter((other) => {
        const status = s.statuses[other.id];
        const finished = status?.load === "loaded" && status.next.kind === "done";
        return other.repo_id === branch.repo_id && !excluded.has(other.id) && !finished;
      })
      .map((other) => other.name);
    return JSON.stringify([info.trunk, ...names].filter((name, at, all) => name !== info.parent && all.indexOf(name) === at));
  });
  return joined === "" ? [] : (JSON.parse(joined) as string[]);
}

export function SetParentControls({ id, name, info }: { id: string; name: string; info: SetParentInfo }) {
  const [open, setOpen] = useState(false);
  // The request was accepted and the card's own state has not yet shown the end
  // of the change (the new parent, or a new reason).
  const [accepted, setAccepted] = useState<{ parent: string; notice: string | null } | null>(null);

  useEffect(() => {
    if (accepted && info.setting === null && (info.parent === accepted.parent || info.notice !== accepted.notice)) {
      setAccepted(null);
    }
  }, [info, accepted]);

  const working = accepted !== null || info.setting !== null;
  return (
    <>
      <button
        type="button"
        className="agents-toggle"
        disabled={working}
        title="Repoint this branch's stack parent"
        onClick={() => setOpen(true)}
      >
        {working ? "Setting parent…" : "Change parent"}
      </button>
      {info.notice !== null && !working && (
        <div className="error" role="alert">
          {info.notice}
        </div>
      )}
      {open && (
        <SetParentDialog
          id={id}
          name={name}
          info={info}
          onAccepted={(parent) => {
            setAccepted({ parent, notice: info.notice });
            setOpen(false);
          }}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

function SetParentDialog({
  id,
  name,
  info,
  onAccepted,
  onClose,
}: {
  id: string;
  name: string;
  info: SetParentInfo;
  onAccepted: (parent: string) => void;
  onClose: () => void;
}) {
  const options = useOptions(id, info);
  const [choice, setChoice] = useState("");
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const target = options.includes(choice) ? choice : "";

  const apply = (): void => {
    setSending(true);
    setProblem(null);
    setParent(id, target)
      .then(() => onAccepted(target))
      .catch((error: unknown) => {
        setProblem(error instanceof Error ? error.message : String(error));
        setSending(false);
      });
  };

  return (
    <Dialog title={`Change the parent of ${name}`} onClose={onClose}>
      <p>
        {name} is stacked on <span className="branch">{info.parent}</span>.
      </p>
      <p>
        <label>
          New parent{" "}
          <StableSelect
            value={target}
            disabled={sending}
            options={[
              { value: "", label: "Choose a branch…" },
              ...options.map((option) => ({ value: option, label: option === info.trunk ? `${option} (trunk)` : option })),
            ]}
            onChange={setChoice}
          />
        </label>
      </p>
      <p className="muted">
        {target === ""
          ? "Choose the branch it should be stacked on. "
          : `Makes ${target} the parent of ${name} (it must exist and form no cycle), merges it in and pushes ${name}, and points ${name}'s open PR at it. Commits inherited from ${info.parent} stay on ${name}. `}
        It runs in the background: the card shows when it is done.
      </p>
      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}
      <p>
        <button type="button" className="merge-button" disabled={sending || target === ""} onClick={apply}>
          {sending ? "Sending…" : "Set parent"}
        </button>{" "}
        <button type="button" className="agents-toggle" disabled={sending} onClick={onClose}>
          Cancel
        </button>
      </p>
    </Dialog>
  );
}
