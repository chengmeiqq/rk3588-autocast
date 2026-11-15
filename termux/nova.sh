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
#   ./nova.sh watch    常驻守护，每 20s 巡检一次（flock 单实例）
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
# 锁不能放 $HOME/.nova：service.d 以 root 跑，会在 Termux 家目录里
# 造出 root 属主的目录，之后 Termux 用户就写不进去了。
# /data/local/tmp 是 root 和 app 都能到的地方。
LOCKD=$HOME/.nova/watch.lock.d
LOCKF=$HOME/.nova/watch.lock
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

# 静默版：stderr 直接丢掉，不合并进 stdout。
#
# 为什么需要这个：R() 是 `su -c "$*" 2>&1`，su 自己往 stderr 吐的告警
# （例如 sh 的报错）会被并进 stdout。于是「扫描进程」这种拿 stdout 当
# 数据的调用就收到了垃圾行，外面再写 2>/dev/null 也拦不住（重定向只作用于
# 整条管道，不是 R 内部的合并）。
# 实测后果：2026-11-15 14:37 nova.sh status 打出 `守护: 跑着 pid=/system/bin/sh:`。
RS() {
  if [ "$(id -u 2>/dev/null)" = "0" ]; then
    eval "$*" 2>/dev/null
  else
    su -c "$*" 2>/dev/null
  fi
}

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

# 只读 /api/state 一次，一次性回答所有问题。
# 关键优化：巡检周期只跑这个，不再每 20 秒下载 500KB 的 4K 帧。
# 输出一行 JSON 给调用方解析。
lens_probe() {
  curl -s -m 8 "$API/api/state" 2>/dev/null | python3 -c '
import sys, json
try: d = json.load(sys.stdin)
except Exception:
    print("dead 0 0 0 0"); sys.exit(0)
res = d.get("selectedResolution")
q   = d.get("qualityValue")
z   = d.get("zoomValue") or 0
st  = bool(d.get("streaming"))
params_ok = (res == "3840x2160" and q == 100 and abs(z - 1.53) < 0.02)
print("%s %s %s %s %s" % (
    "live" if st else "idle",
    1 if params_ok else 0,
    res or "-", q or 0, z))
' 2>/dev/null || echo "dead 0 0 0 0"
}

# 帧存活检查：只取前 2KB（Range），确认是 JPEG + HTTP 200。
# 不下载整个 4K 帧——巡检每 20 秒一次，全下等于每 20 秒吃掉 500KB。
# 出图存活检查：用 HEAD 而不是 GET。
#
# 这个服务端不支持 Range（实测 -r 0-2047 仍回整个 498KB 帧），
# 而快照是 302 跳到 /snapshots/<ts>.jpg 的。所以：
#   HEAD + -L  → 302 跟到最终图，只拿响应头，下 0 字节，约 40ms
# 巡检每 20 秒一次，用 GET 等于每 20 秒吃掉 500KB。
# 真正的「有没有画面」由 api/state 的 streaming 判断，
# 这里只确认端点还活着、图像能拼出来（HTTP 200）。
frame_alive() {
  local cd
  cd=$(curl -sIL -m 8 -o /dev/null -w '%{http_code}' \
       "$API/snapshot.jpg" 2>/dev/null)
  [ "${cd:-000}" = "200" ]
}

ensure_lens() {
  local p st params_ok res q z
  p=$(lens_probe)
  set -- $p
  st=${1:-dead}; params_ok=${2:-0}; res=${3:--}; q=${4:-0}; z=${5:-0}

  if [ "$st" = "dead" ]; then
    say "直播 API 不通，拉起 App"
    R "am start -n $ACT" >/dev/null
    sleep 6
    p=$(lens_probe); set -- $p
    st=${1:-dead}; params_ok=${2:-0}; res=${3:--}; q=${4:-0}; z=${5:-0}
    [ "$st" = "dead" ] && { say "API 仍不通"; return 1; }
  fi

  # App 在不在前台？MIUI 只给可见前台 App 相机
  local top
  top=$(top_act)
  case "$top" in
    *"$PKG"*) ;;
    *)
      say "直播 App 不在前台，拉起"
      R "input keyevent KEYCODE_WAKEUP" >/dev/null
      R "am start -n $ACT" >/dev/null
      sleep 6
      ;;
  esac

  # 参数：已经对就不动
  if [ "$params_ok" = "1" ]; then
    :   # 静默，不刷日志
  else
    say "设参数（当前 ${res} q${q} zoom${z}）"
    curl -s -m 10 -X POST \
      "$API/api/control?resolution=3840x2160&quality=100&zoom=1.53" >/dev/null
    sleep 2
    p=$(lens_probe); set -- $p
    params_ok=${2:-0}; res=${3:--}; q=${4:-0}; z=${5:-0}
    [ "$params_ok" = "1" ] && say "参数已设好" \
      || say "参数可能没生效（${res} q${q} zoom${z}）"
  fi

  # 开流：已经在流就不重复 POST
  if [ "$st" != "live" ]; then
    say "开流"
    curl -s -m 10 -X POST "$API/api/control?streaming=true" >/dev/null
    sleep 2
  fi

  # 出图：只取前 2KB 验证，不下整帧
  if frame_alive; then
    [ "$st" = "live" ] || say "直播已开"
    return 0
  fi

  # 这里才真的抓一整帧看大小，用于定位问题
  local f
  f=$(curl -sL -m 18 -o /dev/null -w '%{size_download} %{http_code}' "$API/snapshot.jpg")
  say "帧异常: ${f:-无响应}"
  return 1
}

# 当前前台 Activity（带包名/类名，已去掉尾巴）
top_act() {
  R "dumpsys activity activities" 2>/dev/null | grep -m1 topResumedActivity | sed 's/.*u0 //;s/}.*//;s/ *$//'
}

# 子 shell 里拿 UI 控件（find_ui 的轻量包装，只用一次 dump）
su_ui() {
  dump_ui >/dev/null 2>&1 || return 1
  python3 -c '
import sys, re, html
want = sys.argv[1]
for m in re.finditer(r"<node[^>]*/?>", open(sys.argv[2]).read()):
    tag = m.group(0)
    def attr(n):
        mm = re.search(n + r"=\"([^\"]*)\"", tag)
        return html.unescape(mm.group(1)) if mm else ""
    if attr("text") == want or attr("content-desc") == want:
        sys.exit(0)
sys.exit(1)
' "$1" "$UIDUMP" 2>/dev/null
}

# ---------- 投屏检测 ----------
# 不能用 UI dump：豆包在后台时它的界面不在 UI 树里，会误判成「没投屏」。
# 也不能查 dumpsys media_projection：它一直返回 null（实测）。
#
# 可靠信号是豆包的前台服务：
#   com.larus.nova/com.larus.flow.capture.screen.ScreenCaptureService
#     isForeground=true
# 只要在投屏它就活着，**跟哪个 App 占前台无关**。实测：投屏中 3 次，
# 退出后 0 次。这也就是为什么「豆包后台投屏 + 直播前台」能同时成立。
sharing() {
  local n
  n=$(R "dumpsys activity services $DOUBAO" 2>/dev/null | grep -c 'ScreenCaptureService')
  [ "${n:-0}" -gt 0 ] 2>/dev/null
}

# uiautomator 树里按完全相等找控件，返回 "x y"
# 必须完全相等：豆包的标题 text="正在共享屏幕" 含「共享屏幕」四字，
# 用子串匹配会点中标题(y≈804)而不是按钮(y≈2117)。
UIDUMP=$HOME/.nova/ui.xml

dump_ui() {
  R "uiautomator dump /sdcard/nova_ui.xml" >/dev/null 2>&1
  R "cat /sdcard/nova_ui.xml" > "$UIDUMP" 2>/dev/null
  [ -s "$UIDUMP" ]
}

find_ui() {
  dump_ui || return 1
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

# 断电重启后 WiFi 要几十秒才能关联上。豆包打电话要联网，
# 等不到就拉起来会直接失败，所以必须真等到能出网。
wait_online() {
  local i ip
  # 先看有没有 IP
  for i in $(seq 1 30); do
    ip=$(R "ip -4 addr show wlan0 2>/dev/null" | grep -o 'inet [0-9.]\+' | head -1)
    [ -n "$ip" ] && break
    [ "$i" = "1" ] && say "等 WiFi 拿 IP"
    sleep 2
  done
  [ -z "$ip" ] && { say "WiFi 没拿到 IP"; return 1; }

  # 再看到底能不能出网（MIUI 的 204 端点）
  for i in $(seq 1 30); do
    local cd
    cd=$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://connect.rom.miui.com/generate_204 2>/dev/null)
    [ "$cd" = "204" ] && { say "网络就绪（${ip#inet }）"; return 0; }
    [ "$i" = "1" ] && say "等网络出网"
    sleep 2
  done
  say "有 IP 但出不了网"
  return 1
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
  if sharing; then
    say "已经在投屏，不动手"
    return 0
  fi
  ensure_network
  ensure_screen_on
  # 豆包打电话需要联网；冷启动时 WiFi 可能还没起来
  wait_online || say "网络未就绪，先试一次"

  # 不要让直播 App force-stop：它一死 API 就没了，而且重启后相机要重新初始化。
  # 豆包的投屏流程不需要独占相机（实测流着的时候豆包在前台也不报错），
  # 真要抢也是 联屏后再拉直播，不在这一步做。
  R "input keyevent KEYCODE_WAKEUP" >/dev/null

  say "拉起豆包"
  R "am start -n $DOUBAO_ACT" >/dev/null
  sleep 6

  # 必须查前台，不能 grep activity activities——那个后台 task 也会命中
  local i
  for i in 1 2 3; do
    case "$(top_act)" in
      *"$DOUBAO"*) break ;;
    esac
    R "am start -n $DOUBAO_ACT" >/dev/null 2>&1
    sleep 3
  done
  case "$(top_act)" in
    *"$DOUBAO"*) ;;
    *) say "豆包没起来（前台是 $(top_act)）"; return 1 ;;
  esac

  # 打电话 —— 点完必须确认通话页真的起来了
  local pt
  pt=$(find_ui "打电话")
  if [ -n "$pt" ]; then
    say "点「打电话」@ $pt"
    tap $pt
  else
    say "没找到「打电话」"
  fi
  # 通话页判定：只认 RealtimeCallActivity。
  # 不能用「立即开始」当信号——那是系统投屏授权弹窗的按钮，
  # 弹窗盖住整个屏幕时 UI dump 只有弹窗，会误判成「通话页已起」。
  local ok_call=0 j
  for j in $(seq 1 10); do
    case "$(top_act)" in
      *RealtimeCallActivity*) ok_call=1; break ;;
    esac
    sleep 2
  done
  if [ "$ok_call" != "1" ]; then
    pt=$(find_ui "打电话")
    [ -n "$pt" ] && { say "通话页没起，再点一次 @ $pt"; tap $pt; }
    for j in $(seq 1 8); do
      sleep 2
      case "$(top_act)" in
        *RealtimeCallActivity*) ok_call=1; break ;;
      esac
    done
  fi
  [ "$ok_call" = "1" ] && say "通话页已起" || { say "通话页没起（前台 $(top_act)）"; return 1; }

  # 共享屏幕。注意弹窗可能已经在屏幕上（盖住了通话页），
  # 这时 UI dump 里根本没有「共享屏幕」，得先处理弹窗。
  local pt k
  if ! su_ui "立即开始" >/dev/null; then
    pt=$(find_ui "共享屏幕")
  fi
  local tried_share=0
  if [ -n "$pt" ]; then
    say "点「共享屏幕」@ $pt"
    tap $pt
    tried_share=1
  fi

  # 授权弹窗：标题「要开始使用豆包录制或投射内容吗？」按钮「立即开始」。
  # 它可能立刻弹（此时 tried_share=1），也可能是上次没关掉的残留（tried_share=0）。
  # 两种都得点，反复轮询直到投屏真的起来。
  local sp clicked_last=0
  for k in $(seq 1 12); do
    sharing && { say "投屏已开"; return 0; }
    sp=$(find_ui "立即开始")
    if [ -n "$sp" ]; then
      say "点「立即开始」@ $sp"
      tap $sp
      clicked_last=1
      sleep 3
      continue
    fi
    # 既没投屏、也没弹窗：可能是按钮没找到、或界面还没到达
    if [ "$tried_share" = "0" ]; then
      pt=$(find_ui "共享屏幕")
      [ -n "$pt" ] && { say "点「共享屏幕」@ $pt"; tap $pt; tried_share=1; sleep 2; continue; }
    fi
    sleep 2
  done
  sharing && { say "投屏已开"; return 0; }
  say "投屏没开起来（前台 $(top_act)）"
  return 1
}

# ---------- 共存模式 ----------
#
# 关键认识（之前搞错了）：不需要在「投屏」和「直播」之间二选一。
# 正确顺序是：先在豆包里开屏幕共享，再把直播 App 拉前台。
#
# 豆包的屏幕共享跑在 ScreenCaptureService（前台服务）里，
# 切到后台不影响；直播 App 占了前台就能拿相机。
# 于是豆包看得到直播画面，直播也不断流。
#
# 实测：直播前台 + 豆包后台投屏 → 帧 564544B HTTP 200，
#       ScreenCaptureService isForeground=true 仍在。

# 把直播拉前台（不碰豆包的投屏）
promote_lens() {
  local top
  top=$(R "dumpsys activity activities" | grep -m1 topResumedActivity)
  case "$top" in
    *"$PKG"*) return 0 ;;
  esac
  say "直播拉前台"
  R "am start -n $ACT" >/dev/null 2>&1
  sleep 3
  top=$(R "dumpsys activity activities" | grep -m1 topResumedActivity)
  case "$top" in
    *"$PKG"*) return 0 ;;
  esac
  return 1
}

mode_both() {
  # 1) 投屏先开（豆包得在前台才能操作）
  if ! ensure_share; then
    say "投屏没搞成，只保直播"
    promote_lens
    ensure_lens
    return 1
  fi
  # 2) 立刻把直播拉前台；豆包的 ScreenCaptureService 在后台继续跑
  promote_lens
  sleep 2
  # 3) 直播参数 + 出图
  ensure_lens
  # 4) 确认两边同时成立
  #    这里也用 HEAD 查帧，不下载 500KB（见 frame_alive 注释）
  local ok_share=0 ok_lens=0
  sharing && ok_share=1
  frame_alive && ok_lens=1
  if [ "$ok_share" = "1" ] && [ "$ok_lens" = "1" ]; then
    say "并存 OK：投屏在，直播在出图"
    return 0
  fi
  say "状态：投屏=$ok_share 直播=$ok_lens"
  return 1
}

mode_lens() {
  if sharing; then
    # 投屏已在（可能豆包在前台）。把直播拉前台，两边都要
    promote_lens
    ensure_lens
    return $?
  fi
  mode_both || { say "回退：只保直播"; promote_lens; ensure_lens; }
}

status() {
  echo "===== $(date '+%F %T') ====="
  echo "root:       $(R 'id' 2>/dev/null | head -1)"
  echo "前台:       $(R 'dumpsys activity activities' | grep -m1 topResumedActivity | sed 's/.*u0 //;s/}.*//')"
  echo "屏幕:       $(R 'dumpsys power' | grep -m1 mWakefulness= | sed 's/.*mWakefulness=//;s/,.*//')"
  echo "飞行:       $(R 'settings get global airplane_mode_on')"
  echo "投屏:       $(sharing && echo 开 || echo 关)"
  local st; st=$(api_state)
  if [ -n "$st" ]; then
    echo "直播:       $(echo "$st" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("streaming=%s %s q%s zoom%s"%(d.get("streaming"),d.get("selectedResolution"),d.get("qualityValue"),d.get("zoomValue")))' 2>/dev/null)"
  else
    echo "直播:       API 不通"
  fi
  # 守护存活判断必须扫真实进程，不能只看 $PIDF。
  #
  # 坑：PIDF 是守护自己写的（echo $$ > $PIDF），但由 service.d 起来的
  # 守护是 root，它的 $HOME 不是 Termux 的 $HOME —— Termux 用户去读
  # $HOME/.nova/watch.pid 读的是自己的目录，里面没有 root 守护的 pid。
  # 实测后果：2026-11-15 14:33 进程扫描明明看到 WATCH 16252 活着，
  # nova.sh status 却报「守护: 没跑」，又是一次自己骗自己。
  local wp; wp=$(find_watch_pids 2>/dev/null)
  local nids; nids=$(echo "$wp" | sed '/^$/d' | wc -l | tr -d ' ')
  if [ "${nids:-0}" -gt 0 ] 2>/dev/null; then
    local list; list=$(echo "$wp" | awk '{printf "%s ", $1}')
    if [ "$nids" -gt 1 ]; then
      echo "守护:       跑着 ${nids} 个（异常，应只有 1 个）pid=${list}"
    else
      echo "守护:       跑着 pid=${list}"
    fi
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

# 单实例：已有 nova watch 在跑就退出。
#
# 这里不能用 *nova.sh*watch* 子串匹配 —— 本脚本运行时自己的 cmdline
# 是 /data/adb/service.d/nova.sh，虽然不含 watch，但这个监控脚本曾经
# 被同样的写法坑过（见下面巡检循环的注释）。统一用严格三段式匹配：
#   参数正好 3 个，第 2 个是 ./nova.sh，第 3 个是 watch。
for d in /proc/[0-9]*; do
  [ -r "$d/cmdline" ] || continue
  c=$( { tr '\0' ' ' < "$d/cmdline"; } 2>/dev/null ) || continue
  set -- $c
  [ $# -eq 3 ] || continue
  [ "$2" = "./nova.sh" ] || continue
  [ "$3" = "watch" ] || continue
  log "已有实例（${d#/proc/}），退出"
  exit 0
done

log "启动 pid=$$"
wait_boot || log "等 boot_completed 超时，继续"
log "系统就绪"

# boot_completed=1 不代表能操作：MIUI 还要几十秒初始化包管理器/
# 通知/输入服务。太早 am start 会失败。
sleep 15
log "延迟 15s 后开始"

# Termux 的 bash 是唯一能跑 nova.sh 的（用了 bash 语法）
TB=/data/data/com.termux/files/usr/bin/bash
until [ -x "$TB" ]; do log "等 $TB"; sleep 5; done

# 每 20s 确保守护活着（进程死了就重拉）
#
# 匹配必须严格。这个监控脚本自己的 cmdline 里也含 "nova.sh" 和 "watch"，
# 用 *nova.sh*watch* 子串匹配会匹配到自己 —— 于是永远认为「守护还在」，
# 真死了也不重拉，开机后静静躺平。
while true; do
  alive=0
  for d in /proc/[0-9]*; do
    [ -r "$d/cmdline" ] || continue
    c=$( { tr '\0' ' ' < "$d/cmdline"; } 2>/dev/null ) || continue
    set -- $c
    [ $# -eq 3 ] || continue
    [ "$2" = "./nova.sh" ] || continue
    [ "$3" = "watch" ] || continue
    alive=1
    break
  done
  if [ "$alive" = "0" ]; then
    log "守护不在，拉起"
    cd /data/data/com.termux/files/home && \
      $TB ./nova.sh watch >>"$LOG" 2>&1 &
    sleep 5
  fi
  sleep 20
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

# 找正在跑的 nova.sh watch（任意 uid）。返回 pid，没有则空。
#
# 为什么必须走 root 扫：service.d 起的守护是 root，Termux 自己起的
# 是 u0_a243。同一个 Termux 用户扫不到 root 进程，于是会出现
# 「root 守护 + Termux 守护」两个同时在跑，每 20 秒互相抢投屏和 API。
# 这不是理论风险，2026-11-15 实测把直播 API 打到端口都不监听了。
find_watch_pids() {
  # 找正在跑的 nova.sh watch。返回 "pid 可执行文件路径"，一行一个。
  #
  # 两个必须同时满足的过滤条件：
  #
  # A) 参数严格是 3 个，且第 2、3 个是 ./nova.sh watch。
  #    不能用 *nova.sh*watch* 子串匹配 —— 扫描命令自己的 cmdline 里
  #    就带这两个词，会匹配到自己，于是永远「已有守护在跑」。
  #
  # B) 排除自己这条进程链（$$ 及其祖宗）。
  #    坑：这个函数是 R() 调用的，R 在非 root 下走 su -c，
  #    而 su 会 fork 一个 sh -c，那条命令行正好是
  #      sh -c for d in /proc/... [ "$2" = "./nova.sh" ] [ "$3" = "watch" ] ...
  #    展开后参数形态和真的守护一模一样，条件 A 拦不住它。
  #    实测后果：2026-11-15 所有启动都被自己挡住，日志里刷
  #    「已有守护在跑（3276 3277）」而实际没有任何守护。
  #
  # 所以按进程链排除：从左往右读 ppid，凡是在这条链上的 pid 全跳过。
  #
  # C) 再排除「父进程也是 nova.sh watch」的候选。
  #    坑：bash 执行 $(...) 会 fork 子进程，子进程在 exec 之前
  #    /proc/<pid>/cmdline 完全继承父进程的 argv，就是
  #      bash ./nova.sh watch
  #    正好三个参数，A 和 B 都拦不住。守护每 20s 跑一堆 $(...)，
  #    于是扫描必然看到一群“假守护”。
  #    实测后果：2026-11-15 22:40 日志报「已有守护在跑（16252 2444 2447）」，
  #    后两个是瞬时子进程。
  #    真守护的父进程是 service.d 的 sh 或终端，不会是 nova.sh；
  #    所以父 cmdline 是三段式 nova.sh watch 的，一定是 fork 未 exec 的影子。
  #
  # 附：取 ppid 不能用 awk '{print $4}' —— /proc/pid/stat 的 comm 字段
  # 带括号且可含空格（如 (sh -c)），字段位置会错。先剥掉括号那段。
  # shellcheck disable=SC2016
  RS 'ppid_of() {
         s=$(cat /proc/$1/stat 2>/dev/null) || return 1
         s=${s##*)}          # 去掉 "pid (comm)" 前缀
         set -- $s
         echo "${1:-}"
       }
       is_nova() {
         [ -r "/proc/$1/cmdline" ] || return 1
         set -- $( { tr "\0" " " < "/proc/$1/cmdline"; } 2>/dev/null )
         [ $# -eq 3 ] && [ "$2" = "./nova.sh" ] && [ "$3" = "watch" ]
       }
       chain=" $$ "
       p=$$
       while :; do
         n=$(ppid_of "$p")
         [ -n "$n" ] || break
         [ "$n" = "0" ] && break
         [ "$n" = "1" ] && break
         case "$chain" in *" $n "*) break ;; esac
         chain="$chain$n "
         p=$n
       done
       for d in /proc/[0-9]*; do
         [ -r "$d/cmdline" ] || continue
         pid=${d#/proc/}
         case "$chain" in *" $pid "*) continue ;; esac
         is_nova "$pid" || continue
         pp=$(ppid_of "$pid")
         [ -n "$pp" ] && is_nova "$pp" && continue
         echo "$pid $(set -- $( { tr "\0" " " < "$d/cmdline"; } 2>/dev/null ); echo "$1")"
       done ' | tr -d '\r' | awk '$1 ~ /^[0-9]+$/ {print $1, $2}' | sort -u
  #    ^^ 用 RS（stderr 不合并）；后面的 awk 再按「第一列必须是纯数字」过一道——
  #       双重保险，就算 su / sh 往 stdout 说了什么，也不会被当成 pid。
}

acquire_lock() {
  # 1) 先扫真实进程（能看见 root 的）
  local pids other line pid
  pids=$(find_watch_pids)
  other=""
  for line in $pids; do
    pid=${line%% *}
    [ "$pid" = "$$" ] && continue
    case "$pid" in
      ''|*[!0-9]*) continue ;;
    esac
    other="$other $pid"
  done
  if [ -n "${other# }" ]; then
    say "已有守护在跑（${other# }），不重复起"
    echo "${other# }" | awk '{print $1}' > "$PIDF" 2>/dev/null
    return 1
  fi

  # 2) 原子锁兜底
  #
  # 为什么不再用 mkdir 锁：mkdir 锁需要「判断持锁者是否还活着」，
  # 而 pid 会复用、pid 文件可能残留，判错就等于两个守护并存。
  # flock 由内核管，进程一死锁自动释放，没有这个歧义。
  #
  # 锁放 $HOME/.nova：这个目录本来就是 0777，root（service.d）和
  # Termux 用户（手动启动）都能写。
  # 不要放 /data/local/tmp —— 实测权限是 drwxrwx--x shell shell，
  # Termux 用户没有写权限，exec 9> 直接失败。更糟的是如果忽略这个
  # 失败，flock 就作用在一个没打开的 fd 上，等于没锁。
  if command -v flock >/dev/null 2>&1; then
    if ! { exec 9>"$LOCKF"; } 2>/dev/null; then
      say "✗ 打不开锁文件 $LOCKF，拒绝启动（避免两个守护并存）"
      return 1
    fi
    if ! flock -n 9; then
      say "已有守护持锁，不重复起"
      return 1
    fi
    echo $$ > "$PIDF" 2>/dev/null
    return 0
  fi

  # 没有 flock 时的兜底：mkdir 原子锁
  R "mkdir -p /data/local/tmp 2>/dev/null; chmod 777 /data/local/tmp 2>/dev/null" >/dev/null 2>&1
  if ! mkdir "$LOCKD" 2>/dev/null; then
    say "锁在，不重复起"
    return 1
  fi
  echo $$ > "$LOCKD/pid" 2>/dev/null
  echo $$ > "$PIDF" 2>/dev/null
  return 0
}

case "${1:-watch}" in
  once)   mode_both ;;
  lens)   mode_lens ;;
  share)  ensure_share ;;
  status) status ;;
  install) do_install ;;
  stop)
    # 必须用 root 杀：service.d 起的守护是 root，Termux 的 pkill 杀不掉，
    # 于是「停了」只是假象，老的还在跑。
    pids=$(find_watch_pids)
    n=0
    for line in $pids; do
      pid=${line%% *}
      case "$pid" in ''|*[!0-9]*) continue ;; esac
      R "kill -9 $pid" >/dev/null 2>&1 && n=$((n+1))
    done
    rm -f "$PIDF"; rm -rf "$LOCKD" 2>/dev/null
    echo "已停 ${n} 个守护"
    ;;
  watch)
    acquire_lock || exit 1
    trap 'rm -f "$PIDF"; rm -rf "$LOCKD" 2>/dev/null; exit 0' INT TERM
    say "守护启动（手机独立，不依赖板子）"
    # 一切正常时循环是静默的（这样日志才有用）。
    # 但静默也意味着「看不出还活着」，所以每 15 轮（20s x 15 = 5 分钟）
    # 写一行心跳，带上关键状态。开销可以忽略。
    hb=0
    while :; do
      mode_lens
      rc=$?
      hb=$((hb+1))
      if [ "$hb" -ge 15 ]; then
        hb=0
        # 心跳：只读状态，不下载帧
        sh=0; ln=0; hres="-"
        sharing && sh=1
        hres=$(lens_probe 2>/dev/null | cut -d" " -f3)
        frame_alive && ln=1
        say "心跳 投屏=$sh 直播=$ln $hres rc=$rc"
      fi
      sleep 20
    done
    ;;
  *) echo "用法: $0 {once|lens|share|watch|status|install|stop}" ;;
esac
