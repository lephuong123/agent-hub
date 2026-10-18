#!/usr/bin/env bash
# A-13: giữ quyết định ~150 s ở (a) hook không khai timeout, (b) hook timeout=900, (c) permission-prompt-tool MCP.
# Chạy song song 3 biến thể; mỗi biến thể ghi fixtures/a13-*.jsonl và in thời gian.
M0="$(cd "$(dirname "$0")" && pwd)"; cd "$M0"
SLEEP="${1:-150}"
P="Use the Bash tool to run exactly this one command and nothing else: touch SLOWAPPROVE-\$V.txt   Then report the outcome in one sentence. Do not retry."
run() { V="$1"; shift; local t0=$(date +%s); ./run_iso.sh "a13-$V" "$@" -p "${P//\$V/$V}" > "fixtures/a13-$V.summary.txt"; echo "elapsed=$(( $(date +%s)-t0 ))s" >> "fixtures/a13-$V.summary.txt"; }
export HOOK_SLEEP="$SLEEP" PERM_SLEEP="$SLEEP"
run hook-default --settings "$M0/settings/hub-hooks.json" --tools Bash &
run hook-timeout900 --settings "$M0/settings/hub-hooks-timeout900.json" --tools Bash &
MCP_CFG="$M0/mcp/hubperm.mcp.json" run permtool --settings "$M0/settings/hub-base.json" --permission-prompt-tool mcp__hubperm__approve --tools Bash &
wait
for f in fixtures/a13-*.summary.txt; do echo "== $f"; grep -vE "skills|plugins|thinking" "$f"; done
