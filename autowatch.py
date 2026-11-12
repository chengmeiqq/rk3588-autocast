#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""插电自动跑 —— 板子端守护进程。

用途：手机上插上 USB 通电后，自动执行一个 airweb 流程（比如 启动豆包→打电话→投屏）。

为什么在板子上跑而不是手机 Tasker：
  - 板子这边已经有 airweb + 模板 + 验证逻辑，直接复用，不用在 Tasker UI 里手搓
  - Tasker 侦测 "USB 插入" 在 Android 13 上要 Power Connected 事件 + 无障碍，
    配置繁琐且容易漏；板子直接读 dumpsys battery，一秒一次，最稳
  - 电脑/板子 24 小时开着的时候，这条路零配置

用法：
  ./autowatch.py                      # 用默认流程 flows/通话投屏.json
  ./autowatch.py flows/xxx.json       # 指定流程
  ./autowatch.py --dry                # 只打印，不真执行
  ./autowatch.py --once               # 当前已插电就跑一次然后退出

逻辑：
  每 1 秒读一次充电状态。从「没插电」变成「插电」时触发一次。
  触发后静默 90 秒，避免反复触发。日志写 logs/autowatch.log。
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "http://127.0.0.1:8123"
DEFAULT_FLOW = os.path.join(HERE, "flows", "通话投屏.json")
LOG = os.path.join(HERE, "logs", "autowatch.log")
# 冷却只为挡住「同一次插拔被反复触发」，不该挡人。
# 90 秒太久：刚跑完想立刻再试一次就被忽略，实测踩过这个坑。
COOLDOWN = 12.0


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def api(path, body=None, timeout=180):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_server(timeout=120):
    """等 airweb 起来。启动顺序不确定，不能一上来就 Connection refused。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            api("/power", timeout=5)
            return True
        except Exception:
            time.sleep(2)
    return False


def powered():
    """True=插着电（USB 或 AC）。读不到就返回 None。"""
    try:
        st = api("/power", timeout=15)
        return bool(st.get("plugged"))
    except Exception as e:
        log("读电源状态失败: %s" % e)
        return None


def run_flow(flow_path, dry=False):
    try:
        with open(flow_path) as f:
            flow = json.load(f)
    except Exception as e:
        log("流程文件读不了: %s (%s)" % (flow_path, e))
        return False
    steps = flow.get("steps") or []
    log("开始跑流程「%s」，%d 步" % (flow.get("name", os.path.basename(flow_path)), len(steps)))
    if dry:
        for i, s in enumerate(steps, 1):
            log("  [dry] %d. %s" % (i, s.get("do")))
        return True
    try:
        r = api("/flow/run", {"steps": steps, "stop_on_fail": True,
                              "tries": 8, "interval": 0.8}, timeout=600)
    except Exception as e:
        log("调用流程接口失败: %s" % e)
        return False
    if not r.get("ok"):
        log("流程接口报错: %s" % r.get("error"))
        return False
    for rec in r.get("results", []):
        mark = "OK " if (rec.get("action_ok") and rec.get("verify_ok") is not False) else "FAIL"
        log("  %s %d. %s (%sms)" % (mark, rec.get("i"), rec.get("desc"), rec.get("ms")))
        v = rec.get("verify") or {}
        for c in v.get("checks", []):
            log("       %s %s 期望=%s 实际=%s" % (
                "v" if c.get("pass") else "x",
                c.get("kind"), c.get("expect"),
                c.get("at") or c.get("actual")))
    log("流程结束：%s  用时 %sms  %s/%s 步通过" % (
        "全部通过" if r.get("all_pass") else "有失败",
        r.get("ms"), r.get("done"), r.get("total")))
    return bool(r.get("all_pass"))


def main():
    args = [a for a in sys.argv[1:]]
    dry = "--dry" in args
    once = "--once" in args
    flow = DEFAULT_FLOW
    for a in args:
        if not a.startswith("--"):
            flow = os.path.abspath(a)
            break
    log("===== autowatch 启动  流程=%s  dry=%s  once=%s" % (flow, dry, once))
    if not os.path.exists(flow):
        log("流程文件不存在: %s" % flow)
        return 2

    if not dry:
        log("等待 airweb 服务就绪…")
        if not wait_server(120):
            log("airweb 一直没起来，退出。请先 ./start-web.sh")
            return 3
        log("airweb 就绪")

    if once:
        p = powered()
        log("当前插电状态: %s" % p)
        return 0 if (p and run_flow(flow, dry)) else 1

    last = None
    cooled = 0.0
    while True:
        p = powered()
        if p is None:
            time.sleep(3)
            continue
        if last is None:
            last = p
            log("初始状态：%s" % ("插电" if p else "未插电"))
            # 启动时如果已经插着电，立刻执行一次，
            # 不等下一次拔插（用户要求：立即开始也要点击）
            if p:
                log(">>> 启动时已插电，立刻执行流程")
                ok = run_flow(flow, dry)
                cooled = time.time()
                log(">>> 执行%s" % ("成功" if ok else "失败"))
        elif p and not last:
            if time.time() - cooled < COOLDOWN:
                log("检测到插电，但还在冷却期，跳过")
            else:
                log(">>> 检测到插电，开始执行流程")
                ok = run_flow(flow, dry)
                cooled = time.time()
                log(">>> 执行%s" % ("成功" if ok else "失败"))
        elif not p and last:
            log("已拔出电源")
        last = p
        time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(main())
