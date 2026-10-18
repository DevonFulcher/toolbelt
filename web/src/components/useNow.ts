import { createContext, useContext, useSyncExternalStore } from "react";

/**
 * The current time (ms), re-provided by the app shell every 30 seconds, so
 * ages and timeline tracks stay right between server updates (the server's
 * texts are only as fresh as the last status).
 */
export const NowContext = createContext<number>(Date.now());

export function useNow(): number {
  return useContext(NowContext);
}

/** The app's clock (``Date.now``, or a fixed one in tests). */
export const ClockContext = createContext<() => number>(Date.now);

// One timer, shared by everything that counts seconds (a card has several).
const listeners = new Set<() => void>();
let ticks = 0;
let timer: number | undefined;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === undefined) {
    timer = window.setInterval(() => {
      ticks += 1;
      listeners.forEach((notify) => notify());
    }, 1000);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== undefined) {
      window.clearInterval(timer);
      timer = undefined;
    }
  };
}

/** The current time (ms), re-read every second: for counters that show seconds. */
export function useSecondNow(): number {
  const clock = useContext(ClockContext);
  useSyncExternalStore(subscribe, () => ticks);
  return clock();
}
