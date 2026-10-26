import path from 'node:path';

const str = (v: unknown): string | undefined => (typeof v === 'string' && v.length > 0 ? v : undefined);

export function truncate(text: string, max: number): string {
  const oneLine = text.replace(/\s+/g, ' ').trim();
  return oneLine.length > max ? oneLine.slice(0, max - 1) + '…' : oneLine;
}

export function relPath(p: string, cwd: string = process.cwd()): string {
  if (!path.isAbsolute(p)) return p;
  const rel = path.relative(cwd, p);
  return rel && !rel.startsWith('..') && !path.isAbsolute(rel) ? rel : p;
}

function compactJson(input: Record<string, unknown>, max: number): string {
  let json: string;
  try {
    json = JSON.stringify(input) ?? '';
  } catch {
    json = String(input);
  }
  return truncate(json, max);
}

/** Claude-Code-like one-line summary of a tool call's arguments. */
export function toolSummary(name: string, input: Record<string, unknown> = {}, cwd?: string): string {
  switch (name) {
    case 'Bash':
      return truncate(str(input.command) ?? '', 120);
    case 'Read':
    case 'Write':
    case 'Edit':
    case 'MultiEdit':
    case 'NotebookEdit': {
      const p = str(input.file_path) ?? str(input.notebook_path);
      return p ? relPath(p, cwd) : '';
    }
    case 'Grep': {
      const pattern = str(input.pattern) ?? '';
      const p = str(input.path);
      return p ? `"${pattern}" in ${relPath(p, cwd)}` : `"${pattern}"`;
    }
    case 'Glob':
      return str(input.pattern) ?? '';
    case 'WebFetch':
      return str(input.url) ?? '';
    case 'WebSearch':
      return str(input.query) ?? '';
    case 'Agent':
    case 'Task':
      return str(input.description) ?? '';
    case 'TodoWrite': {
      const n = Array.isArray(input.todos) ? input.todos.length : 0;
      return `${n} todo${n === 1 ? '' : 's'}`;
    }
  }
  if (name.startsWith('mcp__')) {
    const [, server = '', ...rest] = name.split('__');
    const args = Object.keys(input).length ? ` (${compactJson(input, 60)})` : '';
    return `${server} - ${rest.join('__')}${args}`;
  }
  return Object.keys(input).length ? compactJson(input, 80) : '';
}

export function formatTokens(n: number): string {
  if (n < 1000) return String(Math.round(n));
  if (n < 1_000_000) {
    const k = n / 1000;
    return `${k < 10 ? k.toFixed(1).replace(/\.0$/, '') : Math.round(k)}k`;
  }
  return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}m`;
}

export function formatCost(usd: number): string {
  if (!usd) return '$0.00';
  return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
}

export function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function truncateLines(text: string, maxLines: number): { text: string; hidden: number } {
  const lines = text.replace(/\s+$/, '').split(/\r?\n/);
  if (lines.length <= maxLines) return { text: lines.join('\n'), hidden: 0 };
  return { text: lines.slice(0, maxLines).join('\n'), hidden: lines.length - maxLines };
}
