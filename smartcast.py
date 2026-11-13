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

# adb 用绝对路径：systemd 起的服务 PATH 里没有 platform-tools。
ADB = "/usr/lib/android-sdk/platform-tools/adb"


def pick_serial():
    """挑一个真能用的设备。

    为什么不能写死 892e20c9：
      手机靠板子 USB 供电，实测会出现「重新枚举」——同一个物理口，
      设备从 2717:ff48（MTP+ADB）变成 2717:ff40（纯 MTP，无 adb 接口），
      `adb devices` 直接空，所有接口 500。

    所以现在两条腿都要认：
      - USB：序列号 892e20c9
      - WiFi：192.168.1.119:5555
    优先 USB（快），不通就换 WiFi。都不通返回环境变量/默认值，
    让调用方自己报"手机没连上 adb"。
    """
    usb = os.environ.get("PHONE_SERIAL", "892e20c9")
    wifi = os.environ.get("PHONE_WIFI", "192.168.1.119:5555")
    forced = os.environ.get("PHONE_FORCE")     # 想固定某个就设它
    if forced:
        return forced
    try:
        p = subprocess.run([ADB, "devices"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=12)
        states = {}
        for line in p.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] in ("device", "offline"):
                states[parts[0]] = parts[1]
    except Exception:
        return usb
    if states.get(usb) == "device":
        return usb
    if states.get(wifi) == "device":
        return wifi
    # 状态是 offline 的也别用
    return usb

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
        return subprocess.run([ADB, "-s", serial] + list(args),
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
    """从当前界面走到豆包通话页。

    定位方式按精确度排优先级，能用贵的不省：
      1. 无障碍树按名字找（系统报的，像素级准确）
      2. 模板匹配（无障碍树是空的时候兜底）
      3. 视觉模型（前两个都没有，且坐标必须落在真实节点内）

    绝不"大概位置点一下"。找不到就不点，宁可重启重来。
    """
    import uitap

    def in_call():
        return "RealtimeCall" in foreground(serial)

    if in_call():
        return True

    NAME = "打电话"
    for i in range(1, 4):
        # ── 1. 无障碍树 ──
        ns = uitap.dump(serial)
        hit = uitap.find(ns, NAME)
        if hit:
            n = hit[0]
            step("第%d轮：无障碍树找到「%s」@(%d,%d) bounds=%s"
                 % (i, NAME, n["cx"], n["cy"], n["bounds"]))
            uitap.tap_node(serial, n)
        else:
            # ── 2. 模板兜底 ──
            grab(serial, grid=False)
            tpl = find_tpl(HOME_TPL, 0.6)
            if tpl:
                tap_xy(serial, tpl[0], tpl[1])
                step("第%d轮：无障碍树没有名字，模板命中 @(%d,%d) %.3f"
                     % (i, tpl[0], tpl[1], tpl[2]))
            else:
                # ── 3. 模型兜底，坐标必须落在真实可点节点内 ──
                img = grab(serial, grid=True)
                a, raw = (None, "")
                if img:
                    a, raw = ask(img, "屏幕上没有「打电话」按钮。当前是什么页面？"
                                      "该 back 退回首页还是已经可以点别的？")
                if not a:
                    step("第%d轮：无障碍树空、模板没命中、模型没答好（%s）"
                         % (i, (raw or "")[:40]))
                    _restart_home(serial)
                    time.sleep(1.5)
                    continue
                if a.get("action") == "tap":
                    mx, my = int(a.get("x", 0)), int(a.get("y", 0))
                    # 硬约束：模型点必须落在某个真实且可点的节点里
                    inside = [n for n in uitap.dump(serial)
                              if n["clickable"] and n["enabled"]
                              and n["bounds"][0] <= mx <= n["bounds"][2]
                              and n["bounds"][1] <= my <= n["bounds"][3]]
                    if inside:
                        inside.sort(key=lambda n: n["area"])
                        n = inside[0]
                        step("第%d轮：模型点位 (%d,%d) 落在节点「%s」内，改点该节点中心"
                             % (i, mx, my, (n["text"] or n["desc"])[:20]))
                        uitap.tap_node(serial, n)
                    else:
                        step("第%d轮：模型点位 (%d,%d) 不在任何可点节点内，拒绝点击"
                             % (i, mx, my))
                        _restart_home(serial)
                        time.sleep(1.5)
                        continue
                else:
                    step("第%d轮：模型判断 page=%s，重启回首页"
                         % (i, a.get("page")))
                    _restart_home(serial)
                    time.sleep(1.5)
                    continue

        for _ in range(12):
            time.sleep(0.8)
            if in_call():
                return True
    return in_call()


def _open_cast(serial, step, wait_secs=16.0):
    """在通话页把投屏打开。

    「共享屏幕」是豆包给这颗按钮的无障碍描述（实测确认）。
    用名字找而不是看图，好处是：
      - 图标换了样子也能找到
      - 找的是"共享屏幕"，绝不会误点成"挂断通话"（两者挨着）
      - 找到就直接点它自己的中心坐标

    点过之后只等结果，绝不再点第二次：万一状态已经变了，
    同一个位置可能变成「停止投屏」，点下去会把投屏关掉。
    """
    import uitap

    if casting_owner(serial):
        return casting_owner(serial)

    # 界面上如果已经有「停止投屏」，说明开着，别再点
    if uitap.find(uitap.dump(serial), "停止共享", "停止投屏"):
        step("界面上是「停止投屏」，说明已经在投，不动")
        return casting_owner(serial) or "already"

    clicked = False
    for i in range(1, 9):          # 图标有时要几秒才绘制出来
        ns = uitap.dump(serial)
        hit = uitap.find(ns, "共享屏幕") or uitap.find(ns, "投屏")
        if hit:
            n = hit[0]
            step("第%d次：无障碍树找到「共享屏幕」@(%d,%d) bounds=%s"
                 % (i, n["cx"], n["cy"], n["bounds"]))
            uitap.tap_node(serial, n)
            clicked = True
            break
        time.sleep(1)

    if not clicked:
        # 无障碍树里真没有（自绘界面）。模型给的坐标必须落在真实可点节点内。
        img = grab(serial, grid=True)
        if not img:
            step("截不到图")
            return ""
        a, raw = ask(img, "这是通话页。请找到底部的投屏图标（一个方框里带向上箭头的"
                          "小图标），给出它的中心坐标。找不到就 action=fail。")
        if not a or a.get("action") != "tap":
            step("无障碍树和模型都没定位到投屏图标（%s）"
                 % ((raw or "")[:50] if not a else a.get("what", "")[:40]))
            return ""
        mx, my = int(a.get("x", 0)), int(a.get("y", 0))
        inside = [n for n in uitap.dump(serial)
                  if n["clickable"] and n["enabled"]
                  and n["bounds"][0] <= mx <= n["bounds"][2]
                  and n["bounds"][1] <= my <= n["bounds"][3]]
        if not inside:
            step("模型点位 (%d,%d) 不在任何可点节点内，拒绝点击" % (mx, my))
            return ""
        inside.sort(key=lambda n: n["area"])
        n = inside[0]
        step("模型点位 (%d,%d) 落在节点「%s」内，改点其中心 @(%d,%d)"
             % (mx, my, (n["text"] or n["desc"])[:20], n["cx"], n["cy"]))
        uitap.tap_node(serial, n)

    # 只等，不再点图标。权限框有时要好几秒才出来。
    import time as _t
    deadline = _t.monotonic() + wait_secs
    while _t.monotonic() < deadline:
        owner = casting_owner(serial)
        if owner:
            return owner
        # 授权框按钮文案实测是「立即开始」
        got = uitap.click(serial, "立即开始", wait=0.8)
        if got:
            step("点「立即开始」授权 @(%d,%d)" % (got["cx"], got["cy"]))
        else:
            _t.sleep(1.2)
    return casting_owner(serial)


# ── 主流程 ─────────────────────────────────────────────────
CAST_LOCK = "/tmp/_cast.lock"


def run(serial=None, tries=3, log=print):
    """完整投屏流程。

    全程持有一把文件锁：Tasker 的 /cast 和 keepcast 守护可能同时触发，
    两边都点屏幕会互相踩（一个在等授权、另一个已经开始点下一步）。
    锁拿不到说明别人正在跑，直接返回，让对方干完。
    """
    import fcntl

    with open(CAST_LOCK, "w") as _lf:
        try:
            fcntl.flock(_lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"ok": False, "why": "已有投屏流程在跑，本次让路",
                    "steps": []}
        return _run_locked(serial=serial, tries=tries, log=log)


def _run_locked(serial=None, tries=3, log=print):
    serial = serial or pick_serial()
    rec = {"ok": False, "why": "", "steps": []}

    def step(m):
        rec["steps"].append(m)
        log(m)

    # ── 第一步：USB 用途 ──
    # 插线后先放充电动画（约 15 秒），然后弹「USB 用于 / 仅限充电 / 传输文件」。
    # 必须选「传输文件」，否则 USB 只供电不出数据，adb 直接看不到设备。
    # 这一步失败不阻断：WiFi adb 可能还在，后面靠 pick_serial 会切过去。
    try:
        ur = do_step("usb", serial=serial, log=log)
        step("USB：%s" % ur.get("why", ""))
    except Exception as e:
        step("USB 步骤出错（继续）：%s" % str(e)[:60])

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
        # 只有确认没在投屏才 force-stop。正在投屏时杀进程 = 掐断画面。
        if not casting_owner(serial):
            _adb(serial, "shell", "am force-stop " + APP)
            time.sleep(1.2)
        else:
            step("检测到投屏开着，只用 am start 拉起，不杀进程")
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
        # 验证本身出错，不能当成"通过"。
        # 以前这里写 ok=True，结果模型调用失败（返回非 JSON）时
        # 流程报成功、屏幕却是黑的。现在退回到系统状态这一条硬证据。
        owner2 = casting_owner(serial)
        if owner2:
            rec["ok"] = True
            rec["why"] = ("画面确认跳过（%s），但系统仍报投屏 owner=%s"
                          % (str(e)[:50], owner2))
        else:
            rec["ok"] = False
            rec["why"] = ("画面确认出错（%s），且系统里查不到投屏会话"
                          % str(e)[:50])
        step(rec["why"])

    # ── 最后一步：成功就回桌面 ──
    # 为什么要回：投屏共享的是"手机屏幕"，通话页上全是豆包的按钮和头像，
    # 对方看到的就是这些。回 HOME 之后共享画面变成桌面，才是用户想要的。
    #
    # 注意：按 HOME 不会中断投屏（共享的是屏幕，不是某个应用），
    # 但必须按完回头确认 owner 还在 —— 万一某版系统把它当"应用退到后台"
    # 掐掉，得立刻知道，不能报成功。
    if rec["ok"]:
        _adb(serial, "shell", "input keyevent KEYCODE_HOME")
        time.sleep(1.5)
        left = casting_owner(serial)
        cur = foreground(serial)
        if left:
            step("已按 HOME 回桌面（%s），投屏仍在" % (cur or "未知"))
        else:
            rec["ok"] = False
            rec["why"] = "回桌面后投屏没了（按 HOME 把会话掐断了）"
            step(rec["why"])

    return rec


# ── 给 Tasker 用的 HTTP 接口 ───────────────────────────────
STEPS = {
    "usb":      "等插线弹窗，选「文件传输」",
    "wake":     "唤醒屏幕并解锁",
    "popup":    "关掉「USB 用于」弹窗",
    "launch":   "启动豆包（不杀进程）",
    "restart":  "杀干净再启动豆包，落到首页",
    "home_ok":  "确认当前在豆包首页",
    "tap_call": "点「打电话」，确认进了通话页",
    "tap_cast": "点「开启投屏」并授权，确认系统有投屏会话",
    "verify":   "确认画面真的在共享",
    "desktop":  "回到桌面",
    "status":   "只读：报告当前前台/投屏状态",
}


def do_step(name, serial=None, log=print, force=False):
    """单个原子操作。每个都自己验证，返回 {ok, now, ...}。"""
    serial = serial or pick_serial()
    r = {"step": name, "ok": False, "why": ""}

    def fg():
        return foreground(serial)

    # 设备真的在吗？不在就别假装成功。
    def alive():
        out = _adb(serial, "shell", "echo ok", timeout=8)
        return "ok" in out

    if name in ("usb", "wake", "popup", "launch", "restart", "home_ok",
                "tap_call", "tap_cast", "verify", "desktop"):
        if not alive():
            r["why"] = "手机没连上 adb（adb devices 为空）"
            log("[step:%s] FAIL  %s" % (name, r["why"]))
            return r

    if name == "usb":
        # 插线后手机会先放 15 秒左右的充电动画，然后才弹「选择充电方式」，
        # 里面是「仅限充电 / 传输文件 / 传输照片 …」。必须点「传输文件」，
        # 否则 USB 只出电不出数据。
        #
        # 为什么不能像旧 popup 那样一看到 USB 就按 BACK：
        #   这个选择框按 BACK 等于"什么都没选"，多半回落到仅充电，
        #   后面整条链全废。必须点到「传输文件」。
        import uitap

        def picked():
            """已经选过文件传输了吗？看系统属性，比看界面可靠。"""
            st = _adb(serial, "shell", "getprop sys.usb.state").strip()
            cfg = _adb(serial, "shell", "getprop sys.usb.config").strip()
            return "mtp" in st or "mtp" in cfg

        # 弹窗正开着就别短路——哪怕 usb.state 已经是 mtp（插拔中途、
        # 或用户手点过又弹出来），留着这个框会挡住后面所有点击。
        dlg_up = "UsbModeChooser" in fg() or "传输文件" in " ".join(
            uitap.names(uitap.dump(serial), clickable_only=False))

        if picked() and not dlg_up:
            r["ok"] = True
            r["why"] = "USB 已经是文件传输模式（%s）" % (
                _adb(serial, "shell", "getprop sys.usb.state").strip() or "?")
            r["focus"] = fg()
            log("[step:usb] OK  %s" % r["why"])
            return r

        # 主动把选择框叫出来，不靠盲等。
        #
        # 为什么要这样：插线那一刻的弹窗只有一次机会，抓不到就没了。
        # 而这个 Activity 可以直接 am start 出来（实测可行）：
        #   com.android.settings/.connecteddevice.usb.UsbModeChooserActivity
        # 叫出来之后点「传输文件」，等于替用户做了选择。
        #
        # 坑（实测踩了 150 秒）：三个选项的文字节点是 clickable=false，
        # 真正接收点击的是外面那层容器。用默认 clickable_only=True 找
        # 「传输文件」永远找不到，循环空转到超时。必须 clickable_only=False，
        # 点文字中心坐标 —— 容器覆盖着它，照样生效。
        #
        # 另一个坑：文案是「传输文件」，不是「文件传输」。
        TEXTS = ("传输文件", "文件传输")
        got = None

        def try_pick():
            for t in TEXTS:
                lb = " ".join(uitap.names(uitap.dump(serial),
                                          clickable_only=False))
                if t in lb:
                    return uitap.click(serial, t, exact=True,
                                       clickable_only=False, wait=1.8)
            return None

        # 1) 弹窗本来就开着（插线后最常见）——直接点
        got = try_pick()

        # 2) 没开就自己叫出来，再点
        if not got:
            _adb(serial, "shell",
                 "am start -n com.android.settings/"
                 ".connecteddevice.usb.UsbModeChooserActivity")
            time.sleep(2.0)
            got = try_pick()

        if got:
            time.sleep(1.5)
            r["ok"] = picked()
            r["why"] = ("已选「%s」，USB=%s" % (
                (got["text"] or got["desc"])[:10],
                _adb(serial, "shell", "getprop sys.usb.state").strip())
                if r["ok"] else "点了「传输文件」但 USB 仍是 %s" % (
                    _adb(serial, "shell", "getprop sys.usb.state").strip() or "空"))
        else:
            r["ok"] = picked()
            st = _adb(serial, "shell", "getprop sys.usb.state").strip()
            r["why"] = ("叫出了选择框也没点中「传输文件」，但 USB 已是 %s" % st
                        if r["ok"] else
                        "叫出选择框也没找到「传输文件」（界面：%s）"
                        % " ".join(uitap.names(uitap.dump(serial),
                                               clickable_only=False))[:60])
        r["focus"] = fg()

    elif name == "wake":
        _adb(serial, "shell", "input keyevent KEYCODE_WAKEUP")
        _adb(serial, "shell", "wm dismiss-keyguard")
        time.sleep(1.2)
        r["ok"] = True
        r["why"] = "已唤醒"
        r["focus"] = fg()

    elif name == "popup":
        # 两类弹窗会挡住后面所有点击：
        #   1. MIUI 插线的「USB 用于」——按 BACK 关
        #   2. 系统权限框（通知/麦克风/屏幕录制）——必须点允许，BACK 关不掉
        # 实测踩过：重启豆包后弹「是否允许发送通知」，脚本不认它，
        # 后面所有点击全落在权限框上，整轮报废。
        import uitap
        closed, allowed = 0, 0
        for i in range(4):
            ns = uitap.dump(serial)
            labels = uitap.names(ns)
            joined = " ".join(labels)

            # 权限框：出现"允许"类按钮就直接点允许
            if "允许" in joined or "Allow" in joined:
                got = (uitap.click(serial, "始终允许", wait=1.2)
                       or uitap.click(serial, "仅在使用中允许", wait=1.2)
                       or uitap.click(serial, "允许", wait=1.2))
                if got:
                    allowed += 1
                    log("[popup] 点「%s」授权 @(%d,%d)"
                        % ((got["text"] or got["desc"])[:12], got["cx"], got["cy"]))
                    continue

            # USB 弹窗：含「用于」/「用途」的 USB 提示
            if "USB" in joined and ("用于" in joined or "用途" in joined
                                    or "连接" in joined):
                _adb(serial, "shell", "input keyevent KEYCODE_BACK")
                closed += 1
                time.sleep(0.9)
                continue

            r["ok"] = True
            if closed == 0 and allowed == 0:
                r["why"] = "没有弹窗"
            else:
                r["why"] = "关掉 %d 个 USB 弹窗、批准 %d 个权限" % (closed, allowed)
            break
        else:
            r["ok"] = False
            r["why"] = "弹窗关不掉（%s）" % (" ".join(uitap.names(uitap.dump(serial)))[:60])
        r["focus"] = fg()

    elif name == "launch":
        # 注意：不 force-stop！豆包没在前台时只做启动
        if fg().startswith(APP):
            r["ok"] = True
            r["why"] = "豆包已在前台"
        else:
            _adb(serial, "shell",
                 "monkey -p %s -c android.intent.category.LAUNCHER 1" % APP)
            for _ in range(20):
                time.sleep(1)
                if fg().startswith(APP):
                    break
            r["ok"] = fg().startswith(APP)
            r["why"] = "已启动" if r["ok"] else "启动失败（%s）" % (fg() or "未知")
        r["focus"] = fg()

    elif name == "restart":
        # 重启会 force-stop，可能把正在工作的投屏杀掉，所以默认拒绝。
        # 调用方确实要重启（比如页面卡在别处）才传 force=1。
        if casting_owner(serial) and not force:
            r["ok"] = False
            r["why"] = ("正在投屏，拒绝重启豆包（会杀掉投屏）。"
                        "确实要重启请加 ?force=1")
            log("[step:restart] FAIL  %s" % r["why"])
            return r
        ok = False
        for i in range(3):
            ok = bool(_restart_home(serial))
            if ok:
                break
            log("[restart] 第%d次没起来，重试" % (i + 1))
            time.sleep(2)
        r["ok"] = ok
        r["why"] = "已重启到首页" if ok else "起不来（%s）" % (fg() or "未知")
        r["focus"] = fg()

    elif name == "home_ok":
        # 按名字判断，不靠模板。模板换主题/字号就失效。
        import uitap
        hit = None
        for i in range(4):
            ns = uitap.dump(serial)
            hit = uitap.find(ns, "打电话")
            if hit:
                break
            time.sleep(1.2)
        r["ok"] = bool(hit)
        if hit:
            n = hit[0]
            r["why"] = ("首页，无障碍树找到「打电话」@(%d,%d) bounds=%s"
                        % (n["cx"], n["cy"], n["bounds"]))
        else:
            r["why"] = "没找到「打电话」按钮（当前 %s）" % (fg() or "未知")
        r["focus"] = fg()

    elif name == "tap_call":
        # 幂等：已经在通话页就立刻返回，绝不再重启豆包
        # （Tasker 可能重复触发；重启会 force-stop，把正在工作的投屏杀掉）
        if "RealtimeCall" in fg():
            r["ok"] = True
            r["why"] = "已经在通话页，没动"
            r["focus"] = fg()
            log("[step:tap_call] OK  %s" % r["why"])
            return r
        ok = False
        for i in range(3):
            ok = bool(_enter_call(serial, lambda m: log("[tap_call] " + m)))
            if ok:
                break
            log("[tap_call] 第%d次没进通话页，再来一次" % (i + 1))
            time.sleep(2)
        r["ok"] = ok
        r["why"] = "已进入通话页" if ok else "没进通话页（%s）" % (fg() or "未知")
        r["focus"] = fg()

    elif name == "tap_cast":
        owner = casting_owner(serial)
        if owner:
            r["ok"] = True
            r["why"] = "投屏本来就开着（owner %s）" % owner
        else:
            for i in range(3):
                owner = _open_cast(serial, lambda m: log("[tap_cast] " + m))
                if owner:
                    break
                log("[tap_cast] 第%d次没开成，重来" % (i + 1))
                time.sleep(1.5)
            r["ok"] = bool(owner)
            r["why"] = "系统确认投屏" if r["ok"] else "点过但系统没会话"
        r["owner"] = owner
        r["focus"] = fg()

    elif name == "verify":
        try:
            import aiverify
            vr = aiverify.verify(serial)
            r["ok"] = bool(vr["ok"])
            r["why"] = vr.get("reason", "")
        except Exception as e:
            r["ok"] = False
            r["why"] = "验证出错: %s" % str(e)[:80]
        r["owner"] = casting_owner(serial)
        r["focus"] = fg()

    elif name == "desktop":
        _adb(serial, "shell", "input keyevent KEYCODE_HOME")
        time.sleep(1.5)
        cur = fg()
        r["ok"] = bool(cur) and not cur.startswith(APP)
        r["why"] = "已回桌面（%s）" % cur if r["ok"] else "还停在 %s" % (cur or "未知")
        r["focus"] = cur
        r["owner"] = casting_owner(serial)

    elif name == "status":
        r["ok"] = True
        r["why"] = "只读"
        r["focus"] = fg()
        r["owner"] = casting_owner(serial)
        r["casting"] = bool(r["owner"])

    else:
        r["why"] = "未知步骤 %r，可用：%s" % (name, ",".join(STEPS))

    log("[step:%s] %s  %s" % (name, "OK" if r["ok"] else "FAIL", r["why"]))
    return r


def serve(port=8130, log=None):
    """Tasker 只需要一个 HTTP Request：POST http://<板子IP>:8130/cast

    返回 JSON：{"ok":true,...} 就是投屏已确认开好。
    GET /health 用来探板子在不在。

    单步接口（每一步自己验证，失败就是失败，不会假成功）：
      POST /step/tap_call     点「打电话」并确认进了通话页
      POST /step/tap_cast     点「开启投屏」并确认系统有会话
      POST /step/verify       确认画面真在共享
      POST /step/desktop      回桌面
      GET  /step/status       只读状态
      GET  /steps             列出所有可用步骤
    通用形式：POST /do?step=tap_call  或 POST /step/tap_call?serial=xxx
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
            p = self.path.split("?")[0].rstrip("/") or "/"
            if p == "/health":
                return self._send(200, {
                    "ok": True, "app": APP,
                    "casting": casting_owner(pick_serial())})
            if p == "/steps":
                return self._send(200, {"ok": True, "steps": STEPS})
            if p == "/step/status":
                return self._send(200, do_step("status", log=out))
            self._send(404, {"ok": False, "why": "未知路径",
                             "try": ["/health", "/steps", "/step/status"]})

        def do_POST(self):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            p, q = u.path.rstrip("/"), parse_qs(u.query)
            serial = (q.get("serial") or [None])[0]

            # 单步
            name = None
            if p.startswith("/step/"):
                name = p[len("/step/"):]
            elif p == "/do":
                name = (q.get("step") or [None])[0]
            if name:
                try:
                    raw_force = (q.get("force") or ["0"])[0]
                    r = do_step(name, serial=serial, log=out,
                                force=str(raw_force).lower() in ("1", "true", "yes"))
                except Exception as e:
                    r = {"step": name, "ok": False,
                         "why": "执行出错: %s" % e}
                return self._send(200 if r.get("ok") else 500, r)

            if p == "/cast":
                try:
                    r = run(serial=serial, log=out)
                except Exception as e:
                    r = {"ok": False, "why": "执行出错: %s" % e, "steps": []}
                return self._send(200 if r.get("ok") else 500, r)

            self._send(404, {"ok": False, "why": "未知路径",
                             "try": ["/cast", "/steps", "/step/tap_call"]})

        def log_message(self, *a):
            pass

    out("等 Tasker 调用：POST http://0.0.0.0:%d/cast" % port)
    out("单步接口已开：/step/{wake,popup,launch,restart,home_ok,"
        "tap_call,tap_cast,verify,desktop,status}")
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
