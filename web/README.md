# toolbelt web client

The page `tt ui` serves: React 19 + TypeScript + Vite, pnpm, Zustand for the
store, Vitest + React Testing Library for tests. It reads the server's JSON
API (`GET /api/snapshot`) and keeps current from its typed Server-Sent Events
stream (`GET /api/events`); see `src/toolbelt/ui/api_models.py` for the wire
types.

## Using it

`tt ui` builds the client by itself when `src/toolbelt/ui/static/dist` is
missing or older than `web/src`, `package.json` or the lockfile
(`pnpm install --frozen-lockfile && pnpm build`), and `tt upgrade` builds it
too. Both need node and pnpm; without them the command says so in one line.
`tt ui --no-build` skips the check.

## Developing it

Run the API and the Vite dev server (hot reload) side by side:

```sh
tt ui --no-build        # the API on :8765 (TT_UI_PORT below if you change it)
pnpm --dir web install  # once
pnpm --dir web dev      # http://localhost:5173, proxies /api to :8765
```

Set `TT_UI_PORT` for `pnpm dev` when `tt ui --port` is not 8765.

| Script             | What it does                                                    |
| ------------------ | --------------------------------------------------------------- |
| `pnpm dev`         | Vite dev server with hot reload, proxying `/api`                |
| `pnpm build`       | type-check, then build into `src/toolbelt/ui/static/dist`       |
| `pnpm typecheck`   | `tsc --noEmit`                                                  |
| `pnpm lint`        | ESLint                                                          |
| `pnpm test`        | Vitest                                                          |
| `pnpm gen:types`   | regenerate `src/api/{schema.json,catalog.json,generated.ts}`    |
| `pnpm check:types` | fail if those are stale (pre-commit and pytest run this)        |

## API types

`src/api/generated.ts` is generated from the pydantic models in
`src/toolbelt/ui/api_models.py`: every enum and `Literal` there is a literal
union here, including each dimension of the state catalog
(`src/toolbelt/ui/pr_states.py`). Change the Python models, run
`pnpm gen:types`, commit the three generated files. A value added on the
server is a compile error in the client until it is handled (the renderer's
`switch`es end in `assertNever`, and per-state tables are
`Record<PrState, ...>`).

## Layout

```
src/api/         wire types (generated), the fetch client, the real catalog as a fixture
src/logic/       pure logic: view state <-> URL, sort/filter/layout, timeline geometry
src/live/        connection controller (visibility, grace, resume) and the SSE -> store bridge
src/store/       the Zustand store, selectors, URL sync, localStorage preferences
src/components/  App shell, FilterBar, TableView, CardList, Card, Timeline, Tooltip, ...
src/testing/     fixtures and render helpers for tests
```

## Testing

See [`docs/ui-testing.md`](../docs/ui-testing.md): what each layer of tests catches,
the demo (`tools/ui-demo/run.sh`) and how to measure layout, which jsdom cannot
check.
