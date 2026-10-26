import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { ProposalInfo, ProposalList } from "../api/generated";
import { branch, noPr, NOW, snapshot } from "../testing/fixtures";
import { renderWith } from "../testing/render";
import { App } from "./App";

afterEach(() => {
  vi.unstubAllGlobals();
});

const proposal = (id: string, over: Partial<ProposalInfo> = {}): ProposalInfo => ({
  id,
  source: "jira",
  source_ref: `TB-${id}`,
  source_url: `https://jira.example/${id}`,
  content: {
    title: `Proposal ${id}`,
    summary: "A summary.",
    problem: "The problem.",
    why_now: "Now because.",
    evidence: [{ quote: "a quote", url: "https://x.example", author: "Devon", date: "2026-10-01" }],
    approach: "The approach.",
    acceptance_criteria: ["Done when this"],
    risks: ["A risk"],
    size: "S",
    confidence: "high",
    repo: "toolbelt",
    related: [],
    on_approve: "A worker takes it.",
  },
  status: "pending",
  created_at: "2026-10-25T12:00:00+00:00",
  decided_at: null,
  decision_reason: "",
  decision_tags: [],
  task_key: null,
  branch: null,
  work: null,
  ...over,
});

const list = (proposals: ProposalInfo[]): ProposalList => ({
  proposals,
  max_pending: 5,
  proposer: "Next check in 2 hours.",
  tags: ["not_relevant", "too_big", "already_done", "wrong_repo", "low_value", "unclear"],
  guidelines: null,
});

function serve(initial: ProposalList) {
  const posts: { url: string; body: Record<string, unknown> }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as { proposal_id: string };
        posts.push({ url, body });
        if (url.endsWith("guidelines")) return { ok: true, status: 200, json: async () => initial };
        const target = initial.proposals.find((p) => p.id === body.proposal_id) as ProposalInfo;
        return { ok: true, status: 200, json: async () => ({ ...target, status: url.endsWith("approve") ? "approved" : "rejected" }) };
      }
      return { ok: true, status: 200, json: async () => initial };
    }),
  );
  return posts;
}

const show = () => renderWith(<App clock={() => NOW} />, snapshot([branch(1, "devon/a", { status: noPr() })]), { search: "?view=proposals" });

describe("the proposals view", () => {
  test("shows each pending proposal in full and how many wait", async () => {
    serve(list([proposal("1"), proposal("2")]));
    show();

    expect(await screen.findByText("2 of 5 waiting for you")).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Proposal 1" })).toBeInTheDocument();
    expect(screen.getAllByText("Now because.")).toHaveLength(2);
    expect(screen.getAllByText("a quote")).toHaveLength(2);
  });

  test("approving a proposal sends the approval and hides it from the waiting ones", async () => {
    const posts = serve(list([proposal("1"), proposal("2")]));
    show();
    const card = await screen.findByRole("article", { name: "Proposal 1" });

    fireEvent.click(card.querySelector("button.primary") as HTMLElement);

    await waitFor(() => expect(screen.getByText("1 of 5 waiting for you")).toBeInTheDocument());
    expect(posts).toEqual([{ url: "/api/proposals/approve", body: { proposal_id: "1" } }]);
  });

  test("rejecting sends the reason and tags", async () => {
    const posts = serve(list([proposal("1")]));
    show();
    const card = await screen.findByRole("article", { name: "Proposal 1" });

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    fireEvent.change(card.querySelector("textarea") as HTMLElement, { target: { value: "not worth it" } });
    fireEvent.click(screen.getByLabelText("Too big"));
    fireEvent.click(screen.getByRole("button", { name: "Reject proposal" }));

    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({
      url: "/api/proposals/reject",
      body: { proposal_id: "1", reason: "not worth it", tags: ["too_big"] },
    });
  });

  test("saves the guidelines the proposer is given as a new version", async () => {
    const posts = serve(list([proposal("1")]));
    show();
    await screen.findByText("1 of 5 waiting for you");

    fireEvent.click(screen.getByText("What the proposer is told"));
    fireEvent.change(screen.getByLabelText("Guidelines"), { target: { value: "- Prefer small fixes" } });
    fireEvent.click(screen.getByRole("button", { name: "Save guidelines" }));

    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].url).toBe("/api/proposals/guidelines");
    expect(posts[0].body).toEqual({ body: "- Prefer small fixes" });
  });

  test("decided proposals are hidden until history is shown", async () => {
    serve(list([proposal("1"), proposal("2", { status: "rejected", decision_reason: "nope", decision_tags: ["low_value"] })]));
    show();

    await screen.findByText("1 of 5 waiting for you");
    expect(screen.queryByRole("article", { name: "Proposal 2" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Show decided (1)"));

    expect(screen.getByRole("article", { name: "Proposal 2" })).toBeInTheDocument();
    expect(screen.getByText(/Low value — nope/)).toBeInTheDocument();
  });
});
