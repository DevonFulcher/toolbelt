// Splits text into plain and link segments so it can be rendered with clickable
// links without ever treating it as HTML. A link is a markdown link
// `[text](https://...)` or a bare http(s) URL; nothing else becomes a link.
export type Segment = { text: string; href?: string };

const LINK = /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>()[\]]+[^\s<>()[\].,;:!?'"])/g;

export function linkSegments(text: string): Segment[] {
  const segments: Segment[] = [];
  let last = 0;
  for (const match of text.matchAll(LINK)) {
    if (match.index > last) segments.push({ text: text.slice(last, match.index) });
    if (match[2] !== undefined) segments.push({ text: match[1], href: match[2] });
    else segments.push({ text: match[3], href: match[3] });
    last = match.index + match[0].length;
  }
  if (last < text.length) segments.push({ text: text.slice(last) });
  return segments;
}
