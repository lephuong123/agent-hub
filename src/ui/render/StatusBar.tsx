import React from 'react';
import { Text } from 'ink';
import path from 'node:path';
import type { SessionStatus } from '../../types.js';
import { formatCost, formatTokens } from './util.js';

export interface StatusBarProps {
  status: SessionStatus;
}

export function StatusBar({ status }: StatusBarProps) {
  const parts = [
    status.model ?? 'default model',
    path.basename(status.cwd) || status.cwd,
    status.sessionId ? status.sessionId.slice(0, 8) : '',
    status.permissionMode !== 'default' ? status.permissionMode : '',
    formatCost(status.totalCostUsd),
    `↑${formatTokens(status.inputTokens)} ↓${formatTokens(status.outputTokens)}`,
  ].filter(Boolean);
  return (
    <Text dimColor wrap="truncate-end">
      {parts.join(' · ')}
    </Text>
  );
}
