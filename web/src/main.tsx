import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { EVENTS_URL, fetchSnapshot } from "./api/client";
import { App } from "./components/App";
import { startLiveUpdates, type EventSourceLike } from "./live/stream";
import { initialState } from "./logic/viewState";
import { createAppStore } from "./store/appStore";
import { browserStorage, localPreferences } from "./store/localPreferences";
import { StoreProvider } from "./store/StoreContext";
import { syncUrl } from "./store/urlSync";
import "./styles.css";

const preferences = localPreferences(browserStorage());
const store = createAppStore({
  view: initialState(window.location.search, preferences.loadCompact()),
  preferences,
});
syncUrl(store, window);

const live = startLiveUpdates({
  store,
  fetchSnapshot: () => fetchSnapshot(),
  openStream: () => new EventSource(EVENTS_URL) as EventSourceLike,
  isVisible: () => document.visibilityState !== "hidden",
  now: () => Date.now(),
  setTimer: (fn, ms) => window.setTimeout(fn, ms),
  clearTimer: (handle) => window.clearTimeout(handle as number),
});
document.addEventListener("visibilitychange", live.visibilityChanged);
// Fallbacks for environments that do not announce visibility changes.
window.addEventListener("focus", live.visibilityChanged);
window.addEventListener("pageshow", live.visibilityChanged);

const root = document.getElementById("root");
if (!root) throw new Error("no #root element");
createRoot(root).render(
  <StrictMode>
    <StoreProvider store={store}>
      <App />
    </StoreProvider>
  </StrictMode>,
);
