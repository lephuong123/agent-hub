import React, { createContext, useContext, useEffect, useMemo, useRef, type ReactNode, type RefObject } from 'react';
import { Box, type BoxProps, type DOMElement } from 'ink';
import type { TermMouseEvent } from './mouse.js';

export interface MouseHandlers {
  onClick?(e: TermMouseEvent): void;
  /** Return true when handled; otherwise the event bubbles to the next target under the pointer. */
  onWheel?(e: TermMouseEvent): boolean | void;
}

interface Target {
  ref: RefObject<DOMElement | null>;
  handlers: RefObject<MouseHandlers>;
}

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

interface Registry {
  enabled: boolean;
  add(t: Target): () => void;
}

const MouseCtx = createContext<Registry>({ enabled: false, add: () => () => {} });

const clipOffset = (v: number | undefined) => (typeof v === 'number' && Number.isFinite(v) ? Math.trunc(v) : 0);

/**
 * Where `node` is drawn on screen, and the part of it that is visible after the overflow="hidden"
 * ancestors clip it. Mirrors Ink's renderNodeToOutput: an ancestor's contentOffsetX/Y shifts its children.
 */
export function screenRect(node: DOMElement): { rect: Rect; visible: Rect | null; depth: number } | null {
  const chain: DOMElement[] = [];
  for (let n: DOMElement | undefined = node; n; n = n.parentNode) chain.unshift(n);
  let ox = 0;
  let oy = 0;
  let clip = { x1: -Infinity, y1: -Infinity, x2: Infinity, y2: Infinity };
  for (const n of chain) {
    const yoga = n.yogaNode;
    if (!yoga) continue;
    const x = ox + yoga.getComputedLeft();
    const y = oy + yoga.getComputedTop();
    const width = yoga.getComputedWidth();
    const height = yoga.getComputedHeight();
    if (n === node) {
      const x1 = Math.max(clip.x1, x);
      const y1 = Math.max(clip.y1, y);
      const x2 = Math.min(clip.x2, x + width);
      const y2 = Math.min(clip.y2, y + height);
      const visible = x2 > x1 && y2 > y1 ? { x: x1, y: y1, width: x2 - x1, height: y2 - y1 } : null;
      return { rect: { x, y, width, height }, visible, depth: chain.length };
    }
    const s = n.style;
    if ((s.overflowX ?? s.overflow) === 'hidden') clip = { ...clip, x1: Math.max(clip.x1, x), x2: Math.min(clip.x2, x + width) };
    if ((s.overflowY ?? s.overflow) === 'hidden') clip = { ...clip, y1: Math.max(clip.y1, y), y2: Math.min(clip.y2, y + height) };
    ox = x - clipOffset(s.contentOffsetX);
    oy = y - clipOffset(s.contentOffsetY);
  }
  return null;
}

const inside = (r: Rect, x: number, y: number) => x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height;

/** Targets under (x, y), innermost first. */
function hits(targets: Iterable<Target>, x: number, y: number): Target[] {
  const found: { t: Target; depth: number }[] = [];
  for (const t of targets) {
    const node = t.ref.current;
    if (!node) continue;
    const r = screenRect(node);
    if (r?.visible && inside(r.visible, x, y)) found.push({ t, depth: r.depth });
  }
  return found.sort((a, b) => b.depth - a.depth).map((f) => f.t);
}

export interface MouseSource {
  onMouse(listener: (e: TermMouseEvent) => void): () => void;
}

/**
 * Routes mouse events from `source` to the innermost registered target under the pointer.
 * A click is a press and release on the same target. Wheel events bubble outwards until one is
 * handled, then fall back to `onUnhandledWheel`.
 */
export function MouseProvider({
  source,
  onUnhandledWheel,
  children,
}: {
  source: MouseSource | null;
  onUnhandledWheel?: (e: TermMouseEvent) => void;
  children: ReactNode;
}) {
  const targets = useRef(new Set<Target>());
  const fallback = useRef(onUnhandledWheel);
  fallback.current = onUnhandledWheel;

  useEffect(() => {
    if (!source) return;
    let pressed: Target | undefined;
    return source.onMouse((e) => {
      if (e.action === 'wheelUp' || e.action === 'wheelDown') {
        for (const t of hits(targets.current, e.x, e.y)) if (t.handlers.current.onWheel?.(e)) return;
        fallback.current?.(e);
        return;
      }
      if (e.button !== 'left') return;
      if (e.action === 'press') pressed = hits(targets.current, e.x, e.y).find((t) => t.handlers.current.onClick);
      else if (e.action === 'release') {
        const target = pressed;
        pressed = undefined;
        if (target && hits(targets.current, e.x, e.y).includes(target)) target.handlers.current.onClick?.(e);
      }
    });
  }, [source]);

  const registry = useMemo<Registry>(
    () => ({
      enabled: !!source,
      add: (t) => {
        targets.current.add(t);
        return () => targets.current.delete(t);
      },
    }),
    [source],
  );
  return <MouseCtx.Provider value={registry}>{children}</MouseCtx.Provider>;
}

export const useMouseEnabled = () => useContext(MouseCtx).enabled;

/** Register `ref` as a mouse target. Handlers may change every render. */
export function useMouseTarget(ref: RefObject<DOMElement | null>, handlers: MouseHandlers): void {
  const registry = useContext(MouseCtx);
  const h = useRef(handlers);
  h.current = handlers;
  useEffect(() => registry.add({ ref, handlers: h }), [registry, ref]);
}

/** A Box that reacts to clicks (and optionally the wheel). */
export function Clickable({ onClick, onWheel, children, ...box }: MouseHandlers & BoxProps & { children?: ReactNode }) {
  const ref = useRef<DOMElement>(null);
  useMouseTarget(ref, { onClick, onWheel });
  return (
    <Box ref={ref} {...box}>
      {children}
    </Box>
  );
}
