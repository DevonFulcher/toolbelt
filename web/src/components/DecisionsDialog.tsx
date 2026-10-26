// The questions agents asked about a task: read the options (with more context on
// request), choose one or write an answer for each point, and send. A question can
// also be deleted. The answer is stored by toolbelt, so it can be given after the
// session that asked has closed; the agent gets it from there.
import { useCallback, useEffect, useState } from "react";
import { answerDecision, deleteDecision, fetchDecisions } from "../api/client";
import type { DecisionAnswerInfo, DecisionInfo, DecisionOptionInfo, DecisionPointInfo } from "../api/generated";
import { Dialog } from "./Dialog";
import { Linkified } from "./Linkified";

type Draft = Record<string, { option_id: string | null; text: string }>;

const PROPOSER_TEXT = { interactive: "an interactive session", autonomous: "an autonomous agent" } as const;

export function DecisionsDialog({
  id,
  session,
  onClose,
}: {
  id: string;
  /** The task's interactive session (a link to it, when it has one). */
  session: { label: string; href?: string | null } | null;
  onClose: () => void;
}) {
  const [decisions, setDecisions] = useState<DecisionInfo[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const load = useCallback((): void => {
    fetchDecisions(id)
      .then((list) => setDecisions(list.decisions))
      .catch((error: unknown) => setProblem(error instanceof Error ? error.message : String(error)));
  }, [id]);
  useEffect(load, [load]);

  const pending = decisions?.filter((d) => d.status === "pending") ?? [];
  const answered = decisions?.filter((d) => d.status === "answered") ?? [];

  return (
    <Dialog title="Decisions" onClose={onClose}>
      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}
      {decisions === null && !problem && <p className="muted">Loading the questions…</p>}
      {decisions !== null && decisions.length === 0 && <p className="muted">No question is open for this task.</p>}
      {pending.map((decision) => (
        <PendingDecision key={decision.id} decision={decision} session={session} onChanged={load} />
      ))}
      {answered.length > 0 && (
        <details className="decision-answered">
          <summary>{`Answered (${answered.length})`}</summary>
          {answered.map((decision) => (
            <AnsweredDecision key={decision.id} decision={decision} onChanged={load} />
          ))}
        </details>
      )}
    </Dialog>
  );
}

function PendingDecision({
  decision,
  session,
  onChanged,
}: {
  decision: DecisionInfo;
  session: { label: string; href?: string | null } | null;
  onChanged: () => void;
}) {
  const [draft, setDraft] = useState<Draft>(() =>
    Object.fromEntries(decision.points.map((point) => [point.id, { option_id: null, text: "" }])),
  );
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const complete = decision.points.every((point) => draft[point.id].option_id !== null || draft[point.id].text.trim() !== "");

  const send = (): void => {
    const answers: DecisionAnswerInfo[] = decision.points.map((point) => ({
      point_id: point.id,
      option_id: draft[point.id].option_id,
      text: draft[point.id].text.trim(),
    }));
    setSending(true);
    setProblem(null);
    answerDecision(decision.id, answers)
      .then(onChanged)
      .catch((error: unknown) => {
        setProblem(error instanceof Error ? error.message : String(error));
        setSending(false);
      });
  };

  return (
    <section className="decision" aria-label={decision.title}>
      <h3>{decision.title}</h3>
      <p className="muted">
        {`Asked by ${PROPOSER_TEXT[decision.proposer]}`}
        {decision.proposer === "interactive" && session && (
          <>
            {" ("}
            {session.href ? <a href={session.href}>{session.label}</a> : session.label}
            {")"}
          </>
        )}
        {". "}
        {decision.proposer === "autonomous"
          ? "Your answer resumes its run."
          : "Your answer is kept for the session, which reads it (even if it has closed)."}
      </p>
      {decision.points.map((point) => (
        <PointForm
          key={point.id}
          point={point}
          value={draft[point.id]}
          disabled={sending}
          onChange={(value) => setDraft((before) => ({ ...before, [point.id]: value }))}
        />
      ))}
      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}
      <p>
        <button type="button" className="merge-button" disabled={sending || !complete} onClick={send}>
          {sending ? "Sending…" : "Send answers"}
        </button>{" "}
        <DeleteButton decisionId={decision.id} disabled={sending} onDeleted={onChanged} />
      </p>
    </section>
  );
}

function PointForm({
  point,
  value,
  disabled,
  onChange,
}: {
  point: DecisionPointInfo;
  value: { option_id: string | null; text: string };
  disabled: boolean;
  onChange: (value: { option_id: string | null; text: string }) => void;
}) {
  const recommended = point.recommendation;
  return (
    <fieldset className="decision-point" disabled={disabled}>
      <legend>{`${point.id}: ${point.title}`}</legend>
      {point.context && (
        <p className="decision-context">
          <Linkified text={point.context} />
        </p>
      )}
      {point.options.map((option) => (
        <OptionChoice
          key={option.id}
          pointId={point.id}
          option={option}
          recommended={recommended?.option_id === option.id}
          checked={value.option_id === option.id}
          onChoose={() => onChange({ ...value, option_id: option.id })}
        />
      ))}
      {recommended && (
        <details className="decision-recommendation">
          <summary>{`Recommendation${recommended.option_id ? `: ${recommended.option_id}` : ""}`}</summary>
          <p>
            <Linkified text={recommended.reasoning} />
          </p>
        </details>
      )}
      <label className="decision-text">
        <span className="slack-label">{point.options.length > 0 ? "Or in your own words" : "Your answer"}</span>
        <textarea
          rows={2}
          aria-label={`Your words for ${point.id}`}
          value={value.text}
          onChange={(event) => onChange({ ...value, text: event.target.value })}
        />
      </label>
      {value.option_id !== null && (
        <button type="button" className="link-button" onClick={() => onChange({ ...value, option_id: null })}>
          Clear the chosen option
        </button>
      )}
    </fieldset>
  );
}

function OptionChoice({
  pointId,
  option,
  recommended,
  checked,
  onChoose,
}: {
  pointId: string;
  option: DecisionOptionInfo;
  recommended: boolean;
  checked: boolean;
  onChoose: () => void;
}) {
  const more = [
    ["Context", option.context],
    ["Benefits", option.benefits],
    ["Costs", option.costs],
    ["Risks", option.risks],
  ].filter(([, text]) => text !== "");
  return (
    <div className="decision-option">
      <label>
        <input type="radio" name={`${pointId}`} checked={checked} onChange={onChoose} aria-label={`${option.id}: ${option.title}`} />{" "}
        <strong>{`${option.id}: ${option.title}`}</strong>
        {recommended && <span className="decision-recommended">{" (recommended)"}</span>}
        {option.summary && (
          <span className="muted">
            {" — "}
            <Linkified text={option.summary} />
          </span>
        )}
      </label>
      {more.length > 0 && (
        <details>
          <summary>More context</summary>
          <dl className="decision-more">
            {more.map(([label, text]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>
                  <Linkified text={text} />
                </dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </div>
  );
}

function AnsweredDecision({ decision, onChanged }: { decision: DecisionInfo; onChanged: () => void }) {
  return (
    <section className="decision" aria-label={decision.title}>
      <h3>{decision.title}</h3>
      <ul>
        {decision.points.map((point) => {
          const answer = decision.answers.find((a) => a.point_id === point.id);
          const option = point.options.find((o) => o.id === answer?.option_id);
          return (
            <li key={point.id}>
              {`${point.id}: ${point.title} — `}
              {option ? `${option.id}: ${option.title}` : ""}
              {answer?.text ? `${option ? "; " : ""}“${answer.text}”` : ""}
            </li>
          );
        })}
      </ul>
      <p className="muted">
        {decision.delivered_at ? "The agent has read this answer." : "Not read by an agent yet."}{" "}
        <DeleteButton decisionId={decision.id} disabled={false} onDeleted={onChanged} />
      </p>
    </section>
  );
}

function DeleteButton({ decisionId, disabled, onDeleted }: { decisionId: string; disabled: boolean; onDeleted: () => void }) {
  const [state, setState] = useState<"idle" | "asking" | "deleting">("idle");
  const [problem, setProblem] = useState<string | null>(null);
  const remove = (): void => {
    setState("deleting");
    setProblem(null);
    deleteDecision(decisionId)
      .then(onDeleted)
      .catch((error: unknown) => {
        setProblem(error instanceof Error ? error.message : String(error));
        setState("idle");
      });
  };
  return (
    <>
      {state === "idle" && (
        <button type="button" className="agents-toggle" disabled={disabled} onClick={() => setState("asking")}>
          Delete
        </button>
      )}
      {state === "asking" && (
        <>
          <span className="muted">Delete this question for good? </span>
          <button type="button" className="agents-toggle" onClick={remove}>
            Yes, delete
          </button>{" "}
          <button type="button" className="agents-toggle" onClick={() => setState("idle")}>
            Keep
          </button>
        </>
      )}
      {state === "deleting" && (
        <button type="button" className="agents-toggle" disabled>
          Deleting…
        </button>
      )}
      {problem && (
        <span className="error" role="alert">
          {` ${problem}`}
        </span>
      )}
    </>
  );
}
