import React, { useMemo, type Ref } from 'react';
import { Box, Text } from 'ink';
import type { Entry, TaskInfo } from '../../types.js';
import { Clickable } from '../term/MouseContext.js';
import { ScrollView, type ScrollApi } from '../term/ScrollView.js';
import { Markdown } from '../render/Markdown.js';
import { MessageView, agentStats } from '../render/MessageView.js';
import { planTranscript } from '../render/plan.js';
import { useTicker } from '../render/ViewContext.js';
import { ACCENT, cleanResult, splitLines, toolDisplayName, toolSummary } from '../render/util.js';
import { taskLabel } from './TaskPanel.js';

type ToolEntry = Extract<Entry, { kind: 'tool' }>;

export interface AgentViewProps {
  entries: Entry[];
  toolUseId: string;
  task?: TaskInfo;
  tasksByTool: Map<string, TaskInfo>;
  expanded: Set<string>;
  all: boolean;
  scrollRef: Ref<ScrollApi>;
  onBack(): void;
  onStop?(): void;
}

/** Everything one subagent (or background task) did: its prompt, its own transcript, and its result. */
export function AgentView({ entries, toolUseId, task, tasksByTool, expanded, all, scrollRef, onBack, onStop }: AgentViewProps) {
  const entry = entries.find((e): e is ToolEntry => e.kind === 'tool' && e.id === toolUseId);
  const { items } = useMemo(() => planTranscript(entries, toolUseId), [entries, toolUseId]);
  const running = task?.status === 'running' || entry?.status === 'running';
  useTicker(running);
  const tools = items.filter((i) => i.entry.kind === 'tool').length;
  const prompt = typeof entry?.input.prompt === 'string' ? entry.input.prompt : '';
  const result = cleanResult(entry?.result);
  const title = entry ? `${toolDisplayName(entry.name)}(${toolSummary(entry.name, entry.input)})` : task?.description ?? toolUseId;
  const status = task?.status ?? (entry?.status === 'running' ? 'running' : entry?.status === 'error' ? 'failed' : 'completed');

  return (
    <Box flexDirection="column" flexGrow={1} flexShrink={1} flexBasis={0} minHeight={0}>
      <Box borderStyle="round" borderColor={ACCENT} paddingX={1} flexDirection="column" flexShrink={0}>
        <Box flexDirection="row" justifyContent="space-between">
          <Text wrap="truncate-end">
            <Text color={ACCENT}>{task ? `${taskLabel(task)} ` : ''}</Text>
            <Text bold>{title}</Text>
          </Text>
          <Box flexShrink={0} gap={2}>
            {running && onStop && (
              <Clickable onClick={onStop}>
                <Text color="red">[x stop]</Text>
              </Clickable>
            )}
            <Clickable onClick={onBack}>
              <Text color="cyan">[esc back]</Text>
            </Clickable>
          </Box>
        </Box>
        <Text dimColor>
          {`${status}${task?.backgrounded ? ' · in background' : ''} · ${entry ? agentStats(entry, tools, task) : ''}`}
        </Text>
        {task?.summary && <Text>{task.summary}</Text>}
      </Box>
      <ScrollView apiRef={scrollRef}>
        {prompt && (
          <Box flexDirection="column" marginTop={1}>
            <Text bold>Prompt</Text>
            <Box paddingLeft={2}>
              <Text color="gray">{prompt}</Text>
            </Box>
          </Box>
        )}
        {items.map((item) => (
          <MessageView
            key={item.entry.id}
            entry={item.entry}
            childEntries={item.children}
            task={tasksByTool.get(item.entry.id)}
            expanded={all || expanded.has(item.entry.id)}
            nested={false}
          />
        ))}
        {items.length === 0 && !result && (
          <Box marginTop={1}>
            <Text dimColor>{running ? 'Waiting for the first message…' : 'No messages were recorded for this task.'}</Text>
          </Box>
        )}
        {result && (
          <Box flexDirection="column" marginTop={1}>
            <Text bold>{entry?.status === 'error' ? 'Error' : 'Result'}</Text>
            <Box paddingLeft={2} flexDirection="column">
              {entry?.status === 'error' ? <Text color="red">{splitLines(result).join('\n')}</Text> : <Markdown text={result} indent={2} />}
            </Box>
          </Box>
        )}
      </ScrollView>
    </Box>
  );
}
