# How `tt ui` is tested

The UI has four layers of checking. Each catches a different kind of bug, and
the last two exist because the first two **cannot see layout**.

| Layer | Catches | Run it |
| --- | --- | --- |
| 1. Unit and component tests | Logic: next action, ages, timeline geometry, sorting, filtering, grouping, URL state, the state catalog, API payloads | `uv run pytest -q` (runs the web checks too) or `pnpm --dir web test` |
| 2. Guard tests | A rule that has regressed before, derived from the source itself (CSS layout rules, generated types) | same as above |
| 3. The demo (fake `gh`) | What it actually looks like and how it behaves, with every scenario on demand | `tools/ui-demo/run.sh` |
| 4. The real fixture repo | Whether the real GitHub payload parses and means what we think | [`ui-validation-fixture.md`](ui-validation-fixture.md) |

Nothing here calls GitHub or Jira except layer 4, and layer 4 only writes to its
own private repo.

## 1. Unit and component tests

- **Python** (`tests/test_ui*.py`, `test_next_action.py`, `test_pr_*.py`): the
  JSON API and SSE stream through FastAPI's `TestClient`, with the status
  fetcher, the task tracker and the clock **injected**. Nothing is patched and
  nothing sleeps on real time.
- **Web** (`web/src/**/*.test.ts(x)`, Vitest + React Testing Library): pure
  logic in `web/src/logic/` (timeline geometry, focus/cap, layout, sorting) and
  the components, rendered against a store seeded from `web/src/testing/`.
- **The aggregate view** is split by who owns the arithmetic:
  - *Server* (`tests/test_ui_aggregate.py`, `tests/test_pr_link.py`): the
    replay of a PR's history into states (and the CI phases), the status-mix
    **snapshots** (at each UTC task transition every task in flight has one
    state; shares sum to 100%, counts agree, an instant with nothing in
    flight is a gap), the per-PR **milestones** (UTC instants), joining tasks
    to PRs by recorded id (a reused branch name must not mismatch; name
    matches are flagged), the repo filter, and the **incremental fetch** (a
    background loop re-queries open PRs and PRs updated since the newest
    cached `updatedAt`; page loads read the cache, and reopens are
    handled), all with the `gh` page runner (`FakeGitHub`), the stack DB and
    the clock injected. The server does no calendar arithmetic and takes no
    time zone.
  - *Client* (`web/src/logic/localTime.test.ts`, `leadTime.test.ts`,
    `throughput.test.ts`, `aggregateChart.test.ts`, and the component tests in
    `web/src/components/AggregateView.test.tsx`): what a day, a week (Monday
    first) and a weekend are comes only from the browser's `Intl`, with the
    IANA zone injected so tests run in `America/Chicago` and `UTC` (the web
    test environment is `TZ=UTC`). They cover a Friday 19:00 Central merge
    landing on Friday, DST weeks (23/25-hour days), daily vs weekly
    throughput, weekend bands and label thinning, individual merged PRs at
    4 and 12 weeks, medians/p75/p90 per week at 26 and 52 weeks (linear
    interpolation; buckets with no known value are gaps; unknown is counted),
    and **excluding weekends** (elapsed time minus the time on
    Saturday/Sunday in the viewer's zone, no holiday calendar: Friday 17:00 to
    Monday 09:00 is 64h elapsed, 16h without weekends).
- **The finish estimate** (`tests/test_finish_estimate.py`,
  `tests/test_ui_estimate.py`, `web/src/logic/finishEstimate.test.ts`,
  `web/src/components/AggregateView.test.tsx`): see
  [`finish-estimate.md`](finish-estimate.md). Pure functions on fixed synthetic
  histories (exact quantiles, the fallback level, working time across a
  Friday evening, a stacked parent landing late, a regime change, coverage near
  80% on a generated history, tuning), the stage rows from a PR's replay, the
  stored backtest and the card and accuracy payloads, and the client's text.
- **The stack DB** (`tests/test_stack_store.py`,
  `tests/test_stack_store_migrations.py`): the migration list is applied from
  scratch and from every historical `user_version` with rows in place.
  `tests/test_pr_link.py` covers recording a task's PR identity (`tt git pr`,
  sync, merge, branch-clean) and that a recorded link is never replaced.
- **Task claims** (`tests/test_task_claims.py`, `tests/test_ui_claims.py`,
  `web/src/**` claim tests): the claim files' reading (live/expired/idle/
  blocked and stale at an injected clock, ttl-0 tombstones, partial files,
  revocations by `touched_at`), resolving claims to tracked branches (worktree
  list injected, once per read), the `tt task` commands (clock, directories and
  worktree lister injected through `TaskEnvironment`), `gc`'s grace, the
  writer's JSON Schema (`tests/claim_schema/`, validated with a small
  in-test validator), the UI's claims feed (polling gated on a connected
  browser, heartbeats not reported, expiry noticed without a file change,
  git asked only when the claims change), the `claims` SSE event re-deriving a
  branch and its stacked children, `next_action` with a claim (merged/closed
  stays Done; a child of a claimed parent waits on others), and the client's
  store, section and card chips. Nothing sleeps; the claims directory is a
  temp dir.
- **The state catalog** (`src/toolbelt/ui/pr_states.py`) is the single
  declaration of every state a card can show and how the timeline represents
  it. Its exhaustiveness tests fail when a state is added without a timeline
  representation; the generated TypeScript types (`pnpm gen:types`) make the
  renderer's `switch` exhaustive at compile time too.

`uv run pytest -q` also runs `tsc`, ESLint and Vitest (`tests/test_ui_web.py`),
and is skipped for that part when `pnpm` or `web/node_modules` is missing.
`pre-commit` runs the same web checks plus `pnpm check:types`.

## 2. Guard tests

A guard test derives its expectation from the real source, so changing one side
without the other fails:

- `web/src/cardLayout.test.ts` reads `styles.css` and asserts what keeps a
  card from ever being wider than its column: it is a wrapping flex row with no
  width-triggered media query, a group (Git, PR, Task) may shrink
  (`flex-shrink >= 1`, `min-width: 0`), the slots inside a group flow into
  columns capped by the group's own width, and the timeline's full-row rule is
  not outranked by another flex rule. The boxes of a row stretch to the tallest
  (`align-items: stretch`, no `align-self` on a group), and the timeline's
  button row (Expand, Hide commits, ?) wraps, so a narrow, deeply indented card
  is not made wider than its column by its buttons.
- `tests/test_ui_api_types.py` and `pnpm check:types` fail when the generated
  TS types no longer match the pydantic models.

- `web/src/testing/phaseVectors.json` is read by both
  `web/src/logic/phaseVectors.test.ts` (the card timeline's `phaseSpans`) and
  `tests/test_ui_aggregate.py` (the aggregate view's Python replay): the
  draft, waiting, approved, **waiting-on-CI** and CI-failing spans must be the
  same on the card and in the aggregate. CI follows one rule
  (`pr_activity.commit_ci_phases`): a push waits on CI until its check suites
  complete, whatever the outcome (`ciDone` in a vector), and only a failing
  commit then fails, until the next push; without a completion time a failed
  commit fails from its push. Add a case there when a phase rule changes.

When you fix a layout bug, add a guard like these. It is the only automated
protection for CSS, see below.

## Why layout needs layers 3 and 4

jsdom (what Vitest renders into) **does not do layout**: it has no media
queries, container queries, flex/grid sizing or real widths. So these bug
classes pass every unit test and still ship:

- a card wider than its column (the old fixed nine-column grid at 1,120-1,600px)
- a flex rule beaten by a more specific one (timelines only as wide as their
  content)
- labels overlapping or running past the track

Catch them by measuring the real page.

## 3. The demo

```sh
tools/ui-demo/run.sh                  # two repos, 9 branches, every scenario
tools/ui-demo/run.sh --deep 14        # plus a 14-deep stack: depth cap, re-rooting
```

The demo also seeds task claims (a working, an idle, a blocked, an expired
and a revoked holder: see its README); `tt task release <branch> --force`
against `TOOLBELT_HOME=<dir>/home` should move a "With an agent" card within
about a second. Claims have no history, so the aggregate view does not show
them (its notes say so).

`?view=aggregate` shows about 12 weeks of seeded history (about 35 PRs, task
starts recorded for the last 9 weeks only, about two thirds of them with a
recorded PR id and the rest matched by branch name, a reused branch name, two
stacked pairs, check suite completion times and `updatedAt` for the incremental
fetch): see the demo's README.

See [`tools/ui-demo/README.md`](../tools/ui-demo/README.md). It seeds a temp
`TOOLBELT_HOME` and puts a fake `gh` first on `PATH`. To cover a new state, add
a `pr(...)` entry to `SCENARIOS` in `tools/ui-demo/gh` and a row to `STACKS` in
`seed.py`.

### Checking a visual or layout change

1. Run the demo, open the page in a real browser tab (a background tab is
   deliberately paused: "paused (tab hidden)"; bring it to the front).
2. Look at **all four views** (Grouped, Stacked, Table, Aggregate) at several
   widths (the view selector is its own row and must not move between views:
   compare its `getBoundingClientRect()` in each): about 400, 830, 1200, 1500 and 1700px, in light and dark. The card
   has no breakpoint: its Git/PR/Task boxes wrap onto further rows as the width
   shrinks, so look at where they wrap and that the timeline is always the full
   row below them.
3. Use `--deep 14` and re-root to check indentation and the depth cap.
4. Measure instead of eyeballing. In the page's console:

```js
// Anything wider than its card (a group included), and event labels that
// overlap or leave the track.
(() => {
  const hit = (a, b) => a.left < b.right - .5 && b.left < a.right - .5 &&
                        a.top < b.bottom - .5 && b.top < a.bottom - .5;
  const bad = [];
  document.querySelectorAll("article.card").forEach((card) => {
    const c = card.getBoundingClientRect();
    card.querySelectorAll(".group, .g-timeline, .tl-track, .tl, [class*='s-']").forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.right > c.right + 1) bad.push(["overflow", el.className, Math.round(r.right - c.right)]);
    });
    const labels = [...card.querySelectorAll(".tl-label")].map((e) => e.getBoundingClientRect());
    labels.forEach((a, i) => labels.slice(i + 1).forEach((b) => hit(a, b) && bad.push(["labels overlap"])));
  });
  return { viewport: innerWidth, cards: document.querySelectorAll("article.card").length, bad };
})();
```

For the aggregate view use the charts' own selectors (its SVGs are drawn at
the measured width of their container, so a chart wider than its card means
that measurement is wrong):

```js
(() => {
  const bad = [];
  document.querySelectorAll(".agg-chart").forEach((chart) => {
    const c = chart.getBoundingClientRect();
    chart.querySelectorAll("svg, .agg-lead-row, .agg-legend").forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.right > c.right + 1) bad.push([el.tagName, Math.round(r.right - c.right)]);
    });
  });
  return { viewport: innerWidth, docWidth: document.documentElement.scrollWidth, bad };
})();
```

The aggregate's weekend bands, daily labels and lead-time dots follow the
browser's time zone, so check it in a zone that is not UTC (the page states the
zone it used). Throughput is daily at 4 and 12 weeks and weekly beyond.
Lead time shows each merged PR at 4 and 12 weeks and weekly medians beyond;
status mix and who is waiting on keep their stacked charts and mark each
transition time on the x-axis at 4 and 12 weeks.
Reload a 4-week view in the same tab and check that its saved result appears
while the server response is pending.

Also check the empty state (a fresh `TOOLBELT_HOME`, or a `gh` that finds no
PRs: "None of your PRs were updated...", every lead-time step "no data" with
its reason), a window switch (4 / 12 / 26 / 52 weeks: the URL gets `weeks=`)
and a tooltip (hover a chart, or focus it and use the arrow keys).

To check that the boxes of a row are equal-height, compare the heights of the
card's `:scope > .group` elements that share a `top`: they differ by at most
a pixel. In a stacked view with `--deep 8`, `?q=deep` leaves a depth-6 card to
check a narrow width with (400px) against.

`bad` should be empty at every width. A timeline track should also be close to
the card's width (the wrapping layout once left it at 193px in a 1,260px card).

## 4. The real fixture

[`ui-validation-fixture.md`](ui-validation-fixture.md) lists the private
repo's PRs and the state each exercises. Use it when the *GitHub payload* or its
meaning is in question (the parser, `ClosedEvent` on a merge, the partial result
of an unauthorized SAML org), not for layout.

## Known gaps

- **Hidden-tab pause and resume** is covered by unit tests of the connection
  controller and by simulating `visibilityState`, not by a genuinely hidden tab.
- **Reviews from a second user** (approved, changes requested, requested
  reviewer) cannot be produced by one account; those states are covered by the
  fake `gh` only.
- **No automated browser test.** Layout is protected by guard tests and a
  manual measurement, not by Playwright or similar.
- **Keyboard behaviour** is tested by dispatching focus events, not with real
  Tab presses.
