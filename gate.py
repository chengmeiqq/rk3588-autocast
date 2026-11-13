#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""守护总开关：四个守护共用一套暂停标记。

为什么要有这个：
  调试的时候守护老是自己弹出来抢屏幕（重建投屏、顶前台、点弹窗），
  手动关服务再开要等 RestartSec、还会漏掉正在跑的循环。
  暂停标记是"就地生效"的：守护进程不退出，只是每轮开头看一眼，
  有标记就 sleep 继续，一条 adb 命令都不发。

标记放哪里：
  state/allow.json —— 一份总开关，四个守护都读它。
  兼容旧路径：state/keepcast.pause、state/lenscast.pause 仍然有效，
  任何一个存在都算暂停，避免以前的脚本/习惯失效。

为什么用 allow.json 而不是四个 .pause 文件：
  一次调试往往要同时按住投屏和直播两个守护，一个文件写一次就够，
  也便于网页上做成一个总开关。allow.json 里还可以按守护单独关。
"""
from __future__ import print_function

import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")
ALLOW = os.path.join(STATE_DIR, "allow.json")

# 旧路径：这两个文件存在也当作暂停，向后兼容
LEGACY = {
    "keepcast": os.path.join(STATE_DIR, "keepcast.pause"),
    "lenscast": os.path.join(STATE_DIR, "lenscast.pause"),
}

ALL_DAEMONS = ["adbd", "smartcast", "keepcast", "lenscast"]

_DEFAULTS = {
    "all": True,          # 总闸
    "adbd": True,
    "smartcast": True,
    "keepcast": True,
    "lenscast": True,
    "note": "",
    "at": "",
}


def _read():
    """读开关文件，坏了就当全开（守护优先，避免一次写坏就永久停摆）。"""
    data = dict(_DEFAULTS)
    try:
        with open(ALLOW, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k in _DEFAULTS:
                if k in raw:
                    data[k] = raw[k]
    except Exception:
        pass
    return data


def _write(data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = ALLOW + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ALLOW)


def is_allowed(name):
    """这个守护现在能不能干活。True=可以，False=被暂停。"""
    if name in LEGACY and os.path.exists(LEGACY[name]):
        return False
    d = _read()
    if not d.get("all", True):
        return False
    return bool(d.get(name, True))


def pause(name=None, note=""):
    """暂停：name=None 表示全部。"""
    d = _read()
    if name is None:
        d["all"] = False
    elif name in ALL_DAEMONS:
        d[name] = False
    else:
        raise ValueError("未知守护: %s" % name)
    d["note"] = note or d.get("note", "")
    d["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write(d)
    return d


def resume(name=None, note=""):
    """恢复：name=None 表示全部恢复。"""
    d = _read()
    if name is None:
        d["all"] = True
        for k in ALL_DAEMONS:
            d[k] = True
    elif name in ALL_DAEMONS:
        d[name] = True
    else:
        raise ValueError("未知守护: %s" % name)
    d["note"] = note or ""
    d["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write(d)
    # 恢复时顺手清掉旧标记，否则兼容层会把总闸又关掉
    for p in LEGACY.values():
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    return d


def status():
    """给网页用的完整状态。"""
    d = _read()
    out = {
        "all": bool(d.get("all", True)),
        "note": d.get("note", ""),
        "at": d.get("at", ""),
        "daemons": {},
    }
    for k in ALL_DAEMONS:
        legacy = LEGACY.get(k)
        legacy_on = bool(legacy and os.path.exists(legacy))
        out["daemons"][k] = {
            "enabled": bool(d.get(k, True)),
            "legacy_pause": legacy_on,
            "allowed": is_allowed(k),
        }
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    else:
        cmd = sys.argv[1]
        who = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "all" else None
        if cmd == "pause":
            print(json.dumps(pause(who), ensure_ascii=False))
        elif cmd == "resume":
            print(json.dumps(resume(who), ensure_ascii=False))
        else:
            print(json.dumps(status(), ensure_ascii=False, indent=2))
