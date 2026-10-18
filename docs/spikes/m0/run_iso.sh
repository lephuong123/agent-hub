#!/usr/bin/env bash
# Chạy `claude -p` với bộ cờ cô lập khuyến nghị (A-04/A-05) trong work/.
# Dùng: ./run_iso.sh <fixture-name> [extra claude args...]   (prompt nằm trong extra args, sau -p)
set -u
M0="$(cd "$(dirname "$0")" && pwd)"
name="$1"; shift
cd "$M0/work"
[ -n "${MCP_CFG:-}" ] || MCP_CFG='{"mcpServers":{}}'
claude --model haiku --max-budget-usd "${BUDGET:-0.3}" \
  --output-format stream-json --verbose --include-hook-events \
  --setting-sources "" --strict-mcp-config --mcp-config "$MCP_CFG" \
  "$@" < /dev/null > "$M0/fixtures/$name.jsonl" 2> "$M0/fixtures/$name.stderr"
rc=$?
echo "exit=$rc"; [ -s "$M0/fixtures/$name.stderr" ] && { echo "--- stderr"; head -c 1500 "$M0/fixtures/$name.stderr"; echo; } || rm -f "$M0/fixtures/$name.stderr"
python3 "$M0/summarize.py" "$M0/fixtures/$name.jsonl"
