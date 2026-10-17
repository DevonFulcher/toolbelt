// Keeps the store current: loads the snapshot, listens to the server's
// /api/events stream (Server-Sent Events) and writes each typed event into
// the store, outside React.
//
//   hello      on (re)connect; if its version is not the last one seen,
//              events were missed, so the snapshot is re-fetched
//   status     a branch's new live status
//   task       a branch's task title / status
//   claims     who holds a claim on some branches changed (with their
//              re-derived status: "With an agent" decides the next action)
//   structure  the stack DB changed: re-fetch the snapshot
//   refreshed  a polling pass finished ("updated Ns ago")
//   untracked  the open PRs toolbelt does not track changed
//
// The stream is only open while the page is being looked at (see
// connection.ts). Events are handled one at a time, so a status that arrives
// while the snapshot is being re-fetched is applied after it, not lost.
import type { ServerEvent, Snapshot } from "../api/generated";
import type { AppStore } from "../store/appStore";
import {
  createConnectionController,
  missedEvents,
  RECONNECT_DELAY_MS,
  type ConnectionController,
} from "./connection";
import { assertNever } from "../logic/exhaustive";

/** The part of `EventSource` this uses. */
export interface EventSourceLike {
  readonly readyState: number;
  onerror: ((event: unknown) => void) | null;
  addEventListener(type: string, listener: (event: { data: string }) => void): void;
  close(): void;
}

const EVENT_TYPES = ["hello", "status", "task", "claims", "structure", "refreshed", "untracked"] as const;
const CLOSED = 2; // EventSource.CLOSED

export interface LiveDeps {
  store: AppStore;
  fetchSnapshot(): Promise<Snapshot>;
  openStream(): EventSourceLike;
  isVisible(): boolean;
  now(): number;
  setTimer(fn: () => void, ms: number): unknown;
  clearTimer(handle: unknown): void;
}

export interface LiveUpdates {
  controller: ConnectionController;
  /** Re-reads the page's visibility (focus, pageshow, visibilitychange). */
  visibilityChanged(): void;
  stop(): void;
}

export function startLiveUpdates(deps: LiveDeps): LiveUpdates {
  const { store } = deps;
  let chain: Promise<void> = Promise.resolve();
  const enqueue = (task: () => Promise<void> | void): void => {
    chain = chain.then(task).catch((error: unknown) => {
      console.error("live update failed", error);
    });
  };

  async function loadSnapshot(): Promise<void> {
    try {
      store.getState().applySnapshot(await deps.fetchSnapshot());
    } catch (error) {
      store.getState().setSnapshotError(String(error));
    }
  }

  function handle(event: ServerEvent): Promise<void> | void {
    const state = store.getState();
    switch (event.type) {
      case "hello": {
        const missed = missedEvents(state.version, event.v);
        state.hello(event.v, event.age);
        return missed ? loadSnapshot() : undefined;
      }
      case "status":
        state.statusChanged(event.branch_id, event.status, event.v);
        return undefined;
      case "task":
        state.taskChanged(event.branch_id, event.task, event.v);
        return undefined;
      case "claims":
        state.claimsChanged(event.branches, event.v);
        return undefined;
      case "refreshed":
        state.refreshed(event.v, event.fetched);
        return undefined;
      case "untracked":
        state.untrackedChanged(event.untracked, event.v);
        return undefined;
      case "structure":
        return loadSnapshot();
      default:
        return assertNever(event, "event type");
    }
  }

  let source: EventSourceLike | null = null;
  let reconnectTimer: unknown = null;

  function open(): void {
    const stream = deps.openStream();
    source = stream;
    for (const type of EVENT_TYPES) {
      stream.addEventListener(type, (message) => {
        const event = JSON.parse(message.data) as ServerEvent;
        enqueue(() => handle(event));
      });
    }
    stream.onerror = () => {
      if (source !== stream) return;
      if (stream.readyState === CLOSED) {
        store.getState().setConnection("disconnected");
        source = null;
        if (reconnectTimer !== null) deps.clearTimer(reconnectTimer);
        reconnectTimer = deps.setTimer(() => controller.connectionLost(), RECONNECT_DELAY_MS);
      } else {
        store.getState().setConnection("reconnecting");
      }
    };
  }

  function close(): void {
    if (reconnectTimer !== null) deps.clearTimer(reconnectTimer);
    reconnectTimer = null;
    if (source) source.close();
    source = null;
  }

  const controller = createConnectionController({
    isVisible: deps.isVisible,
    now: deps.now,
    open,
    close,
    setTimer: deps.setTimer,
    clearTimer: deps.clearTimer,
    onState: (state) => store.getState().setConnection(state),
  });

  enqueue(loadSnapshot);
  controller.start();
  return {
    controller,
    visibilityChanged: () => controller.visibilityChanged(),
    stop: close,
  };
}
