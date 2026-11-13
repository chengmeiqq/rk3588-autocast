#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 Tasker 可导入的任务文件（.tsk.xml）。

Tasker 6.6.3 / versionCode 5428 的导出格式：
  <TaskerData><Task sr="taskN"><Action sr="actN"><code/><Str sr="argN"/></Action></Task>
HTTP Request 动作 code=339，参数顺序：
  arg0 Method / arg1 URL / arg2 Headers / arg3 Query / arg4 OutputFile
  arg5 Timeout / arg6 TrustCert / arg7 FollowRedirect / arg8 UseCookies
  arg9 StructureOutput / arg10 Body
Flash 动作 code=548，arg0 文本。
"""
import os
import time
import xml.etree.ElementTree as ET

BOARD = os.environ.get("BOARD_IP", "192.168.1.83")
PORT = os.environ.get("BOARD_PORT", "8130")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasker")
NOW = int(time.time() * 1000)

# 名字, 路径, 超时, 说明
STEPS = [
    ("豆包-1唤醒",   "/step/wake",    30),
    ("豆包-2关USB",  "/step/popup",   30),
    ("豆包-3重启",   "/step/restart", 60),
    ("豆包-4确认首页", "/step/home_ok", 30),
    ("豆包-5打电话", "/step/tap_call", 90),
    ("豆包-6开投屏", "/step/tap_cast", 90),
    ("豆包-7验证",   "/step/verify",   90),
    ("豆包-8回桌面", "/step/desktop",  30),
    ("豆包-状态",    "/step/status",   20),
]

# 一键任务：Tasker 只发这一个请求，剩下全在板子上跑完。
#
# 为什么比 8 个分步可靠：
#   分步的问题不在 Tasker，而在"步骤之间"——第 5 步失败时
#   Tasker 未必真的停下来（Flash 不是阻断），于是带着半错的
#   状态继续发第 6 步，最后"全部执行完毕"但屏幕是黑的。
#
#   /cast 在板子内部按顺序跑、每步自带重试和校验、任一关键步
#   失败就整体返回 ok:false。Tasker 只需要看一个布尔值。
ONE_SHOT = ("豆包-一键投屏", "/cast", 240)


def http_action(act, url, timeout):
    """HTTP Request，同一动作里顺手把结果写进 %http_data。"""
    a = ET.SubElement(act, "Action", {"sr": "act0", "ve": "7"})
    ET.SubElement(a, "code").text = "339"
    ET.SubElement(a, "Str", {"sr": "arg0", "ve": "3"}).text = "POST"
    ET.SubElement(a, "Str", {"sr": "arg1", "ve": "3"}).text = url
    ET.SubElement(a, "Str", {"sr": "arg2", "ve": "3"}).text = ""
    ET.SubElement(a, "Str", {"sr": "arg3", "ve": "3"}).text = ""
    ET.SubElement(a, "Str", {"sr": "arg4", "ve": "3"}).text = ""
    ET.SubElement(a, "Int", {"sr": "arg5", "ve": "3"}).text = str(timeout)
    ET.SubElement(a, "Int", {"sr": "arg6", "ve": "3"}).text = "0"
    ET.SubElement(a, "Int", {"sr": "arg7", "ve": "3"}).text = "0"
    ET.SubElement(a, "Int", {"sr": "arg8", "ve": "3"}).text = "0"
    ET.SubElement(a, "Int", {"sr": "arg9", "ve": "3"}).text = "0"
    ET.SubElement(a, "Str", {"sr": "arg10", "ve": "3"}).text = ""
    return a


def flash_action(act, n, text):
    a = ET.SubElement(act, "Action", {"sr": "act%d" % n, "ve": "7"})
    ET.SubElement(a, "code").text = "548"
    ET.SubElement(a, "Str", {"sr": "arg0", "ve": "3"}).text = text
    ET.SubElement(a, "Int", {"sr": "arg1", "ve": "3"}).text = "0"
    return a


def build():
    os.makedirs(OUT, exist_ok=True)
    made = []

    # ── 一键任务放第一个（t00），方便找 ──
    name, path, timeout = ONE_SHOT
    root = ET.Element("TaskerData", {"sr": "", "dvi": "1", "tv": "6.6.3"})
    task = ET.SubElement(root, "Task", {"sr": "task0"})
    ET.SubElement(task, "cdate").text = str(NOW)
    ET.SubElement(task, "edate").text = str(NOW)
    ET.SubElement(task, "id").text = "0"
    ET.SubElement(task, "nme").text = name
    ET.SubElement(task, "pri").text = "100"
    http_action(task, "http://%s:%s%s" % (BOARD, PORT, path), timeout)
    flash_action(task, 1, "%%http_response_code %%http_data")
    fn = os.path.join(OUT, "t00_cast_all.tsk.xml")
    ET.ElementTree(root).write(fn, encoding="utf-8", xml_declaration=True)
    made.append((fn, name, path))

    for i, (name, path, timeout) in enumerate(STEPS, start=1):
        root = ET.Element("TaskerData", {"sr": "", "dvi": "1", "tv": "6.6.3"})
        task = ET.SubElement(root, "Task", {"sr": "task%d" % i})
        ET.SubElement(task, "cdate").text = str(NOW)
        ET.SubElement(task, "edate").text = str(NOW)
        ET.SubElement(task, "id").text = str(i)
        ET.SubElement(task, "nme").text = name
        ET.SubElement(task, "pri").text = "100"

        http_action(task, "http://%s:%s%s" % (BOARD, PORT, path), timeout)
        flash_action(task, 1, "%%http_response_code %%http_data")

        fn = os.path.join(OUT, "t%02d_%s.tsk.xml" % (i, path.strip("/").replace("/", "_")))
        ET.ElementTree(root).write(fn, encoding="utf-8", xml_declaration=True)
        made.append((fn, name, path))
    return made


if __name__ == "__main__":
    files = build()
    for fn, name, path in files:
        ET.parse(fn)          # 自检：能解析才算合格
        print("%-46s %-14s %s" % (os.path.basename(fn), name, path))
    print("\n共 %d 个文件 → %s" % (len(files), OUT))