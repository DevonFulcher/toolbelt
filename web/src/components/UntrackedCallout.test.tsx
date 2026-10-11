import { act, fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import type { UntrackedPrs } from "../api/generated";
import { ago, branch, DAY, NOW, pr, snapshot } from "../testing/fixtures";
import { renderWith } from "../testing/render";
import { App } from "./App";

const loaded: UntrackedPrs = {
  state: "loaded",
  truncated: false,
  hidden: 0,
  prs: [
    {
      repo: "owner/other",
      number: 12,
      title: "Try the thing",
      url: "https://github.com/owner/other/pull/12",
      created_at: ago(3 * DAY),
      is_draft: true,
    },
    {
      repo: "owner/api",
      number: 40,
      title: "Hotfix",
      url: "https://github.com/owner/api/pull/40",
      created_at: ago(2 * 3600_000),
      is_draft: false,
    },
  ],
};

function show(untracked: UntrackedPrs, options: Parameters<typeof renderWith>[2] = {}) {
  return renderWith(
    <App clock={() => NOW} />,
    snapshot([branch(1, "devon/a", { status: pr() })], undefined, { untracked }),
    options,
  );
}

describe("the untracked PRs callout", () => {
  test("lists links only: repo, number, title, age", () => {
    show(loaded);

    const callout = screen.getByRole("region", { name: "Open PRs not tracked by toolbelt" });
    const items = within(callout).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("owner/other");
    expect(items[0]).toHaveTextContent("#12 Try the thing");
    expect(items[0]).toHaveTextContent("draft");
    expect(items[0]).toHaveTextContent("3d");
    expect(within(items[0]).getByRole("link")).toHaveAttribute("href", "https://github.com/owner/other/pull/12");
    expect(items[1]).toHaveTextContent("2h");
    expect(screen.getByRole("button", { name: /Open PRs not tracked/ })).toHaveTextContent("2");
    // No status columns, and the PRs are not branches in the table or cards.
    expect(callout.querySelector(".pill.tone-green, [data-slot]")).toBeNull();
    expect(document.querySelectorAll("article.card")).toHaveLength(1);
  });

  test("is collapsible, and the choice is remembered", () => {
    const { preferences } = show(loaded);
    expect(screen.getAllByRole("listitem").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: /Open PRs not tracked/ }));

    expect(screen.queryByText("#40 Hotfix")).toBeNull();
    expect(preferences.saved.collapsed).toEqual(["untracked"]);
    expect(screen.getByRole("button", { name: /Open PRs not tracked/ })).toHaveAttribute("aria-expanded", "false");
  });

  test("a remembered collapsed callout starts collapsed", () => {
    show(loaded, { collapsed: ["untracked"] });

    expect(screen.queryByText("#40 Hotfix")).toBeNull();
  });

  test("says when it is loading, failed, or empty", () => {
    const { store } = show({ state: "loading" });
    expect(screen.getByText("loading…")).toBeInTheDocument();

    act(() => store.getState().untrackedChanged({ state: "failed", error: "gh: not logged in" }, 1));
    expect(screen.getByText("could not list your open PRs: gh: not logged in")).toBeInTheDocument();

    act(() => store.getState().untrackedChanged({ state: "loaded", prs: [], truncated: false, hidden: 0 }, 2));
    expect(screen.getByText(/every open PR of yours is a tracked branch/)).toBeInTheDocument();

    act(() => store.getState().untrackedChanged({ ...loaded, truncated: true }, 3));
    expect(screen.getByText(/only the first 100/)).toBeInTheDocument();
  });

  test("says how many PRs GitHub would not show", () => {
    show({ ...loaded, hidden: 3 });

    expect(screen.getByText(/3 open PRs could not be read: GitHub denied access/)).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  test("follows the live event", () => {
    const { store } = show({ state: "loaded", prs: [], truncated: false, hidden: 0 });

    act(() => store.getState().untrackedChanged(loaded, 5));

    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });
});
