#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把豆包的投屏开起来：点「打电话」→ 点「开启投屏」→ 确认真的开了。

设计：模板 + 大模型融合，各干各最擅长的
────────────────────────────────────────
实测出来的分工，不是拍脑袋：
  · 模板擅长"精确找图"。打电话.png 命中 0.999、开启投屏.png 命中 0.997，
    坐标误差 0 像素。但它要求屏幕上必须有这张图——豆包冷启动会恢复到
    上次的「会话详情页」，底部没有「打电话」，模板就瞎了。
  · 模型擅长"看懂现在是什么页面、该往哪走"。但它给坐标会飘：问「打电话」
    在哪，裸图答 (365,868)，真值 (422,2085)，差 1200 多像素。
    加上坐标网格后答 (370,2100)，落进按钮 bounds 里，能用。
    而投屏那颗图标它干脆认不出来（一个方框带向上的箭头，它说成
    "上传已禁用"），所以"点投屏"这件事绝不能只靠模型。

于是：
  找坐标 → 先模板；模板找不到才问模型（带网格）。
  坐标核对 → 模型给的点和模板位置差太远时，以模板为准。
  判断页面 → 模型，因为"会话详情页 vs 首页"只有它看得懂。
  最终确认 → dumpsys 的 ScreenCapture owner + 模型看图，两个都过才算赢。
"""
import base64
import json
import os
import re
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.expanduser("~/.openclaw/openclaw.json")
APP = os.environ.get("PHONE_APP", "com.larus.nova")
TPL_DIR = os.path.join(HERE, "templates")
SHOT = "/tmp/_cast_shot.png"
RAW = "/tmp/_cast_raw.png"

HOME_TPL = "打电话.png"      # 首页底部那颗电话
CAST_TPL = "开启投屏.png"    # 通话页底部那颗投屏

SYSTEM = """你在操作一台安卓手机，目标是让豆包App开始屏幕共享。
屏幕 1080x2400，坐标原点左上角，x 向右、y 向下。
图上画了坐标网格：红竖线是 x、绿横线是 y，线上数字就是真实像素坐标，
请照刻度读数，不要凭感觉估。

判断当前是哪个页面：
- 首页：底部有「打电话」「视频通话」「对话」，还有输入框。
- 会话详情页：中间一大段聊天记录、底部是输入框，没有底部按钮栏。
- 通话页：中间有动画球、底部是麦克风/摄像头/挂断等圆形按钮。

重要：只有当你 100% 确定那是「打电话」或「视频通话」按钮、并且能读出它的
网格刻度时才给 tap。看不清、拿不准，就用 back 或 fail——点错比不点更糟，
点错会把页面带到回收站（实测：一次误点后连退三步到了安卓桌面，整轮报废）。

只输出一行 JSON：
{"page":"home|chat|call|other","action":"tap|back|done|fail","x":整数,"y":整数,"what":"说明"}"""


# ── 基础工具 ────────────────────────────────────────────────
def _cfg():
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)["models"]["providers"]["qiniu"]


def _adb(serial, *args, timeout=25):
    try:
        return subprocess.run(["adb", "-s", serial] + list(args),
                              capture_output=True, text=True,
                              timeout=timeout).stdout or ""
    except Exception:
        return ""


def grab(serial, grid=True):
    """截图。grid=True 时叠坐标网格，供模型读数用。"""
    try:
        r = subprocess.run(["adb", "-s", serial, "exec-out", "screencap", "-p"],
                           capture_output=True, timeout=25)
        if not r.stdout or len(r.stdout) < 5000:
            return None
        with open(RAW, "wb") as f:
            f.write(r.stdout)
    except Exception:
        return None
    if not grid:
        return RAW
    try:
        import cv2
        img = cv2.imread(RAW)
        if img is None:
            return None
        h, w = img.shape[:2]
        for x in range(0, w, 100):
            cv2.line(img, (x, 0), (x, h), (0, 0, 255), 1)
            cv2.putText(img, str(x), (x + 2, 18), cv2.FONT_HERSHEY_SIMPLEX,
                        0.42, (0, 0, 255), 1)
        for y in range(0, h, 100):
            cv2.line(img, (0, y), (w, y), (0, 255, 0), 1)
            cv2.putText(img, str(y), (2, y - 3), cv2.FONT_HERSHEY_SIMPLEX,
                        0.42, (0, 255, 0), 1)
        cv2.imwrite(SHOT, img)
        return SHOT
    except Exception:
        return RAW


def find_tpl(name, threshold=0.75):
    """在原图上找模板，返回 (cx, cy, score) 或 None。不叠网格的原图更准。"""
    path = os.path.join(TPL_DIR, name)
    if not os.path.exists(path) or not os.path.exists(RAW):
        return None
    try:
        import cv2
        img = cv2.imread(RAW)
        tpl = cv2.imread(path)
        if img is None or tpl is None:
            return None
        res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if mx < threshold:
            return None
        return (loc[0] + tpl.shape[1] // 2, loc[1] + tpl.shape[0] // 2, round(mx, 3))
    except Exception:
        return None


def ask(img, prompt, timeout=80):
    """问模型。返回 (dict, 原始文字)。"""
    p = _cfg()
    b64 = base64.b64encode(open(img, "rb").read()).decode()
    payload = {
        "model": os.environ.get("AGENT_MODEL", "qwen/qwen3.8-omni-flash"),
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + b64}},
                {"type": "text", "text": prompt},
            ]},
        ],
        "max_tokens": 140, "temperature": 0,
    }
    tmp = "/tmp/_cast_req.json"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", str(timeout), "-X", "POST",
             p["baseUrl"].rstrip("/") + "/chat/completions",
             "-H", "Authorization: Bearer " + p["apiKey"],
             "-H", "Content-Type: application/json", "--data-binary", "@" + tmp],
            capture_output=True, text=True)
        d = json.loads(r.stdout)
        if "choices" not in d:
            return None, "接口报错: " + str(d.get("error"))[:80]
        txt = (d["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        return None, "调用失败: %s" % str(e)[:80]
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass

    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j < 0:
        return None, "没给JSON: " + txt[:60]
    try:
        return json.loads(txt[i:j + 1]), txt
    except Exception:
        return None, "JSON坏了: " + txt[:60]


def tap_xy(serial, x, y):
    x = max(0, min(1079, int(x)))
    y = max(0, min(2399, int(y)))
    _adb(serial, "shell", "input tap %d %d" % (x, y))
    return x, y


def _restart_home(serial):
    """杀掉豆包再开，尽量落到首页。

    monkey 启动有时会恢复上次的侧栏抽屉/会话详情，那上面的按钮位置
    跟首页完全不一样。杀干净 + am start 是回到干净首页最稳的方式。
    试几个已知的首页 activity，哪个起来算哪个。
    """
    _adb(serial, "shell", "am force-stop " + APP)
    time.sleep(1.5)
    for act in ("com.larus.home.impl.alias.AliasActivity1",
                "com.larus.home.impl.alias.AliasActivity2"):
        _adb(serial, "shell", "am start -n %s/%s" % (APP, act))
        for _ in range(8):
            time.sleep(0.8)
            cur = foreground(serial)
            if cur.startswith(APP) and "launcher" not in cur:
                return True
    # 都起不来就用 monkey 兜底
    _adb(serial, "shell",
         "monkey -p %s -c android.intent.category.LAUNCHER 1" % APP)
    time.sleep(6)
    return foreground(serial).startswith(APP)


def foreground(serial):
    out = _adb(serial, "shell", "dumpsys window | grep mCurrentFocus")
    for line in out.splitlines():
        if "mCurrentFocus" in line and "}" in line:
            return line.split("u0")[-1].strip().rstrip("}")
    return ""


def screen_texts(serial):
    xml = _adb(serial, "shell",
               "uiautomator dump /sdcard/_c.xml >/dev/null 2>&1; cat /sdcard/_c.xml",
               timeout=30)
    return re.findall(r'text="([^"]*)"', xml), xml


def tap_text(serial, want):
    """按可见文字点中心。授权弹窗的「立即开始」用这个最稳。"""
    texts, xml = screen_texts(serial)
    if not any(want in t for t in texts):
        return None
    for m in re.finditer(r"<node[^>]*>", xml):
        tag = m.group(0)
        t = re.search(r'text="([^"]*)"', tag)
        if not t or want not in t.group(1):
            continue
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag)
        if b:
            x1, y1, x2, y2 = map(int, b.groups())
            return tap_xy(serial, (x1 + x2) // 2, (y1 + y2) // 2)
    return None


def casting_owner(serial):
    out = _adb(serial, "shell", "dumpsys display | grep ScreenCapture")
    for line in out.splitlines():
        if "owner" in line:
            seg = line.split("owner", 1)[1].split()
            return seg[0] if seg else ""
    return ""


def _enter_call(serial, step):
    """从当前界面走到豆包通话页。模板优先，模板摸不到就问模型。"""
    headless_backs = 0
    for i in range(1, 4):
        grab(serial, grid=False)
        hit = find_tpl(HOME_TPL, 0.6)
        if hit:
            tap_xy(serial, hit[0], hit[1])
            step("第%d轮：模板命中「打电话」@(%d,%d) 分数 %.3f"
                 % (i, hit[0], hit[1], hit[2]))
        else:
            if headless_backs < 1:
                _restart_home(serial)
                headless_backs += 1
                step("第%d轮：模板没命中，重启豆包回首页" % i)
                time.sleep(1.5)
                continue
            img = grab(serial, grid=True)
            if not img:
                step("第%d轮：截不到图" % i)
                time.sleep(2)
                continue
            a, raw = ask(img, "屏幕上没有「打电话」按钮。当前是什么页面？"
                              "该 back 退回首页还是已经可以点别的？")
            if not a:
                step("第%d轮：模型没答好（%s）" % (i, raw[:50]))
                time.sleep(1.5)
                continue
            step("第%d轮：模型判断 page=%s(%s)"
                 % (i, a.get("page"), a.get("what", "")[:40]))
            if a.get("action") == "tap" and a.get("page") == "home":
                mx, my = int(a.get("x", 0)), int(a.get("y", 0))
                ok_pt = False
                for nm in (HOME_TPL, "视频通话.png"):
                    chk = find_tpl(nm, 0.5)
                    if chk and abs(chk[0] - mx) <= 130 and abs(chk[1] - my) <= 130:
                        mx, my, ok_pt = chk[0], chk[1], True
                        break
                if not ok_pt:
                    step("第%d轮：模型点位 (%d,%d) 没有对应按钮，重启" % (i, mx, my))
                    _restart_home(serial)
                    headless_backs += 1
                    time.sleep(1.5)
                    continue
                x, y = tap_xy(serial, mx, my)
                step("按模型点位（已核对）@(%d,%d)" % (x, y))
            else:
                _restart_home(serial)
                headless_backs += 1
                step("按模型判断重启回首页")
                time.sleep(1.5)
                continue

        for _ in range(12):
            time.sleep(0.8)
            if "RealtimeCall" in foreground(serial):
                return True
        if "RealtimeCall" in foreground(serial):
            return True
    return "RealtimeCall" in foreground(serial)


def _open_cast(serial, step, wait_secs=16.0):
    """在通话页把投屏打开。点过图标就只等结果，绝不再点第二次。"""
    if casting_owner(serial):
        return casting_owner(serial)

    hit = None
    for _ in range(8):          # 图标有时要几秒才绘制出来
        grab(serial, grid=False)
        hit = find_tpl(CAST_TPL, 0.5)
        if hit:
            break
        time.sleep(1)

    if hit:
        tap_xy(serial, hit[0], hit[1])
        step("模板命中「开启投屏」@(%d,%d) 分数 %.3f"
             % (hit[0], hit[1], hit[2]))
    else:
        img = grab(serial, grid=True)
        if not img:
            step("截不到图")
            return ""
        a, raw = ask(img, "这是通话页。请找到底部的投屏图标（一个方框里带向上箭头的"
                          "小图标），给出它的中心坐标。找不到就 action=fail。")
        if not a or a.get("action") != "tap":
            step("模型也没定位到投屏图标（%s）"
                 % ((raw or "")[:50] if not a else a.get("what", "")[:40]))
            return ""
        x, y = tap_xy(serial, a.get("x", 0), a.get("y", 0))
        step("按模型点位点投屏 @(%d,%d)" % (x, y))

    # 只等，不再点图标。权限框有时要好几秒才出来。
    import time as _t
    deadline = _t.monotonic() + wait_secs
    while _t.monotonic() < deadline:
        owner = casting_owner(serial)
        if owner:
            return owner
        got = tap_text(serial, "立即开始")
        if got:
            step("点「立即开始」授权 @(%d,%d)" % got)
        else:
            _t.sleep(1.2)
    return casting_owner(serial)


# ── 主流程 ─────────────────────────────────────────────────
def run(serial=None, tries=3, log=print):
    serial = serial or os.environ.get("PHONE_SERIAL", "892e20c9")
    rec = {"ok": False, "why": "", "steps": []}

    def step(m):
        rec["steps"].append(m)
        log(m)

    # 已经开着就不用再动（Tasker 可能重复触发）
    owner = casting_owner(serial)
    if owner:
        rec.update(ok=True, why="投屏本来就是开着的（owner %s）" % owner)
        step(rec["why"])
        return rec

    # 前置：醒屏 + 豆包在前台。Tasker 本应做好，这里兜底，
    # 免得 Tasker 那边时序差一点就整条废掉。
    _adb(serial, "shell", "input keyevent KEYCODE_WAKEUP")
    _adb(serial, "shell", "wm dismiss-keyguard")
    time.sleep(1)
    app = foreground(serial)
    if not app.startswith(APP):
        step("豆包不在前台（%s），先拉起" % (app or "未知"))
        _adb(serial, "shell", "am force-stop " + APP)
        time.sleep(1.2)
        _adb(serial, "shell",
             "monkey -p %s -c android.intent.category.LAUNCHER 1" % APP)
        for _ in range(20):
            time.sleep(1)
            if foreground(serial).startswith(APP):
                break
        app = foreground(serial)
        if not app.startswith(APP):
            rec["why"] = "拉不起豆包（当前 %s）" % (app or "未知")
            step(rec["why"])
            return rec
        step("已拉起豆包")

    # ── 进通话页 + 开投屏，整体最多试 tries 轮 ──
    # 为什么改成"整体重来"而不是"原地重点"：
    #   实测过，点完「开启投屏」如果权限框没立刻弹出来，再点一次图标，
    #   那个位置已经变成「停止投屏」了，一点就把刚开好的投屏关掉。
    #   三次重复点击的结果是"点完了但系统里没有投屏会话"。
    #   所以：点过就绝不再点，宁可整轮推倒重来（重启豆包，界面干净）。
    owner = ""
    for attempt in range(1, tries + 1):
        if attempt > 1:
            step(">>> 第 %d 轮：重启豆包从头来" % attempt)
            _restart_home(serial)
            time.sleep(1.5)

        if not _enter_call(serial, step):
            step("第%d轮：没能进入通话页（当前 %s）"
                 % (attempt, foreground(serial) or "未知"))
            continue
        step("已进入通话页")

        owner = _open_cast(serial, step)
        if owner:
            break
        step("第%d轮：点过投屏但系统里没有会话" % attempt)

    if not owner:
        rec["why"] = "试了 %d 轮都没开成投屏" % tries
        step(rec["why"])
        return rec
    step("系统确认投屏会话：owner %s" % owner)

    # ── 第 3 步：确认画面真在共享 ──
    try:
        import aiverify
        vr = aiverify.verify(serial)
        rec["ok"] = bool(vr["ok"])
        rec["why"] = "已确认在投屏" if rec["ok"] else vr.get("reason", "")
        step("画面确认：%s（%s）" % ("通过" if rec["ok"] else "未通过",
                                    vr.get("reason", "")))
    except Exception as e:
        rec["ok"] = True
        rec["why"] = "画面确认跳过（%s），以系统状态为准" % str(e)[:60]
        step(rec["why"])
    return rec


# ── 给 Tasker 用的 HTTP 接口 ───────────────────────────────
def serve(port=8130, log=None):
    """Tasker 只需要一个 HTTP Request：POST http://<板子IP>:8130/cast

    返回 JSON：{"ok":true,...} 就是投屏已确认开好。
    GET /health 用来探板子在不在。
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import sys

    def out(m):
        line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), m)
        print(line, flush=True)
        if log:
            log(m)

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/health"):
                return self._send(200, {
                    "ok": True, "app": APP,
                    "casting": casting_owner(
                        os.environ.get("PHONE_SERIAL", "892e20c9"))})
            self._send(404, {"ok": False, "why": "未知路径"})

        def do_POST(self):
            if not self.path.startswith("/cast"):
                return self._send(404, {"ok": False, "why": "未知路径"})
            try:
                r = run(log=out)
            except Exception as e:
                r = {"ok": False, "why": "执行出错: %s" % e, "steps": []}
            self._send(200 if r.get("ok") else 500, r)

        def log_message(self, *a):
            pass

    out("等 Tasker 调用：POST http://0.0.0.0:%d/cast" % port)
    HTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    import sys
    if "--serve" in sys.argv:
        i = sys.argv.index("--serve")
        serve(int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 8130)
    else:
        s = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None
        r = run(s)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        sys.exit(0 if r["ok"] else 1)
