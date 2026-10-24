// Builders for API payloads in tests. The catalog is the real one: the file
// the Python side generates (src/api/catalog.json), so the client is tested
// against exactly what the server serves.
import catalogJson from "../api/catalog.json";
import type {
  Branch,
  Catalog,
  Chip,
  LoadedStatus,
  PrDetail,
  Repo,
  Snapshot,
  TimelineEvent,
} from "../api/generated";

export const catalog = catalogJson as unknown as Catalog;

export const NOW = Date.parse("2026-10-20T12:00:00Z");
export const MIN = 60 * 1000;
export const HOUR = 60 * MIN;
export const DAY = 24 * HOUR;
export const iso = (ms: number): string => new Date(ms).toISOString();
export const ago = (ms: number): string => iso(NOW - ms);

export function event(kind: TimelineEvent["kind"], agoMs: number, extra: Partial<TimelineEvent> = {}): TimelineEvent {
  return { t: ago(agoMs), kind, actor: "devon", detail: "", ci: "", subject: "", ci_done: null, ...extra };
}

/** The "now" chips the server would send for a status. */
function chipsFor(status: Omit<LoadedStatus, "now">): Chip[] {
  const chips: Chip[] = [{ dimension: "pr", value: status.pr }];
  if (status.pr !== "none") {
    chips.push(
      { dimension: "ci", value: status.ci },
      { dimension: "review", value: status.review },
      {
        dimension: "comments",
        value: status.open_comments ? "open" : "none",
        n: status.open_comments,
      },
      { dimension: "merge", value: status.merge },
    );
  }
  chips.push({
    dimension: "next",
    value: status.next.kind,
    text: status.next.label,
    who: status.next.who,
    would_be: status.next.would_be?.label ?? null,
  });
  return chips;
}

export function pr(overrides: Partial<Omit<LoadedStatus, "now">> = {}): LoadedStatus {
  const base: Omit<LoadedStatus, "now"> = {
    load: "loaded",
    pr: "open",
    pr_number: 5,
    pr_url: "https://github.com/o/r/pull/5",
    ci: "success",
    review: "none",
    merge: "clean",
    open_comments: 0,
    open_comments_bot: 0,
    conflict: null,
    next: { label: "Request review", who: "you", kind: "request_review", would_be: null },
    detail: null,
    ...overrides,
  };
  return { ...base, now: chipsFor(base) };
}

export function noPr(): LoadedStatus {
  return pr({
    pr: "none",
    pr_number: null,
    pr_url: null,
    ci: "none",
    review: "none",
    merge: "none",
    next: { label: "Open PR", who: "you", kind: "open_pr", would_be: null },
  });
}

export function detail(overrides: Partial<PrDetail> = {}): PrDetail {
  return {
    title: "Add the thing",
    pr_age: { text: "5d", since: ago(5 * DAY) },
    draft_age: null,
    review_request_age: null,
    ci_failures: 0,
    additions: 12,
    deletions: 3,
    created: ago(5 * DAY),
    ended: null,
    is_draft: false,
    events: [event("opened", 5 * DAY, { detail: "Add the thing" })],
    ...overrides,
  };
}

export const repo = (id: number, name: string, extra: Partial<Repo> = {}): Repo => ({
  id,
  name,
  path: `/work/${name}`,
  path_missing: false,
  ...extra,
});

/** A branch; `parent` is the parent's name in the same repo. */
export function branch(
  repoId: number,
  name: string,
  overrides: Partial<Branch> & { parent?: string | null } = {},
): Branch {
  const { parent = null, ...rest } = overrides;
  return {
    id: `${repoId}:${name}`,
    repo_id: repoId,
    name,
    depth: parent ? 1 : 0,
    parent_id: parent ? `${repoId}:${parent}` : null,
    parent,
    order: 0,
    current: false,
    stack_pos: parent ? 2 : 1,
    stack_height: parent ? 2 : 1,
    children: [],
    task: null,
    task_fetchable: false,
    task_live: null,
    agent: null,
    claim: { holders: [] },
    slack: null,
    merge_queue: null,
    sibling_conflicts: null,
    started_at: null,
    status: { load: "loading" },
    ...rest,
  };
}

/** A snapshot of `branches` (their `order` is set from their position). */
export function snapshot(branches: Branch[], repos: Repo[] = [repo(1, "api")], extra: Partial<Snapshot> = {}): Snapshot {
  return {
    version: 0,
    age: null,
    repos,
    branches: branches.map((b, order) => ({ ...b, order })),
    catalog,
    untracked: { state: "loaded", prs: [], truncated: false, hidden: 0 },
    fetched: [],
    extras: null,
    agents: null,
    spend: [],
    webhooks: [],
    filters: [
      { key: "next", label: "Next", options: [
        { value: "you", label: "Needs me" },
        { value: "agent", label: "With an agent" },
        { value: "auto", label: "Autonomous agent" },
        { value: "ci", label: "Waiting on CI" },
        { value: "others", label: "Waiting on others" },
        { value: "none", label: "Done" },
      ] },
      { key: "pr", label: "PR", options: ["none", "draft", "open", "merged", "closed"].map((v) => ({ value: v, label: v })) },
      { key: "ci", label: "CI", options: ["none", "running", "failed", "success"].map((v) => ({ value: v, label: v })) },
      { key: "review", label: "Review", options: ["none", "requested", "approved", "reviewed"].map((v) => ({ value: v, label: v })) },
      { key: "comments", label: "Comments", options: ["has comments", "none"].map((v) => ({ value: v, label: v })) },
      { key: "merge", label: "Merge", options: ["conflicts", "clean", "none"].map((v) => ({ value: v, label: v })) },
    ],
    ...extra,
  };
}
