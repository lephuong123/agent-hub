#!/usr/bin/env bash
# A-04/A-05: so sánh chạy mặc định và chạy cô lập.
#  1) baseline (KHÔNG cô lập) — tốn ~$0.17 vì nạp ~295 tool MCP của người dùng; bỏ qua bằng SKIP_BASELINE=1
#  2) cô lập: --setting-sources "" --strict-mcp-config --mcp-config '{"mcpServers":{}}'
#  3) CLAUDE.md đánh dấu ở thư mục cha (work/CLAUDE.md) với: mặc định / setting-sources "" / setting-sources project
#  4) tắt auto-memory: env CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 hoặc settings {"autoMemoryEnabled": false}
set -u
M0="$(cd "$(dirname "$0")" && pwd)"; cd "$M0"
NOMCP='{"mcpServers":{}}'
C=(claude --model haiku --max-budget-usd 0.3 --output-format stream-json --verbose --include-hook-events --tools "")
if [ "${SKIP_BASELINE:-0}" != 1 ]; then
  (cd work && "${C[@]}" -p "Reply with just OK" < /dev/null > ../fixtures/a04-baseline.jsonl)
  echo "### baseline"; python3 summarize.py fixtures/a04-baseline.jsonl | grep -E "HOOK|mcp_servers|RESULT" | cut -c1-300
fi
(cd work && "${C[@]}" --setting-sources "" --strict-mcp-config --mcp-config "$NOMCP" -p "Reply with just OK" < /dev/null > ../fixtures/a04-iso1.jsonl)
echo "### iso"; python3 summarize.py fixtures/a04-iso1.jsonl | grep -E "HOOK|INIT|tools|mcp_servers|plugins|memory|RESULT"
printf '# Spike marker\nThe secret project marker is ZEBRA-7781. If asked for a marker, reply with it.\n' > work/CLAUDE.md
mkdir -p work/a05
P='If your context (system prompt or any loaded instructions/CLAUDE.md/memory) contains a secret marker string, reply with just that marker; otherwise reply NONE.'
for v in default iso project; do
  case $v in default) s=();; iso) s=(--setting-sources "");; project) s=(--setting-sources project);; esac
  (cd work/a05 && "${C[@]}" ${s[@]+"${s[@]}"} --strict-mcp-config --mcp-config "$NOMCP" -p "$P" < /dev/null > ../../fixtures/a05-claudemd-$v.jsonl)
  echo "### CLAUDE.md marker — $v"; python3 summarize.py fixtures/a05-claudemd-$v.jsonl | grep -E "HOOK hook_started|ASSISTANT|RESULT"
done
rm -rf work/CLAUDE.md work/a05
(cd work && CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 "${C[@]}" --setting-sources "" --strict-mcp-config --mcp-config "$NOMCP" -p "Reply OK" < /dev/null > ../fixtures/a05-automem-env.jsonl)
(cd work && "${C[@]}" --setting-sources "" --settings '{"autoMemoryEnabled":false}' --strict-mcp-config --mcp-config "$NOMCP" -p "Reply OK" < /dev/null > ../fixtures/a05-automem-settings.jsonl)
for f in a05-automem-env a05-automem-settings; do echo "### $f"; python3 summarize.py fixtures/$f.jsonl | grep -E "memory_paths|RESULT"; done
