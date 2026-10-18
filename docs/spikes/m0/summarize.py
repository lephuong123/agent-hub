#!/usr/bin/env python3
"""Tóm tắt một file stream-json của `claude -p` và ghi chi phí vào costs.tsv.
Dùng: python3 summarize.py fixtures/<name>.jsonl [--no-ledger]"""
import json, sys, os
path = sys.argv[1]
ledger = '--no-ledger' not in sys.argv
cost = None
for line in open(path):
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        print('NON-JSON:', line[:200]); continue
    t, st = d.get('type'), d.get('subtype')
    if t == 'system' and st == 'init':
        print('INIT cwd=%s perm=%s apiKeySource=%s' % (d.get('cwd'), d.get('permissionMode'), d.get('apiKeySource')))
        print('  tools(%d)=%s' % (len(d.get('tools', [])), d.get('tools')[:25]))
        print('  mcp_servers=%s' % [(m['name'], m['status'], m.get('source')) for m in d.get('mcp_servers', [])])
        print('  skills(%d)=%s' % (len(d.get('skills', [])), d.get('skills')[:12]))
        print('  plugins=%s' % [p['name'] for p in d.get('plugins', [])])
        print('  memory_paths=%s' % d.get('memory_paths'))
    elif t == 'system' and st in ('hook_started', 'hook_response'):
        print('HOOK %s %s %s' % (st, d.get('hook_name'), (str(d.get('output') or d.get('stdout') or ''))[:160]))
    elif t == 'system':
        print('SYSTEM', st, {k: v for k, v in d.items() if k not in ('type', 'subtype', 'uuid', 'session_id')} if st not in ('thinking_tokens',) else '')
    elif t == 'assistant':
        for c in d['message']['content']:
            if c['type'] == 'text': print('ASSISTANT text:', c['text'][:300].replace('\n', ' | '))
            elif c['type'] == 'tool_use': print('ASSISTANT tool_use:', c['name'], json.dumps(c['input'], ensure_ascii=False)[:200])
    elif t == 'user':
        c = d['message']['content']
        if isinstance(c, list):
            for x in c:
                if x.get('type') == 'tool_result':
                    cc = x.get('content'); cc = cc if isinstance(cc, str) else json.dumps(cc, ensure_ascii=False)
                    print('USER tool_result is_error=%s: %s' % (x.get('is_error'), cc[:250].replace('\n', ' | ')))
                elif x.get('type') == 'text': print('USER text:', x['text'][:200])
        else: print('USER str:', str(c)[:200])
    elif t == 'result':
        cost = d.get('total_cost_usd')
        print('RESULT', {k: d.get(k) for k in ('subtype', 'is_error', 'stop_reason', 'terminal_reason', 'num_turns', 'total_cost_usd', 'permission_denials', 'errors')})
        print('  result=', str(d.get('result'))[:300])
    elif t not in ('stream_event', 'rate_limit_event'):
        print('OTHER', t, st, str(d)[:200])
if ledger and cost is not None:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'costs.tsv'), 'a') as f:
        f.write('%s\t%s\n' % (os.path.basename(path), cost))
