import { useEffect, useState } from "react";
import { statusLabel } from "../live/connection";
import { useApp } from "../store/StoreContext";

/** The connection state and how long ago the data was refreshed. */
export function LiveIndicator() {
  const state = useApp((s) => s.connection);
  const updatedAt = useApp((s) => s.updatedAt);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const ago = updatedAt === null ? null : (now - updatedAt) / 1000;
  return (
    <span id="live" role="status" data-state={state}>
      {statusLabel(state, ago)}
    </span>
  );
}
