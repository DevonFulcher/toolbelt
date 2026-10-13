// A branch as the sort / filter / layout logic sees it: a row of cells, each
// with the value a column's filter matches exactly (`value`) and the key the
// column sorts by (`sort`; "" where there is nothing to order by, which
// always sorts last). Both are "" while the status is still loading.
import type {
  Branch,
  CiState,
  LiveStatus,
  MergeValue,
  Repo,
  ReviewState,
  TaskLive,
  Who,
} from "../api/generated";
import type { Column } from "./viewState";

export interface Cell {
  value: string;
  sort: string;
}

export interface Row {
  /** Unique across repos (the same branch name can be in several). */
  id: string;
  repo: string;
  repoName: string;
  branch: string;
  /** Row id of the parent branch; null for the first branch of a stack. */
  parent: string | null;
  /** Position in the server's depth-first order: the default sort. */
  order: number;
  /** Everything the text filter searches, space-joined. */
  search: string;
  cells: Partial<Record<Column, Cell>>;
}

// Orders the states from most to least urgent ("" sorts last). Typed as
// records over the generated unions: a new state is a compile error here
// until it is ranked.
const CI_RANK: Record<CiState, string> = { failed: "1", running: "2", success: "3", none: "" };
const REVIEW_RANK: Record<ReviewState, string> = {
  requested: "1",
  reviewed: "2",
  approved: "3",
  none: "",
};
const MERGE_RANK: Record<MergeValue, string> = { conflicts: "1", clean: "2", none: "" };
/** "Needs me" first. */
export const WHO_RANK: Record<Who, string> = { you: "1", agent: "2", ci: "3", others: "4", none: "5" };

export const COMMENTS_OPEN = "has comments";
export const COMMENTS_NONE = "none";

const EMPTY: Cell = { value: "", sort: "" };

/** The cells of a branch's live status (all empty until it is loaded). */
function statusCells(status: LiveStatus): Partial<Record<Column, Cell>> {
  if (status.load !== "loaded") {
    return { next: EMPTY, pr: EMPTY, ci: EMPTY, review: EMPTY, comments: EMPTY, merge: EMPTY };
  }
  const hasPr = status.pr !== "none";
  return {
    next: { value: status.next.who, sort: WHO_RANK[status.next.who] },
    pr: { value: status.pr, sort: hasPr && status.pr_number !== null ? String(status.pr_number) : "" },
    ci: { value: status.ci, sort: CI_RANK[status.ci] },
    review: { value: status.review, sort: REVIEW_RANK[status.review] },
    comments: {
      value: status.open_comments > 0 ? COMMENTS_OPEN : COMMENTS_NONE,
      sort: hasPr ? String(status.open_comments) : "",
    },
    merge: { value: status.merge, sort: MERGE_RANK[status.merge] },
  };
}

export function rowOf(
  branch: Pick<Branch, "id" | "repo_id" | "name" | "parent_id" | "order" | "task" | "agent">,
  repo: Pick<Repo, "name"> | undefined,
  status: LiveStatus,
  task: TaskLive | null,
): Row {
  const repoName = repo?.name ?? String(branch.repo_id);
  const taskLabel = branch.task?.label ?? "";
  const search = [
    repoName,
    branch.name,
    status.load === "loaded" ? status.next.label : "",
    status.load === "loaded" && status.pr_number !== null ? String(status.pr_number) : "",
    taskLabel,
    task?.state === "info" ? `${task.title} ${task.status}` : "",
  ]
    .filter(Boolean)
    .join(" ");
  return {
    id: branch.id,
    repo: String(branch.repo_id),
    repoName,
    branch: branch.name,
    parent: branch.parent_id,
    order: branch.order,
    search,
    cells: {
      repo: { value: String(branch.repo_id), sort: repoName },
      branch: { value: branch.name, sort: branch.name },
      ...statusCells(status),
      task: { value: taskLabel, sort: taskLabel },
      session: {
        value: branch.agent ? "linked" : "",
        sort: branch.agent?.label ?? "",
      },
    },
  };
}
