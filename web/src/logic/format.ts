// Time and duration formatting. formatElapsed is kept in step with
// pr_activity.format_age on the server, which produces the same text.

export const MINUTE = 60 * 1000;
export const HOUR = 60 * MINUTE;
export const DAY = 24 * HOUR;

/** "<1m", "5m", "3h", "4d", "3w" for a whole number of seconds. */
export function formatAge(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return "<1m";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 14 * 86400) return `${Math.floor(s / 86400)}d`;
  return `${Math.floor(s / (7 * 86400))}w`;
}

const ELAPSED_UNITS: [string, number][] = [
  ["w", 7 * 86400],
  ["d", 86400],
  ["h", 3600],
  ["m", 60],
];

/** What an age field shows: the largest unit and the two below it, a unit that
 * is zero left out: "<1m", "5m", "3h 12m", "2d 4h 12m", "3w 2d 4h" (weeks from
 * two weeks). */
export function formatElapsed(seconds: number): string {
  let rest = Math.max(0, Math.floor(seconds));
  if (rest < 60) return "<1m";
  const first = rest >= 14 * 86400 ? 0 : rest >= 86400 ? 1 : rest >= 3600 ? 2 : 3;
  const parts: string[] = [];
  for (const [name, size] of ELAPSED_UNITS.slice(first, first + 3)) {
    const count = Math.floor(rest / size);
    rest -= count * size;
    if (count > 0) parts.push(`${count}${name}`);
  }
  return parts.join(" ");
}

/** "2d 4h", "3h 12m", "5m", "3w 2d": the two largest non-zero units. */
export function formatSpan(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return "<1m";
  const units: [string, number][] = [
    ["w", 7 * 86400],
    ["d", 86400],
    ["h", 3600],
    ["m", 60],
  ];
  const parts: [number, string][] = [];
  let rest = s;
  for (const [name, size] of units) {
    const count = Math.floor(rest / size);
    rest -= count * size;
    if (count > 0 || parts.length > 0) parts.push([count, name]);
    if (parts.length === 2) break;
  }
  return parts
    .filter(([count]) => count > 0)
    .map(([count, name]) => `${count}${name}`)
    .join(" ");
}

/** "Oct 8, 2026, 3:42 PM" in the viewer's timezone (or `timeZone`). */
export function formatAbsolute(ms: number, options: { timeZone?: string } = {}): string {
  const text = new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone: options.timeZone,
  }).format(new Date(ms));
  // Newer ICU versions put a narrow no-break space before AM/PM.
  return text.replace(/[\u202f\u00a0]/g, " ");
}

/** "3h 12m ago" */
export function relative(ms: number, now: number): string {
  return `${formatSpan((now - ms) / 1000)} ago`;
}

/** "just now" / "5s ago" / "3m ago" / "2h ago", for the live indicator. */
export function formatAgo(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 3) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}
