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

const AGENT_TOOLS = new Set(['Task', 'Agent']);

/**
 * Group subagent entries under their Agent/Task tool and find how many leading items are finished
 * (those may go to <Static>). Entries whose parent is not an Agent/Task in the list stay top-level.
 */
export function planTranscript(entries: Entry[]): { items: TranscriptItem[]; finishedPrefix: number } {
  const agentIds = new Set(entries.filter((e) => e.kind === 'tool' && AGENT_TOOLS.has(e.name)).map((e) => e.id));
  const children = new Map<string, Entry[]>();
  const items: TranscriptItem[] = [];
  for (const e of entries) {
    const parent = 'parentToolUseId' in e ? e.parentToolUseId : null;
    if (parent != null && agentIds.has(parent)) {
      const list = children.get(parent) ?? [];
      list.push(e);
      children.set(parent, list);
    } else items.push({ entry: e });
  }
  for (const item of items) {
    const c = children.get(item.entry.id);
    if (c) item.children = c;
  }
  let finishedPrefix = items.findIndex((i) => !isFinished(i.entry));
  if (finishedPrefix === -1) finishedPrefix = items.length;
  return { items, finishedPrefix };
}
