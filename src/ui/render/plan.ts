import type { Entry } from '../../types.js';

export interface TranscriptItem {
  entry: Entry;
  /** Subagent entries collapsed into an Agent/Task tool entry. */
  children?: Entry[];
}

export function isFinished(e: Entry): boolean {
  switch (e.kind) {
    case 'assistant':
    case 'thinking':
      return !e.streaming;
    case 'tool':
      return e.status !== 'running';
    default:
      return true;
  }
}

export const AGENT_TOOLS = new Set(['Task', 'Agent']);

export const isAgentTool = (e: Entry): e is Extract<Entry, { kind: 'tool' }> => e.kind === 'tool' && AGENT_TOOLS.has(e.name);

/**
 * Group subagent entries under their Agent/Task tool and find how many leading items are finished
 * (those may go to <Static>). Entries whose parent is not an Agent/Task in the list stay top-level.
 * With `root` set to an Agent/Task tool id, returns that subagent's own entries instead (its nested
 * agents grouped the same way).
 */
export function planTranscript(entries: Entry[], root: string | null = null): { items: TranscriptItem[]; finishedPrefix: number } {
  const agentIds = new Set(entries.filter(isAgentTool).map((e) => e.id));
  const children = new Map<string | null, Entry[]>();
  for (const e of entries) {
    const p = 'parentToolUseId' in e ? e.parentToolUseId : null;
    const parent = p != null && agentIds.has(p) ? p : null;
    const list = children.get(parent) ?? [];
    list.push(e);
    children.set(parent, list);
  }
  const items: TranscriptItem[] = (children.get(root) ?? []).map((entry) => {
    const c = children.get(entry.id);
    return c ? { entry, children: c } : { entry };
  });
  let finishedPrefix = items.findIndex((i) => !isFinished(i.entry));
  if (finishedPrefix === -1) finishedPrefix = items.length;
  return { items, finishedPrefix };
}
