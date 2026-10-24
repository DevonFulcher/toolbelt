// difftastic's changed spans (as the server returns them) in the units
// CodeMirror uses. difftastic counts columns in UTF-8 bytes; CodeMirror (and
// LSP) count UTF-16 code units.
import type { DifftasticFile } from "../api/generated";

/** One changed range, in UTF-16 offsets within a single line. */
export interface LineSpan {
  from: number;
  to: number;
}

export type SideName = "lhs" | "rhs";

export function utf8ColumnToUtf16(line: string, byteColumn: number): number {
  let bytes = 0;
  let index = 0;
  for (const char of line) {
    if (bytes >= byteColumn) break;
    const codePoint = char.codePointAt(0) as number;
    bytes += codePoint < 0x80 ? 1 : codePoint < 0x800 ? 2 : codePoint < 0x10000 ? 3 : 4;
    index += char.length;
  }
  return index;
}

export function splitLines(text: string): string[] {
  return text.split(/\r\n?|\n/);
}

/** The line pairs that line up the two sides; none for a created, deleted or unchanged file. */
export function alignedLines(file: DifftasticFile): [number | null, number | null][] {
  // The generated type leaves the pair's elements `unknown`.
  return (file.aligned_lines ?? []) as [number | null, number | null][];
}

/** Changed spans by 0-indexed line, for one side. */
export function changedSpansByLine(file: DifftasticFile, side: SideName, lines: string[]): Map<number, LineSpan[]> {
  const result = new Map<number, LineSpan[]>();
  for (const chunk of file.chunks ?? []) {
    for (const pair of chunk) {
      const entry = pair[side];
      if (!entry) continue;
      const line = lines[entry.line_number] ?? "";
      result.set(
        entry.line_number,
        entry.changes.map((span) => ({
          from: utf8ColumnToUtf16(line, span.start),
          to: utf8ColumnToUtf16(line, span.end),
        })),
      );
    }
  }
  return result;
}
