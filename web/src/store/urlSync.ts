import { serializeState } from "../logic/viewState";
import type { AppStore } from "./appStore";

/** The parts of `window` the URL sync uses. */
export interface UrlTarget {
  location: { pathname: string; search: string };
  history: { replaceState(data: unknown, unused: string, url: string): void };
}

/**
 * Keeps the address bar in step with the view state (replaceState: no
 * history entries). A stale or hand-edited URL (e.g. the retired
 * `view=timeline`) is rewritten at once to the state it was understood as.
 * Returns the unsubscribe function.
 */
export function syncUrl(store: AppStore, target: UrlTarget): () => void {
  const write = (): void => {
    const query = serializeState(store.getState().view);
    if (query !== target.location.search) {
      target.history.replaceState(null, "", target.location.pathname + query);
    }
  };
  write();
  return store.subscribe((state, previous) => {
    if (state.view !== previous.view) write();
  });
}
