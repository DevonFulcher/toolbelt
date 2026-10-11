import { parseState, serializeState } from "../logic/viewState";
import type { AppStore } from "./appStore";

/** The parts of `window` the URL sync uses. */
export interface UrlTarget {
  location: { pathname: string; search: string };
  history: {
    replaceState(data: unknown, unused: string, url: string): void;
    pushState(data: unknown, unused: string, url: string): void;
  };
  addEventListener(type: "popstate", listener: () => void): void;
  removeEventListener(type: "popstate", listener: () => void): void;
}

/**
 * Keeps the address bar in step with the view state. Changing the focus root
 * adds a history entry (so back and forward step through where you travelled
 * a stack); every other change replaces the current one. A stale or
 * hand-edited URL (e.g. the retired `view=timeline`, or a `root` that is not
 * a branch) is rewritten at once to the state it was understood as, without
 * an entry. Back/forward (popstate) put the URL's state back in the store.
 * Returns the unsubscribe function.
 */
export function syncUrl(store: AppStore, target: UrlTarget): () => void {
  let fromHistory = false;
  const write = (rootChanged: boolean): void => {
    const query = serializeState(store.getState().view);
    if (query === target.location.search) return;
    const url = target.location.pathname + query;
    if (rootChanged && !fromHistory) target.history.pushState(null, "", url);
    else target.history.replaceState(null, "", url);
  };
  write(false);
  const unsubscribe = store.subscribe((state, previous) => {
    if (state.view === previous.view) return;
    // Dropping a root that is not a branch (anymore) is a correction.
    const wasKnown = previous.view.root === "" || previous.view.root in state.branchesById;
    write(state.view.root !== previous.view.root && wasKnown);
  });
  const onPopState = (): void => {
    const current = store.getState().view;
    // The compact table is a remembered preference, not part of a history entry.
    const next = { ...parseState(target.location.search), compact: current.compact };
    const { ready, branchesById } = store.getState();
    if (ready && !(next.root in branchesById)) next.root = "";
    fromHistory = true;
    try {
      store.getState().setView(next);
    } finally {
      fromHistory = false;
    }
  };
  target.addEventListener("popstate", onPopState);
  return () => {
    unsubscribe();
    target.removeEventListener("popstate", onPopState);
  };
}
