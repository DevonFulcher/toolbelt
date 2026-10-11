// Which rows show, in what order and at what depth, for each view; pure
// functions over `Row`s (see rows.ts).
//
//   table    the stack table, as a tree or flat (`mode=flat`);
//   stacked  cards by repo, then stack, in tree order, drawn with rails to
//            their parents and slim stubs for the finished (or filtered-out)
//            parents kept as context;
//   grouped  cards in sections by what happens next (Needs me / Waiting on
//            CI / Waiting on others / Done), flat within a section but
//            ordered by stack so related branches are adjacent; a finished
//            parent is a slim stub above its children there too.
import { capEntries, focusedRows } from "./focus";
import type { Row } from "./rows";
import { type Column, type ViewState, VALUE_FILTERS } from "./viewState";

export interface Entry {
  id: string;
  depth: number;
  /** Kept only as context for the stack below (a stub, not a card). */
  context: boolean;
  /**
   * Set by `capEntries` on an entry at the depth cap with branches below it
   * that are not shown: how many more levels, and how many branches.
   */
  hidden?: { levels: number; branches: number };
}

export interface Section {
  key: string;
  label: string;
  tone: string;
  meaning: string;
  entries: Entry[];
}

// ---- ordering -----------------------------------------------------------------

const NUMBER = /^-?\d+(\.\d+)?$/;

/**
 * Compares two sort keys ascending: numerically when both are numbers, else
 * as natural-order text ("ABC-9" before "ABC-10").
 */
export function compareKeys(a: string, b: string): number {
  if (NUMBER.test(a) && NUMBER.test(b)) return Number(a) - Number(b);
  return a.localeCompare(b, "en", { numeric: true, sensitivity: "base" });
}

/**
 * Rows with no value for the sort column always go last, in both directions;
 * ties keep the server's order.
 */
function rowComparator(state: ViewState): (a: Row, b: Row) => number {
  const sign = state.dir === "desc" ? -1 : 1;
  const column: Column | "" = state.sort;
  return (a, b) => {
    if (column) {
      const ka = a.cells[column]?.sort ?? "";
      const kb = b.cells[column]?.sort ?? "";
      if (ka === "" && kb !== "") return 1;
      if (kb === "" && ka !== "") return -1;
      if (ka !== "" && kb !== "") {
        const result = compareKeys(ka, kb);
        if (result !== 0) return sign * result;
      }
    }
    return a.order - b.order;
  };
}

// ---- filtering ----------------------------------------------------------------

/** Whether a row is in the repo chosen in the repo filter (all, if none). */
export function inScope(row: Row, state: ViewState): boolean {
  return !state.repo || row.repo === state.repo;
}

/**
 * A merged/closed branch ("done": its next action's who is "none") is hidden
 * unless the Include done toggle is on or the Next filter asks for done
 * branches.
 */
export function hiddenAsDone(row: Row, state: ViewState): boolean {
  if (state.done === "1" || state.next === "none") return false;
  return row.cells.next?.value === "none";
}

/**
 * Whether a row passes the repo, done, text and per-column filters. Every
 * whitespace-separated term of the text filter must appear.
 */
export function matches(row: Row, state: ViewState): boolean {
  if (!inScope(row, state) || hiddenAsDone(row, state)) return false;
  for (const key of VALUE_FILTERS) {
    if (state[key] && (row.cells[key]?.value ?? "") !== state[key]) return false;
  }
  const haystack = row.search.toLowerCase();
  return state.q
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((term) => haystack.includes(term.replace(/^#/, "")));
}

// ---- layouts --------------------------------------------------------------------

/**
 * The rows to show, in order. Flat table: the matching rows, sorted. Tree
 * (the table's tree mode and the stacked cards): the same rows plus their
 * ancestors (kept for context, flagged `context`), depth-first, with the sort
 * applied among siblings; each repo's rows stay together, and the repos
 * themselves are in server (name) order, or by the Repo column when it is
 * the sort. Rows whose parent is missing become roots; cycles do not hang.
 * With a focus root (`state.root`, when it names a row) only its subtree is
 * laid out, so depth counts from it; the flat table ignores it.
 */
export function layout(allRows: Row[], state: ViewState): Entry[] {
  const compare = rowComparator(state);
  if (state.view === "table" && state.mode === "flat") {
    return allRows
      .filter((row) => matches(row, state))
      .sort(compare)
      .map((row) => ({ id: row.id, depth: 0, context: false }));
  }

  const rows = focusedRows(allRows, state.root);
  const byId = new Map(rows.map((row) => [row.id, row]));
  const matched = new Set<string>();
  const keep = new Set<string>();
  for (const row of rows) {
    if (!matches(row, state)) continue;
    matched.add(row.id);
    for (let r: Row | undefined = row; r && !keep.has(r.id); r = r.parent ? byId.get(r.parent) : undefined) {
      keep.add(r.id);
    }
  }

  const children = new Map<string, Row[]>();
  const rootsByRepo = new Map<string, Row[]>();
  for (const row of rows) {
    if (!keep.has(row.id)) continue;
    if (row.parent && row.parent !== row.id && byId.has(row.parent)) {
      const siblings = children.get(row.parent) ?? [];
      siblings.push(row);
      children.set(row.parent, siblings);
    } else {
      const roots = rootsByRepo.get(row.repo) ?? [];
      roots.push(row);
      rootsByRepo.set(row.repo, roots);
    }
  }

  // Repos in the server's order (the first row of each), or by the Repo
  // column when that is the sort.
  const repoGroups = [...rootsByRepo.values()].sort((a, b) => {
    if (state.sort === "repo") return compare(a[0], b[0]);
    return a[0].order - b[0].order;
  });

  const out: Entry[] = [];
  const visit = (row: Row, depth: number): void => {
    out.push({ id: row.id, depth, context: !matched.has(row.id) });
    for (const child of [...(children.get(row.id) ?? [])].sort(compare)) {
      visit(child, depth + 1);
    }
  };
  for (const group of repoGroups) {
    for (const row of [...group].sort(compare)) visit(row, 0);
  }
  if (state.view === "table") {
    // A finished ancestor is context in card views, but the table's
    // Include done toggle should also govern its tree rows.
    return out.filter((entry) => !entry.context || !hiddenAsDone(byId.get(entry.id) as Row, state));
  }
  return out;
}

/** The first task that needs me in each stack path. */
export function prioritiesLayout(rows: Row[], state: ViewState): Section[] {
  const byId = new Map(rows.map((row) => [row.id, row]));
  const selected = rows.filter((row) => {
    if (row.cells.next?.value !== "you" || !matches(row, state)) return false;
    const seen = new Set<string>([row.id]);
    for (let parent = row.parent; parent && !seen.has(parent); ) {
      seen.add(parent);
      const ancestor = byId.get(parent);
      if (!ancestor) break;
      if (ancestor.cells.next?.value === "you") return false;
      parent = ancestor.parent;
    }
    return true;
  });
  if (!selected.length) return [];
  return [{
    key: "priorities",
    label: "Priorities",
    tone: "you",
    meaning: "Tasks that need me, with no task below them in the stack that needs me.",
    entries: selected.sort(rowComparator(state)).map((row) => ({ id: row.id, depth: 0, context: false })),
  }];
}

// ---- card views --------------------------------------------------------------------

/**
 * The sections of the grouped view, in order, keyed by the Next cell's value
 * (who the branch is waiting on): "needs my action first".
 */
export const NEXT_SECTIONS = [
  {
    key: "you",
    label: "Needs me",
    tone: "you",
    meaning: "Your move: fix, resolve, publish, request review or merge.",
  },
  {
    key: "ci",
    label: "Waiting on CI",
    tone: "ci",
    meaning: "Checks are running; nothing to do until they finish.",
  },
  {
    key: "others",
    label: "Waiting on others / team",
    tone: "others",
    meaning: "A reviewer, or a parent PR that is waiting on one, has the ball.",
  },
  {
    key: "none",
    label: "Done",
    tone: "none",
    meaning: "Merged or closed.",
  },
  {
    key: "loading",
    label: "Loading",
    tone: "loading",
    meaning: "Status not fetched yet.",
  },
] as const;

/**
 * The grouped view: the sections that have cards. Within a section the cards
 * follow the tree layout (so a stack's branches are adjacent, parents first,
 * with the sort applied among siblings). A card whose parent is not a card
 * of its own (finished, or filtered out) follows a stub for it.
 */
export function groupedLayout(rows: Row[], state: ViewState): Section[] {
  const byId = new Map(rows.map((row) => [row.id, row]));
  const contextIds = new Set<string>();
  // Flat sections: neither the focus root nor the depth cap applies.
  const entries = layout(rows, { ...state, view: "stacked", root: "" });
  for (const entry of entries) if (entry.context) contextIds.add(entry.id);

  const known = new Set<string>(NEXT_SECTIONS.map((section) => section.key));
  const buckets = new Map<string, Entry[]>(NEXT_SECTIONS.map((section) => [section.key, []]));
  const stubbed = new Map<string, Set<string>>(
    NEXT_SECTIONS.map((section) => [section.key, new Set<string>()]),
  );
  for (const entry of entries) {
    if (entry.context) continue;
    const row = byId.get(entry.id);
    if (!row) continue;
    const who = row.cells.next?.value ?? "";
    const key = known.has(who) && who !== "loading" ? who : "loading";
    const bucket = buckets.get(key) as Entry[];
    const shown = stubbed.get(key) as Set<string>;
    const chain: string[] = [];
    for (
      let parent = row.parent ? byId.get(row.parent) : undefined;
      parent && contextIds.has(parent.id);
      parent = parent.parent ? byId.get(parent.parent) : undefined
    ) {
      chain.unshift(parent.id);
    }
    for (const id of chain) {
      if (shown.has(id)) continue;
      shown.add(id);
      bucket.push({ id, depth: 0, context: true });
    }
    bucket.push({ id: entry.id, depth: 0, context: false });
  }
  return NEXT_SECTIONS.filter((section) => (buckets.get(section.key) as Entry[]).length).map(
    (section) => ({ ...section, entries: buckets.get(section.key) as Entry[] }),
  );
}

/**
 * The stacked view: one section per repo (in the tree layout's order), with
 * the stack's finished/filtered-out ancestors as context entries. It shows
 * the focus root's subtree (when set), down to the depth cap.
 */
export function stackedLayout(rows: Row[], state: ViewState): Section[] {
  const byId = new Map(rows.map((row) => [row.id, row]));
  const sections: Section[] = [];
  for (const entry of capEntries(layout(rows, { ...state, view: "stacked" }))) {
    const row = byId.get(entry.id);
    if (!row) continue;
    let section = sections[sections.length - 1];
    if (!section || section.key !== `repo:${row.repo}`) {
      section = {
        key: `repo:${row.repo}`,
        label: row.repoName || row.repo,
        tone: "",
        meaning: "",
        entries: [],
      };
      sections.push(section);
    }
    section.entries.push(entry);
  }
  return sections;
}

export type RailKind = "line" | "tee" | "corner";
export interface RailSegment {
  col: number;
  kind: RailKind;
}

/**
 * The rails of the stacked view: for each entry (`depth`) in depth-first
 * order, the vertical/elbow segments drawn left of it: column `c` belongs to
 * the entry's ancestor (or itself) at depth `c + 1`. `line`: that ancestor
 * has later siblings, so the line passes through; `tee`: the entry itself,
 * with later siblings; `corner`: the entry itself, the last of its siblings.
 */
export function railGuides(entries: { depth: number }[]): RailSegment[][] {
  const later = new Map<number, boolean>();
  const hasLater = (index: number): boolean => {
    let found = later.get(index);
    if (found === undefined) {
      const depth = entries[index].depth;
      found = false;
      for (let j = index + 1; j < entries.length; j += 1) {
        if (entries[j].depth < depth) break;
        if (entries[j].depth === depth) {
          found = true;
          break;
        }
      }
      later.set(index, found);
    }
    return found;
  };
  const path: number[] = [];
  return entries.map((entry, index) => {
    path[entry.depth] = index;
    const segments: RailSegment[] = [];
    for (let col = 0; col < entry.depth; col += 1) {
      const more = hasLater(path[col + 1]);
      if (col === entry.depth - 1) segments.push({ col, kind: more ? "tee" : "corner" });
      else if (more) segments.push({ col, kind: "line" });
    }
    return segments;
  });
}
