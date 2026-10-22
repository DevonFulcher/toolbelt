// The agents view: what toolbelt's autonomous agents are on, which tasks one
// may take next, which it may not and why, the limits they run under, and
// their recent runs. The server pushes it whenever it changes; the page asks for
// it once only when it has not been pushed yet, and again after its own actions.
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { fetchAgentConversation, fetchAgents, setAgentsEnabled, setAgentsLaunching, startAgentRun, stopAgentRun } from "../api/client";
import type { Tone, AgentDecision, AgentMessage, AgentReason, AgentRunInfo, AgentTrigger, Agents } from "../api/generated";
import { formatAbsolute } from "../logic/format";
import { formatUsd } from "../logic/spendFormat";
import type { Counts } from "../store/selectors";
import { useApp, useAppStore } from "../store/StoreContext";
import { Dialog } from "./Dialog";
import { FilterBar } from "./FilterBar";
import "./agents.css";

type Load = { state: "ready"; data: Agents } | { state: "error"; message: string };

export const TRIGGER_LABEL: Record<AgentTrigger, string> = {
  resolve_conflicts: "Resolve conflicts",
  resolve_comments: "Resolve comments",
  fix_ci: "Fix / re-run CI",
};

export const RUN_TONE: Record<AgentRunInfo["state"], Tone> = {
  running: "purple",
  succeeded: "green",
  failed: "red",
  timed_out: "amber",
  cap_reached: "amber",
  stopped: "gray",
  abandoned: "gray",
};

export const RUN_TEXT: Record<AgentRunInfo["state"], string> = {
  running: "running",
  succeeded: "finished",
  failed: "failed",
  timed_out: "timed out",
  cap_reached: "hit its cap",
  stopped: "stopped",
  abandoned: "abandoned",
};

/** The agents' state: what the server last pushed, else (once) what it says
 * when asked. ``reload`` asks again, after an action of the user's own. */
export function useAgents(fetchImpl: typeof fetch = fetch): { load: Load | null; reload: () => void } {
  const store = useAppStore();
  const pushed = useApp((s) => s.agentsView);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    fetchAgents(fetchImpl)
      .then((data) => {
        setError(null);
        store.getState().agentsChanged(data);
      })
      .catch((failure: unknown) => setError(failure instanceof Error ? failure.message : String(failure)));
  }, [fetchImpl, store]);
  useEffect(() => {
    if (store.getState().agentsView === null) reload();
  }, [reload, store]);
  const load: Load | null = pushed !== null ? { state: "ready", data: pushed } : error !== null ? { state: "error", message: error } : null;
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

export const when = (iso: string): string => formatAbsolute(Date.parse(iso));

/** A run costs cents or less: keep four digits under a dollar. */
export const runCost = (usd: number): string => (usd < 1 ? `$${usd.toFixed(4)}` : formatUsd(usd));

export function reasonText(reason: AgentReason): string {
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
  const store = useAppStore();
  const pushed = useApp((s) => s.conversations[runId]);
  const [error, setError] = useState<string | null>(null);
  const missing = pushed === undefined;
  // What the server has pushed (a working run's, as it grows) is shown; asked
  // for once when there is none (a run that has ended, or the page opened).
  useEffect(() => {
    if (!missing) return;
    fetchAgentConversation(runId, fetchImpl)
      .then((data) => store.getState().conversationChanged(data))
      .catch((failure: unknown) => setError(failure instanceof Error ? failure.message : String(failure)));
  }, [runId, fetchImpl, missing, store]);
  if (pushed === undefined) {
    return error === null ? (
      <p className="agents-empty">Loading the conversation…</p>
    ) : (
      <p className="agents-empty error">{`Could not load the conversation: ${error}`}</p>
    );
  }
  if (pushed.messages.length === 0) return <p className="agents-empty">Nothing has been said yet.</p>;
  return (
    <ol className="agent-conversation" aria-label="Conversation">
      {pushed.messages.map((message, index) => (
        <Message key={index} message={message} />
      ))}
    </ol>
  );
}

/** The title of a run's conversation dialog. */
export function conversationTitle(run: AgentRunInfo | undefined): string {
  return run ? `Conversation: ${TRIGGER_LABEL[run.trigger]} on ${run.branch}` : "Conversation";
}

function ConversationButton({ runId, onOpen }: { runId: string; onOpen: (runId: string) => void }) {
  return (
    <button type="button" className="agents-toggle" aria-haspopup="dialog" onClick={() => onOpen(runId)}>
      Conversation
    </button>
  );
}

function AgentCard({
  decision,
  runs,
  onStop,
  onStart,
  launching,
  onOpenConversation,
}: {
  decision: AgentDecision;
  runs: AgentRunInfo[];
  onStop: (runId: string) => void;
  onStart: (id: string) => void;
  launching: boolean;
  onOpenConversation: (runId: string) => void;
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
                <button
                  type="button"
                  className="agents-toggle"
                  disabled={decision.starting}
                  onClick={() => onStart(decision.id)}
                >
                  {decision.starting ? "Starting…" : "Run an agent now"}
                </button>
              </>
            )
          ) : (
            <>
              <span className="agent-blocked">{reasonText(decision.reason)}</span>
              {decision.can_override && (
                <button
                  type="button"
                  className="agents-toggle"
                  disabled={decision.starting}
                  onClick={() => onStart(decision.id)}
                >
                  {decision.starting ? "Starting…" : "Run an agent anyway"}
                </button>
              )}
            </>
          )}
          {decision.start_error && <span className="error">{` ${decision.start_error}`}</span>}
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
                {run.state === "running" && run.activity && <span className="agent-run-activity">{run.activity}</span>}
                {run.state === "running" && (
                  <button type="button" className="agents-toggle" onClick={() => onStop(run.run_id)}>
                    Stop
                  </button>
                )}
                <ConversationButton runId={run.run_id} onOpen={onOpenConversation} />
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
  const closeConversation = useCallback(() => setOpenRun(null), []);
  const { limits, spend } = data;
  const working = data.decisions.filter((d) => d.reason?.code === "working");
  const ready = data.decisions.filter((d) => d.reason === null);
  const held = data.decisions.filter((d) => d.reason !== null && d.reason.code !== "working");
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
        {data.ticked_at !== null && (
          <p className="agents-description">{`Tasks last decided ${when(data.ticked_at)} (every few seconds; PRs are looked up again about every 5 minutes).`}</p>
        )}
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
          {working.length > 0 && (
            <section className="agents-section" aria-label="Agents are on these">
              <h2>An agent is on these</h2>
              <div className="agents-cards">
                {working.map((decision) => (
                  <AgentCard
                    key={decision.id}
                    decision={decision}
                    runs={runsFor(decision.id)}
                    onStop={stop}
                    onStart={start}
                    launching={data.launching}
                    onOpenConversation={setOpenRun}
                  />
                ))}
              </div>
            </section>
          )}
          <section className="agents-section" aria-label="Agents may take these">
            <h2>An agent may take these</h2>
            {ready.length === 0 ? (
              <p className="agents-empty">No task has something an agent does and is free for one.</p>
            ) : (
              <div className="agents-cards">
                {ready.map((decision) => (
                  <AgentCard key={decision.id} decision={decision} runs={runsFor(decision.id)} onStop={stop} onStart={start} launching={data.launching} onOpenConversation={setOpenRun} />
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
                  <AgentCard key={decision.id} decision={decision} runs={runsFor(decision.id)} onStop={stop} onStart={start} launching={data.launching} onOpenConversation={setOpenRun} />
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
                <tr key={run.run_id}>
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
                    <ConversationButton runId={run.run_id} onOpen={setOpenRun} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      {openRun !== null && (
        <Dialog title={conversationTitle(data.runs.find((run) => run.run_id === openRun))} onClose={closeConversation}>
          <Conversation runId={openRun} />
        </Dialog>
      )}
    </section>
  );
}
