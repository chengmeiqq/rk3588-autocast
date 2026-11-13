#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全镜直播(LensCast) 守护：插着 USB 时自动开播，掉了就恢复。

为什么走 HTTP API 而不是点界面：
  这个 App 自带 Web 控制台（默认 41737），参数全在 `/api/control` 上。
  点界面要处理滚动、滑块坐标、下拉框选中态，UI 一改就全废；
  API 是结构化参数，改完还能用 `/api/state` 回读确认。实测一次成功。

为什么用 adb forward 而不是手机 IP：
  手机 IP 是 DHCP 来的（本次 192.168.1.119），换网段就断。
  插着 USB 时 `adb forward tcp:41737 tcp:41737` 让板子的 127.0.0.1:41737
  直通手机，和 IP 无关，插上就一定通。

目标参数（用户指定，实测可达 3840x2160）：
  摄像头 后摄 3   camera=0
  分辨率 3840x2160
  对焦   自动     focus=auto
  变焦   1.53     zoom=1.53
  推流质量 100%
  开始直播

不做什么（边界）：
  - 拔了 USB 完全不动（和 keepcast 同一个门禁），用户拿着手机时不该被抢相机
  - state/lenscast.pause 存在时完全不动
  - 不点界面，因此不会和 Tasker 的投屏点击互相干扰
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import keepcast                                     # noqa: E402  (复用 usb_present/device_ready)

STATE_DIR = os.path.join(HERE, "state")
STATE = os.path.join(STATE_DIR, "lenscast.json")
PAUSE = os.path.join(STATE_DIR, "lenscast.pause")
LOG = os.path.join(HERE, "logs", "lenscast.log")

ADB = "/usr/lib/android-sdk/platform-tools/adb"
PKG = os.environ.get("LENS_PKG", "com.opencode.multilensipcam")
ACT = PKG + "/.MainActivity"
PORT = int(os.environ.get("LENS_PORT", "41737"))
BASE = "http://127.0.0.1:%d" % PORT

CHECK_EVERY = float(os.environ.get("LENS_INTERVAL", "30"))
BACKOFF_MAX = 600.0

# 用户指定的目标参数。改这里就是改自动化目标。
WANT = {
    "camera": "0",             # 后摄 3（logical 0）
    "resolution": "3840x2160",
    "quality": "100",
    "zoom": "1.53",            # 变焦（不是对焦距离）
}
WANT_FOCUS_MODE = "auto"       # 对焦交给相机自己，不手动锁距离
WANT_FPS = "30"

# 判活阈值：一帧 4K JPEG 至少几百 KB，小于这个数当没出图
MIN_FRAME = 20000


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > 2_000_000:
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def save_state(**kw):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        old = {}
        if os.path.exists(STATE):
            try:
                old = json.load(open(STATE, encoding="utf-8"))
            except Exception:
                old = {}
        old.update(kw)
        old["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False)
        os.replace(tmp, STATE)
    except Exception:
        pass


def http(path, timeout=15):
    """访问 App 的 HTTP 接口。返回 (status|None, bytes)。"""
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:
        return None, b""


def get_state():
    code, body = http("/api/state", timeout=8)
    if code != 200:
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def ctl(**params):
    q = "&".join("%s=%s" % (k, urllib.parse.quote(str(v))) for k, v in params.items())
    code, body = http("/api/control?" + q, timeout=15)
    return code == 200 and b'"ok":true' in body.replace(b" ", b"")


def ensure_forward(serial):
    """把手机 41737 映射到板子 127.0.0.1:41737。幂等，失败不影响后续重试。"""
    try:
        subprocess.run([ADB, "-s", serial, "forward", "tcp:%d" % PORT, "tcp:%d" % PORT],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        return True
    except Exception:
        return False


def app_alive(serial):
    try:
        p = subprocess.run([ADB, "-s", serial, "shell", "pidof " + PKG],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        return bool(p.stdout.strip())
    except Exception:
        return False


def start_app(serial):
    try:
        subprocess.run([ADB, "-s", serial, "shell", "am start -n " + ACT],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        return True
    except Exception:
        return False


def crash_reason(serial):
    """最近的崩溃原因。这句 `disabled by policy` 是实测遇到的真实故障。"""
    try:
        p = subprocess.run(
            [ADB, "-s", serial, "shell",
             "su -c 'logcat -d -t 200 2>/dev/null | grep -i am_crash | grep multilens | tail -1'"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=25)
        line = p.stdout.decode("utf-8", "replace").strip()
        if "disabled by policy" in line:
            return "镜头被系统策略禁用（disabled by policy）"
        if line:
            return line.split("am_crash:")[-1][:120]
        return ""
    except Exception:
        return ""


def frame_size():
    """抓一帧，返回 JPEG 字节数（0 = 没图）。走 302 跳转后取实体。"""
    try:
        out = subprocess.run(
            ["curl", "-sL", "-m", "30", "-o", "/tmp/_lens_probe.jpg",
             "-w", "%{http_code} %{size_download}", BASE + "/snapshot.jpg"],
            capture_output=True, text=True, timeout=40).stdout.split()
        if len(out) == 2 and out[0] == "200":
            return int(out[1])
        return 0
    except Exception:
        return 0


def _num_eq(a, b, tol=0.01):
    try:
        return abs(float(a) - float(b)) < tol
    except (TypeError, ValueError):
        return False


def params_ok(st):
    """关键参数是否已经等于目标值。"""
    if st is None:
        return False
    return (st.get("selectedCameraKey") == WANT["camera"]
            and st.get("selectedResolution") == WANT["resolution"]
            and st.get("qualityValue") == int(WANT["quality"])
            and st.get("focusMode") == WANT_FOCUS_MODE
            and _num_eq(st.get("zoomValue"), WANT["zoom"]))


def apply_params(st):
    """按目标值设参数。顺序有意义：
    先设对焦模式再设变焦 —— 改 zoom 会触发相机重新对焦。
    分辨率要在 camera 之后设：换镜头会重置可选分辨率列表。
    """
    if st.get("selectedCameraKey") != WANT["camera"]:
        ctl(camera=WANT["camera"])
        time.sleep(1.5)
    if st.get("selectedResolution") != WANT["resolution"]:
        ctl(resolution=WANT["resolution"])
        time.sleep(1)
    if st.get("qualityValue") != int(WANT["quality"]):
        ctl(quality=WANT["quality"])
        time.sleep(0.5)
    # 对焦模式：用户要「自动」，交给相机自己，不锁距离。
    # 参数名是 focus 不是 focusMode —— 这是 App 的 API 命名，写错了不报错，
    # 只是静默不生效（实测踩过：传 focusMode=manual 返回 ok:true 但模式不变）。
    if st.get("focusMode") != WANT_FOCUS_MODE:
        ctl(focus=WANT_FOCUS_MODE)
        time.sleep(1.2)
        st = get_state() or {}
    # 变焦：参数名是 zoom。注意别和对焦距离（focusDistance）搞混 ——
    # 两者都接受 1.53，但 zoom 是画面放大倍数，focusDistance 是镜头到焦面距离。
    # 改 zoom 会触发相机重新对焦，所以放在对焦模式之后。
    if not _num_eq(st.get("zoomValue"), WANT["zoom"]):
        ctl(zoom=WANT["zoom"])
        time.sleep(0.8)
        st = get_state() or {}
    return st


def bring_up(serial):
    """把直播拉起来。返回 (ok, note)。"""
    if not app_alive(serial):
        log("全镜直播没在跑，拉起")
        start_app(serial)
        time.sleep(7)

    ensure_forward(serial)
    st = get_state()
    if st is None:
        # 进程在但 HTTP 不通，通常还在初始化
        time.sleep(5)
        st = get_state()
        if st is None:
            if not app_alive(serial):
                why = crash_reason(serial)
                return False, "HTTP 不通且进程已死 %s" % why
            return False, "HTTP 不通（App 在初始化或端口未就绪）"

    st = apply_params(st) or get_state() or {}

    if not (st or {}).get("streaming"):
        if not ctl(streaming="true"):
            return False, "开流指令被拒"
        time.sleep(7)

    st = get_state() or {}
    if not st.get("streaming"):
        if not app_alive(serial):
            return False, "开流后崩溃：%s" % crash_reason(serial)
        return False, "streaming 仍为 false"

    size = frame_size()
    if size < MIN_FRAME:
        if not app_alive(serial):
            return False, "抓帧时崩溃：%s" % crash_reason(serial)
        return False, "无画面（%d 字节）" % size

    return True, "直播中 %s %s 质量%s 对焦%s 变焦%s 帧%dKB" % (
        st.get("selectedCameraLabel"), st.get("selectedResolution"),
        st.get("qualityValue"), st.get("focusMode"),
        st.get("zoomValue"), size // 1024)


def main():
    log("lenscast 守护启动（间隔 %.0fs，目标 %s / %s / 质量%s / 对焦%s / 变焦%s）"
        % (CHECK_EVERY, WANT["camera"], WANT["resolution"],
           WANT["quality"], WANT_FOCUS_MODE, WANT["zoom"]))
    fails = 0
    last_ok = None

    while True:
        try:
            if os.path.exists(PAUSE):
                time.sleep(CHECK_EVERY)
                continue

            plugged, why_usb = keepcast.usb_present()
            if not plugged:
                save_state(ok=False, note="USB 未连接，守护待命", why=why_usb)
                fails = 0
                time.sleep(CHECK_EVERY * 2)
                continue

            serial = keepcast.smartcast.pick_serial()
            if not keepcast.device_ready(serial):
                save_state(ok=False, serial=serial, note="USB 在但 adb 不通")
                time.sleep(CHECK_EVERY)
                continue

            # 先看能否零成本判定健康：进程在 + 参数对 + 正在流
            st = get_state()
            if st and st.get("streaming") and params_ok(st):
                size = frame_size()
                if size >= MIN_FRAME:
                    if last_ok != "healthy":
                        log("直播正常（%s %s 帧%dKB）" % (
                            st.get("selectedCameraLabel"), st.get("selectedResolution"),
                            size // 1024))
                        last_ok = "healthy"
                    save_state(ok=True, serial=serial, note="直播中",
                               camera=st.get("selectedCameraLabel"),
                               resolution=st.get("selectedResolution"),
                               quality=st.get("qualityValue"),
                               focus=st.get("focusMode"),
                               zoom=st.get("zoomValue"), why="")
                    fails = 0
                    time.sleep(CHECK_EVERY)
                    continue

            ok, note = bring_up(serial)
            if ok:
                log(note)
                last_ok = "healthy"
                fails = 0
                save_state(ok=True, serial=serial, note=note, why="")
                time.sleep(CHECK_EVERY)
                continue

            fails += 1
            log("未能拉起：%s（第 %d 次）" % (note, fails))
            save_state(ok=False, serial=serial, note="未能拉起", why=note)
            last_ok = None
            # 退避：连续失败时别每 30 秒锤一次，4K 编码很吃资源
            wait = min(CHECK_EVERY * (2 ** min(fails - 1, 4)), BACKOFF_MAX)
            time.sleep(wait)
        except Exception as e:
            log("守护出错（继续）：%s" % str(e)[:100])
            time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
