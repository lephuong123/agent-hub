#!/usr/bin/env python3
"""A-03: ngắt giữa lượt (đang chạy Bash `sleep 30`) rồi `--resume <session-id>`.
Dùng: python3 a03_interrupt.py <mode>
  mode = sigint | sigterm | control   (control = gửi control_request interrupt qua stdin stream-json)
Pha 1 ghi fixtures/a03-<mode>-p1.jsonl ; pha 2 (resume) ghi fixtures/a03-<mode>-p2.jsonl ; dòng thời gian in ra màn hình."""
import json, os, signal, subprocess, sys, time, uuid
M0 = os.path.dirname(os.path.abspath(__file__))
mode = sys.argv[1]
sid = str(uuid.uuid4())
ISO = ['--model', 'haiku', '--max-budget-usd', '0.3', '--output-format', 'stream-json', '--verbose',
       '--setting-sources', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
       '--settings', os.path.join(M0, 'settings', 'hub-hooks.json'), '--tools', 'Bash']
PROMPT = 'Use the Bash tool in the FOREGROUND (do not use run_in_background) to run exactly: python3 -c "import time; time.sleep(30); print(1)" && echo late-done . Then reply with one short sentence.'
t0 = time.time()
def note(s): print('%6.2fs %s' % (time.time() - t0, s), flush=True)
def brief(d):
    t, st = d.get('type'), d.get('subtype')
    if t == 'assistant': return 'assistant ' + ','.join(c['type'] + (':' + c.get('text', '')[:100] if c['type'] == 'text' else '') for c in d['message']['content'])
    if t == 'user':
        c = d['message']['content']; return 'user ' + (c[:80] if isinstance(c, str) else json.dumps(c, ensure_ascii=False)[:160])
    if t == 'result': return 'result ' + json.dumps({k: d.get(k) for k in ('subtype', 'is_error', 'terminal_reason', 'stop_reason', 'num_turns', 'total_cost_usd', 'result', 'errors')}, ensure_ascii=False)[:400]
    if t == 'control_response': return 'control_response ' + json.dumps(d)[:200]
    return f'{t}/{st}' + (' ' + json.dumps({k: v for k, v in d.items() if k in ('status', 'description', 'summary')})[:150] if t == 'system' and st != 'init' else '')
# ---- pha 1
if mode == 'control':
    argv = ['claude', '-p', '--input-format', 'stream-json', '--session-id', sid] + ISO
    p = subprocess.Popen(argv, cwd=os.path.join(M0, 'work'), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
    p.stdin.write(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': PROMPT}}) + '\n'); p.stdin.flush()
else:
    argv = ['claude', '-p', PROMPT, '--session-id', sid] + ISO
    p = subprocess.Popen(argv, cwd=os.path.join(M0, 'work'), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
out = open(os.path.join(M0, 'fixtures', f'a03-{mode}-p1.jsonl'), 'w')
fired = None
for line in p.stdout:
    out.write(line); out.flush()
    try: d = json.loads(line)
    except Exception: note('non-json ' + line[:100]); continue
    if d.get('type') == 'stream_event' or d.get('subtype') == 'thinking_tokens': continue
    note(brief(d))
    if fired is None and d.get('type') == 'system' and d.get('subtype') in ('task_started',) or (d.get('type') == 'assistant' and any(c['type'] == 'tool_use' for c in d['message']['content']) and fired is None and not mode.startswith('x')):
        time.sleep(3)
        fired = time.time()
        if mode == 'sigint': os.kill(p.pid, signal.SIGINT); note('>> SIGINT sent to claude pid')
        elif mode == 'sigterm': os.kill(p.pid, signal.SIGTERM); note('>> SIGTERM sent to claude pid')
        else:
            p.stdin.write(json.dumps({'type': 'control_request', 'request_id': 'int-1', 'request': {'subtype': 'interrupt'}}) + '\n'); p.stdin.flush(); note('>> control_request interrupt sent')
    if mode == 'control' and d.get('type') == 'result':
        note('>> closing stdin'); p.stdin.close()
rc = p.wait(); note('pha1 exit=%s (%.2fs sau tín hiệu) stderr=%s' % (rc, time.time() - (fired or t0), p.stderr.read()[:300].replace('\n', ' ')))
# còn tiến trình `sleep 30` mồ côi không?
note('ps time.sleep(30): ' + subprocess.run("pgrep -fl 'time.sleep(30)' || true", shell=True, capture_output=True, text=True).stdout.strip())
# ---- pha 2: resume
argv2 = ['claude', '-p', 'Earlier in this conversation you started a Bash command. What exact command was it, and did you receive its output? Reply in one sentence and do not run any tools.', '--resume', sid] + ISO
r = subprocess.run(argv2, cwd=os.path.join(M0, 'work'), stdin=subprocess.DEVNULL, capture_output=True, text=True)
open(os.path.join(M0, 'fixtures', f'a03-{mode}-p2.jsonl'), 'w').write(r.stdout)
for line in r.stdout.splitlines():
    d = json.loads(line)
    if d.get('type') in ('result',) or (d.get('type') == 'system' and d.get('subtype') == 'init'):
        note('pha2 ' + (brief(d) if d['type'] == 'result' else 'init session_id=%s (sid=%s)' % (d['session_id'], sid)))
note('pha2 exit=%s stderr=%s' % (r.returncode, r.stderr[:300]))
