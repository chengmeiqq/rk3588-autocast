# -*- coding: utf-8 -*-
"""detail.py — 给总控台用的细节点采集。

为什么单独一个模块：原来页面只有四个粗开关，用户的原话是「分细节点，
把豆包 直播都要加进去」。粗开关只知道"守护开着没有"，看不出
"豆包到底投没投""直播到底推没推""卡在哪一步"。这里把两套链路拆成
一条条可判定的细节点，页面上一排点，红了就是那一步。

采集代价要控制：页面 4 秒轮询一次，不能每次都跑一堆 adb。
所以整份结果缓存 TTL 秒，且 adb 探测尽量合并成一条 shell 命令
（一次 fork 拿全，比连开五条快一个数量级）。
"""

import os
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))

ADB = "/usr/lib/android-sdk/platform-tools/adb"
DOUBAO = os.environ.get("PHONE_APP", "com.larus.nova")
LENS = os.environ.get("LENS_PKG", "com.opencode.multilensipcam")
LENS_API = "http://127.0.0.1:41737"

_cache = {"at": 0.0, "data": None}
TTL = 6.0            # 页面 4 秒轮一次，这里 6 秒够用，少起一半 adb
PROBE_TIMEOUT = 8    # 单次 shell 上限。20 秒太长，卡住会积一堆挂死进程
BREAK_UNTIL = [0.0]  # 熔断到期时刻
BREAK_FAILS = [0]    # 连续失败次数
PROBE_LOCK = "/tmp/_detail_probe.lock"


def _adb(serial, shell, timeout=PROBE_TIMEOUT):
    """跑一条 adb shell。超时就杀掉子进程，绝不留挂死的 adb 在板上。

    教训：这台机上 system_server 一旦卡住，dumpsys 会无限等 binder，
    adb 客户端进程就一直挂着。之前 12 个挂死进程把 adb 通道彻底堵死，
    连 `echo` 都不返回，最后只能重启手机。所以这里必须硬超时 + 熔断。
    """
    try:
        p = subprocess.run([ADB, "-s", serial, "shell", shell],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout)
        return p.stdout.decode("utf-8", "replace"), True
    except subprocess.TimeoutExpired:
        # run() 超时后已经 kill 了子进程
        return "", False
    except Exception:
        return "", False


def _one_shot(serial):
    """一条 shell 拿全部进程/前台/屏幕事实。

    单飞：上一次采集没回来就绝不起第二条。否则页面 4 秒一次、每次卡 8 秒，
    挂死的 adb 进程会越积越多，最后把整条通道堵掉（已真实发生过一次）。
    """
    import fcntl
    try:
        lf = open(PROBE_LOCK, "w")
    except Exception:
        return None
    try:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None          # 上一轮探测还没回来，这轮直接跳过
        return _one_shot_locked(serial)
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        finally:
            lf.close()


def _one_shot_locked(serial):
    sh = (
        'echo "FG:"; dumpsys activity activities | grep topResumedActivity | head -1; '
        'echo "DB:"; pidof ' + DOUBAO + ' 2>/dev/null; '
        'echo "LC:"; pidof ' + LENS + ' 2>/dev/null; '
        'echo "WK:"; dumpsys power | grep -m1 "mWakefulness="; '
        'echo "SZ:"; wm size | tail -1'
    )
    out, ok = _adb(serial, sh, timeout=PROBE_TIMEOUT)
    if not ok:
        return None                  # 探测超时，上层按"数据不全"处理
    facts = {"fg": "", "db_pid": 0, "lc_pid": 0, "wake": "", "size": ""}
    cur = None
    for line in out.splitlines():
        line = line.strip()
        if line in ("FG:", "DB:", "LC:", "LC:", "WK:", "SZ:"):
            cur = line[:-1]
            continue
        if not line or cur is None:
            continue
        if cur == "FG":
            if not facts["fg"] and "u0 " in line:
                # 形如 topResumedActivity=ActivityRecord{... u0
                # com.x/.Y t888}，末尾那个 } 是 ActivityRecord 的收尾，
                # 不剥掉会显示成 "...MainActivity}"，看着像解析坏了。
                facts["fg"] = line.split("u0 ", 1)[1].split()[0].rstrip("}")
        elif cur == "DB":
            facts["db_pid"] = int(line.split()[0]) if line.split()[0].isdigit() else 0
        elif cur == "LC":
            facts["lc_pid"] = int(line.split()[0]) if line.split()[0].isdigit() else 0
        elif cur == "WK":
            facts["wake"] = line.split("=", 1)[-1].strip()
        elif cur == "SZ":
            facts["size"] = line
    return facts


def _lens_state():
    """直播 App 自己报的状态。不通就返回 None。"""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(LENS_API + "/api/state", timeout=3) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _state_file(name):
    import json
    p = os.path.join(HERE, "state", name)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _row(key, label, ok, value):
    """一个细节点。ok=None 表示"不适用/信息项"，画灰点。"""
    return {"key": key, "label": label, "ok": ok, "value": str(value)}


def collect(serial, force=False):
    now = time.time()
    if not force and _cache["data"] and now - _cache["at"] < TTL:
        d = dict(_cache["data"])
        d["age"] = round(now - _cache["at"], 1)
        return d

    import smartcast

    # 熔断：连续探测失败就歇一段时间，别一直锤手机的 adbd。
    # 手机卡死时页面会显示"探测熔断"，比一片红点更接近事实。
    if time.time() < BREAK_UNTIL[0]:
        left = int(BREAK_UNTIL[0] - time.time())
        return {"serial": serial, "doubao": [], "lens": [],
                "fg": "", "owner": "", "screen": "",
                "at": time.strftime("%H:%M:%S"), "age": 0.0,
                "probe_down": True,
                "probe_note": "adb 探测熔断中（还差 %d 秒），手机可能卡住了" % left}

    facts = _one_shot(serial) if serial else None
    if facts is None:
        # 探测没回来：可能是上一轮还没跑完（跳过），也可能是真卡了。
        # 只有真正超时的才算失败，用来累计熔断。
        BREAK_FAILS[0] += 1
        if BREAK_FAILS[0] >= 3:
            BREAK_UNTIL[0] = time.time() + 60
            BREAK_FAILS[0] = 0
        facts = {"fg": "", "db_pid": 0, "lc_pid": 0, "wake": "", "size": ""}
        probe_down = True
    else:
        BREAK_FAILS[0] = 0
        probe_down = False

    try:
        owner = smartcast.casting_owner(serial) if serial else ""
    except Exception:
        owner = ""
    lens = _lens_state()
    kc = _state_file("keepcast.json")
    lc = _state_file("lenscast.json")

    fg = facts["fg"]
    db_front = DOUBAO in fg
    lens_front = LENS in fg

    # ── 豆包 / 投屏链路 ──
    doubao = [
        _row("db_proc", "豆包进程", facts["db_pid"] > 0,
             ("pid %d" % facts["db_pid"]) if facts["db_pid"] else "没在跑"),
        _row("db_cast", "投屏会话", bool(owner),
             owner or "未建立"),
        # 这一项是「信息」不是「故障」：keepcast 投屏成功后必须按 HOME 回桌面，
        # 之后 lenscast 再把直播画面顶回前台 —— 豆包不在前台是预期的正常收尾。
        # 以前标红，页面看着像坏了，实际全好。
        _row("db_front", "当前前台", None,
             ("豆包在前台" if db_front
              else ("直播在前台" if lens_front
                    else (fg or "读不到")))),
        _row("db_usb", "USB 连接", bool(kc.get("plugged")),
             "已连接" if kc.get("plugged") else "未连接"),
        _row("db_dlg", "USB 弹窗", None, kc.get("usb_dialog") or "无"),
        _row("db_tasker", "Tasker 守护", bool(kc.get("tasker_pid")),
             ("pid %s" % kc.get("tasker_pid")) if kc.get("tasker_pid") else "没在跑"),
        _row("db_note", "投屏守护", bool(kc.get("ok")),
             "%s · 失败 %s 次 · 已稳 %ss" % (
                 kc.get("note") or "未知", kc.get("fails", 0), kc.get("uptime", 0))),
        _row("db_at", "最后判定", None, kc.get("at") or "无"),
    ]

    # ── 直播 / 推流链路 ──
    ls = lens or {}
    stream_ok = bool(ls.get("streaming"))
    live = [
        _row("lc_proc", "直播进程", facts["lc_pid"] > 0,
             ("pid %d" % facts["lc_pid"]) if facts["lc_pid"] else "没在跑"),
        _row("lc_api", "控制台 41737", lens is not None,
             "通" if lens is not None else "不通"),
        _row("lc_stream", "推流中", stream_ok,
             "是" if stream_ok else "否"),
        _row("lc_cam", "摄像头", None,
             ls.get("selectedCameraLabel") or ls.get("selectedCameraKey") or "未知"),
        _row("lc_res", "分辨率", None, ls.get("selectedResolution") or "未知"),
        _row("lc_q", "质量", None, ls.get("qualityValue") if ls.get("qualityValue") is not None else "未知"),
        _row("lc_focus", "对焦", None, ls.get("focusMode") or "未知"),
        _row("lc_zoom", "变焦", None, ls.get("zoomValue") if ls.get("zoomValue") is not None else "未知"),
        _row("lc_front", "画面可见", lens_front,
             ("直播在前台" if lens_front else (fg or "读不到"))),
        _row("lc_awake", "屏幕常亮", facts["wake"] == "Awake",
             facts["wake"] or "读不到"),
        _row("lc_note", "直播守护", bool(lc.get("ok")), lc.get("note") or "未知"),
    ]

    data = {
        "serial": serial,
        "doubao": doubao,
        "lens": live,
        "fg": fg,
        "owner": owner,
        "screen": facts["size"],
        "at": time.strftime("%H:%M:%S"),
        "age": 0.0,
        "probe_down": probe_down,
    }
    _cache["at"] = now
    _cache["data"] = data
    return data
