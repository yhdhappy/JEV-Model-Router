#!/bin/zsh
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "未找到项目虚拟环境：.venv/bin/python"
  echo "请先在项目目录完成 Python 环境安装。"
  read -k 1 "?按任意键关闭..."
  echo
  exit 1
fi

URL="http://127.0.0.1:8765"
LOG="/tmp/jev-model-router-panel.log"

if ! curl -fsS "$URL/api/config" >/dev/null 2>&1; then
  nohup .venv/bin/python -m jev_router.panel --port 8765 >"$LOG" 2>&1 </dev/null &
  for _ in {1..30}; do
    if curl -fsS "$URL/api/config" >/dev/null 2>&1; then
      break
    fi
    sleep 0.1
  done
fi

if curl -fsS "$URL/api/config" >/dev/null 2>&1; then
  open "$URL"
  echo "JEV Model Router 操作面板已启动：$URL"
  exit 0
fi

echo "操作面板启动失败。日志：$LOG"
tail -n 30 "$LOG" 2>/dev/null || true
read -k 1 "?按任意键关闭..."
echo
exit 1
