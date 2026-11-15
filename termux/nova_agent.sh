#!/data/data/com.termux/files/usr/bin/bash
# nova_agent.sh —— 手机本地守护：让直播 App 活着且在前台，开流，拉豆包投屏
#
# 为什么在手机里跑：
#   Termux 直接调 am/input 会被拒（uid 没有 INJECT_EVENTS / START_ACTIVITIES）。
#   但 Termux 里装了 adb，连本机 127.0.0.1:5555（adbd 的 shell 身份）就什么都能做，
#   而且完全不走 USB、不占板子的 adb 通道。
#
# 用法：
#   ./nova_agent.sh once     跑一轮就退出（调试用）
#   ./nova_agent.sh watch    常驻守护（默认）
#   ./nova_agent.sh status   只报状态

set -u
export PREFIX=/data/data/com.termux/files/usr
export PATH="$PREFIX/bin:$PATH"
export HOME=/data/data/com.termux/files/home
export TMPDIR="$PREFIX/tmp"
mkdir -p "$TMPDIR" "$HOME/.nova"

ADB="adb -s 127.0.0.1:5555"
LENS_PKG="com.opencode.multilensipcam"
LENS_ACT="$LENS_PKG/.MainActivity"
LENS_API="http://127.0.0.1:41737"
NOVA_PKG="com.larus.nova"
NOVA_ACT="$NOVA_PKG/com.larus.voicecall.impl.ui.RealtimeCallActivity"
LOG="$HOME/.nova/agent.log"

say() { echo "$(date '+%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

# 目标参数（与板子侧 WANT 一致）
WANT_CAM="0"
WANT_RES="3840x2160"
WANT_QUALITY="100"
WANT_ZOOM="1.53"
WANT_FOCUS="auto"

sh_() { $ADB shell "$@" 2>/dev/null; }

adb_ready() {
  $ADB shell true >/dev/null 2>&1 && return 0
  adb connect 127.0.0.1:5555 >/dev/null 2>&1
  sleep 2
  $ADB shell true >/dev/null 2>&1
}

# 屏幕必须醒着：Dozing 时 dumpsys 返回空，会误判「App 不在前台」
ensure_awake() {
  local st
  st=$(sh_ dumpsys power | grep -m1 'mWakefulness=')
  case "$st" in
    *Awake*) return 0 ;;
  esac
  sh_ input keyevent KEYCODE_WAKEUP
  sh_ wm dismiss-keyguard
  sleep 1.5
}

foreground() {
  sh_ dumpsys activity activities | grep -m1 topResumedActivity \
    | sed 's/.*u0 \([^ ]*\) .*/\1/'
}

# 用 uiautomator 按文字找控件，输出 "x y"；找不到返回空
# 必须走 find_ui.py 的完全相等匹配：页面标题「正在共享屏幕」的 text 里
# 就含「共享屏幕」四个字，grep 子串会先命中标题（y≈804）而不是按钮（y≈2117），
# 点下去没反应，日志却写「已点共享屏幕」——比报错更难查。
find_ui() {
  local want="$1" xml="$HOME/ui.xml"
  sh_ uiautomator dump /data/local/tmp/ui.xml >/dev/null 2>&1
  $ADB shell cat /data/local/tmp/ui.xml > "$xml" 2>/dev/null
  [ -s "$xml" ] || return 0
  python "$HOME/find_ui.py" "$xml" "$want" 2>/dev/null
}

# 页面上是否有某个完全相等的文案（用于判断「已经在通话页 / 已经在投屏」）
has_ui() {
  local want="$1" xml="$HOME/ui.xml"
  sh_ uiautomator dump /data/local/tmp/ui.xml >/dev/null 2>&1
  $ADB shell cat /data/local/tmp/ui.xml > "$xml" 2>/dev/null
  [ -s "$xml" ] || return 1
  python "$HOME/find_ui.py" "$xml" "$want" >/dev/null 2>&1
}

lens_state() { curl -s -m 8 "$LENS_API/api/state"; }

lens_ctl() { curl -s -m 15 "$LENS_API/api/control?$1"; }

jget() { python -c 'import sys,json;d=json.load(sys.stdin);print(d.get(sys.argv[1],""))' "$1" 2>/dev/null; }

ensure_lens_front() {
  ensure_awake
  local fg
  fg=$(foreground)
  case "$fg" in
    *"$LENS_PKG"*) return 0 ;;
  esac
  say "直播 App 不在前台（${fg:-空}），拉起"
  sh_ am start -n "$LENS_ACT" >/dev/null
  local i
  for i in 1 2 3 4 5 6; do
    sleep 1.5
    fg=$(foreground)
    case "$fg" in *"$LENS_PKG"*) return 0 ;; esac
  done
  say "拉起失败，前台仍是 ${fg:-空}"
  return 1
}

apply_lens_params() {
  local st cam res q z f
  st=$(lens_state)
  [ -z "$st" ] && { say "取不到 App 状态"; return 1; }
  cam=$(echo "$st" | jget selectedCameraKey)
  res=$(echo "$st" | jget selectedResolution)
  q=$(echo "$st" | jget qualityValue)
  z=$(echo "$st" | jget zoomValue)
  f=$(echo "$st" | jget focusMode)

  if [ "$cam" != "$WANT_CAM" ]; then
    say "设摄像头 $cam -> $WANT_CAM"; lens_ctl "camera=$WANT_CAM"; sleep 2
  fi
  if [ "$res" != "$WANT_RES" ]; then
    say "设分辨率 $res -> $WANT_RES"; lens_ctl "resolution=$WANT_RES"; sleep 1.5
  fi
  if [ "$q" != "$WANT_QUALITY" ]; then
    say "设质量 $q -> $WANT_QUALITY"; lens_ctl "quality=$WANT_QUALITY"; sleep 0.8
  fi
  if [ "$f" != "$WANT_FOCUS" ]; then
    say "设对焦 $f -> $WANT_FOCUS"; lens_ctl "focus=$WANT_FOCUS"; sleep 1.5
  fi
  if [ "$z" != "$WANT_ZOOM" ]; then
    say "设变焦 $z -> $WANT_ZOOM"; lens_ctl "zoom=$WANT_ZOOM"; sleep 1
  fi
}

start_lens_stream() {
  local streaming
  streaming=$(lens_state | jget streaming)
  if [ "$streaming" = "True" ] || [ "$streaming" = "true" ]; then
    return 0
  fi
  # 关键：必须在可见前台开流，否则 App 在后台申请相机会把 HTTP 线程挂死
  ensure_lens_front || return 1
  say "开流"
  lens_ctl "streaming=true" >/dev/null
  local i
  for i in 1 2 3 4 5 6 7 8; do
    sleep 1.5
    streaming=$(lens_state | jget streaming)
    if [ "$streaming" = "True" ] || [ "$streaming" = "true" ]; then
      say "直播中 $(lens_state | python -c 'import sys,json;d=json.load(sys.stdin);print(d.get("selectedResolution"),"q"+str(d.get("qualityValue")),"zoom"+str(d.get("zoomValue")))' 2>/dev/null)"
      return 0
    fi
  done
  say "开流超时，未进入 streaming"
  return 1
}

# 豆包投屏：按文字走，不写死坐标。
#
# ⚠ 已知硬限制（2026-11-15 实测，对照实验三次一致）：
#   MIUI 只允许「可见前台」的 App 用相机。豆包通话页一旦到了前台，直播 App
#   就退到后台，/snapshot.jpg 立刻 0B/HTTP 000（连不上）；把直播 App 拉回前台
#   后变 23B/HTTP 503（连上了但相机没在工作）。两条链路在「谁占前台」上互斥。
#   而且豆包通话页是内部 Activity，`am start` 拉不回来（必须走 App 自己的流程）。
#   所以：投屏期间直播必然断流，这是设计约束，不是 bug，别试图并发调和。
#   现在的取舍 = 先保证直播出图（用户主用途），投屏只在需要时开。
# 关键顺序：先判断「现在在哪一页」，再决定点什么。
# 之前在通话页上又找「共享屏幕」，结果 grep 子串命中标题「正在共享屏幕」，
# 点了个空——所以每一步都要先认页面。
nova_share() {
  local fg pt
  ensure_awake
  fg=$(foreground)
  case "$fg" in
    *"$NOVA_PKG"*) ;;
    *)
      say "拉起豆包"
      sh_ monkey -p "$NOVA_PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
      sleep 6
      ;;
  esac

  # 已经在投屏：标题「正在共享屏幕」存在，别再点任何东西
  if has_ui "正在共享屏幕"; then
    say "已经在投屏，不动手"
    return 0
  fi

  # 不在通话页：从首页底部点「打电话」
  if ! has_ui "挂断通话"; then
    pt=$(find_ui "打电话")
    if [ -z "$pt" ]; then
      say "没找到「打电话」，当前页面不认识"
      return 1
    fi
    say "点「打电话」@ $pt"
    sh_ input tap $pt
    sleep 5
  fi

  # 现在应该通话页了，点底部「共享屏幕」
  if has_ui "挂断通话"; then
    pt=$(find_ui "共享屏幕")
    if [ -n "$pt" ]; then
      say "点「共享屏幕」@ $pt"
      sh_ input tap $pt
      sleep 4
    else
      say "通话页没有「共享屏幕」"
      return 1
    fi
  fi

  # 系统授权弹窗
  pt=$(find_ui "立即开始")
  if [ -n "$pt" ]; then
    say "点「立即开始」@ $pt"
    sh_ input tap $pt
    sleep 4
  fi

  # 确认真的开起来了
  if has_ui "正在共享屏幕"; then
    say "投屏已开"
    return 0
  fi
  say "投屏没开起来"
  return 1
}

# 网络前置条件：飞行模式开着就投不了屏
ensure_network() {
  local ap
  ap=$(sh_ settings get global airplane_mode_on | tr -d '\r')
  if [ "$ap" = "1" ]; then
    say "飞行模式开着，关掉"
    sh_ cmd connectivity airplane-mode disable
    sh_ settings put global airplane_mode_on 0
    sleep 3
  fi
  if ! sh_ ping -c 1 -W 2 192.168.1.1 >/dev/null 2>&1; then
    say "WiFi 不通，尝试打开"
    sh_ svc wifi enable
    sleep 6
  fi
}

# 投屏开着吗：通话页在前台且标题「正在共享屏幕」在
sharing() { has_ui "正在共享屏幕"; }

# 模式一：直播优先（默认）。只保证直播出图，绝不碰豆包，
# 也就不会把豆包从通话页挤掉——挤掉之后 am start 拉不回来，投屏必断。
mode_lens() {
  adb_ready || { say "adb 回环连不上"; return 1; }
  ensure_network

  # 投屏进行中：不动前台，避免打断。直播此时本来就无法出图（MIUI 前台限制）。
  if sharing; then
    say "投屏进行中，直播让路（不抢前台）"
    return 0
  fi

  ensure_lens_front || return 1
  apply_lens_params
  start_lens_stream
}

# 模式二：投屏优先。开了投屏之后直播会断，这是实测的互斥，
# 所以只在用户明确要投屏时用。
mode_share() {
  adb_ready || { say "adb 回环连不上"; return 1; }
  ensure_network
  ensure_lens_front || return 1
  apply_lens_params
  start_lens_stream
  nova_share
}

status() {
  adb_ready || { echo "adb: 断"; return 1; }
  echo "adb: $($ADB devices | grep -c 5555) 台回环设备"
  echo "前台: $(foreground)"
  echo "屏幕: $(sh_ dumpsys power | grep -m1 mWakefulness= | cut -d= -f2)"
  echo "飞行: $(sh_ settings get global airplane_mode_on | tr -d '\r')"
  echo "投屏: $(sharing && echo 开 || echo 关)"
  echo "直播: $(lens_state | head -c 110)"
  echo "adbd 端口: $(sh_ getprop service.adb.tcp.port)"
  echo "守护: $([ -f "$HOME/.nova/watch.pid" ] && kill -0 "$(cat "$HOME/.nova/watch.pid")" 2>/dev/null && echo "跑着 pid=$(cat "$HOME/.nova/watch.pid")" || echo 没跑)"
}

# 单实例锁：watch 只能有一个，否则多个守护互相抢前台
acquire_lock() {
  local pidf="$HOME/.nova/watch.pid"
  if [ -f "$pidf" ]; then
    local old
    old=$(cat "$pidf" 2>/dev/null)
    if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
      echo "已有守护在跑 pid=$old" >&2
      return 1
    fi
  fi
  echo $$ > "$pidf"
  trap 'rm -f "$pidf"' EXIT
  return 0
}

case "${1:-watch}" in
  once) mode_share ;;
  lens) mode_lens ;;
  share) mode_share ;;
  status) status ;;
  stop)
    if [ -f "$HOME/.nova/watch.pid" ]; then
      kill "$(cat "$HOME/.nova/watch.pid")" 2>/dev/null
      rm -f "$HOME/.nova/watch.pid"
      echo "已停"
    else
      pkill -f nova_agent.sh 2>/dev/null
      echo "已停（无 pid 文件）"
    fi
    ;;
  watch)
    acquire_lock || exit 1
    say "守护启动（直播优先）"
    while :; do
      mode_lens
      sleep 20
    done
    ;;
  *) echo "用法: $0 {watch|lens|share|once|status|stop}" ;;
esac
