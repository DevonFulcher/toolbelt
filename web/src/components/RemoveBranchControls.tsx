// A way to remove a branch from its card (`toolbelt git remove`): its worktree,
// the branch and its place in the stack. Removing is not undone, so the button
// opens a dialog that says what happens first; the removal runs on the server in
// the background, so the dialog closes once the request is accepted and the
// card's button reads "Removing…" until the branch is gone. Why it failed is
// shown on the card (a worktree with uncommitted files is refused unless the
// dialog's box to discard them was ticked), and the button is there to try
// again. The command's own rules decide what is refused.
import { useEffect, useState } from "react";
import { removeBranch } from "../api/client";
import type { RemoveInfo } from "../api/generated";
import { Dialog } from "./Dialog";

export function RemoveBranchControls({ id, name, info }: { id: string; name: string; info: RemoveInfo }) {
  const [open, setOpen] = useState(false);
  // The request was accepted and the card's own state has not yet shown the end
  // of the removal (a new reason; success removes the card altogether).
  const [accepted, setAccepted] = useState<{ notice: string | null } | null>(null);

  useEffect(() => {
    if (accepted && !info.removing && info.notice !== accepted.notice) setAccepted(null);
  }, [info, accepted]);

  const working = accepted !== null || info.removing;
  return (
    <>
      <button
        type="button"
        className="agents-toggle"
        disabled={working}
        title="Remove this branch, its worktree and its stack entry"
        onClick={() => setOpen(true)}
      >
        {working ? "Removing…" : "Remove branch"}
      </button>
      {info.notice !== null && !working && (
        <div className="error" role="alert">
          {info.notice}
        </div>
      )}
      {open && (
        <RemoveBranchDialog
          id={id}
          name={name}
          info={info}
          onAccepted={() => {
            setAccepted({ notice: info.notice });
            setOpen(false);
          }}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

function RemoveBranchDialog({
  id,
  name,
  info,
  onAccepted,
  onClose,
}: {
  id: string;
  name: string;
  info: RemoveInfo;
  onAccepted: () => void;
  onClose: () => void;
}) {
  const [force, setForce] = useState(false);
  const [reason, setReason] = useState("");
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const apply = (): void => {
    setSending(true);
    setProblem(null);
    removeBranch(id, { force, reason })
      .then(onAccepted)
      .catch((error: unknown) => {
        setProblem(error instanceof Error ? error.message : String(error));
        setSending(false);
      });
  };

  return (
    <Dialog title={`Remove ${name}`} onClose={onClose}>
      <ul>
        <li>Deletes the branch {name} and its worktree. Unmerged commits are lost; the remote branch is left alone.</li>
        {info.children.length > 0 && (
          <li>
            {info.parent === null ? (
              <>It has branches stacked on it ({info.children.join(", ")}) and no recorded parent to move them to, so it will be refused.</>
            ) : (
              <>
                Moves {info.children.join(", ")} onto <span className="branch">{info.parent}</span> and syncs them.
              </>
            )}
          </li>
        )}
        <li>Closes its open PR, if it has one, with the reason below as a comment.</li>
      </ul>
      <p>
        <label>
          Reason (comment on the PR){" "}
          <input type="text" value={reason} disabled={sending} onChange={(event) => setReason(event.target.value)} />
        </label>
      </p>
      <p>
        <label>
          <input type="checkbox" checked={force} disabled={sending} onChange={(event) => setForce(event.target.checked)} /> Discard
          uncommitted changes in the worktree (otherwise a worktree that has any is refused)
        </label>
      </p>
      <p className="muted">It runs in the background: the card shows when it is done.</p>
      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}
      <p>
        <button type="button" className="merge-button" disabled={sending} onClick={apply}>
          {sending ? "Sending…" : "Remove branch"}
        </button>{" "}
        <button type="button" className="agents-toggle" disabled={sending} onClick={onClose}>
          Cancel
        </button>
      </p>
    </Dialog>
  );
}
