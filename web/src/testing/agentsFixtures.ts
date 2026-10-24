// A small `Agents` payload for the agents view's tests.
import type { Agents } from "../api/generated";

export function agents(parts: Partial<Agents> = {}): Agents {
  return {
    enabled: true,
    launching: false,
    dry_run: true,
    ticked_at: "2026-10-13T17:00:00+00:00",
    error: null,
    blockers: [],
    limits: {
      model: "claude-haiku-5-5",
      run_usd: 0.25,
      run_turns: 40,
      run_timeout_seconds: 900,
      task_runs_per_head: 2,
      task_runs_per_day: 3,
      task_usd_per_day: 0.5,
      global_usd_per_day: 3,
      global_usd_per_month: 30,
      active_working_days: 3,
    },
    spend: { last_24_hours_usd: 0.42, this_month_usd: 4.5 },
    screen: { awake: true, locked: false },
    signed_in: true,
    decisions: [
      { id: "1:devon/a", repo: "api", branch: "devon/a", trigger: "fix_ci", reason: null, decided_at: "2026-10-13T17:00:00+00:00", can_override: false, starting: false, start_error: null },
      {
        id: "1:devon/b",
        repo: "api",
        branch: "devon/b",
        trigger: "resolve_conflicts",
        reason: { code: "claimed", text: "A person or an agent session holds this task.", until: null },
        decided_at: "2026-10-13T17:00:00+00:00",
        can_override: false, starting: false, start_error: null,
      },
      {
        id: "1:devon/c",
        repo: "api",
        branch: "devon/c",
        trigger: "resolve_comments",
        reason: { code: "backoff", text: "The last run on this task ended recently.", until: "2026-10-13T17:10:00+00:00" },
        decided_at: "2026-10-13T17:00:00+00:00",
        can_override: false, starting: false, start_error: null,
      },
    ],
    runs: [
      {
        run_id: "r1",
        id: "1:devon/c",
        repo: "api",
        branch: "devon/c",
        trigger: "resolve_comments",
        model: "claude-haiku-5-5",
        state: "succeeded",
        started_at: "2026-10-13T16:50:00+00:00",
        ended_at: "2026-10-13T16:55:00+00:00",
        turns: 9, activity: null,
        cost_usd: 0.0831,
        summary: "Replied to two Copilot threads and resolved them.",
        error: null,
        result: null,
      },
    ],
    ...parts,
  };
}
