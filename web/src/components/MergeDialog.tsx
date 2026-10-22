// Merge a PR (`toolbelt git merge`): asks first, in a dialog rather than the
// browser's confirm box. Opened by the Merge button of a card; nothing is merged
// until Merge is pressed. The merge runs in the background: the dialog closes at
// once, the card reads "Merging…", and if it fails the card says why.
import { useState } from "react";
import { mergeBranch } from "../api/client";
import { Dialog } from "./Dialog";

export function MergeDialog({ id, target, onClose }: { id: string; target: string; onClose: () => void }) {
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const merge = (): void => {
    setSending(true);
    setProblem(null);
    mergeBranch(id)
      .then(onClose)
      .catch((error: unknown) => {
        setProblem(error instanceof Error ? error.message : String(error));
        setSending(false);
      });
  };

  return (
    <Dialog title={`Merge ${target}`} onClose={onClose}>
      <p>Squash-merge {target} on GitHub and sync its stack (toolbelt git merge).</p>
      <p className="muted">
        If the PR is part of a GitHub stack, this also merges the open PRs below it. It merges in the background: the
        card shows when it is done.
      </p>
      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}
      <p>
        <button type="button" className="merge-button" disabled={sending} onClick={merge}>
          {sending ? "Sending…" : "Merge"}
        </button>{" "}
        <button type="button" className="agents-toggle" disabled={sending} onClick={onClose}>
          Cancel
        </button>
      </p>
    </Dialog>
  );
}
