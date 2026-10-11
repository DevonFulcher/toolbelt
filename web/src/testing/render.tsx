import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import type { Snapshot } from "../api/generated";
import { initialState } from "../logic/viewState";
import { createAppStore, type AppStore, type Preferences } from "../store/appStore";
import { StoreProvider } from "../store/StoreContext";
import { syncUrl, type UrlTarget } from "../store/urlSync";
import { NOW } from "./fixtures";

/** A stand-in for `window` that records the address bar. */
export function fakeUrl(search = ""): UrlTarget & { writes: string[] } {
  const target = {
    location: { pathname: "/", search },
    writes: [] as string[],
    history: {
      replaceState(_data: unknown, _unused: string, url: string) {
        target.writes.push(url);
        target.location.search = url.replace(/^[^?]*/, "");
      },
    },
  };
  return target;
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
  options: { search?: string; storedCompact?: string | null; collapsed?: string[] } = {},
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
