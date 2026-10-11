import { formatAge } from "../logic/format";
import { useApp, useAppStore } from "../store/StoreContext";
import { ExternalLink } from "./presentation";
import { useNow } from "./useNow";

/** The key the callout's collapsed state is remembered under. */
export const UNTRACKED_KEY = "untracked";

/**
 * "Open PRs not tracked by toolbelt": the user's open PRs that are not
 * branches in the stack DB, as links only (no statuses; never in the table or
 * the cards). Collapsible, and remembered.
 */
export function UntrackedCallout() {
  const store = useAppStore();
  const untracked = useApp((s) => s.untracked);
  const collapsed = useApp((s) => s.collapsed.includes(UNTRACKED_KEY));
  const now = useNow();
  const count = untracked.state === "loaded" ? String(untracked.prs.length) : "…";

  return (
    <section className={`callout${collapsed ? " collapsed" : ""}`} aria-label="Open PRs not tracked by toolbelt">
      <h2 className="callout-head">
        <button
          type="button"
          aria-expanded={!collapsed}
          onClick={() => store.getState().toggleSection(UNTRACKED_KEY)}
        >
          <span className="cg-caret" aria-hidden="true" />
          <span className="cg-label">Open PRs not tracked by toolbelt</span>
          <span className="cg-count">{count}</span>
          <span className="cg-meaning">links only: branches toolbelt does not know about</span>
        </button>
      </h2>
      {!collapsed && (
        <div className="callout-body">
          {untracked.state === "loading" && <span className="muted">loading…</span>}
          {untracked.state === "failed" && (
            <span className="error">{`could not list your open PRs: ${untracked.error}`}</span>
          )}
          {untracked.state === "loaded" && untracked.prs.length === 0 && untracked.hidden === 0 && (
            <span className="muted">none: every open PR of yours is a tracked branch</span>
          )}
          {untracked.state === "loaded" && untracked.prs.length > 0 && (
            <ul className="callout-list">
              {untracked.prs.map((pr) => (
                <li key={pr.url}>
                  <span className="callout-repo">{pr.repo}</span>
                  <ExternalLink href={pr.url}>{`#${pr.number} ${pr.title}`}</ExternalLink>
                  {pr.is_draft && <span className="pill tone-gray">draft</span>}
                  <span className="muted">{formatAge((now - Date.parse(pr.created_at)) / 1000)}</span>
                </li>
              ))}
            </ul>
          )}
          {untracked.state === "loaded" && untracked.hidden > 0 && (
            <span className="muted">
              {`${untracked.hidden} open PR${untracked.hidden === 1 ? "" : "s"} could not be read: GitHub denied access (a SAML-protected organization your token is not authorized for?)`}
            </span>
          )}
          {untracked.state === "loaded" && untracked.truncated && (
            <span className="muted">only the first 100 of your open PRs were searched</span>
          )}
        </div>
      )}
    </section>
  );
}
