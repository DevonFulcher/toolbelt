import { act, fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import type { Branch, LiveStatus } from "../api/generated";
import { branch, noPr, pr, repo, snapshot, NOW } from "../testing/fixtures";
import { renderWith } from "../testing/render";
import { App } from "./App";

const needsMe = pr({ pr_number: 1 }); // "Request review", who: you
const waiting = pr({
  pr_number: 2,
  review: "requested",
  next: { label: "Waiting for review", who: "others", kind: "wait_review" },
});
const running = pr({
  pr_number: 3,
  ci: "running",
  next: { label: "Waiting on CI", who: "ci", kind: "wait_ci" },
});
const merged = pr({
  pr: "merged",
  pr_number: 4,
  merge: "none",
  review: "approved",
  next: { label: "Done", who: "none", kind: "done" },
});

function show(branches: Branch[], search = "", options: Parameters<typeof renderWith>[2] = {}) {
  return renderWith(<App clock={() => NOW} />, snapshot(branches), { search, ...options });
}

const sectionKeys = () =>
  [...document.querySelectorAll<HTMLElement>(".cards-section")].map((el) => el.dataset.key);

const namesIn = (key: string): string[] => {
  const section = [...document.querySelectorAll<HTMLElement>(".cards-section")].find((el) => el.dataset.key === key);
  return [...(section?.querySelectorAll('.card [data-slot="branch"] .branch, .card-stub > .branch') ?? [])].map(
    (el) => el.textContent ?? "",
  );
};

describe("the grouped view", () => {
  const branches = () => [
    branch(1, "devon/a", { status: waiting }),
    branch(1, "devon/b", { status: needsMe }),
    branch(1, "devon/c", { status: running }),
    branch(1, "devon/d", { status: needsMe }),
  ];

  test("is the default and puts what needs me first", () => {
    show(branches());

    expect(sectionKeys()).toEqual(["you", "ci", "others"]);
    expect(namesIn("you")).toEqual(["devon/b", "devon/d"]);
    expect(screen.getByRole("button", { name: /Needs me/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "Grouped" })).toHaveAttribute("aria-pressed", "true");
  });

  test("sections collapse, and the choice is remembered", () => {
    const { preferences } = show(branches());

    fireEvent.click(screen.getByRole("button", { name: /Needs me/ }));

    expect(namesIn("you")).toEqual([]);
    expect(screen.getByRole("button", { name: /Needs me/ })).toHaveAttribute("aria-expanded", "false");
    expect(preferences.saved.collapsed).toEqual(["you"]);
    fireEvent.click(screen.getByRole("button", { name: /Needs me/ }));
    expect(namesIn("you")).toEqual(["devon/b", "devon/d"]);
  });

  test("a remembered collapsed section starts collapsed", () => {
    show(branches(), "", { collapsed: ["ci"] });

    expect(namesIn("ci")).toEqual([]);
    expect(namesIn("you")).toHaveLength(2);
  });

  test("shows a slim stub for a finished parent above its children", () => {
    show([
      branch(1, "devon/a", { status: merged }),
      branch(1, "devon/b", { parent: "devon/a", status: needsMe }),
    ]);

    expect(namesIn("you")).toEqual(["devon/a", "devon/b"]);
    const stub = document.querySelector(".card-stub") as HTMLElement;
    expect(stub).toHaveTextContent("devon/a");
    expect(stub).toHaveTextContent("#4 merged");
    expect(stub).toHaveTextContent("kept as context for the stack below");
    // The stub is not a card and not counted.
    expect(document.querySelectorAll("article.card")).toHaveLength(1);
    expect(screen.getByRole("button", { name: /Needs me/ })).toHaveTextContent("1");
  });

  test("a card whose next action changed moves to its new section and flashes", () => {
    const { store } = show(branches());
    expect(namesIn("you")).toContain("devon/b");

    act(() => {
      store.getState().statusChanged("1:devon/b", running, 1);
    });

    expect(namesIn("you")).not.toContain("devon/b");
    expect(namesIn("ci")).toContain("devon/b");
    const card = document.querySelector<HTMLElement>('article[data-id="1:devon/b"]');
    expect(card).toHaveClass("flash");
    // Cards that did not move do not flash.
    expect(document.querySelector('article[data-id="1:devon/d"]')).not.toHaveClass("flash");
  });

  test("a child can open its stack from the branch slot", () => {
    const { url } = show([
      branch(1, "devon/a", { status: waiting }),
      branch(1, "devon/b", { parent: "devon/a", status: needsMe }),
    ]);
    const card = document.querySelector('article[data-id="1:devon/b"]') as HTMLElement;
    fireEvent.click(within(card).getByRole("link", { name: "open in stacked view →" }));
    expect(url.location.search).toBe("?view=stacked&root=1%3Adevon%2Fa");
    expect(document.querySelector('article[data-id="1:devon/b"]')).toHaveClass("highlight");
  });

  test("a branch whose status is not in yet is under Loading", () => {
    show([branch(1, "devon/a"), branch(1, "devon/b", { status: needsMe })]);

    expect(sectionKeys()).toEqual(["you", "loading"]);
  });
});

describe("the stacked view", () => {
  const stack = () => [
    branch(1, "devon/a", { status: needsMe, children: [{ id: "1:devon/b", name: "devon/b" }, { id: "1:devon/c", name: "devon/c" }], stack_pos: 1, stack_height: 2 }),
    branch(1, "devon/b", { parent: "devon/a", status: waiting }),
    branch(1, "devon/c", { parent: "devon/a", status: running }),
  ];

  test("groups by repo, indents by depth and draws rails to the parents", () => {
    show(stack(), "?view=stacked");

    expect(sectionKeys()).toEqual(["repo:1"]);
    expect(document.querySelector(".cards")).toHaveClass("cards-stacked");
    const b = document.querySelector<HTMLElement>('article[data-id="1:devon/b"]') as HTMLElement;
    const c = document.querySelector<HTMLElement>('article[data-id="1:devon/c"]') as HTMLElement;
    expect(b.style.getPropertyValue("--depth")).toBe("1");
    expect(b.querySelector(".rail-seg")).toHaveAttribute("data-kind", "tee");
    expect(c.querySelector(".rail-seg")).toHaveAttribute("data-kind", "corner");
    expect(document.querySelector('article[data-id="1:devon/a"] .card-rail')).toBeNull();
    expect(screen.getByRole("button", { name: "Stacked" })).toHaveAttribute("aria-pressed", "true");
  });

  test("shows separate ancestor and descendant counts", () => {
    show(stack(), "?view=stacked");

    const a = document.querySelector('article[data-id="1:devon/a"]') as HTMLElement;
    expect(a.querySelector('[data-slot="ancestors"]')).toHaveTextContent("0");
    expect(a.querySelector('[data-slot="descendants"]')).toHaveTextContent("2");
    const b = document.querySelector('article[data-id="1:devon/b"]') as HTMLElement;
    expect(b.querySelector('[data-slot="ancestors"]')).toHaveTextContent("1");
    expect(b.querySelector('[data-slot="descendants"]')).toHaveTextContent("0");
  });

  test("keeps a finished parent as a stub", () => {
    show(
      [
        branch(1, "devon/a", { status: merged }),
        branch(1, "devon/b", { parent: "devon/a", status: needsMe, depth: 1 }),
      ],
      "?view=stacked",
    );

    expect(namesIn("repo:1")).toEqual(["devon/a", "devon/b"]);
    expect(document.querySelector(".card-stub")).toBeTruthy();
  });
});

test("no branches match the filters is said, not left blank", () => {
  const statuses: LiveStatus[] = [needsMe, noPr()];
  show(statuses.map((status, i) => branch(1, `devon/${i}`, { status })), "?q=zzz");

  expect(screen.getByText("No branches match the current filters.")).toBeInTheDocument();
});

test("repos are sections in the stacked view, named", () => {
  renderWith(
    <App clock={() => NOW} />,
    snapshot([branch(1, "devon/a", { status: needsMe }), branch(2, "devon/x", { status: needsMe })], [
      repo(1, "alpha"),
      repo(2, "beta"),
    ]),
    { search: "?view=stacked" },
  );

  expect(screen.getByRole("button", { name: /alpha/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /beta/ })).toBeInTheDocument();
});
