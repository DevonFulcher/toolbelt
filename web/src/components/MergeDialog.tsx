// Merge a PR (`toolbelt git merge`): asks first, in a dialog rather than the
// browser's confirm box, and shows toolbelt's output here when the merge fails.
// Opened by the Merge button of a card; nothing is merged until Merge is pressed.
import { useState } from "react";
import { mergeBranch } from "../api/client";
import { Dialog } from "./Dialog";

type Phase = { name: "ready" } | { name: "merging" } | { name: "failed"; text: string };

export function MergeDialog({ id, target, onClose }: { id: string; target: string; onClose: () => void }) {
  const [phase, setPhase] = useState<Phase>({ name: "ready" });

  const merge = (): void => {
    setPhase({ name: "merging" });
    mergeBranch(id)
      .then((result) => {
        if (result.ok) onClose();
        else setPhase({ name: "failed", text: result.output });
      })
      .catch((error: unknown) =>
        setPhase({ name: "failed", text: error instanceof Error ? error.message : String(error) }),
      );
  };

  return (
    <Dialog title={`Merge ${target}`} onClose={onClose}>
      <p>Squash-merge {target} on GitHub and sync its stack (toolbelt git merge).</p>
      <p className="muted">If the PR is part of a GitHub stack, this also merges the open PRs below it.</p>
      {phase.name === "failed" && (
        <pre className="merge-error" role="alert">
          {phase.text}
        </pre>
      )}
      <p>
        <button type="button" className="merge-button" disabled={phase.name === "merging"} onClick={merge}>
          {phase.name === "merging" ? "Merging…" : "Merge"}
        </button>{" "}
        <button type="button" className="agents-toggle" disabled={phase.name === "merging"} onClick={onClose}>
          Cancel
        </button>
      </p>
    </Dialog>
  );
}
