// The Agent and Spend groups of a task card: the branch's interactive
// session, what the autonomous agents would do or why none is on it, its
// latest run, and what the task has cost (interactive and autonomous).
import { useCallback, useState } from "react";
import { startAgentRun, stopAgentRun, validateTask } from "../api/client";
import type { Branch, BranchExtras, CardValidation, SlotDimensions } from "../api/generated";
import { formatUsd } from "../logic/spendFormat";
import { RUN_TEXT, RUN_TONE, TRIGGER_LABEL, reasonText, runCost } from "./AgentsView";
import { RunsDialog } from "./RunsDialog";
import { Age, Pill } from "./presentation";

const NO_RUN = <span className="muted">–</span>;

type SlotProps = { id: keyof SlotDimensions; label: string; state?: "ok" | "na" | "gap"; children: React.ReactNode };

/** `Slot` is the card's; it is passed in so this file does not depend on Card. */
export function AgentSlots({
  extras,
  branchId,
  session,
  Slot,
}: {
  extras: BranchExtras | undefined;
  branchId: string;
  session: Branch["agent"];
  Slot: (props: SlotProps) => React.ReactElement;
}) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [runsOpen, setRunsOpen] = useState(false);
  const closeRuns = useCallback(() => setRunsOpen(false), []);
  const act = (action: () => Promise<void>): void => {
    setBusy(true);
    setProblem(null);
    // The server pushes the new state of the card when the run starts or stops.
    action()
      .catch((error: unknown) => setProblem(error instanceof Error ? error.message : String(error)))
      .finally(() => setBusy(false));
  };
  const agent = extras?.agent;
  const run = agent?.latest_run ?? null;
  const working = agent?.reason?.code === "working" || run?.state === "running";
  // A run asked for by hand is on its way: the server is making the decision.
  const starting = !working && (agent?.starting ?? false);

  return (
    <>
      <Slot id="session" label="Interactive session" state={session ? "ok" : "gap"}>
        {session ? session.href ? <a href={session.href}>{session.label}</a> : session.label : "no session linked"}
      </Slot>
      <Slot id="agent_status" label="Autonomous agent" state={agent ? "ok" : "na"}>
        {agent === undefined ? (
          "loading agent info…"
        ) : working ? (
          <>
            <Pill tone="purple">working</Pill>{" "}
            {run && (
              <button type="button" className="agents-toggle" disabled={busy} onClick={() => act(() => stopAgentRun(run.run_id))}>
                Stop
              </button>
            )}
          </>
        ) : starting ? (
          <>
            <Pill tone="purple">starting…</Pill> <span className="muted">claiming the task and starting an agent</span>
          </>
        ) : !agent.has_action ? (
          <span className="muted">nothing for an agent to do</span>
        ) : agent.reason === null ? (
          <>
            <span className="agent-ready">{`may ${TRIGGER_LABEL[agent.trigger ?? "fix_ci"].toLowerCase()} now`}</span>{" "}
            <button type="button" className="agents-toggle" disabled={busy} onClick={() => act(() => startAgentRun(branchId))}>
              {busy ? "Starting…" : "Run an agent now"}
            </button>
          </>
        ) : (
          <>
            <span className="agent-blocked">{`${TRIGGER_LABEL[agent.trigger ?? "fix_ci"]}: ${reasonText(agent.reason)}`}</span>{" "}
            {agent.can_override && (
              <button type="button" className="agents-toggle" disabled={busy} onClick={() => act(() => startAgentRun(branchId))}>
                {busy ? "Starting…" : "Run an agent anyway"}
              </button>
            )}
          </>
        )}
        {agent?.validation.can_validate && !working && !starting && (
          <>
            {" "}
            <button
              type="button"
              className="agents-toggle"
              disabled={busy}
              title="An agent reviews this branch against its parent, before any PR exists"
              onClick={() => act(() => validateTask(branchId))}
            >
              {busy ? "Starting…" : agent.validation.validated ? "Validate again" : "Validate"}
            </button>
          </>
        )}
        {agent && <ValidationNote validation={agent.validation} />}
        {(problem ?? agent?.start_error) && <span className="error">{` ${problem ?? agent?.start_error}`}</span>}
      </Slot>
      <Slot id="agent_runs" label="Number of Autonomous Runs" state={agent && agent.runs > 0 ? "ok" : "na"}>
        {agent === undefined ? "loading agent info…" : agent.runs}
      </Slot>
      <Slot id="agent_run" label="Latest agent run" state={run ? "ok" : "na"}>
        {agent === undefined ? (
          "loading agent info…"
        ) : run === null ? (
          "no run yet"
        ) : (
          <>
            <Pill tone={RUN_TONE[run.state]}>{RUN_TEXT[run.state]}</Pill>
            {` ${TRIGGER_LABEL[run.trigger]}`}
            {run.state === "running" && (
              <span className="muted">
                {" for "}
                <Age text="" since={run.started_at} />
                {`, turn ${run.turns}, ${runCost(run.cost_usd)} so far`}
              </span>
            )}
          </>
        )}
      </Slot>
      <Slot id="agent_run_summary" label="Latest run summary" state={run ? "ok" : "na"}>
        {run ? (
          <>
            {run.error ? (
              <span className="error">{run.error}</span>
            ) : run.summary ? (
              <span className="agent-run-summary">{run.summary}</span>
            ) : (
              <span className="muted">
                {run.state === "running" ? (run.activity ? `Now: ${run.activity}` : "starting up…") : "no summary"}
              </span>
            )}{" "}
            <button type="button" className="agents-toggle" aria-haspopup="dialog" onClick={() => setRunsOpen(true)}>
              Runs
            </button>
            {runsOpen && agent && <RunsDialog branch={run.branch} runs={agent.recent_runs} onClose={closeRuns} />}
          </>
        ) : (
          NO_RUN
        )}
      </Slot>
    </>
  );
}

/** Where the task stands on validate, personal review and publish. */
function ValidationNote({ validation }: { validation: CardValidation }) {
  const { validated, result, opening_pr: opening, notice } = validation;
  return (
    <>
      {opening && (
        <>
          {" "}
          <Pill tone="purple">Opening PR…</Pill>
        </>
      )}
      {notice && <span className="error">{` ${notice}`}</span>}
      {validated && (
        <span className="muted">{` validated (${result === "fail" ? "the agent would ask for changes" : "the agent would approve"}): awaiting your review of its files`}</span>
      )}
    </>
  );
}

export function SpendSlots({
  extras,
  Slot,
}: {
  extras: BranchExtras | undefined;
  Slot: (props: SlotProps) => React.ReactElement;
}) {
  const spend = extras?.spend;
  return (
    <>
      <Slot id="cost" label="Cost" state={spend ? "ok" : "na"}>
        {spend === undefined ? (
          "loading spend…"
        ) : (
          <span title="Interactive sessions on this machine, from their transcripts at list prices, over all recorded spend">
            {formatUsd(spend.interactive_usd)}
            {spend.interactive_unpriced_calls > 0 && <span className="muted">{" (some calls have no price)"}</span>}
          </span>
        )}
      </Slot>
      <Slot id="agent_cost" label="Autonomous agent cost" state={spend ? "ok" : "na"}>
        {spend === undefined
          ? "loading spend…"
          : spend.autonomous_runs === 0
            ? "no runs"
            : `${runCost(spend.autonomous_usd)} over ${spend.autonomous_runs} run${spend.autonomous_runs === 1 ? "" : "s"}`}
      </Slot>
    </>
  );
}
