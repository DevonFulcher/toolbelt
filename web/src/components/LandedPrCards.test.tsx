import { act, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { LandedPr } from "../api/generated";
import { snapshot, NOW } from "../testing/fixtures";
import { renderWith } from "../testing/render";
import { App } from "./App";

const landed: LandedPr = {
  id: "1:1945",
  repo_id: 1,
  repo: "api",
  branch: "devon/retry-the-upload-with-a-very-long-branch-name",
  pr_number: 1945,
  pr_url: "https://github.com/o/r/pull/1945",
  title: "Retry the upload",
  task: "RD-1",
  removed_at: "2026-10-05T16:30:00Z",
  comments: [
    {
      kind: "review",
      author: "copilot-pull-request-reviewer",
      body: "Needs a closer look: the retry never gives up. ".repeat(12),
      url: "https://github.com/o/r/pull/1945#pullrequestreview-1",
    },
  ],
};

afterEach(() => vi.unstubAllGlobals());

describe("a removed branch's merged PR with comments to acknowledge", () => {
  test("is a reduced card with the PR link, the task, the comments and the button", () => {
    renderWith(<App clock={() => NOW} />, snapshot([], undefined, { landed_prs: [landed] }));

    expect(screen.getByRole("link", { name: "#1945" })).toHaveAttribute("href", "https://github.com/o/r/pull/1945");
    expect(screen.getByText(/Retry the upload/)).toBeInTheDocument();
    expect(screen.getByText(/task RD-1/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "copilot-pull-request-reviewer" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Acknowledge" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Remove branch" })).toBeNull();
  });

  test("pressing Acknowledge names the repo, the removed branch and the PR, and says it is working", async () => {
    let answer: (value: { ok: boolean; status: number }) => void = () => {};
    const fetchMock = vi.fn((...args: [string, RequestInit?]) => {
      void args;
      return new Promise((resolve) => {
        answer = resolve as typeof answer;
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWith(<App clock={() => NOW} />, snapshot([], undefined, { landed_prs: [landed] }));

    await act(async () => {
      screen.getByRole("button", { name: "Acknowledge" }).click();
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/acknowledge");
    expect(JSON.parse(String(init?.body))).toEqual({ id: `1:${landed.branch}`, pr_number: 1945 });
    expect(screen.getByRole("button", { name: "Acknowledging…" })).toBeDisabled();
    await act(async () => answer({ ok: true, status: 204 }));
  });

  test("goes away when the server says the list changed", async () => {
    const { store } = renderWith(<App clock={() => NOW} />, snapshot([], undefined, { landed_prs: [landed] }));

    await act(async () => store.getState().landedPrsChanged([], 5));

    expect(screen.queryByRole("button", { name: "Acknowledge" })).toBeNull();
  });
});
