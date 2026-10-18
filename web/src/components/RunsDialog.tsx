// A branch's autonomous-agent runs in a dialog: the runs on the left, newest
// first, and what the selected one cost and did, with its conversation, on the
// right. Opened by the Runs button of a card.
import { useState } from "react";
import type { AgentRunInfo } from "../api/generated";
import { Conversation, RUN_TEXT, RUN_TONE, TRIGGER_LABEL, runCost, when } from "./AgentsView";
import { Dialog } from "./Dialog";
import { Pill } from "./presentation";

export function RunsDialog({ branch, runs, onClose }: { branch: string; runs: AgentRunInfo[]; onClose: () => void }) {
  const [selected, setSelected] = useState<string | null>(null);
  // The latest run until another is picked; the list is pushed, so a picked run that
  // is no longer listed falls back to the latest too.
  const run = runs.find((candidate) => candidate.run_id === selected) ?? runs[0];

  return (
    <Dialog title={`Agent runs: ${branch}`} onClose={onClose}>
      <div className="runs">
        <ul className="runs-list" aria-label="Runs">
          {runs.map((candidate) => (
            <li key={candidate.run_id}>
              <button
                type="button"
                className="run-item"
                aria-current={candidate.run_id === run.run_id}
                onClick={() => setSelected(candidate.run_id)}
              >
                <span className="run-item-what">
                  <Pill tone={RUN_TONE[candidate.state]}>{RUN_TEXT[candidate.state]}</Pill>
                  {` ${TRIGGER_LABEL[candidate.trigger]}`}
                </span>
                <span className="muted">{`${when(candidate.started_at)} · ${runCost(candidate.cost_usd)}`}</span>
              </button>
            </li>
          ))}
        </ul>
        <section className="run-detail" aria-label="Run">
          <dl className="run-facts">
            <Fact label="State">
              <Pill tone={RUN_TONE[run.state]}>{RUN_TEXT[run.state]}</Pill>
            </Fact>
            <Fact label="What it did">{TRIGGER_LABEL[run.trigger]}</Fact>
            <Fact label="Started">{when(run.started_at)}</Fact>
            <Fact label="Finished">{run.ended_at ? when(run.ended_at) : "not yet"}</Fact>
            <Fact label="Cost">{runCost(run.cost_usd)}</Fact>
            <Fact label="Turns">{String(run.turns)}</Fact>
            <Fact label="Model">{run.model}</Fact>
          </dl>
          {run.error ? (
            <p className="error">{run.error}</p>
          ) : run.summary ? (
            <p className="agent-run-summary">{run.summary}</p>
          ) : null}
          <h3>Conversation</h3>
          <Conversation key={run.run_id} runId={run.run_id} />
        </section>
      </div>
    </Dialog>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="run-fact">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}
