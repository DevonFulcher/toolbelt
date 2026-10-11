import type { Aggregate, Snapshot } from "./generated";

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
