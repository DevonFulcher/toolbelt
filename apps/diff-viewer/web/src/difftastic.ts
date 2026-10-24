// Types for `DFT_UNSTABLE=yes difft --display json`, and conversion of its
// UTF-8 byte columns into the UTF-16 offsets CodeMirror and LSP use.

export interface DifftasticSpan {
  start: number;
  end: number;
  content: string;
  highlight: string;
}

export interface DifftasticSide {
  line_number: number;
  changes: DifftasticSpan[];
}

export interface DifftasticLinePair {
  lhs?: DifftasticSide;
  rhs?: DifftasticSide;
}

export interface DifftasticFile {
  path: string;
  language: string;
  status: "changed" | "created" | "deleted" | "unchanged";
  /** Absent for `created`, `deleted` and `unchanged` files. */
  aligned_lines?: [number | null, number | null][];
  /** Absent for `created`, `deleted` and `unchanged` files. */
  chunks?: DifftasticLinePair[][];
}

/** One changed range, in UTF-16 offsets within a single line. */
export interface LineSpan {
  from: number;
  to: number;
}

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

export type SideName = "lhs" | "rhs";

/** Changed spans per 0-indexed line, for each side, in UTF-16 offsets. */
export function changedSpansByLine(
  file: DifftasticFile,
  side: SideName,
  lines: string[],
): Map<number, LineSpan[]> {
  const result = new Map<number, LineSpan[]>();
  for (const chunk of file.chunks ?? []) {
    for (const pair of chunk) {
      const entry = pair[side];
      if (!entry) continue;
      const line = lines[entry.line_number] ?? "";
      const spans = entry.changes.map((span) => ({
        from: utf8ColumnToUtf16(line, span.start),
        to: utf8ColumnToUtf16(line, span.end),
      }));
      result.set(entry.line_number, spans);
    }
  }
  return result;
}
