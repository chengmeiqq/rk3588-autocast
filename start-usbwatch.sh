#!/bin/sh
# 启动 usbwatch：盯板子自己的 USB 口，手机插上就跑流程。
cd "$(dirname "$0")"
mkdir -p logs
if pgrep -f "python ./usbwatch" >/dev/null 2>&1; then
  echo "usbwatch 已经在跑：$(pgrep -f 'python ./usbwatch' | head -1)"
  exit 0
fi
nohup ./.venv/bin/python ./usbwatch.py >> logs/usbwatch.log 2>&1 &
echo "usbwatch 已启动（pid $!）"
echo "日志: tail -f logs/usbwatch.log"
