#!/usr/bin/env python3
"""Tính tổng chi phí thực của spike từ mọi fixture *.jsonl.
`total_cost_usd` là LUỸ KẾ theo session (kể cả qua --resume), nên lấy max theo session_id rồi cộng."""
import json, glob, os
M0 = os.path.dirname(os.path.abspath(__file__))
per_session = {}
for f in glob.glob(os.path.join(M0, 'fixtures', '**', '*.jsonl'), recursive=True):
    for line in open(f):
        try: d = json.loads(line)
        except Exception: continue
        if d.get('type') == 'result' and d.get('total_cost_usd') is not None:
            s = d.get('session_id')
            per_session[s] = max(per_session.get(s, 0), d['total_cost_usd'])
naive = sum(float(l.split('\t')[1]) for l in open(os.path.join(M0, 'costs.tsv')) if '\t' in l)
print('sessions=%d  total_cost_usd(thực, max theo session)=%.5f  ledger costs.tsv (cộng thô, có trùng resume)=%.5f' % (len(per_session), sum(per_session.values()), naive))
