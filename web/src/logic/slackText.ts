// The text of a Slack post names who is asked as `@name`. Asking someone else
// rewrites the name, keeping any other edits made to the text.

const escapeRegExp = (text: string): string => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** A name ends where no more of one follows: not at `@alan` in `@alan.andradecestelos`
 * (a dot or dash and more letters), but yes at `@alan.` ending a sentence. */
const NAME_END = "(?![\\w-]|\\.\\w)";

/** `text` with `@from` replaced by `@to`: on every line, or when `pr` is given
 * only on the lines linking that PR (`/pull/<pr>`), so one PR of a post can ask
 * someone else. */
export function renameMention(text: string, from: string, to: string, pr?: number): string {
  const mention = new RegExp(`@${escapeRegExp(from)}${NAME_END}`, "g");
  const linksPr = pr === undefined ? null : new RegExp(`/pull/${pr}(?!\\d)`);
  return text
    .split("\n")
    .map((line) => (linksPr && !linksPr.test(line) ? line : line.replace(mention, () => `@${to}`)))
    .join("\n");
}
