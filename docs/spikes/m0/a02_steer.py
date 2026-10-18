#!/usr/bin/env python3
"""A-02: steer qua stdin stream-json khi lượt 1 đang chạy tool.
Dùng: python3 a02_steer.py [mode]   mode = midtool (mặc định) | twoturns
 - midtool : gửi tin thứ 2 ngay khi thấy assistant tool_use (tool `sleep 15` đang chạy).
 - twoturns: chờ result lượt 1 rồi mới gửi tin thứ 2 (đối chứng: hội thoại nhiều lượt trên một tiến trình).
Ghi stdout nguyên văn vào fixtures/a02-steer-<mode>.jsonl, dòng thời gian (giây) vào fixtures/a02-steer-<mode>.timeline.txt."""
import json, os, subprocess, sys, time, uuid
M0 = os.path.dirname(os.path.abspath(__file__))
mode = sys.argv[1] if len(sys.argv) > 1 else 'midtool'
sid = str(uuid.uuid4())
argv = ['claude', '-p', '--model', 'haiku', '--max-budget-usd', '0.3',
        '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose',
        '--replay-user-messages', '--include-hook-events',
        '--setting-sources', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
        '--settings', os.path.join(M0, 'settings', 'hub-hooks.json'),
        '--tools', 'Bash', '--session-id', sid]
def umsg(text):
    return json.dumps({'type': 'user', 'message': {'role': 'user', 'content': text}}) + '\n'
MSG1 = 'Use the Bash tool to run exactly: sleep 15 && echo first-done . After it finishes, reply with one short sentence.'
MSG2 = 'STEER from operator: before finishing, ALSO run the Bash command `echo steer-received`, and include the word PINEAPPLE in your final answer.'
p = subprocess.Popen(argv, cwd=os.path.join(M0, 'work'), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True, bufsize=1)
t0 = time.time()
out = open(os.path.join(M0, 'fixtures', f'a02-steer-{mode}.jsonl'), 'w')
tl = open(os.path.join(M0, 'fixtures', f'a02-steer-{mode}.timeline.txt'), 'w')
def note(s):
    line = '%6.2fs %s' % (time.time() - t0, s); print(line); tl.write(line + '\n'); tl.flush()
p.stdin.write(umsg(MSG1)); p.stdin.flush(); note('>> sent MSG1')
sent2 = False; results = 0
for line in p.stdout:
    out.write(line); out.flush()
    try: d = json.loads(line)
    except Exception: note('non-json ' + line[:100]); continue
    t, st = d.get('type'), d.get('subtype')
    if t == 'stream_event' or st == 'thinking_tokens': continue
    desc = f'{t}/{st}' if st else t
    if t == 'assistant':
        desc += ' ' + ' '.join(c['type'] + (':' + c.get('name', '') + ':' + json.dumps(c.get('input'))[:80] if c['type'] == 'tool_use' else (':' + c.get('text', '')[:120] if c['type'] == 'text' else '')) for c in d['message']['content'])
    elif t == 'user':
        c = d['message']['content']
        desc += (' replay/text:' + (c if isinstance(c, str) else json.dumps(c))[:100]) if isinstance(c, str) or any(x.get('type') == 'text' for x in c) else ' tool_result:' + json.dumps(c)[:100]
        desc += ' isReplay=%s' % d.get('isReplay')
    elif t == 'result':
        results += 1; desc += ' cost=%s turns=%s result=%s' % (d.get('total_cost_usd'), d.get('num_turns'), str(d.get('result'))[:150])
    else:
        desc += ' ' + json.dumps({k: v for k, v in d.items() if k not in ('type', 'subtype', 'uuid', 'session_id', 'tools', 'skills', 'plugins', 'mcp_servers', 'slash_commands', 'agents', 'capabilities')})[:160]
    note(desc)
    if not sent2 and ((mode == 'midtool' and t == 'assistant' and any(c['type'] == 'tool_use' for c in d['message']['content']))
                      or (mode == 'twoturns' and t == 'result')):
        time.sleep(1.0)
        p.stdin.write(umsg(MSG2)); p.stdin.flush(); sent2 = True; note('>> sent MSG2 (steer)')
    if t == 'result' and sent2 and (results >= 2 or mode == 'midtool' and results >= 1 and 'PINEAPPLE' in str(d.get('result'))):
        note('>> closing stdin'); p.stdin.close()
p.wait(); note('exit=%s stderr=%s' % (p.returncode, p.stderr.read()[:300]))
