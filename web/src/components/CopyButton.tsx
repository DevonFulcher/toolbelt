import { useEffect, useState } from "react";

/** How long the button says "Copied" before it goes back to the copy icon. */
const COPIED_MS = 1500;

/** A small button that copies `text` to the clipboard and says so. `what` names
 * the value in its tooltip and label ("branch name"). */
export function CopyButton({ text, what }: { text: string; what: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const timer = setTimeout(() => setState("idle"), COPIED_MS);
    return () => clearTimeout(timer);
  }, [state]);
  const label = state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : `Copy ${what}`;
  return (
    <button
      type="button"
      className={`copy-button${state === "idle" ? "" : ` ${state}`}`}
      title={label}
      aria-label={label}
      onClick={(event) => {
        // Inside a card or a link: copy only, do not open or select anything.
        event.preventDefault();
        event.stopPropagation();
        navigator.clipboard.writeText(text).then(
          () => setState("copied"),
          () => setState("failed"),
        );
      }}
    >
      {/* The icon is drawn by CSS, so it is not part of the cell's text. */}
    </button>
  );
}
