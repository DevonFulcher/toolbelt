import { act, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, test } from "vitest";
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

    const markers = [...track().querySelectorAll<HTMLElement>(".tl-mark:not(.tl-faint)")];
    // Opened, the failed commit, the request and the approval; plain commits are faint dots.
    expect(markers.map((m) => m.firstChild?.textContent)).toEqual(["◆", "●", "▸", "✓"]);
    expect(track().querySelectorAll(".tl-faint")).toHaveLength(1);
    for (const marker of markers) {
      expect(marker).toHaveAttribute("tabindex", "0");
      expect(marker.getAttribute("aria-label")).toMatch(/ago$/);
    }
    expect(markers[3]).toHaveAttribute("aria-label", "approved (sam) 2d ago");
    expect(screen.getByText("started 10d ago")).toBeInTheDocument();
    expect(screen.getByText("now")).toBeInTheDocument();
  });

  test("a label grows toward the roomier side of the track and knows how much room it has", () => {
    show();

    const labels = [...track().querySelectorAll<HTMLElement>(".tl-label")];

    expect(labels.length).toBeGreaterThan(0);
    for (const label of labels) {
      const x = Number(label.style.getPropertyValue("--x"));
      expect(x).toBeGreaterThanOrEqual(0);
      expect(x).toBeLessThanOrEqual(1);
      // Past the middle the label sits left of its marker, so it can never
      // run off the right end of the track (the CSS caps its width by --x).
      expect(label.classList.contains("tl-flip"), label.textContent ?? "").toBe(x > 0.5);
    }
  });

  test("there is one track: always labelled, with no chip row repeating the slots above", () => {
    show();

    expect(document.querySelector(".tl-chips")).toBeNull();
    expect(document.querySelector(".tl-state-row")).toBeNull();
    expect(screen.queryByText("State now")).toBeNull();
    expect(track().querySelectorAll(".tl-label").length).toBeGreaterThan(0);
    expect(track().querySelectorAll(".tl-tick").length).toBeGreaterThan(0);
    expect(track().querySelector(".tl-faint")).toBeTruthy();
    // The log is not part of the track: closed until asked for.
    expect(document.querySelector(".tl-list")).toBeNull();
  });

  test("the end cap's tooltip lists the current states with the time", () => {
    show();
    const cap = track().querySelector<HTMLElement>(".tl-now") as HTMLElement;
    expect(cap).toHaveAttribute("tabindex", "0");

    fireEvent.mouseEnter(cap);

    const tip = screen.getByRole("tooltip");
    expect(tip).toHaveTextContent("Now");
    expect(tip).toHaveTextContent("Ago");
    for (const text of ["PR: open", "CI: passing", "Review: approved", "Comments: 0 unresolved", "Merge: clean"]) {
      expect(tip).toHaveTextContent(text);
    }
  });

  test("Expand shows the chronological log under the track, Collapse hides it again", () => {
    show();
    expect(document.querySelector(".tl-list")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Expand" }));

    const rows = [...document.querySelectorAll(".tl-list tbody tr")].map((tr) => tr.children[1].textContent);
    expect(rows).toEqual(["opened", "commit (CI failed)", "commit", "review requested", "approved"]);
    expect(document.querySelector(".tl-list thead")).toHaveTextContent("CI");
    expect(screen.getByRole("button", { name: "Collapse" })).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(screen.getByRole("button", { name: "Hide commits" }));
    expect([...document.querySelectorAll(".tl-list tbody tr")]).toHaveLength(3);
    expect(track().querySelector(".tl-faint")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Collapse" }));
    expect(document.querySelector(".tl-list")).toBeNull();
  });

  test("Hide commits is there with the log closed, since commit dots are always drawn", () => {
    show();
    const commitDots = () => track().querySelectorAll(".tl-faint").length;
    expect(document.querySelector(".tl-list")).toBeNull();
    expect(commitDots()).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "Hide commits" }));

    expect(screen.getByRole("button", { name: "Show commits" })).toHaveAttribute("aria-pressed", "true");
    expect(commitDots()).toBe(0);
    // The choice is the card's: it survives opening the log.
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    expect(screen.getByRole("button", { name: "Show commits" })).toBeInTheDocument();
    expect([...document.querySelectorAll(".tl-list tbody tr")]).toHaveLength(3);
  });

  test("whether the log is open is remembered per card", () => {
    const first = show();
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    expect(first.preferences.saved.logOpen).toEqual(["1:devon/a"]);
    first.unmount();

    renderWith(
      <App clock={() => NOW} />,
      snapshot([branch(1, "devon/a", { status: pr({ review: "approved", detail: history }) })]),
      { logOpen: ["1:devon/a"] },
    );

    expect(document.querySelector(".tl-list")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Collapse" })).toBeInTheDocument();
  });

  test("another card's remembered log does not open this one", () => {
    renderWith(
      <App clock={() => NOW} />,
      snapshot([branch(1, "devon/a", { status: pr({ review: "approved", detail: history }) })]),
      { logOpen: ["2:devon/a", "1:devon/other"] },
    );

    expect(document.querySelector(".tl-list")).toBeNull();
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
    expect(legend.getByText(/^State now \(listed in the tooltip of the track's right end/)).toBeInTheDocument();
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

  test("the open log survives a live update of the card", () => {
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
    const cap = track().querySelector(".tl-end-cap") as HTMLElement;
    fireEvent.mouseEnter(cap);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Merged");
    expect(screen.getByRole("tooltip")).toHaveTextContent("PR: merged");
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
    expect(markers.map((m) => m.firstChild?.textContent)).toEqual(["◇", "●", "◆"]);
    const working = track().querySelector<HTMLElement>(".tl-phase-working");
    expect(working).toBeTruthy();
    expect(working?.style.left).toBe("0%");
    expect(working?.style.width).toBe("60%");

    fireEvent.focus(working as HTMLElement);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Working (no PR yet)");
  });

  test("lists the start in the log", () => {
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

describe("the weekday lines and weekend bands", () => {
  // The viewer's timezone is the process's: fix it so the calendar is known.
  beforeAll(() => {
    process.env.TZ = "America/Chicago";
  });
  afterEach(() => {
    // jsdom lays nothing out: tests that need a width define one on the prototype.
    delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth;
  });

  const trackWidth = (px: number) =>
    Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => px });

  function showDays(days: number) {
    const long = detail({ created: ago(days * DAY), events: [event("opened", days * DAY)] });
    return renderWith(
      <App clock={() => NOW} />,
      snapshot([branch(1, "devon/a", { status: pr({ detail: long }) })]),
    );
  }
  const lines = () => [...document.querySelectorAll<HTMLElement>(".tl-daygrid .tl-day")];
  const bands = () => document.querySelectorAll(".tl-daygrid .tl-weekend");

  test("every midnight gets a line (labelled with its weekday) and each weekend a band", () => {
    trackWidth(600);
    showDays(10);

    // Sat 10 Oct 07:00 to Tue 20 Oct 07:00 (Chicago): midnights of the 11th to the 20th.
    expect(lines().map((l) => l.dataset.weekday)).toEqual([
      "Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "Mon", "Tue",
    ]);
    expect(bands()).toHaveLength(2);
    expect([...document.querySelectorAll(".tl-day-label")].map((l) => l.textContent)).toEqual([
      "Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "Mon", "Tue",
    ]);
  });

  test("the grid never takes the pointer and is hidden from assistive tech", () => {
    showDays(10);
    const grid = document.querySelector(".tl-daygrid") as HTMLElement;

    expect(grid).toHaveAttribute("aria-hidden", "true");
    expect(grid.closest(".tl-track")).not.toBeNull();
    // The markers and phases are siblings drawn after it, so they stay on top.
    expect(grid.nextElementSibling).toHaveClass("tl-axis");
  });

  test("crowded: only Mondays keep a line, weekends keep their bands", () => {
    trackWidth(200);
    showDays(21);

    expect(lines().map((l) => l.dataset.weekday)).toEqual(["Mon", "Mon", "Mon"]);
    expect(bands().length).toBeGreaterThanOrEqual(3);
  });

  test("very crowded: only the weekend bands", () => {
    trackWidth(200);
    showDays(60);

    expect(lines()).toHaveLength(0);
    expect(bands().length).toBeGreaterThanOrEqual(8);
  });

  test("initials where three letters would collide", () => {
    trackWidth(150);
    showDays(8);

    // 150px over 8 days is 19px a day.
    expect(lines().length).toBeGreaterThan(0);
    for (const label of document.querySelectorAll(".tl-day-label")) {
      expect(label.textContent).toHaveLength(1);
    }
  });

  test("the legend explains them as plain decoration", () => {
    showDays(10);
    fireEvent.click(screen.getByRole("button", { name: "?" }));

    const legend = within(document.querySelector(".tl-legend") as HTMLElement);
    expect(legend.getByText("Days (decoration)")).toBeInTheDocument();
    expect(legend.getByText("Weekday line")).toBeInTheDocument();
    expect(legend.getByText("Weekend")).toBeInTheDocument();
  });
});
