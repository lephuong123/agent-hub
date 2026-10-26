export const PASTE_MAX_CHARS = 800;
export const PASTE_MAX_LINES = 10;

const lineCount = (t: string): number => t.replace(/\n$/, '').split('\n').length;

export const isLargePaste = (t: string): boolean => t.length > PASTE_MAX_CHARS || lineCount(t) > PASTE_MAX_LINES;

export function pasteToken(id: number, text: string): string {
  const n = lineCount(text);
  return `[Pasted text #${id} +${n} line${n === 1 ? '' : 's'}]`;
}

/** Replace every placeholder token still present in `value` with its full text. */
export function expandPastes(value: string, pastes: ReadonlyMap<string, string>): string {
  let out = value;
  for (const [token, text] of pastes) out = out.split(token).join(text);
  return out;
}
