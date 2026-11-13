#!/bin/bash
# 启动 smartcast：给 Tasker 调用的窄接口
#   POST http://<板子IP>:8130/cast   -> 点打电话、点投屏、确认投屏
#   GET  http://<板子IP>:8130/health -> 探活
cd "$(dirname "$0")" || exit 1
PORT=${1:-8130}
if pgrep -f "smartcast.py --serve $PORT" >/dev/null; then
  echo "smartcast 已经在跑: http://$(hostname -I | awk '{print $1}'):$PORT/cast"
  exit 0
fi
mkdir -p logs
setsid nohup ./.venv/bin/python ./smartcast.py --serve "$PORT" > logs/smartcast.log 2>&1 &
sleep 3
if curl -sf -o /dev/null "http://127.0.0.1:$PORT/health"; then
  echo "smartcast 已启动: http://$(hostname -I | awk '{print $1}'):$PORT/cast"
else
  echo "smartcast 启动失败，看 logs/smartcast.log"; tail -5 logs/smartcast.log
fi
