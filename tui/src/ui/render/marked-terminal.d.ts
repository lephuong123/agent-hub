declare module 'marked-terminal' {
  import type { MarkedExtension } from 'marked';
  type Style = (s: string) => string;
  export interface TerminalRendererOptions {
    code?: Style;
    blockquote?: Style;
    html?: Style;
    heading?: Style;
    firstHeading?: Style;
    hr?: Style;
    listitem?: Style;
    table?: Style;
    paragraph?: Style;
    strong?: Style;
    em?: Style;
    codespan?: Style;
    del?: Style;
    link?: Style;
    href?: Style;
    text?: Style;
    unescape?: boolean;
    emoji?: boolean;
    width?: number;
    showSectionPrefix?: boolean;
    reflowText?: boolean;
    tab?: number | string;
    tableOptions?: Record<string, unknown>;
  }
  export function markedTerminal(options?: TerminalRendererOptions, highlightOptions?: Record<string, unknown>): MarkedExtension;
  const Renderer: unknown;
  export default Renderer;
}
