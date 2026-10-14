// The agents view: what toolbelt's autonomous agents are on, which tasks one
// may take next, which it may not and why, the limits they run under, and
// their recent runs. Polled every few seconds while the page is visible.
import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { fetchAgentConversation, fetchAgents, setAgentsEnabled, setAgentsLaunching, startAgentRun, stopAgentRun } from "../api/client";
import type { AgentConversation, AgentDecision, AgentMessage, AgentReason, AgentRunInfo, AgentTrigger, Agents } from "../api/generated";
import { formatAbsolute } from "../logic/format";
import { formatUsd } from "../logic/spendFormat";
import type { Counts } from "../store/selectors";
import { FilterBar } from "./FilterBar";
import "./agents.css";

/** How often the page asks again while it is visible. */
export const REFRESH_MS = 10_000;
/** How often an open conversation of a working run asks again. */
export const CONVERSATION_REFRESH_MS = 3_000;

type Load = { state: "ready"; data: Agents } | { state: "error"; message: string };

export const TRIGGER_LABEL: Record<AgentTrigger, string> = {
  resolve_conflicts: "Resolve conflicts",
  resolve_comments: "Resolve comments",
  fix_ci: "Fix / re-run CI",
};

const RUN_TONE: Record<AgentRunInfo["state"], string> = {
  running: "purple",
  succeeded: "green",
  failed: "red",
  timed_out: "amber",
  cap_reached: "amber",
  stopped: "gray",
  abandoned: "gray",
};

const RUN_TEXT: Record<AgentRunInfo["state"], string> = {
  running: "running",
  succeeded: "finished",
  failed: "failed",
  timed_out: "timed out",
  cap_reached: "hit its cap",
  stopped: "stopped",
  abandoned: "abandoned",
};

export function useAgents(fetchImpl: typeof fetch = fetch): { load: Load | null; reload: () => void } {
  const [load, setLoad] = useState<Load | null>(null);
  const latest = useRef(0);
  const reload = useCallback(() => {
    const id = ++latest.current;
    fetchAgents(fetchImpl)
      .then((data) => {
        if (id === latest.current) setLoad({ state: "ready", data });
      })
      .catch((error: unknown) => {
        if (id === latest.current) setLoad({ state: "error", message: error instanceof Error ? error.message : String(error) });
      });
  }, [fetchImpl]);
  useEffect(() => {
    reload();
    const timer = window.setInterval(() => {
      if (!document.hidden) reload();
    }, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [reload]);
  return { load, reload };
}

export function AgentsPage({ counts }: { counts: Counts }) {
  const { load, reload } = useAgents();
  return (
    <>
      <FilterBar counts={counts} />
      {load === null ? (
        <div className="empty">Loading the agents…</div>
      ) : load.state === "error" ? (
        <div className="empty error">{`Could not load the agents: ${load.message}`}</div>
      ) : (
        <AgentsBody data={load.data} reload={reload} />
      )}
    </>
  );
}

const when = (iso: string): string => formatAbsolute(Date.parse(iso));

/** A run costs cents or less: keep four digits under a dollar. */
const runCost = (usd: number): string => (usd < 1 ? `$${usd.toFixed(4)}` : formatUsd(usd));

function reasonText(reason: AgentReason): string {
  return reason.until ? `${reason.text} Clears ${when(reason.until)}.` : reason.text;
}

function Field({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <div className={`slot s-${id} v-ok`} data-slot={id}>
      <span className="slot-label">{label}</span>
      <span className="slot-value agent-reason">{children}</span>
    </div>
  );
}

function Group({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <div className={`group g-${id}`} role="group" aria-label={title}>
      <div className="group-title">{title}</div>
      <div className="group-slots">{children}</div>
    </div>
  );
}

const MESSAGE_LABEL: Record<AgentMessage["kind"], string> = {
  prompt: "Task",
  text: "Agent",
  tool_use: "Ran",
  tool_result: "Result",
};

function Message({ message }: { message: AgentMessage }) {
  const label = message.kind === "tool_use" && message.tool ? message.tool : MESSAGE_LABEL[message.kind];
  const tone = message.is_error ? " agent-message-error" : "";
  return (
    <li className={`agent-message agent-message-${message.kind}${tone}`}>
      <span className="agent-message-label">{message.is_error ? "Error" : label}</span>
      {message.kind === "text" || message.kind === "prompt" ? (
        <p className="agent-message-text">{message.text}</p>
      ) : (
        <pre className="agent-message-text">{message.text}</pre>
      )}
    </li>
  );
}

/** A run's conversation: what it was asked, what it said, each tool it ran and
 * the output. Asked again every few seconds while the run is working. */
export function Conversation({ runId, fetchImpl = fetch }: { runId: string; fetchImpl?: typeof fetch }) {
  const [load, setLoad] = useState<{ state: "ready"; data: AgentConversation } | { state: "error"; message: string } | null>(null);
  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const read = (): void => {
      fetchAgentConversation(runId, fetchImpl)
        .then((data) => {
          if (stopped) return;
          setLoad({ state: "ready", data });
          if (data.running) timer = window.setTimeout(read, CONVERSATION_REFRESH_MS);
        })
        .catch((error: unknown) => {
          if (!stopped) setLoad({ state: "error", message: error instanceof Error ? error.message : String(error) });
        });
    };
    read();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [runId, fetchImpl]);
  if (load === null) return <p className="agents-empty">Loading the conversation…</p>;
  if (load.state === "error") return <p className="agents-empty error">{`Could not load the conversation: ${load.message}`}</p>;
  if (load.data.messages.length === 0) return <p className="agents-empty">Nothing has been said yet.</p>;
  return (
    <ol className="agent-conversation" aria-label="Conversation">
      {load.data.messages.map((message, index) => (
        <Message key={index} message={message} />
      ))}
    </ol>
  );
}

function ConversationToggle({ runId, open, onToggle }: { runId: string; open: boolean; onToggle: (runId: string) => void }) {
  return (
    <button type="button" className="agents-toggle" aria-expanded={open} onClick={() => onToggle(runId)}>
      {open ? "Hide conversation" : "Conversation"}
    </button>
  );
}

function AgentCard({
  decision,
  runs,
  onStop,
  onStart,
  launching,
  openRun,
  onToggleConversation,
}: {
  decision: AgentDecision;
  runs: AgentRunInfo[];
  onStop: (runId: string) => void;
  onStart: (id: string) => void;
  launching: boolean;
  openRun: string | null;
  onToggleConversation: (runId: string) => void;
}) {
  return (
    <div className="card agent-card" data-branch={decision.branch}>
      <Group id="git" title="Git">
        <Field id="repo" label="Repo">
          <span className="repo-chip">{decision.repo}</span>
        </Field>
        <Field id="branch" label="Branch">
          <span className="branch">{decision.branch}</span>
        </Field>
      </Group>
      <Group id="pr" title="Agent">
        <Field id="next" label="Would do">
          {TRIGGER_LABEL[decision.trigger]}
        </Field>
        <Field id="task" label="Status">
          {decision.reason === null ? (
            launching ? (
              <span className="agent-ready">An agent may take this now</span>
            ) : (
              <>
                <span className="agent-ready">An agent could take this, but Start runs is off</span>
                <button type="button" className="agents-toggle" onClick={() => onStart(decision.id)}>
                  Run an agent now
                </button>
              </>
            )
          ) : (
            <>
              <span className="agent-blocked">{reasonText(decision.reason)}</span>
              {decision.can_override && (
                <button type="button" className="agents-toggle" onClick={() => onStart(decision.id)}>
                  Run an agent anyway
                </button>
              )}
            </>
          )}
        </Field>
      </Group>
      <Group id="task" title="Runs on this task">
        {runs.length === 0 ? (
          <Field id="task" label="Runs">
            none yet
          </Field>
        ) : (
          <ul className="agent-runs s-task">
            {runs.slice(0, 3).map((run) => (
              <li key={run.run_id}>
                <span className={`pill tone-${RUN_TONE[run.state]}`}>{RUN_TEXT[run.state]}</span>
                <span>{`${when(run.started_at)} · ${runCost(run.cost_usd)}`}</span>
                {run.summary && <span className="agent-run-summary">{run.summary}</span>}
                {run.state === "running" && (
                  <button type="button" className="agents-toggle" onClick={() => onStop(run.run_id)}>
                    Stop
                  </button>
                )}
                <ConversationToggle runId={run.run_id} open={openRun === run.run_id} onToggle={onToggleConversation} />
                {openRun === run.run_id && <Conversation runId={run.run_id} />}
              </li>
            ))}
          </ul>
        )}
      </Group>
    </div>
  );
}

function Tile({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="agents-tile">
      <span className="agents-tile-label">{label}</span>
      <strong className="agents-tile-value">{value}</strong>
      {detail && <span className="agents-tile-detail">{detail}</span>}
    </div>
  );
}

function AgentsBody({ data, reload }: { data: Agents; reload: () => void }) {
  const [toggling, setToggling] = useState(false);
  const [toggleError, setToggleError] = useState<string | null>(null);
  const act = (action: () => Promise<void>): void => {
    setToggling(true);
    setToggleError(null);
    action()
      .then(reload)
      .catch((error: unknown) => setToggleError(error instanceof Error ? error.message : String(error)))
      .finally(() => setToggling(false));
  };
  const toggle = (): void => act(() => setAgentsEnabled(!data.enabled));
  const toggleLaunching = (): void => act(() => setAgentsLaunching(!data.launching));
  const stop = (runId: string): void => act(() => stopAgentRun(runId));
  const start = (id: string): void => act(() => startAgentRun(id));
  const [openRun, setOpenRun] = useState<string | null>(null);
  const toggleConversation = (runId: string): void => setOpenRun((open) => (open === runId ? null : runId));
  const { limits, spend } = data;
  const ready = data.decisions.filter((d) => d.reason === null);
  const held = data.decisions.filter((d) => d.reason !== null);
  const runsFor = (id: string): AgentRunInfo[] => data.runs.filter((run) => run.id === id);
  const screen = data.screen === null ? "unknown" : !data.screen.awake ? "asleep" : data.screen.locked ? "locked" : "awake, unlocked";
  const signedIn = data.signed_in === null ? "not checked" : data.signed_in ? "signed in" : "not signed in";

  return (
    <section className="agents" aria-label="Agents">
      <div className="agents-status">
        <div className="agents-status-head">
          <h2>
            {"Autonomous agents are "}
            <span className={data.enabled ? "agents-state-on" : "agents-state-off"}>{data.enabled ? "on" : "off"}</span>
          </h2>
          <button type="button" className="agents-toggle" onClick={toggle} disabled={toggling} aria-pressed={data.enabled}>
            {data.enabled ? "Turn off" : "Turn on"}
          </button>
        </div>
        {toggleError && <div className="notice error">{`That did not work: ${toggleError}`}</div>}
        {data.error && <div className="notice error" role="alert">{`The agents' last pass failed (it tries again): ${data.error}`}</div>}
        {!data.enabled && (
          <p className="agents-description">
            When on, an agent picks up a task as soon as its PR has merge conflicts, unresolved review comments or failing CI,
            but only while the screen is on and unlocked, and within the limits below.
          </p>
        )}
        {data.enabled && (
          <div className="agents-status-head">
            <p className="agents-description">
              {data.launching
                ? "Starting runs: on. An agent starts on the first task below that may be taken, one at a time."
                : "Starting runs: off, so this is a dry run. Tasks are decided every few seconds, but no agent is started: what follows is what would happen."}
            </p>
            <button type="button" className="agents-toggle" onClick={toggleLaunching} disabled={toggling} aria-pressed={data.launching}>
              {data.launching ? "Stop starting runs" : "Start runs"}
            </button>
          </div>
        )}
        {data.blockers.length > 0 && (
          <ul className="agents-blockers" aria-label="Why agents are not running">
            {data.blockers.map((blocker) => (
              <li key={blocker.code}>{reasonText(blocker)}</li>
            ))}
          </ul>
        )}
        {data.enabled && data.blockers.length === 0 && <p className="agents-description">Ready: nothing is stopping the agents.</p>}
      </div>
      <div className="agents-tiles">
        <Tile
          label="Spent in the last 24 hours"
          value={formatUsd(spend.last_24_hours_usd)}
          detail={`of ${formatUsd(limits.global_usd_per_day)} for all agents`}
        />
        <Tile
          label="Spent this month"
          value={formatUsd(spend.this_month_usd)}
          detail={`of ${formatUsd(limits.global_usd_per_month)} for all agents`}
        />
        <Tile
          label="Each run"
          value={`up to ${formatUsd(limits.run_usd)}`}
          detail={`${limits.run_turns} turns · ${Math.round(limits.run_timeout_seconds / 60)} min · ${limits.model}`}
        />
        <Tile label="Screen" value={screen} />
        <Tile label="Claude sign-in" value={signedIn} />
        <Tile
          label="Picks up a task"
          value={`committed to in ${limits.active_working_days} working days`}
          detail={`${limits.task_runs_per_head} runs per commit · ${limits.task_runs_per_day} per day · ${formatUsd(limits.task_usd_per_day)} per day`}
        />
      </div>
      {data.enabled && (
        <>
          <section className="agents-section" aria-label="Agents may take these">
            <h2>An agent may take these</h2>
            {ready.length === 0 ? (
              <p className="agents-empty">No task has something an agent does and is free for one.</p>
            ) : (
              <div className="agents-cards">
                {ready.map((decision) => (
                  <AgentCard key={decision.id} decision={decision} runs={runsFor(decision.id)} onStop={stop} onStart={start} launching={data.launching} openRun={openRun} onToggleConversation={toggleConversation} />
                ))}
              </div>
            )}
          </section>
          <section className="agents-section" aria-label="Agents may not take these">
            <h2>No agent is on these, and why</h2>
            {held.length === 0 ? (
              <p className="agents-empty">Nothing is being held back.</p>
            ) : (
              <div className="agents-cards">
                {held.map((decision) => (
                  <AgentCard key={decision.id} decision={decision} runs={runsFor(decision.id)} onStop={stop} onStart={start} launching={data.launching} openRun={openRun} onToggleConversation={toggleConversation} />
                ))}
              </div>
            )}
          </section>
        </>
      )}
      <section className="agents-table" aria-label="Recent runs">
        <h2>Recent runs</h2>
        {data.runs.length === 0 ? (
          <p className="agents-empty">No agent has run yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Task</th>
                <th>For</th>
                <th>Started</th>
                <th>State</th>
                <th className="num">Turns</th>
                <th className="num">Cost</th>
                <th>What it did</th>
              </tr>
            </thead>
            <tbody>
              {data.runs.map((run) => (
                <Fragment key={run.run_id}>
                <tr>
                  <td>{`${run.repo}: ${run.branch}`}</td>
                  <td>{TRIGGER_LABEL[run.trigger]}</td>
                  <td>{when(run.started_at)}</td>
                  <td>
                    <span className={`pill tone-${RUN_TONE[run.state]}`}>{RUN_TEXT[run.state]}</span>
                  </td>
                  <td className="num">{run.turns}</td>
                  <td className="num">{runCost(run.cost_usd)}</td>
                  <td>
                    {run.error ?? run.summary ?? ""}
                    {run.state === "running" && (
                      <button type="button" className="agents-toggle" onClick={() => stop(run.run_id)}>
                        Stop
                      </button>
                    )}
                    <ConversationToggle runId={run.run_id} open={openRun === run.run_id} onToggle={toggleConversation} />
                  </td>
                </tr>
                {openRun === run.run_id && (
                  <tr>
                    <td colSpan={7}>
                      <Conversation runId={run.run_id} />
                    </td>
                  </tr>
                )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </section>
  );
}
