#!/data/data/com.termux/files/usr/bin/bash
# nova.sh —— 手机全自动：不依赖 RK3588S，不需要回环 adb。
#
# 为什么能做到：这台手机的 Magisk 允许 Termux 直接 `su -c`（实测返回
# uid=0(root), context=u:r:magisk:s0）。有了 root 之后，原先三条被拒的
# 路全通了：
#   input keyevent / input tap  → rc=0        （原先 SecurityException）
#   am start / am force-stop    → 正常        （原先 uid 不匹配）
#   setprop persist.adb.tcp.port→ 写得进去    （原先 rc=1）
# 直播 App 的 HTTP API 更是 Termux 直连 127.0.0.1:41737 就行。
#
# 所以手机自己就能跑完整流程，板子可以彻底不要。
#
# 用法：
#   ./nova.sh once     投屏 + 直播一次（回前台就停）
#   ./nova.sh lens     只保证直播（参数 + 出图）
#   ./nova.sh share    只做投屏
#   ./nova.sh watch    常驻守护，每 20s 巡检一次
#   ./nova.sh status   看状态
#   ./nova.sh stop     停守护
#   ./nova.sh install  装开机自启（写 .termux/boot + termux-services）

set -u
export PATH=/data/data/com.termux/files/usr/bin:$PATH
export HOME=/data/data/com.termux/files/home
export TMPDIR=/data/data/com.termux/files/usr/tmp
mkdir -p "$TMPDIR" "$HOME/.nova"

LOG=$HOME/.nova/nova.log
PIDF=$HOME/.nova/watch.pid
say() { echo "$(date '+%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

# 所有系统操作都过 root。两种运行方式都支持：
#   a) 从 Termux app 里跑（普通 uid）：走 su -c
#   b) 从 Magisk service.d 跑（已经是 root）：直接 eval
# 后者才是真正可靠的自启，因为 MIUI 的 SmartPower 杀的是 app 进程组，
# 碰不到 root shell。
if [ "$(id -u 2>/dev/null)" = "0" ]; then
  R() { eval "$*" 2>&1 | tr -d '\r'; }
else
  R() { su -c "$*" 2>&1 | tr -d '\r'; }
fi

API=http://127.0.0.1:41737
PKG=com.opencode.multilensipcam
ACT=$PKG/.MainActivity
DOUBAO=com.larus.nova
# 别写 .MainActivity —— 这个包没有那个类，am 会报 Error type 3。
# 真正的启动入口是启动别名：
DOUBAO_ACT=$DOUBAO/com.larus.home.impl.alias.AliasActivity1
CALLACT=$DOUBAO/com.larus.voicecall.impl.ui.RealtimeCallActivity

# ---------- 直播 ----------
api_state() { curl -s -m 10 "$API/api/state"; }

lens_params_ok() {
  curl -s -m 10 "$API/api/state" | python3 -c '
import sys, json
try: d = json.load(sys.stdin)
except Exception: sys.exit(1)
ok = (d.get("selectedResolution") == "3840x2160"
      and d.get("qualityValue") == 100
      and abs((d.get("zoomValue") or 0) - 1.53) < 0.02)
sys.exit(0 if ok else 1)
'
}

ensure_lens() {
  local st
  st=$(api_state)
  if [ -z "$st" ]; then
    say "直播 API 不通，拉起 App"
    R "am start -n $ACT" >/dev/null
    sleep 6
    st=$(api_state)
    [ -z "$st" ] && { say "API 还是不通"; return 1; }
  fi

  # App 在不在前台？MIUI 只给可见前台 App 相机
  local top
  top=$(R "dumpsys activity activities | grep -m1 topResumedActivity" | grep -o "$PKG/[^ }]*")
  if [ -z "$top" ]; then
    say "直播 App 不在前台，拉起"
    R "input keyevent KEYCODE_WAKEUP" >/dev/null
    R "am start -n $ACT" >/dev/null
    sleep 6
  fi

  # 参数
  if lens_params_ok; then
    say "参数已对（3840x2160 q100 zoom1.53）"
  else
    say "设参数"
    R "true" >/dev/null
    curl -s -m 10 -X POST "$API/api/control?resolution=3840x2160&quality=100&zoom=1.53" >/dev/null
    sleep 2
    lens_params_ok && say "参数已设好" || say "参数可能没生效"
  fi

  # 开流
  curl -s -m 10 -X POST "$API/api/control?streaming=true" >/dev/null
  sleep 2

  # 出图验证
  local f
  f=$(curl -sL -m 18 -o /dev/null -w '%{size_download} %{http_code}' "$API/snapshot.jpg")
  local sz=${f%% *} cd=${f##* }
  if [ "${cd:-000}" = "200" ] && [ "${sz:-0}" -gt 10000 ] 2>/dev/null; then
    say "直播 OK ${sz}B HTTP $cd"
    return 0
  fi
  say "帧异常: ${f:-无响应}"
  return 1
}

# ---------- 投屏检测 ----------
# 不能查 dumpsys activity：那里没有「正在共享屏幕」这类文字，
# 那是屏幕上的 UI 文本，只在 uiautomator 树里。
# dumpsys media_projection 返回 null 也不代表没开（实测过）。
# 所以：dump 一次 UI，看树里有没有「退出屏幕共享」这个按钮。
UIDUMP=$HOME/.nova/ui.xml

dump_ui() {
  R "uiautomator dump /sdcard/nova_ui.xml" >/dev/null 2>&1
  R "cat /sdcard/nova_ui.xml" > "$UIDUMP" 2>/dev/null
  [ -s "$UIDUMP" ]
}

sharing() {
  # 已有 dump 就先用，避免每次都 dump（dump 要 1-2 秒）
  if [ ! -s "$UIDUMP" ] || [ -n "${1:-}" ]; then
    dump_ui || return 1
  fi
  grep -q '退出屏幕共享' "$UIDUMP" 2>/dev/null
}

# uiautomator 树里按完全相等找控件，返回 "x y"
# 必须完全相等：豆包的标题 text="正在共享屏幕" 含「共享屏幕」四字，
# 用子串匹配会点中标题(y≈804)而不是按钮(y≈2117)。
find_ui() {
  if [ ! -s "$UIDUMP" ] || [ -n "${2:-}" ]; then
    dump_ui || return 1
  fi
  python3 -c '
import sys, re, html
want = sys.argv[1]
lm = []
bound = {}
txt = sys.stdin.read()
for m in re.finditer(r"<node[^>]*/?>", txt):
    tag = m.group(0)
    def attr(n):
        mm = re.search(n + r"=\"([^\"]*)\"", tag)
        return html.unescape(mm.group(1)) if mm else ""
    b = attr("bounds")
    if not b: continue
    clickable = attr("clickable") == "true"
    for a in ("text", "content-desc"):
        if attr(a) == want and want:
            mm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
            if mm:
                x1,y1,x2,y2 = map(int, mm.groups())
                lm.append((0 if clickable else 1, (x1+x2)//2, (y1+y2)//2))
if not lm:
    sys.exit(1)
lm.sort()
print(lm[0][1], lm[0][2])
' "$1" < "$UIDUMP" 2>/dev/null
}

tap() { R "input tap $1 $2" >/dev/null; }

ensure_network() {
  local ap
  ap=$(R "settings get global airplane_mode_on")
  if [ "$ap" = "1" ]; then
    say "飞行模式开着，关掉"
    R "cmd connectivity airplane-mode disable" >/dev/null 2>&1 || R "settings put global airplane_mode_on 0; am broadcast -a android.intent.action.AIRPLANE_MODE --ez state false" >/dev/null
    sleep 5
  fi
}

ensure_screen_on() {
  local s
  s=$(R "dumpsys power | grep -m1 'mWakefulness='")
  case "$s" in
    *Awake*) return 0 ;;
  esac
  say "屏幕没亮，唤醒"
  R "input keyevent KEYCODE_WAKEUP" >/dev/null
  R "wm dismiss-keyguard" >/dev/null
  sleep 2
}

ensure_share() {
  if sharing fresh; then
    say "已经在投屏，不动手"
    return 0
  fi
  ensure_network
  ensure_screen_on

  # 关掉直播前台，让豆包能占住相机
  R "input keyevent KEYCODE_WAKEUP" >/dev/null
  R "am force-stop $PKG" >/dev/null 2>&1
  sleep 2

  say "拉起豆包"
  R "am start -n $DOUBAO_ACT" >/dev/null
  sleep 6

  if ! R "dumpsys activity activities" | grep -q "$DOUBAO"; then
    say "豆包没起来"
    return 1
  fi

  # 打电话
  local pt
  pt=$(find_ui "打电话")
  if [ -n "$pt" ]; then
    say "点「打电话」@ $pt"
    tap $pt
    sleep 5
  else
    say "没找到「打电话」"
  fi

  # MIUI 权限弹窗可能挡着
  local allow
  allow=$(find_ui "允许")
  [ -n "$allow" ] && { say "点「允许」@ $allow"; tap $allow; sleep 3; }

  # 共享屏幕
  pt=$(find_ui "共享屏幕")
  if [ -n "$pt" ]; then
    say "点「共享屏幕」@ $pt"
    tap $pt
    sleep 4
  else
    say "没有「共享屏幕」按钮"
  fi

  # 系统授权
  pt=$(find_ui "立即开始")
  [ -n "$pt" ] && { say "点「立即开始」@ $pt"; tap $pt; sleep 4; }

  if sharing fresh; then
    say "投屏已开"
    return 0
  fi
  say "投屏没开起来"
  return 1
}

# ---------- 模式 ----------
mode_lens() {
  # 投屏开着就让路：抢前台会打断投屏，且 MIUI 相机只给前台
  if sharing fresh; then
    say "投屏进行中，直播让路"
    return 0
  fi
  ensure_lens
}

status() {
  echo "===== $(date '+%F %T') ====="
  echo "root:       $(R 'id' 2>/dev/null | head -1)"
  echo "前台:       $(R 'dumpsys activity activities' | grep -m1 topResumedActivity | sed 's/.*u0 //;s/}.*//')"
  echo "屏幕:       $(R 'dumpsys power' | grep -m1 mWakefulness= | sed 's/.*mWakefulness=//;s/,.*//')"
  echo "飞行:       $(R 'settings get global airplane_mode_on')"
  echo "投屏:       $(sharing fresh && echo 开 || echo 关)"
  local st; st=$(api_state)
  if [ -n "$st" ]; then
    echo "直播:       $(echo "$st" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("streaming=%s %s q%s zoom%s"%(d.get("streaming"),d.get("selectedResolution"),d.get("qualityValue"),d.get("zoomValue")))' 2>/dev/null)"
  else
    echo "直播:       API 不通"
  fi
  if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
    echo "守护:       跑着 pid=$(cat "$PIDF")"
  else
    echo "守护:       没跑"
  fi
}

# ---------- 开机自启 ----------
# 用 Magisk service.d，不用 Termux:Boot（签名不兼容，装不上）。
# 关键区别：service.d 里的脚本是 **root shell**，不是 app 进程。
# MIUI 的 SmartPower 杀 app 进程组，碰不到它。这也是 taskerd.sh 的做法。
do_install() {
  say "装开机自启"

  # 1) persist.adb.tcp.port —— 有了 root 才写得进去
  R "setprop persist.adb.tcp.port 5555" >/dev/null
  say "persist.adb.tcp.port=$(R 'getprop persist.adb.tcp.port')"

  # 2) Magisk 开机服务：真正的自启
  local tmp=$TMPDIR/nova_service.sh
  cat > "$tmp" <<'SVC'
#!/system/bin/sh
# nova 开机服务（Magisk service.d）
# 以 root 身份跑，不依赖 Termux app 是否被系统拉起。
TAG=nova
LOG=/data/local/tmp/nova-boot.log
log() { echo "$(date '+%m-%d %H:%M:%S') $*" >> "$LOG"; }

# 等系统真就绪；不等就 am start 会失败
wait_boot() {
  i=0
  while [ $i -lt 120 ]; do
    [ "$(getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && return 0
    sleep 2; i=$((i+1))
  done
  return 1
}

# 单实例：已有 nova watch 在跑就退出
for d in /proc/[0-9]*; do
  c=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)
  case "$c" in *nova.sh*watch*) log "已有实例（${d#/proc/}），退出"; exit 0 ;; esac
done

log "启动 pid=$$"
wait_boot || log "等 boot_completed 超时，继续"
log "系统就绪"

# Termux 的 bash 是唯一能跑 nova.sh 的（用了 bash 语法）
TB=/data/data/com.termux/files/usr/bin/bash
until [ -x "$TB" ]; do log "等 $TB"; sleep 5; done

# 每 60s 确保守护活着（进程死了就重拉）
while true; do
  alive=0
  for d in /proc/[0-9]*; do
    c=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)
    case "$c" in *nova.sh*watch*) alive=1; break ;; esac
  done
  if [ "$alive" = "0" ]; then
    log "守护不在，拉起"
    cd /data/data/com.termux/files/home && \
      $TB ./nova.sh watch >>"$LOG" 2>&1 &
    sleep 5
  fi
  sleep 60
done
SVC

  # 推到手机上再 root 拷进 service.d
  if R "test -d /data/adb/service.d" | grep -q .; then :; fi
  say "准备写入 /data/adb/service.d/nova.sh"

  # 用 base64 绕过引号/权限问题
  b64=$(base64 -w0 "$tmp" 2>/dev/null || base64 "$tmp" | tr -d '\n')
  R "echo '$b64' | base64 -d > /data/adb/service.d/nova.sh" >/dev/null
  R "chmod 755 /data/adb/service.d/nova.sh" >/dev/null
  local sz
  sz=$(R "stat -c %s /data/adb/service.d/nova.sh")
  if [ "${sz:-0}" -lt 200 ] 2>/dev/null; then
    say "✗ 写入失败（$sz B），service.d 里的文件是空的"
    R "rm -f /data/adb/service.d/nova.sh" >/dev/null
  else
    say "已写 /data/adb/service.d/nova.sh ($sz B)"
  fi

  # 3) Termux 侧兼存一份（万一以后拿到 Termux:Boot）
  mkdir -p "$HOME/.termux/boot"
  cat > "$HOME/.termux/boot/10-nova.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
exec $HOME/nova.sh watch
EOF
  chmod 700 "$HOME/.termux/boot/10-nova.sh"
  say "Termux boot 脚本也放了（备用）"

  say "完成。下次开机自动跑。现在想立即生效：nova.sh watch &"
}

acquire_lock() {
  if [ -f "$PIDF" ]; then
    local old; old=$(cat "$PIDF" 2>/dev/null)
    if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
      say "已在跑 pid=$old"
      return 1
    fi
  fi
  echo $$ > "$PIDF"
  return 0
}

case "${1:-watch}" in
  once)   ensure_share; ensure_lens ;;
  lens)   mode_lens ;;
  share)  ensure_share ;;
  status) status ;;
  install) do_install ;;
  stop)
    if [ -f "$PIDF" ]; then
      kill "$(cat "$PIDF")" 2>/dev/null; rm -f "$PIDF"; echo "已停"
    else
      pkill -f "nova.sh watch" 2>/dev/null; echo "已停"
    fi
    ;;
  watch)
    acquire_lock || exit 1
    trap 'rm -f "$PIDF"; exit 0' INT TERM
    say "守护启动（手机独立，不依赖板子）"
    while :; do
      mode_lens
      sleep 20
    done
    ;;
  *) echo "用法: $0 {once|lens|share|watch|status|install|stop}" ;;
esac
