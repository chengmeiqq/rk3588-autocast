#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按手机音量键触发流程。

为什么用音量键：不用装 App、不用无障碍、不用 root，adbd 就能读到按键事件。
按「音量减」两下（1.5 秒内）跑流程；按「音量加」两下 = 跑一次 dry（只看步骤）。

用法：
  ./.venv/bin/python keytrigger.py                 # 默认 flows/通话投屏.json
  ./.venv/bin/python keytrigger.py flows/xx.json   # 指定流程
  ./.venv/bin/python keytrigger.py --key 加          # 改用音量加
  ./.venv/bin/python keytrigger.py --once          # 只跑一次流程就退出（自测）

日志写 logs/keytrigger.log。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(ROOT, "logs", "keytrigger.log")
BASE = os.environ.get("AIRWEB", "http://127.0.0.1:8123")

# getevent 里的键码
DOWN = {114: "VOLUMEDOWN", 115: "VOLUMEUP"}  # 114=音量减 115=音量加
DOUBLE_WINDOW = 1.5   # 两下间隔上限
COOLDOWN = 8.0        # 一次触发后的冷却，防止连按重复跑


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def api(path, data=None, timeout=300):
    url = BASE + path
    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_server(timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            api("/power", timeout=5)
            return True
        except Exception:
            time.sleep(2)
    return False


def load_flow(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_flow(path, dry=False):
    flow = load_flow(path)
    steps = flow.get("steps") or []
    name = flow.get("name") or os.path.basename(path)
    if dry:
        log("  [dry] 流程「%s」%d 步" % (name, len(steps)))
        for i, s in enumerate(steps, 1):
            log("  [dry] %d. %s %s" % (i, s.get("do"),
                                       s.get("name") or s.get("text") or s.get("pkg") or ""))
        return True
    log("开始跑流程「%s」，%d 步" % (name, len(steps)))
    r = api("/flow/run", {"steps": steps, "stop_on_fail": True,
                          "tries": 6, "interval": 0.8, "retry": 3})
    for step in r.get("results", []):
        mark = "OK  " if (step.get("action_ok") and step.get("verify_ok") is not False) else "FAIL"
        log("  %s %d. %s (%dms)" % (mark, step["i"], step.get("desc", ""), step.get("ms", 0)))
        for c in ((step.get("verify") or {}).get("checks") or []):
            log("       %s %s 期望=%s 实际=%s" % (
                "v" if c.get("pass") else "x", c.get("kind"),
                c.get("expect"), c.get("at") if c.get("at") is not None else c.get("actual")))
    ok = bool(r.get("all_pass"))
    log("流程结束：%s  用时 %dms  %d/%d 步通过" % (
        "全部通过" if ok else "有失败", r.get("ms", 0), r.get("done", 0), r.get("total", 0)))
    return ok


def device_serial():
    """优先用 USB 直连的那台（按键事件只有在 USB 上才稳）。"""
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return None
    usb, net = None, None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device":
            continue
        if ":" in parts[0]:
            net = net or parts[0]
        else:
            usb = usb or parts[0]
    return usb or net


def listen(serial, want=DOWN, once=False, flow=None):
    """读 getevent，两次同向按键触发。"""
    cmd = ["adb", "-s", serial, "shell", "getevent", "-l"]
    log("监听音量键：%s（两下 %.1fs 内触发；%s）" % (
        "音量减" if want is DOWN else "音量加", DOUBLE_WINDOW,
        "只跑一次" if once else "Ctrl-C 退出"))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    presses = []
    last_run = 0.0
    try:
        for line in proc.stdout:
            line = line.strip()
            # getevent -l 输出形如：/dev/input/event1: EV_KEY  KEY_VOLUMEDOWN  DOWN
            if "EV_KEY" not in line:
                continue
            if not line.endswith("DOWN"):
                continue
            code = None
            for k in want:
                if want[k] in line:
                    code = k
                    break
            if code is None:
                continue
            now = time.time()
            if now - last_run < COOLDOWN:
                log("冷却中，忽略这次按键")
                presses = []
                continue
            while presses and now - presses[0] > DOUBLE_WINDOW:
                presses.pop(0)
            presses.append(now)
            if len(presses) < 2:
                log("听到一下 %s，再按一下就会触发" % want[code])
                continue
            presses = []
            log(">>> 触发：%s" % (flow or "默认流程"))
            try:
                run_flow(flow, dry=(want is not DOWN))
            except Exception as e:
                log("跑流程出错：%s: %s" % (type(e).__name__, e))
            last_run = time.time()
            if once:
                log("--once 已跑完，退出")
                return
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


def main():
    args = sys.argv[1:]
    flow = os.path.join(ROOT, "flows", "通话投屏.json")
    want = DOWN
    once = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--once":
            once = True
        elif a == "--key":
            i += 1
            if i < len(args) and args[i] in ("加", "volumeup", "up"):
                want = DOWN  # 占位，下面换成 UP
                want = {115: "VOLUMEUP"}
        elif a.startswith("-"):
            pass
        else:
            flow = a if os.path.isabs(a) else os.path.join(ROOT, a)
        i += 1

    if not os.path.exists(flow):
        log("流程文件不存在: %s" % flow)
        return 2
    log("===== keytrigger 启动  流程=%s  键=%s" % (
        flow, "音量减" if want is DOWN else "音量加"))
    if not wait_server(120):
        log("airweb 没起来，退出。请先 ./start-web.sh")
        return 3
    serial = device_serial()
    if not serial:
        log("没有 adb 设备，退出")
        return 4
    log("设备 %s" % serial)
    listen(serial, want, once, flow)
    return 0


if __name__ == "__main__":
    sys.exit(main())
