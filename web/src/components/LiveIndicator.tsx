import { statusLabel } from "../live/connection";
import { useApp } from "../store/StoreContext";

/** Whether the page is connected to the server's live stream. How fresh the
 * data is is said on each card, and on the views that have none. */
export function LiveIndicator() {
  const state = useApp((s) => s.connection);
  return (
    <span id="live" role="status" data-state={state}>
      {statusLabel(state)}
    </span>
  );
}
