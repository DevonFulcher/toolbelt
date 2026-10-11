import type { Preferences } from "./appStore";

// Where the compact-table choice and the collapsed card sections are
// remembered between visits (the same keys as before the React client).
const COMPACT_KEY = "tt-ui-compact";
const COLLAPSED_KEY = "tt-ui-collapsed";
// The cards (by id: repo id and branch) whose chronological log is open.
const LOG_OPEN_KEY = "tt-ui-log-open";

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
  const readList = (key: string): string[] => {
    try {
      const parsed: unknown = JSON.parse(read(key) ?? "[]");
      return Array.isArray(parsed) ? parsed.filter((k): k is string => typeof k === "string") : [];
    } catch {
      return [];
    }
  };
  return {
    loadCompact: () => read(COMPACT_KEY),
    saveCompact: (value) => write(COMPACT_KEY, value ? "1" : "0"),
    loadCollapsed: () => readList(COLLAPSED_KEY),
    saveCollapsed: (keys) => write(COLLAPSED_KEY, JSON.stringify(keys)),
    loadLogOpen: () => readList(LOG_OPEN_KEY),
    saveLogOpen: (ids) => write(LOG_OPEN_KEY, JSON.stringify(ids)),
  };
}

export function browserStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}
