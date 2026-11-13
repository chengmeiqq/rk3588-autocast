# -*- coding: utf-8 -*-
"""arb.py — 前台仲裁：两个守护轮流用前台，绝不挨着动手。

为什么需要：keepcast 要点屏幕走投屏流程，lenscast 要把直播画面顶到
前台。两者都在跟"当前前台是谁"打交道，只要动作挨得太近就会互相
踩——一个刚切走，另一个的点击就落在空气上，或者落在越界坐标。

CAST_LOCK 只在"投屏流程"期间生效，管不住"投屏刚结束、另一个马上动手"
这种缝隙。所以这里再放一道时间闸：谁动过手，另一个必须等够 GAP 秒
才能动。等是有上限的（MAX_WAIT），等不到就这轮放弃，下轮再来，绝不
硬闯。

状态落在 state/fg.json，两个进程共用。
"""

import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")
STATE = os.path.join(STATE_DIR, "fg.json")
LOCK = "/tmp/_fg_arb.lock"

# 两次动作之间最少隔多久。太小防不住踩踏，太大会让画面回得慢。
GAP = float(os.environ.get("FG_GAP", "12"))
# 最多等多久。等不到就放弃本轮，别把守护卡死。
MAX_WAIT = float(os.environ.get("FG_MAX_WAIT", "45"))


def _locked(fn):
    """在文件锁里读-改-写，两个进程同时来也不会写坏。"""
    import fcntl
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOCK, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _load():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, STATE)
    except Exception:
        pass


def touch(who, what=""):
    """记录"我刚刚动了前台"。"""
    def act():
        d = _load()
        d[who] = {"ts": time.time(), "what": what}
        _save(d)
    try:
        _locked(act)
    except Exception:
        pass


def since_other(who):
    """另一个守护距上次动手过了多少秒。没人动过返回一个很大的数。"""
    d = _load()
    best = None
    for k, v in d.items():
        if k == who or not isinstance(v, dict):
            continue
        ts = v.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        gap = time.time() - ts
        if best is None or gap < best:
            best = gap
    return 1e9 if best is None else best


def last(who=None):
    d = _load()
    if who:
        return d.get(who)
    return d


def wait_turn(who, log=print, max_wait=None):
    """等到"轮到我"再动手。返回 True=可以动，False=等到上限了放弃本轮。"""
    limit = MAX_WAIT if max_wait is None else max_wait
    deadline = time.time() + limit
    waited = 0.0
    while True:
        gap = since_other(who)
        if gap >= GAP:
            if waited > 0:
                log("[间隔] 等了 %.0f 秒，另一个守护已停手，可以动手" % waited)
            return True
        remain = GAP - gap
        if time.time() + remain > deadline:
            log("[间隔] 另一个守护 %.0f 秒前刚动过手，再等就要超时，本轮放弃"
                % gap)
            return False
        time.sleep(min(remain, 2.0))
        waited += min(remain, 2.0)


def status():
    """给网页看的。"""
    now = time.time()
    out = {"gap_seconds": GAP, "daemons": {}}
    for k in ("keepcast", "lenscast"):
        v = (last(k) or {})
        ts = v.get("ts")
        out["daemons"][k] = {
            "since": (round(now - ts, 1) if isinstance(ts, (int, float)) else None),
            "what": v.get("what", ""),
        }
    return out
