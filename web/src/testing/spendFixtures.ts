// A small `Spend` payload for the spend view's tests.
import type { Spend, SpendTokens } from "../api/generated";

const tokens = (parts: Partial<SpendTokens> = {}): SpendTokens => ({
  input: 0,
  output: 0,
  cache_read: 0,
  cache_write_5m: 0,
  cache_write_1h: 0,
  ...parts,
});

export function spend(parts: Partial<Spend> = {}): Spend {
  return {
    as_of: "2026-10-13T12:00:00+00:00",
    ingested_at: "2026-10-13T11:59:00+00:00",
    breakdown_days: 7,
    windows: [
      { label: "Last 24 hours", start: "2026-10-12T12:00:00+00:00", calls: 1166, tokens: tokens({ cache_read: 280_800_000 }), cost_usd: 61.67, unpriced_calls: 0 },
      { label: "Last 7 days", start: "2026-10-06T12:00:00+00:00", calls: 5426, tokens: tokens({ cache_read: 1_800_000_000 }), cost_usd: 377.66, unpriced_calls: 0 },
      { label: "Last 30 days", start: "2026-09-13T12:00:00+00:00", calls: 15234, tokens: tokens({ cache_read: 4_800_000_000 }), cost_usd: 1368.7, unpriced_calls: 0 },
      { label: "This month (UTC)", start: "2026-10-01T00:00:00+00:00", calls: 7951, tokens: tokens({ cache_read: 2_600_000_000 }), cost_usd: 574.42, unpriced_calls: 0 },
    ],
    by_model: [
      { model: "claude-sonnet-5-5", calls: 5090, tokens: tokens({ cache_read: 1_600_000_000 }), cost_usd: 322.55, unpriced_calls: 0 },
      { model: "claude-haiku-5-5", calls: 42, tokens: tokens({ cache_read: 3_600_000 }), cost_usd: 0.18, unpriced_calls: 0 },
    ],
    by_task: [
      { task_id: "t1", repo: "api", branch: "devon/a", calls: 120, tokens: 90_000_000, cost_usd: 41.5, unpriced_calls: 0 },
      { task_id: "gone-task-id", repo: null, branch: null, calls: 10, tokens: 1_000_000, cost_usd: 2.25, unpriced_calls: 0 },
      { task_id: null, repo: null, branch: null, calls: 5296, tokens: 1_700_000_000, cost_usd: 333.9, unpriced_calls: 0 },
    ],
    unpriced_models: [],
    claims_since: "2026-10-13T11:00:00+00:00",
    autonomous: {
      windows: [
        { label: "Last 24 hours", start: "2026-10-12T12:00:00+00:00", runs: 2, cost_usd: 0.1831, tokens: tokens({ cache_read: 90_000 }) },
        { label: "Last 7 days", start: "2026-10-06T12:00:00+00:00", runs: 5, cost_usd: 0.4, tokens: tokens({ cache_read: 200_000 }) },
        { label: "Last 30 days", start: "2026-09-13T12:00:00+00:00", runs: 5, cost_usd: 0.4, tokens: tokens({ cache_read: 200_000 }) },
        { label: "This month (UTC)", start: "2026-10-01T00:00:00+00:00", runs: 5, cost_usd: 0.4, tokens: tokens({ cache_read: 200_000 }) },
      ],
      by_trigger: [
        { trigger: "fix_ci", runs: 3, cost_usd: 0.25 },
        { trigger: "resolve_conflicts", runs: 2, cost_usd: 0.15 },
      ],
      by_task: [{ task_id: "t1", repo: "api", branch: "devon/a", runs: 5, cost_usd: 0.4 }],
    },
    ...parts,
  };
}
