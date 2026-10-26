import { spawn } from 'node:child_process';

/** OSC 52: asks the terminal itself to set the clipboard (Windows Terminal, iTerm2, kitty, WezTerm, VS Code…). */
export const osc52 = (text: string) => `\x1b]52;c;${Buffer.from(text, 'utf8').toString('base64')}\x07`;

function pipeTo(cmd: string, args: string[], input: Buffer): Promise<boolean> {
  return new Promise((resolve) => {
    try {
      const p = spawn(cmd, args, { stdio: ['pipe', 'ignore', 'ignore'], windowsHide: true });
      p.on('error', () => resolve(false));
      p.on('close', (code) => resolve(code === 0));
      p.stdin.end(input);
    } catch {
      resolve(false);
    }
  });
}

/** The OS clipboard tool, for terminals without OSC 52. */
async function nativeCopy(text: string): Promise<boolean> {
  if (process.platform === 'win32') return pipeTo('clip', [], Buffer.from(text, 'utf16le'));
  if (process.platform === 'darwin') return pipeTo('pbcopy', [], Buffer.from(text));
  const data = Buffer.from(text);
  return (await pipeTo('wl-copy', [], data)) || pipeTo('xclip', ['-selection', 'clipboard'], data);
}

/** Copy with both OSC 52 and the OS tool: whichever the terminal and machine support wins. */
export async function copyToClipboard(text: string, write: (s: string) => void): Promise<void> {
  write(osc52(text));
  await nativeCopy(text);
}
