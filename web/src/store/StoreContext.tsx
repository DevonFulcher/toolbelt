import { createContext, useContext, type ReactNode } from "react";
import { useStore } from "zustand";
import type { AppState, AppStore } from "./appStore";

const StoreContext = createContext<AppStore | null>(null);

export function StoreProvider({ store, children }: { store: AppStore; children: ReactNode }) {
  return <StoreContext.Provider value={store}>{children}</StoreContext.Provider>;
}

export function useAppStore(): AppStore {
  const store = useContext(StoreContext);
  if (!store) throw new Error("useAppStore outside a StoreProvider");
  return store;
}

/** Subscribes to a slice of the store; re-renders only when it changes. */
export function useApp<T>(selector: (state: AppState) => T): T {
  return useStore(useAppStore(), selector);
}
