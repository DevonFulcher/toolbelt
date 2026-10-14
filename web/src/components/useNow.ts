import { createContext, useContext } from "react";

/**
 * The current time (ms), re-provided by the app shell every 30 seconds, so
 * ages and timeline tracks stay right between server updates (the server's
 * texts are only as fresh as the last status).
 */
export const NowContext = createContext<number>(Date.now());

export function useNow(): number {
  return useContext(NowContext);
}
