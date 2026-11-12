#!/bin/bash
# 手机音量键触发器（按两下音量减 = 跑流程）
cd "$(dirname "$0")" || exit 1
FLOW=${1:-flows/通话投屏.json}
# 只认真正的 python 进程，别被 pgrep 自己那条 sh -c 命令行骗到
RUNNING=$(pgrep -f "python ./keytrigger" | head -1)
if [ -n "$RUNNING" ]; then
  echo "keytrigger 已经在跑 (pid $RUNNING)"
  exit 0
fi
mkdir -p logs
if ! curl -sf -o /dev/null http://127.0.0.1:8123/ 2>/dev/null; then
  echo "airweb 没在跑，先启动它"; ./start-web.sh >/dev/null 2>&1; sleep 4
fi
setsid nohup ./.venv/bin/python ./keytrigger.py "$FLOW" > logs/keytrigger.out 2>&1 &
sleep 2
if pgrep -f "python ./keytrigger" >/dev/null; then
  echo "音量键触发器已启动，流程: $FLOW"
  echo "用法：在手机上按两下【音量减】"
  echo "日志: tail -f logs/keytrigger.log"
else
  echo "启动失败"; tail -5 logs/keytrigger.out
fi
