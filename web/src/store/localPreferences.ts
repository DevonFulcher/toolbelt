import type { Preferences } from "./appStore";

// Where the compact-table choice and the collapsed card sections are
// remembered between visits (the same keys as before the React client).
const COMPACT_KEY = "tt-ui-compact";
const COLLAPSED_KEY = "tt-ui-collapsed";

/**
 * Preferences in `localStorage`. Remembering is a convenience: a blocked or
 * unavailable store (private windows) just means nothing is remembered.
 */
export function localPreferences(storage: Storage | null): Preferences {
  const read = (key: string): string | null => {
    try {
      return storage ? storage.getItem(key) : null;
    } catch {
      return null;
    }
  };
  const write = (key: string, value: string): void => {
    try {
      if (storage) storage.setItem(key, value);
    } catch {
      // see above
    }
  };
  return {
    loadCompact: () => read(COMPACT_KEY),
    saveCompact: (value) => write(COMPACT_KEY, value ? "1" : "0"),
    loadCollapsed: () => {
      try {
        const parsed: unknown = JSON.parse(read(COLLAPSED_KEY) ?? "[]");
        return Array.isArray(parsed) ? parsed.filter((k): k is string => typeof k === "string") : [];
      } catch {
        return [];
      }
    },
    saveCollapsed: (keys) => write(COLLAPSED_KEY, JSON.stringify(keys)),
  };
}

export function browserStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}
