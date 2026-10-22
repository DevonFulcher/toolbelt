import { memo, useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type {
  BranchClaim,
  Catalog,
  HolderState,
  LiveStatus,
  LoadedStatus,
  RepoWebhook,
  SlackInfo,
  SlotDimensions,
  Tone,
} from "../api/generated";
import { sessionLink } from "../logic/claimSession";
import type { RailSegment } from "../logic/layout";
import { stackViewRoot } from "../logic/focus";
import { serializeState } from "../logic/viewState";
import { rollup, youText } from "../logic/rollup";
import { LOADING, subtreeIds } from "../store/selectors";
import { useApp, useAppStore } from "../store/StoreContext";
import { AgentSlots, SpendSlots } from "./CardAgentSlots";
import { MergeDialog } from "./MergeDialog";
import { SlackDialog } from "./SlackDialog";
import { Timeline } from "./Timeline";
import { Age, CI_TONE, ExternalLink, PR_TONE, Pill, REVIEW_TONE, WHO_TEXT, WHO_TONE } from "./presentation";
import { useNow, useSecondNow } from "./useNow";
import { formatElapsed } from "../logic/format";

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

/** A titled, bordered box of related slots (Git, PR, Task). ``aside`` sits at
 * the right of the title (how fresh the box's data is). */
function Group({ id, title, aside, children }: { id: string; title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <div className={`group g-${id}`} role="group" aria-label={title}>
      <div className="group-title">
        <span>{title}</span>
        {aside}
      </div>
      <div className="group-slots">{children}</div>
    </div>
  );
}

/** A repo the user administers whose GitHub webhook is not working: it is only polled, so its cards are
 * slower to notice changes. Says why on hover. */
const WEBHOOK_LABEL: Record<RepoWebhook["state"], string> = {
  claimed: "webhook claimed elsewhere",
  leftover: "webhook blocked",
  blocked: "webhook blocked",
  failed: "webhook off",
  not_installed: "webhook off",
};

function WebhookProblem({ repoId }: { repoId: number }) {
  const webhook = useApp((s) => s.webhooks[repoId]);
  if (webhook === undefined) return null;
  return (
    <span className="hook-problem" title={`You administer this repo, but its webhook is not working: ${webhook.problem}`}>
      {WEBHOOK_LABEL[webhook.state]}
    </span>
  );
}

/** How old a box's data is, in seconds ("updated 12s ago", counting up), amber once it is older than
 * ``staleAfter`` seconds. ``at`` is when it was last read (ms), if ever. */
function Freshness({
  at,
  staleAfter,
  title,
  error,
}: {
  at: number | null | undefined;
  staleAfter: number;
  title: string;
  /** Why the latest read failed: the data is then as old as ``at``, and the box says so. */
  error?: string;
}) {
  const now = useSecondNow();
  if (at === null || at === undefined) {
    return (
      <span className={`fresh${error ? " stale" : ""}`} title={error ? `${title} Latest attempt failed: ${error}.` : title}>
        {error ? `not read yet (${error})` : "not read yet"}
      </span>
    );
  }
  const seconds = Math.max((now - at) / 1000, 0);
  return (
    <span
      className={`fresh${seconds > staleAfter || error ? " stale" : ""}`}
      title={error ? `${title} Latest attempt failed: ${error}.` : title}
    >
      {`updated ${Math.floor(seconds)}s ago`}
      {error ? ` (${error})` : ""}
    </span>
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
  /**
   * Grouped view, a leaf card: open the stacked view rooted at `root` (its
   * stack's root) and bring this card (`id`) into view.
   */
  onOpenInStack(id: string, root: string): void;
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
    const { id, depth, rails, moved, highlighted, onOpenInStack } = props;
    const store = useAppStore();
    // Card views without stack rails: where to show this card in its stack.
    // A branch with children: at itself (it and what is stacked on it). A
    // leaf: at its stack's root, so the whole stack is drawn around it.
    const stackedRoot = useApp((s) => {
      if (s.view.view !== "grouped" && s.view.view !== "priorities") return null;
      if ((s.branchesById[id]?.children.length ?? 0) > 0) return id;
      return stackViewRoot(id, (parent) => s.branchesById[parent]?.parent_id ?? null);
    });
    const stackedHref = useApp((s) =>
      stackedRoot === null ? null : serializeState({ ...s.view, view: "stacked", root: stackedRoot }),
    );
    // Stacked view only: where "focus on this subtree" goes (not offered at
    // the current focus root).
    const focusHref = useApp((s) =>
      s.view.view === "stacked" && s.view.root !== id ? serializeState({ ...s.view, root: id }) : null,
    );
    const branch = useApp((s) => s.branchesById[id]);
    // Stacked view only: this card and what is stacked below it can be rolled
    // up into one line. `rolled` is the roll-up (as JSON, so the selector
    // returns a primitive) while it is.
    const stackedView = useApp((s) => s.view.view === "stacked");
    const descendants = useApp((s) => subtreeIds(s, id).length - 1);
    const rolled = useApp((s) =>
      s.view.view === "stacked" && s.folded.includes(id)
        ? JSON.stringify(rollup(subtreeIds(s, id), (key) => s.statuses[key]))
        : "",
    );
    const repo = useApp((s) => (branch ? s.reposById[branch.repo_id] : undefined));
    const status = useApp((s) => s.statuses[id]) ?? LOADING;
    const task = useApp((s) => s.tasks[id]) ?? null;
    const slack = useApp((s) => s.slacks[id]) ?? null;
    const claim = useApp((s) => s.claims[id]) ?? NO_CLAIM;
    const extras = useApp((s) => s.extras[id]);
    const decidedAt = useApp((s) => s.decidedAt);
    const spendReadAt = useApp((s) => s.spendReadAt);
    const fetchedAt = useApp((s) => s.fetchedAt[id]);
    const fetchError = useApp((s) => s.fetchError[id]);
    const catalog = useApp((s) => s.catalog);
    const flashed = useFlashOnChange(status);
    const now = useNow();
    if (!branch || !repo || !catalog) return null;

    const classes = ["card"];
    if (flashed || moved) classes.push("flash");
    if (highlighted) classes.push("highlight");
    const hasChildren = branch.children.length > 0;
    // A merged PR means the task is done: its age stops being interesting.
    const taskDone = status.load === "loaded" && status.pr === "merged";
    // Why every live slot is empty while there is no status to show.
    const note = status.load === "loaded" ? null : unloadedNote(status, repo.path_missing);

    const isFolded = rolled !== "" && hasChildren;
    // Next to the card (outside it, in the stacked list's gutter): rolls this
    // card and everything below it up into one line, and back out.
    const tab =
      stackedView && hasChildren ? (
        <button
          type="button"
          className="fold-tab"
          aria-expanded={!isFolded}
          aria-label={`${isFolded ? "Expand" : "Collapse"} ${branch.name} and ${descendants} descendant${descendants === 1 ? "" : "s"}`}
          title={isFolded ? "Show this branch and what is stacked below it" : "Roll this branch and what is stacked below it up into one line"}
          onClick={() => store.getState().toggleFold(id)}
        >
          <span aria-hidden="true">{isFolded ? "▸" : "▾"}</span>
        </button>
      ) : null;

    if (isFolded) {
      const summary = JSON.parse(rolled) as ReturnType<typeof rollup>;
      return (
        <article
          className={[...classes, "card-folded"].join(" ")}
          data-id={id}
          data-folded="true"
          style={{ "--depth": depth } as CSSProperties}
        >
          <Rails rails={rails} />
          {tab}
          <Slot id="repo" label="Repo">
            <span className="repo-chip" title={repo.path}>
              {repo.name}
            </span>
            <WebhookProblem repoId={repo.id} />
          </Slot>
          <Slot id="branch" label="Branch">
            <span className={`branch${branch.current ? " current" : ""}`}>{branch.name}</span>
          </Slot>
          <div className="rollup" role="group" aria-label="This branch and everything stacked below it">
            <span className="rollup-count">{`${summary.total} branches`}</span>
            {summary.you.length > 0 && (
              <Pill tone={WHO_TONE.you} title="What needs you in this branch and below">
                {`${WHO_TEXT.you}: ${youText(summary.you)}`}
              </Pill>
            )}
            {summary.agent > 0 && <Pill tone={WHO_TONE.agent}>{`${WHO_TEXT.agent}: ${summary.agent}`}</Pill>}
            {summary.auto > 0 && <Pill tone={WHO_TONE.auto}>{`${WHO_TEXT.auto}: ${summary.auto}`}</Pill>}
            {summary.ci > 0 && <Pill tone={WHO_TONE.ci}>{`${WHO_TEXT.ci}: ${summary.ci}`}</Pill>}
            {summary.others > 0 && <Pill tone={WHO_TONE.others}>{`${WHO_TEXT.others}: ${summary.others}`}</Pill>}
            {summary.done > 0 && <Pill tone={WHO_TONE.none}>{`${WHO_TEXT.none}: ${summary.done}`}</Pill>}
            {summary.loading > 0 && <Pill tone="gray">{`loading: ${summary.loading}`}</Pill>}
          </div>
        </article>
      );
    }

    return (
      <article
        className={classes.join(" ")}
        data-id={id}
        style={{ "--depth": depth } as CSSProperties}
      >
        <Rails rails={rails} />
        {tab}
        <Group id="git" title="Git">
          <Slot id="repo" label="Repo">
            <span className="repo-chip" title={repo.path}>
              {repo.name}
            </span>
            <WebhookProblem repoId={repo.id} />
          </Slot>
          <Slot id="branch" label="Branch">
            <span className={`branch${branch.current ? " current" : ""}`}>{branch.name}</span>{" "}
            {/* Not for a lone branch: there is no stack to see. */}
            {stackedHref !== null && stackedRoot !== null && (stackedRoot !== id || hasChildren) && (
              <a
                className="stack-open"
                href={stackedHref}
                title={
                  stackedRoot === id
                    ? "Open the Stacked view focused on this branch and what is stacked on it"
                    : "Open the Stacked view showing this branch in its stack"
                }
                onClick={(event) => {
                  event.preventDefault();
                  if (stackedRoot === id) store.getState().focusRoot(id, "stacked");
                  else onOpenInStack(id, stackedRoot);
                }}
              >
                open in stacked view →
              </a>
            )}{" "}
            {focusHref !== null && hasChildren && (
              <a
                className="stack-open"
                href={focusHref}
                title="Show only this branch and what is stacked on it"
                onClick={(event) => {
                  event.preventDefault();
                  store.getState().focusRoot(id);
                }}
              >
                focus on this subtree →
              </a>
            )}
          </Slot>
          <Slot id="ancestors" label="Ancestors">{String(branch.depth)}</Slot>
          <Slot id="descendants" label="Descendants">{String(descendants)}</Slot>
        </Group>
        <Group
          id="pr"
          title="PR"
          aside={
            <Freshness
              at={fetchedAt}
              error={fetchError}
              // Merged and closed PRs are looked up every 5 minutes, the rest every 20 seconds.
              staleAfter={taskDone || (status.load === "loaded" && status.pr === "closed") ? 900 : 90}
              title="When this PR's status was last looked up on GitHub. It is checked about every 20 seconds while this page is open (every 5 minutes for merged and closed PRs, and for repos whose webhooks are forwarded here). The status shown is kept across restarts."
            />
          }
        >
          <PrSlots status={status} note={note} />
        </Group>
        <Group id="task" title="Task">
          <NextSlot id={id} status={status} note={note} claim={claim} slackPosting={slack?.posting ?? false} />
          <Slot
            id="task_age"
            label="Task age"
            state={taskDone ? "na" : branch.started_at ? "ok" : "gap"}
          >
            {taskDone ? (
              "n/a (PR merged)"
            ) : branch.started_at ? (
              <Age
                text={formatElapsed((now - Date.parse(branch.started_at)) / 1000)}
                since={branch.started_at}
              />
            ) : (
              "no start recorded"
            )}
          </Slot>
          <Slot id="task" label="Task" state={branch.task ? "ok" : "gap"}>
            {branch.task ? (
              branch.task.href ? (
                <ExternalLink href={branch.task.href}>{branch.task.label}</ExternalLink>
              ) : (
                branch.task.label
              )
            ) : (
              "no task linked"
            )}
          </Slot>
          <Slot id="task_title" label="Task title" state={task?.state === "info" ? "ok" : "na"}>
            {task?.state === "info" ? (
              task.title
            ) : task?.state === "unavailable" ? (
              <span className="task-warn" title={task.reason}>
                details unavailable ⚠
              </span>
            ) : branch.task && branch.task_fetchable ? (
              "loading…"
            ) : (
              "–"
            )}
          </Slot>
          <Slot id="task_status" label="Task status" state={task?.state === "info" ? "ok" : "na"}>
            {task?.state === "info" ? (
              <span className="pill task-status">{task.status}</span>
            ) : branch.task && branch.task_fetchable && task?.state !== "unavailable" ? (
              "loading…"
            ) : (
              "–"
            )}
          </Slot>
          <Slot
            id="slack_status"
            label="Slack status"
            state={slack?.posted ? "ok" : slack && status.load === "loaded" && status.pr === "open" ? "gap" : "na"}
          >
            {slack ? <SlackStatus slack={slack} /> : "–"}
          </Slot>
          <Slot id="slack_age" label="Post age" state={slack?.last_at ? "ok" : "na"}>
            {slack?.last_at ? <Age text={formatElapsed((now - Date.parse(slack.last_at)) / 1000)} since={slack.last_at} /> : "–"}
          </Slot>
        </Group>
        <Group
          id="agent"
          title="Agent"
          aside={
            <Freshness
              at={decidedAt}
              staleAfter={60}
              title="When the autonomous agents last decided what to do with every task (every few seconds). The server pushes each change here."
            />
          }
        >
          <AgentSlots extras={extras} branchId={id} session={sessionLink(claim, branch.agent)} Slot={Slot} />
        </Group>
        <Group
          id="spend"
          title="Spend"
          aside={
            <Freshness
              at={spendReadAt}
              staleAfter={180}
              title="When the session transcripts were last read for spend (about once a minute). The server pushes each change here."
            />
          }
        >
          <SpendSlots extras={extras} Slot={Slot} />
        </Group>
        <div className="g-timeline">
          <TimelineSlot
            status={status}
            note={note}
            startedAt={branch.started_at}
            catalog={catalog}
            id={id}
            now={now}
          />
        </div>
      </article>
    );
  },
  (a, b) =>
    a.id === b.id &&
    a.depth === b.depth &&
    a.moved === b.moved &&
    a.highlighted === b.highlighted &&
    a.onOpenInStack === b.onOpenInStack &&
    sameRails(a.rails, b.rails),
);

const LOOKUP_FAILED = "gh could not look up this branch's PR";

/** What an unloaded card's live slots say instead of a value. */
function unloadedNote(status: Exclude<LiveStatus, LoadedStatus>, pathMissing: boolean): ReactNode {
  if (pathMissing) return "repo path missing";
  if (status.load === "failed") {
    return (
      <span className="error" title={LOOKUP_FAILED}>
        status unavailable
      </span>
    );
  }
  return "loading…";
}

/** The Next slot (the Task group's first). */
function NextSlot({
  id,
  status,
  note,
  claim,
  slackPosting,
}: {
  id: string;
  status: LiveStatus;
  note: ReactNode;
  claim: BranchClaim;
  /** A message about the PR is being posted in Slack in the background. */
  slackPosting: boolean;
}) {
  if (status.load !== "loaded") {
    return (
      <Slot id="next" label="Next" state="na">
        {note}
        <ClaimChips claim={claim} />
      </Slot>
    );
  }
  const { would_be: wouldBe } = status.next;
  return (
    <Slot id="next" label="Next">
      {status.next.kind === "merge" ? (
        <MergeButton id={id} status={status} />
      ) : status.next.kind === "post_slack" || status.next.kind === "bump_slack" ? (
        <SlackButton id={id} status={status} posting={slackPosting} />
      ) : (
        <Pill tone={WHO_TONE[status.next.who]} title={status.next.kind}>
          {status.next.label}
        </Pill>
      )}{" "}
      <span className="card-next-who muted">
        {wouldBe ? `would be: ${wouldBe.label}` : WHO_TEXT[status.next.who]}
      </span>
      <ClaimChips claim={claim} />
    </Slot>
  );
}

/** Next is Merge: a button that opens a dialog asking whether to land the PR
 * with `toolbelt git merge`. The branch leaves the page when the merge syncs the
 * stack. */
function MergeButton({ id, status }: { id: string; status: LoadedStatus }) {
  const [open, setOpen] = useState(false);
  const target = status.pr_number !== null ? `PR #${status.pr_number}` : "this PR";
  return (
    <>
      <button
        type="button"
        className="merge-button"
        aria-haspopup="dialog"
        title="Asks before running toolbelt git merge for this PR"
        onClick={() => setOpen(true)}
      >
        {status.next.label}
      </button>
      {open && <MergeDialog id={id} target={target} onClose={() => setOpen(false)} />}
    </>
  );
}

/** Where the PR stands in Slack: a link to the thread once posted, "Posting…"
 * while a post is in the background, and why the last one failed or what Slack's
 * copy lacked. */
function SlackStatus({ slack }: { slack: SlackInfo }) {
  const label = slack.posted ? (slack.last_kind === "bump" ? "Bumped in Slack" : "Posted in Slack") : "Not posted in Slack";
  return (
    <>
      {slack.posting ? (
        "Posting in Slack…"
      ) : slack.posted && slack.thread_link ? (
        <a href={slack.thread_link} target="_blank" rel="noreferrer">
          {label}
        </a>
      ) : (
        label
      )}
      {slack.notice && !slack.posting && (
        <span className="error" role="alert">
          {` ${slack.notice}`}
        </span>
      )}
    </>
  );
}

/** Next is Post in Slack or Bump in Slack: a button that opens the message for the
 * user to confirm. Nothing is posted until it is confirmed in the dialog. */
function SlackButton({ id, status, posting }: { id: string; status: LoadedStatus; posting: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className="merge-button"
        aria-haspopup="dialog"
        disabled={posting}
        title={posting ? "Being posted in the background" : "Shows the message to post; nothing is sent until you confirm it"}
        onClick={() => setOpen(true)}
      >
        {posting ? "Posting in Slack…" : status.next.label}
      </button>
      {open && <SlackDialog id={id} onClose={() => setOpen(false)} />}
    </>
  );
}

const NO_CLAIM: BranchClaim = { holders: [] };

const HOLDER_TEXT: Record<HolderState, string> = {
  working: "working",
  idle: "idle",
  blocked: "blocked on you",
};
const HOLDER_TONE: Record<HolderState, Tone> = { working: "purple", idle: "gray", blocked: "amber" };

/** Who holds a claim on the branch: a chip per holder (state, age, and the
 * session's link). Working holders are also the reason for "With an agent". */
function ClaimChips({ claim }: { claim: BranchClaim }) {
  if (claim.holders.length === 0) return null;
  return (
    <span className="claim-chips">
      {claim.holders.map((holder) => {
        const who =
          holder.kind === "human" ? "claimed by you" : holder.kind === "autonomous" ? "autonomous agent" : "agent";
        const text = holder.kind === "human" ? who : `${who} ${HOLDER_TEXT[holder.state]}`;
        const title = holder.stale
          ? "Not heard from for a while: it may be hung (tt task release to take it back)"
          : `${holder.holder}, since ${holder.age.text} ago`;
        const label = holder.stale ? `${text} (stale)` : text;
        return (
          <span
            key={holder.holder}
            className={`pill claim-chip tone-${holder.stale ? "red" : HOLDER_TONE[holder.state]}`}
            data-state={holder.state}
            title={title}
          >
            {holder.link?.href ? (
              <a href={holder.link.href} title={title}>
                {label}
              </a>
            ) : (
              label
            )}
          </span>
        );
      })}
    </span>
  );
}

/** Every slot of the PR group; each says why when it has no value. */
function PrSlots({ status, note }: { status: LiveStatus; note: ReactNode }) {
  if (status.load !== "loaded") {
    const unloaded: [SlotKey, string][] = [
      ["title", "PR title"],
      ["pr", "PR"],
      ["ci", "CI"],
      ["review", "Review"],
      ["comments", "Human comments"],
      ["bot_comments", "Bot comments"],
      ["merge", "Merge"],
      ["conflict_commit", "Conflict commit"],
      ["lines", "Lines"],
      ["pr_age", "PR age"],
      ["in_draft", "In draft"],
      ["review_requested", "Review requested"],
      ["ci_failures", "CI failures"],
      ["reviewers", "Reviewers"],
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
  return <LoadedPrSlots status={status} />;
}

/** The timeline, in its own box below the groups. */
function TimelineSlot({
  status,
  note,
  startedAt,
  catalog,
  id,
  now,
}: {
  status: LiveStatus;
  note: ReactNode;
  startedAt: string | null;
  catalog: Catalog;
  id: string;
  now: number;
}) {
  return (
    <Slot id="timeline" label="Timeline" state={status.load === "loaded" ? "ok" : "na"}>
      {status.load === "loaded" ? (
        <Timeline id={id} status={status} startedAt={startedAt} catalog={catalog} now={now} />
      ) : (
        note
      )}
    </Slot>
  );
}

function LoadedPrSlots({ status }: { status: LoadedStatus }) {
  const { detail } = status;
  const hasPr = status.pr !== "none";
  const humanComments = status.open_comments - status.open_comments_bot;
  return (
    <>
      <Slot id="title" label="PR title" state={detail && hasPr ? "ok" : "na"}>
        {detail && hasPr ? <span title={detail.title}>{detail.title}</span> : hasPr ? "PR details unavailable" : "no PR"}
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
              <Pill tone="gray">no CI configured</Pill>
            </Slot>
          )}
          {status.review !== "none" ? (
            <Slot id="review" label="Review">
              <Pill tone={REVIEW_TONE[status.review]}>{status.review}</Pill>
            </Slot>
          ) : (
            <Slot id="review" label="Review" state="gap">
              <Pill tone="gray">no review requested</Pill>
            </Slot>
          )}
          <Slot id="comments" label="Human comments">
            {humanComments > 0 ? (
              <Pill tone="amber" title="unresolved review threads a person started">
                {`${humanComments} comment${humanComments === 1 ? "" : "s"}`}
              </Pill>
            ) : (
              <Pill tone="green">0 comments</Pill>
            )}
          </Slot>
          <Slot id="bot_comments" label="Bot comments">
            {status.open_comments_bot > 0 ? (
              <Pill tone="amber" title="unresolved review threads a bot (Copilot) started">
                {`${status.open_comments_bot} comment${status.open_comments_bot === 1 ? "" : "s"}`}
              </Pill>
            ) : (
              <Pill tone="green">0 comments</Pill>
            )}
          </Slot>
          {status.merge === "conflicts" ? (
            <Slot id="merge" label="Merge">
              <Pill tone="red">conflicts</Pill>
            </Slot>
          ) : status.merge === "clean" ? (
            <Slot id="merge" label="Merge">
              <Pill tone="green">clean</Pill>
            </Slot>
          ) : (
            <Slot id="merge" label="Merge" state="na">
              {`n/a (PR ${status.pr})`}
            </Slot>
          )}

          {status.conflict ? (
            <Slot id="conflict_commit" label="Conflict commit">
              <ConflictCommit conflict={status.conflict} />
            </Slot>
          ) : (
            <Slot id="conflict_commit" label="Conflict commit" state="na">
              no conflict
            </Slot>
          )}

          {detail ? (
            <>
              <Slot id="lines" label="Lines">
                <span title="lines added and deleted">
                  <span className="lines-added">{`+${detail.additions}`}</span>{" "}
                  <span className="lines-deleted">{`-${detail.deletions}`}</span>
                </span>
              </Slot>
              <Slot id="pr_age" label="PR age">
                <Age {...detail.pr_age} />
              </Slot>
              {detail.draft_age ? (
                <Slot id="in_draft" label="In draft">
                  <Age {...detail.draft_age} />
                </Slot>
              ) : (
                <Slot id="in_draft" label="In draft" state="na">
                  <Pill tone="gray">not a draft</Pill>
                </Slot>
              )}
              {detail.review_request_age ? (
                <Slot id="review_requested" label="Review requested">
                  <Age {...detail.review_request_age} />
                </Slot>
              ) : (
                <Slot id="review_requested" label="Review requested" state="na">
                  <Pill tone="gray">not requested</Pill>
                </Slot>
              )}
              <Slot id="ci_failures" label="CI failures">
                <Pill tone={detail.ci_failures ? "red" : "green"}>
                  {`${detail.ci_failures} failure${detail.ci_failures === 1 ? "" : "s"}`}
                </Pill>
              </Slot>
              {detail.reviewers.length > 0 ? (
                <Slot id="reviewers" label="Reviewers">
                  {detail.reviewers.join(", ")}
                </Slot>
              ) : (
                <Slot id="reviewers" label="Reviewers" state="na">
                  <Pill tone="gray">none requested</Pill>
                </Slot>
              )}
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
          <Slot id="comments" label="Human comments" state="na">
            no PR
          </Slot>
          <Slot id="bot_comments" label="Bot comments" state="na">
            no PR
          </Slot>
          <Slot id="merge" label="Merge" state="na">
            no PR
          </Slot>
          <Slot id="conflict_commit" label="Conflict commit" state="na">
            no PR
          </Slot>
          <UnavailableSlots note="no PR" />
        </>
      )}
    </>
  );
}

/** Where a branch's conflict comes from: the commit (a link to it when the PR
 *  is known), what it is stopped on, and the files that conflict. */
function ConflictCommit({ conflict }: { conflict: NonNullable<LoadedStatus["conflict"]> }) {
  const short = conflict.sha.slice(0, 7);
  const shown = conflict.files.slice(0, 3);
  const hidden = conflict.files.length - shown.length;
  const where =
    conflict.source === "rebase"
      ? `rebase stopped here${conflict.step !== null && conflict.steps !== null ? `, commit ${conflict.step} of ${conflict.steps}` : ""}`
      : conflict.source === "merge"
        ? "merge stopped here"
        : "first commit that conflicts with the base";
  return (
    <span title={conflict.files.length > 0 ? `conflicts in ${conflict.files.join(", ")}` : undefined}>
      {conflict.url ? <ExternalLink href={conflict.url}>{short}</ExternalLink> : short}
      {` ${conflict.subject} `}
      <span className="muted">{`(${where}${conflict.files.length > 0 ? `: ${shown.join(", ")}${hidden > 0 ? ` and ${hidden} more` : ""}` : ""})`}</span>
    </span>
  );
}

/** The PR-detail slots of a card that has no PR (or no detail), each saying why. */
function UnavailableSlots({ note }: { note: string }) {
  const keys: [SlotKey, string][] = [
    ["lines", "Lines"],
    ["pr_age", "PR age"],
    ["in_draft", "In draft"],
    ["review_requested", "Review requested"],
    ["ci_failures", "CI failures"],
    ["reviewers", "Reviewers"],
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
