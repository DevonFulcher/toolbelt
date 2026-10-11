import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import type { Snapshot } from "../api/generated";
import { initialState } from "../logic/viewState";
import { createAppStore, type AppStore, type Preferences } from "../store/appStore";
import { StoreProvider } from "../store/StoreContext";
import { syncUrl, type UrlTarget } from "../store/urlSync";
import { NOW } from "./fixtures";

/**
 * A stand-in for `window` that records the address bar: `writes` are the
 * replaced/pushed URLs, `entries` the session history (`position` is the
 * current one), and `back()` / `forward()` step it and fire `popstate`.
 */
export function fakeUrl(search = "") {
  const entries = [search];
  const listeners = new Set<() => void>();
  const target = {
    location: { pathname: "/", search },
    writes: [] as string[],
    pushes: [] as string[],
    entries,
    position: 0,
    history: {
      replaceState(_data: unknown, _unused: string, url: string) {
        target.writes.push(url);
        target.location.search = url.replace(/^[^?]*/, "");
        entries[target.position] = target.location.search;
      },
      pushState(_data: unknown, _unused: string, url: string) {
        target.pushes.push(url);
        target.location.search = url.replace(/^[^?]*/, "");
        entries.splice(target.position + 1, entries.length, target.location.search);
        target.position += 1;
      },
    },
    addEventListener(_type: "popstate", listener: () => void) {
      listeners.add(listener);
    },
    removeEventListener(_type: "popstate", listener: () => void) {
      listeners.delete(listener);
    },
    go(delta: number) {
      const next = target.position + delta;
      if (next < 0 || next >= entries.length) return;
      target.position = next;
      target.location.search = entries[next];
      for (const listener of listeners) listener();
    },
    back: () => target.go(-1),
    forward: () => target.go(1),
  };
  return target satisfies UrlTarget & Record<string, unknown>;
}

export interface Rendered {
  store: AppStore;
  url: ReturnType<typeof fakeUrl>;
  preferences: Preferences & { saved: Record<string, unknown> };
}

/**
 * Renders `ui` against a store holding `snapshot`, with the address bar
 * following the view state as it does in the page.
 */
export function renderWith(
  ui: ReactElement,
  snapshot: Snapshot | null,
  options: { search?: string; storedCompact?: string | null; collapsed?: string[]; logOpen?: string[] } = {},
): Rendered & ReturnType<typeof render> {
  const saved: Record<string, unknown> = {};
  const preferences = {
    saved,
    loadCompact: () => options.storedCompact ?? null,
    saveCompact: (value: string) => {
      saved.compact = value;
    },
    loadCollapsed: () => options.collapsed ?? [],
    saveCollapsed: (keys: string[]) => {
      saved.collapsed = keys;
    },
    loadLogOpen: () => options.logOpen ?? [],
    saveLogOpen: (ids: string[]) => {
      saved.logOpen = ids;
    },
  };
  const search = options.search ?? "";
  const store = createAppStore({
    view: initialState(search, preferences.loadCompact()),
    preferences,
    clock: () => NOW,
  });
  if (snapshot) store.getState().applySnapshot(snapshot, NOW);
  const url = fakeUrl(search);
  syncUrl(store, url);
  const rendered = render(<StoreProvider store={store}>{ui}</StoreProvider>);
  return { ...rendered, store, url, preferences };
}
