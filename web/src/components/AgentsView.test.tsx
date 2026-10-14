import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { Agents } from "../api/generated";
import { agents } from "../testing/agentsFixtures";
import { branch, noPr, NOW, snapshot } from "../testing/fixtures";
import { renderWith } from "../testing/render";
import { App } from "./App";

afterEach(() => {
  vi.unstubAllGlobals();
});

function serve(initial: Agents | Error) {
  let current = initial;
  const posts: unknown[] = [];
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
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
    expect(within(mayTake).getByText("An agent may take this now")).toBeInTheDocument();

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
});
