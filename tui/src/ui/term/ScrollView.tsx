import React, { useImperativeHandle, useRef, useState, type ReactNode, type Ref } from 'react';
import { Box, Text, useBoxMetrics, type DOMElement } from 'ink';
import { Clickable, screenRect, useMouseTarget } from './MouseContext.js';

export interface ScrollApi {
  scrollBy(rows: number): void;
  page(direction: -1 | 1): void;
  toTop(): void;
  toBottom(): void;
}

export const WHEEL_ROWS = Math.max(1, Number(process.env.CFH_SCROLL_SPEED) || 3);

/** Thumb size and position for a scrollbar `height` rows tall. */
export function thumb(height: number, content: number, offset: number): { start: number; size: number } {
  if (height <= 0 || content <= height) return { start: 0, size: height };
  const size = Math.max(1, Math.round((height * height) / content));
  const max = content - height;
  const start = Math.round(((height - size) * Math.min(offset, max)) / max);
  return { start, size };
}

/**
 * A viewport that clips its children and scrolls them with contentOffsetY. While scrolled to the
 * bottom it stays pinned there as content grows (streaming); scrolling up unpins it.
 */
export function ScrollView({ children, apiRef }: { children: ReactNode; apiRef?: Ref<ScrollApi> }) {
  const viewport = useRef<DOMElement>(null);
  const content = useRef<DOMElement>(null);
  const vp = useBoxMetrics(viewport);
  const ct = useBoxMetrics(content);
  const [pos, setPos] = useState({ offset: 0, pinned: true });

  const height = vp.clientHeight;
  const max = Math.max(0, ct.height - height);
  const offset = pos.pinned ? max : Math.min(pos.offset, max);

  const live = useRef({ offset, max, height });
  live.current = { offset, max, height };

  const scrollTo = (target: number) => {
    const { max: m } = live.current;
    const next = Math.max(0, Math.min(Math.round(target), m));
    setPos({ offset: next, pinned: next >= m });
  };
  const api: ScrollApi = {
    scrollBy: (rows) => scrollTo(live.current.offset + rows),
    page: (dir) => scrollTo(live.current.offset + dir * Math.max(1, live.current.height - 2)),
    toTop: () => scrollTo(0),
    toBottom: () => setPos({ offset: live.current.max, pinned: true }),
  };
  useImperativeHandle(apiRef, () => api);

  useMouseTarget(viewport, {
    onWheel: (e) => {
      api.scrollBy(e.action === 'wheelUp' ? -WHEEL_ROWS : WHEEL_ROWS);
      return true;
    },
  });

  const scrollable = max > 0;

  return (
    <Box flexDirection="column" flexGrow={1} flexShrink={1} flexBasis={0} minHeight={0}>
      <Box flexDirection="row" flexGrow={1} flexShrink={1} flexBasis={0} minHeight={0}>
        <Box ref={viewport} flexDirection="column" flexGrow={1} flexShrink={1} overflow="hidden" contentOffsetY={offset}>
          <Box ref={content} flexDirection="column" flexShrink={0}>
            {children}
          </Box>
        </Box>
        <ScrollBar height={height} content={ct.height} offset={offset} onJump={(f) => scrollTo(f * live.current.max)} />
      </Box>
      {!pos.pinned && scrollable && (
        <Clickable onClick={api.toBottom} paddingX={2}>
          <Text dimColor>{`↓ ${max - offset} more ${max - offset === 1 ? 'line' : 'lines'} · click, End or wheel to jump to bottom`}</Text>
        </Clickable>
      )}
    </Box>
  );
}

/** One-column scrollbar; clicking it jumps to that fraction of the content. Blank when nothing scrolls. */
function ScrollBar({ height, content, offset, onJump }: { height: number; content: number; offset: number; onJump(f: number): void }) {
  const ref = useRef<DOMElement>(null);
  const scrollable = content > height && height > 0;
  useMouseTarget(ref, {
    onClick: (e) => {
      const r = ref.current && screenRect(ref.current);
      if (scrollable && r && height > 1) onJump((e.y - r.rect.y) / (height - 1));
    },
  });
  const { start, size } = thumb(height, content, offset);
  return (
    <Box ref={ref} width={1} flexDirection="column" flexShrink={0} overflow="hidden">
      {scrollable &&
        Array.from({ length: height }, (_, i) =>
          i >= start && i < start + size ? (
            <Text key={i} color="gray">
              ┃
            </Text>
          ) : (
            <Text key={i} dimColor>
              │
            </Text>
          ),
        )}
    </Box>
  );
}
