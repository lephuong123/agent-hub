import React from 'react';
import { Box, Text } from 'ink';
import type { TaskInfo } from '../../types.js';
import { Clickable } from '../term/MouseContext.js';
import { useTicker } from '../render/ViewContext.js';
import { ACCENT, formatElapsed, formatTokens, plural } from '../render/util.js';

const SPIN = ['◐', '◓', '◑', '◒'];
export const PANEL_ROWS = 5;
/** Finished tasks stay in the panel this long so the user sees them end. */
export const LINGER_MS = 8000;

export function taskLabel(t: TaskInfo): string {
  if (t.subagentType) return t.subagentType;
  if (t.type === 'local_bash') return 'shell';
  return t.type?.replace(/^local_/, '') ?? 'task';
}

export function taskStats(t: TaskInfo, now = Date.now()): string {
  return [
    t.status === 'running' ? t.lastToolName ?? '' : '',
    t.toolUses ? plural(t.toolUses, 'tool use') : '',
    t.tokens ? `${formatTokens(t.tokens)} tokens` : '',
    formatElapsed((t.endedAt ?? now) - t.startedAt),
  ]
    .filter(Boolean)
    .join(' · ');
}

/** Tasks worth a row: running ones, and ones that ended a moment ago. */
export function visibleTasks(tasks: TaskInfo[], now = Date.now()): TaskInfo[] {
  return tasks.filter((t) => t.status === 'running' || (t.endedAt != null && now - t.endedAt < LINGER_MS));
}

function StatusIcon({ task, frame }: { task: TaskInfo; frame: number }) {
  switch (task.status) {
    case 'running':
      return <Text color={ACCENT}>{SPIN[frame % SPIN.length]}</Text>;
    case 'completed':
      return <Text color="green">✓</Text>;
    case 'failed':
      return <Text color="red">✗</Text>;
    case 'stopped':
      return <Text color="yellow">■</Text>;
  }
}

export interface TaskPanelProps {
  tasks: TaskInfo[];
  focused: boolean;
  selected: number;
  onOpen(task: TaskInfo): void;
}

/** Running subagents and background shells, below the prompt (like Claude Code's agent tree). */
export function TaskPanel({ tasks, focused, selected, onOpen }: TaskPanelProps) {
  const running = tasks.some((t) => t.status === 'running');
  useTicker(true, running ? 250 : 1000);
  const frame = Math.floor(Date.now() / 250);
  const now = Date.now();
  if (tasks.length === 0) return null;
  const start = Math.max(0, Math.min(selected - PANEL_ROWS + 1, tasks.length - PANEL_ROWS));
  const rows = tasks.slice(start, start + PANEL_ROWS);
  const hidden = tasks.length - rows.length;
  const runningCount = tasks.filter((t) => t.status === 'running').length;
  return (
    <Box flexDirection="column" paddingX={2}>
      <Text dimColor>
        {focused
          ? '↑↓ select · enter or click to open · x stop · esc back to prompt'
          : runningCount
            ? `${plural(runningCount, 'task')} running · ↓ or click to manage`
            : 'Recent tasks · ↓ or click to open'}
      </Text>
      {rows.map((t, i) => {
        const on = focused && start + i === selected;
        return (
          <Clickable key={t.id} flexDirection="column" onClick={() => onOpen(t)}>
            <Text wrap="truncate-end">
              <Text color={on ? 'cyan' : undefined}>{on ? '❯ ' : '  '}</Text>
              <StatusIcon task={t} frame={frame} />
              <Text bold={on} color={on ? 'cyan' : undefined}>{` ${taskLabel(t)}`}</Text>
              <Text>{` ${t.description}`}</Text>
              <Text dimColor>{`  ${taskStats(t, now)}`}</Text>
            </Text>
            {t.summary && t.summary !== t.description && (
              <Text dimColor wrap="truncate-end">
                {`      ${t.summary}`}
              </Text>
            )}
          </Clickable>
        );
      })}
      {hidden > 0 && <Text dimColor>{`  +${hidden} more`}</Text>}
    </Box>
  );
}
