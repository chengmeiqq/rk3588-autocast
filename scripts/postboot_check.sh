#!/bin/sh
# 重启后自检：记录此刻各组件状态，供人工核对。
# 放在 workspace 而不是 /tmp —— /tmp 重启后会被清空，脚本会消失。
export PATH=/usr/lib/android-sdk/platform-tools:$PATH
A="adb -s 892e20c9"
OUT=/root/.openclaw/workspace/Airtest/logs/postboot_check.log
{
echo "===== 开机后自检 $(date -u +%F' '%T) ====="
echo "board uptime: $(uptime -p)"
for s in airtest-adbd airtest-keepcast airtest-smartcast; do
  echo "  $s: $(systemctl is-enabled $s) / $(systemctl is-active $s)"
done
echo "gateway: $(XDG_RUNTIME_DIR=/run/user/0 systemctl --user is-active openclaw-gateway.service 2>&1)"
echo "doubao pid: $($A shell pidof com.larus.nova 2>&1)"
echo "tasker pid: $($A shell pidof net.dinglisch.android.taskerm 2>&1)"
echo "usb: $($A shell getprop sys.usb.state 2>&1)"
echo "casting: $($A shell dumpsys display 2>/dev/null | grep -o 'virtual:com.larus.nova[^,]*' | head -1)"
echo "focus: $($A shell dumpsys window 2>/dev/null | grep mCurrentFocus | tail -1)"
} >> $OUT 2>&1
