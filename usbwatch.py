#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盯板子自己的 USB 口，手机插上/拔下就触发。

为什么不用 adb 轮询：
  1. 慢。轮询一秒一次，插拔那一瞬间可能错过；udev 是内核直接通知，毫秒级。
  2. 脆。走 adb 就得先有可用连接。WiFi 一断、USB 正在枚举，全查不到。
     这次实测就踩过：手机 WiFi 被关，`/power` 全 500，拔插检测彻底失效。
  3. 准。板子看到的是"这条线通没通"，不是手机自报的电池状态。

板子侧看手机：/sys/bus/usb/devices/*/  里 idVendor=18d1（Google/ADB），
再按 serial 匹配（默认 892e20c9）。拔了就整个目录消失，插上就出现。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
USB_ROOT = "/sys/bus/usb/devices"
SERIAL = os.environ.get("PHONE_SERIAL", "892e20c9")
VENDOR = "18d1"          # Google —— adb 接口的手机都报这个
DEBOUNCE = float(os.environ.get("USB_DEBOUNCE", "2.5"))
COOLDOWN = float(os.environ.get("USB_COOLDOWN", "12"))


def log(msg):
    t = time.strftime("%Y-%m-%d %H:%M:%S")
    line = "%s %s" % (t, msg)
    print(line, flush=True)


def find_phone():
    """手机现在插着吗。返回 sysfs 路径或 None。"""
    try:
        names = os.listdir(USB_ROOT)
    except OSError:
        return None
    for n in names:
        d = os.path.join(USB_ROOT, n)
        try:
            with open(os.path.join(d, "idVendor")) as f:
                if f.read().strip() != VENDOR:
                    continue
            sn = ""
            p = os.path.join(d, "serial")
            if os.path.exists(p):
                with open(p) as f:
                    sn = f.read().strip()
            # 有线 adb 的 serial 就是手机序列号；匹配不上也认（换线不影响）
            if SERIAL and sn and sn != SERIAL:
                continue
            return d
        except OSError:
            continue
    return None


def sh(serial, cmd, timeout=20):
    """对某台设备发一条 shell 命令，超时就返回空。"""
    try:
        r = subprocess.run(["adb", "-s", serial, "shell", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def any_device():
    """现在 adb 能看到哪台（USB 序列号或无线都行）。"""
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True,
                             text=True, timeout=15).stdout or ""
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            return parts[0]
    return None


def wait_android_boot(max_wait=240):
    """等手机真的开机完成，而不是"USB 刚插上"。

    用户的真实场景：手机每次都关机，给板子通电后手机靠 USB 供电自动开机。
    手机从关机到进桌面要几十秒，这期间 USB 设备早就枚举出来了
    （`/sys` 里已经有节点），但 Android 根本没起来 —— 这时候跑流程
    只会全盘失败。所以必须先等到 sys.boot_completed=1。
    """
    t0 = time.time()
    last_log = 0
    while time.time() - t0 < max_wait:
        dev = any_device()
        if dev:
            boot = sh(dev, "getprop sys.boot_completed", timeout=12)
            if boot.strip() == "1":
                el = time.time() - t0
                if el > 3:
                    log("手机已开机完成（等了 %.0fs）" % el)
                return True
        el = time.time() - t0
        if el - last_log >= 15:
            log("等手机开机… 已等 %.0fs%s" % (el, "（adb 还没看到设备）" if not dev else ""))
            last_log = el
        time.sleep(3)
    log("等开机超时（%.0fs），先试着跑流程" % max_wait)
    return False


def run_flow():
    """调 airweb 跑流程。"""
    url = os.environ.get("AIRWEB", "http://127.0.0.1:8123")
    flow = os.environ.get("FLOW", os.path.join(HERE, "flows", "通话投屏.json"))
    try:
        with open(flow) as f:
            steps = __import__("json").load(f).get("steps") or []
    except Exception as e:
        log("流程文件读不了: %s" % e)
        return False
    # 手机可能刚开机，等它真的起来再动手
    wait_android_boot()
    log(">>> 开始执行流程（%d 步）" % len(steps))
    try:
        out = subprocess.run(
            ["curl", "-s", "-m", "600", "-X", "POST", url + "/flow/run",
             "-H", "Content-Type: application/json",
             "-d", __import__("json").dumps(
                 {"steps": steps, "tries": 8, "interval": 0.8,
                  "retry": 3, "resume_rounds": 3}, ensure_ascii=False)],
            capture_output=True, text=True, timeout=620).stdout
        r = __import__("json").loads(out)
    except Exception as e:
        log("调用流程接口失败: %s" % e)
        return False
    acc = 0
    for rec in r.get("results", []):
        ok = rec.get("action_ok") and rec.get("verify_ok") is not False
        acc += int(rec.get("ms") or 0)
        # 这些行是流程跑完后一次性补打的，时间戳都一样。额外标出"到这一步
        # 累计用了多少秒"，才能看出时间花在哪、哪一步变慢了。
        log("  %s %s. %s (%sms, 累计%.1fs)" % (
            "OK " if ok else "FAIL", rec.get("i"), rec.get("desc"),
            rec.get("ms"), acc / 1000.0))
    for x in r.get("resume_log", []):
        log("  [恢复] 第%d步失败 → %s" % (x.get("failed_at"), x.get("did")))
    log("流程结束：%s  用时 %sms  %s/%s 步通过（轮次 %s）" % (
        "全部通过" if r.get("all_pass") else "有失败",
        r.get("ms"), r.get("done"), r.get("total"), r.get("rounds")))
    log(">>> 执行成功" if r.get("all_pass") else ">>> 执行失败")
    return bool(r.get("all_pass"))


def main():
    once = "--once" in sys.argv
    if once:
        log("手机%s" % ("已插入: " + str(find_phone()) if find_phone() else "未插入"))
        return 0
    log("盯 USB 口：vendor=%s serial=%s（防抖 %.1fs）" % (VENDOR, SERIAL, DEBOUNCE))
    log("等 airweb 就绪…")
    url = os.environ.get("AIRWEB", "http://127.0.0.1:8123")
    for _ in range(120):
        rc = subprocess.run(["curl", "-s", "-m", "2", "-o", "/dev/null", "-w",
                             "%{http_code}", url + "/power"],
                            capture_output=True, text=True).stdout.strip()
        if rc == "200":
            break
        time.sleep(1)
    else:
        log("airweb 一直没起来，先退出")
        return 1
    log("airweb 就绪")

    present = find_phone() is not None
    log("当前状态：%s" % ("已插着" if present else "没插"))
    last_run = 0.0

    # 板子开机时手机通常已经插着（用户给板子通电、手机靠 USB 自动开机）。
    # 这种情况下永远等不到"拔→插"的跳变，所以启动时插着就先跑一次。
    if present:
        log("启动时已插着，先跑一次")
        last_run = time.time()
        try:
            run_flow()
        except Exception as e:
            log("跑流程出错: %s" % e)

    while True:
        now = find_phone() is not None
        if now != present:
            # 防抖：等电平稳下来，手机枚举会抖几下
            time.sleep(DEBOUNCE)
            now2 = find_phone() is not None
            if now2 == now:
                present = now
                if now:
                    log("已插上 USB")
                    if time.time() - last_run < COOLDOWN:
                        log("还在冷却期（%.0fs），跳过" % COOLDOWN)
                    else:
                        last_run = time.time()
                        try:
                            run_flow()
                        except Exception as e:
                            log("跑流程出错: %s" % e)
                else:
                    log("已拔掉 USB")
        time.sleep(0.25)


if __name__ == "__main__":
    sys.exit(main())
