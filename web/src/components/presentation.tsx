// Small shared pieces of the table and the cards: pills, the task value and
// the age text. The tone of each state is a `Record` over the generated
// union, so a state added on the server does not compile here until it has
// one.
import type { CiState, Link, PrState, ReviewState, TaskLive, Tone, Who } from "../api/generated";
import { useNow } from "./useNow";
import { formatAge } from "../logic/format";

export const PR_TONE: Record<PrState, Tone> = {
  open: "green",
  merged: "purple",
  closed: "red",
  draft: "gray",
  none: "gray",
};

export const CI_TONE: Record<CiState, Tone> = {
  success: "green",
  failed: "red",
  running: "amber",
  none: "gray",
};

export const REVIEW_TONE: Record<ReviewState, Tone> = {
  approved: "green",
  requested: "amber",
  reviewed: "amber",
  none: "gray",
};

/** Who a branch's next action is waiting on, in words. */
export const WHO_TEXT: Record<Who, string> = {
  you: "needs you",
  agent: "with an agent",
  auto: "autonomous agent",
  others: "waiting on others",
  ci: "waiting on CI",
  none: "done",
};
export const WHO_TONE: Record<Who, Tone> = {
  you: "accent",
  agent: "purple",
  auto: "purple",
  ci: "amber",
  others: "gray",
  none: "gray",
};

export function Pill({ tone, title, children }: { tone: Tone; title?: string; children: string }) {
  return (
    <span className={`pill tone-${tone}`} title={title}>
      {children}
    </span>
  );
}

export function ExternalLink({ href, className, children }: { href: string; className?: string; children: React.ReactNode }) {
  return (
    <a className={className} href={href} target="_blank" rel="noopener">
      {children}
    </a>
  );
}

/** The task ref (linked when the tracker can), plus its title/status once fetched. */
export function TaskValue({ link, task }: { link: Link | null; task: TaskLive | null }) {
  if (!link) return null;
  return (
    <>
      {link.href ? <ExternalLink href={link.href}>{link.label}</ExternalLink> : link.label}
      {task?.state === "unavailable" && (
        <span className="task-warn" title={task.reason}>
          ⚠
        </span>
      )}
      {task?.state === "info" && (
        <>
          {" "}
          <span className="task-title" title={task.title}>
            {task.title}
          </span>{" "}
          <span className="pill task-status">{task.status}</span>
        </>
      )}
    </>
  );
}

/** A growing age ("5d"), kept current between server updates. */
export function Age({ text, since }: { text: string; since: string | null }) {
  const now = useNow();
  const started = since ? Date.parse(since) : Number.NaN;
  return <time dateTime={since ?? undefined}>{Number.isNaN(started) ? text : formatAge((now - started) / 1000)}</time>;
}
