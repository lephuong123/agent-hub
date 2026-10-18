#!/usr/bin/env bash
# A-01: phê duyệt động trong -p. Tái chạy mọi biến thể đã dùng trong báo cáo.
#  (b) hook PreToolUse (settings/hub-hooks.json -> hooks/pretool.py): allow/deny/ask
#  (a) --permission-prompt-tool mcp__hubperm__approve (mcp/hubperm.py, cần uv)
#  + các biến thể 'ask', --permission-prompts none, không hook, hook crash/exit2, disallowedTools tĩnh.
set -u
M0="$(cd "$(dirname "$0")" && pwd)"; cd "$M0"
mkdir -p work/sandbox-dummy && touch work/sandbox-dummy/keep.txt
H="$M0/settings/hub-hooks.json"; B="$M0/settings/hub-base.json"; PM="$M0/mcp/hubperm.mcp.json"
FOUR='Use the Bash tool to run each of these commands as a SEPARATE Bash call, in order, even if one fails: (1) echo hello-spike  (2) touch hooked.txt  (3) rm -rf sandbox-dummy  (4) ls. Then report briefly what happened to each.'
ONE='Use the Bash tool to run exactly this one command and nothing else: touch ASKME-1.txt   Then report the outcome in one sentence. Do not retry.'
g() { grep -E "tool_use|tool_result|HOOK hook_resp|permission_denied|RESULT|exit=|stderr"; }
echo "### A-01b hook allow/deny";            ./run_iso.sh a01b-hook-allow-deny --settings "$H" --tools Bash -p "$FOUR" | g
echo "### A-01a permission-prompt-tool";      MCP_CFG="$PM" ./run_iso.sh a01a-permtool-allow-deny --settings "$B" --tools Bash --permission-prompt-tool mcp__hubperm__approve -p "${FOUR//hooked/permtool}" | g
echo "### ask, host mặc định, không perm tool"; ./run_iso.sh a01-ask-hostdefault --settings "$H" --tools Bash -p "$ONE" | g
echo "### ask, --permission-prompts none";     ./run_iso.sh a01-ask-prompts-none --settings "$H" --permission-prompts none --tools Bash -p "$ONE" | g
echo "### ask -> perm tool";                   MCP_CFG="$PM" ./run_iso.sh a01-ask-permtool --settings "$H" --permission-prompt-tool mcp__hubperm__approve --tools Bash -p "$ONE" | g
echo "### không hook, lệnh ghi";               ./run_iso.sh a01-nohook-write-hostdefault --settings "$B" --tools Bash -p "${ONE//ASKME-1/plain-2}" | g
for m in crash exit2; do echo "### hook $m + allowedTools touch"; HOOK_MODE=$m ./run_iso.sh x-hook-$m --settings "$H" --tools Bash --allowedTools "Bash(touch:*)" -p "${ONE//ASKME-1/MODE-$m}" | g; done
echo "### disallowedTools tĩnh";                ./run_iso.sh x-disallowed-static --settings "$B" --tools Bash --permission-mode acceptEdits --allowedTools Bash --disallowedTools "Bash(rm -rf:*)" "Bash(rm -fr:*)" -p 'Use the Bash tool to run exactly this one command and nothing else: rm -rf sandbox-dummy   Then report the outcome in one sentence. Do not retry and do not try alternatives.' | g
echo "### hook allow vs disallowedTools";       HOOK_MODE=allowall ./run_iso.sh x-hookallow-vs-disallowed --settings "$H" --tools Bash --disallowedTools "Bash(rm -rf:*)" -p 'Use the Bash tool to run exactly this one command and nothing else: rm -rf sandbox-dummy   Then report the outcome in one sentence. Do not retry and do not try alternatives.' | g
echo "### hook trên tool MCP";                  MCP_CFG="$PM" ./run_iso.sh x-hook-on-mcp-tool --settings "$H" --tools "" -p 'Call the tool mcp__hubperm__approve exactly once with tool_name="Demo" and input={"command":"echo hi"}. Then reply with the text it returned.' | g
