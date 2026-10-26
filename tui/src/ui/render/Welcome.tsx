import React from 'react';
import { Box, Text } from 'ink';
import { ACCENT } from './util.js';

export interface WelcomeProps {
  cwd: string;
  model?: string;
}

export function Welcome({ cwd, model }: WelcomeProps) {
  return (
    <Box flexDirection="column" borderStyle="round" borderColor={ACCENT} paddingX={1} alignSelf="flex-start">
      <Text>
        <Text color={ACCENT}>✻ </Text>
        <Text bold>Welcome to cfharness!</Text>
      </Text>
      <Box flexDirection="column" marginTop={1} paddingLeft={2}>
        <Text dimColor italic>
          /help for help
        </Text>
        <Text dimColor>{`cwd: ${cwd}`}</Text>
        {model && <Text dimColor>{`model: ${model}`}</Text>}
      </Box>
    </Box>
  );
}
