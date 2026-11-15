#!/bin/bash
# go.sh —— 手动一键启动。插好 USB，然后跑：  ./go.sh
#
# 它做完这一串（每步都可重复跑，不会重复起进程）：
#   1. 确认手机通过 USB 在（不在就报错退出，不瞎猜）
#   2. adb tcpip 5555 —— 让手机内部的 adbd 也监听回环，Termux 才能用 adb
#   3. 把 termux 脚本推到手机（内容一样就跳过）
#   4. 跑手机里的 boot.sh：连回环 + 起守护
#   5. 验证：守护在跑？帧出得来？
#
# 为什么需要第 2 步：手机重启后 adbd 会回到 USB 模式，回环 5555 失效。
# persist.adb.tcp.port 写不了（要 root），所以每次重连后得重新 tcpip 一次。
# 这一步做完，手机里的 Termux 才能拿到 shell 身份。

set -u
cd "$(dirname "$0")" || exit 1

export ANDROID_HOME=${ANDROID_HOME:-/usr/lib/android-sdk}
export PATH="$ANDROID_HOME/platform-tools:$PATH"
USB_SERIAL=${USB_SERIAL:-892e20c9}

say() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*"; }

# ---- 1. 手机在不在 ----
say "1) 找手机 (USB $USB_SERIAL)"
if ! adb devices | grep -q "^$USB_SERIAL[[:space:]]*device"; then
  say "   ✗ USB 上没看到 $USB_SERIAL。检查线/口（应插 2-1.2）。"
  adb devices -l | sed 's/^/     /'
  exit 1
fi
say "   ✓ 在"

# ---- 2. tcpip 5555 ----
say "2) adb tcpip 5555（让手机内部 adbd 监听回环）"
adb -s "$USB_SERIAL" tcpip 5555 2>&1 | sed 's/^/   /'
sleep 3
# tcpip 会重启 adbd，等它回来
for i in 1 2 3 4 5 6 7 8 9 10; do
  adb -s "$USB_SERIAL" shell true >/dev/null 2>&1 && break
  sleep 1
done
if adb -s "$USB_SERIAL" shell true >/dev/null 2>&1; then
  say "   ✓ adbd 回来了 (port=$(adb -s "$USB_SERIAL" shell getprop service.adb.tcp.port 2>/dev/null | tr -d '\r'))"
else
  say "   ✗ adbd 没回来，后面会失败"
fi

# ---- 3. 推脚本（内容不同才推） ----
say "3) 同步 termux 脚本"
for f in nova_agent.sh find_ui.py boot.sh; do
  [ -f "termux/$f" ] || { say "   ✗ 缺 termux/$f"; exit 1; }
  lsize=$(stat -c %s "termux/$f")
  dev_size=$(adb -s "$USB_SERIAL" shell "run-as com.termux stat -c %s files/home/$f 2>/dev/null" 2>/dev/null | tr -d '\r')
  if [ "$lsize" = "$dev_size" ]; then
    say "   = $f 已是最新 ($dev_size B)"
    continue
  fi
  adb -s "$USB_SERIAL" push "termux/$f" "/data/local/tmp/$f" >/dev/null 2>&1
  adb -s "$USB_SERIAL" shell "cat /data/local/tmp/$f | run-as com.termux sh -c 'cat > files/home/$f && chmod 700 files/home/$f'" >/dev/null 2>&1
  adb -s "$USB_SERIAL" shell "rm -f /data/local/tmp/$f" >/dev/null 2>&1
  new_size=$(adb -s "$USB_SERIAL" shell "run-as com.termux stat -c %s files/home/$f" 2>/dev/null | tr -d '\r')
  if [ "$new_size" = "$lsize" ]; then
    say "   ✓ $f -> $new_size B"
  else
    say "   ✗ $f 大小不符 (期望 $lsize, 实际 $new_size)"
  fi
done

# ---- 4. 起守护 ----
say "4) 手机里跑 boot.sh"
adb -s "$USB_SERIAL" shell "run-as com.termux sh -c 'cd files/home && ./boot.sh 2>&1'" 2>&1 | sed 's/^/   /'

# ---- 5. 验证 ----
say "5) 验证"
sleep 3
adb -s "$USB_SERIAL" shell "run-as com.termux sh -c 'cd files/home && ./nova_agent.sh status 2>&1'" 2>&1 | sed 's/^/   /'

say "6) 帧"
frame=$(adb -s "$USB_SERIAL" shell "curl -sL -m 18 -o /dev/null -w '%{size_download} %{http_code}' http://127.0.0.1:41737/snapshot.jpg" 2>/dev/null | tr -d '\r')
size=${frame%% *}
code=${frame##* }
if [ "${code:-000}" = "200" ] && [ "${size:-0}" -gt 10000 ] 2>/dev/null; then
  say "   ✓ 出图 ${size}B HTTP $code"
else
  say "   ⚠ 帧: ${frame:-无响应}（App 可能刚被拉起，相机还在初始化；等 10 秒再看）"
fi

say "完成。日志: adb -s $USB_SERIAL shell 'run-as com.termux tail -f files/home/.nova/agent.log'"
