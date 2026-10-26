import type { SlashCommand } from '../../types.js';

/** Query for the slash menu, or null when the menu should not show ("/" prefix, no whitespace yet). */
export function slashQuery(value: string): string | null {
  return /^\/\S*$/.test(value) ? value.slice(1) : null;
}

const isSubsequence = (q: string, s: string): boolean => {
  let i = 0;
  for (const ch of s) if (ch === q[i]) i++;
  return i === q.length;
};

/** Exact, then prefix, then substring, then subsequence matches; alphabetical within each group. */
export function matchCommands(commands: readonly SlashCommand[], query: string): SlashCommand[] {
  const q = query.toLowerCase();
  const rank = (c: SlashCommand): number => {
    const n = c.name.toLowerCase();
    if (n === q) return 0;
    if (n.startsWith(q)) return 1;
    if (n.includes(q)) return 2;
    if (isSubsequence(q, n)) return 3;
    return -1;
  };
  return commands
    .map((c) => ({ c, r: rank(c) }))
    .filter((x) => x.r >= 0)
    .sort((a, b) => a.r - b.r || a.c.name.localeCompare(b.c.name))
    .map((x) => x.c);
}
