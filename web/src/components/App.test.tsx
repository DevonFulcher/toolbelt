import { act, fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import type { Branch } from "../api/generated";
import { branch, detail, noPr, pr, repo, snapshot, NOW } from "../testing/fixtures";
import { renderWith } from "../testing/render";
import { App } from "./App";

const needsMe = pr({ pr_number: 1, ci: "failed", next: { label: "Fix / re-run CI", who: "you", kind: "fix_ci" } });
const waiting = pr({
  pr_number: 2,
  review: "requested",
  open_comments: 2,
  next: { label: "Waiting for review", who: "others", kind: "wait_review" },
});
const merged = pr({
  pr: "merged",
  pr_number: 3,
  merge: "none",
  next: { label: "Done", who: "none", kind: "done" },
});

function show(branches: Branch[], search = "", options: Parameters<typeof renderWith>[2] = {}) {
  return renderWith(<App clock={() => NOW} />, snapshot(branches), { search, ...options });
}

const stack = () => [
  branch(1, "devon/a", { status: needsMe, task: { label: "ABC-1", href: null } }),
  branch(1, "devon/b", { parent: "devon/a", status: waiting }),
  branch(1, "devon/c", { status: noPr() }),
  branch(1, "devon/old", { status: merged }),
];

/** Branch names of the table rows, in order, with their indentation. */
const tableRows = () =>
  [...document.querySelectorAll<HTMLElement>("tbody tr")].map((tr) => ({
    name: tr.querySelector(".col-branch")?.textContent ?? "",
    depth: tr.style.getPropertyValue("--depth"),
    context: tr.classList.contains("context"),
  }));

describe("the table", () => {
  test("lists branches as a tree: depth-first, children indented", () => {
    show(stack(), "?view=table");

    expect(tableRows()).toEqual([
      { name: "devon/a", depth: "0", context: false },
      { name: "devon/b", depth: "1", context: false },
      { name: "devon/c", depth: "0", context: false },
    ]);
    expect(screen.getByRole("button", { name: "Tree" })).toHaveAttribute("aria-pressed", "true");
  });

  test("flat mode drops the indentation", () => {
    show(stack(), "?view=table&mode=flat");

    expect(tableRows().map((r) => r.depth)).toEqual(["0", "0", "0"]);
    expect(screen.getByRole("button", { name: "Flat" })).toHaveAttribute("aria-pressed", "true");
  });

  test("has a separate, labelled column per status", () => {
    show(stack(), "?view=table");

    const headings = [...document.querySelectorAll("thead th")].map((th) => th.textContent);
    expect(headings).toEqual([
      "Repo",
      "Branch",
      "Next",
      "PR",
      "CI",
      "Review",
      "Comments",
      "Merge",
      "Task",
      "Session",
    ]);
    const row = document.querySelector('tr[data-id="1:devon/b"]') as HTMLElement;
    expect(within(row).getByText("Waiting for review")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "#2 open" })).toHaveAttribute(
      "href",
      "https://github.com/o/r/pull/5",
    );
    expect(row.querySelector(".col-comments")).toHaveTextContent("2");
    expect(row.querySelector(".col-review")).toHaveTextContent("requested");
  });

  test("compact folds PR, CI, review, comments and merge into one Status column", () => {
    show(stack(), "?view=table&compact=1");

    const headings = [...document.querySelectorAll("thead th")].map((th) => th.textContent);
    expect(headings).toEqual(["Repo", "Branch", "Next", "Status", "Task", "Session"]);
    const row = document.querySelector('tr[data-id="1:devon/b"]') as HTMLElement;
    const status = row.querySelector(".col-status") as HTMLElement;
    for (const part of ["#2 open", "CI success", "requested", "2 comments"]) {
      expect(status).toHaveTextContent(part);
    }
    expect(screen.getByRole("button", { name: "Compact" })).toHaveAttribute("aria-pressed", "true");
  });

  test("clicking a header sorts, then flips; the address bar follows", () => {
    const { url } = show(stack(), "?view=table&mode=flat");

    fireEvent.click(within(screen.getByRole("columnheader", { name: /^Next/ })).getByRole("button"));
    expect(tableRows().map((r) => r.name)).toEqual(["devon/a", "devon/c", "devon/b"]);
    expect(screen.getByRole("columnheader", { name: /^Next/ })).toHaveAttribute("aria-sort", "ascending");
    expect(url.location.search).toBe("?view=table&mode=flat&sort=next");

    fireEvent.click(within(screen.getByRole("columnheader", { name: /^Next/ })).getByRole("button"));
    expect(tableRows().map((r) => r.name)).toEqual(["devon/b", "devon/a", "devon/c"]);
    expect(url.location.search).toBe("?view=table&mode=flat&sort=next&dir=desc");
  });

  test("a filtered-out parent stays as a dimmed context row", () => {
    show(stack(), "?view=table&review=requested");

    expect(tableRows()).toEqual([
      { name: "devon/a", depth: "0", context: true },
      { name: "devon/b", depth: "1", context: false },
    ]);
  });

  test("shows the task, its tracker title and status, and the session link", () => {
    const { store } = show(
      [
        branch(1, "devon/a", {
          status: needsMe,
          task: { label: "ABC-1", href: "https://tasks.example/abc-1" },
          task_fetchable: true,
          task_live: { state: "loading" },
          agent: { label: "Open session", href: "claude://x" },
        }),
      ],
      "?view=table",
    );

    act(() => {
      store.getState().taskChanged("1:devon/a", { state: "info", title: "Fix the login", status: "In Progress" }, 1);
    });

    const row = document.querySelector('tr[data-id="1:devon/a"]') as HTMLElement;
    expect(row.querySelector(".col-task")).toHaveTextContent("ABC-1 Fix the login In Progress");
    expect(within(row).getByRole("link", { name: "Open session" })).toHaveAttribute("href", "claude://x");
    // The task text is searchable once it is known.
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "login" } });
    expect(tableRows().map((r) => r.name)).toEqual(["devon/a"]);
  });

  test("a loading row says so, a missing checkout is flagged", () => {
    renderWith(
      <App clock={() => NOW} />,
      snapshot([branch(1, "devon/a"), branch(2, "devon/b")], [repo(1, "api"), repo(2, "gone", { path_missing: true })]),
      { search: "?view=table" },
    );

    expect(document.querySelector('tr[data-id="1:devon/a"] .col-pr')).toHaveTextContent("loading…");
    expect(document.querySelector('tr[data-id="2:devon/b"] .col-pr')).toHaveTextContent("repo path missing");
  });
});

describe("the controls and the address bar", () => {
  test("a bare URL is the grouped view and writes nothing", () => {
    const { url } = show(stack());

    expect(screen.getByRole("button", { name: "Grouped" })).toHaveAttribute("aria-pressed", "true");
    expect(url.writes).toEqual([]);
  });

  test("switching view, filtering and including done round-trip through the URL", () => {
    const { url } = show(stack());

    fireEvent.click(screen.getByRole("button", { name: "Table" }));
    expect(url.location.search).toBe("?view=table");
    fireEvent.click(screen.getByRole("button", { name: "Flat" }));
    expect(url.location.search).toBe("?view=table&mode=flat");
    fireEvent.change(screen.getByLabelText("Next"), { target: { value: "you" } });
    expect(url.location.search).toBe("?view=table&mode=flat&next=you");
    // devon/c has no PR yet: opening one is on me too.
    expect(tableRows().map((r) => r.name)).toEqual(["devon/a", "devon/c"]);
    fireEvent.click(screen.getByRole("button", { name: "Include done" }));
    expect(url.location.search).toBe("?view=table&mode=flat&next=you&done=1");
    fireEvent.click(screen.getByRole("button", { name: "Stacked" }));
    // Table-only parameters stay out of a card view's URL.
    expect(url.location.search).toBe("?view=stacked&next=you&done=1");
    fireEvent.click(screen.getByRole("button", { name: "Grouped" }));
    expect(url.location.search).toBe("?next=you&done=1");
  });

  test("Clear resets the filters but keeps the view", () => {
    const { url } = show(stack(), "?view=stacked&next=you&q=abc&done=1&sort=ci");

    fireEvent.click(screen.getByRole("button", { name: "Clear" }));

    expect(url.location.search).toBe("?view=stacked");
    expect(screen.getByRole("searchbox")).toHaveValue("");
  });

  test("the controls reflect the URL they were opened with", () => {
    show(stack(), "?view=stacked&q=auth&next=you&repo=1&done=1&sort=ci&dir=desc");

    expect(screen.getByRole("searchbox")).toHaveValue("auth");
    expect(screen.getByLabelText("Next")).toHaveValue("you");
    expect(screen.getByLabelText("Repo")).toHaveValue("1");
    expect(screen.getByLabelText("Sort")).toHaveValue("ci");
    expect(screen.getByRole("button", { name: "Include done" })).toHaveAttribute("aria-pressed", "true");
  });

  test("a stale or retired URL is rewritten to the state it was understood as", () => {
    const { url } = show(stack(), "?view=timeline&kind=review&next=you");

    expect(url.location.search).toBe("?next=you");
    expect(url.writes).toEqual(["/?next=you"]);
  });

  test("the compact choice is remembered and applied to a bare table URL", () => {
    const first = show(stack(), "?view=table");
    fireEvent.click(screen.getByRole("button", { name: "Compact" }));
    expect(first.preferences.saved.compact).toBe("1");
    first.unmount();

    show(stack(), "?view=table", { storedCompact: "1" });

    expect(screen.getByRole("button", { name: "Compact" })).toHaveAttribute("aria-pressed", "true");
  });

  test("the Next filter reads in plain words", () => {
    show(stack());

    const options = within(screen.getByLabelText("Next")).getAllByRole("option");

    expect(options.map((o) => o.textContent)).toEqual(["Any", "Needs me", "Waiting on CI", "Waiting on others", "Done"]);
  });

  test("counts say how many branches match and how many done ones are hidden", () => {
    show(stack());

    expect(screen.getByText("3 of 4 branches (1 done hidden)")).toBeInTheDocument();
  });

  test("the Done section appears when done branches are included", () => {
    show(stack(), "?done=1");

    expect(screen.getByRole("button", { name: /Done/ })).toBeInTheDocument();
    expect(screen.getByText("4 of 4 branches")).toBeInTheDocument();
  });
});

describe("the shell", () => {
  test("says there are no tracked stacks", () => {
    renderWith(<App clock={() => NOW} />, snapshot([]));

    expect(screen.getByText(/No tracked stacks/)).toBeInTheDocument();
  });

  test("says it is loading, and what went wrong", () => {
    const { store } = renderWith(<App clock={() => NOW} />, null);
    expect(screen.getByText("Loading…")).toBeInTheDocument();

    act(() => store.getState().setSnapshotError("Error: HTTP 500"));

    expect(screen.getByText("Could not load the stacks: Error: HTTP 500")).toBeInTheDocument();
  });

  test("the live indicator shows the connection and how fresh the data is", () => {
    const { store } = show(stack());
    expect(screen.getByRole("status")).toHaveTextContent("connecting…");

    act(() => store.getState().hello(0, 12));
    expect(screen.getByRole("status")).toHaveAttribute("data-state", "live");
    expect(screen.getByRole("status").textContent).toMatch(/^live · updated (\d+s|just now)/);

    act(() => store.getState().setConnection("paused"));
    expect(screen.getByRole("status")).toHaveTextContent("paused (tab hidden)");
  });

  test("a current branch is marked", () => {
    show([branch(1, "devon/a", { status: needsMe, current: true }), branch(1, "devon/b", { status: waiting })]);

    expect(document.querySelectorAll(".branch.current")).toHaveLength(1);
    expect(document.querySelector(".branch.current")).toHaveTextContent("devon/a");
  });

  test("a PR's ages tick from their start, not from the server's text", () => {
    show([
      branch(1, "devon/a", {
        status: pr({
          detail: detail({ pr_age: { text: "5d", since: new Date(NOW - 3 * 3600_000).toISOString() } }),
        }),
      }),
    ]);

    expect(document.querySelector('[data-slot="pr_age"] time')).toHaveTextContent("3h");
  });
});
