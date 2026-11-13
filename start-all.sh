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

# 4) usbwatch 现在是独立的 systemd 服务（airtest-usbwatch.service）。
#    为什么移出来：它是常驻触发器，必须 Restart=always；而本服务是
#    oneshot+RemainAfterExit，进程死了 systemd 不会管。实测就撞过：
#    它自己退出后，插上手机毫无反应。所以别再在这里启动它。

# 5) smartcast：给 Tasker 调的窄接口（点打电话/点投屏/确认）
./start-smartcast.sh "${CAST_PORT:-8130}" >> logs/service.log 2>&1

# 6) 音量键触发（可选，失败不影响主流程）
if [ -x ./start-keytrigger.sh ]; then
  ./start-keytrigger.sh >> logs/service.log 2>&1
fi

echo "===== $(date '+%F %T') start-all done ====="
exit 0
