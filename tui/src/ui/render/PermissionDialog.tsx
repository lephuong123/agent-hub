import React, { useState } from 'react';
import { Box, Text, useInput, useStdin } from 'ink';
import type { PermissionDecision, PermissionRequest } from '../../types.js';
import { DiffView } from './MessageView.js';
import { buildDiff, PERMISSION, toolDisplayName, truncate } from './util.js';

export interface PermissionDialogProps {
  request: PermissionRequest;
  onDecision: (decision: PermissionDecision) => void;
}

function title(toolName: string): string {
  switch (toolName) {
    case 'Bash':
      return 'Bash command';
    case 'Edit':
    case 'MultiEdit':
      return 'Edit file';
    case 'Write':
      return 'Write file';
    default:
      return `Use ${toolDisplayName(toolName)}`;
  }
}

function Preview({ request }: { request: PermissionRequest }) {
  const { toolName, input } = request;
  if (toolName === 'Bash') {
    return (
      <Box flexDirection="column" paddingLeft={2}>
        <Text>{String(input.command ?? '')}</Text>
        {typeof input.description === 'string' && <Text dimColor>{input.description}</Text>}
      </Box>
    );
  }
  const diff = buildDiff(toolName, input);
  if (diff) {
    return (
      <Box flexDirection="column" paddingLeft={2}>
        <Text bold>{String(input.file_path ?? '')}</Text>
        <DiffView lines={diff} maxLines={16} />
      </Box>
    );
  }
  return (
    <Box paddingLeft={2}>
      <Text dimColor>{truncate(JSON.stringify(input, null, 2), 1200)}</Text>
    </Box>
  );
}

export function PermissionDialog({ request, onDecision }: PermissionDialogProps) {
  const options: { label: string; decision: PermissionDecision; n: number }[] = [
    { label: 'Yes', decision: 'allow', n: 1 },
    ...(request.canAllowAlways ? [{ label: "Yes, and don't ask again this session", decision: 'allow_always' as const, n: 2 }] : []),
    { label: 'No, and tell Claude what to do differently (esc)', decision: 'deny', n: request.canAllowAlways ? 3 : 2 },
  ];
  const [selected, setSelected] = useState(0);

  const { isRawModeSupported } = useStdin();
  useInput(
    (input, key) => {
      if (key.escape) return onDecision('deny');
      if (key.upArrow) return setSelected((s) => (s - 1 + options.length) % options.length);
      if (key.downArrow || key.tab) return setSelected((s) => (s + 1) % options.length);
      if (key.return) return onDecision(options[selected]!.decision);
      const opt = options.find((o) => String(o.n) === input);
      if (opt) onDecision(opt.decision);
    },
    { isActive: Boolean(isRawModeSupported) },
  );

  return (
    <Box flexDirection="column" borderStyle="round" borderColor={PERMISSION} paddingX={1} marginTop={1}>
      <Text bold color={PERMISSION}>
        {title(request.toolName)}
      </Text>
      <Box marginY={1} flexDirection="column">
        <Preview request={request} />
      </Box>
      {request.reason && <Text dimColor>{request.reason}</Text>}
      <Text>Do you want to proceed?</Text>
      {options.map((o, i) => (
        <Text key={o.decision} color={i === selected ? PERMISSION : undefined}>
          {`${i === selected ? '❯' : ' '} ${o.n}. ${o.label}`}
        </Text>
      ))}
    </Box>
  );
}
