import type { Aggregate, AgentConversation, Agents, CardExtras, MergeResult, Snapshot, Spend } from "./generated";

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

export const CARD_EXTRAS_URL = "/api/card-extras";

/** What a task card shows beyond its PR status: the agents' view of each
 * branch and what each task has cost. */
export async function fetchCardExtras(fetchImpl: typeof fetch = fetch): Promise<CardExtras> {
  const response = await fetchImpl(CARD_EXTRAS_URL, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${CARD_EXTRAS_URL}: HTTP ${response.status}`);
  return (await response.json()) as CardExtras;
}

/** Land a branch's PR (`toolbelt git merge`); resolves with what it did, and
 * rejects with the server's reason when the branch is not ready to merge. */
export async function mergeBranch(id: string, fetchImpl: typeof fetch = fetch): Promise<MergeResult> {
  const response = await fetchImpl("/api/merge", {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ id }),
  });
  if (!response.ok) {
    const detail = ((await response.json().catch(() => ({}))) as { detail?: unknown }).detail;
    throw new Error(typeof detail === "string" ? detail : `/api/merge: HTTP ${response.status}`);
  }
  return (await response.json()) as MergeResult;
}
