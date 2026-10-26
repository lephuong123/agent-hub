import React, { useEffect, useState, type ReactNode } from 'react';
import { Box, Text } from 'ink';
import type { Entry } from '../../types.js';
import { Markdown } from './Markdown.js';
import {
  buildDiff,
  cleanResult,
  countFound,
  diffStats,
  parseTodos,
  plural,
  splitLines,
  toolDisplayName,
  toolSummary,
  truncateLines,
  type DiffLine,
} from './util.js';

type ToolEntry = Extract<Entry, { kind: 'tool' }>;

const EXPAND_HINT = '(ctrl+r to expand)';

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
      <Box flexDirection="column" flexGrow={1}>
        {children}
      </Box>
    </Box>
  );
}

function MoreLines({ hidden }: { hidden: number }) {
  return hidden > 0 ? <Text dimColor>{`… +${plural(hidden, 'line')} ${EXPAND_HINT}`}</Text> : null;
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

function AgentBody({ entry, tools }: { entry: ToolEntry; tools: ToolEntry[] }) {
  if (entry.status !== 'running') return <Text dimColor>{`Done (${plural(tools.length, 'tool use')})`}</Text>;
  const last = tools[tools.length - 1];
  return (
    <Box flexDirection="column">
      <Text dimColor>{last ? `${toolDisplayName(last.name)}(${toolSummary(last.name, last.input)})` : 'Initializing…'}</Text>
      {tools.length > 1 && <Text dimColor>{`+${plural(tools.length - 1, 'more tool use')}`}</Text>}
    </Box>
  );
}

function ToolBody({ entry, childEntries }: { entry: ToolEntry; childEntries?: Entry[] }) {
  const { name, input, status } = entry;
  const result = cleanResult(entry.result);

  if (status === 'denied') return <Text color="red">User rejected</Text>;
  if (name === 'TodoWrite') return <TodoList input={input} />;
  if ((name === 'Task' || name === 'Agent') && status !== 'error') {
    const tools = (childEntries ?? []).filter((c): c is ToolEntry => c.kind === 'tool');
    return <AgentBody entry={entry} tools={tools} />;
  }
  if (status === 'running') return <Text dimColor>Running…</Text>;
  if (status === 'error') return <ClippedLines text={result || 'Error'} max={5} color="red" />;

  switch (name) {
    case 'Read':
      return (
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
          <DiffView lines={diff} maxLines={name === 'Write' ? 8 : 12} />
        </Box>
      );
    }
    case 'Bash':
      return <ClippedLines text={result} max={5} />;
    case 'Grep':
    case 'Glob': {
      const n = countFound(result);
      const unit = name === 'Grep' && input.output_mode === 'content' ? 'line' : 'file';
      return (
        <Text>
          Found <Text bold>{n}</Text> {n === 1 ? unit : `${unit}s`}
        </Text>
      );
    }
    default:
      return <ClippedLines text={result} max={3} />;
  }
}

function ToolView({ entry, childEntries }: { entry: ToolEntry; childEntries?: Entry[] }) {
  const running = entry.status === 'running';
  const blink = useBlink(running);
  const color = entry.status === 'done' ? 'green' : running ? 'gray' : 'red';
  const summary = toolSummary(entry.name, entry.input);
  return (
    <Box flexDirection="column">
      <Text>
        <Text color={color}>{blink ? '● ' : '  '}</Text>
        <Text bold>{toolDisplayName(entry.name)}</Text>
        {summary ? `(${summary})` : ''}
      </Text>
      <ResultBlock>
        <ToolBody entry={entry} childEntries={childEntries} />
      </ResultBlock>
    </Box>
  );
}

export interface MessageViewProps {
  entry: Entry;
  /** Subagent entries (parentToolUseId === this tool's id), collapsed into an Agent/Task tool. */
  childEntries?: Entry[];
}

export function MessageView({ entry, childEntries }: MessageViewProps) {
  const nested = 'parentToolUseId' in entry && entry.parentToolUseId != null;
  return (
    <Box flexDirection="column" marginTop={1} paddingLeft={nested ? 4 : 0}>
      <EntryBody entry={entry} childEntries={childEntries} />
    </Box>
  );
}

function EntryBody({ entry, childEntries }: MessageViewProps) {
  switch (entry.kind) {
    case 'user': {
      const [first = '', ...rest] = splitLines(entry.text);
      return <Text color="gray">{`> ${first}${rest.map((l) => `\n  ${l}`).join('')}`}</Text>;
    }
    case 'assistant': {
      const nested = entry.parentToolUseId != null;
      return (
        <Box flexDirection="row">
          <Text color={nested ? 'gray' : 'white'}>{'● '}</Text>
          <Box flexDirection="column" flexGrow={1}>
            <Markdown text={entry.text} streaming={entry.streaming} indent={nested ? 6 : 2} />
          </Box>
        </Box>
      );
    }
    case 'thinking': {
      const body = entry.text.trim();
      const { shown, hidden } = truncateLines(splitLines(body), 3);
      return (
        <Box flexDirection="column">
          <Text dimColor italic>
            ✻ Thinking…
          </Text>
          {body !== '' && (
            <Box paddingLeft={2} flexDirection="column">
              <Text dimColor italic>
                {shown.join('\n')}
              </Text>
              {hidden > 0 && <Text dimColor>{`… +${plural(hidden, 'line')}`}</Text>}
            </Box>
          )}
        </Box>
      );
    }
    case 'tool':
      return <ToolView entry={entry} childEntries={childEntries} />;
    case 'system':
      if (entry.level === 'error') return <Text color="red">{`✗ ${entry.text}`}</Text>;
      if (entry.level === 'warn') return <Text color="yellow">{`⚠ ${entry.text}`}</Text>;
      return <Text color="gray">{entry.text}</Text>;
  }
}
