#!/data/data/com.termux/files/usr/bin/python
# -*- coding: utf-8 -*-
"""find_ui.py —— 在 uiautomator 的 XML 里按「完全相等」找控件，返回可点的中心坐标。

为什么不用 grep：
  「正在共享屏幕」这个标题的 text 里含「共享屏幕」四个字。用 grep -F 子串匹配，
  第一个命中的是标题（y≈804），不是底部真正的按钮（y≈2117）。点下去毫无反应，
  日志还会写「已点共享屏幕」，比报错更难查。这个脚本只认完全相等的 text 或
  content-desc，并且优先挑 clickable=true 的节点。

用法：
  find_ui.py <xml> <文字> [--desc]      # 默认两者都匹配，clickable 优先
  find_ui.py <xml> --list               # 列出全部可点节点，便于排查
退出码：0 找到（stdout: "x y"），1 没找到
"""
import re
import sys
import xml.etree.ElementTree as ET

BOUNDS = re.compile(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]')


def center(bounds):
    m = BOUNDS.search(bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def walk(node, out):
    t = (node.get("text") or "").strip()
    d = (node.get("content-desc") or "").strip()
    if t or d:
        out.append({
            "text": t,
            "desc": d,
            "clickable": node.get("clickable") == "true",
            "enabled": node.get("enabled") == "true",
            "bounds": node.get("bounds"),
            "cls": node.get("class") or "",
        })
    for child in node:
        walk(child, out)


def main():
    if len(sys.argv) < 3:
        print("usage: find_ui.py <xml> <label> | <xml> --list", file=sys.stderr)
        return 2
    path, want = sys.argv[1], sys.argv[2]

    try:
        root = ET.parse(path).getroot()
    except Exception as e:
        print("parse error: %s" % e, file=sys.stderr)
        return 2

    nodes = []
    walk(root, nodes)

    if want == "--list":
        for n in nodes:
            c = center(n["bounds"])
            label = n["text"] or n["desc"]
            print("%-24s %-10s clickable=%-5s %s" % (
                label[:24], "%d,%d" % c if c else "?", n["clickable"], n["cls"]))
        return 0

    # 完全相等才算命中 —— 这是本脚本存在的唯一理由
    hits = [n for n in nodes if n["text"] == want or n["desc"] == want]
    if not hits:
        # 退化：允许「按钮类容器」的 content-desc 完全相等（已在上面覆盖）
        print("not found: %r (nodes with label containing it: %r)" % (
            want, [n["text"] or n["desc"] for n in nodes if want in (n["text"] or n["desc"])][:6]),
            file=sys.stderr)
        return 1

    # 优先可点且启用的；同等条件下 desc 命中优先（图标按钮常只有 desc）
    def rank(n):
        return (0 if (n["clickable"] and n["enabled"]) else 1,
                0 if n["desc"] == want else 1)

    hits.sort(key=rank)
    best = hits[0]
    c = center(best["bounds"])
    if not c:
        print("no bounds for %r" % want, file=sys.stderr)
        return 1
    print("%d %d" % c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
