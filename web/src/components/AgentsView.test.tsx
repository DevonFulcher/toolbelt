import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { AgentConversation, Agents } from "../api/generated";
import { agents } from "../testing/agentsFixtures";
import { branch, noPr, NOW, snapshot } from "../testing/fixtures";
import { renderWith } from "../testing/render";
import { App } from "./App";

afterEach(() => {
  vi.unstubAllGlobals();
});

function serve(initial: Agents | Error, extra: { conversation?: AgentConversation; refuseStart?: string } = {}) {
  let current = initial;
  const posts: unknown[] = [];
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "POST" && _url === "/api/agents/start" && extra.refuseStart) {
      posts.push({ url: _url, ...JSON.parse(String(init.body)) });
      return { ok: false, status: 409, json: async () => ({ detail: extra.refuseStart }) };
    }
    if (_url.endsWith("/conversation")) return { ok: true, status: 200, json: async () => extra.conversation };
    if (init?.method === "POST") {
      const body = init.body ? (JSON.parse(String(init.body)) as { enabled?: boolean; launching?: boolean }) : {};
      posts.push({ url: _url, ...body });
      if (!(current instanceof Error)) {
        if (body.enabled !== undefined) current = { ...current, enabled: body.enabled };
        if (body.launching !== undefined) current = { ...current, launching: body.launching, dry_run: !body.launching };
      }
      return { ok: true, status: 200, json: async () => body };
    }
    if (current instanceof Error) return { ok: false, status: 502, json: async () => ({}) };
    return { ok: true, status: 200, json: async () => current };
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, posts };
}

function show(search = "?view=agents") {
  return renderWith(<App clock={() => NOW} />, snapshot([branch(1, "devon/a", { status: noPr() })]), { search });
}

describe("the agents view", () => {
  test("says agents are off, with a switch, and lists no tasks", async () => {
    serve(agents({ enabled: false, dry_run: false, decisions: [], blockers: [{ code: "disabled", text: "Autonomous agents are switched off.", until: null }] }));
    show();

    expect(await screen.findByRole("heading", { name: /Autonomous agents are off/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Turn on" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Agents may take these" })).not.toBeInTheDocument();
  });

  test("on, in a dry run, lists what an agent may take and what is held back, with the reason", async () => {
    serve(agents());
    show();
    await screen.findByRole("heading", { name: /Autonomous agents are on/ });

    expect(screen.getByText(/this is a dry run/)).toBeInTheDocument();
    const mayTake = screen.getByRole("region", { name: "Agents may take these" });
    expect(within(mayTake).getByText("devon/a")).toBeInTheDocument();
    expect(within(mayTake).getByText("Fix / re-run CI")).toBeInTheDocument();
    expect(within(mayTake).getByText("An agent could take this, but Start runs is off")).toBeInTheDocument();

    const held = screen.getByRole("region", { name: "Agents may not take these" });
    expect(within(held).getByText("A person or an agent session holds this task.")).toBeInTheDocument();
    expect(within(held).getByText(/The last run on this task ended recently\. Clears/)).toBeInTheDocument();
    expect(within(held).getByText("Resolve conflicts")).toBeInTheDocument();
  });

  test("a draft PR is under the held-back tasks, with the reason", async () => {
    const base = agents();
    serve(
      agents({
        decisions: [
          {
            id: "1:devon/d",
            repo: "api",
            branch: "devon/d",
            trigger: "fix_ci",
            reason: { code: "draft", text: "The PR is a draft: agents leave drafts to you.", until: null },
            decided_at: base.decisions[0].decided_at,
            can_override: true, starting: false, start_error: null,
          },
        ],
      }),
    );
    show();

    const held = await screen.findByRole("region", { name: "Agents may not take these" });
    expect(within(held).getByText("devon/d")).toBeInTheDocument();
    expect(within(held).getByText("The PR is a draft: agents leave drafts to you.")).toBeInTheDocument();
    const mayTake = screen.getByRole("region", { name: "Agents may take these" });
    expect(within(mayTake).queryByText("devon/d")).not.toBeInTheDocument();
  });

  test("shows every reason all agents are paused, in the banner", async () => {
    serve(
      agents({
        blockers: [
          { code: "screen_locked", text: "The screen is locked.", until: null },
          { code: "auth", text: "Claude is not signed in: run `claude auth login`.", until: null },
        ],
        screen: { awake: true, locked: true },
        signed_in: false,
      }),
    );
    show();

    const banner = await screen.findByRole("list", { name: "Why agents are not running" });
    expect(within(banner).getByText("The screen is locked.")).toBeInTheDocument();
    expect(within(banner).getByText(/not signed in/)).toBeInTheDocument();
    expect(screen.getByText("locked")).toBeInTheDocument();
    expect(screen.getByText("not signed in")).toBeInTheDocument();
  });

  test("shows spend against the global caps and the limits each run has", async () => {
    serve(agents());
    show();
    await screen.findByText("$0.42");

    expect(screen.getByText("of $3.00 for all agents")).toBeInTheDocument();
    expect(screen.getByText("$4.50")).toBeInTheDocument();
    expect(screen.getByText("of $30.00 for all agents")).toBeInTheDocument();
    expect(screen.getByText("up to $0.25")).toBeInTheDocument();
    expect(screen.getByText("40 turns · 15 min · claude-haiku-5-5")).toBeInTheDocument();
    expect(screen.getByText("committed to in 3 working days")).toBeInTheDocument();
  });

  test("lists recent runs, and the latest ones on a task's card", async () => {
    serve(agents());
    show();

    const table = await screen.findByRole("region", { name: "Recent runs" });
    expect(within(table).getByText("api: devon/c")).toBeInTheDocument();
    expect(within(table).getByText("finished")).toBeInTheDocument();
    expect(within(table).getByText("$0.0831")).toBeInTheDocument();
    expect(within(table).getByText("Replied to two Copilot threads and resolved them.")).toBeInTheDocument();
    const held = screen.getByRole("region", { name: "Agents may not take these" });
    expect(within(held).getByText("Replied to two Copilot threads and resolved them.")).toBeInTheDocument();
  });

  test("the switch posts the new value and shows the result", async () => {
    const { posts } = serve(agents({ enabled: false, dry_run: false, decisions: [] }));
    show();

    fireEvent.click(await screen.findByRole("button", { name: "Turn on" }));

    await waitFor(() => expect(posts).toEqual([{ url: "/api/agents/enabled", enabled: true }]));
    expect(await screen.findByRole("heading", { name: /Autonomous agents are on/ })).toBeInTheDocument();
  });

  test("a dry run says so, and the second switch lets the agents start runs", async () => {
    const { posts } = serve(agents());
    show();

    expect(await screen.findByText(/Starting runs: off, so this is a dry run/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Start runs" }));

    await waitFor(() => expect(posts).toEqual([{ url: "/api/agents/launching", launching: true }]));
    expect(await screen.findByText(/Starting runs: on/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop starting runs" })).toBeInTheDocument();
  });

  test("a working run can be stopped from the table", async () => {
    const running = agents().runs[0];
    const { posts } = serve(agents({ runs: [{ ...running, state: "running", ended_at: null, summary: null }] }));
    show();

    const table = await screen.findByRole("region", { name: "Recent runs" });
    fireEvent.click(within(table).getByRole("button", { name: "Stop" }));

    await waitFor(() => expect(posts).toEqual([{ url: "/api/agents/runs/r1/stop" }]));
  });

  test("says when the dispatcher's last pass failed", async () => {
    serve(agents({ error: "RuntimeError: gh is down" }));
    show();

    expect(await screen.findByRole("alert")).toHaveTextContent("RuntimeError: gh is down");
  });

  test("a failed load says so", async () => {
    serve(new Error("down"));
    show();

    expect(await screen.findByText(/Could not load the agents: \/api\/agents: HTTP 502/)).toBeInTheDocument();
  });

  test("is reachable from the view selector and keeps the URL in step", async () => {
    serve(agents());
    const { url } = show("");

    fireEvent.click(screen.getByRole("button", { name: "Agents" }));
    await screen.findByRole("heading", { name: /Autonomous agents are on/ });

    await waitFor(() => expect(url.location.search).toBe("?view=agents"));
  });

  test("a held-back task a person can override has a button that starts a run on it", async () => {
    const base = agents();
    const draft = { ...base.decisions[0], id: "1:devon/d", branch: "devon/d", reason: { code: "draft" as const, text: "The PR is a draft: agents leave drafts to you.", until: null }, can_override: true, starting: false, start_error: null };
    const claimed = { ...base.decisions[1], can_override: false, starting: false, start_error: null };
    const { posts } = serve(agents({ decisions: [draft, claimed] }));
    show();

    const held = await screen.findByRole("region", { name: "Agents may not take these" });
    expect(within(held).getAllByRole("button", { name: "Run an agent anyway" })).toHaveLength(1);
    fireEvent.click(within(held).getByRole("button", { name: "Run an agent anyway" }));

    await waitFor(() => expect(posts).toEqual([{ url: "/api/agents/start", id: "1:devon/d" }]));
  });

  test("when a reason still stops a hand start, it says which", async () => {
    const base = agents();
    const draft = { ...base.decisions[0], reason: { code: "draft" as const, text: "The PR is a draft.", until: null }, can_override: true, starting: false, start_error: null };
    serve(agents({ decisions: [draft] }), { refuseStart: "A person or an agent session holds this task." });
    show();

    fireEvent.click(await screen.findByRole("button", { name: "Run an agent anyway" }));

    expect(await screen.findByText("That did not work: A person or an agent session holds this task.")).toBeInTheDocument();
  });

  test("a run's conversation opens in a dialog and shows what the agent said and ran", async () => {
    const conversation: AgentConversation = {
      run_id: "r1",
      running: false,
      messages: [
        { kind: "prompt", at: "t", text: "Please fix CI.", tool: null, is_error: false },
        { kind: "text", at: "t", text: "Looking at the failing check.", tool: null, is_error: false },
        { kind: "tool_use", at: "t", text: "gh pr checks 1", tool: "Bash", is_error: false },
        { kind: "tool_result", at: "t", text: "lint failed", tool: null, is_error: true },
      ],
    };
    const { fetchMock } = serve(agents(), { conversation });
    show();

    const table = await screen.findByRole("region", { name: "Recent runs" });
    fireEvent.click(within(table).getByRole("button", { name: "Conversation" }));

    const dialog = await screen.findByRole("dialog", { name: "Conversation: Resolve comments on devon/c" });
    const log = await within(dialog).findByRole("list", { name: "Conversation" });
    expect(within(log).getByText("Looking at the failing check.")).toBeInTheDocument();
    expect(within(log).getByText("gh pr checks 1")).toBeInTheDocument();
    expect(within(log).getByText("Bash")).toBeInTheDocument();
    expect(within(log).getByText("lint failed")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/agents/runs/r1/conversation", expect.anything());

    expect(within(table).queryByRole("list", { name: "Conversation" })).not.toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  test("the conversation dialog also closes with Escape and a click on the page behind it", async () => {
    serve(agents(), { conversation: { run_id: "r1", running: false, messages: [] } });
    show();
    const open = async () => {
      const table = await screen.findByRole("region", { name: "Recent runs" });
      fireEvent.click(within(table).getByRole("button", { name: "Conversation" }));
      return screen.findByRole("dialog");
    };

    const first = await open();
    fireEvent.mouseDown(first); // a click inside the dialog keeps it open
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    const second = await open();
    fireEvent.mouseDown(second.parentElement as HTMLElement); // the dimmed page behind
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  test("a conversation with nothing yet says so", async () => {
    serve(agents(), { conversation: { run_id: "r1", running: false, messages: [] } });
    show();

    const table = await screen.findByRole("region", { name: "Recent runs" });
    fireEvent.click(within(table).getByRole("button", { name: "Conversation" }));

    expect(await within(await screen.findByRole("dialog")).findByText("Nothing has been said yet.")).toBeInTheDocument();
  });

  test("with Start runs on, a task nothing holds back is taken by an agent without a button", async () => {
    serve(agents({ launching: true, dry_run: false }));
    show();

    const mayTake = await screen.findByRole("region", { name: "Agents may take these" });
    expect(within(mayTake).getByText("An agent may take this now")).toBeInTheDocument();
    expect(within(mayTake).queryByRole("button", { name: "Run an agent now" })).not.toBeInTheDocument();
  });

  test("with Start runs off, a task an agent could take has a button to run one now", async () => {
    const { posts } = serve(agents());
    show();

    const mayTake = await screen.findByRole("region", { name: "Agents may take these" });
    fireEvent.click(within(mayTake).getByRole("button", { name: "Run an agent now" }));

    await waitFor(() => expect(posts).toEqual([{ url: "/api/agents/start", id: "1:devon/a" }]));
  });

  test("a task an agent is working on is under its own heading, not the held-back ones", async () => {
    const base = agents({ launching: true, dry_run: false });
    const decision = { ...base.decisions[1], reason: { code: "working" as const, text: "An agent is working on this now.", until: null } };
    const run = { ...base.runs[0], id: decision.id, state: "running" as const, ended_at: null, summary: null };
    serve({ ...base, decisions: [decision], runs: [run] });
    show();

    const on = await screen.findByRole("region", { name: "Agents are on these" });
    expect(within(on).getByText("devon/b")).toBeInTheDocument();
    expect(within(on).getByText("An agent is working on this now.")).toBeInTheDocument();
    expect(within(on).getByRole("button", { name: "Stop" })).toBeInTheDocument();
    const held = screen.getByRole("region", { name: "Agents may not take these" });
    expect(within(held).queryByText("devon/b")).not.toBeInTheDocument();
  });

  test("says when the tasks were last decided", async () => {
    serve(agents());
    show();

    expect(await screen.findByText(/Tasks last decided .*every few seconds/)).toBeInTheDocument();
  });

  test("uses what the server pushed without asking, shows each push, and never polls", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { fetchMock } = serve(agents());
      const { store } = renderWith(
        <App clock={() => NOW} />,
        snapshot([branch(1, "devon/a", { status: noPr() })], undefined, { agents: agents() }),
        { search: "?view=agents" },
      );
      await screen.findByRole("heading", { name: /Autonomous agents are on/ });

      act(() => store.getState().agentsChanged(agents({ enabled: false, decisions: [] })));
      expect(screen.getByRole("heading", { name: /Autonomous agents are off/ })).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(120_000);
      });

      expect(fetchMock).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  test("asks once when nothing was pushed, then does not ask again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { fetchMock } = serve(agents());
      show();
      await screen.findByRole("heading", { name: /Autonomous agents are on/ });

      await act(async () => {
        await vi.advanceTimersByTimeAsync(120_000);
      });

      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  test("a working run's conversation is asked for when opened, then grows with what is pushed", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const running = agents().runs[0];
      const conversation = (texts: string[]) => ({
        run_id: "r1",
        running: true,
        messages: texts.map((text) => ({ kind: "text" as const, at: "t", text, tool: null, is_error: false })),
      });
      const { fetchMock } = serve(agents({ runs: [{ ...running, state: "running", ended_at: null, summary: null }] }), {
        conversation: conversation(["first"]),
      });
      const { store } = show();
      const table = await screen.findByRole("region", { name: "Recent runs" });

      fireEvent.click(within(table).getByRole("button", { name: "Conversation" }));
      const dialog = await screen.findByRole("dialog");
      expect(await within(dialog).findByText("first")).toBeInTheDocument();
      const asked = () => fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/conversation")).length;
      const askedAfterOpening = asked();
      act(() => store.getState().conversationChanged(conversation(["first", "second"])));
      expect(within(dialog).getByText("second")).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });

      expect(asked()).toBe(askedAfterOpening);
      expect(askedAfterOpening).toBe(1); // one dialog, one request
    } finally {
      vi.useRealTimers();
    }
  });
});
