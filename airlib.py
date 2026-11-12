# -*- coding: utf-8 -*-
"""airlib - Airtest 的薄封装，把常用操作压成一行。

最小用法：

    from airlib import phone
    p = phone()                 # 自动连上手机
    p.home()                    # 回桌面
    p.tap_image("btn.png")      # 找图并点
    p.assert_image("ok.png")    # 图不在就报错（等于断言）
    p.shot("result.png")        # 截图存档

在 airtest run 的 .air 脚本里也能直接用（已装进 venv，直接 import）。
"""
from __future__ import print_function

import os
import subprocess
import time
import warnings

os.environ.setdefault("ANDROID_HOME", "/usr/lib/android-sdk")
os.environ["PATH"] = "/usr/lib/android-sdk/platform-tools:" + os.environ.get("PATH", "")

import logging  # noqa: E402

logging.getLogger("airtest").setLevel(logging.WARNING)
warnings.filterwarnings("ignore", message=".*efficiency may be very low.*")

from airtest.core.api import connect_device, device  # noqa: E402
from airtest.aircv import imread, find_template  # noqa: E402

# 注意：airtest 在 import 时自己把日志设成 DEBUG，所以必须在这之后再压低
logging.getLogger("airtest").setLevel(logging.WARNING)
warnings.filterwarnings("ignore", message=".*efficiency may be very low.*")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

def alive(serial, timeout=6):
    """这台设备现在真能用吗。

    `adb devices` 写着 device 不算数：WiFi 断了以后那条 TCP 连接会变成
    "半死"状态，列表里还挂着，但任何命令都不返回。必须真发一条命令试。
    """
    if not serial:
        return False
    try:
        r = subprocess.run(["adb", "-s", serial, "shell", "echo ok"],
                           capture_output=True, text=True, timeout=timeout)
        return "ok" in (r.stdout or "")
    except Exception:
        return False


def system_ready(serial, timeout=12):
    """系统服务都注册好了吗。

    sys.boot_completed=1 只说明"启动流程走完了"，system_server 的服务
    可能还在陆续注册。实测踩过：boot_completed 已是 1，但
    `screencap` 报 cmd: Can't find service: window —— 因为 window
    服务还没上线。这时候截图/取焦点全废，流程必然失败。

    所以要再加一道：window / package / activity 这些关键服务在，
    且已经有窗口焦点。三个都满足才算真能用。
    """
    checks = [
        ("window",   "service check window"),
        ("package",  "service check package"),
        ("activity", "service check activity"),
    ]
    for name, cmd in checks:
        try:
            r = subprocess.run(["adb", "-s", serial, "shell", cmd],
                               capture_output=True, text=True, timeout=timeout)
            out = (r.stdout or "").strip().lower()
        except Exception:
            return False, "%s 查询超时" % name
        # `service check` 找不到会输出 "Service %s: not found"
        if "not found" in out or "notfound" in out.replace(" ", ""):
            return False, "%s 服务还没起来" % name
    # 注意：不能把 mCurrentFocus=null 当成"没就绪"。
    # 息屏 / 锁屏 / 刚开机时它本来就是 null —— 流程第 1 步 wake 就是为了
    # 解决这个。这里只确认"能读到窗口信息"（dumpsys 有输出），
    # 说明 window 服务真的在工作。
    try:
        r = subprocess.run(
            ["adb", "-s", serial, "shell", "dumpsys window"],
            capture_output=True, text=True, timeout=timeout)
        out = r.stdout or ""
    except Exception:
        return False, "读窗口信息超时"
    if "mCurrentFocus" not in out:
        return False, "window 服务还没就绪（dumpsys 无焦点字段）"
    return True, "OK"


def wait_system_ready(serial, max_wait=180, settle=0):
    """等系统真能用，再稳一会儿。

    settle 秒是"稳定期"：服务注册完到 UI 真正响应之间还有一段，
    开机后马上点会撞在动画/首屏加载上。
    """
    import time as _t
    t0 = _t.monotonic()
    last = 0.0
    while _t.monotonic() - t0 < max_wait:
        ok, why = system_ready(serial)
        if ok:
            el = _t.monotonic() - t0
            if settle > 0:
                _t.sleep(settle)
            return True, "系统就绪（等了 %.0fs，另稳定 %.0fs）" % (el, settle)
        el = _t.monotonic() - t0
        if el - last >= 5:
            print("  等系统就绪… %.0fs（%s）" % (el, why))
            last = el
        _t.sleep(1.5)
    return False, "等系统就绪超时（%.0fs）" % max_wait


def candidates():
    """所有在线设备，无线排前面。"""
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True,
                             text=True, timeout=15).stdout or ""
    except Exception:
        return []
    wifi, usb = [], []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device":
            continue
        (wifi if ":" in parts[0] else usb).append(parts[0])
    return wifi + usb


def pick_serial():
    """挑一台在线的设备，优先无线。

    为什么优先无线：拔插 USB 验证「插电自动执行」时，控制通道如果是 USB，
    线一拔 adb 就 device not found，恰好把要看的那一刻弄丢了。
    无线 adb 不受插拔影响。
    """
    forced = os.environ.get("AIR_SERIAL")
    if forced:
        return forced
    for c in candidates():
        if alive(c):
            return c
    # 都探不通：返回第一个候选，让上层报错而不是在这里假装成功
    return (candidates() or [os.environ.get("PHONE_SERIAL", "892e20c9")])[0]


DEFAULT_SERIAL = pick_serial()


def ensure_wifi():
    """无线掉了就用 USB 把它拉回来。

    控制通道走无线（拔线不断），代价是 WiFi 一断就全瞎。USB 在这时还在，
    所以能救：svc wifi enable + adb connect。
    这就是"两条腿"的价值——不是冗余摆设，是互相兜底。
    """
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True,
                             text=True, timeout=15).stdout or ""
        if any(":" in l.split()[0] for l in out.splitlines() if len(l.split()) >= 2):
            return True                      # 无线已在
        usb = [l.split()[0] for l in out.splitlines()
               if len(l.split()) >= 2 and l.split()[1] == "device"
               and ":" not in l.split()[0]]
        if not usb:
            return False                     # 两条都没有，救不了
        dev = usb[0]
        subprocess.run(["adb", "-s", dev, "shell", "svc wifi enable"],
                       capture_output=True, text=True, timeout=25)
        for _ in range(12):
            time.sleep(2)
            ip = subprocess.run(
                ["adb", "-s", dev, "shell",
                 "ip -4 addr show wlan0 | grep -o 'inet [0-9.]*' | cut -d' ' -f2"],
                capture_output=True, text=True, timeout=15).stdout.strip()
            if not ip:
                continue
            subprocess.run(["adb", "connect", ip + ":5555"],
                           capture_output=True, text=True, timeout=20)
            out2 = subprocess.run(["adb", "devices"], capture_output=True,
                                  text=True, timeout=15).stdout or ""
            if any(ip in l and "device" in l for l in out2.splitlines()):
                return True
        return False
    except Exception:
        return False
ROOT = os.path.dirname(os.path.abspath(__file__))
SHOT_DIR = os.path.join(ROOT, "shots")

# 手机上手动推过 minicap/minitouch 后，用它们可以快 6~50 倍。
# 设 AIR_FAST=0 可退回纯 adb 模式。
FAST = os.environ.get("AIR_FAST", "1") not in ("0", "false", "no")
CAP = "MINICAP" if FAST else "ADBCAP"
TOUCH = "MINITOUCH" if FAST else "ADBTOUCH"


class Phone(object):
    def __init__(self, serial=None, threshold=0.75, shot_dir=None, verbose=False):
        self._forced = serial
        self.serial = serial or pick_serial()
        self.threshold = threshold
        self.shot_dir = shot_dir or SHOT_DIR
        os.makedirs(self.shot_dir, exist_ok=True)
        self._dev = self._connect()
        if verbose:
            logging.getLogger("airtest").setLevel(logging.DEBUG)
        self.info()  # 连上就打印一次设备信息，便于确认连的是哪台

    def _connect(self):
        return connect_device(
            "Android:///%s?cap_method=%s&touch_method=%s" % (self.serial, CAP, TOUCH))

    def reconnect(self):
        """换一条还在的通道重连。

        无线优先，断了就用 USB 救；USB 也没了才真算失败。
        不这么做的话，WiFi 掉一次服务就永久废了——这次实测就踩到了。
        """
        if self._forced:
            return self._connect()      # 用户指定了，不擅自换
        # 无线不在就用 USB 把它救回来；救不回来就退回 USB
        # （拔线会断，但总比完全连不上强）
        ensure_wifi()
        new = pick_serial()
        if new != self.serial:
            print("控制通道切换：%s -> %s" % (self.serial, new))
        self.serial = new
        self._dev = self._connect()
        return self._dev

    # ---------- 基础信息 ----------
    def info(self):
        model = self._dev.shell("getprop ro.product.model").strip()
        rel = self._dev.shell("getprop ro.build.version.release").strip()
        print("设备 %s  model=%s  Android %s  %sx%s" % (
            self.serial, model, rel, *self._dev.get_current_resolution()))
        return model

    def resolution(self):
        return self._dev.get_current_resolution()

    # ---------- 截图 / 找图 ----------
    def _wait_device_back(self, max_wait=20):
        """等 adb 重新看到这台设备。

        USB 掉线后重连不是瞬间的事：内核要重新枚举，adbd 要重新起。
        直接 reconnect() 只会拿到 device not ready。
        """
        t0 = time.monotonic()
        while time.monotonic() - t0 < max_wait:
            if alive(self.serial, timeout=4):
                return True
            time.sleep(2)
        return False

    def _raw_screencap(self, timeout=25):
        """自己发 screencap，而不是走 Airtest 的 snapshot()。

        为什么不用 self._dev.snapshot()：它内部是 Popen(...).communicate()，
        **没有超时**。USB 一断，管道直接裂，抛 BrokenPipeError，
        而且那条死连接会被反复复用。这里自己控制超时，断了好重连。
        """
        r = subprocess.run(
            ["adb", "-s", self.serial, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=timeout)
        if r.returncode != 0 or not r.stdout:
            err = (r.stderr or b"").decode("utf-8", "replace")[:200]
            raise IOError("截图失败：%s" % (err or "空输出"))
        buf = np.frombuffer(r.stdout, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            raise IOError("截图解码失败（%d 字节）" % len(r.stdout))
        return img

    def snap(self, name=None, retries=2):
        """截当前屏，返回图像数组；给了 name 就存到 shots/ 下并返回路径。

        设备断了就重连再截。手机上 adbd 可能中途重启（USB 供电不足、
        系统重启都会），不重连只会一直撞在死连接上。
        """
        last = None
        for attempt in range(max(1, retries)):
            try:
                img = self._raw_screencap()
                if name:
                    path = name if os.path.isabs(name) else os.path.join(self.shot_dir, name)
                    cv2.imwrite(path, img)
                    return path
                return img
            except Exception as e:
                last = e
                if attempt + 1 >= retries:
                    break
                # 断连类错误才重连；解码错误重试也没意义，但也无妨
                time.sleep(0.6)
                self._wait_device_back()
                try:
                    self.reconnect()
                except Exception:
                    pass
        raise last

    shot = snap

    def _resolve(self, img):
        if os.path.isabs(img) or os.path.exists(img):
            return img
        cand = os.path.join(self.shot_dir, img)
        if os.path.exists(cand):
            return cand
        raise IOError("找不到图片: %s（也没在 %s 里）" % (img, self.shot_dir))

    def find(self, img, threshold=None, timeout=0.0):
        """找图，返回 (x, y) 或 None。timeout>0 时反复截屏直到找到或超时。"""
        tpl = imread(self._resolve(img))
        th = self.threshold if threshold is None else threshold
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            hit = find_template(self.snap(), tpl, threshold=th)
            if hit:
                return hit["result"]
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.5)

    def exists(self, img, threshold=None):
        return self.find(img, threshold) is not None

    def wait(self, img, timeout=10.0, threshold=None):
        pt = self.find(img, threshold, timeout=timeout)
        if not pt:
            raise AssertionError("%.0f 秒内没等到 %s" % (timeout, img))
        return pt

    def assert_image(self, img, timeout=5.0, msg=None):
        pt = self.find(img, timeout=timeout)
        assert pt, msg or "屏幕上看不到 %s" % img
        return pt

    def assert_no_image(self, img, msg=None):
        assert not self.exists(img), msg or "屏幕上不该出现 %s" % img

    # ---------- 操作 ----------
    def tap(self, x, y=None, image=None, threshold=None, timeout=0.0):
        """tap(540, 1200) 或 tap(image="btn.png")。"""
        if image:
            pt = self.find(image, threshold, timeout)
            assert pt, "屏幕上找不到 %s" % image
            self._dev.touch(pt)
            return pt
        self._dev.touch((x, y))
        return (x, y)

    def tap_image(self, img, **kw):
        return self.tap(None, None, image=img, **kw)

    def double_tap(self, x, y):
        self._dev.double_click((x, y))

    def long_tap(self, x, y, duration=1.0):
        self._dev.long_click((x, y), duration=duration)

    def swipe(self, direction_or_p1, p2=None, duration=0.3, scale=0.3):
        """swipe("up") 或 swipe((x1,y1), (x2,y2))。"""
        w, h = self.resolution()
        if isinstance(direction_or_p1, str):
            d = direction_or_p1.lower()
            cx, cy = int(w * 0.5), int(h * 0.5)
            if d == "up":
                p1, p2 = (cx, int(h * (0.5 + scale))), (cx, int(h * (0.5 - scale)))
            elif d == "down":
                p1, p2 = (cx, int(h * (0.5 - scale))), (cx, int(h * (0.5 + scale)))
            elif d == "left":
                p1, p2 = (int(w * (0.5 + scale)), cy), (int(w * (0.5 - scale)), cy)
            elif d == "right":
                p1, p2 = (int(w * (0.5 - scale)), cy), (int(w * (0.5 + scale)), cy)
            else:
                raise ValueError("方向只能是 up/down/left/right")
        else:
            p1 = direction_or_p1
        self._dev.swipe(p1, p2, duration=duration)

    def text(self, s):
        self._dev.text(s)

    def key(self, name):
        codes = {
            "home": "KEYCODE_HOME", "back": "KEYCODE_BACK", "enter": "KEYCODE_ENTER",
            "power": "KEYCODE_POWER", "wakeup": "KEYCODE_WAKEUP", "sleep": "KEYCODE_SLEEP",
            "delete": "KEYCODE_DEL", "tab": "KEYCODE_TAB", "menu": "KEYCODE_MENU",
        }
        self._dev.keyevent(codes.get(name, name))

    def home(self):
        self.key("home")

    def back(self):
        self.key("back")

    def open(self, pkg, wait=1.5):
        out = self._dev.shell(
            "monkey -p %s -c android.intent.category.LAUNCHER 1" % pkg)
        assert "No activities found" not in out, "这个包名没有可启动界面: %s" % pkg
        time.sleep(wait)
        return self.current_app()

    def current_app(self):
        out = self._dev.shell("dumpsys window | grep mCurrentFocus")
        return out.strip()

    def assert_app(self, pkg, msg=None):
        cur = self.current_app()
        assert pkg in cur, msg or "当前界面不是 %s，而是 %s" % (pkg, cur)

    def shell(self, cmd, timeout=25):
        """跑一条 shell 命令，超时就报错。

        不能直接用 airtest 的 _dev.shell：WiFi 掉了以后 adb 的 TCP 连接
        会变成"半死"——adb devices 还显示 device，但命令永远不返回。
        之前的代码就是这样把服务卡死的（/power 挂住，拔插检测全废）。
        所以走 subprocess 自己控超时，卡住就抛错，让上层换通道重连。
        """
        try:
            r = subprocess.run(
                ["adb", "-s", self.serial, "shell", cmd],
                capture_output=True, text=True, timeout=timeout)
            return r.stdout or ""
        except subprocess.TimeoutExpired:
            raise RuntimeError("设备无响应（%s 超 %ss）：连接可能已断" % (self.serial, timeout))

    def sleep(self, s=1.0):
        time.sleep(s)


def phone(serial=None, **kw):
    return Phone(serial, **kw)


# 让 .air 脚本写得最短：from airlib import *  然后 p = phone()
__all__ = ["phone", "Phone"]
