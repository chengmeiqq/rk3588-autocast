#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用安卓无障碍树点按钮，不用截图拼图。

为什么换掉模板匹配：
  模板匹配是"拿一张旧截图去新截图里找相似区块"。它有三个天生缺陷——
  换个主题、改个字号、深色模式、分辨率变一点点，就找不到了；
  而且它只能告诉你"某个图案在这"，说不出"这是什么按钮"。

  uiautomator dump 是安卓系统自己把当前界面每个控件报给你：
  文字、无障碍描述、可点击性、精确像素边界。系统报的，不会看错。

实测（豆包通话页）拿到的就是这些：
  content-desc="共享屏幕"      bounds=[308,2012][518,2222]  → 中心 (413,2117)
  content-desc="挂断通话"      bounds=[816,2012][1026,2222]
  content-desc="视频通话"      bounds=[562,2012][772,2222]
  content-desc="双击静音，关闭麦克风" bounds=[54,2012][264,2222]

  通话页根本没有"打电话"这个按钮——它是首页的。所以按文字找，
  永远不会出现"点错了位置"：**找不到就不点**。

什么时候还要靠模型：
  无障碍树里确实没有的元素（自绘 canvas、游戏、某些 WebView）。
  这时才截图问视觉模型，而且模型给的坐标必须落在某个真实
  可点击节点的边界内才允许点。这是硬约束，不是建议。
"""
import os
import re
import subprocess
import time

ADB = "/usr/lib/android-sdk/platform-tools/adb"
USB_SER = os.environ.get("PHONE_SERIAL", "892e20c9")
WIFI_ADDR = os.environ.get("PHONE_WIFI", "192.168.1.119:5555")


def pick_serial():
    """USB 优先，断了走 WiFi。两个都不通就返回 USB 号，让上层报错。"""
    try:
        out = subprocess.run([ADB, "devices"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=10
                             ).stdout.decode("utf-8", "replace")
        states = {}
        for line in out.splitlines():
            p = line.split()
            if len(p) == 2 and p[1] in ("device", "offline"):
                states[p[0]] = p[1]
    except Exception:
        return USB_SER
    if states.get(USB_SER) == "device":
        return USB_SER
    if states.get(WIFI_ADDR) == "device":
        return WIFI_ADDR
    return USB_SER


SER = USB_SER      # 兼容旧引用；实际请用 pick_serial()


def _adb(serial, *args, timeout=25):
    try:
        p = subprocess.run([ADB, "-s", serial] + list(args),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout)
        return p.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


NODE_RE = re.compile(r"<node\b[^>]*>")
ATTR_RE = re.compile(r'(\w[\w-]*)="([^"]*)"')
BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def dump(serial=None, retries=3):
    """取无障碍树。返回节点列表，每个是 dict。

    为什么重试：界面切换动画期间 dump 会拿到空树或上一帧。
    拿到空树不代表界面上没东西，只代表这一瞬间没取到。
    """
    serial = serial or pick_serial()
    for i in range(retries):
        _adb(serial, "shell", "rm -f /sdcard/_ui.xml")
        _adb(serial, "shell",
             "uiautomator dump /sdcard/_ui.xml >/dev/null 2>&1")
        xml = _adb(serial, "shell", "cat /sdcard/_ui.xml")
        if "<hierarchy" not in xml:
            time.sleep(1.0)
            continue
        nodes = []
        for m in NODE_RE.finditer(xml):
            s = m.group(0)
            a = dict(ATTR_RE.findall(s))
            b = BOUNDS_RE.search(a.get("bounds", ""))
            if not b:
                continue
            x1, y1, x2, y2 = (int(v) for v in b.groups())
            if x2 <= x1 or y2 <= y1:
                continue
            nodes.append({
                "text": a.get("text", ""),
                "desc": a.get("content-desc", ""),
                "rid": a.get("resource-id", ""),
                "cls": (a.get("class", "") or "").split(".")[-1],
                "pkg": a.get("package", ""),
                "clickable": a.get("clickable") == "true",
                "enabled": a.get("enabled") == "true",
                "checked": a.get("checked") == "true",
                "bounds": (x1, y1, x2, y2),
                "cx": (x1 + x2) // 2,
                "cy": (y1 + y2) // 2,
                "area": (x2 - x1) * (y2 - y1),
            })
        if nodes:
            return nodes
        time.sleep(1.0)
    return []


def find(nodes, *words, exact=False, clickable_only=True, rid=None,
         allow_input=False):
    """按文字或无障碍描述找节点。多个词是"都要包含"。

    两个坑，都踩过：
      1. 输入框里的**内容**也会出现在 text 里。豆包首页输入框残留
         「打电话」三个字，于是"找「打电话」按钮"精确命中了输入框，
         一点就把光标点进去，页面卡在编辑态 —— 投屏永远起不来。
         所以默认跳过 EditText 一类可输入控件。
      2. 真正的按钮常常自己没字，文字挂在它不可点的子节点上。
         这时按 resource-id 找父容器才准。

    rid 给了就只认 rid（子串匹配），不再看文字。
    """
    EDITABLE = ("EditText", "AutoCompleteTextView",
                "MultiAutoCompleteTextView", "SearchView")
    hits = []
    for n in nodes:
        if rid:
            if rid not in n.get("rid", ""):
                continue
            if not n["enabled"]:
                continue
            # 按 id 找人常常要给不可点的外层容器，所以这里不强制 clickable
            hits.append(n)
            continue
        if clickable_only and not n["clickable"]:
            continue
        if not n["enabled"]:
            continue
        if not allow_input and n["cls"] in EDITABLE:
            continue
        label = (n["text"] + " " + n["desc"]).strip()
        if not label:
            continue
        if exact:
            if any(label == w for w in words):
                hits.append(n)
        else:
            if all(w in label for w in words):
                hits.append(n)
    # 可点的优先（按 id 找时会捞到不可点的子节点），再看面积：
    # 面积小的更像"按钮"，大的往往是整页容器
    hits.sort(key=lambda n: (0 if n["clickable"] else 1, n["area"]))
    return hits


def tap_node(serial, n):
    _adb(serial, "shell", "input tap %d %d" % (n["cx"], n["cy"]))
    return n["cx"], n["cy"]


def click(serial, *words, exact=False, wait=1.2, tap_times=1,
          clickable_only=True):
    """找到就点，找不到返回 None。绝不"猜个大概位置点下去"。

    clickable_only=False 用于「标签本身不可点、但父容器可点」的界面：
    例如插线的「选择 USB 用途」，三个选项的文字节点 clickable=false，
    真正接收点击的是底下那层容器。点文字中心坐标仍然有效，
    因为容器覆盖着它。实测「传输文件」@(538,1940) 就这么点中的。
    """
    serial = serial or pick_serial()
    nodes = dump(serial)
    hits = find(nodes, *words, exact=exact, clickable_only=clickable_only)
    if not hits:
        return None
    n = hits[0]
    for _ in range(tap_times):
        tap_node(serial, n)
        time.sleep(wait)
    return n


def foreground(serial=None):
    serial = serial or pick_serial()
    out = _adb(serial, "shell", "dumpsys window | grep mCurrentFocus")
    for line in out.splitlines():
        if "mCurrentFocus" in line and "}" in line:
            return line.split("u0")[-1].strip().rstrip("}")
    return ""


def names(nodes, clickable_only=True):
    out = []
    for n in nodes:
        if clickable_only and not n["clickable"]:
            continue
        lb = (n["text"] or n["desc"]).strip()
        if lb:
            out.append(lb)
    return out


if __name__ == "__main__":
    import sys
    s = sys.argv[1] if len(sys.argv) > 1 else SER
    print("前台:", foreground(s))
    ns = dump(s)
    print("节点数:", len(ns))
    print("可点项:")
    for lb in names(ns):
        print("  ", lb)
