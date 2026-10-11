# How `tt ui` is tested

The UI has four layers of checking. Each catches a different kind of bug, and
the last two exist because the first two **cannot see layout**.

| Layer | Catches | Run it |
| --- | --- | --- |
| 1. Unit and component tests | Logic: next action, ages, timeline geometry, sorting, filtering, grouping, URL state, the state catalog, API payloads | `uv run pytest -q` (runs the web checks too) or `pnpm --dir web test` |
| 2. Guard tests | A rule that has regressed before, derived from the source itself (CSS breakpoints, generated types) | same as above |
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

- `web/src/cardLayout.test.ts` reads `styles.css`, computes the width the card
  grid needs from its actual column definitions, and asserts the
  wrapping-layout breakpoints are at least that (plus room for the deepest
  indentation), and that the timeline's full-row rule is not outranked by
  `.card .slot`.
- `tests/test_ui_api_types.py` and `pnpm check:types` fail when the generated
  TS types no longer match the pydantic models.

When you fix a layout bug, add a guard like these. It is the only automated
protection for CSS, see below.

## Why layout needs layers 3 and 4

jsdom (what Vitest renders into) **does not do layout**: it has no media
queries, container queries, flex/grid sizing or real widths. So these bug
classes pass every unit test and still ship:

- a card wider than its column (the fixed nine-column grid at 1,120-1,600px)
- a flex rule beaten by a more specific one (timelines only as wide as their
  content)
- labels overlapping or running past the track

Catch them by measuring the real page.

## 3. The demo

```sh
tools/ui-demo/run.sh                  # two repos, 8 branches, every scenario
tools/ui-demo/run.sh --deep 14        # plus a 14-deep stack: depth cap, re-rooting
```

See [`tools/ui-demo/README.md`](../tools/ui-demo/README.md). It seeds a temp
`TOOLBELT_HOME` and puts a fake `gh` first on `PATH`. To cover a new state, add
a `pr(...)` entry to `SCENARIOS` in `tools/ui-demo/gh` and a row to `STACKS` in
`seed.py`.

### Checking a visual or layout change

1. Run the demo, open the page in a real browser tab (a background tab is
   deliberately paused: "paused (tab hidden)"; bring it to the front).
2. Look at **all three views** (Grouped, Stacked, Table) at several widths:
   about 400, 830, 1200, 1500 and 1700px. The breakpoints that matter are 92rem
   (about 1,472px) and 101rem for Stacked.
3. Use `--deep 14` and re-root to check indentation and the depth cap.
4. Measure instead of eyeballing. In the page's console:

```js
// Anything wider than its card, and event labels that overlap or leave the track.
(() => {
  const hit = (a, b) => a.left < b.right - .5 && b.left < a.right - .5 &&
                        a.top < b.bottom - .5 && b.top < a.bottom - .5;
  const bad = [];
  document.querySelectorAll("article.card").forEach((card) => {
    const c = card.getBoundingClientRect();
    card.querySelectorAll(".tl-track, .tl, [class*='s-']").forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.right > c.right + 1) bad.push(["overflow", el.className, Math.round(r.right - c.right)]);
    });
    const labels = [...card.querySelectorAll(".tl-label")].map((e) => e.getBoundingClientRect());
    labels.forEach((a, i) => labels.slice(i + 1).forEach((b) => hit(a, b) && bad.push(["labels overlap"])));
  });
  return { viewport: innerWidth, cards: document.querySelectorAll("article.card").length, bad };
})();
```

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
