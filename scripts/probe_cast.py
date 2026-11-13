#!/usr/bin/env python3
"""探针：同时打印三种「投屏是否活着」的判据，用于选定守护判据。

用法: python3 scripts/probe_cast.py [serial]
"""
import subprocess
import sys

ADB = "/usr/lib/android-sdk/platform-tools/adb"
APP = "com.larus.nova"


def sh(serial, *args, timeout=25):
    try:
        r = subprocess.run([ADB, "-s", serial] + list(args),
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout or ""
    except Exception as e:
        return "__ERR__ %s" % e


def owner_virtual_display(serial):
    """现在的判据：dumpsys display 里有没有 ScreenCapture 虚拟屏。"""
    out = sh(serial, "shell", "dumpsys display | grep ScreenCapture")
    for line in out.splitlines():
        if "owner" in line:
            seg = line.split("owner", 1)[1].split()
            return seg[0] if seg else "?"
    return ""


def sc_service_foreground(serial):
    """判据 B：ScreenCaptureService 是否 isForeground=true。"""
    out = sh(serial, "shell", "dumpsys activity services " + APP)
    lines = out.splitlines()
    for i, line in enumerate(lines):
        # 只认 ServiceRecord 那一行，intent= 里也会出现同名类
        if line.lstrip().startswith("* ServiceRecord") and "ScreenCaptureService}" in line:
            # isForeground 在段内较靠后（实测第 8 行），扫到下一个 ServiceRecord 为止
            for nxt in lines[i + 1:i + 40]:
                if nxt.lstrip().startswith("* ServiceRecord"):
                    break
                if "isForeground=" in nxt:
                    return "true" if "isForeground=true" in nxt else "false"
            return "服务在但无 isForeground 字段"
    return "无 ScreenCaptureService"


def notif_count(serial):
    """判据 C：豆包投屏通知（ScreenCaptureChannel）条数。"""
    out = sh(serial, "shell",
             "dumpsys notification --noredact 2>/dev/null | "
             "grep -c 'pkg=%s.*ScreenCaptureChannel'" % APP)
    return (out.strip().splitlines() or ["?"])[-1]


def pid(serial):
    return sh(serial, "shell", "pidof " + APP).strip()


def main():
    serial = sys.argv[1] if len(sys.argv) > 1 else "892e20c9"
    print("serial            :", serial)
    print("豆包 pid          :", pid(serial) or "（无）")
    print("A 虚拟屏 owner    :", repr(owner_virtual_display(serial)))
    print("B 采集服务前台    :", sc_service_foreground(serial))
    print("C 投屏通知条数    :", notif_count(serial))


if __name__ == "__main__":
    main()
