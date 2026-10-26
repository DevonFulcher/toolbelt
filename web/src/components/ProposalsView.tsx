// The proposals view: work agents propose from the connected systems, for the
// user to approve (a worker picks it up) or reject (with an optional reason the
// proposer learns from). A proposal is never deleted: decided ones are hidden
// until history is shown. The server pushes the list whenever it changes; the
// page asks for it once only when it has not been pushed yet.
import { useCallback, useEffect, useState } from "react";
import { approveProposal, fetchProposals, rejectProposal, writeProposalGuidelines } from "../api/client";
import type { ProposalInfo, ProposalList, ProposalTag } from "../api/generated";
import { formatAbsolute } from "../logic/format";
import type { Counts } from "../store/selectors";
import { useApp, useAppStore } from "../store/StoreContext";
import { FilterBar } from "./FilterBar";
import { Linkified } from "./Linkified";
import "./proposals.css";

const TAG_LABEL: Record<ProposalTag, string> = {
  not_relevant: "Not relevant",
  too_big: "Too big",
  already_done: "Already done",
  wrong_repo: "Wrong repo",
  low_value: "Low value",
  unclear: "Unclear",
};

function useProposals(fetchImpl: typeof fetch = fetch): { list: ProposalList | null; error: string | null } {
  const store = useAppStore();
  const pushed = useApp((s) => s.proposalsView);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    fetchProposals(fetchImpl)
      .then((data) => store.getState().proposalsChanged(data))
      .catch((failure: unknown) => setError(failure instanceof Error ? failure.message : String(failure)));
  }, [fetchImpl, store]);
  useEffect(() => {
    if (store.getState().proposalsView === null) reload();
  }, [reload, store]);
  return { list: pushed, error };
}

export function ProposalsPage({ counts }: { counts: Counts }) {
  const { list, error } = useProposals();
  const [history, setHistory] = useState(false);
  return (
    <>
      <FilterBar counts={counts} />
      {list === null ? (
        <div className="empty">{error ? `Could not load the proposals: ${error}` : "Loading the proposals…"}</div>
      ) : (
        <ProposalsBody list={list} history={history} onHistory={setHistory} />
      )}
    </>
  );
}

function ProposalsBody({ list, history, onHistory }: { list: ProposalList; history: boolean; onHistory: (on: boolean) => void }) {
  const pending = list.proposals.filter((p) => p.status === "pending");
  const decided = list.proposals.filter((p) => p.status !== "pending").reverse();
  return (
    <div className="proposals">
      <div className="proposals-status">
        <div>
          <strong>{`${pending.length} of ${list.max_pending} waiting for you`}</strong>
          <p>{list.proposer}</p>
        </div>
        <label>
          <input type="checkbox" checked={history} onChange={(event) => onHistory(event.target.checked)} />{" "}
          {`Show decided (${decided.length})`}
        </label>
      </div>
      <GuidelinesEditor list={list} />
      {pending.length === 0 && <div className="empty">Nothing waiting for a decision.</div>}
      {pending.map((proposal) => (
        <ProposalCard key={proposal.id} proposal={proposal} tags={list.tags} />
      ))}
      {history && decided.map((proposal) => <ProposalCard key={proposal.id} proposal={proposal} tags={list.tags} />)}
    </div>
  );
}

/** What the proposer is told the user wants (written by the curator from the decisions, or by the user). */
function GuidelinesEditor({ list }: { list: ProposalList }) {
  const store = useAppStore();
  const current = list.guidelines?.body ?? "";
  const [draft, setDraft] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const text = draft ?? current;
  const save = (): void => {
    setSaving(true);
    setProblem(null);
    writeProposalGuidelines(text)
      .then((updated) => {
        store.getState().proposalsChanged(updated);
        setDraft(null);
      })
      .catch((failure: unknown) => setProblem(failure instanceof Error ? failure.message : String(failure)))
      .finally(() => setSaving(false));
  };
  return (
    <details className="proposals-guidelines">
      <summary>What the proposer is told</summary>
      <p>
        {list.guidelines
          ? `Written by ${list.guidelines.author === "user" ? "you" : "the curator"} after ${list.guidelines.decisions_seen} decisions. The curator rewrites it as you decide more proposals; each save is a new version.`
          : "None yet. The curator writes them once enough proposals are decided; you can write them now."}
      </p>
      <label>
        Guidelines
        <textarea rows={6} value={text} onChange={(event) => setDraft(event.target.value)} />
      </label>
      <button type="button" disabled={saving || draft === null} onClick={save}>
        {saving ? "Saving…" : "Save guidelines"}
      </button>
      {problem && <p className="proposal-problem">{problem}</p>}
    </details>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function ProposalCard({ proposal, tags }: { proposal: ProposalInfo; tags: ProposalTag[] }) {
  const { content } = proposal;
  const store = useAppStore();
  const [busy, setBusy] = useState<"approving" | "rejecting" | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [chosen, setChosen] = useState<ProposalTag[]>([]);
  const [problem, setProblem] = useState<string | null>(null);

  // The server's push brings the decided proposal; this updates at once.
  const apply = (updated: ProposalInfo): void => {
    const list = store.getState().proposalsView;
    if (list) store.getState().proposalsChanged({ ...list, proposals: list.proposals.map((p) => (p.id === updated.id ? updated : p)) });
  };
  const run = (kind: "approving" | "rejecting", action: () => Promise<ProposalInfo>): void => {
    setBusy(kind);
    setProblem(null);
    action()
      .then(apply)
      .catch((failure: unknown) => setProblem(failure instanceof Error ? failure.message : String(failure)))
      .finally(() => setBusy(null));
  };
  const decidable = proposal.status === "pending";

  return (
    <article className="proposal" aria-label={content.title}>
      <div className="proposal-head">
        <div>
          <h2>{content.title}</h2>
          <div className="proposal-chips">
            <span className="proposal-chip">
              {proposal.source}
              {" · "}
              <a href={proposal.source_url} target="_blank" rel="noopener noreferrer">
                {proposal.source_ref}
              </a>
            </span>
            <span className="proposal-chip">{content.repo}</span>
            <span className="proposal-chip">{`size ${content.size}`}</span>
            <span className="proposal-chip">{`${content.confidence} confidence`}</span>
            <span className="proposal-chip">{`proposed ${formatAbsolute(Date.parse(proposal.created_at))}`}</span>
          </div>
        </div>
        {decidable && (
          <div className="proposal-actions">
            <button type="button" disabled={busy !== null} onClick={() => setRejecting((on) => !on)}>
              Reject
            </button>
            <button
              type="button"
              className="primary"
              disabled={busy !== null}
              onClick={() => run("approving", () => approveProposal(proposal.id))}
            >
              {busy === "approving" ? "Approving…" : "Approve"}
            </button>
          </div>
        )}
      </div>
      {!decidable && (
        <p className="proposal-decided">
          {proposal.status === "approved" ? "Approved" : "Rejected"}
          {proposal.decided_at ? ` ${formatAbsolute(Date.parse(proposal.decided_at))}` : ""}
          {proposal.decision_tags.length > 0 ? ` · ${proposal.decision_tags.map((t) => TAG_LABEL[t]).join(", ")}` : ""}
          {proposal.decision_reason ? ` — ${proposal.decision_reason}` : ""}
          {proposal.work ? ` · ${proposal.work}` : ""}
        </p>
      )}
      <p>{content.summary}</p>
      <Section title="Problem">
        <p>{content.problem}</p>
      </Section>
      <Section title="Why now">
        <p>{content.why_now}</p>
      </Section>
      {content.evidence.length > 0 && (
        <Section title="Evidence">
          <ul className="proposal-evidence">
            {content.evidence.map((item, index) => (
              <li key={index}>
                <blockquote>{item.quote}</blockquote>
                <cite>
                  {[item.author, item.date].filter(Boolean).join(", ")}
                  {item.url && (
                    <>
                      {item.author || item.date ? " · " : ""}
                      <a href={item.url} target="_blank" rel="noopener noreferrer">
                        source
                      </a>
                    </>
                  )}
                </cite>
              </li>
            ))}
          </ul>
        </Section>
      )}
      <Section title="Approach">
        <p>{content.approach}</p>
      </Section>
      <Section title="Done when">
        <ul>
          {content.acceptance_criteria.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </Section>
      {content.risks.length > 0 && (
        <Section title="Risks and unknowns">
          <ul>
            {content.risks.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </Section>
      )}
      {content.related.length > 0 && (
        <Section title="Related">
          <ul>
            {content.related.map((item) => (
              <li key={item}>
                <Linkified text={item} />
              </li>
            ))}
          </ul>
        </Section>
      )}
      <Section title="If you approve">
        <p>{content.on_approve}</p>
      </Section>
      {rejecting && decidable && (
        <div className="proposal-reject">
          <label>
            Why? (optional; the proposer reads these to propose better)
            <textarea rows={2} value={reason} onChange={(event) => setReason(event.target.value)} />
          </label>
          <div className="proposal-tags">
            {tags.map((tag) => (
              <label key={tag}>
                <input
                  type="checkbox"
                  checked={chosen.includes(tag)}
                  onChange={(event) => setChosen(event.target.checked ? [...chosen, tag] : chosen.filter((t) => t !== tag))}
                />{" "}
                {TAG_LABEL[tag]}
              </label>
            ))}
          </div>
          <div className="proposal-actions">
            <button type="button" disabled={busy !== null} onClick={() => setRejecting(false)}>
              Cancel
            </button>
            <button type="button" className="primary" disabled={busy !== null} onClick={() => run("rejecting", () => rejectProposal(proposal.id, reason, chosen))}>
              {busy === "rejecting" ? "Rejecting…" : "Reject proposal"}
            </button>
          </div>
        </div>
      )}
      {problem && <p className="proposal-problem">{problem}</p>}
    </article>
  );
}
