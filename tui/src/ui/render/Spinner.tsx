import React, { useEffect, useState } from 'react';
import { Text } from 'ink';
import { ACCENT, formatElapsed, formatTokens } from './util.js';

const FRAMES_UNICODE = ['·', '✢', '✳', '✶', '✻', '✽', '✻', '✶', '✳', '✢'];
const FRAMES_ASCII = ['-', '\\', '|', '/'];

export interface SpinnerProps {
  activity?: string;
  /** Elapsed time; ignored when `startedAt` is given (then computed live each frame). */
  elapsedMs?: number;
  startedAt?: number;
  tokens?: number;
  /** Extra hint before "esc to interrupt", e.g. "ctrl+b to run in background". */
  hint?: string;
}

export function Spinner({ activity = 'Thinking', elapsedMs = 0, startedAt, tokens = 0, hint }: SpinnerProps) {
  const frames = process.env.CFH_ASCII ? FRAMES_ASCII : FRAMES_UNICODE;
  const [frame, setFrame] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setFrame((f) => f + 1), 120);
    return () => clearInterval(t);
  }, []);
  const elapsed = startedAt != null ? Date.now() - startedAt : elapsedMs;
  const parts = [formatElapsed(elapsed), tokens > 0 ? `↓ ${formatTokens(tokens)} tokens` : '', hint ?? '', 'esc to interrupt'].filter(Boolean);
  return (
    <Text>
      <Text color={ACCENT}>{`${frames[frame % frames.length]} ${activity.replace(/…$/, '')}… `}</Text>
      <Text dimColor>{`(${parts.join(' · ')})`}</Text>
    </Text>
  );
}
