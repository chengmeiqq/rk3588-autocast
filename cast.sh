#!/bin/sh
# 一条一条执行：./cast.sh <步骤>
# 步骤：status wake popup restart home launch call cast verify desktop full
# 板子本地跑，不需要网络接口。手机必须已连 adb。

DIR=$(cd "$(dirname "$0")" && pwd)
SERIAL=${PHONE_SERIAL:-892e20c9}
PORT=${BOARD_PORT:-8130}
CMD=$1

usage() {
  cat <<EOF
用法: ./cast.sh <步骤>

  status    看当前状态（前台 + 投屏 owner），不改任何东西
  wake      唤醒屏幕
  popup     关「USB 用于」弹窗
  restart   杀掉豆包重启，回到首页
  home      只确认是否在首页（找「打电话」图）
  launch    启动豆包（不杀进程）
  call      点「打电话」，确认进通话页
  cast      点「开启投屏」，确认系统有会话
  verify    确认画面真在共享
  desktop   回桌面
  full      从唤醒一路做到投屏（等价 POST /cast）

例:
  ./cast.sh status
  ./cast.sh call
EOF
}

[ -z "$CMD" ] && usage && exit 1

case "$CMD" in
  status)
    adb -s "$SERIAL" shell "dumpsys window | grep mCurrentFocus" 2>/dev/null | tr -d '\r' | tail -1
    adb -s "$SERIAL" shell "dumpsys display | grep ScreenCapture" 2>/dev/null | tr -d '\r' | sed 's/^/  /'
    ;;

  wake)
    adb -s "$SERIAL" shell input keyevent KEYCODE_WAKEUP
    adb -s "$SERIAL" shell wm dismiss-keyguard
    sleep 1
    echo "已唤醒"
    ;;

  popup)
    T=$(adb -s "$SERIAL" shell "uiautomator dump /sdcard/_p.xml >/dev/null 2>&1; grep -o 'text=\"[^\"]*\"' /sdcard/_p.xml" 2>/dev/null | tr -d '\r' | grep -i USB)
    if [ -z "$T" ]; then
      echo "没有 USB 弹窗"
    else
      echo "发现弹窗: $T"
      adb -s "$SERIAL" shell input keyevent KEYCODE_BACK
      sleep 1
      echo "已按 BACK 关闭"
    fi
    ;;

  restart)
    adb -s "$SERIAL" shell am force-stop com.larus.nova
    sleep 2
    adb -s "$SERIAL" shell am start -n com.larus.nova/com.larus.home.impl.alias.AliasActivity1
    sleep 6
    adb -s "$SERIAL" shell "dumpsys window | grep mCurrentFocus" 2>/dev/null | tr -d '\r' | tail -1
    ;;

  launch)
    adb -s "$SERIAL" shell "monkey -p com.larus.nova -c android.intent.category.LAUNCHER 1" >/dev/null 2>&1
    sleep 6
    adb -s "$SERIAL" shell "dumpsys window | grep mCurrentFocus" 2>/dev/null | tr -d '\r' | tail -1
    ;;

  home)
    adb -s "$SERIAL" exec-out screencap -p > /tmp/_home.png 2>/dev/null
    python3 - "$DIR/templates/打电话.png" <<'PY'
import sys, cv2
img = cv2.imread('/tmp/_home.png'); tpl = cv2.imread(sys.argv[1])
if img is None or tpl is None:
    print("截图或模板读不到"); sys.exit(1)
r = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
_, mx, _, loc = cv2.minMaxLoc(r)
if mx < 0.6:
    print("没找到「打电话」按钮（最高分 %.3f），不在首页" % mx); sys.exit(1)
print("在首页，「打电话」@(%d,%d) 分数 %.3f" % (
    loc[0]+tpl.shape[1]//2, loc[1]+tpl.shape[0]//2, mx))
PY
    ;;

  call)
    curl -s -m 90 -X POST "http://127.0.0.1:$PORT/step/tap_call"; echo
    ;;

  cast)
    curl -s -m 90 -X POST "http://127.0.0.1:$PORT/step/tap_cast"; echo
    ;;

  verify)
    curl -s -m 90 -X POST "http://127.0.0.1:$PORT/step/verify"; echo
    ;;

  desktop)
    curl -s -m 30 -X POST "http://127.0.0.1:$PORT/step/desktop"; echo
    ;;

  full)
    curl -s -m 120 -X POST "http://127.0.0.1:$PORT/cast"; echo
    ;;

  *)
    echo "未知步骤: $CMD"; echo; usage; exit 1
    ;;
esac