#!/usr/bin/env bash
# Spike: chạy một lượt `codex exec --json`, prompt qua stdin, ghi JSONL + stderr vào fixtures/.
# Dùng: run_exec.sh <tên> <cwd> "<prompt>" [cờ global trước exec ...] -- [cờ sau exec ...]
# Model rẻ mặc định: gpt-5.6-luna, reasoning low (đổi bằng env MODEL/EFFORT).
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
C="${CODEX_BIN:-$LOCALAPPDATA/Volta/bin/codex.cmd}"
name="$1"; cwd="$2"; prompt="$3"; shift 3
pre=(); post=(); seen=0
for a in "$@"; do
  if [ "$a" = "--" ]; then seen=1; continue; fi
  if [ $seen = 0 ]; then pre+=("$a"); else post+=("$a"); fi
done
cd "$cwd" || exit 1
t0=$(date +%s)
printf "%s" "$prompt" | timeout "${TMO:-300}" "$C" -m "${MODEL:-gpt-5.6-luna}" -c "model_reasoning_effort=\"${EFFORT:-low}\"" "${pre[@]}" \
  exec --json --skip-git-repo-check "${post[@]}" - \
  > "$HERE/fixtures/$name.jsonl" 2> "$HERE/fixtures/$name.stderr.txt"
rc=$?
echo "exit=$rc elapsed=$(( $(date +%s) - t0 ))s" >> "$HERE/fixtures/$name.stderr.txt"
echo "[$name] exit=$rc elapsed=$(( $(date +%s) - t0 ))s"
