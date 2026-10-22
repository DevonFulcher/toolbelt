import { useApp } from "../store/StoreContext";

/** Shown when the server has been updated since this page was loaded: the page
 * keeps running the old code until it is reloaded. */
export function UpdateNotice() {
  const outOfDate = useApp((s) => s.outOfDate);
  if (!outOfDate) return null;
  return (
    <span id="update-notice" role="status" className="notice">
      tt ui was updated.{" "}
      <button type="button" className="agents-toggle" onClick={() => window.location.reload()}>
        Reload
      </button>
    </span>
  );
}
