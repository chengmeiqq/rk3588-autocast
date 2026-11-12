#!/bin/bash
# 板子通电后把整套服务拉起来，不需要人登进来。
# 这是给 systemd 用的；手动跑也没问题。
cd "$(dirname "$0")" || exit 1
mkdir -p logs

export ANDROID_HOME=${ANDROID_HOME:-/usr/lib/android-sdk}
export PATH="$ANDROID_HOME/platform-tools:$PATH"

echo "===== $(date '+%F %T') start-all ====="

# 1) adb server。开机后它可能还没起，先拉起来
if ! pgrep -f "adb .*fork-server" >/dev/null 2>&1; then
  adb start-server >/dev/null 2>&1
fi

# 2) 无线 adb：手机若已在同网段就连上（连不上不影响，USB 兜底）
#    手机 IP 会变，所以从 USB 侧问一次
USB_SERIAL=${USB_SERIAL:-892e20c9}
IP=$(adb -s "$USB_SERIAL" shell "ip -4 addr show wlan0 | grep -o 'inet [0-9.]*' | cut -d' ' -f2" 2>/dev/null | tr -d '\r')
if [ -n "$IP" ]; then
  adb connect "$IP:5555" >/dev/null 2>&1
  export AIR_WIFI_IP="$IP"
fi

# 3) airweb 是流程执行器，必须先在
./start-web.sh "${PORT:-8123}" >> logs/service.log 2>&1

# 4) usbwatch 是触发器：启动时若已插着会自动跑一次
./start-usbwatch.sh >> logs/service.log 2>&1

# 5) 音量键触发（可选，失败不影响主流程）
if [ -x ./start-keytrigger.sh ]; then
  ./start-keytrigger.sh >> logs/service.log 2>&1
fi

echo "===== $(date '+%F %T') start-all done ====="
exit 0
