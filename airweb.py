#!/root/.openclaw/workspace/Airtest/.venv/bin/python
# -*- coding: utf-8 -*-
"""airweb - 手机上点哪儿，网页上框一下就行。

启动:  ./airweb            (默认 0.0.0.0:8123)
       ./airweb 9000       (换端口)

用法:
  1. 左边是手机实时画面（可调刷新）
  2. 在画面上拖动框选一块 -> 起个名字 -> 存为模板
  3. 点"找图并点" -> 它在整屏上搜这个模板并点中心
  4. 也可以直接"点坐标"：在画面上单击，点那个位置
  5. 所有操作都能存成步骤，一键重放
"""
from __future__ import print_function

import io
import os
import sys
import time
import json
import threading
import traceback

os.environ.setdefault("ANDROID_HOME", "/usr/lib/android-sdk")
os.environ["PATH"] = "/usr/lib/android-sdk/platform-tools:" + os.environ.get("PATH", "")

from flask import Flask, request, jsonify, send_file, Response

from airlib import phone as make_phone

ROOT = os.path.dirname(os.path.abspath(__file__))
TPL_DIR = os.path.join(ROOT, "templates")
os.makedirs(TPL_DIR, exist_ok=True)

app = Flask(__name__)
LOCK = threading.Lock()
_phone = None
_cache = {"jpeg": None, "ts": 0.0}


def get_phone():
    global _phone
    if _phone is None:
        _phone = make_phone()
    return _phone


def drop_phone():
    """丢掉缓存的连接，下次自动重连。

    拔 USB 时会话会断（device not found）。不主动丢弃的话，
    后面每个请求都拿着一条死连接报错，服务等于废了。
    """
    global _phone
    _phone = None


def heal_phone():
    """连接出问题时换条通道试试，不行再丢掉。

    drop_phone() 只是丢掉缓存，rebuild 时 pick_serial() 会重新挑一条
    还在的设备——这样 WiFi 掉了能自动退回 USB，USB 拔了能自动走 WiFi。
    """
    global _phone
    p = _phone
    _phone = None
    if p is None:
        return
    try:
        p.reconnect()
        _phone = p
    except Exception:
        _phone = None      # 救不回来就等下一次 get_phone() 从头建


def safe(fn):
    """把所有异常变成 JSON 错误，前端能看见原因。"""
    def inner(*a, **kw):
        try:
            with LOCK:
                return fn(*a, **kw)
        except Exception as e:
            traceback.print_exc()
            msg = "%s: %s" % (type(e).__name__, e)
            # 设备掉线就丢连接，下一次请求重建，别一直拿着死连接
            if "not found" in msg or "Connection" in msg or "connection" in msg:
                try:
                    heal_phone()
                except Exception:
                    pass
            return jsonify(ok=False, error="%s: %s" % (type(e).__name__, e)), 500
    inner.__name__ = fn.__name__
    return inner


POWER_LOCK = threading.Lock()


def safe_light(fn):
    """不抢全局 LOCK 的接口。

    /power 被 autowatch 每秒问一次。跑流程时全局 LOCK 会被占住几十秒，
    走 @safe 就会一直排队到超时，表现成「读电源状态失败: timed out」。
    这类只读、极短的接口用独立的锁，互不阻塞。
    """
    def inner(*a, **kw):
        try:
            with POWER_LOCK:
                return fn(*a, **kw)
        except Exception as e:
            traceback.print_exc()
            return jsonify(ok=False, error="%s: %s" % (type(e).__name__, e)), 500
    inner.__name__ = fn.__name__
    return inner


@app.route("/")
def index():
    return Response(HTML, mimetype="text/html; charset=utf-8")


@app.route("/screen.jpg")
def screen_jpg():
    """当前手机画面。?t= 时间戳防缓存；?fresh=1 强制重截。"""
    import cv2
    fresh = request.args.get("fresh") == "1"
    now = time.monotonic()
    with LOCK:
        if fresh or _cache["jpeg"] is None or now - _cache["ts"] > 0.4:
            p = get_phone()
            img = p.snap()
            ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            _cache["jpeg"] = buf.tobytes()
            _cache["ts"] = now
            h, w = img.shape[:2]
            _cache["size"] = (w, h)
    resp = Response(_cache["jpeg"], mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/info")
@safe
def info():
    p = get_phone()
    return jsonify(ok=True, serial=p.serial, resolution=p.resolution(),
                   app=p.current_app())


@app.route("/tap", methods=["POST"])
@safe
def tap():
    d = request.get_json(force=True)
    p = get_phone()
    w, h = p.resolution()
    # 前端传的是 0~1 的比例，避免缩放误差
    x, y = int(round(float(d["fx"]) * w)), int(round(float(d["fy"]) * h))
    t0 = time.monotonic()
    p.tap(x, y)
    return jsonify(ok=True, x=x, y=y, ms=int((time.monotonic() - t0) * 1000))


def _snap_np(p):
    return p.snap()


def _compare(before, after, x, y, radius=140):
    """返回点击前后的差异证据。"""
    import cv2
    import numpy as np
    b = before.astype(np.int16)
    a = after.astype(np.int16)
    if b.shape != a.shape:
        return {"changed": True, "diff": 100.0, "why": "画面尺寸变了（可能转屏）"}
    d = np.abs(a - b).mean(axis=2)
    overall = float(d.mean())
    h, w = d.shape
    x1, x2 = max(0, x - radius), min(w, x + radius)
    y1, y2 = max(0, y - radius), min(h, y + radius)
    local = float(d[y1:y2, x1:x2].mean()) if x2 > x1 and y2 > y1 else overall
    ratio = float((d > 12).mean()) * 100.0
    changed = overall > 1.2 or local > 3.0
    return {"changed": bool(changed), "diff": round(overall, 2),
            "local_diff": round(local, 2), "changed_pct": round(ratio, 2)}


@app.route("/tap_verify", methods=["POST"])
@safe
def tap_verify():
    """点一个位置，并自动比对点击前后的画面，证明真的点到了。"""
    d = request.get_json(force=True)
    p = get_phone()
    w, h = p.resolution()
    x, y = int(round(float(d["fx"]) * w)), int(round(float(d["fy"]) * h))
    wait_s = float(d.get("wait", 0.7))
    before = p.snap()
    t0 = time.monotonic()
    p.tap(x, y)
    tap_ms = int((time.monotonic() - t0) * 1000)
    time.sleep(wait_s)
    after = p.snap()
    v = _compare(before, after, x, y)
    save_proof(before, after, x, y)
    return jsonify(ok=True, x=x, y=y, ms=tap_ms, verify=v)


LAST_PROOF = {"before": None, "after": None, "x": 0, "y": 0}


def save_proof(before, after, x, y):
    import cv2
    LAST_PROOF["before"], LAST_PROOF["after"] = before, after
    LAST_PROOF["x"], LAST_PROOF["y"] = x, y


@app.route("/proof/<which>.jpg")
@safe
def proof(which):
    import cv2
    img = LAST_PROOF.get("before" if which == "before" else "after")
    if img is None:
        return jsonify(ok=False, error="还没有验证记录"), 404
    if which == "after":
        img = img.copy()
        x, y = LAST_PROOF["x"], LAST_PROOF["y"]
        cv2.circle(img, (x, y), 60, (0, 0, 255), 5)
        cv2.line(img, (x - 90, y), (x + 90, y), (0, 0, 255), 2)
        cv2.line(img, (x, y - 90), (x, y + 90), (0, 0, 255), 2)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    resp = Response(buf.tobytes(), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def foreground(p):
    """当前前台 activity，尽量短。"""
    try:
        out = p.shell("dumpsys window 2>/dev/null | grep mCurrentFocus")
    except Exception:
        return None
    if not out:
        return None
    import re
    found = re.findall(r"u0 ([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)", out)
    if found:
        # MIUI 会打印多行 mCurrentFocus（含历史/其它 display 的残留），
        # 真正生效的是最后一个。
        return found[-1]
    # 全 null 时退回 mFocusedApp
    try:
        out2 = p.shell("dumpsys activity activities 2>/dev/null | grep -m1 mFocusedApp")
        m = re.search(r"u0 ([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)", out2 or "")
        return m.group(1) if m else None
    except Exception:
        return None


def casting(p):
    """投屏是否还在进行。返回 {on, owner, size}。

    别用 dumpsys media_projection —— 这台 MIUI 上它投屏中也是 null。
    投屏一定会建一个 owner=<包名> 的 ScreenCapture 虚拟显示，这个骗不了人。
    """
    try:
        out = p.shell("dumpsys display 2>/dev/null | grep ScreenCapture")
    except Exception:
        return {"on": False, "why": "读不到 display"}
    if not out or "virtual:" not in out:
        return {"on": False, "why": "没有 ScreenCapture 虚拟显示"}
    import re
    m = re.search(r"uniqueId=\"virtual:([A-Za-z0-9_.]+):?\d*[^\"]*\"", out) or \
        re.search(r"owner ([A-Za-z0-9_.]+)", out)
    sz = re.search(r"real (\d+) x (\d+)", out)
    on = "state ON" in out
    return {"on": on, "owner": m.group(1) if m else None,
            "size": "%sx%s" % sz.groups() if sz else None}


def screen_on(p):
    """屏幕是不是亮的。"""
    try:
        out = p.shell("dumpsys deviceidle 2>/dev/null | grep -m1 mScreenOn")
    except Exception:
        return None
    if "mScreenOn=true" in (out or ""):
        return True
    if "mScreenOn=false" in (out or ""):
        return False
    return None


def wake(p, tries_n=3):
    """唤醒屏幕并解开锁屏。

    插 USB 时手机常常是息屏的。息屏状态下 mCurrentFocus 全是 null，
    foreground() 读不到任何东西，所有验证都会超时失败。
    """
    st = screen_on(p)
    if st is True:
        # 亮着也可能停在锁屏上
        p.shell("wm dismiss-keyguard")
        return True
    for _ in range(max(1, tries_n)):
        p.shell("input keyevent KEYCODE_WAKEUP")
        time.sleep(1.0)
        p.shell("wm dismiss-keyguard")
        time.sleep(0.8)
        if screen_on(p) is True:
            return True
    return screen_on(p) is True


def screen_texts(p):
    """读屏幕上的文字（uiautomator），用于语义验证。

    注意：dump 失败时必须返回空列表，绝不能读上一次的残留文件，
    否则验证会拿旧文字冒充当前屏幕。
    """
    try:
        p.shell("rm -f /sdcard/_aw.xml")
        p.shell("uiautomator dump /sdcard/_aw.xml >/dev/null 2>&1")
        if not p.shell("ls /sdcard/_aw.xml 2>/dev/null").strip():
            return []
        xml = p.shell("cat /sdcard/_aw.xml 2>/dev/null")
    except Exception:
        return []
    if not xml or "hierarchy" not in xml:
        return []
    import re
    out = []
    for t in re.findall(r'text="([^"]*)"', xml):
        t = t.strip()
        if t and t not in out:
            out.append(t)
    return out


@app.route("/state")
@safe
def state():
    """当前前台 App + 屏幕上看得见的文字。"""
    p = get_phone()
    return jsonify(ok=True, app=foreground(p), texts=screen_texts(p)[:60])


@app.route("/power")
@safe_light
def power():
    """电源状态：插着电吗。用于「插电自动执行」。"""
    p = get_phone()
    out = p.shell("dumpsys battery") or ""
    usb = "USB powered: true" in out
    ac = "AC powered: true" in out
    wireless = "Wireless powered: true" in out
    level = None
    for line in out.splitlines():
        if "level:" in line:
            try:
                level = int(line.split(":")[1].strip())
            except Exception:
                pass
            break
    return jsonify(ok=True, plugged=bool(usb or ac or wireless),
                   usb=usb, ac=ac, wireless=wireless, level=level)


@app.route("/verify_after_tap", methods=["POST"])
@safe
def verify_after_tap():
    """语义验证：点完之后，是不是变成了预期的 App / 出现了预期文字。

    {expect_app: "com.larus.nova", expect_text: "发送", wait: 2}
    """
    d = request.get_json(force=True)
    p = get_phone()
    wait_s = float(d.get("wait", 2.0))
    time.sleep(wait_s)
    app = foreground(p)
    texts = screen_texts(p)
    res = {"ok": True, "app": app, "texts": texts[:60], "checks": []}
    ea = (d.get("expect_app") or "").strip()
    if ea:
        hit = bool(app and app.startswith(ea))
        res["checks"].append({"kind": "app", "expect": ea, "actual": app, "pass": hit})
    et = (d.get("expect_text") or "").strip()
    if et:
        hit = any(et in t for t in texts)
        res["checks"].append({"kind": "text", "expect": et,
                              "actual": [t for t in texts if et in t][:3], "pass": hit})
    res["all_pass"] = all(c["pass"] for c in res["checks"]) if res["checks"] else None
    return jsonify(**res)


FLOWS_DIR = os.path.join(ROOT, "flows")
os.makedirs(FLOWS_DIR, exist_ok=True)


def _find_text_node(p, want, fuzzy=True):
    """在界面上找含 want 的文字节点，返回中心坐标或 None。

    系统弹窗、设置页这类标准界面能读；豆包自己画的界面读不到，
    那时要用模板匹配（tap_template）。
    """
    import re
    try:
        p.shell("rm -f /sdcard/_tn.xml")
        p.shell("uiautomator dump /sdcard/_tn.xml >/dev/null 2>&1")
        if not p.shell("ls /sdcard/_tn.xml 2>/dev/null").strip():
            return None
        xml = p.shell("cat /sdcard/_tn.xml 2>/dev/null")
    except Exception:
        return None
    if not xml or "hierarchy" not in xml:
        return None
    best = None
    for m in re.finditer(r"<node[^>]*>", xml):
        tag = m.group(0)
        tm = re.search(r'text="([^"]*)"', tag)
        bm = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag)
        if not tm or not bm:
            continue
        label = tm.group(1).strip()
        if not label:
            continue
        hit = (want in label) if fuzzy else (label == want)
        if not hit:
            continue
        x1, y1, x2, y2 = (int(v) for v in bm.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        # 优先精确相等，其次更小的控件（更像按钮）
        score = (0 if label == want else 1, (x2 - x1) * (y2 - y1))
        if best is None or score < best[0]:
            best = (score, cx, cy, label)
    return (best[1], best[2], best[3]) if best else None


def _one_step(p, step):
    """执行一步，返回 (是否动作成功, 说明, 附加)。"""
    kind = step.get("do")
    if kind == "open":
        pkg = step["pkg"]
        # 上次没退出时会直接恢复到通话页，首页的"打电话"根本不存在。
        # 所以先杀干净再启动，保证每次都从首页开始。
        if step.get("force_stop", True):
            try:
                p.shell("am force-stop %s" % pkg)
                time.sleep(float(step.get("stop_wait", 1.5)))
            except Exception:
                pass
        # monkey 比 am start 稳（不依赖具体 activity 名），
        # 但启动后前台切换有延迟，这里等它真的上来再说成功，
        # 免得后面验证据此误判。
        p.open(pkg, wait=float(step.get("open_wait", 1.5)))
        deadline = time.monotonic() + float(step.get("launch_timeout", 12))
        cur = None
        while time.monotonic() < deadline:
            cur = foreground(p)
            if cur and cur.startswith(pkg):
                return True, "启动 %s" % pkg, {"app": cur}
            time.sleep(0.5)
        return True, "已发启动指令 %s（前台仍是 %s）" % (pkg, cur), {"app": cur}
    if kind == "tap_template":
        name = os.path.basename(step["name"])
        path = os.path.join(TPL_DIR, name)
        if not os.path.exists(path):
            return False, "模板不存在：%s" % name, {"fatal": True}
        pt = p.find(path, threshold=float(step.get("threshold", 0.75)),
                    timeout=float(step.get("find_timeout", 8)))
        if not pt:
            return False, "屏幕上找不到 %s" % name, {}
        p.tap(pt[0], pt[1])
        return True, "点 %s @ (%d,%d)" % (name, pt[0], pt[1]), {"x": pt[0], "y": pt[1]}
    if kind == "tap_xy":
        w, h = p.resolution()
        x = int(round(float(step["fx"]) * w))
        y = int(round(float(step["fy"]) * h))
        p.tap(x, y)
        return True, "点 (%d,%d)" % (x, y), {"x": x, "y": y}
    if kind == "tap_text":
        # 按屏幕上看得见的文字点它的中心。系统授权弹窗里没有模板图，
        # 只能靠文字命中（例如录屏弹窗的"立即开始"）。
        want = step["text"]
        deadline = time.monotonic() + float(step.get("find_timeout", 8))
        last_seen = []
        while time.monotonic() < deadline:
            node = _find_text_node(p, want)
            if node:
                p.tap(node[0], node[1])
                return True, "点文字「%s」@ (%d,%d)" % (want, node[0], node[1]), \
                    {"x": node[0], "y": node[1]}
            last_seen = screen_texts(p)
            time.sleep(0.6)
        return False, "屏幕上找不到文字「%s」（看到：%s）" % (
            want, "/".join(last_seen[:8]) or "空"), {}
    if kind == "wake":
        # 插 USB 时手机多半是息屏的。不先唤醒，后面所有验证都会因为
        # mCurrentFocus=null 而超时。
        ok = wake(p)
        return ok, ("屏幕已唤醒" if ok else "屏幕唤不醒"), {"screen_on": ok}
    if kind == "home":
        # 回桌面。投屏是用 MediaProjection 抓整屏，回到桌面不影响共享，
        # 手机还能照常用别的东西。
        p.key("HOME")
        time.sleep(0.8)
        return True, "回桌面", {"app": foreground(p)}
    if kind == "dismiss":
        # 插 USB 时 MIUI 会自己弹「USB 用于：仅限充电/传输文件」全屏框，
        # 把后面的点击全挡掉。按返回关它。
        tries_n = int(step.get("dismiss_tries", 3))
        want_gone = (step.get("dismiss_text") or "USB 用于").strip()
        gone = False
        for _ in range(max(1, tries_n)):
            texts = screen_texts(p)
            if not any(want_gone in t for t in texts):
                gone = True
                break
            p.key("BACK")
            time.sleep(1.2)
        app = foreground(p)
        return True, ("已关掉「%s」弹窗" % want_gone) if gone else \
            "「%s」弹窗还在（当前 %s）" % (want_gone, app), {"app": app}
    if kind == "key":
        p.key(step["name"])
        return True, "按键 %s" % step["name"], {}
    if kind == "text":
        p.text(step["text"])
        return True, "输入「%s」" % step["text"], {}
    if kind == "swipe":
        p.swipe(step.get("dir", "up"))
        return True, "滑动 %s" % step.get("dir", "up"), {}
    if kind == "sleep":
        time.sleep(float(step.get("seconds", 1)))
        return True, "等 %ss" % step.get("seconds", 1), {}
    return False, "看不懂的动作：%r" % kind, {"fatal": True}


def _check(p, step, tries, interval):
    """按期望反复核对，直到通过或超时。返回 (通过?, 详情)。"""
    expect_app = (step.get("expect_app") or "").strip()
    expect_text = (step.get("expect_text") or "").strip()
    expect_tpl = (step.get("expect_template") or "").strip()
    expect_not_text = (step.get("expect_not_text") or "").strip()
    expect_casting = step.get("expect_casting")
    if not expect_app and not expect_text and not expect_tpl \
            and not expect_not_text and expect_casting is None:
        return True, {"skipped": True, "why": "这一步没设期望值"}
    last = {}
    for i in range(max(1, int(tries))):
        app = foreground(p)
        checks = []
        if expect_app:
            checks.append({"kind": "app", "expect": expect_app, "actual": app,
                           "pass": bool(app and app.startswith(expect_app))})
        texts = []
        if expect_text:
            texts = screen_texts(p)
            hit = [t for t in texts if expect_text in t]
            checks.append({"kind": "text", "expect": expect_text,
                           "actual": hit[:3], "pass": bool(hit)})
        if expect_not_text:
            texts = texts or screen_texts(p)
            gone = not any(expect_not_text in t for t in texts)
            checks.append({"kind": "text_gone", "expect": expect_not_text,
                           "actual": [t for t in texts if expect_not_text in t][:3],
                           "pass": gone})
        if expect_casting is not None:
            st = casting(p)
            checks.append({"kind": "casting",
                           "expect": "投屏中" if expect_casting else "未投屏",
                           "actual": st, "pass": st.get("on") == bool(expect_casting)})
        tpl_score = None
        if expect_tpl:
            import cv2
            path = os.path.join(TPL_DIR, os.path.basename(expect_tpl))
            if os.path.exists(path):
                th = float(step.get("expect_threshold", 0.75))
                pt = p.find(path, threshold=th, timeout=0)
                tpl_score = True if pt else False
                checks.append({"kind": "template", "expect": expect_tpl,
                               "threshold": th, "pass": bool(pt),
                               "at": list(pt) if pt else None})
            else:
                checks.append({"kind": "template", "expect": expect_tpl,
                               "pass": False, "error": "模板不存在"})
        ok = all(c["pass"] for c in checks)
        last = {"attempt": i + 1, "app": app, "checks": checks,
                "texts": texts[:40]}
        if ok:
            last["ok"] = True
            return True, last
        if i + 1 < tries:
            time.sleep(interval)
    last["ok"] = False
    return False, last


BLOCKER_TEXTS = ["USB 用于"]


def _clear_blockers(p):
    """把已知的系统弹窗按返回键清掉。

    只清白名单里的文案，不敢乱按：在通话页按一下 BACK 会把通话挂掉。
    """
    cleared = []
    for _ in range(3):
        try:
            texts = screen_texts(p)
        except Exception:
            return cleared
        hit = [b for b in BLOCKER_TEXTS if any(b in t for t in texts)]
        if not hit:
            return cleared
        cleared.extend(hit)
        try:
            p.key("BACK")
        except Exception:
            return cleared
        time.sleep(1.2)
    return cleared


def _is_landmark(step):
    """这一步能不能当"接着重来"的落脚点：做完之后屏幕上会有明确特征。"""
    return bool((step.get("expect_app") or "").strip()
                or (step.get("expect_text") or "").strip()
                or (step.get("expect_template") or "").strip())


def _recover_to(p, steps, anchor):
    """把手机拉回到 anchor 那一步该有的状态。

    光清弹窗不够：如果某个设置页/别的 App 抢了前台，后面每一步都会找不到模板。
    这时候必须重放 anchor 这个"落脚点"，它通常是 open 这种可重复的动作。
    """
    did = []
    cleared = _clear_blockers(p)
    if cleared:
        did.append("清弹窗%s" % cleared)
    step = steps[anchor] if 0 <= anchor < len(steps) else {}
    want = (step.get("expect_app") or "").strip()
    if want:
        try:
            cur = foreground(p) or ""
        except Exception:
            cur = ""
        if not cur.startswith(want):
            try:
                p.key("HOME")
                time.sleep(0.8)
            except Exception:
                pass
            try:
                _one_step(p, step)
                time.sleep(float(step.get("wait", 0.8)))
                did.append("重放第%d步把 %s 拉回前台" % (anchor + 1, want))
            except Exception as e:
                did.append("重放第%d步失败：%s" % (anchor + 1, e))
    return did


@app.route("/flow/run", methods=["POST"])
@safe
def flow_run():
    """跑一串步骤，每步做完立刻核对。

    {name, stop_on_fail: true, steps: [
      {do:"open", pkg:"...", expect_app:"com.larus.nova"},
      {do:"tap_template", name:"打电话.png", expect_text:"拨号"},
      ...
    ]}
    """
    d = request.get_json(force=True)
    steps = d.get("steps") or []
    if not steps:
        return jsonify(ok=False, error="没有步骤"), 400
    stop_on_fail = d.get("stop_on_fail", True)
    tries = int(d.get("tries", 8))
    interval = float(d.get("interval", 0.8))
    p = get_phone()

    t_all = time.monotonic()
    results = []
    all_ok = True
    # 整轮重跑：中途失败不是就此放弃，而是清掉障碍后从最近的落脚点接着做完。
    # 这是"人插上线、希望它一定投上屏"的场景，所以要的是最终结果，不是第一个错误。
    resume_rounds = int(d.get("resume_rounds", 3))
    resume_on = bool(d.get("resume", True))
    max_rounds = max(1, resume_rounds)
    rec_map = {}
    round_log = []
    idx = 0
    for round_no in range(max_rounds):
        start_idx = idx
        while idx < len(steps):
            step = steps[idx]
            rec = {"i": idx + 1, "step": step, "action_ok": False, "verify_ok": None,
                   "desc": "", "verify": None, "ms": 0, "round": round_no + 1}
            t0 = time.monotonic()
            try:
                # 点一下没生效是常事（弹窗慢、UI 还没响应）。验证不过就把这一步
                # 重做一遍，而不是直接判死。默认最多 3 次，可用 step.retry 调。
                max_retry = int(step.get("retry", d.get("retry", 3)))
                act_ok = desc = None
                extra = {}
                v_ok = v_info = None
                for attempt in range(max(1, max_retry)):
                    act_ok, desc, extra = _one_step(p, step)
                    if not act_ok:
                        break
                    time.sleep(float(step.get("wait", 0.8)))
                    v_ok, v_info = _check(p, step, tries, interval)
                    if v_ok:
                        break
                    if attempt + 1 < max_retry:
                        rec.setdefault("retried", []).append(
                            {"attempt": attempt + 1, "why": "验证没过，重做这一步"})
                        time.sleep(1.0)
            except Exception as e:
                act_ok, desc, extra = False, "出错：%s: %s" % (type(e).__name__, e), {"fatal": True}
                v_ok, v_info = None, None
            rec["action_ok"] = act_ok
            rec["desc"] = desc
            rec.update({k: v for k, v in (extra or {}).items() if k != "fatal"})
            rec["verify_ok"] = v_ok
            rec["verify"] = v_info
            rec["ms"] = int((time.monotonic() - t0) * 1000)
            ok_here = bool(act_ok) and v_ok is not False
            rec_map[idx] = rec
            results.append(rec)
            if ok_here:
                idx += 1
                continue
            # 这一步没成。能救就救：清弹窗 → 回到最近的落脚点 → 再往下走。
            if not resume_on:
                rec["aborted"] = True
                idx = len(steps)
                break
            # 找落脚点：失败点之前、最近一个有明确特征、且靠后的步骤
            anchor = None
            for j in range(idx - 1, -1, -1):
                if _is_landmark(steps[j]) and rec_map.get(j, {}).get("verify_ok"):
                    anchor = j
                    break
            if anchor is None:
                anchor = 0
            did = _recover_to(p, steps, anchor)
            if did:
                rec["recovered"] = did
            round_log.append({"round": round_no + 1, "failed_at": idx + 1,
                              "why": (desc or "")[:120],
                              "resume_from": anchor + 1,
                              "did": did})
            # 从落脚点的下一步重来。不能连落脚点本身一起重点：
            # 那些点击多半不幂等（比如再点一次「打电话」可能把通话挂了）。
            idx = anchor + 1
            break
        else:
            break
        if idx >= len(steps):
            break

    # 成败按"最后每一步是不是真的都过了"算，而不是"中途有没有失手过"。
    # 中途失败但补救回来了，那就是成功——用户要的是投上屏，不是零瑕疵。
    all_ok = all(rec_map.get(i2, {}).get("action_ok")
                 and rec_map.get(i2, {}).get("verify_ok") is not False
                 for i2 in range(len(steps)))
    out = {"ok": True, "all_pass": all_ok, "results": results,
           "ms": int((time.monotonic() - t_all) * 1000),
           "done": sum(1 for i2 in range(len(steps))
                       if rec_map.get(i2, {}).get("action_ok")
                       and rec_map.get(i2, {}).get("verify_ok") is not False),
           "total": len(steps), "rounds": round_no + 1, "resume_log": round_log}
    if d.get("save_as"):
        fn = os.path.join(FLOWS_DIR, os.path.basename(str(d["save_as"])) + ".json")
        with open(fn, "w") as f:
            json.dump({"name": d["save_as"], "steps": steps}, f,
                      ensure_ascii=False, indent=2)
        out["saved"] = fn
    return jsonify(**out)


@app.route("/flows")
@safe
def flows_list():
    out = []
    if os.path.isdir(FLOWS_DIR):
        for f in sorted(os.listdir(FLOWS_DIR)):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(FLOWS_DIR, f)) as fh:
                        dd = json.load(fh)
                    out.append({"file": f, "name": dd.get("name", f),
                                "steps": len(dd.get("steps", []))})
                except Exception:
                    pass
    return jsonify(ok=True, flows=out)


@app.route("/flow/save", methods=["POST"])
@safe
def flow_save():
    """只保存流程，不执行。"""
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    steps = d.get("steps") or []
    if not name:
        return jsonify(ok=False, error="流程要起个名字"), 400
    if not steps:
        return jsonify(ok=False, error="没有步骤"), 400
    fn = os.path.join(FLOWS_DIR, os.path.basename(name) + ".json")
    with open(fn, "w") as f:
        json.dump({"name": name, "steps": steps}, f, ensure_ascii=False, indent=2)
    return jsonify(ok=True, file=os.path.basename(fn))


@app.route("/flow/<name>")
@safe
def flow_get(name):
    fn = os.path.join(FLOWS_DIR, os.path.basename(name))
    if not os.path.exists(fn):
        return jsonify(ok=False, error="没有这个流程"), 404
    with open(fn) as fh:
        return jsonify(ok=True, flow=json.load(fh))


@app.route("/flow/<name>", methods=["DELETE"])
@safe
def flow_del(name):
    fn = os.path.join(FLOWS_DIR, os.path.basename(name))
    if os.path.exists(fn):
        os.remove(fn)
        return jsonify(ok=True)
    return jsonify(ok=False, error="没有这个流程"), 404


@app.route("/swipe", methods=["POST"])
@safe
def swipe():
    d = request.get_json(force=True)
    p = get_phone()
    w, h = p.resolution()
    if d.get("dir"):
        p.swipe(d["dir"], duration=float(d.get("duration", 0.3)))
        return jsonify(ok=True, dir=d["dir"])
    p1 = (int(float(d["fx1"]) * w), int(float(d["fy1"]) * h))
    p2 = (int(float(d["fx2"]) * w), int(float(d["fy2"]) * h))
    p.swipe(p1, p2, duration=float(d.get("duration", 0.3)))
    return jsonify(ok=True, **{"from": p1, "to": p2})


@app.route("/key", methods=["POST"])
@safe
def key():
    d = request.get_json(force=True)
    p = get_phone()
    p.key(d["name"])
    return jsonify(ok=True, key=d["name"])


@app.route("/text", methods=["POST"])
@safe
def text():
    d = request.get_json(force=True)
    p = get_phone()
    p.text(d["text"])
    return jsonify(ok=True)


@app.route("/open", methods=["POST"])
@safe
def open_app():
    d = request.get_json(force=True)
    p = get_phone()
    cur = p.open(d["pkg"])
    return jsonify(ok=True, focus=cur)


@app.route("/crop", methods=["POST"])
@safe
def crop():
    """在画面上框选一块存成模板。前端传 0~1 的比例坐标。"""
    import cv2
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, error="模板要起个名字"), 400
    if not name.endswith(".png"):
        name += ".png"
    name = os.path.basename(name)

    p = get_phone()
    img = p.snap()
    h, w = img.shape[:2]
    fx1, fx2 = sorted((float(d["fx1"]), float(d["fx2"])))
    fy1, fy2 = sorted((float(d["fy1"]), float(d["fy2"])))
    x1, x2 = int(fx1 * w), int(fx2 * w)
    y1, y2 = int(fy1 * h), int(fy2 * h)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return jsonify(ok=False, error="框太小了，再拉大一点"), 400
    patch = img[y1:y2, x1:x2]
    path = os.path.join(TPL_DIR, name)
    cv2.imwrite(path, patch)
    return jsonify(ok=True, name=name, w=int(patch.shape[1]), h=int(patch.shape[0]),
                   center=[(x1 + x2) // 2, (y1 + y2) // 2])


@app.route("/templates")
@safe
def templates():
    out = []
    for f in sorted(os.listdir(TPL_DIR)):
        if f.lower().endswith(".png"):
            path = os.path.join(TPL_DIR, f)
            out.append({"name": f, "size": os.path.getsize(path),
                        "mtime": int(os.path.getmtime(path))})
    return jsonify(ok=True, templates=out)


@app.route("/template/<name>")
def template_img(name):
    path = os.path.join(TPL_DIR, os.path.basename(name))
    if not os.path.exists(path):
        return jsonify(ok=False, error="没有这个模板"), 404
    return send_file(path, mimetype="image/png")


@app.route("/templates/<name>", methods=["DELETE"])
@safe
def del_template(name):
    path = os.path.join(TPL_DIR, os.path.basename(name))
    if os.path.exists(path):
        os.remove(path)
        return jsonify(ok=True)
    return jsonify(ok=False, error="没有这个模板"), 404


@app.route("/tap_template", methods=["POST"])
@safe
def tap_template():
    d = request.get_json(force=True)
    name = os.path.basename(d["name"])
    th = float(d.get("threshold", 0.75))
    timeout = float(d.get("timeout", 0))
    verify = d.get("verify", True)
    p = get_phone()
    t0 = time.monotonic()
    before = p.snap() if verify else None
    app_before = p.current_app() if verify else None
    pt = p.find(os.path.join(TPL_DIR, name), threshold=th, timeout=timeout)
    if not pt:
        return jsonify(ok=False, error="屏幕上没找到 %s（阈值 %.2f）" % (name, th))
    p.tap(pt[0], pt[1])
    out = dict(ok=True, x=pt[0], y=pt[1], ms=int((time.monotonic() - t0) * 1000))
    if verify:
        time.sleep(0.7)
        after = p.snap()
        out["verify"] = _compare(before, after, pt[0], pt[1])
        out["app_before"] = app_before
        out["app_after"] = p.current_app()
        save_proof(before, after, pt[0], pt[1])
    return jsonify(**out)


@app.route("/find", methods=["POST"])
@safe
def find_only():
    d = request.get_json(force=True)
    name = os.path.basename(d["name"])
    th = float(d.get("threshold", 0.75))
    timeout = float(d.get("timeout", 0))
    p = get_phone()
    pt = p.find(os.path.join(TPL_DIR, name), threshold=th, timeout=timeout)
    if not pt:
        return jsonify(ok=False, error="没找到")
    return jsonify(ok=True, x=pt[0], y=pt[1])


@app.route("/upload", methods=["POST"])
@safe
def upload():
    """直接上传一张模板图片（从别处截的）。"""
    f = request.files.get("file")
    name = os.path.basename((request.form.get("name") or f.filename or "upload.png"))
    if not name.lower().endswith(".png"):
        name = os.path.splitext(name)[0] + ".png"
    path = os.path.join(TPL_DIR, name)
    f.save(path)
    return jsonify(ok=True, name=name)


HTML = r"""<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Airtest 点哪儿</title>
<style>
  :root { --bg:#0f1115; --panel:#181b22; --line:#2a2f3a; --fg:#e8ebf2; --dim:#98a0b0;
          --ok:#2ecc71; --bad:#ff6b60; --acc:#4a9eff; --warn:#f5a623; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }
  header { display:flex; align-items:center; gap:14px; padding:12px 18px;
           border-bottom:1px solid var(--line); background:var(--panel); position:sticky; top:0; z-index:20; }
  header h1 { font-size:16px; margin:0; font-weight:700; }
  #status { color:var(--dim); font-size:12.5px; flex:1; }
  .wrap { display:flex; gap:20px; padding:18px; align-items:flex-start; }
  .left { flex:0 0 auto; position:relative; }
  #screen { display:block; max-height:82vh; width:auto; border-radius:12px;
            border:2px solid var(--line); cursor:crosshair; user-select:none; }
  #screen.pick { border-color:var(--acc); box-shadow:0 0 0 3px rgba(74,158,255,.22); }
  .panel { flex:1 1 400px; display:flex; flex-direction:column; gap:14px; max-width:480px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px; }
  .step { display:flex; gap:10px; align-items:flex-start; margin-bottom:12px; }
  .step .num { flex:0 0 26px; height:26px; border-radius:50%; background:var(--acc); color:#04121f;
               font-weight:800; font-size:14px; display:flex; align-items:center; justify-content:center; }
  .step .txt { flex:1; }
  .step .txt b { color:#fff; }
  .step .txt .sub { color:var(--dim); font-size:13px; }
  .done .num { background:var(--ok); }
  button { background:#252a34; color:var(--fg); border:1px solid var(--line); border-radius:9px;
           padding:9px 14px; cursor:pointer; font-size:14px; font-family:inherit; }
  button:hover { background:#2f3542; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  button.primary { background:var(--acc); border-color:var(--acc); color:#04121f; font-weight:700; }
  button.primary:hover { background:#63aaff; }
  button.go { background:var(--ok); border-color:var(--ok); color:#04200f; font-weight:700; }
  button.go:hover { background:#4bdc8b; }
  button.sm { padding:5px 10px; font-size:12.5px; border-radius:7px; }
  button.danger { color:var(--bad); }
  input[type=text], input[type=number] { background:#12151b; color:var(--fg);
      border:1px solid var(--line); border-radius:8px; padding:9px 11px; font-size:14px; width:100%; font-family:inherit; }
  input[type=text]:focus { outline:2px solid var(--acc); outline-offset:-1px; }
  .row { display:flex; gap:9px; flex-wrap:wrap; align-items:center; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:9px; }
  .hint { color:var(--dim); font-size:13px; }
  .kbd { background:#0b0e13; border:1px solid var(--line); border-radius:5px; padding:1px 6px;
         font-size:12.5px; color:#cfd6e4; }
  #cropBox { display:none; }
  #cropBox.on { display:block; }
  #cropPrev { max-height:88px; max-width:150px; border-radius:6px; border:1px solid var(--line);
              background:#000; object-fit:contain; }
  .tpl { display:flex; align-items:center; gap:10px; padding:9px; border:1px solid var(--line);
         border-radius:10px; margin-bottom:8px; background:#14171e; }
  .tpl img { height:52px; max-width:92px; object-fit:contain; background:#000; border-radius:6px; }
  .tpl .nm { flex:1; font-size:13px; word-break:break-all; color:#cfd6e4; }
  .tpl .acts { display:flex; gap:6px; }
  #log { font:12px/1.65 ui-monospace,Menlo,monospace; color:var(--dim); max-height:160px;
         overflow:auto; white-space:pre-wrap; }
  .logline.ok { color:var(--ok); } .logline.bad { color:var(--bad); }
  #sel { position:absolute; border:2px dashed var(--acc); background:rgba(74,158,255,.16);
         display:none; pointer-events:none; border-radius:3px; }
  #toast { position:fixed; left:50%; bottom:28px; transform:translateX(-50%) translateY(20px);
           background:#232935; border:1px solid var(--line); color:#fff; padding:11px 20px;
           border-radius:10px; font-size:14px; opacity:0; transition:.22s; pointer-events:none;
           box-shadow:0 8px 26px rgba(0,0,0,.5); z-index:60; max-width:80vw; }
  #toast.on { opacity:1; transform:translateX(-50%) translateY(0); }
  #toast.bad { border-color:var(--bad); }
  #toast.ok { border-color:var(--ok); }
  .help { display:none; } .help.on { display:block; }
  #vcard { display:block; }
  .shots { display:flex; gap:10px; align-items:center; }
  .shots figure { margin:0; flex:1; text-align:center; }
  .shots img { width:100%; border-radius:8px; border:1px solid var(--line); background:#000; }
  .shots figcaption { font-size:12px; color:var(--dim); margin-top:5px; }
  #vres { font-size:13.5px; margin-top:10px; padding:10px; border-radius:8px; line-height:1.55; }
  #vres.ok { background:rgba(46,204,113,.12); border:1px solid rgba(46,204,113,.45); }
  #vres.no { background:rgba(245,166,35,.12); border:1px solid rgba(245,166,35,.45); }

  .help ol { margin:8px 0 0; padding-left:20px; } .help li { margin-bottom:6px; }
</style>
</head><body>
<header>
  <h1>Airtest 点哪儿</h1>
  <span id="status">连接中…</span>
  <label class="hint"><input type="checkbox" id="auto" checked> 自动刷新</label>
  <button class="sm" onclick="refresh(true)">刷新画面</button>
  <button class="sm" onclick="document.querySelector('.help').classList.toggle('on')">怎么用</button>
</header>

<div class="wrap">
  <div class="left">
    <img id="screen" src="/screen.jpg" alt="手机画面">
    <div id="sel"></div>
  </div>

  <div class="panel">
    <div class="card help">
      <b>30 秒说明</b>
      <ol>
        <li>左边是你的手机屏幕，1 秒多刷新一次。</li>
        <li><b>要让它点某个按钮</b>：在左边那块按钮上<b>按住鼠标拖一个框</b>圈住它，
            松手后在右边起个名字，点"保存模板"。以后点"点它"就会自动找这个按钮并点。</li>
        <li><b>只想点现在这个位置</b>：在画面上<b>单击一下</b>（不要拖动），就直接点那里。</li>
        <li>找不到按钮时，把"相似度"调低一点（比如 0.65）。</li>
      </ol>
    </div>

    <div class="card">
      <div class="step" id="step1">
        <div class="num">1</div>
        <div class="txt">
          <b>在左边画面上，把要点的东西圈起来</b>
          <div class="sub">按住鼠标拖一个框，松手就出现在下面</div>
        </div>
      </div>

      <div id="cropBox">
        <div class="row" style="margin-bottom:10px">
          <img id="cropPrev" alt="选区预览">
          <div class="hint" id="cropInfo" style="flex:1"></div>
        </div>
        <input type="text" id="tplname" placeholder="给它起个名字，例如：发送按钮" style="margin-bottom:10px">
        <div class="grid2">
          <button class="primary" onclick="saveCrop(false)">保存模板</button>
          <button class="go" onclick="saveCrop(true)">保存并点它</button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="step" id="step2">
        <div class="num">2</div>
        <div class="txt">
          <b>点"点它"，让它自己找并点</b>
          <div class="sub">相似度越低越容易找到，但可能找错</div>
        </div>
      </div>
      <div class="row" style="margin-bottom:10px">
        <span class="hint">相似度</span>
        <input type="number" id="th" value="0.75" step="0.05" min="0.3" max="0.99" style="width:84px">
        <label class="hint"><input type="checkbox" id="waitit"> 等它出现再点（最多10秒）</label>
      </div>
      <div id="tpls" class="hint">还没有模板</div>
      <hr style="border:0;border-top:1px solid var(--line);margin:12px 0">
      <div class="row">
        <input type="file" id="upfile" accept="image/*" style="font-size:12.5px;flex:1">
        <button class="sm" onclick="uploadTpl()">上传现成图片当模板</button>
      </div>
    </div>

    <div class="card" id="vcard">
      <b style="font-size:13px;color:var(--dim)">验证：怎么确认真的点到了</b>
      <div class="shots" style="margin-top:10px">
      <div class="shots">
        <figure><img id="vBefore" alt="点击前"><figcaption>点击前</figcaption></figure>
        <figure><img id="vAfter" alt="点击后"><figcaption>点击后（红圈=点的位置）</figcaption></figure>
      </div>
      <div id="vres"></div>
      <hr style="border:0;border-top:1px solid var(--line);margin:12px 0">
      <b style="font-size:13px;color:var(--dim)">更硬的验证：是不是真的做成了</b>
      <div class="hint" style="margin:6px 0 9px">填你期望看到的结果，点完它去核对。</div>
      <div class="row" style="margin-bottom:8px">
        <input type="text" id="expApp" placeholder="期望前台包名，如 com.larus.nova" style="flex:1">
      </div>
      <div class="row" style="margin-bottom:9px">
        <input type="text" id="expText" placeholder="期望屏幕上出现的文字，如 发送" style="flex:1">
      </div>
      <div class="row">
        <button class="sm primary" onclick="semCheck()">现在就核对</button>
        <button class="sm" onclick="fillCurrent()">填入当前状态</button>
      </div>
      <div id="semres"></div>
    </div>

    <div class="card">
      <div class="step" id="step4">
        <div class="num">4</div>
        <div class="txt">
          <b>流程：一串动作，每步都验证</b>
          <div class="sub">比如：启动豆包 → 点打电话 → 点投屏 → 点开始投屏</div>
        </div>
      </div>
      <div id="steps" class="hint" style="margin-bottom:10px">还没有步骤。</div>
      <div class="row" style="margin-bottom:9px">
        <select id="addDo" style="flex:1;background:#12151b;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:9px">
          <option value="open">启动 App</option>
          <option value="tap_template">点模板</option>
          <option value="tap_text">点文字（按屏幕文字）</option>
          <option value="tap_xy">点坐标</option>
          <option value="key">按键</option>
          <option value="text">输入文字</option>
          <option value="swipe">滑动</option>
          <option value="sleep">等待</option>
        </select>
        <button class="sm" onclick="addStep()">加一步</button>
      </div>
      <div class="row" style="margin-bottom:9px">
        <input type="text" id="flowName" placeholder="流程名，如 通话投屏" style="flex:1">
        <button class="sm" onclick="saveFlow()">保存</button>
      </div>
      <div class="row">
        <button class="go" onclick="runFlow()">▶ 运行并逐步验证</button>
        <button class="sm" onclick="loadFlows()">刷新流程列表</button>
      </div>
      <div id="flowList" class="hint" style="margin-top:10px"></div>
      <div id="flowRes" style="margin-top:10px"></div>
    </div>

    <div class="card">
      <div class="step" id="step3">
        <div class="num">3</div>
        <div class="txt">
          <b>其它操作</b>
          <div class="sub">在画面上单击 = 直接点那个位置</div>
        </div>
      </div>
      <div class="grid2">
        <button onclick="act('swipe',{dir:'up'})">上滑</button>
        <button onclick="act('swipe',{dir:'down'})">下滑</button>
        <button onclick="act('key',{name:'home'})">回桌面</button>
        <button onclick="act('key',{name:'back'})">返回</button>
      </div>
      <div class="row" style="margin-top:9px">
        <input type="text" id="pkg" placeholder="包名，例如 com.larus.nova" style="flex:1">
        <button onclick="act('open',{pkg:document.getElementById('pkg').value})">启动</button>
      </div>
      <div class="row" style="margin-top:9px">
        <input type="text" id="txt" placeholder="要输入的文字" style="flex:1">
        <button onclick="act('text',{text:document.getElementById('txt').value})">输入</button>
      </div>
    </div>

    <div class="card">
      <div class="row" style="justify-content:space-between;margin-bottom:8px">
        <b style="font-size:13px;color:var(--dim)">日志</b>
        <button class="sm" onclick="document.getElementById('log').textContent=''">清空</button>
      </div>
      <div id="log"></div>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
const img = document.getElementById('screen');
const sel = document.getElementById('sel');
let drag = null, crop = null;

function toast(msg, kind) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'on ' + (kind || '');
  clearTimeout(window._tt);
  window._tt = setTimeout(() => { t.className = kind || ''; }, 2600);
}

function log(msg, kind) {
  const el = document.getElementById('log');
  const d = document.createElement('div');
  d.className = 'logline ' + (kind || '');
  d.textContent = new Date().toTimeString().slice(0,8) + '  ' + msg;
  el.appendChild(d);
  el.scrollTop = el.scrollHeight;
}

function refresh(force) {
  img.src = '/screen.jpg?t=' + Date.now() + (force ? '&fresh=1' : '');
}

function relPos(e) {
  const r = img.getBoundingClientRect();
  return { fx: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
           fy: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) };
}

img.addEventListener('mousedown', e => {
  e.preventDefault();
  drag = { start: relPos(e), moved: false };
  sel.style.display = 'block';
});

img.addEventListener('mousemove', e => {
  if (!drag) return;
  const p = relPos(e);
  if (Math.abs(p.fx - drag.start.fx) > 0.012 || Math.abs(p.fy - drag.start.fy) > 0.012) drag.moved = true;
  drawSel(drag.start, p);
});

window.addEventListener('mouseup', async e => {
  if (!drag) return;
  const p = relPos(e), d = drag;
  drag = null;
  const moved = d.moved && Math.abs(p.fx-d.start.fx) > 0.012 && Math.abs(p.fy-d.start.fy) > 0.012;
  if (moved) {
    crop = { start: d.start, end: p };
    drawSel(d.start, p);
    showCrop(crop);
  } else {
    sel.style.display = 'none';
    await act('tap_verify', { fx: p.fx, fy: p.fy });
  }
});

function drawSel(a, b) {
  const r = img.getBoundingClientRect();
  const x1 = Math.min(a.fx,b.fx)*r.width, x2 = Math.max(a.fx,b.fx)*r.width;
  const y1 = Math.min(a.fy,b.fy)*r.height, y2 = Math.max(a.fy,b.fy)*r.height;
  sel.style.left = x1+'px'; sel.style.top = y1+'px';
  sel.style.width = (x2-x1)+'px'; sel.style.height = (y2-y1)+'px';
  sel.style.display = 'block';
}

function showCrop(c) {
  document.getElementById('cropBox').classList.add('on');
  // 从当前显示的画面里裁一块做预览
  const cv = document.createElement('canvas');
  const sx = Math.min(c.start.fx, c.end.fx) * img.naturalWidth;
  const sy = Math.min(c.start.fy, c.end.fy) * img.naturalHeight;
  const sw = Math.abs(c.end.fx - c.start.fx) * img.naturalWidth;
  const sh = Math.abs(c.end.fy - c.start.fy) * img.naturalHeight;
  cv.width = Math.max(1, sw); cv.height = Math.max(1, sh);
  cv.getContext('2d').drawImage(img, sx, sy, sw, sh, 0, 0, cv.width, cv.height);
  document.getElementById('cropPrev').src = cv.toDataURL();
  document.getElementById('cropInfo').textContent =
    `选区 ${Math.round(sw)}×${Math.round(sh)} 像素`;
  const nm = document.getElementById('tplname');
  if (!nm.value) nm.value = '按钮' + (document.querySelectorAll('.tpl').length + 1);
  document.getElementById('step1').classList.add('done');
  nm.focus(); nm.select();
}

async function post(url, body) {
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                              body: JSON.stringify(body||{})});
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || '失败');
  return j;
}

async function act(kind, body) {
  try {
    const j = await post('/'+kind, body);
    let msg = kind + ' 成功';
    if (kind === 'tap') msg = `已点击 (${j.x}, ${j.y})  ${j.ms}ms`;
    if (kind === 'tap_verify') {
      msg = j.verify.changed
        ? `已点击 (${j.x}, ${j.y})，画面有变化 ✓  ${j.ms}ms`
        : `已点击 (${j.x}, ${j.y})，但画面没怎么变  ${j.ms}ms`;
      showVerify(j.verify, `单击 (${j.x}, ${j.y})`);
    }
    if (kind === 'swipe') msg = '已滑动';
    if (kind === 'key') msg = '已发送 ' + j.key;
    if (kind === 'open') msg = '已启动';
    if (kind === 'text') msg = '已输入';
    log(msg, 'ok'); toast(msg, 'ok');
    setTimeout(() => refresh(true), 260);
  } catch (e) {
    log(kind + ' 失败：' + e.message, 'bad'); toast(kind + ' 失败：' + e.message, 'bad');
  }
}

async function saveCrop(alsoTap) {
  if (!crop) return toast('先在左边画面上拖一个框', 'bad');
  const name = document.getElementById('tplname').value.trim();
  if (!name) return toast('先给它起个名字', 'bad');
  try {
    const j = await post('/crop', {name, fx1:crop.start.fx, fy1:crop.start.fy,
                                   fx2:crop.end.fx, fy2:crop.end.fy});
    log(`模板已保存：${j.name}（${j.w}×${j.h}）`, 'ok');
    toast('已保存 ' + j.name, 'ok');
    document.getElementById('tplname').value = '';
    document.getElementById('cropBox').classList.remove('on');
    sel.style.display = 'none';
    crop = null;
    await loadTpls();
    if (alsoTap) await tapTpl(j.name);
  } catch (e) {
    log('保存失败：' + e.message, 'bad'); toast('保存失败：' + e.message, 'bad');
  }
}

function showVerify(v, title) {
  const card = document.getElementById('vcard');
  card.classList.add('on');
  document.getElementById('vBefore').src = '/proof/before.jpg?t=' + Date.now();
  document.getElementById('vAfter').src = '/proof/after.jpg?t=' + Date.now();
  const box = document.getElementById('vres');
  let lines = [];
  if (title) lines.push('<b>' + title + '</b>');
  if (v.changed) {
    box.className = 'ok';
    lines.push('✅ <b>点击生效了</b>：画面发生了变化');
  } else {
    box.className = 'no';
    lines.push('⚠️ <b>画面几乎没变</b>，可能：点空了 / 按钮本来无反应 / 需要更长的加载时间');
  }
  lines.push('整体像素差 ' + v.diff + '（>1.2 算有变化）');
  if (v.local_diff !== undefined) lines.push('点击位置附近差异 ' + v.local_diff + '（>3 算有变化）');
  if (v.changed_pct !== undefined) lines.push('明显变化的画面占比 ' + v.changed_pct + '%');
  if (v.why) lines.push(v.why);
  box.innerHTML = lines.join('<br>');
}

let flowSteps = [];

function stepLabel(s) {
  if (s.do === 'open') return '启动 ' + s.pkg;
  if (s.do === 'tap_template') return '点模板 ' + s.name;
  if (s.do === 'tap_text') return '点文字「' + s.text + '」';
  if (s.do === 'tap_xy') return '点坐标 (' + s.fx + ',' + s.fy + ')';
  if (s.do === 'key') return '按键 ' + s.name;
  if (s.do === 'text') return '输入 ' + s.text;
  if (s.do === 'swipe') return '滑动 ' + (s.dir || 'up');
  if (s.do === 'sleep') return '等 ' + (s.seconds || 1) + 's';
  return JSON.stringify(s);
}

function expectLabel(s) {
  const parts = [];
  if (s.expect_app) parts.push('前台=' + s.expect_app);
  if (s.expect_text) parts.push('文字含「' + s.expect_text + '」');
  if (s.expect_template) parts.push('出现 ' + s.expect_template);
  if (s.expect_not_text) parts.push('消失「' + s.expect_not_text + '」');
  return parts.length ? '  ⟹ 验：' + parts.join(' + ') : '  （不验证）';
}

function renderSteps() {
  const box = document.getElementById('steps');
  if (!flowSteps.length) {
    box.innerHTML = '<span class="hint">还没有步骤。选上面的类型，点「加一步」。</span>';
    return;
  }
  box.innerHTML = flowSteps.map((s, i) => `
    <div class="tpl">
      <span class="nm"><b>${i+1}.</b> ${stepLabel(s)}<br>
        <span style="color:var(--warn);font-size:12px">${expectLabel(s)}</span></span>
      <span class="acts">
        <button class="sm" onclick="editStep(${i})">改</button>
        <button class="sm danger" onclick="delStep(${i})">删</button>
      </span>
    </div>`).join('');
}

function addStep() {
  const kind = document.getElementById('addDo').value;
  const s = {do: kind, wait: 1.0};
  if (kind === 'open') {
    s.pkg = prompt('要启动的包名：', 'com.larus.nova');
    if (!s.pkg) return;
    s.expect_app = s.pkg;
  } else if (kind === 'tap_template') {
    s.name = prompt('点哪个模板？填完整文件名（可在上面列表看到）', '打电话.png');
    if (!s.name) return;
    const e = prompt('点完期望出现哪个模板？（留空=只验证点击，不验证结果）', '');
    if (e) s.expect_template = e;
  } else if (kind === 'tap_text') {
    s.text = prompt('屏幕上要点的文字（如 立即开始）', '立即开始');
    if (!s.text) return;
    const e = prompt('点完期望出现什么文字？（留空不验证）', '正在共享屏幕');
    if (e) s.expect_text = e;
  } else if (kind === 'tap_xy') {
    const fx = prompt('横向比例 0~1：', '0.5'); if (fx === null) return;
    const fy = prompt('纵向比例 0~1：', '0.5'); if (fy === null) return;
    s.fx = parseFloat(fx); s.fy = parseFloat(fy);
  } else if (kind === 'key') {
    s.name = prompt('按键名（home/back/enter/...）', 'home'); if (!s.name) return;
  } else if (kind === 'text') {
    s.text = prompt('要输入的文字', ''); if (!s.text) return;
  } else if (kind === 'swipe') {
    s.dir = prompt('方向 up/down/left/right', 'up'); if (!s.dir) return;
  } else if (kind === 'sleep') {
    s.seconds = parseFloat(prompt('等几秒', '1') || '1');
  }
  flowSteps.push(s);
  renderSteps();
}

function editStep(i) {
  const s = flowSteps[i];
  const e = prompt('期望前台包名（留空不改）', s.expect_app || '');
  if (e !== null && e !== '') s.expect_app = e;
  const t = prompt('期望屏幕文字（留空不改）', s.expect_text || '');
  if (t !== null && t !== '') s.expect_text = t;
  const p = prompt('期望出现模板名（留空不改）', s.expect_template || '');
  if (p !== null && p !== '') s.expect_template = p;
  renderSteps();
}

function delStep(i) { flowSteps.splice(i, 1); renderSteps(); }

async function saveFlow() {
  const name = document.getElementById('flowName').value.trim();
  if (!name) return toast('先给流程起个名字', 'bad');
  if (!flowSteps.length) return toast('还没有步骤', 'bad');
  const r = await fetch('/flow/save', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name, steps: flowSteps})});
  const j = await r.json();
  if (!j.ok) return toast('保存失败：' + j.error, 'bad');
  toast('已保存流程：' + name, 'ok');
  loadFlows();
}

async function runFlow() {
  if (!flowSteps.length) return toast('还没有步骤', 'bad');
  const box = document.getElementById('flowRes');
  box.innerHTML = '<div class="hint">正在跑，每步都要核对，请稍候…</div>';
  try {
    const r = await fetch('/flow/run', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({steps: flowSteps, stop_on_fail: true, tries: 8, interval: 0.8})});
    const j = await r.json();
    if (!j.ok) { box.innerHTML = '<div style="color:var(--bad)">' + j.error + '</div>'; return; }
    let html = j.all_pass
      ? '<div style="color:var(--ok);font-weight:700;margin-bottom:8px">全部通过 ✓  ' + j.done + '/' + j.total + ' 步，共 ' + j.ms + 'ms</div>'
      : '<div style="color:var(--bad);font-weight:700;margin-bottom:8px">第 ' + (j.results.find(x=>x.aborted)||{i:'?'}).i + ' 步没通过 ✗</div>';
    j.results.forEach(r => {
      const mark = (r.action_ok && r.verify_ok !== false) ? '✅' : '❌';
      html += `<div style="padding:7px 0;border-top:1px solid var(--line);font-size:13px">
        ${mark} <b>${r.i}.</b> ${r.desc} <span style="color:var(--dim)">(${r.ms}ms)</span>`;
      const v = r.verify || {};
      (v.checks || []).forEach(c => {
        const p = c.pass ? '✅' : '❌';
        let det = c.kind === 'app' ? ('前台 ' + (c.actual || '未知'))
                : c.kind === 'text' ? ('文字 ' + ((c.actual||[]).join('/') || '没看到'))
                : ('模板 ' + (c.at ? ('在 ' + c.at.join(',') + ' 找到') : '没找到'));
        html += `<div style="color:${c.pass?'var(--ok)':'var(--bad)'};padding-left:16px">${p} ${c.kind}：期望 ${c.expect} → ${det}</div>`;
      });
      if (v.skipped) html += `<div style="color:var(--dim);padding-left:16px">未设期望值，跳过验证</div>`;
      html += '</div>';
    });
    box.innerHTML = html;
    setTimeout(() => refresh(true), 300);
  } catch (e) {
    box.innerHTML = '<div style="color:var(--bad)">运行失败：' + e.message + '</div>';
  }
}

async function loadFlows() {
  const r = await fetch('/flows'); const j = await r.json();
  const box = document.getElementById('flowList');
  if (!j.flows.length) { box.innerHTML = '<span class="hint">还没有保存的流程</span>'; return; }
  box.innerHTML = j.flows.map(f => `
    <div class="tpl">
      <span class="nm">${f.name}（${f.steps} 步）</span>
      <span class="acts">
        <button class="sm" onclick="useFlow('${f.file}')">载入</button>
        <button class="sm danger" onclick="delFlow('${f.file}')">删</button>
      </span>
    </div>`).join('');
}

async function useFlow(file) {
  const r = await fetch('/flow/' + encodeURIComponent(file));
  const j = await r.json();
  if (!j.ok) return toast('读取失败', 'bad');
  flowSteps = j.flow.steps || [];
  document.getElementById('flowName').value = j.flow.name || '';
  renderSteps(); toast('已载入，可以改了', 'ok');
}

async function delFlow(file) {
  await fetch('/flow/' + encodeURIComponent(file), {method:'DELETE'});
  loadFlows(); toast('已删除');
}

async function semCheck() {
  const app = document.getElementById('expApp').value.trim();
  const txt = document.getElementById('expText').value.trim();
  if (!app && !txt) return toast('先填期望的包名或文字', 'bad');
  try {
    const r = await fetch('/verify_after_tap', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({expect_app: app, expect_text: txt, wait: 1})});
    const j = await r.json();
    const box = document.getElementById('semres');
    let lines = [];
    if (!j.checks || !j.checks.length) lines.push('没填期望值');
    (j.checks || []).forEach(c => {
      if (c.kind === 'app') {
        lines.push((c.pass ? '✅ ' : '❌ ') + '前台 App：期望 <b>' + c.expect +
                   '</b>，实际 <b>' + (c.actual || '未知') + '</b>');
      } else {
        lines.push((c.pass ? '✅ ' : '❌ ') + '屏幕文字包含 <b>' + c.expect + '</b>' +
                   (c.pass ? '' : '（没看到）'));
      }
    });
    const pass = j.all_pass;
    box.className = pass ? 'ok' : 'no';
    box.style.cssText = 'font-size:13px;margin-top:9px;padding:9px;border-radius:8px;line-height:1.6';
    box.innerHTML = lines.join('<br>');
    log(pass ? '语义验证通过' : '语义验证未通过', pass ? 'ok' : 'bad');
    toast(pass ? '验证通过：真的做成了' : '验证未通过：结果和预期不符', pass ? 'ok' : 'bad');
  } catch (e) { toast('核对失败：' + e.message, 'bad'); }
}

async function fillCurrent() {
  try {
    const r = await fetch('/state'); const j = await r.json();
    if (j.app) document.getElementById('expApp').value = j.app.split('/')[0];
    toast('已填入当前前台 App', 'ok');
  } catch (e) { toast('读取失败', 'bad'); }
}

async function loadTpls() {
  const r = await fetch('/templates'); const j = await r.json();
  const box = document.getElementById('tpls');
  if (!j.templates.length) {
    box.innerHTML = '<span class="hint">还没有模板。在左边圈一块，就能存成模板。</span>';
    return;
  }
  box.innerHTML = j.templates.map(t => `
    <div class="tpl">
      <img src="/template/${encodeURIComponent(t.name)}?t=${t.mtime}" alt="">
      <span class="nm">${t.name}</span>
      <span class="acts">
        <button class="go sm" onclick="tapTpl('${t.name.replace(/'/g,"\\'")}')">点它</button>
        <button class="sm" onclick="findTpl('${t.name.replace(/'/g,"\\'")}')">试找</button>
        <button class="sm danger" onclick="delTpl('${t.name.replace(/'/g,"\\'")}')">删</button>
      </span>
    </div>`).join('');
}

async function tapTpl(name) {
  const th = parseFloat(document.getElementById('th').value) || 0.75;
  const waitit = document.getElementById('waitit').checked;
  toast('正在找 ' + name + ' …');
  try {
    const j = await post('/tap_template', {name, threshold: th, timeout: waitit ? 10 : 0});
    const v = j.verify || {};
    const msg = `找到并点击 ${name} → (${j.x}, ${j.y})  ${j.ms}ms`
              + (v.changed ? ' 画面已变化 ✓' : ' 但画面没变');
    log(msg, v.changed ? 'ok' : 'bad');
    toast(v.changed ? '点击成功，画面已变化 ✓' : '点了但画面没变，可能没点中', v.changed ? 'ok' : 'bad');
    showVerify(v, `模板 ${name} @ (${j.x}, ${j.y})`);
    document.getElementById('step2').classList.add('done');
    setTimeout(() => refresh(true), 260);
  } catch (e) {
    log(`没点到 ${name}：${e.message}`, 'bad');
    toast(`屏幕上没找到 ${name}，把相似度调低试试`, 'bad');
  }
}

async function findTpl(name) {
  const th = parseFloat(document.getElementById('th').value) || 0.75;
  try {
    const j = await post('/find', {name, threshold: th});
    log(`找到 ${name} 在 (${j.x}, ${j.y})`, 'ok'); toast(`找到了，在 (${j.x}, ${j.y})`, 'ok');
  } catch (e) {
    log(`没找到 ${name}（相似度 ${th}）`, 'bad'); toast(`没找到，把相似度调低点`, 'bad');
  }
}

async function delTpl(name) {
  await fetch('/templates/' + encodeURIComponent(name), {method:'DELETE'});
  log('已删除 ' + name); loadTpls();
}

async function uploadTpl() {
  const f = document.getElementById('upfile').files[0];
  if (!f) return toast('先选一张图片', 'bad');
  const fd = new FormData(); fd.append('file', f);
  const r = await fetch('/upload', {method:'POST', body: fd});
  const j = await r.json();
  if (j.ok) { log('上传成功：' + j.name, 'ok'); toast('上传成功', 'ok'); loadTpls(); }
  else { log('上传失败：' + j.error, 'bad'); toast('上传失败', 'bad'); }
}

async function pollInfo() {
  try {
    const r = await fetch('/info'); const j = await r.json();
    if (!j.ok) { document.getElementById('status').textContent = '手机未连接'; return; }
    let app = (j.app || '');
    const m = app.match(/mCurrentFocus=Window\{\S+ u0 ([\w.\/]+)/);
    app = m ? m[1] : '';
    document.getElementById('status').textContent =
      `${j.serial} · ${j.resolution[0]}×${j.resolution[1]}` + (app ? ' · ' + app : '');
  } catch (e) {
    document.getElementById('status').textContent = '连接中…';
  }
}

img.onload = () => {
  if (!crop) return;
  // 画面刷新后保持选区位置（比例不变，像素位置自动跟随）
  drawSel(crop.start, crop.end);
};

setInterval(() => { if (document.getElementById('auto').checked) refresh(); }, 1300);
setInterval(pollInfo, 5000);
loadTpls(); pollInfo(); loadFlows(); renderSteps();
</script>
</body></html>
"""


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
    print("airweb 启动中… 打开 http://<板子IP>:%d" % port)
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
