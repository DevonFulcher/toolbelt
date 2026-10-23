import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { DecisionInfo } from "../api/generated";
import { DecisionsDialog } from "./DecisionsDialog";

afterEach(() => {
  vi.unstubAllGlobals();
});

const pending = (over: Partial<DecisionInfo> = {}): DecisionInfo => ({
  id: "d1",
  branch: "devon/a",
  proposer: "interactive",
  session_id: "s-1",
  title: "How to store decisions",
  points: [
    {
      id: "D1",
      title: "Where does it live?",
      context: "The store or a module.",
      options: [
        {
          id: "O1",
          title: "In the store",
          summary: "One table.",
          context: "Reuses the migrations.",
          benefits: "Simple",
          costs: "A migration",
          risks: "",
        },
        { id: "O2", title: "In a module", summary: "", context: "", benefits: "", costs: "", risks: "" },
      ],
      recommendation: { option_id: "O1", reasoning: "It is simpler." },
    },
    { id: "D2", title: "Anything else?", context: "", options: [], recommendation: null },
  ],
  status: "pending",
  answers: [],
  created_at: "2026-10-23T12:00:00+00:00",
  answered_at: null,
  delivered_at: null,
  ...over,
});

/** A fetch serving the list, and recording the answers and deletions sent. */
function serve(list: { current: DecisionInfo[] }, failures: { answer?: string } = {}) {
  const sent: { path: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      if (path.startsWith("/api/decisions?")) {
        return { ok: true, status: 200, json: async () => ({ decisions: list.current }) };
      }
      const body = JSON.parse(String(init?.body)) as { decision_id: string };
      sent.push({ path, body });
      if (path === "/api/decisions/answer" && failures.answer) {
        return { ok: false, status: 409, json: async () => ({ detail: failures.answer }) };
      }
      const target = list.current.find((d) => d.id === body.decision_id) as DecisionInfo;
      if (path === "/api/decisions/delete") list.current = list.current.filter((d) => d.id !== body.decision_id);
      else list.current = list.current.map((d) => (d.id === body.decision_id ? { ...d, status: "answered" as const } : d));
      return { ok: true, status: 200, json: async () => target };
    }),
  );
  return sent;
}

const open = async (session: { label: string; href?: string | null } | null = null) => {
  render(<DecisionsDialog id="1:devon/a" session={session} onClose={() => undefined} />);
  return screen.findByRole("region", { name: "How to store decisions" });
};

describe("the decisions dialog", () => {
  test("shows each point with its options, their summaries and who asked", async () => {
    serve({ current: [pending()] });

    const decision = await open({ label: "Open session", href: "claude://s-1" });

    expect(within(decision).getByText("D1: Where does it live?")).toBeInTheDocument();
    expect(within(decision).getByText("The store or a module.")).toBeInTheDocument();
    expect(within(decision).getByText(/One table\./)).toBeInTheDocument();
    expect(within(decision).getByText(/\(recommended\)/)).toBeInTheDocument();
    expect(within(decision).getByText(/Asked by an interactive session/)).toBeInTheDocument();
    expect(within(decision).getByRole("link", { name: "Open session" })).toHaveAttribute("href", "claude://s-1");
  });

  test("more context on an option is there on request: context, benefits, costs", async () => {
    serve({ current: [pending()] });
    const decision = await open();

    const more = within(decision).getAllByText("More context")[0];
    fireEvent.click(more);

    expect(within(decision).getByText("Reuses the migrations.")).toBeInTheDocument();
    expect(within(decision).getByText("Simple")).toBeInTheDocument();
    expect(within(decision).getByText("A migration")).toBeInTheDocument();
    expect(within(decision).queryByText("Risks")).toBeNull(); // empty fields are left out
  });

  test("the answers cannot be sent until every point has an option or words", async () => {
    serve({ current: [pending()] });
    const decision = await open();
    const send = within(decision).getByRole("button", { name: "Send answers" });
    expect(send).toBeDisabled();

    fireEvent.click(within(decision).getByRole("radio", { name: "O2: In a module" }));
    expect(send).toBeDisabled();
    fireEvent.change(within(decision).getByLabelText("Your words for D2"), { target: { value: "No." } });

    expect(send).toBeEnabled();
  });

  test("sends an option and words for each point, then reloads the list", async () => {
    const list = { current: [pending()] };
    const sent = serve(list);
    const decision = await open();
    fireEvent.click(within(decision).getByRole("radio", { name: "O1: In the store" }));
    fireEvent.change(within(decision).getByLabelText("Your words for D1"), { target: { value: "but check" } });
    fireEvent.change(within(decision).getByLabelText("Your words for D2"), { target: { value: "No." } });

    fireEvent.click(within(decision).getByRole("button", { name: "Send answers" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({
      path: "/api/decisions/answer",
      body: {
        decision_id: "d1",
        answers: [
          { point_id: "D1", option_id: "O1", text: "but check" },
          { point_id: "D2", option_id: null, text: "No." },
        ],
      },
    });
    expect(await screen.findByText("Answered (1)")).toBeInTheDocument();
  });

  test("a refused answer says why and can be sent again", async () => {
    serve({ current: [pending()] }, { answer: "This decision was answered already." });
    const decision = await open();
    fireEvent.click(within(decision).getByRole("radio", { name: "O1: In the store" }));
    fireEvent.change(within(decision).getByLabelText("Your words for D2"), { target: { value: "x" } });

    fireEvent.click(within(decision).getByRole("button", { name: "Send answers" }));

    expect(await within(decision).findByRole("alert")).toHaveTextContent("answered already");
    expect(within(decision).getByRole("button", { name: "Send answers" })).toBeEnabled();
  });

  test("deleting asks first, then removes the question", async () => {
    const list = { current: [pending()] };
    const sent = serve(list);
    const decision = await open();

    fireEvent.click(within(decision).getByRole("button", { name: "Delete" }));
    expect(sent).toEqual([]); // not yet
    fireEvent.click(within(decision).getByRole("button", { name: "Yes, delete" }));

    await waitFor(() => expect(sent).toEqual([{ path: "/api/decisions/delete", body: { decision_id: "d1" } }]));
    expect(await screen.findByText("No question is open for this task.")).toBeInTheDocument();
  });

  test("keeping a question the user was asked about deleting sends nothing", async () => {
    const sent = serve({ current: [pending()] });
    const decision = await open();

    fireEvent.click(within(decision).getByRole("button", { name: "Delete" }));
    fireEvent.click(within(decision).getByRole("button", { name: "Keep" }));

    expect(within(decision).getByRole("button", { name: "Delete" })).toBeInTheDocument();
    expect(sent).toEqual([]);
  });

  test("an autonomous agent's question says the answer resumes its run", async () => {
    serve({ current: [pending({ proposer: "autonomous" })] });

    const decision = await open();

    expect(within(decision).getByText(/resumes its run/)).toBeInTheDocument();
  });

  test("answered questions are listed apart, with whether the agent has read them", async () => {
    serve({
      current: [
        pending({
          status: "answered",
          answers: [
            { point_id: "D1", option_id: "O1", text: "" },
            { point_id: "D2", option_id: null, text: "No." },
          ],
          delivered_at: null,
        }),
      ],
    });
    render(<DecisionsDialog id="1:devon/a" session={null} onClose={() => undefined} />);

    fireEvent.click(await screen.findByText("Answered (1)"));

    expect(screen.getByText(/D1: Where does it live\? — O1: In the store/)).toBeInTheDocument();
    expect(screen.getByText(/“No\.”/)).toBeInTheDocument();
    expect(screen.getByText(/Not read by an agent yet/)).toBeInTheDocument();
  });
});
