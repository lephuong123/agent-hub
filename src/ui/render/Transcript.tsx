import React, { useMemo, useRef, type ReactNode } from 'react';
import { Box, Static } from 'ink';
import type { Entry } from '../../types.js';
import { MessageView } from './MessageView.js';
import { planTranscript, type TranscriptItem } from './plan.js';

export interface TranscriptProps {
  entries: Entry[];
  /** Change to remount <Static> (e.g. after /clear). */
  clearKey?: string | number;
  /** Rendered once as the first static item (e.g. <Welcome />). */
  header?: ReactNode;
}

type StaticItem = { key: string; header: ReactNode } | { key: string; item: TranscriptItem };

/**
 * Finished leading entries are written once to scrollback via <Static> (append-only, never re-rendered);
 * the unfinished tail renders live below. Mirrors Claude Code's flicker-free transcript.
 */
export function Transcript({ entries, clearKey, header }: TranscriptProps) {
  const { items, finishedPrefix } = useMemo(() => planTranscript(entries), [entries]);
  const committed = useRef<{ ids: string[]; epoch: number; clearKey: TranscriptProps['clearKey'] }>({
    ids: [],
    epoch: 0,
    clearKey,
  });
  const c = committed.current;

  // Static is append-only: if what was committed no longer matches the head of the list, remount it.
  const diverged = c.ids.length > items.length || c.ids.some((id, i) => items[i]!.entry.id !== id);
  if (clearKey !== c.clearKey || diverged) {
    c.ids = [];
    c.epoch++;
    c.clearKey = clearKey;
  }
  for (let i = c.ids.length; i < finishedPrefix; i++) c.ids.push(items[i]!.entry.id);
  const staticCount = c.ids.length;

  const staticItems: StaticItem[] = useMemo(() => {
    const list: StaticItem[] = header ? [{ key: 'header', header }] : [];
    for (const item of items.slice(0, staticCount)) list.push({ key: item.entry.id, item });
    return list;
  }, [items, staticCount, header]);

  return (
    <>
      <Static key={`${String(c.clearKey)}:${c.epoch}`} items={staticItems}>
        {(s) => ('header' in s ? <Box key={s.key}>{s.header}</Box> : <MessageView key={s.key} entry={s.item.entry} childEntries={s.item.children} />)}
      </Static>
      <Box flexDirection="column">
        {items.slice(staticCount).map((item) => (
          <MessageView key={item.entry.id} entry={item.entry} childEntries={item.children} />
        ))}
      </Box>
    </>
  );
}
