import React, { useMemo } from 'react';
import { Text, useWindowSize } from 'ink';
import { Marked, type Tokens } from 'marked';
import { markedTerminal } from 'marked-terminal';
import chalk from 'chalk';
import { closeOpenFences, PERMISSION } from './util.js';

const id = (s: string) => s;
const instances = new Map<number, Marked>();
const cache = new Map<string, string>();
const CACHE_MAX = 500;

function markedFor(width: number): Marked {
  let m = instances.get(width);
  if (!m) {
    m = new Marked();
    m.use(
      markedTerminal({
        width,
        reflowText: true,
        showSectionPrefix: false,
        tab: 2,
        heading: chalk.bold,
        firstHeading: chalk.bold,
        code: chalk.reset,
        codespan: chalk.hex(PERMISSION),
        blockquote: chalk.dim.italic,
        paragraph: id,
        listitem: id,
        table: id,
        hr: chalk.dim,
        link: chalk.hex(PERMISSION),
        href: chalk.hex(PERMISSION).underline,
      }),
      {
        // marked-terminal prints tight list items raw; render their inline tokens.
        renderer: {
          text(this: { parser: { parseInline(t: unknown[]): string } }, token: Tokens.Text | Tokens.Escape) {
            return 'tokens' in token && token.tokens ? this.parser.parseInline(token.tokens) : false;
          },
        },
      },
    );
    instances.set(width, m);
  }
  return m;
}

/** Markdown → ANSI string. Pure and cached; safe on partial (streaming) input. */
export function renderMarkdown(text: string, width = 80): string {
  const key = `${width}\u0000${text}`;
  const hit = cache.get(key);
  if (hit !== undefined) return hit;
  let out: string;
  try {
    out = (markedFor(Math.max(20, width)).parse(closeOpenFences(text), { async: false }) as string)
      .replace(/\r/g, '')
      .replace(/\n+$/, '');
  } catch {
    out = text;
  }
  if (cache.size >= CACHE_MAX) cache.delete(cache.keys().next().value!);
  cache.set(key, out);
  return out;
}

export interface MarkdownProps {
  text: string;
  streaming?: boolean;
  /** Columns consumed by surrounding indentation. */
  indent?: number;
}

export function Markdown({ text, streaming, indent = 0 }: MarkdownProps) {
  const { columns } = useWindowSize();
  // One column for the fullscreen scrollbar, one so wrapped lines never touch the edge.
  const width = Math.max(20, (columns || 80) - indent - 2);
  const out = useMemo(() => renderMarkdown(streaming ? text : text.trimEnd(), width), [text, streaming, width]);
  return <Text>{out}</Text>;
}
