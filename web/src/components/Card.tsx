import { memo, useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { Catalog, LiveStatus, LoadedStatus, SlotDimensions } from "../api/generated";
import type { RailSegment } from "../logic/layout";
import { LOADING } from "../store/selectors";
import { useApp } from "../store/StoreContext";
import { Timeline } from "./Timeline";
import { Age, CI_TONE, ExternalLink, PR_TONE, Pill, REVIEW_TONE, TaskValue, WHO_TEXT } from "./presentation";
import { useNow } from "./useNow";

/** Every labelled slot of a card; the catalog says which dimension each shows. */
export type SlotKey = keyof SlotDimensions;

/**
 * How to read a slot's value: `ok` (data), `na` (not applicable: muted) and
 * `gap` (missing but could be set: a little more visible).
 */
type SlotState = "ok" | "na" | "gap";

function Slot({
  id,
  label,
  state = "ok",
  children,
}: {
  id: SlotKey;
  label: string;
  state?: SlotState;
  children: ReactNode;
}) {
  return (
    <div className={`slot s-${id} v-${state}`} data-slot={id}>
      <span className="slot-label">{label}</span>
      <span className="slot-value">{children}</span>
    </div>
  );
}

/** True for a moment after `value` changes (not on first render). */
function useFlashOnChange(value: unknown): boolean {
  const last = useRef(value);
  const [flash, setFlash] = useState(false);
  useEffect(() => {
    if (last.current === value) return undefined;
    last.current = value;
    setFlash(true);
    const timer = window.setTimeout(() => setFlash(false), 1500);
    return () => window.clearTimeout(timer);
  }, [value]);
  return flash;
}

export interface CardProps {
  id: string;
  /** Stack depth to indent by (stacked view only). */
  depth: number;
  rails: RailSegment[];
  /** The card just moved to this section. */
  moved: boolean;
  /** Scrolled to by a stack link: highlighted for a moment. */
  highlighted: boolean;
  /** Where the parent card is when not in this section ("Needs me"). */
  parentIn: string | null;
  /** The parent is not shown at all (filtered out, or collapsed away). */
  parentGone: boolean;
  onJump(target: string, from: string): void;
}

function Rails({ rails }: { rails: RailSegment[] }) {
  if (rails.length === 0) return null;
  return (
    <div className="card-rail" aria-hidden="true">
      {rails.map((segment) => (
        <i
          key={segment.col}
          className="rail-seg"
          data-kind={segment.kind}
          style={{ "--col": segment.col } as CSSProperties}
        />
      ))}
    </div>
  );
}

const sameRails = (a: RailSegment[], b: RailSegment[]): boolean =>
  a.length === b.length && a.every((s, i) => s.col === b[i].col && s.kind === b[i].kind);

export const Card = memo(
  function Card(props: CardProps) {
    const { id, depth, rails, moved, highlighted, parentIn, parentGone, onJump } = props;
    const branch = useApp((s) => s.branchesById[id]);
    const repo = useApp((s) => (branch ? s.reposById[branch.repo_id] : undefined));
    const status = useApp((s) => s.statuses[id]) ?? LOADING;
    const task = useApp((s) => s.tasks[id]) ?? null;
    const catalog = useApp((s) => s.catalog);
    const flashed = useFlashOnChange(status);
    const now = useNow();
    if (!branch || !repo || !catalog) return null;

    const classes = ["card"];
    if (flashed || moved) classes.push("flash");
    if (highlighted) classes.push("highlight");
    const childNames = branch.children.map((child) => child.name);

    return (
      <article
        className={classes.join(" ")}
        data-id={id}
        style={{ "--depth": depth } as CSSProperties}
      >
        <Rails rails={rails} />
        <Slot id="branch" label="Repo / branch">
          <span className="repo-chip" title={repo.path}>
            {repo.name}
          </span>
          <span className={`branch${branch.current ? " current" : ""}`}>{branch.name}</span>
        </Slot>
        <StatusSlots status={status} pathMissing={repo.path_missing} catalog={catalog} id={id} now={now} />
        <Slot id="task" label="Task" state={branch.task ? "ok" : "gap"}>
          {branch.task ? <TaskValue link={branch.task} task={task} /> : "no task linked"}
        </Slot>
        <Slot id="session" label="Session" state={branch.agent ? "ok" : "gap"}>
          {branch.agent ? (
            branch.agent.href ? (
              <a href={branch.agent.href}>{branch.agent.label}</a>
            ) : (
              branch.agent.label
            )
          ) : (
            "no session linked"
          )}
        </Slot>
        <Slot id="stack" label="Stack">
          {branch.parent && branch.parent_id ? (
            <a
              className="branch stack-link"
              href={`#${branch.parent_id}`}
              title={`stacked on ${branch.parent}`}
              data-target={branch.parent_id}
              data-in={parentIn ?? undefined}
              data-gone={parentGone ? "1" : undefined}
              onClick={(event) => {
                event.preventDefault();
                onJump(branch.parent_id as string, id);
              }}
            >
              {branch.parent}
            </a>
          ) : (
            <span className="stack-chip stack-chip-muted">stack root</span>
          )}{" "}
          <span className="stack-chip">
            {branch.stack_height > 1
              ? `stack ${branch.stack_pos} of ${branch.stack_height}`
              : "single branch"}
          </span>{" "}
          <span
            className={`stack-chip${childNames.length ? "" : " stack-chip-muted"}`}
            title={childNames.length ? `stacked on it: ${childNames.join(", ")}` : "nothing stacked on it"}
          >
            {childNames.length === 0
              ? "no children"
              : `${childNames.length} ${childNames.length === 1 ? "child" : "children"}`}
          </span>
        </Slot>
      </article>
    );
  },
  (a, b) =>
    a.id === b.id &&
    a.depth === b.depth &&
    a.moved === b.moved &&
    a.highlighted === b.highlighted &&
    a.parentIn === b.parentIn &&
    a.parentGone === b.parentGone &&
    a.onJump === b.onJump &&
    sameRails(a.rails, b.rails),
);

const LOOKUP_FAILED = "gh could not look up this branch's PR";

/** The live half of a card: everything that comes from the PR's status. */
function StatusSlots({
  status,
  pathMissing,
  catalog,
  id,
  now,
}: {
  status: LiveStatus;
  pathMissing: boolean;
  catalog: Catalog;
  id: string;
  now: number;
}) {
  if (status.load !== "loaded") {
    // Every slot says why it is empty.
    const note = pathMissing ? (
      "repo path missing"
    ) : status.load === "failed" ? (
      <span className="error" title={LOOKUP_FAILED}>
        status unavailable
      </span>
    ) : (
      "loading…"
    );
    const unloaded: [SlotKey, string][] = [
      ["next", "Next"],
      ["pr", "PR"],
      ["ci", "CI"],
      ["review", "Review"],
      ["comments", "Comments"],
      ["merge", "Merge"],
      ["pr_age", "PR age"],
      ["in_draft", "In draft"],
      ["review_requested", "Review requested"],
      ["ci_failures", "CI failures"],
      ["reviewers", "Reviewers"],
      ["title", "PR title"],
      ["timeline", "Timeline"],
    ];
    return (
      <>
        {unloaded.map(([key, label]) => (
          <Slot key={key} id={key} label={label} state="na">
            {note}
          </Slot>
        ))}
      </>
    );
  }
  return <LoadedSlots status={status} catalog={catalog} id={id} now={now} />;
}

function LoadedSlots({
  status,
  catalog,
  id,
  now,
}: {
  status: LoadedStatus;
  catalog: Catalog;
  id: string;
  now: number;
}) {
  const { detail } = status;
  const hasPr = status.pr !== "none";
  return (
    <>
      <Slot id="next" label="Next">
        <span className={`card-next next-${status.next.who}`} title={status.next.kind}>
          {status.next.label}
        </span>{" "}
        <span className="card-next-who muted">{WHO_TEXT[status.next.who]}</span>
      </Slot>

      {hasPr ? (
        <>
          <Slot id="pr" label="PR">
            {status.pr_url ? (
              <ExternalLink className={`pill tone-${PR_TONE[status.pr]}`} href={status.pr_url}>
                {status.pr_number !== null ? `#${status.pr_number} ` : ""}
                {status.pr}
              </ExternalLink>
            ) : (
              <Pill tone={PR_TONE[status.pr]}>{status.pr}</Pill>
            )}
          </Slot>
          {status.ci !== "none" ? (
            <Slot id="ci" label="CI">
              <Pill tone={CI_TONE[status.ci]}>{status.ci}</Pill>
            </Slot>
          ) : (
            <Slot id="ci" label="CI" state="na">
              no CI configured
            </Slot>
          )}
          {status.review !== "none" ? (
            <Slot id="review" label="Review">
              <Pill tone={REVIEW_TONE[status.review]}>{status.review}</Pill>
            </Slot>
          ) : (
            <Slot id="review" label="Review" state="gap">
              no review requested
            </Slot>
          )}
          {status.open_comments > 0 ? (
            <Slot id="comments" label="Comments">
              <Pill tone="amber" title="unresolved review threads">
                {`${status.open_comments} comment${status.open_comments === 1 ? "" : "s"}`}
              </Pill>
            </Slot>
          ) : (
            <Slot id="comments" label="Comments" state="na">
              0 comments
            </Slot>
          )}
          {status.merge === "conflicts" ? (
            <Slot id="merge" label="Merge">
              <Pill tone="red">conflicts</Pill>
            </Slot>
          ) : status.merge === "clean" ? (
            <Slot id="merge" label="Merge">
              clean
            </Slot>
          ) : (
            <Slot id="merge" label="Merge" state="na">
              {`n/a (PR ${status.pr})`}
            </Slot>
          )}

          {detail ? (
            <>
              <Slot id="pr_age" label="PR age">
                <Age {...detail.pr_age} />
              </Slot>
              {detail.draft_age ? (
                <Slot id="in_draft" label="In draft">
                  <Age {...detail.draft_age} />
                </Slot>
              ) : (
                <Slot id="in_draft" label="In draft" state="na">
                  not a draft
                </Slot>
              )}
              {detail.review_request_age ? (
                <Slot id="review_requested" label="Review requested">
                  <Age {...detail.review_request_age} />
                </Slot>
              ) : (
                <Slot id="review_requested" label="Review requested" state="na">
                  not requested
                </Slot>
              )}
              <Slot id="ci_failures" label="CI failures" state={detail.ci_failures ? "ok" : "na"}>
                <span className={detail.ci_failures ? "ci-failed" : undefined}>
                  {`${detail.ci_failures} failure${detail.ci_failures === 1 ? "" : "s"}`}
                </span>
              </Slot>
              {detail.reviewers.length > 0 ? (
                <Slot id="reviewers" label="Reviewers">
                  {detail.reviewers.join(", ")}
                </Slot>
              ) : (
                <Slot id="reviewers" label="Reviewers" state="na">
                  none requested
                </Slot>
              )}
              <Slot id="title" label="PR title">
                <span title={detail.title}>{detail.title}</span>
              </Slot>
            </>
          ) : (
            <UnavailableSlots note="PR details unavailable" />
          )}
        </>
      ) : (
        <>
          <Slot id="pr" label="PR" state="gap">
            no PR
          </Slot>
          <Slot id="ci" label="CI" state="na">
            no PR
          </Slot>
          <Slot id="review" label="Review" state="na">
            no PR
          </Slot>
          <Slot id="comments" label="Comments" state="na">
            no PR
          </Slot>
          <Slot id="merge" label="Merge" state="na">
            no PR
          </Slot>
          <UnavailableSlots note="no PR" />
        </>
      )}
      <Slot id="timeline" label="Timeline">
        <Timeline id={id} status={status} catalog={catalog} now={now} />
      </Slot>
    </>
  );
}

/** The PR-detail slots of a card that has no PR (or no detail), each saying why. */
function UnavailableSlots({ note }: { note: string }) {
  const keys: [SlotKey, string][] = [
    ["pr_age", "PR age"],
    ["in_draft", "In draft"],
    ["review_requested", "Review requested"],
    ["ci_failures", "CI failures"],
    ["reviewers", "Reviewers"],
    ["title", "PR title"],
  ];
  return (
    <>
      {keys.map(([key, label]) => (
        <Slot key={key} id={key} label={label} state="na">
          {note}
        </Slot>
      ))}
    </>
  );
}

/** A slim stand-in for a branch that is not a card of its own but gives the
 *  stack below it its context (a finished or filtered-out parent). */
export const Stub = memo(function Stub({
  id,
  depth,
  rails,
  highlighted,
}: {
  id: string;
  depth: number;
  rails: RailSegment[];
  highlighted: boolean;
}) {
  const branch = useApp((s) => s.branchesById[id]);
  const repo = useApp((s) => (branch ? s.reposById[branch.repo_id] : undefined));
  const status = useApp((s) => s.statuses[id]) ?? LOADING;
  if (!branch || !repo) return null;
  const text =
    status.load === "loaded" && status.pr !== "none"
      ? `${status.pr_number !== null ? `#${status.pr_number} ` : ""}${status.pr}`
      : "no PR";
  return (
    <div className={`card-stub${highlighted ? " highlight" : ""}`} data-id={id} style={{ "--depth": depth } as CSSProperties}>
      <Rails rails={rails} />
      <span>↳</span>
      <span className="repo-chip">{repo.name}</span>
      <span className="branch">{branch.name}</span>
      <span>{`${text} · kept as context for the stack below`}</span>
    </div>
  );
});
