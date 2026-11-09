// Merged PRs whose branch was removed (cleaned up after the merge) but which
// received review comments: a reduced card each (no stack position, no worktree
// actions), kept until the user acknowledges. See ``landed_prs.py``.
import { acknowledgeLandedPr } from "../api/client";
import { useApp } from "../store/StoreContext";
import { AcknowledgeButton, AcknowledgeComments } from "./AcknowledgeControls";
import { ExternalLink } from "./presentation";

export function LandedPrCards() {
  const landed = useApp((s) => s.landedPrs);
  if (landed.length === 0) return null;
  return (
    <section className="landed-prs" aria-label="Merged PRs with comments to acknowledge">
      {landed.map((pr) => (
        <article className="landed-pr" key={pr.id}>
          <h3 className="landed-pr-title">
            <span className="muted">{pr.repo}</span> <ExternalLink href={pr.pr_url}>{`#${pr.pr_number}`}</ExternalLink>{" "}
            {pr.title}
          </h3>
          <div className="landed-pr-meta muted">
            {`merged, branch ${pr.branch} removed`}
            {pr.task ? ` · task ${pr.task}` : ""}
          </div>
          <div className="landed-pr-act">
            <AcknowledgeButton request={() => acknowledgeLandedPr(`${pr.repo_id}:${pr.branch}`, pr.pr_number)} />
          </div>
          <AcknowledgeComments comments={pr.comments} />
        </article>
      ))}
    </section>
  );
}
