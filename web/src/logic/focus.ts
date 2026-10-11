// Focus-root navigation for the tree views (stacked cards and the table's
// tree): show one branch's subtree with depth counted from it, cap how deep
// it indents, and let the user travel a stack of any length by re-rooting.
// Pure functions over `Row`s and layout entries; the components draw them.
import type { Entry } from "./layout";
import type { Row } from "./rows";

/**
 * The most levels shown below the view's root (the root is level 0). A card
 * at this depth with branches below it offers "continue from here" instead
 * of indenting further. The one place the cap is set.
 */
export const MAX_VISIBLE_DEPTH = 6;

/**
 * Of a long chain of ancestors the breadcrumbs show this many from the stack
 * root and this many nearest the current root; the middle collapses into
 * "…" (a menu listing them).
 */
export const CRUMBS_HEAD = 1;
export const CRUMBS_TAIL = 2;

/** `root` when it names a row, else "" (a stale or hand-edited root is ignored). */
export function knownRoot(rows: Row[], root: string): string {
  return root && rows.some((row) => row.id === root) ? root : "";
}

/**
 * The rows of the focused subtree (the root first, then its descendants, in
 * the given order). Without a known root, every row.
 */
export function focusedRows(rows: Row[], root: string): Row[] {
  if (!knownRoot(rows, root)) return rows;
  const inside = new Set<string>([root]);
  // Rows come parents-first only by convention: iterate to a fixed point.
  let grew = true;
  while (grew) {
    grew = false;
    for (const row of rows) {
      if (!inside.has(row.id) && row.parent && inside.has(row.parent)) {
        inside.add(row.id);
        grew = true;
      }
    }
  }
  return rows.filter((row) => inside.has(row.id));
}

/** The ids above `id`, the stack root first and the parent last. Cycle-safe. */
export function ancestorIds(rows: Row[], id: string): string[] {
  const byId = new Map(rows.map((row) => [row.id, row]));
  const chain: string[] = [];
  const seen = new Set<string>([id]);
  let parent = byId.get(id)?.parent ?? null;
  while (parent && byId.has(parent) && !seen.has(parent)) {
    chain.unshift(parent);
    seen.add(parent);
    parent = byId.get(parent)?.parent ?? null;
  }
  return chain;
}

/**
 * Where "up one level" goes from `root`: its parent, or "" (the whole stack)
 * when it is a stack root.
 */
export function upTarget(rows: Row[], root: string): string {
  const chain = ancestorIds(rows, root);
  return chain.length ? chain[chain.length - 1] : "";
}

export interface Crumb {
  id: string;
  name: string;
}

export type CrumbSegment =
  | { kind: "branch"; crumb: Crumb }
  /** The collapsed middle of a long chain, nearest the stack root first. */
  | { kind: "gap"; hidden: Crumb[] };

export interface Breadcrumbs {
  repoName: string;
  segments: CrumbSegment[];
  current: Crumb;
}

/**
 * `repo › stack root › … › parent › [current root]`: every ancestor a
 * segment, the middle of a long chain collapsed into one gap. Null when
 * `root` is not a row.
 */
export function breadcrumbs(rows: Row[], root: string): Breadcrumbs | null {
  const byId = new Map(rows.map((row) => [row.id, row]));
  const current = byId.get(root);
  if (!current) return null;
  const crumbs: Crumb[] = ancestorIds(rows, root).map((id) => ({
    id,
    name: byId.get(id)?.branch ?? id,
  }));
  const segments: CrumbSegment[] = [];
  if (crumbs.length > CRUMBS_HEAD + CRUMBS_TAIL + 1) {
    for (const crumb of crumbs.slice(0, CRUMBS_HEAD)) segments.push({ kind: "branch", crumb });
    segments.push({ kind: "gap", hidden: crumbs.slice(CRUMBS_HEAD, crumbs.length - CRUMBS_TAIL) });
    for (const crumb of crumbs.slice(crumbs.length - CRUMBS_TAIL)) {
      segments.push({ kind: "branch", crumb });
    }
  } else {
    for (const crumb of crumbs) segments.push({ kind: "branch", crumb });
  }
  return {
    repoName: current.repoName,
    segments,
    current: { id: current.id, name: current.branch },
  };
}

/** How many branches the focused subtree holds, the root included. */
export function focusSize(rows: Row[], root: string): number {
  return focusedRows(rows, root).length;
}

/**
 * Drops the entries deeper than `max` and marks each entry at depth `max`
 * that has some below it (`hidden`: how many more levels, and how many
 * branches). Entries are in depth-first order.
 */
export function capEntries(entries: Entry[], max: number = MAX_VISIBLE_DEPTH): Entry[] {
  const out: Entry[] = [];
  entries.forEach((entry, index) => {
    if (entry.depth > max) return;
    if (entry.depth < max) {
      out.push(entry);
      return;
    }
    let branches = 0;
    let deepest = entry.depth;
    for (let j = index + 1; j < entries.length && entries[j].depth > max; j += 1) {
      branches += 1;
      deepest = Math.max(deepest, entries[j].depth);
    }
    out.push(branches ? { ...entry, hidden: { levels: deepest - max, branches } } : entry);
  });
  return out;
}
