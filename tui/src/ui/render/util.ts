export const ACCENT = '#D97757';
export const PERMISSION = '#B1B9F9';

export type DiffLine = { type: 'add' | 'del' | 'ctx'; text: string };

export interface Todo {
  content: string;
  status: 'pending' | 'in_progress' | 'completed';
  activeForm?: string;
}

const FENCE = /^ {0,3}(`{3,}|~{3,})/;

/** Close an unterminated fenced code block so partial (streaming) markdown parses cleanly. */
export function closeOpenFences(text: string): string {
  let open: string | null = null;
  for (const line of text.split('\n')) {
    const m = FENCE.exec(line);
    if (!m) continue;
    const marker = m[1]!;
    if (open === null) open = marker;
    else if (marker[0] === open[0] && marker.length >= open.length && line.trim() === marker) open = null;
  }
  if (open === null) return text;
  return `${text}${text.endsWith('\n') ? '' : '\n'}${open}`;
}

export function splitLines(text: string): string[] {
  const lines = text.replace(/\r\n/g, '\n').split('\n');
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
  return lines;
}

export function truncateLines<T>(lines: T[], max: number): { shown: T[]; hidden: number } {
  if (lines.length <= max) return { shown: lines, hidden: 0 };
  return { shown: lines.slice(0, max), hidden: lines.length - max };
}

export function truncate(s: string, max: number): string {
  return s.length <= max ? s : `${s.slice(0, Math.max(0, max - 1))}…`;
}

/** Line-level diff (LCS) with fallback to all-del/all-add for very large inputs. */
export function diffLines(oldText: string, newText: string): DiffLine[] {
  const a = oldText === '' ? [] : splitLines(oldText);
  const b = newText === '' ? [] : splitLines(newText);
  if (a.length * b.length > 250_000) {
    return [...a.map((text) => ({ type: 'del' as const, text })), ...b.map((text) => ({ type: 'add' as const, text }))];
  }
  const n = a.length;
  const m = b.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--) dp[i]![j] = a[i] === b[j] ? dp[i + 1]![j + 1]! + 1 : Math.max(dp[i + 1]![j]!, dp[i]![j + 1]!);
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ type: 'ctx', text: a[i]! });
      i++;
      j++;
    } else if (dp[i + 1]![j]! >= dp[i]![j + 1]!) out.push({ type: 'del', text: a[i++]! });
    else out.push({ type: 'add', text: b[j++]! });
  }
  while (i < n) out.push({ type: 'del', text: a[i++]! });
  while (j < m) out.push({ type: 'add', text: b[j++]! });
  return out;
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '');

/** Diff to preview for Edit / MultiEdit / Write inputs; null for other tools. */
export function buildDiff(toolName: string, input: Record<string, unknown>): DiffLine[] | null {
  switch (toolName) {
    case 'Edit':
      return diffLines(str(input.old_string), str(input.new_string));
    case 'MultiEdit': {
      const edits = Array.isArray(input.edits) ? (input.edits as Record<string, unknown>[]) : [];
      return edits.flatMap((e, i) => [
        ...(i > 0 ? [{ type: 'ctx' as const, text: '…' }] : []),
        ...diffLines(str(e.old_string), str(e.new_string)),
      ]);
    }
    case 'Write':
      return splitLines(str(input.content)).map((text) => ({ type: 'add' as const, text }));
    default:
      return null;
  }
}

export function diffStats(lines: DiffLine[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const l of lines) {
    if (l.type === 'add') added++;
    else if (l.type === 'del') removed++;
  }
  return { added, removed };
}

const oneLine = (s: string, max = 80) => truncate(s.split('\n')[0]!.trim(), max);

/** "ToolName(summary)" argument text, Claude-Code style. */
export function toolSummary(name: string, input: Record<string, unknown>): string {
  const i = input ?? {};
  switch (name) {
    case 'Bash':
      return oneLine(str(i.command), 100);
    case 'Read':
    case 'Edit':
    case 'MultiEdit':
    case 'Write':
    case 'NotebookEdit':
      return str(i.file_path) || str(i.notebook_path);
    case 'Grep':
      return [`pattern: "${str(i.pattern)}"`, i.path ? `path: "${str(i.path)}"` : '', i.glob ? `glob: "${str(i.glob)}"` : '']
        .filter(Boolean)
        .join(', ');
    case 'Glob':
      return [`pattern: "${str(i.pattern)}"`, i.path ? `path: "${str(i.path)}"` : ''].filter(Boolean).join(', ');
    case 'Task':
    case 'Agent':
      return oneLine(str(i.description));
    case 'WebFetch':
      return str(i.url);
    case 'WebSearch':
      return oneLine(str(i.query));
    case 'TodoWrite':
      return '';
    default: {
      const first = Object.values(i).find((v) => typeof v === 'string') as string | undefined;
      if (first) return oneLine(first);
      const json = JSON.stringify(i);
      return json === '{}' ? '' : truncate(json, 80);
    }
  }
}

/** Display name: MCP tools "mcp__server__tool" → "server - tool (MCP)". */
export function toolDisplayName(name: string): string {
  const m = /^mcp__(.+?)__(.+)$/.exec(name);
  return m ? `${m[1]} - ${m[2]} (MCP)` : name;
}

export function parseTodos(input: Record<string, unknown>): Todo[] {
  const todos = Array.isArray(input?.todos) ? (input.todos as Record<string, unknown>[]) : [];
  return todos.map((t) => ({
    content: str(t.content),
    status: t.status === 'completed' || t.status === 'in_progress' ? t.status : 'pending',
    activeForm: str(t.activeForm) || undefined,
  }));
}

/** Count of results in Grep/Glob output ("Found N files" header, else non-empty lines). */
export function countFound(result: string | undefined): number {
  if (!result) return 0;
  const m = /Found (\d+)/.exec(result);
  if (m) return Number(m[1]);
  if (/^No (files|matches) found/i.test(result.trim())) return 0;
  return splitLines(result).filter((l) => l.trim() !== '').length;
}

export function formatTokens(n: number): string {
  if (n < 1000) return String(Math.round(n));
  if (n < 1_000_000) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k`;
  return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}m`;
}

export function formatCost(usd: number): string {
  return `$${usd.toFixed(usd >= 1 ? 2 : 4)}`;
}

export function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

/** Drop <system-reminder> blocks and trailing whitespace from tool results. */
export function cleanResult(result: string | undefined): string {
  return (result ?? '').replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, '').replace(/\s+$/, '');
}
