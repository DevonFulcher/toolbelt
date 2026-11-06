// A large dialog over the page (an agent run's conversation, a branch's runs):
// closed with its Close button, the Escape key or a click on the dimmed page
// behind it.
import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

export function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const dialog = useRef<HTMLDivElement>(null);
  // Callers pass a new `onClose` on every render, and the page renders on every
  // update from the server: the effect below must not re-run for it, or it
  // would take focus back from an open select every few seconds.
  const close = useRef(onClose);
  close.current = onClose;

  useEffect(() => {
    const before = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialog.current?.focus();
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") close.current();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      before?.focus();
    };
  }, []);

  return createPortal(
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div ref={dialog} className="modal" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button type="button" className="agents-toggle" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>,
    document.body,
  );
}
