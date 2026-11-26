"""Tóm tắt một fixture JSONL của `codex exec --json`: kiểu event, item, lệnh, kết quả."""
import json
import sys

for path in sys.argv[1:]:
    print("==", path)
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            print("  RAW", line[:200])
            continue
        t = e.get("type")
        it = e.get("item") or {}
        if t.startswith("item."):
            ty = it.get("type")
            extra = {k: v for k, v in it.items() if k not in ("id", "type")}
            s = json.dumps(extra, ensure_ascii=False)
            print(f"  {t:15} {ty:18} {s[:400]}")
        else:
            print(f"  {t:15} {json.dumps({k: v for k, v in e.items() if k != 'type'}, ensure_ascii=False)[:400]}")
