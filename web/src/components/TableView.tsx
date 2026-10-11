import { memo, useMemo, type CSSProperties } from "react";
import type { LiveStatus, LoadedStatus } from "../api/generated";
import { layout } from "../logic/layout";
import type { Row } from "../logic/rows";
import { COLUMNS, toggleSort, type Column } from "../logic/viewState";
import { LOADING } from "../store/selectors";
import { useApp, useAppStore } from "../store/StoreContext";
import { CI_TONE, ExternalLink, PR_TONE, Pill, REVIEW_TONE, TaskValue } from "./presentation";

const HEADINGS: Record<Column, string> = {
  repo: "Repo",
  branch: "Branch",
  next: "Next",
  pr: "PR",
  ci: "CI",
  review: "Review",
  comments: "Comments",
  merge: "Merge",
  task: "Task",
  session: "Session",
};

const LOOKUP_FAILED = "gh could not look up this branch's PR";

export function TableView({ rows }: { rows: Row[] }) {
  const store = useAppStore();
  const view = useApp((s) => s.view);
  const entries = useMemo(() => layout(rows, { ...view, view: "table" }), [rows, view]);
  const compact = view.compact === "1";

  const sortable = (column: Column) => {
    const sorted = column === view.sort;
    return {
      "aria-sort": sorted ? (view.dir === "desc" ? "descending" : "ascending") : "none",
    } as const;
  };
  const hiddenWhenCompact: Column[] = ["pr", "ci", "review", "comments", "merge"];

  return (
    <div className="table-wrap" id="table-wrap">
      <table className={`stacks${compact ? " compact" : ""}`} id="stacks">
        <thead>
          <tr>
            {COLUMNS.flatMap((column) => {
              const cells = [];
              if (column === "task" && compact) {
                cells.push(
                  <th key="status" data-col="status" aria-sort="none">
                    Status
                  </th>,
                );
              }
              if (!(compact && hiddenWhenCompact.includes(column))) {
                cells.push(
                  <th key={column} data-col={column} {...sortable(column)}>
                    <button
                      type="button"
                      onClick={() => store.getState().setView(toggleSort(view, column))}
                    >
                      {HEADINGS[column]}
                      <span className="arrow" aria-hidden="true" />
                    </button>
                  </th>,
                );
              }
              return cells;
            })}
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <TableRow key={entry.id} id={entry.id} depth={entry.depth} context={entry.context} compact={compact} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

const Muted = ({ children }: { children?: string }) => <span className="muted">{children}</span>;

const TableRow = memo(function TableRow({
  id,
  depth,
  context,
  compact,
}: {
  id: string;
  depth: number;
  context: boolean;
  compact: boolean;
}) {
  const branch = useApp((s) => s.branchesById[id]);
  const repo = useApp((s) => (branch ? s.reposById[branch.repo_id] : undefined));
  const status = useApp((s) => s.statuses[id]) ?? LOADING;
  const task = useApp((s) => s.tasks[id]) ?? null;
  if (!branch || !repo) return null;
  const pathMissing = repo.path_missing;
  const failed = status.load === "failed";
  const loaded = status.load === "loaded" ? status : null;
  // While loading nothing is said for a failed lookup or a missing checkout.
  const placeholder = <Muted>{pathMissing || failed ? "" : "…"}</Muted>;

  return (
    <tr
      className={context ? "context" : undefined}
      data-id={id}
      style={{ "--depth": depth } as CSSProperties}
    >
      <td className="col-repo" title={repo.path}>
        {repo.name}
      </td>
      <td className="col-branch">
        <span className={`branch${branch.current ? " current" : ""}`}>{branch.name}</span>
      </td>
      <td className="col-next">
        {loaded ? (
          <span className={`pill next-${loaded.next.who}`} title={loaded.next.kind}>
            {loaded.next.label}
          </span>
        ) : (
          placeholder
        )}
      </td>
      {!compact && (
        <>
          <td className="col-pr">
            <PrCell status={status} pathMissing={pathMissing} />
          </td>
          <td className="col-ci">
            {loaded ? loaded.ci !== "none" ? <Pill tone={CI_TONE[loaded.ci]}>{loaded.ci}</Pill> : <Muted>—</Muted> : placeholder}
          </td>
          <td className="col-review">
            {loaded ? (
              loaded.review !== "none" ? (
                <Pill tone={REVIEW_TONE[loaded.review]}>{loaded.review}</Pill>
              ) : (
                <Muted>—</Muted>
              )
            ) : (
              placeholder
            )}
          </td>
          <td className="col-comments">
            {loaded ? (
              loaded.open_comments > 0 ? (
                <Pill
                  tone="amber"
                  title={`${loaded.open_comments} unresolved review thread${loaded.open_comments === 1 ? "" : "s"}`}
                >
                  {String(loaded.open_comments)}
                </Pill>
              ) : (
                <Muted>—</Muted>
              )
            ) : (
              placeholder
            )}
          </td>
          <td className="col-merge">
            {loaded ? (
              loaded.merge === "conflicts" ? (
                <Pill tone="red">conflicts</Pill>
              ) : loaded.merge === "clean" ? (
                <Muted>clean</Muted>
              ) : (
                <Muted>—</Muted>
              )
            ) : (
              placeholder
            )}
          </td>
        </>
      )}
      {compact && (
        <td className="col-status">
          <CompactStatus status={status} pathMissing={pathMissing} />
        </td>
      )}
      <td className="col-task">
        {branch.task ? <TaskValue link={branch.task} task={task} /> : <Muted>—</Muted>}
      </td>
      <td className="col-session">
        {branch.agent ? (
          branch.agent.href ? (
            <a href={branch.agent.href}>{branch.agent.label}</a>
          ) : (
            branch.agent.label
          )
        ) : (
          <Muted>—</Muted>
        )}
      </td>
    </tr>
  );
});

function PrLink({ status }: { status: LoadedStatus }) {
  const text = `${status.pr_number !== null ? `#${status.pr_number} ` : ""}${status.pr}`;
  const tone = PR_TONE[status.pr];
  return status.pr_url ? (
    <ExternalLink className={`pill tone-${tone}`} href={status.pr_url}>
      {text}
    </ExternalLink>
  ) : (
    <Pill tone={tone}>{text}</Pill>
  );
}

/** What stands in for a PR that has no loaded status (or no PR). */
function PrPlaceholder({ status, pathMissing }: { status: LiveStatus; pathMissing: boolean }) {
  if (pathMissing) return <Muted>repo path missing</Muted>;
  if (status.load === "failed") {
    return (
      <span className="error" title={LOOKUP_FAILED}>
        status unavailable
      </span>
    );
  }
  if (status.load === "loading") return <Muted>loading…</Muted>;
  return <Muted>no PR</Muted>;
}

function PrCell({ status, pathMissing }: { status: LiveStatus; pathMissing: boolean }) {
  if (pathMissing || status.load !== "loaded" || status.pr === "none") {
    return <PrPlaceholder status={status} pathMissing={pathMissing} />;
  }
  return <PrLink status={status} />;
}

/** The compact table's combined cell: PR, CI, review, comments and merge in one. */
function CompactStatus({ status, pathMissing }: { status: LiveStatus; pathMissing: boolean }) {
  if (pathMissing || status.load !== "loaded" || status.pr === "none") {
    return <PrPlaceholder status={status} pathMissing={pathMissing} />;
  }
  return (
    <>
      <PrLink status={status} />
      {status.ci !== "none" && <Pill tone={CI_TONE[status.ci]}>{`CI ${status.ci}`}</Pill>}
      {status.review !== "none" && <Pill tone={REVIEW_TONE[status.review]}>{status.review}</Pill>}
      {status.open_comments > 0 && (
        <Pill tone="amber" title="unresolved review threads">
          {`${status.open_comments} comment${status.open_comments === 1 ? "" : "s"}`}
        </Pill>
      )}
      {status.merge === "conflicts" && <Pill tone="red">conflicts</Pill>}
    </>
  );
}
