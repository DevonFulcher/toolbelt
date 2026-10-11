import { act, fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { ago, branch, catalog, DAY, detail, event, HOUR, MIN, noPr, NOW, pr, snapshot } from "../testing/fixtures";
import { renderWith } from "../testing/render";
import { App } from "./App";

const history = detail({
  created: ago(10 * DAY),
  events: [
    event("opened", 10 * DAY, { detail: "Add the thing" }),
    event("commit", 9 * DAY, { ci: "failed", detail: "a1a1a1a Fix it", actor: "devon" }),
    event("commit", 8 * DAY, { ci: "success", detail: "b2b2b2b Fix it again" }),
    event("review_requested", 6 * DAY, { subject: "sam", detail: "requested review from sam" }),
    event("review", 2 * DAY, { detail: "approved", actor: "sam", subject: "sam" }),
  ],
});

function show() {
  return renderWith(
    <App clock={() => NOW} />,
    snapshot([branch(1, "devon/a", { status: pr({ review: "approved", detail: history }) })]),
  );
}

const track = () => document.querySelector(".tl-track") as HTMLElement;

describe("a card's timeline", () => {
  test("draws prominent events as focusable, labelled markers", () => {
    show();

    const markers = [...track().querySelectorAll<HTMLElement>(".tl-mark")];
    // Opened, the failed commit, the request and the approval; plain commits are faint.
    expect(markers.map((m) => m.textContent)).toEqual(["◆", "●", "▸", "✓"]);
    for (const marker of markers) {
      expect(marker).toHaveAttribute("tabindex", "0");
      expect(marker.getAttribute("aria-label")).toMatch(/ago$/);
    }
    expect(markers[3]).toHaveAttribute("aria-label", "approved (sam) 2d ago");
    expect(screen.getByText("started 10d ago")).toBeInTheDocument();
    expect(screen.getByText("now")).toBeInTheDocument();
  });

  test("shows the state as labelled chips at the right end", () => {
    show();

    const chips = screen.getByLabelText("Current state");
    for (const text of ["PR open", "CI passing", "Review approved", "Comments 0 unresolved", "Merge clean"]) {
      expect(chips).toHaveTextContent(text);
    }
  });

  test("expands to labelled events and a chronological table, and collapses again", () => {
    show();
    expect(document.querySelector(".tl-list")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Expand" }));

    expect(document.querySelector(".tl")).toHaveClass("tl-expanded");
    const rows = [...document.querySelectorAll(".tl-list tbody tr")].map((tr) => tr.children[1].textContent);
    expect(rows).toEqual(["opened", "commit (CI failed)", "commit", "review requested", "approved"]);
    expect(document.querySelector(".tl-list thead")).toHaveTextContent("CI");
    expect(track().querySelector(".tl-label")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Collapse" })).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(screen.getByRole("button", { name: "Hide commits" }));
    expect([...document.querySelectorAll(".tl-list tbody tr")]).toHaveLength(3);

    fireEvent.click(screen.getByRole("button", { name: "Collapse" }));
    expect(document.querySelector(".tl-list")).toBeNull();
  });

  test("clicking the collapsed track expands it", () => {
    show();

    fireEvent.click(track());

    expect(document.querySelector(".tl")).toHaveClass("tl-expanded");
  });

  test("the legend is generated from the catalog and toggles with ?", () => {
    show();
    expect(document.querySelector(".tl-legend")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "?" }));

    const legend = within(document.querySelector(".tl-legend") as HTMLElement);
    for (const marker of Object.values(catalog.events)) {
      expect(legend.getAllByText(marker.label).length).toBeGreaterThan(0);
    }
    for (const phase of Object.values(catalog.phases)) {
      expect(legend.getAllByText(phase.label).length).toBeGreaterThan(0);
    }
    for (const item of catalog.not_represented) {
      expect(legend.getAllByText(item.label).length).toBeGreaterThan(0);
    }
    expect(screen.getByRole("button", { name: "?" })).toHaveAttribute("aria-pressed", "true");
  });

  test("hovering or focusing a marker shows its tooltip, Escape hides it", () => {
    show();
    const approval = screen.getByLabelText("approved (sam) 2d ago");

    fireEvent.mouseEnter(approval);

    const tip = screen.getByRole("tooltip");
    expect(tip).toHaveTextContent("approved");
    expect(tip).toHaveTextContent("Actor");
    expect(tip).toHaveTextContent("sam");
    expect(tip).toHaveTextContent("2d ago");
    expect(approval).toHaveAttribute("aria-describedby", "tl-tip");

    fireEvent.mouseLeave(approval);
    expect(screen.queryByRole("tooltip")).toBeNull();

    fireEvent.focus(approval);
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  test("a shaded span has a tooltip with its duration", () => {
    show();

    const phases = [...track().querySelectorAll<HTMLElement>(".tl-phase")];
    const approved = phases.find((p) => p.classList.contains("tl-phase-approved")) as HTMLElement;
    expect(approved).toHaveAttribute("tabindex", "0");

    fireEvent.focus(approved);

    expect(screen.getByRole("tooltip")).toHaveTextContent("Approved");
    expect(screen.getByRole("tooltip")).toHaveTextContent("Duration");
  });

  test("the expanded state survives a live update of the card", () => {
    const { store } = show();
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));

    act(() => {
      store.getState().statusChanged(
        "1:devon/a",
        pr({
          review: "approved",
          detail: { ...history, events: [...history.events, event("commit", HOUR, { ci: "success" })] },
        }),
        1,
      );
    });

    expect(document.querySelector(".tl")).toHaveClass("tl-expanded");
    expect(document.querySelectorAll(".tl-list tbody tr")).toHaveLength(6);
  });

  test("a minute-old PR still lays out", () => {
    renderWith(
      <App clock={() => NOW} />,
      snapshot([
        branch(1, "devon/a", {
          status: pr({
            detail: detail({
              created: ago(MIN),
              events: [event("opened", MIN)],
              pr_age: { text: "1m", since: ago(MIN) },
            }),
          }),
        }),
      ]),
    );

    expect(screen.getByText("started 1m ago")).toBeInTheDocument();
  });
});

describe("a merged PR", () => {
  test("ends at the merge", () => {
    renderWith(
      <App clock={() => NOW} />,
      snapshot([
        branch(1, "devon/a", {
          status: pr({
            pr: "merged",
            merge: "none",
            next: { label: "Done", who: "none", kind: "done" },
            detail: detail({
              ended: ago(2 * DAY),
              events: [event("opened", 10 * DAY), event("merged", 2 * DAY)],
            }),
          }),
        }),
      ]),
      { search: "?done=1" },
    );

    expect(screen.getByText("merged 2d ago")).toBeInTheDocument();
    expect(track().querySelector(".tl-now")).toBeNull();
  });
});

describe("a task that started before its PR", () => {
  const events = [
    event("commit", 8 * DAY, { ci: "failed", detail: "a1a1a1a Early work" }),
    event("opened", 4 * DAY, { detail: "Add the thing" }),
  ];

  function showStarted() {
    return renderWith(
      <App clock={() => NOW} />,
      snapshot([
        branch(1, "devon/a", {
          started_at: ago(10 * DAY),
          status: pr({ detail: detail({ created: ago(4 * DAY), events }) }),
        }),
      ]),
    );
  }

  test("starts the track at the task and shades the working phase", () => {
    showStarted();

    expect(screen.getByText("started 10d ago")).toBeInTheDocument();
    const markers = [...track().querySelectorAll<HTMLElement>(".tl-mark")];
    expect(markers.map((m) => m.textContent)).toEqual(["◇", "●", "◆"]);
    const working = track().querySelector<HTMLElement>(".tl-phase-working");
    expect(working).toBeTruthy();
    expect(working?.style.left).toBe("0%");
    expect(working?.style.width).toBe("60%");

    fireEvent.focus(working as HTMLElement);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Working (no PR yet)");
  });

  test("lists the start in the expanded table", () => {
    showStarted();

    fireEvent.click(screen.getByRole("button", { name: "Expand" }));

    const rows = [...document.querySelectorAll(".tl-list tbody tr")].map((tr) => tr.children[1].textContent);
    expect(rows).toEqual(["Task started", "commit (CI failed)", "opened"]);
  });

  test("a branch with no PR yet still gets a track", () => {
    renderWith(
      <App clock={() => NOW} />,
      snapshot([branch(1, "devon/a", { started_at: ago(3 * DAY), status: noPr() })]),
    );

    expect(screen.getByText("started 3d ago")).toBeInTheDocument();
    expect(screen.queryByText(/no history to draw/)).toBeNull();
    expect(track().querySelector(".tl-phase-working")).toBeTruthy();
    expect(screen.getByText("now")).toBeInTheDocument();
  });
});
