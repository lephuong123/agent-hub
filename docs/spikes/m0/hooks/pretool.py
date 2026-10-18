#!/usr/bin/env python3
"""Hook PreToolUse mẫu cho spike M0 (mô phỏng bộ phân loại FR-SG-03 của hub).
- Đọc JSON từ stdin, ghi nguyên văn vào $HOOK_LOG (mặc định fixtures/hook-inputs.jsonl).
- Quyết định theo lệnh Bash:
    chứa 'rm -rf'      -> deny  (phá huỷ)
    chứa 'ASKME'       -> ask   (đẩy về cơ chế hỏi của CLI)
    chứa 'SLOWAPPROVE' -> ngủ $HOOK_SLEEP giây (mặc định 150) rồi allow  (A-13)
    còn lại            -> allow
- Ghi quyết định + thời điểm vào log để đối chiếu."""
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
log = os.environ.get('HOOK_LOG', os.path.join(here, '..', 'fixtures', 'hook-inputs.jsonl'))
raw = sys.stdin.read()
t0 = time.time()
try:
    data = json.loads(raw)
except Exception:
    data = {'_raw': raw}
cmd = json.dumps(data.get('tool_input', {}))
mode = os.environ.get('HOOK_MODE', '')
if mode == 'crash':          # mô phỏng hook lỗi: exit 1, không in gì
    sys.exit(1)
if mode == 'exit2':          # mô phỏng chặn bằng exit code 2 + stderr
    print('hub guard (exit2): blocked', file=sys.stderr); sys.exit(2)
decision, reason = 'allow', 'hub: safe/write -> allow'
if mode == 'allowall':      # mô phỏng: người đã duyệt -> hook trả allow cho mọi thứ
    pass
elif 'rm -rf' in cmd:
    decision, reason = 'deny', 'hub guard: destructive operation (rm -rf) is blocked pending human approval'
elif 'ASKME' in cmd:
    decision, reason = 'ask', 'hub: needs confirmation'
elif 'SLOWAPPROVE' in cmd:
    time.sleep(float(os.environ.get('HOOK_SLEEP', '150')))
    decision, reason = 'allow', 'hub: approved after wait'
out = {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                              'permissionDecision': decision,
                              'permissionDecisionReason': reason}}
with open(log, 'a') as f:
    f.write(json.dumps({'ts_start': t0, 'ts_end': time.time(), 'pid': os.getpid(),
                        'input': data, 'output': out}, ensure_ascii=False) + '\n')
print(json.dumps(out))
sys.exit(0)
