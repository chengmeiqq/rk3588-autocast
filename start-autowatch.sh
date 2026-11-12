#!/bin/bash
# 插电自动执行守护进程（启动时若已插电，立刻跑一次）
cd "$(dirname "$0")" || exit 1
FLOW=${1:-flows/通话投屏.json}
if pgrep -f "autowatch[.]py" >/dev/null; then
  echo "autowatch 已经在跑:"; pgrep -af "autowatch[.]py" | head -2
  exit 0
fi
mkdir -p logs
# 确保 airweb 在跑（autowatch 依赖它的 /power 接口）
if ! curl -sf -o /dev/null http://127.0.0.1:8123/ 2>/dev/null; then
  echo "airweb 没在跑，先启动它"
  ./start-web.sh >/dev/null 2>&1
  sleep 4
fi
setsid nohup ./.venv/bin/python ./autowatch.py "$FLOW" > logs/autowatch.out 2>&1 &
sleep 2
if pgrep -f "autowatch[.]py" >/dev/null; then
  echo "autowatch 已启动，流程: $FLOW"
  echo "启动时若已插电，会立刻执行一次"
  echo "日志: tail -f logs/autowatch.log"
else
  echo "启动失败"; tail -5 logs/autowatch.out
fi
