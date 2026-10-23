import type {
  Aggregate,
  AgentConversation,
  DecisionAnswerInfo,
  DecisionInfo,
  DecisionList,
  Agents,
  MergeQueueInfo,
  Snapshot,
  SlackDraft,
  SlackPrRef,
  SlackSendRequest,
  SlackAccepted,
  Spend,
} from "./generated";

export const SNAPSHOT_URL = "/api/snapshot";
export const EVENTS_URL = "/api/events";

export async function fetchSnapshot(fetchImpl: typeof fetch = fetch): Promise<Snapshot> {
  const response = await fetchImpl(SNAPSHOT_URL, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${SNAPSHOT_URL}: HTTP ${response.status}`);
  return (await response.json()) as Snapshot;
}

export const AGGREGATE_URL = "/api/aggregate";

/** The aggregate view's numbers for the last `weeks` weeks, from the server's
 * background cache unless `refresh` asks GitHub for changes now. */
export async function fetchAggregate(
  weeks: number,
  repo: string,
  fetchImpl: typeof fetch = fetch,
  refresh = false,
): Promise<Aggregate> {
  const query = new URLSearchParams({ weeks: String(weeks) });
  if (repo) query.set("repo", repo);
  if (refresh) query.set("refresh", "true");
  const response = await fetchImpl(`${AGGREGATE_URL}?${query}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`${AGGREGATE_URL}: HTTP ${response.status}`);
  return (await response.json()) as Aggregate;
}

export const SPEND_URL = "/api/spend";

/** The spend view's data: windows of spend, and by-model and by-task tables
 * over the last `days` days. */
export async function fetchSpend(days: number, fetchImpl: typeof fetch = fetch): Promise<Spend> {
  const response = await fetchImpl(`${SPEND_URL}?days=${days}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`${SPEND_URL}: HTTP ${response.status}`);
  return (await response.json()) as Spend;
}

export const AGENTS_URL = "/api/agents";

/** What the autonomous agents are doing, may do, and may not do and why. */
export async function fetchAgents(fetchImpl: typeof fetch = fetch): Promise<Agents> {
  const response = await fetchImpl(AGENTS_URL, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${AGENTS_URL}: HTTP ${response.status}`);
  return (await response.json()) as Agents;
}

/** Switch the autonomous agents on or off. */
export async function setAgentsEnabled(enabled: boolean, fetchImpl: typeof fetch = fetch): Promise<void> {
  const response = await fetchImpl(`${AGENTS_URL}/enabled`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!response.ok) throw new Error(`${AGENTS_URL}/enabled: HTTP ${response.status}`);
}

/** Let the agents start runs (or only decide, as in a dry run). */
export async function setAgentsLaunching(launching: boolean, fetchImpl: typeof fetch = fetch): Promise<void> {
  const response = await fetchImpl(`${AGENTS_URL}/launching`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ launching }),
  });
  if (!response.ok) throw new Error(`${AGENTS_URL}/launching: HTTP ${response.status}`);
}

/** Stop a run that is working. */
export async function stopAgentRun(runId: string, fetchImpl: typeof fetch = fetch): Promise<void> {
  const response = await fetchImpl(`${AGENTS_URL}/runs/${encodeURIComponent(runId)}/stop`, {
    method: "POST",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`${AGENTS_URL}/runs/${runId}/stop: HTTP ${response.status}`);
}

/** What a run has said and done so far, read from its Claude session. */
export async function fetchAgentConversation(runId: string, fetchImpl: typeof fetch = fetch): Promise<AgentConversation> {
  const url = `${AGENTS_URL}/runs/${encodeURIComponent(runId)}/conversation`;
  const response = await fetchImpl(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return (await response.json()) as AgentConversation;
}

/** Start a run on a task by hand; the error says which rule still stops it. */
export async function startAgentRun(id: string, fetchImpl: typeof fetch = fetch): Promise<void> {
  const response = await fetchImpl(`${AGENTS_URL}/start`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ id }),
  });
  if (response.ok) return;
  const detail = ((await response.json().catch(() => ({}))) as { detail?: unknown }).detail;
  throw new Error(typeof detail === "string" ? detail : `${AGENTS_URL}/start: HTTP ${response.status}`);
}

/** Land a branch's PR (`toolbelt git merge`); resolves with what it did, and
 * rejects with the server's reason when the branch is not ready to merge. */
/** POST ``body`` as JSON to ``path``; an error response throws its ``detail``. */
async function postJson<T>(path: string, body: unknown, fetchImpl: typeof fetch): Promise<T> {
  const response = await fetchImpl(path, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = ((await response.json().catch(() => ({}))) as { detail?: unknown }).detail;
    throw new Error(typeof detail === "string" ? detail : `${path}: HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

/** The Slack message toolbelt would post (or bump) for the branch's PR. `prs` names
 * the PRs of a post's stack to cover, each with who is asked about it (omitted: the
 * PRs that are ready; the draft lists the others, unchecked). `reassign` drafts
 * asking someone else to review the PR, in the thread it was posted in. */
export function fetchSlackDraft(
  id: string,
  options: { assigneeId?: string | null; prs?: SlackPrRef[]; reassign?: boolean } = {},
  fetchImpl: typeof fetch = fetch,
): Promise<SlackDraft> {
  const body = {
    id,
    assignee_id: options.assigneeId ?? null,
    ...(options.prs === undefined ? {} : { prs: options.prs }),
    ...(options.reassign ? { reassign: true } : {}),
  };
  return postJson<SlackDraft>("/api/slack/draft", body, fetchImpl);
}

/** Post the confirmed message. It is posted in the background: this resolves once
 * the server has accepted it (the PRs then read `posting`), and rejects with the
 * server's reason when it cannot go. */
export function sendSlack(request: SlackSendRequest, fetchImpl: typeof fetch = fetch): Promise<SlackAccepted> {
  return postJson<SlackAccepted>("/api/slack/send", request, fetchImpl);
}

/** Merge the branch's PR as soon as its CI has passed; rejects with the server's
 * reason when it is not approved and waiting on nothing but CI. */
export function queueMerge(id: string, fetchImpl: typeof fetch = fetch): Promise<MergeQueueInfo> {
  return postJson<MergeQueueInfo>("/api/merge/queue", { id }, fetchImpl);
}

/** The questions agents asked about the branch's task, oldest first. */
export async function fetchDecisions(id: string, fetchImpl: typeof fetch = fetch): Promise<DecisionList> {
  const response = await fetchImpl(`/api/decisions?id=${encodeURIComponent(id)}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`/api/decisions: HTTP ${response.status}`);
  return (await response.json()) as DecisionList;
}

/** Answer a question: one answer per point (an option, words, or both). Rejects
 * with the server's reason when the answers do not fit or it was answered already. */
export function answerDecision(
  decisionId: string,
  answers: DecisionAnswerInfo[],
  fetchImpl: typeof fetch = fetch,
): Promise<DecisionInfo> {
  return postJson<DecisionInfo>("/api/decisions/answer", { decision_id: decisionId, answers }, fetchImpl);
}

/** Delete a question (an orphaned one, or one that is no longer wanted). */
export function deleteDecision(decisionId: string, fetchImpl: typeof fetch = fetch): Promise<DecisionInfo> {
  return postJson<DecisionInfo>("/api/decisions/delete", { decision_id: decisionId }, fetchImpl);
}

/** Look the branch's PR up again now. The server does it in the background: this
 * resolves once it has accepted the request, and the card's age moves when the
 * lookup arrives. */
export async function refreshBranch(id: string, fetchImpl: typeof fetch = fetch): Promise<void> {
  await postJson<unknown>("/api/refresh", { id }, fetchImpl);
}

/** Take the branch's PR off the merge queue. */
export async function unqueueMerge(id: string, fetchImpl: typeof fetch = fetch): Promise<void> {
  const response = await fetchImpl("/api/merge/unqueue", {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ id }),
  });
  if (!response.ok) throw new Error(`/api/merge/unqueue: HTTP ${response.status}`);
}

/** Merge the branch's PR. It is merged in the background: this resolves once the
 * server has accepted it (the PR then reads `merging`, and a failure is its merge
 * queue `notice`), and rejects with the server's reason when it is not ready. */
export async function mergeBranch(id: string, fetchImpl: typeof fetch = fetch): Promise<void> {
  await postJson<unknown>("/api/merge", { id }, fetchImpl);
}
