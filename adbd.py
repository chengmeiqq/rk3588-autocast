#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""adb 连接守护：USB 优先，断了自动走 WiFi。

背景（为什么需要这个东西）：
  手机靠板子的 USB 口供电，插着就自动开机。但我们见过它"重新枚举"——
  dmesg 里是 `USB disconnect, device number 18` 然后 `new high-speed
  USB device number 19`，新设备是 `2717:ff40`（纯 MTP，1 个接口，
  class=06），**没有 adb 接口**。这时 `adb devices` 直接空，
  所有接口 500，投屏起不来。

  这跟软件无关，是 USB 层的物理/协议问题。我们不能修 USB，
  但可以：一旦发现 adb 没了，改走 WiFi（5555）继续干活。

  两条腿都有：
    - USB：快、稳，但会被重新枚举打掉
    - WiFi：慢一点，但不受 USB 枚举影响

策略：
  1. 每轮先看 usb 设备（892e20c9）在不在
  2. 不在就看 WiFi（192.168.1.119:5555），必要时 adb connect
  3. 两个都断 -> 记日志、继续等（不疯狂重试）

  为什么不用 systemd 的 Restart=always：这个进程本来就不该退出。
  它是个长期守着的循环，退出才需要重启。
"""
import os
import re
import subprocess
import time

ADB = "/usr/lib/android-sdk/platform-tools/adb"
USB_SER = os.environ.get("PHONE_SERIAL", "892e20c9")
WIFI_ADDR = os.environ.get("PHONE_WIFI", "192.168.1.119:5555")
WIFI_IP = WIFI_ADDR.split(":")[0]
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "logs", "adbd.log")
CHECK_EVERY = 20          # 秒
CONNECT_COOLDOWN = 60     # adb connect 失败后至少等这么久再试


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def adb(*args, timeout=15):
    try:
        p = subprocess.run([ADB] + list(args), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
        return p.stdout.decode("utf-8", "replace")
    except Exception as e:
        return "ERR %s" % e


def devices():
    """返回 {serial: state}"""
    out = adb("devices")
    d = {}
    for line in out.splitlines():
        m = re.match(r"^(\S+)\s+(\S+)$", line.strip())
        if m and not line.startswith("List of"):
            d[m.group(1)] = m.group(2)
    return d


def ping(serial):
    """真发一条命令，确认能通。比看 devices 状态准。"""
    out = adb("-s", serial, "shell", "echo _ALIVE_", timeout=8)
    return "_ALIVE_" in out


def port_open(ip, port=5555, timeout=3):
    import socket
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def main():
    log("守护启动：USB=%s  WiFi=%s  检查间隔=%ds"
        % (USB_SER, WIFI_ADDR, CHECK_EVERY))
    last_connect = 0.0
    prev = None

    while True:
        try:
            d = devices()
            usb_ok = d.get(USB_SER) == "device" and ping(USB_SER)
            wifi_ok = (d.get(WIFI_ADDR) == "device" and ping(WIFI_ADDR))

            state = "usb" if usb_ok else ("wifi" if wifi_ok else "none")

            if state != prev:
                if state == "usb":
                    log("连接正常：走 USB（%s）" % USB_SER)
                elif state == "wifi":
                    log("USB 不可用，改走 WiFi（%s）" % WIFI_ADDR)
                else:
                    log("两条腿都断了（adb devices: %s）" % (d or "空"))
                prev = state

            # USB 没了 -> 试着拉 WiFi 上来
            if not wifi_ok and not usb_ok:
                now = time.time()
                if now - last_connect > CONNECT_COOLDOWN:
                    last_connect = now
                    if port_open(WIFI_IP):
                        r = adb("connect", WIFI_ADDR)
                        log("尝试 WiFi 重连：%s" % r.strip()[:80])
                    else:
                        log("5555 端口不通（手机可能没开无线调试或断网）")

            # adb server 自己挂了的兜底
            if not d:
                adb("start-server", timeout=20)

        except Exception as e:
            log("循环出错（继续跑）：%s" % e)

        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
