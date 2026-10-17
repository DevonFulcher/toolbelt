// When the live stream is open. It is only open while the page is being
// looked at: once the page has been hidden (background tab, minimized
// window) for GRACE_MS the stream is closed, which is what makes the server
// stop polling GitHub, and it is reopened as soon as the page is visible
// again (the server then refetches whatever went stale). A brief tab switch
// shorter than the grace period never closes the stream.

export const RECONNECT_DELAY_MS = 3000;
export const GRACE_MS = 15000;
/** How often visibility is re-read in case no event announced a change. */
export const VISIBILITY_POLL_MS = 3000;

export type ConnectionState =
  | "connecting"
  | "live"
  | "reconnecting"
  | "disconnected"
  | "paused"
  | "resuming";

/**
 * On connect the server reports the version of the latest event; if it is
 * not the last one this page has seen, something was missed.
 */
export function missedEvents(lastVersion: number | null, helloVersion: number): boolean {
  return lastVersion !== helloVersion;
}

export type ConnectionAction = "connect" | "disconnect" | "wait" | "none";

/**
 * What to do about the stream given the page's visibility: "connect" a
 * missing one when visible, "disconnect" a live one once the page has been
 * hidden for the grace period, "wait" while that period runs.
 */
export function connectionAction(input: {
  visible: boolean;
  connected: boolean;
  hiddenForMs: number;
  graceMs: number;
}): ConnectionAction {
  const { visible, connected, hiddenForMs, graceMs } = input;
  if (visible) return connected ? "none" : "connect";
  if (!connected) return "none";
  return hiddenForMs >= graceMs ? "disconnect" : "wait";
}

/** The label for the connection indicator. (How fresh the data is is said on
 * each card and view: this only says whether the page is connected.) */
export function statusLabel(state: ConnectionState): string {
  switch (state) {
    case "connecting":
      return "connecting…";
    case "resuming":
      return "resuming…";
    case "paused":
      return "paused (tab hidden)";
    case "reconnecting":
      return "reconnecting…";
    case "disconnected":
      return "disconnected";
    case "live":
      return "live";
  }
}

export interface ConnectionDeps {
  isVisible(): boolean;
  /** Current time, ms. */
  now(): number;
  open(): void;
  close(): void;
  setTimer(fn: () => void, ms: number): unknown;
  clearTimer(handle: unknown): void;
  /** Told "paused" when the stream was closed for being hidden and "resuming" when reopened after that. */
  onState(state: "paused" | "resuming"): void;
}

export interface ConnectionController {
  /** Call once at page load. */
  start(): void;
  /**
   * Re-reads the page's visibility. Called on "visibilitychange" and also as
   * a fallback (window focus, pageshow, a slow timer): some embedded
   * browsers never dispatch visibilitychange, which used to leave a page
   * that was opened in the background paused for good. Idempotent: the time
   * the page went hidden is kept while it stays hidden, so calling this
   * repeatedly does not restart the grace period.
   */
  visibilityChanged(): void;
  /** The stream died on its own (not by us); reconnect if still wanted. */
  connectionLost(): void;
}

export function createConnectionController(deps: ConnectionDeps): ConnectionController {
  let connected = false;
  let paused = false;
  let hiddenSince: number | null = null;
  let timer: unknown = null;

  // A slow poll of isVisible(), for browsers that never dispatch
  // "visibilitychange" (the page would stay paused while shown).
  function schedulePoll(): void {
    deps.setTimer(() => {
      controller.visibilityChanged();
      schedulePoll();
    }, VISIBILITY_POLL_MS);
  }

  function cancelTimer(): void {
    if (timer !== null) deps.clearTimer(timer);
    timer = null;
  }

  function reconcile(): void {
    cancelTimer();
    const hiddenForMs = hiddenSince === null ? 0 : deps.now() - hiddenSince;
    const action = connectionAction({
      visible: deps.isVisible(),
      connected,
      hiddenForMs,
      graceMs: GRACE_MS,
    });
    if (action === "connect") {
      connected = true;
      if (paused) deps.onState("resuming");
      paused = false;
      deps.open();
    } else if (action === "disconnect") {
      connected = false;
      paused = true;
      deps.close();
      deps.onState("paused");
    } else if (action === "wait") {
      timer = deps.setTimer(reconcile, GRACE_MS - hiddenForMs);
    }
  }

  const controller: ConnectionController = {
    start() {
      schedulePoll();
      if (deps.isVisible()) {
        hiddenSince = null;
      } else {
        // Loaded in a background tab: nothing to poll for until it is shown.
        hiddenSince = deps.now();
        paused = true;
        deps.onState("paused");
      }
      reconcile();
    },
    visibilityChanged() {
      if (deps.isVisible()) hiddenSince = null;
      else if (hiddenSince === null) hiddenSince = deps.now();
      reconcile();
    },
    connectionLost() {
      connected = false;
      reconcile();
    },
  };
  return controller;
}
