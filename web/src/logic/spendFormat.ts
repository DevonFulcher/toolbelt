// Spend figures as text.

/** "$1,368.70", "$0.00", "$0.0029" (a few cents or less keeps more digits). */
export function formatUsd(amount: number): string {
  const digits = amount !== 0 && Math.abs(amount) < 0.01 ? 4 : 2;
  return `$${amount.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

/** "1.8B", "280.8M", "14.6k", "532". */
export function formatTokens(count: number): string {
  for (const [unit, size] of [
    ["B", 1e9],
    ["M", 1e6],
    ["k", 1e3],
  ] as const) {
    if (count >= size) return `${(count / size).toFixed(1)}${unit}`;
  }
  return String(Math.round(count));
}
