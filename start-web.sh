#!/bin/bash
# 启动 airweb（网页版点哪儿）
cd "$(dirname "$0")" || exit 1
PORT=${1:-8123}
if pgrep -f "airweb.py $PORT" >/dev/null; then
  echo "已经在跑: http://$(hostname -I | awk '{print $1}'):$PORT"
  exit 0
fi
mkdir -p logs
setsid nohup ./.venv/bin/python ./airweb.py "$PORT" > logs/airweb.log 2>&1 &
sleep 4
if curl -sf -o /dev/null "http://127.0.0.1:$PORT/"; then
  echo "已启动: http://$(hostname -I | awk '{print $1}'):$PORT"
else
  echo "启动失败，看 logs/airweb.log"; tail -5 logs/airweb.log
fi
