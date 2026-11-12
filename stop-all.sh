#!/bin/bash
# 停掉整套服务。systemd stop/restart 和手动都用它。
cd "$(dirname "$0")" || exit 1
for pat in "python ./usbwatch" "python ./keytrigger" "airweb[.]py"; do
  pids=$(pgrep -f "$pat" 2>/dev/null)
  if [ -n "$pids" ]; then
    echo "停 $pat: $pids"
    echo "$pids" | xargs -r kill 2>/dev/null
  fi
done
# 给它们两秒自己退
sleep 2
for pat in "python ./usbwatch" "python ./keytrigger" "airweb[.]py"; do
  pids=$(pgrep -f "$pat" 2>/dev/null)
  [ -n "$pids" ] && echo "$pids" | xargs -r kill -9 2>/dev/null
done
exit 0
