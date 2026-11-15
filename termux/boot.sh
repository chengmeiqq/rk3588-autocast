#!/data/data/com.termux/files/usr/bin/bash
# boot.sh —— Termux 侧的「一键起守护」，给 Tasker / 板子 / 手动调用统一入口。
#
# 为什么需要它：
#   Termux:Boot 插件的签名跟手机上装的 Termux 不匹配，装不上，
#   所以没有 .termux/boot 自动扫描。改为让外部（Tasker 开机任务或板子）
#   调这一个脚本，它负责：
#     1. 打开 termux-wake-lock（防 MIUI 休眠）
#     2. 确保回环 adb 连得上（127.0.0.1:5555，shell 身份）
#     3. 起 nova_agent.sh watch，且有单实例锁
#
# 注意：这里不用 `am`/`input`。Termux 的 uid 没有 INJECT_EVENTS /
# START_ACTIVITIES，直接调会被 SecurityException 拒掉（实测）。
# 一切系统操作都通过回环 adb 走 shell 身份。

set -u
export PREFIX=/data/data/com.termux/files/usr
export PATH="$PREFIX/bin:$PATH"
export HOME=/data/data/com.termux/files/home
export TMPDIR="$PREFIX/tmp"
mkdir -p "$TMPDIR" "$HOME/.nova"

LOG="$HOME/.nova/boot.log"
say() { echo "$(date '+%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

say "boot.sh 开始"

# 1) 持有 wake lock，降低被 MIUI 冻结的概率
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock 2>/dev/null

# 2) 回环 adb：adbd 若还在 5555 上，直接连；连不上就等板子那边 tcpip
export TMPDIR="$PREFIX/tmp"
adb start-server >/dev/null 2>&1
for i in 1 2 3 4 5 6 7 8 9 10; do
  if adb -s 127.0.0.1:5555 shell true >/dev/null 2>&1; then
    say "回环 adb 已连（尝试 $i）"
    break
  fi
  adb connect 127.0.0.1:5555 >/dev/null 2>&1
  sleep 2
done
if ! adb -s 127.0.0.1:5555 shell true >/dev/null 2>&1; then
  say "回环 adb 连不上：adbd 可能回到了 USB 模式，需要板子执行 adb tcpip 5555"
  # 仍然起守护：nova_agent.sh 自己会重试
fi

# 3) 起守护（单实例）
if [ -f "$HOME/.nova/watch.pid" ] && kill -0 "$(cat "$HOME/.nova/watch.pid")" 2>/dev/null; then
  say "守护已在跑 pid=$(cat "$HOME/.nova/watch.pid")，不重复起"
else
  cd "$HOME" || exit 1
  setsid nohup ./nova_agent.sh watch >/dev/null 2>&1 </dev/null &
  sleep 2
  if pgrep -f "nova_agent.sh watch" >/dev/null 2>&1; then
    say "守护已起 pid=$(pgrep -f 'nova_agent.sh watch' | head -1)"
  else
    say "守护没起来，见 agent.log"
  fi
fi

say "boot.sh 结束"
