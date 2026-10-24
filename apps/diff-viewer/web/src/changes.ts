import { Change } from "@codemirror/merge";
import { Text } from "@codemirror/state";
import { changedSpansByLine, splitLines, type DifftasticFile, type LineSpan } from "./difftastic";

/**
 * difftastic's changed spans as CodeMirror merge changes. Entries are walked in
 * `aligned_lines` order so the A and B ranges both stay increasing; a side with
 * no line in an entry gets a zero-length range where the other side's change
 * would be inserted.
 */
export function buildChanges(file: DifftasticFile, aText: string, bText: string): Change[] {
  const aLines = splitLines(aText);
  const bLines = splitLines(bText);
  const aDoc = Text.of(aLines);
  const bDoc = Text.of(bLines);
  // difftastic prints no line data for these: the whole file is one side's change.
  if (file.status === "created") return [new Change(0, 0, 0, bDoc.length)];
  if (file.status === "deleted") return [new Change(0, aDoc.length, 0, 0)];
  const lhs = changedSpansByLine(file, "lhs", aLines);
  const rhs = changedSpansByLine(file, "rhs", bLines);
  const changes: Change[] = [];
  let nextA = 0;
  let nextB = 0;
  const range = (lineFrom: number | null, spans: LineSpan[] | undefined, insertAt: number): [number, number] => {
    if (lineFrom === null || !spans) return [insertAt, insertAt];
    if (!spans.length) return [lineFrom, lineFrom];
    return [lineFrom + Math.min(...spans.map((s) => s.from)), lineFrom + Math.max(...spans.map((s) => s.to))];
  };
  for (const [l, r] of file.aligned_lines ?? []) {
    const aLine = l !== null && l < aDoc.lines ? aDoc.line(l + 1) : null;
    const bLine = r !== null && r < bDoc.lines ? bDoc.line(r + 1) : null;
    const ls = l !== null ? lhs.get(l) : undefined;
    const rs = r !== null ? rhs.get(r) : undefined;
    if (ls || rs) {
      const [fromA, toA] = range(aLine && aLine.from, ls, nextA);
      const [fromB, toB] = range(bLine && bLine.from, rs, nextB);
      changes.push(new Change(fromA, toA, fromB, toB));
    }
    if (aLine) nextA = Math.min(aLine.to + 1, aDoc.length);
    if (bLine) nextB = Math.min(bLine.to + 1, bDoc.length);
  }
  return changes;
}
