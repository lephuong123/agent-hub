import React, { memo, useEffect, useState, type ReactNode } from 'react';
import { Box, Text } from 'ink';
import type { Entry, TaskInfo } from '../../types.js';
import { Clickable } from '../term/MouseContext.js';
import { Markdown } from './Markdown.js';
import { AGENT_TOOLS } from './plan.js';
import { useTicker, useViewActions } from './ViewContext.js';
import {
  buildDiff,
  cleanResult,
  countFound,
  diffStats,
  formatElapsed,
  formatTokens,
  parseTodos,
  plural,
  splitLines,
  toolDisplayName,
  toolSummary,
  truncateLines,
  type DiffLine,
} from './util.js';

type ToolEntry = Extract<Entry, { kind: 'tool' }>;

const ALL = Number.POSITIVE_INFINITY;

function useBlink(active: boolean, ms = 500): boolean {
  const [on, setOn] = useState(true);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setOn((v) => !v), ms);
    return () => clearInterval(t);
  }, [active, ms]);
  return active ? on : true;
}

/** "  ⎿  " gutter for tool result lines. */
export function ResultBlock({ children }: { children: ReactNode }) {
  return (
    <Box flexDirection="row">
      <Text dimColor>{'  ⎿  '}</Text>
      <Box flexDirection="column" flexGrow={1} flexShrink={1}>
        {children}
      </Box>
    </Box>
  );
}

function MoreLines({ hidden }: { hidden: number }) {
  const { interactive } = useViewActions();
  if (hidden <= 0) return null;
  return <Text dimColor>{`… +${plural(hidden, 'line')}${interactive ? ' (click to expand)' : ''}`}</Text>;
}

export interface DiffViewProps {
  lines: DiffLine[];
  maxLines?: number;
}

export function DiffView({ lines, maxLines = 12 }: DiffViewProps) {
  const { shown, hidden } = truncateLines(lines, maxLines);
  return (
    <Box flexDirection="column">
      {shown.map((l, i) =>
        l.type === 'ctx' ? (
          <Text key={i} dimColor>{`  ${l.text}`}</Text>
        ) : (
          <Text key={i} color={l.type === 'add' ? 'green' : 'red'}>{`${l.type === 'add' ? '+' : '-'} ${l.text}`}</Text>
        ),
      )}
      <MoreLines hidden={hidden} />
    </Box>
  );
}

function TodoList({ input }: { input: Record<string, unknown> }) {
  const todos = parseTodos(input);
  if (todos.length === 0) return <Text dimColor>(no todos)</Text>;
  return (
    <Box flexDirection="column">
      {todos.map((t, i) =>
        t.status === 'completed' ? (
          <Text key={i} color="green" strikethrough>{`☒ ${t.content}`}</Text>
        ) : t.status === 'in_progress' ? (
          <Text key={i} bold>{`☐ ${t.content}`}</Text>
        ) : (
          <Text key={i}>{`☐ ${t.content}`}</Text>
        ),
      )}
    </Box>
  );
}

function ClippedLines({ text, max, color }: { text: string; max: number; color?: string }) {
  if (text.trim() === '') return <Text dimColor>(No content)</Text>;
  const { shown, hidden } = truncateLines(splitLines(text), max);
  return (
    <Box flexDirection="column">
      <Text color={color} dimColor={!color}>
        {shown.join('\n')}
      </Text>
      <MoreLines hidden={hidden} />
    </Box>
  );
}

/** "12 tool uses · 3.4k tokens · 45s" for an agent, from its task when the SDK reported one. */
export function agentStats(entry: ToolEntry, tools: number, task?: TaskInfo): string {
  const end = task?.endedAt ?? entry.endedAt ?? Date.now();
  const start = task?.startedAt ?? entry.startedAt;
  return [
    plural(Math.max(tools, task?.toolUses ?? 0), 'tool use'),
    task?.tokens ? `${formatTokens(task.tokens)} tokens` : '',
    start ? formatElapsed(end - start) : '',
  ]
    .filter(Boolean)
    .join(' · ');
}

function AgentBody({ entry, childEntries, task, expanded }: { entry: ToolEntry; childEntries: Entry[]; task?: TaskInfo; expanded: boolean }) {
  const { interactive } = useViewActions();
  const running = entry.status === 'running' || task?.status === 'running';
  useTicker(running);
  const tools = childEntries.filter((c): c is ToolEntry => c.kind === 'tool');
  const stats = agentStats(entry, tools.length, task);
  if (expanded)
    return (
      <Box flexDirection="column">
        <Text dimColor>{stats}</Text>
        {childEntries.map((c) => (
          <MessageView key={c.id} entry={c} expanded />
        ))}
      </Box>
    );
  const open = interactive ? ' · click to open' : '';
  if (!running)
    return (
      <Box flexDirection="column">
        <Text dimColor>{`Done (${stats})${open}`}</Text>
        {task?.status === 'stopped' && <Text color="yellow">Stopped</Text>}
      </Box>
    );
  const last = tools[tools.length - 1];
  return (
    <Box flexDirection="column">
      {task?.summary && <Text>{task.summary}</Text>}
      <Text dimColor>{last ? `${toolDisplayName(last.name)}(${toolSummary(last.name, last.input)})` : 'Initializing…'}</Text>
      <Text dimColor>{`${stats}${task?.backgrounded ? ' · in background' : ''}${open}`}</Text>
    </Box>
  );
}

function ToolBody({ entry, childEntries, task, expanded }: { entry: ToolEntry; childEntries?: Entry[]; task?: TaskInfo; expanded: boolean }) {
  const { name, input, status } = entry;
  const result = cleanResult(entry.result);

  if (status === 'denied') return <Text color="red">User rejected</Text>;
  if (name === 'TodoWrite') return <TodoList input={input} />;
  if (AGENT_TOOLS.has(name) && status !== 'error')
    return <AgentBody entry={entry} childEntries={childEntries ?? []} task={task} expanded={expanded} />;
  if (status === 'running') return <Text dimColor>{task?.backgrounded ? 'Running in the background…' : 'Running…'}</Text>;
  if (status === 'error') return <ClippedLines text={result || 'Error'} max={expanded ? ALL : 5} color="red" />;

  switch (name) {
    case 'Read':
      return expanded ? (
        <ClippedLines text={result} max={ALL} />
      ) : (
        <Text>
          Read <Text bold>{result ? splitLines(result).length : 0}</Text> lines
        </Text>
      );
    case 'Edit':
    case 'MultiEdit':
    case 'Write': {
      const diff = buildDiff(name, input) ?? [];
      const { added, removed } = diffStats(diff);
      const path = String(input.file_path ?? '');
      return (
        <Box flexDirection="column">
          {name === 'Write' ? (
            <Text>
              Wrote <Text bold>{added}</Text> lines to <Text bold>{path}</Text>
            </Text>
          ) : (
            <Text>
              Updated <Text bold>{path}</Text> with <Text bold>{added}</Text> {added === 1 ? 'addition' : 'additions'} and{' '}
              <Text bold>{removed}</Text> {removed === 1 ? 'removal' : 'removals'}
            </Text>
          )}
          <DiffView lines={diff} maxLines={expanded ? ALL : name === 'Write' ? 8 : 12} />
        </Box>
      );
    }
    case 'Bash':
      return <ClippedLines text={result} max={expanded ? ALL : 5} />;
    case 'Grep':
    case 'Glob': {
      if (expanded) return <ClippedLines text={result} max={ALL} />;
      const n = countFound(result);
      const unit = name === 'Grep' && input.output_mode === 'content' ? 'line' : 'file';
      return (
        <Text>
          Found <Text bold>{n}</Text> {n === 1 ? unit : `${unit}s`}
        </Text>
      );
    }
    default:
      return <ClippedLines text={result} max={expanded ? ALL : 3} />;
  }
}

function ToolView({ entry, childEntries, task, expanded }: { entry: ToolEntry; childEntries?: Entry[]; task?: TaskInfo; expanded: boolean }) {
  const { interactive, toggle, open } = useViewActions();
  const running = entry.status === 'running';
  const blink = useBlink(running);
  const color = entry.status === 'done' ? 'green' : running ? 'gray' : 'red';
  const summary = toolSummary(entry.name, entry.input);
  const agent = AGENT_TOOLS.has(entry.name);
  const onClick = !interactive ? undefined : agent || task ? () => open(entry.id) : () => toggle(entry.id);
  return (
    <Clickable flexDirection="column" onClick={onClick}>
      <Text>
        <Text color={color}>{blink ? '● ' : '  '}</Text>
        <Text bold>{toolDisplayName(entry.name)}</Text>
        {agent && task?.subagentType ? <Text dimColor>{` [${task.subagentType}]`}</Text> : null}
        {summary ? `(${summary})` : ''}
      </Text>
      <ResultBlock>
        <ToolBody entry={entry} childEntries={childEntries} task={task} expanded={expanded} />
      </ResultBlock>
    </Clickable>
  );
}

function ThinkingView({ entry, expanded }: { entry: Extract<Entry, { kind: 'thinking' }>; expanded: boolean }) {
  const { interactive, toggle } = useViewActions();
  const body = entry.text.trim();
  const { shown, hidden } = truncateLines(splitLines(body), expanded ? ALL : 3);
  return (
    <Clickable flexDirection="column" onClick={interactive ? () => toggle(entry.id) : undefined}>
      <Text dimColor italic>
        ✻ Thinking…
      </Text>
      {body !== '' && (
        <Box paddingLeft={2} flexDirection="column">
          <Text dimColor italic>
            {shown.join('\n')}
          </Text>
          <MoreLines hidden={hidden} />
        </Box>
      )}
    </Clickable>
  );
}

export interface MessageViewProps {
  entry: Entry;
  /** Subagent entries (parentToolUseId === this tool's id), collapsed into an Agent/Task tool. */
  childEntries?: Entry[];
  /** The SDK task this tool call started, if any (subagent or background shell). */
  task?: TaskInfo;
  /** Show the whole result instead of a clipped preview. */
  expanded?: boolean;
  /** Indent as a subagent's entry. Defaults to whether the entry has a parent; false in a subagent's own view. */
  nested?: boolean;
}

export const MessageView = memo(function MessageView({ entry, childEntries, task, expanded = false, nested }: MessageViewProps) {
  const indent = nested ?? ('parentToolUseId' in entry && entry.parentToolUseId != null);
  return (
    <Box flexDirection="column" marginTop={1} paddingLeft={indent ? 4 : 0}>
      <EntryBody entry={entry} childEntries={childEntries} task={task} expanded={expanded} nested={indent} />
    </Box>
  );
});

function EntryBody({ entry, childEntries, task, expanded = false, nested = false }: MessageViewProps) {
  switch (entry.kind) {
    case 'user': {
      const [first = '', ...rest] = splitLines(entry.text);
      return <Text color="gray">{`> ${first}${rest.map((l) => `\n  ${l}`).join('')}`}</Text>;
    }
    case 'assistant':
      return (
        <Box flexDirection="row">
          <Text color={nested ? 'gray' : 'white'}>{'● '}</Text>
          <Box flexDirection="column" flexGrow={1} flexShrink={1}>
            <Markdown text={entry.text} streaming={entry.streaming} indent={nested ? 6 : 2} />
          </Box>
        </Box>
      );
    case 'thinking':
      return <ThinkingView entry={entry} expanded={expanded} />;
    case 'tool':
      return <ToolView entry={entry} childEntries={childEntries} task={task} expanded={expanded} />;
    case 'system':
      if (entry.level === 'error') return <Text color="red">{`✗ ${entry.text}`}</Text>;
      if (entry.level === 'warn') return <Text color="yellow">{`⚠ ${entry.text}`}</Text>;
      return <Text color="gray">{entry.text}</Text>;
  }
}
