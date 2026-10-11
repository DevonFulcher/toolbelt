import { act, fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import type { Branch } from "../api/generated";
import { MAX_VISIBLE_DEPTH } from "../logic/focus";
import { branch, NOW, pr, snapshot } from "../testing/fixtures";
import { renderWith } from "../testing/render";
import { App } from "./App";

/** One linear stack l0 <- l1 <- ... <- l(n-1) in repo 1. */
function linear(n: number): Branch[] {
  return Array.from({ length: n }, (_, i) =>
    branch(1, `l${i}`, {
      parent: i ? `l${i - 1}` : null,
      children: i < n - 1 ? [{ id: `1:l${i + 1}`, name: `l${i + 1}` }] : [],
      status: pr({ pr_number: i + 1 }),
    }),
  );
}

function show(branches: Branch[], search = "") {
  return renderWith(<App clock={() => NOW} />, snapshot(branches), { search });
}

const cardNames = (): string[] =>
  [...document.querySelectorAll('.card [data-slot="branch"] .branch')].map((el) => el.textContent ?? "");
const tableNames = (): string[] =>
  [...document.querySelectorAll("tbody tr[data-id] .col-branch .branch")].map((el) => el.textContent ?? "");
const crumbs = (): HTMLElement => screen.getByRole("navigation", { name: "Focused stack" });

describe("the depth cap in the stacked view", () => {
  test("stops indenting at the cap and offers to continue from there", () => {
    show(linear(20), "?view=stacked");

    expect(cardNames()).toEqual(["l0", "l1", "l2", "l3", "l4", "l5", "l6"]);
    expect(MAX_VISIBLE_DEPTH).toBe(6);
    const control = screen.getByRole("link", { name: "13 more levels below — continue from here →" });
    expect(control).toBeInTheDocument();
    expect(screen.getByText("13 branches not shown")).toBeInTheDocument();
    // The control belongs to the last visible card: it follows it.
    const last = document.querySelector('.card[data-id="1:l6"]') as HTMLElement;
    expect(last.nextElementSibling).toContainElement(control);
    // Only one: the cards above the cap have nothing hidden.
    expect(document.querySelectorAll(".cap")).toHaveLength(1);
    // No focus bar until something is focused.
    expect(screen.queryByRole("navigation", { name: "Focused stack" })).toBeNull();
  });

  test("continuing re-roots there, so a stack of any length can be walked", () => {
    const { url } = show(linear(20), "?view=stacked");

    fireEvent.click(screen.getByRole("link", { name: /continue from here/ }));

    expect(url.location.search).toBe("?view=stacked&root=1%3Al6");
    expect(url.pushes).toEqual(["/?view=stacked&root=1%3Al6"]);
    expect(cardNames()).toEqual(["l6", "l7", "l8", "l9", "l10", "l11", "l12"]);
    expect(screen.getByRole("link", { name: "7 more levels below — continue from here →" })).toBeInTheDocument();
    // Depth restarts at the root: it is not indented.
    expect((document.querySelector('.card[data-id="1:l6"]') as HTMLElement).style.getPropertyValue("--depth")).toBe("0");

    fireEvent.click(screen.getByRole("link", { name: /continue from here/ }));
    expect(cardNames()).toEqual(["l12", "l13", "l14", "l15", "l16", "l17", "l18"]);
    fireEvent.click(screen.getByRole("link", { name: "1 more level below — continue from here →" }));
    expect(cardNames()).toEqual(["l18", "l19"]);
    expect(document.querySelector(".cap")).toBeNull();
  });

  test("a branching stack caps each branch and counts what is below", () => {
    // t0 <- t1 <- t2 <- t3 (a short arm) and t0 <- u1 <- u2 <- ... (a long arm)
    const arm = (prefix: string, n: number) =>
      Array.from({ length: n }, (_, i) =>
        branch(1, `${prefix}${i + 1}`, {
          parent: i ? `${prefix}${i}` : "t0",
          status: pr({ pr_number: 100 + i }),
        }),
      );
    show([branch(1, "t0", { status: pr() }), ...arm("s", 2), ...arm("u", 9)], "?view=stacked");

    expect(cardNames()).toEqual(["t0", "s1", "s2", "u1", "u2", "u3", "u4", "u5", "u6"]);
    // u6 sits at depth 6, with u7..u9 below it.
    expect(screen.getAllByRole("link", { name: /continue from here/ })).toHaveLength(1);
    expect(screen.getByRole("link", { name: "3 more levels below — continue from here →" })).toBeInTheDocument();
    expect(screen.getByText("3 branches not shown")).toBeInTheDocument();
  });

  test("rails and indentation stay right up to the cap", () => {
    show(linear(20), "?view=stacked");

    const depth = (id: string) =>
      (document.querySelector(`.card[data-id="${id}"]`) as HTMLElement).style.getPropertyValue("--depth");
    expect(["l0", "l3", "l6"].map((n) => depth(`1:${n}`))).toEqual(["0", "3", "6"]);
    // A rail segment for every level between the root and the last visible card.
    expect(document.querySelectorAll('.card[data-id="1:l6"] .rail-seg')).toHaveLength(1);
  });
});

describe("the way back up", () => {
  test("breadcrumbs link to each ancestor, the root is not a link, and the size is shown", () => {
    show(linear(20), "?view=stacked&root=1%3Al3");

    const bar = within(crumbs());
    expect(bar.getByText("api")).toBeInTheDocument();
    expect(["l0", "l1", "l2"].map((n) => bar.getByRole("link", { name: n }).getAttribute("href"))).toEqual([
      "?view=stacked&root=1%3Al0",
      "?view=stacked&root=1%3Al1",
      "?view=stacked&root=1%3Al2",
    ]);
    expect(bar.getByText("l3", { selector: ".focus-current" })).toHaveAttribute("aria-current", "page");
    expect(bar.getByText("Focused on l3 (17 branches)")).toBeInTheDocument();
    expect(cardNames()[0]).toBe("l3");
  });

  test("clicking an ancestor re-roots there", () => {
    const { url } = show(linear(20), "?view=stacked&root=1%3Al5");

    fireEvent.click(within(crumbs()).getByRole("link", { name: "l2" }));

    expect(cardNames()[0]).toBe("l2");
    expect(url.location.search).toBe("?view=stacked&root=1%3Al2");
  });

  test("a long chain collapses its middle into a menu that lists it", () => {
    show(linear(20), "?view=stacked&root=1%3Al15");

    const bar = within(crumbs());
    const items = [...crumbs().querySelectorAll(".focus-crumbs > li")].map((li) =>
      li.classList.contains("focus-gap") ? "…" : li.textContent,
    );
    expect(items).toEqual(["api", "l0", "…", "l13", "l14", "l15"]);
    const summary = bar.getByText("…");
    expect(summary).toHaveAttribute("title", expect.stringContaining("l1 › l2"));
    const menu = within(crumbs().querySelector(".focus-gap ul") as HTMLElement);
    expect(menu.getAllByRole("link").map((a) => a.textContent)).toEqual(
      Array.from({ length: 12 }, (_, i) => `l${i + 1}`),
    );

    fireEvent.click(menu.getByRole("link", { name: "l7" }));
    expect(cardNames()[0]).toBe("l7");
  });

  test("up one level goes to the parent; from a stack root it shows the whole stack", () => {
    const { url } = show(linear(20), "?view=stacked&root=1%3Al1");

    fireEvent.click(screen.getByRole("button", { name: "↑ Up one level" }));
    expect(url.location.search).toBe("?view=stacked&root=1%3Al0");
    expect(cardNames()[0]).toBe("l0");

    fireEvent.click(screen.getByRole("button", { name: "↑ Up one level" }));
    expect(url.location.search).toBe("?view=stacked");
    expect(screen.queryByRole("navigation", { name: "Focused stack" })).toBeNull();
  });

  test("show whole stack clears the root", () => {
    const { url, store } = show(linear(20), "?view=stacked&root=1%3Al9");

    fireEvent.click(screen.getByRole("button", { name: "Show whole stack" }));

    expect(store.getState().view.root).toBe("");
    expect(url.location.search).toBe("?view=stacked");
    expect(cardNames()[0]).toBe("l0");
  });

  test("the focus root's parent link re-roots to the parent instead of scrolling", () => {
    show(linear(20), "?view=stacked&root=1%3Al4");

    const root = document.querySelector('.card[data-id="1:l4"]') as HTMLElement;
    const parent = within(root).getByRole("link", { name: "l3" });
    expect(parent).toHaveAttribute("data-outside", "1");
    expect(parent).not.toHaveAttribute("data-gone");

    fireEvent.click(parent);

    expect(cardNames()[0]).toBe("l3");
  });

  test("a card inside the view still scrolls to its parent", () => {
    const { url } = show(linear(20), "?view=stacked&root=1%3Al4");

    const card = document.querySelector('.card[data-id="1:l6"]') as HTMLElement;
    fireEvent.click(within(card).getByRole("link", { name: "l5" }));

    expect(cardNames()[0]).toBe("l4");
    expect(url.pushes).toEqual([]);
  });
});

describe("history", () => {
  test("each root change is a history entry; other changes replace the current one", () => {
    const { url, store } = show(linear(20), "?view=stacked");

    fireEvent.click(screen.getByRole("link", { name: /continue from here/ }));
    fireEvent.click(screen.getByRole("link", { name: /continue from here/ }));
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "l1" } });

    expect(url.entries).toEqual([
      "?view=stacked",
      "?view=stacked&root=1%3Al6",
      "?view=stacked&q=l1&root=1%3Al12",
    ]);
    expect(url.pushes).toHaveLength(2);

    // Back steps to the previous root (with the view as it was left there).
    act(() => url.back());
    expect(store.getState().view.root).toBe("1:l6");
    expect(url.location.search).toBe("?view=stacked&root=1%3Al6");
    expect(url.pushes).toHaveLength(2);
    act(() => url.back());
    expect(store.getState().view.root).toBe("");
    expect(screen.queryByRole("navigation", { name: "Focused stack" })).toBeNull();
    expect(cardNames()[0]).toBe("l0");

    act(() => url.forward());
    expect(store.getState().view.root).toBe("1:l6");
    expect(cardNames()[0]).toBe("l6");
    expect(url.entries).toHaveLength(3);
  });

  test("a root that is not a branch is ignored and removed from the URL, without a history entry", () => {
    const { url, store } = show(linear(20), "?view=stacked&root=1%3Agone");

    expect(store.getState().view.root).toBe("");
    expect(url.location.search).toBe("?view=stacked");
    expect(url.pushes).toEqual([]);
    expect(url.entries).toEqual(["?view=stacked"]);
    expect(cardNames()).toHaveLength(7);
    expect(screen.queryByRole("navigation", { name: "Focused stack" })).toBeNull();
  });

  test("a focused branch that disappears from a refresh drops the focus", () => {
    const { store, url } = show(linear(20), "?view=stacked&root=1%3Al8");
    expect(cardNames()[0]).toBe("l8");

    act(() => store.getState().applySnapshot(snapshot(linear(5)), NOW));

    expect(store.getState().view.root).toBe("");
    expect(url.location.search).toBe("?view=stacked");
    expect(url.pushes).toEqual([]);
    expect(cardNames()).toEqual(["l0", "l1", "l2", "l3", "l4"]);
  });
});

describe("the other views", () => {
  test("the grouped view is flat and unaffected: no cap, no focus bar, root is not kept in the URL", () => {
    const { url } = show(linear(20), "?root=1%3Al3");

    expect(cardNames()).toHaveLength(20);
    expect(document.querySelector(".cap")).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Focused stack" })).toBeNull();
    expect(url.location.search).toBe("");
  });

  test("the grouped view's stack chip opens the stacked view rooted at that branch", () => {
    const { url } = show(linear(20));

    const card = document.querySelector('.card[data-id="1:l5"]') as HTMLElement;
    const open = within(card).getByRole("link", { name: "open in stacked view →" });
    expect(open).toHaveAttribute("href", "?view=stacked&root=1%3Al5");
    // A leaf has nothing stacked on it: no link.
    const leaf = document.querySelector('.card[data-id="1:l19"]') as HTMLElement;
    expect(within(leaf).queryByRole("link", { name: "open in stacked view →" })).toBeNull();

    fireEvent.click(open);

    expect(url.location.search).toBe("?view=stacked&root=1%3Al5");
    expect(url.pushes).toHaveLength(1);
    expect(cardNames()[0]).toBe("l5");
    expect(screen.getByRole("navigation", { name: "Focused stack" })).toBeInTheDocument();
    // Back returns to the grouped view.
    act(() => url.back());
    expect(screen.getByRole("button", { name: "Grouped" })).toHaveAttribute("aria-pressed", "true");
    expect(cardNames()).toHaveLength(20);
  });

  test("the stacked view's links are not offered (the stack is already drawn)", () => {
    show(linear(4), "?view=stacked");

    expect(screen.queryByRole("link", { name: "open in stacked view →" })).toBeNull();
  });

  test("the table's tree shows the subtree, caps it, and has the same navigation", () => {
    const { url } = show(linear(20), "?view=table");

    expect(tableNames()).toEqual(["l0", "l1", "l2", "l3", "l4", "l5", "l6"]);
    const capRow = document.querySelector("tr.cap-row") as HTMLElement;
    expect(capRow.previousElementSibling).toHaveAttribute("data-id", "1:l6");

    fireEvent.click(within(capRow).getByRole("link", { name: /continue from here/ }));

    expect(url.location.search).toBe("?view=table&root=1%3Al6");
    expect(tableNames()[0]).toBe("l6");
    expect(within(crumbs()).getByText("Focused on l6 (14 branches)")).toBeInTheDocument();
    expect(document.querySelector('tr[data-id="1:l6"]')).toHaveStyle({ "--depth": "0" });
  });

  test("the flat table is not focused or capped", () => {
    const { url } = show(linear(20), "?view=table&mode=flat&root=1%3Al3");

    expect(tableNames()).toHaveLength(20);
    expect(document.querySelector("tr.cap-row")).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Focused stack" })).toBeNull();
    expect(url.location.search).toBe("?view=table&mode=flat");
  });
});

describe("with filters", () => {
  test("Clear resets the filters but keeps the focus", () => {
    const { store } = show(linear(20), "?view=stacked&root=1%3Al3&q=l5");

    fireEvent.click(screen.getByRole("button", { name: "Clear" }));

    expect(store.getState().view.root).toBe("1:l3");
    expect(store.getState().view.q).toBe("");
    expect(cardNames()[0]).toBe("l3");
  });
});
