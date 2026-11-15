#!/data/data/com.termux/files/usr/bin/bash
# 三轮验证：每轮从干净状态起，逐步核对每一步的验证信号
export PATH=/data/data/com.termux/files/usr/bin:$PATH
export HOME=/data/data/com.termux/files/home
export TMPDIR=/data/data/com.termux/files/usr/tmp
cd "$HOME" || exit 1
mkdir -p "$HOME/.nova"
UIT="$HOME/.nova/tu.xml"     # 绝对路径，避免 python 里变量不展开

DOUBAO=com.larus.nova
DOUBAO_ACT=$DOUBAO/com.larus.home.impl.alias.AliasActivity1
PKG=com.opencode.multilensipcam
ACT=$PKG/.MainActivity
API=http://127.0.0.1:41737
R() { su -c "$*" 2>/dev/null; }

top_act() { R "dumpsys activity activities" | grep -m1 topResumedActivity | sed 's/.*u0 //;s/}.*//;s/ *$//'; }
share_n() { R "dumpsys activity services $DOUBAO" | grep -c ScreenCaptureService; }
frame()   { curl -sL -m 18 -o /dev/null -w '%{size_download} %{http_code}' "$API/snapshot.jpg"; }
params()  { curl -s -m 10 "$API/api/state" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("%s|%s|%s|%s"%(d.get("streaming"),d.get("selectedResolution"),d.get("qualityValue"),d.get("zoomValue")))' 2>/dev/null; }

dump_ui() { R "uiautomator dump /sdcard/tu.xml" >/dev/null 2>&1; R "cat /sdcard/tu.xml" > "$UIT" 2>/dev/null; [ -s "$UIT" ]; }

# 完全相等匹配，返回 "x y"
find_ui() {
  dump_ui || return 1
  python3 -c '
import sys,re,html
want,p=sys.argv[1],sys.argv[2]; lm=[]
for m in re.finditer(r"<node[^>]*/?>",open(p).read()):
    t=m.group(0)
    def a(n):
        mm=re.search(n+r"=\"([^\"]*)\"",t); return html.unescape(mm.group(1)) if mm else ""
    b=a("bounds")
    if not b: continue
    if a("text")==want or a("content-desc")==want:
        mm=re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",b)
        if mm:
            x1,y1,x2,y2=map(int,mm.groups()); lm.append((0 if a("clickable")=="true" else 1,(x1+x2)//2,(y1+y2)//2))
if not lm: sys.exit(1)
lm.sort(); print(lm[0][1],lm[0][2])
' "$1" "$UIT" 2>/dev/null
}

has_ui() {
  dump_ui || return 1
  python3 -c '
import sys,re,html
want,p=sys.argv[1],sys.argv[2]
for m in re.finditer(r"<node[^>]*/?>",open(p).read()):
    t=m.group(0)
    def a(n):
        mm=re.search(n+r"=\"([^\"]*)\"",t); return html.unescape(mm.group(1)) if mm else ""
    if a("text")==want or a("content-desc")==want: sys.exit(0)
sys.exit(1)
' "$1" "$UIT" 2>/dev/null
}

clean_state() {
  R "am start -n $DOUBAO_ACT" >/dev/null; sleep 5
  local pt i
  pt=$(find_ui "退出屏幕共享")
  [ -n "$pt" ] && { R "input tap $pt" >/dev/null; sleep 4; }
  for i in 1 2 3 4; do [ "$(share_n)" = "0" ] && break; sleep 2; done
}

for round in 1 2 3; do
  echo ""
  echo "════════════ 第 $round 轮 ════════════"
  clean_state
  echo "基线: 投屏=$(share_n) 帧=$(frame)"

  R "am start -n $DOUBAO_ACT" >/dev/null; sleep 6
  t=$(top_act); case "$t" in *"$DOUBAO"*) v1="ok";; *) v1="NG";; esac
  echo "1) 豆包前台 .... $v1   ($t)"

  pt=$(find_ui "打电话"); [ -n "$pt" ] && R "input tap $pt" >/dev/null
  call_ok=0
  for i in $(seq 1 10); do
    sleep 2
    case "$(top_act)" in *RealtimeCallActivity*) call_ok=1; break;; esac
  done
  [ "$call_ok" = "1" ] && v2="ok" || v2="NG"
  echo "2) 通话页 ...... $v2   (点@${pt:-无} 前台=$(top_act))"

  # 弹窗可能盖住页面：先处理弹窗，再找共享屏幕
  sp=$(find_ui "立即开始")
  if [ -n "$sp" ]; then
    echo "   注意: 已有授权弹窗 @ $sp，先点它"
    R "input tap $sp" >/dev/null; sleep 3
    pt=$(find_ui "共享屏幕")
  else
    pt=$(find_ui "共享屏幕")
  fi
  if [ -n "$pt" ]; then R "input tap $pt" >/dev/null; sleep 3; v3="ok"; else v3="NG"; fi
  echo "3) 共享屏幕 .... $v3   (点@${pt:-无})"

  # 轮询：投屏起来 / 弹窗出现就点
  v5="NG"; v4="skip"
  for i in $(seq 1 14); do
    [ "$(share_n)" -gt 0 ] && { v5="ok"; break; }
    sp=$(find_ui "立即开始")
    if [ -n "$sp" ]; then
      R "input tap $sp" >/dev/null; v4="ok"; echo "4) 立即开始 .... ok   (点@$sp)"; sleep 3; continue
    fi
    sleep 2
  done
  [ "$v4" = "skip" ] && echo "4) 立即开始 .... skip (无弹窗)"
  [ "$(share_n)" -gt 0 ] && v5="ok" || v5="NG"
  echo "5) 投屏已开 .... $v5   (ScreenCaptureService=$(share_n))"

  v6="NG"
  for i in 1 2 3 4; do
    R "am start -n $ACT" >/dev/null 2>&1; sleep 3
    case "$(top_act)" in *"$PKG"*) v6="ok"; break;; esac
    sleep 2
  done
  t=$(top_act)
  echo "6) 直播前台 .... $v6   ($t)"

  curl -s -m 10 -X POST "$API/api/control?resolution=3840x2160&quality=100&zoom=1.53" >/dev/null
  sleep 2
  p=$(params)
  case "$p" in True\|3840x2160\|100\|1.53*) v7="ok";; *) v7="NG";; esac
  echo "7) 参数正确 .... $v7   ($p)"

  curl -s -m 10 -X POST "$API/api/control?streaming=true" >/dev/null
  sleep 2
  f=$(frame); sz=${f%% *}; cd=${f##* }
  if [ "${cd:-000}" = "200" ] && [ "${sz:-0}" -gt 10000 ] 2>/dev/null; then v8="ok"; else v8="NG"; fi
  echo "8) 出图 ........ $v8   ($f)"

  [ "$(share_n)" -gt 0 ] && v9a="ok" || v9a="NG"
  case "$(top_act)" in *"$PKG"*) v9b="ok";; *) v9b="NG";; esac
  echo "9) 并存 ........ 投屏$v9a 直播前台$v9b  (ScreenCaptureService=$(share_n))"

  echo ">>> 第 $round 轮: $v1 $v2 $v3 $v4 $v5 $v6 $v7 $v8 $v9a/$v9b"
done

echo ""
echo "════════════ 完成 ════════════"
